"""Allowlist enforcement tests (Phase 3; DECISIONS.md §8a).

Permitted/denied domain and route cases, action-type permission cases,
and loading the real /config/allowlist.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bankops.safety.allowlist import Allowlist, AllowlistViolation
from bankops import settings


def make_allowlist(**overrides) -> Allowlist:
    base = dict(
        domains=["localhost:8080", "127.0.0.1:8080"],
        routes=["/parabank/", "/parabank/*"],
        action_types={
            "click": "allow",
            "type_text": "allow",
            "navigate": "allow",
            "remember": "allow",
            "finish": "allow",
            "report_stuck": "allow",
        },
    )
    base.update(overrides)
    return Allowlist(**base)


class TestNavigateAllowlist:
    def test_permitted_domain_and_route(self) -> None:
        allowlist = make_allowlist()
        allowlist.check_navigate("http://localhost:8080/parabank/index.htm")

    def test_denied_domain(self) -> None:
        allowlist = make_allowlist()
        with pytest.raises(AllowlistViolation, match="domain 'evil.com'"):
            allowlist.check_navigate("http://evil.com/parabank/index.htm")

    def test_denied_route_on_allowed_domain(self) -> None:
        allowlist = make_allowlist()
        with pytest.raises(AllowlistViolation, match="route '/admin'"):
            allowlist.check_navigate("http://localhost:8080/admin")

    def test_denied_scheme(self) -> None:
        allowlist = make_allowlist()
        with pytest.raises(AllowlistViolation, match="scheme 'file'"):
            allowlist.check_navigate("file:///etc/passwd")

    def test_wildcard_domain_and_route(self) -> None:
        allowlist = make_allowlist(
            domains=["*"], routes=["*"], schemes=["http", "https", "file"]
        )
        allowlist.check_navigate("file:///tmp/fixture.html")
        allowlist.check_navigate("https://example.org/any/route")


class TestActionTypeAllowlist:
    def test_permitted_action(self) -> None:
        make_allowlist().check_action("click")

    def test_denied_action_type(self) -> None:
        allowlist = make_allowlist()
        with pytest.raises(AllowlistViolation, match="'delete_database'"):
            allowlist.check_action("delete_database")

    def test_action_disabled_in_config(self) -> None:
        allowlist = make_allowlist(action_types={"click": "deny"})
        with pytest.raises(AllowlistViolation, match="'click'"):
            allowlist.check_action("click")


class TestConfigLoading:
    def test_real_config_file_loads_and_permits_parabank(self) -> None:
        config = Path(settings.ALLOWLIST_PATH)
        if not config.exists():
            pytest.skip(f"no allowlist config at {config}")
        allowlist = Allowlist.load(config)
        allowlist.check_navigate("http://localhost:8080/parabank/transfer.htm")
        allowlist.check_action("click")
        with pytest.raises(AllowlistViolation):
            allowlist.check_navigate("http://evil.com/")
