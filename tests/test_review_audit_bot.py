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


# --- №8: вывод MEXC «исполнен» — только со статусом 7 (SUCCESS) ---------------------------------------------------

WD = {"coin": "USDT", "amount": "30", "applyTime": "2026-09-27 10:00:00"}


def test_mexc_history_keeps_only_completed_withdrawals():
    session = _UrlJsonSession({"capital/deposit/hisrec": [], "capital/withdraw/history": [
        dict(WD, status=4), dict(WD, status=8, amount="31"), dict(WD, status=9, amount="32"),
        dict(WD, status=7, amount="33")]})
    hist = asyncio.run(accounts.mexc_history(session, "k", "s"))
    assert [(it["kind"], it["amount"]) for it in hist] == [("withdraw", 33.0)]


def test_mexc_withdraw_processing_then_success_notifies_once(tmp_path, monkeypatch):
    """4 (в обработке) → 8/9 не «исполнен вывод»; переход 4 → 7 — ровно одно уведомление о выводе."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    bodies = dict(MEXC_EMPTY)
    bot = Stub(p2p.Config())
    bot.s = _UrlJsonSession(bodies)
    asyncio.run(bot.check_accounts())                                   # первый опрос — база
    for status in (4, 8, 9, 4):
        bodies["capital/withdraw/history"] = [dict(WD, status=status)]
        asyncio.run(bot.check_accounts())
    assert not [t for t in texts(bot) if "вывод" in t]
    bodies["capital/withdraw/history"] = [dict(WD, status=7)]
    asyncio.run(bot.check_accounts())
    asyncio.run(bot.check_accounts())
    done = [t for t in texts(bot) if "вывод" in t]
    assert len(done) == 1 and "исполнен вывод" in done[0] and "30 USDT" in done[0]


# --- №14: пометка «⌛ устарела» — только после ответа ok, временные ошибки повторяем -------------------------------

KEY = ("Bybit", "USDT", "MEXC", "USDT")


def _stale_bot(monkeypatch, answers):
    """Бот с живой карточкой по KEY; editMessageText отвечает по очереди из answers (исключение — бросает)."""
    bot = Stub(p2p.Config())
    bot.live_msg[KEY] = {"message_id": 7, "photo": False, "deal_id": None, "last_edit": 0.0, "caption": "c",
                         "stale": False}
    answers = list(answers)

    async def call(method, **p):
        bot.out.append((method, p))
        r = answers.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    bot.call = call
    now = [1_790_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    return bot, now


def _edits(bot):
    return [p for m, p in bot.out if m == "editMessageText"]


def test_stale_mark_after_429_is_retried_after_retry_after(monkeypatch):
    too_many = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 5",
                "parameters": {"retry_after": 5}}
    bot, now = _stale_bot(monkeypatch, [too_many, {"ok": True}])
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_edits(bot)) == 1 and not bot.live_msg[KEY]["stale"]      # 429 — пометка не доставлена
    now[0] += 2
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_edits(bot)) == 1                                          # retry_after ещё не прошёл
    now[0] += 4
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_edits(bot)) == 2 and bot.live_msg[KEY]["stale"]          # вторая попытка — доставлено
    assert _edits(bot)[-1]["text"].endswith("⌛ <i>связка устарела</i>")
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_edits(bot)) == 2                                          # после доставки — больше не правим


def test_stale_mark_after_timeout_is_retried(monkeypatch):
    bot, now = _stale_bot(monkeypatch, [asyncio.TimeoutError(), {"ok": True}])
    asyncio.run(bot.mark_stale_deals(set()))
    assert not bot.live_msg[KEY]["stale"]
    now[0] += B.STALE_RETRY_BASE
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_edits(bot)) == 2 and bot.live_msg[KEY]["stale"]


def test_stale_mark_on_missing_message_is_not_retried(monkeypatch):
    """Сообщения больше нет (400) — повтор не поможет: помечаем и больше не пробуем."""
    gone = {"ok": False, "error_code": 400, "description": "Bad Request: message to edit not found"}
    bot, now = _stale_bot(monkeypatch, [gone])
    asyncio.run(bot.mark_stale_deals(set()))
    now[0] += B.STALE_RETRY_MAX
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_edits(bot)) == 1 and bot.live_msg[KEY]["stale"]


# --- №15: кнопка «✖» в /fav удаляет маршрут, который на ней написан ------------------------------------------------

def test_old_favdel_button_removes_only_its_route(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config())
    favorites.toggle(("HTX", "USDT", "KuCoin", "USDT"))
    favorites.toggle(("MEXC", "USDT", "Bybit", "USDT"))
    _, kb = bot.favorites_view()
    old = {b["text"]: b["callback_data"] for row in kb["inline_keyboard"] for b in row}
    press = {"id": "1", "data": old["✖ 1. HTX USDT → KuCoin USDT"], "message": {"message_id": 4}}
    favorites.toggle(("Bybit", "USDT", "MEXC", "USDT"))       # список сменился: новый маршрут встал первым
    asyncio.run(bot.on_callback(press))
    assert favorites.keys() == {"MEXC|USDT|Bybit|USDT", "Bybit|USDT|MEXC|USDT"}
    asyncio.run(bot.on_callback(press))                       # та же кнопка ещё раз — маршрута уже нет
    assert favorites.keys() == {"MEXC|USDT|Bybit|USDT", "Bybit|USDT|MEXC|USDT"}
    edit = [p for m, p in bot.out if m == "editMessageText"][-1]
    assert "уже нет" in edit["text"] and "Bybit USDT → MEXC USDT" in edit["text"]


def test_favdel_button_fits_telegram_callback_limit():
    """callback_data у Telegram — до 64 байт: и самый длинный маршрут (BestChange ↔ BitPapa) в кнопку влезает."""
    bot = Stub(p2p.Config())
    favorites.toggle(("BestChange", "USDT", "BitPapa", "USDT"))
    _, kb = bot.favorites_view()
    assert all(len(b["callback_data"].encode()) <= 64 for row in kb["inline_keyboard"] for b in row)
