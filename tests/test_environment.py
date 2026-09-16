"""Phase 0 smoke test: the package imports cleanly and local ParaBank's
login page is reachable at the configured URL (IMPLEMENTATION_PLAN.md, Phase 0).

If ParaBank is not running, the reachability test skips with instructions
(``docker compose up -d``) rather than failing — all other test topics in
this project run against local static fixtures / mocks, never live ParaBank.
"""

from __future__ import annotations

import importlib
import urllib.error
import urllib.request

import pytest

import bankops  # noqa: F401
from bankops import (
    actions,
    agent,
    artifact,
    escalation,
    evidence,
    perception,
    replay,
    safety,
    settings,
)


def test_package_imports_cleanly() -> None:
    """The whole skeleton imports without error (no logic yet)."""
    for module in (
        bankops,
        artifact,
        perception,
        actions,
        safety,
        agent,
        replay,
        escalation,
        evidence,
        settings,
    ):
        assert importlib.import_module(module.__name__) is not None


def _parabank_is_up() -> bool:
    url = f"{settings.PARABANK_BASE_URL}/index.htm"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


@pytest.mark.skipif(
    not _parabank_is_up(),
    reason="ParaBank not reachable — run `docker compose up -d` and wait for boot",
)
def test_parabank_login_page_reachable() -> None:
    """ParaBank's login page is reachable and shows the login form."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"{settings.PARABANK_BASE_URL}/index.htm", timeout=15_000)
            assert page.get_by_role("button", name="Log In").count() == 1
            assert page.get_by_role("link", name="Register").count() >= 1
        finally:
            browser.close()
