#!/usr/bin/env python3
"""Run one pass of the autonomous coding and TDD cycle.

Chains the engineering tools in authority and dependency order:
  1. myplanner plan          - refresh the recommended DAG sequence and priority ranking.
  2. myarchitect decompose   - decompose size:L candidates on the active milestone into S/M tasks.
  3. mysearcher context      - gather AST symbols and relevant files for candidate context packs.
  4. fleet_dispatch          - dispatch my-coder workers against unblocked ready leaf nodes in isolated worktrees.
  5. mytester run            - generate coverage for uncovered branches on touched repos.
  6. merge_ready_prs         - verify CI status and Policy(ALLOW) and merge passing PRs.
  7. mychangelogger update   - fold new ledger entries into CHANGELOG.md.
  8. myreporter post         - post tracking issue status digest.

Dry-run by default. Billed/mutating steps require --execute / --dispatch-execute.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from myfleet.cycle_driver import Stage, run_cycle

DEFAULT_ORG = "MyThingsLab"
DEFAULT_ENGINE = "claude-cli"


def build_coding_stages(
    args: argparse.Namespace,
    *,
    repo_root: Path,
) -> list[Stage]:
    stages: list[Stage] = []
    org = getattr(args, "org", DEFAULT_ORG)
    engine = getattr(args, "engine", DEFAULT_ENGINE)
    goal = getattr(args, "goal", None)

    # 1. MyPlanner: refresh DAG sequence
    planner_argv = ["myplanner", "plan", "--repo-root", str(repo_root), "--engine", engine]
    if goal:
        planner_argv.extend(["--goal", goal])
    stages.append(
        Stage(
            name="myplanner plan",
            argv=planner_argv,
            mutating=True,
            skip=None if (repo_root / "my-planner").exists() else "my-planner not found",
        )
    )

    # 2. MyArchitect: decompose large items
    arch_argv = ["myarchitect", "decompose", "--repo-root", str(repo_root), "--engine", engine]
    if goal:
        arch_argv.extend(["--goal", goal])
    stages.append(
        Stage(
            name="myarchitect decompose",
            argv=arch_argv,
            mutating=True,
            skip=None if (repo_root / "my-architect").exists() else "my-architect not found",
        )
    )

    # 3. Fleet Dispatch: dispatch my-coder against ready leaf nodes
    dispatch_argv = [
        sys.executable,
        "-m",
        "myfleet.fleet_dispatch",
        "--repo-root",
        str(repo_root),
        "--org",
        org,
    ]
    if getattr(args, "accounts", None):
        dispatch_argv.extend(["--accounts", args.accounts])
    if getattr(args, "dispatch_execute", False):
        dispatch_argv.append("--execute")
    if goal:
        dispatch_argv.extend(["--goal", goal])
    if getattr(args, "allow_personal_token", False):
        dispatch_argv.append("--allow-personal-token")

    stages.append(
        Stage(
            name="fleet_dispatch (my-coder)",
            argv=dispatch_argv,
            mutating=getattr(args, "dispatch_execute", False),
        )
    )

    # 4. MyTester: per-repo test coverage
    stages.append(
        Stage(
            name="mytester run",
            argv=["mytester", "run", "--repo-root", str(repo_root), "--engine", engine],
            mutating=True,
            skip=None if (repo_root / "my-tester").exists() else "my-tester not found",
        )
    )

    # 5. Merge ready PRs
    merge_argv = [
        sys.executable,
        "-m",
        "myfleet.merge_ready_prs",
        "--repo-root",
        str(repo_root),
        "--org",
        org,
    ]
    stages.append(
        Stage(
            name="merge_ready_prs",
            argv=merge_argv,
            mutating=True,
        )
    )

    # 6. MyChangelogger: update changelogs
    stages.append(
        Stage(
            name="mychangelogger update",
            argv=["mychangelogger", "update", "--repo-root", str(repo_root)],
            mutating=True,
            skip=None if (repo_root / "my-changelogger").exists() else "my-changelogger not found",
        )
    )

    # 7. MyReporter: digest and status report
    stages.append(
        Stage(
            name="myreporter post",
            argv=["myreporter", "post", "--repo-root", str(repo_root), "--org", org],
            mutating=True,
            skip=None if (repo_root / "my-reporter").exists() else "my-reporter not found",
        )
    )

    return stages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the autonomous MyThingsLab coding and TDD cycle."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Root directory containing tool checkouts (default: current dir).",
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"GitHub organization (default: {DEFAULT_ORG}).",
    )
    parser.add_argument(
        "--accounts",
        help="Comma-separated paths to Claude auth configs for worker dispatch.",
    )
    parser.add_argument(
        "--engine",
        default=DEFAULT_ENGINE,
        help=f"Engine to use for single-call tools (default: {DEFAULT_ENGINE}).",
    )
    parser.add_argument(
        "--goal",
        help="Constrain cycle execution to a specific goal/milestone.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run mutating non-dispatch subcommands for real.",
    )
    parser.add_argument(
        "--dispatch-execute",
        action="store_true",
        help="Allow fleet_dispatch to spawn billed claude -p worker sessions.",
    )
    parser.add_argument(
        "--allow-personal-token",
        action="store_true",
        help="Accept running workers on personal ambient gh token.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously in a loop with backoff.",
    )
    parser.add_argument(
        "--loop-delay-sec",
        type=int,
        default=60,
        help="Delay in seconds between loop iterations (default: 60).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()

    while True:
        stages = build_coding_stages(args, repo_root=repo_root)
        rc = run_cycle(stages, execute=args.execute, cwd=repo_root)
        if not args.loop:
            return rc
        print(f"\n[coding-cycle] Completed iteration (rc={rc}). Sleeping {args.loop_delay_sec}s...")
        time.sleep(args.loop_delay_sec)


if __name__ == "__main__":
    sys.exit(main())
