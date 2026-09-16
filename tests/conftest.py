"""Project-wide test configuration.

Evidence redaction (DECISIONS.md §8c) is fully implemented and verified
here, but disabled by default in the running system "for now" per operator
preference (settings.REDACT, default false). Tests force it ON so every
masking guarantee stays pinned regardless of the default; the off path is
covered by explicit toggle tests.
"""

from __future__ import annotations

import pytest

from bankops import settings


@pytest.fixture(autouse=True)
def _redaction_enabled():
    original = settings.REDACT
    settings.REDACT = True
    yield
    settings.REDACT = original
