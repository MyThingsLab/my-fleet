# No pyproject at this root (scripts, not a package), so register the marker
# here; the slow integration tests are excluded from CI's explicit file list.
import pytest

import myfleet.fleet_ask as fleet_ask


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "slow: integration/slow tests, excluded from the fast local loop"
    )
    config.addinivalue_line(
        "markers",
        "real_notifiers: test the notifier functions themselves; opts out of the "
        "`telegram` stub and must stub `fleet_ask.subprocess.run` instead",
    )


@pytest.fixture(autouse=True)
def telegram(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, dict]]:
    # `alert_spend` and `escalate_blocker` are push-only, so unlike `enable()`
    # they get no daemon/credential preflight to stop them -- they shell straight
    # out to the installed `mytelegrambot`. That is a real system boundary, and
    # it was the one these tests could still reach: the dispatch test drives a
    # candidate to its attempt cap, which escalates, and seven real alerts
    # reached the operator's phone in 18 hours before anyone noticed (#47).
    #
    # Autouse so the safe case is the default -- a test that wanders onto an
    # escalation path cannot message a human by accident. Tests that care about
    # the call assert on the returned list.
    calls: list[tuple[str, dict]] = []
    if request.node.get_closest_marker("real_notifiers"):
        return calls

    def record(name: str, result: bool):
        def stub(**kwargs) -> bool:
            calls.append((name, kwargs))
            return result

        return stub

    monkeypatch.setattr(fleet_ask, "alert_spend", record("alert_spend", True))
    monkeypatch.setattr(fleet_ask, "escalate_blocker", record("escalate_blocker", True))
    return calls
