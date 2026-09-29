"""Суточные резервные копии баз (backup.py): согласованная копия SQLite, JSON, ротация, без ключей и снимков."""
import os
import sqlite3
from datetime import datetime

import backup
import bot as B
import p2p
from helpers import arun

NOW = datetime(2026, 9, 28, 9, 30, tzinfo=backup.MSK).timestamp()


def _data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    con = sqlite3.connect(d / "trades.db")
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()
    con.close()
    (d / "presets.json").write_text('{"a": 1}', encoding="utf-8")
    (d / "keys.json").write_text('{"secret": "FAKE"}', encoding="utf-8")
    (d / "snapshots.db").write_bytes(b"big")
    return str(d)


def _fresh(monkeypatch):
    monkeypatch.setattr(backup, "_state", {"tried": 0.0})


def test_run_copies_listed_files_only(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    dest, files = backup.run(NOW, data)
    assert os.path.basename(dest) == "20260928-0930" and files == ["trades.db", "presets.json"]
    con = sqlite3.connect(os.path.join(dest, "trades.db"))
    assert con.execute("SELECT x FROM t").fetchall() == [(42,)]
    con.close()
    assert sorted(os.listdir(dest)) == ["presets.json", "trades.db"]            # без ключей и снимков


def test_run_copies_hedge_circles_db(tmp_path, monkeypatch):
    """Хеджи кругов (data/hedge_circles.db, trading/hedge.py) — в копии, согласованным снимком SQLite."""
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    con = sqlite3.connect(os.path.join(data, "hedge_circles.db"))
    con.execute("CREATE TABLE hedges (id INTEGER PRIMARY KEY, grp TEXT, status TEXT)")
    con.execute("INSERT INTO hedges (grp, status) VALUES ('cycle:trade:1', 'open')")
    con.commit()
    con.close()
    dest, files = backup.run(NOW, data)
    assert "hedge_circles.db" in files
    con = sqlite3.connect(os.path.join(dest, "hedge_circles.db"))
    assert con.execute("SELECT grp, status FROM hedges").fetchall() == [("cycle:trade:1", "open")]
    con.close()


def test_run_copies_core_journal_in_wal_with_a_live_writer(tmp_path, monkeypatch):
    """Журнал ордеров ядра (data/trading.db) — в WAL, и бот держит его открытым и пишет. sqlite3 backup API снимает
    согласованную копию: закоммиченное (ещё в -wal, без checkpoint) — есть, незакоммиченное пишущего — нет; копия не
    ждёт и не ломает пишущего, а его коммит после копии проходит."""
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    src = os.path.join(data, "trading.db")
    writer = sqlite3.connect(src, isolation_level=None, timeout=0)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")                         # всё закоммиченное — только в -wal
        writer.execute("CREATE TABLE orders (client_id TEXT, state TEXT)")
        writer.execute("INSERT INTO orders VALUES ('t1', 'filled')")
        assert os.path.getsize(src + "-wal") > 0
        writer.execute("BEGIN IMMEDIATE")                                    # пишущий держит транзакцию во время копии
        writer.execute("INSERT INTO orders VALUES ('t2', 'sending')")
        dest, files = backup.run(NOW, data)
        writer.execute("COMMIT")                                             # и спокойно её завершает
        assert writer.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    finally:
        writer.close()
    assert "trading.db" in files
    copy = sqlite3.connect(os.path.join(dest, "trading.db"))
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT client_id, state FROM orders").fetchall() == [("t1", "filled")]
    finally:
        copy.close()


def test_rotation_keeps_last_n_and_ignores_foreign_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "2")
    data = _data(tmp_path)
    os.makedirs(os.path.join(data, "backup", "мои-файлы"))
    for day in range(4):
        _fresh(monkeypatch)
        backup.run(NOW + day * 86400, data)
    assert backup.copies(data) == ["20260930-0930", "20261001-0930"]
    assert os.path.isdir(os.path.join(data, "backup", "мои-файлы"))


def test_due_daily_by_folder_name_and_off_at_zero(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = _data(tmp_path)
    assert backup.due(NOW, data)
    backup.run(NOW, data)
    _fresh(monkeypatch)
    assert not backup.due(NOW + 3600, data)
    assert backup.due(NOW + 86400, data)
    monkeypatch.setenv("BACKUP_KEEP", "0")
    assert not backup.due(NOW + 86400, data) and backup.run(NOW, data) == (None, [])
    for bad in ("abc", "-3"):
        monkeypatch.setenv("BACKUP_KEEP", bad)
        assert backup.keep() == backup.KEEP_DEFAULT


def test_failed_attempt_waits_retry_period(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("BACKUP_KEEP", raising=False)
    data = str(tmp_path / "nodata")
    os.makedirs(data)
    monkeypatch.setattr(backup.sqlite3, "connect", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    open(os.path.join(data, "trades.db"), "w").close()
    try:
        backup.run(NOW, data)
    except OSError:
        pass
    assert not backup.due(NOW + 60, data)                 # сбой — не повторять каждый скан
    assert backup.due(NOW + backup.RETRY, data)           # и неудачная копия не засчитана как сделанная
    assert os.listdir(os.path.join(data, "backup")) == []  # временная папка убрана


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)


def test_bot_runs_one_background_backup(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(backup, "due", lambda: not calls)
    monkeypatch.setattr(backup, "run", lambda: calls.append(1) or ("/x/20260928-0930", ["trades.db"]))
    bot = Stub(p2p.Config())

    async def go():
        bot.schedule_backup()
        bot.schedule_backup()
        await bot.backup_task
        bot.schedule_backup()

    arun(go())
    assert calls == [1]
    monkeypatch.setattr(backup, "due", lambda: True)
    monkeypatch.setattr(backup, "run", lambda: (_ for _ in ()).throw(OSError("disk full")))
    arun(bot.run_backup())
    assert "backup: disk full" in caplog.text
