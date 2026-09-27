"""Торговое ядро, venues: allowlist (метод, путь) и полей, запреты (вывод, переводы, P2P…), подпись по точным байтам,
без редиректов, разбор ответов. Сеть — заглушка; ключи — фиктивные."""
import ast
import hashlib
import hmac
import inspect
import json
import os
import re
from decimal import Decimal
from urllib.parse import unquote

import pytest

import accounts
from test_payout_pins import _senders
from trading import gates, journal, keys, risk, switch, venues
from trading_stubs import CREDS, KEY, SECRET, Resp, Session, bingx_ok, bybit_err, bybit_ok, run

CID = "t260927013512a1b2c3d4"
TS = "1700000000000"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    writes = {(v, m, p) for v, calls in venues.ALLOWED.items() for (m, p), sp in calls.items() if sp.kind == "write"}
    assert all(m != "GET" for _, m, _ in writes) and all(sp.kind == "read" for calls in venues.ALLOWED.values()
                                                         for (m, _), sp in calls.items() if m == "GET")


DENY = re.compile(r"withdraw|transfer|p2p|sub-?member|subaccount|sub-api|deposit|convert|exchange|loan|earn|asset|"
                  r"closeall|reverse|batch|margin-mode|switch-mode|switch-isolated|twap|getvst|autoaddmargin|"
                  r"positionmargin|add-margin|update-api|create-sub|spot-margin|borrow|repay|vst", re.I)


def test_no_allowlisted_path_looks_like_money_movement():
    for venue, calls in venues.ALLOWED.items():
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
    ("mexc", "GET", "/api/v3/account"), ("cryptomus", "POST", "/v1/payout"),
])
def test_denylist_refused_before_signing(venue, method, path):
    s = Session()
    with pytest.raises(ValueError):
        run(venues.call(s, venue, method, path, {"symbol": "BTCUSDT", "amount": "1"}, CREDS))
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
    {"qty": " 1"}, {"qty": "NaN"}, {"positionIdx": True}, {"positionIdx": 3}, {"positionIdx": "0"},
    {"reduceOnly": "true"}, {"orderLinkId": "manual-order-1"}, {"orderLinkId": CID.upper()},
    {"orderLinkId": CID + "&x=1"}, {"stopLoss": "0"}, {"tpslMode": "Partial"}, {"timeInForce": "RPI"},
])
def test_bybit_order_bad_values_refused(over):
    with pytest.raises(ValueError):
        venues.prepare("bybit", "POST", "/v5/order/create", linear(**over), CREDS)


@pytest.mark.parametrize("params", [
    linear(category="spot", positionIdx=None),                                   # None — не строка правила
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
    {"symbol": "ETHUSDT"}, {"symbol": "DOGE-USDT"}, {"positionSide": "LONG", "reduceOnly": "true"},
    {"reduceOnly": True}, {"type": "LIMIT"}, {"price": "3000"}, {"quantity": "0.01&side=BUY"},
    {"clientOrderId": "Manual1"}, {"stopLoss": '{"type":"STOP_MARKET","stopPrice":"2900","workingType":"MARK_PRICE"}'},
    {"stopLoss": '{"type":"STOP","stopPrice":2900,"price":2890,"workingType":"MARK_PRICE"}'},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":2900,"workingType":"MARK_PRICE","stopGuaranteed":true}'},
    {"stopLoss": '{"type":"STOP_MARKET","stopPrice":-1,"workingType":"MARK_PRICE"}'}, {"stopLoss": "not json"},
    {"reduceOnly": "true", "stopLoss": venues.bingx_stop_loss(Decimal("2900"))},
])
def test_bingx_bad_values_refused(over):
    with pytest.raises(ValueError):
        venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(**over), CREDS)


@pytest.mark.parametrize("venue,method,path,params", [
    ("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "4",
                                                    "sellLeverage": "4"}),
    ("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2.5",
                                                    "sellLeverage": "2.5"}),
    ("bybit", "POST", "/v5/position/set-leverage", {"category": "linear", "symbol": "BTCUSDT", "buyLeverage": "2",
                                                    "sellLeverage": "3"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/leverage", {"symbol": "BTC-USDT", "side": "LONG", "leverage": "10"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/leverage", {"symbol": "BTC-USDT", "side": "LONG", "leverage": "0"}),
    ("bingx", "POST", "/openApi/swap/v2/trade/marginType", {"symbol": "BTC-USDT", "marginType": "SEPARATE_ISOLATED"}),
    ("bybit", "POST", "/v5/position/trading-stop", {"category": "linear", "symbol": "BTCUSDT", "tpslMode": "Full",
                                                    "positionIdx": 0, "stopLoss": "0"}),   # снять стоп нельзя
    ("bybit", "POST", "/v5/order/cancel", {"category": "linear", "symbol": "BTCUSDT", "orderId": "123"}),
    ("bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "BTC-USDT", "orderId": "123"}),
    ("bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "BTC-USDT", "clientOrderId": "someoneelse1"}),
    ("bybit", "GET", "/v5/position/list", {"category": "spot"}),
])
def test_leverage_caps_stop_removal_and_foreign_orders_refused(venue, method, path, params):
    s = Session()
    with pytest.raises(ValueError):
        run(venues.call(s, venue, method, path, params, CREDS))
    assert s.calls == []


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
    assert req.headers == {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": "f64e81ab62555d07099dcdecba860cb1cc773308c8700ac4eae6218b3c3c59b7",
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
    req = venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order",
                         swap(side="BUY", positionSide="LONG", stopLoss=stop), CREDS, timestamp=TS)
    query = req.url.split("?", 1)[1]
    enc = [p for p in query.split("&") if p.startswith("stopLoss=")][0]
    assert "{" not in enc and '"' not in enc and unquote(enc[len("stopLoss="):]) == stop
    assert query.endswith("signature=f1fd23beca0218e958e8f695224ff3dd6d74f2e257aa143377f4315e3772e215")


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
    run(venues.call(s, "bybit", "POST", "/v5/order/create", linear(), CREDS, timestamp=TS))
    run(venues.call(s, "bingx", "POST", "/openApi/swap/v2/trade/order", swap(stopLoss=venues.bingx_stop_loss(
        Decimal("3100"))), CREDS, timestamp=TS))
    run(venues.call(s, "bingx", "DELETE", "/openApi/swap/v2/trade/order", {"symbol": "ETH-USDT", "clientOrderId": CID},
                    CREDS, timestamp=TS))
    run(venues.call(s, "bybit", "GET", "/v5/order/realtime", {"category": "linear", "symbol": "BTCUSDT"}, CREDS))
    by, bx, dl, rt = s.calls
    assert by["data"] == venues.prepare("bybit", "POST", "/v5/order/create", linear(), CREDS, TS).body.encode()
    assert bx["url"] == venues.prepare("bingx", "POST", "/openApi/swap/v2/trade/order", swap(
        stopLoss=venues.bingx_stop_loss(Decimal("3100"))), CREDS, TS).url and bx["data"] is None
    assert dl["method"] == "DELETE" and dl["query"]["clientOrderId"] == CID
    assert all(c["redirects"] is False for c in s.calls)
    for c in s.calls:
        wire = c["url"] + (c["data"] or b"").decode() + json.dumps({k: v for k, v in c["headers"].items()})
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
    ("bingx", 200, {"code": 101400}, "rejected"), ("bingx", 200, {"code": 100500}, "ambiguous"),
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
    _, _, sp = venues.create_call(venues.Order("bybit", "spot", "TONUSDT", "buy", "market", "10"), CID)
    assert sp["marketUnit"] == "baseCoin" and "positionIdx" not in sp
    _, _, lim = venues.create_call(venues.Order("bybit", "linear", "ETHUSDT", "buy", "limit", "0.01", price="3000.10",
                                                reduce_only=True), CID)
    assert lim["price"] == "3000.1" and lim["timeInForce"] == "GTC" and lim["reduceOnly"] is True
    m, p, bx = venues.create_call(venues.Order("bingx", "swap", "ETHUSDT", "buy", "market", "0.01", position="long",
                                               stop_loss="2900"), CID)
    assert (m, p) == ("POST", "/openApi/swap/v2/trade/order") and bx["positionSide"] == "LONG"
    assert bx["symbol"] == "ETH-USDT" and "reduceOnly" not in bx and json.loads(bx["stopLoss"])["stopPrice"] == 2900
    _, _, close = venues.create_call(venues.Order("bingx", "swap", "ETHUSDT", "buy", "market", "0.01",
                                                  reduce_only=True), CID)
    assert close["reduceOnly"] == "true" and close["positionSide"] == "BOTH"
    assert venues.Order("bingx", "swap", "ETHUSDT", "sell", "market", "1", position="long").reducing


@pytest.mark.parametrize("order", [
    lambda: venues.Order("bybit", "spot", "BTCUSDT", "sell", "market", "0.1", reduce_only=True),
    lambda: venues.Order("bybit", "spot", "BTCUSDT", "buy", "market", "0.1", stop_loss="1"),
    lambda: venues.Order("bybit", "linear", "BTCUSDT", "buy", "limit", "0.1"),              # лимитка без цены
    lambda: venues.Order("bybit", "linear", "DOGEUSDT", "buy", "market", "1"),
    lambda: venues.Order("bybit", "linear", "BTCUSDT", "buy", "market", 0.1),                # float
    lambda: venues.Order("bybit", "linear", "BTCUSDT", "buy", "market", "0.1", position="both"),
    lambda: venues.Order("bingx", "linear", "BTCUSDT", "buy", "market", "0.1"),
    lambda: venues.Order("bingx", "swap", "BTCUSDT", "sell", "market", "0.1", reduce_only=True, position="long"),
    lambda: venues.Order("okx", "swap", "BTCUSDT", "sell", "market", "0.1"),
])
def test_create_call_refuses_bad_orders(order):
    with pytest.raises(ValueError):
        venues.create_call(order(), CID)


def test_order_and_position_views():
    v = venues.order_view("bybit", {"orderId": "9", "orderLinkId": CID, "symbol": "BTCUSDT", "side": "Sell",
                                    "orderType": "Market", "qty": "0.001", "price": "0", "cumExecQty": "0.001",
                                    "avgPrice": "65000.5", "orderStatus": "Filled", "reduceOnly": False})
    assert v["state"] == "filled" and v["filled"] == Decimal("0.001") and v["side"] == "sell" and v["type"] == "market"
    assert venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": "Rejected", "cumExecQty": "0"})["state"] \
        == "rejected"
    assert venues.order_view("bybit", {"orderLinkId": CID, "orderStatus": "Weird"})["state"] is None
    b = venues.order_view("bingx", {"order": {"symbol": "ETH-USDT", "orderId": 1735950529123455000123, "side": "SELL",
                                              "type": "MARKET", "origQty": "0.01", "status": "FILLED",
                                              "clientOrderID": CID.upper(), "executedQty": "0.01"}})
    assert b["client_id"] == CID and b["order_id"] == "1735950529123455000123" and b["symbol"] == "ETHUSDT"
    assert b["state"] == "filled" and b["qty"] == Decimal("0.01")
    p = venues.position_view("bybit", {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01", "avgPrice": "65000",
                                       "markPrice": "64000", "liqPrice": "", "leverage": "2", "positionIdx": 0})
    assert p["side"] == "short" and p["liq"] is None and p["position"] == "oneway"
    assert venues.position_view("bybit", {"symbol": "BTCUSDT", "side": "", "size": "0"}) is None
    assert venues.position_view("bybit", {"symbol": "DOGEUSDT", "side": "Buy", "size": "1"}) is None
    q = venues.position_view("bingx", {"symbol": "ETH-USDT", "positionAmt": "0.20", "positionSide": "SHORT",
                                       "isolated": True, "avgPrice": "3000", "liquidationPrice": Decimal("4500.5"),
                                       "leverage": 2})
    assert q["side"] == "short" and q["size"] == Decimal("0.20") and q["liq"] == Decimal("4500.5") and q["isolated"]


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


# --- кто вообще может слать запросы ---

TRADING_MODULES = (venues, journal, risk, switch, gates, keys)


def test_only_venues_send_requests():
    """POST/PUT/DELETE/PATCH и сетевые клиенты — только в venues._send; остальные модули ядра и скрипт ключей
    ходят в сеть только через venues.call."""
    assert _senders(venues) == {"_send"}
    for mod in TRADING_MODULES[1:]:
        assert _senders(mod) == set(), mod.__name__
    script = type("M", (), {"__file__": os.path.join(ROOT, "scripts", "trading_keys.py")})
    assert _senders(script) == set()


def test_no_urls_outside_venues_and_bases_are_the_exchanges():
    assert venues.BASES == {"bybit": "https://api.bybit.com", "bingx": "https://open-api.bingx.com"}
    for mod in TRADING_MODULES[1:]:
        assert "https://" not in inspect.getsource(mod), mod.__name__
    tree = ast.parse(inspect.getsource(venues))
    urls = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "://" in n.value}
    assert urls == set()   # хосты — только из accounts.BYBIT_BASE / BINGX_BASE
