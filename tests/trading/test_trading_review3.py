"""Торговое ядро, третье ревью: сценарии подтверждённых находок — теперь с исправленным поведением. Биржа — заглушка
Book (исполнения, условные стопы, отмена), сеть заблокирована, ключи фиктивные.

1     чужое на символе: стопы бота больше позиции ключа снимаются до любого раннего выхода; стопы, которые не доказать
      как только бота, снимаются, владельцу — «позиция бота без стопа»; доказуемо целая доля бота — стоп остаётся;
2     сделка владельца за секунды до открытия — открытие отложено (не «чужое навсегда»);
3, 8  открытие, закончившееся closed/unknown с частичным исполнением, получает стоп;
4     закрытие не ботом: худший убыток — изолированная маржа (кросс — весь номинал), все открытия ждут подтверждения;
5     сверка стопа не удалась — закрытие биржей не записывается (нет фантомной встречной позиции);
6     уменьшающий ордер ключ не переворачивает (излишек — другому ключу или событие); стоп при двух ключах — отказ;
7     сбой начислений фандинга BingX не пропускает перестановку стопа и не теряет начисления;
9     пыль спота списывается и группы фандинга не держит;
10    приращение исполнения — по своей цене; результат без цены уточняется, когда цена придёт;
11    отметка «в отправке» в строке: разбор из другого процесса ждёт; принятый повтор разобранной строки — событие;
12    направленная — без метки группы: встречная сторона под другой меткой не проходит;
13    стоп не подтверждён, цена за стопом — программный стоп закрывает позицию;
14    смена плеча — в потолках режима gates и позиции; торговля выключена — потолки minlot;
15    стоп ожидающего открытия — в бюджете худшего убытка;
16    пороги confirm/auto — только из локальных файлов владельца и журнала, не из словарей вызывающего;
17    закоммиченная статистика с другим регистром пути — «в git»;
18    BingX: кросс-позиция владельца на другом символе — открытие запрещено.
"""
import asyncio
import json
import os
import subprocess
import time
from decimal import Decimal as D

import pytest

from trading import gates, journal, risk, switch, venues
from trading_stubs import (CREDS, REAL_GATE_MODE, Book, Resp, Session, bingx_err, bingx_ok, body, business,
                           bybit_err, bybit_ok, fresh_journal, now_ms, run)

CREATE_BY, CREATE_BX = ("POST", "/v5/order/create"), ("POST", "/openApi/swap/v2/trade/order")
CANCEL_BY, CANCEL_BX = ("POST", "/v5/order/cancel"), ("DELETE", "/openApi/swap/v2/trade/order")
VSYM = {("bybit", "BTCUSDT"): "BTCUSDT", ("bybit", "ETHUSDT"): "ETHUSDT", ("bingx", "BTCUSDT"): "BTC-USDT",
        ("bingx", "ETHUSDT"): "ETH-USDT"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(venues, "_RESOLVED", {})
    fresh_journal(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")


def O(venue="bybit", symbol="BTCUSDT", side="sell", qty=None, order_type="market", **kw):
    category = kw.pop("category", "linear" if venue == "bybit" else "swap")
    qty = qty or ("0.001" if symbol == "BTCUSDT" else "0.01")
    return venues.Order(venue, category, symbol, side, order_type, qty, **kw)


def submit(s, order, **kw):
    if kw.get("purpose", "open") == "open":
        kw.setdefault("strategy", "hedge")
    return run(journal.submit(s, order, CREDS, **kw))


def recon(s, positions=True):
    return run(journal.reconcile(s, lambda v: CREDS, positions=positions))


def filled_routes(book, venue="bybit", **over):
    if venue == "bybit":
        return book.routes("bybit", **{"POST /v5/order/create": book.bybit_create(status="Filled")}, **over)
    return book.routes("bingx", **{"POST /openApi/swap/v2/trade/order": book.bingx_create(status="FILLED")}, **over)


def own(venue="bybit", symbol="BTCUSDT", side="sell", qty=None, strategy="hedge", group="", price="65000", book=None):
    """Исполненная позиция бота в журнале (и на «бирже», если дана)."""
    o = O(venue, symbol, side, qty)
    row = journal._insert_intent(o, "open", strategy, "confirm", None, group=group)
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="filled", filled=venues.fmt(o.qty), avg_price=price)
    if book is not None:
        sym = VSYM[(venue, symbol)]
        book.position[sym] = book.position.get(sym, D(0)) + (o.qty if side == "buy" else -o.qty)
    return journal.get(row["client_id"])


def foreign_exec(book, symbol="BTCUSDT", side="Buy", qty="0.001", kind="Trade", price="65000", ago_ms=0):
    """Исполнение без id бота (Bybit): ручная сделка владельца — Trade, ликвидация — BustTrade."""
    book.foreign_execs.append({"symbol": symbol, "orderLinkId": "", "orderId": f"x{len(book.foreign_execs)}",
                               "side": side, "execQty": qty, "execPrice": price, "execTime": str(now_ms() - ago_ms),
                               "execType": kind, "execId": f"f{len(book.foreign_execs)}", "execFee": "0"})


def events():
    return [e["event"] for e in journal.pending_events(500)]


def notes(event):
    return [e["note"] for e in journal.pending_events(500) if e["event"] == event]


def stops_by(s):
    return [body(c) for c in s.sent(*CREATE_BY) if "triggerPrice" in body(c)]


def stops_bx(s):
    return [business(c) for c in s.sent(*CREATE_BX) if business(c)["type"] == "STOP_MARKET"]


def live_stops(book):
    return [o for o in book.orders.values() if o.get("orderStatus") == "Untriggered"
            or (o.get("type") == "STOP_MARKET" and o.get("status") == "NEW")]


def flaky(answer, fails=1):
    """Ответ биржи: первые fails вызовов — таймаут (исход неясен), потом — как обычно."""
    left = {"n": fails}

    def call(c):
        if left["n"] > 0:
            left["n"] -= 1
            return Resp(exc=asyncio.TimeoutError())
        return answer(c)
    return call


def pnl(kind=None):
    rows = journal._rows("SELECT amount, kind FROM pnl")
    return sum((D(r["amount"]) for r in rows if kind is None or r["kind"] == kind), D(0))


# --- 1. чужое на символе: стопы бота, которые могли бы закрыть позицию владельца, снимаются ---

def test_oversize_stop_after_failed_resize_is_removed_before_owner_add_is_cut():
    """Лонг бота 0.002 со стопом 0.002; частичное закрытие 0.001, перестановка стопа не удалась (снятие — таймаут):
    стоп 0.002 на позицию бота 0.001. Владелец добавил свой лонг 0.01 — сверка снимает лишний стоп (свой ордер по своему
    id — владельца не касается), событие «позиция без стопа»; цена у стопа — позиция владельца цела."""
    book = Book()
    s = Session(filled_routes(book, **{"POST /v5/order/cancel": flaky(book.bybit_cancel)}))
    assert submit(s, O(side="buy", qty="0.002", stop_loss="60000"), strategy="directional")["state"] == "filled"
    res = submit(s, O(side="sell", qty="0.001", reduce_only=True), purpose="close", strategy="directional")
    assert res["state"] in ("filled", "closed")
    assert "stop_missing" in events() and [o["qty"] for o in live_stops(book)] == ["0.002"]
    book.position["BTCUSDT"] += D("0.01")                                     # владелец добавил свой лонг
    foreign_exec(book, side="Buy", qty="0.01")
    got = recon(s)
    assert ("foreign", "BTCUSDT") in [(e, r["symbol"]) for e, r in got]
    assert live_stops(book) == [] and "stop_removed" in events()
    assert any("БЕЗ СТОПА" in n for n in notes("stop_removed"))
    book.trigger("BTCUSDT", "60000")
    assert book.position["BTCUSDT"] == D("0.011")                            # позиция владельца не тронута
    recon(s)
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == D("0.001")   # фантома нет
    res = submit(s, O("bybit", "ETHUSDT"), strategy="hedge")
    assert res["state"] == "refused" and "без полного стопа" in res["reason"]     # открытия стоят, пока стопа нет


def test_partial_liquidation_removes_bot_stop_before_owner_position_appears():
    """Частичная ликвидация (BustTrade без id бота) — доля бота не доказана: стоп бота снимается сразу, владельцу —
    событие. Потом владелец открывает свой лонг — цена у стопа, позиция владельца цела."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", qty="0.002", stop_loss="60000"), strategy="directional")["state"] == "filled"
    book.position["BTCUSDT"] -= D("0.001")
    foreign_exec(book, side="Sell", qty="0.001", kind="BustTrade", price="61000")
    recon(s)
    assert "foreign" in events() and "stop_removed" in events() and live_stops(book) == []
    assert len(s.sent(*CANCEL_BY)) == 1
    book.position["BTCUSDT"] += D("0.01")
    foreign_exec(book, side="Buy", qty="0.01")
    recon(s)
    book.trigger("BTCUSDT", "60000")
    assert book.position["BTCUSDT"] == D("0.011")


def test_owner_close_and_reopen_between_watches_removes_bot_stop():
    """Владелец закрыл всю позицию символа (вместе с долей бота) и открыл свой лонг 0.01 до следующей сверки: «чужое»,
    исполнение владельца в обратную сторону — стоп бота снимается, по позиции владельца он не сработает."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    book.position["BTCUSDT"] = D(0)
    foreign_exec(book, side="Sell", qty="0.001")
    book.position["BTCUSDT"] = D("0.01")
    foreign_exec(book, side="Buy", qty="0.01")
    recon(s)
    assert "foreign" in events() and "stop_removed" in events() and "closed_by_venue" not in events()
    book.trigger("BTCUSDT", "60000")
    assert book.position["BTCUSDT"] == D("0.01")


def test_flat_venue_with_unknown_row_removes_orphan_stop():
    """Неясная строка бота на символе выключает «закрыла биржа», а на бирже пусто: стоп бота защищать нечего — снят
    (иначе сработал бы по будущей позиции владельца). Потом владелец открывает лонг — цена у стопа, позиция цела."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    row = journal._insert_intent(O(side="sell", reduce_only=True), "close", "directional", "confirm", None)
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="unknown", note="test: ambiguous")
    book.position["BTCUSDT"] = D(0)                                           # владелец закрыл позицию бота
    foreign_exec(book, side="Sell", qty="0.001")
    real = journal._reconcile_one

    async def skip_unknown(s_, r, c):
        return None if r["client_id"] == row["client_id"] else await real(s_, r, c)
    journal._reconcile_one = skip_unknown
    try:
        recon(s)
        assert "closed_by_venue" not in events() and "stop_removed" in events() and live_stops(book) == []
        book.position["BTCUSDT"] = D("0.01")
        foreign_exec(book, side="Buy", qty="0.01")
        recon(s)
    finally:
        journal._reconcile_one = real
    book.trigger("BTCUSDT", "60000")
    assert book.position["BTCUSDT"] == D("0.01")


def test_bingx_oversize_stop_is_removed_when_owner_adds():
    book = Book()
    s = Session(filled_routes(book, "bingx", **{"DELETE /openApi/swap/v2/trade/order": flaky(book.bingx_cancel)}))
    opened = submit(s, O("bingx", qty="0.002", side="buy", stop_loss="60000"), strategy="directional")
    assert opened["state"] == "filled"
    res = submit(s, O("bingx", side="sell", qty="0.001", reduce_only=True), purpose="close", strategy="directional")
    assert res["state"] in ("filled", "closed")
    assert [o["origQty"] for o in live_stops(book)] == ["0.002"]
    book.position["BTC-USDT"] += D("0.01")
    book.foreign_execs.append({"symbol": "BTC-USDT", "orderId": 5, "side": "BUY", "type": "MARKET",
                               "executedQty": "0.01", "avgPrice": "65000", "clientOrderId": "",
                               "updateTime": now_ms(), "status": "FILLED"})
    recon(s)
    assert live_stops(book) == [] and "stop_removed" in events()
    book.trigger("BTC-USDT", "60000")
    assert book.position["BTC-USDT"] == D("0.011")


def test_owner_only_added_bot_stop_of_bot_size_stays_and_closes_only_bot_share():
    """Владелец только добавил к позиции символа (его исполнения — в сторону позиции бота и ровно объясняют разницу):
    доля бота доказуемо цела — стоп бота на его размер остаётся и закрывает только её."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    book.position["BTCUSDT"] += D("0.01")
    foreign_exec(book, side="Buy", qty="0.01")
    recon(s)
    assert "foreign" in events() and "stop_removed" not in events() and not s.sent(*CANCEL_BY)
    book.trigger("BTCUSDT", "60000")
    assert book.position["BTCUSDT"] == D("0.01")                             # закрылась только доля бота
    recon(s)
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0


def test_bot_share_intact_rules():
    book = {"net": D("0.001"), "uncertain": [], "client_ids": {"t1"}}

    def snap(net, execs, errors=()):
        return journal.ownership.Snapshot("bybit", "linear", "BTCUSDT", "BTCUSDT", {"net": D(net), "rows": []}, [],
                                          None, None, None, None, None, tuple(errors), time.time(), None, {}, execs)

    def ex(side, qty, cid="", kind="Trade"):
        return {"client_id": cid, "side": side, "qty": D(qty), "kind": kind, "ts": 1, "order_id": "", "exec_id": "",
                "price": None, "fee": None}
    assert journal._bot_share_intact(snap("0.001", []), book)                        # только ордер владельца
    assert journal._bot_share_intact(snap("0.011", [ex("buy", "0.01")]), book)       # владелец добавил
    assert not journal._bot_share_intact(snap("0.011", []), book)                     # не объяснено исполнениями
    assert not journal._bot_share_intact(snap("0.01", [ex("sell", "0.001"), ex("buy", "0.01")]), book)
    assert not journal._bot_share_intact(snap("0.0005", [ex("sell", "0.0005", kind="BustTrade")]), book)
    assert not journal._bot_share_intact(snap("0.011", None), book)                   # исполнения не прочитаны
    assert not journal._bot_share_intact(snap("0.011", [ex("buy", "0.01")]), dict(book, uncertain=["x"]))


# --- 2. сделка владельца за секунды до открытия ---

def test_owner_trade_seconds_before_open_defers_the_open():
    """Владелец закрыл свой лонг за секунду до открытия бота (на бирже пусто): открытие откладывается (после исполнения
    бота его сделка попала бы в окно отметки сверки — символ «чужой» навсегда, без стопа). Позже — открытие со стопом,
    сверка не видит чужого, закрытие работает."""
    book = Book()
    foreign_exec(book, side="Sell", qty="0.02")
    s = Session(filled_routes(book))
    res = submit(s, O(side="buy", stop_loss="60000"), strategy="directional")
    assert res["state"] == "refused" and "без id бота" in res["reason"] and not s.sent(*CREATE_BY)
    book.foreign_execs[-1]["execTime"] = str(now_ms() - 10_000)              # прошло 10 с
    res = submit(s, O(side="buy", stop_loss="60000"), strategy="directional")
    assert res["state"] == "filled" and len(stops_by(s)) == 1
    recon(s)
    recon(s)
    assert "foreign" not in events() and "stop_missing" not in events()
    close = submit(s, O(side="sell", reduce_only=True), purpose="close", strategy="directional")
    assert close["state"] == "filled" and book.position["BTCUSDT"] == 0


# --- 3, 8. открытие с частичным исполнением и финальным статусом получает стоп ---

def test_bingx_ioc_partial_fill_final_in_create_response_gets_its_stop():
    book = Book()

    def create(call):
        q = call["query"]
        if q["type"] == "STOP_MARKET":
            return book.bingx_create()(call)
        book.next_id += 1
        order = {"symbol": q["symbol"], "orderId": book.next_id, "side": q["side"], "positionSide": "BOTH",
                 "type": q["type"], "origQty": q["quantity"], "price": q.get("price", "0"), "executedQty": "0.0006",
                 "status": "CANCELED", "clientOrderId": q["clientOrderId"], "avgPrice": "65000", "stopPrice": "",
                 "reduceOnly": "false", "commission": "-0.01", "updateTime": now_ms()}
        book.orders[q["clientOrderId"]] = order
        book._move(q["symbol"], q["side"], "0.0006", False)
        return bingx_ok({"order": order})
    s = Session(book.routes("bingx", **{"POST /openApi/swap/v2/trade/order": create}))
    res = submit(s, O("bingx", side="buy", order_type="limit", price="65000", stop_loss="60000"),
                 strategy="directional")
    assert res["state"] == "closed" and res["filled"] == D("0.0006")          # стратегия видит открытую часть
    assert [(b["quantity"], b["stopPrice"]) for b in stops_bx(s)] == [("0.0006", "60000")]
    assert journal._uncovered_stops() == []


def test_open_found_after_timeout_as_partially_filled_cancelled_gets_its_stop():
    book = Book()
    create = book.bybit_create(status="PartiallyFilledCanceled", filled="0.0005")

    def timeout_after_create(call):
        create(call)                                                          # ордер создан и частично исполнен
        return Resp(exc=asyncio.TimeoutError())
    s = Session(book.routes("bybit", **{"POST /v5/order/create": timeout_after_create}))
    res = submit(s, O(side="buy", stop_loss="60000"), strategy="directional")
    assert res["state"] == "closed" and res["filled"] == D("0.0005")
    assert [(b["qty"], b["triggerPrice"]) for b in stops_by(s)] == [("0.0005", "60000")]
    assert journal._uncovered_stops() == []


# --- 4. закрытие не ботом: худший убыток по марже; все открытия ждут подтверждения ---

def test_liquidation_books_isolated_margin_not_stop_distance_and_blocks_all_opens():
    """BingX, направленная лонг 0.06 ETH по 3000, стоп 2800, 2x изолированная (180 USDT); стопа на бирже нет, позицию
    ликвидировали (~1530). Худший убыток — вся изолированная маржа 90 + комиссии, не 18.36 по стопу; открытия всех
    стратегий (и хедж на Bybit) ждут подтверждения."""
    book = Book()
    s = Session(filled_routes(book, "bingx") | book.routes("bybit"))
    eth = O("bingx", "ETHUSDT", side="buy", qty="0.06", stop_loss="2800")
    assert submit(s, eth, strategy="directional")["state"] == "filled"
    liq = risk.isolated_liq("buy", D(3000), D(2))
    book.position["ETH-USDT"] = D(0)
    recon(s)
    assert "closed_by_venue" in events()
    booked = -pnl("venue_close")
    real = D("0.06") * (D(3000) - liq)
    assert booked == D("90.36") and booked >= real > D(85)
    res = submit(s, O("bybit", "BTCUSDT"), strategy="hedge", group="c9")
    assert res["state"] == "refused" and "не подтверждено" in res["reason"] and "всех стратегий" in res["reason"]


def test_flat_worst_by_margin_mode():
    net, avg, stop = D("0.06"), D(3000), D(2800)
    assert journal.flat_worst(net, avg, stop, "isolated", D(2)) == D("90.36")          # маржа + комиссии
    assert journal.flat_worst(net, avg, D(2990), "isolated", D(3)) == D("60.36")
    assert journal.flat_worst(net, avg, stop, "cross", D(2)) == D(180)                 # кросс — весь номинал
    assert journal.flat_worst(net, avg, stop, None, None) == D(180)                   # не узнали — весь номинал
    assert journal.flat_worst(net, avg, stop, "isolated", None) == D(180)
    assert journal.flat_worst(net, avg, None, "isolated", D(2)) == D(180)              # без стопа — весь номинал
    assert journal.flat_worst(-net, avg, D(3200), "isolated", D(2)) == D("90.36")      # шорт


# --- 5. сверка стопа не удалась — закрытие биржей не записывается ---

def test_stop_fill_with_failed_lookup_is_not_booked_as_venue_close():
    """Стоп бота сработал (исполнение с id бота), а запрос стопа по id дважды упёрся в лимит запросов: сверка не
    объявляет «закрыла биржа» (иначе исполнение стопа, записанное после обнуления, дало бы фантомный шорт и второй
    убыток на весь номинал) — ждёт следующего прохода; там стоп записан, позиции нет, убыток — настоящий."""
    book = Book()
    fail = {"cid": None, "left": 2}
    real_q = book.bybit_query

    def lookup(call):
        cid = call["query"].get("orderLinkId")
        if cid and cid == fail["cid"] and fail["left"] > 0:
            fail["left"] -= 1
            return bybit_err(10006, "Too many visits")
        return real_q(call)
    s = Session(filled_routes(book, **{"GET /v5/order/realtime": lookup}))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    fail["cid"] = stops_by(s)[0]["orderLinkId"]
    book.trigger("BTCUSDT", "60000")
    recon(s)
    assert "closed_by_venue" not in events() and "watch_error" in events()
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == D("0.001") and journal.unsettled() == []
    recon(s)
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0 and "closed_by_venue" not in events()
    assert journal.unsettled() == [] and journal.pnl_today() == D("-5.02")   # −5 по стопу и комиссии 0.01 + 0.01


def test_stop_execution_seen_before_its_order_status_is_not_a_venue_close():
    """Исполнение стопа бота (с его id) уже в списке исполнений, а запрос ордера ещё отвечает «не сработал» (биржа
    отстаёт): «закрыла биржа» не записывается, пока исполнение не попадёт в журнал."""
    book = Book()
    stale = {"cid": None, "on": True}
    real_q = book.bybit_query

    def lookup(call):
        cid = call["query"].get("orderLinkId")
        if cid and cid == stale["cid"] and stale["on"]:
            return bybit_ok({"list": [dict(book.orders[cid], orderStatus="Untriggered", cumExecQty="0",
                                           avgPrice="")], "nextPageCursor": ""})
        return real_q(call)
    s = Session(filled_routes(book, **{"GET /v5/order/realtime": lookup}))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    stale["cid"] = stops_by(s)[0]["orderLinkId"]
    book.trigger("BTCUSDT", "60000")
    recon(s)
    assert "closed_by_venue" not in events() and journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == D("0.001")
    stale["on"] = False
    recon(s)
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0 and journal.unsettled() == []
    assert "closed_by_venue" not in events() and journal.pnl_today() == D("-5.02")


# --- 6. уменьшающий ордер ключ не переворачивает; стоп при двух ключах — отказ ---

def test_set_stop_refused_when_symbol_has_two_bot_keys():
    book = Book()
    own(group="c1", book=book)
    own(group="c2", book=book)
    s = Session(filled_routes(book))
    kind, why = run(journal.set_stop(s, "bybit", "BTCUSDT", "70000", CREDS, strategy="hedge", group="c1"))
    assert kind == "refused" and "перекрывающиеся стопы" in why and not stops_by(s)


def _fill(order, purpose, group, filled, price, strategy="hedge"):
    r = journal._insert_intent(order, purpose, strategy, "confirm", None, group=group)
    journal._update(r["client_id"], state="sending")
    if purpose == "stop":
        journal._update(r["client_id"], state="open")
    return journal._update(r["client_id"], state="filled", filled=filled, avg_price=price)


def test_stop_fill_beyond_its_key_closes_the_other_key_not_a_phantom():
    """Два хедж-шорта c1, c2; c1 закрыт, а его стоп сработал до снятия (против доли c2): ключ c1 через ноль не
    переходит — излишек закрыл позицию c2 (по её входу), событие владельцу; фантомных «лонг c1 / шорт c2» нет."""
    own(group="c1")
    own(group="c2")
    _fill(O(side="buy", reduce_only=True), "close", "c1", "0.001", "65000")
    _fill(O(side="buy", order_type="stop", reduce_only=True, trigger="70000"), "stop", "c1", "0.001", "70000")
    assert journal._rows("SELECT * FROM keys") == []
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0
    assert "cross_key" in events() and pnl("trade") == D("-5")               # c2: 0.001 × (65000 − 70000)


def test_reduce_fill_beyond_all_bot_keys_does_not_flip_and_alerts():
    own(group="c1")
    _fill(O(side="buy", reduce_only=True), "close", "c1", "0.001", "65000")
    _fill(O(side="buy", order_type="stop", reduce_only=True, trigger="70000"), "stop", "c1", "0.001", "70000")
    assert journal._rows("SELECT * FROM keys") == [] and "overfill" in events()
    assert any("не позиция бота" in n for n in notes("overfill"))


# --- 7. сбой начислений фандинга BingX ---

def test_bingx_funding_failure_does_not_skip_stop_resync_nor_lose_funding(monkeypatch):
    monkeypatch.setattr(journal, "WATERMARK_LAG", 0)                          # отметка сверки уходит вперёд сразу
    book = Book()
    income = {"ok": False}
    s = Session(filled_routes(book, "bingx", **{
        "GET /openApi/swap/v2/user/income": lambda c: (bingx_ok(list(book.funding)) if income["ok"]
                                                       else bingx_err(100410, "rate limited"))}))
    assert submit(s, O("bingx", "ETHUSDT", side="buy", qty="0.02", stop_loss="2800"),
                  strategy="directional")["state"] == "filled"
    first = stops_bx(s)[0]["clientOrderId"]
    book.orders[first]["status"] = "CANCELLED"                                # стоп бота пропал на бирже
    book.funding.append({"tranId": "fund1", "income": "-0.05", "time": now_ms(), "symbol": "ETH-USDT"})
    time.sleep(0.01)
    recon(s)
    assert len(stops_bx(s)) == 2 and journal._uncovered_stops() == []       # стоп переставлен, несмотря на сбой
    assert "watch_error" in events() and pnl("funding") == 0
    income["ok"] = True
    recon(s)
    assert pnl("funding") == D("-0.05")                                        # окно начислений не потеряно


# --- 9. пыль спота ---

def test_spot_fee_dust_is_written_off_and_does_not_hold_funding_groups():
    book = Book()
    s = Session(book.routes("bybit", **{"POST /v5/order/create": book.bybit_create(status="Filled",
                                                                                     cumExecFee="0.0000002")}))
    for grp in ("f1", "f2"):
        buy = O("bybit", "BTCUSDT", side="buy", qty="0.0002", category="spot")
        assert submit(s, buy, strategy="funding", group=grp)["state"] in ("open", "filled")
        recon(s, positions=False)
        held = journal.spot_inventory("bybit", "BTCUSDT")
        key = journal._rows("SELECT net FROM keys WHERE grp=?", (grp,))
        assert D(key[0]["net"]) == D("0.0001996")                             # без монеты комиссии
        sell = O("bybit", "BTCUSDT", side="sell", qty="0.000199", category="spot")
        assert D("0.000199") <= held
        assert submit(s, sell, purpose="close", strategy="funding", group=grp)["state"] in ("open", "filled")
        recon(s)                                                               # сверка: исполнение и пыль
    assert journal._rows("SELECT * FROM keys") == [] and events().count("spot_dust") == 2
    res = submit(s, O("bybit", "BTCUSDT", side="buy", qty="0.0002", category="spot"), strategy="funding", group="f3")
    assert res["state"] in ("open", "filled"), res["reason"]


# --- 10. цена приращения и результат без цены ---

def test_partial_fill_realized_uses_marginal_price():
    own(side="buy", price="65000")
    row = journal._insert_intent(O(side="sell", reduce_only=True), "close", "hedge", "confirm", None)
    cid = row["client_id"]
    journal._update(cid, state="sending")
    journal._update(cid, state="open", filled="0.0005", avg_price="60000")
    journal._update(cid, state="filled", filled="0.001", avg_price="61000")   # вторая половина — по 62000
    assert pnl("trade") == D("-4.0")


def test_partial_open_entry_uses_marginal_price():
    row = journal._insert_intent(O(side="buy"), "open", "hedge", "confirm", None)
    cid = row["client_id"]
    journal._update(cid, state="sending")
    journal._update(cid, state="open", filled="0.0005", avg_price="60000")
    journal._update(cid, state="filled", filled="0.001", avg_price="61000")
    k = journal.bot_book("bybit", "linear", "BTCUSDT")["keys"][("hedge", "")]
    assert k["px"] == D("61000") and D(journal._rows("SELECT cost FROM keys")[0]["cost"]) == D("61")


def test_fill_without_price_is_corrected_when_price_arrives():
    own(side="buy", price="65000")
    row = journal._insert_intent(O(side="sell", reduce_only=True), "close", "hedge", "confirm", None)
    cid = row["client_id"]
    journal._update(cid, state="sending")
    journal._update(cid, state="open", filled="0.001")                        # исполнение без средней цены
    assert pnl("trade") == D("-65")                                            # пока — худшее
    journal._update(cid, state="filled", filled="0.001", avg_price="65000")  # цена пришла: в ноль
    assert pnl("trade") == 0


def test_unpriced_then_priced_partial_fills():
    own(side="buy", price="65000")
    row = journal._insert_intent(O(side="sell", reduce_only=True), "close", "hedge", "confirm", None)
    cid = row["client_id"]
    journal._update(cid, state="sending")
    journal._update(cid, state="open", filled="0.0005")
    journal._update(cid, state="filled", filled="0.001", avg_price="61000")
    assert pnl("trade") == D("-4.0")


# --- 11. отметка «в отправке» в строке ---

def _resend_session(book, on_second):
    real_create = book.bybit_create(status="Filled")
    posts = {"n": 0}

    def create(call):
        posts["n"] += 1
        if posts["n"] == 1:
            return Resp(exc=asyncio.TimeoutError())                           # первый POST: исход неясен
        on_second(body(call)["orderLinkId"])
        return real_create(call)                                              # повтор дошёл до биржи
    return Session(book.routes("bybit", **{"POST /v5/order/create": create,
                                           "GET /v5/order/realtime": lambda c: (bybit_ok({"list": []})
                                                                                if c["query"].get("orderLinkId")
                                                                                else book.bybit_query(c))}))


def test_owner_resolve_from_other_process_waits_for_the_resend():
    book = Book()
    seen = {}

    def owner(cid):
        journal._inflight.discard(cid)                                        # скрипт владельца — другой процесс
        with pytest.raises(ValueError, match="отправляется"):
            journal.resolve(cid, "rejected", "в кабинете ордера нет")
        journal._inflight.add(cid)
        seen["cid"] = cid
    res = submit(_resend_session(book, owner), O())
    assert res["state"] == "open" and seen
    assert book.position["BTCUSDT"] == D("-0.001")
    recon(_resend_session(book, owner), positions=False)
    assert journal.get(seen["cid"])["inflight_until"] == 0


def test_accepted_resend_of_a_resolved_row_alerts_the_owner():
    book = Book()

    def owner(cid):
        journal._inflight.discard(cid)
        con = journal._connect()
        with journal._tx(con):                                                # отметка истекла (бот завис дольше TTL)
            con.execute("UPDATE orders SET inflight_until=0 WHERE client_id=?", (cid,))
        journal.resolve(cid, "rejected", "в кабинете ордера нет")
        journal._inflight.add(cid)
    res = submit(_resend_session(book, owner), O())
    assert res["state"] == "rejected" and res["reason"] == journal.CHANGED
    assert book.position["BTCUSDT"] == D("-0.001")
    assert "mismatch" in events() and any("уже rejected" in n for n in notes("mismatch"))


def test_resume_clears_inflight_marks():
    row = journal._insert_intent(O(), "open", "hedge", "confirm", None)
    journal._update(row["client_id"], state="sending", inflight_until=time.time() + 100)
    with pytest.raises(ValueError, match="отправляется"):
        journal.resolve(row["client_id"], "rejected", "проверил")               # строка ещё не unknown и в отправке
    journal.resume()                                                           # бот перезапущен: ничего не отправляет
    got = journal.get(row["client_id"])
    assert got["state"] == "unknown" and got["inflight_until"] == 0
    assert journal.resolve(row["client_id"], "rejected", "проверил")["state"] == "rejected"


# --- 12. направленная без метки группы ---

def test_directional_other_label_opposite_side_is_refused_by_guard():
    inst = venues.Instrument(D("0.000001"), D("0.000001"), None, D("0.1"), D("5"))

    def row(**over):
        base = {"venue": "bybit", "category": "linear", "symbol": "BTCUSDT", "strategy": "directional", "group": "",
                "side": "long", "qty": D("0.0005"), "net": D("0.0005"), "pending": D(0), "px": D("65000"),
                "entry": D("65000"), "notional": D("32.5"), "unrealized": D(0), "stop": True,
                "stop_price": D("64000")}
        base.update(over)
        return base

    def guard(order, group, exposure):
        return risk.guard_open(order, "directional", group, "confirm", mark=D("65000"), instrument=inst,
                               leverage=D(2), margin_mode="isolated", foreign=(), foreign_account=(),
                               exposure=exposure, realized_today=D(0), unrealized=D(0), capital=D("10000"))
    short = O(side="sell", qty="0.0005", stop_loss="66000")
    assert any("встречная" in r for r in guard(short, "b", [row(group="a")]))          # ключ ранней версии с меткой
    assert any("против своей же позиции" in r for r in guard(short, "b", [row()]))     # тот же ключ журнала
    small = O(side="buy", qty="0.0001", stop_loss="64900")
    assert any("перекрывающиеся" in r for r in guard(small, "", [row(group="a")]))
    a = row(group="a", qty=D("0.001"), net=D("0.001"), notional=D("65"), stop_price=D("61700"))
    mine, others = risk.stop_budget([a], "bybit", "linear", "BTCUSDT", "directional", "b", "buy", D("0.0001"),
                                    D("65000"), D("64900"))
    real = risk.key_worst("buy", D("0.001"), D("65000"), D("61700")) + \
        risk.key_worst("buy", D("0.0001"), D("65000"), D("64900"))
    assert mine + others == real                                               # чужой ключ — в «остальных»


def test_directional_label_is_dropped_and_opposite_side_open_refused():
    book = Book()
    s = Session(filled_routes(book))
    a = submit(s, O(side="buy", stop_loss="64000"), strategy="directional", group="a")
    assert a["state"] == "filled" and a["row"]["grp"] == ""
    b = submit(s, O(side="sell", stop_loss="66000"), strategy="directional", group="b")
    assert b["state"] == "refused" and "против своей же позиции" in b["reason"]
    assert book.position["BTCUSDT"] == D("0.001") and len(creates_by(s)) == 2          # открытие и стоп
    close = submit(s, O(side="sell", reduce_only=True), purpose="close", strategy="directional", group="a")
    assert close["state"] == "filled" and book.position["BTCUSDT"] == 0


def creates_by(s):
    return s.sent(*CREATE_BY)


# --- 13. программный стоп ---

def test_unconfirmed_stop_and_price_past_it_closes_the_position():
    book = Book()
    real = book.bybit_create(status="Filled")

    def create(call):
        if "triggerPrice" in body(call):
            return Resp(exc=asyncio.TimeoutError())
        return real(call)
    s = Session(book.routes("bybit", **{"POST /v5/order/create": create}))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    stop_rows = [r for r in journal.history(20) if r["purpose"] == "stop"]
    assert stop_rows and stop_rows[0]["state"] == "unknown"
    book.marks["BTCUSDT"] = "50000"
    recon(s)
    closes = [body(c) for c in creates_by(s) if "triggerPrice" not in body(c) and body(c).get("reduceOnly")]
    assert [(c["side"], c["qty"]) for c in closes] == [("Sell", "0.001")]
    assert book.position["BTCUSDT"] == 0 and "stop_breached" in events()
    recon(s)
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0


def test_unconfirmed_stop_with_price_before_it_only_alerts():
    book = Book()
    real = book.bybit_create(status="Filled")

    def create(call):
        if "triggerPrice" in body(call):
            return Resp(exc=asyncio.TimeoutError())
        return real(call)
    s = Session(book.routes("bybit", **{"POST /v5/order/create": create}))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    recon(s)
    assert book.position["BTCUSDT"] == D("0.001") and "stop_breached" not in events() and "stop_missing" in events()


# --- 14. смена плеча ---

def test_set_leverage_capped_by_gates_and_position_mode(monkeypatch):
    monkeypatch.setattr(journal, "_gate_mode", lambda strategy: "minlot")
    book = Book()
    book.marks["BTCUSDT"] = "40000"
    s = Session(filled_routes(book, **{"POST /v5/position/set-leverage": bybit_ok({})}))
    res = submit(s, O(side="sell"), strategy="hedge", group="h1")
    assert res["state"] in ("open", "filled") and res["row"]["mode"] == "minlot"
    kind, why = run(journal.set_leverage(s, "bybit", "BTCUSDT", 3, CREDS, strategy="hedge"))
    assert kind == "refused" and "вне 1..2" in why
    monkeypatch.setattr(journal, "_gate_mode", lambda strategy: "auto")          # пороги подросли, позиция — minlot
    kind, why = run(journal.set_leverage(s, "bybit", "BTCUSDT", 3, CREDS, strategy="hedge"))
    assert kind == "refused" and "вне 1..2" in why
    assert not s.sent("POST", "/v5/position/set-leverage")


def test_set_leverage_with_trading_stopped_uses_minlot_caps(monkeypatch):
    book = Book()
    book.marks["BTCUSDT"] = "40000"
    s = Session(filled_routes(book, **{"POST /v5/position/set-leverage": bybit_ok({})}))
    assert submit(s, O(side="sell"), strategy="hedge", group="h1")["state"] in ("open", "filled")
    assert run(journal.set_leverage(s, "bybit", "BTCUSDT", 3, CREDS, strategy="hedge"))[0] == "ok"   # confirm: 3
    switch.stop()                                                              # «Стоп» из Telegram: TRADING=0
    kind, why = run(journal.set_leverage(s, "bybit", "BTCUSDT", 3, CREDS, strategy="hedge"))
    assert kind == "refused" and "вне 1..2" in why
    assert risk.limits("hedge", "paper")["leverage"] == 2 and risk.limits("hedge", "bogus")["leverage"] == 2
    assert risk.limits("hedge", "confirm")["leverage"] == 3


# --- 15. стоп ожидающего открытия — в бюджете ---

def test_pending_open_stop_counts_in_combined_worst(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "minlot")
    book = Book()
    book.margin["bybit"] = "REGULAR_MARGIN"
    s = Session(book.routes("bybit"))
    btc = O(side="sell", qty="0.0007", stop_loss="68500")                     # худший 3.77 USDT
    r = journal._insert_intent(btc, "open", "hedge", "minlot", None, group="h1")
    journal._update(r["client_id"], state="sending")
    journal._update(r["client_id"], state="open")                             # принят, исполнение ещё не записано
    exp = journal.exposure({("bybit", "linear", "BTCUSDT"): D("65000")})
    assert exp[0]["stop"] is True and exp[0]["stop_price"] == D("68500") and risk._row_worst(exp[0]) > D("3.7")
    eth = O(symbol="ETHUSDT", side="sell", qty="0.01", stop_loss="3200")      # худший 3.06 USDT
    res = submit(s, eth, strategy="hedge", group="h2")
    assert res["state"] == "refused" and "худший убыток" in res["reason"], res["reason"]


# --- 16. пороги confirm/auto — только из локальных файлов владельца и журнала ---

def _stats_file(root, name, stats):
    data = {"version": 1, "generated_at": time.time() - 60, "strategies": {"hedge": stats}}
    with open(os.path.join(root, "data", name), "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_gate_mode_reads_paper_and_live_only_from_owner_files(monkeypatch, tmp_path):
    from test_trading_gates import HEDGE_LIVE, HEDGE_PAPER, bot_root, git_index
    root = bot_root(tmp_path / "bt")
    monkeypatch.setattr(gates, "ROOT", root)
    with pytest.raises(TypeError):
        run(journal.submit(Session({}), O(), CREDS, strategy="hedge", gate_paper=HEDGE_PAPER))
    assert REAL_GATE_MODE("hedge") == "minlot"                                 # только сильный бэктест
    _stats_file(root, gates.PAPER_FILE, HEDGE_PAPER)
    assert REAL_GATE_MODE("hedge") == "confirm"
    _stats_file(root, gates.LIVE_FILE, HEDGE_LIVE)
    assert REAL_GATE_MODE("hedge") == "confirm"                                # журнал: дней и хеджей — 0
    for i in range(30):                                                        # 30 реальных хеджей за 21 день
        row = own(group=f"h{i}")
        con = journal._connect()
        with journal._tx(con):
            con.execute("UPDATE orders SET created_ts=? WHERE client_id=?", (time.time() - 22 * 86400,
                                                                             row["client_id"]))
    assert REAL_GATE_MODE("hedge") == "auto"
    bad = journal._insert_intent(O(), "open", "hedge", "confirm", None)        # unknown дольше 10 минут
    journal._update(bad["client_id"], state="sending")
    journal._transition(bad["client_id"], state="unknown", note="test", event="unknown")
    con = journal._connect()
    with journal._tx(con):
        con.execute("UPDATE outbox SET ts=? WHERE client_id=?", (time.time() - 700, bad["client_id"]))
    assert journal.live_stats("hedge", HEDGE_LIVE)["unknown_over_10min"] == 1
    assert REAL_GATE_MODE("hedge") == "confirm"
    (tmp_path / "bt" / "bot" / ".git" / "index").write_bytes(
        git_index(["research/hedge_bt.py", "data/gates_paper.json"]))          # бумага из git — не в счёт
    assert gates.load_paper("hedge", root=root)[0] is None
    assert REAL_GATE_MODE("hedge") == "paper"                                  # в git что-то под data/ — и бэктест


def test_live_stats_file_cannot_hide_journal_facts():
    from test_trading_gates import HEDGE_LIVE
    fake = dict(HEDGE_LIVE, days=999, count=999)
    got = journal.live_stats("hedge", fake)
    assert got["days"] == 0 and got["count"] == 0 and got["unknown_over_10min"] == 0
    journal.add_pnl("bybit", "BTCUSDT", "-60", "trade", "test-day")            # день с убытком больше потолка
    assert journal.live_stats("hedge", fake)["limit_violations"] == 1


# --- 17. закоммиченная статистика с другим регистром пути ---

def test_committed_stats_with_other_case_or_under_data_counts_as_tracked(tmp_path):
    from test_trading_gates import bot_root, git_index
    root = bot_root(tmp_path)
    index = os.path.join(root, ".git", "index")
    with open(index, "wb") as f:
        f.write(git_index(["research/hedge_bt.py", "DATA/gates_backtest.json"]))
    bt, why = gates.load_backtest("hedge", root=root)
    assert bt is None and "есть в git" in why
    with open(index, "wb") as f:
        f.write(git_index(["research/hedge_bt.py", "Data/other.txt"]))
    bt, why = gates.load_backtest("hedge", root=root)
    assert bt is None and "есть в git" in why
    with open(index, "wb") as f:
        f.write(git_index(["research/hedge_bt.py"]))
    assert gates.load_backtest("hedge", root=root)[0] is not None


def _git(cwd, *args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", HOME=str(cwd))
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=",
                           "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.skipif(os.name != "nt", reason="регистронезависимая файловая система (ПК владельца — Windows)")
@pytest.mark.parametrize("name", ["DATA/gates_backtest.json", "Data/gates_backtest.json"])
def test_committed_stats_pulled_on_windows_are_refused(tmp_path, name):
    origin = tmp_path / "origin"
    (origin / "research").mkdir(parents=True)
    (origin / "research" / "hedge_bt.py").write_bytes(b"X = 1\n")
    (origin / ".gitignore").write_text("data/\n", encoding="utf-8")
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "init")
    pc = tmp_path / "pc"
    _git(tmp_path, "clone", "-q", str(origin), str(pc))
    (pc / "data").mkdir()
    sha = gates.research_sha(str(pc))
    forged = {"version": 1, "generated_at": time.time() - 60, "research_sha": sha,
              "strategies": {"hedge": {"days": 99, "count": 999, "ratio_ok_share": 1, "cost_to_buffer": 0,
                                       "sigma_ratio": 0}}}
    blob = origin / "forged.json"
    blob.write_text(json.dumps(forged), encoding="utf-8")
    oid = _git(origin, "hash-object", "-w", str(blob)).strip()
    blob.unlink()
    _git(origin, "update-index", "--add", "--cacheinfo", f"100644,{oid},{name}")
    _git(origin, "commit", "-q", "-m", "stats")
    _git(pc, "pull", "-q", "--no-rebase", "origin", "main")
    assert (pc / "data" / "gates_backtest.json").exists()
    bt, why = gates.load_backtest("hedge", root=str(pc))
    assert bt is None and "есть в git" in why


# --- 18. BingX: кросс-позиция владельца на другом символе ---

def test_bingx_owner_cross_position_on_other_symbol_blocks_isolated_open():
    book = Book()
    book.other = [("ETH-USDT", D("-0.5"))]                                    # шорт владельца на ETH
    book.cross = {"ETH-USDT"}                                                  # с кросс-маржой
    s = Session(book.routes("bingx", **{"POST /openApi/swap/v2/trade/order": book.bingx_create(status="FILLED")}))
    res = submit(s, O("bingx", "BTCUSDT", side="sell", qty="0.0007"), strategy="hedge")
    assert res["state"] == "refused" and "кросс-маржой" in res["reason"] and "ETH-USDT" in res["reason"]
    assert not s.sent(*CREATE_BX)
    assert [c for c in s.sent("GET", "/openApi/swap/v2/user/positions") if "symbol" not in c["query"]]
    book.cross = set()                                                         # изолированная — открытие можно
    res = submit(s, O("bingx", "BTCUSDT", side="sell", qty="0.0007"), strategy="hedge")
    assert res["state"] in ("open", "filled"), res["reason"]


def test_bingx_account_positions_unread_blocks_open():
    book = Book()
    routes = book.routes("bingx", **{"POST /openApi/swap/v2/trade/order": book.bingx_create(status="FILLED")})
    real = routes[("GET", "/openApi/swap/v2/user/positions")]
    routes[("GET", "/openApi/swap/v2/user/positions")] = lambda c: (real(c) if "symbol" in c["query"]
                                                                    else Resp(502, b""))
    res = submit(Session(routes), O("bingx", "BTCUSDT", side="sell", qty="0.0007"), strategy="hedge")
    assert res["state"] == "refused" and "позиции аккаунта" in res["reason"]
