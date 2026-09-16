"""Command-line interface: ``python -m bankops discover|replay``.

Loads ./.env (or config/.env) before importing bankops settings so that
credentials come from the environment — never from argv or shell history
(DECISIONS.md §8c).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _load_env_file() -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments; never overrides
    variables already present in the environment."""
    for candidate in (Path(".env"), Path("config/.env")):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.lower().startswith("export "):
                    key = key[7:].strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
            return


def _parse_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"bad --param {pair!r} (expected KEY=VALUE)")
        key, _, value = pair.partition("=")
        params[key.strip()] = value
    return params


def _print_replay_result(result) -> int:
    from bankops.replay.results import (
        BusinessOutcomeResult,
        FailureResult,
        SuccessResult,
    )

    if isinstance(result, SuccessResult):
        print(f"[replay] SUCCESS — {result.steps_executed} steps executed")
        for key, value in result.outputs.items():
            print(f"[replay]   {key} = {value}")
        return 0
    if isinstance(result, BusinessOutcomeResult):
        print(
            f"[replay] BUSINESS OUTCOME at step {result.step_index} "
            f"({result.classification.value})"
        )
        print(f"[replay]   matched: {result.matched_text!r}")
        return 1
    print(f"[replay] FAILURE at step {result.failed_step}: {result.reason}")
    print(f"[replay]   expected: {result.expected}")
    print(f"[replay]   observed: {result.observed}")
    return 1


def main(argv: list[str] | None = None) -> int:
    _load_env_file()
    parser = argparse.ArgumentParser(
        prog="bankops",
        description=(
            "LLM-driven workflow discovery and deterministic replay "
            "against a self-hosted ParaBank instance."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser(
        "discover", help="run a live LLM discovery run and distill an artifact"
    )
    discover.add_argument("--goal", required=True, help="natural-language goal")
    discover.add_argument(
        "--name", required=True, help="artifact name (saved on a finish-ended run)"
    )
    discover.add_argument("--max-steps", type=int, default=None)
    discover.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window while the agent works",
    )
    discover.add_argument("--run-id", default=None)
    discover.add_argument(
        "--redact",
        action="store_true",
        help="mask credentials/sensitive values in the feed and evidence (§8c)",
    )

    replay = sub.add_parser(
        "replay", help="deterministically replay a saved artifact (no LLM)"
    )
    replay.add_argument("--artifact", required=True, help="path to artifact JSON")
    replay.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="caller-supplied input value (repeatable)",
    )
    replay.add_argument(
        "--confirm",
        action="store_true",
        help="authorize the point-of-no-return step of irreversible artifacts",
    )
    replay.add_argument(
        "--headed", action="store_true", help="show the browser window"
    )
    replay.add_argument("--run-id", default=None)
    replay.add_argument(
        "--redact",
        action="store_true",
        help="mask credentials/sensitive values in the evidence (§8c)",
    )

    args = parser.parse_args(argv)

    # Imported only after .env loading so settings see the values.
    from playwright.sync_api import sync_playwright

    from bankops import settings
    from bankops.agent.loop import OpenAIToolCallingClient
    from bankops.artifact.models import Artifact
    from bankops.evidence.paths import artifacts_dir
    from bankops.perception.playwright_adapter import PlaywrightPerceptionAdapter
    from bankops.runner import _new_run_id, run_discovery, run_replay

    if args.redact:
        settings.REDACT = True
    if not settings.REDACT:
        print(
            "[bankops] evidence redaction is OFF (operator preference, "
            "'for now') — real values (credentials, account numbers) WILL "
            "appear in this terminal and under /evidence/. "
            "Re-enable with --redact or BANKOPS_REDACT=true.",
            flush=True,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        try:
            page = browser.new_page()
            page.goto(f"{settings.PARABANK_BASE_URL}/index.htm", timeout=30_000)
            adapter = PlaywrightPerceptionAdapter(page)

            if args.command == "discover":
                run_id = args.run_id or _new_run_id("discovery")

                def _report_progress(event) -> None:
                    """Live operator feed: every action, its target, its
                    result — plus the model's own one-line rationale
                    (masked when redaction is enabled, raw when disabled)."""
                    line = f"[{event.step:>2}] {event.action}"
                    if event.target:
                        line += f" → {event.target}"
                    line += f"  ({event.status})"
                    if event.page:
                        line += f"  ·  {event.page}"
                    print(line, flush=True)
                    if event.reasoning:
                        print(f"        ↳ {event.reasoning}", flush=True)
                    if event.status == "error" and event.message:
                        print(f"        ! {event.message[:140]}", flush=True)

                print(f"[discover] run {run_id}: {args.goal}", flush=True)
                try:
                    result, artifact = run_discovery(
                        args.goal,
                        artifact_name=args.name,
                        adapter=adapter,
                        client=OpenAIToolCallingClient(),
                        run_id=run_id,
                        max_steps=args.max_steps,
                        progress=_report_progress,
                    )
                except Exception as exc:
                    print(
                        f"[discover] run {run_id}: CRASHED — "
                        f"{type(exc).__name__}: {exc}"
                    )
                    print(
                        "[discover] evidence (incl. summary.json): "
                        f"evidence/discovery_run_{run_id}/"
                    )
                    return 1
                print(
                    f"[discover] run {run_id}: {result.status.value} — {result.reason}"
                )
                print(f"[discover] evidence: evidence/discovery_run_{run_id}/")
                if artifact is not None:
                    print(
                        "[discover] artifact saved: "
                        f"{artifacts_dir() / (args.name + '.json')}"
                    )
                else:
                    print(
                        "[discover] no artifact saved "
                        "(runs only distill when they end via finish)"
                    )
                return 0 if result.status.value == "completed" else 1

            artifact = Artifact.load(args.artifact)
            run_id = args.run_id or _new_run_id("replay")
            try:
                result = run_replay(
                    artifact,
                    _parse_params(args.param),
                    adapter=adapter,
                    confirm=args.confirm,
                    run_id=run_id,
                )
            except Exception as exc:
                print(
                    f"[replay] run {run_id}: CRASHED — "
                    f"{type(exc).__name__}: {exc}"
                )
                print(
                    "[replay] evidence (incl. summary.json): "
                    f"evidence/replay_success_{run_id}/"
                )
                return 1
            code = _print_replay_result(result)
            folder = "replay_error" if result.status != "success" else "replay_success"
            print(f"[replay] evidence: evidence/{folder}_{run_id}/")
            return code
        finally:
            browser.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
