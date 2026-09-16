"""Pause/resume handoff on a headed browser (Phase 8; DECISIONS.md §9b, §9c).

On escalation the loop stops issuing commands entirely and blocks on a
resume signal while a human operates the same visible Playwright-controlled
window — the "operator console" is the raw browser window itself (§9b).
The handoff mechanism is real, not simulated.

Resume (§9c): by default automation re-observes and verifies the next
step's expected checkpoint against the current state — a mismatch raises
a *new* intervention rather than guessing; a human can also explicitly
declare continue / mark_complete / abandon for cases checkpoint-matching
alone can't resolve. A discovery resume has no checkpoint to verify — the
loop simply continues its normal per-step cycle from the new state.
"""

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel

from bankops import settings
from bankops.escalation.human_log import Capture, HumanTurnLog
from bankops.escalation.intervention import InterventionRequest, raise_intervention
from bankops.perception.base import Observation, PerceptionAdapter
from bankops.replay.engine import checkpoint_matches

_VERIFY_WORDS = {"verify", "resume", ""}


class ResumeSignal(str, Enum):
    """What the human declared on resume (§9c). VERIFY is the default —
    re-observe and checkpoint-verify; the others are explicit overrides."""

    VERIFY = "verify"
    CONTINUE = "continue"
    MARK_COMPLETE = "mark_complete"
    ABANDON = "abandon"


class HandoffAction(str, Enum):
    RESUME = "resume"
    MARK_COMPLETE = "mark_complete"
    ABANDON = "abandon"


class HandoffResult(BaseModel):
    action: HandoffAction
    signal: str
    verified: bool  # checkpoint verification ran and passed
    observation: Observation
    request: InterventionRequest
    new_request: InterventionRequest | None = None


class ResumeWaiter(Protocol):
    """Blocks until a human resume signal arrives (§9b: CLI/file/stdin).
    Implementations may expose a ``nav_trail`` list of URLs sampled while
    waiting (§9d human navigation trail)."""

    def __call__(self, request: InterventionRequest) -> ResumeSignal: ...


class FileResumeWaiter:
    """Resume via a file: a human (or tooling) writes one of
    ``continue`` / ``mark_complete`` / ``abandon`` / ``verify`` (or just
    ``resume``) to ``{pending_dir}/{request_id}.resume``."""

    def __init__(
        self,
        adapter: PerceptionAdapter,
        *,
        pending_dir: str | Path | None = None,
        poll_interval: float = 0.5,
        timeout: float | None = None,
    ):
        self.adapter = adapter
        self.pending_dir = Path(pending_dir or settings.PENDING_DIR)
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.nav_trail: list[str] = []

    def __call__(self, request: InterventionRequest) -> ResumeSignal:
        resume_path = self.pending_dir / f"{request.id}.resume"
        deadline = time.monotonic() + self.timeout if self.timeout else None
        while True:
            try:
                url = self.adapter.current_url()
                if url and (not self.nav_trail or self.nav_trail[-1] != url):
                    self.nav_trail.append(url)
            except Exception:  # pragma: no cover — surface mid-handoff
                pass
            if resume_path.exists():
                content = resume_path.read_text(encoding="utf-8").strip().lower()
                resume_path.unlink()  # consume the signal
                return self.parse(content)
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"no resume signal written to {resume_path} within "
                    f"{self.timeout}s"
                )
            time.sleep(self.poll_interval)

    @staticmethod
    def parse(content: str) -> ResumeSignal:
        if content in _VERIFY_WORDS:
            return ResumeSignal.VERIFY
        if content == ResumeSignal.CONTINUE.value:
            return ResumeSignal.CONTINUE
        if content == ResumeSignal.MARK_COMPLETE.value:
            return ResumeSignal.MARK_COMPLETE
        if content == ResumeSignal.ABANDON.value:
            return ResumeSignal.ABANDON
        raise ValueError(
            f"unrecognized resume signal {content!r} — expected one of: "
            f"verify/resume, continue, mark_complete, abandon"
        )


class HandoffManager:
    """Runs pause/resume cycles on the *same live session* (§9b/§6d)."""

    def __init__(self, adapter: PerceptionAdapter, *, pending_dir: str | Path | None = None):
        self.adapter = adapter
        self.pending_dir = Path(pending_dir or settings.PENDING_DIR)

    def run_handoff(
        self,
        request: InterventionRequest,
        *,
        wait_for_resume: ResumeWaiter,
        expected_checkpoint=None,
        max_cycles: int = 5,
    ) -> HandoffResult:
        """Pause (request already raised and persisted), block for the
        human, then resume per §9c. A failed verification raises a *new*
        intervention and pauses again — never a blind resume."""
        current = request
        for _ in range(max_cycles):
            signal = wait_for_resume(current)
            nav_trail = list(getattr(wait_for_resume, "nav_trail", []) or [])

            after_observation = self.adapter.observe()
            after_screenshot = None
            try:
                after_screenshot = str(
                    self.adapter.capture_screenshot(
                        self.pending_dir / f"{current.id}_resume.png"
                    )
                )
            except Exception:  # pragma: no cover — evidence only
                pass

            HumanTurnLog(
                run_id=current.run_id,
                request_id=current.id,
                before=Capture(
                    url=request.observation.url,
                    page_identity=request.observation.page_identity,
                    timestamp=request.timestamp,
                    screenshot_path=request.screenshot_path,
                ),
                nav_trail=nav_trail,
                after=Capture(
                    url=after_observation.url,
                    page_identity=after_observation.page_identity,
                    screenshot_path=after_screenshot,
                ),
            ).save(self.pending_dir / f"{current.id}.human_log.json")

            if signal is ResumeSignal.ABANDON:
                return HandoffResult(
                    action=HandoffAction.ABANDON,
                    signal=signal.value,
                    verified=False,
                    observation=after_observation,
                    request=request,
                )
            if signal is ResumeSignal.MARK_COMPLETE:
                return HandoffResult(
                    action=HandoffAction.MARK_COMPLETE,
                    signal=signal.value,
                    verified=False,
                    observation=after_observation,
                    request=request,
                )
            if signal is ResumeSignal.CONTINUE:
                # Explicit override (§9c): resume without verification —
                # the human takes responsibility for the state.
                return HandoffResult(
                    action=HandoffAction.RESUME,
                    signal=signal.value,
                    verified=False,
                    observation=after_observation,
                    request=request,
                )

            # VERIFY — the default resume path (§9c).
            if expected_checkpoint is None:
                # Discovery resume: nothing to verify — the loop re-observes
                # every step anyway; continuing from the new state is safe.
                return HandoffResult(
                    action=HandoffAction.RESUME,
                    signal=signal.value,
                    verified=True,
                    observation=after_observation,
                    request=request,
                )
            if checkpoint_matches(expected_checkpoint, after_observation):
                return HandoffResult(
                    action=HandoffAction.RESUME,
                    signal=signal.value,
                    verified=True,
                    observation=after_observation,
                    request=request,
                )
            # Mismatch → a *new* intervention, not a guess (§9c).
            current = raise_intervention(
                self.adapter,
                run_id=current.run_id,
                source=current.source,
                reference=current.reference,
                step_index=current.step_index,
                reason=(
                    f"resume verification failed: expected "
                    f"{expected_checkpoint.url} / "
                    f"{expected_checkpoint.page_identity!r}, observed "
                    f"{after_observation.url} / "
                    f"{after_observation.page_identity!r}"
                ),
                pending_dir=self.pending_dir,
            )

        raise RuntimeError(
            f"handoff exceeded {max_cycles} resume cycles without resolution"
        )
