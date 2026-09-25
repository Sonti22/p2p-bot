"""Гости: доступ друга к сигналам и рыночным командам; настройки, ключи, журнал — только владельцу."""
import asyncio

import blacklist
import bot as B
import p2p
import trades
from helpers import make_ad


class Stub(B.Bot):
    def __init__(self, cfg, guests=()):
        super().__init__(None, "x", "1", cfg)
        self.guests = set(guests)
        self.out, self.env = [], {}

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.out)}}

    async def _post_photo(self, png, caption, markup, thread=None, chat_id=None):
        return await self.call("sendPhoto", chat_id=chat_id or self.chat_id, caption=caption, reply_markup=markup,
                               message_thread_id=thread)


def deal():
    return 3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def snap(deals):
    return p2p.Snapshot(88.0, "t", {}, {}, deals, {}, {}, {})


def sent(bot, method="sendMessage"):
    return [p for m, p in bot.out if m == method]


def msg(chat, text, **sender):
    return {"message": {"chat": {"id": chat}, "text": text, "from": {"first_name": "Вася", **sender}}}


def buttons(markup):
    return [b for row in (markup or {}).get("inline_keyboard", []) for b in row]


def test_stranger_gets_id_and_owner_is_told_once(monkeypatch):
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_update(msg(42, "/start", username="vasya")))
    asyncio.run(bot.on_update(msg(42, "/best")))
    to_stranger = [p for p in sent(bot) if p["chat_id"] == "42"]
    to_owner = [p for p in sent(bot) if p["chat_id"] == "1"]
    assert len(to_stranger) == 1 and "/allow 42" in to_stranger[0]["text"]
    assert len(to_owner) == 1 and "Вася (@vasya)" in to_owner[0]["text"] and "/allow 42" in to_owner[0]["text"]
    assert not bot.guests


def test_allow_and_deny(monkeypatch):
    saved = {}
    monkeypatch.setattr(B, "save_env", lambda k, v: saved.__setitem__(k, v))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/allow abc"))
    assert "Нужен id" in sent(bot)[-1]["text"] and not bot.guests
    asyncio.run(bot.handle("/allow 42"))
    assert bot.guests == {"42"} and saved["TG_GUESTS"] == "42"
    welcome = [p for p in sent(bot) if p["chat_id"] == "42"]
    assert len(welcome) == 1 and welcome[0]["reply_markup"] == B.GUEST_MENU
    asyncio.run(bot.handle("/allow 42"))
    assert "уже" in sent(bot)[-1]["text"]
    asyncio.run(bot.handle("/guests"))
    assert "42" in sent(bot)[-1]["text"] and "/deny 42" in sent(bot)[-1]["text"]
    asyncio.run(bot.handle("/deny 42"))
    assert not bot.guests and saved["TG_GUESTS"] == ""
    assert sent(bot)[-1]["chat_id"] == "42" and "закрыл" in sent(bot)[-1]["text"]
    asyncio.run(bot.on_update(msg(42, "/best")))            # снова чужой — снова подсказка про /allow
    assert "/allow 42" in sent(bot)[-1]["text"]


def test_guest_gets_market_commands_but_not_settings(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(), guests=["42"])
    bot.last = snap([deal()])
    asyncio.run(bot.on_update(msg(42, "/best")))
    photo = sent(bot, "sendPhoto")[-1]
    assert photo["chat_id"] == "42" and photo["message_thread_id"] is None
    assert not any("callback_data" in b and b["callback_data"].startswith(("did:", "steps:", "bl:"))
                   for b in buttons(photo["reply_markup"]))
    assert not bot.deals_by_id                              # гость не заводит сделок в памяти владельца
    for cmd in ("/settings", "/balance", "/stats", "/paper", "/alerts", "/dev", "/logs", "/pause", "/amount 100000"):
        asyncio.run(bot.on_update(msg(42, cmd)))
        assert sent(bot)[-1]["chat_id"] == "42" and sent(bot)[-1]["text"] == B.GUEST_DENIED, cmd
    assert bot.cfg.amount == 50000 and not bot.paused
    asyncio.run(bot.on_update(msg(42, "/start")))
    assert sent(bot)[-1]["reply_markup"] == B.GUEST_MENU and sent(bot)[-1]["chat_id"] == "42"
    assert sent(bot)[-1]["chat_id"] == "42"


def test_guest_text_does_not_consume_owner_input_state():
    bot = Stub(p2p.Config(), guests=["42"])
    bot.awaiting_amount = True                              # владелец вводит сумму
    asyncio.run(bot.on_update(msg(42, "70000")))
    assert bot.awaiting_amount is True and bot.cfg.amount == 50000
    assert sent(bot)[-1]["chat_id"] == "42" and sent(bot)[-1]["text"] == B.GUEST_DENIED


def test_signal_goes_to_owner_and_guests(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(min_profit=2.0), guests=["42", "43"])
    bot.live_scans = 1
    bot.topics = {"signals": 11, "journal": 12, "settings": 13, "dev": 14}
    d = deal()
    asyncio.run(bot.notify(snap([d])))
    photos = sent(bot, "sendPhoto")
    assert [p["chat_id"] for p in photos] == ["1", "42", "43"]
    assert photos[0]["message_thread_id"] == 11 and photos[1]["message_thread_id"] is None
    assert any(b.get("callback_data", "").startswith("did:") for b in buttons(photos[0]["reply_markup"]))
    assert not any(b.get("callback_data", "").startswith("did:") for b in buttons(photos[1]["reply_markup"]))
    assert len(bot.live_msg) == 1 and list(bot.live_msg.values())[0]["message_id"] == photos[0]["message_id"] \
        if "message_id" in photos[0] else len(bot.live_msg) == 1
    assert len(bot.deals_by_id) == 1                       # одна запись — для кнопок владельца


def test_guest_signal_not_duplicated_while_owner_delivery_retries(monkeypatch):
    """Владельцу не доставили (429) — сигнал повторится на следующем скане; гостю он уходит один раз,
    вместе с доставкой владельцу, а не на каждой попытке."""
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(min_profit=2.0), guests=["42"])
    bot.live_scans, bot.fancy = 1, False
    owner_fails = {"on": True}
    real_call = bot.call

    async def call(method, **p):
        if owner_fails["on"] and p.get("chat_id") in ("1", None):
            return {"ok": False, "error_code": 429, "description": "Too Many Requests"}
        return await real_call(method, **p)

    bot.call = call
    asyncio.run(bot.notify(snap([deal()])))
    assert not sent(bot, "sendPhoto") and not bot.sent
    owner_fails["on"] = False
    asyncio.run(bot.notify(snap([deal()])))
    assert [p["chat_id"] for p in sent(bot, "sendPhoto")] == ["1", "42"]


def test_guest_callbacks(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(), guests=["42"])
    bot.last = snap([deal()])
    cq = {"id": "7", "data": "best", "message": {"chat": {"id": 42}, "message_id": 5}}
    asyncio.run(bot.on_update({"callback_query": cq}))
    assert sent(bot, "sendPhoto")[-1]["chat_id"] == "42"
    cq["data"] = "did:1"
    asyncio.run(bot.on_update({"callback_query": cq}))
    answer = sent(bot, "answerCallbackQuery")[-1]
    assert "владельца" in answer["text"] and sent(bot, "sendPhoto")[-1]["chat_id"] == "42"


def test_background_send_unaffected_by_guest_context():
    """REPLY_CHAT задан только внутри обработки команды гостя; вне её send идёт владельцу."""
    bot = Stub(p2p.Config(), guests=["42"])
    asyncio.run(bot.on_update(msg(42, "/help")))
    assert sent(bot)[-1]["chat_id"] == "42"
    asyncio.run(bot.send("фоновое сообщение"))
    assert sent(bot)[-1]["chat_id"] == "1"
    assert B.REPLY_CHAT.get() is None


def test_guest_cannot_note_blacklist_and_gets_no_owner_risk_info():
    """Причина в блэклисте, счётчик контрагентов в /stats и строка про СБП в справке — только владельцу."""
    entry_id = blacklist.add("Bybit", "Плохой")
    trades.log_trade(deal(), 10000)
    bot = Stub(p2p.Config(), guests=["42"])
    for cmd in (f"/blacklist note {entry_id} гость пишет", "/blacklist", "/stats"):
        asyncio.run(bot.on_update(msg(42, cmd)))
        assert sent(bot)[-1]["chat_id"] == "42" and sent(bot)[-1]["text"] == B.GUEST_DENIED, cmd
    assert blacklist.list_all()[0][4] == ""
    asyncio.run(bot.on_update(msg(42, "/help")))
    assert sent(bot)[-1]["chat_id"] == "42" and "ОД-2506" not in sent(bot)[-1]["text"]
    assert not any("Контрагенты" in p["text"] or "ОД-2506" in p["text"] for p in sent(bot) if p["chat_id"] == "42")
    asyncio.run(bot.handle("/stats"))                                      # владельцу — блок есть
    assert sent(bot)[-1]["chat_id"] == "1" and "Контрагенты по картам" in sent(bot)[-1]["text"]


def test_guest_can_use_maker_with_book_block():
    """/maker — рыночная команда: гостю тоже место в стакане, конкуренты и спред (только публичный стакан)."""
    g = {("MEXC", "buy", "USDT"): [make_ad("MEXC", "buy", 92.0), make_ad("MEXC", "buy", 92.3)],
         ("MEXC", "sell", "USDT"): [make_ad("MEXC", "sell", 90.0)]}
    bot = Stub(p2p.Config(exchanges=["mexc"]), guests=["42"])
    bot.last = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, groups=g)
    asyncio.run(bot.on_update(msg(42, "/maker usdt")))
    text = sent(bot)[-1]["text"]
    assert sent(bot)[-1]["chat_id"] == "42" and text != B.GUEST_DENIED
    assert "Место в стакане: 1-е из 3" in text and "Конкуренты рядом" in text and "Спред MEXC" in text
    assert not [p for p in sent(bot) if p["chat_id"] == "1"]   # владельцу ничего не ушло
