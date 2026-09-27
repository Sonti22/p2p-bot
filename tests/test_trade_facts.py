"""Факты реальных сделок (этап 2.5): автосопоставление пишет чистый факт с fact_source=auto и заменяет им
«как расчёт»/±0.5; спот-сделки других площадок ногой P2P не считаются."""
import time

import p2p
import trades
from helpers import arun, make_ad
from test_bot import Stub, texts


def leg(side, amount, price, ts):
    return {"id": f"{side}{ts}", "side": side, "asset": "USDT", "fiat": "RUB", "amount": amount, "price": price, "ts": ts}


def _log(sell_ex="Bybit", route="внутри биржи", profit=5.0):
    d = (profit, make_ad("Bybit", "buy", 85.0), make_ad(sell_ex, "sell", 90.0), route)
    return trades.log_trade(d, 50000, ts=time.time())[0]


def _row(trade_id):
    return next(r for r in trades.export_rows(0) if r["id"] == trade_id)


def test_auto_match_replaces_plan_fact_with_net_auto_fact():
    trade_id = _log()
    trades.set_fact(trade_id, 5.0, source=trades.FACT_PLAN)          # владелец нажал «как расчёт»
    now = time.time()
    hist = {"bybit": [leg("buy", 588.24, 85.0, now), leg("sell", 588.24, 89.0, now + 300)]}
    bot = Stub(p2p.Config())
    arun(bot.auto_match_facts(hist))
    row = _row(trade_id)
    assert row["fact_source"] == "auto" and abs(row["fact"] - (89.0 / 85.0 - 1) * 100) < 1e-9
    (msg,) = texts(bot)
    assert f"#{trade_id}" in msg and "чистыми вместо «как расчёт»" in msg
    arun(bot.auto_match_facts(hist))                          # факт уже настоящий — второй раз не трогаем
    assert len(texts(bot)) == 1
    st = trades.stats()["day"]
    assert st["fact_count"] == 1 and st["plan_facts"] == 0


def test_auto_match_keeps_manual_fact_and_ignores_spot_history():
    manual = _log()
    trades.set_fact(manual, 1.0, source=trades.FACT_MANUAL)
    spot_only = _log(sell_ex="MEXC", route="перевод −1 USDT (TRC20) на MEXC")
    now = time.time()
    hist = {"bybit": [leg("buy", 588.24, 85.0, now), leg("sell", 588.24, 89.0, now + 300)],
            "mexc": [{"kind": "trade", "asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0, "ts": now}]}
    bot = Stub(p2p.Config())
    arun(bot.auto_match_facts(hist))
    assert texts(bot) == []
    assert _row(manual)["fact"] == 1.0 and _row(manual)["fact_source"] == "manual"
    assert _row(spot_only)["fact"] is None                           # у MEXC в истории нет P2P-ордеров
