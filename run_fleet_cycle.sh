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

# account_usage.py exits 1 when every account is unusable -- exactly the case
# the `-z "$USABLE"` branch below exists to handle. Under `set -e`, letting
# that nonzero exit code reach us (pipefail propagates it through the old
# `| tail -1`) aborts the script right here, before the fallback branch can
# run -- so it's disabled for just this one command. Diagnostics print to
# stderr inside account_usage.py; stdout is exactly the CSV line, so no
# `tail -1` is needed to separate them.
set +e
USABLE="$(python3 -m myfleet.account_usage --accounts "$ALL_ACCOUNTS" --max-session-pct "$MAX_SESSION_PCT")"
set -e

CYCLE_ARGS=("$@")

if [ -z "$USABLE" ]; then
    echo "no accounts under ${MAX_SESSION_PCT}% session usage -- running cycle with --skip-dispatch"
    exec python3 -m myfleet.fleet_cycle --accounts "$ALL_ACCOUNTS" --skip-dispatch "${CYCLE_ARGS[@]}"
else
    echo "usable accounts: $USABLE"
    exec python3 -m myfleet.fleet_cycle --accounts "$USABLE" "${CYCLE_ARGS[@]}"
fi
