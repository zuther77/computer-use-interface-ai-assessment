"""Discovery agent loop (Phase 4; DECISIONS.md §2d, §3c, §3d).

Observe→decide→act: GLM via the OpenAI SDK tool-calling interface, over a
perception adapter and typed tools. Structured state (current observation +
compact action-history log + remember store, no transcript replay) and
layered stopping conditions (max steps, no-progress via observation-hash
comparison, finish/report_stuck self-report).
"""
