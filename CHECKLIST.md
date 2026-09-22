# CHECKLIST — Before Submitting

Status verified against the actual repo state on **Sep 21, 2026**
(each ✅ carries what was verified; open items carry what's still missing).

---

## 1. Commit the working tree

- [x] ✅ **Committed + pushed** — three batches live on `origin/main`:
      `39985f1` (step feed / reasoning / observe tool), `3ec93a2`
      (escalation wiring, checkpoint hardening, UUID-safe locators),
      `1b49161` (discovery resume, admin-route deny, no-fabrication
      prompt). `DECISIONS.md` committed in `3ec93a2`.
- [x] ✅ **`IMPLEMENTATION_PLAN.md` intentionally untracked** — excluded
      by operator decision; do not add.
- [x] ✅ **No secrets committed** — verified Sep 21: `git log --all --
      .env` is empty (never committed); the only `LLM_API_KEY` references
      in tracked files are placeholders in `README.md` /
      `config/.env.example`. The real key lives only in the git-ignored
      `.env`.

## 2. Phase 10 validation checklist (from IMPLEMENTATION_PLAN.md)

- [x] ✅ `/evidence/artifacts/` — current store: `parabank_login`,
      `parabank_pay_bills`, `parabank_request_loan` (re-recorded fresh
      Sep 21; the earlier store was superseded — see §3).
- [x] ✅ `/evidence/discovery_run_*/` — real LLM runs throughout Sep 17–21
      (bill-pay, pay-bills, loan, plus the stuck/escalation runs).
- [x] ✅ `/evidence/replay_success_*/` — clean deterministic replays
      (incl. `parabank_pay_bills` replay success Sep 21 18:08).
- [x] ✅ `/evidence/replay_error_*/` — correct failure classifications
      (swapped-dates `required_locator` miss; `parabank_pay_bills`
      checkpoint_mismatch Sep 21 18:07 → fixed → success at 18:08).
- [ ] **"No credentials or unmasked sensitive fields under `/evidence/`"
      is still VIOLATED by choice** — `BANKOPS_REDACT=false`. Re-run the
      key demo evidence with redaction, or state the deviation explicitly
      in REPORT.md (§ Safety).

## 3. Artifact store repair — SUPERSEDED

- [x] ✅ The original repair plan (recover `check_transactions_all`,
      re-record `transfer_funds`) is **superseded**: the store was
      deliberately rebuilt with fresh Sep-21 recordings
      (`login` / `pay_bills` / `request_loan`). What still applies from
      this section is folded into §4 (risk tags for the *current* set).

## 4. Tracked action items (DECISIONS.md — "do not lose these")

- [x] ✅ **#2 — Manual risk tagging (§8b)** — done Sep 22, verified against
      the live steps first: `parabank_pay_bills` →
      `state_changing_irreversible`, `point_of_no_return_step=13` (the
      real "Send Payment" button, confirmed by its role locator before
      setting); `parabank_request_loan` → `state_changing_reversible`
      (submits a request, no funds move at submit); `parabank_login` →
      `read_only`. Still carry the manual-tagging limitation note to
      REPORT.md.
- [x] ✅ **#1 — Deliberate non-happy-path capture (§7a)** — done Sep 22:
      ParaBank's native loan denial triggered live on the recorded loan
      artifact's submit step; the signature (`#requestLoanResult`,
      "Denied", `business_outcome`) was deliberately captured and merged
      into the artifact; replay reproduces `BUSINESS OUTCOME at step 7`
      with the matched denial text (evidence
      `replay_error_replay_20260922_142953/`, correctly NOT escalated).
      The primary checkpoint uses the live-observed marker div
      `#loanRequestApproved:visible` — ParaBank keeps a *hidden* approval
      template ("Congratulations, your loan has been approved.") in the
      DOM on every result page, so text-based `:has-text()` selectors
      match denials too (first attempt's live-observed failure; carried
      to REPORT.md §4e notes). Honest note: the recorded capability's own
      outcome in this environment is a denial (the wealth provider
      declined every observed request, including the original
      recording) — an approval is unproducible, so the approval-status
      primary is inferred, not observed.

## 5. Replay validations

- [x] ✅ **`parabank_pay_bills` replay-validated** — a failure run
      (checkpoint_mismatch, Sep 21 18:07) followed by a clean success
      replay (18:08) exists in evidence.
- [x] ✅ **Irreversible-capability demo** (Sep 22): replay without
      `--confirm` refused exactly at the point of no return —
      `FAILURE at step 13: confirmation_required` (evidence
      `replay_error_replay_20260922_015915/`, correctly NOT escalated —
      nothing to hand off); with `--confirm` the payment executed —
      `SUCCESS — 14 steps` (`replay_success_replay_20260922_015916/`).

## 6. Escalation/handoff evidence

- [x] ✅ Live `INTERVENTION RAISED` demo:
      `pending_interventions/05831c8edf7b4e75909af113a838a691.json` from
      run `discovery_20260917_164515`.
- [x] ✅ A live handoff was completed Sep 21 (run `…162029`, request
      `7d9a4f7c…`): human logged in during the pause, `verify` consumed,
      `human_log.json` written with before/after captures + nav trail.
- [x] ✅ Discovery resume is implemented (§9c, commit `1b49161`): a
      `RESUME` signal continues the loop from the human's state.
- [ ] **Replay partial-resume remains a documented cut** — carry to
      REPORT.md §7 (the replay handoff records the decision but does not
      resume the engine mid-run).

## 7. Documentation

- [ ] **`REPORT.md` — not started** (the plan's final deliverable). Sections
      1–6 from DECISIONS.md; **Section 7 (Cuts) last**, from what actually
      got simplified. Accumulated honest cuts to document:
      - Replay handoff records decisions but does not resume the engine
        mid-run (discovery resume IS implemented, §9c).
      - Phone recorded as *integer* input (value-shape type inference, §4c)
        — `+`-prefixed values fail pre-flight.
      - Redaction currently OFF by operator preference (see §2 above).
      - `required_locator` / `allowed_error_messages` are manual/deliberate
        authoring, not auto-inference (§4e/§7a philosophy).
      - Stale artifacts recorded before a schema change must be re-recorded
        (happened twice with checkpoint fields).
      - The `--name` overwrite hazard (see §9).
      - The no-fabrication rule is a prompt constraint, not a deterministic
        gate (grounding checks would be the fuzzy matching §7a rejects).
- [ ] **README**: escalation is documented; add the new §9c resume
      semantics (a `RESUME` signal now continues the discovery loop from
      the human's state). Your uncommitted README tweak (goal wording) is
      still unstaged.
- [ ] **Docs match reality**: DECISIONS.md says "GLM-5.2"; the actual
      provider is `subconscious/glm-5.3-marathon` via the OpenAI SDK.
      One reconciling line in REPORT.md (provider swap is a
      `base_url` change — §2d validated in practice).

## 8. Final verification pass

- [x] ✅ Full test suite green: **185/185** (verified Sep 21, commit
      `1b49161`).
- [ ] Docker reproducibility from a clean clone: `docker compose up -d` +
      `pip install -e . && playwright install` — one fresh-clone smoke
      test before submission.
- [ ] Re-walk this checklist once more after §4/§7 land.

## 9. Submission mechanics — the big gotchas

- [ ] **`/evidence/*` and `/pending_interventions/*` contents are
      GIT-IGNORED.** Pushing the repo submits WITHOUT the evidence. Either
      `git add -f evidence/ pending_interventions/` (plus a commit), or
      zip the whole directory. **Verify the submitted artifact actually
      contains the evidence.**
- [x] ✅ Real LLM API key never committed (verified §1); rotate it before
      submission anyway since it sat in `.env` through many runs.
- [ ] Optional hardening before the freeze: the `--name` overwrite guard
      (refuse to save over an existing artifact whose `goal` differs) —
      the incident that cost the original transfer-funds artifact.
- [ ] Tag the submission commit; note the last verified suite run in
      REPORT.md.

---

**Remaining, condensed:** redaction decision (§2) → REPORT.md (§7) → README
resume note → force-add evidence → fresh-clone smoke test → tag the
submission commit.
