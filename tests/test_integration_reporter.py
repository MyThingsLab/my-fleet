"""Cross-tool integration: drive my-reporter's real CLI over a real ledger.

Every tool's own suite tests against synthetic in-fixture data; nothing in the
fleet proved that tool A's actual output is readable by tool B's actual CLI.
This is that proof for the ledger seam. Marked slow and skipped when the
sibling isn't installed (e.g. this repo's CI, which has no checkouts) — it runs
in the shared workspace venv via `fleet_test.py --include-slow` or pytest -m slow.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from mythings.ledger import Ledger, LedgerEntry

MYREPORTER = Path(sys.executable).parent / "myreporter"

pytestmark = pytest.mark.slow


def make_repo_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    ledger_path = root / ".mythings" / "ledger.jsonl"
    shared = Ledger(ledger_path)
    shared.append(
        LedgerEntry(
            tool="my-scraper",
            kind="scrape",
            outcome="success",
            detail="fetched 3 pages",
            ts="2026-07-10T10:00:00Z",
        )
    )
    dev = Ledger(root / "dev-ledger" / "2026-07-10.jsonl")
    dev.append(
        LedgerEntry(
            tool="claude-code",
            kind="decision",
            outcome="recorded",
            detail="prefer the bare-origin builder over a git lambda",
            ts="2026-07-10T11:00:00Z",
        )
    )
    return ledger_path, root


def run_reporter(*argv: str) -> subprocess.CompletedProcess[str]:
    if not MYREPORTER.exists():
        pytest.skip("myreporter is not installed in this environment")
    return subprocess.run(
        [str(MYREPORTER), *argv], capture_output=True, text=True, timeout=60
    )


def test_digest_reflects_a_real_sibling_ledger(tmp_path: Path) -> None:
    ledger_path, root = make_repo_root(tmp_path)
    proc = run_reporter(
        "digest",
        "--ledger",
        str(ledger_path),
        "--repo-root",
        str(root),
        "--since",
        "2026-07-01T00:00:00Z",
    )
    assert proc.returncode == 0, proc.stderr
    assert "my-scraper" in proc.stdout
    assert "prefer the bare-origin builder over a git lambda" in proc.stdout


def test_handoff_brief_reads_the_same_ledgers(tmp_path: Path) -> None:
    ledger_path, root = make_repo_root(tmp_path)
    proc = run_reporter(
        "digest",
        "--handoff",
        "--ledger",
        str(ledger_path),
        "--repo-root",
        str(root),
        "--since",
        "2026-07-01T00:00:00Z",
    )
    assert proc.returncode == 0, proc.stderr
    assert "prefer the bare-origin builder over a git lambda" in proc.stdout
