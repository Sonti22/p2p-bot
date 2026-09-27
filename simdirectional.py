"""Бумажная направленная стратегия на перпе Bybit — только симуляция, ордеров нет. База data/sim_directional.db.

Одна заранее заданная стратегия (book="ema"): тренд по EMA(20) и EMA(100) на закрытых 1ч свечах. Пересечение вверх —
лонг, вниз — шорт; выход — обратное пересечение или стоп. Стоп обязателен: ATR(14) × DIR_ATR_MULT от входа. Риск
фиксированный: объём = DIR_RISK_USDT / расстояние до стопа (не больше DIR_MAX_NOTIONAL USDT). Позиция одна на все
монеты (DIR_ASSETS, по умолчанию BTC,ETH). Вход только по живой свече — не позже DIR_ENTRY_LAG сек после её закрытия
(на старте прошлые свечи не торгуются: без заглядывания назад).
База сравнения (book="random"): случайные входы — на каждой новой свече с вероятностью DIR_RANDOM_P случайное
направление, те же объём, стоп и комиссии; выход по стопу или через столько часов, сколько в среднем держит
стратегия (пока сделок нет — DIR_RANDOM_HOLD_H). Случайность детерминирована свечой (повторяемо). И «держать» —
изменение цены с первой увиденной свечи.
Исполнение — по стакану (тейкер на вход и выход, ×DIR_FEE_MULT — по умолчанию 2, как в research/directional_bt.py;
/futures показывает и итог при ×1), стоп внутри свечи — по котировке (бид/аск за стопом), пропущенный
(бот не работал) — по high/low закрытой свечи ценой стопа с проскальзыванием DIR_STOP_SLIP %. Фандинг — по
фактическим расчётам (perp.settle): лонг платит ставку, шорт получает.

Настройки .env: SIM_DIRECTIONAL (1), DIR_ASSETS, DIR_RISK_USDT (2), DIR_MAX_NOTIONAL (200), DIR_ATR_MULT (2),
DIR_ENTRY_LAG (900), DIR_RANDOM_P (0.02), DIR_RANDOM_HOLD_H (48), DIR_STOP_SLIP (0.05), DIR_FEE_MULT (2).
"""
import json
import os
import random
import sqlite3
import time

import perp

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "sim_directional.db")
VENUE = "Bybit"
FAST, SLOW, ATR_N = 20, 100, 14
HOUR = 3600
BOOKS = {"ema": "EMA 20/100", "random": "случайные входы"}


def _on(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def settings():
    f = perp.env_float   # опечатка в числе — значение по умолчанию, а не сбой тика и /futures
    return {"on": _on("SIM_DIRECTIONAL", "1"),
            "assets": [x.strip().upper() for x in os.getenv("DIR_ASSETS", "BTC,ETH").split(",") if x.strip()],
            "risk": f("DIR_RISK_USDT", 2), "max_notional": f("DIR_MAX_NOTIONAL", 200), "atr_mult": f("DIR_ATR_MULT", 2),
            "entry_lag": f("DIR_ENTRY_LAG", 900), "random_p": f("DIR_RANDOM_P", 0.02),
            "random_hold_h": f("DIR_RANDOM_HOLD_H", 48), "stop_slip": f("DIR_STOP_SLIP", 0.05),
            "fee_mult": f("DIR_FEE_MULT", 2, lo=0.0)}


# --- индикаторы (чистые функции) ---

def ema(values, n):
    """EMA с затравкой первым значением; список той же длины."""
    out, k = [], 2 / (n + 1)
    for v in values:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def atr(candles, n=ATR_N):
    """ATR по Уайлдеру; candles — [(начало, o, h, l, c)]; список той же длины (первые n−1 — по неполным данным)."""
    out, prev = [], None
    for _, _, h, l, c in candles:
        tr = h - l if prev is None else max(h - l, abs(h - prev), abs(l - prev))
        out.append(tr if not out else out[-1] + (tr - out[-1]) / n)
        prev = c
    return out


def crosses(closes):
    """Для каждой свечи: +1 — EMA(20) пересекла EMA(100) вверх, −1 — вниз, 0 — нет."""
    f, s = ema(closes, FAST), ema(closes, SLOW)
    out = [0]
    for i in range(1, len(closes)):
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            out.append(1)
        elif f[i - 1] >= s[i - 1] and f[i] < s[i]:
            out.append(-1)
        else:
            out.append(0)
    return out


# --- база ---

def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, book TEXT, asset TEXT, "
                "symbol TEXT, side INTEGER, qty REAL, ts_open REAL, px_open REAL, stop REAL, risk REAL, "
                "fees REAL DEFAULT 0, funding REAL DEFAULT 0, ts_close REAL DEFAULT NULL, px_close REAL DEFAULT NULL, "
                "pnl REAL DEFAULT NULL, reason_open TEXT DEFAULT '', reason_close TEXT DEFAULT '', "
                "state TEXT DEFAULT '{}')")
    con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con


def _dicts(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set_state(con, key, value):
    con.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, json.dumps(value)))


# --- сделки ---

def _open(con, book, asset, q, side, atr_value, cfg, now, reason):
    """Вход по стакану: лонг — по аскам, шорт — по бидам. None — объём меньше лота или глубины не хватает."""
    dist = atr_value * cfg["atr_mult"]
    if dist <= 0:
        return None
    ref = q.ask if side > 0 else q.bid
    qty = min(cfg["risk"] / dist, cfg["max_notional"] / ref)
    qty = perp.floor_lot(qty, q.lot, q.min_qty)
    if not qty or qty * ref < (q.min_notional or 0):
        return None
    px = perp.walk(q.asks if side > 0 else q.bids, qty)
    if px is None:
        return None
    fund = {}
    perp.settle(fund, q, now)
    stop = px - side * dist
    mult = cfg["fee_mult"]   # комиссии ×2, как в research/directional_bt.py — сравнение с бэктестом на равных
    cur = con.execute("INSERT INTO positions (book, asset, symbol, side, qty, ts_open, px_open, stop, risk, fees, "
                      "reason_open, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (book, asset, q.symbol, side, qty, now, px, stop, qty * dist, q.taker_fee / 100 * px * qty * mult,
                       reason, json.dumps({"fund": fund, "taker": q.taker_fee, "fee_mult": mult})))
    return cur.lastrowid


def _close(con, p, px, now, reason, taker=None):
    st = json.loads(p["state"] or "{}")
    taker = st.get("taker", 0.055) if taker is None else taker
    fees = (p["fees"] or 0.0) + taker / 100 * px * p["qty"] * st.get("fee_mult", 1.0)   # множитель — со входа
    pnl = (px - p["px_open"]) * p["side"] * p["qty"] - fees + (p["funding"] or 0.0)
    con.execute("UPDATE positions SET ts_close = ?, px_close = ?, fees = ?, pnl = ?, reason_close = ? WHERE id = ?",
                (now, px, fees, pnl, reason, p["id"]))
    return {"id": p["id"], "book": p["book"], "asset": p["asset"], "pnl": pnl, "reason": reason}


def _open_positions(con, book=None, asset=None):
    sql, args = "SELECT * FROM positions WHERE ts_close IS NULL", []
    if book:
        sql, args = sql + " AND book = ?", args + [book]
    if asset:
        sql, args = sql + " AND asset = ?", args + [asset]
    return _dicts(con.execute(sql, args))


def _avg_hold_h(con, default):
    row = con.execute("SELECT AVG(ts_close - ts_open) FROM positions WHERE book = 'ema' AND ts_close IS NOT NULL"
                      ).fetchone()
    return row[0] / HOUR if row and row[0] else default


def _funding(con, p, q_last, now):
    """Наступившие расчёты фандинга позиции: лонг платит ставку × mark × объём, шорт получает."""
    st = json.loads(p["state"] or "{}")
    total = p["funding"] or 0.0
    for _, rate, mark, _ in perp.settle(st.setdefault("fund", {}), q_last, now):
        total -= p["side"] * rate * mark * p["qty"]
    con.execute("UPDATE positions SET funding = ?, state = ? WHERE id = ?", (total, json.dumps(st), p["id"]))
    p["funding"], p["state"] = total, json.dumps(st)


def tick(now=None, path=DB_PATH):
    """После каждого опроса перпов, по времени событий: сначала стопы по high/low новых закрытых свечей (пропущенные,
    пока бот не работал: фандинг — только до свечи стопа, закрытие — её временем), затем фандинг до «сейчас» и стопы
    открытых позиций по котировке, затем выход по обратному пересечению, входы стратегии и случайной базы.
    Возвращает события."""
    cfg = settings()
    out = {"opened": [], "closed": []}
    if not cfg["on"]:
        return out
    now = time.time() if now is None else now
    con = _connect(path)
    with con:
        # монету убрали из DIR_ASSETS, а позиция по ней открыта — ведём её до выхода (стоп, разворот), новых входов нет
        held = [r[0] for r in con.execute("SELECT DISTINCT asset FROM positions WHERE ts_close IS NULL")]
        for asset in cfg["assets"] + [a for a in held if a not in cfg["assets"]]:
            entries = asset in cfg["assets"]
            sym = perp.venue_symbol(VENUE, asset)
            q = perp.quote_for(VENUE, asset, now)
            last = perp.last_for(VENUE, asset)
            hold_h = _avg_hold_h(con, cfg["random_hold_h"])
            candles = perp.klines(VENUE, sym)
            skew = last.skew if last else 0.0
            server_now = now - skew
            # закрытая свеча — только если свечи загружены после её закрытия: иначе в кеше её неполная версия
            # (докачка раз в минуту), а решение по ней не пересматривается
            seen = min(server_now, perp.kline_time(VENUE, sym) or 0.0) - perp.KLINE_GRACE
            closed = [k for k in candles if k[0] + HOUR <= seen]
            key = f"last:{asset}"
            last_ts = _get_state(con, key)
            # стоп, пропущенный между опросами (бот не работал), — по времени, до всего, что случилось позже: фандинг
            # начисляем только до свечи стопа, время закрытия — время этой свечи (по часам бота), а не «сейчас»
            for start, _, high, low, _ in ([] if last_ts is None else [k for k in closed if k[0] > last_ts]):
                for p in _open_positions(con, asset=asset):
                    if p["ts_open"] > start:
                        continue
                    if (p["side"] > 0 and low <= p["stop"]) or (p["side"] < 0 and high >= p["stop"]):
                        t_stop = start + skew
                        _funding(con, p, None, t_stop)
                        px = p["stop"] * (1 - p["side"] * cfg["stop_slip"] / 100)
                        out["closed"].append(_close(con, p, px, t_stop, "стоп (по свече)"))
            for p in _open_positions(con, asset=asset):
                _funding(con, p, last, now)
                if q is None:
                    continue
                hit = q.bid <= p["stop"] if p["side"] > 0 else q.ask >= p["stop"]
                pending = json.loads(p["state"] or "{}").get("exit")   # выход решён, но котировки не было
                timeout = p["book"] == "random" and now - p["ts_open"] >= hold_h * HOUR
                if hit or pending or timeout:
                    px = perp.walk(q.bids if p["side"] > 0 else q.asks, p["qty"])
                    px = px if px is not None else (q.bid if p["side"] > 0 else q.ask)
                    reason = "стоп" if hit else pending or f"срок {hold_h:.0f} ч"
                    out["closed"].append(_close(con, p, px, now, reason, q.taker_fee))
            # невосстановимый разрыв свечей (perp.kline_gap: бот стоял дольше, чем можно догрузить) — явная пауза:
            # часы после last_ts пропущены, по ним не торгуем; ждём непрерывной истории на прогрев EMA и начинаем
            # с последней свечи заново (как на первой встрече), позиции ведём дальше по котировке
            pause = None
            if len(closed) < SLOW + 1:
                if last_ts is None or perp.kline_gap(VENUE, sym) is None:
                    continue
                pause = f"разрыв свечей — ждём {SLOW + 1} ч непрерывной истории"
            elif last_ts is not None and closed[0][0] > last_ts + HOUR:
                pause = "разрыв свечей после простоя — пропущенные часы не торгуем"
            if pause:
                if _get_state(con, f"pause:{asset}") is None:
                    out.setdefault("paused", []).append({"asset": asset, "reason": pause})
                _set_state(con, f"pause:{asset}", pause)
                if len(closed) >= SLOW + 1:
                    _set_state(con, key, closed[-1][0])
                continue
            con.execute("DELETE FROM state WHERE key = ?", (f"pause:{asset}",))
            if last_ts is None:   # первая встреча: прошлое не торгуем, запоминаем старт для «держать»
                _set_state(con, key, closed[-1][0])
                _set_state(con, f"hold:{asset}", closed[-1][4])
                continue
            if closed[-1][0] <= last_ts:
                continue   # новых закрытых свечей нет — индикаторы не пересчитываем
            closes = [k[4] for k in closed]
            cross, atrs = crosses(closes), atr(closed)
            for i, k in enumerate(closed):
                if k[0] <= last_ts:
                    continue
                start = k[0]
                live = i == len(closed) - 1 and server_now - (start + HOUR) <= cfg["entry_lag"] and q is not None
                if cross[i]:
                    for p in _open_positions(con, "ema", asset):
                        if p["side"] == cross[i]:
                            continue
                        px = perp.walk(q.bids if p["side"] > 0 else q.asks, p["qty"]) if q is not None else None
                        if px is not None:
                            out["closed"].append(_close(con, p, px, now, "разворот тренда", q.taker_fee))
                        else:   # котировки нет — закроем на ближайшем опросе с ней
                            st = json.loads(p["state"] or "{}")
                            st["exit"] = "разворот тренда"
                            con.execute("UPDATE positions SET state = ? WHERE id = ?", (json.dumps(st), p["id"]))
                    if live and entries and not _open_positions(con, "ema"):
                        side = "лонг" if cross[i] > 0 else "шорт"
                        pid = _open(con, "ema", asset, q, cross[i], atrs[i], cfg, now,
                                    f"EMA{FAST} {'выше' if cross[i] > 0 else 'ниже'} EMA{SLOW} — {side}")
                        if pid:
                            out["opened"].append({"book": "ema", "asset": asset, "side": cross[i]})
                rng = random.Random(f"{asset}:{int(start)}")
                if live and entries and rng.random() < cfg["random_p"] and not _open_positions(con, "random"):
                    side = rng.choice((1, -1))
                    if _open(con, "random", asset, q, side, atrs[i], cfg, now, "случайный вход"):
                        out["opened"].append({"book": "random", "asset": asset, "side": side})
            _set_state(con, key, closed[-1][0])
    con.close()
    return out


# --- итоги ---

def book_stats(rows):
    """Сделки одной книги → число, доля в плюсе, profit factor, итог, макс. просадка по кривой итогов, средний R."""
    pnls = [r["pnl"] for r in rows]
    wins = [x for x in pnls if x > 0]
    loss = -sum(x for x in pnls if x < 0)
    peak = cum = dd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    rs = [r["pnl"] / r["risk"] for r in rows if r.get("risk")]
    return {"trades": len(pnls), "win_rate": len(wins) / len(pnls) if pnls else None,
            "pf": (sum(wins) / loss if loss else (float("inf") if wins else None)), "pnl": sum(pnls),
            "max_dd": dd, "avg_r": sum(rs) / len(rs) if rs else None}


def at_fees_x1(r):
    """Сделка при комиссиях ×1 (реальный тейкер, без запаса): хранится с множителем DIR_FEE_MULT со входа."""
    mult = json.loads(r.get("state") or "{}").get("fee_mult", 1.0) or 1.0
    return dict(r, pnl=r["pnl"] + (r["fees"] or 0.0) * (1 - 1 / mult))


def stats(path=DB_PATH):
    """(итоги по книгам с комиссиями ×DIR_FEE_MULT, открытые позиции, старт «держать», итоги при комиссиях ×1)."""
    if not os.path.exists(path):
        empty = {b: book_stats([]) for b in BOOKS}
        return empty, [], {}, dict(empty)
    con = _connect(path)
    closed = _dicts(con.execute("SELECT * FROM positions WHERE ts_close IS NOT NULL ORDER BY ts_close"))
    opened = _open_positions(con)
    hold = {k[5:]: json.loads(v) for k, v in con.execute("SELECT key, value FROM state WHERE key LIKE 'hold:%'")}
    con.close()
    return ({b: book_stats([r for r in closed if r["book"] == b]) for b in BOOKS}, opened, hold,
            {b: book_stats([at_fees_x1(r) for r in closed if r["book"] == b]) for b in BOOKS})


def _fmt_book(name, s):
    if not s["trades"]:
        return f"{name}: сделок пока нет"
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    line = (f"{name}: {s['trades']} сделок, в плюсе {s['win_rate'] * 100:.0f}%, PF {pf}, итог {s['pnl']:+.2f} USDT, "
            f"просадка {s['max_dd']:.2f}")
    return line + (f", средний R {s['avg_r']:+.2f}" if s["avg_r"] is not None else "")


def view(path=DB_PATH, now=None):
    """Текст /futures (только владельцу)."""
    now = time.time() if now is None else now
    cfg = settings()
    books, opened, hold, books_x1 = stats(path)
    lines = ["📈 <b>Направленная стратегия — бумага</b> (ордеров нет)",
             f"Статус: {'🟢 включена' if cfg['on'] else '⚪ выключена'} · EMA{FAST}/{SLOW} 1ч Bybit · стоп ATR{ATR_N}"
             f"×{cfg['atr_mult']:g} · риск {cfg['risk']:g} USDT на сделку, до {cfg['max_notional']:g} USDT · "
             f"комиссии тейкера ×{cfg['fee_mult']:g} (как в бэктесте) · {', '.join(cfg['assets'])}", ""]
    for p in opened:
        q = perp.quote_for(VENUE, p["asset"], now)
        upnl = ""
        if q is not None:
            u = ((q.bid if p["side"] > 0 else q.ask) - p["px_open"]) * p["side"] * p["qty"] - p["fees"] + p["funding"]
            upnl = f", сейчас {u:+.2f} USDT"
        lines.append(f"🔄 {BOOKS[p['book']]}: {'лонг' if p['side'] > 0 else 'шорт'} {p['qty']:g} {p['asset']} по "
                     f"{p['px_open']:g}, стоп {p['stop']:g}{upnl}")
    if opened:
        lines.append("")
    lines.append(_fmt_book("Стратегия", books["ema"]))
    lines.append(_fmt_book("Случайные входы", books["random"]))
    e, r = books["ema"], books["random"]
    if e["trades"] and r["trades"]:
        lines.append(f"Стратегия vs случайные: итог {e['pnl'] - r['pnl']:+.2f} USDT")
    e1, r1 = books_x1["ema"], books_x1["random"]
    if e1["trades"] or r1["trades"]:
        lines.append(f"При комиссиях ×1: стратегия {e1['pnl']:+.2f} USDT, случайные {r1['pnl']:+.2f} USDT")
    for asset in cfg["assets"]:
        con = _connect(path)
        pause = _get_state(con, f"pause:{asset}")
        con.close()
        if pause:
            lines.append(f"⏸ {asset}: пауза стратегии — {pause}")
    for asset, start in sorted(hold.items()):
        q = perp.quote_for(VENUE, asset, now)
        if q is not None and start:
            lines.append(f"Держать {asset}: {(q.mid / start - 1) * 100:+.2f}% с начала прогона")
    lines.append("")
    lines.append("Порог «бумага → кнопка»: бэктест ≥ 12 мес. вне выборки, ≥ 200 сделок, PF ≥ 1.2, лучше случайных; "
                 "бумага ≥ 90 дней / 50 сделок.")
    return "\n".join(lines)
