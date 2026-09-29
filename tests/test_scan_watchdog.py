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


def _ticks(bot, start, end=None, step=B.WATCHDOG_TICK):
    """Тики watchdog раз в step секунд с start по end включительно; вернуть, что ушло в Telegram за эти тики."""
    before = len(texts(bot))
    t = start
    while t <= (start if end is None else end):
        arun(bot.watchdog_check(now=t))
        t += step
    return texts(bot)[before:]


def test_alert_once_then_recovery():
    bot = _bot()
    bot.last_scan_ts = 1000.0
    assert _ticks(bot, 1060.0, 1000.0 + 5 * 60) == []                   # до 5 мин — ещё норма
    sent = _ticks(bot, 1000.0 + 6 * 60)
    assert len(sent) == 1 and bot.stall_alerted
    assert sent[0].startswith("⚠️ Скан стоит: последний успешный скан 6 мин назад") and "завис" in sent[0]
    assert _ticks(bot, 1000.0 + 7 * 60, 1000.0 + 20 * 60) == []         # один раз, без повторов
    bot.last_scan_ts = 1000.0 + 20 * 60 + 30
    assert _ticks(bot, 1000.0 + 21 * 60) == ["✅ Скан снова идёт."] and not bot.stall_alerted
    assert _ticks(bot, 1000.0 + 22 * 60) == []


def test_no_successful_scan_since_start_and_error_text():
    bot = _bot()
    bot.scan_errors, bot.scan_error = 7, "ValueError: <bad>"
    sent = _ticks(bot, 1000.0 + 10 * 60)
    assert len(sent) == 1 and "с запуска (10 мин) ни одного успешного скана" in sent[0]
    assert "Ошибок скана подряд: 7, последняя: ValueError: &lt;bad&gt;" in sent[0]
    assert _ticks(bot, 1000.0 + 11 * 60) == []                          # восстановления без скана нет


SLEEP = 480 * 60   # ПК спал 8 часов


def test_pc_sleep_skips_tick_and_measures_downtime_from_wake():
    """После сна ПК: ни ложного «⚠️ Скан стоит… 480 мин», ни «✅ снова идёт»; скан пошёл — тишина."""
    bot = _bot()
    bot.last_scan_ts = 1000.0
    assert _ticks(bot, 1060.0, 1120.0) == []
    wake = 1120.0 + SLEEP
    assert _ticks(bot, wake) == [] and bot.watchdog_wake_ts == wake     # тик после сна пропущен
    assert _ticks(bot, wake + 60, wake + 120) == []                     # простой от пробуждения — 1-2 мин
    for k in range(30):                                                 # скан после сна пошёл, каждую минуту
        bot.last_scan_ts = wake + 150 + 60 * k
        assert _ticks(bot, wake + 180 + 60 * k) == []
    assert not bot.stall_alerted


def test_pc_sleep_then_scan_does_not_resume_alerts_from_wake():
    bot = _bot()
    bot.last_scan_ts = 1000.0
    _ticks(bot, 1060.0, 1120.0)
    wake = 1120.0 + SLEEP
    assert _ticks(bot, wake, wake + 5 * 60) == []                       # 5 мин после пробуждения — ещё норма
    sent = _ticks(bot, wake + 6 * 60)
    assert len(sent) == 1 and sent[0].startswith("⚠️ Скан стоит: после пробуждения ПК (6 мин) ни одного успешного скана")


def test_alert_before_sleep_no_false_recovery_after_wake():
    """Алерт ушёл до сна; после пробуждения «✅ снова идёт» — только когда скан реально прошёл."""
    bot = _bot()
    bot.last_scan_ts = 1000.0
    assert len(_ticks(bot, 1060.0, 1000.0 + 6 * 60)) == 1 and bot.stall_alerted
    wake = 1000.0 + 6 * 60 + SLEEP
    assert _ticks(bot, wake, wake + 10 * 60) == [] and bot.stall_alerted
    bot.last_scan_ts = wake + 10 * 60 + 30
    assert _ticks(bot, wake + 11 * 60) == ["✅ Скан снова идёт."]


def test_alert_state_changes_only_when_telegram_accepted():
    """stall_alerted меняется только после ok от Telegram: не дошло — повтор на следующем тике."""
    bot = _bot()
    bot.last_scan_ts = 1000.0
    replies = [{"ok": False, "error_code": 429, "description": "Too Many Requests"}, {"ok": True},
               {"ok": False, "description": "Bad Gateway"}, {"ok": True}]
    sent = []

    async def call(method, **p):
        sent.append(p["text"])
        return replies.pop(0)
    bot.call = call
    arun(bot.watchdog_check(now=1000.0 + 6 * 60))
    assert len(sent) == 1 and not bot.stall_alerted                     # алерт не дошёл — состояние прежнее
    arun(bot.watchdog_check(now=1000.0 + 7 * 60))
    assert len(sent) == 2 and bot.stall_alerted                         # повтор дошёл
    arun(bot.watchdog_check(now=1000.0 + 8 * 60))
    assert len(sent) == 2 and all(t.startswith("⚠️ Скан стоит") for t in sent)
    bot.last_scan_ts = 1000.0 + 8 * 60 + 30
    arun(bot.watchdog_check(now=1000.0 + 9 * 60))
    assert bot.stall_alerted                                            # восстановление не дошло — повторим
    arun(bot.watchdog_check(now=1000.0 + 10 * 60))
    assert not bot.stall_alerted and sent[2:] == ["✅ Скан снова идёт."] * 2 and not replies


def test_alert_state_kept_when_send_raises():
    bot = _bot()
    bot.last_scan_ts = 1000.0

    async def call(method, **p):
        raise OSError("network down")
    bot.call = call
    with pytest.raises(OSError):
        arun(bot.watchdog_check(now=1000.0 + 6 * 60))
    assert not bot.stall_alerted


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


def test_watchdog_loop_notices_sleep_without_owner_chat(monkeypatch):
    """ПК спал, пока чата владельца не было: тики всё равно запоминаются — появился чат, ложного алерта нет."""
    bot = _bot()
    bot.chat_id = ""
    bot.last_scan_ts = 1000.0
    times = iter([1060.0, 1060.0 + SLEEP, 1120.0 + SLEEP])
    now = [1000.0]
    calls = []

    async def fake_sleep(t):
        calls.append(t)
        if len(calls) > 3:
            raise asyncio.CancelledError
        now[0] = next(times)
        if len(calls) == 3:
            bot.chat_id = "1"
    monkeypatch.setattr(B.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    with pytest.raises(asyncio.CancelledError):
        arun(bot.watchdog_loop())
    assert texts(bot) == [] and bot.watchdog_wake_ts == 1060.0 + SLEEP and not bot.stall_alerted


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
