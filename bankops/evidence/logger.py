"""JSONL step-log writer (Phase 9; DECISIONS.md §10a).

One typed StepLogEntry record per step (action, locator used + which
strategy tier resolved, observation summary, LLM reasoning, result
classification, timestamp), appended incrementally so a mid-run crash
doesn't lose already-logged data. Redaction (DECISIONS.md §8c) is applied
before anything is written.
"""
