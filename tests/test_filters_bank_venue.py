"""Банки (INCLUDE_PAY) и «только внутри одной площадки» (SAME_VENUE_ONLY) видны и переключаются в «🎛 Фильтры»
и /filters: раньше их резали объявления, но поменять из Telegram было нельзя, а TOPIC_HINTS обещал /filters,
которого не было."""
import functools

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


def cb(data, message_id=1):
    return {"id": "cbid", "data": data, "message": {"message_id": message_id, "chat": {"id": "1"}}}


def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    return env


def test_filters_view_shows_banks_and_venue_lines_with_marks():
    cfg = p2p.Config(include_pay=["sberbank", "vtb"], same_venue_only=True)
    text, kb = B.filters_view(cfg)
    assert "Банки: sberbank, vtb" in text
    assert "Только внутри одной площадки: включено" in text
    buttons = {b["text"]: b["callback_data"] for row in kb["inline_keyboard"] for b in row}
    assert buttons["✅ Sberbank"] == "flt_b:Sberbank"
    assert buttons["⬜ T-Bank"] == "flt_b:T-Bank"
    assert buttons["✅ VTB"] == "flt_b:VTB"
    assert any(cd == "flt_sv" for cd in buttons.values())
    assert any(cd == "flt_b:*" for cd in buttons.values())   # список банков не пуст — кнопка сброса есть


def test_filters_view_no_reset_button_when_banks_empty():
    cfg = p2p.Config()
    text, kb = B.filters_view(cfg)
    assert "Банки: любые" in text
    assert "Только внутри одной площадки: выключено" in text
    buttons = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "flt_b:*" not in buttons


def test_flt_b_toggle_adds_and_removes_preserving_manual_values(tmp_path, monkeypatch):
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config(include_pay=["revolut"]))   # ручное значение не из ONBOARD_BANKS

    toast = bot.apply("flt_b:Sberbank")
    assert "sberbank" in bot.cfg.include_pay and "revolut" in bot.cfg.include_pay
    assert "включ" in toast.lower()
    assert "INCLUDE_PAY=revolut,sberbank" in env.read_text(encoding="utf-8")

    toast = bot.apply("flt_b:Sberbank")
    assert bot.cfg.include_pay == ["revolut"]
    assert "выключ" in toast.lower()
    assert "INCLUDE_PAY=revolut" in env.read_text(encoding="utf-8")


def test_flt_b_star_clears_include_pay(tmp_path, monkeypatch):
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config(include_pay=["sberbank", "revolut"]))
    toast = bot.apply("flt_b:*")
    assert bot.cfg.include_pay == []
    assert "любые" in toast.lower()
    assert "INCLUDE_PAY=" in env.read_text(encoding="utf-8")
    assert "INCLUDE_PAY=sberbank" not in env.read_text(encoding="utf-8")


def test_flt_b_forged_button_name_is_rejected(tmp_path, monkeypatch):
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config(include_pay=["sberbank"]))

    toast = bot.apply("flt_b:Evil\nSECRET=1")
    assert toast == "Нет такой кнопки"
    assert bot.cfg.include_pay == ["sberbank"]
    assert env.read_text(encoding="utf-8") == ""

    toast = bot.apply("flt_b:НетТакого")
    assert toast == "Нет такой кнопки"
    assert bot.cfg.include_pay == ["sberbank"]
    assert env.read_text(encoding="utf-8") == ""


def test_flt_sv_toggles_same_venue_only(tmp_path, monkeypatch):
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config())
    assert bot.cfg.same_venue_only is False

    toast = bot.apply("flt_sv")
    assert bot.cfg.same_venue_only is True
    assert "включ" in toast.lower()
    assert "SAME_VENUE_ONLY=1" in env.read_text(encoding="utf-8")

    toast = bot.apply("flt_sv")
    assert bot.cfg.same_venue_only is False
    assert "выключ" in toast.lower()
    assert "SAME_VENUE_ONLY=0" in env.read_text(encoding="utf-8")


def test_settings_view_banks_and_venue_lines_conditional():
    bot = Stub(p2p.Config())
    text, _ = bot.settings_view()
    assert "Банки:" not in text
    assert "Только внутри одной площадки" not in text

    bot.cfg.include_pay = ["sberbank"]
    bot.cfg.same_venue_only = True
    text, _ = bot.settings_view()
    assert "Банки: sberbank" in text
    assert "Только внутри одной площадки: включено" in text


def test_filters_command_owner_vs_guest(tmp_path, monkeypatch):
    env_file(tmp_path, monkeypatch)
    owner = Stub(p2p.Config())
    arun(owner.dispatch("/filters", ""))
    assert texts(owner) and "🎛" in texts(owner)[-1]

    guest = Stub(p2p.Config())
    token = B.REPLY_CHAT.set("2")
    try:
        arun(guest.dispatch("/filters", ""))
    finally:
        B.REPLY_CHAT.reset(token)
    assert texts(guest) == [B.GUEST_DENIED]
    assert guest.cfg.include_pay == []


def test_all_filter_buttons_callback_data_within_64_bytes():
    cfg = p2p.Config(include_pay=["sberbank"])
    _, kb = B.filters_view(cfg)
    for row in kb["inline_keyboard"]:
        for b in row:
            assert len(b["callback_data"].encode("utf-8")) <= 64, b["callback_data"]


def test_on_callback_flt_b_redraws_filters(tmp_path, monkeypatch):
    env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cb("flt_b:Sberbank")))
    assert "sberbank" in bot.cfg.include_pay
    redraw = edits(bot)
    assert redraw and "🎛" in redraw[-1]["text"]


def test_on_callback_flt_sv_redraws_filters(tmp_path, monkeypatch):
    env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config())
    arun(bot.on_callback(cb("flt_sv")))
    assert bot.cfg.same_venue_only is True
    redraw = edits(bot)
    assert redraw and "🎛" in redraw[-1]["text"]
