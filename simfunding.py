"""Бумажный арбитраж фандинга — только симуляция на публичных котировках perp.py, ордеров нет. База data/sim_funding.db.

Две схемы на одной монете (FUND_ASSETS, по умолчанию BTC,ETH,TON; символ на площадке — perp.venue_symbol):
  perp_perp — лонг перпа там, где ставка (в час) ниже, шорт — где выше (Bybit ↔ BingX): доход — разница ставок;
  spot_perp — лонг спота Bybit + шорт перпа Bybit: доход — ставка, пока она положительная.
Учёт: фандинг каждой ноги в её собственный расчёт (perp.settle — ставка из последней котировки до расчёта, mark
в этот момент; лонг платит ставку, шорт получает); тейкер обеих ног на входе и выходе, исполнение по стакану;
базис — MTM по стаканам (сколько вышло бы, закрой сейчас, с комиссиями выхода).
Вход (консервативно): годовая доходность ставки (разницы) ≥ FUND_ENTRY_APR, вход+выход окупаются по текущей
ставке за ≤ FUND_PAYBACK_HOURS, разрыв цен ног ≤ FUND_MAX_BASIS %; не больше FUND_MAX_OPEN позиций, одна на
(схему, символ), за тик — одна новая. Выход: после ≥ 1 расчёта доходность ниже FUND_EXIT_APR (или сразу — если
ставка развернулась сильнее −FUND_ENTRY_APR), MTM хуже −FUND_STOP % позиции, срок FUND_MAX_DAYS.

Настройки .env: SIM_FUNDING (1), FUND_ASSETS, FUND_NOTIONAL (1000 USDT на ногу), FUND_ENTRY_APR (20),
FUND_EXIT_APR (5), FUND_PAYBACK_HOURS (48), FUND_MAX_BASIS (0.3), FUND_STOP (1.0), FUND_MAX_DAYS (14),
FUND_MAX_OPEN (2), FUND_SPOT_FEE (0.1 — % тейкера спота Bybit).
"""
import html
import json
import os
import sqlite3
import time

import perp

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "sim_funding.db")
SCHEMES = {"perp_perp": "перп–перп", "spot_perp": "спот+шорт перпа"}


def _on(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def settings():
    f = perp.env_float   # опечатка в числе — значение по умолчанию, а не сбой тика и /funding
    return {"on": _on("SIM_FUNDING", "1"),
            "assets": [x.strip().upper() for x in os.getenv("FUND_ASSETS", "BTC,ETH,TON").split(",") if x.strip()],
            "notional": f("FUND_NOTIONAL", 1000), "entry_apr": f("FUND_ENTRY_APR", 20), "exit_apr": f("FUND_EXIT_APR", 5),
            "payback_h": f("FUND_PAYBACK_HOURS", 48), "max_basis": f("FUND_MAX_BASIS", 0.3), "stop": f("FUND_STOP", 1.0),
            "max_days": f("FUND_MAX_DAYS", 14), "max_open": int(f("FUND_MAX_OPEN", 2)), "spot_fee": f("FUND_SPOT_FEE", 0.1)}


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY AUTOINCREMENT, scheme TEXT, symbol TEXT, "
                "long_venue TEXT, short_venue TEXT, qty REAL, ts_open REAL, long_open REAL, short_open REAL, "
                "fees REAL DEFAULT 0, funding REAL DEFAULT 0, ts_close REAL DEFAULT NULL, long_close REAL DEFAULT NULL, "
                "short_close REAL DEFAULT NULL, pnl REAL DEFAULT NULL, mtm REAL DEFAULT 0, mtm_min REAL DEFAULT 0, "
                "apr_open REAL, reason_open TEXT DEFAULT '', reason_close TEXT DEFAULT '', state TEXT DEFAULT '{}')")
    con.execute("CREATE TABLE IF NOT EXISTS fundings (id INTEGER PRIMARY KEY AUTOINCREMENT, pos_id INTEGER, ts REAL, "
                "venue TEXT, leg TEXT, rate REAL, mark REAL, amount REAL, approx INTEGER DEFAULT 0)")
    con.commit()
    return con


def _dicts(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def apr(rate_per_hour):
    """Ставка в час (доля) → годовых, %."""
    return rate_per_hour * 24 * 365 * 100


def hourly(q):
    return q.funding_rate / (q.interval_h or 8)


def _fee(q, spot_fee):
    return (spot_fee if q.kind == "spot" else q.taker_fee) / 100


def evaluate(scheme, long_q, short_q, cfg=None):
    """Кандидат на вход: ноги (лонг, шорт), объём, доходность, стоимость входа+выхода по стаканам, окупаемость,
    базис. ok — проходит ли фильтры входа; why — почему нет."""
    cfg = cfg or settings()
    if long_q is None or short_q is None:
        return None
    mid = (long_q.mid + short_q.mid) / 2
    lot = max(long_q.lot or 0, short_q.lot or 0)
    qty = perp.floor_lot(cfg["notional"] / mid, lot, max(long_q.min_qty or 0, short_q.min_qty or 0))
    edge_h = hourly(short_q) - (hourly(long_q) if long_q.kind == "perp" else 0.0)
    c = {"scheme": scheme, "symbol": short_q.asset or short_q.symbol, "long": long_q, "short": short_q, "qty": qty,
         "apr": apr(edge_h), "edge_h": edge_h, "basis": (short_q.mid / long_q.mid - 1) * 100, "ok": False, "why": ""}
    if not qty:
        c["why"] = "лот больше позиции"
        return c
    lo, so = perp.walk(long_q.asks, qty), perp.walk(short_q.bids, qty)
    lc, sc = perp.walk(long_q.bids, qty), perp.walk(short_q.asks, qty)
    if None in (lo, so, lc, sc):
        c["why"] = "не хватает глубины стакана"
        return c
    fl, fs = _fee(long_q, cfg["spot_fee"]), _fee(short_q, cfg["spot_fee"])
    fees_open = fl * lo * qty + fs * so * qty
    cost = (lo - lc) * qty + (sc - so) * qty + fees_open + fl * lc * qty + fs * sc * qty
    notional = qty * mid
    c.update(long_open=lo, short_open=so, fees_open=fees_open, cost=cost, cost_pct=cost / notional * 100,
             payback_h=cost / (edge_h * notional) if edge_h > 0 else float("inf"))
    if c["apr"] < cfg["entry_apr"]:
        c["why"] = f"доходность {c['apr']:.1f}% < {cfg['entry_apr']:g}%"
    elif c["payback_h"] > cfg["payback_h"]:
        c["why"] = f"окупаемость {c['payback_h']:.0f} ч > {cfg['payback_h']:g} ч"
    elif abs(c["basis"]) > cfg["max_basis"]:
        c["why"] = f"разрыв цен {c['basis']:+.2f}% > {cfg['max_basis']:g}%"
    else:
        c["ok"] = True
    return c


def candidates(now=None, cfg=None):
    """Все пары ног по монетам (по свежим котировкам): перп–перп — в ту сторону, где разница ставок в час в пользу
    шорта (интервалы площадок могут различаться), и спот Bybit + шорт перпа Bybit."""
    cfg = cfg or settings()
    out = []
    for asset in cfg["assets"]:
        a, b = perp.quote_for("Bybit", asset, now), perp.quote_for("BingX", asset, now)
        if a and b:
            long_q, short_q = (a, b) if hourly(a) <= hourly(b) else (b, a)
            out.append(evaluate("perp_perp", long_q, short_q, cfg))
        spot = perp.spot_for("Bybit", asset, now)
        if spot and a:
            out.append(evaluate("spot_perp", spot, a, cfg))
    return [c for c in out if c]


def _leg_quote(venue, asset, spot=False, fresh=True, now=None):
    """Котировка ноги позиции по монете (symbol в базе — монета бота): свежая или последняя любой давности."""
    if spot:
        return perp.spot_for(venue, asset, now)
    return perp.quote_for(venue, asset, now) if fresh else perp.last_for(venue, asset)


def _open(con, c, now):
    fund = {"short": {}}
    perp.settle(fund["short"], c["short"], now)
    if c["scheme"] == "perp_perp":
        fund["long"] = {}
        perp.settle(fund["long"], c["long"], now)
    con.execute("INSERT INTO positions (scheme, symbol, long_venue, short_venue, qty, ts_open, long_open, short_open, "
                "fees, apr_open, reason_open, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c["scheme"], c["symbol"], c["long"].venue, c["short"].venue, c["qty"], now, c["long_open"],
                 c["short_open"], c["fees_open"], c["apr"],
                 f"доходность {c['apr']:.1f}%, окупаемость {c['payback_h']:.1f} ч, разрыв {c['basis']:+.2f}%",
                 json.dumps({"fund": fund, "settled": 0}, ensure_ascii=False)))


def _exit_values(p, lq, sq, cfg):
    """(цена закрытия лонга, шорта, комиссии выхода) по стаканам; None — глубины не хватает."""
    lc, sc = perp.walk(lq.bids, p["qty"]), perp.walk(sq.asks, p["qty"])
    if lc is None or sc is None:
        return None
    return lc, sc, _fee(lq, cfg["spot_fee"]) * lc * p["qty"] + _fee(sq, cfg["spot_fee"]) * sc * p["qty"]


def tick(now=None, path=DB_PATH):
    """После каждого опроса перпов: фандинг открытых позиций по наступившим расчётам, MTM, выход, затем
    не больше одного входа. Возвращает {"opened": [...], "closed": [...]}."""
    cfg = settings()
    out = {"opened": [], "closed": []}
    if not cfg["on"]:
        return out
    now = time.time() if now is None else now
    con = _connect(path)
    with con:
        for p in _dicts(con.execute("SELECT * FROM positions WHERE ts_close IS NULL")):
            st = json.loads(p["state"] or "{}")
            spot = p["scheme"] == "spot_perp"
            funding = p["funding"] or 0.0
            for leg, venue in (("long", p["long_venue"]), ("short", p["short_venue"])):
                if leg not in st["fund"]:
                    continue   # спот фандинга не платит
                sign = 1 if leg == "short" else -1   # ставка > 0: лонг платит, шорт получает
                for ts, rate, mark, approx in perp.settle(st["fund"][leg], _leg_quote(venue, p["symbol"], fresh=False),
                                                          now):
                    amount = sign * rate * mark * p["qty"]
                    funding += amount
                    st["settled"] = st.get("settled", 0) + 1
                    con.execute("INSERT INTO fundings (pos_id, ts, venue, leg, rate, mark, amount, approx) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (p["id"], ts, venue, leg, rate, mark, amount,
                                                                    int(approx)))
            lq = _leg_quote(p["long_venue"], p["symbol"], spot=spot, now=now)
            sq = _leg_quote(p["short_venue"], p["symbol"], now=now)
            fields = {"funding": funding, "state": json.dumps(st, ensure_ascii=False)}
            ev = _exit_values(p, lq, sq, cfg) if lq and sq else None
            if ev:
                lc, sc, fees_exit = ev
                mtm = (lc - p["long_open"]) * p["qty"] + (p["short_open"] - sc) * p["qty"] + funding - p["fees"] - fees_exit
                fields.update(mtm=mtm, mtm_min=min(p["mtm_min"] or 0.0, mtm))
                notional = p["qty"] * (p["long_open"] + p["short_open"]) / 2
                edge = apr(hourly(sq) - (hourly(lq) if not spot else 0.0))
                reason = ""
                if mtm <= -cfg["stop"] / 100 * notional:
                    reason = f"стоп: MTM {mtm:+.2f} USDT"
                elif now - p["ts_open"] >= cfg["max_days"] * 86400:
                    reason = f"срок {cfg['max_days']:g} дн."
                elif edge < -cfg["entry_apr"] or (st.get("settled", 0) and edge < cfg["exit_apr"]):
                    reason = f"доходность упала до {edge:.1f}%"
                if reason:
                    pnl = (lc - p["long_open"]) * p["qty"] + (p["short_open"] - sc) * p["qty"] + funding \
                        - p["fees"] - fees_exit
                    fields.update(ts_close=now, long_close=lc, short_close=sc, fees=p["fees"] + fees_exit, pnl=pnl,
                                  reason_close=reason)
                    out["closed"].append({"id": p["id"], "scheme": p["scheme"], "symbol": p["symbol"], "pnl": pnl,
                                          "reason": reason})
            con.execute(f"UPDATE positions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        [*fields.values(), p["id"]])
        busy = {(r[0], r[1]) for r in con.execute("SELECT scheme, symbol FROM positions WHERE ts_close IS NULL")}
        if len(busy) < cfg["max_open"]:
            good = [c for c in candidates(now, cfg) if c["ok"] and (c["scheme"], c["symbol"]) not in busy]
            if good:
                best = max(good, key=lambda c: c["apr"])
                _open(con, best, now)
                out["opened"].append({"scheme": best["scheme"], "symbol": best["symbol"], "apr": best["apr"]})
    con.close()
    return out


def stats(path=DB_PATH, now=None):
    """Итоги для /funding: открытые позиции, закрытые (итог, фандинг, комиссии, доходность годовых на позицию),
    худший MTM, число расчётов (оценочных — отдельно)."""
    now = time.time() if now is None else now
    empty = {"open": [], "closed": 0, "wins": 0, "pnl": 0.0, "funding": 0.0, "fees": 0.0, "apr": None,
             "worst_mtm": 0.0, "settlements": 0, "approx": 0, "days": 0.0}
    if not os.path.exists(path):
        return empty
    con = _connect(path)
    rows = _dicts(con.execute("SELECT * FROM positions ORDER BY id"))
    settled, approx = con.execute("SELECT COUNT(*), COALESCE(SUM(approx), 0) FROM fundings").fetchone()
    con.close()
    out = dict(empty, settlements=settled, approx=approx)
    usd_days = 0.0
    for p in rows:
        notional = p["qty"] * (p["long_open"] + p["short_open"]) / 2
        out["worst_mtm"] = min(out["worst_mtm"], p["mtm_min"] or 0.0)
        if p["ts_close"] is None:
            out["open"].append(p)
            continue
        out["closed"] += 1
        out["wins"] += (p["pnl"] or 0) > 0
        out["pnl"] += p["pnl"] or 0.0
        out["funding"] += p["funding"] or 0.0
        out["fees"] += p["fees"] or 0.0
        usd_days += notional * max(p["ts_close"] - p["ts_open"], 1.0) / 86400
    if rows:
        out["days"] = (now - rows[0]["ts_open"]) / 86400
    if usd_days:
        out["apr"] = out["pnl"] / usd_days * 365 * 100
    return out


def view(path=DB_PATH, now=None):
    """Текст /funding (только владельцу)."""
    now = time.time() if now is None else now
    cfg = settings()
    s = stats(path, now)
    lines = ["💱 <b>Арбитраж фандинга — бумага</b> (ордеров нет)",
             f"Статус: {'🟢 включён' if cfg['on'] else '⚪ выключен'} · позиция {cfg['notional']:g} USDT на ногу · "
             f"вход от {cfg['entry_apr']:g}% годовых, окупаемость ≤ {cfg['payback_h']:g} ч, выход ниже "
             f"{cfg['exit_apr']:g}%", ""]
    if s["open"]:
        lines.append("<b>Открыто:</b>")
        for p in s["open"]:
            hours = (now - p["ts_open"]) / 3600
            lines.append(f"• #{p['id']} {SCHEMES.get(p['scheme'], p['scheme'])} {p['symbol']}: лонг {p['long_venue']}, "
                         f"шорт {p['short_venue']}, {hours:.0f} ч, фандинг {p['funding'] or 0:+.2f}, "
                         f"MTM {p['mtm'] or 0:+.2f} USDT (вход при {p['apr_open']:.1f}%)")
    else:
        lines.append("Открытых позиций нет.")
    if s["closed"]:
        line = (f"Закрыто {s['closed']} (в плюсе {s['wins']}): итог {s['pnl']:+.2f} USDT = фандинг "
                f"{s['funding']:+.2f} − комиссии {s['fees']:.2f} ± базис")
        if s["apr"] is not None:
            line += f", {s['apr']:+.1f}% годовых на позицию"
        lines.append(line + f"; при комиссиях ×2, как в бэктесте, — {s['pnl'] - s['fees']:+.2f} USDT")
    lines.append(f"Расчётов фандинга: {s['settlements']}" + (f" (оценочных {s['approx']})" if s["approx"] else "")
                 + f" · худший MTM {s['worst_mtm']:+.2f} USDT · данных {s['days']:.1f} дн.")
    market = candidates(now, cfg)
    if market:
        lines += ["", "<b>Сейчас</b> (годовых по текущей ставке):"]
        for c in sorted(market, key=lambda c: -c["apr"]):
            mark = "✅" if c["ok"] else "·"
            legs = f"лонг {c['long'].venue}{' спот' if c['long'].kind == 'spot' else ''} / шорт {c['short'].venue}"
            lines.append(f"{mark} {c['symbol']} {SCHEMES[c['scheme']]} ({legs}): {c['apr']:+.1f}%"
                         + (f" — {html.escape(c['why'])}" if c["why"] else ""))
    st = perp.status(now)
    if st["errors"] or st["paused"]:
        problems = [*st["errors"], *(f"{v} на паузе" for v in st["paused"])]
        lines.append("⚠️ данные перпов: " + html.escape(", ".join(problems)))
    lines.append("")
    lines.append("Порог перехода «бумага → кнопка»: ≥ 60 дней и 90 расчётов, чистая доходность ≥ earn + 3 п.п., "
                 "просадка ≤ 2%.")
    return "\n".join(lines)
