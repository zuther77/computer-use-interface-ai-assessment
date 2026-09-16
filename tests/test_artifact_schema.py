"""Artifact schema tests (Phase 1; DECISIONS.md §4a–4f, §8b).

Round-trip serialization, required-field validation, malformed input
parameter type rejection, and cross-reference integrity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


def make_artifact(**overrides) -> Artifact:
    """A minimal but complete ParaBank transfer-funds artifact."""
    base = dict(
        name="parabank_transfer_funds",
        goal="Transfer funds between two accounts",
        target_base_url="http://localhost:8080/parabank",
        steps=[
            Step(
                action=ActionType.NAVIGATE,
                navigate_url="http://localhost:8080/parabank/transfer.htm",
                checkpoint=Checkpoint(
                    url="http://localhost:8080/parabank/transfer.htm",
                    page_identity="heading 'Transfer Funds'",
                ),
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                locator_candidates=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.ROLE_NAME,
                        value='role=textbox[name="Amount"]',
                    ),
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE,
                        value="#amount",
                    ),
                ],
                input_name="amount",
                checkpoint=Checkpoint(
                    url="http://localhost:8080/parabank/transfer.htm",
                    page_identity="textbox 'Amount' has value",
                ),
            ),
            Step(
                action=ActionType.SELECT_OPTION,
                locator_candidates=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.ID_ATTRIBUTE, value="#fromAccountId"
                    )
                ],
                input_name="from_account",
                checkpoint=Checkpoint(
                    url="http://localhost:8080/parabank/transfer.htm",
                    page_identity="option selected in 'From account'",
                ),
            ),
            Step(
                action=ActionType.CLICK,
                locator_candidates=[
                    LocatorCandidate(
                        strategy=LocatorStrategy.ROLE_NAME,
                        value='role=button[name="Transfer"]',
                    ),
                    LocatorCandidate(
                        strategy=LocatorStrategy.CSS_STRUCTURAL,
                        value="input[value='Transfer']",
                    ),
                ],
                # Recorded known business outcome: insufficient funds (§7a).
                outcome_signatures=[
                    OutcomeSignature(
                        locator=LocatorCandidate(
                            strategy=LocatorStrategy.CSS_STRUCTURAL,
                            value=".error",
                        ),
                        text_pattern="insufficient funds",
                        classification=OutcomeClass.BUSINESS_OUTCOME,
                        description="Transfer amount exceeds balance",
                    )
                ],
                checkpoint=Checkpoint(
                    url="http://localhost:8080/parabank/transfer.htm",
                    page_identity="transfer complete message",
                ),
            ),
        ],
        inputs=[
            InputParam(
                name="amount", param_type=ParamType.DECIMAL, description="Amount to transfer"
            ),
            InputParam(name="from_account", param_type=ParamType.STRING),
        ],
        outputs=[],
        terminal_checkpoint=Checkpoint(
            url="http://localhost:8080/parabank/transfer.htm",
            page_identity="text 'Transfer Complete!'",
        ),
        risk_level=RiskLevel.STATE_CHANGING_REVERSIBLE,
    )
    base.update(overrides)
    return Artifact(**base)


class TestRoundTrip:
    def test_json_round_trip_is_lossless(self) -> None:
        artifact = make_artifact()
        rebuilt = Artifact.from_json(artifact.to_json())
        assert rebuilt == artifact
        assert rebuilt.to_json() == artifact.to_json()

    def test_serialization_is_plain_json_with_schema_version(self) -> None:
        import json

        data = json.loads(make_artifact().to_json())
        assert data["schema_version"] == "1.0"
        assert data["risk_level"] == "state_changing_reversible"
        assert data["steps"][1]["locator_candidates"][0]["strategy"] == "role_name"

    def test_save_and_load_file(self, tmp_path) -> None:
        artifact = make_artifact()
        path = artifact.save(tmp_path / "evidence" / "artifacts" / "transfer.json")
        assert Artifact.load(path) == artifact


class TestRequiredFieldValidation:
    def test_artifact_requires_steps(self) -> None:
        with pytest.raises(ValidationError):
            Artifact(
                name="x",
                goal="y",
                steps=[],
                terminal_checkpoint=Checkpoint(url="u", page_identity="p"),
            )

    def test_step_requires_action(self) -> None:
        with pytest.raises(ValidationError):
            Step(locator_candidates=[])

    def test_locator_candidate_requires_value(self) -> None:
        with pytest.raises(ValidationError):
            LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value="")

    def test_checkpoint_requires_page_identity(self) -> None:
        with pytest.raises(ValidationError):
            Checkpoint(url="http://x", page_identity="")

    def test_input_name_must_be_identifier(self) -> None:
        with pytest.raises(ValidationError):
            InputParam(name="Not A Valid Name")


class TestInputParamTypes:
    def test_malformed_input_param_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InputParam(name="amount", param_type="float")

    def test_malformed_input_param_type_rejected_via_json(self) -> None:
        raw = make_artifact().to_json().replace('"param_type": "decimal"', '"param_type": "money"')
        with pytest.raises(ValidationError):
            Artifact.from_json(raw)

    @pytest.mark.parametrize("pt", ["string", "integer", "decimal", "boolean"])
    def test_valid_param_types_accepted(self, pt: str) -> None:
        assert InputParam(name="x", param_type=pt).param_type == pt


class TestCrossReferenceIntegrity:
    def test_step_referencing_undeclared_input_rejected(self) -> None:
        good = make_artifact().model_dump()
        good["steps"][1]["input_name"] = "no_such_param"
        with pytest.raises(ValidationError, match="undeclared input"):
            Artifact.model_validate(good)

    def test_extract_step_requires_output(self) -> None:
        dump = make_artifact().model_dump()
        dump["steps"].append({"action": "extract", "locator_candidates": []})
        with pytest.raises(ValidationError, match="extract requires output_name"):
            Artifact.model_validate(dump)

    def test_irreversible_requires_point_of_no_return(self) -> None:
        with pytest.raises(ValidationError, match="point_of_no_return_step"):
            make_artifact(risk_level=RiskLevel.STATE_CHANGING_IRREVERSIBLE)

    def test_irreversible_point_of_no_return_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError, match="valid step index"):
            make_artifact(
                risk_level=RiskLevel.STATE_CHANGING_IRREVERSIBLE,
                point_of_no_return_step=99,
            )

    def test_reversible_must_not_set_point_of_no_return(self) -> None:
        with pytest.raises(ValidationError, match="may only be set"):
            make_artifact(point_of_no_return_step=3)

    def test_raw_value_only_when_constant(self) -> None:
        dump = make_artifact().model_dump()
        dump["steps"][1]["value"] = "100.00"  # is_constant is still False
        with pytest.raises(ValidationError, match="is_constant=True"):
            Artifact.model_validate(dump)

    def test_declared_but_unused_input_rejected(self) -> None:
        dump = make_artifact().model_dump()
        dump["inputs"].append(
            {"name": "unused_param", "param_type": "string", "required": True}
        )
        with pytest.raises(ValidationError, match="never used"):
            Artifact.model_validate(dump)
