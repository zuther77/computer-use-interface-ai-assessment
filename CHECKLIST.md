# CHECKLIST — Before Submitting

Status snapshot taken **Sep 17, 2026** after the escalation-wiring session.
Everything here is either a known gap, a tracked plan item, or a submission
mechanic. Work top-to-bottom; items marked ✅ are already done and verified.

---

## 1. Commit the working tree

- [ ] **Commit + push the uncommitted batch** — 13 modified files
      (`bankops/*` + `tests/*`: escalation wiring, checkpoint error rules +
      `required_locator`, UUID-safe locators, resolution fall-through, +9
      regression tests). Last commit is `39985f1` (step-feed batch).
- [ ] **Commit `IMPLEMENTATION_PLAN.md` and `DECISIONS.md`** — they are
      currently *untracked* and have never been committed.
- [ ] Verify no secrets in the diff: `.env` (real LLM API key) is
      git-ignored — confirm it stays out (`git log --all --full-history -p --
      .env` should be empty).

## 2. Phase 10 validation checklist (from IMPLEMENTATION_PLAN.md)

- [x] ✅ `/evidence/artifacts/` contains real saved artifacts — but the store
      currently holds **only `parabank_bill_pay.json`**. Repair (§3 below).
- [x] ✅ `/evidence/discovery_run_*/` contains real LLM runs (bill-pay
      `discovery_20260917_151936`, find-transactions `…145417`, savings+loan,
      plus the stuck/escalation runs).
- [x] ✅ `/evidence/replay_success_*/` shows clean deterministic replays
      (bill-pay replay validation still missing — §5 below).
- [x] ✅ `/evidence/replay_error_*/` shows correct failure classification
      (swapped-dates `replay_20260917_145939`: missing `required_locator`).
- [ ] **"No credentials or unmasked sensitive fields under `/evidence/`" is
      currently VIOLATED by choice** — redaction is OFF (`BANKOPS_REDACT=false`
      "for now"). Decide: re-enable redaction and re-run the key demo
      evidence, or state the deviation explicitly in REPORT.md (§ Safety).
      Passwords, account numbers and goals currently appear in evidence.

## 3. Repair the artifact store

- [ ] **Recover `parabank_check_transactions_all`** from the per-run copy in
      `evidence/discovery_run_discovery_20260917_145417/artifact_*.json`
      (the store copy was deleted) — then **re-author the goal-tied
      `required_locator`** (`a[href*='transaction.htm?id=']`) on the submit
      step + terminal checkpoint (the run-folder copy predates it). No live
      run needed; validate with one correct-dates replay (must SUCCEED) and
      one swapped-dates replay (must FAIL).
- [ ] **Re-record `parabank_transfer_funds`** with a live discovery run — the
      original was lost to the `--name` overwrite incident, and its
      checkpoints predate the page-identity upgrade (would not replay
      cleanly anyway). One `discover` run + one happy-path replay.

## 4. Tracked action items (DECISIONS.md — "do not lose these")

- [ ] **#2 — Manual risk tagging (§8b)** on *every* store artifact:
      - `parabank_bill_pay.json` → `state_changing_irreversible`,
        `point_of_no_return_step` = the "Send Payment" click step (verify
        the index in the artifact).
      - recovered `parabank_check_transactions_all` → `read_only` ✓ (correct
        by default, confirm explicitly).
      - re-recorded `parabank_transfer_funds` → `state_changing_reversible`.
      - note the manual-tagging limitation in REPORT.md (§ Safety/Cuts).
- [ ] **#1 — Deliberate non-happy-path capture (§7a)**: trigger one real
      native business outcome (e.g., transfer more than the balance →
      "insufficient funds"), capture its locator + text pattern, and
      **manually merge the signature** into the corresponding artifact step
      tagged `business_outcome`. Then demonstrate a live replay ending in
      `BusinessOutcomeResult` — currently that result type exists only in
      unit tests. This run doubles as additional `replay_error_*` evidence.

## 5. Replay validations still owed

- [ ] **`parabank_bill_pay` replay** (recorded but never replayed) — also
      live-exercises the ephemeral-UUID → `input[name="payee.phoneNumber"]`
      fall-through. Use `--param` for all 12 inputs; phone is typed as
      *integer* (digits only — a `+`-prefixed value fails pre-flight; §7).
- [ ] **Irreversible-capability demo**: replay bill-pay once with
      `confirm=True` (after risk tagging) and once without — the second must
      fail with `confirmation_required` (and must NOT escalate).

## 6. Escalation/handoff evidence

- [x] ✅ Live `INTERVENTION RAISED` demo exists:
      `pending_interventions/05831c8edf7b4e75909af113a838a691.json` (+ pause
      screenshot) from run `discovery_20260917_164515`.
- [ ] **The resume loop was never walked live** — the handoff ended when the
      browser window was closed (no `.resume` consumed, no `human_log.json`).
      Either re-run a short escalation demo and complete it
      (`echo mark_complete > pending_interventions/{id}.resume`) so
      `{id}.human_log.json` exists as evidence, or note the documented cut
      (CLI records decisions but doesn't resurrect the loop mid-run).
- [ ] Clean stale files from `pending_interventions/` (the demo request is
      evidence; anything else test-generated should go).

## 7. Documentation

- [ ] **`REPORT.md` — not started** (the plan's final deliverable). Sections
      1–6 from DECISIONS.md; **Section 7 (Cuts) last**, from what actually
      got simplified. Accumulated honest cuts to document:
      - CLI handoff records decisions but does not resume the loop/engine
        mid-run.
      - Phone recorded as *integer* input (value-shape type inference, §4c)
        — `+`-prefixed values fail pre-flight.
      - Redaction currently OFF by operator preference (see §2 above).
      - `required_locator` / `allowed_error_messages` are manual/deliberate
        authoring, not auto-inference (§4e/§7a philosophy).
      - Auto `is_constant` override exists but was never exercised live.
      - Stale artifacts recorded before a schema change must be re-recorded
        (happened twice with checkpoint fields).
      - The `--name` overwrite hazard (see §9).
- [ ] **README**: document the new escalation behavior (INTERVENTION RAISED,
      `.resume` file options) alongside discover/replay.
- [ ] **Docs match reality**: DECISIONS.md says "GLM-5.2"; the actual
      provider is `subconscious/glm-5.3-marathon` via the OpenAI SDK
      (`.env`). One reconciling line in REPORT.md (provider swap is a
      `base_url` change — §2d validated in practice).

## 8. Final verification pass

- [ ] Full test suite green: `uv run pytest tests/ -q`
      (last known: 177/177).
- [ ] Docker ParaBank reproducibility: `docker compose up -d` from a clean
      clone brings the app up; `pip install -e . && playwright install`
      documented in README.
- [ ] Walk the plan's Phase-10 checklist one final time against the final
      state of `/evidence/`.

## 9. Submission mechanics — the big gotchas

- [ ] **`/evidence/*` and `/pending_interventions/*` contents are
      GIT-IGNORED.** If you submit by pushing this repo, all evidence is
      EXCLUDED and the assignment's core deliverable is missing. Either
      `git add -f evidence/ pending_interventions/` (plus a commit), or
      zip the whole directory and submit the zip. **Verify the submitted
      artifact actually contains the evidence.**
- [ ] Strip/rotate the real LLM API key from anything submitted; keep
      `config/.env.example` as the stub and README instructions.
- [ ] Confirm the ParaBank container state doesn't matter for review:
      evidence must be self-contained (per-run folders with logs +
      screenshots + artifacts) so a reviewer needs no live bank.
- [ ] Optional hardening before the freeze: the `--name` overwrite guard
      (refuse to save over an existing artifact whose `goal` differs) — the
      incident that cost the original transfer-funds artifact.
- [ ] Tag the submission commit; note the last verified suite run in
      REPORT.md.
