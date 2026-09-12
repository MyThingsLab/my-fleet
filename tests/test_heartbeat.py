from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from mythings.ledger import Ledger

import myfleet.heartbeat as hb


def test_last_heartbeat_none_when_never_recorded(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    assert hb.last_heartbeat(ledger, "build") is None


def test_record_then_last_heartbeat_round_trips(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    hb.record(ledger, "build")
    last = hb.last_heartbeat(ledger, "build")
    assert last is not None
    assert (datetime.now(UTC) - last).total_seconds() < 5


def test_last_heartbeat_ignores_other_ticks(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    hb.record(ledger, "bookkeeping")
    assert hb.last_heartbeat(ledger, "build") is None


def test_stale_ticks_flags_missing_and_expired_and_leaves_fresh(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    hb.record(ledger, "build")
    now = datetime.now(UTC)
    stale = hb.stale_ticks(
        ledger,
        {"build": 60.0, "bookkeeping": 60.0, "extra": 60.0},
        now=now,
    )
    # build was just recorded -> fresh, not in the stale dict.
    assert "build" not in stale
    # bookkeeping/extra never recorded -> stale with a None last-seen.
    assert stale["bookkeeping"] is None
    assert stale["extra"] is None


def test_stale_ticks_flags_a_heartbeat_older_than_its_own_max_age(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    hb.record(ledger, "build")
    # A negative max age guarantees staleness regardless of the ledger
    # timestamp's second-level precision (no risk of a same-second tie).
    stale = hb.stale_ticks(ledger, {"build": -1.0}, now=datetime.now(UTC))
    assert "build" in stale
    assert stale["build"] is not None


def test_main_reports_fresh_when_both_ticks_recorded_recently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_path)
    hb.record(ledger, "build")
    hb.record(ledger, "bookkeeping")

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("should not send an alert when both heartbeats are fresh")

    monkeypatch.setattr(hb, "HTTPTelegramTransport", boom)
    rc = hb.main(["--ledger", str(ledger_path)])
    assert rc == 0
    assert "heartbeats fresh" in capsys.readouterr().out


def test_main_alerts_and_returns_nonzero_when_a_tick_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)  # never recorded -- both ticks are stale from the start
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    sent: list[str] = []

    class FakeTransport:
        def __init__(self, token: str, chat_id: str) -> None:
            assert token == "tok"
            assert chat_id == "chat"

        def send_message(self, message: str) -> None:
            sent.append(message)

    monkeypatch.setattr(hb, "HTTPTelegramTransport", FakeTransport)
    rc = hb.main(
        ["--ledger", str(ledger_path), "--max-age-build-min", "0", "--max-age-bookkeeping-min", "0"]
    )
    assert rc == 1
    assert len(sent) == 1
    assert "build" in sent[0] and "bookkeeping" in sent[0]
    out = capsys.readouterr().out
    assert "heartbeat is stale" in out


def test_main_falls_back_to_print_without_telegram_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not construct a transport without credentials")

    monkeypatch.setattr(hb, "HTTPTelegramTransport", boom)
    rc = hb.main(
        ["--ledger", str(ledger_path), "--max-age-build-min", "0", "--max-age-bookkeeping-min", "0"]
    )
    assert rc == 1
    assert "would have sent" in capsys.readouterr().out
