from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mythings.ledger import Ledger

import myfleet.heartbeat as hb
from myfleet import workspace


def test_the_default_ledger_follows_an_explicit_fleet_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The failure this guards is silent in the worst way: under an isolation
    # Workspace the bare parents[3] climb lands in a /tmp scratch prefix, so the
    # switch reads a ledger nobody writes, sees no heartbeat, and a dead fleet
    # reports as alive. Both constants are module-level, so the reload is what
    # makes this a test of heartbeat's own resolution rather than of the seam it
    # calls -- assert against workspace.fleet_root directly and it passes just as
    # happily with the parents[3] climb still in place.
    monkeypatch.setenv(workspace.ROOT_ENV, str(tmp_path))
    reloaded = importlib.reload(hb)
    try:
        assert reloaded.WORKSPACE_ROOT == tmp_path
        assert reloaded.DEFAULT_LEDGER == workspace.ledger_path(tmp_path)
    finally:
        monkeypatch.delenv(workspace.ROOT_ENV, raising=False)
        importlib.reload(hb)


def test_the_switch_reads_the_ledger_the_cycle_actually_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The bug this replaces a green test with. The line above used to assert
    # DEFAULT_LEDGER == tmp_path / ".fleet-dispatch" / "ledger.jsonl" -- a
    # literal, so when #62 renamed the runtime dir to .my-fleet/ the writer moved
    # and the reader did not, and the test happily confirmed the stale path.
    # Nothing anywhere compared the two ends, so for weeks the switch reported
    # both ticks "never recorded" against an empty path while the real ledger
    # filled up beside it: a dead-man's switch that cannot tell dead from alive.
    #
    # Pinning them to each other rather than to a literal is the whole point --
    # rename the directory again and this fails only if the two disagree.
    monkeypatch.setenv(workspace.ROOT_ENV, str(tmp_path))
    reader = importlib.reload(hb)
    writer = importlib.reload(importlib.import_module("myfleet.fleet_dispatch"))
    try:
        assert reader.DEFAULT_LEDGER == writer.DISPATCH_LEDGER
    finally:
        monkeypatch.delenv(workspace.ROOT_ENV, raising=False)
        importlib.reload(hb)
        importlib.reload(writer)


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
    assert "fresh" in capsys.readouterr().out


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


def _fake_transport(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    class FakeTransport:
        def __init__(self, token: str, chat_id: str) -> None:
            pass

        def send_message(self, message: str) -> None:
            sent.append(message)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(hb, "HTTPTelegramTransport", FakeTransport)


STALE_NOW = ["--max-age-build-min", "0", "--max-age-bookkeeping-min", "0"]


def test_an_unfixed_stale_tick_is_reported_once_not_every_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flood this closes: the check timer fires on a cadence, the condition
    # does not change, and the operator got the same message every single run.
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)
    sent: list[str] = []
    _fake_transport(monkeypatch, sent)

    assert hb.main(["--ledger", str(ledger_path), *STALE_NOW]) == 1
    assert len(sent) == 1

    for _ in range(5):
        # Still broken, still nothing new to say -- and zero, so systemd's
        # OnFailure= hook does not send the copy the dedup just suppressed.
        assert hb.main(["--ledger", str(ledger_path), *STALE_NOW]) == 0
    assert len(sent) == 1


def test_an_unfixed_stale_tick_is_reported_again_after_the_realert_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)
    sent: list[str] = []
    _fake_transport(monkeypatch, sent)

    assert hb.main(["--ledger", str(ledger_path), *STALE_NOW]) == 1
    assert hb.main(["--ledger", str(ledger_path), *STALE_NOW, "--realert-hours", "0"]) == 1
    assert len(sent) == 2


def test_a_recovered_tick_is_announced_once_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    ledger = Ledger(ledger_path)
    sent: list[str] = []
    _fake_transport(monkeypatch, sent)

    hb.main(["--ledger", str(ledger_path), *STALE_NOW])  # both ticks alerted
    hb.record(ledger, "build")
    hb.record(ledger, "bookkeeping")

    # A recovery is good news: reported, but not an OnFailure= trigger.
    assert hb.main(["--ledger", str(ledger_path)]) == 0
    assert "is back" in sent[1]
    assert hb.main(["--ledger", str(ledger_path)]) == 0
    assert len(sent) == 2  # announced once, not on every subsequent healthy run


def test_a_failed_send_does_not_consume_the_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recording the alert before sending it would mean a transport outage
    # silenced the switch for the whole re-alert window.
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path)

    class ErrorTransport:
        def __init__(self, token: str, chat_id: str) -> None:
            pass

        def send_message(self, message: str) -> None:
            raise RuntimeError("telegram unreachable")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(hb, "HTTPTelegramTransport", ErrorTransport)
    with pytest.raises(RuntimeError):
        hb.main(["--ledger", str(ledger_path), *STALE_NOW])

    sent: list[str] = []
    _fake_transport(monkeypatch, sent)
    assert hb.main(["--ledger", str(ledger_path), *STALE_NOW]) == 1
    assert len(sent) == 1  # the retry still gets through


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
