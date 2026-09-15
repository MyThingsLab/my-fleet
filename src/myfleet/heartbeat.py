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

from mytelegrambot.transport import HTTPTelegramTransport
from mythings.ledger import Ledger

from myfleet.workspace import fleet_root, ledger_path

TOOL = "fleet_cycle"
KIND = "heartbeat"

# The switch's own memory of what it has already said. A stale tick is a
# *standing condition*, not an event: the timer that checks it fires on a
# cadence, so alerting whenever it was stale re-sent the identical message
# forever. A broken timer stays broken until a human fixes it, which is exactly
# the window in which they are most likely to mute the channel -- and this
# channel also carries the Allow/Deny prompts that gate merges, so a muted
# operator stops the fleet. Recording each transition makes "have I already
# reported this" answerable without a second state file.
ALERT_KIND = "heartbeat_alert"
STALE, RECOVERED = "stale", "recovered"
# A condition nobody has fixed still earns one reminder a day.
DEFAULT_REALERT_HOURS = 24.0

# Still not imported from fleet_dispatch.py: that module pulls in myorchestrator
# and friends, weight this dead-man's-switch has no reason to carry. But the
# path itself now comes from myfleet.workspace rather than being spelled out
# again here -- writing it twice is what let the reader keep pointing at
# `.fleet-dispatch/` after #62 renamed the directory, so this alerted on a
# ledger nobody writes while the real one filled up beside it.
WORKSPACE_ROOT = fleet_root(__file__)
DEFAULT_LEDGER = ledger_path(WORKSPACE_ROOT)

# One tick per timer, given each timer's own cadence plus slack for one missed
# run and its RandomizedDelaySec jitter -- not the bare interval, which would
# alert on every ordinary run.
DEFAULT_MAX_AGE_MIN = {"build": 8 * 60.0, "bookkeeping": 30 * 60.0}


def record(ledger: Ledger, tick: str) -> None:
    ledger.record(tool=TOOL, kind=KIND, outcome="ok", detail=tick, tick=tick)


def _parse_ts(ts: str) -> datetime:
    # Matches mythings.ledger's own _utc_now() format exactly.
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def last_heartbeat(ledger: Ledger, tick: str) -> datetime | None:
    entries = [e for e in ledger.read(tool=TOOL, kind=KIND) if e.data.get("tick") == tick]
    if not entries:
        return None
    return _parse_ts(entries[-1].ts)


def last_alert(ledger: Ledger, tick: str) -> tuple[str, datetime] | None:
    """The most recent thing this switch said about `tick`, and when."""
    entries = [e for e in ledger.read(tool=TOOL, kind=ALERT_KIND) if e.data.get("tick") == tick]
    if not entries:
        return None
    return entries[-1].outcome, _parse_ts(entries[-1].ts)


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


def transitions(
    ledger: Ledger,
    stale: dict[str, datetime | None],
    ticks: list[str],
    *,
    realert: timedelta,
    now: datetime | None = None,
) -> list[tuple[str, str, str]]:
    """The `(tick, state, message)` triples this run should actually report.

    A tick that is stale and was already reported stale within `realert` yields
    nothing -- the condition has not changed and the operator has been told.
    """
    now = now or datetime.now(UTC)
    out: list[tuple[str, str, str]] = []
    for tick in sorted(ticks):
        prior = last_alert(ledger, tick)
        if tick in stale:
            if prior is not None and prior[0] == STALE and now - prior[1] < realert:
                continue
            last = stale[tick]
            when = "never recorded" if last is None else f"last seen {last.isoformat()}"
            out.append(
                (
                    tick,
                    STALE,
                    f"\U0001f6d1 fleet_cycle {tick!r} tick heartbeat is stale ({when}) "
                    "-- its timer may have stopped firing",
                )
            )
        elif prior is not None and prior[0] == STALE:
            # The counterpart of an alert. Without it a silenced channel is
            # indistinguishable from a fixed one, and the edge-triggering above
            # would be the reason nobody ever learned the fleet came back.
            out.append((tick, RECOVERED, f"✅ fleet_cycle {tick!r} tick heartbeat is back"))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument(
        "--max-age-build-min", type=float, default=DEFAULT_MAX_AGE_MIN["build"]
    )
    parser.add_argument(
        "--max-age-bookkeeping-min", type=float, default=DEFAULT_MAX_AGE_MIN["bookkeeping"]
    )
    parser.add_argument(
        "--realert-hours",
        type=float,
        default=DEFAULT_REALERT_HOURS,
        help="how long before an unfixed stale tick is reported again (0 = every run)",
    )
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)
    max_age_min = {"build": args.max_age_build_min, "bookkeeping": args.max_age_bookkeeping_min}
    stale = stale_ticks(ledger, max_age_min)
    changes = transitions(
        ledger,
        stale,
        list(max_age_min),
        realert=timedelta(hours=args.realert_hours),
    )

    if not changes:
        # Deliberately reports *silence*, not health: a tick can be stale here
        # and still say nothing, because it was already reported.
        state = "stale but already reported" if stale else "fresh"
        print(f"nothing to report ({', '.join(sorted(max_age_min))}: {state})")
        return 0

    lines = [message for _tick, _state, message in changes]
    message = "\n".join(lines)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        HTTPTelegramTransport(token, chat_id).send_message(message)
    else:
        print(f"TELEGRAM_BOT_TOKEN/CHAT_ID not set, would have sent:\n{message}")

    # Only after the send. Recorded first, a transport outage would consume the
    # alert and suppress it for the whole re-alert window -- the switch would go
    # quiet about a dead fleet because it failed to say so once.
    for tick, state, _message in changes:
        ledger.record(tool=TOOL, kind=ALERT_KIND, outcome=state, detail=tick, tick=tick)

    for line in lines:
        print(line)
    # Nonzero means "something is newly wrong", which is what an OnFailure= hook
    # should act on. A suppressed repeat and a recovery are both zero: a stale
    # tick already reported must not keep firing OnFailure either, or the dedup
    # above buys nothing -- systemd would send the second copy instead.
    return 1 if any(state == STALE for _tick, state, _message in changes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
