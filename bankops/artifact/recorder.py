"""Recorder / distillation pipeline (Phase 5; DECISIONS.md §5a–5c).

Given a raw action log from a ``finish``-terminated discovery run:
successful-path-only filtering (last-write-wins per target, abandoned
branches dropped), classification of typed values into InputParams and
extract results into OutputFields, then assembly of the final Artifact.
"""
