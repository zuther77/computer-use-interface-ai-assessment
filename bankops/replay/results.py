"""Replay result types (Phase 6; DECISIONS.md §7b, §7c).

Discriminated union of SuccessResult / BusinessOutcomeResult /
FailureResult, each carrying only the fields relevant to that outcome, plus
the first-non-success-short-circuits rollup logic.
"""
