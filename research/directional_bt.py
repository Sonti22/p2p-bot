"""Бэктест одной заранее заданной направленной стратегии на перпах Bybit (только история, без ордеров).

Стратегия (зафиксирована до прогона, не подбиралась по результату):
  • свечи 1 ч, EMA(20) и EMA(100) по закрытиям; пересечение вверх — лонг, вниз — шорт (вход по open следующей
    свечи, сигнал известен на закрытии предыдущей); противоположное пересечение — выход и разворот;
  • стоп обязателен: k × ATR(14) от цены входа, проверяется внутри каждой свечи по high/low; гэп за стоп —
    исполнение по open; после стопа — вне рынка до следующего пересечения;
  • фиксированный риск: 1% капитала на сделку (номинал = риск / расстояние до стопа), но не больше 2× капитала;
  • комиссия taker 0.055% × 2 (запас) на вход и выход, проскальзывание на каждую сторону, фандинг по истории
    (лонг платит положительную ставку, шорт получает).
Walk-forward: окно 6 мес. in-sample выбирает k из заранее заданной сетки {1.5, 2, 3} по t-статистике сделок
в R, следующие 3 мес. — вне выборки с этим k; окна сдвигаются на 3 мес. Справочно — фиксированный k = 2.
Базы сравнения: случайные входы (та же доля лонгов и то же распределение времени удержания, ≥ 1000 прогонов,
фиксированный seed) и «купить и держать» спот. p-value — доля случайных прогонов со средним результатом
на сделку не хуже стратегии.
"""
import bisect
import dataclasses
import math
import random

from research import metrics

H = 3_600_000
DAY_MS = 24 * H
MONTH_MS = int(30.4375 * DAY_MS)


@dataclasses.dataclass
class Params:
    fast: int = 20
    slow: int = 100
    atr_n: int = 14
    stop_k: float = 2.0                       # справочный фиксированный k
    k_grid: tuple = (1.5, 2.0, 3.0)           # сетка для walk-forward (задана заранее)
    risk: float = 0.01                        # доля капитала под риск на сделку
    lev_cap: float = 2.0
    taker: float = 0.055                      # %, Bybit перп
    fee_mult: float = 2.0
    slippage: dict = dataclasses.field(default_factory=lambda: {"BTC": 0.02, "ETH": 0.02, "TON": 0.05})
    in_months: int = 6
    out_months: int = 3
    min_is_trades: int = 10                   # меньше сделок в in-sample — берём stop_k
    sims: int = 1000
    seed: int = 20260927
    capital: float = 1000.0                   # USDT на монету


def ema(xs, n):
    a = 2.0 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else e + a * (x - e)
        out.append(e)
    return out


def atr(bars, n):
    """ATR Уайлдера; первые n значений — None."""
    out, a, trs = [], None, []
    prev = None
    for b in bars:
        h, lo, c = b[2], b[3], b[4]
        tr = h - lo if prev is None else max(h - lo, abs(h - prev), abs(lo - prev))
        prev = c
        if a is None:
            trs.append(tr)
            if len(trs) == n:
                a = sum(trs) / n
            out.append(a)
        else:
            a = (a * (n - 1) + tr) / n
            out.append(a)
    return out


class Funding:
    """Выплаты отрезка: префиксные суммы rate × цена, чтобы быстро считать фандинг сделки за (t0, t1]."""

    def __init__(self, funding, bars):
        opens = {b[0]: b[1] for b in bars}
        closes = {b[0] + H: b[4] for b in bars}
        rows = []
        for ts, rate in (f[:2] for f in funding):
            ts -= ts % H
            px = opens.get(ts) or closes.get(ts)
            if px:
                rows.append((ts, rate * px))
        rows.sort()
        self.ts = [r[0] for r in rows]
        self.pref = [0.0]
        for r in rows:
            self.pref.append(self.pref[-1] + r[1])

    def cost_frac(self, side, t0, t1, entry_px):
        """Фандинг сделки долей номинала входа: + получили, − заплатили."""
        lo = bisect.bisect_right(self.ts, t0)
        hi = bisect.bisect_right(self.ts, t1)
        return -side * (self.pref[hi] - self.pref[lo]) / entry_px


def _slip(p, coin):
    return p.slippage.get(coin, 0.05) / 100


def simulate(segment, coin, p, k):
    """Сделки стратегии на одном отрезке свечей. Возвращает список словарей со всеми частями результата."""
    bars = segment["klines"]
    fund = Funding(segment.get("funding") or [], bars)
    closes = [b[4] for b in bars]
    ef, es, at = ema(closes, p.fast), ema(closes, p.slow), atr(bars, p.atr_n)
    slip, fee = _slip(p, coin), p.taker * p.fee_mult / 100
    trades, pos, pending = [], None, None

    def close_pos(j, px, reason):
        nonlocal pos
        side = pos["side"]
        fill = px * (1 - side * slip)
        e = pos["fill"]
        gross = side * (fill / e - 1)
        fees = fee * (1 + fill / e)
        fnd = fund.cost_frac(side, bars[pos["i"]][0], bars[j][0], e)
        trades.append({"coin": coin, "seg": segment.get("symbol"), "side": side, "i": pos["i"], "j": j,
                       "entry_ts": bars[pos["i"]][0], "exit_ts": bars[j][0], "entry": e, "exit": fill,
                       "stop_frac": pos["stop_frac"], "gross": gross, "fees": fees, "funding": fnd,
                       "ret": gross - fees + fnd, "reason": reason, "hold_h": max(1, j - pos["i"]), "k": k})
        pos = None

    for i in range(1, len(bars)):
        o, hi, lo = bars[i][1], bars[i][2], bars[i][3]
        if pending is not None:
            want, pending = pending, None
            if pos is not None and pos["side"] != want:
                close_pos(i, o, "cross")
            if pos is None and at[i - 1]:
                fill = o * (1 + want * slip)
                dist = k * at[i - 1]
                pos = {"side": want, "i": i, "fill": fill, "stop": fill - want * dist, "stop_frac": dist / fill}
        if pos is not None:
            st = pos["stop"]
            if pos["side"] == 1 and lo <= st:
                close_pos(i, min(o, st), "stop")
            elif pos["side"] == -1 and hi >= st:
                close_pos(i, max(o, st), "stop")
        if i >= p.slow and i < len(bars) - 1:
            if ef[i] > es[i] and ef[i - 1] <= es[i - 1]:
                pending = 1
            elif ef[i] < es[i] and ef[i - 1] >= es[i - 1]:
                pending = -1
    if pos is not None:
        close_pos(len(bars) - 1, bars[-1][4], "end")
    return trades


def _score(trades):
    """t-статистика среднего результата в R (результат / расстояние до стопа) — критерий выбора k in-sample."""
    rs = [t["ret"] / t["stop_frac"] for t in trades if t["stop_frac"] > 0]
    if len(rs) < 2:
        return -math.inf
    s = metrics.stdev(rs)
    return metrics.mean(rs) / s * math.sqrt(len(rs)) if s > 0 else -math.inf


def walk_forward(segments, coin, p):
    """Сделки вне выборки по окнам 6/3 мес. и выбранные k. segments — отрезки свечей перпа монеты."""
    by_k = {k: sorted((t for s in segments for t in simulate(s, coin, p, k)), key=lambda t: t["entry_ts"])
            for k in sorted(set(p.k_grid) | {p.stop_k})}
    first = min(s["klines"][0][0] for s in segments if s["klines"])
    last = max(s["klines"][-1][0] for s in segments if s["klines"])
    in_len, out_len = p.in_months * MONTH_MS, p.out_months * MONTH_MS
    folds, oos, last_exit = [], [], -1
    start = first
    while start + in_len < last - 7 * DAY_MS:
        is_end = start + in_len
        oos_end = min(is_end + out_len, last + H)
        scores = {}
        for k in p.k_grid:
            is_tr = [t for t in by_k[k] if start <= t["entry_ts"] and t["exit_ts"] < is_end]   # без заглядывания в OOS
            scores[k] = _score(is_tr) if len(is_tr) >= p.min_is_trades else -math.inf
        best = max(p.k_grid, key=lambda k: (scores[k], -abs(k - p.stop_k)))
        if scores[best] == -math.inf:
            best = p.stop_k
        taken = []
        for t in by_k[best]:
            if is_end <= t["entry_ts"] < oos_end and t["entry_ts"] >= last_exit:
                taken.append(t)
                last_exit = t["exit_ts"]
        oos += taken
        folds.append({"is_start": start, "oos_start": is_end, "oos_end": oos_end, "k": best,
                      "is_score": metrics.fnum(scores[best]), "oos_trades": len(taken),
                      "oos_mean_ret_pct": metrics.fnum(metrics.mean(t["ret"] for t in taken) * 100, 4)})
        start += out_len
    window = (folds[0]["oos_start"], folds[-1]["oos_end"]) if folds else None
    fixed = [t for t in by_k[p.stop_k] if window and window[0] <= t["entry_ts"] < window[1]]
    return {"folds": folds, "oos": oos, "fixed": fixed, "window": window}


def equity(trades, segments, p, window):
    """Капитал по часам (закрытия свечей) при фиксированном риске: реализованное + переоценка открытой сделки."""
    closes = {}
    for s in segments:
        for b in s["klines"]:
            if window[0] <= b[0] < window[1]:
                closes[b[0]] = b[4]
    grid = sorted(closes)
    cap, pts, pnls = p.capital, [], []
    by_entry = sorted(trades, key=lambda t: t["entry_ts"])
    ti, cur, notional = 0, None, 0.0
    for ts in grid:
        while True:   # выход и новый вход в одной свече (разворот, стоп в свече входа) — по порядку
            if cur is not None and cur["exit_ts"] <= ts:
                pnl = notional * cur["ret"]
                cap += pnl
                pnls.append(pnl)
                cur = None
            elif cur is None and ti < len(by_entry) and by_entry[ti]["entry_ts"] <= ts:
                cur = by_entry[ti]
                ti += 1
                notional = min(p.risk * cap / cur["stop_frac"], p.lev_cap * cap) if cur["stop_frac"] > 0 else 0.0
            else:
                break
        unreal = 0.0
        if cur is not None:
            fee_in = p.taker * p.fee_mult / 100
            unreal = notional * (cur["side"] * (closes[ts] / cur["entry"] - 1) - fee_in)
        pts.append((ts + H, cap + unreal))
    if cur is not None:
        pnl = notional * cur["ret"]
        cap += pnl
        pnls.append(pnl)
        pts.append((cur["exit_ts"] + H, cap))
    return pts, pnls


def _stats(trades, pts, pnls, capital, window):
    years = max((window[1] - window[0]) / (365 * DAY_MS), 1e-9)
    st = metrics.equity_stats(pts, capital)
    final = pts[-1][1] if pts else capital
    rets = [t["ret"] for t in trades]
    return {"trades": len(trades), "months": round((window[1] - window[0]) / MONTH_MS, 1),
            "win_rate": metrics.fnum(sum(r > 0 for r in rets) / len(rets) if rets else 0.0),
            "mean_ret_pct": metrics.fnum(metrics.mean(rets) * 100, 4),
            "profit_factor": metrics.fnum(metrics.profit_factor(pnls)),
            "sharpe": st["sharpe"], "max_dd_pct": st["max_dd_pct"], "worst_day_pct": st["worst_day_pct"],
            "total_return_pct": metrics.fnum((final / capital - 1) * 100, 2),
            "cagr_pct": metrics.fnum(((final / capital) ** (1 / years) - 1) * 100 if final > 0 else -100.0, 2),
            "avg_hold_h": metrics.fnum(metrics.mean(t["hold_h"] for t in trades), 1),
            "long_share": metrics.fnum(sum(t["side"] == 1 for t in trades) / len(trades) if trades else 0.0),
            "stops_share": metrics.fnum(sum(t["reason"] == "stop" for t in trades) / len(trades) if trades else 0.0),
            "fees_pct_sum": metrics.fnum(sum(t["fees"] for t in trades) * 100, 2),
            "funding_pct_sum": metrics.fnum(sum(t["funding"] for t in trades) * 100, 2)}


class RandomPool:
    """Случайные сделки монеты внутри окна вне выборки: вход по open случайной свечи, выход через h свечей."""

    def __init__(self, segments, coin, p, window):
        self.segs = []
        self.cands = []
        slip, fee = _slip(p, coin), p.taker * p.fee_mult / 100
        self.slip, self.fee = slip, fee
        for si, s in enumerate(segments):
            bars = s["klines"]
            self.segs.append((bars, Funding(s.get("funding") or [], bars)))
            for i, b in enumerate(bars):
                if window[0] <= b[0] < window[1]:
                    self.cands.append((si, i))

    def trade_ret(self, rng, h, side):
        for _ in range(50):
            si, i = self.cands[rng.randrange(len(self.cands))]
            bars, fund = self.segs[si]
            j = i + h
            if j < len(bars):
                e = bars[i][1] * (1 + side * self.slip)
                x = bars[j][1] * (1 - side * self.slip)
                return side * (x / e - 1) - self.fee * (1 + x / e) + fund.cost_frac(side, bars[i][0], bars[j][0], e)
        return None


def random_baseline(pools, trades_by_coin, p):
    """p-value стратегии против случайных входов (одна статистика: средний результат на сделку, доля номинала)."""
    rng = random.Random(p.seed)
    strat = [t["ret"] for tr in trades_by_coin.values() for t in tr]
    if not strat:
        return {"sims": 0}
    target = metrics.mean(strat)
    per_coin_target = {c: metrics.mean(t["ret"] for t in tr) for c, tr in trades_by_coin.items() if tr}
    sims, per_coin_ge = [], {c: 0 for c in per_coin_target}
    for _ in range(p.sims):
        allr = []
        for coin, tr in trades_by_coin.items():
            if not tr:
                continue
            holds = [t["hold_h"] for t in tr]
            p_long = sum(t["side"] == 1 for t in tr) / len(tr)
            rs = []
            for _t in tr:
                side = 1 if rng.random() < p_long else -1
                r = pools[coin].trade_ret(rng, holds[rng.randrange(len(holds))], side)
                if r is not None:
                    rs.append(r)
            if rs and metrics.mean(rs) >= per_coin_target[coin]:
                per_coin_ge[coin] += 1
            allr += rs
        sims.append(metrics.mean(allr))
    ge = sum(s >= target for s in sims)
    return {"sims": p.sims, "seed": p.seed, "statistic": "средний результат на сделку, % номинала",
            "strategy_mean_pct": metrics.fnum(target * 100, 4),
            "random_mean_pct": metrics.fnum(metrics.mean(sims) * 100, 4),
            "random_p95_pct": metrics.fnum(metrics.percentile(sims, 95) * 100, 4),
            "p_value": metrics.fnum((1 + ge) / (p.sims + 1), 4),
            "p_value_by_coin": {c: metrics.fnum((1 + n) / (p.sims + 1), 4) for c, n in per_coin_ge.items()}}


def buy_and_hold(spot_klines, window, capital, fee_frac):
    pts = [(b[0] + H, b[4]) for b in spot_klines if window[0] <= b[0] < window[1]]
    if len(pts) < 2:
        return {"ok": False}
    base = pts[0][1]
    eq = [(ts, capital * (px / base) * (1 - fee_frac) ** 2) for ts, px in pts]
    st = metrics.equity_stats(eq, capital)
    years = max((window[1] - window[0]) / (365 * DAY_MS), 1e-9)
    final = eq[-1][1]
    return {"ok": True, "total_return_pct": metrics.fnum((final / capital - 1) * 100, 2),
            "cagr_pct": metrics.fnum(((final / capital) ** (1 / years) - 1) * 100, 2),
            "max_dd_pct": st["max_dd_pct"], "sharpe": st["sharpe"], "points": eq}


def _combine(curves, capital_each):
    """Сумма рядов капитала монет (у каждой свой капитал; до начала/после конца ряда — крайнее значение)."""
    all_ts = sorted({ts for c in curves for ts, _ in c})
    out = []
    idx = [0] * len(curves)
    lastv = [capital_each] * len(curves)
    for ts in all_ts:
        for n, c in enumerate(curves):
            while idx[n] < len(c) and c[idx[n]][0] <= ts:
                lastv[n] = c[idx[n]][1]
                idx[n] += 1
        out.append((ts, sum(lastv)))
    return out


def run(dataset, p=None, log=print, with_random=True):
    """Все монеты: walk-forward вне выборки, фикс. k, случайные входы, «купить и держать»; справочно — комиссии ×1."""
    p = p or Params()
    fee_frac = p.taker * p.fee_mult / 100
    res = {"params": dataclasses.asdict(p), "coins": {}, "portfolio": {}}
    oos_by_coin, fixed_by_coin, pools = {}, {}, {}
    curves, curves_fixed, bh_curves, all_pnls, all_pnls_fixed = [], [], [], [], []
    windows = []
    for coin, c in dataset["coins"].items():
        segs = [s for s in c["perp"] if len(s["klines"]) > p.slow * 2]
        if not segs:
            continue
        log(f"направленная {coin}")
        wf = walk_forward(segs, coin, p)
        if not wf["window"]:
            res["coins"][coin] = {"ok": False, "reason": "мало истории для walk-forward"}
            continue
        win = wf["window"]
        windows.append(win)
        pts, pnls = equity(wf["oos"], segs, p, win)
        ptsf, pnlsf = equity(wf["fixed"], segs, p, win)
        bh = buy_and_hold(c["spot"]["klines"], win, p.capital, fee_frac)
        res["coins"][coin] = {
            "ok": True, "window": {"start": win[0], "end": win[1]},
            "walk_forward": _stats(wf["oos"], pts, pnls, p.capital, win),
            "fixed_k": _stats(wf["fixed"], ptsf, pnlsf, p.capital, win),
            "k_chosen": [f["k"] for f in wf["folds"]], "folds": wf["folds"],
            "buy_and_hold": {k: v for k, v in bh.items() if k != "points"},
        }
        oos_by_coin[coin] = wf["oos"]
        fixed_by_coin[coin] = wf["fixed"]
        pools[coin] = RandomPool(segs, coin, p, win)
        curves.append(pts)
        curves_fixed.append(ptsf)
        all_pnls += pnls
        all_pnls_fixed += pnlsf
        if bh.get("ok"):
            bh_curves.append(bh["points"])
    if not curves:
        return res
    if with_random:
        log("случайные входы")
        res["random"] = random_baseline(pools, oos_by_coin, p)
    win = (min(w[0] for w in windows), max(w[1] for w in windows))
    capital = p.capital * len(curves)
    port = _combine(curves, p.capital)
    all_tr = [t for tr in oos_by_coin.values() for t in tr]
    res["portfolio"] = _stats(all_tr, port, all_pnls, capital, win)
    res["portfolio"]["window"] = {"start": win[0], "end": win[1]}
    fixed_tr = [t for tr in fixed_by_coin.values() for t in tr]
    res["portfolio_fixed_k"] = _stats(fixed_tr, _combine(curves_fixed, p.capital), all_pnls_fixed, capital, win)
    if bh_curves:
        bh = _combine(bh_curves, p.capital)
        st = metrics.equity_stats(bh, capital)
        res["buy_and_hold_portfolio"] = {"total_return_pct": metrics.fnum((bh[-1][1] / capital - 1) * 100, 2),
                                         "max_dd_pct": st["max_dd_pct"], "sharpe": st["sharpe"]}
    if with_random:
        x1 = run(dataset, dataclasses.replace(p, fee_mult=1.0), log=lambda *a: None, with_random=False)
        res["fees_x1_portfolio"] = {k: x1["portfolio"].get(k) for k in
                                    ("total_return_pct", "sharpe", "profit_factor", "max_dd_pct", "mean_ret_pct")}
    return res


def summary_ru(res):
    lines = []
    for coin, c in res.get("coins", {}).items():
        if not c.get("ok"):
            lines.append(f"{coin}: нет оценки ({c.get('reason')})")
            continue
        w, f, b = c["walk_forward"], c["fixed_k"], c["buy_and_hold"]
        lines.append(f"{coin}: вне выборки {w['months']} мес., сделок {w['trades']}, Sharpe {w['sharpe']}, "
                     f"PF {w['profit_factor']}, просадка {w['max_dd_pct']}%, итог {w['total_return_pct']}% "
                     f"(фикс. k=2: {f['total_return_pct']}%, купить и держать: {b.get('total_return_pct')}%)")
    pf = res.get("portfolio")
    if pf:
        r = res.get("random", {})
        lines.append(f"Портфель: сделок {pf['trades']}, Sharpe {pf['sharpe']}, PF {pf['profit_factor']}, "
                     f"просадка {pf['max_dd_pct']}%, итог {pf['total_return_pct']}%; "
                     f"против случайных входов p = {r.get('p_value')} "
                     f"(стратегия {r.get('strategy_mean_pct')}% на сделку, случайные {r.get('random_mean_pct')}%)")
    return lines
