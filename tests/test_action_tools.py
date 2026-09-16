"""Action tool tests (Phase 3; DECISIONS.md §3b, §8a).

Each tool executes correctly against the local static fixture (not live
ParaBank), including allowlist enforcement at navigate and at every tool
invocation, and the finish/report_stuck control signals.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from bankops.actions.tools import (
    ToolContext,
    ToolStatus,
    click,
    dispatch,
    extract,
    finish,
    navigate,
    remember,
    report_stuck,
    select_option,
    type_text,
)
from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter
from bankops.safety.allowlist import Allowlist, AllowlistViolation

PAGE_ONE = Path(__file__).parent / "fixtures" / "tools_page1.html"
PAGE_TWO = Path(__file__).parent / "fixtures" / "tools_page2.html"


def make_allowlist(**overrides) -> Allowlist:
    base = dict(
        domains=["*"],
        routes=["*"],
        schemes=["http", "https", "file"],
        action_types={
            name: "allow"
            for name in (
                "click",
                "type_text",
                "select_option",
                "navigate",
                "extract",
                "remember",
                "finish",
                "report_stuck",
                "observe",
            )
        },
    )
    base.update(overrides)
    return Allowlist(**base)


@pytest.fixture()
def ctx():
    """A fresh tooled-up context on page one, with a current observation.

    Element indexes on page one (document order):
      [0] textbox "Amount"     [1] combobox "Mode"
      [2] button "Apply"       [3] link "Go to page two"
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_ONE.as_uri())
        context = ToolContext(
            adapter=PlaywrightPerceptionAdapter(page),
            allowlist=make_allowlist(),
        )
        context.refresh_observation()
        yield context
        browser.close()


class TestPageActions:
    def test_type_text_fills_input(self, ctx: ToolContext) -> None:
        result = type_text(ctx, index=0, text="150.00")
        assert result.status is ToolStatus.SUCCESS
        assert ctx.adapter.page.locator("#amount").input_value() == "150.00"

    def test_select_option_by_value(self, ctx: ToolContext) -> None:
        result = select_option(ctx, index=1, option="slow")
        assert result.status is ToolStatus.SUCCESS
        assert ctx.adapter.page.locator("#mode").input_value() == "slow"

    def test_select_option_by_label_fallback(self, ctx: ToolContext) -> None:
        result = select_option(ctx, index=1, option="Fast Shipping")
        assert result.status is ToolStatus.SUCCESS
        assert ctx.adapter.page.locator("#mode").input_value() == "fast"

    def test_click_executes(self, ctx: ToolContext) -> None:
        result = click(ctx, index=2)
        assert result.status is ToolStatus.SUCCESS
        assert ctx.adapter.page.locator("#out").inner_text() == "applied"

    def test_click_navigation_via_link(self, ctx: ToolContext) -> None:
        result = click(ctx, index=3)
        assert result.status is ToolStatus.SUCCESS
        assert ctx.adapter.page.url.endswith("tools_page2.html")

    def test_navigate_moves_to_page_two(self, ctx: ToolContext) -> None:
        result = navigate(ctx, url=PAGE_TWO.as_uri())
        assert result.status is ToolStatus.SUCCESS
        assert ctx.adapter.page.locator("#status").inner_text() == (
            "You arrived at page two."
        )

    def test_extract_returns_input_value(self, ctx: ToolContext) -> None:
        type_text(ctx, index=0, text="99.50")
        result = extract(ctx, index=0)
        assert result.status is ToolStatus.SUCCESS
        assert result.data == "99.50"

    def test_extract_from_non_input_returns_text(self, ctx: ToolContext) -> None:
        extract(ctx, index=0)  # ensure page state settled
        result = extract(ctx, index=2)  # "Apply" button
        assert result.status is ToolStatus.SUCCESS
        assert result.data == "Apply"


class TestMemoryAndControlSignals:
    def test_remember_persists_in_context(self, ctx: ToolContext) -> None:
        result = remember(ctx, key="customer_id", value="12345")
        assert result.status is ToolStatus.SUCCESS
        assert ctx.memory == {"customer_id": "12345"}

    def test_finish_signal(self, ctx: ToolContext) -> None:
        result = finish(ctx, summary="Transferred $150 from checking to savings")
        assert result.status is ToolStatus.FINISHED
        assert "Transferred" in result.message

    def test_report_stuck_signal(self, ctx: ToolContext) -> None:
        result = report_stuck(ctx, reason="Transfer page never loads")
        assert result.status is ToolStatus.STUCK
        assert result.message == "Transfer page never loads"


class TestAllowlistEnforcement:
    def test_navigate_denied_domain(self, ctx: ToolContext) -> None:
        ctx.allowlist = make_allowlist(
            domains=["localhost:8080"], routes=["/parabank/*"]
        )
        with pytest.raises(AllowlistViolation, match="domain"):
            navigate(ctx, url="http://evil.com/parabank/index.htm")

    def test_action_type_denied_at_invocation(self, ctx: ToolContext) -> None:
        ctx.allowlist = make_allowlist(action_types={"click": "allow"})
        with pytest.raises(AllowlistViolation, match="'type_text'"):
            type_text(ctx, index=0, text="x")


class TestErrorHandling:
    def test_bad_index_returns_error_result(self, ctx: ToolContext) -> None:
        result = dispatch(ctx, "click", {"index": 99})
        assert result.status is ToolStatus.ERROR
        assert "no element at index 99" in result.message

    def test_acting_without_observation_errors(self, ctx: ToolContext) -> None:
        ctx.observation = None
        result = dispatch(ctx, "click", {"index": 0})
        assert result.status is ToolStatus.ERROR
        assert "no current observation" in result.message

    def test_dispatch_unknown_action(self, ctx: ToolContext) -> None:
        result = dispatch(ctx, "explode", {})
        assert result.status is ToolStatus.ERROR
        assert "unknown action type" in result.message

    def test_dispatch_bad_params(self, ctx: ToolContext) -> None:
        result = dispatch(ctx, "type_text", {"index": 0})  # missing text
        assert result.status is ToolStatus.ERROR
        assert "invalid parameters" in result.message

    def test_dispatch_routes_to_tools(self, ctx: ToolContext) -> None:
        result = dispatch(ctx, "remember", {"key": "k", "value": "v"})
        assert result.status is ToolStatus.SUCCESS
        assert ctx.memory == {"k": "v"}


class _ExplodingLocator:
    def count(self) -> int:
        return 1

    def fill(self, text: str) -> None:
        raise RuntimeError("simulated Playwright timeout")


class _StubAdapter:
    """Minimal surface whose only element resolves to a locator that fails
    on fill — used to prove surface-level action failures are typed
    errors, not crashes (§3d)."""

    def __init__(self):
        from bankops.artifact.models import (
            LocatorCandidate,
            LocatorStrategy,
        )
        from bankops.perception.base import Observation, ObservedElement

        self._observation = Observation(
            url="http://localhost:8080/parabank/transfer.htm",
            title="ParaBank | Transfer Funds",
            page_identity="Transfer Funds",
            elements=[
                ObservedElement(
                    index=0,
                    tag="input",
                    role="textbox",
                    name="Amount",
                    locators=[
                        LocatorCandidate(
                            strategy=LocatorStrategy.ID_ATTRIBUTE,
                            value="#amount",
                        )
                    ],
                )
            ],
        )

    def observe(self):
        return self._observation

    def capture_screenshot(self, path):
        return Path(path)

    def resolve_locator(self, candidate):
        return _ExplodingLocator()

    def navigate(self, url: str) -> None: ...

    def current_url(self) -> str:
        return self._observation.url


class TestOperationalFailureHandling:
    def test_playwright_timeout_becomes_typed_error_not_crash(self) -> None:
        """Regression: a Playwright timeout mid-action (element went
        hidden after its observation) must not kill the run."""
        ctx = ToolContext(adapter=_StubAdapter(), allowlist=make_allowlist())
        ctx.refresh_observation()
        result = dispatch(ctx, "type_text", {"index": 0, "text": "25.00"})
        assert result.status is ToolStatus.ERROR
        assert "action failed" in result.message
        assert "simulated Playwright timeout" in result.message

    def test_allowlist_violation_still_raises_through_dispatch(self) -> None:
        """The catch-all must never swallow policy violations (§8a)."""
        ctx = ToolContext(
            adapter=_StubAdapter(),
            allowlist=make_allowlist(action_types={"type_text": "deny"}),
        )
        ctx.refresh_observation()
        with pytest.raises(AllowlistViolation):
            dispatch(ctx, "type_text", {"index": 0, "text": "25.00"})


class TestNavigateErrorPages:
    def test_http_error_navigation_is_a_typed_error(self) -> None:
        """Regression: navigating onto a 404/error page must come back as
        a typed ERROR (the page state is empty — operating on it silently
        is how discovery runs die in bounce loops)."""

        class _NotFoundAdapter(_StubAdapter):
            def navigate(self, url: str):
                return 404

        ctx = ToolContext(adapter=_NotFoundAdapter(), allowlist=make_allowlist())
        result = dispatch(
            ctx, "navigate", {"url": "http://localhost:8080/parabank/nowhere.htm"}
        )
        assert result.status is ToolStatus.ERROR
        assert "HTTP 404" in result.message


class TestObserveTool:
    def test_observe_returns_fresh_render(self, ctx) -> None:
        """`observe` is the legitimate way to express 'let me look at the
        page' — a typed, no-op tool that returns the fresh observation."""
        result = dispatch(ctx, "observe", {})
        assert result.status is ToolStatus.SUCCESS
        assert result.data is not None
        assert '[0] textbox "Amount"' in result.data

    def test_observe_blocked_when_denied(self, ctx) -> None:
        ctx.allowlist.action_types = {
            **ctx.allowlist.action_types,
            "observe": "deny",
        }
        with pytest.raises(AllowlistViolation):
            dispatch(ctx, "observe", {})
