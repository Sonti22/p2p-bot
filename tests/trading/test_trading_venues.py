"""Торговое ядро, venues: allowlist (метод, путь) и полей, запреты (вывод, переводы, P2P…), подпись по точным байтам,
без редиректов, разбор ответов, символы (TON → GRAM, ловушка BingX GRAM-USDT), режим маржи. Сеть — заглушка;
ключи — фиктивные."""
import ast
import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import time
from decimal import Decimal
from urllib.parse import unquote

import pytest

import accounts
from test_payout_pins import _senders
from trading import gates, journal, keys, ownership, risk, switch, venues
from trading_stubs import CREDS, KEY, SECRET, Book, Resp, Session, bingx_err, bingx_ok, bybit_err, bybit_ok, loop, run

CID = "t260927013512a1b2c3d4"
TS = "1700000000000"
T0 = 1_700_000_000_000
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _fresh_symbols(monkeypatch):
    monkeypatch.setattr(venues, "_RESOLVED", {})


def linear(**over):
    p = {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001",
         "orderLinkId": CID, "positionIdx": 0}
    p.update(over)
    return p


def swap(**over):
    p = {"symbol": "ETH-USDT", "side": "SELL", "positionSide": "BOTH", "type": "MARKET", "quantity": "0.01",
         "clientOrderId": CID}
    p.update(over)
    return p


# --- allowlist: ровно эти вызовы ---

def test_allowlist_is_exactly_the_approved_calls():
    """Стопа всей позиции символа (Bybit trading-stop) нет; BingX — история ордеров символа (исполнения без id
    бота)."""
    assert set(venues.ALLOWED[venues.BYBIT]) == {
        ("POST", "/v5/order/create"), ("POST", "/v5/order/cancel"),
        ("POST", "/v5/position/set-leverage"), ("GET", "/v5/order/realtime"), ("GET", "/v5/order/history"),
        ("GET", "/v5/execution/list"), ("GET", "/v5/position/list"), ("GET", "/v5/position/closed-pnl"),
        ("GET", "/v5/account/wallet-balance"), ("GET", "/v5/account/info"), ("GET", "/v5/user/query-api")}
    assert set(venues.ALLOWED[venues.BINGX]) == {
        ("POST", "/openApi/swap/v2/trade/order"), ("DELETE", "/openApi/swap/v2/trade/order"),
        ("POST", "/openApi/swap/v2/trade/leverage"), ("POST", "/openApi/swap/v2/trade/marginType"),
        ("GET", "/openApi/swap/v2/trade/order"), ("GET", "/openApi/swap/v2/trade/openOrders"),
        ("GET", "/openApi/swap/v2/trade/allOrders"),
        ("GET", "/openApi/swap/v2/user/positions"), ("GET", "/openApi/swap/v3/user/balance"),
        ("GET", "/openApi/swap/v2/user/income"), ("GET", "/openApi/swap/v2/trade/leverage"),
        ("GET", "/openApi/swap/v2/trade/marginType"), ("GET", "/openApi/swap/v1/positionSide/dual"),
        ("GET", "/openApi/v1/account/apiPermissions")}
    assert {v: set(p) for v, p in venues.PUBLIC.items()} == {
        "bybit": {"/v5/market/instruments-info", "/v5/market/tickers"},
        "bingx": {"/openApi/swap/v2/quote/contracts", "/openApi/swap/v2/quote/ticker"}}
    assert all(sp.kind == "read" for calls in venues.ALLOWED.values() for (m, _), sp in calls.items() if m == "GET")
    assert all(sp.kind == "write" for calls in venues.ALLOWED.values() for (m, _), sp in calls.items() if m != "GET")


def test_symbol_table_ton_is_gram_and_bingx_gram_trap_absent():
    assert venues.CANDIDATES == {
        ("bybit", "linear"): {"BTCUSDT": ("BTCUSDT",), "ETHUSDT": ("ETHUSDT",), "TONUSDT": ("GRAMUSDT",)},
        ("bybit", "spot"): {"BTCUSDT": ("BTCUSDT",), "ETHUSDT": ("ETHUSDT",), "TONUSDT": ("GRAMUSDT",)},
        ("bingx", "swap"): {"BTCUSDT": ("BTC-USDT",), "ETHUSDT": ("ETH-USDT",), "TONUSDT": ("GRAMTON-USDT",)}}
    assert "GRAM-USDT" not in venues.VENUE_SYMBOLS["bingx"] and "TONUSDT" not in venues.VENUE_SYMBOLS["bybit"]
    assert venues.canonical_symbol("bybit", "GRAMUSDT") == "TONUSDT"
    assert venues.canonical_symbol("bingx", "GRAMTON-USDT") == "TONUSDT"
    for trap in ("GRAM-USDT", "TON-USDT", "GRM-USDT"):
        assert venues.canonical_symbol("bingx", trap) is None
    assert venues.canonical_symbol("bybit", "TONUSDT") is None


@pytest.mark.parametrize("venue,method,path,params", [
    ("bingx", "POST", "/openApi/swap/v2/trade/order", swap(symbol="GRAM-USDT")),
    ("bingx", "GET", "/openApi/swap/v2/trade/order", {"symbol": "GRAM-USDT", "clientOrderId": CID}),
    ("bingx", "POST", "/openApi/swap/v2/trade/order", swap(symbol="TON-USDT")),
    ("bybit", "POST", "/v5/order/create", linear(symbol="TONUSDT")),
    ("bybit", "POST", "/v5/order/create", linear(symbol="GRAMTON-USDT")),
])
def test_wrong_ton_symbols_refused_before_signing(venue, method, path, params):
    s = Session()
    with pytest.raises(ValueError):
        run(venues.call(s, venue, method, path, params, CREDS))
    with pytest.raises(ValueError):
        run(venues.public_get(s, "bingx", "/openApi/swap/v2/quote/ticker", {"symbol": "GRAM-USDT"}))
    assert s.calls == []


DENY = re.compile(r"withdraw|transfer|p2p|sub-?member|subaccount|sub-api|deposit|convert|exchange|loan|earn|asset|"
                  r"closeall|reverse|batch|margin-mode|switch-mode|switch-isolated|twap|getvst|autoaddmargin|"
                  r"positionmargin|add-margin|update-api|create-sub|spot-margin|borrow|repay|vst", re.I)


def test_no_allowlisted_path_looks_like_money_movement():
    for venue, calls in list(venues.ALLOWED.items()) + [(v, {("GET", p): None for p in ps})
                                                        for v, ps in venues.PUBLIC.items()]:
        for method, path in calls:
            assert not DENY.search(path), (venue, method, path)


@pytest.mark.parametrize("venue,method,path", [
    # Bybit: вывод, переводы, субаккаунты, P2P, займы, earn, конвертация, пакеты, режимы
    ("bybit", "POST", "/v5/asset/withdraw/create"), ("bybit", "POST", "/v5/asset/withdraw/cancel"),
    ("bybit", "POST", "/v5/asset/transfer/inter-transfer"), ("bybit", "POST", "/v5/asset/transfer/universal-transfer"),
    ("bybit", "POST", "/v5/asset/transfer/save-transfer-sub-member"), ("bybit", "POST", "/v5/user/create-sub-member"),
    ("bybit", "POST", "/v5/user/create-sub-api"), ("bybit", "POST", "/v5/user/update-api"),
    ("bybit", "POST", "/v5/user/update-sub-api"), ("bybit", "POST", "/v5/user/del-submember"),
    ("bybit", "POST", "/v5/p2p/order/finish"), ("bybit", "POST", "/v5/p2p/order/pay"),
    ("bybit", "POST", "/v5/p2p/item/create"), ("bybit", "POST", "/v5/p2p/order/simplifyList"),
    ("bybit", "POST", "/v5/asset/exchange/convert-execute"), ("bybit", "POST", "/v5/asset/exchange/quote-apply"),
    ("bybit", "POST", "/v5/crypto-loan/borrow"), ("bybit", "POST", "/v5/earn/place-order"),
    ("bybit", "POST", "/v5/spot-margin-trade/switch-mode"), ("bybit", "POST", "/v5/account/set-margin-mode"),
    ("bybit", "POST", "/v5/position/switch-mode"), ("bybit", "POST", "/v5/position/add-margin"),
    ("bybit", "POST", "/v5/order/create-batch"), ("bybit", "POST", "/v5/order/cancel-all"),
    ("bybit", "POST", "/v5/order/amend"), ("bybit", "POST", "/v5/asset/deposit/deposit-to-account"),
    ("bybit", "POST", "/v5/order/disconnected-cancel-all"), ("bybit", "POST", "/v5/order/pre-check"),
    ("bybit", "GET", "/v5/asset/withdraw/query-record"), ("bybit", "GET", "/v5/order/create"),
    ("bybit", "DELETE", "/v5/order/create"), ("bybit", "POST", "/v5/order/create/"),
    ("bybit", "POST", "/v5/order/../asset/withdraw/create"), ("bybit", "POST", "/V5/ORDER/CREATE"),
    ("bybit", "POST", "/v5/order/create?category=linear"),
    # BingX: вывод, переводы, субаккаунты, закрыть всё, разворот, пакеты, режимы, VST, TWAP
    ("bingx", "POST", "/openApi/wallets/v1/capital/withdraw/apply"),
    ("bingx", "POST", "/openApi/api/v3/post/asset/transfer"), ("bingx", "POST", "/openApi/api/asset/v1/transfer"),
    ("bingx", "POST", "/openApi/wallets/v1/capital/innerTransfer/apply"),
    ("bingx", "POST", "/openApi/subAccount/v1/create"), ("bingx", "POST", "/openApi/subAccount/v1/transfer"),
    ("bingx", "POST", "/openApi/swap/v2/trade/closeAllPositions"), ("bingx", "POST", "/openApi/swap/v1/trade/closePosition"),
    ("bingx", "POST", "/openApi/swap/v1/trade/reverse"), ("bingx", "POST", "/openApi/swap/v2/trade/batchOrders"),
    ("bingx", "DELETE", "/openApi/swap/v2/trade/allOpenOrders"), ("bingx", "POST", "/openApi/swap/v1/positionSide/dual"),
    ("bingx", "POST", "/openApi/swap/v2/trade/positionMargin"), ("bingx", "POST", "/openApi/swap/v1/trade/assetMode"),
    ("bingx", "POST", "/openApi/swap/v1/trade/autoAddMargin"), ("bingx", "POST", "/openApi/swap/v2/trade/getVst"),
    ("bingx", "POST", "/openApi/swap/v1/twap/order"), ("bingx", "POST", "/openApi/swap/v1/trade/cancelReplace"),
    ("bingx", "POST", "/openApi/swap/v1/trade/amend"), ("bingx", "POST", "/openApi/swap/v2/trade/order/test"),
    ("bingx", "POST", "/openApi/spot/v1/trade/order"), ("bingx", "POST", "/openApi/swap/v2/trade/cancelAllAfter"),
    ("bingx", "GET", "/openApi/api/v3/capital/withdraw/history"), ("bingx", "PUT", "/openApi/swap/v2/trade/order"),
    # чужая биржа
    ("mexc", "GET", "/api/v3/account"), ("cryptomus", "POST", "/v1/balance"),
])
def test_denylist_refused_before_signing(venue, method, path):
    s = Session()
    with pytest.raises(ValueError):
        run(venues.call(s, venue, method, path, {"symbol": "BTCUSDT", "amount": "1"}, CREDS))
    assert s.calls == []


@pytest.mark.parametrize("venue,path", [("bybit", "/v5/market/orderbook"), ("bybit", "/v5/asset/withdraw/create"),
                                        ("bingx", "/openApi/swap/v2/trade/order"), ("okx", "/api/v5/market/tickers")])
def test_public_get_only_public_list(venue, path):
    s = Session()
    with pytest.raises(ValueError):
        run(venues.public_get(s, venue, path, {}))
    assert s.calls == []


@pytest.mark.parametrize("field,value", [
    ("isLeverage", 1), ("triggerPrice", "60000"), ("closeOnTrigger", True), ("takeProfit", "70000"),
    ("orderFilter", "tpslOrder"), ("slippageTolerance", "1"), ("timestamp", TS), ("signature", "x"),
    ("recvWindow", "5000"), ("smpType", "None"), ("orderIv", "0.5"), ("marketUnit", "quoteCoin"),
    ("stopLoss", "70000"), ("tpslMode", "Full"), ("tpslMode", "Partial"), ("slTriggerBy", "MarkPrice"),
    ("slSize", "0.001"), ("slOrderType", "Market"),
])
def test_bybit_order_extra_or_foreign_fields_refused(field, value):
    """Стоп всей позиции символа у ордера (stopLoss/tpslMode) — нет: стоп бота — отдельный условный ордер."""
    with pytest.raises(ValueError):
        venues.prepare("bybit", "POST", "/v5/order/create", linear(**{field: value}), CREDS)


@pytest.mark.parametrize("field,value", [
    ("quoteOrderQty", "100"), ("stopPrice", "2000"), ("closePosition", "true"), ("takeProfit", "{}"),
    ("priceRate", "0.05"), ("activationPrice", "2000"), ("stopGuaranteed", "true"), ("positionId", "1"),
    ("workingType", "MARK_PRICE"), ("timestamp", TS), ("signature", "x"), ("recvWindow", "5000"),
    ("stopLoss", '{"type":"STOP_MARKET","stopPrice":2900,"workingType":"MARK_PRICE"}'),
])
def test_bingx_order_extra_fields_refused(field, value):
    with pytest.raises(ValueError):
        venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(**{field: value}), CREDS)


@pytest.mark.parametrize("over", [
    {"symbol": "DOGEUSDT"}, {"symbol": "btcusdt"}, {"symbol": "BTCUSDT "}, {"symbol": "BTC-USDT"},
    {"category": "inverse"}, {"category": "option"}, {"side": "buy"}, {"orderType": "StopMarket"},
    {"qty": "-1"}, {"qty": "0"}, {"qty": "1e3"}, {"qty": "1,5"}, {"qty": 0.001}, {"qty": "0.0000000000001"},
    {"qty": " 1"}, {"qty": "NaN"}, {"positionIdx": True}, {"positionIdx": 1}, {"positionIdx": 2},
    {"positionIdx": "0"}, {"reduceOnly": "true"}, {"orderLinkId": "manual-order-1"}, {"orderLinkId": CID.upper()},
    {"orderLinkId": CID + "&x=1"}, {"stopLoss": "0"}, {"tpslMode": "Partial"}, {"timeInForce": "RPI"},
])
def test_bybit_order_bad_values_refused(over):
    with pytest.raises(ValueError):
        venues.prepare("bybit", "POST", "/v5/order/create", linear(**over), CREDS)


@pytest.mark.parametrize("params", [
    linear(category="spot", positionIdx=None),                                   # None — не по правилу
    {k: v for k, v in linear(category="spot").items() if k != "positionIdx"},  # спот-маркет без marketUnit
    {**{k: v for k, v in linear(category="spot").items() if k != "positionIdx"}, "marketUnit": "baseCoin",
     "reduceOnly": True},
    linear(orderType="Limit"),                                                  # лимитка без цены
    linear(price="60000"),                                                      # маркет с ценой
    linear(timeInForce="GTC"),                                                  # маркет с timeInForce
    {k: v for k, v in linear().items() if k != "positionIdx"},                  # linear без positionIdx
    linear(reduceOnly=True, stopLoss="70000"),                                  # стоп позиции — нет
    linear(tpslMode="Full"),                                                    # tpslMode — нет
    linear(marketUnit="baseCoin"),                                              # marketUnit не для linear
    linear(orderType="Limit", price="60000"),                                   # лимитка без IOC/FOK
    linear(orderType="Limit", price="60000", timeInForce="GTC"),                # висящая лимитка — нет
    linear(orderType="Limit", price="60000", timeInForce="PostOnly"),
    linear(triggerPrice="70000", triggerDirection=1, triggerBy="MarkPrice"),    # условный без reduceOnly
    linear(side="Buy", reduceOnly=True, triggerPrice="70000", triggerDirection=2, triggerBy="MarkPrice"),  # не туда
    linear(reduceOnly=True, triggerPrice="60000", triggerDirection=2),          # без triggerBy
    linear(reduceOnly=True, triggerPrice="60000", triggerDirection=2, triggerBy="LastPrice"),
    linear(reduceOnly=True, triggerPrice="60000", triggerDirection="2", triggerBy="MarkPrice"),
    linear(orderType="Limit", price="60000", timeInForce="IOC", reduceOnly=True, triggerPrice="60000",
           triggerDirection=2, triggerBy="MarkPrice"),                          # условная лимитка — нет
    {**{k: v for k, v in linear(category="spot").items() if k != "positionIdx"}, "marketUnit": "baseCoin",
     "triggerPrice": "1", "triggerDirection": 2, "triggerBy": "MarkPrice"},     # условный на споте — нет
])
def test_bybit_cross_field_rules(params):
    with pytest.raises(ValueError):
        venues.prepare("bybit", "POST", "/v5/order/create", params, CREDS)


def test_bot_stop_orders_are_conditional_reduce_only_with_our_id():
    """Стоп бота — условный рыночный reduceOnly-ордер с нашим клиентским id на размер позиции бота (Bybit: triggerPrice
    по mark в сторону убытка; BingX: STOP_MARKET) — не стоп всей позиции символа."""
    stop = venues.Order("bybit", "linear", "BTCUSDT", "sell", "stop", "0.001", reduce_only=True, trigger="60000")
    _, _, p = venues.create_call(stop, CID)
    assert p == {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001",
                 "orderLinkId": CID, "positionIdx": 0, "reduceOnly": True, "triggerPrice": "60000",
                 "triggerDirection": 2, "triggerBy": "MarkPrice"}
    short_stop = venues.Order("bybit", "linear", "BTCUSDT", "buy", "stop", "0.001", reduce_only=True, trigger="70000")
    assert venues.create_call(short_stop, CID)[2]["triggerDirection"] == 1
    bx = venues.Order("bingx", "swap", "ETHUSDT", "buy", "stop", "0.01", reduce_only=True, trigger="3500")
    _, _, q = venues.create_call(bx, CID)
    assert q == {"symbol": "ETH-USDT", "side": "BUY", "positionSide": "BOTH", "type": "STOP_MARKET",
                 "quantity": "0.01", "stopPrice": "3500", "workingType": "MARK_PRICE", "clientOrderId": CID,
                 "reduceOnly": "true"}
    venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", q, CREDS)
    for bad in (dict(reduce_only=False), dict(trigger=None), dict(price="1"), dict(tif="IOC")):
        kw = dict(reduce_only=True, trigger="60000")
        kw.update(bad)
        with pytest.raises(ValueError):
            venues.create_call(venues.Order("bybit", "linear", "BTCUSDT", "sell", "stop", "0.001", **kw), CID)
    with pytest.raises(ValueError):                                             # спот — без условных
        venues.create_call(venues.Order("bybit", "spot", "BTCUSDT", "sell", "stop", "0.001", trigger="1"), CID)
    with pytest.raises(ValueError):                                             # цена срабатывания — только у стопа
        venues.create_call(venues.Order("bybit", "linear", "BTCUSDT", "sell", "market", "0.001", trigger="1"), CID)
    for over in ({"type": "STOP_MARKET"}, {"type": "STOP_MARKET", "stopPrice": "3500", "workingType": "MARK_PRICE"},
                 {"type": "STOP_MARKET", "stopPrice": "3500", "workingType": "MARK_PRICE", "reduceOnly": "false"},
                 {"type": "STOP_MARKET", "stopPrice": "3500", "workingType": "CONTRACT_PRICE", "reduceOnly": "true"},
                 {"type": "LIMIT", "price": "3000", "timeInForce": "GTC"}, {"type": "LIMIT", "price": "3000"}):
        with pytest.raises(ValueError):
            venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(**over), CREDS)


def test_open_limits_are_ioc_by_default_and_journal_stop_not_sent():
    """Лимитка — IOC (или FOK), висящих ордеров бот не ставит; stop_loss ордера на открытие на биржу не уходит (стоп
    бота журнал ставит отдельным ордером после исполнения)."""
    o = venues.Order("bybit", "linear", "ETHUSDT", "buy", "limit", "0.01", price="3000", stop_loss="2800")
    _, _, p = venues.create_call(o, CID)
    assert p["timeInForce"] == "IOC" and "stopLoss" not in p and "tpslMode" not in p
    fok = venues.Order("bingx", "swap", "ETHUSDT", "buy", "limit", "0.01", price="3000", tif="FOK", stop_loss="2800")
    q = venues.create_call(fok, CID)[2]
    assert q["timeInForce"] == "FOK" and "stopLoss" not in q
    with pytest.raises(ValueError):
        venues.create_call(venues.Order("bybit", "linear", "ETHUSDT", "buy", "limit", "0.01", price="3000",
                                        tif="GTC"), CID)


@pytest.mark.parametrize("over", [
    {"symbol": "ETHUSDT"}, {"symbol": "DOGE-USDT"}, {"positionSide": "LONG"}, {"positionSide": "SHORT"},
    {"positionSide": "LONG", "reduceOnly": "true"}, {"reduceOnly": True}, {"type": "LIMIT"}, {"price": "3000"},
    {"quantity": "0.01&side=BUY"}, {"clientOrderId": "Manual1"},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":"2900","workingType":"MARK_PRICE"}'},
    {"stopLoss": '{"type":"STOP","stopPrice":2900,"price":2890,"workingType":"MARK_PRICE"}'},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":2900,"workingType":"MARK_PRICE","stopGuaranteed":true}'},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":-1,"workingType":"MARK_PRICE"}'}, {"stopLoss": "not json"},
    {"reduceOnly": "true", "stopLoss": '{"type":"STOP_MARKET","stopPrice":2900,"workingType":"MARK_PRICE"}'},
    {"type": "LIMIT", "price": "3000", "timeInForce": "PostOnly"},
])
def test_bingx_bad_values_refused(over):
    with pytest.raises(ValueError):
        venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(**over), CREDS)


def test_bingx_position_side_always_explicit():
    with pytest.raises(ValueError):   # без positionSide BingX подставил бы LONG
        venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order",
                       {k: v for k, v in swap().items() if k != "positionSide"}, CREDS)


@pytest.mark.parametrize("venue,method,path,params", [
    ("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "4",
                                                    "sellLeverage": "4"}),
    ("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2.5",
                                                    "sellLeverage": "2.5"}),
    ("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2",
                                                    "sellLeverage": "3"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/leverage", {"symbol": "BTC-USDT", "side": "BOTH", "leverage": "10"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/leverage", {"symbol": "BTC-USDT", "side": "BOTH", "leverage": "0"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/leverage", {"symbol": "BTC-USDT", "side": "LONG", "leverage": "2"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/marginType", {"symbol": "BTC-USDT", "marginType": "CROSSED"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/marginType", {"symbol": "BTC-USDT", "marginType": "SEPARATE_ISOLATED"}),
    ("bybit", "POST", "/v5/position/trading-stop", {"category": "linear", "symbol": "BTCUSDT", "tpslMode": "Full",
                                                    "positionIdx": 0, "stopLoss": "0"}),   # снять стоп нельзя
    ("bybit", "POST", "/v5/order/cancel", {"category": "linear", "symbol": "BTCUSDT", "orderId": "123"}),
    ("bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "BTC-USDT", "orderId": "123"}),
    ("bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "BTC-USDT", "clientOrderId": "someoneelse1"}),
    ("bybit", "GET", "/v5/position/list", {"category": "spot"}),
])
def test_leverage_caps_margin_stop_removal_and_foreign_orders_refused(venue, method, path, params):
    s = Session()
    with pytest.raises(ValueError):
        run(venues.call(s, venue, method, path, params, CREDS))
    assert s.calls == []


def test_isolated_margin_and_leverage_calls_allowed():
    venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/marginType", {"symbol": "BTC-USDT",
                                                                          "marginType": "ISOLATED"}, CREDS)
    venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/leverage", {"symbol": "BTC-USDT", "side": "BOTH",
                                                                        "leverage": "2"}, CREDS)
    venues.prepare("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "GRAMUSDT",
                                                                  "buyLeverage": "3", "sellLeverage": "3"}, CREDS)


def test_no_key_nothing_signed():
    for creds in (None, (), ("", ""), (KEY,), (KEY, None)):
        with pytest.raises(ValueError):
            venues.prepare("bybit", "POST", "/v5/order/create", linear(), creds)


# --- подпись по точным байтам ---

def test_bybit_post_exact_body_and_signature_vector():
    """Тело — компактный JSON в порядке полей; подпись — HMAC(secret, ts+key+recv+тело). Эталон посчитан отдельно
    (openssl dgst -sha256 -hmac), а не этим же кодом."""
    req = venues.prepare("bybit", "POST", "/v5/order/create", linear(), CREDS, timestamp=TS)
    expected_body = ('{"category":"linear","symbol":"BTCUSDT","side":"Sell","orderType":"Market","qty":"0.001",'
                     '"orderLinkId":"t260927013512a1b2c3d4","positionIdx":0}')
    assert req.body == expected_body and req.url == "https://api.bybit.com/v5/order/create"
    assert req.headers == {"X-BAPI-API-KEY": KEY,
                           "X-BAPI-SIGN": "f64e81ab62555d07099dcdecba860cb1cc773308c8700ac4eae6218b3c3c59b7",
                           "X-BAPI-SIGN-TYPE": "2", "X-BAPI-TIMESTAMP": TS, "X-BAPI-RECV-WINDOW": "5000",
                           "Content-Type": "application/json"}
    assert req.headers == accounts.bybit_post_headers(KEY, SECRET, expected_body, "5000", TS)


def test_bybit_get_exact_query_and_signature_vector():
    params = {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": CID}
    req = venues.prepare("bybit", "GET", "/v5/order/realtime", params, CREDS, timestamp=TS)
    assert req.url == f"https://api.bybit.com/v5/order/realtime?category=linear&symbol=BTCUSDT&orderLinkId={CID}"
    assert req.headers["X-BAPI-SIGN"] == "f39a9e39f3ac0639f519db6e470f1333f4f94421af536d8ef8a9c751d9f2dec4"
    assert req.body is None and "Content-Type" not in req.headers
    empty = venues.prepare("bybit", "GET", "/v5/user/query-api", {}, CREDS, timestamp=TS)
    assert empty.url == "https://api.bybit.com/v5/user/query-api"
    assert empty.headers["X-BAPI-SIGN"] == hmac.new(SECRET.encode(), (TS + KEY + "5000").encode(),
                                                    hashlib.sha256).hexdigest()


def test_bingx_query_sorted_signed_and_vector():
    req = venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(), CREDS, timestamp=TS)
    canonical = ("clientOrderId=t260927013512a1b2c3d4&positionSide=BOTH&quantity=0.01&recvWindow=5000&side=SELL"
                 "&symbol=ETH-USDT&timestamp=1700000000000&type=MARKET")
    assert req.url == ("https://open-api.bingx.com/openApi/swap/v2/trade/order?" + canonical
                       + "&signature=b2574bd4299becdfe03abe27bbc97bc4c4b4e9e287f44b83b379f5a12e03ea90")
    assert req.url.endswith(accounts.bingx_signed_query(SECRET, swap(), timestamp=TS))
    assert req.headers == {"X-BX-APIKEY": KEY} and req.body is None


def test_bingx_stop_order_query_sorted_and_signed_independently():
    """Стоп бота BingX (STOP_MARKET): query отсортирован, подпись — HMAC-SHA256 секрета по сырой строке (эталон
    считается здесь hmac, а не кодом ядра); JSON в query больше нет."""
    p = swap(side="BUY", type="STOP_MARKET", stopPrice="3500.5", workingType="MARK_PRICE", reduceOnly="true")
    req = venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", p, CREDS, timestamp=TS)
    canonical = ("clientOrderId=t260927013512a1b2c3d4&positionSide=BOTH&quantity=0.01&recvWindow=5000&reduceOnly=true"
                 "&side=BUY&stopPrice=3500.5&symbol=ETH-USDT&timestamp=1700000000000&type=STOP_MARKET"
                 "&workingType=MARK_PRICE")
    sig = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    assert req.url == f"https://open-api.bingx.com/openApi/swap/v2/trade/order?{canonical}&signature={sig}"
    assert "{" not in unquote(req.url)


def test_bingx_official_signature_example_through_accounts():
    """Официальный пример BingX (секрет из документации): ядро подписывает тем же accounts.bingx_signed_query."""
    secret = "UuGuyEGt6ZEkpUObCYCmIfh0elYsZVh80jlYwpJuRZEw70t6vomMH7Sjmf94ztSI"
    q = accounts.bingx_signed_query(secret, {"symbol": "ETHUSDT", "type": "MARKET", "side": "BUY", "quoteOrderQty": 20},
                                    timestamp=1649404670162, recv_window=None)
    assert q.endswith("&signature=428a3c383bde514baff0d10d3c20e5adfaacaf799e324546dafe5ccc480dd827")


def test_sent_bytes_equal_signed_bytes_no_redirects_no_secret_on_wire():
    s = Session({("POST", "/v5/order/create"): bybit_ok({"orderId": "1", "orderLinkId": CID}),
                 ("POST", "/openApi/swap/v2/trade/order"): bingx_ok({}),
                 ("DELETE", "/openApi/swap/v2/trade/order"): bingx_ok({}),
                 ("GET", "/v5/order/realtime"): bybit_ok({"list": []})})
    stop = {"type": "STOP_MARKET", "stopPrice": "3100", "workingType": "MARK_PRICE", "reduceOnly": "true",
            "side": "BUY"}
    run(venues.call(s, "bybit", "POST", "/v5/order/create", linear(), CREDS, timestamp=TS))
    run(venues.call(s, "bingx", "POST", "/openApi/swap/v2/trade/order", swap(**stop), CREDS, timestamp=TS))
    run(venues.call(s, "bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "ETH-USDT", "clientOrderId": CID},
                    CREDS, timestamp=TS))
    run(venues.call(s, "bybit", "GET", "/v5/order/realtime", {"category": "linear", "symbol": "BTCUSDT"}, CREDS))
    by, bx, dl, rt = s.calls
    assert by["data"] == venues.prepare("bybit", "POST", "/v5/order/create", linear(), CREDS, TS).body.encode()
    assert bx["url"] == venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(**stop), CREDS,
                                       TS).url and bx["data"] is None
    assert dl["method"] == "DELETE" and dl["query"]["clientOrderId"] == CID
    assert all(c["redirects"] is False for c in s.calls)
    for c in s.calls:
        wire = c["url"] + (c["data"] or b"").decode() + json.dumps(c["headers"])
        assert SECRET not in wire


@pytest.mark.parametrize("resp", [Resp(301, b""), Resp(302, {"retCode": 0, "result": {}}), Resp(307, b"")])
def test_redirect_is_not_followed_and_not_success(resp):
    s = Session({("POST", "/v5/order/create"): resp})
    status, j = run(venues.call(s, "bybit", "POST", "/v5/order/create", linear(), CREDS))
    assert len(s.calls) == 1 and venues.outcome("bybit", status, j)[0] == "ambiguous"


# --- разбор ответов ---

@pytest.mark.parametrize("venue,status,j,kind", [
    ("bybit", 200, {"retCode": 0, "result": {"orderId": "1"}}, "ok"),
    ("bybit", 200, {"retCode": 110072, "retMsg": "OrderLinkedID is duplicate"}, "duplicate"),
    ("bybit", 200, {"retCode": 170141, "retMsg": "Duplicate clientOrderId"}, "duplicate"),
    ("bybit", 200, {"retCode": 110001, "retMsg": "Order does not exist"}, "notfound"),
    ("bybit", 200, {"retCode": 110007, "retMsg": "Available balance is insufficient"}, "rejected"),
    ("bybit", 200, {"retCode": 10001, "retMsg": "params error"}, "rejected"),
    ("bybit", 200, {"retCode": 10016, "retMsg": "Server error"}, "ambiguous"),
    ("bybit", 200, {"retCode": 10006, "retMsg": "Too many visits"}, "ambiguous"),
    ("bybit", 200, {"retCode": 999999}, "ambiguous"), ("bybit", 200, {"retCode": "x"}, "ambiguous"),
    ("bybit", 200, {"result": {}}, "ambiguous"), ("bybit", 500, {"retCode": 0, "result": {}}, "ambiguous"),
    ("bybit", 403, {"retCode": 10001}, "ambiguous"), ("bybit", 429, None, "ambiguous"),
    ("bybit", 200, None, "ambiguous"), ("bybit", 200, [], "ambiguous"),
    ("bingx", 200, {"code": 0, "data": {"order": {}}}, "ok"),
    ("bingx", 200, {"code": 101481, "msg": "clientOrderID used"}, "duplicate"),
    ("bingx", 200, {"code": 109201}, "duplicate"), ("bingx", 200, {"code": 109421}, "notfound"),
    ("bingx", 200, {"code": 101204, "msg": "Insufficient margin"}, "rejected"),
    ("bingx", 200, {"code": 101400}, "rejected"), ("bingx", 200, {"code": 109418}, "rejected"),
    ("bingx", 200, {"code": 100500}, "ambiguous"), ("bingx", 200, {"code": 109429}, "ambiguous"),
    ("bingx", 200, {"code": 110500}, "ambiguous"), ("bingx", 200, {"code": 100410}, "ambiguous"),
    ("bingx", 502, {"code": 0}, "ambiguous"), ("bingx", 200, {"msg": "x"}, "ambiguous"),
])
def test_outcome_table(venue, status, j, kind):
    assert venues.outcome(venue, status, j)[0] == kind


def test_outcome_text_scrubs_key_and_secret():
    kind, _, code, msg = venues.outcome("bybit", 200, {"retCode": 10003, "retMsg": f"API key {KEY} invalid"}, CREDS)
    assert kind == "rejected" and code == 10003 and KEY not in msg and "•••" in msg


def test_create_call_builders():
    o = venues.Order("bybit", "linear", "BTCUSDT", "sell", "market", "0.001", stop_loss="70000")
    m, p, params = venues.create_call(o, CID)
    assert (m, p) == ("POST", "/v5/order/create") and params == {
        "category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.001",
        "orderLinkId": CID, "positionIdx": 0}                         # стоп — отдельным ордером бота после исполнения
    _, _, sp = venues.create_call(venues.Order("bybit", "spot", "ETHUSDT", "buy", "market", "0.1"), CID)
    assert sp["marketUnit"] == "baseCoin" and "positionIdx" not in sp
    _, _, lim = venues.create_call(venues.Order("bybit", "linear", "ETHUSDT", "buy", "limit", "0.01", price="3000.10",
                                                reduce_only=True), CID)
    assert lim["price"] == "3000.1" and lim["timeInForce"] == "IOC" and lim["reduceOnly"] is True
    m, p, bx = venues.create_call(venues.Order("bingx", "swap", "ETHUSDT", "buy", "market", "0.01", stop_loss="2900"),
                                  CID)
    assert (m, p) == ("POST", "/openApi/swap/v2/trade/order") and bx["positionSide"] == "BOTH"
    assert bx["symbol"] == "ETH-USDT" and "reduceOnly" not in bx and "stopLoss" not in bx
    assert json.dumps(bx)
    # символ позиции бота из журнала (закрытие, стоп): только кандидат этого канонического символа, свежесть не нужна
    ton = venues.Order("bingx", "swap", "TONUSDT", "buy", "market", "10", reduce_only=True)
    assert venues.create_call(ton, CID, venue_sym="GRAMTON-USDT")[2]["symbol"] == "GRAMTON-USDT"
    for wrong in ("GRAM-USDT", "BTC-USDT"):
        with pytest.raises(ValueError):
            venues.create_call(ton, CID, venue_sym=wrong)
    _, _, close = venues.create_call(venues.Order("bingx", "swap", "ETHUSDT", "buy", "market", "0.01",
                                                  reduce_only=True), CID)
    assert close["reduceOnly"] == "true" and close["positionSide"] == "BOTH"
    assert venues.Order("bybit", "spot", "ETHUSDT", "sell", "market", "1").reducing
    assert not venues.Order("bybit", "spot", "ETHUSDT", "buy", "market", "1").reducing
    assert venues.Order("bybit", "linear", "ETHUSDT", "buy", "market", "1", reduce_only=True).reducing


@pytest.mark.parametrize("order", [
    lambda: venues.Order("bybit", "spot", "BTCUSDT", "sell", "market", "0.1", reduce_only=True),
    lambda: venues.Order("bybit", "spot", "BTCUSDT", "buy", "market", "0.1", stop_loss="1"),
    lambda: venues.Order("bybit", "linear", "BTCUSDT", "buy", "limit", "0.1"),              # лимитка без цены
    lambda: venues.Order("bybit", "linear", "DOGEUSDT", "buy", "market", "1"),
    lambda: venues.Order("bybit", "linear", "BTCUSDT", "buy", "market", 0.1),                # float
    lambda: venues.Order("bingx", "linear", "BTCUSDT", "buy", "market", "0.1"),
    lambda: venues.Order("okx", "swap", "BTCUSDT", "sell", "market", "0.1"),
    lambda: venues.Order("bybit", "linear", "TONUSDT", "sell", "market", "10"),              # TON не проверен
    lambda: venues.Order("bingx", "swap", "TONUSDT", "sell", "market", "10"),
])
def test_create_call_refuses_bad_orders(order):
    with pytest.raises(ValueError):
        venues.create_call(order(), CID)


# --- TON → GRAM: проверка по справочнику ---

def bybit_instrument(symbol, status="Trading", contract="LinearPerpetual", turnover="25000000"):
    return {("GET", "/v5/market/instruments-info"): lambda c: bybit_ok({"category": c["query"]["category"], "list": [
                {"symbol": symbol, "status": status, "contractType": contract, "baseCoin": symbol[:-4],
                 "quoteCoin": "USDT", "settleCoin": "USDT"}] if c["query"]["symbol"] == symbol else []}),
            ("GET", "/v5/market/tickers"): lambda c: bybit_ok({"list": [{"symbol": c["query"]["symbol"],
                                                                         "turnover24h": turnover}]})}


def bingx_contract(symbol, status=1, api_open="true", volume="9000000"):
    return {("GET", "/openApi/swap/v2/quote/contracts"): lambda c: bingx_ok([
                {"symbol": symbol, "currency": "USDT", "asset": symbol.split("-")[0], "status": status,
                 "apiStateOpen": api_open, "displayName": "GRAM-USDT"}] if c["query"]["symbol"] == symbol else []),
            ("GET", "/openApi/swap/v2/quote/ticker"): bingx_ok({"symbol": symbol, "quoteVolume": volume})}


def test_resolve_bybit_gram_maps_ton_then_orders_use_gramusdt():
    s = Session(bybit_instrument("GRAMUSDT"))
    mapping, why = run(venues.resolve_symbols(s, "bybit", "linear"))
    assert mapping["TONUSDT"] == "GRAMUSDT" and "TONUSDT" not in why
    assert venues.venue_symbol("bybit", "linear", "BTCUSDT") == "BTCUSDT"   # BTC/ETH — без проверки справочника
    assert all(c["headers"] == {} for c in s.calls)                      # публичный справочник — без ключа
    _, _, p = venues.create_call(venues.Order("bybit", "linear", "TONUSDT", "sell", "market", "10"), CID)
    assert p["symbol"] == "GRAMUSDT"
    with pytest.raises(ValueError):                                         # спот не проверен — не торгуется
        venues.create_call(venues.Order("bybit", "spot", "TONUSDT", "buy", "market", "10"), CID)
    with pytest.raises(ValueError):                                         # проверка устарела
        venues.venue_symbol("bybit", "linear", "TONUSDT", now=time.time() + venues.RESOLVE_TTL + 1)


@pytest.mark.parametrize("routes,reason", [
    (bybit_instrument("GRAMUSDT", status="Closed"), "статус"),
    (bybit_instrument("GRAMUSDT", contract="LinearFutures"), "статус"),
    (bybit_instrument("GRAMUSDT", turnover="12000"), "оборот"),
    (bybit_instrument("OTHERUSDT"), "нет в справочнике"),
    ({("GET", "/v5/market/instruments-info"): Resp(500, b"")}, "ошибка проверки"),
    ({("GET", "/v5/market/instruments-info"): bybit_err(10001, "Not supported symbols")}, "ошибка проверки"),
])
def test_resolve_bybit_ton_not_tradable(routes, reason):
    s = Session(routes)
    mapping, why = run(venues.resolve_symbols(s, "bybit", "linear"))
    assert mapping["TONUSDT"] is None and reason in why["TONUSDT"]
    with pytest.raises(ValueError, match="не торгуется"):
        venues.create_call(venues.Order("bybit", "linear", "TONUSDT", "sell", "market", "10"), CID)


def test_resolve_bingx_gramton_and_refusals():
    s = Session(bingx_contract("GRAMTON-USDT"))
    assert run(venues.resolve_symbols(s, "bingx", "swap"))[0]["TONUSDT"] == "GRAMTON-USDT"
    assert venues.create_call(venues.Order("bingx", "swap", "TONUSDT", "sell", "market", "10"), CID)[2]["symbol"] \
        == "GRAMTON-USDT"
    for routes in (bingx_contract("GRAMTON-USDT", status=0), bingx_contract("GRAMTON-USDT", api_open="false"),
                   bingx_contract("GRAMTON-USDT", volume="10"), bingx_contract("GRAM-USDT"),
                   {("GET", "/openApi/swap/v2/quote/contracts"): Resp(exc=TimeoutError())}):
        s = Session(routes)
        assert run(venues.resolve_symbols(s, "bingx", "swap"))[0]["TONUSDT"] is None
        assert not any(c["query"].get("symbol") == "GRAM-USDT" for c in s.calls)   # ловушку даже не спрашиваем


# --- ордер/позиция: вид ядра ---

def test_order_and_position_views():
    v = venues.order_view("bybit", {"orderId": "9", "orderLinkId": CID, "symbol": "GRAMUSDT", "side": "Buy",
                                    "orderType": "Market", "qty": "10", "price": "0", "cumExecQty": "10",
                                    "avgPrice": "3.1", "orderStatus": "Filled", "reduceOnly": False,
                                    "cumFeeDetail": {"GRAM": "0.01"}, "cumExecFee": "0.03"})
    assert v["state"] == "filled" and v["filled"] == Decimal("10") and v["side"] == "buy" and v["type"] == "market"
    assert v["symbol"] == "TONUSDT" and v["fee"] == Decimal("0.01")
    assert venues.order_view("bybit", {"orderLinkId": CID, "symbol": "BTCUSDT", "cumExecFee": "0.2"})["fee"] \
        == Decimal("0.2")
    assert venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": "Rejected", "cumExecQty": "0"})["state"] \
        == "rejected"
    assert venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": "Weird"})["state"] is None
    cond = venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": "Triggered", "orderType": "Market",
                                       "triggerPrice": "60000", "updatedTime": "1700000000000"})
    assert cond["state"] == "open" and cond["type"] == "stop" and cond["trigger"] == Decimal("60000")
    assert cond["ts"] == 1700000000
    plain = venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": "New", "orderType": "Market",
                                        "triggerPrice": "0"})
    assert plain["type"] == "market" and plain["trigger"] is None and plain["ts"] is None
    bx_stop = venues.order_view("bingx", {"clientOrderId": CID, "status": "NEW", "type": "STOP_MARKET",
                                          "stopPrice": "3500", "commission": "-0.0123"})
    assert bx_stop["type"] == "stop" and bx_stop["trigger"] == Decimal("3500") and bx_stop["fee"] == Decimal("0.0123")
    b = venues.order_view("bingx", {"order": {"symbol": "ETH-USDT", "orderId": 1735950529123455000123, "side": "SELL",
                                              "type": "MARKET", "origQty": "0.01", "status": "FILLED",
                                              "clientOrderID": CID.upper(), "executedQty": "0.01",
                                              "reduceOnly": "true"}})
    assert b["client_id"] == CID and b["order_id"] == "1735950529123455000123" and b["symbol"] == "ETHUSDT"
    assert b["state"] == "filled" and b["qty"] == Decimal("0.01") and b["reduce_only"] is True
    assert venues.order_view("bingx", {"status": "PARTIALLYFILLED", "clientOrderId": CID})["state"] == "open"
    assert venues.order_view("bingx", {"symbol": "GRAM-USDT", "clientOrderId": CID})["symbol"] is None   # ловушка
    p = venues.position_view("bybit", {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01", "avgPrice": "65000",
                                       "markPrice": "64000", "liqPrice": "", "leverage": "2", "positionIdx": 0})
    assert p["side"] == "short" and p["liq"] is None and p["isolated"] is None
    assert venues.position_view("bybit", {"symbol": "BTCUSDT", "side": "", "size": "0"}) is None
    assert venues.position_view("bybit", {"symbol": "DOGEUSDT", "side": "Buy", "size": "1"}) is None
    q = venues.position_view("bingx", {"symbol": "GRAMTON-USDT", "positionAmt": "20", "positionSide": "SHORT",
                                       "isolated": True, "avgPrice": "3", "liquidationPrice": Decimal("4.5"),
                                       "leverage": 2})
    assert q["symbol"] == "TONUSDT" and q["side"] == "short" and q["size"] == Decimal("20") and q["isolated"]
    assert venues.position_view("bingx", {"symbol": "GRAM-USDT", "positionAmt": "20"}) is None


def test_find_order_bybit_looks_open_then_closed_then_history():
    order = {"orderLinkId": CID, "symbol": "BTCUSDT", "orderStatus": "Filled", "cumExecQty": "1"}
    s = Session({("GET", "/v5/order/realtime"): lambda c: bybit_ok({"list": [order] if c["query"].get("openOnly")
                                                                    == "1" else []}),
                 ("GET", "/v5/order/history"): bybit_ok({"list": []})})
    kind, view, _ = run(venues.find_order(s, "bybit", "linear", "BTCUSDT", CID, CREDS))
    assert kind == "found" and view["state"] == "filled" and [c["query"].get("openOnly") for c in s.calls] == [None, "1"]
    s = Session({("GET", "/v5/order/realtime"): bybit_ok({"list": []}),
                 ("GET", "/v5/order/history"): bybit_ok({"list": []})})
    assert run(venues.find_order(s, "bybit", "linear", "BTCUSDT", CID, CREDS))[0] == "notfound" and len(s.calls) == 3
    s = Session({("GET", "/v5/order/realtime"): [bybit_ok({"list": []}), bybit_err(10016)]})
    assert run(venues.find_order(s, "bybit", "linear", "BTCUSDT", CID, CREDS))[0] == "ambiguous"
    s = Session({("GET", "/v5/order/realtime"): bybit_ok({"list": [dict(order, orderLinkId="t" + "0" * 20)]}),
                 ("GET", "/v5/order/history"): bybit_ok({"list": []})})
    assert run(venues.find_order(s, "bybit", "linear", "BTCUSDT", CID, CREDS))[0] == "notfound"   # чужой id
    s = Session({("GET", "/openApi/swap/v2/trade/order"): bingx_err(109421, "order not exist")})
    assert run(venues.find_order(s, "bingx", "swap", "ETH-USDT", CID, CREDS))[0] == "notfound"


@pytest.mark.parametrize("venue,answer,mode", [
    ("bybit", bybit_ok({"marginMode": "ISOLATED_MARGIN"}), "isolated"),
    ("bybit", bybit_ok({"marginMode": "REGULAR_MARGIN"}), "cross"),
    ("bybit", bybit_ok({"marginMode": "PORTFOLIO_MARGIN"}), "portfolio"),
    ("bybit", bybit_ok({"marginMode": "SOMETHING"}), None), ("bybit", bybit_ok({}), None),
    ("bybit", Resp(exc=TimeoutError()), None), ("bybit", bybit_err(10005), None),
    ("bingx", bingx_ok({"marginType": "ISOLATED"}), "isolated"), ("bingx", bingx_ok({"marginType": "CROSSED"}), "cross"),
    ("bingx", bingx_ok({"marginType": "SEPARATE_ISOLATED"}), None), ("bingx", Resp(502, b""), None),
])
def test_margin_mode_read_from_exchange(venue, answer, mode):
    path = "/v5/account/info" if venue == "bybit" else "/openApi/swap/v2/trade/marginType"
    s = Session({("GET", path): answer})
    got, why = run(venues.margin_mode(s, venue, "BTCUSDT" if venue == "bybit" else "BTC-USDT", CREDS))
    assert got == mode and (why == "") == (mode is not None)
    assert all(c["method"] == "GET" for c in s.calls)


# --- кто вообще может слать запросы ---

TRADING_MODULES = (venues, journal, risk, switch, gates, keys, ownership)


def test_only_venues_send_requests():
    """POST/PUT/DELETE/PATCH и сетевые клиенты — только в venues._send; остальные модули ядра и скрипт ключей
    ходят в сеть только через venues.call / venues.public_get."""
    assert _senders(venues) == {"_send"}
    for mod in TRADING_MODULES[1:]:
        assert _senders(mod) == set(), mod.__name__
    script = type("M", (), {"__file__": os.path.join(ROOT, "scripts", "trading_keys.py")})
    assert _senders(script) == set()


def test_no_urls_outside_accounts_bases():
    assert venues.BASES == {"bybit": "https://api.bybit.com", "bingx": "https://open-api.bingx.com"}
    for mod in TRADING_MODULES:
        tree = ast.parse(inspect.getsource(mod))
        urls = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and "://" in n.value}
        assert urls == set(), mod.__name__   # хосты — только accounts.BYBIT_BASE / BINGX_BASE


def test_resolve_needs_exactly_one_good_candidate_and_no_errors(monkeypatch):
    """Кандидатов больше одного: торгуется, только если «да» ровно у одного и проверка ни одного не сорвалась —
    иначе сбой проверки второго кандидата мог бы скрыть, что их два (не угадываем)."""
    monkeypatch.setitem(venues.CANDIDATES, ("bybit", "linear"), {"TONUSDT": ("GRAMUSDT", "TONUSDT")})
    s = Session(bybit_instrument("GRAMUSDT"))                  # TONUSDT не из allowlist → ошибка проверки
    mapping, why = run(venues.resolve_symbols(s, "bybit", "linear"))
    assert mapping == {"TONUSDT": None} and "ошибка проверки" in why["TONUSDT"]
    monkeypatch.setitem(venues.CANDIDATES, ("bybit", "linear"), {"TONUSDT": ("BTCUSDT", "ETHUSDT")})
    both = {("GET", "/v5/market/instruments-info"): lambda c: bybit_ok({"list": [
                {"symbol": c["query"]["symbol"], "status": "Trading", "contractType": "LinearPerpetual",
                 "quoteCoin": "USDT", "settleCoin": "USDT"}]}),
            ("GET", "/v5/market/tickers"): lambda c: bybit_ok({"list": [{"symbol": c["query"]["symbol"],
                                                                         "turnover24h": "9e9"}]})}
    mapping, why = run(venues.resolve_symbols(Session(both), "bybit", "linear"))
    assert mapping == {"TONUSDT": None} and "несколько" in why["TONUSDT"]


# --- таймаут у каждого запроса, общий цикл тестов ---

def test_every_request_has_its_own_client_timeout():
    s = Session({("GET", "/v5/order/realtime"): bybit_ok({"list": []}),
                 ("GET", "/openApi/swap/v2/quote/ticker"): bingx_ok({"symbol": "ETH-USDT", "lastPrice": "3000"})})
    run(venues.call(s, "bybit", "GET", "/v5/order/realtime", {"category": "linear", "symbol": "BTCUSDT"}, CREDS))
    run(venues.public_get(s, "bingx", "/openApi/swap/v2/quote/ticker", {"symbol": "ETH-USDT"}))
    assert [c["timeout"].total for c in s.calls] == [venues.REQUEST_TIMEOUT] * 2 and venues.REQUEST_TIMEOUT == 10


def test_tests_share_one_event_loop():
    """Заглушки гоняют корутины на одном цикле (не asyncio.run на каждый тест — WinError 10055 на ПК владельца)."""
    async def which():
        return asyncio.get_running_loop()
    first = run(which())
    assert run(which()) is first is loop() and not first.is_closed()


# --- разбор ответов: мутации кода, конверта, таблиц статусов ---

@pytest.mark.parametrize("venue,j,kind,data", [
    ("bybit", {"retCode": 0, "result": {"a": 1}, "data": {"b": 2}}, "ok", {"a": 1}),        # конверт Bybit — result
    ("bingx", {"code": 0, "result": {"a": 1}, "data": {"b": 2}}, "ok", {"b": 2}),           # конверт BingX — data
    ("bybit", {"code": 0, "result": {"a": 1}}, "ambiguous", None),                           # код BingX у Bybit
    ("bingx", {"retCode": 0, "data": {"b": 2}}, "ambiguous", None),                          # код Bybit у BingX
    ("bybit", {"retCode": "0", "result": {}}, "ok", {}), ("bybit", {"retCode": "0.0"}, "ambiguous", None),
    ("bybit", {"retCode": None, "result": {}}, "ambiguous", None),
    ("bingx", {"code": 1, "data": {}}, "ambiguous", None), ("bybit", {"retCode": -1}, "ambiguous", None),
])
def test_outcome_code_and_envelope(venue, j, kind, data):
    got = venues.outcome(venue, 200, j)
    assert got[0] == kind and got[1] == data


def test_outcome_code_sets_are_disjoint_and_messages_by_venue():
    for dup, nf, rej in ((venues.BYBIT_DUPLICATE, venues.BYBIT_NOT_FOUND, venues.BYBIT_REJECT),
                         (venues.BINGX_DUPLICATE, venues.BINGX_NOT_FOUND, venues.BINGX_REJECT)):
        assert not (dup & nf) and not (dup & rej) and not (nf & rej) and 0 not in dup | nf | rej
    assert venues.outcome("bybit", 200, {"retCode": 10001, "retMsg": "a", "msg": "b"})[3] == "код 10001: a"
    assert venues.outcome("bingx", 200, {"code": 100001, "retMsg": "a", "msg": "b"})[3] == "код 100001: b"
    assert venues.outcome("bybit", 200, {"retCode": 10001})[3] == "код 10001"


def test_status_tables_exact():
    assert venues._BYBIT_STATES == {"New": "open", "PartiallyFilled": "open", "Untriggered": "open",
                                    "Triggered": "open", "Active": "open", "Filled": "filled", "Cancelled": "closed",
                                    "PartiallyFilledCanceled": "closed", "Deactivated": "closed", "Rejected": "closed"}
    assert venues._BINGX_STATES == {"NEW": "open", "PARTIALLY_FILLED": "open", "PARTIALLYFILLED": "open",
                                    "PENDING": "open", "TRIGGERED": "open", "FILLED": "filled", "CANCELED": "closed",
                                    "CANCELLED": "closed", "EXPIRED": "closed", "FAILED": "closed"}
    for status, state in venues._BYBIT_STATES.items():
        want = "rejected" if status == "Rejected" else state
        assert venues.order_view("bybit", {"orderStatus": status, "cumExecQty": "0"})["state"] == want
    assert venues.order_view("bybit", {"orderStatus": "Rejected", "cumExecQty": "0.5"})["state"] == "closed"
    assert venues.order_view("bingx", {"status": "failed", "executedQty": "0"})["state"] == "rejected"   # регистр
    assert venues.order_view("bybit", {"orderStatus": "filled"})["state"] is None           # у Bybit регистр важен
    assert venues.order_view("bingx", {"status": "CANCELED", "executedQty": "0.1"})["state"] == "closed"


def test_order_view_envelopes_and_fields():
    flat = {"symbol": "ETH-USDT", "orderId": 7, "side": "SELL", "type": "MARKET", "origQty": "0.01", "status": "NEW",
            "clientOrderId": CID}
    assert venues.order_view("bingx", {"order": flat}) == venues.order_view("bingx", flat)
    by = venues.order_view("bybit", {"order": {"orderLinkId": CID}})                       # Bybit не разворачивает
    assert by["client_id"] == "" and by["state"] is None
    v = venues.order_view("bingx", flat)
    assert (v["raw_symbol"], v["symbol"], v["qty"], v["order_id"]) == ("ETH-USDT", "ETHUSDT", Decimal("0.01"), "7")
    assert venues.order_view("bingx", dict(flat, symbol="GRAM-USDT"))["symbol"] is None     # другой токен
    assert venues.order_view("bybit", {"orderLinkId": CID, "qty": "1", "origQty": "2"})["qty"] == Decimal("1")
    assert venues.order_view("bingx", {"clientOrderID": CID.upper(), "clientOrderId": ""})["client_id"] == CID
    assert venues.order_view("bybit", {"reduceOnly": "true"})["reduce_only"] is True
    assert venues.order_view("bybit", {"reduceOnly": "yes"})["reduce_only"] is None
    assert venues.order_view("bybit", "x") is None and venues.order_view("bingx", None) is None
    junk = venues.order_view("bingx", {"order": "x"})                    # не ордер: ни id, ни статуса — несовпадение
    assert junk["client_id"] == "" and junk["state"] is None and junk["raw_symbol"] == ""


# --- чтение по символу: строго ---

def test_open_orders_listing_strict():
    book = Book()
    book.foreign.append({"orderId": "9", "orderLinkId": "", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market",
                         "qty": "0", "stopOrderType": "StopLoss", "triggerPrice": "60000", "closeOnTrigger": True})
    s = Session(book.routes("bybit"))
    rows, why = run(venues.open_orders(s, "bybit", "linear", "BTCUSDT", CREDS))
    assert why == "" and rows == [{"client_id": "", "order_id": "9", "raw_symbol": "BTCUSDT", "side": "buy",
                                   "type": "market", "qty": Decimal("0"), "price": None,
                                   "trigger": Decimal("60000"), "stop_type": "StopLoss", "reduce_only": True}]
    assert s.calls[0]["query"] == {"category": "linear", "symbol": "BTCUSDT", "limit": "50"}
    assert s.calls[1]["query"] == {"category": "linear", "symbol": "BTCUSDT", "limit": "50", "orderFilter": "StopOrder"}
    assert len(rows) == 1                                               # один и тот же стоп в обоих ответах — один раз
    only_stops = Session({("GET", "/v5/order/realtime"): lambda c: bybit_ok({"list": [
        {"orderId": "9", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "stopOrderType": "StopLoss",
         "triggerPrice": "60000"}] if c["query"].get("orderFilter") == "StopOrder" else []})})
    rows, _ = run(venues.open_orders(only_stops, "bybit", "linear", "BTCUSDT", CREDS))
    assert [r["stop_type"] for r in rows] == ["StopLoss"]              # стоп виден, даже если «все виды» его не дали
    for answer in (bybit_ok({"list": [{"symbol": "ETHUSDT"}]}), bybit_ok({"list": [{"symbol": "BTCUSDT"}] * 50}),
                   bybit_ok({"nolist": []}), bybit_ok({"list": ["x"]}), Resp(500, b""), Resp(exc=TimeoutError())):
        s = Session({("GET", "/v5/order/realtime"): answer})
        assert run(venues.open_orders(s, "bybit", "linear", "BTCUSDT", CREDS))[0] is None
    bx = Session({("GET", "/openApi/swap/v2/trade/openOrders"): bingx_ok({"orders": [
        {"symbol": "ETH-USDT", "orderId": 1, "type": "STOP_MARKET", "side": "BUY", "stopPrice": "3500",
         "clientOrderId": "", "reduceOnly": "true"}]})})
    rows, _ = run(venues.open_orders(bx, "bingx", "swap", "ETH-USDT", CREDS))
    assert rows[0]["stop_type"] == "STOP_MARKET" and rows[0]["trigger"] == Decimal("3500") and rows[0]["reduce_only"]
    for data in ({"orders": None}, [], {"orders": [{"symbol": "GRAM-USDT"}]}):
        bx = Session({("GET", "/openApi/swap/v2/trade/openOrders"): bingx_ok(data)})
        assert run(venues.open_orders(bx, "bingx", "swap", "ETH-USDT", CREDS))[0] is None


@pytest.mark.parametrize("rows,net,lev,ok", [
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "", "size": "0", "leverage": "2"}], 0, 2, True),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "None", "size": "0", "leverage": "3"}], 0, 3, True),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Sell", "size": "0.01", "leverage": "2"}], "-0.01", 2, True),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Buy", "size": "0.01", "leverage": ""}], "0.01", None, True),
    ([{"symbol": "BTCUSDT", "positionIdx": 1, "side": "Buy", "size": "0", "leverage": "2"},
      {"symbol": "BTCUSDT", "positionIdx": 2, "side": "Sell", "size": "0", "leverage": "2"}], 0, None, True),
    ([{"symbol": "BTCUSDT", "positionIdx": 1, "side": "Buy", "size": "0.1", "leverage": "2"}], None, None, False),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Buy", "size": "-1"}], None, None, False),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Up", "size": "1"}], None, None, False),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Buy", "size": ""}], None, None, False),
    ([{"symbol": "ETHUSDT", "positionIdx": 0, "side": "", "size": "0"}], None, None, False),
    ([{"symbol": "BTCUSDT", "positionIdx": 0, "side": "Buy", "size": "1"},
      {"symbol": "BTCUSDT", "positionIdx": 0, "side": "Sell", "size": "1"}], None, None, False),
    ("x", None, None, False),
])
def test_symbol_positions_bybit_strict(rows, net, lev, ok):
    s = Session({("GET", "/v5/position/list"): bybit_ok({"list": rows} if rows != "x" else "x")})
    got, why = run(venues.symbol_positions(s, "bybit", "BTCUSDT", CREDS))
    assert (got is not None) is ok, why
    if ok:
        assert got["net"] == Decimal(net) and got["leverage"] == (None if lev is None else Decimal(lev))
    assert s.calls[0]["query"] == {"category": "linear", "symbol": "BTCUSDT"}


def test_symbol_positions_bingx_sides_and_leverage():
    def pos(amt, side="BOTH"):
        s = Session({("GET", "/openApi/swap/v2/user/positions"): bingx_ok([{"symbol": "ETH-USDT", "positionAmt": amt,
                                                                            "positionSide": side}])})
        return run(venues.symbol_positions(s, "bingx", "ETH-USDT", CREDS))
    assert pos("-0.5")[0]["net"] == Decimal("-0.5") and pos("0.5", "SHORT")[0]["net"] == Decimal("-0.5")
    assert pos("0.5", "LONG")[0]["net"] == Decimal("0.5") and pos("0")[0]["net"] == 0
    assert pos("0.5", "WEIRD")[0] is None and pos("x")[0] is None
    for data, want in (({"longLeverage": 2, "shortLeverage": 3}, Decimal(3)), ({"longLeverage": 2}, None),
                       ({"longLeverage": 0, "shortLeverage": 2}, None), ("x", None)):
        s = Session({("GET", "/openApi/swap/v2/trade/leverage"): bingx_ok(data)})
        assert run(venues.symbol_leverage(s, "ETH-USDT", CREDS))[0] == want


def test_account_positions_include_other_coins():
    book = Book()
    book.position["BTCUSDT"] = Decimal("-0.01")
    book.other.append(("DOGEUSDT", Decimal("100")))
    s = Session(book.routes("bybit"))
    rows, why = run(venues.account_positions(s, "bybit", CREDS))
    assert why == "" and rows == [
        {"raw_symbol": "BTCUSDT", "symbol": "BTCUSDT", "signed": Decimal("-0.01"), "isolated": None},
        {"raw_symbol": "DOGEUSDT", "symbol": None, "signed": Decimal("100"), "isolated": None}]
    assert s.calls[0]["query"] == {"category": "linear", "settleCoin": "USDT", "limit": "200"}
    bad = Session({("GET", "/v5/position/list"): bybit_ok({"list": [{"symbol": "X", "side": "Buy", "size": "?"}]})})
    assert run(venues.account_positions(bad, "bybit", CREDS))[0] is None


def test_cross_account_check_sees_usdc_inverse_option_and_borrows():
    """Bybit UTA под кросс-маржой: общий залог и у USDC-перпов, инверсных, опционов и займов спот-маржи — всё это
    читается и считается чужим (symbol None); не прочитали хоть что-то — None (кросс запрещён)."""
    book = Book()
    book.lists[("linear", "USDC")] = [{"symbol": "BTCPERP", "positionIdx": 0, "side": "Sell", "size": "5"}]
    book.lists[("inverse", None)] = [{"symbol": "BTCUSD", "positionIdx": 0, "side": "Buy", "size": "100"}]
    book.lists[("option", None)] = [{"symbol": "BTC-27DEC26-80000-C", "positionIdx": 0, "side": "Buy", "size": "1"}]
    book.borrows = [{"coin": "USDT", "borrowAmount": "250"}, {"coin": "BTC", "borrowAmount": ""}]
    s = Session(book.routes("bybit"))
    rows, why = run(venues.account_positions(s, "bybit", CREDS))
    assert why == "" and [(r["raw_symbol"], r["symbol"], r["signed"]) for r in rows] == [
        ("BTCPERP (linear USDC)", None, Decimal("-5")), ("BTCUSD (inverse)", None, Decimal("100")),
        ("BTC-27DEC26-80000-C (option)", None, Decimal("1")), ("заём USDT", None, Decimal("250"))]
    assert [c["query"] for c in s.sent("GET", "/v5/position/list")] == [
        {"category": "linear", "settleCoin": "USDT", "limit": "200"},
        {"category": "linear", "settleCoin": "USDC", "limit": "200"},
        {"category": "inverse", "limit": "200"}, {"category": "option", "limit": "200"}]
    for route in (("GET", "/v5/account/wallet-balance"), ("GET", "/v5/position/list")):
        broken = Session(book.routes("bybit") | {route: lambda c: (Resp(500, b"") if "USDC" in str(c["query"]) or
                                                                   "wallet" in c["path"] else book.routes("bybit")[
                                                                       ("GET", c["path"])](c))})
        assert run(venues.account_positions(broken, "bybit", CREDS))[0] is None
    for params in ({"category": "inverse", "symbol": "BTCUSDT"}, {"category": "option", "settleCoin": "USDT"},
                   {"category": "linear"}, {"category": "spot", "settleCoin": "USDT"}):
        with pytest.raises(ValueError):                                  # только чтение, без лишних сочетаний
            venues.prepare("bybit", "GET", "/v5/position/list", params, CREDS)


def test_executions_windows_strict_and_views():
    s = Session({("GET", "/v5/execution/list"): lambda c: bybit_ok({"list": [
        {"symbol": "BTCUSDT", "orderLinkId": CID, "orderId": "1", "side": "Sell", "execQty": "0.001",
         "execPrice": "65000", "execTime": c["query"]["startTime"], "execType": "Trade", "execId": "e1",
         "execFee": "0.03"}]})})
    week = venues.EXEC_WINDOW_MS
    rows, why = run(venues.executions(s, "bybit", "linear", "BTCUSDT", 1_700_000_000_000,
                                      1_700_000_000_000 + 2 * week, CREDS))
    assert why == "" and len(rows) == 3 and len(s.calls) == 3                # окна по 7 дней
    assert rows[0] == {"client_id": CID, "order_id": "1", "exec_id": "e1", "side": "sell", "qty": Decimal("0.001"),
                       "price": Decimal("65000"), "ts": 1_700_000_000_000, "kind": "Trade", "fee": Decimal("0.03")}
    too_long = run(venues.executions(s, "bybit", "linear", "BTCUSDT", 0, (venues.EXEC_MAX_WINDOWS + 1) * week, CREDS))
    assert too_long[0] is None
    for answer in (bybit_ok({"list": [{"symbol": "BTCUSDT", "execQty": "1", "execTime": "1", "execType": "Trade"}]
                            * 100}), bybit_ok({"list": [{"symbol": "ETHUSDT"}]}),
                   bybit_ok({"list": [{"symbol": "BTCUSDT", "execQty": "x", "execTime": "1", "execType": "Trade"}]}),
                   Resp(500, b"")):
        s = Session({("GET", "/v5/execution/list"): answer})
        assert run(venues.executions(s, "bybit", "linear", "BTCUSDT", T0, T0 + 1, CREDS))[0] is None
    bx = Session({("GET", "/openApi/swap/v2/trade/allOrders"): bingx_ok({"orders": [
        {"symbol": "ETH-USDT", "orderId": 5, "clientOrderId": "", "side": "BUY", "type": "MARKET", "executedQty": "0.5",
         "updateTime": 1700000000000, "avgPrice": "3000"},
        {"symbol": "ETH-USDT", "orderId": 6, "clientOrderId": CID.upper(), "side": "SELL", "type": "LIMIT",
         "executedQty": "0", "updateTime": 1700000000000}]})})
    rows, why = run(venues.executions(bx, "bingx", "swap", "ETH-USDT", T0, T0 + 1, CREDS))
    assert why == "" and [(r["client_id"], r["qty"]) for r in rows] == [("", Decimal("0.5"))]   # неисполненный — мимо
    assert bx.calls[0]["query"]["startTime"] == str(T0) and bx.calls[0]["query"]["endTime"] == str(T0 + 1)


def test_capital_position_mode_funding_and_closed_pnl_readers():
    book = Book()
    s = Session(book.routes("bybit") | book.routes("bingx"))
    assert run(venues.capital(s, "bybit", CREDS)) == (Decimal("10000"), "")
    assert run(venues.capital(s, "bingx", CREDS)) == (Decimal("10000"), "")
    book.equity = "0"
    assert run(venues.capital(s, "bybit", CREDS))[0] is None
    assert run(venues.position_mode(s, "bingx", CREDS)) == ("oneway", "")
    book.dual = "true"
    assert run(venues.position_mode(s, "bingx", CREDS))[0] is None
    book.dual = "maybe"
    assert run(venues.position_mode(s, "bingx", CREDS))[0] is None
    assert run(venues.position_mode(Session(), "bybit", CREDS)) == ("oneway", "")
    book.funding = [{"symbol": "ETH-USDT", "income": "-0.02", "time": 1700000000000, "tranId": "f1"}]
    assert run(venues.funding_income(s, "ETH-USDT", T0, T0 + 1, CREDS)) == ([{"ref": "f1", "amount": Decimal("-0.02"),
                                                                       "ts": 1700000000000}], "")
    book.funding = [{"symbol": "ETH-USDT", "income": "x", "time": 1, "tranId": "f1"}]
    assert run(venues.funding_income(s, "ETH-USDT", T0, T0 + 1, CREDS))[0] is None
    book.closed = [{"symbol": "BTCUSDT", "orderId": "77", "closedSize": "0.001", "closedPnl": "-3.3",
                    "updatedTime": "1700000000000"}]
    assert run(venues.closed_pnl(s, "BTCUSDT", T0, T0 + 1, CREDS)) == ([{"order_id": "77", "qty": Decimal("0.001"),
                                                                   "pnl": Decimal("-3.3"), "ts": 1700000000000}], "")


def test_instrument_and_mark_price():
    book = Book()
    s = Session(book.routes("bybit") | book.routes("bingx"))
    inst, _ = run(venues.instrument(s, "bybit", "linear", "BTCUSDT"))
    assert inst == venues.Instrument(Decimal("0.001"), Decimal("0.001"), Decimal("1000"), Decimal("0.1"), Decimal("5"))
    spot, _ = run(venues.instrument(s, "bybit", "spot", "ETHUSDT"))
    assert spot.qty_step == Decimal("0.0001") and spot.min_notional == Decimal("1")
    bx, _ = run(venues.instrument(s, "bingx", "swap", "ETH-USDT"))
    assert bx == venues.Instrument(Decimal("0.01"), Decimal("0.01"), None, Decimal("0.01"), Decimal("2"))
    assert run(venues.mark_price(s, "bybit", "linear", "BTCUSDT")) == (Decimal("65000"), "")
    assert run(venues.mark_price(s, "bingx", "swap", "ETH-USDT")) == (Decimal("3000"), "")
    assert all(c["headers"] == {} for c in s.calls)                                  # публичное — без ключа
    lot = {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "10", "minNotionalValue": "5"}
    assert venues.instrument_view("bybit", "linear", {"lotSizeFilter": lot, "priceFilter": {"tickSize": "0.1"}})
    for broken in ({"lotSizeFilter": dict(lot, qtyStep="0"), "priceFilter": {"tickSize": "0.1"}},
                   {"lotSizeFilter": dict(lot, minOrderQty=""), "priceFilter": {"tickSize": "0.1"}},
                   {"lotSizeFilter": lot, "priceFilter": {}}, {"lotSizeFilter": lot},
                   {"lotSizeFilter": dict(lot, maxOrderQty="0.0001"), "priceFilter": {"tickSize": "0.1"}}):
        assert venues.instrument_view("bybit", "linear", broken) is None
    assert venues.instrument_view("bingx", "swap", {"quantityPrecision": "2", "pricePrecision": 2,
                                                    "tradeMinQuantity": "1", "tradeMinUSDT": "2"}) is None
    assert venues.instrument_view("bingx", "swap", {"quantityPrecision": True, "pricePrecision": 2,
                                                    "tradeMinQuantity": "1", "tradeMinUSDT": "2"}) is None
    s = Session({("GET", "/v5/market/tickers"): bybit_ok({"list": [{"symbol": "ETHUSDT", "markPrice": "1"}]})})
    assert run(venues.mark_price(s, "bybit", "linear", "BTCUSDT"))[0] is None           # не тот символ


def test_setting_call_builders():
    assert venues.leverage_call("bybit", "BTCUSDT", 2) == ("POST", "/v5/position/set-leverage", {
        "category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2", "sellLeverage": "2"})
    assert venues.leverage_call("bingx", "ETH-USDT", 3)[2] == {"symbol": "ETH-USDT", "side": "BOTH", "leverage": "3"}
    for bad in (4, 0, "2.5", -1):
        with pytest.raises(ValueError):
            venues.leverage_call("bybit", "BTCUSDT", bad)
    assert venues.margin_isolated_call("ETH-USDT")[2] == {"symbol": "ETH-USDT", "marginType": "ISOLATED"}
    assert not hasattr(venues, "stop_call")                              # стопа всей позиции символа нет
