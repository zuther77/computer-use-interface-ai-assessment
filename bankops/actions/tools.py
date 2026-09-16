"""Typed action tools (Phase 3; DECISIONS.md §3b, §8a).

One typed tool per action type — click, type_text, select_option, navigate,
extract, remember, finish, report_stuck — each with parameters scoped to
exactly what it needs, taking an element index from the perception
adapter's current observation (DECISIONS.md §3a). A generic act() and raw
code generation were both ruled out (§3b): this closed, auditable action
space is what the two-dimensional allowlist (§8a) is checked against.

Division of errors (deliberate):
- AllowlistViolation *raises* — policy violations must never be swallowed.
- Operational failures (bad index, ambiguous locators, timeouts) come back
  as ToolResult(status=ERROR) so the discovery loop can record them as
  failed actions for no-progress detection (§3d).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel

from bankops.perception.base import Observation, PerceptionAdapter
from bankops.safety.allowlist import Allowlist


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    FINISHED = "finished"
    STUCK = "stuck"


class ToolResult(BaseModel):
    action: str
    status: ToolStatus
    message: str = ""
    data: str | None = None
    element_index: int | None = None


class ActionError(Exception):
    """Operational action failure (element missing, locator ambiguous, …)."""


class ToolContext:
    """Execution state shared by all tools in one run (DECISIONS.md §3c):
    the adapter, the allowlist, the current observation (updated by the
    loop each step — tools never observe on their own), and the ``remember``
    working-memory store."""

    def __init__(self, adapter: PerceptionAdapter, allowlist: Allowlist):
        self.adapter = adapter
        self.allowlist = allowlist
        self.memory: dict[str, str] = {}
        self.observation: Observation | None = None

    def refresh_observation(self) -> Observation:
        self.observation = self.adapter.observe()
        return self.observation


def _resolve_unique(ctx: ToolContext, index: int):
    """First-unique-match over the element's ordered candidate locators
    (DECISIONS.md §4b): try candidates in priority order until exactly one
    element resolves."""
    if ctx.observation is None:
        raise ActionError("no current observation — observe before acting")
    element = ctx.observation.element_by_index(index)
    if element is None:
        raise ActionError(
            f"no element at index {index} in the current observation "
            f"({len(ctx.observation.elements)} elements)"
        )
    attempts: list[str] = []
    for candidate in element.locators:
        locator = ctx.adapter.resolve_locator(candidate)
        count = locator.count()
        if count == 1:
            return element, candidate, locator
        attempts.append(f"{candidate.strategy.value} matched {count}")
    raise ActionError(
        f"could not uniquely resolve element {index} "
        f'({element.role} "{element.name}"): {"; ".join(attempts)}'
    )


# -- Tools -------------------------------------------------------------------


def click(ctx: ToolContext, index: int) -> ToolResult:
    ctx.allowlist.check_action("click")
    element, _, locator = _resolve_unique(ctx, index)
    locator.click()
    return ToolResult(
        action="click",
        status=ToolStatus.SUCCESS,
        element_index=index,
        message=f'clicked {element.role} "{element.name}"',
    )


def type_text(ctx: ToolContext, index: int, text: str) -> ToolResult:
    ctx.allowlist.check_action("type_text")
    element, _, locator = _resolve_unique(ctx, index)
    locator.fill(text)
    return ToolResult(
        action="type_text",
        status=ToolStatus.SUCCESS,
        element_index=index,
        data=text,
        message=f'filled {element.role} "{element.name}"',
    )


def select_option(ctx: ToolContext, index: int, option: str) -> ToolResult:
    ctx.allowlist.check_action("select_option")
    element, _, locator = _resolve_unique(ctx, index)
    # Try value first, then label — recorded options replay either way.
    try:
        locator.select_option(value=option)
    except Exception:
        try:
            locator.select_option(label=option)
        except Exception as exc:
            raise ActionError(
                f'option "{option}" not found in {element.role} '
                f'"{element.name}": {exc}'
            ) from exc
    return ToolResult(
        action="select_option",
        status=ToolStatus.SUCCESS,
        element_index=index,
        data=option,
        message=f'selected "{option}" in {element.role} "{element.name}"',
    )


def navigate(ctx: ToolContext, url: str) -> ToolResult:
    ctx.allowlist.check_action("navigate")
    ctx.allowlist.check_navigate(url)
    ctx.adapter.navigate(url)
    return ToolResult(
        action="navigate",
        status=ToolStatus.SUCCESS,
        message=f"navigated to {url}",
    )


def extract(ctx: ToolContext, index: int) -> ToolResult:
    """Read a value from the page. Anything meant to be caller-facing must
    come through extract (DECISIONS.md §4d) — remember is internal memory."""
    ctx.allowlist.check_action("extract")
    element, _, locator = _resolve_unique(ctx, index)
    try:
        value = str(locator.input_value())
    except Exception:
        value = locator.inner_text()
    return ToolResult(
        action="extract",
        status=ToolStatus.SUCCESS,
        element_index=index,
        data=value,
        message=f'extracted {element.role} "{element.name}": {value!r}',
    )


def remember(ctx: ToolContext, key: str, value: str) -> ToolResult:
    ctx.allowlist.check_action("remember")
    ctx.memory[key] = value
    return ToolResult(
        action="remember",
        status=ToolStatus.SUCCESS,
        data=value,
        message=f"remembered '{key}'",
    )


def finish(ctx: ToolContext, summary: str) -> ToolResult:
    ctx.allowlist.check_action("finish")
    return ToolResult(
        action="finish", status=ToolStatus.FINISHED, message=summary
    )


def report_stuck(ctx: ToolContext, reason: str) -> ToolResult:
    ctx.allowlist.check_action("report_stuck")
    return ToolResult(
        action="report_stuck", status=ToolStatus.STUCK, message=reason
    )


# -- Registry & dispatch ------------------------------------------------------

ACTIONS: dict[str, Callable[..., ToolResult]] = {
    "click": click,
    "type_text": type_text,
    "select_option": select_option,
    "navigate": navigate,
    "extract": extract,
    "remember": remember,
    "finish": finish,
    "report_stuck": report_stuck,
}


def dispatch(ctx: ToolContext, name: str, params: dict[str, Any]) -> ToolResult:
    """Execute a typed tool by name. Unknown names and operational errors
    come back as typed ERROR results; allowlist violations still raise."""
    handler = ACTIONS.get(name)
    if handler is None:
        return ToolResult(
            action=name,
            status=ToolStatus.ERROR,
            message=f"unknown action type '{name}' (allowed: {sorted(ACTIONS)})",
        )
    try:
        return handler(ctx, **params)
    except ActionError as exc:
        # Operational failures come back as typed ERROR results (§3d:
        # failed actions feed no-progress detection), never as crashes.
        return ToolResult(action=name, status=ToolStatus.ERROR, message=str(exc))
    except TypeError as exc:
        return ToolResult(
            action=name,
            status=ToolStatus.ERROR,
            message=f"invalid parameters for '{name}': {exc}",
        )


# -- OpenAI tool-calling schemas (consumed by the Phase 4 loop, §2d) -----------

OPENAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click the interactive element at the given observation index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Element index from the current observation"},
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the input element at the given observation index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Element index from the current observation"},
                    "text": {"type": "string", "description": "Text to type"},
                },
                "required": ["index", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": "Select an option in the dropdown at the given observation index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Element index from the current observation"},
                    "option": {"type": "string", "description": "Option value or visible label to select"},
                },
                "required": ["index", "option"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL. Only allowlisted domains/routes are permitted.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full URL to navigate to"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract",
            "description": (
                "Read the current value of the element at the given observation index. "
                "Use this specifically for information the goal asked you to retrieve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Element index from the current observation"},
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store a key/value pair in working memory for later steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key"},
                    "value": {"type": "string", "description": "Value to store"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Declare the goal achieved and end the run, with a summary of the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What was accomplished, including any extracted values"},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_stuck",
            "description": "Report that you cannot make progress and end the run, with the reason.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you are stuck"},
                },
                "required": ["reason"],
            },
        },
    },
]
