"""Хедж кругов (trading/hedge.py, этап 5): карточка после «✅ Сделал» только когда реальный хедж возможен, одноразовая
кнопка с TTL, открытие через journal.submit по заглушке биржи (плечо до ордера, один Sell Market, объём вниз к лоту и
≤ монета × 1.05), закрытие кнопкой / из /hedge / по сроку (и при выключенной торговле), unknown и rejected без повтора,
closed_by_venue, хаос на создании ордера, миграции базы. Сеть — нет (заглушки), ключи — фиктивные."""
import asyncio
import functools
import json
import sqlite3
import time
from collections import deque
from decimal import Decimal

import pytest

import bot as B
import p2p
import perp
from helpers import arun, make_ad
from perpfx import install, quote
from trading import hedge, journal, keys, switch, venues, wiring
from trading_stubs import CREDS, Book, Resp, Session, body, bybit_err, bybit_ok, fresh_journal

OWNER = 1
CREATE = ("POST", "/v5/order/create")
LEVER = ("POST", "/v5/position/set-leverage")
REF = 90.0                      # ₽ за USDT
COIN, AMOUNT = 0.00102, 6000.0  # 0.00102 BTC ≈ 66 USDT при 65 000: лот 0.001 → шорт 0.001, коэф. 0.98
ETH_COIN, ETH_AMOUNT = 0.081, 21870.0   # ≈ 243 USDT при 3000: лот 0.01 → 0.08, коэф. 0.99


class Stub(B.Bot):
    """Бот без сети: вызовы Telegram — в self.out; bot.s — заглушка биржи."""

    def __init__(self, session=None, replies=None):
        super().__init__(session, "x", str(OWNER), p2p.Config())
        self.out, self.replies = [], list(replies or [])

    async def call(self, method, **p):
        self.out.append((method, p))
        if method == "sendMessage" and self.replies:
            return self.replies.pop(0)
        return {"ok": True, "result": {"message_id": len(self.out)}}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def toasts(bot):
    return [p.get("text") for m, p in bot.out if m == "answerCallbackQuery"]


def buttons(bot, i=-1):
    msg = [p for m, p in bot.out if m == "sendMessage"][i]
    return [b["callback_data"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def all_buttons(bot):
    return [b["callback_data"] for m, p in bot.out if m == "sendMessage"
            for row in (p.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def press(data, chat=OWNER, sender=None, ctype="private"):
    return {"callback_query": {"id": "cq", "data": data, "from": {"id": chat if sender is None else sender},
                               "message": {"message_id": 7, "chat": {"id": chat, "type": ctype}}}}


def msg(chat, text, ctype="private"):
    return {"message": {"message_id": 5, "chat": {"id": chat, "type": ctype}, "text": text,
                        "from": {"id": chat, "first_name": "Вася"}}}


async def settle(bot):
    """Дождаться фоновых открытий/закрытий хеджа (задачи в wiring.link_of(bot).tasks)."""
    link = wiring.link_of(bot)
    while any(not t.done() for t in link.tasks):
        await asyncio.gather(*list(link.tasks), return_exceptions=True)
    await asyncio.sleep(0)


def creates(s):
    return s.sent(*CREATE)


def link_ids(s):
    return {body(c)["orderLinkId"] for c in creates(s)}


def db_set(hid, **fields):
    con = sqlite3.connect(hedge.DB_PATH)
    con.execute(f"UPDATE hedges SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), hid))
    con.commit()
    con.close()


def quotes(btc_fee=0.055, mid=65000.0, eth=False, bingx=False):
    """Свежие котировки перпов (как после perp.refresh): Bybit BTC; ETH и дешёвый BingX — по желанию."""
    now = time.time()
    qs = [quote("Bybit", "BTCUSDT", mid=mid, spread=1.0, lot=0.001, min_qty=0.001, fee=btc_fee, rate=0.0, ts=now,
                now=now)]
    if eth:
        qs.append(quote("Bybit", "ETHUSDT", mid=3000.0, spread=0.1, lot=0.01, min_qty=0.01, fee=0.055, rate=0.0,
                        ts=now, now=now))
    if bingx:
        qs.append(quote("BingX", "BTCUSDT", mid=mid, spread=0.1, lot=0.0001, min_qty=0.0001, fee=0.01, rate=0.0,
                        ts=now, now=now))
    install(*qs)


def exchange(create=None, status="Filled", **over):
    """«Биржа» Bybit: ордера исполняются сразу (status), плечо ставится; create — подмена создания ордера."""
    book = Book()
    routes = book.routes("bybit", **{"POST /v5/order/create": create or book.bybit_create(status=status),
                                     "POST /v5/position/set-leverage": bybit_ok({})}, **over)
    return book, Session(routes)


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    """Журнал, база хеджей и проверка ключей — в tmp; ключ Bybit фиктивный и «проверенный»; TRADING=1 confirm (и в
    процессе, и в .env, который бот перечитывает на лету); пороги gates не ограничивают (fresh_journal)."""
    perp.reset()
    monkeypatch.setattr(venues, "_RESOLVED", {})
    fresh_journal(monkeypatch, tmp_path)
    path = tmp_path / ".env"
    path.write_text("TRADING=1\nTRADING_MODE=confirm\n", encoding="utf-8")
    monkeypatch.setattr(wiring, "ENV_PATH", str(path))
    monkeypatch.setattr(keys, "raw_credentials", lambda v: CREDS)
    monkeypatch.setattr(switch, "STATE_PATH", str(tmp_path / "trading_state.json"))
    monkeypatch.setattr(hedge, "_silent", deque(maxlen=5))
    for k in ("HEDGE_VENUES", "HEDGE_MIN_AMOUNT_RUB", "HEDGE_RATIO", "HEDGE_RATIO_BAND", "HEDGE_MAX_HOURS",
              "HEDGE_ASSETS", "HEDGE_HOLD_MINUTES", "HEDGE_RESIDUAL", "PAPER_HEDGE", "PERPS", "PERP_MAX_AGE",
              "RISK_BUFFER", "TRADING_MAX_POSITION_USDT", "TRADING_MAX_TOTAL_USDT", "TRADING_DAILY_LOSS_USDT",
              "TRADING_MAX_LEVERAGE", "TG_GUESTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")
    yield path
    perp.reset()


async def offer_btc(bot, circle=12, coin=COIN, amount=AMOUNT):
    return await hedge.offer(bot, "trade", circle, "BTC", coin, amount, REF, 0.3)


async def opened(bot, circle=12):
    """Предложить и открыть хедж BTC кнопкой владельца (через bot.on_update → wiring.callback)."""
    hid = await offer_btc(bot, circle)
    assert hid
    await bot.on_update(press(buttons(bot)[0]))
    await settle(bot)
    assert hedge.get(hid)["status"] == "open"
    return hid


# --- открытие ---

def test_offer_then_open_sends_exactly_one_sell_market_after_leverage():
    quotes()
    book, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        card, data = texts(bot)[-1], buttons(bot)
        assert creates(s) == [] and s.sent(*LEVER) == []                    # карточка — не ордер
        await bot.on_update(press(data[0]))
        await settle(bot)
        return hid, card, data

    hid, card, data = arun(go())
    row = hedge.get(hid)
    assert "Хедж круга #12" in card and "Шорт 0.001 BTC на Bybit" in card and "плечо 2×" in card
    assert "изолированная" in card and "60 с" in card
    assert data[0].startswith(f"trd_hedge_open:{hid}:") and len(data[0].split(":")[2]) == 8
    assert data[1] == f"trd_hedge_skip:{hid}" and row["nonce"] == ""                  # nonce погашен нажатием
    assert len(creates(s)) == 1 and len(link_ids(s)) == 1
    b = body(creates(s)[0])
    assert b["side"] == "Sell" and b["orderType"] == "Market" and b["category"] == "linear"
    qty = Decimal(b["qty"])
    assert qty == Decimal("0.001") and qty % Decimal("0.001") == 0 and qty <= Decimal(str(COIN)) * Decimal("1.05")
    assert "reduceOnly" not in b and venues.CLIENT_ID_RE.fullmatch(b["orderLinkId"])
    order = [i for i, c in enumerate(s.calls) if (c["method"], c["path"]) in (LEVER, CREATE)]
    assert [(s.calls[i]["method"], s.calls[i]["path"]) for i in order] == [LEVER, CREATE]   # плечо — до ордера
    assert body(s.sent(*LEVER)[0])["buyLeverage"] == "2" == body(s.sent(*LEVER)[0])["sellLeverage"]
    assert row["status"] == "open" and row["open_cid"] == b["orderLinkId"] and row["grp"] == "cycle:trade:12"
    jr = journal.get(b["orderLinkId"])
    assert jr["strategy"] == "hedge" and jr["grp"] == "cycle:trade:12" and jr["purpose"] == "open"
    assert jr["mode"] == "confirm"
    assert "открыт" in texts(bot)[-1] and buttons(bot) == [f"trd_hedge_close:{hid}", f"trd_hedge_close:{hid}:f"]
    assert toasts(bot) == ["открываю шорт"]


def test_nonce_is_one_time_even_on_fast_double_press():
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        data = buttons(bot)[0]
        await bot.on_update(press(data))
        await bot.on_update(press(data))                                    # второй клик до конца отправки
        await settle(bot)
        await bot.on_update(press(data))                                    # и после
        return hid

    arun(go())
    assert len(creates(s)) == 1 and len(link_ids(s)) == 1
    assert toasts(bot)[0] == "открываю шорт" and all("уже решено" in t for t in toasts(bot)[1:])


def test_stale_button_sends_new_card_with_fresh_plan_and_new_nonce():
    quotes(mid=65000.0)
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        old = buttons(bot)[0]
        old_plan = json.loads(hedge.get(hid)["plan"])
        db_set(hid, offered_ts=time.time() - hedge.OFFER_TTL - 1)
        await hedge.tick(bot)                                              # цикл: карточка устарела
        assert hedge.get(hid)["status"] == "expired"
        quotes(mid=64000.0)                                                # котировка сдвинулась
        await bot.on_update(press(old))
        await settle(bot)
        assert creates(s) == [] and "устарела" in toasts(bot)[-1]          # старая кнопка ордер не шлёт
        new = buttons(bot)[0]
        assert new != old and new.startswith(f"trd_hedge_open:{hid}:") and "План пересчитан" in texts(bot)[-1]
        plan = json.loads(hedge.get(hid)["plan"])
        assert plan["mark"] == 64000.0 != old_plan["mark"]                   # старый план не используется
        await bot.on_update(press(old))                                    # старая — уже ничья
        assert "уже решено" in toasts(bot)[-1] and creates(s) == []
        await bot.on_update(press(new))
        await settle(bot)
        return hid

    hid = arun(go())
    assert len(creates(s)) == 1 and hedge.get(hid)["status"] == "open"


def test_skip_button_and_open_after_skip():
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        yes, no = buttons(bot)
        await bot.on_update(press(no))
        await bot.on_update(press(yes))
        await settle(bot)
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "skipped" and creates(s) == [] and "уже решено" in toasts(bot)[-1]


# --- когда карточки нет ---

def test_eth_hedged_only_from_20000_rub():
    quotes(eth=True)
    _, s = exchange()
    bot = Stub(s)
    small_coin = 0.0404                                                    # ≈ 10 900 ₽: лот сам по себе годится
    assert arun(hedge.offer(bot, "trade", 1, "ETH", small_coin, small_coin * 3000 * REF, REF, 0.5)) is None
    assert texts(bot) == [] and "HEDGE_MIN_AMOUNT_RUB" in hedge._silent[-1][2]
    hid = arun(hedge.offer(bot, "trade", 2, "ETH", ETH_COIN, ETH_AMOUNT, REF, 0.5))
    assert hid and "Шорт 0.08 ETH" in texts(bot)[-1]
    assert arun(hedge.offer(bot, "trade", 3, "BTC", COIN, AMOUNT, REF, 0.3))   # на BTC правило ETH не действует


def test_min_amount_setting_parsing_fails_closed(monkeypatch):
    monkeypatch.setenv("HEDGE_MIN_AMOUNT_RUB", "ETH:25000, BTC:abc,TON:5 000,junk")
    got = hedge.settings()["min_amount"]
    assert got["ETH"] == 25000 and got["BTC"] == float("inf") and got["TON"] == 5000 and "JUNK" not in got
    quotes()
    bot = Stub(exchange()[1])
    assert arun(offer_btc(bot)) is None and texts(bot) == []               # мусор — монету не хеджируем


def test_only_bybit_even_if_bingx_is_cheaper(monkeypatch):
    quotes(bingx=True)
    bot = Stub(exchange()[1])
    hid = arun(offer_btc(bot))
    plan = json.loads(hedge.get(hid)["plan"])
    assert plan["venue"] == "bybit" and plan["venue_name"] == "Bybit" and plan["alt"] == {}
    monkeypatch.setenv("HEDGE_VENUES", "bingx")                            # BingX пока не поддержан
    assert hedge.settings()["venues"] == []
    assert arun(offer_btc(bot, circle=13)) is None and "HEDGE_VENUES" in hedge._silent[-1][2]
    assert len(texts(bot)) == 1


def test_cost_gate_blocks_expensive_hedge():
    quotes(btc_fee=0.2)                                                    # 0.4% комиссий > 0.6 × 0.3%
    bot = Stub(exchange()[1])
    assert arun(offer_btc(bot)) is None and texts(bot) == []
    assert "дороже 0.6" in hedge._silent[-1][2]


def test_silent_when_trading_off_paper_no_keys_or_gates_paper(monkeypatch):
    quotes()
    bot = Stub(exchange()[1])
    monkeypatch.setenv("TRADING", "0")
    assert arun(offer_btc(bot, 1)) is None
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert arun(offer_btc(bot, 2)) is None
    monkeypatch.setenv("TRADING_MODE", "confirm")
    monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
    assert arun(offer_btc(bot, 3)) is None
    monkeypatch.setattr(keys, "raw_credentials", lambda v: CREDS)
    monkeypatch.setattr(journal, "_gate_mode", lambda strategy: "paper")
    assert arun(offer_btc(bot, 4)) is None
    assert texts(bot) == [] and hedge.rows() == []
    assert "gates" in hedge._silent[-1][2] and "ключ" in hedge._silent[-2][2]


def test_offer_never_raises(monkeypatch):
    quotes()
    monkeypatch.setattr(hedge, "build_plan", lambda *a, **k: 1 / 0)
    assert arun(offer_btc(Stub(exchange()[1]))) is None


def test_minlot_caps_short_to_minlot_limit(env, monkeypatch):
    env.write_text("TRADING=1\nTRADING_MODE=minlot\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_MODE", "minlot")
    quotes(eth=True)
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await hedge.offer(bot, "trade", 5, "ETH", ETH_COIN, ETH_AMOUNT, REF, 0.5)
        assert "урезан до потолка minlot" in texts(bot)[-1] and "Шорт 0.01 ETH" in texts(bot)[-1]
        await bot.on_update(press(buttons(bot)[0]))
        await settle(bot)
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "open" and body(creates(s)[0])["qty"] == "0.01"
    assert journal.get(hedge.get(hid)["open_cid"])["mode"] == "minlot"


# --- отказы и сбои: без повтора ---

def test_cross_margin_refused_with_isolated_hint_before_any_order():
    quotes()
    book, s = exchange()
    book.margin["bybit"] = "REGULAR_MARGIN"
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        await bot.on_update(press(buttons(bot)[0]))
        await settle(bot)
        return hid

    hid = arun(go())
    row = hedge.get(hid)
    assert row["status"] == "refused" and row["refusal_where"] == "margin"
    assert "Isolated margin" in texts(bot)[-1] and "не открыт" in texts(bot)[-1]
    assert creates(s) == [] and s.sent(*LEVER) == []


def test_trading_off_in_env_between_card_and_press_sends_nothing(env):
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        env.write_text("TRADING=0\nTRADING_MODE=confirm\n", encoding="utf-8")   # launcher / владелец на ПК
        await bot.on_update(press(buttons(bot)[0]))
        await settle(bot)
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "refused" and creates(s) == [] and s.sent(*LEVER) == []


def test_rejected_is_not_repeated():
    quotes()
    _, s = exchange(create=bybit_err(110007, "ab not enough for new order"))
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        data = buttons(bot)[0]
        await bot.on_update(press(data))
        await settle(bot)
        await bot.on_update(press(data))
        await hedge.tick(bot)
        await hedge.tick(bot)
        return hid

    hid = arun(go())
    row = hedge.get(hid)
    assert row["status"] == "rejected" and len(creates(s)) == 1
    assert any("110007" in t and "Повторять не буду" in t for t in texts(bot))
    assert "уже решено" in toasts(bot)[-1]


def test_unknown_never_resent_by_hedge_and_asks_pc_after_10_min():
    quotes()
    _, s = exchange(create=Resp(exc=asyncio.TimeoutError()))
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        await bot.on_update(press(buttons(bot)[0]))
        await settle(bot)
        n = len(creates(s))
        for _ in range(3):
            await hedge.tick(bot)
        assert len(creates(s)) == n                                        # хедж сам ничего не шлёт
        db_set(hid, unknown_ts=time.time() - hedge.UNKNOWN_LONG - 1)
        await hedge.tick(bot)
        await hedge.tick(bot)
        return hid

    hid = arun(go())
    row = hedge.get(hid)
    assert row["status"] == "unknown" and len(link_ids(s)) == 1 and row["open_cid"] in link_ids(s)
    assert any("не подтверждён" in t and "Повторно" in t for t in texts(bot))
    assert len([t for t in texts(bot) if "реши на ПК" in t]) == 1
    assert journal.blocking()                                              # ядро не даст открыть новое до разбора


@pytest.mark.parametrize("answer", [Resp(exc=asyncio.TimeoutError()), Resp(502, b""), Resp(200, b"<html>"),
                                    Resp(200, b'{"retCode":0,"result":'), Resp(503, {"retCode": 10016})],
                         ids=["timeout", "502", "html", "broken-json", "5xx-json"])
def test_chaos_on_order_create_gives_unknown_one_client_id(answer):
    quotes()
    _, s = exchange(create=answer)
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        await bot.on_update(press(buttons(bot)[0]))
        await settle(bot)
        await hedge.tick(bot)
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "unknown" and len(link_ids(s)) == 1


def test_chaos_timeout_after_venue_accepted_is_found_not_resent():
    quotes()
    book = Book()

    def create(call):
        book.bybit_create(status="Filled")(call)                           # биржа приняла, ответ потерялся
        return Resp(exc=asyncio.TimeoutError())

    s = Session(book.routes("bybit", **{"POST /v5/order/create": create,
                                        "POST /v5/position/set-leverage": bybit_ok({})}))
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        await bot.on_update(press(buttons(bot)[0]))
        await settle(bot)
        return hid

    hid = arun(go())
    assert len(creates(s)) == 1 and hedge.get(hid)["status"] == "open"


# --- закрытие ---

def test_close_button_buys_back_exactly_group_position_and_double_press_is_idempotent():
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await opened(bot)
        await bot.on_update(press(f"trd_hedge_close:{hid}"))
        await bot.on_update(press(f"trd_hedge_close:{hid}"))               # второй клик, пока закрывается
        await settle(bot)
        await bot.on_update(press(f"trd_hedge_close:{hid}"))               # и после
        await settle(bot)
        return hid

    hid = arun(go())
    row = hedge.get(hid)
    assert len(creates(s)) == 2
    b = body(creates(s)[1])
    assert b["side"] == "Buy" and b["orderType"] == "Market" and b["reduceOnly"] is True and b["qty"] == "0.001"
    assert row["status"] == "closed" and row["close_reason"] == "sold" and row["close_cid"] == b["orderLinkId"]
    assert row["exit_price"] and row["pnl_usdt"]
    assert journal.get(row["close_cid"])["purpose"] == "close" and journal.get(row["close_cid"])["grp"] == row["grp"]
    assert any("закрыт" in t and "монета продана" in t for t in texts(bot))
    assert "закрываю хедж" in toasts(bot) and "закрытие уже идёт" in toasts(bot) and "хедж уже закрыт" in toasts(bot)


def test_close_works_with_trading_off(env):
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await opened(bot)
        env.write_text("TRADING=0\nTRADING_MODE=paper\n", encoding="utf-8")
        switch.stop()
        await bot.on_update(press(f"trd_hedge_close:{hid}:f"))              # «🆘 Покупка не состоялась»
        await settle(bot)
        return hid

    hid = arun(go())
    row = hedge.get(hid)
    assert not switch.enabled() and row["status"] == "closed" and row["close_reason"] == "purchase_failed"
    assert body(creates(s)[-1])["reduceOnly"] is True and len(creates(s)) == 2
    assert any("покупка не состоялась" in t for t in texts(bot))


def test_close_pressed_while_opening_closes_right_after_open():
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        await bot.on_update(press(buttons(bot)[0]))
        await bot.on_update(press(f"trd_hedge_close:{hid}:f"))            # до конца открытия
        await settle(bot)
        return hid

    hid = arun(go())
    assert "шорт ещё открывается" in toasts(bot)[-1]
    assert hedge.get(hid)["status"] == "closed" and len(creates(s)) == 2
    assert body(creates(s)[1])["reduceOnly"] is True


def test_auto_close_after_max_hours_warns_risk_is_back(monkeypatch):
    monkeypatch.setenv("HEDGE_MAX_HOURS", "6")
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await opened(bot)
        await hedge.tick(bot)
        assert len(creates(s)) == 1                                        # рано
        db_set(hid, filled_ts=time.time() - 6 * 3600 - 1)
        await hedge.tick(bot)
        await hedge.tick(bot)
        return hid

    hid = arun(go())
    row = hedge.get(hid)
    assert row["status"] == "closed" and row["close_reason"] == "auto_timeout" and len(creates(s)) == 2
    msg_ = [t for t in texts(bot) if "закрыт автоматически" in t]
    assert len(msg_) == 1 and "Курсовой риск круга снова на тебе" in msg_[0] and "6 ч" in msg_[0]


def test_closed_by_venue_marked_once_and_core_event_left_for_wiring():
    quotes()
    book, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await opened(bot)
        await journal.reconcile(s, lambda v: CREDS, positions=False)      # исполнение шорта — в журнал
        book.position["BTCUSDT"] = Decimal(0)                              # ликвидация / владелец закрыл
        journal.sync_flat("bybit", "linear", "BTCUSDT")                    # сверка ядра: на бирже пусто
        await hedge.tick(bot)
        await hedge.tick(bot)
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "closed_by_venue" and len(creates(s)) == 1
    assert len([t for t in texts(bot) if "закрыла биржа" in t and "снова на тебе" in t]) == 1
    assert "closed_by_venue" in [e["event"] for e in journal.pending_events()]   # не тронуто: доставит wiring


def test_close_without_verified_key_tells_owner_to_close_by_hand(monkeypatch):
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await opened(bot)
        monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
        await bot.on_update(press(f"trd_hedge_close:{hid}"))
        await settle(bot)
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "open" and hedge.get(hid)["close_req"] == "sold" and len(creates(s)) == 1
    assert "вручную" in texts(bot)[-1]


# --- /hedge, кнопки только владельцу, цикл, bot.py ---

def test_hedge_command_lists_open_hedges_with_close_and_stop_buttons(monkeypatch):
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await opened(bot)
        await journal.reconcile(s, lambda v: CREDS, positions=False)
        await hedge.tick(bot)                                              # вход — из журнала
        await bot.on_update(msg(OWNER, "/hedge"))
        return hid

    hid = arun(go())
    text, kb = texts(bot)[-1], buttons(bot)
    assert "Хедж кругов" in text and "BTC 0.001" in text and "вход 65000" in text and "P&L" in text
    assert "до ликвидации" in text and "режим .env confirm" in text and "ключ Bybit: ✅ проверен" in text
    assert f"trd_hedge_close:{hid}:m" in kb and "trd_stop" in kb

    async def close():
        await bot.on_update(press(f"trd_hedge_close:{hid}:m"))
        await settle(bot)

    arun(close())
    assert hedge.get(hid)["status"] == "closed" and hedge.get(hid)["close_reason"] == "manual_hedge_cmd"


def test_hedge_buttons_and_command_only_for_owner_in_private_chat(monkeypatch):
    monkeypatch.setenv("TG_GUESTS", "42")
    quotes()
    _, s = exchange()
    bot = Stub(s)

    async def go():
        hid = await offer_btc(bot)
        data = buttons(bot)[0]
        for u in (press(data, ctype="group"), press(data, chat=42), press(data, chat=777),
                  press(data, sender=42)):                                  # кнопку в чате владельца нажал другой
            await bot.on_update(u)
        await settle(bot)
        await bot.on_update(msg(42, "/hedge"))
        await bot.on_update(msg(OWNER, "/hedge", ctype="group"))
        return hid

    hid = arun(go())
    assert hedge.get(hid)["status"] == "offered" and creates(s) == []
    assert not any("Хедж кругов" in t for t in texts(bot))


def test_wiring_tick_runs_hedge_tick_after_reconcile(monkeypatch):
    order = []

    async def reconcile(s, creds_for, now=None, positions=True):
        order.append("reconcile")
        return []

    async def htick(bot, now=None):
        order.append("hedge")

    monkeypatch.setattr(journal, "reconcile", reconcile)
    monkeypatch.setattr(hedge, "tick", htick)
    arun(wiring.tick(Stub()))
    assert order == ["reconcile", "hedge"]


def test_hedge_tick_never_raises(monkeypatch):
    quotes()
    arun(offer_btc(Stub(exchange()[1])))
    monkeypatch.setattr(hedge, "rows", lambda *a, **k: 1 / 0)
    assert arun(hedge.tick(Stub())) is None


def test_mark_done_offers_real_hedge_with_the_hedge_plans_coin_qty(tmp_path, monkeypatch):
    seen = []

    async def offer(bot, source, circle_id, asset, coin_qty, amount_rub, ref, risk_pct):
        seen.append((source, circle_id, asset, coin_qty, amount_rub, ref, risk_pct))

    monkeypatch.setattr(hedge, "offer", offer)
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=str(tmp_path / "t.db")))
    assert B.trading.hedge is hedge                                        # bot.py видит модуль через trading.wiring
    bot = Stub()
    d = (2.0, make_ad("Bybit", "buy", 84000 * REF, asset="BTC"), make_ad("Bybit", "sell", REF), "спот BTC→USDT")
    deal_id = bot.remember_deal(d, snap=p2p.Snapshot(REF, "test", {}, {}, [d], {}, {}, {}))
    arun(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    (src, cid, asset, coin, amount, ref, risk), = seen
    import hedge_plans
    assert (src, asset, amount, ref, risk) == ("trade", "BTC", bot.cfg.amount, REF, 0.3) and cid
    assert coin == pytest.approx(hedge_plans.coin_qty(d, bot.cfg.amount)) == pytest.approx(bot.cfg.amount / (84000 * REF))


def test_mark_done_survives_hedge_failure(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db")

    monkeypatch.setattr(hedge, "offer", boom)
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=str(tmp_path / "t.db")))
    bot = Stub()
    d = (2.0, make_ad("Bybit", "buy", 84000 * REF, asset="BTC"), make_ad("Bybit", "sell", REF), "r")
    deal_id = bot.remember_deal(d)
    arun(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    assert any(m == "answerCallbackQuery" for m, _ in bot.out) and bot.out[-1][0] == "editMessageReplyMarkup"


# --- база и кнопки ---

def test_db_migrations_are_idempotent_and_keep_rows(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE hedges (id INTEGER PRIMARY KEY AUTOINCREMENT, grp TEXT UNIQUE NOT NULL, status TEXT)")
    con.execute("INSERT INTO hedges (grp, status) VALUES ('cycle:trade:1', 'closed')")
    con.commit()
    con.close()
    for _ in range(3):
        with hedge._db(path) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(hedges)")}
    assert set(hedge._COLUMNS) <= cols
    row = hedge.get(1, path=path)
    assert row["status"] == "closed" and row["grp"] == "cycle:trade:1" and row["close_req"] == ""


def test_reads_do_not_create_the_database(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(hedge, "DB_PATH", str(tmp_path / "none" / "hedge_circles.db"))
    assert hedge.get(1) is None and hedge.rows() == []
    arun(hedge.tick(Stub()))
    assert not os.path.exists(hedge.DB_PATH)


def test_callback_data_fits_telegram_64_bytes():
    big = 10 ** 12
    kb = hedge.offer_kb(big, "f" * 8)["inline_keyboard"] + hedge.close_kb(big)["inline_keyboard"]
    data = [b["callback_data"] for row in kb for b in row] + [f"trd_hedge_close:{big}:m", "trd_hedge_view"]
    assert all(len(d.encode("utf-8")) <= 64 and d.startswith("trd_hedge_") for d in data)


def test_bad_callbacks_are_answered_not_raised():
    bot = Stub()
    for data in ("trd_hedge_open:x:y", "trd_hedge_close:1:z", "trd_hedge_open:99:abc", "trd_hedge_skip", "trd_hedge_"):
        toast = arun(wiring.callback(bot, press(data)["callback_query"], data, None))
        assert toast
    assert all(m == "answerCallbackQuery" for m, _ in bot.out)
