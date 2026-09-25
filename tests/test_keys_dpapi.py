"""Ключи бирж в data/keys.json зашифрованы Windows DPAPI: на диске нет открытых секретов, чтение прозрачно,
файл от прошлой версии шифруется при старте, чужой/испорченный шифр — ключ считается неподключённым.
ФИКТИВНЫЕ ключи, во временной папке (conftest подменяет KEYS_PATH)."""
import json
import sys

import pytest

import accounts

KEY, SECRET, PASS = "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234", "fakepass"
windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI есть только в Windows")


def _raw():
    return json.load(open(accounts.KEYS_PATH, encoding="utf-8"))


def test_roundtrip_through_accounts_api():
    accounts.save_key("kucoin", KEY, SECRET, PASS)
    assert accounts.keys("kucoin") == (KEY, SECRET) and accounts.passphrase("kucoin") == PASS


@windows
def test_no_plaintext_secrets_on_disk():
    accounts.save_key("kucoin", KEY, SECRET, PASS)
    raw = open(accounts.KEYS_PATH, encoding="utf-8").read()
    assert KEY not in raw and SECRET not in raw and PASS not in raw
    entry = _raw()["kucoin"]
    assert all(entry[f].startswith(accounts.DPAPI_PREFIX) for f in ("key", "secret", "passphrase"))


@windows
def test_old_plaintext_file_is_encrypted_on_start_and_still_works():
    open(accounts.KEYS_PATH, "w", encoding="utf-8").write(json.dumps(
        {"bybit": {"key": KEY, "secret": SECRET, "verified": "unsafe", "verified_msg": "Trade"},
         "htx": {"disabled": True}}))
    assert accounts.keys("bybit") == (KEY, SECRET)                 # до миграции — читается как раньше
    assert accounts.encrypt_saved_keys() == 2
    raw = open(accounts.KEYS_PATH, encoding="utf-8").read()
    assert KEY not in raw and SECRET not in raw
    assert accounts.keys("bybit") == (KEY, SECRET)
    assert accounts.verify_status("bybit") == ("unsafe", "Trade")  # остальные поля не тронуты
    assert _raw()["htx"] == {"disabled": True}
    assert accounts.encrypt_saved_keys() == 0                      # повторно — нечего шифровать


@windows
def test_foreign_or_broken_cipher_means_key_not_connected():
    accounts.save_key("bybit", KEY, SECRET)
    data = _raw()
    data["bybit"]["secret"] = accounts.DPAPI_PREFIX + "bm90IGEgcmVhbCBibG9i"   # «не настоящий шифр»
    open(accounts.KEYS_PATH, "w", encoding="utf-8").write(json.dumps(data))
    assert accounts.keys("bybit") is None and accounts.verify_status("bybit") == ("none", "")
    assert accounts.unprotect(accounts.DPAPI_PREFIX + "%%%") is None


def test_protect_is_idempotent_and_handles_empty():
    enc = accounts.protect(SECRET)
    assert accounts.protect(enc) == enc and accounts.unprotect(enc) == SECRET
    assert accounts.protect("") == "" and accounts.unprotect(None) is None
