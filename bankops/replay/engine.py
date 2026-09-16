"""Replay engine (Phase 6; DECISIONS.md §6a–6d, §7b, §7c).

Pre-flight Pydantic validation of caller params; priority-order locator
resolution (first unique match wins); tiered wait/retry with backoff;
per-invocation session lifecycle (torn down on success/hard-failure, kept
alive on escalation exit). Outcome signatures (Phase 7; §7a) are checked
before falling back to hard failure.
"""
