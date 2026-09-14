#!/usr/bin/env python3
"""Fan out ready fleet work across multiple Claude Code accounts.

Reuses myorchestrator's ranking (myorchestrator next --count N) to pick one
distinct candidate per available worker, then runs each as a `mycoder build`
call under a different CLAUDE_CONFIG_DIR — so two subscriptions can work the
fleet concurrently without touching each other's files. my-coder owns the
worker role end to end (its own Workspace worktree, branch naming/resume,
prompt, the single push + PR side effect); this module picks which candidate to
run and translates the result into the resume/recover outcomes below.

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

Each run ends at "PR opened" — ready for review when my-coder's own suite
passed, draft when it did not. This module promotes nothing, never pushes to
main, and never merges; the gate is the merge, and a human performs it.
Defaults to --dry-run; pass --execute to actually spawn the headless sessions.

Kill switch: `--abort` touches a HALT marker (.my-fleet/HALT) and exits;
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
import threading
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
from myfleet.workspace import ROOT_ENV, fleet_root, ledger_path, runtime_dir

# Climbs myfleet/<file>.py -> src -> my-fleet -> MyThingsLab/ (the fleet root),
# unless $MYTHINGS_WORKSPACE_ROOT says otherwise -- the climb lands in a scratch
# dir when this module is imported from a Workspace worktree (#48).
WORKSPACE_ROOT = fleet_root(__file__)
RUNTIME_DIR = runtime_dir(WORKSPACE_ROOT)
DISPATCH_LEDGER = ledger_path(WORKSPACE_ROOT)
TRANSCRIPTS_DIR = RUNTIME_DIR / "transcripts"
# The kill switch: a marker file, not a signal or a flag a running process has
# to poll mid-loop. `--execute` checks for it before launching anything and
# refuses outright if it's there, so arming it (`--abort`) always beats a run
# that starts after it -- no race between "halt" and "launch". It doesn't
# reach into an already-running headless session (those are already bounded by
# --max-budget-usd/--max-turns and end on their own); it stops the *next* one.
HALT_MARKER = RUNTIME_DIR / "HALT"

# The spend alert's "Raise cap" button (mytelegrambot's spend_command) shells
# back into `--raise-daily-cap AMOUNT`; this is where that lands. Day-scoped
# like the spend it overrides, so a raise from a busy day doesn't silently
# persist into the next one -- an operator who wants a permanently higher
# ceiling should pass --max-daily-usd instead.
DAILY_CAP_OVERRIDE = RUNTIME_DIR / "daily-cap-override.json"

# Active worker registry blackboard: coordinates concurrent workers deterministically (my-fleet#70).
ACTIVE_WORKERS_FILE = RUNTIME_DIR / "active_workers.json"
_WORKER_REGISTRY_LOCK = threading.Lock()


def _read_active_workers(path: Path | None = None) -> dict[str, dict[str, str]]:
    target_path = path or ACTIVE_WORKERS_FILE
    if not target_path.exists():
        return {}
    try:
        data = json.loads(target_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_active_workers(data: dict[str, dict[str, str]], path: Path | None = None) -> None:
    target_path = path or ACTIVE_WORKERS_FILE
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp_path.replace(target_path)


def _register_worker(
    account_name: str,
    candidate_id: str,
    repo: str,
    *,
    started_at: str | None = None,
    path: Path | None = None,
) -> None:
    target_path = path or ACTIVE_WORKERS_FILE
    now_ts = started_at or datetime.now(UTC).isoformat()
    with _WORKER_REGISTRY_LOCK:
        workers = _read_active_workers(target_path)
        workers[account_name] = {
            "candidate": candidate_id,
            "repo": repo,
            "started_at": now_ts,
        }
        _write_active_workers(workers, target_path)


def _deregister_worker(account_name: str, *, path: Path | None = None) -> None:
    target_path = path or ACTIVE_WORKERS_FILE
    with _WORKER_REGISTRY_LOCK:
        workers = _read_active_workers(target_path)
        if account_name in workers:
            del workers[account_name]
            _write_active_workers(workers, target_path)


def _cleanup_stale_leases(
    *,
    max_age_s: float = 7200.0,
    force_all: bool = False,
    now: datetime | None = None,
    path: Path | None = None,
) -> int:
    target_path = path or ACTIVE_WORKERS_FILE
    with _WORKER_REGISTRY_LOCK:
        workers = _read_active_workers(target_path)
        if not workers:
            return 0
        if force_all:
            removed = len(workers)
            _write_active_workers({}, target_path)
            return removed

        current_time = now or datetime.now(UTC)
        cleaned = {}
        removed = 0
        for name, info in workers.items():
            started_str = info.get("started_at", "")
            is_stale = False
            try:
                started_dt = datetime.fromisoformat(started_str)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=UTC)
                if (current_time - started_dt).total_seconds() > max_age_s:
                    is_stale = True
            except (ValueError, TypeError):
                is_stale = True

            if is_stale:
                removed += 1
            else:
                cleaned[name] = info

        if removed > 0:
            _write_active_workers(cleaned, target_path)
        return removed


def _active_fleet_context(current_account: str, *, path: Path | None = None) -> str:
    target_path = path or ACTIVE_WORKERS_FILE
    with _WORKER_REGISTRY_LOCK:
        workers = _read_active_workers(target_path)
    siblings = [
        (acc, info)
        for acc, info in workers.items()
        if acc != current_account and isinstance(info, dict)
    ]
    if not siblings:
        return ""
    lines = [
        "Active Fleet Context:",
        "The following sibling tasks are currently in flight across the fleet:",
    ]
    for acc, info in siblings:
        cand = info.get("candidate", "unknown")
        repo = info.get("repo", "unknown")
        started = info.get("started_at", "")
        lines.append(f"- Worker '{acc}': {cand} (repo: {repo}, started: {started})")
    lines.append(
        "Be aware of related upstream/downstream changes in flight; avoid conflicting modifications."
    )
    return "\n".join(lines)


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


# --- PR merge-readiness report ----------------------------------------------
#
# Observation only: nothing here changes a PR's state. "success" means my-coder
# reported its suite passing AND CI independently agrees, so a human can pick
# the PR up and merge it. Anything short of that reports "needs_review" (a
# resumable outcome). The gate is the merge, and the human performs it.


def _checks_state(org: str, repo: str, number: int) -> str:
    # Collapses gh's per-check buckets into one verdict:
    #   'none'    -> no required checks are configured/reported. Usually means
    #                the repo's main is unprotected, or its protection lists no
    #                required check: a green `test` run that branch protection
    #                does not require is not something to merge on.
    #   'fail'    -> at least one required check failed or was cancelled
    #   'pending' -> nothing failed yet but something is still running/queued
    #   'skipped' -> a required check was skipped, so it produced no result
    #   'pass'    -> every required check settled successfully
    #
    # Two things this deliberately does NOT do.
    #
    # It does not treat a skipped check as a pass. `ci.yml` skips while a PR is
    # a draft, and my-coder used to open every PR as one -- so every worker PR
    # reported buckets of `skipping`, which fell through to 'pass' and got the
    # PR promoted and logged as "(CI green)" for a suite that never ran (#32).
    # A verified PR now opens ready, so this should no longer fire in practice;
    # it stays because absence of evidence is not evidence, and answering that
    # question is this function's entire job.
    #
    # It does not treat a skipped check as a failure either. Plenty of jobs skip
    # legitimately -- path filters, or the dependabot automerge job, which skips
    # on every human PR. Flipping skipped to 'fail' would report every worker
    # PR in the fleet as broken. The distinction that matters is whether the *required*
    # checks ran, so ask gh for only those and judge nothing else.
    result = subprocess.run(
        [
            "gh",
            "pr",
            "checks",
            str(number),
            "--repo",
            f"{org}/{repo}",
            "--required",
            "--json",
            "bucket",
            "--jq",
            ".[].bucket",
        ],
        capture_output=True,
        text=True,
    )
    buckets = [b for b in result.stdout.split() if b]
    if not buckets:
        return "none"
    if any(b in ("fail", "cancel") for b in buckets):
        return "fail"
    if any(b == "pending" for b in buckets):
        return "pending"
    # `any`, not `all`: a required check that skipped produced no result, and a
    # sibling check going green is not evidence for it. Branch protection would
    # hold the merge on that missing result anyway.
    if any(b == "skipping" for b in buckets):
        return "skipped"
    return "pass"


def _critical_halt_issues(org: str) -> list[dict]:
    # Any open `critical`-labelled issue anywhere in the org is a soft halt:
    # new dispatch stops fleet-wide until it's closed. See CONVENTIONS.md
    # "Filing bugs".
    result = subprocess.run(
        [
            "gh",
            "search",
            "issues",
            "--owner",
            org,
            "--state",
            "open",
            "--label",
            "critical",
            "--json",
            "repository,number,title,url",
        ],
        capture_output=True,
        text=True,
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
    # state ('pass'/'fail'/'none'/'skipped'), or 'pending' if it timed out still
    # running. 'skipped' is terminal: a skipped check will not start later, so
    # waiting on it would just burn the whole timeout to reach the same answer.
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


def _finalize_pr(
    org: str, repo: str, pr_number: int, *, tests_passed: bool | None, ready_timeout: float
) -> tuple[str, str]:
    # Observes a freshly-pushed PR and maps it onto the outcome vocabulary the
    # resume/recover router understands. It no longer PROMOTES anything.
    #
    # my-coder now opens the PR ready-for-review when its own in-worktree suite
    # passed, and draft otherwise (#32). That resolves a circularity this
    # function used to sit inside: promotion was gated on CI green, but `ci.yml`
    # skips required checks while a PR is a draft, so the gate could only ever
    # be satisfied by misreading a skip as a pass -- which is exactly what it
    # did. Opening ready is what makes CI run at all, and the gate that matters
    # moved to the merge, which a human always performs.
    #
    # So "success" here means: my-coder verified it, and CI independently agrees
    # -- a PR a human can pick up and merge. Anything else is "needs_review",
    # which is resumable. Trusts my-coder's structured tests_passed signal
    # rather than re-parsing the PR body for a checked box: a real measurement,
    # not prose a session could get wrong without anyone noticing.
    if not tests_passed:
        return (
            "needs_review",
            f"PR #{pr_number} left draft: my-coder did not report tests passing",
        )
    state = _wait_for_checks(org, repo, pr_number, timeout=ready_timeout)
    if state == "pass":
        return "success", f"PR #{pr_number} ready for review, CI green -- awaiting a human merge"
    if state == "none":
        return "needs_review", f"PR #{pr_number} not merged: no CI checks to verify green"
    if state == "skipped":
        # Should be unreachable now that a verified PR is opened ready: a
        # required check only skips on a draft. Kept because reaching it means
        # the ready-on-verified path silently regressed, and saying so beats
        # falling through to a misleading verdict.
        return (
            "needs_review",
            f"PR #{pr_number} not merged: required CI was skipped (never ran), not green",
        )
    if state == "pending":
        return (
            "needs_review",
            f"PR #{pr_number} not merged: CI still running after {ready_timeout:.0f}s",
        )
    return "needs_review", f"PR #{pr_number} not merged: CI failing"


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
        and not (e.outcome == "failed" and _is_transient_failure(e.data.get("final_message", "")))
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


def _ensure_repo_graph(repo_path: Path) -> Path | None:
    """Ensure the target repository's deterministic codebase graph is indexed and cached by commit SHA."""
    try:
        from mythings.graph import CodebaseGraph, MarkdownExtractor, PythonAstExtractor
    except ImportError:
        return None

    if not repo_path.is_dir() or not (repo_path / ".git").exists():
        return None

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        head_sha = proc.stdout.strip()
    except Exception:
        return None

    cache_dir = repo_path / ".mythings"
    cache_dir.mkdir(parents=True, exist_ok=True)
    db_path = cache_dir / "graph.sqlite"
    meta_path = cache_dir / "graph.meta.json"

    if db_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("commit_sha") == head_sha:
                return db_path
        except Exception:
            pass

    temp_db = cache_dir / "graph.sqlite.tmp"
    if temp_db.exists():
        try:
            temp_db.unlink()
        except OSError:
            pass

    try:
        graph = CodebaseGraph(temp_db)
        PythonAstExtractor(repo_root=repo_path).index_repo(graph)
        MarkdownExtractor(repo_root=repo_path).index_docs(graph)
        graph.close()
        temp_db.replace(db_path)
        meta_data = {
            "commit_sha": head_sha,
            "indexed_at": datetime.now(UTC).isoformat(),
        }
        meta_path.write_text(json.dumps(meta_data), encoding="utf-8")
        return db_path
    except Exception:
        if temp_db.exists():
            try:
                temp_db.unlink()
            except OSError:
                pass
        return None


def _mycoder_branch(candidate: Candidate) -> str:
    # Mirrors my-coder's own naming (Coder._attempt: f"{TOOL}/{self._repo_name()}
    # -{issue.number}"), so the "already has an open PR in flight" dedup check
    # and the console/ledger output can find the branch my-coder actually cuts.
    repo, number = candidate.id.split("#")
    return f"mycoder/{repo}-{number}"


def _open_pr_number(org: str, repo: str, branch: str) -> int | None:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{org}/{repo}",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[0].number // empty",
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
    provider: str = "claude",
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
        f"\n=== {account.name} -> {candidate.id} ({repo}) [{mode}] [{provider}] ===\n"
        f"  branch: {branch}\n"
        f"  config: {account.config_dir}\n"
        f"  budget cap: ${max_budget_usd}, turn cap: {max_turns}, "
        f"session timeout: {session_timeout_s:.0f}s"
    )

    if not execute:
        print("  [dry-run] not launched")
        return

    _register_worker(account.name, candidate.id, repo)
    try:
        ledger.record(
            tool="fleet_dispatch",
            kind="dispatch",
            outcome="started",
            detail=f"{account.name} -> {candidate.id} ({mode}) [{provider}]",
            candidate=candidate.id,
            account=account.name,
            branch=branch,
            attempt=attempt_number,
        )

        # my-coder owns the whole worker role now: its own Workspace worktree,
        # branch naming/resume, prompt (searcher/researcher context, the blocker/
        # critical-bug protocol), running the target repo's tests, and the single
        # push + PR side effect. This just picks which candidate to run and
        # translates the result into the outcome vocabulary _dispatch_decision
        # already knows.
        _fresh_base_ref(repo_path)  # best-effort fetch; see its docstring
        graph_db = _ensure_repo_graph(repo_path)
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "CLAUDE_CONFIG_DIR": str(account.config_dir),
            "GEMINI_CONFIG_DIR": str(account.config_dir),
        }
        if graph_db is not None:
            env["MYTHINGS_GRAPH_PATH"] = str(graph_db)
        fleet_ctx = _active_fleet_context(account.name)
        if fleet_ctx:
            env["MYTHINGS_FLEET_CONTEXT"] = fleet_ctx
        argv = [
            "mycoder",
            "build",
            "--repo",
            f"{org}/{repo}",
            "--issue",
            number,
            "--source",
            str(repo_path),
            "--session-runner",
            provider,
            "--max-budget-usd",
            str(max_budget_usd),
            "--max-turns",
            str(max_turns),
            "--session-timeout-s",
            str(session_timeout_s),
            "--run-tests",
            "--transcripts-dir",
            str(TRANSCRIPTS_DIR),
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
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=session_timeout_s + 300,
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
            # A PR is open; ask CI whether it's actually mergeable by a human.
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
    finally:
        _deregister_worker(account.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts",
        help="comma-separated config dir paths, one per available worker. Not required with "
        "--abort/--clear-halt.",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "gemini"],
        default="claude",
        help="model provider for worker sessions (default: %(default)s)",
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
        help="remove the HALT marker and exit immediately, restoring normal --execute operation.",
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
        help="seconds to wait for a pushed PR's CI to settle before recording "
        "the dispatch outcome; on timeout the run is reported needs_review and "
        "the PR is left for a human (default: 600). 0 checks once and does not "
        "wait. Nothing is promoted or merged either way -- my-coder opens the "
        "PR ready when its own suite passed, and a human always merges.",
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
        stale = _cleanup_stale_leases(force_all=True)
        if HALT_MARKER.exists():
            HALT_MARKER.unlink()
            print(f"HALT marker cleared: {HALT_MARKER}")
        else:
            print("no HALT marker was set")
        if stale:
            print(f"cleaned up {stale} stale worker lease(s)")
        return 0

    if args.raise_daily_cap is not None:
        DAILY_CAP_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        DAILY_CAP_OVERRIDE.write_text(json.dumps({"day": today, "cap_usd": args.raise_daily_cap}))
        print(
            f"today's effective daily cap raised to ${args.raise_daily_cap:.2f} "
            f"(--max-daily-usd default: ${args.max_daily_usd:.2f})"
        )
        return 0

    # Same inheritance, one step earlier: a worker runs inside a Workspace
    # worktree, where climbing out of `__file__` lands in the scratch dir rather
    # than the fleet root (#48). This process is not in a worktree, so say where
    # the root is instead of letting each worker re-derive it from a path that
    # moved -- including the ledger `mytelegrambot ask` polls for the reply.
    os.environ.setdefault(ROOT_ENV, str(WORKSPACE_ROOT))

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
        proc = subprocess.run(["gh", "api", "user", "-q", ".login"], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"refusing to dispatch: `gh auth status` failed — {proc.stderr.strip()}")
            return 1
        print(f"using the ambient personal gh token, authenticated as '{proc.stdout.strip()}'")

    # A fleet of accounts that are secretly the same account is not a fleet.
    # Always gate on distinct identities -- cheap, local, and it prevents silently
    # draining one account twice (which is exactly what happened once).
    if args.provider == "claude":
        account_problems = _preflight_distinct_accounts(accounts)
    else:
        seen_dirs: set[str] = set()
        account_problems = []
        for account in accounts:
            resolved = str(account.config_dir.resolve())
            if resolved in seen_dirs:
                account_problems.append(
                    f"{account.name} ({account.config_dir}) duplicates another worker's config directory"
                )
            seen_dirs.add(resolved)
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
    # scaffold proposal (#51).
    fetch_count = max(len(accounts) * 10, 50)
    pool: list[Recommendation] = orch.next_n(fetch_count)
    candidates_pool = [r.chosen for r in pool if r.chosen is not None]
    dispatchable = [c for c in candidates_pool if c.kind == "issue"]
    skipped = [c for c in candidates_pool if c.kind != "issue"]

    if skipped:
        names = ", ".join(c.id for c in skipped)
        print(f"skipping (need MyScaffolder, not built yet): {names}")

    # Don't re-dispatch an issue that already has an open fleet-dispatch PR in
    # flight: the orchestrator ranks open issues without knowing one is already
    # being handled, and re-running it just burns an account to open a second,
    # duplicate PR for the same issue.
    in_flight = [
        c for c in dispatchable if _open_pr_number(args.org, c.repo, _mycoder_branch(c)) is not None
    ]
    if in_flight:
        ids = {c.id for c in in_flight}
        names = ", ".join(sorted(ids))
        print(f"skipping (already has an open fleet-dispatch PR): {names}")
        dispatchable = [c for c in dispatchable if c.id not in ids]

    # No re-sort here, deliberately. The order `Orchestrator.next_n` returned is
    # `mythings.labels.sort_key`'s, which ranks priority above lane on purpose;
    # a second pass keyed on lane makes lane primary again and demotes every
    # `prio:P0` outside core behind every `prio:P3` inside it. "Core stays
    # stable" is already carried by that key's lane tiebreak, where it decides
    # among equals instead of overruling them.
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

    pairs = list(
        zip(accounts, plan, strict=False)
    )  # deliberately truncates to the shorter of the two

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
        if projected >= args.spend_alert_fraction * effective_cap and not _spend_alert_sent_today(
            dispatch_ledger
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

    if args.execute:
        stale = _cleanup_stale_leases(max_age_s=args.session_timeout_s + 600)
        if stale:
            print(f"cleaned up {stale} stale worker lease(s) from previous runs")

    failures: list[tuple[Account, Candidate, BaseException]] = []
    if pairs:
        if args.execute:
            for account, (candidate, _) in pairs:
                _register_worker(account.name, candidate.id, candidate.id.split("#")[0])
        # One worker thread per account: each `mycoder build` invocation gets
        # its own Workspace worktree (mythings.isolation.Workspace, inside
        # my-coder) under its own config dir, so nothing about running
        # them at the same time needs new isolation.
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            futures = {}
            for account, (candidate, prior) in pairs:
                call_kw: dict[str, object] = dict(
                    execute=args.execute,
                    max_budget_usd=args.max_budget_usd,
                    max_turns=args.max_turns,
                    ledger=dispatch_ledger,
                    org=args.org,
                    prior=prior,
                    ready_timeout=args.ready_timeout,
                    session_timeout_s=args.session_timeout_s,
                )
                if args.provider != "claude":
                    call_kw["provider"] = args.provider
                futures[pool.submit(_dispatch_one, account, candidate, **call_kw)] = (
                    account,
                    candidate,
                )
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

    if not pairs:
        if candidates_pool:
            dispatch_ledger.record(
                tool="fleet_dispatch",
                kind="dispatch",
                outcome="no_dispatchable_candidates",
                detail=(
                    f"considered {len(candidates_pool)} candidate(s) from backlog, "
                    f"but none were dispatchable ({len(skipped)} scaffolds skipped)"
                ),
                skipped_scaffolds=[c.id for c in skipped],
            )
        else:
            dispatch_ledger.record(
                tool="fleet_dispatch",
                kind="dispatch",
                outcome="backlog_empty",
                detail="no candidates in backlog",
            )

    if not args.execute:
        print("\n(dry run — pass --execute to actually launch these sessions)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
