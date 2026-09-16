"""Risk-tag / confirm=True gate (Phase 3; DECISIONS.md §8b).

Per-artifact risk tags (read_only / state_changing_reversible /
state_changing_irreversible) set manually at authoring time; irreversible
artifacts flag their point-of-no-return step, and replay requires an
explicit confirm=True before executing that step.
"""
