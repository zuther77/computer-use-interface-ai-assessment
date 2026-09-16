"""Redaction tests (Phase 3; DECISIONS.md §8c).

Sensitive-named fields get masked, non-sensitive ones don't.
"""

from __future__ import annotations

from bankops.safety.redaction import MASK, is_sensitive_name, mask, redact_params


class TestSensitiveNameDetection:
    def test_sensitive_names_detected(self) -> None:
        for name in (
            "password",
            "Password",
            "new_password",
            "pin",
            "card_pin",
            "ssn",
            "account_number",
            "from_account",
            "to_account",
            "credit_card",
            "cvv",
            "api_key",
            "social_security_number",
        ):
            assert is_sensitive_name(name), name

    def test_non_sensitive_names_not_detected(self) -> None:
        for name in (
            "username",
            "amount",
            "shipping",
            "mode",
            "first_name",
            "memo",
        ):
            assert not is_sensitive_name(name), name

    def test_empty_name_is_not_sensitive(self) -> None:
        assert not is_sensitive_name("")


class TestMasking:
    def test_mask_returns_constant(self) -> None:
        assert mask("hunter2") == MASK
        assert mask("") == MASK
        assert mask(12345) == MASK

    def test_redact_params_masks_only_sensitive_keys(self) -> None:
        params = {
            "username": "john",
            "password": "hunter2",
            "account_number": "10001",
            "amount": "150.00",
        }
        redacted = redact_params(params)
        assert redacted["password"] == MASK
        assert redacted["account_number"] == MASK
        assert redacted["username"] == "john"
        assert redacted["amount"] == "150.00"

    def test_redact_params_does_not_mutate_input(self) -> None:
        params = {"password": "hunter2"}
        redact_params(params)
        assert params["password"] == "hunter2"
