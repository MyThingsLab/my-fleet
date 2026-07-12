from __future__ import annotations

import pytest

import myfleet.notify_systemd_status as ns


class FakeTransport:
    instances: list[FakeTransport] = []

    def __init__(self, token: str, chat_id: str) -> None:
        self.sent: list[str] = []
        FakeTransport.instances.append(self)

    def send_message(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ns, "HTTPTelegramTransport", FakeTransport)
    monkeypatch.setattr(ns, "_journal_tail", lambda unit: f"{unit} exploded")
    FakeTransport.instances = []


def test_sends_alert_with_unit_and_journal_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    assert ns.main(["myserver.service"]) == 0
    (transport,) = FakeTransport.instances
    assert "systemd unit failed: myserver.service" in transport.sent[0]
    assert "myserver.service exploded" in transport.sent[0]


def test_without_env_reports_failure_and_never_sends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert ns.main(["myserver.service"]) == 1
    assert FakeTransport.instances == []
    assert "would have sent" in capsys.readouterr().out
