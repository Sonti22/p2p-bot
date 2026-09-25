"""Сухой прогон (paper trading) — этап 1 полуавтомата: виртуальные круги без реальных денег,
чтобы увидеть, что бот сделал бы сам, и сравнить план с фактом. Хранилище, движок и запуск круга
по сигналу уже есть; проверка стадий transfer/sell по свежим снимкам (сейчас готова только buy)
и команда /paper — в следующих задачах очереди.

SQLite data/paper.db, таблицы:
  cycles  — один виртуальный круг: сумма, объявления покупки/продажи на момент старта, маршрут,
            плановая прибыль (%), текущая стадия (buy → transfer → sell, пока круг открыт),
            итог (result: done/failed_buy/failed_transfer/failed_sell, пока NULL — круг открыт)
            и реализованная прибыль (realized_pct) по ценам на момент стадий.
  balance — один виртуальный баланс: старт = PAPER_AMOUNT, меняется на realized_pct каждого
            завершённого круга.
"""
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "paper.db")

STAGES = ("buy", "transfer", "sell")
RESULTS = ("done", "failed_buy", "failed_transfer", "failed_sell")

_COLUMNS = ("id", "ts_start", "amount", "buy_ex", "buy_asset", "buy_price", "buy_nick",
            "sell_ex", "sell_asset", "sell_price", "sell_nick", "route", "planned_pct",
            "stage", "ts_stage", "realized_pct", "result", "note")


def settings():
    """Настройки сухого прогона из .env — читать при каждом обращении (после load_env()), а не
    константой при импорте (как ACCOUNT_POLL_INTERVAL раньше читался слишком рано)."""
    return {
        "on": os.getenv("PAPER", "0").strip().lower() in ("1", "true", "yes", "on"),
        "amount": float(os.getenv("PAPER_AMOUNT", 10000)),
        "pay_minutes": float(os.getenv("PAPER_PAY_MINUTES", 5)),
        "transfer_minutes": float(os.getenv("PAPER_TRANSFER_MINUTES", 3)),
        "max_open": int(os.getenv("PAPER_MAX_OPEN", 1)),
    }


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS cycles ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, "
                "buy_ex TEXT, buy_asset TEXT, buy_price REAL, buy_nick TEXT, "
                "sell_ex TEXT, sell_asset TEXT, sell_price REAL, sell_nick TEXT, "
                "route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, "
                "realized_pct REAL DEFAULT NULL, result TEXT DEFAULT NULL, note TEXT DEFAULT '')")
    con.execute("CREATE TABLE IF NOT EXISTS balance (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "amount REAL, updated_ts REAL)")
    con.commit()
    return con


def init_balance(start_amount, path=DB_PATH):
    """Завести виртуальный баланс, если его ещё нет (старт = PAPER_AMOUNT); уже есть — не трогать."""
    con = _connect(path)
    with con:
        con.execute("INSERT OR IGNORE INTO balance (id, amount, updated_ts) VALUES (1, ?, ?)",
                    (start_amount, time.time()))
    con.close()


def get_balance(path=DB_PATH):
    """Текущий виртуальный баланс — None, если ещё не заведён (init_balance не вызывался)."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    row = con.execute("SELECT amount FROM balance WHERE id = 1").fetchone()
    con.close()
    return row[0] if row else None


def apply_result(realized_pct, amount, path=DB_PATH, ts=None):
    """Изменить виртуальный баланс на результат круга (realized_pct % от его amount); баланс ещё
    не заведён — считается от 0 (не должно происходить в обычном режиме: круг стартует только
    после init_balance)."""
    con = _connect(path)
    delta = amount * realized_pct / 100
    with con:
        con.execute("INSERT OR IGNORE INTO balance (id, amount, updated_ts) VALUES (1, 0, ?)",
                    (ts if ts is not None else time.time(),))
        con.execute("UPDATE balance SET amount = amount + ?, updated_ts = ? WHERE id = 1",
                    (delta, ts if ts is not None else time.time()))
    con.close()


def start_cycle(amount, buy, sell, route, planned_pct, path=DB_PATH, ts=None):
    """Завести новый виртуальный круг со стадией buy. buy/sell — объявления покупки/продажи
    (p2p.Ad) на момент старта. Возвращает id круга."""
    ts = ts if ts is not None else time.time()
    con = _connect(path)
    with con:
        cur = con.execute(
            "INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, buy_price, buy_nick, "
            "sell_ex, sell_asset, sell_price, sell_nick, route, planned_pct, stage, ts_stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'buy', ?)",
            (ts, amount, buy.ex, buy.asset, buy.price, buy.nick,
             sell.ex, sell.asset, sell.price, sell.nick, route, planned_pct, ts))
        cycle_id = cur.lastrowid
    con.close()
    return cycle_id


def _row_to_dict(row):
    return dict(zip(_COLUMNS, row))


def get_cycle(cycle_id, path=DB_PATH):
    """Круг по id — словарь со всеми колонками, None — не найден."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    row = con.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    con.close()
    return _row_to_dict(row) if row else None


def open_cycles(path=DB_PATH):
    """Незавершённые круги (result ещё не выставлен), по времени старта — для PAPER_MAX_OPEN и
    для обработки стадий."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute("SELECT * FROM cycles WHERE result IS NULL ORDER BY id").fetchall()
    con.close()
    return [_row_to_dict(r) for r in rows]


def set_stage(cycle_id, stage, path=DB_PATH, ts=None):
    """Перевести открытый круг на следующую стадию (buy → transfer → sell)."""
    con = _connect(path)
    with con:
        con.execute("UPDATE cycles SET stage = ?, ts_stage = ? WHERE id = ?",
                    (stage, ts if ts is not None else time.time(), cycle_id))
    con.close()


def check_buy_stage(cycle, snap, pay_minutes, now=None):
    """Проверка исполнимости стадии buy по свежему снимку (только чтение snap.groups, без сети и
    без записи в БД — решение применяет вызывающий). Раньше PAPER_PAY_MINUTES с начала круга не
    ждём. После — ищем в текущем стакане покупки то же объявление (тот же мерчант): нет — цена
    ушла хуже плана.

    Возвращает (action, note):
      "wait"    — ещё не прошло pay_minutes, ничего не решаем;
      "advance" — объявление на месте, цена не хуже плана — можно переходить к transfer;
      "fail"    — объявление исчезло или цена ушла хуже плана (result станет failed_buy).
    """
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < pay_minutes * 60:
        return "wait", ""
    ads = snap.groups.get((cycle["buy_ex"], "buy", cycle["buy_asset"]), [])
    ad = next((a for a in ads if a.nick == cycle["buy_nick"]), None)
    if ad is None:
        return "fail", "объявление покупки исчезло"
    if ad.price > cycle["buy_price"]:
        return "fail", f"цена ушла: было {cycle['buy_price']:g} ₽, стало {ad.price:g} ₽"
    return "advance", ""


def finish_cycle(cycle_id, result, realized_pct=0.0, note="", path=DB_PATH, ts=None):
    """Завершить круг: result — 'done' (успех) или 'failed_buy'/'failed_transfer'/'failed_sell'
    (срыв на соответствующей стадии). Обновляет виртуальный баланс на realized_pct (для срыва —
    обычно 0, круг не состоялся). Круга с таким id нет — возвращает False, баланс не трогает."""
    ts = ts if ts is not None else time.time()
    con = _connect(path)
    row = con.execute("SELECT amount FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is None:
        con.close()
        return False
    amount = row[0]
    with con:
        con.execute("UPDATE cycles SET result = ?, ts_stage = ?, realized_pct = ?, note = ? "
                    "WHERE id = ?", (result, ts, realized_pct, note, cycle_id))
    con.close()
    apply_result(realized_pct, amount, path=path, ts=ts)
    return True
