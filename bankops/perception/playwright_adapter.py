"""Playwright-backed PerceptionAdapter (Phase 2; DECISIONS.md §2b, §2c, §3a).

Walks the accessibility snapshot, emits an indexed list of interactive
elements, each with ordered candidate locators (role_name, id_attribute,
text_content, css_structural). Screenshots are captured as a separate,
parallel evidence call — never a decision input.
"""
