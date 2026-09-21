"""End-to-end pipeline glue for the Phase 10 live demo runs.

Thin orchestration over the Phase 2–9 building blocks: run a real
discovery (GLM via the OpenAI SDK), distill and store the artifact,
replay an artifact deterministically, and file every run under its
per-run evidence folder (§10b) with a redacted JSONL log (§10a) and
final screenshot (§2b).

These are the entry points for live runs against ParaBank — unit tests
elsewhere in this project never exercise them with a live LLM.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from bankops import settings
from bankops.actions.tools import ToolContext
from bankops.agent.loop import (
    ActionLogEntry,
    DiscoveryLoop,
    OpenAIToolCallingClient,
    RunResult,
    RunStatus,
)
from bankops.artifact.models import Artifact
from bankops.artifact.recorder import distill
from bankops.escalation.handoff import HandoffAction, HandoffResult
from bankops.escalation.intervention import (
    InterventionRequest,
    escalate_discovery_stop,
    escalate_replay_failure,
)

# Bounded pause/resume cycles per discovery run (§9c): a human may verify
# and resume a halted loop; each resume gets a fresh per-cycle step budget.
_MAX_HANDOFF_CYCLES = 3
from bankops.evidence.logger import StepLogger
from bankops.evidence.paths import artifacts_dir, run_dir
from bankops.perception.base import PerceptionAdapter
from bankops.replay.engine import ReplayEngine
from bankops.replay.results import FailureResult, ReplayResult, SuccessResult
from bankops.safety.allowlist import Allowlist


def _new_run_id(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}"


def _default_allowlist() -> Allowlist:
    path = Path(settings.ALLOWLIST_PATH)
    if path.exists():
        return Allowlist.load(path)
    raise FileNotFoundError(
        f"allowlist config not found at {path} — required for live runs (§8a)"
    )


def run_discovery(
    goal: str,
    *,
    artifact_name: str,
    adapter: PerceptionAdapter,
    client: Any | None = None,
    allowlist: Allowlist | None = None,
    run_id: str | None = None,
    evidence_dir: str | Path | None = None,
    max_steps: int | None = None,
    progress: Any | None = None,
    pending_dir: str | Path | None = None,
    handoff: Any | None = None,
) -> tuple[RunResult, Artifact | None, InterventionRequest | None]:
    """One live discovery run: observe → decide → act with the real LLM,
    logged to evidence/discovery_run_{id}/. If (and only if) it ends via
    ``finish`` (§5c), the run is distilled into an artifact stored in the
    canonical artifact store."""
    run_id = run_id or _new_run_id("discovery")
    directory = run_dir("discovery", run_id, evidence_dir=evidence_dir)
    logger = StepLogger(directory, run_id=run_id, run_kind="discovery")

    ctx = ToolContext(
        adapter=adapter, allowlist=allowlist or _default_allowlist()
    )
    handoff_cycles: list[dict[str, Any]] = []
    request: InterventionRequest | None = None
    result: RunResult | None = None
    start_step = 1
    seed_log: list[ActionLogEntry] = []
    try:
        for _cycle in range(_MAX_HANDOFF_CYCLES):
            loop = DiscoveryLoop(
                client or OpenAIToolCallingClient(),
                ctx,
                max_steps=max_steps,
                step_logger=logger,
                on_progress=progress,
                start_step=start_step,
                action_log=seed_log,
            )
            result = loop.run(goal)
            if result.status is RunStatus.COMPLETED:
                break
            # §9a: any typed non-completed stop is an escalation trigger —
            # the request is persisted the moment it's raised, complete
            # with the current observation and screenshot.
            request = escalate_discovery_stop(
                result, adapter, run_id=run_id, pending_dir=pending_dir
            )
            if handoff is None:
                break  # headless / library use: the request stays pending
            outcome = handoff(request)
            handoff_cycles.append(
                {
                    "request_id": request.id,
                    "action": outcome.action.value,
                    "verified": outcome.verified,
                }
            )
            if outcome.action is not HandoffAction.RESUME:
                break  # the human ended the run (mark_complete / abandon)
            # §9c discovery resume: the loop continues its normal per-step
            # cycle from the new state — the resumed loop re-observes
            # first, so whatever the human did becomes the next decision's
            # input. State (§3c) carries over; step numbering continues.
            start_step = (
                result.action_log[-1].step + 1 if result.action_log else start_step
            )
            seed_log = list(result.action_log)
    except BaseException as exc:
        # Evidence durability: a mid-run crash must still leave the run's
        # final screenshot and summary behind (§10a/§10b) — the JSONL step
        # log is already written incrementally per step.
        try:
            adapter.capture_screenshot(directory / "final.png")
        except Exception:
            pass
        logger.write_summary(
            {
                "status": "crashed",
                "reason": f"{type(exc).__name__}: {exc}",
                "goal": goal,
                "memory": ctx.memory,
            }
        )
        raise

    assert result is not None

    try:
        adapter.capture_screenshot(directory / "final.png")
    except Exception:  # evidence only — never block the run result
        pass

    final_url = result.final_observation.url if result.final_observation else ""
    final_identity = (
        result.final_observation.page_identity
        if result.final_observation
        else ""
    )
    logger.write_summary(
        {
            "status": result.status.value,
            "reason": result.reason,
            "goal": goal,
            "memory": result.memory,
            "final_url": final_url,
            "final_page_identity": final_identity,
            "handoffs": handoff_cycles,
        },
        mask_values=result.sensitive_values,
    )

    artifact: Artifact | None = None
    if result.status is RunStatus.COMPLETED:
        artifact = distill(result, name=artifact_name)
        if artifact is not None:
            store = artifacts_dir(evidence_dir=evidence_dir)
            artifact.save(store / f"{artifact_name}.json")
            artifact.save(directory / f"artifact_{artifact_name}.json")
    return result, artifact, request


def run_replay(
    artifact: Artifact,
    params: dict[str, Any],
    *,
    adapter: PerceptionAdapter,
    allowlist: Allowlist | None = None,
    run_id: str | None = None,
    evidence_dir: str | Path | None = None,
    pending_dir: str | Path | None = None,
    confirm: bool = False,
    retry_delays: tuple[float, ...] = (0.0, 1.0, 3.0),
) -> tuple[ReplayResult, InterventionRequest | None]:
    """One deterministic replay, logged to evidence/replay_success_{id}/
    (renamed to replay_error_{id}/ when the run classifies as a business
    outcome or a failure — the required /evidence/replay_error_*/
    deliverable, §10b)."""
    run_id = run_id or _new_run_id("replay")
    directory = run_dir("replay_success", run_id, evidence_dir=evidence_dir)
    logger = StepLogger(directory, run_id=run_id, run_kind="replay")

    engine = ReplayEngine(
        adapter,
        allowlist=allowlist or _default_allowlist(),
        retry_delays=retry_delays,
        step_logger=logger,
    )
    result = engine.execute(artifact, params, confirm=confirm)

    replay_request: InterventionRequest | None = None
    if isinstance(result, FailureResult):
        # §10b: business-outcome/failure runs live under replay_error_{id}/.
        error_dir = run_dir("replay_error", run_id, evidence_dir=evidence_dir)
        directory.rename(error_dir)
        directory = error_dir
        # §9a: mid-flow hard failures are escalation triggers. The function
        # itself never escalates pre-flight rejections or confirmation
        # gates — nothing to hand off on the live session.
        replay_request = escalate_replay_failure(
            result, adapter, run_id=run_id, pending_dir=pending_dir
        )

    try:
        adapter.capture_screenshot(directory / "final.png")
    except Exception:  # evidence only
        pass

    summary: dict[str, Any] = {
        "artifact": artifact.name,
        "status": result.status,
        "steps_executed": getattr(result, "steps_executed", None),
    }
    if isinstance(result, SuccessResult):
        summary["outputs"] = result.outputs
    elif isinstance(result, FailureResult):
        summary["reason"] = result.reason
        summary["failed_step"] = result.failed_step
        summary["expected"] = result.expected
        summary["observed"] = result.observed
    else:
        summary["step_index"] = result.step_index
        summary["classification"] = result.classification.value
        summary["matched_text"] = result.matched_text
    # Bind the summary writer to the *final* directory — after a rename the
    # streaming logger's path no longer exists.
    StepLogger(directory, run_id=run_id, run_kind="replay").write_summary(summary)

    return result, replay_request
