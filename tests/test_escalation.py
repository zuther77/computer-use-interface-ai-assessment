"""Escalation & handoff tests (Phase 8; DECISIONS.md §9a–9d).

The pause/resume signal is mocked — no real manual interaction. Covers:
request persistence and content completeness, the re-observe-and-verify
branch (checkpoint match → continue; mismatch → a *new* intervention),
each explicit override path (continue / mark_complete / abandon), the
human-turn log, and the file-based resume-signal mechanism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bankops.agent.loop import ActionLogEntry, RunResult, RunStatus
from bankops.artifact.models import Checkpoint
from bankops.escalation.handoff import (
    FileResumeWaiter,
    HandoffAction,
    HandoffManager,
    ResumeSignal,
)
from bankops.escalation.human_log import HumanTurnLog
from bankops.escalation.intervention import (
    InterventionRequest,
    escalate_discovery_stop,
    escalate_replay_failure,
    raise_intervention,
)
from bankops.perception.base import (
    Observation,
    ObservedElement,
    PerceptionAdapter,
)
from bankops.replay.results import FailureResult


class FakeAdapter(PerceptionAdapter):
    """A fake headed 'session' whose page state the tests can mutate to
    simulate what the human did during their turn."""

    def __init__(self, observation: Observation):
        self._observation = observation
        self.closed = False

    def set_observation(self, observation: Observation) -> None:
        self._observation = observation

    def observe(self) -> Observation:
        return self._observation

    def capture_screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return path

    def resolve_locator(self, candidate):  # pragma: no cover — unused here
        raise NotImplementedError

    def navigate(self, url: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def current_url(self) -> str:
        return self._observation.url

    def close(self) -> None:
        self.closed = True


def observation(url="http://localhost:8080/parabank/transfer.htm", identity="Transfer Funds") -> Observation:
    return Observation(
        url=url,
        title="ParaBank",
        page_identity=identity,
        elements=[ObservedElement(index=0, tag="button", role="button", name="Transfer")],
    )


class ScriptedWaiter:
    """Mocked resume signal: pops one scripted signal per pause."""

    def __init__(self, signals: list[ResumeSignal]):
        self.signals = list(signals)
        self.nav_trail: list[str] = ["http://x/transfer.htm", "http://x/overview.htm"]
        self.requests: list[InterventionRequest] = []

    def __call__(self, request: InterventionRequest) -> ResumeSignal:
        self.requests.append(request)
        return self.signals.pop(0)


@pytest.fixture()
def pending_dir(tmp_path) -> Path:
    return tmp_path / "pending_interventions"


@pytest.fixture()
def adapter() -> FakeAdapter:
    return FakeAdapter(observation())


def make_request(adapter, pending_dir, **overrides) -> InterventionRequest:
    base = dict(
        run_id="run-42",
        source="replay",
        reference="parabank_transfer_funds",
        step_index=3,
        reason="checkpoint_mismatch: expected success, observed error banner",
        pending_dir=pending_dir,
    )
    base.update(overrides)
    return raise_intervention(adapter, **base)


class TestRequestPersistence:
    def test_request_persisted_and_content_complete(
        self, adapter, pending_dir
    ) -> None:
        request = make_request(adapter, pending_dir)
        path = pending_dir / f"{request.id}.json"
        assert path.exists()
        raw = json.loads(path.read_text())
        # §9a: every field a human needs to act on the request.
        assert raw["run_id"] == "run-42"
        assert raw["source"] == "replay"
        assert raw["reference"] == "parabank_transfer_funds"
        assert raw["step_index"] == 3
        assert "checkpoint_mismatch" in raw["reason"]
        assert raw["timestamp"]
        assert raw["observation"]["url"].endswith("transfer.htm")
        assert raw["observation"]["elements"]
        assert raw["screenshot_path"] and Path(raw["screenshot_path"]).exists()
        # Round-trips losslessly.
        assert InterventionRequest.load(path) == request

    def test_discovery_stuck_stop_triggers_escalation(self, adapter, pending_dir):
        result = RunResult(
            status=RunStatus.STUCK,
            goal="Log in and transfer funds",
            reason="transfer form never loads",
            action_log=[
                ActionLogEntry(step=4, action="report_stuck", status="stuck")
            ],
        )
        request = escalate_discovery_stop(
            result, adapter, run_id="run-7", pending_dir=pending_dir
        )
        assert request is not None
        assert request.source == "discovery"
        assert request.reference == "Log in and transfer funds"
        assert request.step_index == 4
        assert "stuck" in request.reason
        assert (pending_dir / f"{request.id}.json").exists()

    def test_completed_discovery_run_escalates_nothing(self, adapter, pending_dir):
        result = RunResult(
            status=RunStatus.COMPLETED,
            goal="g",
            reason="done",
            action_log=[],
        )
        assert (
            escalate_discovery_stop(
                result, adapter, run_id="run-8", pending_dir=pending_dir
            )
            is None
        )

    def test_midflow_replay_failure_triggers_escalation(self, adapter, pending_dir):
        failure = FailureResult(
            artifact_name="parabank_transfer_funds",
            steps_executed=2,
            reason="checkpoint_mismatch",
            failed_step=2,
            expected="url=x, identity='Transfer Complete!'",
            observed="url=x, identity='Transfer Funds'",
        )
        request = escalate_replay_failure(
            failure, adapter, run_id="run-9", pending_dir=pending_dir
        )
        assert request is not None
        assert request.source == "replay"
        assert request.step_index == 2
        assert "expected" in request.reason

    def test_preflight_failure_does_not_escalate(self, adapter, pending_dir):
        failure = FailureResult(
            artifact_name="a",
            steps_executed=0,
            reason="invalid_params",
            failed_step=None,
        )
        assert (
            escalate_replay_failure(
                failure, adapter, run_id="r", pending_dir=pending_dir
            )
            is None
        )


class TestResumeVerification:
    def test_checkpoint_match_resumes_verified(self, adapter, pending_dir):
        request = make_request(adapter, pending_dir)
        # The human fixed the page so the next step's checkpoint now holds.
        expected = Checkpoint(
            url="http://localhost:8080/parabank/transfer.htm",
            page_identity="Transfer Complete!",
        )
        adapter.set_observation(
            observation(identity="Transfer Complete!")
        )
        result = HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request,
            wait_for_resume=ScriptedWaiter([ResumeSignal.VERIFY]),
            expected_checkpoint=expected,
        )
        assert result.action is HandoffAction.RESUME
        assert result.verified is True

    def test_checkpoint_mismatch_raises_new_intervention_then_resumes(
        self, adapter, pending_dir
    ):
        request = make_request(adapter, pending_dir)
        expected = Checkpoint(
            url="http://localhost:8080/parabank/transfer.htm",
            page_identity="Transfer Complete!",
        )
        # Page still wrong on first resume → new intervention; second
        # resume is an explicit 'continue' → resume without verification.
        result = HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request,
            wait_for_resume=ScriptedWaiter(
                [ResumeSignal.VERIFY, ResumeSignal.CONTINUE]
            ),
            expected_checkpoint=expected,
        )
        assert result.action is HandoffAction.RESUME
        assert result.verified is False
        # A *new* intervention request was persisted for the mismatch.
        assert result.request.id == request.id
        assert (pending_dir / f"{request.id}.json").exists()

    def test_discovery_resume_needs_no_checkpoint(self, adapter, pending_dir):
        request = make_request(adapter, pending_dir, source="discovery")
        result = HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request, wait_for_resume=ScriptedWaiter([ResumeSignal.VERIFY])
        )
        assert result.action is HandoffAction.RESUME
        assert result.verified is True


class TestExplicitOverrides:
    def test_continue_overrides_verification(self, adapter, pending_dir):
        request = make_request(adapter, pending_dir)
        expected = Checkpoint(url="http://nowhere/", page_identity="Nothing Matches")
        result = HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request,
            wait_for_resume=ScriptedWaiter([ResumeSignal.CONTINUE]),
            expected_checkpoint=expected,  # would mismatch — but overridden
        )
        assert result.action is HandoffAction.RESUME
        assert result.verified is False

    def test_mark_complete(self, adapter, pending_dir):
        request = make_request(adapter, pending_dir)
        result = HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request, wait_for_resume=ScriptedWaiter([ResumeSignal.MARK_COMPLETE])
        )
        assert result.action is HandoffAction.MARK_COMPLETE

    def test_abandon(self, adapter, pending_dir):
        request = make_request(adapter, pending_dir)
        result = HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request, wait_for_resume=ScriptedWaiter([ResumeSignal.ABANDON])
        )
        assert result.action is HandoffAction.ABANDON


class TestHumanTurnLog:
    def test_human_log_written_with_before_after_and_trail(
        self, adapter, pending_dir
    ):
        request = make_request(adapter, pending_dir)
        waiter = ScriptedWaiter([ResumeSignal.MARK_COMPLETE])
        HandoffManager(adapter, pending_dir=pending_dir).run_handoff(
            request, wait_for_resume=waiter
        )
        log_path = pending_dir / f"{request.id}.human_log.json"
        assert log_path.exists()
        log = HumanTurnLog.load(log_path)
        assert log.request_id == request.id
        assert log.before is not None and log.before.screenshot_path
        assert log.after is not None and log.after.screenshot_path
        assert log.nav_trail  # the URL trail sampled during the human's turn


class TestFileResumeWaiter:
    def test_file_signal_parsed(self, adapter, pending_dir):
        pending_dir.mkdir(parents=True, exist_ok=True)
        request = make_request(adapter, pending_dir)
        (pending_dir / f"{request.id}.resume").write_text("continue\n")
        waiter = FileResumeWaiter(
            adapter, pending_dir=pending_dir, poll_interval=0.01
        )
        assert waiter(request) is ResumeSignal.CONTINUE
        # Signal file consumed and URL trail sampled.
        assert not (pending_dir / f"{request.id}.resume").exists()
        assert waiter.nav_trail

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("verify", ResumeSignal.VERIFY),
            ("resume", ResumeSignal.VERIFY),
            ("mark_complete", ResumeSignal.MARK_COMPLETE),
            ("abandon", ResumeSignal.ABANDON),
        ],
    )
    def test_signal_parsing(
        self, content, expected, adapter, pending_dir
    ):
        assert FileResumeWaiter.parse(content) is expected

    def test_unrecognized_signal_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValueError, match="unrecognized resume signal"):
            FileResumeWaiter.parse("do_a_backflip")
