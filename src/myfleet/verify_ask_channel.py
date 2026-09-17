#!/usr/bin/env python3
"""Prove #69: does an armed ASK channel actually stop myplanner/myprojector failing closed?

Both fail closed in unattended CI for want of an ASK channel: a Policy ASK
with nobody listening collapses to DENY (README.md, "Asking a human"). MyGuard
reads MYTHINGS_ASK_CMD itself -- a bare `Guard()` anywhere in the fleet honours
it once it's armed -- so there is nothing tool-specific for myplanner or
myprojector to wire once MyThingsLab/my-guard#10 has landed. This script is the
proof case itself, not a simulation of it: it runs each tool's real production
command once unarmed (today's known fail-closed baseline) and once with a live
channel armed, and reports whether arming it actually flips the outcome.

Needs both tools installed, a live `mytelegrambot run` daemon reachable from
here, and a human ready to tap Allow when the armed pass asks -- there is no
way to press through a real ASK without one. Run it by hand on the Pi:

    python -m myfleet.verify_ask_channel \
      --org MyThingsLab --tracking-repo MyThingsLab/my-fleet \
      --tracking-issue 121 --project-number 1
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

import myfleet.fleet_ask as fleet_ask
from myfleet.workspace import fleet_root

WORKSPACE_ROOT = fleet_root(__file__)

# Stripped from the baseline run so a shell that already has the channel armed
# (e.g. re-running this by hand right after a real cycle) still exercises the
# unarmed case it's meant to be the control for.
_ASK_ENV_KEYS = ("MYTHINGS_ASK_CMD", "MYTHINGS_ASK_TIMEOUT")

Runner = Callable[[list[str], dict[str, str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=WORKSPACE_ROOT, env=env, capture_output=True, text=True)


@dataclass(frozen=True)
class ToolCheck:
    name: str
    argv: list[str]


def planner_check(
    *, org: str, tracking_repo: str, tracking_issue: str, engine: str
) -> ToolCheck:
    return ToolCheck(
        "myplanner",
        [
            "myplanner", "plan",
            "--org", org,
            "--repo-root", str(WORKSPACE_ROOT),
            "--tracking-repo", tracking_repo,
            "--tracking-issue", tracking_issue,
            "--engine", engine,
        ],
    )


def projector_check(
    *, org: str, project_number: str, tracking_repo: str, tracking_issue: str, engine: str
) -> ToolCheck:
    return ToolCheck(
        "myprojector",
        [
            "myprojector", "sync",
            "--org", org,
            "--project-number", project_number,
            "--tracking-repo", tracking_repo,
            "--tracking-issue", tracking_issue,
            "--engine", engine,
            "--apply-checklist",
        ],
    )


@dataclass(frozen=True)
class Verdict:
    tool: str
    baseline_denied: bool
    armed_ok: bool

    @property
    def outcome(self) -> str:
        if self.baseline_denied and self.armed_ok:
            return "CONFIRMED: the armed channel unblocks it"
        if not self.baseline_denied:
            return "INCONCLUSIVE: nothing needed asking on the unarmed run"
        return "STILL FAILING CLOSED: the diagnosis was wrong, revisit #69's premise"


def run_check(
    check: ToolCheck,
    *,
    runner: Runner,
    remote_daemon: bool = False,
    timeout: int = fleet_ask.DEFAULT_ASK_TIMEOUT,
) -> Verdict:
    baseline_env = {k: v for k, v in os.environ.items() if k not in _ASK_ENV_KEYS}
    baseline_env["GITHUB_ACTIONS"] = "true"
    baseline = runner(check.argv, baseline_env)

    armed_env = dict(os.environ)
    wiring = fleet_ask.enable(env=armed_env, remote_daemon=remote_daemon, timeout=timeout)
    print(f"{check.name}: ask channel armed -> {wiring['MYTHINGS_ASK_CMD']}")
    armed = runner(check.argv, armed_env)

    return Verdict(
        tool=check.name,
        baseline_denied=baseline.returncode != 0,
        armed_ok=armed.returncode == 0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--org", default="MyThingsLab")
    parser.add_argument("--tracking-repo", required=True)
    parser.add_argument("--tracking-issue", required=True)
    parser.add_argument("--project-number", required=True)
    parser.add_argument("--engine", default="noop")
    parser.add_argument(
        "--ask-remote-daemon",
        action="store_true",
        help="skip the local daemon check: it runs on another host sharing the ledger",
    )
    parser.add_argument("--ask-timeout", type=int, default=fleet_ask.DEFAULT_ASK_TIMEOUT)
    args = parser.parse_args(argv)

    checks = [
        planner_check(
            org=args.org,
            tracking_repo=args.tracking_repo,
            tracking_issue=args.tracking_issue,
            engine=args.engine,
        ),
        projector_check(
            org=args.org,
            project_number=args.project_number,
            tracking_repo=args.tracking_repo,
            tracking_issue=args.tracking_issue,
            engine=args.engine,
        ),
    ]

    verdicts: list[Verdict] = []
    for check in checks:
        try:
            verdicts.append(
                run_check(
                    check,
                    runner=_default_runner,
                    remote_daemon=args.ask_remote_daemon,
                    timeout=args.ask_timeout,
                )
            )
        except fleet_ask.AskChannelUnavailable as exc:
            print(f"{check.name}: cannot arm the ask channel: {exc}", file=sys.stderr)
            return 2

    print()
    for v in verdicts:
        print(f"{v.tool}: {v.outcome}")

    return 0 if all(v.baseline_denied and v.armed_ok for v in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
