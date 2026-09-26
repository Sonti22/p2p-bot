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
  balance — один виртуальный баланс: старт = PAPER_AMOUNT, меняется на realized_pct каждого
            завершённого круга.
"""
import csv
import dataclasses
import datetime
import json
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
                   "avg_buy_min", "avg_transfer_min", "avg_sell_min", "avg_index_start", "avg_streak_start",
                   "avg_depth_margin", "avg_buy_orders", "avg_buy_rate", "avg_sell_orders", "avg_sell_rate",
                   "avg_buy_check_drift_pct", "avg_buy_check_cover", "failed_by_reason")

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
    момент старта) — не передан, пишем пустой маршрут; для стадий transfer/sell позже (ROADMAP «межмонетные,
    часть 2») — в этой задаче только сохраняем, не используем.
    Для разбора: index/reasons — индекс и причины надёжности связки на старте (p2p.reliability_index/reliability),
    streak — сколько сканов подряд она держалась (Bot.live), depth — запас глубины (depth_margin), snapshot_id —
    id снимка скана (snapshots.scan_id); сделки/% успешных мерчантов берём из buy/sell.
    Возвращает id круга."""
    ts = ts if ts is not None else time.time()
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
            "reasons_start, streak_start, buy_orders, buy_rate, sell_orders, sell_rate, depth_margin, snapshot_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, amount, buy.ex, buy.asset, buy.price, buy.nick,
             sell.ex, sell.asset, sell.price, sell.nick, route, planned_pct, ts, bank, label, kind,
             sell_qty, sell.net or "", json.dumps(list(buy.nicks or (buy.nick,)), ensure_ascii=False),
             buy.net or "", json.dumps(list(buy.pays or []), ensure_ascii=False), sell.parts or 1, pay_fee_used,
             planned_raw, json.dumps(hops, ensure_ascii=False) if hops else "",
             index, json.dumps(list(reasons), ensure_ascii=False) if reasons is not None else None, streak,
             buy.orders, buy.rate, sell.orders, sell.rate, depth, snapshot_id))
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


def check_buy_stage(cycle, snap, pay_minutes, now=None, stale_minutes=30.0):
    """Проверка исполнимости стадии buy по свежему снимку (только чтение snap.groups, без сети и
    без записи в БД — решение применяет вызывающий). Раньше PAPER_PAY_MINUTES с начала круга не
    ждём. После — ищем в текущем стакане покупки то же объявление (тот же мерчант): нет — мерчант
    ушёл/снял объявление, круг не состоялся. Цену не сравниваем: в ордере она фиксируется при создании.
    Покупка собрана из нескольких объявлений (buy_nicks) — срыв, только если ушли все мерчанты. Площадка в
    этом скане не ответила — ждём (дольше stale_minutes — срыв «площадка недоступна»).

    Возвращает (action, note):
      "wait"    — ещё не прошло pay_minutes или площадка не ответила, ничего не решаем;
      "advance" — объявление на месте — можно переходить к transfer;
      "fail"    — все мерчанты покупки ушли (result станет failed_buy).
    """
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < pay_minutes * 60:
        return "wait", ""
    if _venue_down(snap, cycle["buy_ex"], cycle["buy_asset"]):
        if _stale(cycle, stale_minutes, now, after=pay_minutes * 60):
            return "fail", f"площадка {cycle['buy_ex']} недоступна"
        return "wait", ""
    nicks = set(json.loads(cycle.get("buy_nicks") or "[]") or [cycle["buy_nick"]])
    left = [a for a in snap.groups.get((cycle["buy_ex"], "buy", cycle["buy_asset"]), []) if a.nick in nicks]
    if not left:
        return "fail", "объявление покупки исчезло"
    if len(nicks) > 1 and p2p._stack(left, cycle["amount"]) is None:   # составная покупка: оставшиеся — на всю сумму?
        return "fail", "часть мерчантов покупки ушла, остальные сумму не покрывают"
    return "advance", ""


def check_transfer_stage(cycle, cfg, transfer_minutes, now=None):
    """Проверка стадии transfer по свежему справочнику (только чтение fees.json/netstatus через
    p2p.withdraw_open, без сети и без записи в БД). Раньше PAPER_TRANSFER_MINUTES с начала стадии
    не ждём. После — проверяем, что вывод buy_asset с buy_ex на sell_ex всё ещё возможен (известна комиссия,
    сеть открыта, получатель принимает) — иначе перевод сорвался бы в реальности. Внутри одной площадки перевода
    нет; монету от обменника (buy_ex=BestChange) шлёт сам обменник — сведений о его выводе нет, не проверяем.

    Возвращает (action, note) как check_buy_stage: "wait"/"advance"/"fail" (result станет
    failed_transfer)."""
    now = now if now is not None else time.time()
    if now - cycle["ts_stage"] < transfer_minutes * 60:
        return "wait", ""
    if cycle["buy_ex"] == cycle["sell_ex"] or cycle["buy_ex"] == "BestChange":
        return "advance", ""
    if not p2p.withdraw_open(cfg, cycle["buy_ex"], cycle["buy_asset"], receiver=cycle["sell_ex"]):
        return "fail", f"вывод {cycle['buy_asset']} с {cycle['buy_ex']} закрыт"
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


def recompute_sell_qty(cycle, cfg, spot):
    """Свежий выход маршрута в монете продажи по текущим курсам спота (snap.spot из свежего скана) —
    для межмонетных связок и связок через промежуточную монету: курс между стартом круга и стадией sell
    мог уйти, факт должен это увидеть, а не застревать на числе, посчитанном при старте (sell_qty).
    Простая связка (одна монета для покупки и продажи, спот не участвует) — сохранённый sell_qty не
    устаревает, возвращаем его как есть.

    Комиссия банка (и решение о том, какой банк исчерпал лимит СБП) уже приняты на старте и сохранены
    как pay_fee_used — здесь не пересчитываем их заново с чужим over_banks, а просто уменьшаем сумму
    круга на эту долю и отключаем банковскую комиссию в _route_qty (disable={"bank"}), пересчитывая
    только шаги перевода/спота монеты. None — маршрут (нужная спот-пара) сейчас недоступен."""
    if cycle["buy_asset"] == cycle["sell_asset"]:
        return sell_qty(cycle)
    b = p2p.Ad(cycle["buy_ex"], "buy", cycle["buy_price"], 0, 0, 0, json.loads(cycle.get("buy_pays") or "[]"),
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
    обычно 0, круг не состоялся); sell_fact — фактическая цена продажи. Круга с таким id нет —
    возвращает False, баланс не трогает."""
    ts = ts if ts is not None else time.time()
    con = _connect(path)
    row = con.execute("SELECT amount, stage FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is None:
        con.close()
        return False
    amount, stage = row
    with con:
        con.execute("UPDATE cycles SET result = ?, ts_stage = ?, realized_pct = ?, note = ?, sell_fact = ? "
                    "WHERE id = ?", (result, ts, realized_pct, note, sell_fact, cycle_id))
        if stage in STAGES:   # стадия, на которой круг закончился (исполнился или сорвался), — её время окончания
            con.execute(f"UPDATE cycles SET ts_{stage}_done = COALESCE(ts_{stage}_done, ?) WHERE id = ?",
                        (ts, cycle_id))
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
        f"ts_start, ts_stage, note, {_MEASURE_SQL} FROM cycles WHERE result IS NOT NULL").fetchall()
    con.close()
    groups = {}
    for buy_ex, buy_asset, sell_ex, sell_asset, result, planned, realized, ts_start, ts_stage, note, *m in rows:
        g = groups.setdefault((buy_ex, buy_asset, sell_ex, sell_asset), {
            "total": 0, "done": 0, "failed_by_reason": {}, "depth_shortfall": 0,
            "planned": [], "realized_done": [], "duration_done": [], "measures": []})
        g["total"] += 1
        g["planned"].append(planned)
        g["measures"].append(_measures(ts_start, *m))
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
            **_measure_avgs(g["measures"]),
        })
    return out


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
            w.writerow([r.get(c) for c in REPORT_COLUMNS[:-1]] + [reasons])   # нет поля — пустая ячейка
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
