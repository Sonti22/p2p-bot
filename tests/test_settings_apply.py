"""Настройки, сохранённые из Telegram (save_env), действуют сразу, без перезапуска бота."""
import functools
import os

import bot as B
import p2p
import paper
from test_bot import Stub
from helpers import arun


def _env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    return env


def test_save_env_updates_process_environment(tmp_path):
    B.save_env("SOME_SETTING", "42", path=str(tmp_path / ".env"))
    assert os.environ["SOME_SETTING"] == "42"


def test_paper_on_off_takes_effect_immediately(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    env = _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    arun(bot.cmd_paper("on"))
    assert paper.settings()["on"] and "PAPER=1" in env.read_text()
    arun(bot.cmd_paper("off"))
    assert not paper.settings()["on"] and "PAPER=0" in env.read_text()


def test_paper_amount_takes_effect_immediately(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER_AMOUNT", raising=False)
    _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    arun(bot.cmd_paper("amount 20000"))
    assert paper.settings()["amount"] == 20000


def test_ladder_button_takes_effect_immediately(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    arun(bot.on_callback({"id": "1", "data": "paper_ladder:20000", "message": {"message_id": 9}}))
    assert paper.settings()["amount"] == 20000
