"""История спредов: SQLite data/history.db — лучший % по каждой паре площадок, раз в 5 минут
(пишет scan_loop бота). Агрегации для /history: лучшее время суток и хитмап час×день недели за
7 дней (МСК = UTC+3, без перехода на летнее время), медиана P2P против BestChange за окно 7-30 дней.

Таблица history_routes (этап 1 «измерения») — в тот же момент лучший % по каждой связке с монетами (площадка и
монета покупки, площадка и монета продажи): связок в скане в 10–15 раз больше, чем пар площадок, поэтому они лежат
отдельно — /history, /backtest и калибровка читают только history (строка на пару площадок, как раньше). Срок
хранения тот же, RETENTION.

Таблица signals (этап 1 «измерения») — эпизоды связок выше порога сигнала: связка непрерывно, скан за сканом,
держится от порога — одна строка: первый и последний скан, сколько сканов, максимум прибыли, был ли сигнал (и когда)
и, если не было, почему (последняя преграда: unconfirmed / max_signals / cooldown / quiet / trap / paused / unsent /
stale — данные площадки устарели: она не ответила за VENUE_TIMEOUT, это пропуск).
Пишет бот после отправки сигналов (Bot.record_signals); доля «пропущенных» — signal_stats().

Таблица bank_spreads — в тот же момент, что history, лучший % связок по банку: side "buy" — банк, которым платим
мерчанту на покупке (способы оплаты объявления покупки), "sell" — банк, в который получаем рубли на продаже; «SBP» —
способ «СБП» без названия банка. /banks без монеты — bank_spread_stats() за 7 дней. Срок хранения тот же.
"""
import os
import sqlite3
import statistics
import time

import trades

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
                "buy_ex TEXT, sell_ex TEXT, asset_buy TEXT, asset_sell TEXT, profit REAL, ref REAL, amount REAL)")
    cols = {r[1] for r in con.execute("PRAGMA table_info(history)")}
    if "amount" not in cols:   # старая база без колонки — довносим её
        con.execute("ALTER TABLE history ADD COLUMN amount REAL")
    con.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history (ts)")
    con.execute("CREATE TABLE IF NOT EXISTS history_routes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                "buy_ex TEXT, asset_buy TEXT, sell_ex TEXT, asset_sell TEXT, profit REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_history_routes_ts ON history_routes (ts)")
    con.execute("CREATE TABLE IF NOT EXISTS signals ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, buy_ex TEXT, buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, "
                "first_seen REAL, last_seen REAL, scans INTEGER, max_profit REAL, signalled INTEGER, signal_ts REAL, "
                "reason_not_signalled TEXT, amount REAL, min_profit REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_signals_last ON signals (last_seen)")
    con.execute("CREATE TABLE IF NOT EXISTS bank_spreads ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, side TEXT, bank TEXT, profit REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bank_spreads_ts ON bank_spreads (ts)")
    return con


def _insert(rows, path=DB_PATH, routes=(), banks=()):
    """rows: [(ts, buy_ex, sell_ex, asset_buy, asset_sell, profit, ref[, amount]), ...].
    Сумма круга (amount) необязательна для обратной совместимости со старыми записями/тестами —
    без неё в базе останется NULL, и backtest() возьмёт сумму, переданную в него самого.
    routes — строки history_routes [(ts, buy_ex, asset_buy, sell_ex, asset_sell, profit), ...], пишутся в той же
    транзакции. banks — строки bank_spreads [(ts, side, bank, profit), ...], там же."""
    if not rows and not routes and not banks:
        return
    rows = [r if len(r) == 8 else (*r, None) for r in rows]
    con = _connect(path)
    with con:
        con.executemany("INSERT INTO history (ts, buy_ex, sell_ex, asset_buy, asset_sell, profit, ref, amount) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.executemany("INSERT INTO history_routes (ts, buy_ex, asset_buy, sell_ex, asset_sell, profit) "
                        "VALUES (?, ?, ?, ?, ?, ?)", list(routes))
        con.executemany("INSERT INTO bank_spreads (ts, side, bank, profit) VALUES (?, ?, ?, ?)", list(banks))
    con.close()


_last = {"t": 0.0}   # время последней записи — троттлинг раз в 5 минут (сбрасывается в тестах)


def ad_banks(ad):
    """Банки способов оплаты объявления (trades.bank_of), «SBP» — есть СБП без названия банка; без повторов."""
    out = []
    for p in ad.pays:
        bank = trades.bank_of(p) or ("SBP" if trades.is_sbp(p) else "")
        if bank and bank not in out:
            out.append(bank)
    return out


def _bank_best(deals):
    """{(side, bank): лучший % связки} по сделкам скана: buy — банки объявления покупки, sell — продажи."""
    best = {}
    for profit, b, s, _route in deals:
        for side, ad in (("buy", b), ("sell", s)):
            for bank in ad_banks(ad):
                key = (side, bank)
                if key not in best or profit > best[key]:
                    best[key] = profit
    return best


def record(snap, amount=None, path=DB_PATH):
    """Лучший % по каждой паре площадок (buy_ex, sell_ex) из snap.deals + ориентир snap.ref и
    сумма круга (`amount`, cfg.amount на момент скана — от неё зависит сам % через комиссию вывода
    и глубину стакана), чтобы backtest() честно переводил исторический % в рубли той суммы, для
    которой он был посчитан, а не текущей настройки. В history_routes — лучший % по каждой связке с монетами
    (buy_ex, монета покупки, sell_ex, монета продажи) того же момента. Не чаще раза в 5 минут; пустой снимок (нет
    связок) кулдаун не расходует — запишем, как только связки появятся."""
    now = time.time()
    if now - _last["t"] < THROTTLE or not snap.deals:
        return False
    best = {}     # (buy_ex, sell_ex) -> (profit, asset_buy, asset_sell)
    routes = {}   # (buy_ex, asset_buy, sell_ex, asset_sell) -> profit
    for profit, b, s, _route in snap.deals:
        key = (b.ex, s.ex)
        if key not in best or profit > best[key][0]:
            best[key] = (profit, b.asset, s.asset)
        rkey = (b.ex, b.asset, s.ex, s.asset)
        if rkey not in routes or profit > routes[rkey]:
            routes[rkey] = profit
    _insert([(now, buy_ex, sell_ex, ab, sa, profit, snap.ref, amount)
            for (buy_ex, sell_ex), (profit, ab, sa) in best.items()], path,
            routes=[(now, *rkey, profit) for rkey, profit in routes.items()],
            banks=[(now, side, bank, profit) for (side, bank), profit in _bank_best(snap.deals).items()])
    _last["t"] = now
    return True


SIGNAL_REASONS = ("unconfirmed", "max_signals", "cooldown", "quiet", "trap", "paused", "unsent", "stale")


def track_signals(rows, ts, open_ids, amount=None, min_profit=None, path=DB_PATH):
    """Эпизоды связок выше порога сигнала (таблица signals) по одному скану.
    rows — [(ключ (buy_ex, монета, sell_ex, монета), прибыль %, сигнал ушёл в этом скане, причина или None)] — все
    связки скана выше порога; ts — время скана. open_ids — {ключ: id строки} открытых эпизодов (хранит вызывающий,
    после рестарта бота эпизоды начинаются заново): связка есть и в прошлом скане — дописываем её строку (последний
    скан, максимум прибыли, сигнал; причина — последняя, у связки с сигналом — пусто), новой — новая строка; ключ,
    которого в rows нет, выпал из-под порога — эпизод закрыт. Возвращает open_ids для следующего скана."""
    out = {}
    if not rows:   # выше порога никого — все эпизоды закрыты, писать нечего
        return out
    con = _connect(path)
    try:
        with con:
            for key, profit, sent, reason in rows:
                sent = 1 if sent else 0
                reason = None if sent else reason
                rid = open_ids.get(key)
                if rid is not None:
                    cur = con.execute(
                        "UPDATE signals SET last_seen = ?, scans = scans + 1, max_profit = MAX(max_profit, ?), "
                        "signal_ts = COALESCE(signal_ts, CASE WHEN ? THEN ? END), "
                        "reason_not_signalled = CASE WHEN signalled OR ? THEN NULL ELSE ? END, "
                        "signalled = MAX(signalled, ?) WHERE id = ?",
                        (ts, profit, sent, ts, sent, reason, sent, rid))
                    if cur.rowcount:
                        out[key] = rid
                        continue
                cur = con.execute(
                    "INSERT INTO signals (buy_ex, buy_asset, sell_ex, sell_asset, first_seen, last_seen, scans, "
                    "max_profit, signalled, signal_ts, reason_not_signalled, amount, min_profit) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (*key, ts, ts, profit, sent, ts if sent else None, reason, amount, min_profit))
                out[key] = cur.lastrowid
    finally:
        con.close()
    return out


# причины, по которым связка без сигнала — не пропуск: тишина и пауза — выбор владельца, cooldown — сигнал по ней
# уже был недавно (антидубль), trap — ловушки не шлём по настройке (причина бывает только при SIGNAL_TRAPS=0)
NOT_MISSED = ("quiet", "paused", "cooldown", "trap")


def _cooldown():
    """COOLDOWN бота (сек антидубля, по умолчанию 600) — как Bot.cooldown."""
    try:
        return float(os.getenv("COOLDOWN", 600))
    except ValueError:
        return 600.0


def _merged_episodes(rows, gap):
    """Эпизоды одной связки, разделённые перерывом короче gap сек (связка на скан-другой ушла под порог и вернулась),
    — один эпизод: первый/последний скан, сигнал — был ли хоть в одной части, причина — последней части.
    rows — (ключ, first_seen, last_seen, signalled, reason) в порядке ключа и first_seen."""
    out = []
    for key, first, last, signalled, reason in rows:
        ep = out[-1] if out else None
        if ep and ep["key"] == key and first - ep["last"] < gap:
            if last >= ep["last"]:
                ep["last"], ep["reason"] = last, reason
            ep["signalled"] = ep["signalled"] or bool(signalled)
        else:
            out.append({"key": key, "first": first, "last": last, "signalled": bool(signalled), "reason": reason})
    return out


def bank_spread_stats(days=7, path=DB_PATH, now=None):
    """Спред связок по банкам за `days` дней (bank_spreads): {side: [(банк, срезов, средний лучший %, лучший %,
    доля срезов с плюсом)]}, внутри стороны — по убыванию среднего. side: "buy" — чем платим мерчанту на покупке,
    "sell" — куда получаем на продаже. Базы или данных нет — {}."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return {}
    con = _connect(path)
    rows = con.execute("SELECT side, bank, COUNT(*), AVG(profit), MAX(profit), "
                       "SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) FROM bank_spreads WHERE ts >= ? "
                       "GROUP BY side, bank", (now - days * 86400,)).fetchall()
    con.close()
    out = {}
    for side, bank, n, avg, best, pos in rows:
        out.setdefault(side, []).append((bank, n, avg, best, pos / n if n else 0.0))
    for side in out:
        out[side].sort(key=lambda r: (-r[2], r[0]))
    return out


def signal_stats(path=DB_PATH, days=7, now=None, min_minutes=3, cooldown=None):
    """Сводка эпизодов signals за `days` дней: всего, с сигналом, «долгие» (держались ≥ min_minutes от первого до
    последнего скана) и сколько из них прошло без сигнала — доля пропущенных связок (цель этапа 2 — ≤ 10%), и
    причины пропуска. Эпизоды одной связки с перерывом короче cooldown (по умолчанию COOLDOWN) — один эпизод:
    связка просела на скан и вернулась — это та же возможность. Долгие без сигнала по причинам NOT_MISSED (тишина,
    пауза, антидубль, выключенные ловушки) — не пропуск: они в "excluded" и не входят в долю. Доля — пропущенные
    из долгих без исключённых; таких нет — None. Эпизод, который ещё идёт, считается по уже увиденным сканам."""
    now = time.time() if now is None else now
    gap = _cooldown() if cooldown is None else cooldown
    out = {"episodes": 0, "signalled": 0, "long": 0, "missed": 0, "missed_share": None, "reasons": {},
           "excluded": 0, "excluded_reasons": {}}
    if not os.path.exists(path):
        return out
    con = _connect(path)
    rows = con.execute("SELECT buy_ex, buy_asset, sell_ex, sell_asset, first_seen, last_seen, signalled, "
                       "reason_not_signalled FROM signals WHERE last_seen >= ? "
                       "ORDER BY buy_ex, buy_asset, sell_ex, sell_asset, first_seen, id",
                       (now - days * 86400,)).fetchall()
    con.close()
    for ep in _merged_episodes([(r[:4], *r[4:]) for r in rows], gap):
        out["episodes"] += 1
        out["signalled"] += 1 if ep["signalled"] else 0
        if ep["last"] - ep["first"] < min_minutes * 60:
            continue
        out["long"] += 1
        if ep["signalled"]:
            continue
        reason = ep["reason"] or "?"
        if reason in NOT_MISSED:
            out["excluded"] += 1
            counts = out["excluded_reasons"]
        else:
            out["missed"] += 1
            counts = out["reasons"]
        counts[reason] = counts.get(reason, 0) + 1
    eligible = out["long"] - out["excluded"]
    if eligible:
        out["missed_share"] = out["missed"] / eligible
    return out


def cleanup(path=DB_PATH, now=None):
    """Удалить записи старше 30 дней (и строки history_routes того же возраста, и эпизоды signals, закончившиеся
    раньше); возвращает число удалённых строк history."""
    if not os.path.exists(path):
        return 0
    now = time.time() if now is None else now
    con = _connect(path)
    with con:
        cur = con.execute("DELETE FROM history WHERE ts < ?", (now - RETENTION,))
        con.execute("DELETE FROM history_routes WHERE ts < ?", (now - RETENTION,))
        con.execute("DELETE FROM signals WHERE last_seen < ?", (now - RETENTION,))
        con.execute("DELETE FROM bank_spreads WHERE ts < ?", (now - RETENTION,))
    con.close()
    return cur.rowcount


def _rows(path, since):
    """(ts, buy_ex, sell_ex, profit, amount) — строка на пару площадок и запись, в порядке записи."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute("SELECT ts, buy_ex, sell_ex, profit, amount FROM history WHERE ts >= ? ORDER BY ts, id",
                       (since,)).fetchall()
    con.close()
    return rows


def is_empty(path=DB_PATH):
    """Нет ни одной записи — одна строка из базы, а не вся таблица."""
    if not os.path.exists(path):
        return True
    con = _connect(path)
    try:
        return con.execute("SELECT 1 FROM history LIMIT 1").fetchone() is None
    finally:
        con.close()


def hourly_avg(path=DB_PATH, days=7, now=None):
    """{час МСК 0..23: средний лучший % за `days` дней} — None, если по часу нет данных."""
    now = time.time() if now is None else now
    buckets = {h: [] for h in range(24)}
    for ts, _buy_ex, _sell_ex, profit, _amount in _rows(path, now - days * 86400):
        buckets[time.gmtime(ts + MSK_OFFSET).tm_hour].append(profit)
    return {h: (sum(v) / len(v) if v else None) for h, v in buckets.items()}


def heatmap(path=DB_PATH, days=7, now=None):
    """{(день недели МСК 0=пн..6=вс, час 0..23): максимальный лучший % за `days` дней}, только
    непустые ячейки."""
    now = time.time() if now is None else now
    grid = {}
    for ts, _buy_ex, _sell_ex, profit, _amount in _rows(path, now - days * 86400):
        t = time.gmtime(ts + MSK_OFFSET)
        key = (t.tm_wday, t.tm_hour)
        if key not in grid or profit > grid[key]:
            grid[key] = profit
    return grid


def backtest(min_profit, amount, path=DB_PATH, now=None):
    """Бэктест маршрута по истории спредов за 7 и 30 дней: для каждой пары площадок — сколько раз
    записанный лучший % (уже чистый, с комиссиями) был >= порога `min_profit`, средний и медианный %
    в такие моменты, оценка результата в ₽. Рубли считаются по сумме круга, которая была активна на
    момент самой записи (хранится рядом с процентом — от неё зависел расчёт %); `amount` — только
    запасной вариант для старых записей без сохранённой суммы. Это оценка по прошлым снимкам, а не
    перепрогон маршрута на текущих объявлениях — реальный результат может отличаться. Возвращает
    {7: [...], 30: [...]}, списки словарей отсортированы по числу попаданий (для топ-N); пары без ни
    одного попадания не включаются."""
    now = time.time() if now is None else now
    out = {}
    for days in (7, 30):
        by_pair = {}
        for ts, buy_ex, sell_ex, profit, amt in _rows(path, now - days * 86400):
            by_pair.setdefault((buy_ex, sell_ex), []).append((profit, amt if amt is not None else amount))
        rows = []
        for (buy_ex, sell_ex), pairs in by_pair.items():
            hits = [(p, a) for p, a in pairs if p >= min_profit]
            if not hits:
                continue
            profits = [p for p, _a in hits]
            rows.append({"buy_ex": buy_ex, "sell_ex": sell_ex, "hits": len(hits), "total": len(pairs),
                        "avg": statistics.mean(profits), "median": statistics.median(profits),
                        "est_rub": statistics.mean([p / 100 * a for p, a in hits])})
        rows.sort(key=lambda r: r["hits"], reverse=True)
        out[days] = rows
    return out


def median_vs_bestchange(path=DB_PATH, days=30, now=None):
    """Медиана лучшего % по дням (МСК): связки между площадками P2P против связок, где buy_ex
    или sell_ex — BestChange. Окно 7-30 дней (RETENTION = 30 дней, меньше данных — меньше дней).
    Возвращает (даты по возрастанию "ДД.MM", p2p-медианы, bc-медианы); None — за день нет сделок группы."""
    now = time.time() if now is None else now
    by_day = {}   # "YYYY-MM-DD" -> {"p2p": [...], "bc": [...]}
    for ts, buy_ex, sell_ex, profit, _amount in _rows(path, now - days * 86400):
        t = time.gmtime(ts + MSK_OFFSET)
        day = time.strftime("%Y-%m-%d", t)
        bucket = by_day.setdefault(day, {"p2p": [], "bc": []})
        bucket["bc" if "BestChange" in (buy_ex, sell_ex) else "p2p"].append(profit)
    order = sorted(by_day)
    labels = [time.strftime("%d.%m", time.strptime(d, "%Y-%m-%d")) for d in order]
    p2p = [statistics.median(by_day[d]["p2p"]) if by_day[d]["p2p"] else None for d in order]
    bc = [statistics.median(by_day[d]["bc"]) if by_day[d]["bc"] else None for d in order]
    return labels, p2p, bc
