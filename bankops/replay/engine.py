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

from bankops.artifact.models import (
    ActionType,
    Artifact,
    LocatorCandidate,
    OutcomeClass,
    OutcomeSignature,
    ParamType,
    Step,
)
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


def normalize_url(url: str) -> str:
    """ParaBank decorates URLs with ;jsessionid=... — strip for compare."""
    return _JSESSIONID.sub("", url).rstrip("/")


def checkpoint_matches(checkpoint, observation: Observation) -> bool:
    """§4e page-state signature match: URL (session-id-insensitive) plus the
    stable page-identity signal (with title fallbacks). Shared by the
    replay engine and the escalation resume verification (§9c) so both
    verify state with exactly the same rule."""
    return normalize_url(checkpoint.url) == normalize_url(
        observation.url
    ) and (
        checkpoint.page_identity == observation.page_identity
        or checkpoint.page_identity == observation.title
        or checkpoint.page_identity in observation.page_identity
    )


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
        step_logger=None,
    ):
        self.adapter = adapter
        self.allowlist = allowlist
        self.retry_delays = retry_delays
        self.step_logger = step_logger

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

            applied = self._log_params(step, validated)
            try:
                candidate = self._execute_step(artifact, step, index, validated, outputs)
            except ElementNotResolved as exc:
                self._log(
                    index, step, "failure",
                    message=str(exc),
                    params=applied,
                    classification="element_not_found",
                )
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
            self._log(
                index,
                step,
                "success",
                params=applied,
                data=outputs.get(step.output_name) if step.output_name else None,
                strategy=candidate.strategy.value if candidate else None,
            )

            # §4e: verify the per-step checkpoint before moving on — never
            # assume the click worked. When it doesn't match, check the
            # step's recorded alternate-outcome signatures (§7a) before
            # falling back to a hard failure.
            if step.checkpoint is not None:
                # §6b tiered wait: async-rendered pages (ParaBank renders
                # account data via JMS with a delay) transiently lack the
                # page-identity heading, so the instant post-action
                # observation can fall back to the title. Re-observe with
                # backoff before declaring a mismatch.
                observation = self._await_checkpoint(step.checkpoint)
                if observation is None:
                    observation = self.adapter.observe()
                if not self._checkpoint_matches(step.checkpoint, observation):
                    match = self._match_outcome_signature(step)
                    if match is not None:
                        signature, matched_text = match
                        if (
                            signature.classification
                            is OutcomeClass.BUSINESS_OUTCOME
                        ):
                            self._log(
                                index,
                                step,
                                "business_outcome",
                                message=matched_text,
                                classification="business_outcome",
                            )
                            # §7b: a business outcome short-circuits the run
                            # immediately with its own result type.
                            return BusinessOutcomeResult(
                                artifact_name=artifact.name,
                                steps_executed=steps_executed,
                                step_index=index,
                                classification=signature.classification,
                                description=signature.description,
                                matched_text=matched_text,
                                outputs=outputs,
                            )
                        if signature.classification is OutcomeClass.HARD_FAILURE:
                            self._log(
                                index,
                                step,
                                "failure",
                                message=matched_text,
                                classification="recorded_hard_failure",
                            )
                            return FailureResult(
                                artifact_name=artifact.name,
                                steps_executed=steps_executed - 1,
                                reason="recorded_hard_failure",
                                failed_step=index,
                                expected=(
                                    f"signature {signature.text_pattern!r} "
                                    f"({signature.description or 'no description'})"
                                ),
                                observed=matched_text,
                            )
                        # RECOVERABLE (§7b): handled inline — the recorded
                        # signature says the flow proceeds from this state,
                        # so the run continues to the next step. Full
                        # automatic recovery procedures are a documented
                        # future upgrade, not silently assumed here.
                        self._log(
                            index,
                            step,
                            "recoverable",
                            message=matched_text,
                            classification="recoverable_continued",
                        )
                    else:
                        self._log(
                            index,
                            step,
                            "failure",
                            message=(
                                f"checkpoint mismatch: expected "
                                f"{self._checkpoint_str(step)}, observed "
                                f"{self._observation_str(observation)}"
                            ),
                            classification="checkpoint_mismatch",
                        )
                        return FailureResult(
                            artifact_name=artifact.name,
                            steps_executed=steps_executed - 1,
                            reason="checkpoint_mismatch",
                            failed_step=index,
                            expected=self._checkpoint_str(step),
                            observed=self._observation_str(observation),
                        )

        # The deliberate terminal checkpoint (§4e) — with the same §6b
        # tiered wait for async-rendered pages.
        observation = self._await_checkpoint(artifact.terminal_checkpoint)
        if observation is None:
            observation = self.adapter.observe()
        if not self._checkpoint_matches(artifact.terminal_checkpoint, observation):
            self._log(
                len(artifact.steps) - 1,
                Step(action=ActionType.CLICK, locator_candidates=[]),
                "failure",
                message=(
                    f"terminal checkpoint mismatch: expected "
                    f"{self._checkpoint_str_of(artifact.terminal_checkpoint)}, "
                    f"observed {self._observation_str(observation)}"
                ),
                classification="terminal_checkpoint_mismatch",
            )
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
    ) -> LocatorCandidate | None:
        """Execute one step; return the candidate locator that resolved it
        (None for navigate), so evidence logging records which strategy
        tier won (§10a/§11b)."""
        action = step.action
        if action is ActionType.NAVIGATE:
            url = step.navigate_url or ""
            if self.allowlist is not None:
                self.allowlist.check_navigate(url)
            self.adapter.navigate(url)
            return None
        if action is ActionType.EXTRACT:
            candidate, locator = self._resolve(step, index)
            try:
                value = str(locator.input_value())
            except Exception:
                value = locator.inner_text()
            if step.output_name:
                outputs[step.output_name] = value
            return candidate

        # click / type_text / select_option all need a resolved element.
        candidate, locator = self._resolve(step, index)
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
        return candidate

    @staticmethod
    def _step_value(step: Step, validated: dict[str, str]) -> str:
        if step.is_constant:
            return step.value or ""
        return validated[step.input_name or ""]

    # -- Evidence logging (§10a) ------------------------------------------------

    @staticmethod
    def _log_params(step: Step, validated: dict[str, str]) -> dict[str, str]:
        """The values a step actually applied — what the evidence log
        records (and the redaction writer masks when the input/output name
        is sensitive, §8c)."""
        if step.action is ActionType.NAVIGATE:
            return {"url": step.navigate_url or ""}
        if step.is_constant:
            return {"value": step.value or ""}
        if step.input_name:
            return {"value": validated.get(step.input_name, "")}
        return {}

    def _log(
        self,
        index: int,
        step: Step,
        status: str,
        *,
        message: str = "",
        params: dict[str, str] | None = None,
        data: str | None = None,
        strategy: str | None = None,
        classification: str | None = None,
    ) -> None:
        if self.step_logger is None:
            return
        self.step_logger.log_step(
            step=index,
            action=step.action.value,
            status=status,
            params=params or {},
            message=message,
            data=data,
            element_name=step.input_name or step.output_name or "",
            locator_strategy=strategy,
            url=self.adapter.current_url(),
            result_classification=classification,
        )

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

    def _match_outcome_signature(
        self, step: Step
    ) -> tuple[OutcomeSignature, str] | None:
        """Check a step's recorded alternate-outcome signatures (§7a)
        against the live page: locator resolves AND text pattern matches.
        Returns the first match, or None when nothing matches — the caller
        then falls back to a hard failure."""
        for signature in step.outcome_signatures:
            try:
                locator = self.adapter.resolve_locator(signature.locator)
                if locator.count() < 1:
                    continue
                try:
                    text = str(locator.input_value())
                except Exception:
                    text = locator.inner_text()
            except Exception:
                continue
            if re.search(signature.text_pattern, text, re.IGNORECASE):
                return signature, text
        return None

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

    def _await_checkpoint(self, checkpoint) -> Observation | None:
        """§6b tiered waiting for checkpoint verification: async-rendered
        pages (ParaBank renders account data via JMS with a delay)
        transiently lack the page-identity heading, so the observation
        immediately after an action can fall back to the page title and
        fail to match a checkpoint recorded in the settled state.
        Re-observe with backoff; return the matching observation, or None
        when the checkpoint never settles."""
        for delay in self.retry_delays:
            if delay:
                time.sleep(delay)
            observation = self.adapter.observe()
            if self._checkpoint_matches(checkpoint, observation):
                return observation
        return None

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
        return checkpoint_matches(checkpoint, observation)

    @staticmethod
    def _normalize_url(url: str) -> str:
        return normalize_url(url)


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
