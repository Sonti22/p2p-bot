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
from test_bot import Stub, _callbacks, deal, msk_ts, photos, texts
from test_bot import snap as bot_snap


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


# --- ревью ветки: №8 и на HTX/KuCoin, депозиты MEXC — в ленту только успешные операции -----------------------------

TS = 1_790_000_000_000


def _htx(kind, state):
    rec = [{"currency": "usdt", "amount": 30, "created-at": TS, "state": state}] if state else []
    other = "withdraw" if kind == "deposit" else "deposit"
    return {f"type={kind}": {"status": "ok", "data": rec}, f"type={other}": {"status": "ok", "data": []}}


def _kucoin(kind, status):
    empty = {"code": "200000", "data": {"items": []}}
    rec = {"code": "200000", "data": {"items": [{"currency": "USDT", "amount": "30", "createdAt": TS,
                                                  "status": status}]}} if status else empty
    path = "api/v1/deposits" if kind == "deposit" else "api/v1/withdrawals"
    return {"api/v1/deposits": empty, "api/v1/withdrawals": empty, "api/v1/fills": empty, path: rec}


def _mexc(kind, status):
    rec = [{"coin": "USDT", "amount": "30", "insertTime": TS, "applyTime": "2026-09-27 10:00:00",
            "status": status}] if status else []
    return dict(MEXC_EMPTY, **{"capital/deposit/hisrec" if kind == "deposit" else "capital/withdraw/history": rec})


VENUES = {"htx": _htx, "kucoin": _kucoin, "mexc": _mexc}
LABEL = {"deposit": "пришёл депозит", "withdraw": "исполнен вывод"}


def _account_texts(tmp_path, monkeypatch, ex, kind, statuses):
    """Один и тот же депозит/вывод площадки ex проходит статусы statuses (после базового пустого опроса): что ушло
    в чат о депозите/выводе после каждого опроса — список на каждый статус."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key(ex, "k", "s", passphrase="pp" if ex == "kucoin" else None)
    bot = Stub(p2p.Config())
    bot.s = _UrlJsonSession(VENUES[ex](kind, None))
    asyncio.run(bot.check_accounts())                                   # первый опрос — база
    steps = []
    for status in statuses:
        bot.s = _UrlJsonSession(VENUES[ex](kind, status))
        before = len(bot.out)
        asyncio.run(bot.check_accounts())
        steps.append([p["text"] for m, p in bot.out[before:] if m == "sendMessage"
                      and ("депозит" in p["text"] or "вывод" in p["text"])])
    return steps


NOT_DONE = [("htx", "withdraw", ["submitted", "canceled"]), ("htx", "withdraw", ["pre-transfer", "wallet-reject"]),
            ("htx", "deposit", ["confirming", "orphan"]), ("kucoin", "withdraw", ["PROCESSING", "FAILURE"]),
            ("kucoin", "withdraw", ["WALLET_PROCESSING"]), ("kucoin", "deposit", ["PROCESSING", "FAILURE"]),
            ("mexc", "deposit", [4, 7]), ("mexc", "deposit", [6, 8])]
DONE = [("htx", "withdraw", ["submitted", "pass", "confirmed", "confirmed"], 2),       # (…, первый успешный статус)
        ("htx", "deposit", ["confirming", "confirmed", "safe"], 1),
        ("kucoin", "withdraw", ["PROCESSING", "WALLET_PROCESSING", "SUCCESS", "SUCCESS"], 2),
        ("kucoin", "deposit", ["PROCESSING", "SUCCESS"], 1), ("mexc", "deposit", [4, 5, 12], 1)]


@pytest.mark.parametrize("ex,kind,statuses", NOT_DONE)
def test_unfinished_or_failed_operation_is_not_announced(tmp_path, monkeypatch, ex, kind, statuses):
    """В обработке → не прошла/отменена: «исполнен вывод»/«пришёл депозит» в чат не уходит."""
    assert _account_texts(tmp_path, monkeypatch, ex, kind, statuses) == [[] for _ in statuses]


@pytest.mark.parametrize("ex,kind,statuses,done_at", DONE)
def test_pending_then_success_is_announced_once(tmp_path, monkeypatch, ex, kind, statuses, done_at):
    """В обработке → исполнена: ровно одно уведомление — на опросе, где операция впервые успешна."""
    steps = _account_texts(tmp_path, monkeypatch, ex, kind, statuses)
    assert [len(s) for s in steps] == [int(i == done_at) for i in range(len(statuses))]
    assert LABEL[kind] in steps[done_at][0] and "30 " in steps[done_at][0]


# --- ревью ветки: №3 и в утреннем дайджесте — % и сумма круга из одного снимка -------------------------------------

def test_night_digest_signs_deal_with_its_snapshot_amount(monkeypatch):
    """Ночью связку посчитали на 50 000 ₽ (+1.14%), до конца тихих часов сумму сменили на 1 000 ₽: в дайджесте
    «+1.14% на 50 000», а не «+1.14% на 1 000» (на 1 000 ₽ маршрут даёт −0.54%)."""
    bot = _scan_bot(monkeypatch)
    bot.quiet_on = True
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    snap = asyncio.run(bot.fresh_scan())
    asyncio.run(bot.quiet_and_pause_tick(snap))
    bot.apply("amt:1000")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))
    asyncio.run(bot.quiet_and_pause_tick(p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})))
    digest = [t for t in texts(bot) if "Топ-3 связки за ночь" in t]
    assert len(digest) == 1
    assert f"+1.14%</b> на {p2p._money(50000)} RUB" in digest[0] and f"на {p2p._money(1000)} RUB" not in digest[0]


# --- ревью ветки: живая правка и «⌛ устарела» не снимают кнопки с сигнальной карточки ------------------------------

def _live_bot(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1

    async def fake_send_photo(png, caption, markup=None):
        bot.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": True, "result": {"message_id": 555}}
    bot.send_photo = fake_send_photo
    return bot


def _caption_edits(bot):
    return [p for m, p in bot.out if m == "editMessageCaption"]


def test_live_edit_and_stale_mark_keep_card_buttons(monkeypatch):
    """Без reply_markup Telegram снимает с сообщения все кнопки: и живая правка, и «⌛ устарела» передают их."""
    bot = _live_bot(monkeypatch)
    asyncio.run(bot.notify(bot_snap([deal(5)])))
    bot.live_msg[next(iter(bot.live_msg))]["last_edit"] -= B.LIVE_EDIT_INTERVAL + 1
    asyncio.run(bot.notify(bot_snap([deal(5.1)])))
    live_edit = _caption_edits(bot)[-1]
    assert "did" in _callbacks(live_edit.get("reply_markup") or {"inline_keyboard": []})
    asyncio.run(bot.notify(bot_snap([])))                           # связка ушла из топа
    stale = _caption_edits(bot)[-1]
    assert "устарел" in stale["caption"]
    buttons = _callbacks(stale.get("reply_markup") or {"inline_keyboard": []})
    assert {"steps", "did", "fav", "bl"} <= set(buttons)
    asyncio.run(bot.on_callback({"id": "1", "data": buttons["did"], "message": {"message_id": 555}}))
    assert trades.stats()["day"]["count"] == 1 and trades.stats()["day"]["avg_profit"] == pytest.approx(5.1)


def test_stale_mark_after_done_keeps_buttons_without_journal(monkeypatch):
    """«✅ Сделал» уже нажали — «⌛ устарела» не возвращает журнальные кнопки, но и купить/продать не снимает."""
    bot = _live_bot(monkeypatch)
    asyncio.run(bot.notify(bot_snap([deal(5)])))
    did = _callbacks(photos(bot)[-1][1]["markup"])["did"]
    asyncio.run(bot.on_callback({"id": "1", "data": did, "message": {"message_id": 555}}))
    asyncio.run(bot.notify(bot_snap([])))
    stale = _caption_edits(bot)[-1]
    kb = (stale.get("reply_markup") or {"inline_keyboard": []})["inline_keyboard"]
    assert kb and not _callbacks({"inline_keyboard": kb})
    assert any("url" in b for row in kb for b in row)


def test_stale_mark_429_with_colored_buttons_waits_and_keeps_colors(monkeypatch):
    """429 на правке с цветными кнопками — не «цвета не поддерживаются»: без мгновенного повтора, цвета остаются,
    повтор — после retry_after, и уже с кнопками."""
    bot = _live_bot(monkeypatch)
    bot.fancy = True
    asyncio.run(bot.notify(bot_snap([deal(5)])))
    answers = [{"ok": False, "error_code": 429, "parameters": {"retry_after": 5}}, {"ok": True}]

    async def call(method, **p):
        bot.out.append((method, p))
        return answers.pop(0) if method == "editMessageCaption" else {"ok": True}
    bot.call = call
    now = [1_790_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    asyncio.run(bot.mark_stale_deals(set()))
    assert len(_caption_edits(bot)) == 1 and bot.fancy
    now[0] += 6
    asyncio.run(bot.mark_stale_deals(set()))
    edits = _caption_edits(bot)
    assert len(edits) == 2 and B.is_fancy(edits[-1]["reply_markup"]) and "did" in _callbacks(edits[-1]["reply_markup"])
    assert bot.live_msg[next(iter(bot.live_msg))]["stale"]


def test_live_edit_falls_back_to_plain_buttons(monkeypatch):
    """Цветные кнопки сервер не принял (400) — та же правка повторяется с обычными, кнопки на карточке остаются."""
    bot = _live_bot(monkeypatch)
    bot.fancy = True
    asyncio.run(bot.notify(bot_snap([deal(5)])))

    async def call(method, **p):
        bot.out.append((method, p))
        if B.is_fancy(p.get("reply_markup")):
            return {"ok": False, "error_code": 400, "description": "Bad Request: can't parse reply keyboard markup"}
        return {"ok": True}
    bot.call = call
    asyncio.run(bot.mark_stale_deals(set()))
    edits = _caption_edits(bot)
    assert len(edits) == 2 and not B.is_fancy(edits[-1]["reply_markup"]) and not bot.fancy
    assert "did" in _callbacks(edits[-1]["reply_markup"]) and bot.live_msg[next(iter(bot.live_msg))]["stale"]
