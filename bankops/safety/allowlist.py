"""Two-dimensional allowlist enforcement (Phase 3; DECISIONS.md §8a).

Dimension 1 — domain/route: checked at ``navigate``.
Dimension 2 — action type/capability: checked at every tool invocation.

Loaded from /config/allowlist.yaml as a declarative, reviewable config —
arbitrary policy code was deliberately ruled out (§8a).
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field


class AllowlistViolation(Exception):
    """An action or navigation target is not permitted by the allowlist.

    Raised (never swallowed) so a disallowed action can never execute.
    """


class Allowlist(BaseModel):
    """Declarative allowlist: permitted domains, routes, and action types."""

    domains: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    # Deny-first (§8a): explicitly forbidden routes, checked before the
    # allowed set — e.g. ParaBank's admin page hosts destructive controls
    # (database Initialize/Clean/Shutdown, access-mode switching) inside
    # the otherwise-allowed /parabank/* route space. Observed live: a
    # goal-blocked discovery run navigated there and wiped the database.
    denied_routes: list[str] = Field(default_factory=list)
    action_types: dict[str, str] = Field(default_factory=dict)
    schemes: list[str] = Field(default_factory=lambda: ["http", "https"])

    @classmethod
    def load(cls, path: str | Path) -> "Allowlist":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    # -- Dimension 1: domain/route, checked at navigate --------------------

    @staticmethod
    def _domain_matches(entry: str, host_port: str, hostname: str) -> bool:
        if entry == "*":
            return True
        return entry in (host_port, hostname)

    @staticmethod
    def _route_matches(route: str, path: str) -> bool:
        if route == "*":
            return True
        if route.endswith("/"):
            return path.startswith(route)
        return fnmatch.fnmatch(path, route)

    def check_navigate(self, url: str) -> None:
        """Raise AllowlistViolation unless ``url`` is on an allowed
        domain+route with an allowed scheme."""
        parts = urlparse(url)
        if parts.scheme not in self.schemes:
            raise AllowlistViolation(
                f"navigation blocked: scheme '{parts.scheme}' not allowed "
                f"(allowed: {self.schemes})"
            )
        # Deny-first (§8a): an explicitly denied route blocks navigation
        # even when a broader allowed pattern also matches it.
        path = parts.path or "/"
        if any(self._route_matches(route, path) for route in self.denied_routes):
            raise AllowlistViolation(
                f"navigation blocked: route '{path}' is explicitly denied "
                f"(denied routes take precedence over allowed ones)"
            )
        host_port = parts.hostname or ""
        if parts.port:
            host_port = f"{parts.hostname}:{parts.port}"
        if not any(
            self._domain_matches(entry, host_port, parts.hostname or "")
            for entry in self.domains
        ):
            raise AllowlistViolation(
                f"navigation blocked: domain '{host_port or parts.scheme}' "
                f"not in allowlist (allowed: {self.domains})"
            )
        path = parts.path or "/"
        if not any(self._route_matches(route, path) for route in self.routes):
            raise AllowlistViolation(
                f"navigation blocked: route '{path}' not in allowlist "
                f"(allowed: {self.routes})"
            )

    # -- Dimension 2: action type, checked at every tool invocation ---------

    def check_action(self, action_type: str) -> None:
        permission = self.action_types.get(action_type)
        if permission != "allow":
            raise AllowlistViolation(
                f"action blocked: '{action_type}' is not an allowed action "
                f"type (allowed: {sorted(self.action_types)})"
            )
