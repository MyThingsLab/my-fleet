from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import myfleet.fleet_ask as fleet_ask
import myfleet.verify_ask_channel as vac


def _completed(returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _fake_enable(*, env: dict, remote_daemon: bool = False, timeout: int = 0) -> dict:
    env["MYTHINGS_ASK_CMD"] = "fake-ask --ledger x --timeout 1"
    return {"MYTHINGS_ASK_CMD": env["MYTHINGS_ASK_CMD"]}


def test_planner_check_builds_the_same_argv_shape_fleet_cycle_uses() -> None:
    check = vac.planner_check(
        org="MyThingsLab", tracking_repo="MyThingsLab/my-fleet", tracking_issue="121",
        engine="noop",
    )
    assert check.argv[:2] == ["myplanner", "plan"]
    assert "--tracking-issue" in check.argv and "121" in check.argv


def test_projector_check_always_applies_the_checklist() -> None:
    check = vac.projector_check(
        org="MyThingsLab", project_number="1", tracking_repo="MyThingsLab/my-fleet",
        tracking_issue="121", engine="noop",
    )
    assert check.argv[:2] == ["myprojector", "sync"]
    assert "--apply-checklist" in check.argv


def test_run_check_confirms_when_baseline_denies_and_armed_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vac.fleet_ask, "enable", _fake_enable)
    calls: list[dict[str, str]] = []

    def runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append(env)
        return _completed(1 if "MYTHINGS_ASK_CMD" not in env else 0)

    check = vac.ToolCheck("myplanner", ["myplanner", "plan"])
    verdict = vac.run_check(check, runner=runner)

    assert verdict.baseline_denied is True
    assert verdict.armed_ok is True
    assert "CONFIRMED" in verdict.outcome
    # The baseline run must never see a channel, even if the ambient shell had one armed.
    assert "MYTHINGS_ASK_CMD" not in calls[0]
    assert calls[0]["GITHUB_ACTIONS"] == "true"
    assert calls[1]["MYTHINGS_ASK_CMD"] == "fake-ask --ledger x --timeout 1"


def test_run_check_flags_inconclusive_when_baseline_already_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vac.fleet_ask, "enable", _fake_enable)
    runner = lambda argv, env: _completed(0)  # noqa: E731

    verdict = vac.run_check(vac.ToolCheck("myplanner", ["myplanner", "plan"]), runner=runner)

    assert verdict.baseline_denied is False
    assert "INCONCLUSIVE" in verdict.outcome


def test_run_check_flags_still_failing_closed_when_armed_run_also_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vac.fleet_ask, "enable", _fake_enable)
    runner = lambda argv, env: _completed(1)  # noqa: E731

    verdict = vac.run_check(vac.ToolCheck("myprojector", ["myprojector", "sync"]), runner=runner)

    assert verdict.baseline_denied is True
    assert verdict.armed_ok is False
    assert "STILL FAILING CLOSED" in verdict.outcome


def test_main_surfaces_ask_channel_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raising_enable(**kwargs):
        raise fleet_ask.AskChannelUnavailable("no daemon")

    monkeypatch.setattr(vac.fleet_ask, "enable", raising_enable)
    monkeypatch.setattr(vac, "_default_runner", lambda argv, env: _completed(1))

    code = vac.main(
        [
            "--tracking-repo", "MyThingsLab/my-fleet",
            "--tracking-issue", "121",
            "--project-number", "1",
        ]
    )

    assert code == 2
    assert "cannot arm the ask channel" in capsys.readouterr().err


def test_main_exit_code_reflects_every_verdict_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vac.fleet_ask, "enable", _fake_enable)

    def fake_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return _completed(1 if "MYTHINGS_ASK_CMD" not in env else 0)

    monkeypatch.setattr(vac, "_default_runner", fake_runner)

    code = vac.main(
        [
            "--tracking-repo", "MyThingsLab/my-fleet",
            "--tracking-issue", "121",
            "--project-number", "1",
        ]
    )

    assert code == 0


def test_main_exit_code_nonzero_when_a_tool_stays_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vac.fleet_ask, "enable", _fake_enable)
    monkeypatch.setattr(vac, "_default_runner", lambda argv, env: _completed(1))

    code = vac.main(
        [
            "--tracking-repo", "MyThingsLab/my-fleet",
            "--tracking-issue", "121",
            "--project-number", "1",
        ]
    )

    assert code == 1
