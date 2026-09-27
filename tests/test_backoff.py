import time

import p2p
from helpers import arun


def test_backoff_grows_then_caps_at_ten_minutes():
    p2p._venue_backoff.clear()
    delays = []
    for _ in range(7):
        p2p._venue_backoff_fail("bybit")
        delays.append(p2p._venue_backoff["bybit"]["delay"])
    assert delays == [30, 60, 120, 240, 480, 600, 600]   # растёт вдвое, дальше не выше 10 мин


def test_backoff_paused_until_reflects_current_delay(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(p2p.time, "time", lambda: t[0])
    p2p._venue_backoff.clear()
    p2p._venue_backoff_fail("bybit")
    assert p2p._venue_paused_until("bybit") == 1030.0     # 1000 + 30 (первая пауза)
    t[0] = 1031.0                                          # пауза истекла
    assert p2p._venue_paused_until("bybit") is None


def test_backoff_resets_after_success():
    p2p._venue_backoff.clear()
    p2p._venue_backoff_fail("bybit")
    assert p2p._venue_paused_until("bybit") is not None
    p2p._venue_backoff_ok("bybit")
    assert p2p._venue_paused_until("bybit") is None
    assert "bybit" not in p2p._venue_backoff


def test_scan_skips_paused_venue_and_marks_pause_in_errors(offline, monkeypatch):
    calls = []

    async def failing(s, cfg, side, asset):
        calls.append((side, asset))
        raise RuntimeError("boom")

    monkeypatch.setitem(p2p.FETCHERS, "fake", failing)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0)

    snap1 = arun(p2p.scan(None, c))   # первая ошибка — площадка ещё не была на паузе
    assert len(calls) == 2                    # buy + sell
    assert "fake/USDT" in snap1.errors
    assert p2p._venue_paused_until("fake") is not None

    snap2 = arun(p2p.scan(None, c))    # теперь площадка на паузе — фетчер не дёргаем
    assert len(calls) == 2                     # звонков не прибавилось
    assert "fake" in snap2.errors and "пауза до" in snap2.errors["fake"]


def test_scan_backoff_resets_after_pause_expires_and_success(offline, monkeypatch):
    calls = []
    fail = [True]

    async def flaky(s, cfg, side, asset):
        calls.append(1)
        if fail[0]:
            raise RuntimeError("boom")
        return []

    monkeypatch.setitem(p2p.FETCHERS, "fake", flaky)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0)

    arun(p2p.scan(None, c))
    assert p2p._venue_paused_until("fake") is not None
    assert len(calls) == 2

    p2p._venue_backoff["fake"]["until"] = time.time() - 1   # эмулируем окончание паузы
    fail[0] = False
    snap = arun(p2p.scan(None, c))
    assert len(calls) == 4                                   # снова опросили (buy+sell)
    assert "fake" not in snap.errors
    assert p2p._venue_paused_until("fake") is None            # сброс после успеха


def test_scan_pause_message_shows_hhmm(offline, monkeypatch):
    async def failing(s, cfg, side, asset):
        raise RuntimeError("boom")

    monkeypatch.setitem(p2p.FETCHERS, "fake", failing)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0)
    arun(p2p.scan(None, c))
    until = p2p._venue_paused_until("fake")
    snap = arun(p2p.scan(None, c))
    expected = time.strftime("%H:%M", time.localtime(until))
    assert snap.errors["fake"] == f"пауза до {expected}"
