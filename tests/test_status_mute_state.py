"""/status и «📋 Подробно» должны прямо говорить, что сигналы сейчас не отправляются: пауза или тихие часы.
Без паузы и тихих часов тексты не меняются ни на байт (регресс test_help_status_ux.py). Без сети."""
import time

import bot as B
import p2p
from helpers import arun, make_ad


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def sent(bot, method="sendMessage"):
    return [p for m, p in bot.out if m == method]


def labels(markup):
    return [b.get("callback_data") or b.get("url") for row in markup["inline_keyboard"] for b in row]


def _bot(**kw):
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.paused = False
    bot.pause_until = 0.0
    bot.quiet_on = False
    for k, v in kw.items():
        setattr(bot, k, v)
    return bot


def test_paused_forever_line_in_status_brief_and_view_even_before_first_scan(tmp_path):
    bot = _bot(paused=True)
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "на паузе (бессрочно)" in text and "/resume" in text
    text2 = bot.status_view(str(tmp_path / "none.json"))
    assert "на паузе (бессрочно)" in text2 and "/resume" in text2
    assert "Скан ещё не выполнялся." in text2
    assert text2.index("на паузе") < text2.index("Скан ещё не выполнялся.")


def test_pause_until_line_and_expiry(tmp_path):
    until = time.time() + 1800
    bot = _bot(pause_until=until)
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert f"на паузе до {B._hhmm_msk(until)} МСК" in text
    bot.pause_until = time.time() - 5
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "на паузе" not in text and "⏸" not in text


def test_quiet_hours_line_only_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "in_quiet_hours", lambda spec, ts=None: True)
    bot = _bot(quiet_on=True, quiet_hours="01:00-08:00")
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "Тихие часы до 08:00 МСК" in text
    bot.quiet_on = False
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "Тихие часы" not in text and "🌙" not in text


def test_quiet_hours_garbage_spec_does_not_crash_and_has_no_time(monkeypatch):
    monkeypatch.setattr(B, "in_quiet_hours", lambda spec, ts=None: True)
    bot = _bot(quiet_on=True, quiet_hours="мусор")
    line = bot.mute_line()
    assert line == "🌙 Тихие часы — сигналы копятся для дайджеста"


def test_no_pause_no_quiet_hours_regression_matches_existing_texts(tmp_path):
    bot = _bot()
    bot.last_scan_ts, bot.last_scan_duration = time.time() - 1, 1.5
    d = (5.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "r")
    bot.last = p2p.Snapshot(88.0, "t", {}, {}, [d], {}, {}, {f"v{i}/USDT": "TimeoutError" for i in range(6)})
    assert bot.mute_line() is None
    text, kb = bot.status_brief(str(tmp_path / "none.json"))
    assert "⏸" not in text and "🌙" not in text and "(не отправляются)" not in text
    lines = text.split("\n")
    assert lines[3] == "🔔 Связок выше порога 2%: 1"
    full = bot.status_view(str(tmp_path / "none.json"))
    assert "⏸" not in full and "🌙" not in full and "(не отправляются)" not in full


def test_mute_suffix_and_position_on_active_pause(tmp_path):
    bot = _bot(paused=True)
    bot.last_scan_ts, bot.last_scan_duration = time.time() - 1, 1.5
    bot.last = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})
    text, kb = bot.status_brief(str(tmp_path / "none.json"))
    lines = text.split("\n")
    assert lines[1].startswith("🟢 Скан")
    assert lines[2].startswith("⏸ Сигналы на паузе")
    assert lines[-1].endswith("(не отправляются)")
    assert labels(kb) == ["status_full", "status"]


def test_buttons_unchanged_without_and_with_pause(tmp_path):
    bot = _bot()
    _, kb = bot.status_brief(str(tmp_path / "none.json"))
    assert labels(kb) == ["status_full", "status"]
    bot.paused = True
    _, kb2 = bot.status_brief(str(tmp_path / "none.json"))
    assert labels(kb2) == ["status_full", "status"]


def test_resume_and_pause_commands_update_mute_line(tmp_path):
    bot = _bot(paused=True)
    assert bot.mute_line() is not None
    arun(bot.handle("/resume"))
    assert bot.mute_line() is None
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "на паузе" not in text
    arun(bot.handle("/pause"))
    text, _ = bot.status_brief(str(tmp_path / "none.json"))
    assert "бессрочно" in text
