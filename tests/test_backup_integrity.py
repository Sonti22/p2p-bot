"""Проверка целостности суточных копий (backup.py): испорченная база (битая шапка или страница) и битый JSON не
попадают в новую копию, остальные файлы копируются как раньше, ротация сохраняет последнюю исправную копию
испорченного файла, владелец получает ровно одно сообщение о порче и одно о восстановлении."""
import os
import sqlite3
from datetime import datetime

import backup
import p2p
from helpers import arun
from test_bot import Stub, texts

NOW = datetime(2026, 9, 28, 9, 30, tzinfo=backup.MSK).timestamp()


def _fresh(monkeypatch):
    monkeypatch.setattr(backup, "_state", {"tried": 0.0})


def _data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return str(d)


def _good_db(data, fname="trades.db"):
    con = sqlite3.connect(os.path.join(data, fname))
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_bad_header_skips_file_others_copied_then_clean_after_fix(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    with open(os.path.join(data, "trades.db"), "wb") as f:
        f.write(b"not a database" * 200)
    (tmp_path / "data" / "presets.json").write_text('{"a": 1}', encoding="utf-8")
    dest, done = backup.run(NOW, data)
    assert done == ["presets.json"]
    assert not os.path.exists(os.path.join(dest, "trades.db"))
    bad = backup.bad()
    assert len(bad) == 1 and bad[0][0] == "trades.db" and "not a database" in bad[0][1]

    _fresh(monkeypatch)
    os.remove(os.path.join(data, "trades.db"))
    _good_db(data)
    backup.run(NOW + 86400, data)
    assert backup.bad() == []


def test_corrupted_page_at_intact_header_skipped(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    path = os.path.join(data, "trades.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (x)")
    for _ in range(2000):
        con.execute("INSERT INTO t VALUES (?)", ("x" * 100,))
    con.commit()
    con.close()
    with open(path, "r+b") as f:
        f.seek(4096 * 3)
        f.write(b"\xff" * 4096)
    dest, done = backup.run(NOW, data)
    assert "trades.db" not in done
    assert not os.path.exists(os.path.join(dest, "trades.db"))
    bad = dict(backup.bad())
    assert "trades.db" in bad


def test_bad_json_skipped(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    (tmp_path / "data" / "presets.json").write_text("{oops", encoding="utf-8")
    (tmp_path / "data" / "favorites.json").write_text("[1]", encoding="utf-8")
    dest, done = backup.run(NOW, data)
    assert done == []
    assert sorted(os.listdir(dest)) == []
    bad = dict(backup.bad())
    assert bad.get("presets.json") == "битый JSON"
    assert bad.get("favorites.json") == "не словарь"


def test_non_database_failure_not_masked_as_corruption_connect(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    _good_db(data)
    monkeypatch.setattr(backup.sqlite3, "connect", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    try:
        backup.run(NOW, data)
    except OSError:
        pass
    else:
        assert False, "OSError ожидался"
    assert backup.bad() == []
    assert os.listdir(os.path.join(data, "backup")) == []


def test_non_database_failure_not_masked_as_corruption_quick_check(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    _good_db(data)

    def boom(path):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup, "_check_db", boom)
    try:
        backup.run(NOW, data)
    except sqlite3.OperationalError:
        pass
    else:
        assert False, "OperationalError ожидался"
    assert backup.bad() == []
    assert os.listdir(os.path.join(data, "backup")) == []


def test_rotation_keeps_last_good_copy_of_bad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "2")
    data = _data(tmp_path)
    path = os.path.join(data, "trades.db")

    def write_good():
        if os.path.exists(path):
            os.remove(path)
        _good_db(data)

    def write_bad():
        with open(path, "wb") as f:
            f.write(b"not a database" * 200)

    for day, step in enumerate((write_good, write_good, write_bad, write_bad)):
        _fresh(monkeypatch)
        step()
        backup.run(NOW + day * 86400, data)
    copies = backup.copies(data)
    assert copies == ["20260929-0930", "20260930-0930", "20261001-0930"]   # day1 (последняя исправная) пережила ротацию
    assert backup.last_good("trades.db", data) == "20260929-0930"

    _fresh(monkeypatch)
    write_good()
    backup.run(NOW + 4 * 86400, data)
    assert backup.copies(data) == ["20261001-0930", "20261002-0930"]   # починка — лишняя копия на следующем прогоне ушла


def test_wal_db_copied_clean_and_leaves_no_wal_shm_in_copy(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    src = os.path.join(data, "paper.db")
    writer = sqlite3.connect(src, isolation_level=None, timeout=0)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (x)")
        writer.execute("INSERT INTO t VALUES (1)")
        dest, done = backup.run(NOW, data)
    finally:
        writer.close()
    assert done == ["paper.db"]
    assert backup.bad() == []
    assert sorted(os.listdir(dest)) == ["paper.db"]


def _stub():
    bot = Stub(p2p.Config())
    bot.topics = {"dev": 55}
    return bot


def test_run_backup_alerts_once_and_recovers(monkeypatch, caplog):
    monkeypatch.setattr(backup, "run", lambda: ("/x/20260928-0930", ["presets.json"]))
    monkeypatch.setattr(backup, "bad", lambda: [("trades.db", "file is not a database <x>")])
    monkeypatch.setattr(backup, "last_good", lambda fname, data_dir=backup.DATA_DIR: "20260927-0930")
    bot = _stub()

    arun(bot.run_backup())
    sent = texts(bot)
    assert len(sent) == 1
    text = sent[0]
    assert "trades.db" in text and "20260927-0930" in text and "&lt;x&gt;" in text
    thread_msgs = [p.get("message_thread_id") for m, p in bot.out if m == "sendMessage"]
    assert thread_msgs == [55]

    arun(bot.run_backup())   # тот же bad — тишина
    assert texts(bot) == sent

    monkeypatch.setattr(backup, "bad", lambda: [])
    arun(bot.run_backup())
    assert texts(bot)[-1] == "✅ trades.db снова проходит проверку целостности и попал в копию."

    arun(bot.run_backup())   # уже без файла — тишина
    assert len(texts(bot)) == 2


def test_run_backup_alert_send_failure_does_not_set_flag_or_crash(monkeypatch, caplog):
    monkeypatch.setattr(backup, "run", lambda: ("/x/20260928-0930", []))
    monkeypatch.setattr(backup, "bad", lambda: [("trades.db", "file is not a database")])
    monkeypatch.setattr(backup, "last_good", lambda fname, data_dir=backup.DATA_DIR: None)
    bot = _stub()

    async def failing_call(method, **p):
        return {"ok": False}

    monkeypatch.setattr(bot, "call", failing_call)
    arun(bot.run_backup())
    assert not bot.backup_bad_alerted   # не подтверждено Telegram — флаг не выставлен

    async def raising_call(method, **p):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "call", raising_call)
    with caplog.at_level("WARNING"):
        arun(bot.run_backup())   # исключение из send не должно ронять run_backup
    assert "backup alert:" in caplog.text
