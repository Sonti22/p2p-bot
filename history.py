"""История спредов: SQLite data/history.db — лучший % по каждой паре площадок, раз в 5 минут
(пишет scan_loop бота). Агрегации для /history: лучшее время суток и хитмап час×день недели за
7 дней (МСК = UTC+3, без перехода на летнее время), медиана P2P против BestChange за окно 7-30 дней.
"""
import os
import sqlite3
import statistics
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "history.db")
THROTTLE = 5 * 60         # сек — не чаще раза в 5 минут
RETENTION = 30 * 86400    # хранить 30 дней
MSK_OFFSET = 3 * 3600     # UTC+3
DOW_NAMES = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                "buy_ex TEXT, sell_ex TEXT, asset_buy TEXT, asset_sell TEXT, profit REAL, ref REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history (ts)")
    return con


def _insert(rows, path=DB_PATH):
    """rows: [(ts, buy_ex, sell_ex, asset_buy, asset_sell, profit, ref), ...]."""
    if not rows:
        return
    con = _connect(path)
    with con:
        con.executemany("INSERT INTO history (ts, buy_ex, sell_ex, asset_buy, asset_sell, profit, ref) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()


_last = {"t": 0.0}   # время последней записи — троттлинг раз в 5 минут (сбрасывается в тестах)


def record(snap, path=DB_PATH):
    """Лучший % по каждой паре площадок (buy_ex, sell_ex) из snap.deals + ориентир snap.ref.
    Не чаще раза в 5 минут; пустой снимок (нет связок) кулдаун не расходует — запишем, как
    только связки появятся."""
    now = time.time()
    if now - _last["t"] < THROTTLE or not snap.deals:
        return False
    best = {}   # (buy_ex, sell_ex) -> (profit, asset_buy, asset_sell)
    for profit, b, s, _route in snap.deals:
        key = (b.ex, s.ex)
        if key not in best or profit > best[key][0]:
            best[key] = (profit, b.asset, s.asset)
    _insert([(now, buy_ex, sell_ex, ab, sa, profit, snap.ref)
            for (buy_ex, sell_ex), (profit, ab, sa) in best.items()], path)
    _last["t"] = now
    return True


def cleanup(path=DB_PATH, now=None):
    """Удалить записи старше 30 дней; возвращает число удалённых строк."""
    if not os.path.exists(path):
        return 0
    now = time.time() if now is None else now
    con = _connect(path)
    with con:
        cur = con.execute("DELETE FROM history WHERE ts < ?", (now - RETENTION,))
    con.close()
    return cur.rowcount


def _rows(path, since):
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute("SELECT ts, buy_ex, sell_ex, profit FROM history WHERE ts >= ? ORDER BY ts",
                       (since,)).fetchall()
    con.close()
    return rows


def is_empty(path=DB_PATH):
    return not _rows(path, 0)


def hourly_avg(path=DB_PATH, days=7, now=None):
    """{час МСК 0..23: средний лучший % за `days` дней} — None, если по часу нет данных."""
    now = time.time() if now is None else now
    buckets = {h: [] for h in range(24)}
    for ts, _buy_ex, _sell_ex, profit in _rows(path, now - days * 86400):
        buckets[time.gmtime(ts + MSK_OFFSET).tm_hour].append(profit)
    return {h: (sum(v) / len(v) if v else None) for h, v in buckets.items()}


def heatmap(path=DB_PATH, days=7, now=None):
    """{(день недели МСК 0=пн..6=вс, час 0..23): максимальный лучший % за `days` дней}, только
    непустые ячейки."""
    now = time.time() if now is None else now
    grid = {}
    for ts, _buy_ex, _sell_ex, profit in _rows(path, now - days * 86400):
        t = time.gmtime(ts + MSK_OFFSET)
        key = (t.tm_wday, t.tm_hour)
        if key not in grid or profit > grid[key]:
            grid[key] = profit
    return grid


def backtest(min_profit, amount, path=DB_PATH, now=None):
    """Бэктест маршрута по истории спредов за 7 и 30 дней: для каждой пары площадок — сколько раз
    записанный лучший % (уже чистый, с комиссиями) был >= порога `min_profit`, средний и медианный %
    в такие моменты, оценка результата в ₽ на сумму круга `amount`. Возвращает {7: [...], 30: [...]},
    списки словарей отсортированы по числу попаданий (для топ-N); пары без ни одного попадания не
    включаются."""
    now = time.time() if now is None else now
    out = {}
    for days in (7, 30):
        by_pair = {}
        for ts, buy_ex, sell_ex, profit in _rows(path, now - days * 86400):
            by_pair.setdefault((buy_ex, sell_ex), []).append(profit)
        rows = []
        for (buy_ex, sell_ex), profits in by_pair.items():
            hits = [p for p in profits if p >= min_profit]
            if not hits:
                continue
            avg = statistics.mean(hits)
            rows.append({"buy_ex": buy_ex, "sell_ex": sell_ex, "hits": len(hits), "total": len(profits),
                        "avg": avg, "median": statistics.median(hits), "est_rub": avg / 100 * amount})
        rows.sort(key=lambda r: r["hits"], reverse=True)
        out[days] = rows
    return out


def median_vs_bestchange(path=DB_PATH, days=30, now=None):
    """Медиана лучшего % по дням (МСК): связки между площадками P2P против связок, где buy_ex
    или sell_ex — BestChange. Окно 7-30 дней (RETENTION = 30 дней, меньше данных — меньше дней).
    Возвращает (даты по возрастанию "ДД.MM", p2p-медианы, bc-медианы); None — за день нет сделок группы."""
    now = time.time() if now is None else now
    by_day = {}   # "YYYY-MM-DD" -> {"p2p": [...], "bc": [...]}
    for ts, buy_ex, sell_ex, profit in _rows(path, now - days * 86400):
        t = time.gmtime(ts + MSK_OFFSET)
        day = time.strftime("%Y-%m-%d", t)
        bucket = by_day.setdefault(day, {"p2p": [], "bc": []})
        bucket["bc" if "BestChange" in (buy_ex, sell_ex) else "p2p"].append(profit)
    order = sorted(by_day)
    labels = [time.strftime("%d.%m", time.strptime(d, "%Y-%m-%d")) for d in order]
    p2p = [statistics.median(by_day[d]["p2p"]) if by_day[d]["p2p"] else None for d in order]
    bc = [statistics.median(by_day[d]["bc"]) if by_day[d]["bc"] else None for d in order]
    return labels, p2p, bc
