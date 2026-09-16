"""Outcome-signature tests (Phase 7; DECISIONS.md §7a, §7b).

A step whose page shows a recorded business-outcome pattern instead of the
primary checkpoint must produce BusinessOutcomeResult — not FailureResult
— and the classification tags must route correctly: business_outcome
short-circuits, recoverable continues inline, hard_failure fails with
detail, and an unmatched alternate outcome falls back to a hard failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from bankops.artifact.models import (
    ActionType,
    Artifact,
    Checkpoint,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutcomeClass,
    OutcomeSignature,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
)
from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter
from bankops.replay.engine import ReplayEngine
from bankops.replay.results import BusinessOutcomeResult, FailureResult, SuccessResult

OUTCOME_PAGE = (Path(__file__).parent / "fixtures" / "outcome_page.html").as_uri()


def loc(strategy: LocatorStrategy, value: str) -> LocatorCandidate:
    return LocatorCandidate(strategy=strategy, value=value)


def make_signature(
    classification: OutcomeClass, pattern: str = "insufficient funds"
) -> OutcomeSignature:
    return OutcomeSignature(
        locator=loc(LocatorStrategy.CSS_STRUCTURAL, "#error"),
        text_pattern=pattern,
        classification=classification,
        description="Transfer amount exceeds account balance",
    )


def make_artifact(
    signature: OutcomeSignature | None,
    extra_extract_step: bool = False,
) -> Artifact:
    """Navigate → type amount → click Transfer. The click's primary
    checkpoint expects success, but the live page reveals an error banner
    instead — exactly the recorded-alternate-outcome scenario (§7a)."""
    steps = [
        Step(
            action=ActionType.NAVIGATE,
            navigate_url=OUTCOME_PAGE,
            checkpoint=Checkpoint(url=OUTCOME_PAGE, page_identity="Transfer Funds"),
        ),
        Step(
            action=ActionType.TYPE_TEXT,
            locator_candidates=[loc(LocatorStrategy.ID_ATTRIBUTE, "#amount")],
            input_name="amount",
            checkpoint=Checkpoint(url=OUTCOME_PAGE, page_identity="Transfer Funds"),
        ),
        Step(
            action=ActionType.CLICK,
            locator_candidates=[loc(LocatorStrategy.ID_ATTRIBUTE, "#transfer")],
            checkpoint=Checkpoint(url=OUTCOME_PAGE, page_identity="Transfer Complete!"),
            outcome_signatures=[signature] if signature else [],
        ),
    ]
    outputs = []
    if extra_extract_step:
        steps.append(
            Step(
                action=ActionType.EXTRACT,
                locator_candidates=[loc(LocatorStrategy.ID_ATTRIBUTE, "#error")],
                output_name="error_message",
                checkpoint=Checkpoint(url=OUTCOME_PAGE, page_identity="Transfer Funds"),
            )
        )
        outputs.append(
            OutputField(name="error_message", output_type=ParamType.STRING)
        )
    return Artifact(
        name="transfer_funds",
        goal="Transfer funds between accounts",
        target_base_url=OUTCOME_PAGE,
        steps=steps,
        inputs=[InputParam(name="amount", param_type=ParamType.DECIMAL)],
        outputs=outputs,
        terminal_checkpoint=Checkpoint(url=OUTCOME_PAGE, page_identity="Transfer Funds"),
        risk_level=RiskLevel.READ_ONLY,
    )


@pytest.fixture()
def engine():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        yield ReplayEngine(
            PlaywrightPerceptionAdapter(page), retry_delays=(0.0, 0.0)
        )
        browser.close()


class TestBusinessOutcome:
    def test_matched_business_outcome_signature(
        self, engine: ReplayEngine
    ) -> None:
        """The recorded insufficient-funds outcome appears instead of the
        primary checkpoint → BusinessOutcomeResult, not FailureResult."""
        artifact = make_artifact(
            make_signature(OutcomeClass.BUSINESS_OUTCOME)
        )
        result = engine.execute(artifact, {"amount": "999999.00"})
        assert isinstance(result, BusinessOutcomeResult)
        assert result.status == "business_outcome"
        assert result.step_index == 2
        assert result.classification is OutcomeClass.BUSINESS_OUTCOME
        assert "insufficient funds" in result.matched_text
        assert "balance" in result.description

    def test_business_outcome_not_failure_type(self, engine: ReplayEngine) -> None:
        result = engine.execute(
            make_artifact(make_signature(OutcomeClass.BUSINESS_OUTCOME)),
            {"amount": "1.00"},
        )
        assert not isinstance(result, FailureResult)


class TestRecoverable:
    def test_recoverable_signature_continues_inline(
        self, engine: ReplayEngine
    ) -> None:
        """§7b: a recoverable classification does not terminate the run —
        the flow continues to the next step and completes."""
        artifact = make_artifact(
            make_signature(OutcomeClass.RECOVERABLE),
            extra_extract_step=True,
        )
        result = engine.execute(artifact, {"amount": "10.00"})
        assert isinstance(result, SuccessResult)
        assert "insufficient funds" in result.outputs["error_message"]


class TestHardFailureSignature:
    def test_recorded_hard_failure_signature(
        self, engine: ReplayEngine
    ) -> None:
        artifact = make_artifact(make_signature(OutcomeClass.HARD_FAILURE))
        result = engine.execute(artifact, {"amount": "10.00"})
        assert isinstance(result, FailureResult)
        assert result.reason == "recorded_hard_failure"
        assert result.failed_step == 2
        assert "insufficient funds" in (result.observed or "")


class TestNoMatchFallsBackToHardFailure:
    def test_unmatched_signature_is_checkpoint_mismatch(
        self, engine: ReplayEngine
    ) -> None:
        """The recorded signature's text pattern doesn't match the live
        error text → the step matched nothing → hard failure (§7a)."""
        artifact = make_artifact(
            make_signature(OutcomeClass.BUSINESS_OUTCOME, pattern="account locked")
        )
        result = engine.execute(artifact, {"amount": "10.00"})
        assert isinstance(result, FailureResult)
        assert result.reason == "checkpoint_mismatch"
        assert "Transfer Complete!" in (result.expected or "")

    def test_no_signatures_at_all_hard_failure(self, engine: ReplayEngine) -> None:
        result = engine.execute(make_artifact(None), {"amount": "10.00"})
        assert isinstance(result, FailureResult)
        assert result.reason == "checkpoint_mismatch"
