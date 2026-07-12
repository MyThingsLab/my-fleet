"""Push a Telegram alert when a systemd unit hits OnFailure (or is invoked
manually for a status check). Not a fleet `My[X]` tool -- ops glue, invoked
by systemd itself via `telegram-alert@.service`, one instance per watched
unit, templated with the failing unit's name as the instance argument (%i).
"""

from __future__ import annotations

import argparse
import os
import subprocess

from mytelegrambot.transport import HTTPTelegramTransport


def _journal_tail(unit: str, lines: int = 15) -> str:
    proc = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alert Telegram that a systemd unit failed.")
    parser.add_argument("unit", help="the failing unit name, e.g. myserver.service")
    args = parser.parse_args(argv)

    tail = _journal_tail(args.unit)
    message = f"🛑 systemd unit failed: {args.unit}\n\nLast log lines:\n{tail[-1500:]}"

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"TELEGRAM_BOT_TOKEN/CHAT_ID not set, would have sent:\n{message}")
        return 1

    HTTPTelegramTransport(token, chat_id).send_message(message)
    print(f"alerted for {args.unit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
