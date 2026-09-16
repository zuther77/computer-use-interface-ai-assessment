"""PerceptionAdapter interface (Phase 2; DECISIONS.md §2b, §3a, §11a).

The formal seam between the rest of the system and any automation surface.
The rest of the system only ever interacts with a surface through two
shapes: "give me an indexed list of elements with candidate locators"
(:meth:`PerceptionAdapter.observe`) and "execute against a locator"
(:meth:`PerceptionAdapter.resolve_locator`), plus screenshots as a
parallel evidence call — never a decision input (§2b). A legacy-web or
desktop adapter would implement the same interface with different
locator-strategy tags; the artifact schema, replay engine, and error
taxonomy operate only on these output shapes (§11a).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from bankops.artifact.models import LocatorCandidate


class ObservedElement(BaseModel):
    """One interactive element from an observation (DECISIONS.md §3a).

    ``index`` is the position in the observation's element list — the
    cheap, stable reference the LLM points back at ("[3] textbox
    \"Username\""). ``locators`` holds the ordered candidate locators
    (§4b) tried in priority order at replay time.
    """

    index: int
    tag: str
    role: str
    name: str = ""
    value: str | None = None
    options: list[str] | None = None
    locators: list[LocatorCandidate] = Field(default_factory=list)


class Observation(BaseModel):
    """A full page observation: URL, page-identity signal (§4e), and the
    indexed interactive-element list."""

    url: str
    title: str = ""
    page_identity: str = ""
    elements: list[ObservedElement] = Field(default_factory=list)

    def render(self) -> str:
        """Compact serialized form shown to the LLM (DECISIONS.md §3a) —
        a closed, numbered set of elements, never raw HTML."""
        lines = [f"URL: {self.url}", f"Page: {self.page_identity}"]
        for element in self.elements:
            line = f"[{element.index}] {element.role}"
            if element.name:
                line += f' "{element.name}"'
            if element.value:
                line += f' (value: "{element.value}")'
            if element.options:
                line += f" (options: {', '.join(element.options)})"
            lines.append(line)
        return "\n".join(lines)

    def element_by_index(self, index: int) -> ObservedElement | None:
        return next((e for e in self.elements if e.index == index), None)

    @property
    def observation_hash(self) -> str:
        """Stable hash over the element set for no-progress detection
        (DECISIONS.md §3d)."""
        import hashlib
        import json

        payload = json.dumps(
            [
                [e.index, e.role, e.name, e.value]
                for e in self.elements
            ]
            + [self.url],
            sort_keys=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class PerceptionAdapter(ABC):
    """The automation-surface abstraction (DECISIONS.md §11a)."""

    @abstractmethod
    def observe(self) -> Observation:
        """Return the current indexed interactive-element list."""

    @abstractmethod
    def capture_screenshot(self, path: str | Path) -> Path:
        """Capture a screenshot as parallel evidence (never a decision
        input, §2b). Implemented as a separate call so callers can keep
        observation cheap and capture evidence in parallel."""

    @abstractmethod
    def resolve_locator(self, candidate: LocatorCandidate) -> Any:
        """Resolve a candidate locator (strategy tag + value, §4b) into a
        surface-specific locator handle. Uniqueness ("exactly one
        visible/enabled element") is checked by the caller (replay's
        first-unique-match rule, §6a)."""

    @abstractmethod
    def navigate(self, url: str) -> None:
        """Navigate the surface to a URL (the navigate tool's surface call;
        allowlist enforcement happens at the tool layer, §8a)."""

    def current_url(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        """Tear down the per-invocation session (DECISIONS.md §6d). Default
        no-op; concrete adapters close their page/context. Not called on
        the escalation exit path, where the session must stay alive (§9b)."""
