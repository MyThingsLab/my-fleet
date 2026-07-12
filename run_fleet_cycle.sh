#!/usr/bin/env bash
# Wrapper around fleet_cycle.py that filters --accounts down to whichever
# Claude Code subscriptions are still under the 90% session-usage ceiling
# (see account_usage.py) before handing off. If none are usable, still runs
# the rest of the cycle with --skip-dispatch instead of failing outright.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ALL_ACCOUNTS="${FLEET_ACCOUNTS:-$HOME/.claude-personal,$HOME/.claude-work}"
MAX_SESSION_PCT="${FLEET_MAX_SESSION_PCT:-90}"

USABLE="$(python3 -m myfleet.account_usage --accounts "$ALL_ACCOUNTS" --max-session-pct "$MAX_SESSION_PCT" | tail -1)"

CYCLE_ARGS=("$@")

if [ -z "$USABLE" ]; then
    echo "no accounts under ${MAX_SESSION_PCT}% session usage -- running cycle with --skip-dispatch"
    exec python3 -m myfleet.fleet_cycle --accounts "$ALL_ACCOUNTS" --skip-dispatch "${CYCLE_ARGS[@]}"
else
    echo "usable accounts: $USABLE"
    exec python3 -m myfleet.fleet_cycle --accounts "$USABLE" "${CYCLE_ARGS[@]}"
fi
