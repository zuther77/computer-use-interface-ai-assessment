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
    // A select's own text is its options and a textarea's is its value —
    // neither is a field label; skip both (they fall through to the
    // legacy-label fallback below instead of producing option-soup names).
    if (el.tagName !== 'SELECT' && el.tagName !== 'TEXTAREA') {
      const text = norm(el.textContent);
      if (text) return text;
    }
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
    // Legacy-layout label fallback (§11a "table layouts", generalized
    // after probing live ParaBank): fields are labelled by plain text
    // before them — a label-like *first* previous element sibling
    // (<b>Amount:</b> $<input>, <tr><td>Amount</td><td><input>, or the
    // <p> above a login div), or a bare lettered text node in the same
    // container (<div>From account #<select>). Deliberately conservative:
    // candidate labels must not contain a form control (a wrapping
    // <label> around another input is that input's label, not ours),
    // only the immediately preceding contiguous text is considered (no
    // chain-walking past elements into unrelated text), and the text
    // must contain letters (skips "$" prefixes and whitespace).
    const LABELLIKE = ['P','B','STRONG','LABEL','TH','TD','DT','LEGEND','SPAN','EM','I','SMALL'];
    const isLabelFor = (cand) => (
      LABELLIKE.includes(cand.tagName) &&
      !cand.querySelector('input, select, textarea, button') &&
      cand.textContent.length < 80
    );
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) {
      let node = el;
      for (let i = 0; i < 4 && node && node.parentElement; i++) {
        let sib = node.previousSibling;
        while (sib && sib.nodeType === 3) {
          const t = norm(sib.textContent);
          if (t) {
            if (/[a-zA-Z]/.test(t) && t.length < 80) return t;
            break; // "$" or similar decoration — stop, try the element path
          }
          sib = sib.previousSibling;
        }
        const prev = node.previousElementSibling;
        if (prev && isLabelFor(prev)) return norm(prev.textContent);
        node = node.parentElement;
      }
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
  // Identity (§4e): prefer the page's own content heading — ParaBank marks
  // it h1.title ("Loan Request Processed", "Account Opened"); the first
  // h1-h3 in DOM order is otherwise the shared sidebar "Account Services",
  // which distinguishes nothing.
  const titleHeading = document.querySelector('h1.title');
  const heading = titleHeading || document.querySelector('h1, h2, h3');
  // Visible outcome/status text a human operator would read (denial
  // banners, confirmations) — non-interactive, short, deduped; elements
  // containing links/controls are skipped because those are already
  // indexed elements.
  const messages = [];
  const errorMessages = [];
  for (const m of document.querySelectorAll(
    'p, .error, .warning, .notice, [class*="message"], [class*="status"]'
  )) {
    if (messages.length >= 6) break;
    if (!m.getClientRects().length) continue;
    if (m.querySelector('a, button, input, select, textarea')) continue;
    const t = norm(m.textContent);
    if (!t || t.length > 300 || messages.includes(t)) continue;
    messages.push(t);
    // Error-classed messages are tracked separately: replay treats an
    // unrecorded error message as "the step did not land in the recorded
    // state" (e.g. ParaBank's 'Invalid date format' validation spans all
    // carry class="error").
    if (/(?:^|\s)error(?:\s|$)/i.test(m.className || '')) {
      errorMessages.push(t);
    }
  }
  return {
    title: document.title,
    identity: (heading ? norm(heading.textContent) : '') || document.title,
    messages: messages,
    errorMessages: errorMessages,
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
        options: (tag === 'select')
          ? Array.from(el.options).slice(0, 30).map((o) => o.value) : null,
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
                # A raw '#id' is only valid CSS when the id is a valid CSS
                # identifier — ParaBank's bill-pay page deliberately gives
                # the phone input a random UUID id each render (observed
                # live: a digit-leading UUID made '#<uuid>' an INVALID
                # selector that killed the action). Ids that are not safe
                # identifiers use the always-valid attribute form.
                raw_id = el["id"]
                id_value = (
                    f"#{raw_id}"
                    if re.fullmatch(r"-?[a-zA-Z_][a-zA-Z0-9_-]*", raw_id)
                    else f'[id="{raw_id}"]'
                )
                locators.append(
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE,
                        value=id_value,
                    )
                )
            if el["nameAttr"]:
                # Emitted ALONGSIDE the id candidate, not as an elif: an id
                # can be ephemeral (the same random-UUID input above) while
                # the name attribute is stable across sessions — replay's
                # first-unique-match (§6a) falls through when the recorded
                # id candidate dies.
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
                    options=el.get("options"),
                    locators=locators,
                )
            )

        return Observation(
            url=raw["url"],
            title=raw["title"],
            page_identity=raw["identity"],
            elements=elements,
            messages=raw.get("messages", []),
            error_messages=raw.get("errorMessages", []),
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

    def navigate(self, url: str) -> int | None:
        """Navigate and report the HTTP status (None when unavailable) —
        the navigate tool turns error-page landings (≥400) into typed
        errors instead of silently operating on a 404 page."""
        response = self.page.goto(url)
        return response.status if response else None

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass  # session already gone — teardown must never mask results
