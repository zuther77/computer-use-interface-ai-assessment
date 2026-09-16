"""Risk-tag / confirm=True gate (Phase 3; DECISIONS.md §8b).

Per-artifact risk tags are set manually at authoring time; an irreversible
artifact flags its point-of-no-return step, and replay will not execute
that step without an explicit ``confirm=True`` from the caller. Risk lives
in what a capability accomplishes, not in the mechanics of a click.
"""

from __future__ import annotations

from bankops.artifact.models import RiskLevel


class ConfirmationRequired(Exception):
    """Executing the point-of-no-return step of an irreversible artifact
    requires an explicit confirm=True from the caller (DECISIONS.md §8b)."""


def check_confirmation(
    risk_level: RiskLevel,
    point_of_no_return_step: int | None,
    step_index: int,
    *,
    confirm: bool = False,
) -> None:
    """Raise ConfirmationRequired if ``step_index`` is the point of no return
    of a state_changing_irreversible artifact and confirm was not given."""
    if (
        risk_level is RiskLevel.STATE_CHANGING_IRREVERSIBLE
        and step_index == point_of_no_return_step
        and not confirm
    ):
        raise ConfirmationRequired(
            f"step {step_index} is the point of no return for an irreversible "
            f"capability ({risk_level.value}); re-invoke with confirm=True to "
            f"execute it"
        )
