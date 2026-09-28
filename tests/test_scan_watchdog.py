"""Watchdog цикла сканирования: скан целиком не проходит (завис или падает каждый раз) дольше SCAN_STALL_MINUTES —
один алерт владельцу, пошёл снова — сообщение о восстановлении; ошибки скана считаются подряд."""
import asyncio

import pytest

import bot as B
import p2p
from helpers import arun
from test_bot import Stub, snap, texts


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SCAN_STALL_MINUTES", raising=False)


def _bot(interval=20):
    bot = Stub(p2p.Config(interval=interval))
    bot.start_ts = 1000.0
    return bot


def test_alert_once_then_recovery():
    bot = _bot()
    bot.last_scan_ts = 1000.0
    assert bot.watchdog_message(now=1000.0 + 4 * 60) is None            # 4 мин — ещё норма
    text = bot.watchdog_message(now=1000.0 + 6 * 60)
    assert text.startswith("⚠️ Скан стоит: последний успешный скан 6 мин назад") and "завис" in text
    assert bot.watchdog_message(now=1000.0 + 20 * 60) is None           # один раз, без повторов
    bot.last_scan_ts = 1000.0 + 21 * 60
    assert bot.watchdog_message(now=1000.0 + 21 * 60 + 5) == "✅ Скан снова идёт."
    assert bot.watchdog_message(now=1000.0 + 21 * 60 + 10) is None


def test_no_successful_scan_since_start_and_error_text():
    bot = _bot()
    bot.scan_errors, bot.scan_error = 7, "ValueError: <bad>"
    text = bot.watchdog_message(now=1000.0 + 10 * 60)
    assert "с запуска (10 мин) ни одного успешного скана" in text
    assert "Ошибок скана подряд: 7, последняя: ValueError: &lt;bad&gt;" in text
    assert bot.watchdog_message(now=1000.0 + 11 * 60) is None           # восстановления без скана нет


def test_limit_from_env_and_not_below_three_intervals(monkeypatch):
    monkeypatch.setenv("SCAN_STALL_MINUTES", "2")
    assert _bot(interval=20).scan_stall_limit() == 120
    assert _bot(interval=60).scan_stall_limit() == 180                  # 3 × INTERVAL
    for bad in ("abc", "0", "-3", "nan"):
        monkeypatch.setenv("SCAN_STALL_MINUTES", bad)
        assert _bot().scan_stall_limit() == 300


def test_scan_loop_counts_failed_scans_and_resets_on_success(monkeypatch):
    bot = _bot()
    results = [RuntimeError("boom"), RuntimeError("boom2"), snap([])]

    async def fresh_scan(cfg=None, force_alt=False):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    bot.fresh_scan = fresh_scan
    bot.chat_id = ""   # без чата: только скан

    async def fast_sleep(t):
        if not results:
            raise asyncio.CancelledError
    monkeypatch.setattr(B.asyncio, "sleep", fast_sleep)
    seen = []
    orig = bot.track_liveness

    def track(s, now=None):
        seen.append(bot.scan_errors)
        return orig(s, now)
    bot.track_liveness = track
    with pytest.raises(asyncio.CancelledError):
        arun(bot.scan_loop())
    assert bot.scan_error == "RuntimeError: boom2" and seen == [0] and bot.scan_errors == 0


def test_watchdog_loop_sends_to_dev_topic(monkeypatch):
    bot = _bot()
    bot.chat_id = "1"
    ticks = []

    async def fake_sleep(t):
        ticks.append(t)
        if len(ticks) > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(B.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(B.time, "time", lambda: 1000.0 + 30 * 60)
    with pytest.raises(asyncio.CancelledError):
        arun(bot.watchdog_loop())
    assert ticks == [B.WATCHDOG_TICK, B.WATCHDOG_TICK]
    assert any(t.startswith("⚠️ Скан стоит") for t in texts(bot))


def test_watchdog_waits_for_owner_chat(monkeypatch):
    """Чата владельца ещё нет — алерт не «тратится»: появился чат — приходит."""
    bot = _bot()
    bot.chat_id = ""
    calls = []

    async def fake_sleep(t):
        calls.append(t)
        if len(calls) == 2:
            bot.chat_id = "1"
        if len(calls) > 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(B.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(B.time, "time", lambda: 1000.0 + 30 * 60)
    with pytest.raises(asyncio.CancelledError):
        arun(bot.watchdog_loop())
    assert [t for t in texts(bot) if t.startswith("⚠️ Скан стоит")]
