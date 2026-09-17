from __future__ import annotations

from pathlib import Path

import pytest

import myfleet.coding_cycle as cc


def test_parse_args_defaults() -> None:
    args = cc.parse_args([])
    assert args.org == "MyThingsLab"
    assert args.engine == "claude-cli"
    assert args.execute is False
    assert args.dispatch_execute is False
    assert args.loop is False
    assert args.goal is None


def test_parse_args_with_options() -> None:
    args = cc.parse_args(
        [
            "--org",
            "CustomOrg",
            "--engine",
            "gemini-cli",
            "--goal",
            "goal/cad-foundation",
            "--execute",
            "--dispatch-execute",
            "--accounts",
            "/tmp/acct1,/tmp/acct2",
        ]
    )
    assert args.org == "CustomOrg"
    assert args.engine == "gemini-cli"
    assert args.goal == "goal/cad-foundation"
    assert args.execute is True
    assert args.dispatch_execute is True
    assert args.accounts == "/tmp/acct1,/tmp/acct2"


def test_build_coding_stages_creates_expected_tool_sequence(tmp_path: Path) -> None:
    # Create fake repo folders so skip is None
    for name in ("my-planner", "my-architect", "my-tester", "my-changelogger", "my-reporter"):
        (tmp_path / name).mkdir()

    args = cc.parse_args(
        [
            "--goal",
            "goal/cad-foundation",
            "--accounts",
            "/tmp/acct",
            "--execute",
            "--dispatch-execute",
        ]
    )
    stages = cc.build_coding_stages(args, repo_root=tmp_path)

    stage_names = [s.name for s in stages]
    assert stage_names == [
        "myplanner plan",
        "myarchitect decompose",
        "fleet_dispatch (my-coder)",
        "mytester run",
        "merge_ready_prs",
        "mychangelogger update",
        "myreporter post",
    ]

    # Verify goal propagation
    planner = next(s for s in stages if s.name == "myplanner plan")
    assert "--goal" in planner.argv
    assert "goal/cad-foundation" in planner.argv

    dispatch = next(s for s in stages if s.name == "fleet_dispatch (my-coder)")
    assert "--goal" in dispatch.argv
    assert "--accounts" in dispatch.argv
    assert "--execute" in dispatch.argv
    assert dispatch.mutating is True


def test_main_runs_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: list[list[str]] = []

    def fake_run_cycle(stages, *, execute: bool, cwd: Path, runner=None) -> int:
        captured.append([s.name for s in stages])
        return 0

    monkeypatch.setattr(cc, "run_cycle", fake_run_cycle)

    rc = cc.main(["--repo-root", str(tmp_path)])
    assert rc == 0
    assert len(captured) == 1
    assert "merge_ready_prs" in captured[0]
