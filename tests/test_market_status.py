"""Закреплённое сообщение «Статус рынка»: ориентир, лучшая связка, площадки ок/недоступны, раз в минуту."""
import asyncio

import bot as B
import p2p
from helpers import make_ad


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []
        self.next_id = 100

    async def call(self, method, **p):
        self.out.append((method, p))
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": {}}


def calls(bot, method):
    return [p for m, p in bot.out if m == method]


def deal():
    return 3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def snapshot(deals=(), errors=None):
    return p2p.Snapshot(88.0, "медиана P2P", {}, {}, list(deals), {}, {}, errors or {})


def test_market_status_view_ok_and_failed_venues():
    cfg = p2p.Config(exchanges=["bybit", "mexc"])
    snap = snapshot([deal()], errors={"mexc/USDT": "timeout"})
    text = B.market_status_view(snap, cfg)
    assert "Ориентир USDT: 88.00 ₽" in text
    assert "Bybit → MEXC (USDT→USDT) +3.00%" in text
    assert "✅ Bybit" in text and "⚠️ MEXC" in text


def test_market_status_view_no_deals_and_all_ok():
    cfg = p2p.Config(exchanges=["bybit"])
    text = B.market_status_view(snapshot(), cfg)
    assert "нет связок выше порога" in text
    assert "✅ Bybit" in text and "⚠️" not in text


def test_update_market_status_sends_pins_and_edits(monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: 1000.0)
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    asyncio.run(bot.update_market_status(snapshot([deal()])))
    assert len(calls(bot, "sendMessage")) == 1
    pins = calls(bot, "pinChatMessage")
    assert pins and pins[0]["message_id"] == bot.market_msg_id == 101

    # раньше MARKET_STATUS_INTERVAL — ничего не делаем
    monkeypatch.setattr(B.time, "time", lambda: 1030.0)
    asyncio.run(bot.update_market_status(snapshot([deal()])))
    assert len(calls(bot, "sendMessage")) == 1 and not calls(bot, "editMessageText")

    # прошла минута — правим сообщение на месте, новое не шлём
    monkeypatch.setattr(B.time, "time", lambda: 1061.0)
    asyncio.run(bot.update_market_status(snapshot([deal()])))
    assert len(calls(bot, "sendMessage")) == 1
    edits = calls(bot, "editMessageText")
    assert len(edits) == 1 and edits[0]["message_id"] == 101


def test_update_market_status_without_chat_id_noop():
    bot = Stub(p2p.Config())
    bot.chat_id = ""
    asyncio.run(bot.update_market_status(snapshot()))
    assert bot.out == []


def test_update_market_status_recreates_after_edit_failure(monkeypatch):
    class FlakyStub(Stub):
        async def call(self, method, **p):
            if method == "editMessageText":
                self.out.append((method, p))
                return {"ok": False, "description": "Bad Request: message to edit not found"}
            return await super().call(method, **p)

    monkeypatch.setattr(B.time, "time", lambda: 2000.0)
    bot = FlakyStub(p2p.Config(exchanges=["bybit"]))
    asyncio.run(bot.update_market_status(snapshot()))
    first_id = bot.market_msg_id

    monkeypatch.setattr(B.time, "time", lambda: 2061.0)
    asyncio.run(bot.update_market_status(snapshot()))
    assert len(calls(bot, "editMessageText")) == 1
    assert len(calls(bot, "sendMessage")) == 2   # неудачный edit -> новое сообщение
    assert bot.market_msg_id != first_id
