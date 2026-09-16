"""bankops — Computer-use automation system for ParaBank.

Phase layout (see IMPLEMENTATION_PLAN.md / DECISIONS.md):

- ``bankops.artifact``   — Pydantic artifact schema + recorder (Phases 1, 5, 7)
- ``bankops.perception``— PerceptionAdapter interface + Playwright adapter (Phase 2)
- ``bankops.actions``   — typed action tools (Phase 3)
- ``bankops.safety``    — allowlist, risk gates, redaction (Phase 3)
- ``bankops.agent``     — discovery loop (Phase 4)
- ``bankops.replay``    — deterministic replay engine + result union (Phases 6–7)
- ``bankops.escalation``— intervention, handoff, human-action log (Phase 8)
- ``bankops.evidence``   — JSONL logging + per-run evidence paths (Phase 9)
"""

__version__ = "0.1.0"
