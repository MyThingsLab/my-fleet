#!/usr/bin/env python3
"""Run one full autonomous fleet cycle by chaining every tool's own CLI.

The step *order* is no longer hand-coded here: it comes from my-pipeline's
declarative graph (`mypipeline.plan.build_plan`), the fleet's one source of
tool-chaining truth. Each graph node names a `stage`; a resolver in this file
(RESOLVERS below) binds that stage to concrete argv at run time -- my-pipeline
owns *what runs and in what order*, my-fleet owns *how each stage binds and
whether we can afford to tick now*. The default graph reproduces the order this
docstring used to enumerate by hand:

  myplanner -> fleet_dispatch -> myresearcher -> mytester -> mychangelogger ->
  mydocs -> mydashboard -> myprojector -> myreporter -> mypipeline sync
  (fire ledger handoffs) -> mytelegrambot notify.

No tool calls another tool's CLI directly (each stays a separate `gh`-attributed
run, per their CLAUDE.md invariants) -- this script is the external driver that
chains them, the same role fleet_dispatch.py already plays for orchestrator+workers.

Defaults to a dry run (report only, no mutating subcommands). Pass --execute to
actually run myresearcher/mytester/mychangelogger/mydocs/mydashboard/myprojector/
myreporter/mypipeline/mytelegrambot for real; fleet_dispatch's own --execute is
passed through separately since it spawns billed headless sessions.

--loop keeps re-running the cycle instead of exiting after one pass, meant for
an always-on host (see the systemd unit alongside this file's PR). Each
iteration re-derives the usable account pool via account_usage.select_accounts
(polled on --account-recheck-min, not every iteration -- each poll is a real
`claude -p /usage` call per account) and skips step 2 for that iteration if none
are usable rather than stopping the loop. An iteration that dispatches nothing
backs off (doubling up to --max-backoff-min) before the next one; one that
dispatches something resets the backoff. --max-duration-min and
--max-cycle-budget-usd are optional caps for a bounded run (e.g. testing the
loop by hand); the systemd deployment omits both and just lets it run
indefinitely, relying on Restart=on-failure for crash recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mypipeline.plan import build_plan
from mythings.ledger import Ledger

import myfleet.account_usage as account_usage
import myfleet.fleet_ask as fleet_ask
from myfleet.cycle_driver import Stage, run_command
from myfleet.fleet_dispatch import DISPATCH_LEDGER, HALT_MARKER, _critical_halt_issues

# Climbs myfleet/<file>.py -> src -> my-fleet -> MyThingsLab/ (the fleet root).
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
ORG = "MyThingsLab"
TRACKING_REPO = f"{ORG}/my-things-core"
TRACKING_ISSUE = "1"
PROJECT_NUMBER = "1"
DOCS_SITE_CLONE = "mythingslab-site-genesis"

# The study repo is content, not a tool: myuni files topic issues there and
# myresearcher turns them into cited brief PRs. Like the my-<x> entries, `study`
# is a symlink to the sibling checkout (see .gitignore).
STUDY_REPO = f"{ORG}/study"
STUDY_ROOT = WORKSPACE_ROOT / "study"
RESEARCH_LABEL = "my-researcher"

# Repos that get a mytester/mychangelogger pass: every checkout with a
# pyproject.toml (shipped tools + the core SDK), discovered at runtime so a
# newly scaffolded tool joins the cycle without editing this list. Excludes
# my-template (a scaffold, not a real tool); non-Python repos have no
# pyproject.toml and never match.
EXCLUDED_REPOS = {"my-template"}


def tool_repos(root: Path) -> list[str]:
    return sorted(
        p.parent.name
        for p in root.glob("*/pyproject.toml")
        if p.parent.name not in EXCLUDED_REPOS
    )


def _run(cmd: list[str], *, check: bool = False, env: dict[str, str] | None = None) -> int:
    # Thin adapter over the shared driver's runner: fixes cwd to the workspace
    # root and keeps the (cmd, *, check, env) signature the cycle's steps call.
    return run_command(cmd, cwd=WORKSPACE_ROOT, env=env, check=check)


def _gh_json(argv: list[str]) -> list[dict] | None:
    result = subprocess.run(["gh", *argv], cwd=WORKSPACE_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"gh {' '.join(argv)} failed: {result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def _select_brief_issues(
    open_issues: list[int], open_pr_branches: list[str], count: int
) -> list[int]:
    # A brief PR (`Closes #N` on branch my-researcher/N) closes its issue on
    # merge, so an open issue with a pending brief PR is already briefed —
    # re-briefing it would burn an Engine call for nothing.
    pending = {
        branch.removeprefix(f"{RESEARCH_LABEL}/")
        for branch in open_pr_branches
        if branch.startswith(f"{RESEARCH_LABEL}/")
    }
    return [n for n in sorted(open_issues) if str(n) not in pending][:count]


def _brief_candidates(count: int) -> list[int] | None:
    issues = _gh_json([
        "issue", "list", "--repo", STUDY_REPO, "--label", RESEARCH_LABEL,
        "--state", "open", "--json", "number", "--limit", "200",
    ])
    prs = _gh_json([
        "pr", "list", "--repo", STUDY_REPO, "--state", "open",
        "--json", "headRefName", "--limit", "200",
    ])
    if issues is None or prs is None:
        return None
    return _select_brief_issues(
        [i["number"] for i in issues], [p["headRefName"] for p in prs], count
    )


def _cycle_halt_reason() -> str | None:
    # Mirror fleet_dispatch's two launch gates for the rest of the cycle. The
    # dispatch step already refuses on its own, but steps 3-10 make billed
    # Engine calls and mutate GitHub too, so a HALT marker / open critical
    # issue must stop them just the same -- otherwise "kill switch armed"
    # still spends money every iteration.
    if HALT_MARKER.exists():
        return (
            f"HALT marker present at {HALT_MARKER} "
            f"(clear with `python3 -m myfleet.fleet_dispatch --clear-halt`)"
        )
    critical = _critical_halt_issues(ORG)
    if critical:
        refs = ", ".join(
            f"{i['repository']['nameWithOwner']}#{i['number']}" for i in critical
        )
        return f"critical issue(s) open: {refs}"
    return None


@dataclass(frozen=True)
class _Ctx:
    args: argparse.Namespace
    accounts: str
    skip_dispatch: bool
    py: str


# One resolver per graph `stage`: binds the stage to zero or more concrete
# Stages at tick time (fan-out returns many; a missing input returns a skip).
# `mutating=False` runs even in a dry run (planner/dispatch/tester/projector
# always run, only their argv changes); `mutating=True` runs only under
# --execute. This is the runtime binding my-pipeline's graph deliberately does
# not hold -- accounts, per-repo fan-out, live gh queries, missing-clone guards.
def _stage_planner(ctx: _Ctx) -> list[Stage]:
    return [Stage("myplanner", [
        "myplanner", "plan",
        "--org", ORG,
        "--repo-root", str(WORKSPACE_ROOT),
        "--tracking-repo", TRACKING_REPO,
        "--tracking-issue", TRACKING_ISSUE,
        "--engine", ctx.args.engine,
    ], mutating=False)]


def _stage_dispatch(ctx: _Ctx) -> list[Stage]:
    if ctx.skip_dispatch:
        return []
    cmd = [ctx.py, "-m", "myfleet.fleet_dispatch", "--accounts", ctx.accounts]
    if ctx.args.dispatch_execute:
        cmd.append("--execute")
    if ctx.args.allow_personal_token:
        cmd.append("--allow-personal-token")
    return [Stage("fleet-dispatch", cmd, mutating=False)]


def _stage_researcher(ctx: _Ctx) -> list[Stage]:
    if ctx.args.brief_count <= 0:
        return []
    if not STUDY_ROOT.exists():
        return [Stage("myresearcher", [], skip=f"no study clone at {STUDY_ROOT}")]
    candidates = _brief_candidates(ctx.args.brief_count)
    if candidates is None:
        return [Stage("myresearcher", [], skip=f"could not query {STUDY_REPO}")]
    if not candidates:
        return [Stage(
            "myresearcher", [], skip=f"no open {RESEARCH_LABEL} issues in {STUDY_REPO} left to brief"
        )]
    # ClaudeCLIEngine needs an authenticated CLI; borrow the first fleet
    # account's CLAUDE_CONFIG_DIR (TAVILY_API_KEY is not set on this host, so
    # retrieval sticks to keyless arXiv).
    account = ctx.accounts.split(",")[0].strip()
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(Path(account).expanduser())}
    return [
        Stage("myresearcher", [
            "myresearcher", "brief",
            "--issue", str(number),
            "--repo", STUDY_REPO,
            "--repo-root", str(STUDY_ROOT),
            "--engine", ctx.args.engine,
            "--sources", "arxiv",
        ], mutating=True, env=env)
        for number in candidates
    ]


def _stage_tester(ctx: _Ctx) -> list[Stage]:
    stages = []
    for repo in tool_repos(WORKSPACE_ROOT):
        cmd = ["mytester", "run", "--source", str(WORKSPACE_ROOT / repo), "--engine", ctx.args.engine]
        if not ctx.args.execute:
            cmd.append("--local-only")
        stages.append(Stage("mytester", cmd, mutating=False))
    return stages


def _stage_changelogger(ctx: _Ctx) -> list[Stage]:
    return [
        Stage("mychangelogger", ["mychangelogger", "update", "--source", str(WORKSPACE_ROOT / repo)])
        for repo in tool_repos(WORKSPACE_ROOT)
    ]


def _stage_docs(ctx: _Ctx) -> list[Stage]:
    docs_site_root = WORKSPACE_ROOT / DOCS_SITE_CLONE
    if not docs_site_root.is_dir():
        return [Stage("mydocs", [], skip=f"no local docs-site clone at {docs_site_root}")]
    return [Stage("mydocs", [
        "mydocs", "sync", "--all",
        "--repo-root", str(docs_site_root),
        "--engine", ctx.args.engine,
    ])]


def _stage_dashboard(ctx: _Ctx) -> list[Stage]:
    docs_site_root = WORKSPACE_ROOT / DOCS_SITE_CLONE
    if not docs_site_root.is_dir():
        return [Stage("mydashboard", [], skip=f"no local docs-site clone at {docs_site_root}")]
    return [Stage("mydashboard", [
        "mydashboard", "render",
        "--repo-root", str(docs_site_root),
        "--workspace", str(WORKSPACE_ROOT),
        "--engine", ctx.args.engine,
    ])]


def _stage_projector(ctx: _Ctx) -> list[Stage]:
    cmd = [
        "myprojector", "sync",
        "--org", ORG,
        "--project-number", PROJECT_NUMBER,
        "--tracking-repo", TRACKING_REPO,
        "--tracking-issue", TRACKING_ISSUE,
        "--engine", ctx.args.engine,
    ]
    cmd.append("--apply-checklist" if ctx.args.execute else "--dry-run")
    return [Stage("myprojector", cmd, mutating=False)]


def _stage_reporter(ctx: _Ctx) -> list[Stage]:
    return [Stage("myreporter", [
        "myreporter", "post",
        "--repo", TRACKING_REPO,
        "--issue", TRACKING_ISSUE,
        "--repo-root", str(WORKSPACE_ROOT),
        "--summarize",
        "--engine", ctx.args.engine,
    ])]


def _stage_handoffs(ctx: _Ctx) -> list[Stage]:
    # Fire the graph's ledger->issue handoffs (the file-issue nodes) -- the
    # third chaining axis, now wired into the cycle it used to sit beside.
    return [Stage("mypipeline-sync", ["mypipeline", "sync", "--repo-root", str(WORKSPACE_ROOT), "--org", ORG])]


def _stage_telegram(ctx: _Ctx) -> list[Stage]:
    return [Stage("mytelegrambot", ["mytelegrambot", "notify"])]


RESOLVERS: dict[str, Callable[[_Ctx], list[Stage]]] = {
    "myplanner": _stage_planner,
    "fleet-dispatch": _stage_dispatch,
    "myresearcher": _stage_researcher,
    "mytester": _stage_tester,
    "mychangelogger": _stage_changelogger,
    "mydocs": _stage_docs,
    "mydashboard": _stage_dashboard,
    "myprojector": _stage_projector,
    "myreporter": _stage_reporter,
    "mypipeline-sync": _stage_handoffs,
    "mytelegrambot": _stage_telegram,
}


def _execute_stage(stage: Stage, *, execute: bool) -> None:
    # Same dry-run/skip semantics as cycle_driver.run_stage, but routed through
    # this module's `_run` so the workspace-root cwd (and the test seam) hold.
    if stage.skip is not None:
        print(f"(skipping {stage.name} — {stage.skip})")
        return
    if stage.mutating and not execute:
        print(f"(dry run — would run: {' '.join(stage.argv)})")
        return
    _run(stage.argv, env=stage.env)


def _run_cycle(args: argparse.Namespace, *, accounts: str, skip_dispatch: bool, py: str) -> None:
    # Only a run that spends or mutates is gated; a pure dry run reports as
    # usual (matching fleet_dispatch, whose dry run also proceeds with a note).
    if args.execute or args.dispatch_execute:
        reason = _cycle_halt_reason()
        if reason is not None:
            print(f"(cycle halted — {reason})")
            return

    ctx = _Ctx(args=args, accounts=accounts, skip_dispatch=skip_dispatch, py=py)
    for item in build_plan():
        resolver = RESOLVERS.get(item.stage)
        if resolver is None:
            print(f"(no resolver for graph stage {item.stage!r} — skipping)")
            continue
        for stage in resolver(ctx):
            _execute_stage(stage, execute=args.execute)


def _loop_should_stop(
    *, elapsed_min: float, spent_usd: float, max_duration_min: float | None, max_cycle_budget_usd: float | None,
) -> str | None:
    """Pure breakout check, split out from _run_loop so it's testable without
    a fake clock: returns the reason to stop, or None to keep looping."""
    if max_duration_min is not None and elapsed_min >= max_duration_min:
        return f"reached --max-duration-min {max_duration_min} ({elapsed_min:.1f} min elapsed)"
    if max_cycle_budget_usd is not None and spent_usd >= max_cycle_budget_usd:
        return f"reached --max-cycle-budget-usd {max_cycle_budget_usd} (${spent_usd:.2f} spent)"
    return None


def _next_backoff_s(current_backoff_s: float, *, dispatched: bool, idle_backoff_s: float, max_backoff_s: float) -> float:
    if dispatched:
        return idle_backoff_s
    return min(current_backoff_s * 2.0, max_backoff_s)


def _refresh_ask_channel(ledger: Ledger, *, timeout: int) -> None:
    # The daemon-liveness preflight in fleet_ask.enable() runs once, when the
    # channel is armed at startup. In --loop that isn't enough: a daemon that
    # dies on day 2 turns every ASK into a full-timeout block followed by a
    # DENY -- slower than no channel and just as closed, for the rest of the
    # run, with nothing in the output to say why. Re-check the process table
    # each iteration; disarm loudly when the daemon is gone (ASKs then deny
    # fast, exactly the pre-channel behavior) and re-arm when it comes back.
    armed = bool(os.environ.get("MYTHINGS_ASK_CMD"))
    alive = fleet_ask.daemon_is_running()
    if armed and not alive:
        os.environ.pop("MYTHINGS_ASK_CMD", None)
        os.environ.pop("MYTHINGS_ASK_TIMEOUT", None)
        print(
            "(ask daemon is gone — channel disarmed: ASKs now deny fast instead of "
            "blocking the full timeout; restart `mytelegrambot run` to re-arm)"
        )
        ledger.record(
            tool="fleet_cycle",
            kind="ask_channel",
            outcome="disarmed",
            detail="mytelegrambot run daemon no longer running; ask channel disarmed",
        )
    elif not armed and alive:
        try:
            fleet_ask.enable(timeout=timeout)
        except fleet_ask.AskChannelUnavailable as exc:
            print(f"(ask daemon is back but the channel is still unavailable: {exc})")
            return
        print("(ask daemon is back — channel re-armed)")
        ledger.record(
            tool="fleet_cycle",
            kind="ask_channel",
            outcome="rearmed",
            detail="mytelegrambot run daemon back; ask channel re-armed",
        )


def _run_loop(args: argparse.Namespace, py: str) -> int:
    pool = [a.strip() for a in args.accounts.split(",") if a.strip()]
    dispatch_ledger = Ledger(DISPATCH_LEDGER)
    start_usage_count = len(dispatch_ledger.read(tool="fleet_dispatch", kind="usage"))
    loop_start = time.monotonic()
    idle_backoff_s = args.idle_backoff_min * 60.0
    backoff_s = idle_backoff_s
    last_account_check: float | None = None
    usable_accounts: list[str] = []
    iteration = 0

    while True:
        elapsed_min = (time.monotonic() - loop_start) / 60.0
        spent_usd = sum(
            e.data.get("cost_usd", 0.0)
            for e in dispatch_ledger.read(tool="fleet_dispatch", kind="usage")[start_usage_count:]
        )
        stop_reason = _loop_should_stop(
            elapsed_min=elapsed_min, spent_usd=spent_usd,
            max_duration_min=args.max_duration_min, max_cycle_budget_usd=args.max_cycle_budget_usd,
        )
        if stop_reason is not None:
            print(f"(--loop stopping: {stop_reason})")
            return 0

        # account_usage.select_accounts spends one real `claude -p /usage` call
        # per account, so this is polled on a cadence, not every iteration.
        now = time.monotonic()
        if last_account_check is None or (now - last_account_check) >= args.account_recheck_min * 60.0:
            usable, over = account_usage.select_accounts(pool, args.max_session_pct)
            usable_accounts = [u.config_dir for u in usable]
            last_account_check = now
            if over:
                print(f"(accounts over {args.max_session_pct}%: {[u.config_dir for u in over]})")

        # A remote daemon shares only the ledger, not this process table, so
        # its liveness can't be probed from here -- the startup preflight is
        # skipped for it too (--ask-remote-daemon).
        if args.ask_human and not args.ask_remote_daemon:
            _refresh_ask_channel(dispatch_ledger, timeout=args.ask_timeout)

        iteration += 1
        print(f"\n=== loop iteration {iteration} ({elapsed_min:.1f} min elapsed, ${spent_usd:.2f} spent) ===")
        skip_dispatch = args.skip_dispatch or not usable_accounts
        if not usable_accounts:
            print("(no usable accounts this iteration — skipping dispatch, not stopping the loop)")

        entries_before = len(dispatch_ledger.read(tool="fleet_dispatch"))
        cycle_accounts = ",".join(usable_accounts) if usable_accounts else args.accounts
        _run_cycle(args, accounts=cycle_accounts, skip_dispatch=skip_dispatch, py=py)
        dispatched = len(dispatch_ledger.read(tool="fleet_dispatch")) > entries_before

        backoff_s = _next_backoff_s(
            backoff_s, dispatched=dispatched, idle_backoff_s=idle_backoff_s,
            max_backoff_s=args.max_backoff_min * 60.0,
        )
        if not dispatched:
            print(f"(nothing dispatched this iteration — backing off {backoff_s:.0f}s)")
            time.sleep(backoff_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--accounts", required=True, help="passed through to fleet_dispatch.py --accounts (--loop: the candidate pool, re-filtered each recheck)")
    parser.add_argument("--execute", action="store_true", help="run mutating subcommands for real (all steps)")
    parser.add_argument("--dispatch-execute", action="store_true", help="also let fleet_dispatch spawn real headless sessions (separate from --execute since it's billed)")
    parser.add_argument("--engine", choices=["noop", "claude-cli"], default="noop", help="Engine backend for planner/tester/projector/reporter")
    parser.add_argument("--skip-dispatch", action="store_true", help="skip step 2 (fleet_dispatch); --loop: applies to every iteration")
    parser.add_argument(
        "--allow-personal-token",
        action="store_true",
        help="forwarded to fleet_dispatch.py: explicitly accept spawning workers "
        "on the ambient personal gh token instead of the GitHub App (see "
        "fleet_dispatch.py --help for why this is opt-in)",
    )
    parser.add_argument(
        "--ask-human",
        action="store_true",
        help=(
            "arm the ASK escalation: a Policy ASK becomes a real Allow/Deny prompt in "
            "Telegram instead of collapsing to DENY. Needs a running `mytelegrambot run` "
            "daemon, and refuses to start without one -- a channel nobody answers is "
            "slower than no channel and just as closed."
        ),
    )
    parser.add_argument(
        "--ask-remote-daemon",
        action="store_true",
        help="skip the local daemon check: it runs on another host sharing this ledger",
    )
    parser.add_argument(
        "--ask-timeout",
        type=int,
        default=fleet_ask.DEFAULT_ASK_TIMEOUT,
        help="seconds to wait for the human to tap Allow/Deny (default: %(default)s)",
    )
    parser.add_argument("--brief-count", type=int, default=1, help="max open my-researcher topic issues in MyThingsLab/study to brief per cycle (one billed Engine call each with --engine claude-cli; 0 disables the step)")
    parser.add_argument("--loop", action="store_true", help="keep cycling instead of running once (see module docstring)")
    parser.add_argument("--max-duration-min", type=float, default=None, help="--loop only: stop after this many wall-clock minutes (default: run indefinitely)")
    parser.add_argument("--max-cycle-budget-usd", type=float, default=None, help="--loop only: stop once fleet_dispatch's aggregate cost_usd since the loop started reaches this cap (default: no cap)")
    parser.add_argument("--account-recheck-min", type=float, default=10.0, help="--loop only: how often to re-poll account_usage.select_accounts")
    parser.add_argument("--max-session-pct", type=int, default=90, help="--loop only: per-account session-usage ceiling passed to account_usage.select_accounts")
    parser.add_argument("--idle-backoff-min", type=float, default=1.0, help="--loop only: backoff between iterations that dispatched nothing")
    parser.add_argument("--max-backoff-min", type=float, default=30.0, help="--loop only: backoff ceiling")
    args = parser.parse_args(argv)

    if args.ask_human:
        # Arms it for this process, and so for every tool CLI and headless worker
        # spawned below -- they all inherit the environment. Without this the
        # fleet's ASKs are still silently denied.
        try:
            wiring = fleet_ask.enable(
                timeout=args.ask_timeout, remote_daemon=args.ask_remote_daemon
            )
        except fleet_ask.AskChannelUnavailable as exc:
            print(f"fleet_cycle: cannot arm the ask channel: {exc}", file=sys.stderr)
            return 2
        print(f"ask channel armed -> {wiring['MYTHINGS_ASK_CMD']}")

    py = sys.executable

    if args.loop:
        return _run_loop(args, py)

    _run_cycle(args, accounts=args.accounts, skip_dispatch=args.skip_dispatch, py=py)

    if not args.execute:
        print("\n(dry run — pass --execute to run myresearcher/mytester/mychangelogger/mydocs/mydashboard/myprojector/myreporter/mytelegrambot for real; --dispatch-execute for fleet_dispatch's billed sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
