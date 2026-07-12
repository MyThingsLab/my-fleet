from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import myfleet.notify_usage as nu


class FakeTransport:
    instances: list[FakeTransport] = []

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.sent: list[str] = []
        FakeTransport.instances.append(self)

    def send_message(self, text: str) -> None:
        self.sent.append(text)


def usage(config_dir: str, pct: int = 95) -> SimpleNamespace:
    return SimpleNamespace(config_dir=config_dir, session_pct=pct, session_resets="18:00")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nu, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(nu, "HTTPTelegramTransport", FakeTransport)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    FakeTransport.instances = []


def run(usable: list, over: list, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(nu, "select_accounts", lambda dirs, pct: (usable, over))
    return nu.main(["--accounts", "a,b"])


def test_alerts_only_on_the_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run([usage("a", 10)], [usage("b")], monkeypatch) == 0
    (transport,) = FakeTransport.instances
    assert "hit 95% session usage" in transport.sent[0]
    assert "b" in transport.sent[0]

    # Same accounts still over: no new transition, nothing sent.
    assert run([usage("a", 10)], [usage("b")], monkeypatch) == 0
    assert len(FakeTransport.instances) == 1


def test_recovery_alerts_and_clears_state(monkeypatch: pytest.MonkeyPatch) -> None:
    run([], [usage("b")], monkeypatch)
    run([usage("b", 5)], [], monkeypatch)
    assert any("back in rotation" in m for t in FakeTransport.instances for m in t.sent)


def test_all_over_adds_the_stalled_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    run([], [usage("a"), usage("b")], monkeypatch)
    (transport,) = FakeTransport.instances
    assert any("Fleet dispatch is paused" in m for m in transport.sent)


def test_without_env_prints_but_never_sends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert run([], [usage("a")], monkeypatch) == 0
    assert FakeTransport.instances == []
    assert "hit 95% session usage" in capsys.readouterr().out
    # State still advances so a later configured run doesn't re-alert.
    assert nu.STATE_PATH.exists()
