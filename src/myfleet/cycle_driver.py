#!/usr/bin/env python3
"""The shared machinery both fleet cycles are built on.

`fleet_cycle.py` chains the build-loop's tools; `study_cycle.py` chains the
learn-loop's. Both do the same thing structurally: run a sequence of tool CLIs in
an authority order, each as its own `gh`/ledger-attributed subprocess, dry-run by
default and only mutating under `--execute`. That structure lives here so neither
driver reimplements it.

A `Stage` is one tool invocation. `mutating` stages (an Engine call, a PR, a file
write) run only under `--execute` and otherwise print what they would do; a
read-only stage (`mutating=False`) always runs. A stage with a `skip` reason is
announced and skipped — the way a cycle handles a missing local checkout without
aborting the rest of the pass.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[..., int]


def run_command(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> int:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(list(cmd), cwd=cwd, env=env)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.returncode


@dataclass(frozen=True)
class Stage:
    name: str
    argv: list[str]
    mutating: bool = True  # billed / side-effecting: runs only under --execute
    skip: str | None = None  # a reason to skip this stage (printed), e.g. missing input
    env: dict[str, str] | None = None


def run_stage(
    stage: Stage,
    *,
    execute: bool,
    cwd: Path,
    runner: Runner = run_command,
) -> int:
    if stage.skip is not None:
        print(f"(skipping {stage.name} — {stage.skip})")
        return 0
    if stage.mutating and not execute:
        print(f"(dry run — would run: {' '.join(stage.argv)})")
        return 0
    return runner(stage.argv, cwd=cwd, env=stage.env)


def run_cycle(
    stages: Sequence[Stage],
    *,
    execute: bool,
    cwd: Path,
    runner: Runner = run_command,
) -> int:
    # Best-effort: every stage runs even if an earlier one failed (a soft failure
    # in one tool should not abort the pass), and the worst return code is
    # surfaced so a caller/loop can tell whether anything went wrong.
    worst = 0
    for stage in stages:
        rc = run_stage(stage, execute=execute, cwd=cwd, runner=runner)
        worst = worst or rc
    return worst
