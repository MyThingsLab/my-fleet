#!/usr/bin/env python3
"""File a P0 when a kernel repo's `main` goes red, and close it when it recovers.

The fleet had no watcher for its own foundation. core#157 edited `harness.md`
without re-vendoring the `HARNESS.md` copies, three kernel mains went red on
`test_harness_drift`, and for a day no issue existed at all -- every downstream
worker inherited a broken base and nothing in the fleet knew. A red main is not
a backlog item; it is the thing every other repo is built on being broken.

Deliberately not a rewrite of CI notification. GitHub already emails on a
failed run; what was missing is a *tracked, prioritized* artifact the queue can
see, so `myorchestrator next` puts the breakage ahead of feature work instead
of a human having to notice and label it.

    python -m myfleet.red_main                # dry run: report only
    python -m myfleet.red_main --execute      # file/close for real
    python -m myfleet.red_main --repos my-fleet my-coder
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

from myguard import Guard
from mythings.labels import escalate
from mythings.ledger import Ledger
from mythings.policy import Action, Decision

from myfleet.workspace import fleet_root, ledger_path

ORG = "MyThingsLab"
TOOL = "red_main"
KIND = "watch"

WORKSPACE_ROOT = fleet_root(__file__)
DEFAULT_LEDGER = ledger_path(WORKSPACE_ROOT)

# Marker label, same trick as mytester's `test-driven`: it makes "is there
# already an open issue for this" a label query rather than title matching,
# which would break the moment someone edits the title.
MARKER_LABEL = "red-main"
_MARKER_COLOR = "b60205"
_MARKER_DESCRIPTION = "main is failing CI, filed by myfleet.red_main"

# Written out rather than derived. `tools_manifest.json` carries no lane, and
# guessing one from the repo name is exactly the kind of inference that files a
# P0 against the wrong repo. Adding a repo here is a deliberate act, and the
# lane it maps to is the lane `escalate()` will promote on.
WATCHED: dict[str, str] = {
    "my-things-core": "lane:core",
    "my-fleet": "lane:kernel",
    "my-guard": "lane:kernel",
    "my-coder": "lane:kernel",
    "my-orchestrator": "lane:kernel",
    "my-tester": "lane:kernel",
    "my-reporter": "lane:kernel",
    "my-telegram-bot": "lane:kernel",
    "my-architect": "lane:kernel",
}


@dataclass(frozen=True)
class MainStatus:
    repo: str
    conclusion: str  # success | failure | cancelled | ... | none
    title: str = ""
    url: str = ""
    sha: str = ""

    @property
    def red(self) -> bool:
        # Only an outright failure counts. A cancelled or still-running main is
        # absence of evidence, and filing a P0 on it would train everyone to
        # ignore the label -- the same fail-closed reasoning fleet_dispatch
        # applies to a skipped required check, pointed the other way: there,
        # "not green" must not read as pass; here, "not green" must not read as
        # broken either.
        return self.conclusion == "failure"


@dataclass(frozen=True)
class Outcome:
    repo: str
    action: str  # filed | already-open | recovered | ok | denied | no-runs
    detail: str = ""


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    return proc.stdout


def main_status(repo: str, *, runner=_run) -> MainStatus:
    raw = runner(
        [
            "gh",
            "run",
            "list",
            "--repo",
            f"{ORG}/{repo}",
            "--branch",
            "main",
            "--limit",
            "1",
            "--json",
            "conclusion,displayTitle,url,headSha",
        ]
    )
    runs = json.loads(raw)
    if not runs:
        return MainStatus(repo=repo, conclusion="none")
    run = runs[0]
    return MainStatus(
        repo=repo,
        # An in-progress run reports conclusion null, not a string.
        conclusion=run.get("conclusion") or "in-progress",
        title=run.get("displayTitle", ""),
        url=run.get("url", ""),
        sha=(run.get("headSha") or "")[:7],
    )


def open_marker_issue(repo: str, *, runner=_run) -> int | None:
    raw = runner(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{ORG}/{repo}",
            "--label",
            MARKER_LABEL,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number",
        ]
    )
    issues = json.loads(raw)
    return issues[0]["number"] if issues else None


def issue_labels(lane: str) -> tuple[str, ...]:
    # The whole point of routing through escalate() rather than hardcoding
    # prio:P0: the rule about what a core/kernel bug is worth lives in one
    # place, and this watcher inherits it instead of holding a second opinion.
    return escalate([lane, "kind:bug", "size:M", "state:ready", MARKER_LABEL]).labels


def _issue_body(status: MainStatus) -> str:
    return (
        f"`main` is failing CI on `{ORG}/{status.repo}`.\n\n"
        f"- Run: {status.url}\n"
        f"- Commit: `{status.sha}` — {status.title}\n\n"
        "Every worker dispatched against this repo inherits a broken base, so "
        "this outranks the backlog until it is green again.\n\n"
        f"Filed by `myfleet.red_main`. It closes this automatically once `main` "
        f"goes green; the `{MARKER_LABEL}` label is how it finds this issue again, "
        "so leave it on."
    )


def _ensure_marker_label(repo: str, *, runner=_run) -> None:
    runner(
        [
            "gh",
            "label",
            "create",
            MARKER_LABEL,
            "--color",
            _MARKER_COLOR,
            "--description",
            _MARKER_DESCRIPTION,
            "--force",
            "--repo",
            f"{ORG}/{repo}",
        ]
    )


def _file(status: MainStatus, lane: str, *, policy: Guard, runner=_run) -> Outcome:
    labels = issue_labels(lane)
    action = Action(
        kind="issue-create",
        payload={"repo": status.repo, "title": f"main is red on {status.repo}"},
    )
    if policy.evaluate(action).under(unattended=True) is not Decision.ALLOW:
        return Outcome(status.repo, "denied", "policy denied issue-create")

    _ensure_marker_label(status.repo, runner=runner)
    url = runner(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            f"{ORG}/{status.repo}",
            "--title",
            f"main is red on {status.repo}",
            "--body",
            _issue_body(status),
            *[arg for label in labels for arg in ("--label", label)],
        ]
    ).strip()
    return Outcome(status.repo, "filed", url.splitlines()[-1] if url else "")


def _close(status: MainStatus, number: int, *, policy: Guard, runner=_run) -> Outcome:
    # `issue-close` is not one of MyGuard's routine kinds, so it answers ASK:
    # with the channel live that is a tap on the operator's phone, and with no
    # channel it collapses to DENY and the issue simply stays open.
    action = Action(kind="issue-close", payload={"repo": status.repo, "issue": number})
    if policy.evaluate(action).under(unattended=True) is not Decision.ALLOW:
        # Not a failure: main is green either way, and an issue left open is
        # visible rather than silent. Reported so the human can close it.
        return Outcome(status.repo, "denied", f"#{number} still open, policy denied issue-close")

    runner(
        [
            "gh",
            "issue",
            "close",
            str(number),
            "--repo",
            f"{ORG}/{status.repo}",
            "--comment",
            f"`main` is green again at `{status.sha}` — {status.url}",
        ]
    )
    return Outcome(status.repo, "recovered", f"closed #{number}")


def sweep(
    watched: dict[str, str],
    *,
    policy: Guard,
    execute: bool,
    runner=_run,
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for repo, lane in sorted(watched.items()):
        status = main_status(repo, runner=runner)
        if status.conclusion == "none":
            outcomes.append(Outcome(repo, "no-runs", "no CI run on main yet"))
            continue

        existing = open_marker_issue(repo, runner=runner)
        if status.red and existing is not None:
            outcomes.append(Outcome(repo, "already-open", f"#{existing}"))
        elif status.red:
            if not execute:
                outcomes.append(Outcome(repo, "filed", f"[dry run] {status.sha} {status.title}"))
            else:
                outcomes.append(_file(status, lane, policy=policy, runner=runner))
        elif existing is not None:
            if not execute:
                outcomes.append(Outcome(repo, "recovered", f"[dry run] would close #{existing}"))
            else:
                outcomes.append(_close(status, existing, policy=policy, runner=runner))
        else:
            outcomes.append(Outcome(repo, "ok", status.conclusion))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--execute", action="store_true", help="file/close for real")
    parser.add_argument("--repos", nargs="+", help="subset of the watched repos")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    args = parser.parse_args(argv)

    watched = WATCHED
    if args.repos:
        unknown = [r for r in args.repos if r not in WATCHED]
        if unknown:
            raise SystemExit(f"not watched: {', '.join(unknown)} (add them to WATCHED)")
        watched = {r: WATCHED[r] for r in args.repos}

    outcomes = sweep(watched, policy=Guard(), execute=args.execute, runner=_run)

    ledger = Ledger(args.ledger)
    for outcome in outcomes:
        print(f"{outcome.repo:<18} {outcome.action:<13} {outcome.detail}")
        if outcome.action in ("ok", "no-runs"):
            continue
        ledger.record(
            tool=TOOL,
            kind=KIND,
            outcome=outcome.action,
            detail=outcome.detail,
            target=outcome.repo,
        )

    red = [o for o in outcomes if o.action in ("filed", "already-open")]
    if red:
        print(f"\n{len(red)} kernel repo(s) with a red main", file=sys.stderr)
    # Non-zero on a red main so a systemd unit's OnFailure= can alert on it,
    # matching heartbeat.py. A denied close is not a failure -- main is green.
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
