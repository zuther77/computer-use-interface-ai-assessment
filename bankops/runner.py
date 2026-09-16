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
) -> tuple[RunResult, Artifact | None]:
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
    loop = DiscoveryLoop(
        client or OpenAIToolCallingClient(),
        ctx,
        max_steps=max_steps,
        step_logger=logger,
    )
    result = loop.run(goal)

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
        }
    )

    artifact: Artifact | None = None
    if result.status is RunStatus.COMPLETED:
        artifact = distill(result, name=artifact_name)
        if artifact is not None:
            store = artifacts_dir(evidence_dir=evidence_dir)
            artifact.save(store / f"{artifact_name}.json")
            artifact.save(directory / f"artifact_{artifact_name}.json")
    return result, artifact


def run_replay(
    artifact: Artifact,
    params: dict[str, Any],
    *,
    adapter: PerceptionAdapter,
    allowlist: Allowlist | None = None,
    run_id: str | None = None,
    evidence_dir: str | Path | None = None,
    confirm: bool = False,
    retry_delays: tuple[float, ...] = (0.0, 1.0, 3.0),
) -> SuccessResult | ReplayResult:
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

    if isinstance(result, FailureResult):
        # §10b: business-outcome/failure runs live under replay_error_{id}/.
        error_dir = run_dir("replay_error", run_id, evidence_dir=evidence_dir)
        directory.rename(error_dir)
        directory = error_dir

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

    return result
