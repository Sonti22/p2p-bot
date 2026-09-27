"""Бумажный хедж кругов сухого прогона (paper.py) шортом бессрочного фьючерса — только симуляция, ордеров нет.

Круг с BTC/ETH/TON (HEDGE_ASSETS): на старте — виртуальный шорт перпа на объём монеты круга (коэффициент HEDGE_RATIO,
округление до шага лота) на той из двух бирж (Bybit/BingX), где ожидаемая стоимость ниже: исполнение по стакану
(бидам) и тейкер на входе, откуп по аскам и тейкер на выходе, ожидаемый фандинг за время круга. Фандинг —
по фактическим расчётам, попавшим в окно (perp.settle: ставка из последней котировки до расчёта). Закрытие —
когда круг завершён (продажа или срыв) или через HEDGE_MAX_HOURS (6 ч).

HEDGE_PLAN=1 — план круга (planned_pct) считается не с запасом на курс, а с ожидаемой стоимостью хеджа плюс
остаточный запас: курс USDT/RUB шорт не закрывает (HEDGE_RESIDUAL, % круга) и недохеджированная доля
|1 − коэффициент| × запас на курс монеты. Факт круга и виртуальный баланс хедж не меняет: итог — в колонках
hedge_* (USDT), сравнение «с хеджем / без» — в /paper report.

Настройки .env: PAPER_HEDGE (1), HEDGE_PLAN (0), HEDGE_RATIO (1.0), HEDGE_RATIO_BAND (0.1 — допустимое отклонение
коэффициента после округления лота), HEDGE_MAX_HOURS (6), HEDGE_RESIDUAL (0.1), HEDGE_HOLD_MINUTES (40 — сколько
в среднем идёт круг, для оценки фандинга), HEDGE_ASSETS (BTC,ETH,TON).
"""
import json
import logging
import os
import statistics
import time

import paper
import perp

logger = logging.getLogger(__name__)
STALE_CLOSE = 1800   # сек: пора закрывать, а свежей котировки нет дольше — закрываем по последней (с пометкой)


def _on(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def settings():
    """Опечатка в числе — значение по умолчанию (perp.env_float), не исключение: хедж считается при старте круга,
    перед отправкой сигналов."""
    f = perp.env_float
    return {"on": _on("PAPER_HEDGE", "1"), "plan": _on("HEDGE_PLAN", "0"),
            "ratio": f("HEDGE_RATIO", 1.0, lo=0.0), "band": f("HEDGE_RATIO_BAND", 0.1, lo=0.0),
            "max_hours": f("HEDGE_MAX_HOURS", 6, lo=0.0), "residual": f("HEDGE_RESIDUAL", 0.1, lo=0.0),
            "hold_min": f("HEDGE_HOLD_MINUTES", 40, lo=0.0),
            "assets": [a.strip().upper() for a in os.getenv("HEDGE_ASSETS", "BTC,ETH,TON").split(",") if a.strip()]}


def estimate(q, qty, hold_hours, now=None):
    """Ожидаемая стоимость шорта qty на котировке q, USDT (>0 — расход); None — глубины стакана не хватает."""
    open_px, close_px = perp.walk(q.bids, qty), perp.walk(q.asks, qty)
    if open_px is None or close_px is None:
        return None
    fees = q.taker_fee / 100 * (open_px + close_px) * qty
    spread = (close_px - open_px) * qty
    windows = perp.funding_windows(q, q.server_now(now), hold_hours)
    funding = q.funding_rate * q.mark * qty * windows   # ставка > 0 — шорт получает
    return {"open_px": open_px, "close_px": close_px, "fees": fees, "spread": spread, "funding": funding,
            "windows": windows, "cost": fees + spread - funding}


def choose(asset, coin_qty, amount, ref, coin_rub, risk=0.0, now=None):
    """Хедж круга: (план или None, причина, почему без хеджа). coin_qty — монета круга, amount — сумма круга ₽,
    ref — ₽ за USDT (snap.ref; 0 — оценка по цене покупки coin_rub и mid перпа), risk — запас на курс монеты, %."""
    st = settings()
    asset = (asset or "").upper()
    if not st["on"] or not perp.settings()["on"] or asset not in st["assets"] or coin_qty <= 0 or amount <= 0:
        return None, ""   # PERPS=0 — котировок не будет: не хеджируем и не пишем «нет котировки» в каждый круг
    now = time.time() if now is None else now
    plans, notes = [], []
    for venue in perp.VENUES:
        sym = perp.venue_symbol(venue, asset)   # TON: GRAMUSDT там, где он торгуется
        q = perp.quote(venue, sym, now)
        if q is None:
            inst = perp.instrument(venue, sym)
            notes.append(f"{venue}: " + (f"{sym} не торгуется ({inst.note})" if inst and not inst.active
                                         else "нет свежей котировки"))
            continue
        qty = perp.round_lot(coin_qty * st["ratio"], q.lot, q.min_qty)
        ratio = qty / coin_qty
        if not qty or abs(ratio - st["ratio"]) > st["band"] + 1e-9:
            notes.append(f"{venue}: лот {q.lot:g} {asset} — коэффициент {ratio:.2f}")
            continue
        if qty * q.mid < q.min_notional:
            notes.append(f"{venue}: меньше минимума {q.min_notional:g} USDT")
            continue
        e = estimate(q, qty, st["hold_min"] / 60, now)
        if e is None:
            notes.append(f"{venue}: не хватает глубины стакана")
            continue
        rub = ref if ref and ref > 0 else (coin_rub / q.mid if coin_rub and q.mid else 0.0)
        if not rub:
            notes.append(f"{venue}: нет курса ₽/USDT")
            continue
        plans.append(dict(e, venue=venue, symbol=sym, qty=qty, ratio=ratio, coin_qty=coin_qty, ref=rub, mid=q.mid,
                          cost_pct=e["cost"] * rub / amount * 100, taker=q.taker_fee, rate=q.funding_rate,
                          residual_pct=st["residual"] + abs(1 - ratio) * risk))
    if not plans:
        return None, "; ".join(notes)
    plans.sort(key=lambda p: p["cost_pct"])
    best = plans[0]
    best["alt"] = {p["venue"]: round(p["cost_pct"], 4) for p in plans[1:]}
    return best, "; ".join(notes)


def hedged_plan(raw_pct, plan):
    """План круга с хеджем: план без запаса на курс − ожидаемая стоимость хеджа − остаточный запас."""
    return raw_pct - plan["cost_pct"] - plan["residual_pct"]


def for_cycle(asset, coin_qty, amount, ref, coin_rub, raw_pct, plan_pct, risk=0.0):
    """Всё про хедж при старте круга одним вызовом, который не бросает исключений (он стоит перед отправкой
    сигналов): (план хеджа или None, причина без хеджа, план круга — при HEDGE_PLAN=1 с хеджем, строка карточки).
    Сбой — (None, "", plan_pct, "") и запись в лог: круг и сигналы идут как без хеджа."""
    try:
        hedge, note = choose(asset, coin_qty, amount, ref, coin_rub, risk=risk)
        pct = hedged_plan(raw_pct, hedge) if hedge and settings()["plan"] else plan_pct
        return hedge, note, pct, card_line(hedge, note, asset)
    except Exception as e:
        logger.error("simperp: %s: %s", type(e).__name__, e)
        return None, "", plan_pct, ""


def card_line(plan, note, asset):
    """Строка для карточки «🧪 Сухой прогон»; пусто — монета не хеджируется или хедж выключен."""
    if plan:
        alt = "".join(f", {v} {c:.2f}%" for v, c in plan["alt"].items())
        return (f"🛡 хедж (бумага): шорт {plan['qty']:g} {asset} на {plan['venue']} — ожидаемая стоимость "
                f"{plan['cost_pct']:.2f}%{alt}, остаток запаса {plan['residual_pct']:.2f}%")
    return f"🛡 без хеджа: {note}" if note else ""


def _update(con, cycle_id, fields):
    cols = [k for k in fields if k in dict(paper.HEDGE_COLUMNS)]
    con.execute(f"UPDATE cycles SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                [fields[k] for k in cols] + [cycle_id])


def open_hedge(cycle_id, plan, note="", now=None, path=paper.DB_PATH):
    """Записать хедж круга: план из choose() — открытый шорт; без плана с причиной — пометка «без хеджа»."""
    if not cycle_id or (plan is None and not note):
        return
    now = time.time() if now is None else now
    con = paper._connect(path)
    with con:
        if plan is None:
            _update(con, cycle_id, {"hedge_state": json.dumps({"status": "none", "note": note}, ensure_ascii=False)})
        else:
            fund = {}
            perp.settle(fund, perp._quotes.get((plan["venue"], plan["symbol"])), now)   # ближайший расчёт и ставка
            state = {"status": "open", "symbol": plan["symbol"], "ts_open": now, "ratio": plan["ratio"],
                     "mid_open": plan.get("mid"),   # для факта стоимости: спред входа = mid − цена по бидам
                     "coin_qty": plan["coin_qty"], "exp_cost_usdt": plan["cost"], "exp_cost_pct": plan["cost_pct"],
                     "residual_pct": plan["residual_pct"], "ref_open": plan["ref"], "alt": plan["alt"],
                     "fund": fund, "fundings": [], "note": note}
            _update(con, cycle_id, {"hedge_venue": plan["venue"], "hedge_qty": plan["qty"],
                                    "hedge_open": plan["open_px"],
                                    "hedge_fees": plan["taker"] / 100 * plan["open_px"] * plan["qty"],
                                    "hedge_funding": 0.0, "hedge_state": json.dumps(state, ensure_ascii=False)})
    con.close()


def _rows(sql, args=(), path=paper.DB_PATH):
    if not os.path.exists(path):
        return []
    con = paper._connect(path)
    rows = paper._dicts(con.execute(sql, args))
    con.close()
    return rows


def tick(ref=0.0, now=None, path=paper.DB_PATH):
    """Каждый скан: фандинг открытых шортов по наступившим расчётам и закрытие шорта у завершённых кругов
    (или по таймауту HEDGE_MAX_HOURS). Нужна свежая котировка; нет её дольше STALE_CLOSE после срока —
    закрываем по последней известной с пометкой. ref — ₽ за USDT (snap.ref). Возвращает закрытые хеджи."""
    now = time.time() if now is None else now
    max_hours = settings()["max_hours"]
    closed = []
    for c in _rows("SELECT * FROM cycles WHERE hedge_qty > 0 AND hedge_close IS NULL", path=path):
        try:   # одна битая запись не останавливает учёт и закрытие остальных хеджей
            done = _tick_one(c, ref, now, max_hours, path)
        except Exception as e:
            logger.error("simperp: круг #%s: %s: %s", c.get("id"), type(e).__name__, e)
            continue
        if done:
            closed.append(done)
    return closed


def _tick_one(c, ref, now, max_hours, path):
    st = json.loads(c.get("hedge_state") or "{}")
    if st.get("status") != "open":
        return None
    venue, qty = c["hedge_venue"], c["hedge_qty"]
    last = perp._quotes.get((venue, st["symbol"]))
    funding = c.get("hedge_funding") or 0.0
    for ts, rate, mark, approx in perp.settle(st["fund"], last, now):
        amt = rate * mark * qty   # шорт: ставка > 0 — получает, < 0 — платит
        funding += amt
        st["fundings"].append([ts, rate, mark, amt, approx])
    fields = {"hedge_funding": funding}
    reason, done = "", None
    if c.get("result"):
        reason = "круг исполнен" if c["result"] == "done" else f"круг сорван ({c['result']})"
    elif now - st["ts_open"] >= max_hours * 3600:
        reason = f"таймаут {max_hours:g} ч"
    q = None
    if reason:
        st.setdefault("due", now)
        q = perp.quote(venue, st["symbol"], now)
        if q is None and last is not None and now - st["due"] >= STALE_CLOSE:
            q, st["stale"] = last, True
    if q is not None:
        px = perp.walk(q.asks, qty)
        if px is None:   # глубины стакана не хватило — худший уровень с запасом 0.1%
            px, st["thin"] = (q.asks[-1][0] if q.asks else q.ask) * 1.001, True
        fees = (c.get("hedge_fees") or 0.0) + q.taker_fee / 100 * px * qty
        pnl = (c["hedge_open"] - px) * qty - fees + funding
        rub = ref if ref and ref > 0 else st.get("ref_open") or 0.0
        # спред и проскальзывание обеих сторон относительно mid — часть стоимости хеджа (в плане он тоже есть)
        spread = ((st["mid_open"] - c["hedge_open"]) + (px - q.mid)) * qty if st.get("mid_open") else None
        st.update(status="closed", ts_close=now, reason=reason, ref_close=rub, mid_close=q.mid, spread_usdt=spread,
                  pnl_pct=pnl * rub / c["amount"] * 100 if rub and c.get("amount") else None)
        fields.update(hedge_close=px, hedge_fees=fees, hedge_pnl=pnl)
        done = {"id": c["id"], "venue": venue, "pnl": pnl, "pnl_pct": st["pnl_pct"], "reason": reason}
    fields["hedge_state"] = json.dumps(st, ensure_ascii=False)
    con = paper._connect(path)
    with con:
        _update(con, c["id"], fields)
    con.close()
    return done


def fact_cost_pct(row, st):
    """Фактическая стоимость хеджа закрытого круга, % суммы круга — как в плане: комиссии + спред/проскальзывание −
    фандинг (у старых записей спреда нет). row — строка cycles (amount, hedge_fees, hedge_funding), st — hedge_state;
    нет курса ₽/USDT или суммы — None. Её же берёт calibration (/calibration) для сравнения с запасом на курс."""
    rub, amount = st.get("ref_close") or st.get("ref_open") or 0.0, row.get("amount") or 0.0
    if not (rub and amount):
        return None
    fact = (row.get("hedge_fees") or 0.0) + (st.get("spread_usdt") or 0.0) - (row.get("hedge_funding") or 0.0)
    return fact * rub / amount * 100


def report(path=paper.DB_PATH):
    """Сводка хеджа по кругам: сколько открыто/закрыто/без хеджа (и почему), σ(факт − план) по исполненным кругам
    без хеджа и с хеджем, ожидаемая и фактическая стоимость, доля кругов с коэффициентом в полосе."""
    rows = _rows(f"SELECT result, {paper._PLAN_CMP} AS plan, realized_pct, amount, hedge_qty, hedge_fees, "
                 "hedge_funding, hedge_pnl, hedge_state FROM cycles WHERE hedge_state != ''", path=path)
    out = {"open": 0, "closed": 0, "none": {}, "pairs": 0, "sigma_u": None, "sigma_h": None, "exp_cost": None,
           "fact_cost": None, "ratio_ok": None, "fundings": 0}
    diffs_u, diffs_h, exp_cost, fact_cost, ratios = [], [], [], [], []
    band = settings()["band"]
    for r in rows:
        try:
            st = json.loads(r["hedge_state"] or "{}")
        except ValueError:   # битая запись — не повод ломать /paper report
            continue
        status = st.get("status")
        if status == "none":
            key = st.get("note") or "?"
            out["none"][key] = out["none"].get(key, 0) + 1
            continue
        out[status if status in ("open", "closed") else "open"] += 1
        ratios.append(abs(st.get("ratio", 0) - 1) <= band + 1e-9)
        out["fundings"] += len(st.get("fundings") or [])
        if status != "closed" or st.get("pnl_pct") is None:
            continue
        fact = fact_cost_pct(r, st)
        if fact is not None:
            exp_cost.append(st.get("exp_cost_pct") or 0.0)
            fact_cost.append(fact)
        if r["result"] == "done" and r["plan"] is not None and r["realized_pct"] is not None:
            diffs_u.append(r["realized_pct"] - r["plan"])
            diffs_h.append(r["realized_pct"] + st["pnl_pct"] - (r["plan"] - (st.get("exp_cost_pct") or 0.0)))
    out["pairs"] = len(diffs_u)
    if len(diffs_u) >= 2:
        out["sigma_u"], out["sigma_h"] = statistics.pstdev(diffs_u), statistics.pstdev(diffs_h)
    if exp_cost:
        out["exp_cost"], out["fact_cost"] = sum(exp_cost) / len(exp_cost), sum(fact_cost) / len(fact_cost)
    if ratios:
        out["ratio_ok"] = sum(ratios) / len(ratios)
    return out


def report_lines(path=paper.DB_PATH):
    """Строки для /paper report; хеджей не было — пусто."""
    r = report(path)
    if not (r["open"] or r["closed"] or r["none"]):
        return []
    lines = ["", "🛡 <b>Хедж шортом перпа (бумага)</b>: "
             f"закрыто {r['closed']}, открыто {r['open']}, расчётов фандинга {r['fundings']}"]
    if r["sigma_u"] is not None:
        k = r["sigma_h"] / r["sigma_u"] if r["sigma_u"] else None
        lines.append(f"σ(факт − план) по {r['pairs']} исполненным кругам: без хеджа {r['sigma_u']:.2f} п.п., "
                     f"с хеджем {r['sigma_h']:.2f} п.п." + (f" (×{k:.2f})" if k is not None else ""))
    elif r["pairs"]:
        lines.append(f"σ(факт − план): исполненных кругов с хеджем пока {r['pairs']} — мало для сравнения")
    if r["exp_cost"] is not None:
        lines.append(f"стоимость хеджа (комиссии + спред − фандинг): ожидалась {r['exp_cost']:.2f}%, факт "
                     f"{r['fact_cost']:.2f}% суммы круга")
    if r["ratio_ok"] is not None:
        lines.append(f"коэффициент хеджа в полосе 1 ± {settings()['band']:g}: {r['ratio_ok'] * 100:.0f}% кругов")
    for note, n in sorted(r["none"].items(), key=lambda kv: -kv[1])[:3]:
        lines.append(f"без хеджа {n}×: {note}")
    return lines
