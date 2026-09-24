"""Алерты на целевой курс: /alert USDT sell 92 7d — сообщить, когда надёжный покупатель/обменник
даёт нужную цену, до истечения срока. SQLite data/alerts.db. По умолчанию одноразовые: срабатывают
один раз и удаляются. С «repeat <кулдаун>» алерт не удаляется по сроку — ждёт следующего скана и
может сработать снова не раньше, чем пройдёт кулдаун с прошлого срабатывания (условия объёма/
надёжности — следующим пунктом очереди)."""
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "alerts.db")
DURATIONS = {"h": 3600, "d": 86400, "w": 7 * 86400}
MAX_DURATION = 90 * 86400  # дольше 90 дней смысла не имеет — курс успеет уйти куда угодно


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS alerts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, asset TEXT, side TEXT, "
                "rate REAL, created_ts REAL, expires_ts REAL, "
                "repeat_cooldown REAL, last_fired_ts REAL)")
    cols = {row[1] for row in con.execute("PRAGMA table_info(alerts)")}
    if "repeat_cooldown" not in cols:   # апгрейд базы, созданной до режима «повторно»
        con.execute("ALTER TABLE alerts ADD COLUMN repeat_cooldown REAL")
    if "last_fired_ts" not in cols:
        con.execute("ALTER TABLE alerts ADD COLUMN last_fired_ts REAL")
    con.commit()
    return con


def parse_duration(text):
    """«7d» / «12h» / «2w» → секунды; None — не разобрано или вне диапазона (0; 90 дней]."""
    m = re.fullmatch(r"(\d+)([hdw])", (text or "").strip().lower())
    if not m:
        return None
    sec = int(m.group(1)) * DURATIONS[m.group(2)]
    return sec if 0 < sec <= MAX_DURATION else None


def add(chat_id, asset, side, rate, expires_ts, path=DB_PATH, repeat_cooldown=None):
    """Создать алерт, вернуть его id. repeat_cooldown (сек) — режим «повторно»: алерт не удаляется
    по срабатыванию, а может сработать снова не раньше, чем пройдёт кулдаун; None — одноразовый."""
    con = _connect(path)
    with con:
        cur = con.execute("INSERT INTO alerts (chat_id, asset, side, rate, created_ts, expires_ts, "
                          "repeat_cooldown) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (chat_id, asset, side, rate, time.time(), expires_ts, repeat_cooldown))
        alert_id = cur.lastrowid
    con.close()
    return alert_id


def _prune_expired(con, now):
    con.execute("DELETE FROM alerts WHERE expires_ts <= ?", (now,))


def list_all(chat_id, path=DB_PATH, now=None):
    """Активные алерты чата [(id, asset, side, rate, expires_ts, repeat_cooldown), ...] — для /alerts;
    попутно чистит истёкшие."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return []
    con = _connect(path)
    with con:
        _prune_expired(con, now)
    rows = con.execute("SELECT id, asset, side, rate, expires_ts, repeat_cooldown FROM alerts "
                       "WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
    con.close()
    return rows


def remove(alert_id, chat_id, path=DB_PATH):
    """Удалить алерт (только своего чата — id снаружи виден лишь владельцу через /alerts)."""
    con = _connect(path)
    with con:
        con.execute("DELETE FROM alerts WHERE id = ? AND chat_id = ?", (alert_id, chat_id))
    con.close()


def due(snap, path=DB_PATH, now=None):
    """Сработавшие алерты по текущему снимку: [(id, chat_id, asset, side, rate, price, Ad), ...].
    Цена берётся из snap.best (объявления уже прошли фильтры usable() — мин. сделок/отзывов, отсев
    аномалий, блэклист). Одноразовый алерт (repeat_cooldown is NULL) сразу удаляется. Алерт «повторно»
    не удаляется по сроку — ждёт следующего скана и срабатывает снова не раньше, чем пройдёт кулдаун
    с прошлого срабатывания; истёкшие (expires_ts) удаляются в любом режиме."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return []
    con = _connect(path)
    with con:
        _prune_expired(con, now)
    rows = con.execute("SELECT id, chat_id, asset, side, rate, repeat_cooldown, last_fired_ts "
                       "FROM alerts").fetchall()
    fired, one_shot, repeat_fired = [], [], []
    for alert_id, chat_id, asset, side, rate, cooldown, last_fired in rows:
        if cooldown is not None and last_fired is not None and now - last_fired < cooldown:
            continue   # алерт «повторно» ещё «отдыхает» после прошлого срабатывания
        best_ad, best_price = None, None
        for (ex, ad_side, ad_asset), ad in snap.best.items():
            if ad_side != side or ad_asset != asset:
                continue
            if best_price is None or (ad.price > best_price if side == "sell" else ad.price < best_price):
                best_price, best_ad = ad.price, ad
        if best_ad is None:
            continue
        ok = best_price >= rate if side == "sell" else best_price <= rate
        if ok:
            fired.append((alert_id, chat_id, asset, side, rate, best_price, best_ad))
            (repeat_fired if cooldown is not None else one_shot).append(alert_id)
    if one_shot:
        with con:
            con.executemany("DELETE FROM alerts WHERE id = ?", [(i,) for i in one_shot])
    if repeat_fired:
        with con:
            con.executemany("UPDATE alerts SET last_fired_ts = ? WHERE id = ?", [(now, i) for i in repeat_fired])
    con.close()
    return fired
