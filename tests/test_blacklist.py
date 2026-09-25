import sqlite3
import time

import blacklist


def test_add_list_and_blocked(tmp_path):
    db = str(tmp_path / "blacklist.db")
    blacklist.add("Bybit", "Плохой Мерчант", path=db)
    blacklist.add("BestChange", "Обменник [TRC20]", path=db)
    rows = blacklist.list_all(path=db)
    assert [(ex, nick) for _, ex, nick, *_ in rows] == [("BestChange", "Обменник [TRC20]"), ("Bybit", "Плохой Мерчант")]
    assert blacklist.blocked(path=db) == {("Bybit", "Плохой Мерчант"), ("BestChange", "Обменник [TRC20]")}


def test_add_duplicate_ignored(tmp_path):
    db = str(tmp_path / "blacklist.db")
    blacklist.add("Bybit", "nick", path=db)
    blacklist.add("Bybit", "nick", path=db)
    assert len(blacklist.list_all(path=db)) == 1


def test_remove(tmp_path):
    db = str(tmp_path / "blacklist.db")
    blacklist.add("Bybit", "nick", path=db)
    entry_id = blacklist.list_all(path=db)[0][0]
    blacklist.remove(entry_id, path=db)
    assert blacklist.list_all(path=db) == []


def test_list_and_blocked_empty_when_db_missing(tmp_path):
    db = str(tmp_path / "none.db")
    assert blacklist.list_all(path=db) == []
    assert blacklist.blocked(path=db) == set()


def test_add_records_time_and_returns_id_duplicate_keeps_first_time(tmp_path):
    db = str(tmp_path / "blacklist.db")
    first = time.time() - 5 * 86400
    entry_id = blacklist.add("Bybit", "nick", path=db, ts=first)
    assert blacklist.add("Bybit", "nick", path=db) == entry_id           # повтор — тот же id, дата прежняя
    assert blacklist.list_all(path=db) == [(entry_id, "Bybit", "nick", first, "")]


def test_old_db_without_columns_is_migrated(tmp_path):
    db = str(tmp_path / "blacklist.db")
    con = sqlite3.connect(db)   # база прошлой версии: только (ex, nick)
    con.execute("CREATE TABLE blacklist (id INTEGER PRIMARY KEY AUTOINCREMENT, ex TEXT, nick TEXT, UNIQUE(ex, nick))")
    con.execute("INSERT INTO blacklist (ex, nick) VALUES ('Bybit', 'старый')")
    con.commit()
    con.close()
    assert blacklist.list_all(path=db) == [(1, "Bybit", "старый", None, "")]   # даты нет, запись на месте
    assert blacklist.blocked(path=db) == {("Bybit", "старый")}
    new_id = blacklist.add("MEXC", "новый", path=db, ts=1000.0)
    assert blacklist.set_note(1, "не отпускал крипту", path=db)
    rows = {r[0]: r for r in blacklist.list_all(path=db)}
    assert rows[1] == (1, "Bybit", "старый", None, "не отпускал крипту")
    assert rows[new_id] == (new_id, "MEXC", "новый", 1000.0, "")


def test_set_note_unknown_id_and_normalized_text(tmp_path):
    db = str(tmp_path / "blacklist.db")
    assert blacklist.set_note(1, "текст", path=db) is False              # базы ещё нет
    entry_id = blacklist.add("Bybit", "nick", path=db)
    assert blacklist.set_note(entry_id + 100, "текст", path=db) is False
    assert blacklist.set_note(entry_id, "  долго\nне   платил ", path=db) is True
    assert blacklist.list_all(path=db)[0][4] == "долго не платил"
    blacklist.set_note(entry_id, "x" * 500, path=db)
    assert len(blacklist.list_all(path=db)[0][4]) == blacklist.NOTE_MAX
