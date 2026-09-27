"""/allow @username: доступ по нику открывается с первого сообщения этого пользователя боту."""

import bot as B
import p2p
from test_guests import Stub, msg, sent
from helpers import arun


def test_allow_by_username_then_first_message_connects(monkeypatch):
    saved = {}
    monkeypatch.setattr(B, "save_env", lambda k, v: saved.__setitem__(k, v))
    bot = Stub(p2p.Config())
    bot.username = "p2psckabot"
    arun(bot.handle("/allow @ALLUM1N"))
    assert bot.pending == {"@allum1n"} and saved["TG_GUESTS"] == "@allum1n"
    assert "@p2psckabot" in sent(bot)[-1]["text"]
    arun(bot.handle("/guests"))
    assert "@allum1n" in sent(bot)[-1]["text"] and "ждёт" in sent(bot)[-1]["text"]
    # чужой с другим ником — по-прежнему подсказка про id
    arun(bot.on_update(msg(7, "привет", username="someone")))
    assert sent(bot)[-1]["chat_id"] == "1" and "/allow 7" in sent(bot)[-1]["text"] and not bot.guests
    # тот самый — подключается сразу, регистр ника не важен
    arun(bot.on_update(msg(42, "/start", username="Allum1n")))
    assert bot.guests == {"42"} and not bot.pending and saved["TG_GUESTS"] == "42"
    welcome = [p for p in sent(bot) if p["chat_id"] == "42"]
    assert len(welcome) == 1 and welcome[0]["reply_markup"] == B.GUEST_MENU
    assert "@allum1n" in sent(bot)[-1]["text"] and "/deny 42" in sent(bot)[-1]["text"]   # владельцу — отчёт
    # дальше он гость: команда обрабатывается, а не «бот приватный»
    arun(bot.on_update(msg(42, "/settings")))
    assert sent(bot)[-1]["chat_id"] == "42" and sent(bot)[-1]["text"] == B.GUEST_DENIED


def test_deny_pending_username_and_env_loading(monkeypatch):
    saved = {}
    monkeypatch.setattr(B, "save_env", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setenv("TG_GUESTS", "42,@Friend_1, @two")
    bot = Stub(p2p.Config())
    bot.guests, bot.pending = {"42"}, {"@friend_1", "@two"}   # Stub сбрасывает guests — проверяем разбор отдельно
    real = B.Bot(None, "x", "1", p2p.Config())
    assert real.guests == {"42"} and real.pending == {"@friend_1", "@two"}
    arun(bot.handle("/deny @FRIEND_1"))
    assert bot.pending == {"@two"} and saved["TG_GUESTS"] == "42,@two"
    arun(bot.handle("/allow @ab"))                      # слишком короткий ник — не id и не ник
    assert "id чата или @ник" in sent(bot)[-1]["text"]
