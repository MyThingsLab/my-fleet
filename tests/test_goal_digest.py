"""Tests for myfleet.goal_digest — executive 6-hour goal digest."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from myfleet.goal_digest import (
    DispatchStats,
    GoalDigest,
    _compile_ledger_stats,
    _parse_ts,
    compile_digest,
    main,
    render_human_digest,
)


def test_parse_ts():
    ts = _parse_ts("2026-09-13T20:00:00Z")
    assert ts is not None
    assert ts.year == 2026
    assert ts.month == 9
    assert ts.day == 13


def test_compile_ledger_stats(tmp_path: Path):
    ledger_file = tmp_path / "ledger.jsonl"

    now = datetime.now(UTC)
    recent_ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_ts = (now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        json.dumps({
            "tool": "fleet_dispatch",
            "kind": "dispatch",
            "outcome": "started",
            "detail": "recent run",
            "ts": recent_ts,
            "data": {"cost_usd": 0.25, "account": "acct-1"},
        }),
        json.dumps({
            "tool": "fleet_dispatch",
            "kind": "dispatch",
            "outcome": "needs_human",
            "detail": "blocked capability",
            "ts": recent_ts,
            "data": {"cost_usd": 0.10, "account": "acct-1", "candidate": "my-tool#1"},
        }),
        json.dumps({
            "tool": "fleet_dispatch",
            "kind": "dispatch",
            "outcome": "started",
            "detail": "old run",
            "ts": old_ts,
            "data": {"cost_usd": 0.50, "account": "acct-2"},
        }),
    ]
    ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    window_start = now - timedelta(hours=6)
    stats = _compile_ledger_stats(ledger_file, window_start, now)

    assert stats.total == 2
    assert stats.outcomes["started"] == 1
    assert stats.outcomes["needs_human"] == 1
    assert pytest.approx(stats.total_spend_usd, 0.001) == 0.35
    assert len(stats.needs_human_items) == 1
    assert stats.needs_human_items[0]["candidate"] == "my-tool#1"


def test_compile_digest(tmp_path: Path):
    ledger_file = tmp_path / "ledger.jsonl"

    digest = compile_digest(
        root=tmp_path,
        window_hours=6.0,
        ledger_file=ledger_file,
    )

    assert digest.window_hours == 6.0
    assert isinstance(digest.to_dict(), dict)


def test_render_human_digest():
    now = datetime.now(UTC).isoformat()
    digest = GoalDigest(
        timestamp=now,
        window_hours=6.0,
        window_start=now,
        window_end=now,
        dispatch_stats=DispatchStats(
            total=3,
            outcomes={"started": 2, "needs_human": 1},
            total_spend_usd=0.45,
            spend_by_account={"acct-1": 0.45},
            needs_human_items=[{"candidate": "my-tool#1", "reason": "blocked capability"}],
        ),
        open_prs=[{"repo": "my-tool", "number": 5, "title": "feat: test", "url": "http://pr", "is_draft": False}],
        graph_audit_summary={"repos_scanned": 1, "repos_with_findings": 0, "total_clones": 0},
    )

    rendered = render_human_digest(digest)
    assert "Fleet Executive Digest (6h Window)" in rendered
    assert "Total Dispatches:** 3" in rendered
    assert "$0.4500" in rendered
    assert "my-tool#1" in rendered
    assert "my-tool#5" in rendered


def test_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["--root", str(tmp_path), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "window_hours" in data


def test_cli_human(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["--root", str(tmp_path), "--window-hours", "3.0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fleet Executive Digest (3h Window)" in out
