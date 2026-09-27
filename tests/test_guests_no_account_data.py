"""Данные по ключам бирж (балансы, история операций, статус ключей, автожурнал) — только владельцу, гостям никогда."""

import accounts
import bot as B
import p2p
from test_guests import Stub, msg, sent
from helpers import arun


def test_guest_cannot_reach_any_account_command_or_button(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "FAKEKEY1234567890AB", "FAKESECRET0123456789012345678901234")
    bot = Stub(p2p.Config(), guests=["42"])
    for cmd in ("/balance", "/stats", "/alerts", "/settings", "/dev", "/status", "/logs", "/guests", "/allow 7"):
        arun(bot.on_update(msg(42, cmd)))
        last = sent(bot)[-1]
        assert last["chat_id"] == "42" and last["text"] == B.GUEST_DENIED, cmd
    for data in ("balance", "accounts", "acc:bybit", "acc_check:bybit", "acc_add:bybit", "acc_del:bybit", "settings"):
        before = len(bot.out)
        cq = {"id": "1", "data": data, "message": {"chat": {"id": 42}, "message_id": 5}}
        arun(bot.on_update({"callback_query": cq}))
        new = bot.out[before:]
        assert [m for m, _ in new] == ["answerCallbackQuery"], data          # только «только для владельца», ничего не шлём
        assert "владельца" in new[0][1]["text"]
    assert accounts.keys("bybit") is not None                                # гость не может удалить ключ
    shown = " ".join(p.get("text", "") for _, p in bot.out)
    assert "FAKEKEY" not in shown and "FAKESECRET" not in shown


def test_account_history_and_balance_go_to_owner_only(monkeypatch):
    bot = Stub(p2p.Config(), guests=["42", "43"])
    bot.topics = {"signals": 11, "journal": 12, "settings": 13, "dev": 14}

    async def history(s, ex):
        return [{"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1000.0, "id": "d1"}]

    monkeypatch.setattr(B.accounts, "keys", lambda ex: ("k", "s") if ex == "bybit" else None)
    monkeypatch.setattr(B.accounts, "account_history", history)
    bot.acc_seen["bybit"] = set()                                            # первый опрос уже был — дальше уведомления
    arun(bot.check_accounts())
    hist = [p for p in sent(bot) if "USDT" in p.get("text", "")]
    assert hist and all(p["chat_id"] == "1" for p in hist)                  # только владельцу
    assert all(p.get("message_thread_id") == 12 for p in hist)              # в его топик «Журнал»
    assert not [p for _, p in bot.out if p.get("chat_id") in ("42", "43")]  # гостям — ничего


def test_guest_context_never_leaks_into_background_account_sends(monkeypatch):
    """Даже если владелец и гость пишут одновременно: фоновые отправки идут владельцу."""
    bot = Stub(p2p.Config(), guests=["42"])
    arun(bot.on_update(msg(42, "/help")))                             # контекст гостя отработал и сброшен
    arun(bot.send("баланс: 100 USDT", topic="journal"))
    assert sent(bot)[-1]["chat_id"] == "1"
