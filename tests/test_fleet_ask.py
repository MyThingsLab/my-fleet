from __future__ import annotations

import subprocess
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


@pytest.mark.parametrize(
    "argv",
    [
        # A shell whose single argv string merely mentions the daemon. This is the
        # case `pgrep -f "mytelegrambot run"` gets wrong, arming a channel nobody
        # is listening on.
        [b"/bin/sh", b"-c", b"mytelegrambot run &"],
        [b"/usr/bin/python3", b"-c", b"import time; time.sleep(30)  # mytelegrambot run"],
        [b"/usr/bin/grep", b"-r", b"mytelegrambot run", b"."],
        # Right binary, wrong subcommand: `ask` is the short-lived client the
        # daemon serves, not the daemon.
        [b"/v/bin/python3", b"/v/bin/mytelegrambot", b"ask", b"--timeout", b"60"],
        # `run` is there, but not as the argument following the binary.
        [b"/v/bin/python3", b"/v/bin/mytelegrambot", b"--ledger", b"run"],
        [],
        [b""],
    ],
)
def test_a_process_that_merely_mentions_the_daemon_is_not_the_daemon(argv: list[bytes]) -> None:
    assert fleet_ask.is_daemon_argv(argv) is False


@pytest.mark.parametrize(
    "argv",
    [
        # The venv console script: the kernel execs the interpreter named in the
        # shebang, so argv[0] is python and the process name is "python3".
        # Checking argv[0] (or `pgrep -x mytelegrambot`) would never find the
        # daemon and would refuse to arm the channel with it running.
        [b"/v/bin/python3", b"/v/bin/mytelegrambot", b"run"],
        [b"/v/bin/python3", b"/v/bin/mytelegrambot", b"run", b"--testers-db", b".mythings/t.db"],
        # A direct exec, and the module form.
        [b"mytelegrambot", b"run"],
        [b"/usr/bin/python3", b"-m", b"mytelegrambot", b"run"],
    ],
)
def test_the_real_daemon_forms_are_recognised(argv: list[bytes]) -> None:
    assert fleet_ask.is_daemon_argv(argv) is True


def test_the_walk_reads_every_pid_and_survives_one_that_exits(tmp_path: Path) -> None:
    # /proc's shape is all this walk assumes: numeric dirs holding a NUL-separated
    # `cmdline`. Faking it keeps the test hermetic -- the real /proc would make the
    # answer depend on whether this host happens to be running the daemon.
    proc_root = tmp_path / "proc"
    (proc_root / "1").mkdir(parents=True)
    (proc_root / "1" / "cmdline").write_bytes(b"/sbin/init\x00")
    (proc_root / "self").mkdir()  # non-numeric, skipped
    (proc_root / "42").mkdir()  # no cmdline at all: exited mid-walk
    assert fleet_ask.daemon_is_running(proc_root=proc_root) is False

    (proc_root / "7").mkdir()
    (proc_root / "7" / "cmdline").write_bytes(b"/v/bin/python3\x00/v/bin/mytelegrambot\x00run\x00")
    assert fleet_ask.daemon_is_running(proc_root=proc_root) is True


def test_the_console_script_form_in_a_real_proc_matches_what_the_predicate_expects(
    tmp_path: Path,
) -> None:
    # The one thing a fake /proc cannot check: that a venv console script really
    # does exec the interpreter, leaving the script path at argv[1]. Assert on
    # *this* process's own cmdline rather than on `daemon_is_running()`, which
    # would answer True for an unrelated live daemon and pass even if the
    # predicate were broken (my-fleet#45).
    import subprocess
    import sys
    import time

    fake_daemon = tmp_path / "mytelegrambot"
    fake_daemon.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    fake_daemon.chmod(0o755)

    proc = subprocess.Popen([str(fake_daemon), "run"])
    try:
        cmdline = Path("/proc") / str(proc.pid) / "cmdline"
        # Popen returns before the child has finished exec'ing, so its argv is not
        # in /proc yet. Poll rather than sleep a fixed amount.
        deadline = time.monotonic() + 5
        argv: list[bytes] = []
        while time.monotonic() < deadline:
            argv = cmdline.read_bytes().split(b"\0")
            if fleet_ask.is_daemon_argv(argv):
                break
            time.sleep(0.05)
        assert fleet_ask.is_daemon_argv(argv) is True, argv
        assert Path(argv[0].decode()).name != "mytelegrambot", (
            "the kernel should have exec'd the interpreter, not the script"
        )
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


def test_ask_channel_probes_daemon_on_nonzero_exit_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #72: When a channel returns non-zero, Guard probes the daemon. If it has died,
    # it raises AskChannelUnavailable rather than recording a false human DENY.
    from myguard.ask import SubprocessAsk
    from mythings.policy import Action

    monkeypatch.setattr(fleet_ask, "daemon_is_running", lambda: False)
    channel = SubprocessAsk("false", liveness_check=fleet_ask.daemon_is_running)
    action = Action(kind="pr-merge", payload={})

    with pytest.raises(fleet_ask.AskChannelUnavailable, match="daemon is not running"):
        channel(action)



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


# The push-only notifications (fleet-dispatch#41 spend tripwire, #44 blocker
# escalation), the other direction from `ask`: these never block on a reply,
# so a failure to reach `mytelegrambot` must degrade to a bool, never raise --
# the run that triggered the push must survive a dead channel.


@pytest.mark.real_notifiers
def test_alert_spend_invokes_the_bot_with_the_right_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(fleet_ask.subprocess, "run", fake_run)
    ledger = tmp_path / "ledger.jsonl"

    ok = fleet_ask.alert_spend(spent=15.5, cap=20.0, raise_to=30.0, ledger=ledger)

    assert ok is True
    argv = captured["argv"]
    assert argv[:2] == ["/usr/bin/mytelegrambot", "alert-spend"]
    assert argv[argv.index("--spent") + 1] == "15.50"
    assert argv[argv.index("--cap") + 1] == "20.00"
    assert argv[argv.index("--raise-to") + 1] == "30.00"
    assert argv[argv.index("--ledger") + 1] == str(ledger)


@pytest.mark.real_notifiers
def test_alert_spend_returns_false_on_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    monkeypatch.setattr(
        fleet_ask.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1)
    )

    assert fleet_ask.alert_spend(spent=1, cap=2, raise_to=3) is False


@pytest.mark.real_notifiers
def test_alert_spend_swallows_a_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))

    def raise_missing(argv, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(fleet_ask.subprocess, "run", raise_missing)

    assert fleet_ask.alert_spend(spent=1, cap=2, raise_to=3) is False


@pytest.mark.real_notifiers
def test_escalate_blocker_invokes_the_bot_with_the_right_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(fleet_ask.subprocess, "run", fake_run)
    ledger = tmp_path / "ledger.jsonl"

    ok = fleet_ask.escalate_blocker(
        candidate="repo#1", detail="gave up after 3 attempts", attempt=3, ledger=ledger
    )

    assert ok is True
    argv = captured["argv"]
    assert argv[:2] == ["/usr/bin/mytelegrambot", "escalate-blocker"]
    assert argv[argv.index("--candidate") + 1] == "repo#1"
    assert argv[argv.index("--detail") + 1] == "gave up after 3 attempts"
    assert argv[argv.index("--attempt") + 1] == "3"
    assert argv[argv.index("--ledger") + 1] == str(ledger)


@pytest.mark.real_notifiers
def test_escalate_blocker_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_ask, "ask_binary", lambda: Path("/usr/bin/mytelegrambot"))

    def raise_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    monkeypatch.setattr(fleet_ask.subprocess, "run", raise_timeout)

    assert fleet_ask.escalate_blocker(candidate="repo#1", detail="d", attempt=1) is False


def test_fleet_root_raises_or_resolves_env_var_in_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from myfleet.workspace import ROOT_ENV, fleet_root

    isolated_file = tmp_path / "worktree" / "my-tool" / "src" / "mytool" / "mod.py"
    isolated_file.parent.mkdir(parents=True)

    monkeypatch.delenv(ROOT_ENV, raising=False)
    with pytest.raises(RuntimeError, match="Cannot resolve MyThingsLab fleet root"):
        fleet_root(str(isolated_file))

    real_root = tmp_path / "real_fleet_root"
    real_root.mkdir()
    monkeypatch.setenv(ROOT_ENV, str(real_root))
    assert fleet_root(str(isolated_file)) == real_root.resolve()

