"""Replay engine (Phase 6; DECISIONS.md §6a–6d, §7b, §7c).

Executes a saved artifact deterministically — no LLM involved at replay
time. Pre-flight validation (§6c) rejects bad caller params before any
browser interaction; locator resolution (§6a) tries each step's candidate
locators in priority order and stops at the first that resolves exactly
one element; tiered retry with backoff (§6b) tolerates slow loads; the
per-invocation session (§6d) is torn down on success/hard-failure and
kept alive specifically on the escalation exit (Phase 8).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from bankops.artifact.models import ActionType, Artifact, ParamType, Step
from bankops.perception.base import Observation, PerceptionAdapter
from bankops.replay.results import (
    BusinessOutcomeResult,
    FailureResult,
    ReplayResult,
    SuccessResult,
)
from bankops.safety.allowlist import Allowlist
from bankops.safety.risk import ConfirmationRequired, check_confirmation

_JSESSIONID = re.compile(r";jsessionid=[^?]*")


class EscalationRequested(Exception):
    """Raised by escalation wiring (Phase 8) mid-replay. The per-invocation
    session is kept alive on this exit path (DECISIONS.md §6d/§9b)."""


class ElementNotResolved(Exception):
    """Every candidate locator for a step failed to resolve uniquely."""

    def __init__(self, step: Step, index: int, detail: str):
        self.step = step
        self.index = index
        self.detail = detail
        super().__init__(
            f"step {index}: no candidate locator resolved uniquely "
            f"({detail})"
        )


class ReplayEngine:
    """Deterministic artifact executor over one live session."""

    def __init__(
        self,
        adapter: PerceptionAdapter,
        *,
        allowlist: Allowlist | None = None,
        retry_delays: tuple[float, ...] = (0.0, 1.0, 3.0),
    ):
        self.adapter = adapter
        self.allowlist = allowlist
        self.retry_delays = retry_delays

    # -- Public entry point ---------------------------------------------------

    def execute(
        self, artifact: Artifact, params: dict[str, Any], *, confirm: bool = False
    ) -> SuccessResult | BusinessOutcomeResult | FailureResult:
        # Pre-flight (§6c): validate before any browser interaction.
        validated, failure = self._validate_params(artifact, params)
        if failure is not None:
            return failure

        outputs: dict[str, str] = {}
        steps_executed = 0

        for index, step in enumerate(artifact.steps):
            # §8b: the point-of-no-return step of an irreversible artifact
            # requires an explicit confirm=True.
            try:
                check_confirmation(
                    artifact.risk_level,
                    artifact.point_of_no_return_step,
                    index,
                    confirm=confirm,
                )
            except ConfirmationRequired as exc:
                return FailureResult(
                    artifact_name=artifact.name,
                    steps_executed=steps_executed,
                    reason="confirmation_required",
                    failed_step=index,
                    expected=str(exc),
                    observed="invoked without confirm=True",
                )

            try:
                self._execute_step(artifact, step, index, validated, outputs)
            except ElementNotResolved as exc:
                observed = self._observed_state()
                return FailureResult(
                    artifact_name=artifact.name,
                    steps_executed=steps_executed,
                    reason="element_not_found",
                    failed_step=index,
                    expected=(
                        f"one of: "
                        + "; ".join(
                            f"{c.strategy.value}={c.value!r}"
                            for c in step.locator_candidates
                        )
                        + f" — checkpoint {self._checkpoint_str(step)}"
                    ),
                    observed=observed,
                )

            steps_executed += 1

            # §4e: verify the per-step checkpoint before moving on — never
            # assume the click worked.
            if step.checkpoint is not None:
                observation = self.adapter.observe()
                if not self._checkpoint_matches(step.checkpoint, observation):
                    return FailureResult(
                        artifact_name=artifact.name,
                        steps_executed=steps_executed - 1,
                        reason="checkpoint_mismatch",
                        failed_step=index,
                        expected=self._checkpoint_str(step),
                        observed=self._observation_str(observation),
                    )

        # The deliberate terminal checkpoint (§4e).
        observation = self.adapter.observe()
        if not self._checkpoint_matches(artifact.terminal_checkpoint, observation):
            return FailureResult(
                artifact_name=artifact.name,
                steps_executed=steps_executed,
                reason="terminal_checkpoint_mismatch",
                failed_step=len(artifact.steps) - 1,
                expected=self._checkpoint_str_of(artifact.terminal_checkpoint),
                observed=self._observation_str(observation),
            )

        return SuccessResult(
            artifact_name=artifact.name,
            steps_executed=steps_executed,
            outputs=outputs,
        )

    # -- Step execution ---------------------------------------------------------

    def _execute_step(
        self,
        artifact: Artifact,
        step: Step,
        index: int,
        validated: dict[str, str],
        outputs: dict[str, str],
    ) -> None:
        action = step.action
        if action is ActionType.NAVIGATE:
            url = step.navigate_url or ""
            if self.allowlist is not None:
                self.allowlist.check_navigate(url)
            self.adapter.navigate(url)
            return
        if action is ActionType.EXTRACT:
            _, locator = self._resolve(step, index)
            try:
                value = str(locator.input_value())
            except Exception:
                value = locator.inner_text()
            if step.output_name:
                outputs[step.output_name] = value
            return

        # click / type_text / select_option all need a resolved element.
        _, locator = self._resolve(step, index)
        if action is ActionType.CLICK:
            locator.click()
        elif action is ActionType.TYPE_TEXT:
            locator.fill(self._step_value(step, validated))
        elif action is ActionType.SELECT_OPTION:
            option = self._step_value(step, validated)
            try:
                locator.select_option(value=option)
            except Exception:
                locator.select_option(label=option)
        else:  # pragma: no cover — ActionType is closed
            raise ValueError(f"unsupported replay action: {action}")

    @staticmethod
    def _step_value(step: Step, validated: dict[str, str]) -> str:
        if step.is_constant:
            return step.value or ""
        return validated[step.input_name or ""]

    def _resolve(self, step: Step, index: int):
        """§6a: candidates in priority order, stop at the first unique
        match; §6b: tiered retry with backoff between full passes."""
        detail = ""
        for delay in self.retry_delays:
            if delay:
                time.sleep(delay)
            for candidate in step.locator_candidates:
                try:
                    locator = self.adapter.resolve_locator(candidate)
                    count = locator.count()
                except Exception as exc:  # malformed locator — try next
                    detail = f"{candidate.strategy.value}: {exc}"
                    continue
                if count == 1:
                    return candidate, locator
                detail = (
                    f"{candidate.strategy.value}={candidate.value!r} "
                    f"matched {count} elements"
                )
        raise ElementNotResolved(step, index, detail)

    # -- Param validation (§6c) ---------------------------------------------------

    def _validate_params(
        self, artifact: Artifact, params: dict[str, Any]
    ) -> tuple[dict[str, str], FailureResult | None]:
        declared = {p.name: p for p in artifact.inputs}
        unknown = set(params) - set(declared)
        if unknown:
            return None, FailureResult(
                artifact_name=artifact.name,
                steps_executed=0,
                reason="invalid_params",
                expected=f"inputs: {sorted(declared)}",
                observed=f"unexpected inputs: {sorted(unknown)}",
            )
        validated: dict[str, str] = {}
        for name, param in declared.items():
            if name not in params:
                if param.required:
                    return None, FailureResult(
                        artifact_name=artifact.name,
                        steps_executed=0,
                        reason="invalid_params",
                        expected=f"required input '{name}' "
                        f"({param.param_type.value})",
                        observed="missing",
                    )
                if param.default is not None:
                    validated[name] = str(param.default)
                continue
            value = params[name]
            kind = param.param_type
            if kind is ParamType.STRING:
                if isinstance(value, (dict, list)):
                    return None, self._param_failure(artifact, name, kind, value)
                validated[name] = str(value)
            elif kind is ParamType.INTEGER:
                if isinstance(value, bool) or not (
                    isinstance(value, int)
                    or (isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()))
                ):
                    return None, self._param_failure(artifact, name, kind, value)
                validated[name] = str(int(value))
            elif kind is ParamType.DECIMAL:
                normalized = (
                    str(value).strip().replace(",", "").lstrip("$")
                    if isinstance(value, str)
                    else None
                )
                ok = (
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                ) or (normalized is not None and re.fullmatch(r"-?\d+(\.\d+)?", normalized))
                if not ok:
                    return None, self._param_failure(artifact, name, kind, value)
                validated[name] = normalized if isinstance(value, str) else str(value)
            elif kind is ParamType.BOOLEAN:
                if isinstance(value, bool):
                    validated[name] = "true" if value else "false"
                elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
                    validated[name] = value.strip().lower()
                else:
                    return None, self._param_failure(artifact, name, kind, value)
        return validated, None

    @staticmethod
    def _param_failure(artifact: Artifact, name: str, kind: ParamType, value: Any) -> FailureResult:
        return FailureResult(
            artifact_name=artifact.name,
            steps_executed=0,
            reason="invalid_params",
            expected=f"input '{name}' must be {kind.value}",
            observed=repr(value),
        )

    # -- Checkpoints (§4e) --------------------------------------------------------

    def _observed_state(self) -> str:
        try:
            observation = self.adapter.observe()
        except Exception:
            return f"url={self.adapter.current_url()} (observation failed)"
        return self._observation_str(observation)

    @staticmethod
    def _observation_str(observation: Observation) -> str:
        return (
            f"url={observation.url}, identity={observation.page_identity!r}"
        )

    @staticmethod
    def _checkpoint_str(step: Step) -> str:
        assert step.checkpoint is not None
        return ReplayEngine._checkpoint_str_of(step.checkpoint)

    @staticmethod
    def _checkpoint_str_of(checkpoint) -> str:
        return (
            f"url={checkpoint.url}, identity={checkpoint.page_identity!r}"
        )

    def _checkpoint_matches(self, checkpoint, observation: Observation) -> bool:
        return self._normalize_url(
            checkpoint.url
        ) == self._normalize_url(observation.url) and (
            checkpoint.page_identity == observation.page_identity
            or checkpoint.page_identity == observation.title
            or checkpoint.page_identity in observation.page_identity
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        # ParaBank decorates URLs with ;jsessionid=... — strip for compare.
        return _JSESSIONID.sub("", url).rstrip("/")


def replay_artifact(
    artifact: Artifact,
    params: dict[str, Any],
    *,
    adapter_factory: Callable[[], PerceptionAdapter],
    confirm: bool = False,
    allowlist: Allowlist | None = None,
    retry_delays: tuple[float, ...] = (0.0, 1.0, 3.0),
) -> SuccessResult | BusinessOutcomeResult | FailureResult:
    """Per-invocation session lifecycle (§6d): one session per replay, torn
    down on success/hard-failure, kept alive on the escalation exit path
    (EscalationRequested propagates without teardown so a human can take
    over the same live session, §9b)."""
    adapter = adapter_factory()
    try:
        result = ReplayEngine(
            adapter, allowlist=allowlist, retry_delays=retry_delays
        ).execute(artifact, params, confirm=confirm)
    except EscalationRequested:
        raise  # session stays alive for handoff
    except BaseException:
        adapter.close()
        raise
    adapter.close()
    return result
