"""Typed action tools (Phase 3; DECISIONS.md §3b, §8a).

One typed tool per action type — click, type_text, select_option, navigate,
extract, remember, finish, report_stuck — each taking an index/locator from
the perception adapter's output. No generic act() and no raw code
generation: the closed, auditable action space is what the allowlist
(DECISIONS.md §8a) depends on.
"""
