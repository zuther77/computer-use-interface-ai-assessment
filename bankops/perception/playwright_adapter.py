"""Playwright-backed PerceptionAdapter (Phase 2; DECISIONS.md §2b, §2c, §3a).

Walks the page's interactive elements (a practical approximation of the
accessibility snapshot: role + accessible-name per element), emits an
indexed list with ordered candidate locators, and captures screenshots as
a separate evidence call.

Locator strategies (§4b), in priority order:
  1. role_name       — ``role=button name="Log In"`` (get_by_role, exact)
  2. id_attribute    — ``#id``, or ``tag[name="..."]`` when only a name
                       attribute exists (attribute-based fallback)
  3. text_content    — ``the element's own text`` (get_by_text, exact)
  4. css_structural  — nth-of-type path from <body> (always unique)

Candidates that cannot uniquely resolve (e.g. duplicate labels) are
dropped at observation time, so disambiguation falls through to the
unique fallbacks — replay's first-unique-match rule (§6a) then never sees
an ambiguous candidate from this adapter.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page

from bankops.artifact.models import LocatorCandidate, LocatorStrategy
from bankops.perception.base import Observation, ObservedElement, PerceptionAdapter

# One page.evaluate round trip: collect every interactive element in
# document order with its role, accessible name, and structural info.
_OBSERVE_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const roleFor = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      return 'textbox';
    }
    return '';
  };
  const nameFor = (el) => {
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return norm(ariaLabel);
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((e) => norm(e.textContent))
        .filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label) return norm(label.textContent);
    }
    const wrapLabel = el.closest('label');
    if (wrapLabel) return norm(wrapLabel.textContent);
    const text = norm(el.textContent);
    if (text) return text;
    const title = el.getAttribute('title');
    if (title) return norm(title);
    if (el.tagName === 'INPUT') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(type)) {
        if (el.value) return norm(el.value);
        return type;
      }
      const placeholder = el.getAttribute('placeholder');
      if (placeholder) return norm(placeholder);
    }
    return '';
  };
  const cssPath = (el) => {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName.toLowerCase() !== 'html') {
      const tag = cur.tagName.toLowerCase();
      const parent = cur.parentNode;
      if (!parent || !parent.children) break;
      const sameTag = Array.from(parent.children)
        .filter((c) => c.tagName === cur.tagName);
      const idx = sameTag.indexOf(cur) + 1;
      parts.unshift(sameTag.length > 1 ? tag + ':nth-of-type(' + idx + ')' : tag);
      cur = parent;
    }
    return parts.join(' > ');
  };
  const all = Array.from(document.querySelectorAll(
    'a[href], button, input, select, textarea, [role]'
  ));
  const visible = all.filter((el) => el.getClientRects().length > 0);
  const heading = document.querySelector('h1, h2, h3');
  return {
    title: document.title,
    identity: (heading ? norm(heading.textContent) : '') || document.title,
    url: location.href,
    elements: visible.map((el, i) => {
      const tag = el.tagName.toLowerCase();
      const nameAttr = el.getAttribute('name');
      return {
        index: i,
        tag: tag,
        role: roleFor(el),
        name: nameFor(el),
        value: (el.value !== undefined && el.value !== null && el.value !== '')
          ? String(el.value) : null,
        id: el.id || null,
        nameAttr: nameAttr,
        text: norm(el.textContent).slice(0, 120) || null,
        css: cssPath(el),
      };
    }),
  };
}
"""

_ROLE_NAME_PATTERN = re.compile(r'^role=(\S+) name="(.*)"$', re.DOTALL)


class PlaywrightPerceptionAdapter(PerceptionAdapter):
    """Perception over a live Playwright page (DECISIONS.md §2c)."""

    def __init__(self, page: Page):
        self.page = page

    # -- Observation -------------------------------------------------------

    def observe(self) -> Observation:
        raw = self.page.evaluate(_OBSERVE_JS)
        raw_elements = raw["elements"]

        # Duplicate detection: a (role, name) or text value that occurs more
        # than once cannot be resolved uniquely, so the corresponding
        # candidate is dropped — disambiguation falls to the unique
        # fallbacks (css_structural is unique by construction).
        role_name_counts: dict[tuple[str, str], int] = {}
        text_counts: dict[str, int] = {}
        for el in raw_elements:
            if el["name"]:
                key = (el["role"], el["name"])
                role_name_counts[key] = role_name_counts.get(key, 0) + 1
            if el["text"]:
                text_counts[el["text"]] = text_counts.get(el["text"], 0) + 1

        elements: list[ObservedElement] = []
        for el in raw_elements:
            locators: list[LocatorCandidate] = []

            if el["name"] and role_name_counts[(el["role"], el["name"])] == 1:
                locators.append(
                    LocatorCandidate(
                        strategy=LocatorStrategy.ROLE_NAME,
                        value=f'role={el["role"]} name="{el["name"]}"',
                    )
                )
            if el["id"]:
                locators.append(
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE,
                        value=f'#{el["id"]}',
                    )
                )
            elif el["nameAttr"]:
                locators.append(
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE,
                        value=f'{el["tag"]}[name="{el["nameAttr"]}"]',
                    )
                )
            if el["text"] and text_counts[el["text"]] == 1:
                locators.append(
                    LocatorCandidate(
                        strategy=LocatorStrategy.TEXT_CONTENT,
                        value=el["text"],
                    )
                )
            locators.append(
                LocatorCandidate(
                    strategy=LocatorStrategy.CSS_STRUCTURAL,
                    value=el["css"],
                )
            )

            elements.append(
                ObservedElement(
                    index=el["index"],
                    tag=el["tag"],
                    role=el["role"],
                    name=el["name"],
                    value=el["value"],
                    locators=locators,
                )
            )

        return Observation(
            url=raw["url"],
            title=raw["title"],
            page_identity=raw["identity"],
            elements=elements,
        )

    # -- Evidence (§2b: separate, never a decision input) ------------------

    def capture_screenshot(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path))
        return path

    # -- Resolution (§6a: uniqueness is checked by the caller) -------------

    def resolve_locator(self, candidate: LocatorCandidate) -> Locator:
        value = candidate.value
        if candidate.strategy is LocatorStrategy.ROLE_NAME:
            match = _ROLE_NAME_PATTERN.match(value)
            if not match:
                raise ValueError(f"malformed role_name locator: {value!r}")
            role, name = match.group(1), match.group(2)
            return self.page.get_by_role(role, name=name, exact=True)
        if candidate.strategy is LocatorStrategy.ID_ATTRIBUTE:
            return self.page.locator(value)
        if candidate.strategy is LocatorStrategy.TEXT_CONTENT:
            return self.page.get_by_text(value, exact=True)
        if candidate.strategy is LocatorStrategy.CSS_STRUCTURAL:
            return self.page.locator(value)
        raise ValueError(f"unsupported locator strategy: {candidate.strategy}")

    def current_url(self) -> str:
        return self.page.url

