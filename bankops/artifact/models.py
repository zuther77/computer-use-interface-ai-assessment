"""Pydantic models for the canonical artifact schema.

Phase 1 (DECISIONS.md §4a–4f), with the outcome-signature extension built
out in Phase 7 (§7a) and the risk-tag fields from §8b.

The artifact is the system's central contract: discovery produces one,
replay executes one, and every safety/escalation/evidence mechanism reads
from it. Serialization is plain JSON with a ``schema_version`` field (§4f);
a database was deliberately ruled out at this scope.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LocatorStrategy(str, Enum):
    """Locator strategy tags for candidate locators (DECISIONS.md §4b, §11a).

    Tried in priority order at replay time until exactly one visible/enabled
    element resolves. The set is deliberately closed for the web adapter;
    a future surface adapter (§11a) would extend it with tags such as
    ``table_cell_position``, ``frame_scoped_xpath`` or ``automation_id``.
    """

    ROLE_NAME = "role_name"
    ID_ATTRIBUTE = "id_attribute"
    TEXT_CONTENT = "text_content"
    CSS_STRUCTURAL = "css_structural"


class ActionType(str, Enum):
    """Recorded step action types (DECISIONS.md §3b).

    Only the page-interacting tools become artifact steps; ``remember``,
    ``finish`` and ``report_stuck`` are loop-control signals, not steps.
    """

    CLICK = "click"
    TYPE_TEXT = "type_text"
    SELECT_OPTION = "select_option"
    NAVIGATE = "navigate"
    EXTRACT = "extract"


class ParamType(str, Enum):
    """Allowed input/output field types (DECISIONS.md §4c, §4d).

    Malformed types are rejected at schema level — replay validates
    caller-supplied params against these before touching the browser (§6c).
    """

    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"


class OutcomeClass(str, Enum):
    """Classification tags for recorded alternate-outcome signatures
    (DECISIONS.md §7a). Captured deliberately at recording time — never
    inferred generically at replay time."""

    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class RiskLevel(str, Enum):
    """Per-artifact risk tag, set manually at authoring time
    (DECISIONS.md §8b). Risk lives in what a capability accomplishes,
    not in the mechanics of a click."""

    READ_ONLY = "read_only"
    STATE_CHANGING_REVERSIBLE = "state_changing_reversible"
    STATE_CHANGING_IRREVERSIBLE = "state_changing_irreversible"


class LocatorCandidate(BaseModel):
    """A single candidate locator: a strategy tag plus the locator value
    encoded in that strategy's canonical form (DECISIONS.md §4b)."""

    strategy: LocatorStrategy
    value: str = Field(min_length=1)


class Checkpoint(BaseModel):
    """A page-state signature (DECISIONS.md §4e): post-step URL plus a
    stable page-identity signal (e.g. the key element/heading that proves
    the page actually progressed). Captured passively during observation —
    implicit success ("all steps ran") is exactly the "assumed the click
    worked" mistake this exists to prevent.

    ``allowed_error_messages`` records any error-classed page messages the
    success path itself showed (normally empty): at replay time, an
    error-classed message that the recording did not have means the step
    did NOT land in the recorded state — observed live when a swapped
    date-range replay showed ParaBank's 'Invalid date format' error while
    every URL/heading checkpoint still matched.
    """

    url: str
    page_identity: str = Field(min_length=1)
    allowed_error_messages: list[str] = Field(default_factory=list)
    required_locator: LocatorCandidate | None = Field(
        default=None,
        description=(
            "Deliberate, goal-tied content check (§4e's 'one deliberate "
            "terminal checkpoint tied to the goal'): an element that MUST "
            "be present for the page state to count as the recorded one — "
            "e.g. a results-table link after a search step. Manually "
            "authored at artifact-authoring time (same deliberate-capture "
            "philosophy as §7a signatures and §8b risk tags), never "
            "auto-inferred: guessing 'what the goal implies' from page "
            "content would be the fuzzy detection §7a rejects. Closes the "
            "gap observed live: a swapped-dates replay changed no URL/"
            "heading/error signal, silently returning a wrong result set."
        ),
    )


class OutcomeSignature(BaseModel):
    """A recorded known alternate outcome for a step (DECISIONS.md §7a):
    a locator + expected text pattern + classification tag, merged into the
    artifact after a deliberate non-happy-path capture run."""

    locator: LocatorCandidate
    text_pattern: str = Field(min_length=1)
    classification: OutcomeClass
    description: str = ""


class Step(BaseModel):
    """One recorded step in the flat, linear ordered step list
    (DECISIONS.md §4a). Order is list order; branches are replay-time
    checks (outcome signatures), never artifact structure.

    Inputs/outputs are referenced *by name* into the artifact's declared
    ``inputs`` / ``outputs`` — the typed contract callers program against.
    A ``type_text`` / ``select_option`` value that should NOT become a
    parameter stays as a constant via ``is_constant`` + ``value`` (§4c).
    """

    action: ActionType
    locator_candidates: list[LocatorCandidate] = Field(
        default_factory=list,
        description="Ordered candidate locators, tried in priority order "
        "at replay time until exactly one visible/enabled element resolves.",
    )
    input_name: str | None = Field(
        default=None,
        description="Name of the InputParam supplying this step's value.",
    )
    value: str | None = Field(
        default=None,
        description="Raw recorded value. Kept only when is_constant=True.",
    )
    is_constant: bool = Field(
        default=False,
        description="Per-step override (§4c): keep the recorded value as a "
        "constant instead of promoting it to a named InputParam.",
    )
    output_name: str | None = Field(
        default=None,
        description="Name of the OutputField this extract step produces.",
    )
    navigate_url: str | None = Field(
        default=None,
        description="Target URL for navigate steps.",
    )
    description: str = ""
    checkpoint: Checkpoint | None = Field(
        default=None,
        description="Automatic per-step signature (§4e): post-step URL + "
        "page-identity signal, captured passively during observation.",
    )
    outcome_signatures: list[OutcomeSignature] = Field(
        default_factory=list,
        description="Known alternate-outcome signatures (§7a), checked by "
        "replay when the primary checkpoint doesn't match.",
    )


class InputParam(BaseModel):
    """A declared, caller-supplied input parameter (DECISIONS.md §4c).
    The name is derived from the target element's accessible label, so
    sensitive-pattern redaction (§8c) can rely on descriptive naming."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    param_type: ParamType = ParamType.STRING
    required: bool = True
    default: str | None = None
    description: str = ""


class OutputField(BaseModel):
    """A declared, typed output field (DECISIONS.md §4d) — produced by a
    deliberate ``extract`` step; ``remember`` values never surface here."""

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    output_type: ParamType = ParamType.STRING
    description: str = ""


class Artifact(BaseModel):
    """The canonical, replayable capability record (DECISIONS.md §4a, §4f).

    Serialized to plain JSON with a ``schema_version`` field. An artifact
    only ever comes from a ``finish``-terminated discovery run (§5c) —
    never from a partial/stuck run.
    """

    model_config = ConfigDict(validate_assignment=True)

    schema_version: str = "1.0"
    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    target_base_url: str = ""
    steps: list[Step] = Field(min_length=1)
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    terminal_checkpoint: Checkpoint
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    point_of_no_return_step: int | None = Field(
        default=None,
        description="Required for state_changing_irreversible artifacts "
        "(§8b): the step replay will not execute without confirm=True.",
    )

    # -- JSON (de)serialization ------------------------------------------

    def to_json(self) -> str:
        """Canonical serialization (§4f): plain JSON, schema_version included."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Artifact":
        return cls.model_validate_json(raw)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> "Artifact":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    # -- Cross-reference integrity ----------------------------------------

    @model_validator(mode="after")
    def _validate_references(self) -> "Artifact":
        # Unique step references (inputs/outputs are addressed by name).
        input_names = [p.name for p in self.inputs]
        output_names = [o.name for o in self.outputs]
        if len(set(input_names)) != len(input_names):
            raise ValueError("duplicate input parameter names")
        if len(set(output_names)) != len(output_names):
            raise ValueError("duplicate output field names")

        input_name_set = set(input_names)
        output_name_set = set(output_names)

        for position, step in enumerate(self.steps):
            # Steps must reference declared inputs/outputs, and a step can't
            # be both a parameterized value and a burned-in constant.
            if step.is_constant and step.input_name is not None:
                raise ValueError(
                    f"step {position}: is_constant steps must not also "
                    f"reference an input_name"
                )
            if not step.is_constant and step.value is not None:
                raise ValueError(
                    f"step {position}: raw values are only kept when "
                    f"is_constant=True"
                )
            if step.input_name is not None and step.input_name not in input_name_set:
                raise ValueError(
                    f"step {position}: references undeclared input "
                    f"'{step.input_name}'"
                )
            if step.output_name is not None and step.output_name not in output_name_set:
                raise ValueError(
                    f"step {position}: references undeclared output "
                    f"'{step.output_name}'"
                )
            # Action/field coherence: every declared input/output must be
            # referenced by at least one step, and vice versa.
            if step.action is ActionType.EXTRACT and step.output_name is None:
                raise ValueError(f"step {position}: extract requires output_name")
            if (
                step.action
                in (ActionType.TYPE_TEXT, ActionType.SELECT_OPTION)
                and step.input_name is None
                and not step.is_constant
            ):
                raise ValueError(
                    f"step {position}: {step.action.value} requires an "
                    f"input_name (or is_constant=True)"
                )
            if step.action is ActionType.NAVIGATE and not step.navigate_url:
                raise ValueError(f"step {position}: navigate requires navigate_url")

        referenced_inputs = {s.input_name for s in self.steps if s.input_name}
        unreferenced_inputs = input_name_set - referenced_inputs
        if unreferenced_inputs:
            raise ValueError(
                f"declared inputs never used by any step: {sorted(unreferenced_inputs)}"
            )
        referenced_outputs = {s.output_name for s in self.steps if s.output_name}
        unreferenced_outputs = output_name_set - referenced_outputs
        if unreferenced_outputs:
            raise ValueError(
                f"declared outputs never produced by any step: "
                f"{sorted(unreferenced_outputs)}"
            )

        # Risk-tag completeness (§8b): irreversible capabilities must name
        # their point of no return; reversible/read-only ones must not.
        if self.risk_level is RiskLevel.STATE_CHANGING_IRREVERSIBLE:
            if self.point_of_no_return_step is None:
                raise ValueError(
                    "state_changing_irreversible artifacts must set "
                    "point_of_no_return_step"
                )
            if not (
                0 <= self.point_of_no_return_step < len(self.steps)
            ):
                raise ValueError(
                    f"point_of_no_return_step {self.point_of_no_return_step} "
                    f"is not a valid step index (0..{len(self.steps) - 1})"
                )
        elif self.point_of_no_return_step is not None:
            raise ValueError(
                "point_of_no_return_step may only be set on "
                "state_changing_irreversible artifacts"
            )

        return self
