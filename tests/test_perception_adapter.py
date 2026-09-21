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
            # The stable name-attribute candidate is emitted alongside the
            # id (not as an elif): an id can be ephemeral — ParaBank's
            # bill-pay phone input gets a fresh random UUID id each render —
            # so replay's first-unique-match needs a durable fallback.
            (LocatorStrategy.ID_ATTRIBUTE, 'input[name="username"]'),
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
            "<html><body>"
            "<h2>Account Services</h2>"
            "<h1 class='title'>Loan Request Processed</h1>"
            "<p class='error'>We cannot grant a loan in that amount with "
            "your available funds.</p>"
            "<p>Congratulations, your account has been opened.</p>"
            "<p>Footer with a <a href='#'>link</a> is not a message.</p>"
            "<form><p><b>Username</b></p>"
            "<div class='login'><input name='username'></div>"
            "<p><b>Amount:</b> $<input id='amount'></p>"
            "<div>From account #<select id='from'><option>12345</option></select></div>"
            "<table><tr><td>To account</td><td><select id='to'></select></td></tr>"
            "</table></form></body></html>"
        )
        try:
            observation = adapter.observe()
            # Content-heading identity preferred over the sidebar heading:
            assert observation.page_identity == "Loan Request Processed"
            # Outcome text is visible to the model (the live failure: a
            # loan-denied banner the observation structurally could not
            # contain, so the agent called finish on a denial):
            assert "We cannot grant a loan" in " ".join(observation.messages)
            assert "Congratulations" in " ".join(observation.messages)
            # Error-classed messages are tracked separately and flagged:
            assert observation.error_messages == [
                "We cannot grant a loan in that amount with your "
                "available funds."
            ]
            rendered = observation.render()
            assert "Error: We cannot grant a loan" in rendered
            assert "Message: Congratulations" in rendered
            assert "not a message" not in " ".join(observation.messages)
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


    def test_dropdown_options_not_truncated_for_long_lists(self, adapter) -> None:
        """Regression (observed live): the options exposure capped at 12 —
        ParaBank lists accounts ascending by id, so the newly created
        account (the goal's target!) sorted last and was sliced off. The
        model concluded a 'contradiction in the environment' and stalled.
        The cap now comfortably covers real account lists."""

        page = adapter.page  # module fixture; restore its page after
        options = "".join(
            f"<option>{12300 + i * 111}</option>" for i in range(15)
        )
        page.set_content(
            "<html><body><h1 class='title'>Request Loan</h1>"
            f"<div>From account #<select id='from'>{options}</select></div>"
            "</body></html>"
        )
        try:
            observation = adapter.observe()
            select = next(
                e for e in observation.elements if e.name == "From account #"
            )
            assert len(select.options) == 15
            # The LAST option — a freshly created, highest-id account —
            # must be visible to the model:
            assert select.options[-1] == str(12300 + 14 * 111)
            assert str(12300 + 14 * 111) in observation.render()
        finally:
            page.goto(FIXTURE.as_uri())


    def test_random_uuid_id_and_name_fallback_candidates(self, adapter) -> None:
        """Regression (observed live): ParaBank's bill-pay page gives the
        phone input a random UUID id each render — a digit-leading UUID
        made '#<uuid>' INVALID CSS and killed the action outright. Ids
        that are not safe CSS identifiers must use the attribute form, and
        the stable name-attribute candidate must be emitted alongside the
        (ephemeral) id so replay has a durable fallback."""

        page = adapter.page  # module fixture; restore its page after
        page.set_content(
            "<html><body><h1 class='title'>Bill Payment Service</h1>"
            "<form>"
            "<div><b>Payee name</b>: <input id='payeeName'></div>"
            "<div><b>Phone #</b>: <input id='12494cd6-a38a-45f9-9179-28ef1129a9de'"
            " name='payee.phoneNumber'></div>"
            "</form></body></html>"
        )
        try:
            observation = adapter.observe()
            phone = next(
                e for e in observation.elements if "Phone" in (e.name or "")
            )
            id_candidates = [
                c.value
                for c in phone.locators
                if c.strategy.value == "id_attribute"
            ]
            # Safe ids keep the readable '#id' form...
            payee = next(
                e for e in observation.elements if e.name == "Payee name"
            )
            assert any(
                c.value == "#payeeName" for c in payee.locators
            )
            # ...digit-leading UUID ids use the always-valid attribute form,
            assert '[id="12494cd6-a38a-45f9-9179-28ef1129a9de"]' in (
                id_candidates
            )
            # and the stable name candidate is emitted alongside the
            # ephemeral id (not as an elif):
            assert 'input[name="payee.phoneNumber"]' in id_candidates
            # No candidate throws; the attribute candidates resolve
            # uniquely. The role_name candidate may legitimately resolve 0
            # here — the adapter's heuristic accessible name can differ
            # from Playwright's own a11y name for label-less inputs, and
            # first-unique-match falls through, exactly as observed live
            # on this field.
            for candidate in phone.locators:
                locator = adapter.resolve_locator(candidate)
                if candidate.strategy.value == "role_name":
                    continue
                assert locator.count() == 1
        finally:
            page.goto(FIXTURE.as_uri())


    def test_link_hrefs_collected_for_navigation_guard(self, adapter) -> None:
        """§8a link-click guard: the adapter must record each anchor's
        absolute href so the click tool can check the navigation target
        before executing (observed live: the 'Admin Page' link bypassed
        route checks entirely). Non-link elements carry no href."""

        page = adapter.page
        page.set_content(
            "<html><body><h1 class='title'>Test Page</h1>"
            "<a id='admin-link' href='/parabank/admin.htm'>Admin Page</a>"
            "<a id='overview' href='/parabank/overview.htm'>Overview</a>"
            "<input id='q'>"
            "</body></html>"
        )
        try:
            observation = adapter.observe()
            by_name = {e.name: e for e in observation.elements}
            assert by_name["Admin Page"].href.endswith("/parabank/admin.htm")
            assert by_name["Overview"].href.endswith("/parabank/overview.htm")
            text_input = next(
                e for e in observation.elements if e.tag == "input"
            )
            assert text_input.href is None
        finally:
            page.goto(FIXTURE.as_uri())
