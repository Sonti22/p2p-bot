"""План хеджа реальных сделок (hedge_plans.py): по «✅ Сделал» — запись «что открыл бы бот» (площадка, символ, объём,
ожидаемая стоимость по simperp.choose) в hedge_plans журнала сделок; без хеджа — причина; показ в /paper report.
Ордеров нет, торговое ядро не участвует."""
import functools
import time

import pytest

import bot as B
import hedge_plans
import p2p
import perp
import trades
from helpers import arun, make_ad
from perpfx import install, quote
from test_bot import Stub

RUB = 90.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    perp.reset()
    for k in ("PAPER_HEDGE", "HEDGE_RATIO", "HEDGE_RATIO_BAND", "HEDGE_ASSETS", "PERP_MAX_AGE", "PERPS"):
        monkeypatch.delenv(k, raising=False)
    yield
    perp.reset()


def _quotes(ts):
    install(quote("Bybit", "BTCUSDT", mid=84000, spread=1.0, lot=0.001, min_qty=0.001, fee=0.055, ts=ts, now=ts),
            quote("BingX", "BTCUSDT", mid=84000, spread=2.0, lot=0.0001, min_qty=0.0001, fee=0.05, ts=ts, now=ts))


def _btc_deal(price=84000 * RUB):
    return 2.0, make_ad("Bybit", "buy", price, asset="BTC"), make_ad("Bybit", "sell", RUB), "спот BTC→USDT на Bybit"


def test_record_plan_with_cheapest_venue(tmp_path):
    db, now = str(tmp_path / "trades.db"), 1_790_494_000.0
    _quotes(now)
    pid = hedge_plans.record(7, _btc_deal(), 10000, ref=RUB, risk=0.3, path=db, now=now)
    (r,) = hedge_plans.rows(db)
    assert pid == r["id"] and r["trade_id"] == 7 and r["asset"] == "BTC" and r["note"] == ""
    assert r["venue"] == "BingX" and r["symbol"] == "BTCUSDT"       # лот Bybit 0.001 — коэффициент вне полосы
    assert r["qty"] == pytest.approx(0.0013) and 0.9 <= r["ratio"] <= 1.1
    assert r["cost_pct"] > 0 and r["cost_usdt"] > 0
    lines = hedge_plans.report_lines(db)
    assert "сделок 1, хедж возможен в 1" in lines[1] and "BingX 1" in lines[2]
    assert "сделка #7 BTC: шорт 0.0013 на BingX (BTCUSDT)" in lines[-1]


def test_no_quote_records_reason(tmp_path):
    db = str(tmp_path / "trades.db")
    assert hedge_plans.record(3, _btc_deal(), 10000, ref=RUB, path=db, now=1_790_494_000.0)
    (r,) = hedge_plans.rows(db)
    assert r["venue"] == "" and "нет свежей котировки" in r["note"]
    assert any(x.startswith("без хеджа 1×") for x in hedge_plans.report_lines(db))


def test_usdt_or_hedge_off_writes_nothing(tmp_path, monkeypatch):
    db, now = str(tmp_path / "trades.db"), 1_790_494_000.0
    _quotes(now)
    usdt = (2.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 86.0), "перевод")
    assert hedge_plans.record(1, usdt, 10000, ref=RUB, path=db, now=now) is None
    monkeypatch.setenv("PAPER_HEDGE", "0")
    assert hedge_plans.record(2, _btc_deal(), 10000, ref=RUB, path=db, now=now) is None
    assert hedge_plans.rows(db) == [] and hedge_plans.report_lines(db) == []


def test_done_button_records_plan_and_report_shows_it(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(trades.log_trade, path=db))
    monkeypatch.setattr(B.hedge_plans, "record", functools.partial(hedge_plans.record, path=db))
    monkeypatch.setattr(B.hedge_plans, "report_lines", functools.partial(hedge_plans.report_lines, path=db))
    _quotes(time.time())
    bot = Stub(p2p.Config(amount=10000))
    snap = p2p.Snapshot(RUB, "t", {}, {}, [], {}, {}, {})
    deal_id = bot.remember_deal(_btc_deal(), bot.cfg, snap)
    arun(bot.on_callback({"id": "1", "data": f"did:{deal_id}", "message": {"message_id": 9}}))
    (r,) = hedge_plans.rows(db)
    trade = trades.get_trade(r["trade_id"], path=db)
    assert trade and trade["amount"] == 10000 and r["asset"] == "BTC" and r["venue"] == "BingX"
    assert "Хедж реальных сделок — план" in bot.paper_report_view([])   # без кругов прогона — тоже видно


def test_plan_failure_does_not_break_the_journal(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(trades.log_trade, path=db))

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(B.hedge_plans, "record", boom)
    bot = Stub(p2p.Config(amount=10000))
    deal_id = bot.remember_deal(_btc_deal(), bot.cfg, None)
    arun(bot.on_callback({"id": "1", "data": f"did:{deal_id}", "message": {"message_id": 9}}))
    assert trades.stats(path=db)["day"]["count"] == 1
