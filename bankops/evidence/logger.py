"""JSONL step-log writer (Phase 9; DECISIONS.md §10a, §8c).

One typed ``StepLogEntry`` record per step — action taken, locator used +
which strategy tier resolved, observation summary, result classification,
timestamp — appended incrementally, so a mid-run crash doesn't lose
already-logged data. Redaction (§8c) is applied *inside* this writer,
before anything is written: the on-disk log is the redacted record, never
a post-hoc cleanup target.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bankops.safety.redaction import redact_action, redact_nested


class StepLogEntry(BaseModel):
    """One typed step record (§10a). ``locator_strategy`` records which
    candidate-locator tier resolved the step (§4b) — the same field the
    multi-tenant drift story keys on (§11b)."""

    run_id: str
    run_kind: str  # "discovery" | "replay"
    step: int
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    action: str
    status: str
    params: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    data: Any = None
    element_name: str = ""
    locator_strategy: str | None = None
    url: str = ""
    observation_summary: str = ""
    result_classification: str = ""


class StepLogger:
    """Appends one JSON line per step to ``{run_dir}/steps.jsonl``."""

    def __init__(self, run_dir: str | Path, *, run_id: str, run_kind: str):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "steps.jsonl"
        self.run_id = run_id
        self.run_kind = run_kind
        self.lines_written = 0

    def log_step(
        self,
        *,
        step: int,
        action: str,
        status: str,
        params: dict[str, Any] | None = None,
        message: str = "",
        data: Any = None,
        element_name: str = "",
        locator_strategy: str | None = None,
        url: str = "",
        observation_summary: str = "",
        result_classification: str | None = None,
    ) -> StepLogEntry:
        """Redact (§8c), then append one line. The redaction happens here —
        before the write — never after."""
        safe_params, safe_data = redact_action(
            action, params or {}, element_name=element_name, data=data
        )
        entry = StepLogEntry(
            run_id=self.run_id,
            run_kind=self.run_kind,
            step=step,
            action=action,
            status=status,
            params=safe_params,
            message=message,
            data=safe_data,
            element_name=element_name,
            locator_strategy=locator_strategy,
            url=url,
            observation_summary=observation_summary,
            result_classification=result_classification or status,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
        self.lines_written += 1
        return entry

    def write_summary(self, payload: dict[str, Any]) -> Path:
        """Write a per-run ``summary.json`` (status, reason, memory, …)
        with sensitive-named keys masked at every nesting level (§8c)."""
        path = self.run_dir / "summary.json"
        path.write_text(
            json.dumps(redact_nested(payload), indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def read_entries(self) -> list[StepLogEntry]:
        """Load back the written JSONL (for verification/tests)."""
        return [
            StepLogEntry.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
