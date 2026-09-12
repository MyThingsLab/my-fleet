from __future__ import annotations

import json
import subprocess
from pathlib import Path

import myfleet.preflight as pf


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["claude"], returncode, stdout=stdout, stderr=stderr)


def _account(tmp_path: Path, name: str, *, trusted_for: Path | None = None) -> Path:
    config_dir = tmp_path / name
    config_dir.mkdir()
    projects = {}
    if trusted_for is not None:
        projects[str(trusted_for)] = {"hasTrustDialogAccepted": True}
    (config_dir / ".claude.json").write_text(json.dumps({"projects": projects}))
    return config_dir


def test_a_working_account_passes(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = _account(tmp_path, "good", trusted_for=workspace)
    monkeypatch.setattr(pf.subprocess, "run", lambda cmd, **kw: _completed(0, "ALIVE"))

    result = pf.check_account(str(config_dir), workspace)

    assert result.outcome == pf.OK
    assert result.usable
    assert result.authenticated and result.trusted


def test_expired_oauth_is_its_own_outcome_and_keeps_the_reason(tmp_path: Path, monkeypatch) -> None:
    # The exact shape observed on both worker accounts: exit 1, and the reason
    # on STDOUT rather than stderr. Reporting stderr alone loses it entirely,
    # which is how an expired login kept rendering as an idle fleet.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = _account(tmp_path, "expired", trusted_for=workspace)
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda cmd, **kw: _completed(
            1, stdout="Failed to authenticate: OAuth session expired and could not be refreshed"
        ),
    )

    result = pf.check_account(str(config_dir), workspace)

    assert result.outcome == pf.AUTH_EXPIRED
    assert not result.usable
    assert not result.authenticated
    assert "OAuth session expired" in result.detail


def test_authenticated_but_untrusted_workspace_is_blocked(tmp_path: Path, monkeypatch) -> None:
    # The account logs in fine; its permissions.allow entries are silently
    # dropped, so the worker fails later on a permission it was configured with.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = _account(tmp_path, "untrusted")
    monkeypatch.setattr(pf.subprocess, "run", lambda cmd, **kw: _completed(0, "ALIVE"))

    result = pf.check_account(str(config_dir), workspace)

    assert result.outcome == pf.WORKSPACE_UNTRUSTED
    assert result.authenticated and not result.trusted
    assert not result.usable


def test_a_reply_that_is_not_the_reply_fails(tmp_path: Path, monkeypatch) -> None:
    # Exit 0 is not evidence. The probe asserts on what came back.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = _account(tmp_path, "chatty", trusted_for=workspace)
    monkeypatch.setattr(
        pf.subprocess, "run", lambda cmd, **kw: _completed(0, "I'd be happy to help!")
    )

    result = pf.check_account(str(config_dir), workspace)

    assert result.outcome == pf.PROBE_FAILED
    assert "ALIVE" in result.detail


def test_missing_config_dir_never_spawns_a_probe(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def explode(cmd, **kw):
        raise AssertionError("should not have run claude")

    monkeypatch.setattr(pf.subprocess, "run", explode)

    result = pf.check_account(str(tmp_path / "nope"), workspace)

    assert result.outcome == pf.CONFIG_MISSING


def test_a_timeout_is_a_probe_failure_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = _account(tmp_path, "hung", trusted_for=workspace)

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120.0)

    monkeypatch.setattr(pf.subprocess, "run", timeout)

    result = pf.check_account(str(config_dir), workspace, timeout=120.0)

    assert result.outcome == pf.PROBE_FAILED
    assert "timed out" in result.detail


def test_one_bad_account_does_not_take_out_the_batch(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    good = _account(tmp_path, "good", trusted_for=workspace)
    bad = _account(tmp_path, "bad", trusted_for=workspace)

    def fake_run(cmd, **kw):
        if kw["env"]["CLAUDE_CONFIG_DIR"] == str(bad):
            return _completed(1, stdout="Failed to authenticate: OAuth session expired")
        return _completed(0, "ALIVE")

    monkeypatch.setattr(pf.subprocess, "run", fake_run)

    usable, blocked = pf.select_accounts([str(bad), str(good)], workspace)

    assert [u.config_dir for u in usable] == [str(good)]
    assert [b.outcome for b in blocked] == [pf.AUTH_EXPIRED]


def test_the_probe_runs_in_the_workspace_under_the_accounts_config_dir(
    tmp_path: Path, monkeypatch
) -> None:
    # Trust is recorded per project path, so probing from anywhere else would
    # test a directory the worker never runs in.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config_dir = _account(tmp_path, "good", trusted_for=workspace)
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(cwd=kw["cwd"], config=kw["env"]["CLAUDE_CONFIG_DIR"], cmd=cmd)
        return _completed(0, "ALIVE")

    monkeypatch.setattr(pf.subprocess, "run", fake_run)

    pf.check_account(str(config_dir), workspace)

    assert seen["cwd"] == workspace
    assert seen["config"] == str(config_dir)
    assert pf.PROBE_PROMPT in seen["cmd"]


def test_main_prints_only_the_usable_csv_on_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    good = _account(tmp_path, "good", trusted_for=workspace)
    bad = _account(tmp_path, "bad", trusted_for=workspace)

    def fake_run(cmd, **kw):
        if kw["env"]["CLAUDE_CONFIG_DIR"] == str(bad):
            return _completed(1, stdout="Failed to authenticate: OAuth session expired")
        return _completed(0, "ALIVE")

    monkeypatch.setattr(pf.subprocess, "run", fake_run)

    code = pf.main(["--accounts", f"{bad},{good}", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == str(good)
    assert "AUTH_EXPIRED" in captured.err


def test_main_exits_nonzero_when_no_account_can_work(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bad = _account(tmp_path, "bad", trusted_for=workspace)
    monkeypatch.setattr(
        pf.subprocess,
        "run",
        lambda cmd, **kw: _completed(1, stdout="Failed to authenticate: OAuth session expired"),
    )

    code = pf.main(["--accounts", str(bad), "--workspace", str(workspace)])

    assert code == 1
    assert capsys.readouterr().out.strip() == ""
