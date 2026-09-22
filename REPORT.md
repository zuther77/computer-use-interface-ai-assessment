# REPORT — Computer-Use Automation System (bankops)

A Python system that lets an LLM agent *discover* a workflow on a web
application once, distills what it did into a typed, reviewable
**artifact**, and then **replays** that artifact deterministically — no LLM
involved — with typed success/business-outcome/failure classification,
guardrails, and a real human-handoff mechanism.

Target surface: **ParaBank** (Parasoft's demo bank), self-hosted via
`docker-compose.yml`. Everything below is grounded in `DECISIONS.md`
(§-references) and in real run evidence under `/evidence/` (run ids cited).

---

## 1. Architecture

**Stack (§1–§2d).** Python 3.13 · Playwright (DOM/role-based locators;
screenshots are evidence-only, never decision inputs) · GLM via the OpenAI
SDK (configurable `base_url`/`api_key`, so a provider swap is a config
change, not a rewrite) · Pydantic v2 everywhere — the artifact contract,
tool results, evidence logs, and intervention requests are all
runtime-validated typed models.

**The two loops.**

- *Discovery* (`bankops/agent/loop.py`): observe → decide → act. Each step
  the LLM sees the **current observation only** — a compact indexed list of
  interactive elements built from the accessibility snapshot
  (`[3] textbox "Username"`, §3a) — plus a compact action-history summary and
  an explicit `remember()` working store. No transcript replay: state is
  structured, so request cost grows linearly, not quadratically (§3c).
  One typed tool per action (`click`, `type_text`, `select_option`,
  `navigate`, `extract`, `remember`, `observe`, `finish`, `report_stuck`,
  §3b) — a closed, auditable action space the allowlist depends on.
  Layered, typed stopping conditions (§3d): max-step ceiling, no-progress
  detection via observation-hash comparison, and explicit
  `finish`/`report_stuck` — which is exactly what the escalation trigger
  consumes.
- *Replay* (`bankops/replay/engine.py`): executes a saved artifact with no
  LLM in the loop, validating every step's post-state against recorded
  checkpoints (§4e) and classifying deviations (§7a–§7c). One browser
  context per invocation; kept alive only on the escalation exit path (§6d).

**The seam (§11a).** Both loops touch the browser only through
`PerceptionAdapter` (`bankops/perception/base.py`): *observe → indexed
elements with candidate locators*, *resolve a locator*, *screenshot*. The
adapter emits role-based locator candidates (`role_name`, `id_attribute`,
`text_content`, `css_structural`); a different surface (desktop, legacy
frames) is a new adapter emitting the same shapes — schema, replay, and
error taxonomy are untouched. This isn't a hypothetical: it's the same seam
that made ParaBank's legacy table layouts (fields labelled by a preceding
`<td>`, no `<label>`) work with one fallback rule in the adapter.

**Recording pipeline (§5a–§5c).** The discovery loop produces a raw action
log only. A separate pass distills it: drop failures, drop abandoned
excursions (a navigation that returns to a previously-seen page closes the
branch), last-write-wins per target for retypes, then classify surviving
`type_text`/`select_option` values into named `InputParam`s
(label-derived names, §4c) and `extract` results into typed `OutputField`s
(§4d). An artifact is built **only** from a `finish`-terminated run — a
half-built capability next to real ones is a safety hazard, not
convenience (§5c).

**Evidence (§10a/§10b).** Every run writes a self-contained folder
(`discovery_run_{id}/`, `replay_success_{id}/`, `replay_error_{id}/`):
JSONL step log (one typed line per step — action, which locator tier
resolved, the model's one-sentence reasoning, result classification),
screenshots, the saved artifact, and a run summary. Redaction is applied
inside the writer, before anything hits disk (§8c).

## 2. Artifact schema (`bankops/artifact/models.py`)

A flat, linear ordered step list (§4a) — what one real run produced;
branching lives in replay-time checks, never in artifact structure.

- **Locator candidates (§4b)**: per step, an *ordered* list of
  `(strategy, value)` pairs tried in priority order until **exactly one**
  visible/enabled element resolves. Racing all candidates was rejected for
  reintroducing non-determinism; a single locator was rejected for having
  no fallback. Live-proven repeatedly — e.g. ParaBank renders the bill-pay
  phone field with a **random UUID id**, so the recorded `#<uuid>` dies on
  the next session and the stable `input[name="payee.phoneNumber"]`
  candidate (emitted alongside, after observing the id was
  non-identifier-shaped) carries the replay (`replay_20260921_180816`).
- **Inputs (§4c)**: auto-promoted from typed values, names derived from
  accessible labels — which is also what makes sensitive-name redaction
  (§8c) reliable. Optional per-step `is_constant` override for when
  auto-promotion is wrong. Live lesson recorded: a field the model skipped
  because the UI default already matched the goal never became a parameter
  — the transfer artifact was silently locked to the dropdown default
  (`discovery_20260921_152832`). The recorder can only parameterize what
  it saw executed; the system prompt now forces explicit setting of
  goal-specified fields, and the artifact was repaired by hand (documented
  in the step's description).
- **Outputs (§4d)**: `extract` steps declare typed `OutputField`s;
  `remember` stays internal scratch — caller-facing data is deliberately
  separated from working memory.
- **Checkpoints (§4e)**: every step carries an automatic post-step
  signature (URL + stable page-identity signal), plus one deliberate
  terminal checkpoint tied to the goal. Implicit "all steps ran" success was
  ruled out — it's exactly the "assumed the click worked" mistake. The
  schema grew two fields from live incidents (see §3): `allowed_error_messages`
  (error-classed page text observed on the recorded path) and
  `required_locator` (a goal-tied element that must be present).
- **Outcome signatures (§7a)**: per step, recorded alternate outcomes —
  `(locator, text_pattern, classification)` with classification
  `business_outcome` / `recoverable` / `hard_failure`. Captured deliberately
  (a targeted triggering pass), never inferred generically.
- **Risk fields (§8b)**: `risk_level` set manually at authoring time;
  irreversible artifacts also name their `point_of_no_return_step`, which
  replay refuses to execute without an explicit `confirm=True`.
- **Serialization (§4f)**: plain JSON with `schema_version`; Pydantic is
  the canonical typed schema. A database was ruled out — file-first is
  reviewer-friendly and right at this scope.

## 3. Determinism & error handling

**Locator resolution (§6a)**: candidates in priority order, stop at the
first *unique* match, fail only when all are exhausted. First-unique-match
(rather than racing) is what keeps replay deterministic.

**Waiting (§6b)**: Playwright auto-wait at the element level, plus an
explicit tiered step-level retry (0/1/3s) for checkpoint verification —
crucial for ParaBank, which renders its loan decision **asynchronously**
into a page whose URL and heading never change. The retry only helps when
the recorded state is distinguishable (next point).

**Pre-flight validation (§6c)**: caller-supplied params are validated
against the artifact's Pydantic input model before any browser
interaction. Cheap (the schema is already Pydantic), and bad input is
rejected at the boundary instead of surfacing mid-flow.
One honest quirk: value-shape type inference typed ParaBank's phone field
as `integer`, so `+1-555…` fails pre-flight — a §4c heuristic limit, not a
bug.

**The three live-proven state rules** (all added because a real run got
them wrong, each with the failing run kept as evidence):

1. *Error-banner rule*: an error-classed page message that the recorded
   path did not have means the step did **not** land in the recorded state.
   Live: a swapped-dates transaction search replayed "successfully" while
   the page showed ParaBank's `Invalid date format` — URL and heading
   matched, the old code never looked at page text
   (`replay_error_replay_20260921_145939`-era runs).
2. *Goal-tied `required_locator`*: some wrong outcomes leave URL, heading
   and error banners all clean — only the goal's content differs. Live: a
   swapped-dates search silently returned an empty result set; the terminal
   checkpoint now requires a transaction-row link to be present.
3. *Outcome signatures (§7a)*: when the primary checkpoint fails, recorded
   alternate outcomes are checked before declaring a hard failure. Live
   demo (`replay_error_replay_20260922_142953`): replaying the recorded
   loan request produces ParaBank's native denial —
   `BUSINESS OUTCOME at step 7`, matched text `Status: Denied … you do not
   have sufficient funds for the given down payment.` — not a failure,
   and not an escalation.

**Result taxonomy (§7b/§7c)**: a discriminated union — `SuccessResult`,
`BusinessOutcomeResult`, `FailureResult` — each carrying only its
relevant fields. First non-success short-circuits the run immediately:
continuing to act on a page in a state the artifact was never designed
for is a safety issue on a financial system, not just a complexity one.
Recoverable signatures are the deliberate exception (§7b): they are
tolerated inline and their errors become tolerated by later checkpoints.

**Two §4e authoring lessons from the live environment**, worth stating
because they generalize:

- ParaBank keeps a **hidden approval template** ("Congratulations, your
  loan has been approved.") in the DOM on *every* loan-result page. A
  text-matching discriminator (`:has-text("Approved")`) therefore matched
  denials too — the first authoring misclassified a denial as a success
  through the pre-render window. The fix discriminates on the marker
  div's *visibility* (`#loanRequestApproved:visible`), which text search
  cannot fake.
- The primary/alternate outcome split assumed an approval is observable.
  In this environment the wealth provider denied every observed request —
  including the original recording run — so the approval-primary is
  *inferred* (documented in the artifact), and the denial signature is
  the only observed truth.

## 4. Heterogeneity & multi-tenant

Not built beyond the seam — by design, the design itself is the answer:

- **Surface abstraction (§11a)**: the `PerceptionAdapter` interface is
  real and load-bearing (both loops and the safety guards use it
  exclusively). A legacy-web adapter would add locator tags
  (`table_cell_position`, `frame_scoped_xpath`); a desktop adapter would
  emit `automation_id` candidates from an OS accessibility API — while
  the schema, replay engine, and error taxonomy operate unchanged on the
  adapter's output shapes. ParaBank's legacy table layouts (labels in
  preceding cells, attribute-only ids) are a working down-payment on this
  claim: they were absorbed by adapter-level fallbacks, with zero changes
  to schema, replay, or taxonomy.
- **Multi-tenant reuse (§11b)**: one canonical base artifact per
  capability; a per-tenant override is a small, optional diff replacing
  specific fields (a locator candidate, a route segment, a label string).
  Drift detection is free: `StepLogEntry` already records which
  locator-strategy tier resolved each step (§10a) — a tenant whose replays
  start falling back to lower-priority tiers (or failing) at a materially
  higher rate is a measurable drift signal with no new instrumentation.
- **Honest boundary case (stated, not hidden)**: a tenant whose
  customization is too large for a small diff gets a newly recorded base
  artifact. Re-recording is the honest answer beyond a certain divergence,
  not a failure of the diff design.

## 5. Escalation & handoff

**InterventionRequest (§9a)**: a structured Pydantic record — run id,
source, goal/artifact reference, step index, typed reason, observation
snapshot, screenshot — persisted to `/pending_interventions/{id}.json`
*the moment it's raised*. Notification routing is a stubbed log alert
(the request object itself is real and complete); routing infrastructure
is out of scope. Both discovery typed-stops and replay hard failures are
wired as triggers (live evidence for both exists).

**The handoff is real (§9b)**: on escalation the loop stops issuing
commands entirely and blocks on a resume signal while a human operates
the **same visible Playwright-controlled window** — the operator console
is the browser itself, matching the assignment's "even a bare operator
surface" note. Live-completed handoffs exist in evidence: e.g.
`7d9a4f7c…` (run `discovery_20260921_162029`) — the agent honestly
reported stuck on bad credentials, the human logged in with the right
password during the pause, wrote `verify`, and the before/after captures
show `login.htm / Error!` → `overview.htm / Accounts Overview`.

**Resume (§9c)**: re-observe-and-verify by default, or an explicit human
decision (`continue` / `mark_complete` / `abandon`). For **discovery**
this is now fully implemented: a `RESUME` signal continues the loop from
the human's state — it re-observes first (so whatever the human did
becomes the next decision's input), keeps the structured action history,
and continues the evidence step numbering; bounded to three
pause/resume cycles per run. The incident that motivated it is itself in
evidence: the first version recorded the decision and ended the run —
the resumed loop simply wasn't built (fixed in commit `1b49161`).
For **replay**, resume verifies the next step's expected checkpoint;
a mismatch raises a *new* intervention rather than guessing
(`HandoffManager`, unit-tested). Partial replay resume — continuing
mid-artifact from a human-fixed page — is deliberately a documented cut
(§7 below): the engine replays artifacts from the top, and the honest
human path is `continue` after verification or a fresh invocation.

**Human-action recording (§9d)**: passive before/after capture —
screenshot + observation snapshot at pause and resume, plus a navigation
trail during the human's turn (`{id}.human_log.json`), reusing the
existing evidence machinery. Input-event-level capture (every keystroke)
was ruled out as disproportionate; documented as a limitation, not a gap.

## 6. Safety

**Two-dimensional allowlist (§8a)**, declarative in
`config/allowlist.yaml`: (1) domains/schemes plus route patterns checked
at `navigate`, now with **deny-first** `denied_routes`; (2) an
action-type permission set checked at *every* tool invocation. Two
real incidents shaped it:

- *Deny-first routes*: ParaBank's admin page hosts destructive controls
  (database Initialize/Clean/Shutdown, access-mode switching) *inside*
  the allowed `/parabank/*` route space. A goal-blocked run navigated
  there and clicked **Clean — wiping the database** — because nothing
  could say "this route, specifically, is forbidden" (`…160437`, evidence
  kept). `/parabank/admin*` is now explicitly denied, taking precedence
  over any allowed pattern.
- *Link clicks are navigations*: the same run reached the admin page by
  **clicking a link** — the route check only guarded the `navigate`
  tool. The adapter now records every anchor's absolute `href` and the
  `click` tool checks it against the allowlist *before* executing.
  (Replay-side clicks remain trusted-by-review — a different trust model:
  artifacts are reviewed, risk-tagged, and confirmation-gated before
  they ever run.)

**Risk tags & the confirmation gate (§8b)** — both DECISIONS.md tracked
action items, done and live-demonstrated: `parabank_pay_bills` is tagged
`state_changing_irreversible` with its point of no return at the real
"Send Payment" button (verified by its role locator before tagging);
`parabank_request_loan` is `state_changing_reversible`; `parabank_login`
is `read_only`. Live demo (both directions, evidence
`replay_20260922_015915/015916`): replaying the bill payment **without**
`confirm=True` refuses exactly at step 13 —
`confirmation_required … re-invoke with confirm=True` — and correctly
does **not** escalate (a missing confirmation is not a handoff); with
`--confirm` the payment executes. Classification-by-action-type was
rejected throughout: risk lives in what a capability *accomplishes*, not
in click mechanics; hence manual authoring-time tags.

**No fabricated data (prompt-level, honestly labeled)**: a bill-pay run
whose goal omitted zip and phone *invented* values to pass validation
(`…175212`, evidence kept — it fabricated a real-looking zip from
general knowledge). The system prompt now forbids inventing any value
the goal doesn't provide and routes missing required values through
`report_stuck` → intervention → human supplies them during the handoff.
This is a **model-judgment constraint, not a deterministic gate** — a
grounding check would be exactly the fuzzy matching the design rejects
elsewhere (§7a/§8a rationale); the compensating control is that every
typed value and the model's reasoning are logged for review, which is
precisely how this incident was caught.

**Redaction (§8c)** — implemented and test-verified, but **currently OFF
by operator preference** (`BANKOPS_REDACT=false`, `--redact` to enable):
live demo runs show real (demo-bank) values in the terminal and evidence.
This is a **deliberate, documented deviation** from the §10 checklist
item "no unmasked sensitive fields under `/evidence/`": the values in
evidence are ParaBank demo data (john/demo, sample SSN/account numbers,
fabricated demo address); the real LLM API key lives only in the
git-ignored `.env` and is masked by the writer whenever redaction is on.
Credentials are read from environment/config at session start and never
written to any log, artifact, or evidence file in either mode.

## 7. Cuts — what actually got simplified, and why

Written last, from what really happened — not a guess made in advance.

1. **Replay handoff doesn't resume the engine mid-run.** The replay
   handoff records the human's decision and verifies the next
   checkpoint, but a partial-artifact resume (continue from step N on a
   human-fixed page) is not implemented; the engine replays from the top.
   Discovery resume *is* implemented (§9c above). A partial-resume API on
   the engine is the natural next step.
2. **Redaction off by default (operator preference, "for now").** The
   masking machinery exists and is tested (§8c); the shipped evidence
   was produced with it off so demo runs show real values. One env var
   or `--redact` flips it.
3. **The no-fabrication rule is prompt-level.** No deterministic
   grounding check exists — deliberately (fuzzy-matching rejection,
   §6 above). The compensating control is complete logging.
4. **Value-shape type inference has rough edges.** The bill-pay phone
   field became an `integer` input because the recorded value was digits;
   `+`-prefixed numbers fail pre-flight (§6c). A authoring-time type
   review would catch this class.
5. **`required_locator` / `allowed_error_messages` are manual authoring.**
   The recorder cannot infer what "the goal implies" from page content —
   that would be the heuristic detection §7a rejects. Both fields were
   added from live incidents and are set by hand where a capability needs
   them, like risk tags and outcome signatures.
6. **The page-identity signal is heading-based and result-blind.**
   ParaBank's loan page keeps the *form's* heading after submission, so
   URL+identity checkpoints matched before any decision rendered. The
   §6b retry + a decision-dependent `required_locator` close it per
   artifact; a general "wait for the interesting element" rule was
   considered and rejected as heuristic.
7. **Approvals are inferred, denials are observed.** The wealth loan
   provider declined every observed request, so the approval-primary
   checkpoint text was never observed live (documented in the artifact).
8. **Auto `is_constant` (§4c) exists but was never exercised live** — no
   run has needed it. The escape hatch costs nothing; it stays.
9. **Artifacts are keyed by `--name` with an overwrite guard missing.**
   A `--name` reuse overwrote the original transfer artifact with a
   different capability once. A refuse-if-goal-changes guard was scoped
   (and is on the pre-submission checklist) but not built.
10. **Recorder parameterization only sees executed actions.** A field the
    model skipped because the UI default matched never became a parameter
    (live: `from_account`, §2 above). Mitigated by prompt + manual repair;
    a "default-visible" signal in observations could make the recorder
    smarter, deliberately not built.
11. **Escalation notification is a log alert.** The `InterventionRequest`
    is complete and persisted; routing (email/Slack/whatever) is out of
    scope (§9a).
12. **§9d human-action capture is passive** — screenshots + observation
    snapshots + nav trail, no keystroke-level recording (§9d rationale).

## Evidence map (for the reviewer)

- `evidence/artifacts/` — three recorded capabilities:
  `parabank_login` (read_only), `parabank_pay_bills`
  (irreversible, confirmation-gated), `parabank_request_loan`
  (reversible, carries the live-captured loan-denial business-outcome
  signature).
- `evidence/discovery_run_*/` — real LLM runs, incl. the honest stuck run
  (`…162029`), the no-fabrication incident (`…175212`, kept as evidence),
  and the admin-page incident (`…160437`, kept as evidence).
- `evidence/replay_success_*/` — deterministic replays (bill payment with
  `--confirm`, `015916`; pay-bills `180816`).
- `evidence/replay_error_*/` — typed classifications: business outcome
  (`142953`), confirmation gate (`015915`), checkpoint mismatch /
  required-locator miss (swapped-dates `145939`).
- `pending_interventions/` — persisted intervention requests and
  completed handoff logs (`7d9a4f7c…human_log.json` etc.).
- Last verified full suite: **186/186** (`uv run pytest tests/`).

## Reproducing

```bash
docker compose up -d            # ParaBank on http://localhost:8080/parabank
pip install -e ".[dev]" && playwright install chromium
cp config/.env.example .env     # fill LLM_BASE_URL/LLM_API_KEY
uv run pytest tests/            # 186 tests, no live API calls
uv run python -m bankops discover --name <x> --goal "…" [--headed]
uv run python -m bankops replay --artifact evidence/artifacts/<x>.json \
    --param k=v … [--confirm] [--redact]
```
