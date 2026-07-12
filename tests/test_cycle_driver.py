from __future__ import annotations

from pathlib import Path

import pytest

import myfleet.cycle_driver as cd
from myfleet.cycle_driver import Stage, run_cycle, run_stage


def _recorder():
    calls: list[list[str]] = []

    def runner(argv, *, cwd, env=None, check=False):
        calls.append(list(argv))
        return 0

    return calls, runner


def test_read_only_stage_always_runs(tmp_path: Path) -> None:
    calls, runner = _recorder()
    stage = Stage("read", ["tool", "due"], mutating=False)
    run_stage(stage, execute=False, cwd=tmp_path, runner=runner)
    assert calls == [["tool", "due"]]  # ran despite execute=False


def test_mutating_stage_is_dry_run_without_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, runner = _recorder()
    run_stage(Stage("write", ["tool", "build"]), execute=False, cwd=tmp_path, runner=runner)
    assert calls == []  # not run
    assert "would run: tool build" in capsys.readouterr().out


def test_mutating_stage_runs_under_execute(tmp_path: Path) -> None:
    calls, runner = _recorder()
    run_stage(Stage("write", ["tool", "build"]), execute=True, cwd=tmp_path, runner=runner)
    assert calls == [["tool", "build"]]


def test_skipped_stage_is_announced_and_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls, runner = _recorder()
    run_stage(Stage("x", ["tool"], skip="no corpus"), execute=True, cwd=tmp_path, runner=runner)
    assert calls == []
    assert "skipping x — no corpus" in capsys.readouterr().out


def test_run_cycle_runs_all_and_returns_worst_rc(tmp_path: Path) -> None:
    seen: list[str] = []

    def runner(argv, *, cwd, env=None, check=False):
        seen.append(argv[0])
        return 2 if argv[0] == "b" else 0

    stages = [Stage("a", ["a"]), Stage("b", ["b"]), Stage("c", ["c"])]
    worst = run_cycle(stages, execute=True, cwd=tmp_path, runner=runner)
    assert seen == ["a", "b", "c"]  # a failure in b does not abort c
    assert worst == 2


def test_run_command_reports_and_returns_code(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    rc = cd.run_command(["true"], cwd=tmp_path)
    assert rc == 0
    assert "$ true" in capsys.readouterr().out
