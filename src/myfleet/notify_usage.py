"""Poll account_usage.py on a cadence and push a Telegram alert whenever an
account's session usage first crosses the ceiling (rotation event) or every
account is over it (fleet fully stalled). Stateful across runs via a small
JSON file so it only alerts on the *transition*, not every poll.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mytelegrambot.transport import HTTPTelegramTransport

from myfleet.account_usage import select_accounts

STATE_PATH = Path.home() / ".mythings" / "account_usage_state.json"


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll Claude account usage and alert on rotation.")
    parser.add_argument("--accounts", required=True)
    parser.add_argument("--max-session-pct", type=int, default=90)
    args = parser.parse_args(argv)

    config_dirs = [d.strip() for d in args.accounts.split(",") if d.strip()]
    usable, over = select_accounts(config_dirs, args.max_session_pct)

    state = _load_state()
    prev_over = set(state.get("over", []))
    now_over = {u.config_dir for u in over}

    newly_over = now_over - prev_over
    recovered = prev_over - now_over

    messages = []
    for d in newly_over:
        usage = next(u for u in over if u.config_dir == d)
        messages.append(
            f"⚠️ Claude account {d} hit {usage.session_pct}% session usage "
            f"(resets {usage.session_resets}) — rotating it out until reset."
        )
    for d in recovered:
        messages.append(f"✅ Claude account {d} session usage reset — back in rotation.")

    if not usable and now_over and messages:
        messages.append("🛑 All configured Claude accounts are over the session-usage ceiling. Fleet dispatch is paused until one resets.")

    if messages and os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        transport = HTTPTelegramTransport(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
        transport.send_message("\n".join(messages))

    for m in messages:
        print(m)

    _save_state({"over": sorted(now_over)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
