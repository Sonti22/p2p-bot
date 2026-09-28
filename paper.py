"""Сухой прогон (paper trading) — этап 1 полуавтомата: виртуальные круги без реальных денег,
чтобы увидеть, что бот сделал бы сам, и сравнить план с фактом. Хранилище, движок, запуск круга
по сигналу, все три стадии (buy → transfer → sell) и статистика для команды /paper готовы.

SQLite data/paper.db, таблицы:
  cycles  — один виртуальный круг: сумма, объявления покупки/продажи на момент старта, маршрут,
            плановая прибыль (%), текущая стадия (buy → transfer → sell, пока круг открыт),
            итог (result: done/failed_buy/failed_transfer/failed_sell, пока NULL — круг открыт),
            реализованная прибыль (realized_pct) по ценам на момент стадий, как платили (pay_kind:
            intra — внутри банка, sbp — по СБП со своего банка, trades.pay_plan) и банк (bank) — для учёта
            виртуального оборота по бесплатному лимиту СБП. planned_pct — план с запасом на курс (его видит
            владелец), planned_raw — тот же план без запаса: с ним сравнивается факт (в факте запаса нет).
            Для разбора (этап 1 «измерения»): время окончания каждой стадии (ts_buy_done/ts_transfer_done/
            ts_sell_done — при переходе дальше или завершении круга на ней), на старте — индекс, причины
            надёжности и серия «живости» связки, сделки/% успешных мерчантов покупки и продажи, запас глубины
            стакана (depth_margin) и id снимка скана (snapshot_id, snapshots.py); на проверке покупки — лучшая
            цена и доступный объём у мерчантов покупки (buy_check_price/buy_check_avail). У старых кругов — NULL.
            Реализм (этап 2.4): цена покупки по свежему стакану и её сдвиг к плану (buy_fill_price/buy_slip_pct),
            время перевода по сетям, записанное на старте (transfer_min), риски стадий (risk_notes: неизвестный
            статус сети, покупка у других мерчантов). У старых кругов — NULL/пусто, стадии идут как раньше.
  balance — один виртуальный баланс: старт = PAPER_AMOUNT, меняется на realized_pct каждого
            завершённого круга.
"""
import csv
import dataclasses
import datetime
import json
import math
import os
import sqlite3
import time

import netstatus
import p2p
import trades

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "paper.db")
REPORT_CSV_PATH = os.path.join(HERE, "data", "paper_report.csv")
REPORT_COLUMNS = ("buy_ex", "buy_asset", "sell_ex", "sell_asset", "total", "done", "failed",
                   "depth_shortfall", "avg_planned_pct", "avg_realized_pct", "avg_duration_min",
                   "avg_buy_min", "avg_transfer_min", "avg_sell_min", "avg_index_start", "avg_streak_start",
                   "avg_depth_margin", "avg_buy_orders", "avg_buy_rate", "avg_sell_orders", "avg_sell_rate",
                   "avg_buy_check_drift_pct", "avg_buy_check_cover", "avg_buy_slip_pct", "buy_from_book",
                   "net_unknown", "fail_reasons", "failed_by_reason")

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
            "stage", "ts_stage", "realized_pct", "result", "note", "bank", "label", "sell_fact", "pay_kind",
            "sell_qty", "sell_net", "buy_nicks", "buy_net", "buy_pays", "sell_parts", "pay_fee_used",
            "planned_raw", "route_hops", "ts_buy_done", "ts_transfer_done", "ts_sell_done", "index_start",
            "reasons_start", "streak_start", "buy_orders", "buy_rate", "sell_orders", "sell_rate", "depth_margin",
            "buy_check_price", "buy_check_avail", "snapshot_id")
# колонки разбора (этап 1 «измерения») — в конце таблицы, в этом порядке и у новой базы, и у старой после миграции
_MEASURE_DDL = (("ts_buy_done", "REAL DEFAULT NULL"), ("ts_transfer_done", "REAL DEFAULT NULL"),
                ("ts_sell_done", "REAL DEFAULT NULL"), ("index_start", "INTEGER DEFAULT NULL"),
                ("reasons_start", "TEXT DEFAULT NULL"), ("streak_start", "INTEGER DEFAULT NULL"),
                ("buy_orders", "INTEGER DEFAULT NULL"), ("buy_rate", "REAL DEFAULT NULL"),
                ("sell_orders", "INTEGER DEFAULT NULL"), ("sell_rate", "REAL DEFAULT NULL"),
                ("depth_margin", "REAL DEFAULT NULL"), ("buy_check_price", "REAL DEFAULT NULL"),
                ("buy_check_avail", "REAL DEFAULT NULL"), ("snapshot_id", "INTEGER DEFAULT NULL"))
# план для сравнения с фактом: без запаса на курс; у кругов до planned_raw — план с запасом, как раньше
_PLAN_CMP = "COALESCE(planned_raw, planned_pct)"
# бумажный хедж круга шортом перпа (simperp.py): площадка, объём в монете, цены входа/выхода (USDT), комиссии,
# фандинг (+ получено шортом) и итог хеджа в USDT; hedge_state — JSON симуляции (статус, ожидаемая стоимость, расчёты)
HEDGE_COLUMNS = (("hedge_venue", "TEXT DEFAULT ''"), ("hedge_qty", "REAL DEFAULT NULL"),
                 ("hedge_open", "REAL DEFAULT NULL"), ("hedge_close", "REAL DEFAULT NULL"),
                 ("hedge_fees", "REAL DEFAULT NULL"), ("hedge_funding", "REAL DEFAULT NULL"),
                 ("hedge_pnl", "REAL DEFAULT NULL"), ("hedge_state", "TEXT DEFAULT ''"))
# колонки хеджа добавляет свой блок миграций после колонок разбора — и у новой базы, и у старой они последние
_COLUMNS += tuple(col for col, _ddl in HEDGE_COLUMNS)
# реализм прогона (план, этап 2.4): цена покупки по свежему стакану на проверке и её сдвиг к плану (%), время перевода
# круга по сетям (мин, считается на старте — перезапуск его не меняет) и риски стадий (JSON [[код, текст], ...])
REALISM_COLUMNS = (("buy_fill_price", "REAL DEFAULT NULL"), ("buy_slip_pct", "REAL DEFAULT NULL"),
                   ("transfer_min", "REAL DEFAULT NULL"), ("risk_notes", "TEXT DEFAULT ''"))
_COLUMNS += tuple(col for col, _ddl in REALISM_COLUMNS)

# Время перевода монеты между площадками по сети, мин: вывод + подтверждения до зачисления. Грубые средние по
# правилам зачисления бирж (число подтверждений × время блока + обработка вывода) — уточнять по факту прогона;
# переопределение — PAPER_NET_MINUTES="TRC20:3,BTC:40". Сеть неизвестна (нет в таблице или не выбрана) —
# PAPER_TRANSFER_MINUTES.
NET_MINUTES = {"TRC20": 3.0, "BEP20": 2.0, "ERC20": 6.0, "TON": 2.0, "SOL": 2.0, "POLYGON": 5.0, "ARBITRUM": 3.0,
               "APT": 2.0, "BTC": 40.0}

# Причины срыва по тексту note (как depth_shortfall в report_rows): (код, подстрока note, подпись для отчёта).
# Порядок важен: «конвертация на споте недоступна» — раньше общего «недоступна».
FAIL_REASONS = (
    ("buy_gone", "объявление покупки исчезло", "мерчант покупки ушёл, стакана не хватило"),
    ("buy_part", "остальные сумму не покрывают", "часть мерчантов ушла, остаток не покрыл"),
    ("buy_depth", "стакана покупки не хватает", "стакана покупки не хватило"),
    ("buy_slip", "цена ушла", "цена покупки ушла дальше допуска"),
    ("bc_stale", "нет свежей котировки BestChange", "нет свежей котировки BestChange"),
    ("spot", "конвертация на споте недоступна", "спот-конвертация недоступна"),
    ("venue_down", "недоступна", "площадка недоступна"),
    ("net_closed", "закрыт", "перевод закрыт"),
    ("sell_depth", "не хватает глубины стакана продажи", "не хватило глубины продажи"),
)
REASON_LABELS = {code: label for code, _sub, label in FAIL_REASONS} | {"other": "другое"}
# Риски круга (risk_notes): не срыв, но круг прошёл стадию на допущении — видно в отчёте
RISK_LABELS = {"net_unknown": "статус сети неизвестен", "buy_book": "покупка у других мерчантов"}


def _env_float(name, default):
    """Число из .env; пусто, не число, nan/inf или минус — значение по умолчанию."""
    try:
        v = float(os.getenv(name, default))
    except ValueError:
        return default
    return v if math.isfinite(v) and v >= 0 else default


def net_minutes_table():
    """NET_MINUTES с правками из PAPER_NET_MINUTES («сеть:минуты» через запятую; кривая часть пропускается)."""
    table = dict(NET_MINUTES)
    for part in os.getenv("PAPER_NET_MINUTES", "").split(","):
        net, _, val = part.partition(":")
        try:
            v = float(val)
        except ValueError:
            continue
        if net.strip() and math.isfinite(v) and 0 <= v < 1e4:
            table[net.strip().upper()] = v
    return table


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
        # площадка круга не отвечает дольше — круг сорван (иначе занятый слот висел бы вечно)
        "stale_minutes": float(os.getenv("PAPER_STALE_MINUTES", 30)),
        # покупка по свежему стакану хуже плана больше чем на столько % — круг не покупаем («цена ушла»)
        "buy_slip_max": _env_float("PAPER_BUY_SLIP_MAX", 1.0),
        # покупка у обменника: котировка BestChange по направлению должна быть не старше стольких минут
        "bc_fresh_minutes": _env_float("PAPER_BC_FRESH_MINUTES", 5.0),
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
                "bank TEXT DEFAULT '', label TEXT DEFAULT '', sell_fact REAL DEFAULT NULL, pay_kind TEXT DEFAULT '', "
                "sell_qty REAL DEFAULT NULL, sell_net TEXT DEFAULT '', buy_nicks TEXT DEFAULT '', "
                "buy_net TEXT DEFAULT '', buy_pays TEXT DEFAULT '[]', sell_parts INTEGER DEFAULT 1, "
                "pay_fee_used REAL DEFAULT 0, planned_raw REAL DEFAULT NULL, route_hops TEXT DEFAULT '', "
                + ", ".join(f"{col} {ddl}" for col, ddl in _MEASURE_DDL) + ")")
    cols = [r[1] for r in con.execute("PRAGMA table_info(cycles)")]
    for col, ddl in (("bank", "TEXT DEFAULT ''"), ("label", "TEXT DEFAULT ''"), ("sell_fact", "REAL DEFAULT NULL"),
                     ("pay_kind", "TEXT DEFAULT ''"), ("sell_qty", "REAL DEFAULT NULL"), ("sell_net", "TEXT DEFAULT ''"),
                     ("buy_nicks", "TEXT DEFAULT ''"), ("buy_net", "TEXT DEFAULT ''"),
                     ("buy_pays", "TEXT DEFAULT '[]'"), ("sell_parts", "INTEGER DEFAULT 1"),
                     ("pay_fee_used", "REAL DEFAULT 0"), ("planned_raw", "REAL DEFAULT NULL"),
                     ("route_hops", "TEXT DEFAULT ''"), *_MEASURE_DDL):
        if col not in cols:   # база от прошлой версии — добавляем колонку, данные не трогаем
            con.execute(f"ALTER TABLE cycles ADD COLUMN {col} {ddl}")
    for col, ddl in HEDGE_COLUMNS:   # хедж simperp — отдельным блоком миграций
        if col not in cols:
            con.execute(f"ALTER TABLE cycles ADD COLUMN {col} {ddl}")
    for col, ddl in REALISM_COLUMNS:   # реализм прогона (этап 2.4) — свой блок после хеджа
        if col not in cols:
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
    with con:
        _add_balance(con, amount * realized_pct / 100, ts if ts is not None else time.time())
    con.close()


def _add_balance(con, delta, ts):
    """Изменить баланс на delta ₽ в открытой транзакции con (баланса нет — от 0)."""
    con.execute("INSERT OR IGNORE INTO balance (id, amount, updated_ts) VALUES (1, 0, ?)", (ts,))
    con.execute("UPDATE balance SET amount = amount + ?, updated_ts = ? WHERE id = 1", (delta, ts))


def start_cycle(amount, buy, sell, route, planned_pct, path=DB_PATH, ts=None, label="", sell_qty=None, pay_fee=0.0,
                over=None, planned_raw=None, hops=None, index=None, reasons=None, streak=None, depth=None,
                snapshot_id=None):
    """Завести новый виртуальный круг со стадией buy. buy/sell — объявления покупки/продажи
    (p2p.Ad) на момент старта. Как платим (trades.pay_plan: внутри банка или по СБП со своего банка, у
    которого виртуальный лимит ещё есть) пишется сразу — в реальности рубли уходят в момент оплаты, до
    проверки стадии buy. label — метка надёжности
    связки на старте (p2p.reliability) — для разбора в отчёте. Для стадий запоминаем: sell_qty — сколько монеты
    продажи выходит по маршруту (бот передаёт объём стека продажи из _match: после комиссий, без запаса на курс,
    в монете продажи — и для межмонетных маршрутов; нет — оценка по плану, см. sell_qty()), sell_net — сеть обменника (стек продажи только этой сети), buy_nicks —
    все мерчанты, из которых собрана покупка на сумму; buy_net/buy_pays/sell_parts — сеть и способы оплаты
    покупки и число частей стакана продажи, нужные, чтобы на стадии sell пересчитать выход межмонетной/спот
    связки по свежему курсу (recompute_sell_qty); pay_fee — % комиссии банка (cfg.pay_fee), которой уже
    учтён при расчёте sell_qty на старте (сверх нужного — SBP_OVER_FEE, если банк за лимитом), запоминаем
    как pay_fee_used, чтобы recompute_sell_qty не пересчитывал её заново с чужим over_banks. over — банки за
    лимитом СБП, с которыми бот считал план и sell_qty (реальные сделки trades + виртуальный оборот прогона):
    банк оплаты и pay_fee_used берём по ним же, иначе комиссия в круге разошлась бы с планом; не передан —
    только виртуальный оборот прогона. planned_raw — план без запаса на курс (для сравнения с фактом).
    hops — p2p.route_hops(buy, sell, ...) (площадки конвертации и сеть/комиссия каждого хопа маршрута на
    момент старта) — не передан, пишем пустой маршрут; стадия transfer проверяет именно эти переводы
    (transfer_check), стадия sell считает выход по их комиссиям (qty_from_hops).
    Для разбора: index/reasons — индекс и причины надёжности связки на старте (p2p.reliability_index/reliability),
    streak — сколько сканов подряд она держалась (Bot.live), depth — запас глубины (depth_margin), snapshot_id —
    id снимка скана (snapshots.scan_id); сделки/% успешных мерчантов берём из buy/sell.
    По hops же — время перевода круга по сетям (transfer_min, hops_transfer_minutes): пишется сразу, чтобы перезапуск
    или правка PAPER_NET_MINUTES не меняли уже идущий круг.
    Возвращает id круга."""
    ts = ts if ts is not None else time.time()
    transfer_min = hops_transfer_minutes(hops["hops"], net_minutes_table(), settings()["transfer_minutes"]) \
        if hops and hops.get("hops") else None
    if over is None:
        own = trades.own_banks()[0]
        over = {b for b in own if bank_month_total(b, path, ts) >= trades.free_limit(b)}
    kind, bank = trades.pay_plan(buy.pays, over=over)
    pay_fee_used = trades.SBP_OVER_FEE if kind == "sbp" and bank in over and pay_fee < trades.SBP_OVER_FEE else pay_fee
    con = _connect(path)
    with con:
        cur = con.execute(
            "INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, buy_price, buy_nick, sell_ex, sell_asset, "
            "sell_price, sell_nick, route, planned_pct, stage, ts_stage, bank, label, pay_kind, sell_qty, sell_net, "
            "buy_nicks, buy_net, buy_pays, sell_parts, pay_fee_used, planned_raw, route_hops, index_start, "
            "reasons_start, streak_start, buy_orders, buy_rate, sell_orders, sell_rate, depth_margin, snapshot_id, "
            "transfer_min) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, amount, buy.ex, buy.asset, buy.price, buy.nick,
             sell.ex, sell.asset, sell.price, sell.nick, route, planned_pct, ts, bank, label, kind,
             sell_qty, sell.net or "", json.dumps(list(buy.nicks or (buy.nick,)), ensure_ascii=False),
             buy.net or "", json.dumps(list(buy.pays or []), ensure_ascii=False), sell.parts or 1, pay_fee_used,
             planned_raw, json.dumps(hops, ensure_ascii=False) if hops else "",
             index, json.dumps(list(reasons), ensure_ascii=False) if reasons is not None else None, streak,
             buy.orders, buy.rate, sell.orders, sell.rate, depth, snapshot_id, transfer_min))
        cycle_id = cur.lastrowid
    con.close()
    return cycle_id


def _dicts(cur):
    """Строки SELECT * — словари по именам колонок из курсора: порядок колонок в базе зависит от порядка миграций
    (у каждой ветки свои ALTER TABLE) и не обязан совпадать с _COLUMNS. По позиции (zip с _COLUMNS) строки
    cycles не читать — остальные чтения берут колонки явным списком."""
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def get_cycle(cycle_id, path=DB_PATH):
    """Круг по id — словарь со всеми колонками, None — не найден."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    rows = _dicts(con.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,)))
    con.close()
    return rows[0] if rows else None


def open_cycles(path=DB_PATH):
    """Незавершённые круги (result ещё не выставлен), по времени старта — для PAPER_MAX_OPEN и
    для обработки стадий."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = _dicts(con.execute("SELECT * FROM cycles WHERE result IS NULL ORDER BY id"))
    con.close()
    return rows


def set_stage(cycle_id, stage, path=DB_PATH, ts=None):
    """Перевести открытый круг на следующую стадию (buy → transfer → sell); время окончания прошлой стадии —
    в ts_<стадия>_done (уже записанное не трогаем)."""
    ts = ts if ts is not None else time.time()
    prev = STAGES[STAGES.index(stage) - 1] if stage in STAGES[1:] else None
    con = _connect(path)
    with con:
        con.execute("UPDATE cycles SET stage = ?, ts_stage = ? WHERE id = ?", (stage, ts, cycle_id))
        if prev:
            con.execute(f"UPDATE cycles SET ts_{prev}_done = COALESCE(ts_{prev}_done, ?) WHERE id = ?", (ts, cycle_id))
    con.close()


def depth_margin(snap, buy, sell, amount, qty):
    """Запас глубины на старте круга: во сколько раз стакан (snap.groups, у обменника — той же сети) покрывает
    круг — покупка: объём в фиате к сумме круга, продажа: монета к выходу маршрута qty; меньшее из двух.
    None — стакана одной из сторон нет или объём круга не задан."""
    buy_ads = p2p._same_net(snap.groups.get((buy.ex, "buy", buy.asset), []), buy)
    sell_ads = p2p._same_net(snap.groups.get((sell.ex, "sell", sell.asset), []), sell)
    if not buy_ads or not sell_ads or not amount or not qty or amount <= 0 or qty <= 0:
        return None
    fiat = sum(min(a.max_amt, a.avail * a.price) for a in buy_ads if a.avail > 0)
    coin = sum(min(a.avail, a.max_amt / a.price) for a in sell_ads if a.avail > 0 and a.price > 0)
    return round(min(fiat / amount, coin / qty), 3)


def buy_observed(cycle, snap):
    """Что видно на проверке покупки: (лучшая цена, доступный объём в фиате) у мерчантов покупки круга в свежем
    стакане — (None, 0.0), если их там нет."""
    nicks = set(json.loads(cycle.get("buy_nicks") or "[]") or [cycle["buy_nick"]])
    left = [a for a in snap.groups.get((cycle["buy_ex"], "buy", cycle["buy_asset"]), []) if a.nick in nicks]
    if not left:
        return None, 0.0
    return min(a.price for a in left), sum(min(a.max_amt, a.avail * a.price) for a in left if a.avail > 0)


def set_buy_check(cycle_id, price, avail, path=DB_PATH):
    """Запомнить итог проверки покупки (buy_observed) — цену и объём, с которыми круг прошёл или сорвался."""
    con = _connect(path)
    with con:
        con.execute("UPDATE cycles SET buy_check_price = ?, buy_check_avail = ? WHERE id = ?", (price, avail, cycle_id))
    con.close()


def _venue_down(snap, ex, asset):
    """Площадка в этом скане не ответила: запрос упал или она на паузе (snap.errors) — по такому снимку решать
    о круге нельзя, ждём следующего. Ответила, но годных объявлений нет (группы нет) — это пустой стакан."""
    n = (ex or "").lower()
    return n in snap.errors or f"{n}/{asset}" in snap.errors


def _stale(cycle, stale_minutes, now, after=0.0):
    """Площадка молчит дольше stale_minutes с момента, когда стадию уже можно было решать (after — сек ожидания)."""
    return now - cycle["ts_stage"] - after > stale_minutes * 60


def _buy_nicks(cycle):
    return set(json.loads(cycle.get("buy_nicks") or "[]") or [cycle["buy_nick"]])


def _buy_book(cycle, snap):
    """Свежий стакан покупки круга (годные объявления snap.groups, лучшая цена первой); у обменника — только сеть круга."""
    grp = snap.groups.get((cycle["buy_ex"], "buy", cycle["buy_asset"]), [])
    if cycle["buy_ex"] == "BestChange" and cycle.get("buy_net"):
        grp = [a for a in grp if a.net == cycle["buy_net"]]
    return grp


def buy_fill(cycle, snap):
    """Покупка на сумму круга по свежему стакану (только чтение): сначала мерчанты круга (buy_nicks) по их СВЕЖИМ цене,
    лимитам и объёму, не хватило (ушли, подняли минимум, мало монеты) — дальше остальные объявления стакана по цене
    (проскальзывание; у обменника — той же сети). Лимиты и объём — как в p2p._stack. Возвращает {"price": средняя
    цена, "slip_pct": сдвиг к плановой цене (+ — дороже), "book": пришлось ли брать у других мерчантов, "own": сколько
    мерчантов круга на месте} или None — стакана на сумму не хватает."""
    nicks = _buy_nicks(cycle)
    grp = _buy_book(cycle, snap)
    own = sorted((a for a in grp if a.nick in nicks), key=lambda a: a.price)
    others = [a for a in grp if a.nick not in nicks]
    st = p2p._stack(own + others, cycle["amount"])
    if st is None:
        return None
    used = set(st.nicks or (st.nick,))
    return {"price": st.price, "slip_pct": (st.price / cycle["buy_price"] - 1) * 100, "book": bool(used - nicks),
            "own": len(own)}


def bc_data_ts(snap, asset):
    """Время выгрузки BestChange, из которой собран снимок, по монете: fetched_ts объявлений обменников (у всей выгрузки
    одно время скачивания), без объявлений — по замеру запроса (конец запроса − возраст данных); 0 — неизвестно."""
    ts = [a.fetched_ts for side in ("buy", "sell") for a in snap.groups.get(("BestChange", side, asset), [])
          if a.fetched_ts]
    if ts:
        return max(ts)
    for rec in snap.jobs:
        if rec.get("ex") == "bestchange" and rec.get("asset") == asset and rec.get("age") is not None:
            return rec.get("t1", 0.0) - rec["age"]
    return 0.0


def bc_quote_fresh(cycle, snap, now, fresh_minutes):
    """Покупка у обменника засчитывается только по свежей котировке BestChange этого направления: выгрузка скачана
    после начала стадии (не та, по которой круг стартовал) и не старше fresh_minutes. Старая выгрузка из кэша (сбой
    скачивания, VPN) живёт в скане сколько угодно — по ней покупка «проходила» бы всегда."""
    ts = bc_data_ts(snap, cycle["buy_asset"])
    return bool(ts) and ts > cycle["ts_stage"] and now - ts <= fresh_minutes * 60


def check_buy_stage(cycle, snap, pay_minutes, now=None, stale_minutes=30.0, slip_max=None, bc_fresh_minutes=None):
    """Проверка исполнимости стадии buy по свежему снимку (только чтение snap.groups, без сети и
    без записи в БД — решение применяет вызывающий). Раньше PAPER_PAY_MINUTES с начала круга не
    ждём. После — покупаем по свежему стакану (buy_fill): мерчанты круга по их текущим цене, лимитам и объёму, не
    хватило — остальные объявления по цене (проскальзывание). Цена хуже плана больше slip_max % (PAPER_BUY_SLIP_MAX)
    — круг не покупаем («цена ушла»). Площадка в этом скане не ответила — ждём (дольше stale_minutes — срыв
    «площадка недоступна»). Покупка у обменника (BestChange) — только по свежей котировке направления (bc_quote_fresh,
    PAPER_BC_FRESH_MINUTES): её нет — ждём, дольше stale_minutes — срыв «нет свежей котировки BestChange».

    Возвращает (action, note):
      "wait"    — ещё не прошло pay_minutes, площадка не ответила или нет свежей котировки BestChange;
      "advance" — на сумму круга стакана хватает по цене в пределах допуска — можно переходить к transfer
                  (цену и проскальзывание вызывающий берёт из buy_fill);
      "fail"    — result станет failed_buy, note — причина.
    """
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < pay_minutes * 60:
        return "wait", ""
    if _venue_down(snap, cycle["buy_ex"], cycle["buy_asset"]):
        if _stale(cycle, stale_minutes, now, after=pay_minutes * 60):
            return "fail", f"площадка {cycle['buy_ex']} недоступна"
        return "wait", ""
    st = settings()
    if cycle["buy_ex"] == "BestChange":
        fresh = st["bc_fresh_minutes"] if bc_fresh_minutes is None else bc_fresh_minutes
        if not bc_quote_fresh(cycle, snap, now, fresh):
            if _stale(cycle, stale_minutes, now, after=pay_minutes * 60):
                return "fail", (f"нет свежей котировки BestChange по {cycle['buy_asset']}"
                                f"{' (' + cycle['buy_net'] + ')' if cycle.get('buy_net') else ''} "
                                f"(не старше {fresh:g} мин)")
            return "wait", ""
    nicks = _buy_nicks(cycle)
    grp = _buy_book(cycle, snap)
    left = [a for a in grp if a.nick in nicks]
    fill = buy_fill(cycle, snap) if grp else None
    if fill is None:
        if not left:
            return "fail", "объявление покупки исчезло"
        if len(nicks) > 1:
            return "fail", "часть мерчантов покупки ушла, остальные сумму не покрывают"
        return "fail", "стакана покупки не хватает на сумму круга"
    limit = st["buy_slip_max"] if slip_max is None else slip_max
    if fill["slip_pct"] > limit:
        return "fail", (f"цена ушла: покупка по {fill['price']:g} ₽ вместо {cycle['buy_price']:g} ₽ "
                        f"({fill['slip_pct']:+.2f}%, допуск {limit:g}%)")
    return "advance", ""


def buy_fill_risks(fill):
    """Риски покупки для risk_notes: пришлось брать у других мерчантов (проскальзывание по стакану)."""
    if not fill or not fill["book"]:
        return []
    return [["buy_book", f"мерчанты круга ушли или их мало — покупка у других по {fill['price']:g} ₽ "
                         f"({fill['slip_pct']:+.2f}% к плану)"]]


def set_buy_fill(cycle_id, fill, path=DB_PATH):
    """Запомнить покупку по свежему стакану (buy_fill): цену и сдвиг к плану; взяли у других мерчантов — риск в круге."""
    if not fill:
        return
    con = _connect(path)
    with con:
        con.execute("UPDATE cycles SET buy_fill_price = ?, buy_slip_pct = ? WHERE id = ?",
                    (fill["price"], fill["slip_pct"], cycle_id))
    con.close()
    add_risks(cycle_id, buy_fill_risks(fill), path=path)


def cycle_risks(cycle):
    """Риски круга [[код, текст], ...] из risk_notes (у старых кругов — пусто)."""
    try:
        risks = json.loads(cycle.get("risk_notes") or "[]")
    except ValueError:
        return []
    return [r for r in risks if isinstance(r, list) and len(r) == 2]


def add_risks(cycle_id, risks, path=DB_PATH):
    """Дописать риски в risk_notes круга (без повторов)."""
    if not risks:
        return
    con = _connect(path)
    row = con.execute("SELECT risk_notes FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is not None:
        have = cycle_risks({"risk_notes": row[0]})
        have += [list(r) for r in risks if list(r) not in have]
        with con:
            con.execute("UPDATE cycles SET risk_notes = ? WHERE id = ?",
                        (json.dumps(have, ensure_ascii=False), cycle_id))
    con.close()


def _real_hops(hops):
    """Хопы, где монета действительно переводится (у той же биржи — нет; обменник всегда внешний)."""
    return [h for h in hops if h.get("frm") != h.get("to") or h.get("frm") == "BestChange"]


def hops_transfer_minutes(hops, table, default):
    """Время перевода круга, мин: сумма по реальным переводам маршрута по таблице сетей (NET_MINUTES с правками
    PAPER_NET_MINUTES); сеть хопа неизвестна или её нет в таблице — default (PAPER_TRANSFER_MINUTES). Обменник →
    свой кошелёк на Bybit → другой обменник — два перевода. Переводов нет (одна биржа) — 0."""
    def one(net):
        return table.get((net or "").upper(), default) if net else default
    total = 0.0
    for h in _real_hops(hops):
        if h.get("frm") == "BestChange" and h.get("to") == "BestChange":
            total += one(h.get("frm_net")) + one(h.get("to_net"))
        else:
            total += one(h.get("to_net") or h.get("frm_net"))
    return total


def transfer_minutes_for(cycle, default):
    """Сколько ждать стадию transfer: записанное на старте время по сетям (transfer_min), у кругов без него —
    по сохранённым хопам и таблице сейчас, без хопов (старые круги) — default, как раньше."""
    if cycle.get("transfer_min") is not None:
        try:
            return float(cycle["transfer_min"])
        except (TypeError, ValueError):
            pass
    hops = cycle_hops(cycle)["hops"]
    return hops_transfer_minutes(hops, net_minutes_table(), default) if hops else default


def _net_state(venue, asset, net, deposit):
    """Статус ввода (deposit) или вывода монеты в сети по живому справочнику: True/False, None — неизвестно. Справочник
    площадки есть, а такой сети в нём нет — закрыта (как в p2p._withdraw). Сеть не выбрана — неизвестно."""
    if not net:
        return None
    known = netstatus.known_nets(venue, asset)
    if known and net not in known:
        return False
    return (netstatus.deposit_ok if deposit else netstatus.withdraw_ok)(venue, asset, net)


def _hop_state(h):
    """(закрыто: [текст], неизвестно: [текст]) для сохранённого хопа маршрута. Обменник шлёт монету сам — проверяем
    только ввод у получателя в сети обменника; обменник → Bybit → обменник — ввод и вывод на Bybit; биржа → биржа —
    вывод у отправителя и ввод у получателя; на обменник — только вывод (его приём проверен при старте по стакану)."""
    frm, to, asset = h.get("frm"), h.get("to"), h.get("asset")
    net, frm_net = h.get("to_net") or "", h.get("frm_net") or ""
    if frm == "BestChange" and to == "BestChange":
        checks = [("ввод", "Bybit", frm_net, True), ("вывод", "Bybit", net, False)]
    elif frm == "BestChange":
        checks = [("ввод", to, frm_net or net, True)]
    else:
        checks = [("вывод", frm, net, False)] + ([("ввод", to, net, True)] if to != "BestChange" else [])
    closed, unknown = [], []
    for what, venue, n, deposit in checks:
        state = _net_state(venue, asset, n, deposit)
        where = f"{'на' if deposit else 'с'} {venue}"
        if state is False:
            closed.append(f"{what} {asset}{f' ({n})' if n else ''} {where} закрыт")
        elif state is None:
            unknown.append(f"{what} {asset}{f' ({n})' if n else ''} {where}" if n
                           else f"{what} {asset} {where}: сеть не выбрана")
    return closed, unknown


def transfer_check(cycle, cfg):
    """Переводы круга по живому справочнику (только чтение netstatus/fees.json): (срыв: текст или None, риски). С
    сохранёнными хопами (route_hops) — именно они: монета и сеть каждого перевода маршрута (buy_ex → площадка
    конвертации в монете покупки, → вторая площадка в USDT, → sell_ex в монете продажи), а не вывод монеты покупки
    напрямую buy_ex → sell_ex; buy_ex == sell_ex с конвертацией на третьей бирже — тоже оба перевода. Закрыт вывод или
    ввод — срыв. Статус неизвестен (у площадки нет справочника, ключа или сеть не выбрана) — не «открыто», а риск
    ["net_unknown", текст]. Круг без хопов (старая версия) — вывод монеты покупки buy_ex → sell_ex, как раньше."""
    hops = cycle_hops(cycle)["hops"]
    if not hops:
        if cycle["buy_ex"] == cycle["sell_ex"] and cycle["buy_ex"] != "BestChange":
            return None, []
        if cycle["buy_ex"] == "BestChange":
            hops = [{"frm": "BestChange", "frm_net": cycle.get("buy_net") or "", "to": cycle["sell_ex"],
                     "to_net": cycle.get("buy_net") or "", "asset": cycle["buy_asset"]}]
        else:
            w = p2p._withdraw(cfg, cycle["buy_ex"], cycle["buy_asset"], receiver=cycle["sell_ex"])
            if w is None:
                return f"вывод {cycle['buy_asset']} с {cycle['buy_ex']} закрыт", []
            hops = [{"frm": cycle["buy_ex"], "frm_net": "", "to": cycle["sell_ex"], "to_net": w[1],
                     "asset": cycle["buy_asset"]}]
    risks = []
    for h in _real_hops(hops):
        closed, unknown = _hop_state(h)
        if closed:
            return "; ".join(closed), []
        risks += [["net_unknown", f"{h['frm']}→{h['to']}: статус неизвестен — {u}"] for u in unknown]
    return None, risks


def transfer_risks(cycle, cfg):
    """Риски стадии transfer (неизвестный статус сетей переводов) — для risk_notes при переходе к продаже."""
    return transfer_check(cycle, cfg)[1]


def check_transfer_stage(cycle, cfg, transfer_minutes, now=None):
    """Проверка стадии transfer по свежему справочнику (только чтение fees.json/netstatus, без сети и без записи в
    БД). Ждём время перевода круга по сетям (transfer_minutes_for: записанное на старте, у старых кругов —
    transfer_minutes = PAPER_TRANSFER_MINUTES), потом проверяем переводы маршрута (transfer_check): закрыт вывод или
    ввод — перевод сорвался бы в реальности. Неизвестный статус сети — не срыв, а риск круга (transfer_risks).

    Возвращает (action, note) как check_buy_stage: "wait"/"advance"/"fail" (result станет
    failed_transfer)."""
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < transfer_minutes_for(cycle, transfer_minutes) * 60:
        return "wait", ""
    closed, _risks = transfer_check(cycle, cfg)
    if closed:
        return "fail", closed
    return "advance", ""


def simple_route(deal):
    """Связку можно честно прогнать: покупка и продажа одной монеты без конвертаций на споте. Межмонетные и через
    промежуточную монету пока не берём (разбор #110): площадка конвертации и сети хопов в круге не хранятся —
    пересчёт на продаже мог уйти на другую биржу, сбой спота срывал круг сразу, transfer проверял не ту монету.
    Условия возврата — ROADMAP «Прогон: межмонетные связки, часть 2». «через Bybit» (обменник → свой кошелёк на
    Bybit → другой обменник) тоже не берём: стадия transfer такой ретранслятор не проверяет, а круги BestChange ↔
    BestChange в прогоне 25.09 завышали баланс."""
    _, b, s, route = deal
    return b.asset == s.asset and "спот" not in (route or "") and "через" not in (route or "")


def cycle_hops(cycle):
    """Площадки конвертации и хопы маршрута, сохранённые при старте круга (start_cycle(..., hops=...) —
    p2p.route_hops на момент старта); круг без записи (старая версия или простая связка без hops) —
    {"venues": [], "hops": []}."""
    raw = cycle.get("route_hops")
    return json.loads(raw) if raw else {"venues": [], "hops": []}


def sell_qty(cycle):
    """Сколько монеты продажи выходит по маршруту круга: сохранённый при старте объём стека продажи; у кругов
    старой версии — оценка из плана (сумма × (1 + план) / плановая цена продажи)."""
    if cycle.get("sell_qty"):
        return cycle["sell_qty"]
    return cycle["amount"] * (1 + cycle["planned_pct"] / 100) / cycle["sell_price"]


def _fill_price(cycle):
    """Цена покупки по факту: записанная на проверке покупки (buy_fill_price), иначе плановая."""
    try:
        fill = float(cycle.get("buy_fill_price") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    return fill if fill > 0 else cycle["buy_price"]


def _coins_bought(cycle, price):
    return cycle["amount"] * (1 - (cycle.get("pay_fee_used") or 0.0) / 100) / price


def _same_asset_qty(cycle, price):
    """Выход простой связки (одна монета) при покупке по price: купленная монета минус те же комиссии переводов, что
    заложены в сохранённом sell_qty (он посчитан при плановой цене). Цена — плановая — sell_qty как есть."""
    base = sell_qty(cycle)
    if abs(price - cycle["buy_price"]) <= 1e-12 * max(1.0, price):
        return base
    fees = max(_coins_bought(cycle, cycle["buy_price"]) - base, 0.0)
    return _coins_bought(cycle, price) - fees


def qty_from_hops(cycle, hops, cfg, spot, price):
    """Выход маршрута в монете продажи по сохранённым хопам (p2p.route_hops на старте): комиссия каждого перевода —
    из хопа (сети заново не проверяем), конвертации — по свежему spot именно сохранённых площадок (venues) с
    комиссией спота из cfg, покупка — по price. Та же арифметика, что p2p._route_qty без запаса на курс и банка (банк —
    pay_fee_used). None — тикера сохранённой площадки нет или хопы не сходятся с маршрутом."""
    venues, hs = hops.get("venues") or [], hops.get("hops") or []
    b_asset, s_asset = cycle["buy_asset"], cycle["sell_asset"]
    qty = _coins_bought(cycle, price)
    if not venues:
        return qty - sum(h.get("fee") or 0.0 for h in hs)
    if len(venues) == 1 and len(hs) == 2:
        v = venues[0]
        q = spot.get(v, {})
        sf = p2p._spot_fee(cfg, v)
        qty -= hs[0].get("fee") or 0.0
        if "USDT" in (b_asset, s_asset):
            alt = s_asset if b_asset == "USDT" else b_asset
            if alt not in q:
                return None
            bid, ask = q[alt]
            qty = (qty / ask if b_asset == "USDT" else qty * bid) * (1 - sf / 100)
        else:
            if b_asset not in q or s_asset not in q:
                return None
            qty = qty * q[b_asset][0] * (1 - sf / 100)
            qty = qty / q[s_asset][1] * (1 - sf / 100)
        return qty - (hs[1].get("fee") or 0.0)
    if len(venues) == 2 and len(hs) == 3:
        v1, v2 = venues
        if b_asset not in spot.get(v1, {}) or s_asset not in spot.get(v2, {}):
            return None
        qty -= hs[0].get("fee") or 0.0
        qty = qty * spot[v1][b_asset][0] * (1 - p2p._spot_fee(cfg, v1) / 100)
        qty -= hs[1].get("fee") or 0.0
        qty = qty / spot[v2][s_asset][1] * (1 - p2p._spot_fee(cfg, v2) / 100)
        return qty - (hs[2].get("fee") or 0.0)
    return None


def recompute_sell_qty(cycle, cfg, spot):
    """Свежий выход маршрута в монете продажи по текущим курсам спота (snap.spot из свежего скана) —
    для межмонетных связок и связок через промежуточную монету: курс между стартом круга и стадией sell
    мог уйти, факт должен это увидеть, а не застревать на числе, посчитанном при старте (sell_qty).
    Простая связка (одна монета для покупки и продажи, спот не участвует) — сохранённый sell_qty не
    устаревает, возвращаем его как есть.

    Комиссия банка (и решение о том, какой банк исчерпал лимит СБП) уже приняты на старте и сохранены
    как pay_fee_used — здесь не пересчитываем их заново с чужим over_banks, а просто уменьшаем сумму
    круга на эту долю и отключаем банковскую комиссию в _route_qty (disable={"bank"}), пересчитывая
    только шаги перевода/спота монеты. None — маршрут (нужная спот-пара) сейчас недоступен.

    Реализм (этап 2.4): покупка идёт по цене проверки buy_fill_price (свежий стакан, проскальзывание), если она
    записана, иначе по плану. Круг с сохранёнными хопами (route_hops) пересчитывается по ним (qty_from_hops): комиссии
    переводов — сохранённые на старте, сети, приём у обменника и минимум вывода уже прошедших переводов заново не
    проверяются (ROADMAP «межмонетные, часть 2», п. 3) — меняется только курсовая часть."""
    price = _fill_price(cycle)
    if cycle["buy_asset"] == cycle["sell_asset"]:
        return _same_asset_qty(cycle, price)
    hops = cycle_hops(cycle)
    if hops["hops"]:
        return qty_from_hops(cycle, hops, cfg, spot, price)
    b = p2p.Ad(cycle["buy_ex"], "buy", price, 0, 0, 0, json.loads(cycle.get("buy_pays") or "[]"),
               cycle["buy_nick"], 0, 0, asset=cycle["buy_asset"], net=cycle.get("buy_net") or "")
    s = p2p.Ad(cycle["sell_ex"], "sell", cycle["sell_price"], 0, 0, 0, [], cycle["sell_nick"], 0, 0,
               asset=cycle["sell_asset"], net=cycle.get("sell_net") or "", parts=cycle.get("sell_parts") or 1)
    net_amount = cycle["amount"] * (1 - (cycle.get("pay_fee_used") or 0.0) / 100)
    route_cfg = dataclasses.replace(cfg, amount=net_amount)
    return p2p._route_qty(b, s, route_cfg, spot, disable=frozenset({"bank", "risk"}))


def check_sell_stage(cycle, snap, cfg=None, now=None, stale_minutes=30.0):
    """Проверка стадии sell по свежему снимку (только чтение snap.groups, без сети и без записи
    в БД). Монета уже на площадке продажи — продаём тем, кто сейчас есть в стакане: цена — средняя по
    лучшим объявлениям на весь объём круга (p2p.sell_fill_price, тот же стек, что в _match), даже если
    плановый мерчант ушёл или цена стала хуже (факт тогда ниже плана, может быть и минус). Срыв — только
    если покупателей на весь объём нет.

    cfg передан — объём для межмонетных/спот связок пересчитывается по свежему snap.spot
    (recompute_sell_qty), чтобы факт видел движение курса спота между стартом круга и продажей; без cfg
    (старые вызовы/простая связка) — используем сохранённый на старте sell_qty, как раньше. Пересчёт не
    находит маршрут (спот-пара пропала) — срыв.

    Возвращает (action, note, price): price — фактическая цена продажи (для realized_pct) при
    "advance", note — чем факт отличается от плана; "fail" — result станет failed_sell; "wait" — площадка
    продажи (или площадка конвертации на споте — см. ниже) в этом скане не ответила (дольше stale_minutes —
    срыв). Объём — выход маршрута в монете продажи, у обменника — только стакан той же сети (sell_net), что
    и в плане.

    Межмонетная/спот связка (cfg передан, cycle_hops хранит venues) — курс берём именно с площадок,
    сохранённых при старте круга (p2p.spot_venues_ready), другую биржу вместо пропавшего тикера не
    подбираем: сбой — тоже «wait»/срыв по stale_minutes, как и пропажа площадки продажи."""
    now = now if now is not None else time.time()
    if _venue_down(snap, cycle["sell_ex"], cycle["sell_asset"]):
        if _stale(cycle, stale_minutes, now):
            return "fail", f"площадка {cycle['sell_ex']} недоступна", None
        return "wait", "", None
    if cfg is not None:
        venues = cycle_hops(cycle)["venues"]
        if venues and not p2p.spot_venues_ready(snap.spot, venues, cycle["buy_asset"], cycle["sell_asset"]):
            if _stale(cycle, stale_minutes, now):
                return "fail", f"площадка {'/'.join(venues)} недоступна", None
            return "wait", "", None
    qty = recompute_sell_qty(cycle, cfg, snap.spot) if cfg is not None else sell_qty(cycle)
    if qty is None:
        return "fail", "конвертация на споте недоступна", None
    ads = snap.groups.get((cycle["sell_ex"], "sell", cycle["sell_asset"]), [])
    if cycle.get("sell_net"):
        ads = [a for a in ads if a.net == cycle["sell_net"]]
    price = p2p.sell_fill_price(ads, qty)
    if price is None:
        return "fail", "не хватает глубины стакана продажи", None
    note = ""
    if abs(price / cycle["sell_price"] - 1) >= 1e-4:
        note = (f"продажа по {price:g} ₽ вместо {cycle['sell_price']:g} ₽ "
                f"({(price / cycle['sell_price'] - 1) * 100:+.2f}%)")
    return "advance", note, price


def realized_pct(cycle, sell_price, qty=None):
    """Итоговая прибыль круга по факту: выручка за выход маршрута (уже после комиссий банка, вывода и
    спота, без гипотетического запаса на курс) по фактической цене продажи против суммы круга. qty —
    фактический выход, посчитанный на стадии sell (recompute_sell_qty по свежему споту, если был
    пересчёт) — используем именно его, а не сохранённый на старте cycle["sell_qty"], чтобы факт видел
    движение курса спота; не передан — как раньше (сохранённый sell_qty или, у кругов совсем старой
    версии без него, план, масштабированный на изменение цены продажи)."""
    used = qty if qty is not None else cycle.get("sell_qty")
    if used:
        return (used * sell_price / cycle["amount"] - 1) * 100
    return ((1 + cycle["planned_pct"] / 100) * sell_price / cycle["sell_price"] - 1) * 100


def finish_cycle(cycle_id, result, realized_pct=0.0, note="", path=DB_PATH, ts=None, sell_fact=None):
    """Завершить круг: result — 'done' (успех) или 'failed_buy'/'failed_transfer'/'failed_sell'
    (срыв на соответствующей стадии). Обновляет виртуальный баланс на realized_pct (для срыва —
    обычно 0, круг не состоялся); sell_fact — фактическая цена продажи. Итог круга и баланс — одной
    транзакцией: сбой посередине откатывает оба (круг остаётся открытым, следующий скан завершит его снова).
    Круга с таким id нет или он уже завершён — возвращает False, баланс не трогает (иначе повторный вызов
    начислил бы прибыль дважды)."""
    ts = ts if ts is not None else time.time()
    con = _connect(path)
    try:
        with con:
            row = con.execute("SELECT amount, stage FROM cycles WHERE id = ? AND result IS NULL",
                              (cycle_id,)).fetchone()
            if row is None:
                return False
            amount, stage = row
            # result IS NULL и в самом UPDATE: круг, который успел завершить другой вызов, второй раз не начисляем
            cur = con.execute("UPDATE cycles SET result = ?, ts_stage = ?, realized_pct = ?, note = ?, sell_fact = ? "
                              "WHERE id = ? AND result IS NULL", (result, ts, realized_pct, note, sell_fact, cycle_id))
            if cur.rowcount != 1:
                return False
            if stage in STAGES:   # стадия, на которой круг закончился (исполнился или сорвался), — её время окончания
                con.execute(f"UPDATE cycles SET ts_{stage}_done = COALESCE(ts_{stage}_done, ?) WHERE id = ?",
                            (ts, cycle_id))
            _add_balance(con, amount * realized_pct / 100, ts)
    finally:
        con.close()
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
    (факт − план) в п.п. по исполнившимся (None — исполнившихся ещё не было; план — без запаса на курс,
    _PLAN_CMP). «day» — календарные сутки по МСК, «week» — последние 7 суток, «all» — за всё время."""
    now = time.time() if now is None else now
    starts = {"day": _day_start(now), "week": now - STATS_WEEK, "all": 0.0}
    empty = {"total": 0, "done": 0, "failed": 0, "failed_by_reason": {}, "avg_diff": None}
    if not os.path.exists(path):
        return {p: dict(empty) for p in starts}
    con = _connect(path)
    out = {}
    for period, start in starts.items():
        rows = con.execute(f"SELECT result, {_PLAN_CMP}, realized_pct FROM cycles "
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


def summary_since(since, path=DB_PATH, notes=3):
    """Итог кругов, завершённых (result IS NOT NULL) с начала `since` — для утреннего дайджеста «за сутки»: как
    stats() за период, плюс profit_rub — сумма результата исполнившихся кругов в ₽ (amount × realized_pct), и
    top_notes — до `notes` самых частых пояснений срывов [(текст, сколько раз)] (пустые не считаем)."""
    out = {"total": 0, "done": 0, "failed": 0, "failed_by_reason": {}, "avg_diff": None, "profit_rub": 0.0,
           "top_notes": []}
    if not os.path.exists(path):
        return out
    con = _connect(path)
    rows = con.execute(f"SELECT result, {_PLAN_CMP}, realized_pct, amount, note FROM cycles "
                       "WHERE result IS NOT NULL AND ts_start >= ?", (since,)).fetchall()
    con.close()
    diffs, counts = [], {}
    for res, plan, real, amount, note in rows:
        out["total"] += 1
        if res == "done":
            out["done"] += 1
            if plan is not None and real is not None:
                diffs.append(real - plan)
            out["profit_rub"] += (amount or 0) * (real or 0) / 100
            continue
        out["failed"] += 1
        out["failed_by_reason"][res] = out["failed_by_reason"].get(res, 0) + 1
        note = (note or "").strip()
        if note:
            counts[note] = counts.get(note, 0) + 1
    out["avg_diff"] = sum(diffs) / len(diffs) if diffs else None
    out["top_notes"] = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:notes]
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
    LADDER_UP_MIN_MEDIAN п.п. (план без запаса на курс — иначе запас сдвигал бы факт вверх и прятал
    неблагоприятный курс). Понижение (обратно на LADDER_LOW): сумма сейчас LADDER_HIGH и за
    последнюю неделю доля сорвавшихся > LADDER_DOWN_MIN_FAILED.
    Возвращает {"action": "up"/"down", "amount": ...} или None — предлагать нечего."""
    amount = settings()["amount"]
    if not os.path.exists(path):
        return None
    now = time.time() if now is None else now
    con = _connect(path)
    all_rows = con.execute(f"SELECT result, {_PLAN_CMP}, realized_pct FROM cycles "
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


_MEASURE_SQL = ("amount, buy_price, ts_buy_done, ts_transfer_done, ts_sell_done, index_start, streak_start, "
                "depth_margin, buy_orders, buy_rate, sell_orders, sell_rate, buy_check_price, buy_check_avail")


def _measures(ts_start, amount, buy_price, t_buy, t_transfer, t_sell, index, streak, depth, buy_orders, buy_rate,
              sell_orders, sell_rate, check_price, check_avail):
    """Поля разбора одного круга: длительность стадий (мин; нет отметки — None), значения на старте, на проверке
    покупки — сдвиг цены к плану (%) и объём у мерчантов к сумме круга (ушли все — 0)."""
    def minutes(a, b):
        return (b - a) / 60 if a is not None and b is not None else None
    return {"buy_min": minutes(ts_start, t_buy), "transfer_min": minutes(t_buy, t_transfer),
            "sell_min": minutes(t_transfer, t_sell), "index_start": index, "streak_start": streak,
            "depth_margin": depth, "buy_orders": buy_orders, "buy_rate": buy_rate, "sell_orders": sell_orders,
            "sell_rate": sell_rate,
            "buy_check_drift_pct": (check_price / buy_price - 1) * 100 if check_price and buy_price else None,
            "buy_check_cover": check_avail / amount if check_avail is not None and amount else None}


def _measure_avgs(items):
    """{"avg_<поле>": среднее по кругам, где поле есть (None — ни у одного)} для списка _measures."""
    out = {}
    for key in (items[0] if items else {}):
        vals = [m[key] for m in items if m[key] is not None]
        out[f"avg_{key}"] = sum(vals) / len(vals) if vals else None
    return out


def report_rows(path=DB_PATH):
    """План/факт, срывы по причинам, средняя длительность круга и нехватка глубины стакана —
    по каждой связке площадка/монета покупки → площадка/монета продажи (для `/paper report` и
    экспорта CSV). Только завершённые круги (result IS NOT NULL); depth_shortfall считает срывы
    на продаже с причиной «не хватает глубины стакана продажи» (текст из check_sell_stage). План — без запаса
    на курс (_PLAN_CMP), чтобы сравнивался с фактом. Поля разбора (_measures) — средние по кругам, где они
    записаны: длительность стадий (мин), индекс/серия/запас глубины на старте, сделки и % мерчантов, на проверке
    покупки — сдвиг цены (%) и объём к сумме круга; у старых кругов их нет — None."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute(
        f"SELECT buy_ex, buy_asset, sell_ex, sell_asset, result, {_PLAN_CMP}, realized_pct, "
        f"ts_start, ts_stage, risk_notes, buy_slip_pct, note, {_MEASURE_SQL} FROM cycles "
        "WHERE result IS NOT NULL").fetchall()
    con.close()
    groups = {}
    for buy_ex, buy_asset, sell_ex, sell_asset, result, planned, realized, ts_start, ts_stage, risk_notes, slip, note, \
            *m in rows:
        g = groups.setdefault((buy_ex, buy_asset, sell_ex, sell_asset), {
            "total": 0, "done": 0, "failed_by_reason": {}, "depth_shortfall": 0, "fail_reasons": {}, "risks": {},
            "slips": [], "planned": [], "realized_done": [], "duration_done": [], "measures": []})
        g["total"] += 1
        g["planned"].append(planned)
        g["measures"].append(_measures(ts_start, *m))
        for code in dict.fromkeys(code for code, _text in cycle_risks({"risk_notes": risk_notes})):
            g["risks"][code] = g["risks"].get(code, 0) + 1   # кругов с этим риском, а не записей
        if slip is not None:
            g["slips"].append(float(slip))
        if result == "done":
            g["done"] += 1
            g["realized_done"].append(realized)
            g["duration_done"].append(ts_stage - ts_start)
        else:
            g["failed_by_reason"][result] = g["failed_by_reason"].get(result, 0) + 1
            code = fail_reason(note)
            g["fail_reasons"][code] = g["fail_reasons"].get(code, 0) + 1
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
            **_measure_avgs(g["measures"]),
            "avg_buy_slip_pct": sum(g["slips"]) / len(g["slips"]) if g["slips"] else None,
            "buy_from_book": g["risks"].get("buy_book", 0), "net_unknown": g["risks"].get("net_unknown", 0),
            "fail_reasons": g["fail_reasons"],
        })
    return out


def fail_reason(note):
    """Код причины срыва по тексту note (FAIL_REASONS); не распознана — "other"."""
    for code, sub, _label in FAIL_REASONS:
        if sub in (note or ""):
            return code
    return "other"


def first_start(path=DB_PATH):
    """Время старта самого первого круга (epoch) — начало окна сравнения с реальными сделками; None — кругов нет."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    ts, = con.execute("SELECT MIN(ts_start) FROM cycles").fetchone()
    con.close()
    return ts


def label_stats(path=DB_PATH):
    """Итоги завершённых кругов по метке надёжности на старте (✅/⚠️/🪤, p2p.reliability): сколько
    кругов, сколько исполнилось, средний план (без запаса на курс) и факт исполнившихся — видно, оправдывает
    ли себя метка; срывы по стадиям и средние полей разбора (avg_<поле> как в report_rows)."""
    if not os.path.exists(path):
        return {}
    con = _connect(path)
    rows = con.execute(f"SELECT label, result, {_PLAN_CMP}, realized_pct, ts_start, {_MEASURE_SQL} FROM cycles "
                       "WHERE result IS NOT NULL").fetchall()
    con.close()
    out = {}
    for label, result, planned, realized, ts_start, *m in rows:
        g = out.setdefault(label or "—", {"total": 0, "done": 0, "planned": [], "realized": [],
                                          "failed_by_reason": {}, "measures": []})
        g["total"] += 1
        g["planned"].append(planned)
        g["measures"].append(_measures(ts_start, *m))
        if result == "done":
            g["done"] += 1
            g["realized"].append(realized)
        else:
            g["failed_by_reason"][result] = g["failed_by_reason"].get(result, 0) + 1
    return {k: {"total": g["total"], "done": g["done"],
                "avg_planned_pct": sum(g["planned"]) / len(g["planned"]),
                "avg_realized_pct": sum(g["realized"]) / len(g["realized"]) if g["realized"] else None,
                "failed_by_reason": g["failed_by_reason"], **_measure_avgs(g["measures"])}
            for k, g in out.items()}


def write_report_csv(rows, path=REPORT_CSV_PATH):
    """Экспорт report_rows() в CSV (по умолчанию data/paper_report.csv) для `/paper report`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(REPORT_COLUMNS)
        for r in rows:
            reasons = ";".join(f"{FAIL_LABELS.get(k, k)}:{v}" for k, v in r["failed_by_reason"].items())
            cells = [r.get(c) for c in REPORT_COLUMNS[:-1]]   # нет поля — пустая ячейка
            if isinstance(r.get("fail_reasons"), dict):        # причины срыва — подписями: «цена покупки ушла…:2»
                cells[REPORT_COLUMNS.index("fail_reasons")] = ";".join(
                    f"{REASON_LABELS.get(k, k)}:{v}" for k, v in r["fail_reasons"].items())
            w.writerow(cells + [reasons])
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


def reset(path=DB_PATH, now=None):
    """/paper reset: база прогона уходит в архив рядом — paper-archive-ГГГГММДД-ЧЧММ.db (по МСК; архив не
    удаляется, второй сброс в ту же минуту — с суффиксом -2, -3…), на её месте — пустая база той же схемы
    (статистика, баланс и лестница — с нуля, открытые круги тоже в архиве). Соединений модуль не держит —
    каждая функция закрывает своё, файл свободен. Настройки PAPER/PAPER_AMOUNT живут в .env — не трогаем.
    Кругов нет — архивировать нечего, None; иначе {"archive": путь архива, "cycles": сколько кругов,
    "change": итог завершённых, ₽}."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    total, = con.execute("SELECT COUNT(*) FROM cycles").fetchone()
    con.close()
    if not total:
        return None
    change = balance_change(path)
    stamp = datetime.datetime.fromtimestamp(time.time() if now is None else now, MSK).strftime("%Y%m%d-%H%M")
    base = os.path.join(os.path.dirname(path), f"paper-archive-{stamp}")
    archive, n = base + ".db", 1
    while os.path.exists(archive):
        n += 1
        archive = f"{base}-{n}.db"
    os.rename(path, archive)
    _connect(path).close()
    return {"archive": archive, "cycles": total, "change": change}
