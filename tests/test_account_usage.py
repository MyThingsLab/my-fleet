from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import myfleet.account_usage as au


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], returncode, stdout=stdout, stderr="")


def _usage_json(result: str) -> str:
    return json.dumps({"result": result})


def test_select_accounts_reports_healthy_account(tmp_path: Path, monkeypatch) -> None:
    good = tmp_path / "good"
    good.mkdir()

    def fake_run(cmd, **kwargs):
        return _completed(0, _usage_json("Current session: 42% used · resets in 3h\n"))

    monkeypatch.setattr(au.subprocess, "run", fake_run)

    usable, over = au.select_accounts([str(good)])

    assert [u.config_dir for u in usable] == [str(good)]
    assert over == []
    assert usable[0].session_pct == 42
    assert usable[0].error == ""


def test_select_accounts_treats_probe_failure_as_unusable_not_a_crash(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression test: one account's probe failing (e.g. a transient
    # `claude -p "/usage"` crash) must not raise out of select_accounts --
    # run_fleet_cycle.sh's fallback depends on this degrading gracefully
    # (excluding just the broken account) instead of killing the whole
    # fleet cycle when it runs under `set -euo pipefail`.
    healthy = tmp_path / "healthy"
    healthy.mkdir()
    broken = tmp_path / "broken"
    broken.mkdir()

    def fake_run(cmd, **kwargs):
        config_dir = kwargs["env"]["CLAUDE_CONFIG_DIR"]
        if config_dir == str(broken):
            return _completed(1, "")
        return _completed(0, _usage_json("Current session: 10% used · resets in 5h\n"))

    monkeypatch.setattr(au.subprocess, "run", fake_run)

    usable, over = au.select_accounts([str(healthy), str(broken)])

    assert [u.config_dir for u in usable] == [str(healthy)]
    assert [u.config_dir for u in over] == [str(broken)]
    assert over[0].error != ""
    assert over[0].session_pct == 100


def test_select_accounts_missing_config_dir_is_unusable_not_a_crash(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    usable, over = au.select_accounts([str(missing)])

    assert usable == []
    assert len(over) == 1
    assert "does not exist" in over[0].error


def test_check_account_raises_when_session_pct_missing(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "acct"
    config_dir.mkdir()

    def fake_run(cmd, **kwargs):
        return _completed(0, _usage_json("no usage info in this reply\n"))

    monkeypatch.setattr(au.subprocess, "run", fake_run)

    with pytest.raises(au.UsageCheckError):
        au.check_account(str(config_dir))


def test_main_stdout_is_only_ever_the_usable_csv_line(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression test: run_fleet_cycle.sh captures stdout via
    # `USABLE="$(python3 -m myfleet.account_usage ...)"`. Diagnostic [ERROR]/
    # [OVER]/[ok] lines used to share stdout with the CSV result, so which
    # line survived a `| tail -1` depended on command-substitution's trailing-
    # newline stripping. They now go to stderr so stdout is unambiguous.
    good = tmp_path / "good"
    good.mkdir()
    broken = tmp_path / "broken"
    broken.mkdir()

    def fake_run(cmd, **kwargs):
        config_dir = kwargs["env"]["CLAUDE_CONFIG_DIR"]
        if config_dir == str(broken):
            return _completed(1, "")
        return _completed(0, _usage_json("Current session: 10% used · resets in 5h\n"))

    monkeypatch.setattr(au.subprocess, "run", fake_run)

    rc = au.main(["--accounts", f"{good},{broken}"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == f"{good}\n"
    assert "[ERROR]" in captured.err


def test_main_stdout_is_empty_when_every_account_is_unusable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist"

    rc = au.main(["--accounts", str(missing)])
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == "\n"
    assert "[ERROR]" in captured.err
