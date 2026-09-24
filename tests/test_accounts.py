import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import accounts


def test_bybit_headers_signature_matches_documented_formula():
    # Формула из офиц. примера Bybit v5: sign = HMAC_SHA256(secret, timestamp+api_key+recv_window+query)
    key, secret, ts = "apikey123", "secretxyz", "1700000000000"
    params = {"accountType": "UNIFIED", "coin": "USDT"}
    h = accounts.bybit_headers(key, secret, params=params, timestamp=ts)
    query = "accountType=UNIFIED&coin=USDT"
    expected = hmac.new(secret.encode(), (ts + key + "5000" + query).encode(), hashlib.sha256).hexdigest()
    assert h["X-BAPI-SIGN"] == expected
    assert h["X-BAPI-API-KEY"] == key
    assert h["X-BAPI-TIMESTAMP"] == ts
    assert h["X-BAPI-RECV-WINDOW"] == "5000"
    assert h["X-BAPI-SIGN-TYPE"] == "2"


def test_bybit_headers_no_params_signs_empty_query():
    key, secret, ts = "k", "s", "1700000000000"
    h = accounts.bybit_headers(key, secret, timestamp=ts)
    expected = hmac.new(secret.encode(), (ts + key + "5000").encode(), hashlib.sha256).hexdigest()
    assert h["X-BAPI-SIGN"] == expected


def test_mexc_signed_params_signature_matches_documented_formula():
    # MEXC v3 (как у Binance): signature = HMAC_SHA256(secret, querystring всех параметров вкл. timestamp)
    secret, ts = "secretxyz", "1700000000000"
    signed = accounts.mexc_signed_params(secret, {"symbol": "BTCUSDT"}, timestamp=ts)
    assert signed["timestamp"] == ts
    assert signed["recvWindow"] == "5000"
    unsigned = {k: v for k, v in signed.items() if k != "signature"}
    from urllib.parse import urlencode
    expected = hmac.new(secret.encode(), urlencode(unsigned).encode(), hashlib.sha256).hexdigest()
    assert signed["signature"] == expected


def test_signature_changes_with_secret_or_params():
    ts = "1700000000000"
    a = accounts.bybit_headers("k", "secret1", params={"coin": "USDT"}, timestamp=ts)
    b = accounts.bybit_headers("k", "secret2", params={"coin": "USDT"}, timestamp=ts)
    c = accounts.bybit_headers("k", "secret1", params={"coin": "BTC"}, timestamp=ts)
    assert a["X-BAPI-SIGN"] != b["X-BAPI-SIGN"] != c["X-BAPI-SIGN"]


def test_keys_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such_file.json"))
    for var in ("BYBIT_API_KEY", "BYBIT_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    assert accounts.keys("bybit") is None


def test_keys_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such_file.json"))
    monkeypatch.setenv("MEXC_API_KEY", "envkey")
    monkeypatch.setenv("MEXC_API_SECRET", "envsecret")
    assert accounts.keys("mexc") == ("envkey", "envsecret")


def test_keys_from_file_takes_priority(monkeypatch, tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"bybit": {"key": "filekey", "secret": "filesecret"}}), encoding="utf-8")
    monkeypatch.setattr(accounts, "KEYS_PATH", str(path))
    monkeypatch.setenv("BYBIT_API_KEY", "envkey")
    monkeypatch.setenv("BYBIT_API_SECRET", "envsecret")
    assert accounts.keys("bybit") == ("filekey", "filesecret")


class _FakeResp:
    def __init__(self, url, headers):
        self.url, self.headers = url, headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return {"url": self.url, "headers": self.headers}


class _FakeSession:
    def get(self, url, headers=None):
        return _FakeResp(url, headers)


def test_bybit_get_signs_and_builds_url():
    j = asyncio.run(accounts.bybit_get(_FakeSession(), "k", "s", "/v5/account/wallet-balance",
                                        {"accountType": "UNIFIED"}))
    assert j["url"] == "https://api.bybit.com/v5/account/wallet-balance?accountType=UNIFIED"
    assert j["headers"]["X-BAPI-API-KEY"] == "k"
    assert "X-BAPI-SIGN" in j["headers"]


def test_mexc_get_signs_and_builds_url():
    j = asyncio.run(accounts.mexc_get(_FakeSession(), "k", "s", "/api/v3/account"))
    assert j["url"].startswith("https://api.mexc.com/api/v3/account?")
    assert "signature=" in j["url"]
    assert j["headers"] == {"X-MEXC-APIKEY": "k"}


def test_save_key_then_keys_and_mask(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "sub" / "keys.json"))
    accounts.save_key("bybit", "abcd1234efgh", "secretval")
    assert accounts.keys("bybit") == ("abcd1234efgh", "secretval")
    assert accounts.mask("abcd1234efgh") == "•••efgh"


def test_save_key_overwrites_only_that_exchange(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k1", "s1")
    accounts.save_key("mexc", "k2", "s2")
    assert accounts.keys("bybit") == ("k1", "s1")
    assert accounts.keys("mexc") == ("k2", "s2")


def test_delete_key_removes_saved_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k1", "s1")
    assert accounts.delete_key("bybit") is True
    assert accounts.keys("bybit") is None
    assert accounts.delete_key("bybit") is False


def _clear_env_keys(monkeypatch):
    for ex in ("BYBIT", "MEXC", "HTX", "KUCOIN"):
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
            monkeypatch.delenv(f"{ex}_{suffix}", raising=False)


def test_delete_key_blocks_env_fallback(tmp_path, monkeypatch):
    # ключ и в файле, и в .env: удаление должно выключить оба источника
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    monkeypatch.setenv("BYBIT_API_KEY", "envkey")
    monkeypatch.setenv("BYBIT_API_SECRET", "envsecret")
    accounts.save_key("bybit", "k1", "s1")
    assert accounts.delete_key("bybit") is True
    assert accounts.keys("bybit") is None
    assert accounts._keys_file()["bybit"] == {"disabled": True}


def test_delete_key_env_only_disables(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    monkeypatch.setenv("BYBIT_API_KEY", "envkey")
    monkeypatch.setenv("BYBIT_API_SECRET", "envsecret")
    assert accounts.delete_key("bybit") is True
    assert accounts.keys("bybit") is None
    assert accounts.delete_key("bybit") is False   # уже выключен


def test_delete_key_nothing_returns_false(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    monkeypatch.setattr(accounts, "KEYS_PATH", str(path))
    _clear_env_keys(monkeypatch)
    assert accounts.delete_key("bybit") is False
    assert not path.exists()


def test_save_key_after_delete_reenables(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    monkeypatch.setenv("BYBIT_API_KEY", "envkey")
    monkeypatch.setenv("BYBIT_API_SECRET", "envsecret")
    accounts.delete_key("bybit")
    accounts.save_key("bybit", "k2", "s2")
    assert accounts.keys("bybit") == ("k2", "s2")
    assert "disabled" not in accounts._keys_file()["bybit"]


def test_passphrase_disabled_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    for suffix, value in (("API_KEY", "envkey"), ("API_SECRET", "envsecret"), ("API_PASSPHRASE", "envpass")):
        monkeypatch.setenv(f"KUCOIN_{suffix}", value)
    assert accounts.delete_key("kucoin") is True
    assert accounts.keys("kucoin") is None
    assert accounts.passphrase("kucoin") is None


def test_mask_short_key_falls_back():
    assert accounts.mask("") == "••••"
    assert accounts.mask("abc") == "••••"


def test_save_key_with_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", "pp")
    assert accounts.keys("kucoin") == ("k", "s")
    assert accounts.passphrase("kucoin") == "pp"


def test_save_key_without_passphrase_leaves_it_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    assert accounts.passphrase("bybit") is None


def test_passphrase_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    monkeypatch.setenv("KUCOIN_API_PASSPHRASE", "envpass")
    assert accounts.passphrase("kucoin") == "envpass"


def test_htx_and_kucoin_are_onboardable_and_connectable():
    assert "htx" in accounts.ONBOARDABLE and "htx" in accounts.CONNECTABLE
    assert "kucoin" in accounts.ONBOARDABLE and "kucoin" in accounts.CONNECTABLE
    assert accounts.ONBOARDABLE.count("kucoin") == 1   # kucoin в CONNECTABLE и PASSPHRASE_REQUIRED сразу


def test_htx_signed_params_matches_documented_formula():
    # Формула HTX (Huobi) Signature Version 2: Base64(HMAC_SHA256(secret, METHOD\nhost\npath\nquery))
    key, secret, ts = "apikey123", "secretxyz", "2024-01-01T00:00:00"
    params = accounts.htx_signed_params(key, secret, "GET", "/v1/account/accounts",
                                         {"coin": "usdt"}, timestamp=ts)
    assert params["AccessKeyId"] == key
    assert params["Timestamp"] == ts
    base = {"AccessKeyId": key, "SignatureMethod": "HmacSHA256", "SignatureVersion": "2",
            "Timestamp": ts, "coin": "usdt"}
    query = urlencode(sorted(base.items()))
    payload = "\n".join(["GET", "api.htx.com", "/v1/account/accounts", query])
    expected = base64.b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()).decode()
    assert params["Signature"] == expected


def test_kucoin_headers_matches_documented_formula():
    # Формула KuCoin v2: sign/passphrase = Base64(HMAC_SHA256(secret, ...)), заголовок KC-API-KEY-VERSION=2
    key, secret, pp, ts = "apikey123", "secretxyz", "mypassphrase", "1700000000000"
    h = accounts.kucoin_headers(key, secret, pp, "GET", "/api/v1/accounts", timestamp=ts)
    expected_sign = base64.b64encode(
        hmac.new(secret.encode(), (ts + "GET" + "/api/v1/accounts").encode(), hashlib.sha256).digest()
    ).decode()
    expected_pp = base64.b64encode(hmac.new(secret.encode(), pp.encode(), hashlib.sha256).digest()).decode()
    assert h["KC-API-KEY"] == key
    assert h["KC-API-SIGN"] == expected_sign
    assert h["KC-API-PASSPHRASE"] == expected_pp
    assert h["KC-API-KEY-VERSION"] == "2"
    assert h["KC-API-TIMESTAMP"] == ts


def test_htx_get_signs_and_builds_url():
    j = asyncio.run(accounts.htx_get(_FakeSession(), "k", "s", "/v1/account/accounts"))
    assert j["url"].startswith("https://api.htx.com/v1/account/accounts?")
    assert "Signature=" in j["url"] and "AccessKeyId=k" in j["url"]
    assert j["headers"] == {}


def test_kucoin_get_signs_and_builds_url():
    j = asyncio.run(accounts.kucoin_get(_FakeSession(), "k", "s", "pp", "/api/v1/accounts"))
    assert j["url"] == "https://api.kucoin.com/api/v1/accounts"
    assert j["headers"]["KC-API-KEY"] == "k"
    assert j["headers"]["KC-API-KEY-VERSION"] == "2"


def test_kucoin_get_with_params_signs_path_and_query():
    j = asyncio.run(accounts.kucoin_get(_FakeSession(), "k", "s", "pp", "/api/v1/accounts", {"currency": "USDT"}))
    assert j["url"] == "https://api.kucoin.com/api/v1/accounts?currency=USDT"


class _JsonResp:
    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self.body


class _JsonSession:
    def __init__(self, body):
        self.body = body

    def get(self, url, headers=None):
        return _JsonResp(self.body)


def test_verify_no_keys_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    ok, msg = asyncio.run(accounts.verify(_JsonSession({}), "bybit"))
    assert not ok and "не сохранён" in msg


def test_verify_bybit_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"retCode": 0, "result": {}}), "bybit"))
    assert ok and "чтени" in msg


def test_verify_bybit_bad_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"retCode": 10003, "retMsg": "Invalid api_key"}), "bybit"))
    assert not ok and "Invalid api_key" in msg


def test_verify_mexc_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"balances": []}), "mexc"))
    assert ok


def test_verify_unsupported_exchange(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bitpapa", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({}), "bitpapa"))
    assert not ok and "не реализована" in msg


def test_verify_htx_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"status": "ok", "data": []}), "htx"))
    assert ok and "чтени" in msg


def test_verify_htx_bad_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"status": "error", "err-msg": "Api key not found"}), "htx"))
    assert not ok and "Api key not found" in msg


def test_verify_kucoin_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", "pp")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"code": "200000", "data": []}), "kucoin"))
    assert ok and "чтени" in msg


def test_verify_kucoin_without_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s")   # без passphrase
    ok, msg = asyncio.run(accounts.verify(_JsonSession({}), "kucoin"))
    assert not ok and "passphrase" in msg


def test_verify_kucoin_bad_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", "pp")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({"code": "400003", "msg": "KC-API-KEY not exists"}), "kucoin"))
    assert not ok and "KC-API-KEY not exists" in msg


def _request_info(url):
    u = URL(url)
    return aiohttp.RequestInfo(u, "GET", CIMultiDictProxy(CIMultiDict()), u)


class _HttpErrorSession:
    """raise_for_status() бросает настоящий aiohttp.ClientResponseError с фактическим URL запроса,
    как aiohttp при ответе 4xx/5xx (str() такой ошибки содержит URL целиком)."""
    def __init__(self, status=401, message="Unauthorized"):
        self.status, self.message, self.errors = status, message, []

    def get(self, url, headers=None):
        session = self

        class _Resp(_JsonResp):
            def raise_for_status(self):
                e = aiohttp.ClientResponseError(_request_info(url), (), status=session.status, message=session.message)
                session.errors.append(e)
                raise e

        return _Resp(None)


def test_verify_htx_http_error_hides_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "AKID-TEST-KEY-1234", "s")
    s = _HttpErrorSession()
    ok, msg = asyncio.run(accounts.verify(s, "htx"))
    assert "AKID-TEST-KEY-1234" in str(s.errors[0])   # сама ошибка aiohttp несёт ключ в URL
    assert not ok and "401" in msg
    assert "AKID-TEST-KEY-1234" not in msg and "Signature" not in msg and "https://" not in msg


def test_verify_mexc_http_error_hides_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_HttpErrorSession(429, "Too Many Requests"), "mexc"))
    assert not ok and msg == "HTTP 429: Too Many Requests"


def test_verify_timeout_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")

    class _Slow:
        def get(self, url, headers=None):
            raise asyncio.TimeoutError()

    ok, msg = asyncio.run(accounts.verify(_Slow(), "bybit"))
    assert not ok and "таймаут" in msg   # str(TimeoutError()) пустой — пользователь видел пустое «⚠️ »


_URL_WITH_KEY = "https://api.htx.com/v1/account/accounts?AccessKeyId=AKID-TEST&Signature=abc%3D"


@pytest.mark.parametrize("exc", [
    aiohttp.ContentTypeError(_request_info(_URL_WITH_KEY), (), status=200, message="unexpected mimetype: text/html"),
    aiohttp.InvalidURL(_URL_WITH_KEY),
    RuntimeError(f"boom {_URL_WITH_KEY}"),
    aiohttp.ClientConnectorError(SimpleNamespace(host="api.htx.com", port=443, ssl=True), OSError(1, "refused")),
])
def test_api_error_text_has_no_url_or_query(exc):
    text = accounts.api_error_text(exc)
    assert text and "AKID-TEST" not in text and "https://" not in text and "?" not in text


def test_api_permissions_no_key_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession({}), "bybit"))
    assert safe and detail == ""


def test_api_permissions_bybit_readonly_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    body = {"retCode": 0, "result": {"readOnly": 1, "permissions": {"Spot": [], "Wallet": []}}}
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession(body), "bybit"))
    assert safe and detail == ""


def test_api_permissions_bybit_trade_key_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    body = {"retCode": 0, "result": {"readOnly": 0, "permissions": {"Spot": ["SpotTrade"], "Wallet": []}}}
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession(body), "bybit"))
    assert not safe and "Spot" in detail


def test_api_permissions_mexc_readonly_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    body = {"canTrade": False, "canWithdraw": False, "balances": []}
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession(body), "mexc"))
    assert safe and detail == ""


def test_api_permissions_mexc_withdraw_key_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    body = {"canTrade": False, "canWithdraw": True, "balances": []}
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession(body), "mexc"))
    assert not safe and "вывод" in detail


class _HtxKeySession:
    """Заглушка HTX: /v2/user/uid -> uid; /v2/user/api-key без uid отвечает ошибкой параметра, как биржа."""
    def __init__(self, permission, uid_body=None):
        self.permission, self.urls = permission, []
        self.uid_body = uid_body or {"code": 200, "data": 123456}

    def get(self, url, headers=None):
        self.urls.append(url)
        if "/v2/user/uid?" in url:
            return _JsonResp(self.uid_body)
        if "/v2/user/api-key?" in url:
            if "uid=123456" not in url:
                return _JsonResp({"code": 2002, "message": "invalid.parameter"})
            return _JsonResp({"code": 200, "data": [{"accessKey": "k", "permission": self.permission}]})
        raise AssertionError(f"unexpected url: {url}")


def test_api_permissions_htx_readonly_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "k", "s")
    safe, detail = asyncio.run(accounts.api_permissions(_HtxKeySession("readOnly"), "htx"))
    assert safe and detail == ""


def test_api_permissions_htx_trade_key_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "k", "s")
    s = _HtxKeySession("readOnly,trade,withdraw")
    safe, detail = asyncio.run(accounts.api_permissions(s, "htx"))
    # сначала uid, затем api-key с uid в подписанной query
    assert [u.split("?")[0] for u in s.urls] == ["https://api.htx.com/v2/user/uid",
                                                  "https://api.htx.com/v2/user/api-key"]
    assert "uid=123456" in s.urls[1] and "Signature=" in s.urls[1]
    assert not safe and detail == "trade, withdraw"


def test_api_permissions_htx_uid_error_fails_open(tmp_path, monkeypatch):
    """Не удалось узнать uid (сбой/нет прав) — ключ не блокируем, как и при других ошибках проверки."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "k", "s")
    s = _HtxKeySession("readOnly,trade", uid_body={"code": 1002, "message": "unauthorized"})
    safe, detail = asyncio.run(accounts.api_permissions(s, "htx"))
    assert safe and detail == ""
    assert len(s.urls) == 1   # api-key без uid не запрашиваем


def test_api_permissions_kucoin_general_only_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", "pp")
    body = {"code": "200000", "data": {"permission": "General"}}
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession(body), "kucoin"))
    assert safe and detail == ""


def test_api_permissions_kucoin_spot_key_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", "pp")
    body = {"code": "200000", "data": {"permission": "General,Spot,Withdraw"}}
    safe, detail = asyncio.run(accounts.api_permissions(_JsonSession(body), "kucoin"))
    assert not safe and "Spot" in detail and "Withdraw" in detail


class _UrlJsonSession:
    """Возвращает тело по подстроке в URL — для эндпоинтов, которые запрашиваются несколько раз подряд."""
    def __init__(self, by_substr):
        self.by_substr = by_substr

    def get(self, url, headers=None):
        for substr, body in self.by_substr.items():
            if substr in url:
                return _JsonResp(body)
        raise AssertionError(f"unexpected url: {url}")


def test_bybit_balances_sums_unified_and_funding():
    session = _UrlJsonSession({
        "accountType=UNIFIED": {"retCode": 0, "result": {"list": [
            {"coin": [{"coin": "USDT", "walletBalance": "10.5"}, {"coin": "BTC", "walletBalance": "0"}]}]}},
        "accountType=FUND": {"retCode": 0, "result": {"balance": [
            {"coin": "USDT", "walletBalance": "2.5"}, {"coin": "TON", "walletBalance": "3"}]}},
    })
    bal = asyncio.run(accounts.bybit_balances(session, "k", "s"))
    assert bal == {"USDT": 13.0, "TON": 3.0}   # нулевой BTC не попадает в результат


def test_bybit_balances_ignores_failed_call():
    session = _UrlJsonSession({
        "accountType=UNIFIED": {"retCode": 10003, "retMsg": "Invalid api_key"},
        "accountType=FUND": {"retCode": 0, "result": {"balance": [{"coin": "USDT", "walletBalance": "1"}]}},
    })
    bal = asyncio.run(accounts.bybit_balances(session, "k", "s"))
    assert bal == {"USDT": 1.0}


def test_mexc_balances_sums_free_and_locked():
    session = _JsonSession({"balances": [{"asset": "USDT", "free": "5", "locked": "1.5"},
                                          {"asset": "ETH", "free": "0", "locked": "0"}]})
    bal = asyncio.run(accounts.mexc_balances(session, "k", "s"))
    assert bal == {"USDT": 6.5}   # нулевой ETH не попадает в результат


def test_portfolio_skips_exchange_without_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    port = asyncio.run(accounts.portfolio(_JsonSession({})))
    assert port == {}


def test_portfolio_filters_to_balance_coins(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    body = {"balances": [{"asset": "USDT", "free": "10", "locked": "0"}, {"asset": "SHIB", "free": "1000", "locked": "0"}]}
    port = asyncio.run(accounts.portfolio(_JsonSession(body)))
    assert port == {"mexc": {"USDT": 10.0}}   # SHIB не в BALANCE_COINS


def test_portfolio_skips_exchange_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")

    class _Boom:
        def get(self, url, headers=None):
            raise RuntimeError("network down")

    assert asyncio.run(accounts.portfolio(_Boom())) == {}


class _FakePostResp:
    def __init__(self, url, headers, data):
        self.url, self.headers, self.data = url, headers, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return {"url": self.url, "headers": self.headers, "data": self.data}


class _FakePostSession:
    def post(self, url, headers=None, data=None):
        return _FakePostResp(url, headers, data)


def test_bybit_post_signs_body():
    ts = "1700000000000"
    j = asyncio.run(accounts.bybit_post(_FakePostSession(), "k", "s", "/v5/p2p/order/simplifyList",
                                         {"page": 1, "size": 20}, timestamp=ts))
    assert j["url"] == "https://api.bybit.com/v5/p2p/order/simplifyList"
    assert j["headers"]["X-BAPI-API-KEY"] == "k"
    payload = j["data"]
    expected = hmac.new("s".encode(), (ts + "k" + "5000" + payload).encode(), hashlib.sha256).hexdigest()
    assert j["headers"]["X-BAPI-SIGN"] == expected


class _P2pSession:
    def __init__(self, body):
        self.body = body

    def post(self, url, headers=None, data=None):
        return _JsonResp(self.body)


def test_bybit_p2p_orders_parses_completed_items():
    # Контракт /v5/p2p/order/simplifyList: ret_code (snake_case), side 0 = Buy / 1 = Sell,
    # amount — сумма в фиате, количество монеты — notifyTokenQuantity
    body = {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"count": 2, "items": [
        {"id": "1", "side": 1, "tokenId": "USDT", "currencyId": "EUR", "amount": "64.400", "price": "0.920",
         "notifyTokenQuantity": "70.0000", "status": 50, "createDate": "1700000000000"},
        {"id": "2", "side": 0, "tokenId": "USDT", "currencyId": "RUB", "amount": "9500", "price": "95",
         "notifyTokenQuantity": "100", "status": 50, "createDate": "1700000001000"},
    ]}}
    orders = asyncio.run(accounts.bybit_p2p_orders(_P2pSession(body), "k", "s"))
    assert orders == [
        {"id": "1", "side": "sell", "asset": "USDT", "fiat": "EUR", "amount": 70.0, "price": 0.92, "ts": 1700000000.0},
        {"id": "2", "side": "buy", "asset": "USDT", "fiat": "RUB", "amount": 100.0, "price": 95.0, "ts": 1700000001.0},
    ]


def test_bybit_p2p_orders_documented_example():
    # Пример ответа из документации Bybit P2P (Get All Orders), поля как есть
    body = {
        "ret_code": 0, "ret_msg": "SUCCESS", "ext_code": "", "ext_info": {}, "time_now": "1741774253.840364",
        "result": {"count": 1, "items": [{
            "id": "1899742990873296896", "side": 1, "tokenId": "USDT", "orderType": "ORIGIN",
            "amount": "64.400", "currencyId": "EUR", "price": "0.920", "notifyTokenQuantity": "70.0000",
            "notifyTokenId": "USDT", "fee": "0", "status": 50, "createDate": "1741769000000",
        }]},
    }
    orders = asyncio.run(accounts.bybit_p2p_orders(_P2pSession(body), "k", "s"))
    assert orders == [{"id": "1899742990873296896", "side": "sell", "asset": "USDT", "fiat": "EUR",
                       "amount": 70.0, "price": 0.92, "ts": 1741769000.0}]


def test_bybit_p2p_orders_accepts_camel_retcode():
    body = {"retCode": 0, "result": {"items": []}}
    assert asyncio.run(accounts.bybit_p2p_orders(_P2pSession(body), "k", "s")) == []


def test_bybit_p2p_orders_quantity_falls_back_to_amount_over_price():
    # нет notifyTokenQuantity/quantity — количество монеты = сумма в фиате / цена
    body = {"ret_code": 0, "result": {"items": [
        {"id": "1", "side": 0, "tokenId": "USDT", "currencyId": "EUR", "amount": "64.4", "price": "0.92",
         "createDate": "1700000000000"},
        {"id": "2", "side": 0, "tokenId": "USDT", "currencyId": "EUR", "amount": "64.4", "price": "0",
         "createDate": "1700000000000"},
    ]}}
    orders = asyncio.run(accounts.bybit_p2p_orders(_P2pSession(body), "k", "s"))
    assert orders[0]["amount"] == pytest.approx(70.0)
    assert orders[1]["amount"] == 0.0   # цены нет — не делим на ноль


def test_bybit_p2p_orders_returns_none_on_bad_retcode():
    body = {"ret_code": 10005, "ret_msg": "Permission denied"}
    assert asyncio.run(accounts.bybit_p2p_orders(_P2pSession(body), "k", "s")) is None


def test_bybit_p2p_orders_returns_none_on_error():
    class _Boom:
        def post(self, url, headers=None, data=None):
            raise RuntimeError("network down")

    assert asyncio.run(accounts.bybit_p2p_orders(_Boom(), "k", "s")) is None


def test_mexc_history_merges_deposits_and_withdrawals_by_time():
    session = _UrlJsonSession({
        "capital/deposit/hisrec": [{"coin": "USDT", "amount": "100.5", "insertTime": 1700000000000}],
        "capital/withdraw/history": [{"coin": "USDT", "amount": "9", "applyTime": "2023-11-16 00:00:00"}],
    })
    hist = asyncio.run(accounts.mexc_history(session, "k", "s"))
    assert hist == [
        {"kind": "withdraw", "asset": "USDT", "amount": 9.0,
         "ts": datetime(2023, 11, 16, 0, 0, 0, tzinfo=timezone.utc).timestamp()},
        {"kind": "deposit", "asset": "USDT", "amount": 100.5, "ts": 1700000000.0},
    ]


def test_mexc_history_falls_back_to_withdrawals_when_no_deposits():
    session = _UrlJsonSession({
        "capital/deposit/hisrec": [],
        "capital/withdraw/history": [{"coin": "USDT", "amount": "9", "applyTime": "2023-11-14 22:13:20"}],
    })
    hist = asyncio.run(accounts.mexc_history(session, "k", "s"))
    assert hist == [{"kind": "withdraw", "asset": "USDT", "amount": 9.0,
                     "ts": datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc).timestamp()}]


def test_mexc_history_returns_empty_list_when_both_sources_empty():
    session = _UrlJsonSession({"capital/deposit/hisrec": [], "capital/withdraw/history": []})
    assert asyncio.run(accounts.mexc_history(session, "k", "s")) == []


def test_mexc_history_returns_none_when_a_source_fails():
    """Депозитов нет, а выводы не ответили — пустоту подтвердить нечем, это не «история пуста»."""
    session = _UrlJsonSession({"capital/deposit/hisrec": [],
                               "capital/withdraw/history": {"code": 700002, "msg": "Signature for this request is not valid."}})
    assert asyncio.run(accounts.mexc_history(session, "k", "s")) is None


def test_htx_history_merges_deposits_and_withdrawals_by_time():
    session = _UrlJsonSession({
        "type=deposit": {"status": "ok", "data": [{"currency": "usdt", "amount": 50, "created-at": 1700000000000}]},
        "type=withdraw": {"status": "ok", "data": [{"currency": "usdt", "amount": 5, "created-at": 1700000009000}]},
    })
    hist = asyncio.run(accounts.htx_history(session, "k", "s"))
    assert hist == [
        {"kind": "withdraw", "asset": "usdt", "amount": 5.0, "ts": 1700000009.0},
        {"kind": "deposit", "asset": "usdt", "amount": 50.0, "ts": 1700000000.0},
    ]


def test_htx_history_returns_empty_list_when_both_sources_empty():
    session = _UrlJsonSession({"type=deposit": {"status": "ok", "data": []}, "type=withdraw": {"status": "ok", "data": []}})
    assert asyncio.run(accounts.htx_history(session, "k", "s")) == []


def test_htx_history_returns_none_when_one_source_fails():
    session = _UrlJsonSession({"type=deposit": {"status": "ok", "data": []},
                               "type=withdraw": {"status": "error", "err-msg": "no permission"}})
    assert asyncio.run(accounts.htx_history(session, "k", "s")) is None


def test_htx_history_returns_none_on_error_status():
    session = _UrlJsonSession({
        "type=deposit": {"status": "error", "err-msg": "no permission"},
        "type=withdraw": {"status": "error", "err-msg": "no permission"},
    })
    assert asyncio.run(accounts.htx_history(session, "k", "s")) is None


def test_kucoin_history_reads_paginated_items():
    session = _UrlJsonSession({
        "api/v1/deposits": {"code": "200000", "data": {"items": [
            {"currency": "USDT", "amount": "30", "createdAt": 1700000000000}]}},
        "api/v1/withdrawals": {"code": "200000", "data": {"items": []}},
    })
    hist = asyncio.run(accounts.kucoin_history(session, "k", "s", "pp"))
    assert hist == [{"kind": "deposit", "asset": "USDT", "amount": 30.0, "ts": 1700000000.0}]


def test_kucoin_history_merges_deposits_and_withdrawals_by_time():
    session = _UrlJsonSession({
        "api/v1/deposits": {"code": "200000", "data": {"items": [
            {"currency": "USDT", "amount": "30", "createdAt": 1700000000000}]}},
        "api/v1/withdrawals": {"code": "200000", "data": {"items": [
            {"currency": "USDT", "amount": "12", "createdAt": 1700000005000}]}},
    })
    hist = asyncio.run(accounts.kucoin_history(session, "k", "s", "pp"))
    assert hist == [
        {"kind": "withdraw", "asset": "USDT", "amount": 12.0, "ts": 1700000005.0},
        {"kind": "deposit", "asset": "USDT", "amount": 30.0, "ts": 1700000000.0},
    ]


def test_kucoin_history_empty_success_and_error():
    empty = _JsonSession({"code": "200000", "data": {"items": []}})
    assert asyncio.run(accounts.kucoin_history(empty, "k", "s", "pp")) == []
    bad = _JsonSession({"code": "400003", "msg": "KC-API-KEY not exists"})
    assert asyncio.run(accounts.kucoin_history(bad, "k", "s", "pp")) is None


def test_mexc_spot_trades_merges_symbols_and_sorts_by_time():
    session = _UrlJsonSession({
        "symbol=USDCUSDT": [],
        "symbol=BTCUSDT": [{"isBuyer": True, "qty": "0.001", "price": "60000", "time": 1700000000000}],
        "symbol=ETHUSDT": [{"isBuyer": False, "qty": "0.2", "price": "3000", "time": 1700000005000}],
        "symbol=TONUSDT": [],
    })
    hist = asyncio.run(accounts.mexc_spot_trades(session, "k", "s"))
    assert hist == [
        {"kind": "trade", "asset": "ETH", "side": "sell", "amount": 0.2, "price": 3000.0, "ts": 1700000005.0},
        {"kind": "trade", "asset": "BTC", "side": "buy", "amount": 0.001, "price": 60000.0, "ts": 1700000000.0},
    ]


def test_mexc_spot_trades_returns_empty_list_when_no_symbol_has_trades():
    session = _UrlJsonSession({sym: [] for sym in accounts.SPOT_TRADE_SYMBOLS})
    assert asyncio.run(accounts.mexc_spot_trades(session, "k", "s")) == []


def test_mexc_spot_trades_returns_none_when_a_symbol_fails_and_no_trades():
    bodies = {f"symbol={sym}": [] for sym in accounts.SPOT_TRADE_SYMBOLS}
    bodies["symbol=TONUSDT"] = {"code": 10007, "msg": "bad symbol"}
    assert asyncio.run(accounts.mexc_spot_trades(_UrlJsonSession(bodies), "k", "s")) is None


def test_kucoin_spot_trades_reads_fills_without_symbol():
    session = _JsonSession({"code": "200000", "data": {"items": [
        {"symbol": "TON-USDT", "side": "buy", "size": "12.5", "price": "5.1", "createdAt": 1700000000000}]}})
    hist = asyncio.run(accounts.kucoin_spot_trades(session, "k", "s", "pp"))
    assert hist == [{"kind": "trade", "asset": "TON", "side": "buy", "amount": 12.5, "price": 5.1, "ts": 1700000000.0}]


def test_kucoin_spot_trades_returns_empty_list_when_no_items():
    session = _JsonSession({"code": "200000", "data": {"items": []}})
    assert asyncio.run(accounts.kucoin_spot_trades(session, "k", "s", "pp")) == []


def test_kucoin_spot_trades_returns_none_on_error_code():
    session = _JsonSession({"code": "400003", "msg": "KC-API-KEY not exists"})
    assert asyncio.run(accounts.kucoin_spot_trades(session, "k", "s", "pp")) is None


def test_account_history_mexc_falls_back_to_spot_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    session = _UrlJsonSession({
        "capital/deposit/hisrec": [],
        "capital/withdraw/history": [],
        "symbol=USDCUSDT": [],
        "symbol=BTCUSDT": [{"isBuyer": True, "qty": "0.001", "price": "60000", "time": 1700000000000}],
        "symbol=ETHUSDT": [],
        "symbol=TONUSDT": [],
    })
    hist = asyncio.run(accounts.account_history(session, "mexc"))
    assert hist == [{"kind": "trade", "asset": "BTC", "side": "buy", "amount": 0.001, "price": 60000.0, "ts": 1700000000.0}]


def test_account_history_kucoin_falls_back_to_spot_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", passphrase="pp")
    session = _UrlJsonSession({
        "api/v1/deposits": {"code": "200000", "data": {"items": []}},
        "api/v1/withdrawals": {"code": "200000", "data": {"items": []}},
        "api/v1/fills": {"code": "200000", "data": {"items": [
            {"symbol": "TON-USDT", "side": "sell", "size": "3", "price": "5.2", "createdAt": 1700000000000}]}},
    })
    hist = asyncio.run(accounts.account_history(session, "kucoin"))
    assert hist == [{"kind": "trade", "asset": "TON", "side": "sell", "amount": 3.0, "price": 5.2, "ts": 1700000000.0}]


def test_account_history_mexc_does_not_hide_fresh_trade_behind_old_deposit(tmp_path, monkeypatch):
    """Старый депозит не должен скрывать более свежую спот-сделку — источники объединяются, а не
    берётся первый непустой (до фикса при непустых депозитах спот-сделки вообще не запрашивались)."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    session = _UrlJsonSession({
        "capital/deposit/hisrec": [{"coin": "USDT", "amount": "100.5", "insertTime": 1700000000000}],
        "capital/withdraw/history": [],
        "symbol=USDCUSDT": [],
        "symbol=BTCUSDT": [{"isBuyer": True, "qty": "0.001", "price": "60000", "time": 1700000009000}],
        "symbol=ETHUSDT": [],
        "symbol=TONUSDT": [],
    })
    hist = asyncio.run(accounts.account_history(session, "mexc"))
    assert hist == [
        {"kind": "trade", "asset": "BTC", "side": "buy", "amount": 0.001, "price": 60000.0, "ts": 1700000009.0},
        {"kind": "deposit", "asset": "USDT", "amount": 100.5, "ts": 1700000000.0},
    ]


def test_account_history_kucoin_does_not_hide_fresh_trade_behind_old_deposit(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", passphrase="pp")
    session = _UrlJsonSession({
        "api/v1/deposits": {"code": "200000", "data": {"items": [
            {"currency": "USDT", "amount": "30", "createdAt": 1700000000000}]}},
        "api/v1/withdrawals": {"code": "200000", "data": {"items": []}},
        "api/v1/fills": {"code": "200000", "data": {"items": [
            {"symbol": "TON-USDT", "side": "sell", "size": "3", "price": "5.2", "createdAt": 1700000009000}]}},
    })
    hist = asyncio.run(accounts.account_history(session, "kucoin"))
    assert hist == [
        {"kind": "trade", "asset": "TON", "side": "sell", "amount": 3.0, "price": 5.2, "ts": 1700000009.0},
        {"kind": "deposit", "asset": "USDT", "amount": 30.0, "ts": 1700000000.0},
    ]


MEXC_EMPTY = {"capital/deposit/hisrec": [], "capital/withdraw/history": [],
              **{f"symbol={sym}": [] for sym in accounts.SPOT_TRADE_SYMBOLS}}


def test_account_history_mexc_empty_success_returns_empty_list(tmp_path, monkeypatch):
    """Все источники MEXC ответили пусто — [], чтобы бот считал это первым опросом (см. check_accounts)."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    assert asyncio.run(accounts.account_history(_UrlJsonSession(MEXC_EMPTY), "mexc")) == []


def test_account_history_mexc_partial_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    err = {"code": 700002, "msg": "Signature for this request is not valid."}
    for key in ("capital/withdraw/history", "symbol=BTCUSDT"):   # не ответили выводы / одна из спот-пар
        bodies = dict(MEXC_EMPTY, **{key: err})
        assert asyncio.run(accounts.account_history(_UrlJsonSession(bodies), "mexc")) is None, key
    all_fail = {k: err for k in MEXC_EMPTY}
    assert asyncio.run(accounts.account_history(_UrlJsonSession(all_fail), "mexc")) is None


def test_account_history_kucoin_empty_success_and_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", passphrase="pp")
    empty = {"code": "200000", "data": {"items": []}}
    err = {"code": "400003", "msg": "KC-API-KEY not exists"}
    bodies = {"api/v1/deposits": empty, "api/v1/withdrawals": empty, "api/v1/fills": empty}
    assert asyncio.run(accounts.account_history(_UrlJsonSession(bodies), "kucoin")) == []
    for key in ("api/v1/withdrawals", "api/v1/fills"):
        failed = dict(bodies, **{key: err})
        assert asyncio.run(accounts.account_history(_UrlJsonSession(failed), "kucoin")) is None, key


def test_account_history_dispatches_bybit_to_p2p_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    body = {"ret_code": 0, "ret_msg": "SUCCESS", "result": {"items": [
        {"id": "1", "side": 0, "tokenId": "USDT", "currencyId": "RUB", "amount": "950", "price": "95",
         "notifyTokenQuantity": "10", "createDate": "1700000000000"}]}}
    hist = asyncio.run(accounts.account_history(_P2pSession(body), "bybit"))
    assert hist[0]["id"] == "1" and hist[0]["side"] == "buy" and hist[0]["amount"] == 10.0


def test_account_history_returns_none_without_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    assert asyncio.run(accounts.account_history(_JsonSession({}), "mexc")) is None


def test_account_history_kucoin_without_passphrase_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s")   # без passphrase
    assert asyncio.run(accounts.account_history(_JsonSession({}), "kucoin")) is None


def test_api_permissions_fails_open_when_api_errors(tmp_path, monkeypatch):
    """Если проверку прав нельзя выполнить (ошибка сети/формата) — не блокируем уже сохранённый ключ."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")

    class _Boom:
        def get(self, url, headers=None):
            raise RuntimeError("network down")

    safe, detail = asyncio.run(accounts.api_permissions(_Boom(), "bybit"))
    assert safe and detail == ""


# key_permissions: «не удалось проверить» (None) отличается от подтверждённого «только чтение» (True)

def test_key_permissions_no_key_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    assert asyncio.run(accounts.key_permissions(_JsonSession({}), "bybit")) == (None, "")


def test_key_permissions_bybit_readonly_is_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    body = {"retCode": 0, "result": {"readOnly": 1, "permissions": {"Spot": [], "Wallet": []}}}
    assert asyncio.run(accounts.key_permissions(_JsonSession(body), "bybit")) == (True, "")


def test_key_permissions_bybit_api_error_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    body = {"retCode": 10005, "retMsg": "Permission denied"}
    assert asyncio.run(accounts.key_permissions(_JsonSession(body), "bybit")) == (None, "")
    assert asyncio.run(accounts.api_permissions(_JsonSession(body), "bybit")) == (True, "")   # старт — fail-open


def test_key_permissions_network_error_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")

    class _Boom:
        def get(self, url, headers=None):
            raise RuntimeError("network down")

    assert asyncio.run(accounts.key_permissions(_Boom(), "bybit")) == (None, "")


def test_key_permissions_mexc_error_body_is_unknown(tmp_path, monkeypatch):
    """Ответ MEXC с ошибкой подписи не содержит canTrade/canWithdraw — это не «только чтение»."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    body = {"code": 700002, "msg": "Signature for this request is not valid."}
    assert asyncio.run(accounts.key_permissions(_JsonSession(body), "mexc")) == (None, "")
    assert asyncio.run(accounts.api_permissions(_JsonSession(body), "mexc")) == (True, "")


def test_key_permissions_mexc_readonly_and_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    ro = {"canTrade": False, "canWithdraw": False, "balances": []}
    trade = {"canTrade": True, "canWithdraw": True, "balances": []}
    assert asyncio.run(accounts.key_permissions(_JsonSession(ro), "mexc")) == (True, "")
    assert asyncio.run(accounts.key_permissions(_JsonSession(trade), "mexc")) == (False, "торговля, вывод")


def test_key_permissions_htx_unknown_key_and_kucoin_no_passphrase_are_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "k", "s")
    accounts.save_key("kucoin", "k", "s")   # без passphrase
    other = {"code": 200, "data": [{"accessKey": "другой", "permission": "readOnly"}]}
    assert asyncio.run(accounts.key_permissions(_JsonSession(other), "htx")) == (None, "")
    assert asyncio.run(accounts.key_permissions(_JsonSession({}), "kucoin")) == (None, "")
