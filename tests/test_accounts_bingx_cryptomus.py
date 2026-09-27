"""BingX и Cryptomus: только чтение по построению (allowlist путей, POST к BingX нет), подписи, права ключа,
определение кабинета Cryptomus, балансы, история, экраны бота, старт с ALLOW_UNSAFE_KEYS, гости.
Все ключи — фиктивные, сеть — заглушка: ответ по (метод, путь)."""
import ast
import hashlib
import hmac
import inspect
import socket
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import accounts
import bot as B
import p2p
from test_guests import Stub as GuestStub, msg, sent
from helpers import arun

BX_KEY, BX_SECRET = "FAKEBINGXKEY0123456789AB", "FAKEBINGXSECRET0123456789ABCDEF"
CM_ID, CM_KEY = "11111111-2222-3333-4444-555555555555", "FAKECRYPTOMUSKEY0123456789ABCDEF"
MSK = timezone(timedelta(hours=3))

SPOT, FUND = "/openApi/spot/v1/account/balance", "/openApi/fund/v1/account/balance"
PERMS = "/openApi/v1/account/apiPermissions"
DEPOSITS, WITHDRAWS = "/openApi/api/v3/capital/deposit/hisrec", "/openApi/api/v3/capital/withdraw/history"
CM_USER, CM_MERCHANT, CM_HIST = "/v2/user-api/balance", "/v1/balance", "/v2/user-api/transaction/list"

CM_USER_OK = {"state": 0, "result": {"balances": [
    {"walletUuid": "w1", "currency_code": "USDT", "balance": "12.5", "balanceUsd": "12.5"},
    {"walletUuid": "w2", "currency_code": "GRAM", "balance": "4.00000000", "balanceUsd": "20"},
    {"walletUuid": "w3", "currency_code": "BTC", "balance": "0.00000000", "balanceUsd": "0"}]}}
CM_MERCHANT_OK = {"state": 0, "result": [{"balance": {
    "merchant": [{"uuid": "a", "balance": "100.00000000", "currency_code": "USDT"}],
    "user": [{"uuid": "b", "balance": "5.5", "currency_code": "USDT"},
             {"uuid": "c", "balance": "2", "currency_code": "GRAM"}]}}]}
CM_FAIL = {"state": 1, "message": "Unauthorized"}


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(accounts, "_CRYPTOMUS_MODE", {})   # кэш режима Cryptomus не протекает между тестами
    for ex in ("BINGX", "CRYPTOMUS"):
        for suffix in ("API_KEY", "API_SECRET"):
            monkeypatch.delenv(f"{ex}_{suffix}", raising=False)
    monkeypatch.delenv("ALLOW_UNSAFE_KEYS", raising=False)


def _http_error(status=401, message="Unauthorized"):
    u = URL(accounts.CRYPTOMUS_BASE + "/x")
    return aiohttp.ClientResponseError(aiohttp.RequestInfo(u, "GET", CIMultiDictProxy(CIMultiDict()), u), (),
                                       status=status, message=message)


class Redirect:
    """Ответ 3xx (как его отдаёт aiohttp при allow_redirects=False)."""
    def __init__(self, status=307):
        self.status = status


class _Resp:
    def __init__(self, body):
        self.body = body
        self.status = body.status if isinstance(body, Redirect) else 200
        u = URL(accounts.CRYPTOMUS_BASE)
        self.request_info, self.history = aiohttp.RequestInfo(u, "GET", CIMultiDictProxy(CIMultiDict()), u), ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if isinstance(self.body, Exception):
            raise self.body

    async def json(self, content_type=None):
        return self.body


class Session:
    """Ответ по (метод, путь); каждый запрос записывается: (метод, URL, заголовки, тело), allow_redirects — в redirects."""
    def __init__(self, routes):
        self.routes, self.calls, self.redirects = routes, [], []

    def _answer(self, method, url, headers, data=None, allow_redirects=True):
        self.calls.append((method, url, headers, data))
        self.redirects.append(allow_redirects)
        body = self.routes.get((method, URL(url).path))
        if body is None:
            raise AssertionError(f"unexpected request: {method} {URL(url).path}")
        return _Resp(body)

    def get(self, url, headers=None, allow_redirects=True):
        return self._answer("GET", url, headers, allow_redirects=allow_redirects)

    def post(self, url, headers=None, data=None, allow_redirects=True):
        return self._answer("POST", url, headers, data, allow_redirects=allow_redirects)

    def paths(self):
        return [(m, URL(u).path) for m, u, _, _ in self.calls]


def run(coro):
    return arun(coro)


# --- BingX: подпись и allowlist ---

def test_bingx_signature_official_example():
    """Официальный пример подписи из документации BingX (секрет из примера, запрос никуда не отправляется)."""
    secret = "UuGuyEGt6ZEkpUObCYCmIfh0elYsZVh80jlYwpJuRZEw70t6vomMH7Sjmf94ztSI"
    q = accounts.bingx_signed_query(secret, {"symbol": "ETHUSDT", "type": "MARKET", "side": "BUY", "quoteOrderQty": 20},
                                    timestamp=1649404670162, recv_window=None)
    assert q == ("quoteOrderQty=20&side=BUY&symbol=ETHUSDT&timestamp=1649404670162&type=MARKET"
                 "&signature=428a3c383bde514baff0d10d3c20e5adfaacaf799e324546dafe5ccc480dd827")


def test_bingx_signed_query_sorted_with_recv_window_and_signature_last():
    q = accounts.bingx_signed_query("s", {"limit": 20}, timestamp="1700000000000")
    canonical = "limit=20&recvWindow=5000&timestamp=1700000000000"
    assert q == canonical + "&signature=" + hmac.new(b"s", canonical.encode(), hashlib.sha256).hexdigest()


def test_bingx_get_is_signed_get_with_api_key_header():
    s = Session({("GET", SPOT): {"code": 0, "data": {"balances": []}}})
    run(accounts.bingx_get(s, BX_KEY, BX_SECRET, SPOT))
    method, url, headers, data = s.calls[0]
    assert method == "GET" and url.startswith(accounts.BINGX_BASE + SPOT + "?recvWindow=5000&timestamp=")
    assert headers == {"X-BX-APIKEY": BX_KEY} and data is None
    assert url.rsplit("&", 1)[1].startswith("signature=") and BX_SECRET not in url


@pytest.mark.parametrize("path", [
    "/openApi/spot/v1/trade/order", "/openApi/wallets/v1/capital/withdraw/apply",
    "/openApi/wallets/v1/capital/innerTransfer/apply", "/openApi/api/asset/v1/transfer",
    "/openApi/wallets/v1/capital/subAccountInnerTransfer/apply", "/openApi/spot/v1/trade/cancel",
    SPOT + "?x=1", SPOT + "/../../trade/order", "/openApi/spot/v1/account/balance/"])
def test_bingx_get_refuses_paths_outside_allowlist_before_sending(path):
    s = Session({})
    with pytest.raises(ValueError):
        run(accounts.bingx_get(s, BX_KEY, BX_SECRET, path))
    assert s.calls == []


def _state_changing_calls():
    """Имена функций accounts.py, в которых есть вызов .post/.put/.delete/.patch/.request."""
    tree = ast.parse(inspect.getsource(accounts))
    found = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("post", "put", "delete", "patch", "request")):
                    found.add(fn.name)
    return found


def test_no_bingx_post_anywhere_and_only_known_post_senders():
    """POST в accounts.py — только bybit_post (P2P-история Bybit, было раньше) и cryptomus_call (со своим allowlist)."""
    assert _state_changing_calls() == {"bybit_post", "cryptomus_call"}
    assert not [n for n in dir(accounts) if n.lower().startswith("bingx") and "post" in n.lower()]
    assert {m for m, _ in accounts.CRYPTOMUS_READ_CALLS} <= {"GET", "POST"}


def test_every_bingx_feature_sends_only_allowlisted_gets():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    ok = {"code": 0, "data": {"balances": []}}
    s = Session({("GET", SPOT): ok, ("GET", FUND): ok, ("GET", PERMS): {"code": 0, "data": {"permissions": [2]}},
                 ("GET", DEPOSITS): [], ("GET", WITHDRAWS): []})
    run(accounts.verify(s, "bingx"))
    run(accounts.key_permissions(s, "bingx"))
    run(accounts.portfolio(s))
    run(accounts.account_history(s, "bingx"))
    assert s.calls and all(m == "GET" and p in accounts.BINGX_READ_PATHS for m, p in s.paths())
    assert all(URL(u).host == URL(accounts.BINGX_BASE).host for _, u, _, _ in s.calls)
    assert s.redirects and not any(s.redirects)   # ни один запрос не идёт за редиректом


# --- Cryptomus: подпись и allowlist ---

def test_cryptomus_sign_matches_independent_md5():
    # base64("{}") == "e30=" — считаем без модуля base64, чтобы не повторять реализацию
    assert accounts.cryptomus_sign("{}", CM_KEY) == hashlib.md5(("e30=" + CM_KEY).encode()).hexdigest()
    assert accounts.cryptomus_sign("", CM_KEY) == hashlib.md5(CM_KEY.encode()).hexdigest()   # без тела — md5(key)


@pytest.mark.parametrize("method,path", [
    ("POST", "/v2/user-api/exchange/orders"), ("DELETE", "/v2/user-api/convert/x"), ("POST", "/v1/payout"),
    ("POST", "/v2/user-api/aml/check/packages/purchase"), ("POST", "/v2/user-api/convert/"),
    ("POST", "/v2/user-api/convert/limit"), ("POST", "/v2/user-api/exchange/orders/market"),
    ("DELETE", "/v2/user-api/exchange/orders/123"), ("POST", "/v1/transfer/to-personal"),
    ("POST", "/v1/payment/refund"), ("POST", "/v1/wallet/blocked-address-refund"), ("POST", "/v1/payment"),
    ("GET", "/v1/balance"), ("POST", "/v2/user-api/balance"), ("GET", "/v2/user-api/transaction/list"),
    ("POST", "/v1/balance/"), ("PUT", "/v1/balance")])
def test_cryptomus_call_refuses_pairs_outside_allowlist_before_sending(method, path):
    s = Session({})
    with pytest.raises(ValueError):
        run(accounts.cryptomus_call(s, CM_ID, CM_KEY, method, path, {}, mode="user"))
    assert s.calls == []


def test_cryptomus_call_refuses_unknown_mode():
    s = Session({})
    with pytest.raises(ValueError):
        run(accounts.cryptomus_call(s, CM_ID, CM_KEY, "GET", CM_USER, mode="payout"))
    assert s.calls == []


def test_cryptomus_call_headers_for_user_and_merchant():
    s = Session({("GET", CM_USER): CM_USER_OK, ("POST", CM_MERCHANT): CM_MERCHANT_OK})
    run(accounts.cryptomus_call(s, CM_ID, CM_KEY, "GET", CM_USER, mode="user"))
    run(accounts.cryptomus_call(s, CM_ID, CM_KEY, "post", CM_MERCHANT, {}, mode="merchant"))
    (_, url1, h1, d1), (_, url2, h2, d2) = s.calls
    assert url1 == accounts.CRYPTOMUS_BASE + CM_USER and d1 is None
    assert h1 == {"userId": CM_ID, "sign": hashlib.md5(CM_KEY.encode()).hexdigest(), "Content-Type": "application/json"}
    assert url2 == accounts.CRYPTOMUS_BASE + CM_MERCHANT and d2 == "{}"
    assert h2 == {"merchant": CM_ID, "sign": hashlib.md5(("e30=" + CM_KEY).encode()).hexdigest(),
                  "Content-Type": "application/json"}
    assert CM_KEY not in str(s.calls)   # сам ключ в запрос не уходит — только подпись


def test_every_cryptomus_feature_sends_only_allowlisted_calls():
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    s = Session({("GET", CM_USER): CM_USER_OK, ("POST", CM_HIST): {"state": 0, "result": {"items": []}}})
    run(accounts.verify(s, "cryptomus"))
    run(accounts.key_permissions(s, "cryptomus"))
    run(accounts.portfolio(s))
    run(accounts.account_history(s, "cryptomus"))
    assert s.calls and all(pair in accounts.CRYPTOMUS_READ_CALLS for pair in s.paths())
    assert all(URL(u).host == URL(accounts.CRYPTOMUS_BASE).host for _, u, _, _ in s.calls)
    assert s.redirects and not any(s.redirects)


# --- редиректы: путь и хост не может подменить и сервер ---

def test_redirect_answer_is_an_error_and_not_followed():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    s = Session({("GET", SPOT): Redirect(302)})
    assert run(accounts.verify(s, "bingx")) == (False, "HTTP 302: редирект запрещён")
    assert s.redirects == [False]
    s = Session({("GET", CM_USER): Redirect(307), ("POST", CM_MERCHANT): Redirect(308)})
    assert run(accounts.cryptomus_detect(s, CM_ID, CM_KEY)) == (None, "HTTP 308: редирект запрещён")
    assert s.paths() == [("GET", CM_USER), ("POST", CM_MERCHANT)] and s.redirects == [False, False]


async def _loopback_site(handler):
    """Локальный aiohttp-сервер на 127.0.0.1 (порт выбирает ОС) — наружу запросы не уходят. (runner, base URL)."""
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    await web.SockSite(runner, sock).start()
    return runner, str(URL.build(scheme="http", host="127.0.0.1", port=sock.getsockname()[1]))


def test_real_aiohttp_does_not_follow_redirect_off_the_allowlist(monkeypatch):
    """Настоящий aiohttp: 307 с POST /v1/balance на /v1/payout и 302 с баланса BingX на торговый путь другого хоста.
    Без allow_redirects=False aiohttp повторил бы POST с телом и sign на /v1/payout, а X-BX-APIKEY отдал бы чужому хосту."""
    seen = []

    async def scenario():
        async def handler(request):
            seen.append((request.method, request.path, request.headers.get("merchant"), request.headers.get("X-BX-APIKEY")))
            if request.path == CM_MERCHANT:
                raise web.HTTPTemporaryRedirect("/v1/payout")
            if request.path == SPOT:
                raise web.HTTPFound(str(URL(other).with_path("/openApi/spot/v1/trade/order")))
            return web.json_response({"code": 0, "state": 0, "result": {}, "data": {}})

        runner, base = await _loopback_site(handler)
        runner2, other = await _loopback_site(handler)
        monkeypatch.setattr(accounts, "CRYPTOMUS_BASE", base)
        monkeypatch.setattr(accounts, "BINGX_BASE", base)
        try:
            async with aiohttp.ClientSession() as s:
                with pytest.raises(aiohttp.ClientResponseError) as cm:
                    await accounts.cryptomus_call(s, CM_ID, CM_KEY, "POST", CM_MERCHANT, {}, mode="merchant")
                with pytest.raises(aiohttp.ClientResponseError) as bx:
                    await accounts.bingx_get(s, BX_KEY, BX_SECRET, SPOT)
        finally:
            await runner.cleanup()
            await runner2.cleanup()
        return cm.value.status, bx.value.status

    assert run(scenario()) == (307, 302)
    assert seen == [("POST", CM_MERCHANT, CM_ID, None), ("GET", SPOT, None, BX_KEY)]   # второго запроса нет


# --- Cryptomus: определение кабинета ---

def test_cryptomus_detect_user_mode_first():
    s = Session({("GET", CM_USER): CM_USER_OK})
    mode, j = run(accounts.cryptomus_detect(s, CM_ID, CM_KEY))
    assert mode == "user" and j == CM_USER_OK and s.paths() == [("GET", CM_USER)]
    assert s.calls[0][2]["userId"] == CM_ID and "merchant" not in s.calls[0][2]


@pytest.mark.parametrize("user_answer", [CM_FAIL, _http_error(401), {"message": "Unauthorized"}])
def test_cryptomus_detect_falls_back_to_merchant_and_caches_it(user_answer):
    s = Session({("GET", CM_USER): user_answer, ("POST", CM_MERCHANT): CM_MERCHANT_OK})
    assert run(accounts.cryptomus_detect(s, CM_ID, CM_KEY))[0] == "merchant"
    assert s.paths() == [("GET", CM_USER), ("POST", CM_MERCHANT)]
    assert s.calls[1][2]["merchant"] == CM_ID and "userId" not in s.calls[1][2]
    run(accounts.cryptomus_detect(s, CM_ID, CM_KEY))   # второй раз — сразу бизнес-кабинет
    assert s.paths()[2:] == [("POST", CM_MERCHANT)]


def test_cryptomus_detect_none_when_both_fail():
    s = Session({("GET", CM_USER): CM_FAIL, ("POST", CM_MERCHANT): _http_error(401)})
    mode, err = run(accounts.cryptomus_detect(s, CM_ID, CM_KEY))
    assert mode is None and "401" in err and accounts._CRYPTOMUS_MODE == {}


# --- права ключа ---

def test_bingx_key_safety_docs_v3_shape():
    assert accounts.bingx_key_safety({"apiKey": "***", "permissions": [2], "ipAddresses": ["1.2.3.4"]}) == (True, "")
    safe, detail = accounts.bingx_key_safety({"permissions": [1, 2, 3, 4, 5, 7], "ipAddresses": []})
    assert safe is False
    for word in ("торговля спот", "фьючерсы", "переводы между своими счетами",
                 "вывод и переводы другим пользователям BingX", "переводы между субаккаунтами", "без привязки к IP"):
        assert word in detail, word
    assert accounts.bingx_key_safety({"permissions": ["2", "1"], "ipAddresses": ["1.2.3.4"]}) == (False, "торговля спот")
    assert accounts.bingx_key_safety({"permissions": [2, 9]}) == (False, "право 9")   # незнакомый код — не чтение


def test_bingx_key_safety_enable_flags_shape():
    ro = {"enableReading": True, "enableSpotAndMarginTrading": False, "enableWithdrawals": False,
          "enableInternalTransfer": False, "enableFutures": False, "permitsUniversalTransfer": False,
          "enableVanillaOptions": False, "ipRestrict": True, "createTime": 1700000000000}
    assert accounts.bingx_key_safety(ro) == (True, "")
    trade = dict(ro, enableSpotAndMarginTrading=True, enableFutures=True, ipRestrict=False)
    assert accounts.bingx_key_safety(trade) == (False, "торговля спот, фьючерсы; без привязки к IP")
    wd = dict(ro, enableWithdrawals=True, permitsUniversalTransfer=True)
    safe, detail = accounts.bingx_key_safety(wd)
    assert safe is False and "вывод и переводы другим пользователям BingX" in detail and "без привязки" not in detail


RO_FLAGS = {"enableReading": True, "enableSpotAndMarginTrading": False, "enableWithdrawals": False,
            "enableInternalTransfer": False, "enableFutures": False, "permitsUniversalTransfer": False}


@pytest.mark.parametrize("data", [
    None, {}, {"foo": 1}, "text", {"permissions": {"a": 1}}, {"permissions": ["x"]},
    {"permissions": []},                                            # пустой список прав — чтение не подтверждено
    {"enableReading": True},                                        # флагов прав сверх чтения нет вовсе
    {"ipRestrict": True, "createTime": 1, "permitsUniversalTransfer": False, "enableReading": True,
     "enableFutures": False, "enableSpotAndMarginTrading": False},  # урезанная форма api-ai-skills: без enableWithdrawals
    dict(RO_FLAGS, enableWithdrawals="yes"),                        # значение не распознано
    dict(RO_FLAGS, enableWithdrawals=None),
    dict(RO_FLAGS, enableReading=False),                            # чтение не подтверждено
    dict(RO_FLAGS, enableVanillaOptions="maybe")])
def test_bingx_key_safety_unknown_or_incomplete_is_unknown(data):
    assert accounts.bingx_key_safety(data) == (None, "")


@pytest.mark.parametrize("value", [True, "true", "True", "TRUE", " true ", "1", 1])
def test_bingx_key_safety_any_spelling_of_true_is_a_right(value):
    assert accounts.bingx_key_safety(dict(RO_FLAGS, enableWithdrawals=value)) == (
        False, "вывод и переводы другим пользователям BingX")


@pytest.mark.parametrize("value", [False, "false", "False", "FALSE", "0", 0])
def test_bingx_key_safety_any_spelling_of_false_is_not_a_right(value):
    assert accounts.bingx_key_safety(dict(RO_FLAGS, enableWithdrawals=value, enableVanillaOptions=value)) == (True, "")


def test_bingx_key_safety_unknown_flag_and_partial_answer():
    # незнакомый флаг enable*/permits* со значением «истина» — право сверх чтения
    assert accounts.bingx_key_safety(dict(RO_FLAGS, enableMargin=True, permitsSubAccounts="true")) == (
        False, "право enableMargin, право permitsSubAccounts")
    # право сверх чтения видно, а остальных флагов нет — False, и честно: что не проверено
    safe, detail = accounts.bingx_key_safety({"enableReading": True, "enableSpotAndMarginTrading": True,
                                              "ipRestrict": False})
    assert safe is False and detail.startswith("торговля спот; без привязки к IP; не проверено: ")
    for word in ("фьючерсы", "переводы между своими счетами", "вывод и переводы другим пользователям BingX",
                 "переводы между субаккаунтами"):
        assert word in detail, word
    assert accounts.bingx_key_safety({"permissions": [1]}) == (False, "торговля спот")   # без Read, но с торговлей


def test_bingx_key_safety_mixed_codes_and_flags():
    """Коды и флаги в одном ответе: флаг сверх чтения не прячется за permissions [2]; нераспознанный флаг —
    «только чтение» не подтверждено."""
    assert accounts.bingx_key_safety({"permissions": [2], "ipAddresses": [], "enableWithdrawals": True}) == (
        False, "вывод и переводы другим пользователям BingX; без привязки к IP")
    assert accounts.bingx_key_safety({"permissions": "2", "enableSpotAndMarginTrading": "true"}) == (
        False, "торговля спот")
    assert accounts.bingx_key_safety({"permissions": [2], "enableFutures": "maybe"}) == (None, "")
    assert accounts.bingx_key_safety({"permissions": [2], "enableReading": True, "enableFutures": False}) == (True, "")


BX_DOCS_V3_PERMS = {"apiKey": "", "permissions": [1, 2], "ipAddresses": [], "note": "demo"}   # пример из docs-v3


def test_key_permissions_bingx_reads_api_permissions():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    s = Session({("GET", PERMS): {"code": 0, "msg": "", "data": {"permissions": [1, 2], "ipAddresses": []}}})
    assert run(accounts.key_permissions(s, "bingx")) == (False, "торговля спот; без привязки к IP")
    assert s.paths() == [("GET", PERMS)]


@pytest.mark.parametrize("body,expected", [
    (BX_DOCS_V3_PERMS, (False, "торговля спот; без привязки к IP")),
    ({"apiKey": "", "permissions": [2], "ipAddresses": ["1.2.3.4"], "note": "bot"}, (True, "")),
    ({"code": "0", "data": {"permissions": [2], "ipAddresses": ["1.2.3.4"]}}, (True, "")),
    (dict(RO_FLAGS, ipRestrict=True), (True, "")),                  # флаги тоже бывают без обёртки
    ({"msg": "something went wrong"}, (None, "")), ({"code": 0}, (None, ""))])
def test_key_permissions_bingx_accepts_answer_with_and_without_envelope(body, expected):
    """docs-v3 показывает ответ apiPermissions без обёртки {code, data} — его тоже разбираем, а не считаем ошибкой."""
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    assert run(accounts.key_permissions(Session({("GET", PERMS): body}), "bingx")) == expected


def test_startup_without_flag_deletes_bingx_trading_key_from_docs_v3_answer():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    bot = Stub(Session({("GET", PERMS): BX_DOCS_V3_PERMS}))
    run(bot.check_key_safety())
    assert accounts.keys("bingx") is None
    assert [t for t in texts(bot) if t.startswith("⚠️ BingX: ключ даёт больше, чем чтение (торговля спот")]


@pytest.mark.parametrize("body", [{"code": 100413, "msg": "Null apiKey"}, {"code": 100004, "msg": "no permission"},
                                  _http_error(403, "Forbidden"), []])
def test_key_permissions_bingx_errors_are_unknown_and_fail_open(body):
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    s = Session({("GET", PERMS): body})
    assert run(accounts.key_permissions(s, "bingx")) == (None, "")
    assert run(accounts.api_permissions(s, "bingx")) == (True, "")


def test_key_permissions_cryptomus_is_never_readonly_and_sends_nothing():
    """Read-only ключей у Cryptomus нет — ответ известен заранее: False без единого запроса."""
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    s = Session({})
    safe, detail = run(accounts.key_permissions(s, "cryptomus"))   # кабинет ещё не определён — перечисляем оба
    assert safe is False and "нет ключей только для чтения" in detail and s.calls == []
    for word in ("конвертации", "отмену ордеров", "AML", "возвраты", "выплаты"):
        assert word in detail, word
    run(accounts.cryptomus_detect(Session({("GET", CM_USER): CM_USER_OK}), CM_ID, CM_KEY))
    assert run(accounts.key_permissions(s, "cryptomus")) == (False, accounts.CRYPTOMUS_KEY_RIGHTS["user"])
    accounts._CRYPTOMUS_MODE[CM_ID] = "merchant"
    assert run(accounts.key_permissions(s, "cryptomus")) == (False, accounts.CRYPTOMUS_KEY_RIGHTS["merchant"])
    assert s.calls == []


@pytest.mark.parametrize("routes", [{}, {("GET", CM_USER): CM_FAIL, ("POST", CM_MERCHANT): CM_FAIL},
                                    {("GET", CM_USER): _http_error(502, "Bad Gateway"),
                                     ("POST", CM_MERCHANT): _http_error(502, "Bad Gateway")}])
def test_key_permissions_cryptomus_false_even_when_cryptomus_unreachable(routes):
    """Сбой сети/ключ не принят — не «проверить не удалось» (при старте это считалось бы безопасным), а всё так же False."""
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    safe, detail = run(accounts.key_permissions(Session(routes), "cryptomus"))
    assert safe is False and "нет ключей только для чтения" in detail
    assert run(accounts.api_permissions(Session(routes), "cryptomus")) == (False, detail)


# --- verify: подсказки по ошибкам без ключа ---

def test_verify_bingx_ok():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    ok, _ = run(accounts.verify(Session({("GET", SPOT): {"code": 0, "data": {"balances": []}}}), "bingx"))
    assert ok


@pytest.mark.parametrize("code", sorted(accounts.BINGX_ERRORS))
def test_verify_bingx_error_codes_give_hint_without_key(code):
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    body = {"code": code, "msg": f"apiKey {BX_KEY} secret {BX_SECRET}", "timestamp": 1700000000000}
    ok, msg = run(accounts.verify(Session({("GET", SPOT): body}), "bingx"))
    assert not ok and accounts.BINGX_ERRORS[code] in msg and str(code) in msg
    assert BX_KEY not in msg and BX_SECRET not in msg


def test_verify_bingx_unknown_code_scrubs_echoed_key_and_http_error_hides_url():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    ok, msg = run(accounts.verify(Session({("GET", SPOT): {"code": 109999, "msg": f"bad key {BX_KEY}"}}), "bingx"))
    assert not ok and "109999" in msg and BX_KEY not in msg
    ok, msg = run(accounts.verify(Session({("GET", SPOT): _http_error(403, "Forbidden")}), "bingx"))
    assert not ok and msg == "HTTP 403: Forbidden"


def test_verify_cryptomus_user_ok_and_failure_hides_id_and_key():
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    ok, msg = run(accounts.verify(Session({("GET", CM_USER): CM_USER_OK}), "cryptomus"))
    assert ok and "личный кабинет" in msg
    accounts._CRYPTOMUS_MODE.clear()
    echo = {"state": 1, "message": f"user {CM_ID} key {CM_KEY} not found"}
    ok, msg = run(accounts.verify(Session({("GET", CM_USER): echo, ("POST", CM_MERCHANT): echo}), "cryptomus"))
    assert not ok and "Cryptomus" in msg and CM_ID not in msg and CM_KEY not in msg


# --- балансы ---

def test_bingx_balances_sum_spot_and_fund():
    s = Session({
        ("GET", SPOT): {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "10.5", "locked": "1"},
                                                          {"asset": "BTC", "free": "0", "locked": "0"}]}},
        ("GET", FUND): {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "2", "locked": "0"},
                                                          {"asset": "TON", "free": "3", "locked": "0"}]}}})
    assert run(accounts.bingx_balances(s, BX_KEY, BX_SECRET)) == {"USDT": 13.5, "TON": 3.0}


@pytest.mark.parametrize("fund", [{"code": 100004, "msg": "no permission"}, _http_error(500, "Server Error"),
                                  {"code": 0, "data": "weird"}])
def test_bingx_balances_keep_spot_when_fund_fails(fund):
    s = Session({("GET", SPOT): {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "1", "locked": "0"}]}},
                 ("GET", FUND): fund})
    assert run(accounts.bingx_balances(s, BX_KEY, BX_SECRET)) == {"USDT": 1.0}


def test_cryptomus_balances_user_wallet_gram_is_ton():
    s = Session({("GET", CM_USER): CM_USER_OK})
    assert run(accounts.cryptomus_balances(s, CM_ID, CM_KEY)) == {"USDT": 12.5, "TON": 4.0}


def test_cryptomus_balances_merchant_mode_sums_business_and_personal():
    s = Session({("GET", CM_USER): CM_FAIL, ("POST", CM_MERCHANT): CM_MERCHANT_OK})
    assert run(accounts.cryptomus_balances(s, CM_ID, CM_KEY)) == {"USDT": 105.5, "TON": 2.0}


def test_cryptomus_balances_empty_when_key_not_accepted():
    s = Session({("GET", CM_USER): CM_FAIL, ("POST", CM_MERCHANT): CM_FAIL})
    assert run(accounts.cryptomus_balances(s, CM_ID, CM_KEY)) == {}


def test_portfolio_includes_bingx_and_cryptomus():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    s = Session({("GET", SPOT): {"code": 0, "data": {"balances": [{"asset": "USDT", "free": "7", "locked": "0"},
                                                                  {"asset": "SHIB", "free": "1000", "locked": "0"}]}},
                 ("GET", FUND): {"code": 0, "data": {"balances": []}}, ("GET", CM_USER): CM_USER_OK})
    assert run(accounts.portfolio(s)) == {"bingx": {"USDT": 7.0}, "cryptomus": {"USDT": 12.5, "TON": 4.0}}


# --- история ---

def test_hist_ts_iso_offset_naive_zone_and_z():
    assert accounts._hist_ts("2023-12-14T04:05:02.000+08:00") == datetime(2023, 12, 13, 20, 5, 2,
                                                                          tzinfo=timezone.utc).timestamp()
    assert accounts._hist_ts("2026-09-20 12:00:00", MSK) == datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc).timestamp()
    assert accounts._hist_ts("2023-11-16 00:00:00") == datetime(2023, 11, 16, tzinfo=timezone.utc).timestamp()
    assert accounts._hist_ts("2023-11-16T00:00:00Z") == datetime(2023, 11, 16, tzinfo=timezone.utc).timestamp()
    assert accounts._hist_ts(1700000000000) == 1700000000.0
    assert accounts._hist_ts("мусор") == 0.0 and accounts._hist_ts(None) == 0.0


def test_bingx_coin_strips_network_suffix_only():
    assert accounts._bingx_coin("USDTTRC20", "TRC20") == "USDT"
    assert accounts._bingx_coin("USDTBEP20") == "USDT"          # без поля network — по известной сети
    assert accounts._bingx_coin("USDT", "TRC20") == "USDT"
    assert accounts._bingx_coin("ETHW") == "ETHW" and accounts._bingx_coin("TON", "TON") == "TON"


BX_DEPOSITS = [
    {"coin": "USDTTRC20", "network": "TRC20", "amount": "100", "status": 1, "insertTime": 1700000000000},
    {"coin": "USDT", "network": "BEP20", "amount": "50", "status": 6, "insertTime": 1700000100000},
    {"coin": "USDT", "amount": "7", "status": 0, "insertTime": 1700000200000}]     # ещё в пути — пропускаем
BX_WITHDRAWS = [
    {"id": "1", "coin": "USDT", "amount": "20", "status": 6, "transferType": 1,
     "applyTime": "2023-12-14T04:05:02.000+08:00"},
    {"id": "2", "coin": "USDT", "amount": "5", "status": 6, "transferType": 2,
     "applyTime": "2023-12-14T05:00:00.000+08:00"},
    {"id": "3", "coin": "USDT", "amount": "9", "status": 5, "applyTime": "2023-12-14T06:00:00.000+08:00"}]


def test_bingx_history_completed_only_with_transfer_to_another_user():
    s = Session({("GET", DEPOSITS): BX_DEPOSITS, ("GET", WITHDRAWS): BX_WITHDRAWS})
    hist = run(accounts.bingx_history(s, BX_KEY, BX_SECRET))
    assert hist == [
        {"kind": "transfer_out", "asset": "USDT", "amount": 5.0,
         "ts": datetime(2023, 12, 13, 21, 0, tzinfo=timezone.utc).timestamp()},
        {"kind": "withdraw", "asset": "USDT", "amount": 20.0,
         "ts": datetime(2023, 12, 13, 20, 5, 2, tzinfo=timezone.utc).timestamp()},
        {"kind": "deposit", "asset": "USDT", "amount": 50.0, "ts": 1700000100.0},
        {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1700000000.0}]
    assert all("limit=20" in u for _, u, _, _ in s.calls)


def test_bingx_transfer_to_another_user_is_announced_as_money_leaving():
    """transferType 2 у BingX — перевод другому пользователю (право Withdraw): уведомление не должно звучать как
    безобидное перемещение между своими счетами — это единственная сигнализация владельцу об уходе денег."""
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    s = Session({("GET", DEPOSITS): [], ("GET", WITHDRAWS): []})
    bot = Stub(s)
    run(bot.check_accounts())   # первый опрос — база без сообщений
    s.routes[("GET", WITHDRAWS)] = [{"id": "9", "coin": "USDT", "amount": "500", "status": 6, "transferType": 2,
                                     "applyTime": "2026-09-26T10:00:00.000+08:00"}]
    run(bot.check_accounts())
    assert texts(bot) == ["💰 BingX: списан перевод другому пользователю — 500 USDT"]
    assert "внутренний" not in texts(bot)[0]


def test_bingx_history_empty_and_failure():
    assert run(accounts.bingx_history(Session({("GET", DEPOSITS): [], ("GET", WITHDRAWS): []}), BX_KEY, BX_SECRET)) == []
    s = Session({("GET", DEPOSITS): [], ("GET", WITHDRAWS): {"code": 100413, "msg": "Null apiKey"}})
    assert run(accounts.bingx_history(s, BX_KEY, BX_SECRET)) is None   # пустоту не подтвердить


def test_account_history_dispatches_bingx():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    s = Session({("GET", DEPOSITS): BX_DEPOSITS[:1], ("GET", WITHDRAWS): []})
    assert run(accounts.account_history(s, "bingx")) == [
        {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1700000000.0}]


CM_TX = {"state": 0, "result": {"items": [
    {"uuid": "1", "type": "payment", "status": "paid", "amount": "25", "currency": "USDT",
     "created_at": "2026-09-20 12:00:00"},
    {"uuid": "2", "type": "payout", "status": "paid", "amount": "10", "currency": "GRAM",
     "created_at": "2026-09-21 12:00:00"},
    {"uuid": "3", "type": "transfer", "status": "paid", "amount": "3", "currency": "USDT",
     "created_at": "2026-09-22 12:00:00"},
    {"uuid": "4", "type": "payment", "status": "check", "amount": "99", "currency": "USDT",
     "created_at": "2026-09-23 12:00:00"}], "paginate": {"count": 4, "hasPages": False}}}


def test_cryptomus_history_user_paid_only_msk_time():
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    s = Session({("GET", CM_USER): CM_USER_OK, ("POST", CM_HIST): CM_TX})
    hist = run(accounts.account_history(s, "cryptomus"))
    ts = lambda d: datetime(2026, 9, d, 12, 0, tzinfo=MSK).timestamp()
    assert hist == [{"kind": "transfer", "asset": "USDT", "amount": 3.0, "ts": ts(22)},
                    {"kind": "withdraw", "asset": "TON", "amount": 10.0, "ts": ts(21)},
                    {"kind": "deposit", "asset": "USDT", "amount": 25.0, "ts": ts(20)}]
    method, _, headers, data = s.calls[-1]
    assert (method, data, headers["userId"]) == ("POST", "{}", CM_ID)


def test_cryptomus_history_merchant_or_failure_is_none():
    s = Session({("GET", CM_USER): CM_FAIL, ("POST", CM_MERCHANT): CM_MERCHANT_OK})
    assert run(accounts.cryptomus_history(s, CM_ID, CM_KEY)) is None
    assert ("POST", CM_HIST) not in s.paths()   # у бизнес-ключа историю личного кабинета не запрашиваем
    accounts._CRYPTOMUS_MODE.clear()
    for bad in (CM_FAIL, _http_error(500, "Server Error"), {"state": 0, "result": []}):
        s = Session({("GET", CM_USER): CM_USER_OK, ("POST", CM_HIST): bad})
        assert run(accounts.cryptomus_history(s, CM_ID, CM_KEY)) is None


# --- бот: экраны, ввод ключа, старт, P2P не затронут, гости ---

class Stub(B.Bot):
    def __init__(self, session=None, cfg=None):
        super().__init__(session, "x", "1", cfg or p2p.Config())
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def callbacks(kb):
    return [b.get("callback_data", "") for row in kb["inline_keyboard"] for b in row]


def test_account_view_bingx_and_cryptomus_offer_connect_with_specific_hints():
    text, kb = B.account_view("bingx")
    assert "BingX" in text and "«Read»" in text and "Withdraw" in text and "acc_add:bingx" in callbacks(kb)
    text, kb = B.account_view("cryptomus")
    assert "Cryptomus" in text and "нет ключей только для чтения" in text and "acc_add:cryptomus" in callbacks(kb)
    assert "User ID" in text and "Merchant ID" in text and "Payout key" in text and "ALLOW_UNSAFE_KEYS=1" in text


def test_account_view_connected_cryptomus_masks_id_and_explains_no_readonly():
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    accounts.set_verified("cryptomus", "unsafe", accounts.CRYPTOMUS_KEY_RIGHTS["user"])
    text, kb = B.account_view("cryptomus")
    assert accounts.mask(CM_ID) in text and CM_ID not in text and CM_KEY not in text
    assert "оставлен по твоему решению" in text and "Ключей только для чтения у Cryptomus нет" in text
    assert "acc_check:cryptomus" in callbacks(kb) and "acc_del:cryptomus" in callbacks(kb)


def test_accounts_view_lists_account_only_exchanges_and_unsafe_status():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    accounts.set_verified("bingx", "unsafe", "торговля спот")
    text, kb = B.accounts_view(p2p.Config(exchanges=["bybit", "bestchange"]))
    assert "⚠️ BingX (права сверх чтения" in text and "➖ Cryptomus" in text and "➖ Bybit" in text
    assert callbacks(kb)[:3] == ["acc:bybit", "acc:bingx", "acc:cryptomus"]


def test_bingx_and_cryptomus_are_not_p2p_venues():
    for ex in ("bingx", "cryptomus"):
        assert ex in accounts.ONBOARDABLE and ex in accounts.BALANCE_FETCHERS and ex in accounts.HISTORY_FETCHERS
        assert ex not in p2p.ALL_EXCHANGES.split(",") and ex not in B.EXCHANGE_LIST and ex not in p2p.FETCHERS
        assert ex not in B.EXCHANGE_NAMES and ex not in B.VENUE_NAMES and ex not in p2p.Config().exchanges
    assert B.EXCHANGE_LIST == ("bybit", "mexc", "htx", "kucoin", "bitpapa", "lbank", "bestchange")
    _, kb = B.filters_view(p2p.Config())
    assert not [c for c in callbacks(kb) if c in ("flt_e:bingx", "flt_e:cryptomus")]


def test_cryptomus_key_entry_asks_id_then_api_key_and_keeps_unsafe_key_with_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    bot = Stub(Session({("GET", CM_USER): CM_USER_OK}))
    run(bot.on_callback({"id": "1", "data": "acc_add:cryptomus", "message": {"message_id": 1}}))
    assert bot.awaiting_key == {"ex": "cryptomus", "step": "key"}
    assert "Пришли <b>ID (User ID или Merchant ID, UUID)</b>" in texts(bot)[-1]
    assert "Пришли <b>API key</b>" not in texts(bot)[-1]
    run(bot.handle_key_input(CM_ID, None))
    assert texts(bot)[-1] == "ID получен. Теперь пришли <b>API key</b> для Cryptomus."
    run(bot.handle_key_input(CM_KEY, None))
    assert bot.awaiting_key is None and accounts.keys("cryptomus") == (CM_ID, CM_KEY)
    assert accounts.verify_status("cryptomus")[0] == "unsafe"
    assert any(t.startswith("✅ Подключено. ⚠️ Ключ даёт больше, чем чтение") for t in texts(bot))
    assert not any(CM_KEY in t or CM_ID in t for t in texts(bot))


def test_cryptomus_key_dropped_without_flag_with_honest_advice():
    bot = Stub(Session({("GET", CM_USER): CM_USER_OK}))
    bot.awaiting_key = {"ex": "cryptomus", "step": "secret", "key": CM_ID}
    run(bot.handle_key_input(CM_KEY, None))
    assert accounts.keys("cryptomus") is None
    msg_ = next(t for t in texts(bot) if "удалил его из бота" in t)
    assert "Ключей только для чтения у Cryptomus нет" in msg_ and "ALLOW_UNSAFE_KEYS=1" in msg_
    assert "Создай новый ключ ТОЛЬКО для чтения" not in msg_


def test_bingx_key_entry_keeps_default_labels():
    bot = Stub()
    run(bot.on_callback({"id": "1", "data": "acc_add:bingx", "message": {"message_id": 1}}))
    assert "Пришли <b>API key</b>" in texts(bot)[-1] and "Create API" in texts(bot)[-1]
    run(bot.handle_key_input(BX_KEY, None))
    assert texts(bot)[-1] == "Ключ получен. Теперь пришли <b>secret</b> для BingX."


def _unsafe_exchanges():
    return Session({("GET", CM_USER): CM_USER_OK,
                    ("GET", PERMS): {"code": 0, "data": {"permissions": [1, 2], "ipAddresses": []}}})


def test_startup_with_allow_flag_marks_unsafe_silently_every_start(monkeypatch):
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    for _ in range(2):   # каждый перезапуск бота — ни удаления, ни сообщений
        bot = Stub(_unsafe_exchanges())
        run(bot.check_key_safety())
        assert texts(bot) == []
    assert accounts.keys("bingx") == (BX_KEY, BX_SECRET) and accounts.keys("cryptomus") == (CM_ID, CM_KEY)
    assert accounts.verify_status("bingx") == ("unsafe", "торговля спот; без привязки к IP")
    state, detail = accounts.verify_status("cryptomus")
    assert state == "unsafe" and "нет ключей только для чтения" in detail


def test_startup_without_flag_deletes_both_keys():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    bot = Stub(_unsafe_exchanges())
    run(bot.check_key_safety())
    assert accounts.keys("bingx") is None and accounts.keys("cryptomus") is None
    dropped = [t for t in texts(bot) if "удалил его из бота" in t]
    assert len(dropped) == 2 and any(t.startswith("⚠️ BingX") for t in dropped)
    assert not any(BX_KEY in t or BX_SECRET in t or CM_KEY in t for t in texts(bot))


def test_startup_when_exchanges_unreachable_keeps_bingx_but_still_drops_cryptomus():
    """BingX не ответил — права не проверить, ключ остаётся (не блокируем по недоступности API). Cryptomus без
    ALLOW_UNSAFE_KEYS=1 удаляется всегда: ключей только для чтения у него нет, и сеть тут ни при чём."""
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    s = Session({("GET", CM_USER): _http_error(502, "Bad Gateway"), ("POST", CM_MERCHANT): _http_error(502, "Bad Gateway"),
                 ("GET", PERMS): _http_error(502, "Bad Gateway")})
    bot = Stub(s)
    run(bot.check_key_safety())
    assert accounts.keys("bingx") == (BX_KEY, BX_SECRET) and accounts.keys("cryptomus") is None
    assert len(texts(bot)) == 1 and texts(bot)[0].startswith("⚠️ Cryptomus: ключ даёт больше, чем чтение")
    assert ("GET", CM_USER) not in s.paths() and ("POST", CM_MERCHANT) not in s.paths()   # ключом Cryptomus не ходили
    run(accounts.portfolio(s))   # удалённый ключ больше не используется
    assert ("GET", CM_USER) not in s.paths() and ("POST", CM_MERCHANT) not in s.paths()


@pytest.mark.parametrize("data", ["acc_check:cryptomus", None])
def test_cryptomus_key_dropped_without_flag_even_if_cryptomus_is_down(data):
    """Подключение и «🔄 Проверить» при недоступном Cryptomus: без ALLOW_UNSAFE_KEYS=1 ключ не остаётся «unknown»."""
    s = Session({("GET", CM_USER): _http_error(504, "Gateway Timeout"),
                 ("POST", CM_MERCHANT): _http_error(504, "Gateway Timeout")})
    bot = Stub(s)
    if data:
        accounts.save_key("cryptomus", CM_ID, CM_KEY)
        run(bot.on_callback({"id": "1", "data": data, "message": {"message_id": 1}}))
    else:
        bot.awaiting_key = {"ex": "cryptomus", "step": "secret", "key": CM_ID}
        run(bot.handle_key_input(CM_KEY, None))
    assert accounts.keys("cryptomus") is None and s.calls == []
    assert any("удалил его из бота" in t for t in texts(bot))
    assert not any("проверить не удалось" in t for t in texts(bot))


def test_cryptomus_with_flag_and_cryptomus_down_is_unsafe_and_error_not_unknown(monkeypatch):
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    bot = Stub(Session({("GET", CM_USER): _http_error(504, "Gateway Timeout"),
                        ("POST", CM_MERCHANT): _http_error(504, "Gateway Timeout")}))
    run(bot.check_key_safety())
    assert texts(bot) == [] and accounts.verify_status("cryptomus")[0] == "unsafe"
    run(bot.on_callback({"id": "1", "data": "acc_check:cryptomus", "message": {"message_id": 1}}))
    assert accounts.keys("cryptomus") == (CM_ID, CM_KEY) and accounts.verify_status("cryptomus")[0] == "error"
    assert not any("проверить не удалось" in t for t in texts(bot))


def test_hist_text_and_portfolio_use_account_names():
    it = {"kind": "transfer", "asset": "USDT", "amount": 5.0, "ts": 1.0}
    assert B.hist_text("cryptomus", it) == "💰 Cryptomus: внутренний перевод — 5 USDT"   # между своими кошельками
    rows, _ = B.portfolio_rows({"bingx": {"USDT": 1.0}, "cryptomus": {"TON": 2.0}}, None)
    assert [name for name, _ in rows] == ["BingX", "Cryptomus"]
    empty = B.portfolio_view({}, None)
    assert "BingX" in empty and "Cryptomus" in empty


def test_guest_cannot_see_or_touch_bingx_and_cryptomus():
    accounts.save_key("bingx", BX_KEY, BX_SECRET)
    accounts.save_key("cryptomus", CM_ID, CM_KEY)
    bot = GuestStub(p2p.Config(), guests=["42"])
    for data in ("acc:bingx", "acc:cryptomus", "acc_add:cryptomus", "acc_check:bingx", "acc_check:cryptomus",
                 "acc_del:bingx", "acc_del:cryptomus", "accounts", "balance"):
        before = len(bot.out)
        cq = {"id": "1", "data": data, "message": {"chat": {"id": 42}, "message_id": 5}}
        run(bot.on_update({"callback_query": cq}))
        new = bot.out[before:]
        assert [m for m, _ in new] == ["answerCallbackQuery"] and "владельца" in new[0][1]["text"], data
    run(bot.on_update(msg(42, "/balance")))
    assert sent(bot)[-1]["chat_id"] == "42" and sent(bot)[-1]["text"] == B.GUEST_DENIED
    assert accounts.keys("bingx") is not None and accounts.keys("cryptomus") is not None
    assert bot.awaiting_key is None
