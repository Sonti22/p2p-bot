"""Торговое ядро, чьё это: бот не трогает ручные позиции и ордера владельца на основном аккаунте (одностороннее
сальдирование, стоп tpslMode=Full на всю позицию, плечо/маржа на весь символ); закрытие — только своей позиции по
журналу; свои встречные позиции и перекрывающиеся стопы; фактическое плечо и режим маржи с биржи; кросс-маржа;
жёсткие проверки открытия внутри submit без precheck. Сеть — заглушка, ключи — фиктивные."""
import time
from decimal import Decimal as D

import pytest

from trading import journal, keys, ownership, risk, venues
from trading_stubs import CREDS, Book, Resp, Session, bingx_ok, body, bybit_err, bybit_ok, fresh_journal, run

CREATE_BY, CREATE_BX = ("POST", "/v5/order/create"), ("POST", "/openApi/swap/v2/trade/order")
LEV_BY, LEV_BX = ("POST", "/v5/position/set-leverage"), ("POST", "/openApi/swap/v2/trade/leverage")
MARGIN_BX, STOP_BY = ("POST", "/openApi/swap/v2/trade/marginType"), ("POST", "/v5/position/trading-stop")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(venues, "_RESOLVED", {})
    fresh_journal(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")


def BY(**kw):
    return venues.Order("bybit", "linear", "BTCUSDT", kw.pop("side", "sell"), kw.pop("order_type", "market"),
                        kw.pop("qty", "0.001"), **kw)


def BX(**kw):
    return venues.Order("bingx", "swap", "ETHUSDT", kw.pop("side", "sell"), "market", kw.pop("qty", "0.01"), **kw)


def submit(s, order, **kw):
    if kw.get("purpose", "open") == "open":
        kw.setdefault("strategy", "hedge")
    return run(journal.submit(s, order, CREDS, **kw))


def creates(s):
    return s.sent(*CREATE_BY) + s.sent(*CREATE_BX)


def own(venue="bybit", side="sell", qty=None, strategy="hedge", group="", stop=None, book=None):
    """Исполненная позиция бота в журнале (и на «бирже», если дана)."""
    qty = qty or ("0.001" if venue == "bybit" else "0.01")
    order = (BY if venue == "bybit" else BX)(side=side, qty=qty, stop_loss=stop)
    row = journal._insert_intent(order, "open", strategy, "confirm", None, group=group)
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="filled", filled=qty, avg_price="65000" if venue == "bybit" else "3000")
    if book is not None:
        sym = "BTCUSDT" if venue == "bybit" else "ETH-USDT"
        book.position[sym] = book.position.get(sym, D(0)) + (D(qty) if side == "buy" else -D(qty))
    return journal.get(row["client_id"])


MANUAL_BY = {"orderId": "777", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Limit",
             "qty": "0.01", "price": "60000", "orderStatus": "New", "stopOrderType": "", "triggerPrice": "0"}


def stop_order(trigger, side="Buy"):
    """Стоп позиции Bybit (tpslMode=Full): условный ордер без клиентского id."""
    return {"orderId": "888", "orderLinkId": "", "symbol": "BTCUSDT", "side": side, "orderType": "Market", "qty": "0",
            "orderStatus": "Untriggered", "stopOrderType": "StopLoss", "triggerPrice": trigger, "reduceOnly": True,
            "closeOnTrigger": True}


# --- 1, 7. ручная позиция или ордер владельца по символу — бот не трогает ---

@pytest.mark.parametrize("setup", ["position", "manual_order", "other_client_id", "stop_without_position",
                                   "our_format_not_in_journal"])
def test_open_refused_on_symbol_with_owner_position_or_order(setup):
    book = Book()
    if setup == "position":
        book.position["BTCUSDT"] = D("0.01")                                 # лонг владельца
    elif setup == "manual_order":
        book.foreign.append(MANUAL_BY)
    elif setup == "other_client_id":
        book.foreign.append(dict(MANUAL_BY, orderLinkId="my-manual-1"))
    elif setup == "stop_without_position":
        book.foreign.append(stop_order("70000"))
    else:
        book.foreign.append(dict(MANUAL_BY, orderLinkId=journal.new_client_id()))
    s = Session(book.routes("bybit"))
    res = submit(s, BY(stop_loss="70000"))                                    # tpslMode=Full заменил бы его стоп
    assert res["state"] == "refused" and res["reason"].startswith(risk.FOREIGN), res["reason"]
    assert not creates(s) and journal.history() == []


def test_open_refused_on_bingx_symbol_with_owner_position_or_stop():
    book = Book()
    book.position["ETH-USDT"] = D("-0.5")
    s = Session(book.routes("bingx"))
    assert submit(s, BX())["reason"].startswith(risk.FOREIGN)
    book.position.clear()
    book.foreign.append({"symbol": "ETH-USDT", "orderId": 5, "side": "BUY", "type": "STOP_MARKET", "origQty": "0.5",
                         "stopPrice": "3500", "clientOrderId": "", "status": "NEW"})
    assert submit(s, BX())["reason"].startswith(risk.FOREIGN) and not creates(s)


def test_own_orders_and_own_position_stop_are_attributed():
    """Свой открытый ордер (id из журнала) и стоп своей позиции (цена — последний стоп бота) — не чужое."""
    book = Book()
    own(stop="70000", book=book)                                               # шорт бота 0.001 со стопом 70000
    book.foreign.append(stop_order("70000"))
    s = Session(book.routes("bybit"))
    first = submit(s, BY(stop_loss="70000"))                                  # тот же стоп — не перекрывается
    assert first["state"] == "open"
    book.foreign[0] = stop_order("70000")
    assert submit(s, BY())["state"] == "open"                                 # в списке — и наш открытый ордер
    book.foreign[0] = stop_order("71000")                                     # стоп поменял владелец
    res = submit(s, BY())
    assert res["reason"].startswith(risk.FOREIGN) and "StopLoss" in res["reason"]


def test_position_not_matching_journal_is_foreign():
    book = Book()
    own(book=book)                                                             # бот: шорт 0.001
    book.position["BTCUSDT"] -= D("0.002")                                    # владелец добавил шорт вручную
    res = submit(Session(book.routes("bybit")), BY())
    assert res["reason"].startswith(risk.FOREIGN) and "short 0.003" in res["reason"]


def test_position_closed_by_venue_syncs_journal_with_event():
    """На бирже пусто, у бота по журналу шорт (стоп сработал): поправка обнуляет сальдо, событие владельцу, открытие
    идёт дальше."""
    book = Book()
    own()                                                                      # на «бирже» позиции нет
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == D("-0.001")
    res = submit(Session(book.routes("bybit")), BY())
    assert res["state"] == "open"
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["net"] == 0
    assert [e["event"] for e in journal.pending_events()] == ["closed_by_venue"]


@pytest.mark.parametrize("route,answer", [
    (("GET", "/v5/position/list"), Resp(500, b"")),
    (("GET", "/v5/order/realtime"), Resp(502, b"")),
    (("GET", "/v5/position/list"), bybit_ok({"list": [{"symbol": "BTCUSDT", "positionIdx": 1, "side": "Buy",
                                                       "size": "0.1", "leverage": "2"}]})),     # режим хеджа
    (("GET", "/v5/position/list"), bybit_ok({"list": [{"symbol": "ETHUSDT", "positionIdx": 0, "side": "",
                                                       "size": "0", "leverage": "2"}]})),       # чужой символ
    (("GET", "/v5/position/list"), bybit_ok({"list": [{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Buy",
                                                       "size": "x", "leverage": "2"}]})),       # битое количество
    (("GET", "/v5/order/realtime"), bybit_ok({"list": [dict(MANUAL_BY, symbol="GRAMUSDT")]})),
    (("GET", "/v5/order/realtime"), bybit_ok({"list": [dict(MANUAL_BY, orderLinkId=f"x{i}") for i in range(50)]})),
])
def test_unreadable_or_strange_snapshot_refuses(route, answer):
    book = Book()
    s = Session(book.routes("bybit") | {route: lambda c, a=answer: a if "orderLinkId" not in c["query"] else
                                        book.bybit_query(c)})
    res = submit(s, BY())
    assert res["state"] == "refused" and not creates(s), res["reason"]


# --- 1. закрытие — только своя позиция бота по журналу ---

def test_close_limited_to_own_journal_size():
    book = Book()
    own(book=book)                                                             # бот: шорт 0.001
    book.position["BTCUSDT"] -= D("0.01")                                     # плюс шорт владельца 0.01
    s = Session(book.routes("bybit"))
    res = submit(s, BY(side="buy", qty="0.002", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and "своей позиции бота" in res["reason"] and not creates(s)
    assert submit(s, BY(side="sell", qty="0.001", reduce_only=True), purpose="close")["state"] == "refused"
    assert submit(s, BY(side="buy", reduce_only=True), purpose="close")["state"] == "open"
    again = submit(s, BY(side="buy", reduce_only=True), purpose="close")       # первое закрытие ещё не исполнено
    assert again["state"] == "refused" and len(creates(s)) == 1


def test_close_refused_without_own_position_even_if_owner_has_one():
    book = Book()
    book.position["BTCUSDT"] = D("-0.5")
    s = Session(book.routes("bybit"))
    res = submit(s, BY(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "refused" and "нет своей позиции" in res["reason"] and not creates(s)


def test_close_limited_per_strategy_group():
    own(strategy="hedge", group="c1")
    own(strategy="hedge", group="c2", qty="0.002")
    s = Session(Book().routes("bybit"))
    res = submit(s, BY(side="buy", qty="0.002", reduce_only=True), purpose="close", strategy="hedge", group="c1")
    assert res["state"] == "refused"
    assert submit(s, BY(side="buy", qty="0.001", reduce_only=True), purpose="close", strategy="hedge",
                  group="c1")["state"] == "open"


# --- 1. свои встречные позиции и перекрывающиеся стопы на одном символе ---

def test_bot_netting_and_overlapping_stops_refused():
    book = Book()
    own(strategy="hedge", group="c1", book=book)                               # шорт хеджа без стопа
    s = Session(book.routes("bybit"))
    res = submit(s, BY(side="buy", stop_loss="60000"), strategy="directional")
    assert res["state"] == "refused" and "встречная" in res["reason"]
    res = submit(s, BY(stop_loss="70000"), strategy="funding")
    assert res["state"] == "refused" and "перекрывающиеся стопы" in res["reason"]
    assert submit(s, BY(), strategy="hedge", group="c1")["state"] == "open"   # тот же круг — добавить можно
    assert not [c for c in creates(s) if body(c)["side"] == "Buy"]


# --- 6. плечо и режим маржи — с биржи ---

def test_actual_leverage_from_venue():
    book = Book()
    book.leverage = "3"
    s = Session(book.routes("bybit"))
    assert submit(s, BY(), mode="confirm")["state"] == "open"                 # хедж: потолок 3
    res = submit(s, BY(stop_loss="70000"), strategy="directional")             # направленная: потолок 2
    assert res["state"] == "refused" and "плечо символа на бирже 3" in res["reason"]
    book.leverage = ""
    res = submit(Session(book.routes("bybit")), BY())
    assert res["state"] == "refused" and "плечо" in res["reason"]


def test_bingx_leverage_unreadable_refuses():
    book = Book()
    s = Session(book.routes("bingx") | {("GET", "/openApi/swap/v2/trade/leverage"): Resp(502, b"")})
    res = submit(s, BX())
    assert res["state"] == "refused" and "плечо" in res["reason"] and not creates(s)


def test_set_leverage_only_without_foreign_and_110043_is_ok():
    book = Book()
    book.foreign.append(MANUAL_BY)
    s = Session(book.routes("bybit") | {LEV_BY: bybit_err(110043, "leverage not modified")})
    kind, why = run(journal.set_leverage(s, "bybit", "BTCUSDT", 2, CREDS))
    assert kind == "refused" and why.startswith(risk.FOREIGN) and not s.sent(*LEV_BY)
    book.foreign.clear()
    assert run(journal.set_leverage(s, "bybit", "BTCUSDT", 2, CREDS))[0] == "ok"
    assert body(s.sent(*LEV_BY)[0]) == {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2",
                                        "sellLeverage": "2"}
    with pytest.raises(ValueError):
        run(journal.set_leverage(s, "bybit", "BTCUSDT", 4, CREDS))
    assert len(s.sent(*LEV_BY)) == 1
    s.routes[LEV_BY] = bybit_err(110043)
    assert venues.leverage_outcome("bybit", 200, {"retCode": 110043})[0] == "ok"
    assert venues.leverage_outcome("bybit", 200, {"retCode": 110044})[0] != "ok"
    assert venues.outcome("bybit", 200, {"retCode": 110043})[0] == "rejected"     # для ордеров — отказ, как был


def test_set_leverage_and_margin_bingx_refused_with_owner_position():
    book = Book()
    book.position["ETH-USDT"] = D("1")
    s = Session(book.routes("bingx") | {LEV_BX: bingx_ok({}), MARGIN_BX: bingx_ok({})})
    assert run(journal.set_leverage(s, "bingx", "ETHUSDT", 2, CREDS))[0] == "refused"
    assert run(journal.set_margin_isolated(s, "ETHUSDT", CREDS))[0] == "refused"
    assert not s.sent(*LEV_BX) and not s.sent(*MARGIN_BX)
    book.position.clear()
    assert run(journal.set_margin_isolated(s, "ETHUSDT", CREDS))[0] == "ok"
    assert s.sent(*MARGIN_BX)[0]["query"]["marginType"] == "ISOLATED"


# --- 7. стоп всей позиции (tpslMode=Full) — только когда вся позиция бота ---

def test_set_stop_only_on_whole_own_position():
    book = Book()
    book.liq["BTCUSDT"] = "96000"
    s = Session(book.routes("bybit") | {STOP_BY: bybit_ok({})})
    assert run(journal.set_stop(s, "bybit", "BTCUSDT", "70000", CREDS))[0] == "refused"    # позиции бота нет
    own(book=book)
    book.position["BTCUSDT"] -= D("0.004")                                    # и шорт владельца на том же символе
    kind, why = run(journal.set_stop(s, "bybit", "BTCUSDT", "70000", CREDS))
    assert kind == "refused" and why.startswith(risk.FOREIGN)
    book.position["BTCUSDT"] += D("0.004")
    assert run(journal.set_stop(s, "bybit", "BTCUSDT", "64000", CREDS))[0] == "refused"    # шорт: стоп ниже mark
    assert run(journal.set_stop(s, "bybit", "BTCUSDT", "97000", CREDS))[0] == "refused"    # за ликвидацией
    assert not s.sent(*STOP_BY)
    assert run(journal.set_stop(s, "bybit", "BTCUSDT", "70000", CREDS))[0] == "ok"
    assert body(s.sent(*STOP_BY)[0]) == {"category": "linear", "symbol": "BTCUSDT", "tpslMode": "Full",
                                         "positionIdx": 0, "stopLoss": "70000", "slTriggerBy": "MarkPrice"}
    assert journal.bot_book("bybit", "linear", "BTCUSDT")["stop"] == D("70000")
    book.foreign.append(stop_order("70000"))                                  # теперь стоп позиции — бота
    snap = run(ownership.fetch(s, "bybit", "linear", "BTCUSDT", "BTCUSDT", CREDS, market=False))
    assert ownership.foreign(snap, journal.bot_book("bybit", "linear", "BTCUSDT")) == []
    assert run(journal.set_stop(s, "bingx", "ETHUSDT", "3500", CREDS))[0] == "refused"


# --- 11. кросс-маржа основного аккаунта ---

def test_cross_margin_rules_in_submit(monkeypatch):
    book = Book()
    book.margin["bybit"] = "REGULAR_MARGIN"
    s = Session(book.routes("bybit"))
    res = submit(s, BY(stop_loss="70000"))                                     # confirm — кросс нельзя
    assert res["state"] == "refused" and "только в режиме minlot" in res["reason"]
    monkeypatch.setenv("TRADING_MODE", "minlot")
    book.marks["BTCUSDT"] = "40000"                                             # 0.001 BTC = 40 USDT ≤ 50
    res = submit(s, BY())
    assert res["state"] == "refused" and res["reason"] == "кросс-маржа: нужен стоп с правильной стороны — иначе "         "убыток не ограничен"
    res = submit(s, BY(stop_loss="45000"))                                      # 0.001×5000×1.5 + 0.08 > 5
    assert res["reason"].startswith("кросс-маржа: худший убыток по стопу 7.58")
    book.other.append(("DOGEUSDT", D("100")))                                  # позиция владельца на другой монете
    res = submit(s, BY(stop_loss="41000"))
    assert res["state"] == "refused" and "ваши позиции" in res["reason"] and "DOGEUSDT" in res["reason"]
    book.other.clear()
    assert submit(s, BY(stop_loss="41000"))["state"] == "open"                 # 1.5 + 0.08 ≤ 5, аккаунт чистый
    assert "только изолированная" in submit(s, BY(side="buy", stop_loss="39900"), strategy="directional")["reason"]


def test_startup_warnings():
    w = risk.startup_warnings({"bybit": "cross", "bingx": "isolated"},
                              {"bybit": keys.KeyCheck(True, "ok", "", False),
                               "bingx": keys.KeyCheck(False, "unsafe", "у ключа лишние права: вывод", True)})
    assert any(risk.CROSS_NOTE in x and x.startswith("bybit") for x in w)
    assert any("без привязки к IP" in x for x in w) and any("bingx: торговля по ключу запрещена" in x for x in w)
    assert risk.startup_warnings({"bybit": None}) == ["bybit: режим маржи не прочитан (None) — открытия запрещены"]
    assert risk.startup_warnings({"bybit": "isolated"}, {"bybit": keys.KeyCheck(True, "ok", "", True)}) == []


# --- 5, 8. жёсткие проверки открытия внутри submit — без precheck ---

def test_submit_hard_checks_without_precheck(monkeypatch):
    book = Book()
    s = Session(book.routes("bybit"))
    row = journal._insert_intent(BY(), "open", "hedge", "confirm", None)      # брошенная отправка
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="unknown")
    res = submit(s, BY())
    assert res["state"] == "refused" and "неясным исходом" in res["reason"] and s.calls == []
    journal.resolve(row["client_id"], "rejected", "проверил")
    for _ in range(10):                                                         # 10 ордеров за минуту (отклонены)
        r = journal._insert_intent(BY(), "close", "hedge", "confirm", None)
        journal._update(r["client_id"], state="sending")
        journal._update(r["client_id"], state="rejected")
    assert "в минуту" in submit(s, BY())["reason"] and s.calls == []


def test_minlot_cap_counts_pending_opens(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "minlot")
    book = Book()
    s = Session(book.routes("bybit"))
    assert submit(s, BY(qty="0.0006"))["state"] == "refused"                   # не кратно шагу 0.001
    first = submit(s, BY(order_type="limit", price="65000"))                   # 65 > 50
    assert first["state"] == "refused" and "итоговая позиция" in first["reason"]
    book.marks["BTCUSDT"] = "40000"
    assert submit(s, BY())["state"] == "open"                                  # 40 USDT, ещё не исполнен
    res = submit(s, BY())                                                       # 40 ожидающих + 40 > 50
    assert res["state"] == "refused" and "итоговая позиция" in res["reason"] and len(creates(s)) == 1


def test_unchecked_or_failed_key_refuses_open_before_network(monkeypatch, tmp_path):
    s = Session(Book().routes("bybit"))
    monkeypatch.setattr(keys, "CHECK_PATH", str(tmp_path / "none.json"))
    res = submit(s, BY())
    assert res["state"] == "refused" and "не проверены" in res["reason"] and s.calls == []
    keys.save_check("bybit", CREDS, keys.KeyCheck(False, "unsafe", "вывод", True))
    assert "не пройдена" in submit(s, BY())["reason"] and s.calls == []


def test_directional_stop_rules_in_submit():
    s = Session(Book().routes("bybit"))
    assert "нужен стоп" in submit(s, BY(side="buy"), strategy="directional")["reason"]
    assert "худший убыток" in submit(s, BY(side="buy", qty="0.002", stop_loss="40000"),
                                     strategy="directional")["reason"]
    res = submit(s, BY(side="buy", stop_loss="33000"), strategy="directional")   # ликвидация ≈ 33150 при плече 2
    assert res["reason"] == "стоп за ценой ликвидации — сработает позже ликвидации"
    assert submit(s, BY(side="buy", stop_loss="60000"), strategy="directional")["state"] == "open"


def test_hedge_must_be_short_in_submit():
    res = submit(Session(Book().routes("bybit")), BY(side="buy"))
    assert res["state"] == "refused" and "только шорт" in res["reason"]


# --- чистые функции ownership ---

def _snap(net, orders, errors=()):
    return ownership.Snapshot("bybit", "linear", "BTCUSDT", "BTCUSDT", {"net": D(net), "rows": [], "leverage": D(2)},
                              orders, D(2), "isolated", None, None, None, tuple(errors))


def _order(cid="", stop_type="", trigger=None, side="buy", oid="1"):
    return {"client_id": cid, "order_id": oid, "raw_symbol": "BTCUSDT", "side": side, "type": "market", "qty": None,
            "price": None, "trigger": None if trigger is None else D(trigger), "stop_type": stop_type,
            "reduce_only": True}


def test_foreign_pure():
    book = {"net": D("-0.001"), "client_ids": {"tbot"}, "stop": D("70000"), "active": [], "uncertain": []}
    assert ownership.foreign(_snap("-0.001", [_order("tbot"), _order(stop_type="StopLoss", trigger="70000")]),
                             book) == []
    assert ownership.foreign(_snap("-0.001", [_order(stop_type="StopLoss", trigger="70000", side="sell")]), book)
    assert ownership.foreign(_snap("-0.002", []), book)
    assert ownership.foreign(_snap("0", [_order(stop_type="StopLoss", trigger="70000")]), book)
    assert ownership.foreign(_snap("0", []), book) == [] and ownership.flat_external(_snap("0", []), book)
    assert not ownership.flat_external(_snap("0", []), dict(book, active=["x"]))
    assert ownership.foreign(_snap("0", [], errors=("позиция: 500",)), book) is None
    assert ownership.foreign(_snap("0", None), book) is None
    snap = _snap("0", [])._replace(account=[{"raw_symbol": "DOGEUSDT", "symbol": None, "signed": D(5)},
                                            {"raw_symbol": "BTCUSDT", "symbol": "BTCUSDT", "signed": D("-0.001")}])
    assert ownership.foreign_account(snap, {"BTCUSDT": D("-0.001")}) == ["DOGEUSDT 5"]
    assert ownership.foreign_account(snap._replace(account=None), {}) is None


def test_spot_funding_buy_checks_orders_of_symbol():
    book = Book()
    s = Session(book.routes("bybit"))
    buy = venues.Order("bybit", "spot", "ETHUSDT", "buy", "market", "0.01")
    assert submit(s, buy, strategy="funding")["state"] == "open"
    assert body(creates(s)[0])["marketUnit"] == "baseCoin"
    assert not s.sent("GET", "/v5/position/list") and not s.sent("GET", "/v5/account/info")   # у спота позиций нет
    book.foreign.append(dict(MANUAL_BY, symbol="ETHUSDT"))
    res = submit(s, buy, strategy="funding")
    assert res["reason"].startswith(risk.FOREIGN) and len(creates(s)) == 1
    book.foreign.clear()
    assert "спот — только покупка ноги фандинга" in submit(s, buy, strategy="hedge")["reason"]


def test_funding_spot_buy_and_perp_short_on_one_venue_symbol():
    """Спот-нога не сальдируется с перп-ногой: шорт перпа фандинга после покупки спота на той же бирже — можно."""
    book = Book()
    s = Session(book.routes("bybit"))
    spot = venues.Order("bybit", "spot", "ETHUSDT", "buy", "market", "0.01")
    perp = venues.Order("bybit", "linear", "ETHUSDT", "sell", "market", "0.01")
    assert submit(s, spot, strategy="funding", group="f1")["state"] == "open"
    res = submit(s, perp, strategy="funding", group="f1")
    assert res["state"] == "open", res["reason"]
    assert [body(c)["category"] for c in creates(s)] == ["spot", "linear"]


def test_position_actions_only_for_bot_positions_from_exchange():
    """venues.positions отдаёт и позиции владельца; own_positions помечает свои — сопровождение трогает только их."""
    book = Book()
    own(book=book)                                                             # бот: шорт BTC 0.001
    book.position["ETH-USDT"] = D("-1")                                        # владелец: шорт ETH на BingX
    book.liq.update({"BTCUSDT": "70000", "ETH-USDT": "3100"})                 # обе близко к ликвидации
    s = Session(book.routes("bybit") | book.routes("bingx"))
    rows = run(venues.positions(s, "bybit", CREDS))[0] + run(venues.positions(s, "bingx", CREDS))[0]
    marked = journal.own_positions(rows)
    assert [(p["symbol"], p["owned"], p["bot_size"]) for p in marked] == [("BTCUSDT", True, D("0.001")),
                                                                          ("ETHUSDT", False, D(0))]
    acts = risk.position_actions(marked)
    assert [(p["symbol"], a) for p, a, _ in acts] == [("BTCUSDT", "reduce")]
    book.position["BTCUSDT"] -= D("0.002")                                    # владелец добавил к шорту бота
    marked = journal.own_positions(run(venues.positions(s, "bybit", CREDS))[0])
    assert marked[0]["owned"] is False and marked[0]["bot_size"] == D("0.001")
    assert risk.position_actions(marked) == []


def test_exposure_counts_fills_and_pending_opens():
    own(strategy="hedge", group="c1")                                          # 0.001 исполнено
    row = journal._insert_intent(BY(qty="0.002"), "open", "hedge", "confirm", D("130"), group="c1")
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="open", filled="0.0005")
    ex = journal.exposure({("bybit", "BTCUSDT"): D("60000")})
    assert len(ex) == 1 and ex[0]["qty"] == D("0.0030") and ex[0]["side"] == "short" and ex[0]["group"] == "c1"
    assert ex[0]["notional"] == D("0.0030") * 60000 and ex[0]["net"] == D("-0.0015")
    assert time.time()
