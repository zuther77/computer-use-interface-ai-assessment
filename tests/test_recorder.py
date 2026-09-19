"""Recorder / distillation tests (Phase 5; DECISIONS.md §4c, §4d, §5a–5c).

Fixed fake raw action logs — no browser, no LLM. Covers: a clean run, a
run with a self-correction (retype), a run with a fully abandoned branch,
and a report_stuck-terminated run (must produce no artifact).
"""

from __future__ import annotations

from bankops.agent.loop import ActionLogEntry, RunResult, RunStatus
from bankops.artifact.models import (
    ActionType,
    LocatorCandidate,
    LocatorStrategy,
    ParamType,
    RiskLevel,
)
from bankops.artifact.recorder import distill
from bankops.perception.base import Observation

BASE = "http://localhost:8080/parabank"
OVERVIEW = f"{BASE}/overview.htm"
TRANSFER = f"{BASE}/transfer.htm"
BILLPAY = f"{BASE}/billpay.htm"

AMOUNT_LOCATORS = [
    LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value='role=textbox name="Amount"'),
    LocatorCandidate(strategy=LocatorStrategy.ID_ATTRIBUTE, value="#amount"),
]


def entry(
    step: int,
    action: str,
    *,
    url: str,
    identity: str,
    params: dict | None = None,
    data: str | None = None,
    element_index: int | None = None,
    element_role: str = "",
    element_name: str = "",
    locators: list | None = None,
    status: str = "success",
) -> ActionLogEntry:
    return ActionLogEntry(
        step=step,
        action=action,
        params=params or {},
        status=status,
        data=data,
        element_index=element_index,
        url=url,
        page_title="ParaBank",
        page_identity=identity,
        element_role=element_role,
        element_name=element_name,
        locator_candidates=locators or [],
    )


def make_result(entries: list[ActionLogEntry], status: RunStatus = RunStatus.COMPLETED) -> RunResult:
    last = entries[-1]
    return RunResult(
        status=status,
        goal="Transfer $150 between accounts",
        reason="done",
        action_log=entries,
        memory={},
        final_observation=Observation(
            url=last.url, title="ParaBank", page_identity=last.page_identity
        ),
    )


def clean_run() -> list[ActionLogEntry]:
    return [
        entry(1, "navigate", url=TRANSFER, identity="Transfer Funds",
              params={"url": TRANSFER}),
        entry(2, "type_text", url=TRANSFER, identity="Transfer Funds",
              params={"index": 1, "text": "150.00"}, data="150.00",
              element_index=1, element_role="textbox", element_name="Amount",
              locators=AMOUNT_LOCATORS),
        entry(3, "select_option", url=TRANSFER, identity="Transfer Funds",
              params={"index": 2, "option": "10001"}, data="10001",
              element_index=2, element_role="combobox", element_name="From Account",
              locators=[LocatorCandidate(strategy=LocatorStrategy.ID_ATTRIBUTE, value="#fromAccountId")]),
        entry(4, "click", url=TRANSFER, identity="Transfer Complete!",
              params={"index": 3}, element_index=3, element_role="button",
              element_name="Transfer",
              locators=[LocatorCandidate(strategy=LocatorStrategy.ROLE_NAME, value='role=button name="Transfer"')]),
        entry(5, "extract", url=TRANSFER, identity="Transfer Complete!",
              params={"index": 4}, data="$150.00 sent", element_index=4,
              element_role="text", element_name="Transfer Complete!",
              locators=[LocatorCandidate(strategy=LocatorStrategy.CSS_STRUCTURAL, value="#transferResult")]),
        entry(6, "finish", url=TRANSFER, identity="Transfer Complete!",
              params={"summary": "transferred"}),
    ]


class TestCleanRun:
    def test_clean_run_produces_minimal_correct_artifact(self) -> None:
        artifact = distill(make_result(clean_run()), name="parabank_transfer_funds")
        assert artifact is not None
        assert artifact.name == "parabank_transfer_funds"
        assert artifact.goal == "Transfer $150 between accounts"
        assert artifact.target_base_url == "http://localhost:8080"

        # Minimal: one step per successful action, remember/finish excluded.
        assert [s.action for s in artifact.steps] == [
            ActionType.NAVIGATE,
            ActionType.TYPE_TEXT,
            ActionType.SELECT_OPTION,
            ActionType.CLICK,
            ActionType.EXTRACT,
        ]

        # §4c: values auto-promoted into named InputParams with
        # label-derived names; types inferred from value shape.
        assert [(p.name, p.param_type) for p in artifact.inputs] == [
            ("amount", ParamType.DECIMAL),
            ("from_account", ParamType.INTEGER),
        ]
        assert artifact.steps[1].input_name == "amount"
        assert artifact.steps[2].input_name == "from_account"
        # Raw values are never burned into the artifact (§4c).
        assert artifact.steps[1].value is None
        assert not artifact.steps[1].is_constant

        # §4d: extract becomes a declared typed OutputField.
        assert [(o.name, o.output_type) for o in artifact.outputs] == [
            ("transfer_complete", ParamType.STRING)
        ]
        assert artifact.steps[4].output_name == "transfer_complete"

        # §4b: candidate locators carried through in priority order.
        assert artifact.steps[1].locator_candidates == AMOUNT_LOCATORS

        # §4e: per-step signatures plus the deliberate terminal checkpoint.
        assert artifact.steps[0].checkpoint.url == TRANSFER
        assert artifact.steps[3].checkpoint.page_identity == "Transfer Complete!"
        assert artifact.terminal_checkpoint == artifact.steps[3].checkpoint
        assert artifact.steps[0].checkpoint.page_identity == "Transfer Funds"

        # §8b: manual risk-tag action item — recorder defaults, author sets.
        assert artifact.risk_level is RiskLevel.READ_ONLY

    def test_clean_run_artifact_serializes(self, tmp_path) -> None:
        artifact = distill(make_result(clean_run()), name="parabank_transfer_funds")
        path = artifact.save(tmp_path / "transfer.json")
        from bankops.artifact.models import Artifact

        assert Artifact.load(path) == artifact


class TestSelfCorrection:
    def test_retype_keeps_only_final_value(self) -> None:
        log = [
            entry(1, "navigate", url=TRANSFER, identity="Transfer Funds",
                  params={"url": TRANSFER}),
            entry(2, "type_text", url=TRANSFER, identity="Transfer Funds",
                  params={"index": 1, "text": "10.00"}, data="10.00",
                  element_index=1, element_role="textbox", element_name="Amount",
                  locators=AMOUNT_LOCATORS),
            entry(3, "type_text", url=TRANSFER, identity="Transfer Funds",
                  params={"index": 1, "text": "150.00"}, data="150.00",
                  element_index=1, element_role="textbox", element_name="Amount",
                  locators=AMOUNT_LOCATORS),
            entry(4, "click", url=TRANSFER, identity="Transfer Complete!",
                  params={"index": 3}, element_index=3, element_role="button",
                  element_name="Transfer",
                  locators=[]),
            entry(5, "finish", url=TRANSFER, identity="Transfer Complete!",
                  params={"summary": "done"}),
        ]
        artifact = distill(make_result(log), name="transfer")
        assert artifact is not None
        # Exactly one type_text step for the superseded target (§5a).
        type_steps = [
            s for s in artifact.steps if s.action is ActionType.TYPE_TEXT
        ]
        assert len(type_steps) == 1
        assert [p.name for p in artifact.inputs] == ["amount"]


class TestAbandonedBranch:
    def test_fully_abandoned_branch_dropped(self) -> None:
        log = [
            entry(1, "navigate", url=OVERVIEW, identity="Accounts Overview",
                  params={"url": OVERVIEW}),
            # -- abandoned branch: transfer page, then back to overview --
            entry(2, "navigate", url=TRANSFER, identity="Transfer Funds",
                  params={"url": TRANSFER}),
            entry(3, "type_text", url=TRANSFER, identity="Transfer Funds",
                  params={"index": 1, "text": "50.00"}, data="50.00",
                  element_index=1, element_role="textbox", element_name="Amount",
                  locators=AMOUNT_LOCATORS),
            entry(4, "navigate", url=OVERVIEW, identity="Accounts Overview",
                  params={"url": OVERVIEW}),
            # -- the path that actually reached the goal --
            entry(5, "navigate", url=BILLPAY, identity="Bill Pay",
                  params={"url": BILLPAY}),
            entry(6, "type_text", url=BILLPAY, identity="Bill Pay",
                  params={"index": 1, "text": "150.00"}, data="150.00",
                  element_index=1, element_role="textbox", element_name="Amount",
                  locators=AMOUNT_LOCATORS),
            entry(7, "click", url=BILLPAY, identity="Bill Payment Sent!",
                  params={"index": 3}, element_index=3, element_role="button",
                  element_name="Send"),
            entry(8, "finish", url=BILLPAY, identity="Bill Payment Sent!",
                  params={"summary": "paid"}),
        ]
        artifact = distill(make_result(log), name="billpay")
        assert artifact is not None
        assert [s.action for s in artifact.steps] == [
            ActionType.NAVIGATE,   # → overview
            ActionType.NAVIGATE,   # → billpay (the return navigate repurposed)
            ActionType.TYPE_TEXT,  # billpay amount — the only surviving input
            ActionType.CLICK,
        ]
        urls = {s.checkpoint.url for s in artifact.steps}
        assert TRANSFER not in urls  # abandoned branch is gone entirely
        assert [(p.name, p.param_type) for p in artifact.inputs] == [
            ("amount", ParamType.DECIMAL)
        ]


class TestStrictTrigger:
    def test_report_stuck_run_produces_no_artifact(self) -> None:
        log = [
            entry(1, "navigate", url=TRANSFER, identity="Transfer Funds",
                  params={"url": TRANSFER}),
            entry(2, "report_stuck", url=TRANSFER, identity="Transfer Funds",
                  params={"reason": "transfer form rejects every amount"}),
        ]
        artifact = distill(
            make_result(log, status=RunStatus.STUCK), name="should_not_exist"
        )
        assert artifact is None

    def test_max_steps_run_produces_no_artifact(self) -> None:
        log = [
            entry(1, "click", url=OVERVIEW, identity="Accounts Overview",
                  params={"index": 0}, element_index=0, element_role="button",
                  element_name="Log In"),
        ]
        assert distill(
            make_result(log, status=RunStatus.MAX_STEPS), name="x"
        ) is None

    def test_failed_actions_excluded_from_steps(self) -> None:
        log = clean_run()
        # A failed click (bad index) and a blocked navigate — neither belongs
        # in the successful path, but they stay in the raw evidence log.
        log.insert(
            2,
            entry(2, "click", url=TRANSFER, identity="Transfer Funds",
                  params={"index": 99}, status="error"),
        )
        artifact = distill(make_result(log), name="transfer")
        assert artifact is not None
        assert [s.action for s in artifact.steps] == [
            ActionType.NAVIGATE,
            ActionType.TYPE_TEXT,
            ActionType.SELECT_OPTION,
            ActionType.CLICK,
            ActionType.EXTRACT,
        ]


class TestObserveEntriesDistilledOut:
    """Regression (observed live, twice): a completed run containing
    successful `observe` actions crashed distillation with
    ``ValueError: 'observe' is not a valid ActionType`` — no artifact was
    saved from either run. Observe is a perception aid, not a capability
    step (§3d class, like remember): it must be filtered out, and the
    artifact must contain only recorded page-interacting steps."""

    def test_observe_entries_are_dropped_not_crashed(self) -> None:
        from bankops.agent.loop import ActionLogEntry, RunResult, RunStatus
        from bankops.artifact.recorder import distill

        def entry(step, action, **kw):
            base = dict(
                step=step,
                action=action,
                status="success",
                url="http://localhost:8080/parabank/requestloan.htm",
                page_title="ParaBank | Request Loan",
                page_identity="Apply for a Loan",
            )
            base.update(kw)
            return ActionLogEntry(**base)

        run = RunResult(
            status=RunStatus.COMPLETED,
            goal="request a loan",
            reason="done",
            action_log=[
                entry(
                    1,
                    "type_text",
                    params={"index": 3, "text": "45000"},
                    data="45000",
                    element_index=3,
                    element_role="textbox",
                    element_name="Loan Amount: $",
                ),
                entry(2, "observe", params={}),
                entry(3, "finish", params={"summary": "done"}),
            ],
        )
        artifact = distill(run, name="loan_request")
        assert artifact is not None
        # No crash, and observe never appears as a step:
        assert [s.action.value for s in artifact.steps] == ["type_text"]
        assert [i.name for i in artifact.inputs] == ["loan_amount"]
        # The finish entry still supplies the terminal checkpoint:
        assert artifact.terminal_checkpoint.url.endswith("requestloan.htm")


class TestCheckpointErrorRecording:
    """§4e: checkpoints must carry the error-classed messages the recorded
    success path showed (normally none) so replay can distinguish the
    recorded state from an error state with identical URL/heading."""

    def test_recorder_populates_allowed_error_messages(self) -> None:
        from bankops.agent.loop import ActionLogEntry, RunResult, RunStatus
        from bankops.artifact.recorder import distill

        def entry(step, action, **kw):
            base = dict(
                step=step,
                action=action,
                status="success",
                url="http://localhost:8080/parabank/findtrans.htm",
                page_title="ParaBank | Find Transactions",
                page_identity="Find Transactions",
                error_messages=[],
            )
            base.update(kw)
            return ActionLogEntry(**base)

        run = RunResult(
            status=RunStatus.COMPLETED,
            goal="find transactions",
            reason="done",
            action_log=[
                entry(
                    1,
                    "type_text",
                    params={"index": 7, "text": "01-01-2000"},
                    data="01-01-2000",
                    element_index=7,
                    element_role="textbox",
                    element_name="Between",
                    # The recorded success path showed a tolerated error:
                    error_messages=["Some tolerated notice"],
                ),
                entry(2, "finish", params={"summary": "done"}),
            ],
        )
        artifact = distill(run, name="find_transactions")
        assert artifact is not None
        assert artifact.steps[0].checkpoint.allowed_error_messages == [
            "Some tolerated notice"
        ]
        # Terminal checkpoint recorded from the finish entry too:
        assert artifact.terminal_checkpoint.allowed_error_messages == []
