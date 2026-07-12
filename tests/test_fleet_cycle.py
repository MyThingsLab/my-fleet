from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import myfleet.fleet_cycle as fc


@pytest.fixture(autouse=True)
def _no_halt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Keep every test hermetic: never read the real HALT marker and never
    # shell out to `gh search issues` for the critical-issue gate. Tests that
    # exercise the gate override these explicitly.
    monkeypatch.setattr(fc, "HALT_MARKER", tmp_path / "HALT")
    monkeypatch.setattr(fc, "_critical_halt_issues", lambda org: [])


def test_select_brief_issues_skips_issues_with_pending_brief_pr() -> None:
    picked = fc._select_brief_issues(
        [4, 5, 6, 11],
        ["my-researcher/11", "my-researcher/5", "feat/unrelated"],
        count=2,
    )
    assert picked == [4, 6]


def test_select_brief_issues_caps_at_count_oldest_first() -> None:
    assert fc._select_brief_issues([9, 7, 8], [], count=2) == [7, 8]


def test_select_brief_issues_ignores_non_research_branches() -> None:
    assert fc._select_brief_issues([3], ["my-researcher/study-plan", "fix/3"], count=1) == [3]


def test_select_brief_issues_empty_when_all_pending() -> None:
    assert fc._select_brief_issues([2], ["my-researcher/2"], count=1) == []


def _capture_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict | None]]:
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd: list[str], *, check: bool = False, env: dict | None = None) -> int:
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(fc, "_run", fake_run)
    return calls


def test_main_dry_run_does_not_invoke_myresearcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "STUDY_ROOT", tmp_path)
    monkeypatch.setattr(fc, "_brief_candidates", lambda count: [4])
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch"])
    assert not any(cmd[0] == "myresearcher" for cmd, _ in calls)


def test_main_execute_briefs_candidates_under_first_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "STUDY_ROOT", tmp_path)
    monkeypatch.setattr(fc, "_brief_candidates", lambda count: [4, 5][:count])
    fc.main([
        "--accounts", "/tmp/acct1,/tmp/acct2",
        "--skip-dispatch", "--execute",
        "--engine", "claude-cli",
        "--brief-count", "2",
    ])
    briefs = [(cmd, env) for cmd, env in calls if cmd[0] == "myresearcher"]
    assert [cmd[cmd.index("--issue") + 1] for cmd, _ in briefs] == ["4", "5"]
    for cmd, env in briefs:
        assert cmd[1] == "brief"
        assert cmd[cmd.index("--repo") + 1] == "MyThingsLab/study"
        assert cmd[cmd.index("--repo-root") + 1] == str(tmp_path)
        assert cmd[cmd.index("--engine") + 1] == "claude-cli"
        assert cmd[cmd.index("--sources") + 1] == "arxiv"
        assert env is not None and env["CLAUDE_CONFIG_DIR"] == "/tmp/acct1"


def test_main_brief_count_zero_never_queries_github(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_runs(monkeypatch)

    def boom(count: int) -> list[int]:
        raise AssertionError("should not query GitHub when --brief-count 0")

    monkeypatch.setattr(fc, "_brief_candidates", boom)
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--brief-count", "0", "--execute"])
    assert not any(cmd[0] == "myresearcher" for cmd, _ in calls)


def test_main_skips_briefs_when_study_clone_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "STUDY_ROOT", tmp_path / "missing")
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--execute"])
    assert not any(cmd[0] == "myresearcher" for cmd, _ in calls)
    assert "no study clone" in capsys.readouterr().out


def test_main_execute_runs_mydashboard_render_after_mydocs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "WORKSPACE_ROOT", tmp_path)
    docs_site_root = tmp_path / fc.DOCS_SITE_CLONE
    docs_site_root.mkdir()
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--execute", "--brief-count", "0"])
    tools = [cmd[0] for cmd, _ in calls]
    assert "mydashboard" in tools
    assert tools.index("mydocs") < tools.index("mydashboard") < tools.index("myprojector")
    dashboard_cmd, _ = next((cmd, env) for cmd, env in calls if cmd[0] == "mydashboard")
    assert dashboard_cmd[1] == "render"
    assert dashboard_cmd[dashboard_cmd.index("--repo-root") + 1] == str(docs_site_root)
    assert dashboard_cmd[dashboard_cmd.index("--workspace") + 1] == str(tmp_path)


def test_main_skips_mydashboard_when_docs_site_clone_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(fc, "WORKSPACE_ROOT", tmp_path)
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--execute", "--brief-count", "0"])
    assert not any(cmd[0] == "mydashboard" for cmd, _ in calls)
    assert "skipping mydashboard" in capsys.readouterr().out


def test_main_forwards_allow_personal_token_to_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_runs(monkeypatch)
    fc.main([
        "--accounts", "/tmp/acct",
        "--dispatch-execute", "--allow-personal-token", "--brief-count", "0",
    ])
    (dispatch_cmd,) = [cmd for cmd, _ in calls if any("myfleet.fleet_dispatch" in c for c in cmd)]
    assert "--allow-personal-token" in dispatch_cmd
    assert "--execute" in dispatch_cmd


def test_main_does_not_forward_allow_personal_token_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_runs(monkeypatch)
    fc.main(["--accounts", "/tmp/acct", "--brief-count", "0"])
    (dispatch_cmd,) = [cmd for cmd, _ in calls if any("myfleet.fleet_dispatch" in c for c in cmd)]
    assert "--allow-personal-token" not in dispatch_cmd


def test_execute_cycle_refuses_when_halt_marker_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    marker = tmp_path / "HALT"
    marker.write_text("halted\n")
    monkeypatch.setattr(fc, "HALT_MARKER", marker)
    fc.main(["--accounts", "/tmp/acct", "--execute", "--skip-dispatch", "--brief-count", "0"])
    assert calls == []
    assert "HALT marker present" in capsys.readouterr().out


def test_dispatch_execute_alone_is_also_gated_by_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    marker = tmp_path / "HALT"
    marker.write_text("halted\n")
    monkeypatch.setattr(fc, "HALT_MARKER", marker)
    fc.main(["--accounts", "/tmp/acct", "--dispatch-execute", "--brief-count", "0"])
    assert calls == []
    assert "cycle halted" in capsys.readouterr().out


def test_execute_cycle_refuses_when_critical_issue_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _capture_runs(monkeypatch)
    monkeypatch.setattr(
        fc,
        "_critical_halt_issues",
        lambda org: [
            {
                "repository": {"nameWithOwner": "MyThingsLab/my-guard"},
                "number": 9,
                "title": "broken invariant",
                "url": "https://github.com/MyThingsLab/my-guard/issues/9",
            }
        ],
    )
    fc.main(["--accounts", "/tmp/acct", "--execute", "--skip-dispatch", "--brief-count", "0"])
    assert calls == []
    assert "my-guard#9" in capsys.readouterr().out


def test_dry_run_is_not_gated_by_halt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_runs(monkeypatch)
    marker = tmp_path / "HALT"
    marker.write_text("halted\n")
    monkeypatch.setattr(fc, "HALT_MARKER", marker)
    fc.main(["--accounts", "/tmp/acct", "--skip-dispatch", "--brief-count", "0"])
    assert any(cmd[0] == "myplanner" for cmd, _ in calls)


def test_gh_json_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    assert fc._gh_json(["issue", "list"]) is None


# ---- --loop ----------------------------------------------------------------


class _FakeClock:
    """time.monotonic()/time.sleep() stand-in: sleep advances the fake clock
    instead of actually blocking, so loop tests run instantly."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _loop_ns(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = dict(
        accounts="/tmp/acct1,/tmp/acct2",
        execute=False,
        dispatch_execute=False,
        engine="noop",
        skip_dispatch=False,
        brief_count=0,
        loop=True,
        max_duration_min=None,
        max_cycle_budget_usd=None,
        account_recheck_min=10.0,
        max_session_pct=90,
        idle_backoff_min=1.0,
        max_backoff_min=30.0,
        ask_human=False,
        ask_remote_daemon=False,
        ask_timeout=300,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _usable(pool: list[str]) -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
    return [SimpleNamespace(config_dir=d) for d in pool], []


def test_loop_should_stop_none_when_no_caps() -> None:
    assert fc._loop_should_stop(
        elapsed_min=999, spent_usd=999, max_duration_min=None, max_cycle_budget_usd=None
    ) is None


def test_loop_should_stop_on_duration_cap() -> None:
    reason = fc._loop_should_stop(elapsed_min=10, spent_usd=0, max_duration_min=5, max_cycle_budget_usd=None)
    assert reason is not None and "max-duration-min" in reason


def test_loop_should_stop_on_budget_cap() -> None:
    reason = fc._loop_should_stop(elapsed_min=0, spent_usd=5, max_duration_min=None, max_cycle_budget_usd=3)
    assert reason is not None and "max-cycle-budget-usd" in reason


def test_next_backoff_resets_to_idle_on_dispatch() -> None:
    assert fc._next_backoff_s(240.0, dispatched=True, idle_backoff_s=60.0, max_backoff_s=1800.0) == 60.0


def test_next_backoff_doubles_and_caps_when_idle() -> None:
    assert fc._next_backoff_s(10.0, dispatched=False, idle_backoff_s=60.0, max_backoff_s=1800.0) == 20.0
    assert fc._next_backoff_s(1200.0, dispatched=False, idle_backoff_s=60.0, max_backoff_s=1800.0) == 1800.0


def test_run_loop_stops_at_max_duration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fc, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    clock = _FakeClock()
    monkeypatch.setattr(fc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(fc.time, "sleep", clock.sleep)
    monkeypatch.setattr(fc, "_run_cycle", lambda *a, **k: None)  # never dispatches -> always backs off
    monkeypatch.setattr(fc.account_usage, "select_accounts", lambda pool, pct: _usable(pool))

    result = fc._run_loop(_loop_ns(max_duration_min=5.0), "python3")
    assert result == 0
    assert clock.now >= 5.0 * 60.0


def test_run_loop_stops_at_budget_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(fc, "DISPATCH_LEDGER", ledger_path)
    ledger = fc.Ledger(ledger_path)

    def fake_run_cycle(args: argparse.Namespace, *, accounts: str, skip_dispatch: bool, py: str) -> None:
        ledger.record(tool="fleet_dispatch", kind="usage", outcome="success", detail="", cost_usd=2.0)

    monkeypatch.setattr(fc, "_run_cycle", fake_run_cycle)
    monkeypatch.setattr(fc.account_usage, "select_accounts", lambda pool, pct: _usable(pool))

    result = fc._run_loop(_loop_ns(max_cycle_budget_usd=3.0), "python3")
    assert result == 0
    spent = sum(e.data["cost_usd"] for e in ledger.read(tool="fleet_dispatch", kind="usage"))
    assert spent >= 3.0


def test_run_loop_skips_dispatch_when_no_usable_accounts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fc, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")

    class _StopLoop(Exception):
        pass

    calls = []

    def fake_run_cycle(args: argparse.Namespace, *, accounts: str, skip_dispatch: bool, py: str) -> None:
        calls.append((accounts, skip_dispatch))
        raise _StopLoop

    monkeypatch.setattr(fc, "_run_cycle", fake_run_cycle)
    over = SimpleNamespace(config_dir="/tmp/acct1")
    monkeypatch.setattr(fc.account_usage, "select_accounts", lambda pool, pct: ([], [over]))

    with pytest.raises(_StopLoop):
        fc._run_loop(_loop_ns(), "python3")

    assert calls == [("/tmp/acct1,/tmp/acct2", True)]


def test_run_loop_rechecks_accounts_on_cadence_not_every_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fc, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    clock = _FakeClock()
    monkeypatch.setattr(fc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(fc.time, "sleep", clock.sleep)
    monkeypatch.setattr(fc, "_run_cycle", lambda *a, **k: None)

    calls: list[list[str]] = []

    def fake_select(pool: list[str], pct: int) -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
        calls.append(pool)
        return _usable(pool)

    monkeypatch.setattr(fc.account_usage, "select_accounts", fake_select)

    fc._run_loop(
        _loop_ns(max_duration_min=1.0, idle_backoff_min=0.1, max_backoff_min=0.1, account_recheck_min=10.0),
        "python3",
    )
    # 1 min of wall time at a 0.1 min (6s) fixed backoff is ~10 iterations, but
    # the 10-min recheck cadence never elapses within that -- one poll, not ten.
    assert len(calls) == 1


def _ask_loop_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fc, "DISPATCH_LEDGER", tmp_path / "ledger.jsonl")
    clock = _FakeClock()
    monkeypatch.setattr(fc.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(fc.time, "sleep", clock.sleep)
    monkeypatch.setattr(fc, "_run_cycle", lambda *a, **k: None)
    monkeypatch.setattr(fc.account_usage, "select_accounts", lambda pool, pct: _usable(pool))


def test_loop_disarms_ask_channel_once_when_daemon_dies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ask_loop_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MYTHINGS_ASK_CMD", "mytelegrambot ask --ledger x --timeout 300")
    monkeypatch.setenv("MYTHINGS_ASK_TIMEOUT", "330")
    monkeypatch.setattr(fc.fleet_ask, "daemon_is_running", lambda: False)

    # ~3 iterations at the 60s idle backoff before the 2.5 min duration cap.
    fc._run_loop(_loop_ns(ask_human=True, max_duration_min=2.5), "python3")

    assert "MYTHINGS_ASK_CMD" not in os.environ
    assert "MYTHINGS_ASK_TIMEOUT" not in os.environ
    assert "channel disarmed" in capsys.readouterr().out
    ledger = fc.Ledger(tmp_path / "ledger.jsonl")
    disarms = [e for e in ledger.read(tool="fleet_cycle", kind="ask_channel")]
    assert len(disarms) == 1  # once per transition, not once per iteration
    assert disarms[0].outcome == "disarmed"


def test_loop_rearms_ask_channel_when_daemon_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _ask_loop_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MYTHINGS_ASK_CMD", "mytelegrambot ask --ledger x --timeout 300")
    liveness = iter([False, True, True])
    monkeypatch.setattr(fc.fleet_ask, "daemon_is_running", lambda: next(liveness))

    def fake_enable(*, timeout: int, remote_daemon: bool = False) -> dict[str, str]:
        os.environ["MYTHINGS_ASK_CMD"] = "mytelegrambot ask --ledger x --timeout 300"
        return {"MYTHINGS_ASK_CMD": os.environ["MYTHINGS_ASK_CMD"]}

    monkeypatch.setattr(fc.fleet_ask, "enable", fake_enable)

    fc._run_loop(_loop_ns(ask_human=True, max_duration_min=2.5), "python3")

    assert os.environ.get("MYTHINGS_ASK_CMD")
    out = capsys.readouterr().out
    assert "channel disarmed" in out and "re-armed" in out
    ledger = fc.Ledger(tmp_path / "ledger.jsonl")
    outcomes = [e.outcome for e in ledger.read(tool="fleet_cycle", kind="ask_channel")]
    assert outcomes == ["disarmed", "rearmed"]


def test_loop_never_probes_daemon_without_ask_human(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ask_loop_env(monkeypatch, tmp_path)

    def boom() -> bool:
        raise AssertionError("daemon liveness must not be probed when --ask-human is off")

    monkeypatch.setattr(fc.fleet_ask, "daemon_is_running", boom)
    fc._run_loop(_loop_ns(max_duration_min=1.0), "python3")


def test_main_loop_flag_dispatches_to_run_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_loop(args: argparse.Namespace, py: str) -> int:
        captured["accounts"] = args.accounts
        captured["max_duration_min"] = args.max_duration_min
        return 0

    monkeypatch.setattr(fc, "_run_loop", fake_run_loop)
    rc = fc.main(["--accounts", "/tmp/acct", "--loop", "--max-duration-min", "5"])
    assert rc == 0
    assert captured == {"accounts": "/tmp/acct", "max_duration_min": 5.0}
