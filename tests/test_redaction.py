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


class TestReasoningRedaction:
    def test_masks_value_echoed_from_sensitive_element(self) -> None:
        from bankops.safety.redaction import redact_reasoning

        out = redact_reasoning(
            "typing the password hunter2 into the Password field",
            "type_text",
            {"index": 5, "text": "hunter2"},
            element_name="Password",
        )
        assert "hunter2" not in out
        assert "***MASKED***" in out

    def test_masks_sensitive_named_param_value(self) -> None:
        from bankops.safety.redaction import redact_reasoning

        out = redact_reasoning(
            "storing password=hunter2 for later", "remember", {"password": "hunter2"}
        )
        assert "hunter2" not in out

    def test_masks_remember_value_under_sensitive_key(self) -> None:
        from bankops.safety.redaction import redact_reasoning

        out = redact_reasoning(
            "I remember the pin 9911", "remember", {"key": "pin", "value": "9911"}
        )
        assert "9911" not in out

    def test_non_sensitive_reasoning_untouched(self) -> None:
        from bankops.safety.redaction import redact_reasoning

        text = "clicking Transfer because the form is complete"
        out = redact_reasoning(text, "click", {"index": 4}, element_name="Transfer")
        assert out == text


class TestMaskKnownValues:
    def test_masks_echoes_in_text_dict_and_scalars(self) -> None:
        from bankops.safety.redaction import mask_known_values

        out = mask_known_values(
            {"summary": "used password s3cret", "items": ["s3cret", "safe"], "n": 12345},
            ["s3cret", "12345"],
        )
        assert out["summary"] == "used password ***MASKED***"
        assert out["items"][0] == "***MASKED***"
        assert out["items"][1] == "safe"
        assert out["n"] == "***MASKED***"

    def test_leaves_unrelated_values_untouched(self) -> None:
        from bankops.safety.redaction import mask_known_values

        text = "transferred 25.00 from account 12345"
        out = mask_known_values(text, ["s3cret"])
        assert out == text
