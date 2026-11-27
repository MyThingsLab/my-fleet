from __future__ import annotations

import json
from pathlib import Path

from mythings.ledger import Ledger

import myfleet.rows as rows


def test_record_writes_the_repository_name_with_the_component_and_the_old_word(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")

    rows.record(ledger, "fleet_cycle", "cycle", "stage_failed", "one stage", failed_stages=["x"])

    (entry,) = ledger
    assert (entry.tool, entry.kind, entry.outcome) == ("my-fleet", "dispatch", "failure")
    assert entry.data["component"] == "fleet_cycle"
    assert entry.data["type"] == "cycle"
    assert entry.data["outcome_was"] == "stage_failed"


def test_read_returns_rows_from_before_the_move_in_core_outcome_words(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    old = {"tool": "fleet_dispatch", "kind": "dispatch", "outcome": "failed", "detail": "x"}
    path.write_text(json.dumps(old) + "\n")
    ledger = Ledger(path)
    rows.record(ledger, "fleet_dispatch", "dispatch", "deferred", "y")

    read = rows.read(ledger, "fleet_dispatch", "dispatch")

    assert [e.outcome for e in read] == ["failure", "paused"]
    assert [rows.word_of(e) for e in read] == ["failed", "deferred"]


def test_read_keeps_one_component_apart_from_another(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    rows.record(ledger, "fleet_dispatch", "usage", "success", cost_usd=1.0)
    rows.record(ledger, "fleet_cycle", "heartbeat", "ok", tick="build")

    assert [e.data["cost_usd"] for e in rows.read(ledger, "fleet_dispatch", "usage")] == [1.0]
    assert rows.read(ledger, "fleet_cycle", "usage") == []
