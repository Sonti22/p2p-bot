"""ALLOW_UNSAFE_KEYS: по умолчанию ключ с торговлей/выводом удаляется; при =1 остаётся по решению владельца."""

import accounts
import bot as B
import p2p
from helpers import arun


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def fake_exchange(monkeypatch, safe=False, detail="Trade, Withdraw", ok=True):
    async def perms(s, ex):
        return safe, detail

    async def verify(s, ex):
        return ok, "" if ok else "HTTP 401"

    monkeypatch.setattr(B.accounts, "key_permissions", perms)
    monkeypatch.setattr(B.accounts, "api_permissions", perms)
    monkeypatch.setattr(B.accounts, "verify", verify)


def connect(bot):
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "FAKEKEY1234567890AB"}
    arun(bot.handle_key_input("FAKESECRET0123456789012345678901234", None))


def test_default_still_deletes_unsafe_key_on_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.delenv("ALLOW_UNSAFE_KEYS", raising=False)
    fake_exchange(monkeypatch)
    bot = Stub(p2p.Config())
    connect(bot)
    assert accounts.keys("bybit") is None
    assert any("удалил его из бота" in t for t in texts(bot))


def test_allow_keeps_unsafe_key_on_connect_and_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    fake_exchange(monkeypatch)
    bot = Stub(p2p.Config())
    connect(bot)
    assert accounts.keys("bybit") == ("FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    assert accounts.verify_status("bybit") == ("unsafe", "Trade, Withdraw")
    msg = next(t for t in texts(bot) if t.startswith("✅ Подключено"))
    assert "больше, чем чтение" in msg and "Trade, Withdraw" in msg and "только чтение)" not in msg
    card = texts(bot)[-1]
    assert "оставлен по твоему решению" in card and "FAKESECRET" not in card


def test_allow_check_button_and_startup_keep_key_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    accounts.save_key("bybit", "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    fake_exchange(monkeypatch)
    bot = Stub(p2p.Config())
    arun(bot.check_key_safety())                  # старт: не удаляем и не шлём сообщений
    assert accounts.keys("bybit") is not None and texts(bot) == []
    assert accounts.verify_status("bybit")[0] == "unsafe"
    arun(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 5}}))
    assert accounts.keys("bybit") is not None
    assert any("Ключ рабочий" in t and "больше, чем чтение" in t for t in texts(bot))


def test_allow_off_again_deletes_on_next_start(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    fake_exchange(monkeypatch)
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "0")
    bot = Stub(p2p.Config())
    arun(bot.check_key_safety())
    assert accounts.keys("bybit") is None


def test_readonly_key_unaffected_by_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    fake_exchange(monkeypatch, safe=True, detail="")
    bot = Stub(p2p.Config())
    connect(bot)
    assert accounts.verify_status("bybit") == ("ok", "")
    assert any(t.startswith("✅ Подключено (только чтение)") for t in texts(bot))
