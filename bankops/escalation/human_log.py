"""Human-action recording (Phase 8; DECISIONS.md §9d).

Passive before/after capture: screenshot and observation snapshot at
pause and at resume, plus the navigation/URL trail sampled during the
human's turn — all reusing the existing screenshot/evidence machinery.
Full input-event-level capture was deliberately ruled out as
disproportionate (§9d); this is a documented limitation, not a silent
gap.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class Capture(BaseModel):
    """One state capture: URL, page-identity signal, timestamp, and the
    screenshot taken at that moment."""

    url: str = ""
    page_identity: str = ""
    timestamp: str = ""
    screenshot_path: str | None = None


class HumanTurnLog(BaseModel):
    """The complete record of one human turn (§9d): pause capture, the URL
    trail sampled while the human operated the session, and the resume
    capture."""

    run_id: str
    request_id: str
    before: Capture | None = None
    nav_trail: list[str] = Field(default_factory=list)
    after: Capture | None = None

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "HumanTurnLog":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
