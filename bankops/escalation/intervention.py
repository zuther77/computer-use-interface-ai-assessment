"""InterventionRequest model + persistence (Phase 8; DECISIONS.md §9a).

A structured request — run_id, source, goal/artifact reference,
step_index, reason, screenshot_path, observation snapshot, timestamp —
persisted to /pending_interventions/{id}.json the moment it is raised.
Notification routing is stubbed as a log line (out of scope by §9a), but
the request object itself is real and complete.

Both triggers are wired here: ``report_stuck`` / other typed discovery
stops (§3d → §9a) and mid-flow replay hard failures.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from bankops import settings
from bankops.perception.base import Observation, PerceptionAdapter

logger = logging.getLogger("bankops.escalation")


class InterventionRequest(BaseModel):
    """A human intervention request (DECISIONS.md §9a) — carries enough
    context for a human to act on, not just a log line."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    source: Literal["discovery", "replay"]
    reference: str  # the goal (discovery) or artifact name (replay)
    step_index: int | None = None
    reason: str
    url: str = ""
    observation: Observation
    screenshot_path: str | None = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def save(self, directory: str | Path | None = None) -> Path:
        directory = Path(directory or settings.PENDING_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.id}.json"
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "InterventionRequest":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


def raise_intervention(
    adapter: PerceptionAdapter,
    *,
    run_id: str,
    source: Literal["discovery", "replay"],
    reference: str,
    reason: str,
    step_index: int | None = None,
    pending_dir: str | Path | None = None,
) -> InterventionRequest:
    """Capture the current state and persist a complete intervention
    request immediately (§9a: persisted *the moment* it's raised)."""
    pending_dir = Path(pending_dir or settings.PENDING_DIR)
    pending_dir.mkdir(parents=True, exist_ok=True)

    observation = adapter.observe()
    request = InterventionRequest(
        run_id=run_id,
        source=source,
        reference=reference,
        step_index=step_index,
        reason=reason,
        url=observation.url,
        observation=observation,
    )
    # Pause-state screenshot (§9d before/after capture). Screenshot failure
    # must never block escalation — the observation snapshot still ships.
    try:
        request.screenshot_path = str(
            adapter.capture_screenshot(pending_dir / f"{request.id}_pause.png")
        )
    except Exception as exc:  # pragma: no cover — environment-dependent
        logger.warning("pause screenshot failed: %s", exc)

    request.save(pending_dir)
    # Notification routing is deliberately stubbed (§9a): a log line now,
    # real routing infrastructure is out of scope.
    logger.warning(
        "INTERVENTION RAISED [%s] run=%s step=%s reason=%s — awaiting human "
        "at %s",
        request.source,
        request.run_id,
        request.step_index,
        request.reason,
        request.url,
    )
    return request


def escalate_discovery_stop(
    result,
    adapter: PerceptionAdapter,
    *,
    run_id: str,
    pending_dir: str | Path | None = None,
) -> InterventionRequest | None:
    """Wire typed non-completed discovery stops (report_stuck and its
    siblings, §3d/§9a) as escalation triggers. A completed run escalates
    nothing."""
    from bankops.agent.loop import RunStatus

    if result.status is RunStatus.COMPLETED:
        return None
    step_index = result.action_log[-1].step if result.action_log else None
    return raise_intervention(
        adapter,
        run_id=run_id,
        source="discovery",
        reference=result.goal,
        step_index=step_index,
        reason=f"{result.status.value}: {result.reason}",
        pending_dir=pending_dir,
    )


def escalate_replay_failure(
    failure,
    adapter: PerceptionAdapter,
    *,
    run_id: str,
    pending_dir: str | Path | None = None,
) -> InterventionRequest | None:
    """Wire mid-flow replay hard failures (§9a) as escalation triggers.
    Pre-flight rejections never touched the browser (nothing to hand off)
    and a confirmation gate is resolved by re-invoking with confirm=True,
    not by a human on the live page — neither escalates."""
    if failure.failed_step is None:
        return None
    if failure.reason == "confirmation_required":
        return None
    return raise_intervention(
        adapter,
        run_id=run_id,
        source="replay",
        reference=failure.artifact_name,
        step_index=failure.failed_step,
        reason=(
            f"{failure.reason}: expected {failure.expected}; "
            f"observed {failure.observed}"
        ),
        pending_dir=pending_dir,
    )
