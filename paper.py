"""Сухой прогон (paper trading) — этап 1 полуавтомата: виртуальные круги без реальных денег,
чтобы увидеть, что бот сделал бы сам, и сравнить план с фактом. Хранилище, движок, запуск круга
по сигналу, все три стадии (buy → transfer → sell) и статистика для команды /paper готовы.

SQLite data/paper.db, таблицы:
  cycles  — один виртуальный круг: сумма, объявления покупки/продажи на момент старта, маршрут,
            плановая прибыль (%), текущая стадия (buy → transfer → sell, пока круг открыт),
            итог (result: done/failed_buy/failed_transfer/failed_sell, пока NULL — круг открыт),
            реализованная прибыль (realized_pct) по ценам на момент стадий, как платили (pay_kind:
            intra — внутри банка, sbp — по СБП со своего банка, trades.pay_plan) и банк (bank) — для учёта
            виртуального оборота по бесплатному лимиту СБП.
  balance — один виртуальный баланс: старт = PAPER_AMOUNT, меняется на realized_pct каждого
            завершённого круга.
"""
import csv
import datetime
import os
import sqlite3
import time

import p2p
import trades

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "paper.db")
REPORT_CSV_PATH = os.path.join(HERE, "data", "paper_report.csv")
REPORT_COLUMNS = ("buy_ex", "buy_asset", "sell_ex", "sell_asset", "total", "done", "failed",
                   "depth_shortfall", "avg_planned_pct", "avg_realized_pct", "avg_duration_min",
                   "failed_by_reason")

STAGES = ("buy", "transfer", "sell")
RESULTS = ("done", "failed_buy", "failed_transfer", "failed_sell")
MSK = datetime.timezone(datetime.timedelta(hours=3))
STATS_WEEK = 7 * 86400
FAIL_LABELS = {"failed_buy": "покупка", "failed_transfer": "перевод", "failed_sell": "продажа"}

# Лестница суммы круга: две ступени, 10 000 ₽ и 20 000 ₽ (решение всегда подтверждает владелец кнопкой,
# сам PAPER_AMOUNT ladder_suggestion не меняет).
LADDER_LOW = 10000.0
LADDER_HIGH = 20000.0
LADDER_UP_MIN_CYCLES = 20      # минимум завершённых кругов за всё время для предложения повысить
LADDER_UP_MAX_FAILED = 0.2     # доля сорвавшихся не выше 20%
LADDER_UP_MIN_MEDIAN = -0.3    # медиана (факт − план) по исполнившимся, п.п.
LADDER_DOWN_MIN_FAILED = 0.4   # доля сорвавшихся за неделю выше 40% — предложить вернуться

_COLUMNS = ("id", "ts_start", "amount", "buy_ex", "buy_asset", "buy_price", "buy_nick",
            "sell_ex", "sell_asset", "sell_price", "sell_nick", "route", "planned_pct",
            "stage", "ts_stage", "realized_pct", "result", "note", "bank", "label", "sell_fact", "pay_kind")


def settings():
    """Настройки сухого прогона из .env — читать при каждом обращении (после load_env()), а не
    константой при импорте (как ACCOUNT_POLL_INTERVAL раньше читался слишком рано)."""
    return {
        "on": os.getenv("PAPER", "0").strip().lower() in ("1", "true", "yes", "on"),
        "amount": float(os.getenv("PAPER_AMOUNT", 10000)),
        "pay_minutes": float(os.getenv("PAPER_PAY_MINUTES", 5)),
        "transfer_minutes": float(os.getenv("PAPER_TRANSFER_MINUTES", 3)),
        "max_open": int(os.getenv("PAPER_MAX_OPEN", 1)),
        # «🪤 ловушки» по умолчанию не берём — как не взял бы их автомат на этапе 2
        "traps": os.getenv("PAPER_TRAPS", "0").strip().lower() in ("1", "true", "yes", "on"),
    }


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS cycles ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, "
                "buy_ex TEXT, buy_asset TEXT, buy_price REAL, buy_nick TEXT, "
                "sell_ex TEXT, sell_asset TEXT, sell_price REAL, sell_nick TEXT, "
                "route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, "
                "realized_pct REAL DEFAULT NULL, result TEXT DEFAULT NULL, note TEXT DEFAULT '', "
                "bank TEXT DEFAULT '', label TEXT DEFAULT '', sell_fact REAL DEFAULT NULL, pay_kind TEXT DEFAULT '')")
    cols = [r[1] for r in con.execute("PRAGMA table_info(cycles)")]
    for col, ddl in (("bank", "TEXT DEFAULT ''"), ("label", "TEXT DEFAULT ''"), ("sell_fact", "REAL DEFAULT NULL"),
                     ("pay_kind", "TEXT DEFAULT ''")):
        if col not in cols:   # база от прошлой версии — добавляем колонку, данные не трогаем
            con.execute(f"ALTER TABLE cycles ADD COLUMN {col} {ddl}")
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


def start_cycle(amount, buy, sell, route, planned_pct, path=DB_PATH, ts=None, label=""):
    """Завести новый виртуальный круг со стадией buy. buy/sell — объявления покупки/продажи
    (p2p.Ad) на момент старта. Как платим (trades.pay_plan: внутри банка или по СБП со своего банка, у
    которого виртуальный лимит ещё есть) пишется сразу — в реальности рубли уходят в момент оплаты, до
    проверки стадии buy. label — метка надёжности
    связки на старте (p2p.reliability) — для разбора в отчёте. Возвращает id круга."""
    ts = ts if ts is not None else time.time()
    own = trades.own_banks()[0]
    over = {b for b in own if bank_month_total(b, path, ts) >= trades.free_limit(b)}
    kind, bank = trades.pay_plan(buy.pays, over=over)
    con = _connect(path)
    with con:
        cur = con.execute(
            "INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, buy_price, buy_nick, sell_ex, sell_asset, "
            "sell_price, sell_nick, route, planned_pct, stage, ts_stage, bank, label, pay_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'buy', ?, ?, ?, ?)",
            (ts, amount, buy.ex, buy.asset, buy.price, buy.nick,
             sell.ex, sell.asset, sell.price, sell.nick, route, planned_pct, ts, bank, label, kind))
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
    ждём. После — ищем в текущем стакане покупки то же объявление (тот же мерчант): нет — мерчант
    ушёл/снял объявление, круг не состоялся. Цену не сравниваем: в ордере она фиксируется при создании.

    Возвращает (action, note):
      "wait"    — ещё не прошло pay_minutes, ничего не решаем;
      "advance" — объявление на месте — можно переходить к transfer;
      "fail"    — объявление исчезло (result станет failed_buy).
    """
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < pay_minutes * 60:
        return "wait", ""
    ads = snap.groups.get((cycle["buy_ex"], "buy", cycle["buy_asset"]), [])
    if not any(a.nick == cycle["buy_nick"] for a in ads):
        return "fail", "объявление покупки исчезло"
    return "advance", ""


def check_transfer_stage(cycle, cfg, transfer_minutes, now=None):
    """Проверка стадии transfer по свежему справочнику (только чтение fees.json/netstatus через
    p2p.withdraw_open, без сети и без записи в БД). Раньше PAPER_TRANSFER_MINUTES с начала стадии
    не ждём. После — проверяем, что вывод buy_asset с buy_ex всё ещё возможен (известна комиссия,
    сеть открыта) — иначе перевод сорвался бы в реальности.

    Возвращает (action, note) как check_buy_stage: "wait"/"advance"/"fail" (result станет
    failed_transfer)."""
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < transfer_minutes * 60:
        return "wait", ""
    if not p2p.withdraw_open(cfg, cycle["buy_ex"], cycle["buy_asset"]):
        return "fail", f"вывод {cycle['buy_asset']} с {cycle['buy_ex']} закрыт"
    return "advance", ""


def check_sell_stage(cycle, snap):
    """Проверка стадии sell по свежему снимку (только чтение snap.groups, без сети и без записи
    в БД). Монета уже на площадке продажи — продаём тем, кто сейчас есть в стакане: цена — средняя по
    лучшим объявлениям на весь объём круга (p2p.sell_fill_price, тот же стек, что в _match), даже если
    плановый мерчант ушёл или цена стала хуже (факт тогда ниже плана, может быть и минус). Срыв — только
    если покупателей на весь объём нет.

    Возвращает (action, note, price): price — фактическая цена продажи (для realized_pct) при
    "advance", note — чем факт отличается от плана; "fail" — result станет failed_sell."""
    ads = snap.groups.get((cycle["sell_ex"], "sell", cycle["sell_asset"]), [])
    qty = cycle["amount"] / cycle["buy_price"]
    price = p2p.sell_fill_price(ads, qty)
    if price is None:
        return "fail", "не хватает глубины стакана продажи", None
    note = ""
    if abs(price / cycle["sell_price"] - 1) >= 1e-4:
        note = (f"продажа по {price:g} ₽ вместо {cycle['sell_price']:g} ₽ "
                f"({(price / cycle['sell_price'] - 1) * 100:+.2f}%)")
    return "advance", note, price


def realized_pct(cycle, sell_price):
    """Итоговая прибыль круга по факту: план (planned_pct) масштабируется на изменение цены продажи
    относительно плана (цена покупки зафиксирована в ордере, вывод проверен на своей стадии — на
    стадии sell меняется только цена продажи, в любую сторону)."""
    ratio = sell_price / cycle["sell_price"]
    return ((1 + cycle["planned_pct"] / 100) * ratio - 1) * 100


def finish_cycle(cycle_id, result, realized_pct=0.0, note="", path=DB_PATH, ts=None, sell_fact=None):
    """Завершить круг: result — 'done' (успех) или 'failed_buy'/'failed_transfer'/'failed_sell'
    (срыв на соответствующей стадии). Обновляет виртуальный баланс на realized_pct (для срыва —
    обычно 0, круг не состоялся); sell_fact — фактическая цена продажи. Круга с таким id нет —
    возвращает False, баланс не трогает."""
    ts = ts if ts is not None else time.time()
    con = _connect(path)
    row = con.execute("SELECT amount FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is None:
        con.close()
        return False
    amount = row[0]
    with con:
        con.execute("UPDATE cycles SET result = ?, ts_stage = ?, realized_pct = ?, note = ?, sell_fact = ? "
                    "WHERE id = ?", (result, ts, realized_pct, note, sell_fact, cycle_id))
    con.close()
    apply_result(realized_pct, amount, path=path, ts=ts)
    return True


def _day_start(ts):
    """Начало календарных суток по МСК, в которые попадает `ts` (как trades._day_start)."""
    dt = datetime.datetime.fromtimestamp(ts, MSK)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _month_start(ts):
    """Начало календарного месяца по МСК, в который попадает `ts`."""
    dt = datetime.datetime.fromtimestamp(ts, MSK)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def bank_month_total(bank, path=DB_PATH, now=None):
    """Сумма виртуальных «оплат» по СБП со своего банка с начала календарного месяца по МСК — как
    trades.bank_month_total, но по кругам сухого прогона (учитывает круг с момента старта, а не
    только исполнившиеся: в реальности рубли уходят в момент оплаты). Переводы внутри банка лимит
    не тратят; старые круги без pay_kind — считаются, как раньше."""
    if not bank or not os.path.exists(path):
        return 0.0
    now = time.time() if now is None else now
    con = _connect(path)
    total, = con.execute("SELECT COALESCE(SUM(amount), 0) FROM cycles WHERE bank = ? AND ts_start >= ? "
                         "AND pay_kind IN ('sbp', '')", (bank, _month_start(now))).fetchone()
    con.close()
    return total


def banks_this_month(path=DB_PATH, now=None):
    """{банк: сумма виртуальных «оплат» за календарный месяц} — только банки, через которые прошёл
    хоть один круг в этом месяце; для /paper — увидеть, на каком банке виртуальный оборот подходит
    к бесплатному лимиту СБП (trades.free_limit) до реальных денег. Только учёт, без советов по
    обходу лимита."""
    if not os.path.exists(path):
        return {}
    now = time.time() if now is None else now
    con = _connect(path)
    rows = con.execute("SELECT DISTINCT bank FROM cycles WHERE bank != '' AND ts_start >= ? "
                       "AND pay_kind IN ('sbp', '')", (_month_start(now),)).fetchall()
    con.close()
    return {b: bank_month_total(b, path, now) for (b,) in rows}


def stats(path=DB_PATH, now=None):
    """{"day"/"week"/"all": {"total", "done", "failed", "failed_by_reason", "avg_diff"}} — для /paper.
    Считаем по завершённым кругам (result IS NOT NULL): total — сколько завершилось, done — исполнились
    полностью, failed/failed_by_reason — сколько и на какой стадии сорвалось, avg_diff — средняя
    (факт − план) в п.п. по исполнившимся (None — исполнившихся ещё не было). «day» — календарные сутки
    по МСК, «week» — последние 7 суток, «all» — за всё время."""
    now = time.time() if now is None else now
    starts = {"day": _day_start(now), "week": now - STATS_WEEK, "all": 0.0}
    empty = {"total": 0, "done": 0, "failed": 0, "failed_by_reason": {}, "avg_diff": None}
    if not os.path.exists(path):
        return {p: dict(empty) for p in starts}
    con = _connect(path)
    out = {}
    for period, start in starts.items():
        rows = con.execute("SELECT result, planned_pct, realized_pct FROM cycles "
                           "WHERE result IS NOT NULL AND ts_start >= ?", (start,)).fetchall()
        done_diffs = [r - p for res, p, r in rows if res == "done"]
        failed_by_reason = {}
        for res, _, _ in rows:
            if res != "done":
                failed_by_reason[res] = failed_by_reason.get(res, 0) + 1
        out[period] = {"total": len(rows), "done": len(done_diffs), "failed": len(rows) - len(done_diffs),
                       "failed_by_reason": failed_by_reason,
                       "avg_diff": sum(done_diffs) / len(done_diffs) if done_diffs else None}
    con.close()
    return out


def _median(values):
    """Медиана списка чисел; пустой список — None."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def ladder_suggestion(path=DB_PATH, now=None):
    """Лестница суммы круга — предложение, не смена настройки (её пишет вызывающий по кнопке).
    Повышение (LADDER_LOW → LADDER_HIGH): за всё время набралось ≥ LADDER_UP_MIN_CYCLES завершённых
    кругов, доля сорвавшихся ≤ LADDER_UP_MAX_FAILED и медиана (факт − план) по исполнившимся ≥
    LADDER_UP_MIN_MEDIAN п.п. Понижение (обратно на LADDER_LOW): сумма сейчас LADDER_HIGH и за
    последнюю неделю доля сорвавшихся > LADDER_DOWN_MIN_FAILED.
    Возвращает {"action": "up"/"down", "amount": ...} или None — предлагать нечего."""
    amount = settings()["amount"]
    if not os.path.exists(path):
        return None
    now = time.time() if now is None else now
    con = _connect(path)
    all_rows = con.execute("SELECT result, planned_pct, realized_pct FROM cycles "
                           "WHERE result IS NOT NULL").fetchall()
    week_rows = con.execute("SELECT result FROM cycles WHERE result IS NOT NULL AND ts_start >= ?",
                            (now - STATS_WEEK,)).fetchall()
    con.close()
    if amount < LADDER_HIGH and len(all_rows) >= LADDER_UP_MIN_CYCLES:
        failed = sum(1 for res, _, _ in all_rows if res != "done")
        median_diff = _median([realized - planned for res, planned, realized in all_rows if res == "done"])
        if (failed / len(all_rows) <= LADDER_UP_MAX_FAILED
                and median_diff is not None and median_diff >= LADDER_UP_MIN_MEDIAN):
            return {"action": "up", "amount": LADDER_HIGH}
    if amount >= LADDER_HIGH and week_rows:
        failed = sum(1 for (res,) in week_rows if res != "done")
        if failed / len(week_rows) > LADDER_DOWN_MIN_FAILED:
            return {"action": "down", "amount": LADDER_LOW}
    return None


def report_rows(path=DB_PATH):
    """План/факт, срывы по причинам, средняя длительность круга и нехватка глубины стакана —
    по каждой связке площадка/монета покупки → площадка/монета продажи (для `/paper report` и
    экспорта CSV). Только завершённые круги (result IS NOT NULL); depth_shortfall считает срывы
    на продаже с причиной «не хватает глубины стакана продажи» (текст из check_sell_stage)."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute(
        "SELECT buy_ex, buy_asset, sell_ex, sell_asset, result, planned_pct, realized_pct, "
        "ts_start, ts_stage, note FROM cycles WHERE result IS NOT NULL").fetchall()
    con.close()
    groups = {}
    for buy_ex, buy_asset, sell_ex, sell_asset, result, planned, realized, ts_start, ts_stage, note in rows:
        g = groups.setdefault((buy_ex, buy_asset, sell_ex, sell_asset), {
            "total": 0, "done": 0, "failed_by_reason": {}, "depth_shortfall": 0,
            "planned": [], "realized_done": [], "duration_done": []})
        g["total"] += 1
        g["planned"].append(planned)
        if result == "done":
            g["done"] += 1
            g["realized_done"].append(realized)
            g["duration_done"].append(ts_stage - ts_start)
        else:
            g["failed_by_reason"][result] = g["failed_by_reason"].get(result, 0) + 1
            if result == "failed_sell" and "не хватает глубины" in (note or ""):
                g["depth_shortfall"] += 1
    out = []
    for (buy_ex, buy_asset, sell_ex, sell_asset), g in sorted(groups.items()):
        out.append({
            "buy_ex": buy_ex, "buy_asset": buy_asset, "sell_ex": sell_ex, "sell_asset": sell_asset,
            "total": g["total"], "done": g["done"], "failed": g["total"] - g["done"],
            "failed_by_reason": g["failed_by_reason"], "depth_shortfall": g["depth_shortfall"],
            "avg_planned_pct": sum(g["planned"]) / len(g["planned"]) if g["planned"] else None,
            "avg_realized_pct": sum(g["realized_done"]) / len(g["realized_done"]) if g["realized_done"] else None,
            "avg_duration_min": (sum(g["duration_done"]) / len(g["duration_done"]) / 60)
                                 if g["duration_done"] else None,
        })
    return out


def label_stats(path=DB_PATH):
    """Итоги завершённых кругов по метке надёжности на старте (✅/⚠️/🪤, p2p.reliability): сколько
    кругов, сколько исполнилось, средний план и факт исполнившихся — видно, оправдывает ли себя метка."""
    if not os.path.exists(path):
        return {}
    con = _connect(path)
    rows = con.execute("SELECT label, result, planned_pct, realized_pct FROM cycles "
                       "WHERE result IS NOT NULL").fetchall()
    con.close()
    out = {}
    for label, result, planned, realized in rows:
        g = out.setdefault(label or "—", {"total": 0, "done": 0, "planned": [], "realized": []})
        g["total"] += 1
        g["planned"].append(planned)
        if result == "done":
            g["done"] += 1
            g["realized"].append(realized)
    return {k: {"total": g["total"], "done": g["done"],
                "avg_planned_pct": sum(g["planned"]) / len(g["planned"]),
                "avg_realized_pct": sum(g["realized"]) / len(g["realized"]) if g["realized"] else None}
            for k, g in out.items()}


def write_report_csv(rows, path=REPORT_CSV_PATH):
    """Экспорт report_rows() в CSV (по умолчанию data/paper_report.csv) для `/paper report`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(REPORT_COLUMNS)
        for r in rows:
            reasons = ";".join(f"{FAIL_LABELS.get(k, k)}:{v}" for k, v in r["failed_by_reason"].items())
            w.writerow([r[c] for c in REPORT_COLUMNS[:-1]] + [reasons])
    return path


def balance_change(path=DB_PATH):
    """На сколько виртуальный баланс изменился со старта — сумма результата всех завершённых кругов (₽);
    то же самое, что (текущий баланс − стартовый), без отдельного хранения стартового значения."""
    if not os.path.exists(path):
        return 0.0
    con = _connect(path)
    total, = con.execute(
        "SELECT COALESCE(SUM(amount * realized_pct / 100.0), 0) FROM cycles WHERE result IS NOT NULL").fetchone()
    con.close()
    return total
