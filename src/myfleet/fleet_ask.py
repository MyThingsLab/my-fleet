#!/usr/bin/env python3
"""Wire the fleet's ASK escalation to a real human over Telegram.

MyGuard's `Guard` escalates an `ASK` by running `$MYTHINGS_ASK_CMD` and reading
its exit code (0 = the human's ALLOW, anything else = DENY). `mytelegrambot ask`
speaks exactly that contract. This module is the wire between them: it builds the
command, points it at the right ledger, and refuses to arm a channel that cannot
actually reach anyone.

Without this, `Guard` returns ASK, every caller's `PolicyResult.under(
unattended=True)` collapses it to DENY, and the human is never asked -- which is
what the whole fleet did until now.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Climbs myfleet/<file>.py -> src -> my-fleet -> MyThingsLab/ (the fleet root).
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]

# `mytelegrambot run` (the daemon) resolves its ledger relative to its own
# WorkingDirectory, which the systemd unit sets to the bot's repo -- so this is
# the file it writes callback entries to.
#
# This path is the whole ballgame. `mytelegrambot ask` blocks waiting for a
# `kind=callback` entry that the *daemon* writes; the two processes rendezvous
# through this file and nothing else. Point `ask` at a different ledger -- which
# its cwd-relative default silently does, since a worker runs in a git worktree --
# and every prompt is sent, nobody's tap is ever seen, and every ASK times out
# into a DENY. Absolute, always.
BOT_LEDGER = WORKSPACE_ROOT / "my-telegram-bot" / ".mythings" / "ledger.jsonl"

# Per-ask ceiling. A human has to notice a phone notification and answer, so it
# cannot be short; but every ASK in a cycle costs up to this much wall-clock, so
# it cannot be long either. `Guard`'s own subprocess timeout sits just above this
# as a backstop for an `ask` that fails to honour its own deadline.
DEFAULT_ASK_TIMEOUT = 300

# What `mytelegrambot ask` reads from the environment at startup. Never logged, never
# written anywhere -- only their presence is ever checked.
TELEGRAM_CREDENTIALS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


class AskChannelUnavailable(RuntimeError):
    pass


def ask_binary() -> Path | None:
    # Resolve `mytelegrambot` to an absolute path rather than trusting PATH.
    #
    # It is a venv console script, and a subprocess does not necessarily inherit a
    # PATH containing that venv's bin -- on the Pi it does not, because the caller
    # is invoked as `.venv/bin/python3 script.py` without the venv ever being
    # activated. A bare `mytelegrambot` there raises FileNotFoundError, MyGuard
    # dutifully treats that as a DENY, and every merge is silently refused by a
    # channel that never reached anyone. Fail-closed, but for the wrong reason and
    # with no human ever asked.
    #
    # The interpreter running us is the ground truth: the console script sits beside
    # it in the same bin/.
    beside_interpreter = Path(sys.executable).parent / "mytelegrambot"
    if beside_interpreter.exists():
        return beside_interpreter
    found = shutil.which("mytelegrambot")
    return Path(found) if found else None


def ask_command(
    *, ledger: Path = BOT_LEDGER, timeout: int = DEFAULT_ASK_TIMEOUT
) -> str:
    binary = ask_binary() or Path("mytelegrambot")
    return f"{binary} ask --ledger {ledger} --timeout {timeout}"


def daemon_is_running() -> bool:
    # The daemon is the only `getUpdates` caller and we must not become a second
    # one, so liveness cannot be probed by talking to Telegram. It also writes
    # nothing to the ledger while idle (an idle long-poll is not an event), so
    # ledger freshness proves nothing either. That leaves the process table.
    #
    # Read argv directly rather than shelling out to `pgrep -f`, which matches its
    # pattern anywhere in a command line: any shell, editor or grep that merely
    # *mentions* "mytelegrambot run" satisfies it. A false positive here is the
    # exact silent failure this check exists to prevent -- arming a channel nobody
    # is listening on.
    #
    # `pgrep -x mytelegrambot` is no good either, and neither is checking argv[0]:
    # the daemon is a venv console script, so the kernel execs the *interpreter*
    # named in its shebang. Its real argv is
    #
    #     ['/.../.venv/bin/python3', '/.../.venv/bin/mytelegrambot', 'run']
    #
    # -- the process name is "python3" and argv[0] is the interpreter. What is
    # actually invariant is an argv element basenaming to `mytelegrambot` with
    # `run` immediately after it. That matches the shebang form above, a direct
    # exec, and `python -m mytelegrambot run`; it does not match a shell whose one
    # big argv string merely contains the words.
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit() or pid_dir.name == str(os.getpid()):
            continue
        try:
            argv = (pid_dir / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # the process exited, or is not ours to read
        for arg, following in zip(argv, argv[1:], strict=False):
            if following != b"run":
                continue
            if Path(arg.decode(errors="replace")).name == "mytelegrambot":
                return True
    return False


def ask_env(
    *, ledger: Path = BOT_LEDGER, timeout: int = DEFAULT_ASK_TIMEOUT
) -> dict[str, str]:
    return {
        "MYTHINGS_ASK_CMD": ask_command(ledger=ledger, timeout=timeout),
        # Guard's backstop, above `ask`'s own deadline so the inner timeout is the
        # one that normally fires and can report "timeout" honestly.
        "MYTHINGS_ASK_TIMEOUT": str(timeout + 30),
    }


def enable(
    *,
    ledger: Path = BOT_LEDGER,
    timeout: int = DEFAULT_ASK_TIMEOUT,
    remote_daemon: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    # Arms the channel for this process and every subprocess it spawns (both
    # fleet_cycle's tool CLIs and fleet_dispatch's headless workers inherit the
    # environment).
    #
    # Refusing loudly is the point. A channel wired to a daemon that is not
    # running is *worse than no channel*: every ASK still blocks for the full
    # timeout and only then denies, so a cycle becomes slow AND wrong, with
    # nothing in the output to say why. Better to stop here than to spend an hour
    # denying everything.
    env = os.environ if env is None else env

    # Every precondition below has the same failure mode, and it is a nasty one: the
    # ask subprocess dies, MyGuard reads any non-zero exit as a DENY, and the caller
    # logs "not approved" -- indistinguishable from a human tapping Deny. The action
    # is refused by a channel that never reached anyone, and nothing says so. Each of
    # these was found the hard way, in this order, against the real daemon. Check them
    # up front and refuse loudly instead.
    if ask_binary() is None:
        raise AskChannelUnavailable(
            "`mytelegrambot` is not runnable from here.\n"
            "  Every ask would raise FileNotFoundError, which MyGuard reads as a DENY --\n"
            "  so every action would be refused by a channel that never reached anyone.\n"
            "  Install my-telegram-bot into this interpreter's environment, or put its\n"
            "  console script on PATH."
        )

    missing = [name for name in TELEGRAM_CREDENTIALS if not env.get(name)]
    if missing:
        # The daemon gets these from systemd's EnvironmentFile; a script run by hand
        # does not inherit them, and `mytelegrambot ask` dies on os.environ[...] with
        # a KeyError before it ever reaches Telegram.
        raise AskChannelUnavailable(
            f"{' and '.join(missing)} not set in this environment.\n"
            "  `mytelegrambot ask` reads them at startup and would die before reaching\n"
            "  Telegram -- and MyGuard would read that as a DENY, so every action would\n"
            "  be refused without anyone ever being asked.\n"
            "  Source the bot's env file first (the same one the systemd unit uses):\n"
            "    set -a; . ~/.config/mythingslab/telegram.env; set +a"
        )

    if not ledger.parent.exists():
        raise AskChannelUnavailable(
            f"the bot's ledger directory does not exist: {ledger.parent}\n"
            "  The daemon has never run here. Start `mytelegrambot run` (see\n"
            "  my-telegram-bot/deploy/systemd/) before arming the ask channel."
        )

    if not remote_daemon and not daemon_is_running():
        raise AskChannelUnavailable(
            "no `mytelegrambot run` daemon found on this host.\n"
            "  Nothing would ever see the Allow/Deny tap, so every ASK would block\n"
            f"  for {timeout}s and then DENY -- slower than no channel, and just as closed.\n"
            "  Start the daemon, or pass --ask-remote-daemon if it runs on another\n"
            "  host that shares this ledger."
        )

    wiring = ask_env(ledger=ledger, timeout=timeout)
    env.update(wiring)
    return wiring
