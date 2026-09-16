"""Runner glue tests (thin — the runner is Phase 10's live-run entry point;
these verify the folder/artifact/summary wiring with fakes and fixtures,
never a live LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from bankops.artifact.models import (
    ActionType,
    Artifact,
    Checkpoint,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    ParamType,
    RiskLevel,
    Step,
)
from bankops.agent.loop import RunStatus, ToolCall
from bankops.perception.base import (
    Observation,
    ObservedElement,
    PerceptionAdapter,
)
from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter
from bankops.runner import run_discovery, run_replay
from bankops.safety.allowlist import Allowlist
from tests.test_agent_loop import FakeLocator  # reuse the fake surface

PAGE_ONE = (Path(__file__).parent / "fixtures" / "tools_page1.html").as_uri()


def permissive_allowlist() -> Allowlist:
    return Allowlist(
        domains=["*"],
        routes=["*"],
        schemes=["http", "https", "file"],
        action_types={n: "allow" for n in (
            "click", "type_text", "select_option", "navigate",
            "extract", "remember", "finish", "report_stuck",
        )},
    )


class FakeAdapter(PerceptionAdapter):
    def __init__(self):
        self._observation = Observation(
            url="http://localhost:8080/parabank/index.htm",
            title="ParaBank",
            page_identity="Accounts Overview",
            elements=[
                ObservedElement(
                    index=0, tag="button", role="button", name="Log In",
                    locators=[LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#login"
                    )],
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


class TestRunDiscovery:
    def test_completed_run_creates_evidence_and_artifact(self, tmp_path):
        llm = ScriptedLLM([
            [ToolCall(id="1", name="click", params={"index": 0})],
            [ToolCall(id="2", name="remember", params={"key": "acct", "value": "1"})],
            [ToolCall(id="3", name="finish", params={"summary": "done"})],
        ])
        result, artifact = run_discovery(
            "Find my account number",
            artifact_name="demo_capability",
            adapter=FakeAdapter(),
            client=llm,
            allowlist=permissive_allowlist(),
            evidence_dir=tmp_path,
            run_id="d1",
        )
        assert result.status is RunStatus.COMPLETED
        assert artifact is not None and artifact.name == "demo_capability"

        run_folder = tmp_path / "discovery_run_d1"
        assert (run_folder / "steps.jsonl").exists()
        assert (run_folder / "summary.json").exists()
        assert (run_folder / "final.png").exists()
        assert (run_folder / "artifact_demo_capability.json").exists()
        assert (tmp_path / "artifacts" / "demo_capability.json").exists()

    def test_stuck_run_creates_no_artifact(self, tmp_path):
        llm = ScriptedLLM([
            [ToolCall(id="1", name="report_stuck", params={"reason": "lost"})],
        ])
        result, artifact = run_discovery(
            "g",
            artifact_name="never",
            adapter=FakeAdapter(),
            client=llm,
            allowlist=permissive_allowlist(),
            evidence_dir=tmp_path,
            run_id="d2",
        )
        assert result.status is RunStatus.STUCK
        assert artifact is None
        assert not (tmp_path / "artifacts" / "never.json").exists()


class TestRunReplay:
    def test_success_run_lands_in_replay_success_folder(self, tmp_path):
        artifact = Artifact(
            name="apply_flow",
            goal="Fill the form and apply",
            target_base_url=PAGE_ONE,
            steps=[
                Step(
                    action=ActionType.NAVIGATE,
                    navigate_url=PAGE_ONE,
                    checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
                ),
                Step(
                    action=ActionType.CLICK,
                    locator_candidates=[LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#apply"
                    )],
                    checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
                ),
            ],
            inputs=[],
            outputs=[],
            terminal_checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
            risk_level=RiskLevel.READ_ONLY,
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            result = run_replay(
                artifact, {},
                adapter=PlaywrightPerceptionAdapter(page),
                allowlist=permissive_allowlist(),
                evidence_dir=tmp_path,
                run_id="r1",
                retry_delays=(0.0, 0.0),
            )
            browser.close()
        assert result.status == "success"
        folder = tmp_path / "replay_success_r1"
        assert folder.is_dir()
        assert (folder / "steps.jsonl").exists()
        assert (folder / "summary.json").exists()
        assert (folder / "final.png").exists()
        assert not (tmp_path / "replay_error_r1").exists()

    def test_failure_run_moves_evidence_to_replay_error_folder(self, tmp_path):
        artifact = Artifact(
            name="broken_flow",
            goal="Impossible",
            target_base_url=PAGE_ONE,
            steps=[
                Step(
                    action=ActionType.NAVIGATE,
                    navigate_url=PAGE_ONE,
                    checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
                ),
                Step(
                    action=ActionType.CLICK,
                    locator_candidates=[LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#does-not-exist"
                    )],
                    checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
                ),
            ],
            inputs=[],
            outputs=[],
            terminal_checkpoint=Checkpoint(url=PAGE_ONE, page_identity="Tools Page One"),
            risk_level=RiskLevel.READ_ONLY,
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            result = run_replay(
                artifact, {},
                adapter=PlaywrightPerceptionAdapter(page),
                allowlist=permissive_allowlist(),
                evidence_dir=tmp_path,
                run_id="r2",
                retry_delays=(0.0, 0.0),
            )
            browser.close()
        assert result.status == "failure"
        assert (tmp_path / "replay_error_r2").is_dir()
        assert (tmp_path / "replay_error_r2" / "steps.jsonl").exists()
        assert not (tmp_path / "replay_success_r2").exists()
