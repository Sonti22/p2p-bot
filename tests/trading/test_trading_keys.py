"""Торговое ядро, keys: ключи bybit_trade / bingx_trade (основной аккаунт, только торговля), отказ от ключей с правом
вывода/переводов и от непроверяемых; скрипт ввода ключей владельцем (getpass, DPAPI), без Telegram. Сеть — заглушка,
ключи — фиктивные."""
import importlib.util
import json
import os
import sys

import pytest

import accounts
from trading import keys, venues
from trading_stubs import CREDS, KEY, SECRET, Resp, Session, bingx_ok, bybit_err, bybit_ok, run

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SPEC = importlib.util.spec_from_file_location("trading_keys_script", os.path.join(ROOT, "scripts", "trading_keys.py"))
script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(script)

TRADE_ONLY = {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "Wallet": [], "Options": [],
              "Derivatives": [], "CopyTrading": [], "BlockTrade": [], "Exchange": [], "NFT": [], "Affiliate": [],
              "Earn": [], "FiatP2P": []}


def bybit_result(**over):
    r = {"id": "1", "apiKey": "x", "readOnly": 0, "permissions": dict(TRADE_ONLY), "ips": ["203.0.113.5"],
         "isMaster": True, "uta": 1}
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def _keys_env(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    for name in ("BYBIT_TRADE_API_KEY", "BYBIT_TRADE_API_SECRET", "BINGX_TRADE_API_KEY", "BINGX_TRADE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)


def test_key_names_not_connectable_from_telegram(monkeypatch):
    assert keys.KEY_NAMES == {"bybit": "bybit_trade", "bingx": "bingx_trade"}
    for name in keys.KEY_NAMES.values():
        assert name not in accounts.CONNECTABLE and name not in accounts.ONBOARDABLE
    assert keys.credentials("bybit") is None
    monkeypatch.setenv("BYBIT_TRADE_API_KEY", KEY)
    monkeypatch.setenv("BYBIT_TRADE_API_SECRET", SECRET)
    assert keys.credentials("bybit") == CREDS and keys.credentials("bingx") is None
    assert accounts.keys("bybit") is None                                     # read-only ключ — отдельно


def test_bybit_trade_only_key_ok():
    k = keys.bybit_rights(bybit_result())
    assert k.ok and k.state == "ok" and k.ip_bound and k.detail == ""
    k = keys.bybit_rights(bybit_result(ips=[]))
    assert k.ok and not k.ip_bound and "без привязки к IP" in k.detail
    assert not keys.bybit_rights(bybit_result(ips=["*"])).ip_bound


@pytest.mark.parametrize("group,rights", [
    ("Wallet", ["Withdraw"]), ("Wallet", ["AccountTransfer"]), ("Wallet", ["SubMemberTransfer"]),
    ("Wallet", ["SubMemberTransferList"]), ("FiatP2P", ["FiatP2POrder"]), ("FiatP2P", ["Advertising"]),
    ("Earn", ["Earn"]), ("Exchange", ["ExchangeOrder"]), ("Options", ["OptionsTrade"]), ("BlockTrade", ["x"]),
    ("Affiliate", ["x"]), ("FiatBitPay", ["FaitPayOrder"]), ("FiatConvertBroker", ["FiatConvertBrokerOrder"]),
    ("BitCard", ["BitCard"]), ("ByXPost", ["ByXPost"]), ("NewUnknownGroup", ["Anything"]),
    ("ContractTrade", ["Order", "Position", "Transfer"]),
])
def test_bybit_extra_rights_refused(group, rights):
    perms = dict(TRADE_ONLY, **{group: rights})
    k = keys.bybit_rights(bybit_result(permissions=perms))
    assert not k.ok and k.state == "unsafe" and group in k.detail


@pytest.mark.parametrize("over,state", [
    ({"readOnly": 1}, "unusable"), ({"permissions": dict(TRADE_ONLY, ContractTrade=["Order"])}, "unusable"),
    ({"permissions": dict(TRADE_ONLY, ContractTrade=[])}, "unusable"), ({"readOnly": None}, "unknown"),
    ({"permissions": None}, "unknown"), ({"permissions": dict(TRADE_ONLY, Wallet="Withdraw")}, "unknown"),
    ({"permissions": dict(TRADE_ONLY, Wallet=[1])}, "unknown"),
])
def test_bybit_unusable_or_unknown_refused(over, state):
    k = keys.bybit_rights(bybit_result(**over))
    assert not k.ok and k.state == state


def test_bybit_readonly_with_withdraw_is_unsafe_not_unusable():
    k = keys.bybit_rights(bybit_result(readOnly=1, permissions=dict(TRADE_ONLY, Wallet=["Withdraw"])))
    assert k.state == "unsafe"


@pytest.mark.parametrize("data,ok,state", [
    ({"apiKey": "x", "permissions": [2, 3], "ipAddresses": ["1.2.3.4"]}, True, "ok"),
    ({"apiKey": "x", "permissions": [1, 2, 3], "ipAddresses": ["1.2.3.4"]}, True, "ok"),
    ({"apiKey": "x", "permissions": "2,3", "ipAddresses": []}, True, "ok"),
    ({"apiKey": "x", "permissions": [2, 3, 5]}, False, "unsafe"),        # 5 — вывод
    ({"apiKey": "x", "permissions": [2, 3, 4]}, False, "unsafe"),        # 4 — перевод между счетами
    ({"apiKey": "x", "permissions": [2, 3, 7]}, False, "unsafe"),        # 7 — между субаккаунтами
    ({"apiKey": "x", "permissions": [2, 3, 9]}, False, "unsafe"),        # незнакомое право
    ({"apiKey": "x", "permissions": [2, 3], "enableWithdrawals": True}, False, "unsafe"),
    ({"apiKey": "x", "permissions": [2]}, False, "unusable"),
    ({"apiKey": "x", "permissions": [1, 2]}, False, "unusable"),
    ({"apiKey": "x", "permissions": ["x"]}, False, "unknown"), ({"apiKey": "x", "permissions": {}}, False, "unknown"),
    ({"apiKey": "x", "permissions": [2, 3], "enableFutures": "maybe"}, False, "unknown"),
    ({"enableReading": True, "enableSpotAndMarginTrading": False, "enableFutures": True,
      "permitsUniversalTransfer": False, "enableWithdrawals": False, "enableInternalTransfer": False,
      "ipRestrict": True}, True, "ok"),
    ({"enableReading": True, "enableSpotAndMarginTrading": False, "enableFutures": True,
      "permitsUniversalTransfer": False, "enableWithdrawals": True, "enableInternalTransfer": False}, False, "unsafe"),
    ({"enableReading": True, "enableFutures": True}, False, "unknown"),  # неполный набор — не проверить
    ({"enableReading": True, "enableSpotAndMarginTrading": False, "enableFutures": False,
      "permitsUniversalTransfer": False, "enableWithdrawals": False, "enableInternalTransfer": False}, False,
     "unusable"),
    ("мусор", False, "unknown"), (None, False, "unknown"), ([], False, "unknown"),
])
def test_bingx_rights(data, ok, state):
    k = keys.bingx_rights(data)
    assert (k.ok, k.state) == (ok, state), k


def test_bingx_withdraw_detail_readable_and_ip_warning():
    k = keys.bingx_rights({"permissions": [2, 3, 5], "ipAddresses": []})
    assert "вывод" in k.detail
    k = keys.bingx_rights({"permissions": [2, 3], "ipAddresses": []})
    assert k.ok and not k.ip_bound and "IP" in k.detail


@pytest.mark.parametrize("venue,answer,ok,state", [
    ("bybit", bybit_ok(bybit_result()), True, "ok"),
    ("bybit", bybit_ok(bybit_result(permissions=dict(TRADE_ONLY, Wallet=["Withdraw"]))), False, "unsafe"),
    ("bybit", bybit_err(10003, "API key is invalid"), False, "unknown"),
    ("bybit", Resp(exc=TimeoutError()), False, "unknown"), ("bybit", Resp(502, b""), False, "unknown"),
    ("bingx", Resp(200, {"apiKey": "x", "permissions": [2, 3], "ipAddresses": ["1.1.1.1"]}), True, "ok"),
    ("bingx", bingx_ok({"permissions": [2, 3, 5]}), False, "unsafe"),
    ("bingx", Resp(200, {"code": 100413, "msg": "bad key"}), False, "unknown"),
    ("bingx", Resp(307, b""), False, "unknown"),
])
def test_check_asks_exchange_and_fails_closed(venue, answer, ok, state):
    path = "/v5/user/query-api" if venue == "bybit" else "/openApi/v1/account/apiPermissions"
    s = Session({("GET", path): answer})
    k = run(keys.check(s, venue, CREDS))
    assert (k.ok, k.state) == (ok, state)
    assert [(c["method"], c["path"]) for c in s.calls] == [("GET", path)] and s.calls[0]["redirects"] is False


def test_check_without_key_makes_no_request():
    s = Session()
    assert run(keys.check(s, "bybit")) == keys.KeyCheck(False, "none", "торговый ключ не сохранён", None)
    res = run(keys.startup_check(s))
    assert set(res) == {"bybit", "bingx"} and not any(k.ok for k in res.values()) and s.calls == []


# --- скрипт владельца ---

def test_script_set_saves_encrypted_in_bot_dir_and_never_prints_secret(tmp_path, capsys):
    bot = tmp_path / "bot"
    answers = iter([KEY, SECRET])
    rc = script.main(["set", "bybit", "--bot-dir", str(bot)], ask=lambda prompt: next(answers),
                     confirm=lambda prompt: "n")
    out = capsys.readouterr().out
    assert rc == 0 and SECRET not in out and KEY not in out and accounts.mask(KEY) in out
    path = bot / "data" / "keys.json"
    saved = json.loads(path.read_text(encoding="utf-8"))["bybit_trade"]
    assert SECRET not in path.read_text(encoding="utf-8") or sys.platform != "win32"   # DPAPI на Windows
    assert accounts.KEYS_PATH == str(path) and keys.credentials("bybit") == CREDS
    assert set(saved) == {"key", "secret"}


def test_script_check_and_delete(tmp_path, capsys):
    bot = str(tmp_path / "bot")
    accounts.KEYS_PATH = os.path.join(bot, "data", "keys.json")
    accounts.save_key("bingx_trade", KEY, SECRET)
    seen = []

    def fake_run(venue):
        seen.append(venue)
        return keys.KeyCheck(False, "unsafe", "у ключа лишние права: вывод", True)
    assert script.main(["check", "bingx", "--bot-dir", bot], run=fake_run) == 2 and seen == ["bingx"]
    assert "запрещена" in capsys.readouterr().out
    ok = keys.KeyCheck(True, "ok", "", True)
    assert script.main(["check", "bingx", "--bot-dir", bot], run=lambda v: ok) == 0
    assert script.main(["delete", "bingx", "--bot-dir", bot]) == 0 and keys.credentials("bingx") is None


@pytest.mark.parametrize("key,secret", [("", SECRET), (KEY, ""), ("a b", SECRET), (KEY, "x\ty")])
def test_script_refuses_empty_or_spaced(tmp_path, key, secret, capsys):
    answers = iter([key, secret])
    assert script.main(["set", "bingx", "--bot-dir", str(tmp_path)], ask=lambda p: next(answers),
                       confirm=lambda p: "n") == 1
    assert not (tmp_path / "data" / "keys.json").exists()


def test_script_needs_interactive_console(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert script.main(["set", "bybit", "--bot-dir", str(tmp_path)]) == 1


def test_script_is_not_reachable_from_bot():
    import bot as B
    import inspect
    src = inspect.getsource(B)
    assert "trading_keys" not in src and "bybit_trade" not in src and "bingx_trade" not in src
    with pytest.raises(SystemExit):
        script.main(["set", "mexc"])
