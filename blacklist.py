"""Блэклист мерчантов и обменников: SQLite data/blacklist.db. Кнопка «🚫 Не показывать» под сигналом
добавляет обе стороны связки, /blacklist показывает список с удалением; scan() их отсеивает."""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "blacklist.db")


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS blacklist ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ex TEXT, nick TEXT, UNIQUE(ex, nick))")
    return con


def add(ex, nick, path=DB_PATH):
    """Добавить мерчанта/обменника (ex, nick — как в Ad.ex/Ad.nick); уже в списке — не ошибка."""
    con = _connect(path)
    with con:
        con.execute("INSERT OR IGNORE INTO blacklist (ex, nick) VALUES (?, ?)", (ex, nick))
    con.close()


def remove(entry_id, path=DB_PATH):
    con = _connect(path)
    with con:
        con.execute("DELETE FROM blacklist WHERE id = ?", (entry_id,))
    con.close()


def list_all(path=DB_PATH):
    """[(id, ex, nick), ...] отсортировано по площадке и нику — для /blacklist."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute("SELECT id, ex, nick FROM blacklist ORDER BY ex, nick").fetchall()
    con.close()
    return rows


def blocked(path=DB_PATH):
    """{(ex, nick), ...} — для быстрой проверки в usable()/_signal_ok() при скане."""
    return {(ex, nick) for _, ex, nick in list_all(path)}
