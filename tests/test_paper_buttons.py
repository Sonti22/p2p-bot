"""Сухой прогон включается кнопкой под «/paper»: ссылка «/paper» в тексте шлёт команду без аргумента."""
import asyncio
import functools

import bot as B
import p2p
import paper
from test_bot import Stub, texts
from test_guests import Stub as GuestStub, msg


def _env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    return env


def _buttons(markup):
    return {b["callback_data"]: b["text"] for row in markup["inline_keyboard"] for b in row}


def _press(bot, data):
    asyncio.run(bot.on_callback({"id": "1", "data": data, "message": {"message_id": 7}}))
    return [p for m, p in bot.out if m == "editMessageText"][-1]


def test_paper_without_arg_and_dev_button_show_control_buttons(monkeypatch):
    monkeypatch.delenv("PAPER", raising=False)
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper(""))
    asyncio.run(bot.on_callback({"id": "1", "data": "paper", "message": {"message_id": 7}}))
    sent = [p for m, p in bot.out if m == "sendMessage"]
    for p in sent[-2:]:
        buttons = _buttons(p["reply_markup"])
        assert buttons["paper_set:on"] == "▶️ Включить" and "paper_report" in buttons


def test_enable_button_turns_dry_run_on_right_away(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    env = _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    edit = _press(bot, "paper_set:on")
    assert paper.settings()["on"] and "PAPER=1" in env.read_text()
    assert "🟢 включён" in edit["text"] and edit["message_id"] == 7
    assert _buttons(edit["reply_markup"])["paper_set:off"] == "⏹ Выключить"
    edit = _press(bot, "paper_set:off")
    assert not paper.settings()["on"] and "⚪ выключен" in edit["text"]


def test_amount_buttons_mark_current_and_ignore_unknown_values(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    env = _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    assert _buttons(bot.paper_markup())["paper_amt:10000"].startswith("✓")
    edit = _press(bot, "paper_amt:20000")
    assert paper.settings()["amount"] == 20000 and "PAPER_AMOUNT=20000" in env.read_text()
    assert _buttons(edit["reply_markup"])["paper_amt:20000"].startswith("✓")
    _press(bot, "paper_amt:999999")                       # чужое значение в callback — не применяем
    assert paper.settings()["amount"] == 20000


def test_report_button_sends_report(monkeypatch):
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "paper_report", "message": {"message_id": 7}}))
    assert "Отчёт сухого прогона" in texts(bot)[-1]


def test_guest_cannot_press_paper_buttons(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    env = _env(monkeypatch, tmp_path)
    bot = GuestStub(p2p.Config(), guests=["42"])
    for data in ("paper", "paper_set:on", "paper_amt:20000", "paper_report"):
        before = len(bot.out)
        cq = {"id": "1", "data": data, "message": {"chat": {"id": 42}, "message_id": 5}}
        asyncio.run(bot.on_update({"callback_query": cq}))
        assert [m for m, _ in bot.out[before:]] == ["answerCallbackQuery"], data
    assert not paper.settings()["on"] and not env.exists()
    asyncio.run(bot.on_update(msg(42, "/paper on")))
    assert not paper.settings()["on"]
