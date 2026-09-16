"""Evidence & structured-logging tests (Phase 9; DECISIONS.md §10a, §10b, §8c).

A fake run produces the expected folder structure and JSONL line
count/shape, and a deliberately-included sensitive-named field is masked
in the *written* log — not just in memory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from bankops.actions.tools import ToolContext
from bankops.agent.loop import DiscoveryLoop, RunStatus, ToolCall
from bankops.artifact.models import (
    ActionType,
    Checkpoint,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
    Artifact,
)
from bankops.evidence.logger import StepLogEntry, StepLogger
from bankops.evidence.paths import RUN_KINDS, artifacts_dir, run_dir
from bankops.perception.base import (
    Observation,
    ObservedElement,
    PerceptionAdapter,
)
from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter
from bankops.replay.engine import ReplayEngine
from bankops.safety.allowlist import Allowlist

PAGE_ONE = (Path(__file__).parent / "fixtures" / "tools_page1.html").as_uri()


# -- Fakes (no live LLM, no live browser for the discovery part) ----------------


class FakeLocator:
    def count(self) -> int:
        return 1

    def click(self) -> None: ...

    def fill(self, text: str) -> None: ...

    def select_option(self, **kwargs) -> None: ...

    def input_value(self) -> str:
        return "typed"

    def inner_text(self) -> str:
        return "text"


class FakeAdapter(PerceptionAdapter):
    def __init__(self):
        self._observation = Observation(
            url="http://localhost:8080/parabank/index.htm",
            title="ParaBank",
            page_identity="Login",
            elements=[
                ObservedElement(
                    index=0,
                    tag="input",
                    role="textbox",
                    name="Password",
                    locators=[
                        LocatorCandidate(
                            strategy=LocatorStrategy.ID_ATTRIBUTE, value="#password"
                        )
                    ],
                )
            ],
        )

    def observe(self) -> Observation:
        return self._observation

    def capture_screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return path

    def resolve_locator(self, candidate):
        return FakeLocator()

    def navigate(self, url: str) -> None: ...

    def current_url(self) -> str:
        return self._observation.url


class ScriptedLLM:
    def __init__(self, scripts):
        self.scripts = list(scripts)

    def decide(self, messages, tools=None):
        return self.scripts.pop(0) if self.scripts else []


def permissive_allowlist() -> Allowlist:
    return Allowlist(
        domains=["*"],
        routes=["*"],
        schemes=["http", "https", "file"],
        action_types={name: "allow" for name in (
            "click", "type_text", "select_option", "navigate",
            "extract", "remember", "finish", "report_stuck",
        )},
    )


# -- Folder conventions (§10b) ----------------------------------------------------


class TestPaths:
    def test_run_folder_conventions(self, tmp_path):
        discovery = run_dir("discovery", "42", evidence_dir=tmp_path)
        success = run_dir("replay_success", "42", evidence_dir=tmp_path)
        error = run_dir("replay_error", "42", evidence_dir=tmp_path)
        assert discovery == tmp_path / "discovery_run_42"
        assert success == tmp_path / "replay_success_42"
        assert error == tmp_path / "replay_error_42"
        assert discovery.is_dir() and success.is_dir() and error.is_dir()
        assert artifacts_dir(evidence_dir=tmp_path) == tmp_path / "artifacts"

    def test_unknown_run_kind_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="unknown run kind"):
            run_dir("exploding", "42", evidence_dir=tmp_path)

    def test_run_kinds_constant_matches_plan(self):
        assert set(RUN_KINDS) == {"discovery", "replay_success", "replay_error"}


# -- Discovery-run evidence (§10a) ------------------------------------------------


class TestDiscoveryLogging:
    def test_fake_run_writes_expected_folder_and_jsonl(self, tmp_path):
        run_directory = run_dir("discovery", "99", evidence_dir=tmp_path)
        logger = StepLogger(run_directory, run_id="99", run_kind="discovery")

        ctx = ToolContext(adapter=FakeAdapter(), allowlist=permissive_allowlist())
        llm = ScriptedLLM(
            [
                [ToolCall(id="1", name="type_text",
                          params={"index": 0, "text": "hunter2"})],
                [ToolCall(id="2", name="remember",
                          params={"key": "session", "value": "abc"})],
                [ToolCall(id="3", name="finish", params={"summary": "done"})],
            ]
        )
        loop = DiscoveryLoop(llm, ctx, max_steps=10, step_logger=logger)
        result = loop.run("Log in")
        assert result.status is RunStatus.COMPLETED

        # One JSONL line per executed action; expected folder structure.
        jsonl = run_directory / "steps.jsonl"
        assert jsonl.exists()
        lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3  # type_text, remember, finish
        entries = [StepLogEntry.model_validate_json(ln) for ln in lines]
        assert [e.action for e in entries] == ["type_text", "remember", "finish"]
        assert all(e.run_id == "99" for e in entries)
        assert all(e.run_kind == "discovery" for e in entries)
        assert all(e.timestamp for e in entries)

        # The sensitive field is masked in the WRITTEN log — not just in
        # memory: type_text targeted an element labelled "Password".
        raw_text = jsonl.read_text()
        assert "hunter2" not in raw_text
        assert "***MASKED***" in raw_text
        masked_entry = entries[0]
        assert masked_entry.params["text"] == "***MASKED***"
        # While the in-session context still used the live value.
        assert result.action_log[0].params["text"] == "hunter2"

    def test_remember_with_sensitive_key_masked(self, tmp_path):
        run_directory = run_dir("discovery", "98", evidence_dir=tmp_path)
        logger = StepLogger(run_directory, run_id="98", run_kind="discovery")
        ctx = ToolContext(adapter=FakeAdapter(), allowlist=permissive_allowlist())
        llm = ScriptedLLM(
            [
                [ToolCall(id="1", name="remember",
                          params={"key": "password", "value": "hunter2"})],
                [ToolCall(id="2", name="finish", params={"summary": "x"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=10, step_logger=logger).run("g")
        raw = (run_directory / "steps.jsonl").read_text()
        assert "hunter2" not in raw

    def test_summary_written_with_nested_redaction(self, tmp_path):
        run_directory = run_dir("discovery", "97", evidence_dir=tmp_path)
        logger = StepLogger(run_directory, run_id="97", run_kind="discovery")
        logger.log_step(step=0, action="click", status="success")
        path = logger.write_summary(
            {
                "status": "completed",
                "reason": "done",
                "memory": {"password": "hunter2", "account": "10001"},
            }
        )
        assert path.exists()
        raw = json.loads(path.read_text())
        assert raw["memory"]["password"] == "***MASKED***"
        assert raw["memory"]["account"] == "***MASKED***"
        assert raw["status"] == "completed"


# -- Replay-run evidence (§10a) ----------------------------------------------------


def make_artifact() -> Artifact:
    return Artifact(
        name="apply_with_amount",
        goal="Fill the form and apply",
        target_base_url=PAGE_ONE,
        steps=[
            Step(
                action=ActionType.NAVIGATE,
                navigate_url=PAGE_ONE,
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                locator_candidates=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.ROLE_NAME,
                        value='role=textbox name="No Such Field"',
                    ),
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#amount"
                    ),
                ],
                input_name="from_account",  # sensitive name → masked in evidence
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
            ),
            Step(
                action=ActionType.CLICK,
                locator_candidates=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#apply"
                    )
                ],
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
            ),
        ],
        inputs=[InputParam(name="from_account", param_type=ParamType.STRING)],
        outputs=[],
        terminal_checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
        risk_level=RiskLevel.READ_ONLY,
    )


class TestReplayLogging:
    def test_replay_logs_line_per_step_with_strategy_tier(self, tmp_path):
        run_directory = run_dir("replay_success", "55", evidence_dir=tmp_path)
        logger = StepLogger(run_directory, run_id="55", run_kind="replay")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            engine = ReplayEngine(
                PlaywrightPerceptionAdapter(page),
                retry_delays=(0.0, 0.0),
                step_logger=logger,
            )
            result = engine.execute(make_artifact(), {"from_account": "10001"})
            browser.close()

        assert result.status == "success"
        entries = logger.read_entries()
        assert len(entries) == 3  # one line per step
        assert [e.action for e in entries] == ["navigate", "type_text", "click"]
        # Which locator strategy tier resolved (§10a/§4b): the role_name
        # primary failed and id_attribute won on the type_text step.
        assert entries[0].locator_strategy is None  # navigate resolves no element
        assert entries[1].locator_strategy == "id_attribute"
        assert entries[2].locator_strategy == "id_attribute"
        # Sensitive input name masked in the written log, not just in memory.
        raw = (run_directory / "steps.jsonl").read_text()
        assert "10001" not in raw
        assert entries[1].params["value"] == "***MASKED***"

    def test_replay_failure_logged_with_classification(self, tmp_path):
        run_directory = run_dir("replay_error", "56", evidence_dir=tmp_path)
        logger = StepLogger(run_directory, run_id="56", run_kind="replay")
        artifact = make_artifact()
        artifact = artifact.model_copy(
            update={
                "steps": [
                    s.model_copy(
                        update={"locator_candidates": [
                            LocatorCandidate(
                                strategy=LocatorStrategy.ID_ATTRIBUTE, value="#nope"
                            )
                        ]}
                    )
                    if s.action is ActionType.CLICK
                    else s
                    for s in artifact.steps
                ]
            }
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            engine = ReplayEngine(
                PlaywrightPerceptionAdapter(page),
                retry_delays=(0.0, 0.0),
                step_logger=logger,
            )
            result = engine.execute(artifact, {"from_account": "x"})
            browser.close()

        assert result.status == "failure"
        entries = logger.read_entries()
        # Two successes logged, then the failure line with its reason.
        assert entries[-1].status == "failure"
        assert entries[-1].result_classification == "element_not_found"
        assert "matched 0" in entries[-1].message
