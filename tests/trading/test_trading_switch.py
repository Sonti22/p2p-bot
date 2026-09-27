"""Торговое ядро, switch: TRADING включает только .env на ПК, режим из Telegram — только вниз, trading_state.json."""
import ast
import inspect
import json
import os

import pytest

from trading import gates, journal, keys, risk, switch, venues


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("TRADING", "TRADING_MODE"):
        monkeypatch.delenv(name, raising=False)


def env_file(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("text,on", [
    ("TRADING=1\n", True), ("TRADING=1\nTRADING=1\n", True), ("trading=1 # включил владелец\n", True),
    ("﻿TRADING=1\n", True), (" TRADING = 1 \n", True),
    ("TRADING=0\n", False), ("TRADING=1\nTRADING=0\n", False), ("TRADING=0\nTRADING=1\n", False),
    ("TRADING=true\n", False), ("TRADING=yes\n", False), ("TRADING=\n", False), ("# TRADING=1\n", False),
    ("", False), ("TRADING=1#x\n", False), ("TRADING_MODE=auto\n", False),
])
def test_only_env_file_enables(tmp_path, monkeypatch, text, on):
    monkeypatch.setenv("TRADING", "1")          # окружение Windows/родителя само не включает
    switch.switch_from_file(env_file(tmp_path, text))
    assert switch.enabled() is on


def test_missing_file_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "auto")
    assert switch.switch_from_file(str(tmp_path / "нет.env")) == (False, "paper")


@pytest.mark.parametrize("text,env,mode", [
    ("TRADING=1\nTRADING_MODE=minlot\n", None, "minlot"),
    ("TRADING=1\nTRADING_MODE=AUTO\n", None, "auto"),
    ("TRADING=1\nTRADING_MODE=auto\n", "confirm", "confirm"),   # окружение может понизить
    ("TRADING=1\nTRADING_MODE=minlot\n", "auto", "minlot"),     # но не повысить
    ("TRADING=1\nTRADING_MODE=minlot\n", "мусор", "paper"),
    ("TRADING=1\nTRADING_MODE=minlot\nTRADING_MODE=auto\n", None, "paper"),   # противоречие — paper
    ("TRADING=1\nTRADING_MODE=turbo\n", None, "paper"), ("TRADING=1\n", "auto", "paper"),
    ("TRADING=1\nTRADING_MODE=\n", None, "paper"),
])
def test_mode_from_file_env_only_lowers(tmp_path, monkeypatch, text, env, mode):
    if env is not None:
        monkeypatch.setenv("TRADING_MODE", env)
    assert switch.switch_from_file(env_file(tmp_path, text))[1] == mode
    assert switch.mode() == mode


def test_telegram_can_only_lower_or_stop(monkeypatch):
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")
    assert not switch.lower("auto") and switch.mode() == "confirm"
    assert not switch.lower("турбо") and not switch.lower(None) and switch.mode() == "confirm"
    assert switch.lower("confirm") and switch.lower("minlot") and switch.mode() == "minlot"
    assert not switch.lower("confirm") and switch.mode() == "minlot"
    assert switch.can_open() == (True, "")
    switch.stop()
    assert not switch.enabled() and switch.mode() == "paper"
    assert switch.can_open() == (False, switch.OFF)
    assert not switch.lower("minlot")


def test_can_open_and_effective_mode(monkeypatch):
    assert switch.can_open() == (False, switch.OFF) and switch.effective_mode("auto") == "paper"
    monkeypatch.setenv("TRADING", "1")
    assert switch.can_open() == (False, switch.PAPER)          # режима нет — paper
    monkeypatch.setenv("TRADING_MODE", "auto")
    assert switch.effective_mode("minlot") == "minlot" and switch.effective_mode("auto") == "auto"
    assert switch.effective_mode("мусор") == "paper"
    monkeypatch.setenv("TRADING_MODE", "minlot")
    assert switch.effective_mode("auto") == "minlot"


def test_state_file_for_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "minlot")
    path = str(tmp_path / "data" / "trading_state.json")
    pos = [{"venue": "bybit", "symbol": "BTCUSDT", "side": "short", "size": venues.dec("0.01"), "position": "oneway",
            "entry": venues.dec("65000"), "liq": None}]
    data = switch.write_state(pos, open_orders=2, unknown_orders=1, path=path, now=1700000000)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == data == switch.read_state(path)
    assert data == {"version": 1, "ts": 1700000000, "trading": True, "mode": "minlot", "has_positions": True,
                    "open_positions": [{"venue": "bybit", "symbol": "BTCUSDT", "side": "short", "size": "0.01",
                                        "position": "oneway"}], "open_orders": 2, "unknown_orders": 1}
    assert switch.write_state([], path=path)["has_positions"] is False
    assert switch.STATE_PATH.replace("\\", "/").endswith("data/trading_state.json")


def test_no_code_path_enables_trading_or_raises_mode():
    """В коде ядра TRADING пишется только как "0", TRADING_MODE — только в lower (после проверки «не выше»),
    stop ("paper") и switch_from_file (не выше окружения). Никаких putenv/environ.update/setdefault."""
    for mod in (switch, journal, risk, gates, keys, venues):
        src = inspect.getsource(mod)
        assert "putenv" not in src and "environ.update" not in src and "setdefault(\"TRADING" not in src
        for node in ast.walk(ast.parse(src)):
            for t in getattr(node, "targets", []):
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "TRADING":
                    assert isinstance(node.value, ast.Constant) and node.value.value == "0"
    writers = []
    for node in ast.walk(ast.parse(inspect.getsource(switch))):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                for t in getattr(sub, "targets", []):
                    if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                            and t.slice.value == "TRADING_MODE":
                        writers.append(node.name)
    assert sorted(writers) == ["lower", "stop", "switch_from_file"]
    for mod in (journal, risk, gates, keys, venues):
        assert "TRADING_MODE\"] =" not in inspect.getsource(mod)


def test_launcher_money_keys_match():
    import launcher
    assert "TRADING" in launcher.MONEY_KEYS
    assert os.path.basename(switch.STATE_PATH) == "trading_state.json"


def test_stop_and_persist_writes_env_via_bot_save_env(tmp_path, monkeypatch):
    """«⛔ Стоп» как у выплат: сначала процесс, затем .env через save_env бота (настоящий bot.save_env во временный
    .env); после перезапуска switch_from_file не включит торговлю."""
    import bot as B
    path = env_file(tmp_path, "TRADING=1\nTRADING_MODE=auto\nOTHER=x\n")
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "auto")
    assert switch.stop_and_persist(lambda k, v: B.save_env(k, v, path)) is None
    assert not switch.enabled() and switch.mode() == "paper"
    assert open(path, encoding="utf-8").read().splitlines() == ["TRADING=0", "TRADING_MODE=paper", "OTHER=x"]
    monkeypatch.setenv("TRADING", "1")                                      # окружение не вернёт торговлю
    assert switch.switch_from_file(path) == (False, "paper")


def test_stop_and_persist_env_write_failure_still_stops(monkeypatch):
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")
    written = []

    def broken(k, v):
        os.environ[k] = "1"                                                 # «чужая» реализация пишет не то
        written.append(k)
        raise PermissionError("файл занят")
    assert switch.stop_and_persist(broken) == "PermissionError"
    assert not switch.enabled() and switch.mode() == "paper" and written == ["TRADING"]


def test_lower_and_persist(tmp_path, monkeypatch):
    import bot as B
    path = env_file(tmp_path, "TRADING=1\nTRADING_MODE=confirm\n")
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")
    assert switch.lower_and_persist("auto", lambda k, v: B.save_env(k, v, path)) == (False, None)
    assert open(path, encoding="utf-8").read().splitlines() == ["TRADING=1", "TRADING_MODE=confirm"]
    assert switch.lower_and_persist("minlot", lambda k, v: B.save_env(k, v, path)) == (True, None)
    assert switch.mode() == "minlot" and "TRADING_MODE=minlot" in open(path, encoding="utf-8").read()

    def broken(k, v):
        raise OSError("нет доступа")
    assert switch.lower_and_persist("paper", broken) == (True, "OSError") and switch.mode() == "paper"
