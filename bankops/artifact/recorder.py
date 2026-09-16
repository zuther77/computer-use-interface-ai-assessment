"""Recorder / distillation pipeline (Phase 5; DECISIONS.md §4c, §4d, §5a–5c).

Two-phase by design (§5b): the discovery loop produces only a raw action
log; this post-processing pass turns a ``finish``-terminated run into a
minimal, replayable artifact.

Filtering (§5a) — successful path only:
  1. failed/blocked entries are dropped (they belong to raw evidence),
  2. abandoned branches are dropped: a page excursion that returns to a
     previously-seen page without the run ending there is removed entirely
     (its actions — including any extracts — survive only in the raw
     evidence log),
  3. last-write-wins per target: a retype/select on the same element keeps
     only the final successful value.

Trigger (§5c) — strict: an artifact is only built from a run that ended
via ``finish``. A ``report_stuck``/max-steps/no-progress run produces
nothing, deliberately — a half-built capability sitting in the same store
as real ones is a safety concern, not just mess.

Risk level defaults to read_only and is meant to be set manually at
authoring time (§8b, tracked action item).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bankops.agent.loop import ActionLogEntry, RunResult, RunStatus
from bankops.artifact.models import (
    ActionType,
    Artifact,
    Checkpoint,
    InputParam,
    LocatorCandidate,
    OutputField,
    ParamType,
    RiskLevel,
    Step,
)

_VALUE_WRITING = frozenset({ActionType.TYPE_TEXT, ActionType.SELECT_OPTION})


def distill(
    result: RunResult,
    *,
    name: str,
    risk_level: RiskLevel = RiskLevel.READ_ONLY,
    target_base_url: str | None = None,
) -> Artifact | None:
    """Distill a raw discovery run into an Artifact, or return None.

    Returns None unless the run ended via ``finish`` (§5c), or when the
    successful path contains no page-interacting steps to record.
    """
    if result.status is not RunStatus.COMPLETED:
        return None

    # -- Step 1: successful page-interacting entries only -------------------
    # remember is internal working memory (§4d), never an artifact step;
    # finish is the terminal signal, kept aside for the terminal checkpoint.
    flow = [
        e
        for e in result.action_log
        if e.status == "success" and e.action not in ("remember",)
    ]
    finish_entry = next(
        (e for e in reversed(flow) if e.action == "finish"), None
    )
    flow = [e for e in flow if e.action != "finish"]

    if not flow:
        return None

    # -- Step 2: drop abandoned branches (§5a) -------------------------------
    # A revisit of a page seen earlier closes an excursion: every entry
    # between the earlier visit and the return is an abandoned branch.
    flow = _drop_abandoned_branches(flow)
    flow = _collapse_redundant_navigations(flow)

    # -- Step 3: last-write-wins per target (§5a) ----------------------------
    flow = _last_write_wins(flow)

    # -- Step 4: classify values into named inputs/outputs (§4c, §4d) -------
    used_names: set[str] = set()

    def unique_name(base: str, prefix: str) -> str:
        candidate = _snake_case(base)
        if not candidate:
            candidate = f"{prefix}{len(used_names) + 1}"
        while candidate in used_names:
            candidate = f"{candidate}_{len(used_names) + 1}"
        used_names.add(candidate)
        return candidate

    steps: list[Step] = []
    inputs: list[InputParam] = []
    outputs: list[OutputField] = []

    for entry in flow:
        checkpoint = Checkpoint(
            url=entry.url,
            page_identity=(
                entry.page_identity or entry.page_title or "unknown page"
            ),
        )
        action = ActionType(entry.action)
        if action is ActionType.NAVIGATE:
            steps.append(
                Step(
                    action=action,
                    navigate_url=str(entry.params.get("url", "")),
                    checkpoint=checkpoint,
                )
            )
        elif action in _VALUE_WRITING:
            param_name = unique_name(entry.element_name, "input_")
            inputs.append(
                InputParam(
                    name=param_name,
                    param_type=_infer_type(entry.data or ""),
                    description=f"Value for {entry.element_role} "
                    f"'{entry.element_name or param_name}'",
                )
            )
            steps.append(
                Step(
                    action=action,
                    locator_candidates=list(entry.locator_candidates),
                    input_name=param_name,
                    checkpoint=checkpoint,
                )
            )
        elif action is ActionType.EXTRACT:
            output_name = unique_name(entry.element_name, "extract_")
            outputs.append(
                OutputField(
                    name=output_name,
                    output_type=_infer_type(entry.data or ""),
                    description=f"Extracted from {entry.element_role} "
                    f"'{entry.element_name or output_name}'",
                )
            )
            steps.append(
                Step(
                    action=action,
                    locator_candidates=list(entry.locator_candidates),
                    output_name=output_name,
                    checkpoint=checkpoint,
                )
            )
        else:  # click
            steps.append(
                Step(
                    action=action,
                    locator_candidates=list(entry.locator_candidates),
                    checkpoint=checkpoint,
                )
            )

    if not steps:
        return None

    terminal = finish_entry
    if terminal is None and result.final_observation is not None:
        terminal_obs = result.final_observation
        terminal = ActionLogEntry(
            step=0,
            action="finish",
            status="success",
            url=terminal_obs.url,
            page_title=terminal_obs.title,
            page_identity=terminal_obs.page_identity,
        )

    base_url = target_base_url
    if base_url is None:
        first_url = next((e.url for e in flow if e.url), "")
        parts = urlparse(first_url)
        base_url = f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""

    return Artifact(
        name=name,
        goal=result.goal,
        target_base_url=base_url,
        steps=steps,
        inputs=inputs,
        outputs=outputs,
        terminal_checkpoint=Checkpoint(
            url=terminal.url,
            page_identity=(
                terminal.page_identity or terminal.page_title or "unknown page"
            ),
        ),
        risk_level=risk_level,
    )


# -- Filtering helpers ---------------------------------------------------------


def _drop_abandoned_branches(
    flow: list[ActionLogEntry],
) -> list[ActionLogEntry]:
    """Remove closed page excursions (§5a).

    Walking forward, when the flow *transitions* onto a page it has seen
    before, the stretch since that page's last visit is an abandoned
    branch: the flow left, accomplished nothing that survived, and came
    back. All its entries are dropped — they remain in the raw evidence
    log only. Consecutive entries on the same page (typing into several
    fields) are not revisits and are never dropped by this rule.
    """
    kept: list[ActionLogEntry] = []
    last_seen: dict[str, int] = {}
    for entry in flow:
        url = entry.url
        if url and url in last_seen and kept and kept[-1].url != url:
            # Transitioned back onto a previously-seen page: close and
            # drop the excursion since its last visit.
            del kept[last_seen[url] + 1 :]
        if url:
            last_seen[url] = len(kept)
        kept.append(entry)
    return kept


def _collapse_redundant_navigations(
    flow: list[ActionLogEntry],
) -> list[ActionLogEntry]:
    """Drop a navigate immediately followed by another navigate to the same
    URL (e.g. the entry navigation and a branch-return navigation landing
    on the same page). The artifact stays minimal; the dropped navigation
    survives in the raw evidence log."""
    result: list[ActionLogEntry] = []
    for entry in flow:
        if (
            result
            and entry.action == ActionType.NAVIGATE.value
            and result[-1].action == ActionType.NAVIGATE.value
            and entry.params.get("url") == result[-1].params.get("url")
        ):
            result.pop()  # superseded by the later navigation
        result.append(entry)
    return result


def _last_write_wins(flow: list[ActionLogEntry]) -> list[ActionLogEntry]:
    """Keep only the final value written to each target element (§5a):
    a self-correction (retype) leaves one entry in the artifact; the
    superseded attempts survive in the raw evidence log only."""
    last_by_target: dict[tuple[str, int | None], int] = {}
    for position, entry in enumerate(flow):
        if ActionType(entry.action) in _VALUE_WRITING:
            last_by_target[(entry.url, entry.element_index)] = position
    return [
        entry
        for position, entry in enumerate(flow)
        if ActionType(entry.action) not in _VALUE_WRITING
        or last_by_target.get((entry.url, entry.element_index)) == position
    ]


# -- Name/type inference --------------------------------------------------------


def _snake_case(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _infer_type(value: str) -> ParamType:
    """Infer a field type from the recorded value's shape (§4c/§4d)."""
    normalized = value.strip().replace(",", "").lstrip("$")
    if re.fullmatch(r"-?\d+\.\d+", normalized):
        return ParamType.DECIMAL
    # Leading zeros mean identifier (account/zip codes), not a number.
    if re.fullmatch(r"-?[1-9]\d*", normalized) or normalized == "0":
        return ParamType.INTEGER
    if value.strip().lower() in ("true", "false"):
        return ParamType.BOOLEAN
    return ParamType.STRING
