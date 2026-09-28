"""KEY_RECHECK_HOURS: права сохранённых ключей бирж перепроверяются не только при старте, но и из accounts_loop."""

import asyncio

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


def fake_perms(monkeypatch, safe, detail="Trade, Withdraw"):
    calls = []

    async def perms(s, ex):
        calls.append(ex)
        return safe, detail

    monkeypatch.setattr(B.accounts, "api_permissions", perms)
    return calls


def test_hours_default_zero_and_garbage(monkeypatch):
    monkeypatch.delenv("KEY_RECHECK_HOURS", raising=False)
    assert B.key_recheck_hours() == B.KEY_RECHECK_HOURS_DEFAULT
    monkeypatch.setenv("KEY_RECHECK_HOURS", "0")
    assert B.key_recheck_hours() == 0
    monkeypatch.setenv("KEY_RECHECK_HOURS", "2.5")
    assert B.key_recheck_hours() == 2.5
    for bad in ("abc", "-1", "nan", "inf"):
        monkeypatch.setenv("KEY_RECHECK_HOURS", bad)
        assert B.key_recheck_hours() == B.KEY_RECHECK_HOURS_DEFAULT


def test_due_after_interval_and_off_at_zero(monkeypatch):
    monkeypatch.setenv("KEY_RECHECK_HOURS", "1")
    bot = Stub(p2p.Config())
    t0 = bot.key_checked_ts
    assert not bot.key_recheck_due(t0 + 3599)
    assert bot.key_recheck_due(t0 + 3600)
    monkeypatch.setenv("KEY_RECHECK_HOURS", "0")
    assert not bot.key_recheck_due(t0 + 10 ** 9)


def test_periodic_check_drops_key_that_gained_trade_rights(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.delenv("ALLOW_UNSAFE_KEYS", raising=False)
    accounts.save_key("bybit", "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    fake_perms(monkeypatch, False)
    bot = Stub(p2p.Config())
    bot.key_checked_ts = 0
    arun(bot.check_key_safety(periodic=True))
    assert accounts.keys("bybit") is None
    assert any("удалил его из бота" in t for t in texts(bot))
    assert bot.key_checked_ts > 0 and not bot.key_recheck_due()


def test_periodic_check_keeps_allowed_unsafe_key_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setenv("ALLOW_UNSAFE_KEYS", "1")
    accounts.save_key("bybit", "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    fake_perms(monkeypatch, False)
    bot = Stub(p2p.Config())
    arun(bot.check_key_safety())
    arun(bot.check_key_safety(periodic=True))
    assert accounts.keys("bybit") is not None
    assert accounts.verify_status("bybit") == ("unsafe", "Trade, Withdraw")
    assert texts(bot) == []


def test_accounts_loop_rechecks_when_due(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setenv("KEY_RECHECK_HOURS", "1")
    accounts.save_key("bybit", "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    calls = fake_perms(monkeypatch, True, "")
    bot = Stub(p2p.Config())

    async def no_accounts():
        return None

    bot.check_accounts = no_accounts

    async def one_tick(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(B.asyncio, "sleep", one_tick)

    def tick():
        try:
            arun(bot.accounts_loop())
        except asyncio.CancelledError:
            pass

    tick()
    assert calls == []                     # только что стартовали — рано
    bot.key_checked_ts -= 3600
    tick()
    assert calls == ["bybit"]
    assert accounts.keys("bybit") is not None and texts(bot) == []
    tick()
    assert calls == ["bybit"]              # следующая — снова через час


def test_accounts_loop_survives_recheck_error(monkeypatch):
    monkeypatch.setenv("KEY_RECHECK_HOURS", "1")
    bot = Stub(p2p.Config())
    bot.key_checked_ts = 0

    async def no_accounts():
        return None

    async def boom(periodic=False):
        raise RuntimeError("net")

    bot.check_accounts = no_accounts
    bot.check_key_safety = boom
    seen = []

    async def one_tick(_):
        seen.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(B.asyncio, "sleep", one_tick)
    try:
        arun(bot.accounts_loop())
    except asyncio.CancelledError:
        pass
    assert seen == [1]                     # ошибка перепроверки не рвёт цикл — дошли до sleep
