import asyncio
import hashlib
import hmac
import json

import pytest

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


def test_kucoin_is_onboardable_but_not_yet_connectable():
    assert "kucoin" in accounts.ONBOARDABLE
    assert "kucoin" not in accounts.CONNECTABLE


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
    accounts.save_key("htx", "k", "s")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({}), "htx"))
    assert not ok and "не реализована" in msg


def test_verify_kucoin_saved_but_not_signed_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("kucoin", "k", "s", "pp")
    ok, msg = asyncio.run(accounts.verify(_JsonSession({}), "kucoin"))
    assert not ok and "не реализована" in msg
