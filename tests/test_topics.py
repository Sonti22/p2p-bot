"""Топики в личке с ботом (Bot API 9.5): создание по getMe.has_topics_enabled, маршрутизация сообщений."""
import asyncio
import json

import bot as B
import p2p
from helpers import make_ad


class Stub(B.Bot):
    def __init__(self, cfg, topics_enabled=True, create_fails=False):
        super().__init__(None, "x", "1", cfg)
        self.out, self.topics_enabled, self.create_fails = [], topics_enabled, create_fails
        self.next_thread = 10

    async def call(self, method, **p):
        self.out.append((method, p))
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "is_bot": True, "has_topics_enabled": self.topics_enabled}}
        if method == "createForumTopic":
            if self.create_fails:
                return {"ok": False, "description": "Bad Request: not enough rights"}
            self.next_thread += 1
            return {"ok": True, "result": {"message_thread_id": self.next_thread, "name": p["name"]}}
        return {"ok": True, "result": {"message_id": 1}}

    async def _post_photo(self, png, caption, markup, thread=None):
        return await self.call("sendPhoto", caption=caption, reply_markup=markup, message_thread_id=thread)


def sent(bot, method="sendMessage"):
    return [p for m, p in bot.out if m == method]


def deal():
    return 3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def test_setup_topics_creates_and_saves(tmp_path, monkeypatch):
    path = tmp_path / "data" / "topics.json"
    monkeypatch.setattr(B, "TOPICS_PATH", str(path))
    bot = Stub(p2p.Config())
    asyncio.run(bot.setup_topics())
    assert bot.topics == {"signals": 11, "journal": 12, "settings": 13, "dev": 14}
    assert json.loads(path.read_text(encoding="utf-8")) == {"signals": 11, "journal": 12, "settings": 13, "dev": 14}
    created = [p["name"] for p in sent(bot, "createForumTopic")]
    assert created == ["🔔 Сигналы", "📒 Журнал", "⚙️ Настройки", "🛠 Разработка"]
    hints = sent(bot)                                                   # подсказка — в каждый новый топик
    assert [h["message_thread_id"] for h in hints] == [11, 12, 13, 14] and "сигналы" in hints[0]["text"]


def test_setup_topics_reuses_saved_ids(tmp_path, monkeypatch):
    path = tmp_path / "topics.json"
    path.write_text(json.dumps({"signals": 5, "journal": 6, "settings": 7}), encoding="utf-8")
    monkeypatch.setattr(B, "TOPICS_PATH", str(path))
    bot = Stub(p2p.Config())
    asyncio.run(bot.setup_topics())
    assert bot.topics == {"signals": 5, "journal": 6, "settings": 7, "dev": 11}   # создан только недостающий
    assert len(sent(bot, "createForumTopic")) == 1 and len(sent(bot)) == 1


def test_topics_disabled_or_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "TOPICS_PATH", str(tmp_path / "topics.json"))
    bot = Stub(p2p.Config(), topics_enabled=False)
    asyncio.run(bot.setup_topics())
    assert bot.topics == {} and not sent(bot, "createForumTopic")
    bot = Stub(p2p.Config(), create_fails=True)
    asyncio.run(bot.setup_topics())
    assert bot.topics == {} and B.load_topics(str(tmp_path / "topics.json")) == {}
    asyncio.run(bot.send("hi", topic="signals"))
    assert "message_thread_id" not in sent(bot)[-1]                    # без топиков — как раньше
    bot.chat_id = ""
    asyncio.run(bot.setup_topics())
    assert not sent(bot, "getMe")[2:]                                   # без chat_id getMe не зовём


def test_routing_by_topic_and_reply_thread(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config())
    bot.topics = {"signals": 11, "journal": 12, "settings": 13, "dev": 14}
    asyncio.run(bot.send("s", topic="signals"))
    asyncio.run(bot.send("j", topic="journal"))
    asyncio.run(bot.send("plain"))
    assert [p.get("message_thread_id") for p in sent(bot)] == [11, 12, None]
    # команда из топика «Настройки» — ответ туда же; следующая из общего чата — без топика
    own = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}}   # личный чат владельца (топики — в личке)
    upd = {"message": dict(own, text="/help", message_thread_id=13)}
    asyncio.run(bot.on_update(upd))
    assert sent(bot)[-1]["message_thread_id"] == 13
    asyncio.run(bot.on_update({"message": dict(own, text="/help")}))
    assert "message_thread_id" not in sent(bot)[-1]
    # кнопка, нажатая в топике, — ответ туда же
    cq = {"id": "1", "data": "history", "from": {"id": 1},
          "message": {"chat": {"id": 1, "type": "private"}, "message_id": 5, "message_thread_id": 14}}
    asyncio.run(bot.on_update({"callback_query": cq}))
    assert bot.cur_thread == 14
    # сигнал скана — всегда в «Сигналы», даже если последняя команда была из другого топика
    d = deal()
    snap = p2p.Snapshot(88.0, "t", {}, {}, [d], {}, {}, {})
    bot.live_scans = 1
    asyncio.run(bot.notify(snap))
    assert sent(bot, "sendPhoto")[-1]["message_thread_id"] == 11


def test_first_chat_sets_up_topics(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "TOPICS_PATH", str(tmp_path / "topics.json"))
    monkeypatch.setattr(B, "save_env", lambda *a, **k: None)
    bot = Stub(p2p.Config())
    bot.chat_id = ""
    asyncio.run(bot.on_update({"message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/start"}}))
    assert bot.chat_id == "42" and bot.topics["signals"] == 11
    assert sent(bot)[-1]["text"].startswith("👋") and "message_thread_id" not in sent(bot)[-1]
