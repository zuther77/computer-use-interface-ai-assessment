"""Discovery agent loop (Phase 4; DECISIONS.md §2d, §3c, §3d).

Observe → decide → act: an LLM (GLM via the OpenAI SDK's tool-calling
spec, §2d) picks one typed action per step from the perception adapter's
indexed observation (§3a) and the Phase 3 tools execute it.

Context management (§3c) is structured state, never transcript replay:
each LLM request is built fresh from (a) the *current* observation, (b) a
compact action-history log, and (c) the ``remember`` store — no prior
full observations are ever resent.

Stopping conditions (§3d) are layered and produce a *typed* reason:
  - max-step ceiling,
  - no-progress detection: a page-affecting action followed by an
    identical observation hash, N times in a row,
  - explicit self-report via finish / report_stuck.
The typed reason is exactly what the escalation trigger (§9a) carries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

from bankops import settings
from bankops.actions.tools import (
    OPENAI_TOOLS,
    ToolContext,
    ToolResult,
    ToolStatus,
    dispatch,
)
from bankops.artifact.models import LocatorCandidate
from bankops.perception.base import Observation
from bankops.safety.allowlist import AllowlistViolation
from bankops.safety.redaction import mask_known_values, redact_action

# Actions that are supposed to change the page state. Only these feed the
# observation-hash no-progress detector — remember/extract legitimately
# leave the page unchanged.
_PAGE_AFFECTING = frozenset({"click", "type_text", "select_option", "navigate"})

SYSTEM_PROMPT = (
    "You are a web-automation agent operating a banking application through "
    "typed tools. You are shown an indexed list of the current page's "
    "interactive elements; act on them strictly by index. Take exactly one "
    "action per step. Complete each page of the goal in one visit: fill "
    "every required field and submit the form before navigating elsewhere. "
    "Use `extract` specifically for information the goal "
    "asked you to retrieve, and `remember` for facts you will need in later "
    "steps. Never invent element indexes that are not in the current "
    "observation, and never attempt to bypass the navigation/action "
    "allowlist — if an action is blocked, choose a different approach. "
    "Type monetary amounts as plain numbers without currency symbols "
    "(for example 25.00, not $25.00). "
    "Use `observe` whenever you want a fresh look at the page — never "
    "narrate an intention without acting on it. "
    "Before each tool call, state in one short sentence why this action "
    "moves toward the goal. "
    "If the same action fails or the page doesn't change after two "
    "attempts, don't repeat it — try a different element, or call "
    "`report_stuck` with what you observed. "
    "Only call `finish` once the current page visibly confirms the goal "
    "was achieved — never call finish based on assumption; if you truly "
    "cannot proceed, call `report_stuck` with the reason."
)


@dataclass
class ToolCall:
    id: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    """The model's accompanying one-sentence rationale (§10a: LLM
    reasoning is evidence) — captured from message.content, masked before
    persistence (§8c), and never a decision input."""


class LLMClient(Protocol):
    """The minimal interface the loop depends on — one typed tool-call
    decision per request. OpenAIToolCallingClient implements it against
    the OpenAI SDK (§2d); tests use scripted clients."""


class OpenAIToolCallingClient:
    """GLM via the OpenAI Python SDK (DECISIONS.md §2d): the tool-schema
    contract is written once against the OpenAI tool-calling spec, so a
    provider swap is a base_url/api_key change, not a rewrite."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = model or settings.LLM_MODEL
        self._client = OpenAI(
            base_url=base_url or settings.LLM_BASE_URL or None,
            api_key=api_key or settings.LLM_API_KEY or None,
        )

    def decide(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> list[ToolCall]:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or OPENAI_TOOLS,
        )
        message = response.choices[0].message
        # §10a: the model's rationale sentence is captured (not discarded)
        # so it can be persisted as evidence — and so a content-only
        # response (no tool calls) still leaves its exit words behind.
        reasoning = (message.content or "").strip()
        self.last_content = reasoning
        calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            try:
                params = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                params = {}
            calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    params=params,
                    reasoning=reasoning,
                )
            )
        return calls


class RunStatus(str, Enum):
    COMPLETED = "completed"
    STUCK = "stuck"
    MAX_STEPS = "max_steps"
    NO_PROGRESS = "no_progress"


class ActionLogEntry(BaseModel):
    """One executed (or attempted) action in the raw action log. Values are
    held raw in memory for the recorder (§5b); redaction is applied by the
    evidence writer before anything is persisted (§8c).

    The element/checkpoint fields are captured passively per action so the
    recorder (§4c/4d/4e) can build candidate locators, label-derived
    parameter names, and per-step signatures from the raw log alone."""

    step: int
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: str
    message: str = ""
    data: str | None = None
    element_index: int | None = None
    url: str = ""
    page_title: str = ""
    page_identity: str = ""
    observation_hash: str = ""
    element_role: str = ""
    element_name: str = ""
    reasoning: str = ""
    locator_candidates: list[LocatorCandidate] = Field(default_factory=list)


class RunResult(BaseModel):
    status: RunStatus
    goal: str
    reason: str
    action_log: list[ActionLogEntry] = Field(default_factory=list)
    memory: dict[str, str] = Field(default_factory=dict)
    sensitive_values: list[str] = Field(default_factory=list)
    final_observation: Observation | None = None


class StepProgress(BaseModel):
    """A live per-action progress event for operator-facing surfaces (the
    CLI's step feed): what was done, to what target, with what result, and
    the model's one-sentence rationale for it. Fields are §8c-masked by
    the loop before emission — the terminal feed never echoes a
    credential, just like the persisted evidence."""

    step: int
    action: str
    status: str
    target: str = ""
    reasoning: str = ""
    message: str = ""
    url: str = ""
    page: str = ""


class DiscoveryLoop:
    """One discovery run: observe → decide → act until a typed stop."""

    def __init__(
        self,
        client: LLMClient,
        ctx: ToolContext,
        *,
        max_steps: int | None = None,
        no_progress_limit: int = 3,
        step_logger=None,
        on_progress: Callable[[StepProgress], None] | None = None,
    ) -> None:
        self.client = client
        self.ctx = ctx
        self.max_steps = max_steps if max_steps is not None else settings.MAX_STEPS
        self.no_progress_limit = no_progress_limit
        self.action_log: list[ActionLogEntry] = []
        self.step_logger = step_logger
        self.on_progress = on_progress

    # -- Structured state → messages (§3c) ----------------------------------

    def _build_messages(self, goal: str) -> list[dict[str, Any]]:
        observation = self.ctx.observation
        assert observation is not None
        history = "\n".join(
            f"  {e.step}. {e.action}({e.params}) -> {e.status}"
            + (f": {e.message}" if e.message else "")
            for e in self.action_log
        ) or "  (none yet)"
        memory = (
            "\n".join(f"  {k} = {v}" for k, v in sorted(self.ctx.memory.items()))
            or "  (empty)"
        )
        content = (
            f"GOAL: {goal}\n\n"
            f"WORKING MEMORY:\n{memory}\n\n"
            f"ACTION HISTORY (compact summary — full raw log is kept "
            f"separately):\n{history}\n\n"
            f"CURRENT PAGE:\n{observation.render()}\n\n"
            "Choose exactly one next action as a single tool call."
        )
        # A no-tool-call response is an error the model must correct in the
        # *current* turn — repeat the requirement explicitly here, not only
        # in the history summary (§3d: ending a run is a typed tool call).
        if self.action_log and self.action_log[-1].action == "(none)":
            last_words = self.action_log[-1].reasoning
            content += (
                "\n\nREMINDER: your previous response was text-only and "
                "contained no tool call, so nothing happened."
            )
            if last_words:
                content += f' (You said: "{last_words[:160]}")'
            content += (
                " Do not narrate intentions: the CURRENT PAGE section above "
                "IS the live page state — you never need to wait for it or "
                "ask for it. Respond now with exactly one tool call: "
                "`observe` for a fresh look, `finish(summary)` if the goal "
                "is achieved, or `report_stuck(reason)` if you cannot "
                "proceed."
            )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]

    def _mask(self, text: str, values: list[str]) -> str:
        """§8c masking, toggle-gated: implemented and test-verified, but
        disabled by default for now (settings.REDACT) so live agent runs
        show real values in the terminal feed and evidence."""
        if not settings.REDACT:
            return text
        return mask_known_values(text, values)

    # -- The loop ------------------------------------------------------------

    def run(self, goal: str) -> RunResult:
        self._goal = goal
        self.ctx.refresh_observation()
        consecutive_stale = 0
        # Bounded tool-calling cadence (§3c-compliant): only the *previous*
        # step's tool call + result is repeated as proper assistant/tool
        # messages. Constant size per request — never transcript replay.
        # Without it, requests are bare [system, user] pairs and models
        # drift into content-only responses at completion (no finish call).
        last_exchange: list[dict[str, Any]] = []
        # Duplicate-action guard state: the immediately preceding executed
        # action. Repeating the *exact* same call right after it succeeded
        # is a model derpage (observed live: re-typing the same amount four
        # steps in a row), not a legitimate retry — it is rejected with a
        # corrective typed error. Retries after a *failed* action stay
        # allowed, and non-adjacent repeats (e.g. navigating back and
        # clicking the same link again later) are unaffected.
        prev_signature: tuple[str, str] | None = None
        prev_status: str = ""
        # §8c: values this run has already treated as sensitive — they get
        # masked wherever they resurface later (a reasoning sentence echoing
        # a password typed steps earlier, a no-call exit note, …).
        sensitive_seen: list[str] = []
        self.sensitive_seen = sensitive_seen

        for step in range(1, self.max_steps + 1):
            step_start_hash = self.ctx.observation.observation_hash
            messages = self._build_messages(goal) + last_exchange
            calls = self.client.decide(messages)

            if not calls:
                last_exchange = []  # cadence broken — do not replay it
                # The reminder rides into the action history, which the
                # next prompt renders — an explicit corrective signal for
                # models that end runs without calling finish (§3d).
                reminder = (
                    "model returned no tool call — you must respond with "
                    "exactly one tool call; if the goal is now achieved, "
                    "call finish with a summary; if you cannot proceed, "
                    "call report_stuck with the reason"
                )
                self.action_log.append(
                    ActionLogEntry(
                        step=step,
                        action="(none)",
                        params={},
                        status="error",
                        message=reminder,
                        reasoning=getattr(self.client, "last_content", ""),
                        url=self.ctx.adapter.current_url(),
                        page_title=self.ctx.observation.title,
                        page_identity=self.ctx.observation.page_identity,
                        observation_hash=step_start_hash,
                    )
                )
                if self.step_logger is not None:
                    # §10a: one JSONL line per step — no-call steps too.
                    self.step_logger.log_step(
                        step=step,
                        action="(none)",
                        status="error",
                        params={},
                        message=reminder,
                        reasoning=getattr(self.client, "last_content", ""),
                        mask_values=list(sensitive_seen),
                        data=None,
                        element_name="",
                        url=self.ctx.adapter.current_url(),
                        observation_summary=(
                            f"{self.ctx.observation.page_identity} "
                            f"({self.ctx.observation.title})"
                        ),
                    )
                if self.on_progress is not None:
                    self.on_progress(
                        StepProgress(
                            step=step,
                            action="(no tool call)",
                            status="error",
                            reasoning=self._mask(
                                getattr(self.client, "last_content", ""),
                                sensitive_seen,
                            ),
                            message="reminded the model to answer with a tool call",
                            url=self.ctx.adapter.current_url(),
                            page=self.ctx.observation.page_identity,
                        )
                    )
                consecutive_stale += 1
                if consecutive_stale >= self.no_progress_limit:
                    return self._result(RunStatus.NO_PROGRESS, "model repeatedly returned no tool call")
                continue

            page_affecting_attempt = False
            step_exchange: list[dict[str, Any]] = [
                {
                    "role": "assistant",
                    # The model's own rationale rides the cadence turn —
                    # seeing its prior reasoning keeps it consistent.
                    "content": calls[0].reasoning or None,
                    "tool_calls": [
                        {
                            "id": call.id or f"call_{step}_{i}",
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.params),
                            },
                        }
                        for i, call in enumerate(calls)
                    ],
                }
            ]
            for call_position, call in enumerate(calls):
                # Capture the acted-on element from the *pre-action*
                # observation (§3a) — its name and candidate locators are
                # what the recorder later turns into artifact steps (§4b/§4c).
                pre_observation = self.ctx.observation
                index = call.params.get("index")
                element = (
                    pre_observation.element_by_index(index)
                    if isinstance(index, int)
                    else None
                )
                signature = (
                    call.name,
                    json.dumps(call.params, sort_keys=True),
                )
                if prev_signature == signature and prev_status == "success":
                    result = ToolResult(
                        action=call.name,
                        status=ToolStatus.ERROR,
                        message=(
                            f"duplicate action: '{call.name}({call.params})' "
                            f"just succeeded — do not repeat it; choose a "
                            f"different next action"
                        ),
                    )
                else:
                    try:
                        result = dispatch(self.ctx, call.name, call.params)
                    except AllowlistViolation as exc:
                        # The action never executes (enforced), but the run
                        # continues: the blocked attempt is recorded as a
                        # typed error so the model can correct course.
                        result = ToolResult(
                            action=call.name,
                            status=ToolStatus.ERROR,
                            message=f"blocked by allowlist: {exc}",
                        )
                prev_signature, prev_status = signature, result.status.value
                post_observation = self.ctx.refresh_observation()
                # §8c: track the values this step treats as sensitive, so
                # later reasoning sentences / exit notes echoing them are
                # masked at write time.
                safe_params, safe_data = redact_action(
                    call.name,
                    call.params,
                    element_name=element.name if element else "",
                    data=result.data,
                )
                for key, value in call.params.items():
                    if (
                        safe_params.get(key) != value
                        and isinstance(value, (str, int, float))
                        and str(value)
                    ):
                        sensitive_seen.append(str(value))
                if (
                    result.data is not None
                    and safe_data != result.data
                    and str(result.data)
                ):
                    sensitive_seen.append(str(result.data))
                step_exchange.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id
                        or f"call_{step}_{call_position}",
                        "content": f"{result.status.value}: {result.message}"[:400],
                    }
                )
                self.action_log.append(
                    ActionLogEntry(
                        step=step,
                        action=call.name,
                        params=dict(call.params),
                        status=result.status.value,
                        message=result.message,
                        data=result.data,
                        element_index=result.element_index,
                        url=post_observation.url,
                        page_title=post_observation.title,
                        page_identity=post_observation.page_identity,
                        observation_hash=post_observation.observation_hash,
                        element_role=element.role if element else "",
                        element_name=element.name if element else "",
                        reasoning=call.reasoning,
                        locator_candidates=(
                            [c.model_copy() for c in element.locators]
                            if element
                            else []
                        ),
                    )
                )
                if self.step_logger is not None:
                    # Evidence wiring (§10a/§8c): redaction happens inside
                    # the writer, before the line is written.
                    self.step_logger.log_step(
                        step=step,
                        action=call.name,
                        status=result.status.value,
                        params=dict(call.params),
                        message=result.message,
                        data=result.data,
                        element_name=element.name if element else "",
                        reasoning=call.reasoning,
                        mask_values=list(sensitive_seen),
                        url=post_observation.url,
                        observation_summary=(
                            f"{post_observation.page_identity} "
                            f"({post_observation.title})"
                        ),
                    )
                if self.on_progress is not None:
                    if element is not None:
                        target = (
                            f'{element.role} "{element.name}"'
                            if element.name
                            else element.role
                        )
                    elif call.name == "navigate":
                        target = str(call.params.get("url", ""))
                    elif call.name == "remember":
                        target = (
                            f"{safe_params.get('key', '')} = "
                            f"{safe_params.get('value', '')}"
                        )
                    else:
                        target = ""
                    self.on_progress(
                        StepProgress(
                            step=step,
                            action=call.name,
                            status=result.status.value,
                            target=target,
                            reasoning=self._mask(
                                call.reasoning, sensitive_seen
                            ),
                            message=(
                                self._mask(result.message, sensitive_seen)
                                if result.status is ToolStatus.ERROR
                                else ""
                            ),
                            url=post_observation.url,
                            page=post_observation.page_identity,
                        )
                    )
                if call.name in _PAGE_AFFECTING:
                    page_affecting_attempt = True
                if result.status is ToolStatus.FINISHED:
                    # §8c: the model's own summary may echo a credential —
                    # mask values this run already treated as sensitive
                    # before the reason reaches summary.json/CLI.
                    return self._result(
                        RunStatus.COMPLETED,
                        self._mask(result.message, sensitive_seen),
                    )
                if result.status is ToolStatus.STUCK:
                    return self._result(
                        RunStatus.STUCK,
                        self._mask(result.message, sensitive_seen),
                    )

            last_exchange = step_exchange

            # No-progress detection (§3d): a page-affecting action whose
            # post-action observation hash is identical, N steps in a row.
            if page_affecting_attempt:
                if self.ctx.observation.observation_hash == step_start_hash:
                    consecutive_stale += 1
                else:
                    consecutive_stale = 0
            if consecutive_stale >= self.no_progress_limit:
                return self._result(
                    RunStatus.NO_PROGRESS,
                    f"no observable progress for {consecutive_stale} "
                    f"consecutive page-affecting steps",
                )

        return self._result(
            RunStatus.MAX_STEPS,
            f"reached max-step ceiling of {self.max_steps} without finish/report_stuck",
        )

    def _result(self, status: RunStatus, reason: str) -> RunResult:
        return RunResult(
            status=status,
            goal=getattr(self, "_goal", ""),
            reason=reason,
            action_log=list(self.action_log),
            memory=dict(self.ctx.memory),
            sensitive_values=list(getattr(self, "sensitive_seen", [])),
            final_observation=self.ctx.observation,
        )

