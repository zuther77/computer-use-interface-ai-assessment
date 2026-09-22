# bankops — Computer-Use Automation System (ParaBank)

An agent that *learns* a web workflow by doing it once with an LLM, distills what it
did into a typed, replayable **artifact**, and then replays that workflow
**deterministically — with no LLM involved**. Built against a self-hosted
[ParaBank](https://hub.docker.com/r/parasoft/parabank) instance (a realistic
online-banking app) running in local Docker.

Design rationale for every choice lives in [`DECISIONS.md`](DECISIONS.md).

## What happens in a run

1. **Discovery** — an LLM agent (OpenAI-compatible tool-calling) observes ParaBank
   through Playwright. Each step it sees an *indexed list* of the page's interactive
   elements and may call exactly one typed action: `click`, `type_text`,
   `select_option`, `navigate`, `extract`, `remember`, `finish`, `report_stuck`.
   Every action is checked against a two-dimensional allowlist (domains/routes at
   `navigate`, action types at every call). The run stops with a *typed* reason:
   `finish`, `report_stuck`, max-steps, or no-progress.
2. **Distillation** — if (and only if) the run ends via `finish`, the successful
   path is distilled into an artifact JSON: ordered steps, priority-ordered
   candidate locators per step (with fallbacks), typed inputs/outputs, per-step
   checkpoints, and one terminal checkpoint. Corrections and abandoned branches are
   dropped; stuck runs never produce artifacts.
3. **Replay** — the artifact executes deterministically: pre-flight input
   validation, first-unique-match locator resolution with candidate fallbacks,
   tiered retries, and checkpoint verification at every step. The result is a
   typed `SuccessResult` / `BusinessOutcomeResult` / `FailureResult` — never an
   "assumed it worked".

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python ≥ 3.10 is provisioned inside the
  project's local `.venv` — nothing installs into a global Python)
- Docker
- An OpenAI-compatible LLM endpoint with reliable tool-calling — **needed for
  discovery only; replay never calls an LLM**

## Setup

```bash
# 1. Bring up local ParaBank
docker compose up -d

# First boot initializes the sample database lazily on the first request —
# give it ~1 minute, then verify:
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/parabank/index.htm   # -> 200

# 2. Install the package and a browser for Playwright (local .venv only)
uv sync                               # creates .venv; uv.lock pins exact versions
uv run playwright install chromium

# 3. Configure (keys live in .env — git-ignored, never in shell history)
cp config/.env.example .env
# then edit .env:
#   LLM_BASE_URL=...    your OpenAI-compatible endpoint
#   LLM_API_KEY=...     your API key
#   LLM_MODEL=...       any tool-calling model your endpoint serves
```

The CLI loads `.env` (repo root; `config/.env` also works) before anything else.
Every command below runs through `uv run`, which executes inside the project's
local `.venv` — your global Python is never touched.

### Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `PARABANK_BASE_URL` | `http://localhost:8080/parabank` | Target application |
| `LLM_BASE_URL` | *(unset)* | OpenAI-compatible endpoint (discovery only) |
| `LLM_API_KEY` | *(unset)* | API key |
| `LLM_MODEL` | `glm-5.2` | Model name your endpoint serves |
| `BANKOPS_MAX_STEPS` | `40` | Discovery max-step ceiling |

The safety allowlist is declarative and reviewable at `config/allowlist.yaml`:
`domains`/`routes` are checked at `navigate`, `action_types` at every tool call.

## Running without live services

The full test suite needs **no LLM and no ParaBank**:

```bash
uv run pytest   # 134 tests
```

The LLM is mocked everywhere; browser-facing tests run against small static HTML
fixtures under `tests/fixtures/`. The single test that touches live ParaBank
(`tests/test_environment.py::test_parabank_login_page_reachable`) **skips
automatically** when the container is down, so CI needs nothing external.

ParaBank itself is a local Docker container, so a full demo needs nothing external
except the LLM endpoint — and only for the discovery step.

## Demo path

### 1. Run the agent on a goal (real LLM)

ParaBank's sample database ships a verified demo user — **username `john`,
password `demo`**:

```bash
# NOTE: single quotes matter — "$25.00" in double quotes makes bash expand
# $25 as an empty positional parameter, and the agent would be told to
# transfer ".00".
uv run python -m bankops discover \
  --name parabank_transfer_funds \
  --goal 'Log in to ParaBank as user john with password demo, then transfer $25.00 from account 12345 to 12456, then log out.' --headed
```

- Add `--headed` to watch the browser window while the agent works.
- Exit code: `0` on `finish`, `1` on `stuck` / `max_steps` / `no_progress`.
- Every run leaves evidence under `evidence/discovery_run_<id>/`:
  `steps.jsonl` (one typed JSON line per step, sensitive values masked *on disk*),
  `summary.json`, `final.png`.
- On a `finish`-ended run, the distilled artifact is saved to
  `evidence/artifacts/parabank_transfer_funds.json`. Runs that end any other way
  produce evidence but never artifacts.

### 2. Replay the resulting artifact (no LLM)

```bash
uv run python -m bankops replay \
  --artifact evidence/artifacts/parabank_transfer_funds.json \
  --param username=john --param password=demo --param amount=1000
  --param from_account=12345 --param to_account=12456
```

Parameter names come from the artifact — check its declared inputs first:

```bash
uv run python -m json.tool < evidence/artifacts/parabank_transfer_funds.json | less   # see "inputs": [...]
```

Bad or missing parameters are rejected **pre-flight**, before the browser touches
anything. A successful replay prints its typed result and exits `0`; evidence lands
in `evidence/replay_success_<id>/`. Business outcomes and failures exit `1` and
land in `evidence/replay_error_<id>/` with the full expected/observed page state.

### 3. See the error taxonomy work

Replay with an input that should trigger a native ParaBank outcome — e.g. an
amount larger than the account balance. With no alternate-outcome signature
recorded for that step, the replay reports a typed `FAILURE` with expected vs.
observed state. Once a signature for that outcome is recorded and merged into the
artifact (DECISIONS.md §7a), the same situation classifies as
`BUSINESS OUTCOME` — a real business answer, not a technical crash. Both are filed
under `evidence/replay_error_<id>/`.

### 4. When a run can't proceed: escalation & handoff

`bankops.escalation` persists a structured `InterventionRequest` — goal or
artifact reference, step index, typed reason, observation snapshot, screenshot —
into `pending_interventions/<id>.json` the moment it is raised. A run can pause
on the *same* live browser session and wait for a human, who resolves it by
writing a resume-signal file:

```bash
echo verify       > pending_interventions/<id>.resume   # re-observe + checkpoint-verify
echo continue     > pending_interventions/<id>.resume   # resume without verification
echo mark_complete > pending_interventions/<id>.resume   # human finished the goal manually
echo abandon      > pending_interventions/<id>.resume   # terminate the run
```

A `verify` that doesn't match the expected checkpoint raises a *new* intervention
rather than guessing. For **discovery runs**, a `verify`/`continue` resume does
more than release the pause: the agent loop **continues from the human's state** —
it re-observes the page first (so whatever you fixed in the browser becomes its
next input), keeps its action history, and continues the evidence step numbering
(bounded to three pause/resume cycles per run). For **replay runs**, resume
records the decision and verifies the next checkpoint; a partial
mid-artifact resume is a documented cut (see `REPORT.md` §7). A complete
worked example (persistence, verification branch, all four override paths) is
in `tests/test_escalation.py`, and the full discovery resume cycle is covered
in `tests/test_runner.py::TestDiscoveryResumeAfterHandoff`.

## Evidence layout

| Path | Contents |
|---|---|
| `evidence/discovery_run_<id>/` | `steps.jsonl` (redacted), `summary.json`, `final.png`, artifact copy |
| `evidence/replay_success_<id>/` | Same shape for a successful replay |
| `evidence/replay_error_<id>/` | Business-outcome / failure replays (required deliverable) |
| `evidence/artifacts/` | Canonical artifact store |
| `pending_interventions/` | Live intervention requests + resume signal files (runtime state, git-ignored) |

## Safety notes

- **Allowlist**: domains/routes checked at `navigate`; action types checked at
  every tool invocation (`config/allowlist.yaml`).
- **Redaction**: values typed into sensitive-named fields (passwords, PINs,
  account numbers…) are masked in everything that is written to disk, while still
  being used live in-session.
- **Risk gate**: artifacts declare `risk_level` (set by a human at authoring
  time); irreversible artifacts name a point-of-no-return step, which replay will
  not execute without `--confirm`.

## Repository layout

```
bankops/
  artifact/     Pydantic artifact schema + recorder/distillation
  perception/   PerceptionAdapter interface + Playwright implementation
  actions/      Typed action tools + OpenAI tool schemas
  safety/       Allowlist, risk gate, redaction
  agent/        Discovery loop (LLM tool-calling, layered stopping conditions)
  replay/       Deterministic replay engine + typed result union
  escalation/   Intervention requests, pause/resume handoff, human-turn log
  evidence/     Redacted-first JSONL step logs + per-run folder conventions
  runner.py     Live-run pipeline glue
  cli.py        `python -m bankops` entry point (discover / replay)
tests/          Topic-named test suite (mocked LLM, static fixtures)
config/         Allowlist + environment templates
```

## Status

Phases 0–9 are implemented and test-covered (134 tests). The remaining work is
the live Phase 10 demonstration runs — a real discovery run, deterministic
replays, a deliberate non-happy-path capture, and (optionally) a forced
escalation — which populate `/evidence/` once `LLM_BASE_URL`/`LLM_API_KEY` are
configured.
