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
            "observe": "allow",
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


class TestReasoningCapture:
    """§10a: the model's one-sentence rationale is persisted per step, with
    §8c masking — including values treated as sensitive earlier in the
    run, so a later sentence cannot echo a password typed steps before."""

    def _ctx_with_password_field(self):
        observation = Observation(
            url="http://localhost:8080/parabank/index.htm",
            title="ParaBank",
            page_identity="Customer Login",
            elements=[
                ObservedElement(
                    index=0,
                    tag="input",
                    role="textbox",
                    name="Password",
                    locators=[
                        LocatorCandidate(
                            strategy=LocatorStrategy.ID_ATTRIBUTE,
                            value="#password",
                        )
                    ],
                )
            ],
        )
        return make_ctx([observation])

    def test_reasoning_persisted_and_masked(self, tmp_path) -> None:
        import json

        from bankops.evidence.logger import StepLogger

        ctx = self._ctx_with_password_field()
        logger = StepLogger(tmp_path, run_id="r", run_kind="discovery")
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="type_text",
                        params={"index": 0, "text": "s3cret"},
                        reasoning="I will type the password s3cret now",
                    )
                ],
                [ToolCall(id="2", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=5, step_logger=logger).run("goal")
        assert result.status is RunStatus.COMPLETED
        line = json.loads((tmp_path / "steps.jsonl").read_text().splitlines()[0])
        assert line["reasoning"] == "I will type the password ***MASKED*** now"
        assert "s3cret" not in json.dumps(line)

    def test_cross_step_echo_masked_via_sensitive_seen(self, tmp_path) -> None:
        import json

        from bankops.evidence.logger import StepLogger

        ctx = self._ctx_with_password_field()
        logger = StepLogger(tmp_path, run_id="r", run_kind="discovery")
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="type_text",
                        params={"index": 0, "text": "s3cret"},
                        reasoning="typing my password",
                    )
                ],
                [
                    ToolCall(
                        id="2",
                        name="remember",
                        params={"key": "note", "value": "ok"},
                        reasoning="done with the s3cret password field",
                    )
                ],
                [ToolCall(id="3", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=6, step_logger=logger).run("goal")
        lines = (tmp_path / "steps.jsonl").read_text().splitlines()
        second = json.loads(lines[1])
        # This step's own params are not sensitive — only the run-wide
        # sensitive-value tracking can mask the echo.
        assert "s3cret" not in second["reasoning"]
        assert "***MASKED***" in second["reasoning"]

    def test_no_call_content_captured_as_reasoning(self, tmp_path) -> None:
        import json

        from bankops.evidence.logger import StepLogger

        class NoteLLM(ScriptedLLM):
            def decide(self, messages, tools=None):
                self.last_content = "I believe the goal is complete now"
                return super().decide(messages, tools)

        ctx = self._ctx_with_password_field()
        logger = StepLogger(tmp_path, run_id="r", run_kind="discovery")
        llm = NoteLLM(
            [
                [],
                [ToolCall(id="1", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=5, step_logger=logger).run("goal")
        line = json.loads((tmp_path / "steps.jsonl").read_text().splitlines()[0])
        assert line["action"] == "(none)"
        assert line["reasoning"] == "I believe the goal is complete now"


class TestFreeTextEchoMasking:
    """§8c end-to-end: a value the run treated as sensitive (typed into a
    Password-labelled field) can never resurface — not in the finish
    summary's params, its message, nor the run's terminal reason."""

    def test_finish_summary_echo_masked_everywhere(self, tmp_path) -> None:
        import json

        from bankops.evidence.logger import StepLogger

        ctx = TestReasoningCapture()._ctx_with_password_field()
        logger = StepLogger(tmp_path, run_id="r", run_kind="discovery")
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="type_text",
                        params={"index": 0, "text": "s3cret"},
                        reasoning="typing my password",
                    )
                ],
                [
                    ToolCall(
                        id="2",
                        name="finish",
                        params={
                            "summary": "Done — logged in with password s3cret"
                        },
                    )
                ],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=5, step_logger=logger).run("goal")
        assert result.status is RunStatus.COMPLETED
        # RunResult.reason (→ summary.json / CLI output):
        assert "s3cret" not in result.reason
        assert "***MASKED***" in result.reason
        # The persisted finish step line: params, message — all scrubbed.
        line = json.loads((tmp_path / "steps.jsonl").read_text().splitlines()[1])
        assert "s3cret" not in json.dumps(line)
        assert "***MASKED***" in line["params"]["summary"]
        assert "***MASKED***" in line["message"]


    def test_summary_goal_scrubbed_via_sensitive_values(self, tmp_path) -> None:
        """The user's goal string itself can contain a credential; the run
        summary must scrub it against values the run treated as sensitive."""
        import json

        from bankops.evidence.logger import StepLogger

        ctx = TestReasoningCapture()._ctx_with_password_field()
        logger = StepLogger(tmp_path, run_id="r", run_kind="discovery")
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="type_text",
                        params={"index": 0, "text": "s3cret"},
                        reasoning="typing my password",
                    )
                ],
                [ToolCall(id="2", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=5, step_logger=logger).run("goal")
        assert "s3cret" in result.sensitive_values
        logger.write_summary(
            {"goal": "log in with password s3cret", "reason": result.reason},
            mask_values=result.sensitive_values,
        )
        raw = (tmp_path / "summary.json").read_text()
        assert "s3cret" not in raw
        assert "***MASKED***" in raw


class TestProgressEvents:
    """The CLI's live step feed is driven by per-action StepProgress
    events — action, target, status, and the model's own one-sentence
    rationale (§8c-masked before emission, like the persisted evidence)."""

    def test_progress_events_emitted_per_action(self) -> None:
        events = []
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="remember",
                        params={"key": "k", "value": "v"},
                        reasoning="storing k",
                    )
                ],
                [
                    ToolCall(
                        id="2",
                        name="click",
                        params={"index": 0},
                        reasoning="clicking Log In",
                    )
                ],
                [ToolCall(id="3", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(
            llm, ctx, max_steps=6, on_progress=events.append
        ).run("goal")
        assert result.status is RunStatus.COMPLETED
        assert [(e.action, e.status) for e in events] == [
            ("remember", "success"),
            ("click", "success"),
            ("finish", "finished"),
        ]
        assert events[0].target == "k = v"
        assert events[1].target == 'button "Log In"'
        assert events[1].reasoning == "clicking Log In"

    def test_no_call_step_emits_progress_event(self) -> None:
        events = []
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [],
                [ToolCall(id="1", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(
            llm, ctx, max_steps=5, on_progress=events.append
        ).run("goal")
        assert events[0].action == "(no tool call)"
        assert events[0].status == "error"

    def test_progress_reasoning_is_masked(self) -> None:
        events = []
        ctx = TestReasoningCapture()._ctx_with_password_field()
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="type_text",
                        params={"index": 0, "text": "s3cret"},
                        reasoning="typing s3cret now",
                    )
                ],
                [ToolCall(id="2", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(
            llm, ctx, max_steps=5, on_progress=events.append
        ).run("goal")
        assert events[0].reasoning == "typing ***MASKED*** now"


    def test_no_call_reminder_quotes_model_words_and_points_at_observe(
        self,
    ) -> None:
        """Regression (observed live): the model's text-only exit words
        ("let me look at the current page") must be quoted back to it, with
        the explicit fact that CURRENT PAGE is the live state and `observe`
        is the tool for a fresh look."""
        ctx = make_ctx([make_observation()])

        class NarratingLLM(ScriptedLLM):
            def decide(self, messages, tools=None):
                self.last_content = "Let me look at the current page state"
                return super().decide(messages, tools)

        llm = NarratingLLM(
            [
                [],
                [ToolCall(id="1", name="finish", params={"summary": "done"})],
            ]
        )
        DiscoveryLoop(llm, ctx, max_steps=5).run("goal")
        second_content = llm.requests[1][1]["content"]
        assert 'You said: "Let me look at the current page state"' in second_content
        assert "IS the live page state" in second_content
        assert "`observe`" in second_content
        # Terminal-choice framing (observed live: the model deliberated in
        # prose about whether a denied loan 'counts' as goal-complete):
        assert "final business decision" in second_content

    def test_observe_dispatches_through_the_loop(self) -> None:
        ctx = make_ctx([make_observation()])
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="observe",
                        params={},
                        reasoning="re-checking the page",
                    )
                ],
                [ToolCall(id="2", name="finish", params={"summary": "done"})],
            ]
        )
        result = DiscoveryLoop(llm, ctx, max_steps=5).run("goal")
        assert result.status is RunStatus.COMPLETED
        assert result.action_log[0].action == "observe"
        assert result.action_log[0].status == "success"


class TestRedactionToggle:
    """Redaction is implemented (§8c) but disabled by default "for now"
    (settings.REDACT) so live agent runs show real values; the suite forces
    it on (conftest) — this class pins the OFF path."""

    def test_feed_and_evidence_raw_when_redaction_disabled(
        self, tmp_path, monkeypatch
    ) -> None:
        import json

        from bankops import settings as _settings
        from bankops.evidence.logger import StepLogger

        monkeypatch.setattr(_settings, "REDACT", False)
        events = []
        ctx = TestReasoningCapture()._ctx_with_password_field()
        logger = StepLogger(tmp_path, run_id="r", run_kind="discovery")
        llm = ScriptedLLM(
            [
                [
                    ToolCall(
                        id="1",
                        name="type_text",
                        params={"index": 0, "text": "s3cret"},
                        reasoning="typing s3cret now",
                    )
                ],
                [
                    ToolCall(
                        id="2",
                        name="finish",
                        params={"summary": "used password s3cret"},
                    )
                ],
            ]
        )
        result = DiscoveryLoop(
            llm,
            ctx,
            max_steps=5,
            on_progress=events.append,
            step_logger=logger,
        ).run("goal")
        # Terminal feed, persisted evidence, and the run reason all show
        # real values when redaction is disabled:
        assert events[0].reasoning == "typing s3cret now"
        line = json.loads((tmp_path / "steps.jsonl").read_text().splitlines()[0])
        assert line["params"]["text"] == "s3cret"
        assert line["reasoning"] == "typing s3cret now"
        assert "s3cret" in result.reason
        summary = logger.write_summary(
            {"goal": "log in with password s3cret"},
            mask_values=result.sensitive_values,
        )
        assert "s3cret" in summary.read_text()
