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
