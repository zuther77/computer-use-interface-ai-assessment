"""Per-run evidence folder conventions (Phase 9; DECISIONS.md §10b).

One self-contained subfolder per run — ``discovery_run_{id}/``,
``replay_success_{id}/``, ``replay_error_{id}/`` — plus a shared canonical
artifact store at ``evidence/artifacts/``. A flat dump of all files was
ruled out as harder for a reviewer to navigate.
"""

from __future__ import annotations

from pathlib import Path

from bankops import settings

# run kind -> folder-name prefix (DECISIONS.md §10b)
RUN_KINDS: dict[str, str] = {
    "discovery": "discovery_run",
    "replay_success": "replay_success",
    "replay_error": "replay_error",
}


def run_dir(
    kind: str,
    run_id: str,
    *,
    evidence_dir: str | Path | None = None,
) -> Path:
    """Create (idempotently) and return a per-run evidence folder."""
    if kind not in RUN_KINDS:
        raise ValueError(
            f"unknown run kind {kind!r} (expected one of {sorted(RUN_KINDS)})"
        )
    directory = Path(evidence_dir or settings.EVIDENCE_DIR) / (
        f"{RUN_KINDS[kind]}_{run_id}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def artifacts_dir(*, evidence_dir: str | Path | None = None) -> Path:
    """The shared canonical artifact store (§10b)."""
    directory = Path(evidence_dir or settings.EVIDENCE_DIR) / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
