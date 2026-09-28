"""Цветные кнопки/«📋» выключаются только отказом из-за самих кнопок (BUTTON_ERRORS). 403 «bot was blocked by the user»
(гость заблокировал бота), «message is too long», «can't parse entities» раньше читались как «цветные кнопки не
поддерживаются» — один такой отказ переводил весь бот на обычные кнопки до перезапуска и зря повторял отправку."""
import pytest

import bot as B
import p2p
from helpers import arun
from test_buttons import deal, snap
from test_review_audit_bot import _caption_edits, _live_bot
from test_bot import deal as live_deal
from test_bot import snap as live_snap

GUEST = "777"
NOT_BUTTONS = ((403, "Forbidden: bot was blocked by the user"),
               (400, "Bad Request: message is too long"),
               (400, "Bad Request: message caption is too long"),
               (400, "Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 12"))
# что отвечает сервер, не знающий style/copy_text: кнопка «📋» без url/callback_data — «текстовая», и т.п.
UNSUPPORTED = ((400, "Bad Request: can't parse inline keyboard button: Text buttons are unallowed in the inline "
                     "keyboard"),
               (400, "Bad Request: can't parse reply keyboard markup JSON object"),
               (400, "Bad Request: BUTTON_TYPE_INVALID"))


class Stub(B.Bot):
    """Отказ `answer` на любую отправку с цветными кнопками в чат `refuse` (None — во все чаты)."""

    def __init__(self, answer, refuse=None):
        super().__init__(None, "x", "1", p2p.Config())
        self.out, self.answer, self.refuse = [], answer, refuse
        self.fancy = True

    async def call(self, method, **p):
        self.out.append((method, p))
        if (self.refuse is None or str(p.get("chat_id")) == self.refuse) and B.is_fancy(p.get("reply_markup")):
            code, description = self.answer
            return {"ok": False, "error_code": code, "description": description}
        return {"ok": True, "result": {"message_id": 1}}

    async def _post_photo(self, png, caption, markup, thread=None, chat_id=None):
        return await self.call("sendPhoto", chat_id=chat_id or self.chat_id, caption=caption, reply_markup=markup)


def _kb():
    return B.deal_markup(deal(), deal_id=1, cfg=p2p.Config(), snap=snap([deal()]))


def _fancy_flags(bot, method):
    return [B.is_fancy(p["reply_markup"]) for m, p in bot.out if m == method]


@pytest.mark.parametrize("answer", NOT_BUTTONS)
def test_send_error_not_about_buttons_keeps_colors(answer):
    bot = Stub(answer)
    r = arun(bot.send("hi", markup=_kb()))
    assert not r["ok"] and bot.fancy
    assert _fancy_flags(bot, "sendMessage") == [True]          # без повтора с обычными кнопками
    bot.out.clear()
    arun(bot.send("again", markup=_kb()))
    assert _fancy_flags(bot, "sendMessage") == [True]          # и дальше цветные


@pytest.mark.parametrize("answer", NOT_BUTTONS)
def test_send_photo_error_not_about_buttons_keeps_colors(answer):
    bot = Stub(answer)
    r = arun(bot.send_photo(b"png", "cap", _kb()))
    assert not r["ok"] and bot.fancy and _fancy_flags(bot, "sendPhoto") == [True]


def test_guest_blocked_bot_keeps_colors_for_owner(monkeypatch):
    """Гость заблокировал бота: его 403 не выключает цветные кнопки владельцу и другим гостям."""
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub((403, "Forbidden: bot was blocked by the user"), refuse=GUEST)
    bot.guests = {GUEST}
    r = arun(bot.send_deal(deal(), snap=snap([deal()]), chat_id=GUEST, nav=False))
    assert not r["ok"] and bot.fancy
    # картинка не ушла → тот же текст сообщением (photo_or_text); оба раза — цветные, без повтора с обычными
    assert [(m, B.is_fancy(p["reply_markup"])) for m, p in bot.out] == [("sendPhoto", True), ("sendMessage", True)]
    bot.out.clear()
    arun(bot.send_deal(deal(), snap=snap([deal()]), nav=False))                # владельцу — по-прежнему цветные
    owner = [p for m, p in bot.out if m == "sendPhoto"]
    assert len(owner) == 1 and B.is_fancy(owner[0]["reply_markup"]) and owner[0]["chat_id"] == bot.chat_id


def test_stale_mark_parse_entities_keeps_colors(monkeypatch):
    """Правка карточки («⌛ устарела») с отказом «can't parse entities» — тоже не про кнопки."""
    bot = _live_bot(monkeypatch)
    bot.fancy = True
    arun(bot.notify(live_snap([live_deal(5)])))

    async def call(method, **p):
        bot.out.append((method, p))
        if method == "editMessageCaption":
            return {"ok": False, "error_code": 400,
                    "description": "Bad Request: can't parse entities: Unsupported start tag \"x\" at byte offset 3"}
        return {"ok": True}
    bot.call = call
    arun(bot.mark_stale_deals(set()))
    edits = _caption_edits(bot)
    assert bot.fancy and len(edits) == 1 and B.is_fancy(edits[0]["reply_markup"])


@pytest.mark.parametrize("answer", UNSUPPORTED)
def test_unsupported_buttons_still_fall_back_to_plain(answer):
    bot = Stub(answer)
    r = arun(bot.send("hi", markup=_kb()))
    assert r["ok"] and not bot.fancy
    assert _fancy_flags(bot, "sendMessage") == [True, False]   # тот же текст повторён с обычными кнопками
    bot.out.clear()
    arun(bot.send("again", markup=_kb()))
    assert _fancy_flags(bot, "sendMessage") == [False]         # дальше сразу обычные


@pytest.mark.parametrize("answer", UNSUPPORTED)
def test_unsupported_buttons_on_photo_fall_back_to_plain(answer):
    bot = Stub(answer)
    r = arun(bot.send_photo(b"png", "cap", _kb()))
    assert r["ok"] and not bot.fancy and _fancy_flags(bot, "sendPhoto") == [True, False]


def test_fancy_failed_403_with_button_word_is_not_about_colors():
    """403 — отказ доступа к чату, а не разметке, даже если в тексте (здесь условном) есть «button»/«keyboard»."""
    bot = type("B", (), {})()
    bot.fancy = True
    r = {"ok": False, "error_code": 403, "description": "Forbidden: bot can't send keyboard buttons here"}
    assert not B.Bot._fancy_failed(bot, r, _kb()) and bot.fancy
