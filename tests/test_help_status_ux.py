"""/help по разделам кнопками (как docs/owner-guide.md) и компактный /status: главное сверху, подробности — кнопкой."""
import time

import bot as B
import p2p
from helpers import arun, make_ad


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []
        self.edit_ok = True

    async def call(self, method, **p):
        self.out.append((method, p))
        if method == "editMessageText" and not self.edit_ok:
            return {"ok": False, "description": "message can't be edited"}
        return {"ok": True, "result": {"message_id": 1}}


def sent(bot, method="sendMessage"):
    return [p for m, p in bot.out if m == method]


def labels(markup):
    return [b.get("callback_data") or b.get("url") for row in markup["inline_keyboard"] for b in row]


def test_help_command_sends_contents_with_section_buttons_and_links():
    bot = Stub(p2p.Config())
    arun(bot.handle("/help"))
    msg = sent(bot)[-1]
    assert "Справка" in msg["text"] and "/safety" in msg["text"]
    cb = labels(msg["reply_markup"])
    assert [c for c in cb if c.startswith("help:")] == [f"help:{k}" for k in B.HELP_SECTIONS]
    assert any(c.startswith("https://www.bybit.com") for c in cb)            # ссылки на площадки — внизу


def test_help_section_edits_same_message_and_marks_open_section():
    bot = Stub(p2p.Config())
    arun(bot.on_callback({"id": "1", "data": "help:paper", "message": {"message_id": 7, "chat": {"id": 1}}}))
    edit = sent(bot, "editMessageText")[-1]
    assert edit["message_id"] == 7 and "Сухой прогон и бумажные симуляции" in edit["text"]
    assert "/paper report" in edit["text"]
    texts = [b["text"] for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert "• 🧪 Сухой прогон" in texts and "⬅️ Оглавление" in texts
    arun(bot.on_callback({"id": "2", "data": "help:", "message": {"message_id": 7, "chat": {"id": 1}}}))
    assert "Справка" in sent(bot, "editMessageText")[-1]["text"]


def test_help_section_falls_back_to_new_message_when_edit_fails():
    bot = Stub(p2p.Config())
    bot.edit_ok = False
    arun(bot.on_callback({"id": "1", "data": "help:labels", "message": {"message_id": 7, "chat": {"id": 1}}}))
    assert "Метки надёжности" in sent(bot)[-1]["text"]


def test_every_section_fits_telegram_and_unknown_section_is_contents():
    for key in B.HELP_SECTIONS:
        text, _ = B.help_view(key)
        assert len(text) < 4096
    assert B.help_view("nope")[0] == B.HELP_INTRO


def test_guest_help_stays_one_message_with_links(monkeypatch):
    """Кнопки гостя обрабатывает запиненный on_guest_callback (help: он не знает) — гостю прежняя справка."""
    monkeypatch.setenv("TG_GUESTS", "42")
    bot = Stub(p2p.Config())
    arun(bot.on_update({"message": {"message_id": 3, "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
                                    "text": "/help"}}))
    msg = sent(bot)[-1]
    assert msg["chat_id"] == "42" and msg["text"] == B.GUIDE
    assert not [c for c in labels(msg["reply_markup"]) if c.startswith("help:")]


def test_status_brief_main_lines_and_details_button(tmp_path):
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.start_ts = time.time() - 3725
    bot.last_scan_ts, bot.last_scan_duration = time.time() - 1, 1.5
    d = (5.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "r")
    bot.last = p2p.Snapshot(88.0, "t", {}, {}, [d], {}, {}, {f"v{i}/USDT": "TimeoutError" for i in range(6)})
    text, kb = bot.status_brief(str(tmp_path / "none.json"))
    lines = text.split("\n")
    assert lines[0].startswith("📟 <b>Статус</b>") and "аптайм 01:02:0" in lines[0]
    assert lines[1].startswith("🟢 Скан") and "(1.5 с)" in lines[1]
    assert lines[2] == "⚠️ Ошибки площадок (6): v0/USDT, v1/USDT, v2/USDT, v3/USDT +2"
    assert lines[3] == "🔔 Связок выше порога 2%: 1"
    assert len(text) < len(bot.status_view(str(tmp_path / "none.json")))
    assert labels(kb) == ["status_full", "status"]


def test_status_brief_flags_stale_scan_and_no_scan(tmp_path):
    bot = Stub(p2p.Config())
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "⏳ Скана ещё не было" in text
    bot.last_scan_ts, bot.last_scan_duration = time.time() - 600, 2.0
    bot.last = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "🔴 Скан" in text and "10 мин назад" in text and "✅ Ошибок нет" in text
