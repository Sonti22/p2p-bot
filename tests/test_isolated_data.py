"""Тесты не видят состояние живого бота (data/, logs/, .env, .dev_status.json): смоук-тест launcher запускает
их в его папке. Проверки сначала смотрят, куда пойдёт запись, и только потом пишут — регрессия изоляции даёт
красный тест, а не запись в файлы бота."""
import json
import os
import sqlite3

import pytest

import accounts
import bot as B
import paper
import p2p
from conftest import ROOT, STATE_FILES, _ATTRS, _FUNCS, _state_rel

COLLECTED_KEYS_PATH = accounts.KEYS_PATH   # вычислено при сборке тестов, до фикстур функции


def test_all_known_state_files_are_found():
    assert {"data/keys.json", "data/topics.json", "data/alerts.db", "data/blacklist.db", "data/history.db",
            "data/paper.db", "data/paper_report.csv", "data/presets.json", "data/trades.db",
            "logs/bot.log", ".env", ".dev_status.json"} <= set(STATE_FILES.values())


def test_every_found_constant_and_default_is_redirected(_isolated_data):
    """Ни константа модуля или класса, ни `path=...` по умолчанию у функций и методов не ведут в состояние бота."""
    for owner, attr in _ATTRS:
        assert _state_rel(getattr(owner, attr)) is None, (owner, attr)
    for func in _FUNCS:
        defaults = (*(func.__defaults__ or ()), *(func.__kwdefaults__ or {}).values())
        assert not [d for d in defaults if _state_rel(d)], func.__qualname__
    assert B.Bot.status_view.__defaults__[0] == str(_isolated_data.parent / ".dev_status.json")   # метод класса


def test_redirect_is_active_at_collection_time():
    assert _state_rel(COLLECTED_KEYS_PATH) is None


def test_owner_keys_in_working_dir_are_invisible(tmp_path, monkeypatch):
    live = tmp_path / "live" / "data"
    live.mkdir(parents=True)
    (live / "keys.json").write_text(json.dumps({"bybit": {"key": "LIVEKEY1234567890AB", "secret": "S" * 35,
                                                          "verified": "unsafe"}}), encoding="utf-8")
    monkeypatch.chdir(live.parent)
    assert accounts.keys("bybit") is None
    text, _ = B.account_view("bybit")
    assert "LIVEKEY" not in text and "90AB" not in text


def test_default_path_writes_land_in_tmp(_isolated_data):
    target = str(_isolated_data / "paper.db")
    assert paper.init_balance.__defaults__[0] == target and paper.get_balance.__defaults__[0] == target
    paper.init_balance(10000)
    assert paper.get_balance() == 10000 and os.path.exists(target)


def test_save_env_goes_to_tmp_env(_isolated_data):
    env = str(_isolated_data.parent / ".env")
    assert B.save_env.__defaults__[0] == env and p2p.ENV_PATH == env and p2p.load_env.__defaults__[0] == env
    B.save_env("ISOLATION_PROBE", "1")
    assert "ISOLATION_PROBE=1" in open(env, encoding="utf-8").read()


# Пробы целятся в несуществующую подпапку data/ (или делают listdir по файлу): если сторож сломается, операция
# упадёт сама и ничего не создаст и не прочитает в папке живого бота.
MISSING = os.path.join("data", "zz_isolation_probe_missing_dir")


@pytest.mark.parametrize("touch", [
    lambda p: open(os.path.join(p, MISSING, "x.json"), "w"),
    lambda p: open(os.path.join(p, MISSING, "x.json"), encoding="utf-8"),
    lambda p: sqlite3.connect(os.path.join(p, MISSING, "x.db")),
    lambda p: os.listdir(os.path.join(p, MISSING)),
    lambda p: os.remove(os.path.join(p, "logs", "zz_isolation_probe_missing_dir", "x.log")),
    lambda p: os.listdir(os.path.join(p, ".env")),
])
def test_guard_blocks_any_touch_of_live_state(touch):
    """Сторож срабатывает до операции: путь, который подмена не нашла, не открывается и не создаётся."""
    with pytest.raises(PermissionError, match="состоянию живого бота"):
        touch(ROOT)
    assert not os.path.exists(os.path.join(ROOT, MISSING))


def test_guard_resolves_relative_paths_from_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    with pytest.raises(PermissionError):
        open(os.path.join(MISSING, "keys.json"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)                       # та же относительная запись в чужой папке — можно
    os.makedirs(MISSING)
    open(os.path.join(MISSING, "keys.json"), "w").close()
