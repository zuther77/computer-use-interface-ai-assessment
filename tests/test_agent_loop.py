"""Discovery agent loop tests (Phase 4; DECISIONS.md §2d, §3c, §3d).

The LLM client is mocked (scripted tool calls) and the surface is a fake
adapter — no live API calls and no browser, per the plan. Covers the
layered stopping conditions and remember-value persistence in state.
"""

from __future__ import annotations

from pathlib import Path

from bankops.actions.tools import ToolContext
from bankops.agent.loop import DiscoveryLoop, RunStatus, ToolCall
from bankops.artifact.models import LocatorCandidate, LocatorStrategy
from bankops.perception.base import (
    Observation,
    ObservedElement,
    PerceptionAdapter,
)
from bankops.safety.allowlist import Allowlist

PAGE_URL = "http://localhost:8080/parabank/index.htm"


class FakeLocator:
    """Just enough surface for tools to 'execute' against the fake page."""

    def count(self) -> int:
        return 1

    def click(self) -> None: ...

    def fill(self, text: str) -> None: ...

    def select_option(self, **kwargs) -> None: ...

    def input_value(self) -> str:
        return "100.00"

    def inner_text(self) -> str:
        return "sample text"


class FakeAdapter(PerceptionAdapter):
    """A scripted surface: always the same observation unless told
    otherwise (exactly what no-progress detection keys on)."""

    def __init__(self, observations: list[Observation]):
        self._observations = observations
        self._cursor = 0
        self.navigate_calls: list[str] = []

    def observe(self) -> Observation:
        obs = self._observations[min(self._cursor, len(self._observations) - 1)]
        return obs

    def advance(self) -> None:
        self._cursor += 1

    def capture_screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return path

    def resolve_locator(self, candidate: LocatorCandidate) -> FakeLocator:
        return FakeLocator()

    def navigate(self, url: str) -> None:
        self.navigate_calls.append(url)

    def current_url(self) -> str:
        return self.observe().url


def make_observation(url: str = PAGE_URL, identity: str = "Accounts Overview") -> Observation:
    return Observation(
        url=url,
        title="ParaBank",
        page_identity=identity,
        elements=[
            ObservedElement(
                index=0,
                tag="button",
                role="button",
                name="Log In",
                locators=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#login"
                    )
                ],
            )
        ],
    )


class ScriptedLLM:
    """Mocked LLM client: pops one scripted list of tool calls per step."""

    def __init__(self, scripts: list[list[ToolCall]]):
        self.scripts = list(scripts)
        self.requests: list[list[dict]] = []

    def decide(self, messages: list[dict], tools: list[dict] | None = None):
        self.requests.append(messages)
        if self.scripts:
            return self.scripts.pop(0)
        return []


def make_ctx(observations: list[Observation]) -> ToolContext:
    allowlist = Allowlist(
        domains=["*"],
        routes=["*"],
        schemes=["http", "https", "file"],
        action_types={
            "click": "allow",
            "type_text": "allow",
            "select_option": "allow",
            "navigate": "allow",
            "extract": "allow",
            "remember": "allow",
            "finish": "allow",
            "report_stuck": "allow",
        },
    )
    ctx = ToolContext(adapter=FakeAdapter(observations), allowlist=allowlist)
    return ctx


class TestHappyPath:
    def test_finish_terminates_completed(self) -> None:
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [ToolCall(id="1", name="remember", params={"key": "customer_id", "value": "12212"})],
                [ToolCall(id="2", name="finish", params={"summary": "Logged in and found balance"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=10).run("Log in as john")
        assert result.status is RunStatus.COMPLETED
        assert "balance" in result.reason
        assert [e.action for e in result.action_log] == ["remember", "finish"]
        assert [e.step for e in result.action_log] == [1, 2]
        assert result.memory == {"customer_id": "12212"}

    def test_report_stuck_terminates_stuck(self) -> None:
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [[ToolCall(id="1", name="report_stuck", params={"reason": "login page never loads"})]]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=10).run("Log in as john")
        assert result.status is RunStatus.STUCK
        assert result.reason == "login page never loads"


class TestStoppingConditions:
    def test_no_progress_after_identical_observations(self) -> None:
        """Repeated clicks that never change the observation hash halt
        the run with a typed NO_PROGRESS reason."""
        ctx = make_ctx([make_observation()])
        click = [ToolCall(id="n", name="click", params={"index": 0})]
        llm = ScriptedLLM([click, click, click, click, click])
        result = DiscoveryLoop(llm, ctx, max_steps=50, no_progress_limit=3).run("do something")
        assert result.status is RunStatus.NO_PROGRESS
        assert "3 consecutive" in result.reason
        # Halted as soon as the limit fired — the 4th/5th scripts never ran.
        assert len(result.action_log) == 3

    def test_no_progress_counter_resets_on_change(self) -> None:
        """A changing observation resets the stale counter, so a slow but
        progressing run is not misclassified."""
        changing = [
            make_observation(url="http://x/1", identity="Page 1"),
            make_observation(url="http://x/2", identity="Page 2"),
            make_observation(url="http://x/3", identity="Page 3"),
            make_observation(url="http://x/4", identity="Page 4"),
        ]
        ctx = make_ctx(changing)
        click = [ToolCall(id="n", name="click", params={"index": 0})]
        llm = ScriptedLLM(
            [click, click, click, click,
             [ToolCall(id="z", name="finish", params={"summary": "done"})]]
        )
        # The adapter only advances when navigate() is called; emulate page
        # changes by advancing per click.
        adapter = ctx.adapter
        original_click = FakeLocator.click

        class AdvancingLocator(FakeLocator):
            def click(self) -> None:
                adapter.advance()

        ctx.adapter.resolve_locator = lambda candidate: AdvancingLocator()
        result = DiscoveryLoop(llm, ctx, max_steps=10, no_progress_limit=3).run("walk pages")
        assert result.status is RunStatus.COMPLETED

    def test_max_steps_ceiling(self) -> None:
        """Non-page-affecting actions never trip no-progress, so the
        max-step ceiling is what stops an endless remember loop."""
        ctx = make_ctx([make_observation()])
        remember = [
            ToolCall(id="n", name="remember", params={"key": "k", "value": "v"})
        ]
        llm = ScriptedLLM([remember] * 10)
        result = DiscoveryLoop(llm, ctx, max_steps=4).run("impossible goal")
        assert result.status is RunStatus.MAX_STEPS
        assert "max-step ceiling of 4" in result.reason
        assert len(result.action_log) == 4


class TestStructuredState:
    def test_remember_persists_across_steps_and_is_visible_to_model(self) -> None:
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [ToolCall(id="1", name="remember", params={"key": "account_number", "value": "10001"})],
                [ToolCall(id="2", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=10).run("find account number")
        assert result.memory == {"account_number": "10001"}
        # The step-2 request was built from structured state: the remember
        # value persisted into working memory (§3c).
        second_request = llm.requests[1]
        content = second_request[1]["content"]
        assert "account_number = 10001" in content
        # And only the *current* observation is sent, never replayed
        # transcripts of prior observations.
        assert content.count("CURRENT PAGE:") == 1
        assert "ACTION HISTORY" in content

    def test_allowlist_blocked_action_recorded_as_error(self) -> None:
        ctx = make_ctx([make_observation()])
        ctx.allowlist.action_types = {k: "allow" for k in ctx.allowlist.action_types}
        ctx.allowlist.action_types["click"] = "deny"
        llm = ScriptedLLM(
            [
                [ToolCall(id="1", name="click", params={"index": 0})],
                [ToolCall(id="2", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=10).run("try to click")
        assert result.status is RunStatus.COMPLETED
        blocked = result.action_log[0]
        assert blocked.status == "error"
        assert "blocked by allowlist" in blocked.message


class TestNoCallStepLogging:
    def test_no_call_steps_are_logged_to_evidence(self, tmp_path) -> None:
        """Regression: steps where the model returned no tool call must
        still produce their JSONL line (§10a — one line per step)."""
        import json

        from bankops.evidence.logger import StepLogger

        ctx = make_ctx([make_observation()])
        logger = StepLogger(tmp_path, run_id="logtest", run_kind="discovery")
        llm = ScriptedLLM(
            [
                [],
                [ToolCall(id="1", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=5, step_logger=logger).run("goal")
        lines = (tmp_path / "steps.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["action"] == "(none)"
        assert first["status"] == "error"
        second = json.loads(lines[1])
        assert second["action"] == "finish"


class TestToolCallCadence:
    """§3c-compliant bounded cadence: the previous step's tool call and
    result ride as proper assistant/tool messages so the model stays in a
    tool-calling rhythm instead of drifting into content-only responses
    at completion. Constant size per request — never transcript replay."""

    def test_previous_exchange_rides_as_assistant_tool_messages(self) -> None:
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [ToolCall(id="c1", name="remember", params={"key": "k", "value": "v"})],
                [ToolCall(id="c2", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=5).run("goal")
        second = llm.requests[1]
        assert [m["role"] for m in second] == [
            "system", "user", "assistant", "tool",
        ]
        assert second[2]["tool_calls"][0]["id"] == "c1"
        assert second[2]["tool_calls"][0]["function"]["name"] == "remember"
        assert second[3]["tool_call_id"] == "c1"
        assert "success" in second[3]["content"]

    def test_no_call_response_breaks_the_cadence_chain(self) -> None:
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [],
                [ToolCall(id="c1", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=5).run("goal")
        second = llm.requests[1]
        assert [m["role"] for m in second] == ["system", "user"]


class TestDuplicateActionGuard:
    """Regression (observed live): a model that re-issues the exact same
    call right after it succeeded is derping, not retrying — reject it with
    a corrective typed error so the run can move on instead of grinding
    into a no-progress halt."""

    def test_immediate_duplicate_of_successful_action_is_rejected(self) -> None:
        ctx = make_ctx([make_observation()])
        remember_call = ToolCall(
            id="a", name="remember", params={"key": "k", "value": "v"}
        )
        llm = ScriptedLLM(
            [
                [remember_call],
                [remember_call],
                [ToolCall(id="c", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=6).run("goal")
        assert result.status is RunStatus.COMPLETED
        assert result.action_log[0].status == "success"
        assert result.action_log[1].status == "error"
        assert "duplicate action" in result.action_log[1].message

    def test_retry_after_failure_is_still_allowed(self) -> None:
        """A failed action may legitimately be retried unchanged."""
        ctx = make_ctx([make_observation()])
        click_call = ToolCall(id="a", name="click", params={"index": 99})
        llm = ScriptedLLM(
            [
                [click_call],
                [click_call],
                [ToolCall(id="c", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=6).run("goal")
        assert result.status is RunStatus.COMPLETED
        # Both attempts were executed (both failed: no element at 99),
        # neither was rejected as a duplicate.
        assert result.action_log[0].status == "error"
        assert "duplicate action" not in result.action_log[0].message
        assert "duplicate action" not in result.action_log[1].message
        assert "no element at index 99" in result.action_log[1].message
