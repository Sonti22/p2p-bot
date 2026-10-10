"""Онбординг при первом /start: 3 шага кнопками — сумма круга -> банки -> порог сигнала."""

import bot as B
import p2p
from helpers import arun


class Stub(B.Bot):
    """Бот без сети: все вызовы Telegram пишутся в self.out."""
    def __init__(self, cfg, chat_id="1"):
        super().__init__(None, "x", chat_id, cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def texts(bot, method="sendMessage"):
    return [p["text"] for m, p in bot.out if m == method]


def edits(bot):
    return [p for m, p in bot.out if m == "editMessageText"]


def msg_update(chat_id="1", text="/start"):
    """Сообщение из личного чата: id отправителя равен id чата (владельцем становится только личный чат)."""
    return {"message": {"chat": {"id": chat_id, "type": "private"}, "from": {"id": chat_id}, "text": text,
                        "message_id": 5}}


def cb(data, message_id=1):
    return {"id": "cbid", "data": data, "message": {"message_id": message_id, "chat": {"id": "1"}}}


def test_setup_starts_onboarding_only_for_locally_configured_owner(tmp_path, monkeypatch):
    import functools
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(), chat_id="")
    arun(bot.on_update(msg_update(text="/start setup")))
    assert bot.chat_id == "" and bot.onboarding is None
    assert env.read_text(encoding="utf-8") == ""
    bot.chat_id = "1"                              # владелец настроен на ПК
    arun(bot.on_update(msg_update(text="/start setup")))
    assert bot.chat_id == "1"
    assert bot.onboarding == {"step": "amount", "banks": set()}
    sent = texts(bot)
    assert sent and "Шаг 1/3" in sent[-1]
    assert env.read_text(encoding="utf-8") == ""


def test_second_start_skips_onboarding():
    bot = Stub(p2p.Config())
    arun(bot.handle("/start"))
    assert bot.onboarding is None
    assert "Бот P2P-связок на связи" in texts(bot)[-1]


def test_amount_step_saves_amount_and_advances_to_banks(tmp_path, monkeypatch):
    import functools
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    bot.onboarding = {"step": "amount", "banks": set()}
    arun(bot.on_callback(cb("onb_amt:100000")))
    assert bot.cfg.amount == 100000
    assert "AMOUNT=100000" in env.read_text(encoding="utf-8")
    assert bot.onboarding["step"] == "banks"
    assert "Шаг 2/3" in edits(bot)[-1]["text"]


def test_forged_onboarding_amount_and_min_are_ignored(tmp_path, monkeypatch):
    """callback_data можно подделать: onb_amt:/onb_min: проверяются тем же парсером, что /amount и /min —
    nan, ноль и минус не попадут ни в cfg, ни в .env, шаг не переключается."""
    import functools
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(amount=50000, min_profit=1.0))
    bot.onboarding = {"step": "amount", "banks": set()}
    for data in ("onb_amt:nan", "onb_amt:0", "onb_amt:-5", "onb_amt:inf"):
        arun(bot.on_callback(cb(data)))
    assert bot.cfg.amount == 50000 and bot.onboarding["step"] == "amount"
    bot.onboarding["step"] = "min"
    for data in ("onb_min:nan", "onb_min:-5", "onb_min:1e308"):
        arun(bot.on_callback(cb(data)))
    assert bot.cfg.min_profit == 1.0 and bot.onboarding["step"] == "min"
    assert env.read_text(encoding="utf-8") == ""


def test_bank_toggle_marks_selected():
    bot = Stub(p2p.Config())
    bot.onboarding = {"step": "banks", "banks": set()}
    arun(bot.on_callback(cb(f"onb_bank:{B.ONBOARD_BANKS[0]}")))
    assert bot.onboarding["banks"] == {B.ONBOARD_BANKS[0]}
    buttons = edits(bot)[-1]["reply_markup"]["inline_keyboard"]
    label = next(b["text"] for row in buttons for b in row if b["callback_data"] == f"onb_bank:{B.ONBOARD_BANKS[0]}")
    assert label.startswith("✅")
    # повторный клик снимает выбор
    arun(bot.on_callback(cb(f"onb_bank:{B.ONBOARD_BANKS[0]}")))
    assert bot.onboarding["banks"] == set()


def test_bank_next_saves_include_pay_and_advances_to_min(tmp_path, monkeypatch):
    import functools
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    bot.onboarding = {"step": "banks", "banks": {"T-Bank"}}
    arun(bot.on_callback(cb("onb_bank_next")))
    assert bot.cfg.include_pay == ["t-bank"]
    assert "INCLUDE_PAY=t-bank" in env.read_text(encoding="utf-8")
    assert bot.onboarding["step"] == "min"
    assert "Шаг 3/3" in edits(bot)[-1]["text"]


def test_bank_next_with_no_selection_leaves_include_pay_empty(tmp_path, monkeypatch):
    import functools
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    bot.onboarding = {"step": "banks", "banks": set()}
    arun(bot.on_callback(cb("onb_bank_next")))
    assert bot.cfg.include_pay == []


def test_min_step_finishes_onboarding_and_sends_welcome(tmp_path, monkeypatch):
    import functools
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    bot.onboarding = {"step": "min", "banks": set()}
    arun(bot.on_callback(cb("onb_min:2")))
    assert bot.cfg.min_profit == 2
    assert "MIN_PROFIT=2" in env.read_text(encoding="utf-8")
    assert bot.onboarding is None
    assert "Готово" in edits(bot)[-1]["text"]
    assert "Бот P2P-связок на связи" in texts(bot)[-1]


def test_stray_onboarding_click_after_finish_is_ignored():
    bot = Stub(p2p.Config())
    bot.onboarding = None
    arun(bot.on_callback(cb("onb_min:5")))
    assert bot.cfg.min_profit != 5
    assert not edits(bot)


def test_click_from_wrong_step_is_ignored():
    bot = Stub(p2p.Config())
    bot.onboarding = {"step": "banks", "banks": set()}
    arun(bot.on_callback(cb("onb_min:5")))
    assert bot.cfg.min_profit != 5
    assert bot.onboarding["step"] == "banks"
