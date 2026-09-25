"""Исправления по проверке 26.09: /export за прошлые периоды, /blacklist в лимите Telegram, один обменник — один
контрагент, неотправленное сообщение — в логе."""
import asyncio
import datetime
import logging
import time

import blacklist
import bot as B
import p2p
import trades
from test_bot import Stub

MSK = trades.MSK


def ts(y, m, d, h=12):
    return datetime.datetime(y, m, d, h, tzinfo=MSK).timestamp()


def test_period_range_prev_month_prev_year_and_explicit_year():
    now = ts(2027, 3, 15)
    assert trades.period_range("prev", now) == (ts(2027, 2, 1, 0), ts(2027, 3, 1, 0))
    assert trades.period_range("prevyear", now) == (ts(2026, 1, 1, 0), ts(2027, 1, 1, 0))
    assert trades.period_range("2026", now) == (ts(2026, 1, 1, 0), ts(2027, 1, 1, 0))
    assert trades.period_range("year", now) == (ts(2027, 1, 1, 0), None)
    jan = ts(2027, 1, 10)
    assert trades.period_range("prev", jan) == (ts(2026, 12, 1, 0), ts(2027, 1, 1, 0))   # январь → декабрь


def test_export_rows_respects_until(tmp_path):
    db = str(tmp_path / "t.db")
    d = (2.0, p2p.Ad("Bybit", "buy", 85.0, 1000, 500000, 1e4, ["Tinkoff"], "m", 1000, 100.0),
         p2p.Ad("MEXC", "sell", 90.0, 1000, 500000, 1e4, ["SBP"], "k", 1000, 100.0), "r")
    trades.log_trade(d, 10000, path=db, ts=ts(2026, 12, 20))
    trades.log_trade(d, 20000, path=db, ts=ts(2027, 1, 5))
    rows = trades.export_rows(ts(2026, 1, 1, 0), path=db, until=ts(2027, 1, 1, 0))
    assert [r["amount"] for r in rows] == [10000]


def test_export_command_accepts_year_and_prev(monkeypatch):
    bot = Stub(p2p.Config())
    for arg in ("2026", "prev", "прошлый", "prevyear"):
        asyncio.run(bot.cmd_export(arg))
        assert "Формат:" not in bot.out[-1][1].get("text", ""), arg
    asyncio.run(bot.cmd_export("1999"))
    assert "Формат:" in bot.out[-1][1]["text"]


def test_blacklist_view_fits_telegram_limits():
    for i in range(60):
        blacklist.add("bybit", f"merchant_with_long_nick_{i:03d}")
    for entry_id, *_ in blacklist.list_all():
        blacklist.set_note(entry_id, "причина " * 40)
    text, kb = B.blacklist_view()
    assert len(text) <= 4096 and len(kb["inline_keyboard"]) <= 100
    assert "…и ещё" in text and "причина причина" in text and "…" in text


def test_bestchange_exchanger_counts_once_across_networks(tmp_path):
    db = str(tmp_path / "t.db")
    now = time.time()
    for net in ("TRC20", "BEP20", "ERC20"):
        b = p2p.Ad("Bybit", "buy", 85.0, 1000, 500000, 1e4, ["SBP"], "seller1", 1000, 100.0)
        s = p2p.Ad("BestChange", "sell", 90.0, 1000, 500000, 1e4, ["SBP"], f"Obmennik [{net}]", 1000, 100.0,
                   "", "USDT", net)
        trades.log_trade((2.0, b, s, "r"), 10000, path=db, ts=now)
    assert trades.counterparties("T-Bank", path=db, now=now) == (2, 2)


def test_failed_send_is_logged(caplog):
    class Fail(Stub):
        async def call(self, method, **p):
            self.out.append((method, p))
            return {"ok": False, "description": "Bad Request: message is too long"}
    bot = Fail(p2p.Config())
    with caplog.at_level(logging.WARNING):
        asyncio.run(bot.send("x" * 5000))
    assert "message is too long" in caplog.text
