"""Field-level redaction helpers (Phase 3; DECISIONS.md §8c).

Parameter/output fields whose declared name matches a sensitive pattern
(password, ssn, account_number, pin, ...) are masked before persistence,
even though the live value is still used in-session to perform the action.
"""
