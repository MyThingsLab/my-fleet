#!/usr/bin/env python3
"""Fan out ready fleet work across multiple Claude Code accounts.

Reuses myorchestrator's ranking (myorchestrator next --count N) to pick one
distinct candidate per available worker, then runs each as a `mycoder build`
call under a different CLAUDE_CONFIG_DIR — so two subscriptions can work the
fleet concurrently without touching each other's files. my-coder owns the
worker role end to end (its own Workspace worktree, branch naming/resume,
prompt, the single push + draft-PR side effect); this module picks which
candidate to run and translates the result into the resume/recover outcomes
below.

Only "issue" candidates are dispatchable today; "scaffold" candidates (a
not-yet-built tool) need MyScaffolder, which doesn't exist yet, so they're
reported and skipped.

Attempts never restart from scratch. Every terminal outcome is durable (the
branch is pushed even on failure, the transcript + a per-issue ledger entry
persist), so when an issue comes back around a later attempt reads what the
prior one did, resumes its branch, and moves forward. If an issue is blocked by
a missing capability in another tool's repo the worker files that as an issue
there and the issue is paused (not failed) until the blocker closes; after
MAX_ATTEMPTS unresolved tries it's handed to a human.

Each run ends at "draft PR opened", promoted to ready-for-review only once its
tests-passed signal and CI both check out — never pushes to main, never merges.
Defaults to --dry-run; pass --execute to actually spawn the headless sessions.

Kill switch: `--abort` touches a HALT marker (.fleet-dispatch/HALT) and exits;
every subsequent --execute run refuses to launch anything until `--clear-halt`
removes it. See README.md's "Kill switch" section for the one-line runbook.

Every headless session is bounded three ways: --max-budget-usd (spend),
--max-turns (turn count), and --session-timeout-s (wall-clock time, in case a
single turn stalls rather than exhausting its turn/budget cap). A timeout is
recorded as a "deferred" outcome -- resumable, not counted toward MAX_ATTEMPTS.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from myorchestrator.candidates import Candidate
from myorchestrator.manifest import default_manifest_path
from myorchestrator.orchestrator import Orchestrator, Recommendation
from mythings.github import app_installation_org, github_app_token
from mythings.ledger import Ledger

import myfleet.fleet_ask as fleet_ask

# Climbs myfleet/<file>.py -> src -> my-fleet -> MyThingsLab/ (the fleet root).
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DISPATCH_LEDGER = WORKSPACE_ROOT / ".fleet-dispatch" / "ledger.jsonl"
TRANSCRIPTS_DIR = WORKSPACE_ROOT / ".fleet-dispatch" / "transcripts"
# The kill switch: a marker file, not a signal or a flag a running process has
# to poll mid-loop. `--execute` checks for it before launching anything and
# refuses outright if it's there, so arming it (`--abort`) always beats a run
# that starts after it -- no race between "halt" and "launch". It doesn't
# reach into an already-running headless session (those are already bounded by
# --max-budget-usd/--max-turns and end on their own); it stops the *next* one.
HALT_MARKER = WORKSPACE_ROOT / ".fleet-dispatch" / "HALT"

# The spend alert's "Raise cap" button (mytelegrambot's spend_command) shells
# back into `--raise-daily-cap AMOUNT`; this is where that lands. Day-scoped
# like the spend it overrides, so a raise from a busy day doesn't silently
# persist into the next one -- an operator who wants a permanently higher
# ceiling should pass --max-daily-usd instead.
DAILY_CAP_OVERRIDE = WORKSPACE_ROOT / ".fleet-dispatch" / "daily-cap-override.json"

def _utc_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _today_spend_usd(ledger: Ledger, *, today: str | None = None) -> float:
    # UTC calendar day, matching _utc_ts/_utc_now elsewhere in this file — a
    # daily cap that drifted with the host's local timezone would reset at an
    # unpredictable wall-clock hour.
    day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    return sum(
        float(e.data.get("cost_usd", 0.0))
        for e in ledger.read(tool="fleet_dispatch", kind="usage")
        if e.ts.startswith(day)
    )


def _effective_daily_cap(default_cap: float, *, today: str | None = None) -> float:
    # Reads the override --raise-daily-cap writes. Missing, unparsable, or
    # scoped to a different UTC day all fall back to the configured default --
    # the override is meant to widen today's ceiling, never to persist past it.
    if not DAILY_CAP_OVERRIDE.exists():
        return default_cap
    try:
        data = json.loads(DAILY_CAP_OVERRIDE.read_text())
    except (json.JSONDecodeError, OSError):
        return default_cap
    day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if data.get("day") != day:
        return default_cap
    return max(default_cap, float(data.get("cap_usd", default_cap)))


def _spend_alert_sent_today(ledger: Ledger, *, today: str | None = None) -> bool:
    # One push per day is the point: --loop re-evaluates the cap every
    # iteration, and a threshold crossed once stays crossed for the rest of
    # the day -- re-alerting every iteration would just be spam the operator
    # learns to ignore. A push that failed to reach Telegram doesn't count as
    # sent, so the next iteration retries it.
    day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    return any(
        e.ts.startswith(day) and e.outcome == "success"
        for e in ledger.read(tool="fleet_dispatch", kind="spend_alert")
    )


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path


def _parse_accounts(raw: str) -> list[Account]:
    accounts = []
    for i, entry in enumerate(raw.split(",")):
        entry = entry.strip()
        if not entry:
            continue
        accounts.append(Account(name=f"account{i + 1}", config_dir=Path(entry).expanduser()))
    return accounts


def _account_uuid(config_dir: Path) -> str | None:
    # The account a config dir is logged into is recorded by `claude auth login`
    # in .claude.json under oauthAccount. Read-only; no token is touched.
    try:
        data = json.loads((config_dir / ".claude.json").read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return (data.get("oauthAccount") or {}).get("accountUuid") or None


def _preflight_distinct_accounts(accounts: list[Account]) -> list[str]:
    # The whole premise is that each config dir is a *different* Claude account,
    # so the workers don't share one session window / usage quota. Two dirs that
    # resolve to the same accountUuid silently void that -- both drain the one
    # account, "concurrency" buys nothing, and one hits its limit twice as fast.
    # This is invisible without checking (they're separate directories with
    # separate tokens), so verify identity before spending anything.
    problems: list[str] = []
    seen: dict[str, str] = {}  # accountUuid -> the first account name that had it
    for account in accounts:
        uuid = _account_uuid(account.config_dir)
        if uuid is None:
            problems.append(
                f"{account.name} ({account.config_dir}): can't read an account identity "
                f"from .claude.json — is it `claude auth login`'d?"
            )
            continue
        if uuid in seen:
            problems.append(
                f"{account.name} ({account.config_dir}) is the SAME Claude account as "
                f"{seen[uuid]} (accountUuid {uuid[:8]}…) — they would share one session "
                f"and quota. Re-auth one to a different account: "
                f"`CLAUDE_CONFIG_DIR={account.config_dir} claude auth login`."
            )
        else:
            seen[uuid] = account.name
    return problems



# --- PR merge-readiness gate -----------------------------------------------
#
# A pushed draft PR is promoted to "ready for review" only when my-coder itself
# reported tests passing AND its CI actually goes green. Everything short of
# that stays a draft and reports "needs_review" (a resumable outcome), so
# "success" always means a human can pick the PR up to merge. Never merges --
# the human always does that.


def _checks_state(org: str, repo: str, number: int) -> str:
    # Collapses gh's per-check buckets into one verdict:
    #   'none'    -> no CI checks are configured/reported (can't verify green)
    #   'fail'    -> at least one check failed or was cancelled
    #   'pending' -> nothing failed yet but something is still running/queued
    #   'pass'    -> every check settled successfully (or was skipped)
    result = subprocess.run(
        ["gh", "pr", "checks", str(number), "--repo", f"{org}/{repo}", "--json", "bucket", "--jq", ".[].bucket"],
        capture_output=True, text=True,
    )
    buckets = [b for b in result.stdout.split() if b]
    if not buckets:
        return "none"
    if any(b in ("fail", "cancel") for b in buckets):
        return "fail"
    if any(b == "pending" for b in buckets):
        return "pending"
    return "pass"


def _critical_halt_issues(org: str) -> list[dict]:
    # Any open `critical`-labelled issue anywhere in the org is a soft halt:
    # new dispatch stops fleet-wide until it's closed. See CONVENTIONS.md
    # "Filing bugs".
    result = subprocess.run(
        [
            "gh", "search", "issues", "--owner", org, "--state", "open",
            "--label", "critical", "--json", "repository,number,title,url",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _wait_for_checks(
    org: str, repo: str, number: int, *, timeout: float, interval: float = 15.0
) -> str:
    # Polls until CI settles or `timeout` seconds elapse. Returns the terminal
    # state ('pass'/'fail'/'none'), or 'pending' if it timed out still running.
    # timeout=0 degenerates to a single check -- the shape unit tests exercise.
    deadline = time.monotonic() + timeout
    while True:
        state = _checks_state(org, repo, number)
        if state != "pending":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "pending"
        time.sleep(min(interval, remaining))


def _promote_pr(org: str, repo: str, number: int) -> None:
    subprocess.run(["gh", "pr", "ready", str(number), "--repo", f"{org}/{repo}"], check=True)


def _finalize_pr(
    org: str, repo: str, pr_number: int, *, tests_passed: bool | None, ready_timeout: float
) -> tuple[str, str]:
    # Maps a freshly-pushed draft PR onto the existing outcome vocabulary so the
    # resume/recover router still understands it: "success" ONLY when my-coder
    # itself reported tests passing AND CI goes green (then it's promoted out of
    # draft); otherwise "needs_review", which is resumable and leaves the draft
    # for a human. Trusts my-coder's own structured tests_passed signal instead
    # of re-parsing the PR body for a checked box -- a real measurement, not
    # prose a session could get wrong without anyone noticing.
    if not tests_passed:
        return "needs_review", f"PR #{pr_number} left draft: my-coder did not report tests passing"
    state = _wait_for_checks(org, repo, pr_number, timeout=ready_timeout)
    if state == "pass":
        _promote_pr(org, repo, pr_number)
        return "success", f"PR #{pr_number} promoted to ready for review (CI green)"
    if state == "none":
        return "needs_review", f"PR #{pr_number} left draft: no CI checks to verify green"
    if state == "pending":
        return "needs_review", f"PR #{pr_number} left draft: CI still running after {ready_timeout:.0f}s"
    return "needs_review", f"PR #{pr_number} left draft: CI failing"


# --- resume / recover loop -------------------------------------------------
#
# An attempt on an issue never has to start from scratch. Every terminal
# dispatch outcome is durable (the branch is pushed even on failure, the
# transcript + a per-issue ledger entry persist), so a later attempt can read
# what the prior one did, continue its branch, and move forward -- or, if the
# issue is blocked by a missing capability in another tool's repo, file that
# blocker as an issue there and pause this one instead of thrashing.

MAX_ATTEMPTS = 3
_TERMINAL_OUTCOMES = frozenset(
    {
        "success",
        "needs_review",
        "no_changes",
        "failed",
        "blocked",
        "needs_human",
        "deferred",
        # my-coder's own vocabulary: "denied" (its Policy gate blocked the PR --
        # unreachable today since fleet_dispatch doesn't pass --guarded, but
        # recorded honestly if that ever changes) and "skipped" (the picked
        # issue no longer exists, e.g. already closed by someone else).
        "denied",
        "skipped",
    }
)
_RESUMABLE_OUTCOMES = frozenset({"needs_review", "no_changes", "failed", "deferred"})
# Outcomes that count toward MAX_ATTEMPTS. "deferred" (a transient
# infrastructure failure -- session/rate limit, network) is excluded: the issue
# is fine, the fleet just couldn't run right then, so retrying it must not burn
# the budget that escalates a genuinely-stuck issue to a human.
_COUNTED_OUTCOMES = _TERMINAL_OUTCOMES - {"deferred"}
# Substrings that mark a failure as transient/infrastructure rather than a real
# problem with the issue. Matched case-insensitively against the worker's final
# message. Kept deliberately narrow -- only unambiguous capacity/transport
# signals, so a real error is never silently retried forever as "deferred".
_TRANSIENT_MARKERS = (
    "session limit",
    "usage limit",
    "rate limit",
    "overloaded",
    "service unavailable",
)


def _is_transient_failure(final_message: str) -> bool:
    low = final_message.lower()
    return any(marker in low for marker in _TRANSIENT_MARKERS)


@dataclass(frozen=True)
class Attempt:
    candidate_id: str
    outcome: str
    branch: str
    attempt_number: int  # count of terminal attempts so far, this one included
    final_message: str = ""
    blocker: str | None = None  # "<org>/<repo>#<n>" when outcome == "blocked"


def _last_attempt(ledger: Ledger, candidate_id: str) -> Attempt | None:
    entries = [
        e
        for e in ledger
        if e.tool == "fleet_dispatch"
        and e.kind == "dispatch"
        and e.outcome in _TERMINAL_OUTCOMES
        and e.data.get("candidate") == candidate_id
    ]
    if not entries:
        return None
    last = entries[-1]
    # attempt_number is the count that gates the human-escalation cap, so it
    # excludes transient runs -- a string of rate limits mustn't push a healthy
    # issue to needs_human. That means "deferred" outcomes and, defensively, any
    # "failed" entry whose message reads as transient (e.g. ones recorded before
    # transient classification existed, or by an older build).
    counted = sum(
        1
        for e in entries
        if e.outcome in _COUNTED_OUTCOMES
        and not (
            e.outcome == "failed"
            and _is_transient_failure(e.data.get("final_message", ""))
        )
    )
    return Attempt(
        candidate_id=candidate_id,
        outcome=last.outcome,
        branch=last.data.get("branch", ""),
        attempt_number=counted,
        final_message=last.data.get("final_message", ""),
        blocker=last.data.get("blocker"),
    )


def _dispatch_decision(
    attempt: Attempt | None, blocker_open: bool, max_attempts: int = MAX_ATTEMPTS
) -> str:
    # Pure routing rule: what should happen to an issue given its last attempt?
    # Returns one of: "fresh", "resume", "skip:done", "skip:blocked",
    # "skip:needs_human".
    if attempt is None:
        return "fresh"
    if attempt.outcome in ("success", "denied", "skipped"):
        # All three are considered stopping points, not transient misses:
        # denied re-hits the same policy wall every time, and a skipped issue
        # (already closed elsewhere) has nothing left to do.
        return "skip:done"
    if attempt.outcome == "needs_human":
        return "skip:needs_human"
    if attempt.outcome == "blocked":
        return "skip:blocked" if blocker_open else "resume"
    # needs_review / no_changes / failed / deferred -- resumable. The cap uses
    # attempt_number, which already excludes deferred (transient) runs, so those
    # resume indefinitely without ever escalating a healthy issue to a human.
    if attempt.attempt_number >= max_attempts:
        return "skip:needs_human"
    return "resume"


def _issue_is_open(ref: str) -> bool:
    # ref is "<org>/<repo>#<number>", e.g. "MyThingsLab/mythings-core#25".
    if "#" not in ref:
        return False
    repo, number = ref.rsplit("#", 1)
    if not number.isdigit():
        return False
    result = subprocess.run(
        ["gh", "issue", "view", number, "--repo", repo, "--json", "state", "--jq", ".state"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().upper() == "OPEN"


def _fresh_base_ref(repo_path: Path) -> str:
    # my-coder resolves its own fresh-run base ref as "origin/{base}" but never
    # fetches first -- nothing keeps these sibling checkouts current between
    # human syncs, so a stale local origin/main would make it build atop
    # already-superseded code. Called for this fetch side effect before every
    # `mycoder build` invocation; the returned ref is informational (my-coder
    # decides its own base internally). A repo with no reachable origin
    # (offline, or a bare test repo) falls back to the local main with a note
    # rather than refusing outright: if the network is really gone the run
    # fails honestly inside mycoder at push time anyway.
    fetch = subprocess.run(
        ["git", "-C", str(repo_path), "fetch", "origin", "main"],
        capture_output=True,
        text=True,
    )
    if fetch.returncode == 0:
        return "origin/main"
    print(
        f"  (git fetch origin main failed in {repo_path.name}; basing on local main: "
        f"{fetch.stderr.strip()[:120]})"
    )
    return "main"


def _mycoder_branch(candidate: Candidate) -> str:
    # Mirrors my-coder's own naming (Coder._attempt: f"{TOOL}/{self._repo_name()}
    # -{issue.number}"), so the "already has an open PR in flight" dedup check
    # and the console/ledger output can find the branch my-coder actually cuts.
    repo, number = candidate.id.split("#")
    return f"mycoder/{repo}-{number}"


def _open_pr_number(org: str, repo: str, branch: str) -> int | None:
    result = subprocess.run(
        [
            "gh", "pr", "list", "--repo", f"{org}/{repo}", "--head", branch,
            "--state", "open", "--json", "number", "--jq", ".[0].number // empty",
        ],
        capture_output=True,
        text=True,
    )
    out = result.stdout.strip()
    return int(out) if out.isdigit() else None


def _dispatch_one(
    account: Account,
    candidate: Candidate,
    *,
    execute: bool,
    max_budget_usd: float,
    max_turns: int,
    ledger: Ledger,
    org: str,
    prior: Attempt | None = None,
    ready_timeout: float = 0.0,
    session_timeout_s: float = 1800.0,
) -> None:
    repo, number = candidate.id.split("#")
    repo_path = WORKSPACE_ROOT / repo
    branch = _mycoder_branch(candidate)
    attempt_number = (prior.attempt_number + 1) if prior is not None else 1
    mode = "fresh" if prior is None else f"resume#{attempt_number} from {prior.outcome}"

    # One print call, not several: with dispatches now running concurrently in
    # separate threads, individual print()s from different accounts could
    # otherwise interleave mid-block and produce unreadable output.
    print(
        f"\n=== {account.name} -> {candidate.id} ({repo}) [{mode}] ===\n"
        f"  branch: {branch}\n"
        f"  config: {account.config_dir}\n"
        f"  budget cap: ${max_budget_usd}, turn cap: {max_turns}, "
        f"session timeout: {session_timeout_s:.0f}s"
    )

    if not execute:
        print("  [dry-run] not launched")
        return

    ledger.record(
        tool="fleet_dispatch",
        kind="dispatch",
        outcome="started",
        detail=f"{account.name} -> {candidate.id} ({mode})",
        candidate=candidate.id,
        account=account.name,
        branch=branch,
        attempt=attempt_number,
    )

    # my-coder owns the whole worker role now: its own Workspace worktree,
    # branch naming/resume, prompt (searcher/researcher context, the blocker/
    # critical-bug protocol), running the target repo's tests, and the single
    # push + draft-PR side effect. This just picks which candidate to run and
    # translates the result into the outcome vocabulary _dispatch_decision
    # already knows.
    _fresh_base_ref(repo_path)  # best-effort fetch; see its docstring
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(account.config_dir)}
    argv = [
        "mycoder", "build",
        "--repo", f"{org}/{repo}",
        "--issue", number,
        "--source", str(repo_path),
        "--session-runner", "claude",
        "--max-budget-usd", str(max_budget_usd),
        "--max-turns", str(max_turns),
        "--session-timeout-s", str(session_timeout_s),
        "--run-tests",
        "--transcripts-dir", str(TRANSCRIPTS_DIR),
        "--json",
    ]
    # A backstop above my-coder's own internal session timeout: git/test
    # overhead around the session itself isn't bounded by --session-timeout-s,
    # so this catches a genuinely stuck `mycoder build` process rather than
    # blocking the thread forever. Routed to "deferred" below, same as any
    # other transient infrastructure hiccup.
    timed_out = False
    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=session_timeout_s + 300,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        proc = subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")

    data: dict = {}
    if proc.stdout.strip():
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            data = {}

    mycoder_outcome = data.get("outcome")
    detail = data.get("detail", "")
    blocker = data.get("blocker")
    pr_number = data.get("pr")
    tests_passed = data.get("tests_passed")
    cost_usd = float(data.get("cost_usd", 0.0))
    files_touched = data.get("files_touched", [])

    # An explicit blocker signal and a wall-clock timeout both win over
    # everything else -- distinct outcomes from a real failure. Then mycoder's
    # own structured outcome, translated 1:1 except "failure" (its name) ->
    # "failed" (fleet_dispatch's own ledger vocabulary, unchanged since before
    # this swap) and a transient-message reclassification to "deferred" so a
    # session/rate-limit blip never counts toward MAX_ATTEMPTS.
    if timed_out:
        outcome, msg = (
            "deferred",
            f"deferred (transient): mycoder exceeded {session_timeout_s + 300:.0f}s wall-clock timeout",
        )
    elif mycoder_outcome is None:
        # mycoder itself never returned a parseable result -- an infrastructure
        # problem, not a real assessment of the issue.
        tail = proc.stderr.strip()[-300:]
        if _is_transient_failure(tail):
            outcome, msg = "deferred", f"deferred (transient): mycoder exited {proc.returncode}"
        else:
            outcome, msg = "failed", f"mycoder exited {proc.returncode}: {tail or 'no output'}"
    elif mycoder_outcome == "blocked":
        outcome, msg = "blocked", detail
    elif mycoder_outcome == "failure":
        outcome, msg = (
            ("deferred", f"deferred (transient): {detail}")
            if _is_transient_failure(detail)
            else ("failed", detail)
        )
    elif mycoder_outcome == "success":
        # A draft PR is open; the readiness gate decides whether it's promoted.
        outcome, msg = _finalize_pr(
            org, repo, pr_number, tests_passed=tests_passed, ready_timeout=ready_timeout
        )
    else:
        # needs_review / no_changes / denied / skipped pass through unchanged.
        outcome, msg = mycoder_outcome, detail

    note = f" (mycoder: {detail[:160]!r})" if detail and outcome != "success" else ""
    print(f"  [{account.name}] {mode}: {outcome} — {msg}{note}")
    ledger.record(
        tool="fleet_dispatch",
        kind="dispatch",
        outcome=outcome,
        detail=f"{account.name} -> {candidate.id}: {msg}",
        candidate=candidate.id,
        account=account.name,
        branch=branch,
        attempt=attempt_number,
        pr_number=pr_number,
        blocker=blocker,
        final_message=detail[:500],
        files_touched=files_touched,
    )
    if mycoder_outcome is not None:
        # Minimal usage record: _today_spend_usd only needs cost_usd. The
        # richer token/denial breakdown the old inline session recorded isn't
        # available from mycoder's --json output (it would need parsing the
        # transcript file mycoder persists) -- a known, accepted gap, not an
        # oversight.
        ledger.record(
            tool="fleet_dispatch",
            kind="usage",
            outcome="success",
            detail=f"{account.name} -> {candidate.id}: ${cost_usd:.4f}",
            candidate=candidate.id,
            account=account.name,
            cost_usd=cost_usd,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts",
        help="comma-separated CLAUDE_CONFIG_DIR paths, one per available worker "
        "(each must already be `claude auth login`'d). Not required with "
        "--abort/--clear-halt.",
    )
    parser.add_argument("--execute", action="store_true", help="actually launch headless sessions")
    halt_group = parser.add_mutually_exclusive_group()
    halt_group.add_argument(
        "--abort",
        action="store_true",
        help="kill switch: touch the HALT marker and exit immediately (no "
        "--accounts needed). Every subsequent --execute run refuses to launch "
        "anything until --clear-halt runs.",
    )
    halt_group.add_argument(
        "--clear-halt",
        action="store_true",
        help="remove the HALT marker and exit immediately, restoring normal "
        "--execute operation.",
    )
    halt_group.add_argument(
        "--raise-daily-cap",
        type=float,
        default=None,
        metavar="AMOUNT",
        help="raise today's effective --max-daily-usd ceiling to AMOUNT and "
        "exit immediately (no --accounts needed); the CLI hand-off the spend "
        "alert's Raise-cap button in Telegram shells back into. UTC-day-scoped "
        "-- reverts to --max-daily-usd on its own the next day.",
    )
    parser.add_argument(
        "--ask-human",
        action="store_true",
        help="arm the ASK escalation: a Policy ASK a worker's tool hits becomes a "
        "real Allow/Deny prompt in Telegram instead of collapsing to DENY. Needs a "
        "running `mytelegrambot run` daemon, and refuses to start without one -- a "
        "channel nobody answers is slower than no channel and just as closed.",
    )
    parser.add_argument(
        "--ask-remote-daemon",
        action="store_true",
        help="skip the local daemon check: it runs on another host sharing this ledger.",
    )
    parser.add_argument(
        "--ask-timeout",
        type=int,
        default=fleet_ask.DEFAULT_ASK_TIMEOUT,
        help="seconds to wait for the human to tap Allow/Deny (default: %(default)s).",
    )
    parser.add_argument("--org", default="MyThingsLab")
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=3.0,
        help="dollar cap passed to each headless claude session (default: $3)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=40,
        help="turn cap passed to each headless claude session (default: 40)",
    )
    parser.add_argument(
        "--max-daily-usd",
        type=float,
        default=20.0,
        help="hard ceiling on total fleet_dispatch spend (all accounts, kind=usage "
        "ledger entries) per UTC calendar day; a run that would push projected "
        "spend over this refuses to launch anything, before spend (default: $20)",
    )
    parser.add_argument(
        "--spend-alert-fraction",
        type=float,
        default=0.8,
        help="push a Telegram spend alert (with Halt / Raise-cap buttons) the "
        "first time projected spend crosses this fraction of the effective "
        "daily cap, so the operator finds out while the fleet is still "
        "spending rather than after the cap trips or in tomorrow's digest. "
        "One push per UTC day; best-effort -- a failed push never blocks a "
        "run (default: 0.8)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=600.0,
        help="seconds to wait for a pushed PR's CI to go green before promoting "
        "it from draft to ready-for-review; on timeout the PR is left a draft "
        "(default: 600). 0 checks once and does not wait.",
    )
    parser.add_argument(
        "--session-timeout-s",
        type=float,
        default=1800.0,
        help="wall-clock seconds to allow a single headless claude session to "
        "run before killing it; complements --max-budget-usd/--max-turns, which "
        "bound spend and turn count but not a stalled/hung session (default: "
        "1800 = 30 min). A timeout is recorded as 'deferred' (transient, "
        "resumable) rather than counted as a real failure.",
    )
    parser.add_argument(
        "--app-id",
        help="GitHub App ID; combine with --app-installation-id and "
        "--app-private-key to authenticate as the App instead of the ambient "
        "gh PAT for every gh call this run makes -- both fleet_dispatch's own "
        "and each dispatched worker's own gh commands, via GH_TOKEN",
    )
    parser.add_argument("--app-installation-id", help="GitHub App installation ID (see --app-id)")
    parser.add_argument(
        "--app-private-key", help="path to the GitHub App's private key .pem file (see --app-id)"
    )
    parser.add_argument(
        "--allow-personal-token",
        action="store_true",
        help="explicitly accept running --execute on the ambient personal gh "
        "token instead of the GitHub App. The personal token is scoped to every "
        "repo the account can write to (all ventures, not just this org), so a "
        "misbehaving worker inherits that whole blast radius -- opting in must "
        "be a deliberate, visible choice, never the silent default.",
    )
    args = parser.parse_args(argv)

    if args.abort:
        HALT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        HALT_MARKER.write_text(f"halted at {_utc_ts()}\n")
        print(
            f"HALT marker armed at {HALT_MARKER} — every subsequent --execute run "
            f"refuses to launch until `python3 -m myfleet.fleet_dispatch --clear-halt` runs"
        )
        return 0

    if args.clear_halt:
        if HALT_MARKER.exists():
            HALT_MARKER.unlink()
            print(f"HALT marker cleared: {HALT_MARKER}")
        else:
            print("no HALT marker was set")
        return 0

    if args.raise_daily_cap is not None:
        DAILY_CAP_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        DAILY_CAP_OVERRIDE.write_text(
            json.dumps({"day": today, "cap_usd": args.raise_daily_cap})
        )
        print(
            f"today's effective daily cap raised to ${args.raise_daily_cap:.2f} "
            f"(--max-daily-usd default: ${args.max_daily_usd:.2f})"
        )
        return 0

    if args.ask_human:
        # Armed in this process's environment, which every headless worker inherits
        # (`env = {**os.environ, ...}` below), so a tool a worker invokes escalates
        # its ASKs to a real human instead of having them silently denied.
        try:
            wiring = fleet_ask.enable(
                timeout=args.ask_timeout, remote_daemon=args.ask_remote_daemon
            )
        except fleet_ask.AskChannelUnavailable as exc:
            print(f"fleet_dispatch: cannot arm the ask channel: {exc}", file=sys.stderr)
            return 2
        print(f"ask channel armed -> {wiring['MYTHINGS_ASK_CMD']}")

    if not args.accounts:
        parser.error("--accounts must list at least one CLAUDE_CONFIG_DIR")

    accounts = _parse_accounts(args.accounts)
    if not accounts:
        parser.error("--accounts must list at least one CLAUDE_CONFIG_DIR")

    if HALT_MARKER.exists():
        if args.execute:
            print(
                f"refusing to launch: HALT marker present at {HALT_MARKER} (fleet "
                f"kill switch armed). Run `python3 -m myfleet.fleet_dispatch --clear-halt` "
                f"once it's safe to resume."
            )
            return 1
        print(
            f"note: HALT marker present at {HALT_MARKER} — this dry run still "
            f"reports normally, but --execute would refuse until --clear-halt"
        )

    app_flags = [args.app_id, args.app_installation_id, args.app_private_key]
    if any(app_flags) and not all(app_flags):
        parser.error(
            "--app-id, --app-installation-id, and --app-private-key must be given together"
        )
    if all(app_flags):
        # A stale or mistyped --app-installation-id would otherwise mint a
        # perfectly working token silently scoped to the wrong account --
        # nothing about a successful mint proves it's *this* org's
        # installation. Check before the token is ever used anywhere.
        installation_org = app_installation_org(
            args.app_id, args.app_installation_id, args.app_private_key
        )
        if installation_org != args.org:
            print(
                f"refusing to dispatch: --app-installation-id {args.app_installation_id} "
                f"belongs to '{installation_org}', not the target org '{args.org}'. "
                f"Using it would hand every worker credentials scoped to the wrong account."
            )
            return 1
        # Setting it here, once, is enough for every later `gh` call in this
        # process: fleet_dispatch's own bare subprocess.run(["gh", ...]) calls
        # inherit os.environ implicitly, and _dispatch_one's `env = {**os.environ,
        # ...}` for each spawned worker copies it too -- one mint covers both,
        # no Runner-threading needed. Installation tokens last ~1h, comfortably
        # longer than a single fleet_dispatch.py invocation.
        os.environ["GH_TOKEN"] = github_app_token(
            args.app_id, args.app_installation_id, args.app_private_key
        )
        print(
            f"authenticating as the GitHub App (installation {args.app_installation_id}, "
            f"org '{installation_org}') — the personal PAT is not used for this run"
        )

    # Identity gate: spawning workers on the ambient personal token hands every
    # session credentials for every repo the human can write to. The App path
    # exists (above); using the PAT anyway must be said out loud, per run.
    if args.execute and not all(app_flags) and not args.allow_personal_token:
        print(
            "refusing to --execute on the ambient personal gh token: every worker "
            "would inherit credentials for ALL repos this account can write to, "
            "not just the MyThingsLab org. Authenticate as the permission-scoped "
            "GitHub App (--app-id/--app-installation-id/--app-private-key), or "
            "pass --allow-personal-token to accept the wider blast radius for "
            "this run."
        )
        return 1

    if args.execute and not all(app_flags):
        # --allow-personal-token was accepted above; make the actual identity
        # visible rather than trusting the flag alone -- a stale GH_TOKEN env
        # var or a `gh auth switch` since the last run could point somewhere
        # unexpected, silently.
        proc = subprocess.run(
            ["gh", "api", "user", "-q", ".login"], capture_output=True, text=True
        )
        if proc.returncode != 0:
            print(f"refusing to dispatch: `gh auth status` failed — {proc.stderr.strip()}")
            return 1
        print(f"using the ambient personal gh token, authenticated as '{proc.stdout.strip()}'")

    # A fleet of accounts that are secretly the same account is not a fleet.
    # Always gate on distinct identities -- cheap, local, and it prevents silently
    # draining one account twice (which is exactly what happened once).
    account_problems = _preflight_distinct_accounts(accounts)
    if account_problems:
        print("account preflight failed — the configured accounts are not distinct:")
        for p in account_problems:
            print(f"  - {p}")
        return 1

    # Soft halt: a `critical`-labelled issue open anywhere in the org means
    # something security-relevant or fleet-wide-invariant-breaking is
    # unresolved. Stop starting new work until it's closed -- in-flight
    # workers still finish normally. See CONVENTIONS.md "Filing bugs".
    critical_issues = _critical_halt_issues(args.org)
    if critical_issues:
        dispatch_ledger = Ledger(DISPATCH_LEDGER)
        refs = [f"{i['repository']['nameWithOwner']}#{i['number']}" for i in critical_issues]
        print("halted: critical issue(s) open, not dispatching new work:")
        for i, ref in zip(critical_issues, refs, strict=True):
            print(f"  - {ref}: {i['title']} ({i['url']})")
        dispatch_ledger.record(
            tool="fleet_dispatch",
            kind="dispatch",
            outcome="halted_critical",
            detail=f"{len(refs)} critical issue(s) open: {', '.join(refs)}",
            issues=refs,
        )
        return 0

    orch = Orchestrator(
        org=args.org,
        manifest_path=default_manifest_path(),
        repo_root=WORKSPACE_ROOT,
        ledger=Ledger(WORKSPACE_ROOT / "my-orchestrator" / ".mythings" / "ledger.jsonl"),
    )
    # Overfetch the ranked pool so a worker slot falls through to the next
    # dispatchable candidate instead of sitting idle behind an undispatchable
    # scaffold proposal.
    pool: list[Recommendation] = orch.next_n(max(len(accounts) * 5, 20))
    dispatchable = [r.chosen for r in pool if r.chosen is not None and r.chosen.kind == "issue"]
    skipped = [r.chosen for r in pool if r.chosen is not None and r.chosen.kind != "issue"]

    if skipped:
        names = ", ".join(c.id for c in skipped)
        print(f"skipping (need MyScaffolder, not built yet): {names}")

    # Don't re-dispatch an issue that already has an open fleet-dispatch PR in
    # flight: the orchestrator ranks open issues without knowing one is already
    # being handled, and re-running it just burns an account to open a second,
    # duplicate PR for the same issue.
    in_flight = [
        c for c in dispatchable
        if _open_pr_number(args.org, c.repo, _mycoder_branch(c)) is not None
    ]
    if in_flight:
        ids = {c.id for c in in_flight}
        names = ", ".join(sorted(ids))
        print(f"skipping (already has an open fleet-dispatch PR): {names}")
        dispatchable = [c for c in dispatchable if c.id not in ids]

    dispatch_ledger = Ledger(DISPATCH_LEDGER)

    # Resume/recover routing: read each issue's last attempt and decide whether
    # to start fresh, resume the prior branch, or skip it -- still blocked on a
    # cross-repo dependency, or given up on after MAX_ATTEMPTS tries.
    plan: list[tuple[Candidate, Attempt | None]] = []
    for c in dispatchable:
        prior = _last_attempt(dispatch_ledger, c.id)
        blocker_open = (
            _issue_is_open(prior.blocker)
            if prior is not None and prior.outcome == "blocked" and prior.blocker
            else False
        )
        decision = _dispatch_decision(prior, blocker_open)
        if decision == "skip:done":
            continue
        if decision == "skip:blocked":
            print(f"skipping (blocked on {prior.blocker}, still open): {c.id}")
            continue
        if decision == "skip:needs_human":
            print(f"skipping (needs a human after {prior.attempt_number} attempts): {c.id}")
            # Record it once, so it stays skipped instead of being re-evaluated
            # (and re-counted) every run.
            if prior.outcome != "needs_human":
                detail = (
                    f"{c.id}: gave up after {prior.attempt_number} attempts "
                    f"(last outcome: {prior.outcome})"
                )
                dispatch_ledger.record(
                    tool="fleet_dispatch",
                    kind="dispatch",
                    outcome="needs_human",
                    detail=detail,
                    candidate=c.id,
                    account="-",
                    branch=_mycoder_branch(c),
                    attempt=prior.attempt_number,
                    final_message=prior.final_message[:500],
                )
                # Best-effort: needs_human is recorded either way, so a dead
                # channel loses the phone ping, not the escalation itself.
                fleet_ask.escalate_blocker(
                    candidate=c.id, detail=detail, attempt=prior.attempt_number
                )
            continue
        plan.append((c, prior if decision == "resume" else None))

    pairs = list(zip(accounts, plan, strict=False))  # deliberately truncates to the shorter of the two

    # Enforced before spend, not after: sum today's actual usage-ledger cost
    # plus the worst case for every session this run is about to launch, and
    # refuse the whole run if that would cross the daily ceiling. A dry run
    # spends nothing, so it's exempt.
    if args.execute and pairs:
        effective_cap = _effective_daily_cap(args.max_daily_usd)
        spent_today = _today_spend_usd(dispatch_ledger)
        projected = spent_today + len(pairs) * args.max_budget_usd
        if projected > effective_cap:
            print(
                f"refusing to launch: today's fleet_dispatch spend is already "
                f"${spent_today:.2f}, and {len(pairs)} more session(s) at up to "
                f"${args.max_budget_usd:.2f} each could reach ${projected:.2f}, "
                f"over the ${effective_cap:.2f}/day cap (--max-daily-usd, "
                f"or a --raise-daily-cap override). Raise --max-daily-usd, lower "
                f"--max-budget-usd, or wait for the UTC day to roll over."
            )
            return 1
        # A supervised loop should learn it's approaching the cap while it is
        # still spending, not from tomorrow's digest or when the refusal above
        # finally trips -- see fleet-dispatch#41. Once per day is deliberate:
        # --loop re-evaluates this every iteration, and a crossed threshold
        # stays crossed.
        if (
            projected >= args.spend_alert_fraction * effective_cap
            and not _spend_alert_sent_today(dispatch_ledger)
        ):
            raise_to = round(effective_cap * 1.5, 2)
            sent = fleet_ask.alert_spend(spent=spent_today, cap=effective_cap, raise_to=raise_to)
            dispatch_ledger.record(
                tool="fleet_dispatch",
                kind="spend_alert",
                outcome="success" if sent else "failure",
                detail=f"spend alert: ${spent_today:.2f}/${effective_cap:.2f} "
                f"({args.spend_alert_fraction:.0%} threshold)",
            )

    failures: list[tuple[Account, Candidate, BaseException]] = []
    if pairs:
        # One worker thread per account: each `mycoder build` invocation gets
        # its own Workspace worktree (mythings.isolation.Workspace, inside
        # my-coder) under its own CLAUDE_CONFIG_DIR, so nothing about running
        # them at the same time needs new isolation.
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            futures = {
                pool.submit(
                    _dispatch_one,
                    account,
                    candidate,
                    execute=args.execute,
                    max_budget_usd=args.max_budget_usd,
                    max_turns=args.max_turns,
                    ledger=dispatch_ledger,
                    org=args.org,
                    prior=prior,
                    ready_timeout=args.ready_timeout,
                    session_timeout_s=args.session_timeout_s,
                ): (account, candidate)
                for account, (candidate, prior) in pairs
            }
            # future.exception() blocks until that future is done but, unlike
            # future.result(), never raises -- so one account's crash can't
            # stop us from also collecting every other account's outcome.
            for future, (account, candidate) in futures.items():
                exc = future.exception()
                if exc is not None:
                    failures.append((account, candidate, exc))
    for account, candidate, exc in failures:
        print(f"  [{account.name}] {candidate.id} crashed: {exc!r}")
    for account in accounts[len(plan) :]:
        print(f"\n=== {account.name}: no ready issue candidate ===")

    if not args.execute:
        print("\n(dry run — pass --execute to actually launch these sessions)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
