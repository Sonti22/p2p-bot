"""Тесты не видят data/ живого бота: смоук-тест launcher запускает их в папке, где уже подключены ключи."""
import importlib
import json
import os

import accounts
import bot as B
import p2p
from conftest import DATA_PATHS


def test_owner_keys_in_working_dir_are_invisible(tmp_path, monkeypatch):
    live = tmp_path / "live" / "data"
    live.mkdir(parents=True)
    (live / "keys.json").write_text(json.dumps({"bybit": {"key": "LIVEKEY1234567890AB", "secret": "S" * 35,
                                                          "verified": "unsafe"}}), encoding="utf-8")
    monkeypatch.chdir(live.parent)
    assert accounts.keys("bybit") is None
    text, _ = B.account_view("bybit")
    assert "LIVEKEY" not in text and "90AB" not in text


def test_every_data_file_points_to_tmp(_isolated_data):
    for module, attr, _ in DATA_PATHS:
        assert getattr(importlib.import_module(module), attr).startswith(str(_isolated_data)), (module, attr)


def test_save_env_never_touches_bot_env(_isolated_data):
    before = open(p2p.ENV_PATH, "rb").read() if os.path.exists(p2p.ENV_PATH) else None
    B.save_env("ISOLATION_PROBE", "1")
    after = open(p2p.ENV_PATH, "rb").read() if os.path.exists(p2p.ENV_PATH) else None
    assert after == before
    assert "ISOLATION_PROBE=1" in (_isolated_data / ".env").read_text(encoding="utf-8")
