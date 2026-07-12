from __future__ import annotations

from pathlib import Path

import pytest

import myfleet.fleet_ask as fleet_ask

_CREDS = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}

# The wire between MyGuard's escalation seam and `mytelegrambot ask`. Two things
# it must get right, both of which fail *silently* when wrong:
#
#   1. the ledger path -- ask and the daemon rendezvous through that file and
#      nothing else, so a cwd-relative default (a worker runs in a worktree)
#      means every prompt is sent and no tap is ever seen;
#   2. refusing to arm when nobody is listening -- an unanswerable channel is
#      slower than no channel and just as closed.


def test_the_ask_command_carries_an_absolute_ledger_path() -> None:
    # The failure this prevents: `mytelegrambot ask` defaults its --ledger to a
    # cwd-relative .mythings/ledger.jsonl. A headless worker runs in a git
    # worktree, so that resolves to a ledger the daemon never writes to, and every
    # ASK times out into a DENY with nothing to say why.
    command = fleet_ask.ask_command()

    assert "--ledger /" in command
    assert str(fleet_ask.BOT_LEDGER) in command
    assert fleet_ask.BOT_LEDGER.is_absolute()


def test_the_ask_command_points_at_the_ledger_the_daemon_actually_writes() -> None:
    # The systemd unit sets WorkingDirectory to the bot's repo and `mytelegrambot
    # run` resolves .mythings/ledger.jsonl relative to it. If this drifts, the
    # rendezvous breaks and every ASK denies.
    assert fleet_ask.BOT_LEDGER.parts[-3:] == (
        "my-telegram-bot",
        ".mythings",
        "ledger.jsonl",
    )


def test_guards_backstop_timeout_sits_above_asks_own_deadline() -> None:
    # Guard kills the subprocess at MYTHINGS_ASK_TIMEOUT. If that fired first, the
    # honest "timeout: deny" `ask` would have reported becomes an opaque killed
    # process -- same decision, worse diagnosis.
    env = fleet_ask.ask_env(timeout=120)

    assert "--timeout 120" in env["MYTHINGS_ASK_CMD"]
    assert int(env["MYTHINGS_ASK_TIMEOUT"]) > 120


def test_enable_refuses_when_no_daemon_is_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The whole point of the preflight. Wired to a dead daemon, every ASK blocks
    # for the full timeout and only then denies: the cycle becomes slow AND wrong,
    # with nothing in the output explaining why. Refusing loudly beats that.
    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: False)
    env: dict[str, str] = dict(_CREDS)

    with pytest.raises(
        fleet_ask.AskChannelUnavailable, match="no `mytelegrambot run` daemon"
    ):
        fleet_ask.enable(ledger=ledger, env=env)

    assert "MYTHINGS_ASK_CMD" not in env  # nothing armed


def test_enable_refuses_when_the_ledger_directory_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    env: dict[str, str] = dict(_CREDS)

    with pytest.raises(
        fleet_ask.AskChannelUnavailable, match="ledger directory does not exist"
    ):
        fleet_ask.enable(ledger=tmp_path / "nope" / "ledger.jsonl", env=env)

    assert "MYTHINGS_ASK_CMD" not in env


def test_a_remote_daemon_skips_the_local_process_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The daemon lives on the Pi; a cycle driven from a laptop sharing the ledger
    # still wants the channel. The local process table proves nothing there.
    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: False)
    env: dict[str, str] = dict(_CREDS)

    wiring = fleet_ask.enable(ledger=ledger, env=env, remote_daemon=True)

    assert wiring["MYTHINGS_ASK_CMD"] in env["MYTHINGS_ASK_CMD"]
    assert str(ledger) in env["MYTHINGS_ASK_CMD"]


def test_enable_arms_the_env_every_subprocess_inherits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # fleet_cycle's tool CLIs and fleet_dispatch's headless workers both inherit
    # the environment, so setting it once here wires all of them -- which is why
    # MyGuard reads the channel from the env rather than from an argument.
    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    env: dict[str, str] = dict(_CREDS)

    fleet_ask.enable(ledger=ledger, timeout=45, env=env)

    # Absolute, not a bare name: a subprocess may not inherit the venv on PATH.
    assert " ask --ledger " in env["MYTHINGS_ASK_CMD"]
    assert env["MYTHINGS_ASK_CMD"].startswith("/")
    assert "--timeout 45" in env["MYTHINGS_ASK_CMD"]
    assert env["MYTHINGS_ASK_TIMEOUT"] == "75"


def test_the_daemon_check_is_not_fooled_by_a_process_that_merely_mentions_it() -> None:
    # The bug this pins: `pgrep -f "mytelegrambot run"` matches the pattern anywhere
    # in a command line, so any shell, editor or grep that merely *mentions* the
    # string counts as a live daemon. That false positive arms a channel nobody is
    # listening on -- precisely the silent failure the preflight exists to prevent.
    import subprocess
    import sys

    mentions_it = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)  # mytelegrambot run"]
    )
    try:
        assert fleet_ask.daemon_is_running() is False
    finally:
        mentions_it.terminate()
        mentions_it.wait()


def test_the_daemon_check_finds_the_real_console_script_form(tmp_path: Path) -> None:
    # And the converse: it must find the real thing. A venv console script has an
    # absolute-interpreter shebang, so the kernel execs python and the real argv is
    #
    #     ['/.../python3', '/.../mytelegrambot', 'run']
    #
    # argv[0] is the *interpreter*, and the process name is "python3". Checking
    # argv[0] (or `pgrep -x mytelegrambot`) would never find the daemon and would
    # refuse to arm the channel even with it running.
    import subprocess
    import sys
    import time

    fake_daemon = tmp_path / "mytelegrambot"
    fake_daemon.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    fake_daemon.chmod(0o755)

    proc = subprocess.Popen([str(fake_daemon), "run"])
    try:
        # Popen returns before the child has finished exec'ing, so its argv is not
        # in /proc yet. Poll rather than sleep a fixed amount.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not fleet_ask.daemon_is_running():
            time.sleep(0.05)
        assert fleet_ask.daemon_is_running() is True
    finally:
        proc.terminate()
        proc.wait()


def test_the_ask_command_names_the_binary_absolutely_not_by_bare_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The bug this pins, found on the Pi against the real daemon: `mytelegrambot` is
    # a venv console script, and a subprocess does not necessarily inherit a PATH
    # containing that venv's bin. A bare name raised FileNotFoundError, MyGuard read
    # that as a DENY, and every merge was silently refused by a channel that had
    # never reached anyone -- fail-closed, but for the wrong reason, with no human
    # ever asked.
    # my-telegram-bot is not installed in every environment that runs these tests
    # (fleet-dispatch's CI installs only core/guard/orchestrator), so stub the
    # resolution rather than depend on it.
    binary = tmp_path / "bin" / "mytelegrambot"
    binary.parent.mkdir()
    binary.touch()
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: binary)

    command = fleet_ask.ask_command()

    assert command.startswith("/"), command  # absolute, never a bare name
    assert Path(command.split()[0]).name == "mytelegrambot"


def test_enable_refuses_when_the_ask_binary_cannot_be_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: None)
    env: dict[str, str] = {}

    with pytest.raises(fleet_ask.AskChannelUnavailable, match="not runnable"):
        fleet_ask.enable(ledger=ledger, env=env)

    assert "MYTHINGS_ASK_CMD" not in env  # nothing armed


def test_enable_refuses_when_the_bot_credentials_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The third way a channel can be broken while looking like a human deny, and the
    # one that actually bit on the Pi: the daemon gets TELEGRAM_BOT_TOKEN from
    # systemd's EnvironmentFile, but a script run by hand does not inherit it. `ask`
    # dies on os.environ[...] with a KeyError before reaching Telegram, MyGuard reads
    # the non-zero exit as DENY, and three PRs were logged "not approved" as though a
    # human had refused them.
    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    env = {"TELEGRAM_CHAT_ID": "chat"}  # token missing

    with pytest.raises(fleet_ask.AskChannelUnavailable, match="TELEGRAM_BOT_TOKEN"):
        fleet_ask.enable(ledger=ledger, env=env)

    assert "MYTHINGS_ASK_CMD" not in env  # nothing armed


def test_the_refusal_never_echoes_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A secret must not end up in a log line just because a preflight failed.
    ledger = tmp_path / ".mythings" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: True)
    env = {"TELEGRAM_BOT_TOKEN": "super-secret-token"}  # chat id missing

    with pytest.raises(fleet_ask.AskChannelUnavailable) as caught:
        fleet_ask.enable(ledger=ledger, env=env)

    assert "super-secret-token" not in str(caught.value)
