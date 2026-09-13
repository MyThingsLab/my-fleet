"""Tests for myfleet.ledger_audit — runtime ledger plausibility audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mythings.ledger import Ledger

from myfleet.ledger_audit import (
    _parse_ts,
    audit_ledger,
    format_human_report,
    main,
)


def test_parse_ts():
    ts = _parse_ts("2026-09-13T22:00:00Z")
    assert ts is not None
    assert ts.hour == 22


def test_clean_ledger(tmp_path: Path):
    ledger_file = tmp_path / "ledger.jsonl"

    now = datetime.now(UTC)
    ts1 = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts2 = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        json.dumps({"tool": "fleet_dispatch", "kind": "dispatch", "outcome": "ok", "ts": ts1}),
        json.dumps({"tool": "fleet_dispatch", "kind": "dispatch", "outcome": "ok", "ts": ts2}),
    ]
    ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = audit_ledger(ledger_file)
    assert res.is_plausible is True
    assert res.total_entries == 2
    assert res.dispatch_entries_count == 2
    assert res.last_dispatch_ts == ts2


def test_detects_synthetic_test_rows(tmp_path: Path):
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_file)

    ledger.record("fleet_cycle", "bookkeeping", "ok", synthetic=True)

    res = audit_ledger(ledger_file)
    assert res.is_plausible is False
    assert res.synthetic_rows_detected == 1
    assert any(a.kind == "synthetic_test" for a in res.anomalies)


def test_detects_burst_density_cadence(tmp_path: Path):
    ledger_file = tmp_path / "ledger.jsonl"

    # Simulate 20 rows written in sub-second bursts
    now_str = "2026-09-13T22:00:00Z"
    lines = [
        json.dumps({"tool": "fleet_cycle", "kind": "bookkeeping", "outcome": "ok", "ts": now_str})
        for _ in range(20)
    ]
    ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = audit_ledger(ledger_file)
    assert res.is_plausible is False
    assert res.burst_density_detected is True
    assert any(a.kind == "burst_density" for a in res.anomalies)


def test_format_human_report(tmp_path: Path):
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_file)
    ledger.record("fleet_dispatch", "dispatch", "ok")

    res = audit_ledger(ledger_file)
    report = format_human_report(res)
    assert "Fleet Ledger Plausibility Audit" in report
    assert "PLAUSIBLE" in report


def test_cli_audit(tmp_path: Path, capsys):
    ledger_file = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_file)
    ledger.record("fleet_dispatch", "dispatch", "ok")

    rc = main(["--ledger", str(ledger_file), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["is_plausible"] is True


def test_live_ledger_audit():
    # Test against live repository workspace ledger if present
    live_path = Path("/home/lollinux/Desktop/MyThingsLab/.my-fleet/ledger.jsonl")
    if live_path.exists():
        res = audit_ledger(live_path)
        assert isinstance(res.total_entries, int)
        assert isinstance(res.dispatch_entries_count, int)
