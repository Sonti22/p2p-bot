import asyncio

import bot as B
import p2p
from helpers import make_ad


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}

    async def send_photo(self, png, caption, markup=None):
        self.out.append(("sendPhoto", {"caption": caption}))
        return {"ok": True}


def deal(profit=3.0, s_ex="MEXC"):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad(s_ex, "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def snap(deals):
    return p2p.Snapshot(88.0, "t", {}, {}, deals, {}, {}, {})


def captions(bot):
    return [p["caption"] for m, p in bot.out if m == "sendPhoto"]


def test_streak_counts_and_resets():
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.track_liveness(snap([deal(3.0)]), now=1000)
    bot.track_liveness(snap([deal(3.0)]), now=1030)
    assert bot.live[("Bybit", "USDT", "MEXC", "USDT")]["streak"] == 2
    bot.track_liveness(snap([deal(1.0)]), now=1060)        # упала ниже порога — сброс
    assert not bot.live
    bot.track_liveness(snap([deal(3.0)]), now=1090)
    assert bot.live[("Bybit", "USDT", "MEXC", "USDT")] == {"first": 1090, "streak": 1}


def test_notify_waits_for_second_scan(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    s = snap([deal(3.0)])
    bot.track_liveness(s, now=1000)
    asyncio.run(bot.notify(s))
    assert not captions(bot)                                 # первый скан — не сигналим
    bot.track_liveness(s, now=1120)
    asyncio.run(bot.notify(s))
    assert len(captions(bot)) == 1 and "держится" in captions(bot)[0]


def test_live_scans_one_signals_immediately(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    s = snap([deal(3.0)])
    bot.track_liveness(s, now=1000)
    asyncio.run(bot.notify(s))
    assert len(captions(bot)) == 1 and "держится" not in captions(bot)[0]


def test_held_label_minutes():
    bot = Stub(p2p.Config(min_profit=2.0))
    d = deal(3.0)
    bot.track_liveness(snap([d]), now=1000)
    assert bot.held_label(d, now=1300) == ""                 # один скан — метки нет
    bot.track_liveness(snap([d]), now=1300)
    assert bot.held_label(d, now=1300) == "⏱ держится 5 мин · "
