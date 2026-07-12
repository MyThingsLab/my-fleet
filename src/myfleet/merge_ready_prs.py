#!/usr/bin/env python3
"""List (and optionally merge) every open PR across the org that's actually
mergeable: not a draft, no conflicts, every required check green.

This never runs on its own -- the user runs it by hand. Defaults to a dry
run (report only); pass --execute to actually merge. Uses a real merge
commit (`gh pr merge --merge`), matching the "Merge pull request #N from
..." shape already in every repo's history -- not squash, not rebase.

Never touches a draft PR (those aren't "ready" yet -- see fleet_dispatch.py's
own draft -> ready -> green -> merge shape) and never overrides a red/pending
check or a real conflict.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from myguard import Guard
from myguard.rules import MERGE_ACTION
from mythings.policy import Action, Decision

import myfleet.fleet_ask as fleet_ask

ORG = "MyThingsLab"


@dataclass(frozen=True)
class PR:
    repo: str
    number: int
    title: str
    is_draft: bool
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state: str  # CLEAN | BLOCKED | DIRTY | UNSTABLE | ...
    checks: list[dict]
    base: str = "main"
    head: str = ""

    @property
    def blocking_checks(self) -> list[str]:
        blockers = []
        for check in self.checks:
            conclusion = check.get("conclusion") or check.get("status")
            if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                blockers.append(f"{check.get('name', '?')}={conclusion}")
        return blockers

    @property
    def ready(self) -> bool:
        return (
            not self.is_draft
            and self.mergeable == "MERGEABLE"
            and self.merge_state == "CLEAN"
            and not self.blocking_checks
        )

    @property
    def reason_not_ready(self) -> str:
        if self.is_draft:
            return "still a draft"
        if self.mergeable == "CONFLICTING":
            return "has merge conflicts"
        if self.mergeable == "UNKNOWN":
            return "mergeability not yet computed by GitHub (re-run in a moment)"
        if self.blocking_checks:
            return f"checks not green: {', '.join(self.blocking_checks)}"
        if self.merge_state != "CLEAN":
            return f"mergeStateStatus={self.merge_state}"
        return "not ready"


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc.stdout


def list_org_repos(org: str) -> list[str]:
    raw = _run(["gh", "repo", "list", org, "--limit", "200", "--json", "name"])
    return [obj["name"] for obj in json.loads(raw)]


def list_open_prs(repo: str) -> list[PR]:
    raw = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{ORG}/{repo}",
            "--state",
            "open",
            "--json",
            "number,title,isDraft,mergeable,mergeStateStatus,statusCheckRollup,"
            "baseRefName,headRefName",
        ]
    )
    prs = []
    for obj in json.loads(raw):
        prs.append(
            PR(
                repo=repo,
                number=obj["number"],
                title=obj["title"],
                is_draft=obj["isDraft"],
                mergeable=obj["mergeable"],
                merge_state=obj["mergeStateStatus"],
                checks=obj.get("statusCheckRollup") or [],
                base=obj.get("baseRefName", "main"),
                head=obj.get("headRefName", ""),
            )
        )
    return prs


# Merging a stacked PR modifies its base PR's head: GitHub must recompute that
# PR's merge state ("Base branch was modified") and the synchronize-triggered CI
# run on the new head commit needs ~30s before the required check is green
# again ("not mergeable" / "not up to date"). All transient — wait and retry.
_TRANSIENT = ("Base branch was modified", "not mergeable", "not up to date")


def merge(pr: PR, *, retries: int = 4) -> None:
    for attempt in range(retries):
        try:
            _run(
                [
                    "gh",
                    "pr",
                    "merge",
                    str(pr.number),
                    "--repo",
                    f"{ORG}/{pr.repo}",
                    "--merge",
                ]
            )
            return
        except RuntimeError as exc:
            if any(t in str(exc) for t in _TRANSIENT) and attempt < retries - 1:
                time.sleep(15)
                continue
            raise


def approve(pr: PR, guard: Guard) -> Decision:
    # The merge goes through MyGuard as a structured `pr-merge` Action, which its
    # `merge_needs_a_human` rule answers ASK. With an ask channel live that is a
    # real Allow/Deny prompt on the operator's phone, and their tap *is* the merge.
    # With no channel it collapses to DENY and nothing happens -- so this can never
    # merge something a human did not approve.
    action = Action(
        kind=MERGE_ACTION,
        payload={"repo": f"{ORG}/{pr.repo}", "number": pr.number, "title": pr.title},
    )
    return guard.evaluate(action).under(unattended=True)


def merge_by_asking(ready: list[PR], guard: Guard, *, budget_s: float) -> int:
    # Bounded on purpose. Each unanswered ask blocks for the full ask timeout, so a
    # queue of PRs with nobody home would spend the whole pass timing out, one
    # prompt at a time. The budget caps that: when it is gone, the rest go unasked
    # rather than being silently denied by exhaustion.
    deadline = time.monotonic() + budget_s
    merged: list[str] = []
    refused: list[str] = []
    unasked: list[str] = []
    failures: list[str] = []

    for pr in ready:
        name = f"{pr.repo}#{pr.number}"
        if time.monotonic() >= deadline:
            unasked.append(name)
            continue

        print(f"asking about {name} {pr.title!r}...")
        if approve(pr, guard) is not Decision.ALLOW:
            # Denied, or nobody answered. Both are a "no" -- fail-closed, as always.
            print("  not approved — skipped")
            refused.append(name)
            continue
        try:
            merge(pr)
        except RuntimeError as exc:
            print(f"  approved, but the merge FAILED: {exc}", file=sys.stderr)
            failures.append(name)
            continue
        print("  approved — merged")
        merged.append(name)

    print()
    print(f"merged:   {', '.join(merged) or '—'}")
    print(f"refused:  {', '.join(refused) or '—'}")
    if unasked:
        print(f"unasked:  {', '.join(unasked)}  (ask budget exhausted)")
    if failures:
        print(f"failed:   {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default=ORG)
    how = parser.add_mutually_exclusive_group()
    how.add_argument(
        "--execute",
        action="store_true",
        help="merge straight away; the human running this command *is* the human approving. Default is a dry run",
    )
    how.add_argument(
        "--ask",
        action="store_true",
        help="merge only what the operator approves over Telegram: each PR becomes an Allow/Deny prompt via MyGuard's pr-merge ASK rule. Needs a running mytelegrambot daemon",
    )
    parser.add_argument(
        "--ask-budget-min",
        type=float,
        default=15.0,
        help="stop asking after this many minutes; the rest go unasked rather than timing out one by one (default: %(default)s)",
    )
    parser.add_argument(
        "--ask-timeout",
        type=int,
        default=fleet_ask.DEFAULT_ASK_TIMEOUT,
        help="seconds to wait for each Allow/Deny tap (default: %(default)s)",
    )
    parser.add_argument(
        "--ask-remote-daemon",
        action="store_true",
        help="the daemon runs on another host sharing this ledger",
    )
    parser.add_argument(
        "--ask-ledger",
        type=Path,
        default=fleet_ask.BOT_LEDGER,
        help="the ledger the mytelegrambot daemon writes taps to. `ask` and the daemon "
        "rendezvous through this file and nothing else, so it must be the daemon's own. "
        "The default is derived relative to this script, which is only correct when this "
        "repo is the workspace root -- it is not on every host (default: %(default)s)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        help="limit to this repo (repeatable); default: every org repo",
    )
    args = parser.parse_args(argv)

    repos = args.repo or list_org_repos(args.org)

    ready: list[PR] = []
    not_ready: list[PR] = []
    for repo in repos:
        try:
            for pr in list_open_prs(repo):
                (ready if pr.ready else not_ready).append(pr)
        except RuntimeError as exc:
            print(f"skipping {repo}: {exc}", file=sys.stderr)

    if not_ready:
        print("not ready:")
        for pr in not_ready:
            print(f"  {pr.repo}#{pr.number} {pr.title!r} — {pr.reason_not_ready}")
        print()

    if not ready:
        print("nothing mergeable right now")
        return 0

    print("mergeable:")
    for pr in ready:
        print(f"  {pr.repo}#{pr.number} {pr.title!r}")

    if args.ask:
        try:
            wiring = fleet_ask.enable(
                ledger=args.ask_ledger,
                timeout=args.ask_timeout,
                remote_daemon=args.ask_remote_daemon,
            )
        except fleet_ask.AskChannelUnavailable as exc:
            print(f"\ncannot ask: {exc}", file=sys.stderr)
            return 2
        print(f"\nasking over Telegram -> {wiring['MYTHINGS_ASK_CMD']}\n")
        # Built *after* the env is armed, so it picks the channel up.
        return merge_by_asking(ready, Guard(), budget_s=args.ask_budget_min * 60)

    if not args.execute:
        print(
            "\n(dry run — pass --execute to merge these here, or --ask to approve them from Telegram)"
        )
        return 0

    print()
    failures: list[str] = []
    for pr in ready:
        print(f"merging {pr.repo}#{pr.number}...")
        try:
            merge(pr)
        except RuntimeError as exc:
            # One stuck PR shouldn't strand the rest of the queue.
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures.append(f"{pr.repo}#{pr.number}")
    if failures:
        print(
            f"\nfailed to merge: {', '.join(failures)} — re-run after checking them",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
