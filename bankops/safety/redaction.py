"""Field-level redaction helpers (Phase 3; DECISIONS.md §8c).

Parameter/output fields whose declared name matches a sensitive pattern
are masked before persistence — even though the live value is still used
in-session to perform the action. Credentials are read from
environment/config at session start and never written anywhere at all.

Matching is token-based (name split on underscores/hyphens/spaces, token
compared against the sensitive set) rather than raw substring, so a field
like ``shipping`` is not falsely masked by the ``pin`` pattern. Known
limitation, stated honestly (§8c): pattern-based masking depends on
descriptive field naming and could miss a sensitive field with a
non-obvious name.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***MASKED***"

SENSITIVE_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "pin",
        "ssn",
        "social_security",
        "social_security_number",
        "tax_id",
        "account",
        "account_number",
        "card",
        "card_number",
        "credit_card",
        "cvv",
        "cvc",
        "token",
        "api_key",
        "access_key",
        "credentials",
    }
)

_TOKEN_SPLIT = re.compile(r"[_\s\-]+")


def is_sensitive_name(name: str) -> bool:
    """True if the field's declared name matches a sensitive pattern."""
    normalized = (name or "").strip().lower()
    if not normalized:
        return False
    if normalized in SENSITIVE_TOKENS:
        return True
    tokens = {t for t in _TOKEN_SPLIT.split(normalized) if t}
    return bool(tokens & SENSITIVE_TOKENS)


def mask(value: Any) -> str:
    """Replace a sensitive value with a fixed mask before persistence."""
    return MASK


def redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with sensitive-named values masked."""
    return {
        key: mask(value) if is_sensitive_name(key) else value
        for key, value in params.items()
    }


def redact_action(
    action: str,
    params: dict[str, Any],
    element_name: str = "",
    data: Any = None,
) -> tuple[dict[str, Any], Any]:
    """Redact a typed action's parameters and result data for persistence
    (§8c). Three name-based rules, in order:

    1. by declared parameter name (``password=…`` masked directly),
    2. by the target element's accessible name — typing into a field
       labelled "Password" masks the ``text``/``option``/``value`` params
       and the returned ``data``, even though the live value was used
       in-session to perform the action,
    3. for ``remember`` stores, the *key* names the fact: remembering
       'password' masks the stored value.
    """
    redacted = {
        key: mask(value) if is_sensitive_name(key) else value
        for key, value in params.items()
    }
    if element_name and is_sensitive_name(element_name):
        for key in ("text", "option", "value"):
            if key in redacted:
                redacted[key] = mask(redacted[key])
        if data is not None:
            data = mask(data)
    if action == "remember" and "key" in redacted:
        if is_sensitive_name(str(redacted.get("key", ""))):
            if "value" in redacted:
                redacted["value"] = mask(redacted["value"])
            if data is not None:
                data = mask(data)
    return redacted, data


def redact_nested(obj: Any) -> Any:
    """Recursively mask sensitive-named keys in nested dicts/lists —
    used for evidence summaries that embed working memory, etc."""
    if isinstance(obj, dict):
        return {
            key: mask(value) if is_sensitive_name(key) else redact_nested(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_nested(item) for item in obj]
    return obj
