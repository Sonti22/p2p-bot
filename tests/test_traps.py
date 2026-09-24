import asyncio
import time

import p2p


def test_scan_records_trap_for_anomalous_price(offline):
    c = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0,
                    min_rate=0, max_dev=0.01)   # почти любое отклонение — аномалия
    asyncio.run(p2p.scan(None, c))
    traps = p2p.recent_traps()
    assert traps
    t = traps[0]
    assert t["asset"] == "USDT" and t["side"] in ("buy", "sell")
    assert t["dev"] > t["max_dev"] == 0.01


def test_scan_does_not_record_traps_when_prices_are_normal(offline):
    c = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0,
                    min_rate=0, max_dev=100)   # порог заведомо не превысить
    asyncio.run(p2p.scan(None, c))
    assert p2p.recent_traps() == []


def test_recent_traps_newest_first_and_limited():
    p2p._traps.clear()
    for i in range(5):
        p2p._traps.append({"ts": i, "ex": "Bybit", "asset": "USDT", "side": "buy", "price": 90 + i,
                           "ref": 90, "dev": i, "max_dev": 4})
    assert [t["ts"] for t in p2p.recent_traps(3)] == [4, 3, 2]
    p2p._traps.clear()


def test_traps_deque_caps_at_max():
    p2p._traps.clear()
    for i in range(p2p.TRAPS_MAX + 10):
        p2p._traps.append({"ts": i, "ex": "Bybit", "asset": "USDT", "side": "buy", "price": 90,
                           "ref": 90, "dev": 5, "max_dev": 4})
    assert len(p2p._traps) == p2p.TRAPS_MAX
    p2p._traps.clear()


def test_fmt_traps_empty_message():
    assert "не отсеивал" in p2p.fmt_traps([])


def test_fmt_traps_shows_venue_price_and_deviation():
    trap = {"ex": "Bybit", "asset": "USDT", "side": "sell", "price": 120.0, "ref": 90.0,
            "dev": 33.3, "max_dev": 4.0, "ts": time.time()}
    text = p2p.fmt_traps([trap])
    assert "Bybit" in text and "USDT" in text and "33.3%" in text and "продать" in text and "подозрительно дорого" in text
