
import bot as B
import p2p
from helpers import arun, make_ad


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
    arun(bot.notify(s))
    assert not captions(bot)                                 # первый скан — не сигналим
    bot.track_liveness(s, now=1120)
    arun(bot.notify(s))
    assert len(captions(bot)) == 1 and "держится" in captions(bot)[0]


def test_live_scans_one_signals_immediately(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    s = snap([deal(3.0)])
    bot.track_liveness(s, now=1000)
    arun(bot.notify(s))
    assert len(captions(bot)) == 1 and "держится" not in captions(bot)[0]


KEY = ("Bybit", "USDT", "MEXC", "USDT")


def fresh_snap(ts, b_ts, s_ts, profit=3.0):
    """Скан, начатый в ts; стороны связки получены с площадки в b_ts / s_ts (раньше ts — из кэша прошлого скана)."""
    d = deal(profit)
    d[1].fetched_ts, d[2].fetched_ts = b_ts, s_ts
    return p2p.Snapshot(88.0, "t", {}, {}, [d], {}, {}, {}, ts=ts)


def test_cached_side_keeps_streak_unchanged():
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.track_liveness(fresh_snap(1000, 1001, 1002), now=1000)
    assert bot.live[KEY]["streak"] == 1
    bot.track_liveness(fresh_snap(1030, 1031, 1002), now=1030)      # продажа — из кэша прошлого скана
    assert bot.live[KEY] == {"first": 1000, "streak": 1}            # не растёт и не сбрасывается
    bot.track_liveness(fresh_snap(1060, 1002, 1061), now=1060)      # теперь из кэша покупка
    assert bot.live[KEY] == {"first": 1000, "streak": 1}
    bot.track_liveness(fresh_snap(1090, 1091, 1092), now=1090)      # обе стороны свежие — скан засчитан
    assert bot.live[KEY] == {"first": 1000, "streak": 2}
    bot.track_liveness(fresh_snap(1120, 1002, 1002, profit=1.0), now=1120)   # ниже порога — сброс, хоть и из кэша
    assert not bot.live


def test_new_route_seen_from_cache_starts_at_zero():
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.track_liveness(fresh_snap(1000, 1001, 900), now=1000)
    assert bot.live[KEY] == {"first": 1000, "streak": 0}
    assert bot.held_label(deal(3.0), now=1300) == ""
    bot.track_liveness(fresh_snap(1030, 1031, 1032), now=1030)
    assert bot.live[KEY] == {"first": 1000, "streak": 1}


def test_unknown_fetch_time_counts_as_fresh():
    """Время получения неизвестно (0 — связка собрана не сканом) или у снимка нет времени — как раньше, серия растёт."""
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.track_liveness(fresh_snap(1000, 0.0, 0.0), now=1000)
    bot.track_liveness(fresh_snap(0.0, 5.0, 5.0), now=1030)
    assert bot.live[KEY]["streak"] == 2


def test_signal_waits_for_second_fresh_scan(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 2
    for ts, b_ts, s_ts in ((1000, 1001, 1002), (1030, 1031, 1002), (1060, 1061, 1002)):
        s = fresh_snap(ts, b_ts, s_ts)
        bot.track_liveness(s, now=ts)
        arun(bot.notify(s))
    assert not captions(bot)                    # три скана выше порога, но продажа — одни и те же данные из кэша
    assert [r for _, _, r in bot.signal_reasons(s, 0)] == ["unconfirmed"]
    s = fresh_snap(1090, 1091, 1092)
    bot.track_liveness(s, now=1090)
    arun(bot.notify(s))
    assert len(captions(bot)) == 1


def test_alt_cache_scan_does_not_extend_streak(offline):
    """Живой конвейер на фикстурах: второй скан раньше ALT_INTERVAL берёт BTC из кэша _alt — связки с BTC серию не
    продлевают (и не сбрасывают), связки только на USDT (опрошены заново) — продлевают."""
    cfg = p2p.Config(assets=["USDT", "BTC"], exchanges=["bybit", "htx", "kucoin", "mexc"], min_profit=-100.0)
    bot = Stub(cfg)
    first = arun(p2p.scan(None, cfg))
    assert first.deals and all(p2p.deal_fresh(d, first) for d in first.deals)
    bot.track_liveness(first)
    second = arun(p2p.scan(None, cfg))
    assert any(j.get("cached") for j in second.jobs if j.get("asset") == "BTC")
    bot.track_liveness(second)
    streaks = {bot._deal_key(d): bot.live[bot._deal_key(d)]["streak"] for d in second.deals}
    usdt = {k for k in streaks if k[1] == k[3] == "USDT"}
    btc = set(streaks) - usdt
    assert usdt and btc
    assert all(streaks[k] == 2 for k in usdt)
    assert all(streaks[k] == 1 for k in btc)
    assert not any(p2p.deal_fresh(d, second) for d in second.deals if bot._deal_key(d) in btc)


def test_held_label_minutes():
    bot = Stub(p2p.Config(min_profit=2.0))
    d = deal(3.0)
    bot.track_liveness(snap([d]), now=1000)
    assert bot.held_label(d, now=1300) == ""                 # один скан — метки нет
    bot.track_liveness(snap([d]), now=1300)
    assert bot.held_label(d, now=1300) == "⏱ держится 5 мин · "
