"""Replay result types (Phase 6; DECISIONS.md §7b, §7c).

A discriminated union of typed Pydantic result models — SuccessResult,
BusinessOutcomeResult, FailureResult — each carrying only the fields
relevant to that outcome (a single flat result with a generic ``details``
dict was ruled out in §7c for the same untyped-ambiguity reasons as §4d).

The rollup implements §7b: the first non-success classification
short-circuits immediately.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from bankops.artifact.models import OutcomeClass


class SuccessResult(BaseModel):
    status: Literal["success"] = "success"
    artifact_name: str
    steps_executed: int
    outputs: dict[str, str] = Field(default_factory=dict)


class BusinessOutcomeResult(BaseModel):
    """A step matched a recorded known alternate-outcome signature
    (DECISIONS.md §7a) — a real business answer, not a technical failure."""

    status: Literal["business_outcome"] = "business_outcome"
    artifact_name: str
    steps_executed: int
    step_index: int
    classification: OutcomeClass = OutcomeClass.BUSINESS_OUTCOME
    description: str = ""
    matched_text: str = ""
    outputs: dict[str, str] = Field(default_factory=dict)


class FailureResult(BaseModel):
    status: Literal["failure"] = "failure"
    artifact_name: str
    steps_executed: int
    reason: str
    failed_step: int | None = None
    expected: str | None = None
    observed: str | None = None


ReplayResult = Annotated[
    Union[SuccessResult, BusinessOutcomeResult, FailureResult],
    Field(discriminator="status"),
]


def rollup(results: list) -> SuccessResult | BusinessOutcomeResult | FailureResult:
    """§7b run-level rollup: the first non-success result short-circuits;
    only all-success runs merge into a single SuccessResult."""
    if not results:
        raise ValueError("cannot roll up an empty result list")
    outputs: dict[str, str] = {}
    steps_executed = 0
    for result in results:
        if not isinstance(result, SuccessResult):
            return result
        outputs.update(result.outputs)
        steps_executed += result.steps_executed
    return SuccessResult(
        artifact_name=results[0].artifact_name,
        steps_executed=steps_executed,
        outputs=outputs,
    )
