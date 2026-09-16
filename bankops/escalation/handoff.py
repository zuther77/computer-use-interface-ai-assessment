"""Pause/resume handoff on a headed browser (Phase 8; DECISIONS.md §9b, §9c).

On escalation the loop stops issuing commands entirely and blocks on a
resume signal while a human operates the same visible Playwright-controlled
window. Resume re-observes and checkpoint-verifies by default, or accepts
an explicit continue / mark_complete / abandon override.
"""
