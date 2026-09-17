from __future__ import annotations

import pytest

import myfleet.fleet_ask as fleet_ask

# The `telegram` autouse fixture (tests/conftest.py) is the only thing standing
# between the test suite and real Telegram pushes -- it stubs `alert_spend` and
# `escalate_blocker` so a test that drives a candidate to escalation cannot
# message the operator by accident (#47: seven real alerts reached a phone in
# 18 hours before anyone noticed). It had no direct test of its own: nothing
# proved the stub actually records what's called, nor that `real_notifiers`
# really restores the unpatched functions rather than just returning an empty
# list. Both are covered here.

_REAL_ALERT_SPEND = fleet_ask.alert_spend
_REAL_ESCALATE_BLOCKER = fleet_ask.escalate_blocker


def test_default_fixture_stubs_and_records_both_notifiers(
    telegram: list[tuple[str, dict]],
) -> None:
    spend_ok = fleet_ask.alert_spend(spent=15.5, cap=20.0, raise_to=30.0)
    escalate_ok = fleet_ask.escalate_blocker(candidate="repo#1", detail="stuck", attempt=2)

    # The stub's default result (True), not a real subprocess outcome -- proves
    # nothing shelled out.
    assert spend_ok is True
    assert escalate_ok is True
    assert telegram == [
        ("alert_spend", {"spent": 15.5, "cap": 20.0, "raise_to": 30.0}),
        ("escalate_blocker", {"candidate": "repo#1", "detail": "stuck", "attempt": 2}),
    ]


@pytest.mark.real_notifiers
def test_real_notifiers_marker_leaves_the_real_functions_bound(
    telegram: list[tuple[str, dict]],
) -> None:
    # Opting out must mean opting out -- not just an empty call list while the
    # functions are still secretly patched.
    assert telegram == []
    assert fleet_ask.alert_spend is _REAL_ALERT_SPEND
    assert fleet_ask.escalate_blocker is _REAL_ESCALATE_BLOCKER
