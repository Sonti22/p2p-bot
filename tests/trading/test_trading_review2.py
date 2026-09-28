"""Торговое ядро, второе ревью: сценарии подтверждённых находок — теперь с исправленным поведением. Биржа — заглушка
Book (исполнения, условные стопы, отмена), сеть заблокирована, ключи фиктивные.

1, 7  закрытие/стоп — только когда вся позиция символа бота (позиция, ордера, исполнения с отметки сверки);
2     снимок не старше SNAPSHOT_MAX_AGE, повтор открытия — после новой проверки символа;
3     стоп бота — условный ордер на размер бота, лимитки IOC, сверка снимает висящие ордера бота при чужом;
4, 14 результат дня: исполнения бота, закрытие биржей (худший убыток до подтверждения), нереализованный, 2% капитала,
      свежесть сверки;
5     TON: закрытие и стоп по символу из журнала;
6, 21 кросс: позиции USDC/inverse/option/займы — чужие;
8     закрытие биржей находит периодическая сверка (слот и итог не держит фантом);
9     resolve с исполненным количеством, close_out не меняет вклад;
10, 22 закрытие без стратегии — единственный ключ;
11    повторная тревога после разбора владельцем;
12    закрытие не ждёт сверки всех ордеров символа;
13    чтение журнала не растёт с историей, одно соединение;
15, 16 худший убыток итоговой позиции ключа, set_stop/set_leverage в лимитах направленной;
18    пороги gates на жёсткой границе;
19    BingX: знак позиции не сходится — не угадываем;
20    запас до ликвидации в submit;
23    итог 2000 USDT по свежим ценам.
"""
import asyncio
import time
from decimal import Decimal as D

import pytest

from trading import gates, journal, risk, venues
from trading_stubs import (CREDS, REAL_FEED_PROBLEM, REAL_GATE_MODE, Book, Gated, Resp, Session, body, business,
                           bybit_err, bybit_ok, fresh_journal, now_ms, run)

CREATE_BY, CREATE_BX = ("POST", "/v5/order/create"), ("POST", "/openApi/swap/v2/trade/order")
RT = ("GET", "/v5/order/realtime")
VSYM = {("bybit", "BTCUSDT"): "BTCUSDT", ("bybit", "ETHUSDT"): "ETHUSDT", ("bingx", "ETHUSDT"): "ETH-USDT",
        ("bingx", "BTCUSDT"): "BTC-USDT", ("bingx", "TONUSDT"): "GRAMTON-USDT"}


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


def own(venue="bybit", symbol="BTCUSDT", side="sell", qty=None, strategy="hedge", group="", price="65000",
        book=None, category=None):
    """Исполненная позиция бота в журнале (и на «бирже», если дана)."""
    o = O(venue, symbol, side, qty, **({"category": category} if category else {}))
    row = journal._insert_intent(o, "open", strategy, "confirm", None, group=group)
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="filled", filled=venues.fmt(o.qty), avg_price=price)
    if book is not None:
        sym = VSYM[(venue, symbol)]
        book.position[sym] = book.position.get(sym, D(0)) + (o.qty if side == "buy" else -o.qty)
    return journal.get(row["client_id"])


def filled_routes(book, venue="bybit", **over):
    if venue == "bybit":
        return book.routes("bybit", **{"POST /v5/order/create": book.bybit_create(status="Filled")}, **over)
    return book.routes("bingx", **{"POST /openApi/swap/v2/trade/order": book.bingx_create(status="FILLED")}, **over)


def creates(s):
    return s.sent(*CREATE_BY) + s.sent(*CREATE_BX)


def stops(s):
    return [body(c) for c in s.sent(*CREATE_BY) if "triggerPrice" in body(c)]


def foreign_exec(book, symbol="BTCUSDT", side="Buy", qty="0.001", kind="Trade", price="65000"):
    """Исполнение без id бота (ликвидация — BustTrade, ручная сделка владельца — Trade)."""
    book.foreign_execs.append({"symbol": symbol, "orderLinkId": "", "orderId": f"x{len(book.foreign_execs)}",
                               "side": side, "execQty": qty, "execPrice": price, "execTime": str(now_ms()),
                               "execType": kind, "execId": f"f{len(book.foreign_execs)}", "execFee": "0"})


def events():
    return [e["event"] for e in journal.pending_events(200)]


# --- 1, 7. закрытие биржей/владельцем — бот не режет ручную позицию владельца ---

def test_close_after_venue_side_close_does_not_reduce_owner_manual_position():
    """Шорт бота 0.001 закрыла биржа (ликвидация — исполнение без id бота); владелец открыл свой шорт 0.01. Журнал
    об этом ещё не знает, стратегия закрывает «свою» позицию: снимок символа — позиция не равна журналу — отказ,
    событие владельцу, шорт владельца цел."""
    book = Book()
    own(book=book)
    book.position["BTCUSDT"] = D(0)
    foreign_exec(book, side="Buy", kind="BustTrade")
    book.position["BTCUSDT"] = D("-0.01")
    foreign_exec(book, side="Sell", qty="0.01")
    s = Session(book.routes("bybit"))
    res = submit(s, O(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and res["reason"].startswith(risk.FOREIGN) and not creates(s)
    assert book.position["BTCUSDT"] == D("-0.01") and "foreign" in events()


def test_equal_size_owner_position_after_venue_close_is_not_the_bots():
    """Владелец открыл ровно тот же размер (−0.001): сальдо совпало, но после отметки сверки есть исполнения без id бота
    (ликвидация, ручная сделка) — позиция не доказана как бота: ни закрытия, ни добавки, ни стопа."""
    book = Book()
    own(book=book)
    foreign_exec(book, side="Buy", kind="BustTrade")
    foreign_exec(book, side="Sell")                                          # на бирже снова −0.001 — уже владельца
    s = Session(book.routes("bybit"))
    close = submit(s, O(side="buy", reduce_only=True), purpose="close")
    assert close["state"] == "refused" and "исполнение без id бота" in close["reason"]
    add = submit(s, O())
    assert add["state"] == "refused" and "исполнение без id бота" in add["reason"]
    kind, why = run(journal.set_stop(s, "bybit", "BTCUSDT", "70000", CREDS))
    assert kind == "refused" and "исполнение без id бота" in why
    assert not creates(s)


def test_close_after_venue_stop_does_not_cut_owner_manual_position():
    """Стоп бота сработал на бирже (позиция 0), потом владелец открыл лонг 0.005: закрытие «своей» позиции бота не
    уходит — сверка его ордеров показала исполненный стоп, у бота позиции нет; лонг владельца цел."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    book.trigger("BTCUSDT", "60000")
    book.position["BTCUSDT"] += D("0.005")
    res = submit(s, O(side="sell", reduce_only=True), purpose="close", strategy="directional")
    assert res["state"] == "refused", res["reason"]                           # сверка: стоп исполнен, сальдо бота 0
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0
    assert book.position["BTCUSDT"] == D("0.005") and len(creates(s)) == 2   # открытие и стоп — всё


# --- 2. возраст снимка и повтор открытия ---

def test_open_resend_after_timeout_rechecks_symbol_and_stops_on_owner_order():
    """Первый POST — таймаут; пока бот ищет ордер по id, владелец открывает лонг и ставит лимитку. Повтор открытия —
    только после новой проверки символа: чужое — повтора нет, строка unknown, событие владельцу."""
    book = Book()
    seen = {}

    def lookup(call):
        if "orderLinkId" not in call["query"]:
            return book.bybit_query(call)
        if not seen:
            seen["x"] = True
            book.position["BTCUSDT"] = D("0.01")
            book.foreign.append({"orderId": "777", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy",
                                 "orderType": "Limit", "qty": "0.01", "price": "60000", "orderStatus": "New"})
        return bybit_ok({"list": []})
    s = Session(book.routes("bybit", **{"POST /v5/order/create": Resp(exc=asyncio.TimeoutError()),
                                        "GET /v5/order/realtime": lookup}))
    res = submit(s, O())
    assert res["state"] == "unknown" and len(creates(s)) == 1
    assert "повтор не отправлен" in res["reason"] and risk.FOREIGN in res["reason"]
    assert [r["client_id"] for r in journal.blocking()] == [res["row"]["client_id"]]
    assert "unknown" in events()


def test_close_resend_after_timeout_rechecks_ownership():
    """Закрытие: первый POST — таймаут; пока бот ищет ордер, позицию бота закрыла ликвидация, а владелец открыл свой
    шорт. Повтор закрытия — только после нового снимка символа: чужое — повтора нет, строка unknown."""
    book = Book()
    own(book=book)
    seen = {}

    def lookup(call):
        if "orderLinkId" not in call["query"]:
            return book.bybit_query(call)
        if not seen:
            seen["x"] = True
            foreign_exec(book, side="Buy", kind="BustTrade")
            book.position["BTCUSDT"] = D("-0.01")
            foreign_exec(book, side="Sell", qty="0.01")
        return bybit_ok({"list": []})
    s = Session(book.routes("bybit", **{"POST /v5/order/create": Resp(exc=asyncio.TimeoutError()),
                                        "GET /v5/order/realtime": lookup}))
    res = submit(s, O(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "unknown" and len(creates(s)) == 1 and "повтор не отправлен" in res["reason"]
    assert book.position["BTCUSDT"] == D("-0.01")


def test_old_snapshot_refuses_open(monkeypatch):
    monkeypatch.setattr(journal, "SNAPSHOT_MAX_AGE", -1)
    s = Session(Book().routes("bybit"))
    res = submit(s, O())
    assert res["state"] == "refused" and "снимок символа устарел" in res["reason"] and not creates(s)


def test_snapshot_reads_orders_and_position_last():
    """Медленное и редко меняющееся — сначала (плечо/маржа, инструмент, капитал), позиция и ордера — последними."""
    book = Book()
    s = Session(book.routes("bybit"))
    assert submit(s, O())["state"] == "open"
    paths = [c["path"] for c in s.calls if c["method"] == "GET"]
    last = max(paths.index(p) for p in ("/v5/account/info", "/v5/market/instruments-info",
                                        "/v5/account/wallet-balance"))
    assert last < paths.index("/v5/position/list") and last < paths.index("/v5/order/realtime")


# --- 3. стоп бота — на его размер; лимитки IOC; сверка снимает висящие ордера бота при чужом ---

def test_bot_stop_covers_only_bot_qty_and_open_limits_are_ioc():
    """Стоп направленной long 0.001 — условный ордер бота на 0.001 (не на всю позицию символа): владелец добавил
    лонг 0.01, цена дошла до стопа — закрылось только 0.001 бота, 0.01 владельца цел."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    assert [(b["qty"], b["reduceOnly"], b["triggerPrice"]) for b in stops(s)] == [("0.001", True, "60000")]
    book.position["BTCUSDT"] += D("0.01")                                    # владелец добавил к позиции символа
    book.trigger("BTCUSDT", "60000")
    assert book.position["BTCUSDT"] == D("0.01")
    lim = O(side="buy", order_type="limit", price="65000", stop_loss="60000")
    assert venues.create_call(lim, journal.new_client_id())[2]["timeInForce"] == "IOC"


def test_watch_cancels_resting_bot_open_order_when_foreign_appears():
    """Висящий ордер бота на открытие (биржа держит его open) и появившаяся позиция владельца: периодическая сверка —
    событие владельцу и снятие ордера бота (иначе его исполнение сальдировалось бы с позицией владельца)."""
    book = Book()
    s = Session(book.routes("bybit"))
    res = submit(s, O(order_type="limit", price="65000"))
    assert res["state"] == "open" and body(creates(s)[0])["timeInForce"] == "IOC"
    book.position["BTCUSDT"] = D("0.01")
    got = run(journal.reconcile(s, lambda v: CREDS))
    assert ("foreign", "BTCUSDT") in [(e, r["symbol"]) for e, r in got]
    assert [body(c)["orderLinkId"] for c in s.sent("POST", "/v5/order/cancel")] == [res["row"]["client_id"]]
    assert journal.get(res["row"]["client_id"])["state"] == "closed" and "foreign" in events()


# --- 4, 14. результат дня ---

def test_directional_stop_out_is_counted_by_in_core_daily_limit(monkeypatch):
    """minlot, лимит 5 USDT: направленная long 0.001 по 40000 со стопом 36800 (худший 4.88). Стоп бота сработал —
    убыток 3.2 + комиссии попал в результат дня; следующая такая же направленная (худший 4.87) — отказ."""
    monkeypatch.setenv("TRADING_MODE", "minlot")
    book = Book()
    book.marks["BTCUSDT"] = "40000"
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="36800"), strategy="directional")["state"] == "filled"
    book.marks["BTCUSDT"] = "36800"
    book.trigger("BTCUSDT", "36800")
    run(journal.reconcile(s, lambda v: CREDS))
    assert journal.pnl_today() == D("-3.22")                                 # −3.2 и комиссии 0.01 + 0.01
    res = submit(s, O(side="buy", stop_loss="33600"), strategy="directional")
    assert res["state"] == "refused" and "худший убыток" in res["reason"], res["reason"]


def test_liquidation_books_worst_loss_blocks_strategy_and_closed_pnl_settles_it():
    """Ликвидация (исполнение без id бота, позиция 0): периодическая сверка обнуляет сальдо, пишет худший убыток и
    снимает висящий стоп бота; closed-pnl Bybit ровно на размер бота уточняет результат — стратегия снова открывает."""
    book = Book()
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", stop_loss="60000"), strategy="directional")["state"] == "filled"
    book.position["BTCUSDT"] = D(0)
    foreign_exec(book, side="Sell", kind="BustTrade", price="40000")
    blocked = submit(s, O("bybit", "ETHUSDT", side="buy", stop_loss="2800"), strategy="directional")
    assert blocked["state"] == "refused" and "предел 1" in blocked["reason"]   # слот держит позиция, закрытая биржей
    book.closed = [{"symbol": "BTCUSDT", "orderId": "liq1", "closedSize": "0.001", "closedPnl": "-4",
                    "updatedTime": str(now_ms())}]
    got = [e for e, _ in run(journal.reconcile(s, lambda v: CREDS))]
    assert "closed_by_venue" in got and "settled" in got
    assert journal.exposure({("bybit", "BTCUSDT"): D("65000")}) == [] and journal.unsettled() == []
    assert len(s.sent("POST", "/v5/order/cancel")) == 1                      # стоп бота снят
    assert journal.pnl_today() == D("-4.01")                                  # результат по бирже + комиссия входа
    res = submit(s, O("bybit", "ETHUSDT", side="buy", stop_loss="2800"), strategy="directional")
    assert res["state"] == "filled", res["reason"]


def test_flat_position_right_after_bot_fill_is_not_believed(monkeypatch):
    """Пустая позиция на бирже сразу после исполнения бота (ответ о позиции мог отстать) — не «закрыла биржа»: сальдо
    не обнуляется и ничего не отправляется, пока не прошло FLAT_GRACE."""
    monkeypatch.setattr(journal, "FLAT_GRACE", 60)
    book = Book()
    own()                                                                      # на «бирже» позиции нет
    s = Session(book.routes("bybit"))
    run(journal.reconcile(s, lambda v: CREDS))
    res = submit(s, O(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == D("-0.001")
    assert "closed_by_venue" not in events() and not creates(s)
    monkeypatch.setattr(journal, "FLAT_GRACE", 0)
    run(journal.reconcile(s, lambda v: CREDS))
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0 and "closed_by_venue" in events()


def test_stop_rejected_because_price_passed_it_closes_the_key():
    """Стоп ставится после исполнения открытия; цена успела пройти стоп — биржа отказала (110093): ядро закрывает
    позицию ключа рынком, как сделал бы стоп, и сообщает владельцу."""
    book = Book()
    real = book.bybit_create(status="Filled")

    def create(call):
        if "triggerPrice" in body(call):
            return bybit_err(110093, "expect Falling, but trigger_price >= current")
        out = real(call)
        book.marks["BTCUSDT"] = "59000"
        return out
    s = Session(book.routes("bybit", **{"POST /v5/order/create": create}))
    res = submit(s, O(side="buy", stop_loss="60000"), strategy="directional")
    assert res["state"] == "filled"
    sides = [(b["side"], b.get("reduceOnly", False), "triggerPrice" in b) for b in map(body, creates(s))]
    assert sides == [("Buy", False, False), ("Sell", True, True), ("Sell", True, False)]
    assert book.position["BTCUSDT"] == 0 and journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0
    assert "stop_breached" in events() and "stop_missing" not in events()


def test_unrealized_loss_and_capital_share_in_submit(monkeypatch):
    """Дневной лимит внутри submit — реализованный + нереализованный убыток позиций бота по свежей цене и не больше 2%
    капитала аккаунта."""
    monkeypatch.setenv("TRADING_MODE", "minlot")
    book = Book()
    book.marks["BTCUSDT"] = "40000"
    own(book=book, price="40000")                                            # шорт бота 0.001 по 40000
    s = Session(book.routes("bybit"))
    book.marks["BTCUSDT"] = "46000"                                           # −6 USDT по mark
    res = submit(s, O("bybit", "ETHUSDT"))
    assert res["state"] == "refused" and "дневной стоп" in res["reason"]
    book.marks["BTCUSDT"] = "40000"
    book.equity = "150"                                                       # 2% = 3 USDT
    journal.add_pnl("bybit", "BTCUSDT", "-3", "trade", "manual-test")
    res = submit(s, O("bybit", "ETHUSDT"))
    assert res["state"] == "refused" and "дневной стоп" in res["reason"]


def test_open_needs_fresh_reconcile_of_bot_positions(monkeypatch):
    monkeypatch.setattr(journal, "_feed_problem", REAL_FEED_PROBLEM)
    book = Book()
    s = Session(book.routes("bybit"))
    assert submit(s, O())["state"] == "open"                                  # нечего сверять — можно
    own(book=book)
    res = submit(s, O())
    assert res["state"] == "refused" and "reconcile" in res["reason"]
    run(journal.reconcile(s, lambda v: CREDS))
    assert submit(s, O())["state"] == "open"


# --- 5. TON: закрытие и стоп по символу из журнала ---

def test_ton_position_closed_and_stopped_when_symbol_check_expired():
    venues._RESOLVED[("bingx", "swap")] = {"ts": time.time(), "map": {"TONUSDT": "GRAMTON-USDT"}, "why": {}}
    book = Book()
    s = Session(filled_routes(book, "bingx"))
    ton = O("bingx", "TONUSDT", qty="10")
    assert submit(s, ton)["state"] == "filled"
    venues._RESOLVED.clear()                                                  # проверка устарела / не прошла
    kind, why = run(journal.set_stop(s, "bingx", "TONUSDT", "3.3", CREDS))
    assert kind == "ok", why
    stop = business(creates(s)[-1])
    assert stop["symbol"] == "GRAMTON-USDT" and stop["type"] == "STOP_MARKET" and stop["stopPrice"] == "3.3"
    res = submit(s, O("bingx", "TONUSDT", side="buy", qty="10", reduce_only=True), purpose="close")
    assert res["state"] == "filled", res["reason"]
    assert business(creates(s)[-1])["symbol"] == "GRAMTON-USDT" and book.position["GRAMTON-USDT"] == 0
    assert [c["query"]["clientOrderId"] for c in s.sent("DELETE", "/openApi/swap/v2/trade/order")] == [
        stop["clientOrderId"]]                                                # стоп закрытой позиции снят


# --- 6, 21. кросс-маржа: всё на общем залоге ---

def test_cross_minlot_refused_with_owner_usdc_perp(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "minlot")
    book = Book()
    book.margin["bybit"] = "REGULAR_MARGIN"
    book.marks["BTCUSDT"] = "40000"
    book.lists[("linear", "USDC")] = [{"symbol": "BTCPERP", "positionIdx": 0, "side": "Sell", "size": "5"}]
    s = Session(book.routes("bybit"))
    res = submit(s, O(stop_loss="41000"))
    assert res["state"] == "refused" and "ваши позиции" in res["reason"] and "BTCPERP" in res["reason"]
    book.lists.clear()
    assert submit(s, O(stop_loss="41000"))["state"] == "open"


# --- 9. resolve и close_out ---

def test_resolve_needs_filled_and_close_out_keeps_contribution():
    book = Book()
    s = Session(book.routes("bybit", **{"POST /v5/order/create": Resp(exc=asyncio.TimeoutError())}))
    res = submit(s, O())
    cid = res["row"]["client_id"]
    assert res["state"] == "unknown"
    with pytest.raises(ValueError, match="исполненным количеством"):
        journal.resolve(cid, "filled", "проверил")
    with pytest.raises(ValueError, match="средняя цена"):
        journal.resolve(cid, "filled", "проверил", filled="0.001")
    journal.resolve(cid, "filled", "проверил", filled="0.001", avg_price="65000")
    book.position["BTCUSDT"] = D("-0.001")
    s = Session(filled_routes(book))
    assert submit(s, O(side="buy", reduce_only=True), purpose="close")["state"] == "filled"
    journal.close_out(cid)
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0 and journal.exposure() == []
    phantom = submit(s, O(side="sell", reduce_only=True), purpose="close")   # фантомного лонга нет
    assert phantom["state"] == "refused" and "нет своей позиции" in phantom["reason"]
    row = journal._insert_intent(O(), "open", "hedge", "confirm", None)      # filled без числа — всё количество
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="filled")
    before = journal.bot_book("bybit", "linear", "BTCUSDT")["net"]
    journal.close_out(row["client_id"])
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == before == D("-0.001")


# --- 10, 22. закрытие без стратегии ---

def test_close_without_strategy_takes_the_single_key_and_leaves_no_phantom():
    book = Book()
    own(group="c1", book=book)
    s = Session(filled_routes(book))
    res = submit(s, O(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "filled" and (res["row"]["strategy"], res["row"]["grp"]) == ("hedge", "c1")
    assert journal.exposure({("bybit", "BTCUSDT"): D("65000")}) == []
    assert submit(s, O(), group="c2")["state"] == "open"                      # нет «встречной» фантомной позиции
    own(group="c3", book=book)
    res = submit(s, O(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and "несколько позиций бота" in res["reason"]


# --- 11. повторная тревога после разбора ---

def test_repeat_notfound_after_resolve_alerts_again():
    row = journal._insert_intent(O(), "open", "hedge", "confirm", None)
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="open")
    s = Session({RT: bybit_ok({"list": []}), ("GET", "/v5/order/history"): bybit_ok({"list": []})})
    first = run(journal.reconcile(s, lambda v: CREDS, positions=False))
    assert [e for e, _ in first] == ["notfound"]
    journal.resolve(row["client_id"], "open", "в кабинете ордер есть")
    again = run(journal.reconcile(s, lambda v: CREDS, positions=False))
    assert [e for e, _ in again] == ["notfound"]                              # тот же текст — но после разбора
    assert events().count("notfound") == 2 and journal.blocking()


# --- 12. закрытие не ждёт сверки всех ордеров символа ---

def test_close_does_not_wait_for_refresh_of_all_active_rows(monkeypatch):
    monkeypatch.setattr(journal, "REFRESH_DEADLINE", 0.05)
    book = Book()
    own(book=book)
    slow = set()
    for _ in range(3):                                         # висящие ордера бота, биржа отвечает о них долго
        r = journal._insert_intent(O(order_type="limit", price="65000"), "open", "hedge", "confirm", None)
        journal._update(r["client_id"], state="sending")
        journal._update(r["client_id"], state="open")
        slow.add(r["client_id"])
    gate = asyncio.Event()

    def lookup(call):
        if call["query"].get("orderLinkId") in slow:
            return Gated(gate, bybit_ok({"list": []}))
        return book.bybit_query(call)
    s = Session(filled_routes(book, **{"GET /v5/order/realtime": lookup}))

    async def go():
        res = await asyncio.wait_for(journal.submit(s, O(side="buy", reduce_only=True), CREDS, purpose="close"), 2)
        gate.set()
        return res
    res = run(go())
    assert res["state"] == "filled" and len(creates(s)) == 1


# --- 13. журнал: одно соединение, чтение не растёт с историей ---

def test_journal_reads_do_not_scale_with_history(monkeypatch):
    book = Book()
    own(book=book)
    con = journal._connect()
    assert journal._connect() is con                                          # одно соединение на файл
    with journal._tx(con):
        for i in range(3000):                                                 # год истории: завершённые ордера
            con.execute("INSERT INTO orders (client_id, created_ts, venue, category, symbol, side, qty, state, "
                        "purpose, filled, params) VALUES (?, 1, 'bybit', 'linear', 'BTCUSDT', 'sell', '0.001', "
                        "'rejected', 'open', '', '{}')", (f"old{i}",))
    ddl = []
    con.set_trace_callback(lambda sql: ddl.append(sql) if sql.lstrip().upper().startswith(("CREATE", "ALTER"))
                           else None)
    seen = {"rows": 0}
    real = journal._rows

    def counting(sql, args=(), path=None):
        out = real(sql, args, path)
        seen["rows"] += len(out)
        return out
    monkeypatch.setattr(journal, "_rows", counting)
    book_ = journal.bot_book("bybit", "linear", "BTCUSDT")
    journal.exposure({("bybit", "BTCUSDT"): D("65000")})
    journal.blocking()
    journal._uncovered_stops()
    assert book_["net"] == D("-0.001") and "old7" in book_["client_ids"] and "nobody" not in book_["client_ids"]
    assert seen["rows"] < 20 and ddl == []
    con.set_trace_callback(None)


# --- 15, 16. итоговая позиция ключа; set_stop и set_leverage в лимитах направленной ---

def test_second_directional_add_refused_on_resulting_key_worst_loss():
    book = Book()
    book.marks["ETHUSDT"] = "4000"
    s = Session(filled_routes(book))
    eth = O("bybit", "ETHUSDT", side="buy", qty="0.02", stop_loss="2800")
    assert submit(s, eth, strategy="directional")["state"] == "filled"        # 36.16 ≤ 50
    res = submit(s, eth, strategy="directional")                              # итог 0.04: 72.32 > 50
    assert res["state"] == "refused" and "72.32" in res["reason"]


def test_set_stop_and_set_leverage_respect_directional_limits():
    book = Book()
    s = Session(filled_routes(book, **{"POST /v5/position/set-leverage": bybit_err(110043, "not modified")}))
    assert submit(s, O(side="buy", qty="0.003", stop_loss="60000"), strategy="directional")["state"] == "filled"
    kind, why = run(journal.set_stop(s, "bybit", "BTCUSDT", "34000", CREDS))   # дальше от входа — худший 139.9
    assert kind == "refused" and "только к входу" in why
    kind, why = run(journal.set_leverage(s, "bybit", "BTCUSDT", 3, CREDS))     # направленная — потолок 2
    assert kind == "refused" and "вне 1..2" in why
    assert run(journal.set_leverage(s, "bybit", "BTCUSDT", 2, CREDS))[0] == "ok"
    assert len(s.sent("POST", "/v5/position/set-leverage")) == 1


# --- 18. пороги gates на жёсткой границе ---

def test_auto_mode_from_env_is_capped_by_gates(monkeypatch, tmp_path):
    from test_trading_gates import bot_root
    monkeypatch.setattr(journal, "_gate_mode", REAL_GATE_MODE)
    monkeypatch.setenv("TRADING_MODE", "auto")
    empty = tmp_path / "nobt"
    (empty / "research").mkdir(parents=True)
    (empty / "research" / "x.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setattr(gates, "ROOT", str(empty))
    book = Book()
    s = Session(book.routes("bybit"))
    res = submit(s, O(qty="0.01"))                                             # 650 USDT хеджа при auto из .env
    assert res["state"] == "refused" and "пороги gates" in res["reason"] and "paper" in res["reason"]
    assert not s.calls
    monkeypatch.setattr(gates, "ROOT", bot_root(tmp_path / "bt"))              # сильный бэктест — только minlot
    res = submit(s, O(qty="0.01"))
    assert res["state"] == "refused" and "итоговая позиция" in res["reason"]  # потолок minlot 50 USDT
    book.marks["BTCUSDT"] = "40000"
    res = submit(s, O())
    assert res["state"] == "open" and res["row"]["mode"] == "minlot"


# --- 19. BingX: знак позиции не сходится с журналом ---

def test_bingx_sign_mismatch_is_unknown_not_owned():
    book = Book()
    own("bingx", "ETHUSDT", book=book, price="3000")                          # бот: шорт 0.01
    book.position["ETH-USDT"] = D("0.01")                                     # биржа показала +0.01 (знак?)
    s = Session(book.routes("bingx"))
    res = submit(s, O("bingx", "ETHUSDT", side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and "знак позиции BingX" in res["reason"] and not creates(s)
    rows = journal.own_positions(run(venues.positions(s, "bingx", CREDS))[0])
    assert rows[0]["sign_suspect"] is True and rows[0]["owned"] is False
    assert [a for _, a, _ in risk.position_actions(rows)] == ["alert"]
    book.dual = "true"                                                        # режим хеджа позиций — ошибка, не пусто
    res = submit(s, O("bingx", "ETHUSDT", side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and "не прочитаны" in res["reason"]


# --- 20. запас до ликвидации внутри submit ---

def test_submit_refuses_adding_to_hedge_next_to_liquidation():
    book = Book()
    book.leverage = "3"
    own(group="c1", book=book)
    book.liq["BTCUSDT"] = "66000"                                             # 1.5% до ликвидации
    s = Session(book.routes("bybit"))
    res = submit(s, O(), group="c1")
    assert res["state"] == "refused" and "запас до ликвидации 0.015" in res["reason"]


# --- 23. итог 2000 USDT по свежим ценам всех позиций бота ---

def test_total_cap_uses_fresh_prices_of_other_symbols():
    book = Book()
    own("bybit", "ETHUSDT", qty="0.33", group="h1", price="3000", book=book)
    own("bingx", "ETHUSDT", qty="0.33", strategy="funding", group="f1", price="3000", book=book)   # итог 1980
    s = Session(book.routes("bybit") | book.routes("bingx"))
    spot = O("bybit", "BTCUSDT", side="buy", qty="0.0002", category="spot")   # 13 USDT
    book.marks.update({"ETHUSDT": "3600", "ETH-USDT": "3600"})                  # ETH +20%: итог 2376
    res = submit(s, spot, strategy="funding", group="f2")
    assert res["state"] == "refused" and "суммарно открыто" in res["reason"]
    book.marks.update({"ETHUSDT": "3000", "ETH-USDT": "3000"})
    assert submit(s, spot, strategy="funding", group="f2")["state"] == "open"
