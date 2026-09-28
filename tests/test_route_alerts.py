"""Алерт на прибыль связки: /alert route Bybit MEXC USDT 3% 7d — по snap.deals, со своим порогом и сроком."""
import sqlite3
import time

import alerts
import bot as B
import p2p
from helpers import arun, make_ad


def deal(profit, buy="Bybit", sell="MEXC", asset="USDT", stale=False):
    b, s = make_ad(buy, "buy", 85.0, asset=asset), make_ad(sell, "sell", 90.0, asset=asset)
    s.stale = stale
    return profit, b, s, "перевод −1 USDT (TRC20) на " + sell


def snap(deals):
    return p2p.Snapshot(88.0, "t", {}, {}, deals, {}, {}, {})


def test_route_due_best_matching_deal_over_threshold(tmp_path):
    db = str(tmp_path / "alerts.db")
    aid = alerts.add_route("1", "Bybit", "MEXC", "USDT", 3.0, time.time() + 3600, path=db)
    deals = [deal(2.9), deal(3.4), deal(3.2), deal(9.0, sell="HTX"), deal(9.0, asset="USDC"), deal(9.0, stale=True)]
    fired = alerts.route_due(snap(deals), p2p.Config(), path=db)
    assert len(fired) == 1
    fid, chat, buy, sell, asset, pct, d = fired[0]
    assert (fid, chat, buy, sell, asset, pct, d[0]) == (aid, "1", "Bybit", "MEXC", "USDT", 3.0, 3.4)
    assert alerts.route_due(snap([deal(2.9)]), p2p.Config(), path=db) == []


def test_route_alert_one_shot_repeat_and_expiry(tmp_path):
    db = str(tmp_path / "alerts.db")
    once = alerts.add_route("1", "Bybit", "MEXC", "USDT", 1.0, time.time() + 3600, path=db)
    rep = alerts.add_route("1", "Bybit", "MEXC", "USDT", 1.0, time.time() + 3600, path=db, repeat_cooldown=600)
    alerts.add_route("1", "Bybit", "MEXC", "USDT", 1.0, time.time() - 1, path=db)            # истёк
    s = snap([deal(2.0)])
    assert {f[0] for f in alerts.route_due(s, p2p.Config(), path=db)} == {once, rep}
    alerts.mark_fired(once, path=db)
    alerts.mark_fired(rep, path=db)
    assert alerts.route_due(s, p2p.Config(), path=db) == []                                  # once удалён, rep отдыхает
    later = time.time() + 601
    assert [f[0] for f in alerts.route_due(s, p2p.Config(), path=db, now=later)] == [rep]
    assert [r[0] for r in alerts.list_routes("1", path=db)] == [rep]


def test_route_alerts_do_not_leak_into_rate_alerts(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add_route("1", "Bybit", "MEXC", "USDT", 1.0, time.time() + 3600, path=db)
    best = {("MEXC", "sell", "USDT"): make_ad("MEXC", "sell", 999.0)}
    assert alerts.due(p2p.Snapshot(88.0, "t", {}, best, [], {}, {}, {}), p2p.Config(), path=db) == []
    assert alerts.list_all("1", path=db) == []


def test_reliable_route_alert_skips_trap(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    alerts.add_route("1", "Bybit", "MEXC", "USDT", 1.0, time.time() + 3600, path=db, require_reliable=True)
    monkeypatch.setattr(p2p, "reliability", lambda d, c, s: (p2p.TRAP, ["a", "b", "c"]))
    assert alerts.route_due(snap([deal(5.0)]), p2p.Config(), path=db) == []
    monkeypatch.setattr(p2p, "reliability", lambda d, c, s: (p2p.RELIABLE, []))
    assert len(alerts.route_due(snap([deal(5.0)]), p2p.Config(), path=db)) == 1


def test_old_database_upgraded_with_route_columns(tmp_path):
    db = str(tmp_path / "alerts.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, asset TEXT, side TEXT, "
                "rate REAL, created_ts REAL, expires_ts REAL)")
    con.execute("INSERT INTO alerts (chat_id, asset, side, rate, created_ts, expires_ts) "
                "VALUES ('1', 'USDT', 'sell', 92, 0, ?)", (time.time() + 3600,))
    con.commit()
    con.close()
    assert len(alerts.list_all("1", path=db)) == 1 and alerts.list_routes("1", path=db) == []


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def test_command_creates_lists_and_fires_route_alert():
    bot = Stub(p2p.Config())
    arun(bot.handle("/alert route bybit MEXC usdt 2.5% 7d reliable repeat 1h"))
    assert texts(bot)[-1].startswith("🔔 Алерт на связку создан: Bybit → MEXC USDT ≥2.5% чистыми, срок 7d, повтор")
    text, kb = B.alerts_view("1")
    assert "🔀 Bybit → MEXC USDT ≥2.5%" in text and kb["inline_keyboard"][0][0]["callback_data"].startswith("delalert:")
    arun(bot.check_alerts(snap([deal(3.1)])))
    msg = texts(bot)[-1]
    assert msg.startswith("🔔 <b>Алерт связки:</b> Bybit → MEXC USDT <b>+3.10%</b> чистыми (порог 2.5%)")
    arun(bot.check_alerts(snap([deal(3.1)])))
    assert texts(bot)[-1] == msg and len([t for t in texts(bot) if "Алерт связки" in t]) == 1   # кулдаун 1h


def test_command_rejects_bad_input():
    bot = Stub(p2p.Config())
    arun(bot.handle("/alert route Nowhere MEXC USDT 3% 7d"))
    assert texts(bot)[-1].startswith("Площадки:")
    arun(bot.handle("/alert route Bybit MEXC DOGE 3% 7d"))
    assert "не отслеживается" in texts(bot)[-1]
    arun(bot.handle("/alert route Bybit MEXC USDT 3% 100d"))
    assert "не больше 90d" in texts(bot)[-1]
    arun(bot.handle("/alert route Bybit MEXC USDT 3% 7d vol 50000"))
    assert texts(bot)[-1] == B.ALERT_HELP
