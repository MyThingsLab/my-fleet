# No pyproject at this root (scripts, not a package), so register the marker
# here; the slow integration tests are excluded from CI's explicit file list.
def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "slow: integration/slow tests, excluded from the fast local loop"
    )
