"""Отмена операций блэклиста: «↩️ Вернуть» после снятия записи, «↩️ Отменить» после «🚫 Скрыть мерчанта»."""
import blacklist
import bot as B
import p2p
from helpers import arun, make_ad


class Stub(B.Bot):
    """Бот без сети: все вызовы Telegram пишутся в self.out."""
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def cq(data, message_id=3):
    return {"id": "1", "data": data, "message": {"message_id": message_id, "chat": {"id": "1", "type": "private"}}}


def deal(profit=3.0):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "маршрут"


# --- unbl: снятие записи, кнопка «Вернуть» ---------------------------------------------------------------------

def test_unbl_adds_undo_button():
    entry_id = blacklist.add("Bybit", "Плохой")
    blacklist.set_note(entry_id, "кидала")
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cq(f"unbl:{entry_id}")))
    assert blacklist.list_all() == []
    method, params = bot.out[-1]
    assert method == "editMessageText"
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    tokens = [b["callback_data"] for b in buttons if b["callback_data"].startswith("blundo:")]
    assert len(tokens) == 1
    assert len(tokens[0].encode()) <= 64


def test_unbl_unknown_id_no_undo_button():
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cq("unbl:999")))
    method, params = bot.out[-1]
    assert method == "editMessageText"
    assert params["reply_markup"]["inline_keyboard"] == []


def test_blundo_restores_entry_with_note_and_date():
    entry_id = blacklist.add("Bybit", "Плохой", ts=1_700_000_000.0)
    blacklist.set_note(entry_id, "кидала")
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cq(f"unbl:{entry_id}")))
    token = bot.out[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    bot.out.clear()
    arun(bot.on_callback(cq(f"blundo:{token}")))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "Возвращён" in answers[0]["text"]
    rows = blacklist.list_all()
    assert len(rows) == 1
    _, ex, nick, added_ts, note = rows[0]
    assert (ex, nick, added_ts, note) == ("Bybit", "Плохой", 1_700_000_000.0, "кидала")
    method, params = bot.out[-1]
    assert method == "editMessageText"
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(b["callback_data"].startswith("blundo:") for b in buttons)


def test_blundo_repeat_and_unknown_token_say_too_late():
    entry_id = blacklist.add("Bybit", "Плохой")
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cq(f"unbl:{entry_id}")))
    token = bot.out[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    arun(bot.on_callback(cq(f"blundo:{token}")))
    before = blacklist.list_all()
    bot.out.clear()
    arun(bot.on_callback(cq(f"blundo:{token}")))         # повтор — токен уже потрачен
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "поздно" in answers[0]["text"]
    assert blacklist.list_all() == before                 # не задвоилось
    bot.out.clear()
    arun(bot.on_callback(cq("blundo:99999")))              # неизвестный токен
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "поздно" in answers[0]["text"]
    assert blacklist.list_all() == before


def test_blundo_when_entry_re_added_with_other_reason():
    entry_id = blacklist.add("Bybit", "Плохой")
    blacklist.set_note(entry_id, "старая причина")
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cq(f"unbl:{entry_id}")))
    token = bot.out[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    new_id = blacklist.add("Bybit", "Плохой")
    blacklist.set_note(new_id, "свежая причина")
    bot.out.clear()
    arun(bot.on_callback(cq(f"blundo:{token}")))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "Уже в блэклисте" in answers[0]["text"]
    rows = blacklist.list_all()
    assert len(rows) == 1 and rows[0][4] == "свежая причина"    # не затёрта


# --- hide_deal / hideundo: «Скрыть мерчанта» и отмена -------------------------------------------------------

def test_hide_deal_adds_undo_button():
    bot = Stub(p2p.Config())
    deal_id = bot.remember_deal(deal())
    arun(bot.hide_deal(cq("bl:" + str(deal_id), message_id=9), deal_id))
    method, params = [(m, p) for m, p in bot.out if m == "sendMessage"][-1]
    assert "В блэклисте" in params["text"]
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    tokens = [b["callback_data"] for b in buttons if b["callback_data"].startswith("hideundo:")]
    assert len(tokens) == 1


def test_hide_deal_both_sides_already_blocked_no_button():
    blacklist.add("Bybit", "nick")
    blacklist.add("MEXC", "nick")
    bot = Stub(p2p.Config())
    deal_id = bot.remember_deal(deal())
    arun(bot.hide_deal(cq("bl:" + str(deal_id), message_id=9), deal_id))
    method, params = [(m, p) for m, p in bot.out if m == "sendMessage"][-1]
    assert not params.get("reply_markup")


def test_hideundo_removes_only_new_entry():
    old_id = blacklist.add("Bybit", "nick")
    blacklist.set_note(old_id, "старая запись")
    bot = Stub(p2p.Config())
    deal_id = bot.remember_deal(deal())
    arun(bot.hide_deal(cq("bl:" + str(deal_id), message_id=9), deal_id))
    token = None
    for m, p in bot.out:
        if m == "sendMessage" and p.get("reply_markup"):
            token = p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    assert token is not None
    bot.out.clear()
    arun(bot.on_callback(cq(f"hideundo:{token}")))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "Скрытие отменено" in answers[0]["text"]
    rows = {(ex, nick): note for _, ex, nick, _, note in blacklist.list_all()}
    assert rows == {("Bybit", "nick"): "старая запись"}       # новая (MEXC) запись снята, старая и причина на месте
    method, params = bot.out[-1]
    assert method == "editMessageText" and "Отменено" in params["text"]


def test_hideundo_repeat_and_unknown_token():
    bot = Stub(p2p.Config())
    deal_id = bot.remember_deal(deal())
    arun(bot.hide_deal(cq("bl:" + str(deal_id), message_id=9), deal_id))
    token = None
    for m, p in bot.out:
        if m == "sendMessage" and p.get("reply_markup"):
            token = p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    arun(bot.on_callback(cq(f"hideundo:{token}")))
    before = blacklist.list_all()
    bot.out.clear()
    arun(bot.on_callback(cq(f"hideundo:{token}")))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "отменено" in answers[0]["text"].lower()
    method, params = bot.out[-1]
    assert method == "editMessageReplyMarkup" and params["reply_markup"] == {"inline_keyboard": []}
    assert blacklist.list_all() == before
    bot.out.clear()
    arun(bot.on_callback(cq("hideundo:88888")))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "отменено" in answers[0]["text"].lower()


# --- переполнение и перепутанный вид токена ---------------------------------------------------------------

def test_overflow_evicts_oldest():
    bot = Stub(p2p.Config())
    ids = [blacklist.add("Bybit", f"nick{i}") for i in range(25)]
    tokens = []
    for entry_id in ids:
        arun(bot.on_callback(cq(f"unbl:{entry_id}")))
        last_row = bot.out[-1][1]["reply_markup"]["inline_keyboard"][-1]
        tokens.append(last_row[0]["callback_data"].split(":")[1])
    assert len(bot.bl_undo) == 20
    bot.out.clear()
    arun(bot.on_callback(cq(f"blundo:{tokens[0]}")))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "поздно" in answers[0]["text"]
    assert ("Bybit", "nick0") not in {(ex, nick) for _, ex, nick, *_ in blacklist.list_all()}


def test_token_kind_mismatch_is_treated_as_stale_and_kept():
    bot = Stub(p2p.Config())
    deal_id = bot.remember_deal(deal())
    arun(bot.hide_deal(cq("bl:" + str(deal_id), message_id=9), deal_id))
    hide_token = None
    for m, p in bot.out:
        if m == "sendMessage" and p.get("reply_markup"):
            hide_token = p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    before = dict(bot.bl_undo)
    bot.out.clear()
    arun(bot.on_callback(cq(f"blundo:{hide_token}")))      # чужой вид токена для blundo
    assert bot.bl_undo == before                            # ничего не изменилось, токен на месте

    entry_id = blacklist.add("Bybit", "Другой")
    bot.out.clear()
    arun(bot.on_callback(cq(f"unbl:{entry_id}")))
    last_row = bot.out[-1][1]["reply_markup"]["inline_keyboard"][-1]
    bl_token = last_row[0]["callback_data"].split(":")[1]
    before2 = dict(bot.bl_undo)
    bot.out.clear()
    arun(bot.on_callback(cq(f"hideundo:{bl_token}")))       # чужой вид токена для hideundo
    assert bot.bl_undo == before2


# --- гость: не имеет доступа к blundo/hideundo -------------------------------------------------------------

def test_guest_cannot_use_undo_buttons():
    entry_id = blacklist.add("Bybit", "Плохой")
    bot = Stub(p2p.Config())
    bot.guests = {"42"}
    arun(bot.on_callback(cq(f"unbl:{entry_id}")))
    token = bot.out[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    before = dict(bot.bl_undo)
    bot.out.clear()
    update = {"callback_query": {"id": "7", "data": f"blundo:{token}",
                                  "from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"}, "message_id": 5}}}
    arun(bot.on_update(update))
    assert bot.bl_undo == before
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "Только для владельца бота" in answers[0]["text"]

    update["callback_query"]["data"] = f"hideundo:{token}"
    bot.out.clear()
    arun(bot.on_update(update))
    assert bot.bl_undo == before
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "Только для владельца бота" in answers[0]["text"]
