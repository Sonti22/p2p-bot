"""Торговое ядро, venues: allowlist (метод, путь) и полей, запреты (вывод, переводы, P2P…), подпись по точным байтам,
без редиректов, разбор ответов, символы (TON → GRAM, ловушка BingX GRAM-USDT), режим маржи. Сеть — заглушка;
ключи — фиктивные."""
import ast
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
from trading import gates, journal, keys, risk, switch, venues
from trading_stubs import CREDS, KEY, SECRET, Resp, Session, bingx_err, bingx_ok, bybit_err, bybit_ok, run

CID = "t260927013512a1b2c3d4"
TS = "1700000000000"
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
    assert set(venues.ALLOWED[venues.BYBIT]) == {
        ("POST", "/v5/order/create"), ("POST", "/v5/order/cancel"), ("POST", "/v5/position/trading-stop"),
        ("POST", "/v5/position/set-leverage"), ("GET", "/v5/order/realtime"), ("GET", "/v5/order/history"),
        ("GET", "/v5/execution/list"), ("GET", "/v5/position/list"), ("GET", "/v5/position/closed-pnl"),
        ("GET", "/v5/account/wallet-balance"), ("GET", "/v5/account/info"), ("GET", "/v5/user/query-api")}
    assert set(venues.ALLOWED[venues.BINGX]) == {
        ("POST", "/openApi/swap/v2/trade/order"), ("DELETE", "/openApi/swap/v2/trade/order"),
        ("POST", "/openApi/swap/v2/trade/leverage"), ("POST", "/openApi/swap/v2/trade/marginType"),
        ("GET", "/openApi/swap/v2/trade/order"), ("GET", "/openApi/swap/v2/trade/openOrders"),
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
])
def test_bybit_order_extra_or_foreign_fields_refused(field, value):
    with pytest.raises(ValueError):
        venues.prepare("bybit", "POST", "/v5/order/create", linear(**{field: value}), CREDS)


@pytest.mark.parametrize("field,value", [
    ("quoteOrderQty", "100"), ("stopPrice", "2000"), ("closePosition", "true"), ("takeProfit", "{}"),
    ("priceRate", "0.05"), ("activationPrice", "2000"), ("stopGuaranteed", "true"), ("positionId", "1"),
    ("workingType", "MARK_PRICE"), ("timestamp", TS), ("signature", "x"), ("recvWindow", "5000"),
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
    linear(reduceOnly=True, stopLoss="70000"),                                  # reduceOnly со стопом
    linear(tpslMode="Full"),                                                    # tpslMode без стопа
    linear(marketUnit="baseCoin"),                                              # marketUnit не для linear
])
def test_bybit_cross_field_rules(params):
    with pytest.raises(ValueError):
        venues.prepare("bybit", "POST", "/v5/order/create", params, CREDS)


@pytest.mark.parametrize("over", [
    {"symbol": "ETHUSDT"}, {"symbol": "DOGE-USDT"}, {"positionSide": "LONG"}, {"positionSide": "SHORT"},
    {"positionSide": "LONG", "reduceOnly": "true"}, {"reduceOnly": True}, {"type": "LIMIT"}, {"price": "3000"},
    {"quantity": "0.01&side=BUY"}, {"clientOrderId": "Manual1"},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":"2900","workingType":"MARK_PRICE"}'},
    {"stopLoss": '{"type":"STOP","stopPrice":2900,"price":2890,"workingType":"MARK_PRICE"}'},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":2900,"workingType":"MARK_PRICE","stopGuaranteed":true}'},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":-1,"workingType":"MARK_PRICE"}'}, {"stopLoss": "not json"},
    {"reduceOnly": "true", "stopLoss": venues.bingx_stop_loss(Decimal("2900"))},
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


def test_bingx_stop_loss_json_encoded_in_url_signature_over_raw_string():
    stop = venues.bingx_stop_loss(Decimal("2900.50"))
    assert stop == '{"type":"STOP_MARKET","stopPrice":2900.5,"workingType":"MARK_PRICE"}'
    req = venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(side="BUY", stopLoss=stop), CREDS,
                         timestamp=TS)
    query = req.url.split("?", 1)[1]
    enc = [p for p in query.split("&") if p.startswith("stopLoss=")][0]
    assert "{" not in enc and '"' not in enc and unquote(enc[len("stopLoss="):]) == stop
    assert query.endswith("signature=3ac295ae526c75d1af4b085d2263e983eb22283a5a70cf5896e805fa03e78563")


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
    stop = venues.bingx_stop_loss(Decimal("3100"))
    run(venues.call(s, "bybit", "POST", "/v5/order/create", linear(), CREDS, timestamp=TS))
    run(venues.call(s, "bingx", "POST", "/openApi/swap/v2/trade/order", swap(stopLoss=stop), CREDS, timestamp=TS))
    run(venues.call(s, "bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "ETH-USDT", "clientOrderId": CID},
                    CREDS, timestamp=TS))
    run(venues.call(s, "bybit", "GET", "/v5/order/realtime", {"category": "linear", "symbol": "BTCUSDT"}, CREDS))
    by, bx, dl, rt = s.calls
    assert by["data"] == venues.prepare("bybit", "POST", "/v5/order/create", linear(), CREDS, TS).body.encode()
    assert bx["url"] == venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(stopLoss=stop), CREDS,
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
        "orderLinkId": CID, "positionIdx": 0, "stopLoss": "70000", "tpslMode": "Full", "slTriggerBy": "MarkPrice"}
    _, _, sp = venues.create_call(venues.Order("bybit", "spot", "ETHUSDT", "buy", "market", "0.1"), CID)
    assert sp["marketUnit"] == "baseCoin" and "positionIdx" not in sp
    _, _, lim = venues.create_call(venues.Order("bybit", "linear", "ETHUSDT", "buy", "limit", "0.01", price="3000.10",
                                                reduce_only=True), CID)
    assert lim["price"] == "3000.1" and lim["timeInForce"] == "GTC" and lim["reduceOnly"] is True
    m, p, bx = venues.create_call(venues.Order("bingx", "swap", "ETHUSDT", "buy", "market", "0.01", stop_loss="2900"),
                                  CID)
    assert (m, p) == ("POST", "/openApi/swap/v2/trade/order") and bx["positionSide"] == "BOTH"
    assert bx["symbol"] == "ETH-USDT" and "reduceOnly" not in bx and json.loads(bx["stopLoss"])["stopPrice"] == 2900
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
    for weird in ("Weird", "Triggered"):
        assert venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": weird})["state"] is None
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

TRADING_MODULES = (venues, journal, risk, switch, gates, keys)


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
