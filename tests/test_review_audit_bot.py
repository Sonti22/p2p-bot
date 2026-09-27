"""Аудит коммита adb2c47 (2026-09-27), пункты по боту: сумма круга и расчёт снимка (№3), валюта в автосопоставлении
журнала (№7), незавершённый вывод MEXC (№8), пометка «устарела» после сбоя Telegram (№14), старая кнопка удаления
избранного (№15)."""
import asyncio
import dataclasses
import time

import pytest

import accounts
import bot as B
import favorites
import p2p
import trades
from helpers import make_ad
from test_accounts import MEXC_EMPTY, _UrlJsonSession
from test_bot import Stub, _callbacks, photos, texts


# --- №3: % связки, сумма на карточке и запись «✅ Сделал» — из одного расчёта ---------------------------------------

ADS = [make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 86.0)]


def _scan_bot(monkeypatch, during_scan=None):
    """Бот, у которого скан собирает связку Bybit 85 → MEXC 86 под сумму того cfg, что ему передали."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "save_env", lambda *a, **k: None)
    bot = Stub(p2p.Config(amount=50000, assets=["USDT"], min_orders=0, min_rate=0))

    async def fake_scan(s, cfg, force_alt=False):
        if during_scan:
            during_scan(bot)
        await asyncio.sleep(0)
        return p2p.assemble(cfg, ADS, ref=85.5)
    monkeypatch.setattr(B, "scan", fake_scan)
    return bot


def test_amount_change_before_next_scan_keeps_card_and_journal_on_one_calc(monkeypatch):
    """Посчитали на 50 000 ₽ (+1.14%), сумму сменили на 1 000 ₽ до следующего скана: на 1 000 ₽ та же связка даёт
    −0.54%, а /best показывал «+1.14% на 1 000 ₽» и писал эту пару в журнал."""
    bot = _scan_bot(monkeypatch)
    bot.last = asyncio.run(bot.fresh_scan())
    d = bot.last.deals[0]
    assert d[0] == pytest.approx(1.142071, abs=1e-6)
    small = dataclasses.replace(bot.cfg, amount=1000)
    assert p2p._route(d[1], d[2], small, bot.last.spot)[0] == pytest.approx(-0.543529, abs=1e-6)

    bot.apply("amt:1000")
    asyncio.run(bot.show_best())
    card = photos(bot)[-1][1]
    assert f"{d[0]:+.2f}% чистыми</b> на {p2p._money(50000)} ₽" in card["caption"]
    asyncio.run(bot.on_callback({"id": "1", "data": _callbacks(card["markup"])["did"], "message": {"message_id": 9}}))
    day = trades.stats()["day"]
    assert day["count"] == 1 and day["amount"] == 50000 and day["avg_profit"] == pytest.approx(d[0])

    asyncio.run(bot.show_top())
    assert f"круг {p2p._money(50000)} ₽" in photos(bot)[-1][1]["caption"]


def test_amount_changed_during_scan_does_not_leak_into_that_scan(monkeypatch):
    """Сумму сменили, пока шёл скан: снимок собран по копии настроек на начало скана, а карточка — по ним же."""
    bot = _scan_bot(monkeypatch, during_scan=lambda b: b.apply("amt:1000"))
    snap = asyncio.run(bot.fresh_scan())
    assert snap.deals and snap.deals[0][0] == pytest.approx(1.142071, abs=1e-6)
    asyncio.run(bot.show_best(snap))
    assert f"на {p2p._money(50000)} ₽" in photos(bot)[-1][1]["caption"]
    assert bot.cfg.amount == 1000                    # новая сумма применится со следующего скана


# --- №7: RUB-журнал сопоставляется только с рублёвыми P2P-ордерами -----------------------------------------------

def _leg(side, price, fiat, ts, amount=100.0):
    return {"id": f"{side}{fiat}{ts}", "side": side, "asset": "USDT", "fiat": fiat, "amount": amount, "price": price,
            "ts": ts}


def _trade(ts, amount=9000.0):
    return {"id": 1, "ts": ts, "buy_ex": "Bybit", "buy_asset": "USDT", "sell_ex": "Bybit", "sell_asset": "USDT",
            "amount": amount, "profit": 2.0, "route": "внутри биржи"}


@pytest.mark.parametrize("fiat", ["INR", "KZT"])
def test_match_fact_skips_foreign_fiat_orders(fiat):
    now = time.time()
    foreign_sell = {"bybit": [_leg("buy", 90.0, "RUB", now), _leg("sell", 95.0, fiat, now + 60)]}
    foreign_buy = {"bybit": [_leg("buy", 90.0, fiat, now), _leg("sell", 95.0, "RUB", now + 60)]}
    assert trades.match_fact(_trade(now), foreign_sell) is None
    assert trades.match_fact(_trade(now), foreign_buy) is None
    # чужая валюта ближе по времени — берётся рублёвый ордер, а не она
    both = {"bybit": [_leg("buy", 90.0, "RUB", now), _leg("sell", 95.0, fiat, now + 10),
                      _leg("sell", 93.0, "rub", now + 600)]}
    assert trades.match_fact(_trade(now), both) == pytest.approx((93.0 / 90.0 - 1) * 100)


def test_auto_match_does_not_write_fact_from_inr_order():
    """Журнал 9 000 ₽: купил 100 USDT по 90 RUB, продал 100 USDT по 95 INR — это не «факт +5.56%»."""
    d = (2.0, make_ad("Bybit", "buy", 90.0), make_ad("Bybit", "sell", 92.0), "внутри биржи")
    now = time.time()
    trade_id = trades.log_trade(d, 9000, ts=now)[0]
    bot = Stub(p2p.Config())
    asyncio.run(bot.auto_match_facts({"bybit": [_leg("buy", 90.0, "RUB", now), _leg("sell", 95.0, "INR", now + 60)]}))
    assert texts(bot) == [] and trades.get_trade(trade_id) and trades.unmatched()[0]["id"] == trade_id
    asyncio.run(bot.auto_match_facts({"bybit": [_leg("buy", 90.0, "RUB", now), _leg("sell", 95.0, "RUB", now + 60)]}))
    assert len(texts(bot)) == 1 and trades.unmatched() == []
