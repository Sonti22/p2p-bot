"""Отказ Telegram на правку карточки, не связанный с кнопками («message is not modified», «message to edit not found»,
«message can't be edited»), не выключает цветные кнопки бота: после #143 правки живой карточки и «⌛ устарела» идут с
кнопками, и такой отказ раньше читался как «цветные кнопки не поддерживаются» — весь бот до перезапуска переходил на
обычные кнопки."""
import pytest

import bot as B
from helpers import arun
from test_bot import deal
from test_bot import snap as bot_snap
from test_review_audit_bot import _caption_edits, _live_bot

NOT_BUTTONS = ("Bad Request: message is not modified: specified new message content and reply markup are exactly "
               "the same as a current content and reply markup of the message",
               "Bad Request: message to edit not found", "Bad Request: message can't be edited")


def _answer(bot, description):
    async def call(method, **p):
        bot.out.append((method, p))
        if method == "editMessageCaption":
            return {"ok": False, "error_code": 400, "description": description}
        return {"ok": True}
    return call


@pytest.mark.parametrize("description", NOT_BUTTONS)
def test_stale_mark_error_not_about_buttons_keeps_colors(monkeypatch, description):
    bot = _live_bot(monkeypatch)
    bot.fancy = True
    arun(bot.notify(bot_snap([deal(5)])))
    bot.call = _answer(bot, description)
    arun(bot.mark_stale_deals(set()))
    edits = _caption_edits(bot)
    assert bot.fancy and len(edits) == 1 and B.is_fancy(edits[0]["reply_markup"])   # без повтора с обычными
    assert bot.live_msg[next(iter(bot.live_msg))]["stale"]                          # 400 — пометка закрыта


@pytest.mark.parametrize("description", NOT_BUTTONS)
def test_live_edit_error_not_about_buttons_keeps_colors(monkeypatch, description):
    bot = _live_bot(monkeypatch)
    bot.fancy = True
    arun(bot.notify(bot_snap([deal(5)])))
    key = next(iter(bot.live_msg))
    bot.live_msg[key]["last_edit"] -= B.LIVE_EDIT_INTERVAL + 1
    bot.call = _answer(bot, description)
    arun(bot.notify(bot_snap([deal(5.2)])))
    assert bot.fancy and len(_caption_edits(bot)) == 1


def test_button_error_still_falls_back_to_plain():
    """Отказ именно из-за кнопок по-прежнему переключает на обычные (test_live_edit_falls_back_to_plain_buttons)."""
    bot = type("B", (), {})()
    bot.fancy = True
    kb = {"inline_keyboard": [[{"text": "x", "callback_data": "y", "style": "success"}]]}
    r = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse reply keyboard markup"}
    assert B.Bot._fancy_failed(bot, r, kb) and not bot.fancy
