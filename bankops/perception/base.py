"""PerceptionAdapter interface (Phase 2; DECISIONS.md §2b, §3a, §11a).

The formal seam between the rest of the system and any automation surface:
"give me an indexed list of interactive elements with candidate locators"
and "capture a screenshot as parallel evidence". A legacy-web or desktop
adapter would implement the same interface with different locator-strategy
tags; the artifact schema, replay engine, and error taxonomy operate only
on this output shape, not on browser DOM specifics.
"""
