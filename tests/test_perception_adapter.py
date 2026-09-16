"""Perception adapter tests (Phase 2; DECISIONS.md §2b, §2c, §3a).

Run against a small local static HTML fixture — never live ParaBank — for
determinism (per the plan's Phase 2 test note). Covers correct indexing,
candidate locator generation in priority order, duplicate-label
disambiguation, screenshot capture, and locator resolution.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from bankops.artifact.models import LocatorStrategy
from bankops.perception.base import Observation, PerceptionAdapter
from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "perception_fixture.html"


@pytest.fixture(scope="module")
def adapter() -> PerceptionAdapter:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FIXTURE.as_uri())
        yield PlaywrightPerceptionAdapter(page)
        browser.close()


@pytest.fixture(scope="module")
def observation(adapter: PerceptionAdapter) -> Observation:
    return adapter.observe()


class TestIndexing:
    def test_correct_element_count_and_document_order(
        self, observation: Observation
    ) -> None:
        # Hidden button is filtered out; interactive elements in DOM order.
        assert [e.tag for e in observation.elements] == [
            "input",   # 0 username
            "input",   # 1 password
            "button",  # 2 Log In
            "a",       # 3 Register
            "a",       # 4 More Info (duplicate label)
            "a",       # 5 More Info (duplicate label)
            "select",  # 6 Account
            "input",   # 7 agree checkbox
            "input",   # 8 account_type radio
        ]
        assert [e.index for e in observation.elements] == list(range(9))

    def test_roles_and_accessible_names(self, observation: Observation) -> None:
        by_id = {e.index: e for e in observation.elements}
        assert by_id[0].role == "textbox" and by_id[0].name == "Username"
        assert by_id[1].role == "textbox" and by_id[1].name == "Password"
        assert by_id[2].role == "button" and by_id[2].name == "Log In"
        assert by_id[3].role == "link" and by_id[3].name == "Register"
        assert by_id[6].role == "combobox" and by_id[6].name == "Account"
        assert by_id[7].role == "checkbox" and by_id[7].name == "I agree"
        assert by_id[8].role == "radio" and by_id[8].name == ""

    def test_page_identity_and_url(self, observation: Observation) -> None:
        assert observation.page_identity == "Accounts Overview"
        assert observation.title == "Parabank Fixture — Accounts"
        assert observation.url.startswith("file://")


class TestCandidateLocators:
    def test_priority_order_and_full_candidate_set(
        self, observation: Observation
    ) -> None:
        username = observation.element_by_index(0)
        assert [(c.strategy, c.value) for c in username.locators] == [
            (LocatorStrategy.ROLE_NAME, 'role=textbox name="Username"'),
            (LocatorStrategy.ID_ATTRIBUTE, "#username"),
            (LocatorStrategy.CSS_STRUCTURAL, "body > form > input:nth-of-type(1)"),
        ]

    def test_button_gets_text_content_candidate(
        self, observation: Observation
    ) -> None:
        login = observation.element_by_index(2)
        strategies = [c.strategy for c in login.locators]
        assert strategies == [
            LocatorStrategy.ROLE_NAME,
            LocatorStrategy.ID_ATTRIBUTE,
            LocatorStrategy.TEXT_CONTENT,
            LocatorStrategy.CSS_STRUCTURAL,
        ]
        text_candidate = login.locators[2]
        assert text_candidate.value == "Log In"

    def test_duplicate_labels_disambiguated(self, observation: Observation) -> None:
        """Two identical 'More Info' links: role_name and text_content
        cannot uniquely resolve, so both candidates are dropped and each
        element keeps only its unique css_structural locator."""
        more_info_1, more_info_2 = (
            observation.element_by_index(4),
            observation.element_by_index(5),
        )
        for element in (more_info_1, more_info_2):
            assert [c.strategy for c in element.locators] == [
                LocatorStrategy.CSS_STRUCTURAL
            ]
        # The css candidates are distinct and unique per element.
        assert more_info_1.locators[0].value == "body > a:nth-of-type(2)"
        assert more_info_2.locators[0].value == "body > a:nth-of-type(3)"

    def test_name_attribute_fallback_when_no_id(self, observation: Observation) -> None:
        radio = observation.element_by_index(8)
        # No role_name (empty accessible name), no id: falls to name attr.
        assert [(c.strategy, c.value) for c in radio.locators] == [
            (LocatorStrategy.ID_ATTRIBUTE, 'input[name="account_type"]'),
            (LocatorStrategy.CSS_STRUCTURAL, "body > input"),
        ]


class TestResolution:
    def test_resolve_id_attribute(self, adapter: PerceptionAdapter) -> None:
        from bankops.artifact.models import LocatorCandidate

        locator = adapter.resolve_locator(
            LocatorCandidate(strategy=LocatorStrategy.ID_ATTRIBUTE, value="#username")
        )
        assert locator.count() == 1
        assert locator.get_attribute("id") == "username"

    def test_resolve_role_name(self, adapter: PerceptionAdapter) -> None:
        from bankops.artifact.models import LocatorCandidate

        locator = adapter.resolve_locator(
            LocatorCandidate(
                strategy=LocatorStrategy.ROLE_NAME, value='role=button name="Log In"'
            )
        )
        assert locator.count() == 1
        assert locator.inner_text() == "Log In"

    def test_resolve_css_structural(self, adapter: PerceptionAdapter) -> None:
        from bankops.artifact.models import LocatorCandidate

        locator = adapter.resolve_locator(
            LocatorCandidate(
                strategy=LocatorStrategy.CSS_STRUCTURAL, value="body > a:nth-of-type(2)"
            )
        )
        assert locator.count() == 1
        assert locator.get_attribute("href") == "/details/1"

    def test_resolve_malformed_role_name_rejected(self, adapter: PerceptionAdapter) -> None:
        from bankops.artifact.models import LocatorCandidate

        with pytest.raises(ValueError, match="malformed role_name"):
            adapter.resolve_locator(
                LocatorCandidate(
                    strategy=LocatorStrategy.ROLE_NAME, value="button Log In"
                )
            )


class TestScreenshot:
    def test_capture_screenshot_writes_png(
        self, adapter: PerceptionAdapter, tmp_path: Path
    ) -> None:
        path = adapter.capture_screenshot(tmp_path / "evidence" / "shot.png")
        assert path.exists()
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


class TestRender:
    def test_render_is_compact_indexed_list(self, observation: Observation) -> None:
        rendered = observation.render()
        assert '[0] textbox "Username"' in rendered
        assert '[2] button "Log In"' in rendered
        assert "[6] combobox \"Account\"" in rendered
        assert "<html" not in rendered  # never raw markup
        assert observation.observation_hash


class TestLegacyTableLayouts:
    """ParaBank-style legacy pages: the field label is plain text in a
    preceding table cell, with no <label> element at all (§11a's "table
    layouts"). Without this fallback every ParaBank form field is
    nameless, degrading LLM guidance, artifact input names, and
    redaction's descriptive-name assumption (§8c)."""

    def test_table_cell_text_becomes_accessible_name(self, adapter) -> None:
        # Reuse the module fixture's page/browser — Playwright forbids a
        # second sync_playwright() instance in the same thread. The
        # module-scoped `observation` fixture is already cached by earlier
        # tests, so temporarily swapping the page content is safe; we
        # restore the fixture page afterwards regardless.
        page = adapter.page
        page.set_content(
            "<html><body><h2>Transfer Funds</h2>"
            "<form><p><b>Username</b></p>"
            "<div class='login'><input name='username'></div>"
            "<p><b>Amount:</b> $<input id='amount'></p>"
            "<div>From account #<select id='from'><option>12345</option></select></div>"
            "<table><tr><td>To account</td><td><select id='to'></select></td></tr>"
            "</table></form></body></html>"
        )
        try:
            observation = adapter.observe()
            names = {(e.role, e.name) for e in observation.elements}
            # Real ParaBank patterns, verified by live DOM probes:
            assert ("textbox", "Username") in names      # <p><b> above a login div
            assert ("textbox", "Amount:") in names      # <b> sibling, "$" text skipped
            assert ("combobox", "From account #") in names  # bare text node label
            assert ("combobox", "To account") in names  # table-cell label
            # Selects must never name themselves from their option text:
            assert all("12345" not in n for _, n in names)
            # Combobox options are exposed in the observation — the only
            # channel through which the agent can see what a dropdown
            # offers (e.g. the second account to transfer to):
            from_select = next(
                e for e in observation.elements if e.name == "From account #"
            )
            assert from_select.options == ["12345"]
            assert "(options: 12345)" in observation.render()
        finally:
            page.goto(FIXTURE.as_uri())
