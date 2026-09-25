"""Блэклист мерчантов и обменников: SQLite data/blacklist.db. Кнопка «🚫 Не показывать» под сигналом
добавляет обе стороны связки, /blacklist показывает список (возраст записи, причина) с удалением; scan() их
отсеивает. Записи сами не удаляются — снимать или нет, решает владелец."""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "blacklist.db")
NOTE_MAX = 200   # символов в причине — чтобы список /blacklist не разрастался


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS blacklist ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ex TEXT, nick TEXT, "
                "added_ts REAL DEFAULT NULL, note TEXT DEFAULT '', UNIQUE(ex, nick))")
    cols = [r[1] for r in con.execute("PRAGMA table_info(blacklist)")]
    for col, ddl in (("added_ts", "REAL DEFAULT NULL"), ("note", "TEXT DEFAULT ''")):
        if col not in cols:   # база от прошлой версии — добавляем колонку, у старых записей даты нет
            con.execute(f"ALTER TABLE blacklist ADD COLUMN {col} {ddl}")
    con.commit()
    return con


def add(ex, nick, path=DB_PATH, ts=None):
    """Добавить мерчанта/обменника (ex, nick — как в Ad.ex/Ad.nick) с временем добавления; уже в списке —
    не ошибка, дата остаётся прежней. Возвращает id записи — для «/blacklist note <id> <текст>»."""
    ts = time.time() if ts is None else ts
    con = _connect(path)
    with con:
        con.execute("INSERT OR IGNORE INTO blacklist (ex, nick, added_ts) VALUES (?, ?, ?)", (ex, nick, ts))
        entry_id, = con.execute("SELECT id FROM blacklist WHERE ex = ? AND nick = ?", (ex, nick)).fetchone()
    con.close()
    return entry_id


def set_note(entry_id, note, path=DB_PATH):
    """Причина, почему запись в списке (пробелы/переводы строк схлопываются, не длиннее NOTE_MAX);
    True — запись найдена и обновлена."""
    if not os.path.exists(path):
        return False
    con = _connect(path)
    with con:
        cur = con.execute("UPDATE blacklist SET note = ? WHERE id = ?", (" ".join(note.split())[:NOTE_MAX], entry_id))
    con.close()
    return cur.rowcount > 0


def remove(entry_id, path=DB_PATH):
    con = _connect(path)
    with con:
        con.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))
    con.close()


def list_all(path=DB_PATH):
    """[(id, ex, nick, added_ts, note), ...] отсортировано по площадке и нику — для /blacklist
    (added_ts None — запись из версии без даты)."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute("SELECT id, ex, nick, added_ts, note FROM blacklist ORDER BY ex, nick").fetchall()
    con.close()
    return rows


def blocked(path=DB_PATH):
    """{(ex, nick), ...} — для быстрой проверки в usable()/_signal_ok() при скане."""
    return {(ex, nick) for _, ex, nick, *_ in list_all(path)}
