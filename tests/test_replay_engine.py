"""Replay engine tests (Phase 6; DECISIONS.md §6a–6d, §7b, §7c).

Replay against the local static fixture (never live ParaBank, never an
LLM): valid happy path, pre-flight param rejection, injected
missing-element failure with step/expected/observed detail, fallback
locator recovery, checkpoint mismatch, session lifecycle, and the result
union rollup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pydantic import TypeAdapter

from bankops.artifact.models import (
    ActionType,
    Artifact,
    Checkpoint,
    InputParam,
    LocatorCandidate,
    LocatorStrategy,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
)
from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter
from bankops.replay.engine import ReplayEngine, replay_artifact
from bankops.replay.results import (
    BusinessOutcomeResult,
    FailureResult,
    ReplayResult,
    SuccessResult,
    rollup,
)

PAGE_ONE = (Path(__file__).parent / "fixtures" / "tools_page1.html").as_uri()
PAGE_TWO = (Path(__file__).parent / "fixtures" / "tools_page2.html").as_uri()
IDENTITY = "Tools Page One"


def loc(strategy: LocatorStrategy, value: str) -> LocatorCandidate:
    return LocatorCandidate(strategy=strategy, value=value)


def make_artifact(**overrides) -> Artifact:
    base = dict(
        name="apply_with_amount",
        goal="Fill the form and apply",
        target_base_url=PAGE_ONE,
        steps=[
            Step(
                action=ActionType.NAVIGATE,
                navigate_url=PAGE_ONE,
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity=IDENTITY),
            ),
            Step(
                action=ActionType.TYPE_TEXT,
                # Primary candidate deliberately unresolvable: fallback
                # recovery (§4b/§6a) must reach the #amount candidate.
                locator_candidates=[
                    loc(LocatorStrategy.ROLE_NAME, 'role=textbox name="No Such Field"'),
                    loc(LocatorStrategy.ID_ATTRIBUTE, "#amount"),
                ],
                input_name="amount",
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity=IDENTITY),
            ),
            Step(
                action=ActionType.SELECT_OPTION,
                locator_candidates=[loc(LocatorStrategy.ID_ATTRIBUTE, "#mode")],
                input_name="mode",
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity=IDENTITY),
            ),
            Step(
                action=ActionType.CLICK,
                locator_candidates=[loc(LocatorStrategy.ID_ATTRIBUTE, "#apply")],
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity=IDENTITY),
            ),
            Step(
                action=ActionType.EXTRACT,
                locator_candidates=[loc(LocatorStrategy.ID_ATTRIBUTE, "#out")],
                output_name="result",
                checkpoint=Checkpoint(url=PAGE_ONE, page_identity=IDENTITY),
            ),
        ],
        inputs=[
            InputParam(name="amount", param_type=ParamType.DECIMAL),
            InputParam(name="mode", param_type=ParamType.STRING),
        ],
        outputs=[OutputField(name="result", param_type=ParamType.STRING)],
        terminal_checkpoint=Checkpoint(url=PAGE_ONE, page_identity=IDENTITY),
        risk_level=RiskLevel.READ_ONLY,
    )
    base.update(overrides)
    return Artifact(**base)


@pytest.fixture()
def engine():
    """Engine over a fresh page, fast retries, already on page two so the
    artifact's navigate step is what lands us on page one."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(PAGE_TWO)
        yield ReplayEngine(
            PlaywrightPerceptionAdapter(page), retry_delays=(0.0, 0.0, 0.0)
        )
        browser.close()


class TestHappyPath:
    def test_valid_run_produces_success_result(self, engine: ReplayEngine) -> None:
        result = engine.execute(
            make_artifact(), {"amount": "150.00", "mode": "slow"}
        )
        assert isinstance(result, SuccessResult)
        assert result.status == "success"
        assert result.artifact_name == "apply_with_amount"
        assert result.steps_executed == 5
        assert result.outputs == {"result": "applied"}
        # The actions really happened on the live page.
        assert engine.adapter.page.locator("#amount").input_value() == "150.00"
        assert engine.adapter.page.locator("#mode").input_value() == "slow"

    def test_decimal_accepts_number_types(self, engine: ReplayEngine) -> None:
        result = engine.execute(make_artifact(), {"amount": 150.5, "mode": "fast"})
        assert isinstance(result, SuccessResult)
        assert engine.adapter.page.locator("#amount").input_value() == "150.5"


class TestPreFlightValidation:
    def test_bad_input_param_rejected_before_browser_interaction(
        self, engine: ReplayEngine
    ) -> None:
        start_url = engine.adapter.page.url
        result = engine.execute(
            make_artifact(), {"amount": "not-a-number", "mode": "fast"}
        )
        assert isinstance(result, FailureResult)
        assert result.reason == "invalid_params"
        assert result.failed_step is None
        assert "amount" in (result.expected or "")
        # Pre-flight (§6c): the browser was never touched.
        assert engine.adapter.page.url == start_url

    def test_missing_required_param_rejected(self, engine: ReplayEngine) -> None:
        result = engine.execute(make_artifact(), {"amount": "1.00"})
        assert isinstance(result, FailureResult)
        assert result.reason == "invalid_params"
        assert "required input 'mode'" in (result.expected or "")

    def test_unknown_param_rejected(self, engine: ReplayEngine) -> None:
        result = engine.execute(
            make_artifact(), {"amount": "1.00", "mode": "fast", "bonus": "x"}
        )
        assert isinstance(result, FailureResult)
        assert "unexpected inputs" in (result.observed or "")


class TestFailureTaxonomy:
    def test_missing_element_produces_failure_with_detail(
        self, engine: ReplayEngine
    ) -> None:
        artifact = make_artifact()
        # Inject an unresolvable step: no candidate matches anything.
        artifact = artifact.model_copy(
            update={
                "steps": [
                    s.model_copy(
                        update={
                            "locator_candidates": [
                                loc(LocatorStrategy.ID_ATTRIBUTE, "#nope"),
                                loc(LocatorStrategy.CSS_STRUCTURAL, "body > frame"),
                            ]
                        }
                    )
                    if s.action is ActionType.CLICK
                    else s
                    for s in artifact.steps
                ]
            }
        )
        result = engine.execute(artifact, {"amount": "150.00", "mode": "fast"})
        assert isinstance(result, FailureResult)
        assert result.reason == "element_not_found"
        assert result.failed_step == 3  # the click step, 0-indexed
        assert "id_attribute" in (result.expected or "")
        assert "url=" in (result.observed or "")

    def test_checkpoint_mismatch_produces_failure_with_expected_and_observed(
        self, engine: ReplayEngine
    ) -> None:
        artifact = make_artifact()
        artifact = artifact.model_copy(
            update={
                "steps": [
                    s.model_copy(
                        update={"checkpoint": Checkpoint(url=PAGE_ONE, page_identity="Wrong Page Identity")}
                    )
                    if s.action is ActionType.CLICK
                    else s
                    for s in artifact.steps
                ]
            }
        )
        result = engine.execute(artifact, {"amount": "150.00", "mode": "fast"})
        assert isinstance(result, FailureResult)
        assert result.reason == "checkpoint_mismatch"
        assert result.failed_step == 3
        assert "Wrong Page Identity" in (result.expected or "")
        assert "Tools Page One" in (result.observed or "")

    def test_terminal_checkpoint_mismatch(self, engine: ReplayEngine) -> None:
        artifact = make_artifact(
            terminal_checkpoint=Checkpoint(url=PAGE_TWO, page_identity="Tools Page Two")
        )
        result = engine.execute(artifact, {"amount": "150.00", "mode": "fast"})
        assert isinstance(result, FailureResult)
        assert result.reason == "terminal_checkpoint_mismatch"


class TestLocatorFallback:
    def test_primary_fails_secondary_succeeds(self, engine: ReplayEngine) -> None:
        """The type_text step's first candidate matches nothing; replay
        must fall through to #amount and still succeed."""
        result = engine.execute(make_artifact(), {"amount": "150.00", "mode": "fast"})
        assert isinstance(result, SuccessResult)


class TestRiskGate:
    def test_point_of_no_return_requires_confirm(self, engine: ReplayEngine) -> None:
        artifact = make_artifact(
            risk_level=RiskLevel.STATE_CHANGING_IRREVERSIBLE,
            point_of_no_return_step=3,  # the click
        )
        result = engine.execute(artifact, {"amount": "150.00", "mode": "fast"})
        assert isinstance(result, FailureResult)
        assert result.reason == "confirmation_required"
        assert result.failed_step == 3
        assert result.steps_executed == 3  # the gated step never ran

    def test_confirm_allows_point_of_no_return(self, engine: ReplayEngine) -> None:
        artifact = make_artifact(
            risk_level=RiskLevel.STATE_CHANGING_IRREVERSIBLE,
            point_of_no_return_step=3,
        )
        result = engine.execute(
            artifact, {"amount": "150.00", "mode": "fast"}, confirm=True
        )
        assert isinstance(result, SuccessResult)


class TestSessionLifecycle:
    def test_session_torn_down_on_success(self) -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            pages = []

            def factory():
                page = browser.new_page()
                pages.append(page)
                return PlaywrightPerceptionAdapter(page)

            result = replay_artifact(
                make_artifact(),
                {"amount": "150.00", "mode": "fast"},
                adapter_factory=factory,
                retry_delays=(0.0, 0.0),
            )
            assert isinstance(result, SuccessResult)
            # §6d: the per-invocation session was torn down after success.
            assert pages[0].is_closed()
            browser.close()


class TestResultUnion:
    def test_discriminated_union_round_trip(self) -> None:
        adapter = TypeAdapter(ReplayResult)
        for result in (
            SuccessResult(artifact_name="a", steps_executed=1, outputs={"x": "y"}),
            BusinessOutcomeResult(
                artifact_name="a",
                steps_executed=1,
                step_index=2,
                description="insufficient funds",
                matched_text="insufficient funds for transfer",
            ),
            FailureResult(
                artifact_name="a", steps_executed=0, reason="element_not_found"
            ),
        ):
            rebuilt = adapter.validate_json(result.model_dump_json())
            assert rebuilt == result
            assert type(rebuilt) is type(result)

    def test_rollup_first_non_success_short_circuits(self) -> None:
        results = [
            SuccessResult(artifact_name="a", steps_executed=1),
            BusinessOutcomeResult(
                artifact_name="a",
                steps_executed=2,
                step_index=1,
                description="insufficient funds",
            ),
            SuccessResult(artifact_name="a", steps_executed=3),
        ]
        rolled = rollup(results)
        assert isinstance(rolled, BusinessOutcomeResult)
        assert rolled.steps_executed == 2

    def test_rollup_all_success_merges_outputs(self) -> None:
        rolled = rollup(
            [
                SuccessResult(artifact_name="a", steps_executed=2, outputs={"x": "1"}),
                SuccessResult(artifact_name="a", steps_executed=3, outputs={"y": "2"}),
            ]
        )
        assert isinstance(rolled, SuccessResult)
        assert rolled.steps_executed == 5
        assert rolled.outputs == {"x": "1", "y": "2"}


class _TransientAdapter:
    """A surface whose page settles: early observations show the transient
    pre-render identity (title fallback — no heading), later ones the
    settled heading — mirroring ParaBank's async (JMS) account-data
    rendering that instant post-action replay checks must wait out (§6b)."""

    def __init__(self, *, settle: bool = True):
        from bankops.perception.base import Observation, ObservedElement

        def obs(identity: str) -> Observation:
            return Observation(
                url="http://localhost:8080/parabank/overview.htm",
                title="ParaBank | Accounts Overview",
                page_identity=identity,
                elements=[
                    ObservedElement(
                        index=0, tag="a", role="link", name="Log Out",
                        locators=[],
                    )
                ],
            )

        self._settled = obs("Account Services")
        self._transient = obs("ParaBank | Accounts Overview")
        self._transients_left = 2 if settle else 10**9

    def observe(self):
        if self._transients_left > 0:
            self._transients_left -= 1
            return self._transient
        return self._settled

    def capture_screenshot(self, path):
        from pathlib import Path

        return Path(path)

    def resolve_locator(self, candidate):
        raise RuntimeError("not needed")

    def navigate(self, url):
        return 200

    def current_url(self):
        return "http://localhost:8080/parabank/overview.htm"

    def close(self):
        pass


class TestCheckpointSettleRetry:
    def _artifact(self):
        from bankops.artifact.models import (
            ActionType,
            Artifact,
            Checkpoint,
            RiskLevel,
            Step,
        )

        checkpoint = Checkpoint(
            url="http://localhost:8080/parabank/overview.htm",
            page_identity="Account Services",
        )
        return Artifact(
            name="settle_test",
            goal="log in",
            steps=[
                Step(
                    action=ActionType.NAVIGATE,
                    navigate_url="http://localhost:8080/parabank/overview.htm",
                    checkpoint=checkpoint,
                )
            ],
            terminal_checkpoint=checkpoint,
            risk_level=RiskLevel.READ_ONLY,
        )

    def test_checkpoint_waits_out_transient_page_state(self) -> None:
        """Regression (observed live): ParaBank's overview page transiently
        lacks its identity heading right after login (async JMS render);
        replay must re-observe with backoff instead of failing."""
        from bankops.replay.engine import ReplayEngine

        engine = ReplayEngine(_TransientAdapter(), retry_delays=(0.0, 0.0, 0.0))
        result = engine.execute(self._artifact(), {})
        assert result.status == "success"

    def test_never_settling_checkpoint_still_fails_typed(self) -> None:
        from bankops.replay.engine import ReplayEngine

        engine = ReplayEngine(
            _TransientAdapter(settle=False), retry_delays=(0.0, 0.0, 0.0)
        )
        result = engine.execute(self._artifact(), {})
        assert result.status == "failure"
        assert result.reason == "checkpoint_mismatch"
        assert "Account Services" in result.expected
