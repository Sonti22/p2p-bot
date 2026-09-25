"""Журнал сделок: SQLite data/trades.db — время, связка, сумма, расчётный %. Пишется по кнопке «✅ Сделал».
Факт (реальный результат сделки) — необязательное поле `fact`, вводится кнопками или числом после
«✅ Сделал»; `/stats` показывает расчёт vs факт."""
import datetime
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "trades.db")
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}
MSK = datetime.timezone(datetime.timedelta(hours=3))
# Банки, через которые обычно идёт оплата P2P-мерчанту по СБП (определяем по способу оплаты объявления).
# Свободный лимит СБП физлицу — 100 тыс. ₽ в календарный месяц НА КАЖДЫЙ банк, дальше — комиссия до 0.5%.
SBP_BANKS = ("Sberbank", "T-Bank", "Alfa-bank", "VTB", "SBP")
BANK_LIMIT = 100_000.0
SBP_OVER_FEE = 0.5  # % — комиссия банка сверх бесплатного лимита СБП (до 0.5%)
# Без единицы измерения короткое число похоже на проценты (типичный профит связки — единицы процентов),
# длинное — на рубли (типичная сумма выигрыша за круг — сотни-тысячи ₽).
FACT_PLAIN_AS_PERCENT_MAX = 50.0
_FACT_PCT = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*%$")
_FACT_RUB = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(?:₽|руб\.?|р\.?)$", re.I)
_FACT_PLAIN = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)$")


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS trades ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, "
                "buy_ex TEXT, buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, "
                "amount REAL, profit REAL, bank TEXT DEFAULT '', fact REAL DEFAULT NULL)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
    if "bank" not in cols:
        con.execute("ALTER TABLE trades ADD COLUMN bank TEXT DEFAULT ''")
    if "fact" not in cols:
        con.execute("ALTER TABLE trades ADD COLUMN fact REAL DEFAULT NULL")
    return con


def parse_fact(text, amount):
    """Фактический результат сделки из текста: «+1.2%»/«−0.5%» — проценты как есть; «650 ₽»/«-300 руб» —
    сумма в фиате, переводится в % от `amount`; голое число без единицы — проценты, если |x| не больше
    `FACT_PLAIN_AS_PERCENT_MAX`, иначе тоже сумма в ₽. None — не разобрано (мусор) или `amount` пустой
    для рублёвого варианта."""
    t = re.sub(r"\s+", "", (text or "").strip()).replace(",", ".")
    m = _FACT_PCT.match(t)
    if m:
        return float(m.group(1))
    m = _FACT_RUB.match(t)
    if m:
        return float(m.group(1)) / amount * 100 if amount else None
    m = _FACT_PLAIN.match(t)
    if m:
        val = float(m.group(1))
        return val if abs(val) <= FACT_PLAIN_AS_PERCENT_MAX else (val / amount * 100 if amount else None)
    return None


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


def _day_start(ts):
    """Начало календарных суток по МСК, в которые попадает `ts`."""
    dt = datetime.datetime.fromtimestamp(ts, MSK)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


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


def banks_over_limit(banks, path=DB_PATH, now=None):
    """Из списка банков — те, что уже набрали 100 тыс. ₽ за календарный месяц (лимит СБП исчерпан)."""
    return {b for b in banks if b and bank_month_total(b, path, now) >= BANK_LIMIT}


def log_trade(d, amount, path=DB_PATH, ts=None):
    """Записать сделку: d — (profit %, buy Ad, sell Ad, маршрут), amount — сумма круга в фиате.
    Возвращает (id сделки — для ввода факта, банк, сумма за месяц с этой сделкой, пересёк ли лимит
    100 тыс. этой сделкой)."""
    profit, b, s, route = d
    ts = ts if ts is not None else time.time()
    bank = sbp_bank(b.pays)
    prev = bank_month_total(bank, path, ts) if bank else 0.0
    con = _connect(path)
    with con:
        cur = con.execute("INSERT INTO trades (ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit, bank) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (ts, route, b.ex, b.asset, s.ex, s.asset, amount, profit, bank))
    trade_id = cur.lastrowid
    con.close()
    total = prev + amount
    crossed = bool(bank) and prev < BANK_LIMIT <= total
    return trade_id, bank, total, crossed


def get_trade(trade_id, path=DB_PATH):
    """{"amount", "profit"} сделки по id (нужна сумма круга, чтобы перевести факт в ₽ в проценты) —
    None, если сделки нет."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    row = con.execute("SELECT amount, profit FROM trades WHERE id = ?", (trade_id,)).fetchone()
    con.close()
    return {"amount": row[0], "profit": row[1]} if row else None


def set_fact(trade_id, fact_percent, path=DB_PATH):
    """Записать фактический результат (%) для сделки; True — сделка найдена и обновлена."""
    con = _connect(path)
    with con:
        cur = con.execute("UPDATE trades SET fact = ? WHERE id = ?", (fact_percent, trade_id))
    con.close()
    return cur.rowcount > 0


def stats(path=DB_PATH, now=None):
    """{"day"/"week"/"month": {"count", "amount", "avg_profit", "fact_count", "avg_fact", "avg_diff"}} —
    для /stats. «day» — календарные сутки по МСК, «month» — календарный месяц (как счётчик лимита СБП),
    «week» — последние 7 суток. `fact_count`/`avg_fact`/`avg_diff` — только по сделкам, где введён факт
    (avg_diff = среднее факт-расчёт, п.п.); при отсутствии таких сделок avg_fact/avg_diff — None."""
    now = time.time() if now is None else now
    starts = {"day": _day_start(now), "week": now - PERIODS["week"], "month": _month_start(now)}
    out = {p: {"count": 0, "amount": 0.0, "avg_profit": 0.0, "fact_count": 0, "avg_fact": None, "avg_diff": None}
           for p in PERIODS}
    if not os.path.exists(path):
        return out
    con = _connect(path)
    for period, start in starts.items():
        count, amount, avg_profit = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0), COALESCE(AVG(profit), 0) FROM trades WHERE ts >= ?",
            (start,)).fetchone()
        fact_count, avg_fact, avg_diff = con.execute(
            "SELECT COUNT(*), AVG(fact), AVG(fact - profit) FROM trades WHERE ts >= ? AND fact IS NOT NULL",
            (start,)).fetchone()
        out[period] = {"count": count, "amount": amount, "avg_profit": avg_profit,
                       "fact_count": fact_count, "avg_fact": avg_fact, "avg_diff": avg_diff}
    con.close()
    return out
