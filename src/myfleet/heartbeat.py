#!/usr/bin/env python3
"""Dead-man's-switch for fleet_cycle's two timers.

fleet-cycle.timer and fleet-bookkeeping.timer each `OnFailure=` a Telegram
alert, but that only fires when the ExecStart it names actually runs and
returns nonzero. A timer that stops firing at all -- masked, a wedged user
session, or #58's silently-broken ExecStart path -- produces no failure to
catch; that exact gap produced 183 silent misses over several weeks before
anyone noticed. fleet_cycle.py records a heartbeat ledger entry every tick it
completes (`build` whenever it wasn't told to skip dispatch, `bookkeeping`
whenever it wasn't told to skip bookkeeping); `check` reads the last one of
each and alerts when it's older than that tick's own cadence allows for,
closing the gap OnFailure cannot.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mythings.ledger import Ledger
from mytelegrambot.transport import HTTPTelegramTransport

TOOL = "fleet_cycle"
KIND = "heartbeat"

# Climbs myfleet/<file>.py -> src -> my-fleet -> MyThingsLab/ (the fleet root).
# Deliberately not imported from fleet_dispatch.py: that module pulls in
# myorchestrator and friends, weight this dead-man's-switch has no reason to
# carry just to compute the same path fleet_dispatch.DISPATCH_LEDGER already
# resolves to.
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LEDGER = WORKSPACE_ROOT / ".fleet-dispatch" / "ledger.jsonl"

# One tick per timer, given each timer's own cadence plus slack for one missed
# run and its RandomizedDelaySec jitter -- not the bare interval, which would
# alert on every ordinary run.
DEFAULT_MAX_AGE_MIN = {"build": 8 * 60.0, "bookkeeping": 30 * 60.0}


def record(ledger: Ledger, tick: str) -> None:
    ledger.record(tool=TOOL, kind=KIND, outcome="ok", detail=tick, tick=tick)


def last_heartbeat(ledger: Ledger, tick: str) -> datetime | None:
    entries = [e for e in ledger.read(tool=TOOL, kind=KIND) if e.data.get("tick") == tick]
    if not entries:
        return None
    # Matches mythings.ledger's own _utc_now() format exactly.
    return datetime.strptime(entries[-1].ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def stale_ticks(
    ledger: Ledger, max_age_min: dict[str, float], *, now: datetime | None = None
) -> dict[str, datetime | None]:
    """Ticks whose last heartbeat is missing or older than their own max age.

    Value is the last-seen timestamp, or None if that tick has never once
    recorded a heartbeat (a freshly deployed timer, or one that never fired).
    """
    now = now or datetime.now(UTC)
    stale: dict[str, datetime | None] = {}
    for tick, minutes in max_age_min.items():
        last = last_heartbeat(ledger, tick)
        if last is None or now - last > timedelta(minutes=minutes):
            stale[tick] = last
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument(
        "--max-age-build-min", type=float, default=DEFAULT_MAX_AGE_MIN["build"]
    )
    parser.add_argument(
        "--max-age-bookkeeping-min", type=float, default=DEFAULT_MAX_AGE_MIN["bookkeeping"]
    )
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)
    max_age_min = {"build": args.max_age_build_min, "bookkeeping": args.max_age_bookkeeping_min}
    stale = stale_ticks(ledger, max_age_min)

    if not stale:
        print(f"heartbeats fresh: {', '.join(sorted(max_age_min))}")
        return 0

    lines = []
    for tick, last in sorted(stale.items()):
        when = "never recorded" if last is None else f"last seen {last.isoformat()}"
        lines.append(
            f"\U0001f6d1 fleet_cycle {tick!r} tick heartbeat is stale ({when}) "
            "-- its timer may have stopped firing"
        )
    message = "\n".join(lines)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        HTTPTelegramTransport(token, chat_id).send_message(message)
    else:
        print(f"TELEGRAM_BOT_TOKEN/CHAT_ID not set, would have sent:\n{message}")

    for line in lines:
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
