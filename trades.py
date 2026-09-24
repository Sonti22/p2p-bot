"""Журнал сделок: SQLite data/trades.db — время, связка, сумма, расчётный %. Пишется по кнопке «✅ Сделал»."""
import datetime
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "trades.db")
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}
# Банки, через которые обычно идёт оплата P2P-мерчанту по СБП (определяем по способу оплаты объявления).
# Свободный лимит СБП физлицу — 100 тыс. ₽ в календарный месяц НА КАЖДЫЙ банк, дальше — комиссия до 0.5%.
SBP_BANKS = ("Sberbank", "T-Bank", "Alfa-bank", "VTB", "SBP")
BANK_LIMIT = 100_000.0


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS trades ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, "
                "buy_ex TEXT, buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, "
                "amount REAL, profit REAL, bank TEXT DEFAULT '')")
    if "bank" not in [r[1] for r in con.execute("PRAGMA table_info(trades)")]:
        con.execute("ALTER TABLE trades ADD COLUMN bank TEXT DEFAULT ''")
    return con


def sbp_bank(pays):
    """Банк из способов оплаты объявления, если он совпадает с известным (иначе '' — не отслеживаем)."""
    for p in pays:
        pl = p.lower()
        for name in SBP_BANKS:
            if name.lower() in pl:
                return name
    return ""


def _month_start(ts):
    dt = datetime.datetime.fromtimestamp(ts)
    return datetime.datetime(dt.year, dt.month, 1).timestamp()


def bank_month_total(bank, path=DB_PATH, now=None):
    """Сумма отправленного через банк по СБП с начала текущего календарного месяца."""
    if not bank or not os.path.exists(path):
        return 0.0
    now = time.time() if now is None else now
    con = _connect(path)
    total, = con.execute("SELECT COALESCE(SUM(amount), 0) FROM trades WHERE bank = ? AND ts >= ?",
                         (bank, _month_start(now))).fetchone()
    con.close()
    return total


def log_trade(d, amount, path=DB_PATH, ts=None):
    """Записать сделку: d — (profit %, buy Ad, sell Ad, маршрут), amount — сумма круга в фиате.
    Возвращает (банк, сумма за месяц с этой сделкой, пересёк ли лимит 100 тыс. этой сделкой)."""
    profit, b, s, route = d
    ts = ts if ts is not None else time.time()
    bank = sbp_bank(b.pays)
    prev = bank_month_total(bank, path, ts) if bank else 0.0
    con = _connect(path)
    with con:
        con.execute("INSERT INTO trades (ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit, bank) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, route, b.ex, b.asset, s.ex, s.asset, amount, profit, bank))
    con.close()
    total = prev + amount
    crossed = bool(bank) and prev < BANK_LIMIT <= total
    return bank, total, crossed


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
