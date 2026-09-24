"""Журнал сделок: SQLite data/trades.db — время, связка, сумма, расчётный %. Пишется по кнопке «✅ Сделал»."""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "trades.db")
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS trades ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, "
                "buy_ex TEXT, buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, "
                "amount REAL, profit REAL)")
    return con


def log_trade(d, amount, path=DB_PATH, ts=None):
    """Записать сделку: d — (profit %, buy Ad, sell Ad, маршрут), amount — сумма круга в фиате."""
    profit, b, s, route = d
    con = _connect(path)
    with con:
        con.execute("INSERT INTO trades (ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts if ts is not None else time.time(), route, b.ex, b.asset, s.ex, s.asset, amount, profit))
    con.close()


def stats(path=DB_PATH, now=None):
    """{"day"/"week"/"month": {"count", "amount", "avg_profit"}} — для /stats."""
    now = time.time() if now is None else now
    out = {p: {"count": 0, "amount": 0.0, "avg_profit": 0.0} for p in PERIODS}
    if not os.path.exists(path):
        return out
    con = _connect(path)
    for period, span in PERIODS.items():
        count, amount, avg_profit = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0), COALESCE(AVG(profit), 0) FROM trades WHERE ts >= ?",
            (now - span,)).fetchone()
        out[period] = {"count": count, "amount": amount, "avg_profit": avg_profit}
    con.close()
    return out
