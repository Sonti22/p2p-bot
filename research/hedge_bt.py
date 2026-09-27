"""Ценность хеджа P2P-кругов шортом перпа (только история, без ордеров).

Круг с монетой (BTC/ETH/TON) держит её от покупки за рубли до продажи; сейчас риск курса закрывает запас
`RISK_BUFFER` (BTC 0.3%, ETH 0.5%, TON 0.7%). Хедж — шорт перпа на то же количество на время круга. Считаем:
  • без хеджа — движение спота Bybit за окно удержания (для круга в монете это и есть «факт − план» по курсу);
  • с хеджем — остаток: спот − перп (изменение базиса) + фандинг шорта − комиссии taker ×2 − проскальзывание;
  • стоимость хеджа против запаса (порог плана: ≤ 0.6 × запаса), σ с хеджем против σ без (порог ≤ 0.5);
  • коэффициент хеджа после округления лота (порог 0.9–1.1) для сумм круга 10 000 и 20 000 ₽.
Окна удержания: длительности завершённых кругов из paper.db (медиана), 30/60/120/360 мин. Свечи часовые, поэтому
окна < 60 мин — оценка: движение за час × √(T/60) (броуновское приближение), базис — как за час (оценка сверху).
Стартовые моменты — каждый час последних 365 дней; отдельно — моменты сигналов с этой монетой из history.db.
"""
import bisect
import dataclasses
import math
import os
import sqlite3

from research import metrics

H = 3_600_000
DAY_MS = 24 * H


@dataclasses.dataclass
class Params:
    buffers: dict = dataclasses.field(default_factory=lambda: {"BTC": 0.3, "ETH": 0.5, "TON": 0.7})
    bybit_taker: float = 0.055
    bingx_taker: float = 0.05
    fee_mult: float = 2.0
    slippage: dict = dataclasses.field(default_factory=lambda: {"BTC": 0.02, "ETH": 0.02, "TON": 0.05})
    windows_min: tuple = (30, 60, 120, 360)
    main_window: int = 60
    lookback_days: int = 365
    cost_ratio_max: float = 0.6
    sigma_ratio_max: float = 0.5
    amounts_rub: tuple = (10000, 20000)
    rub_per_usdt: float = 0.0                 # 0 — медиана ref из history.db, иначе 92
    coef_band: tuple = (0.9, 1.1)
    coef_share_min: float = 0.95
    coins: tuple = ("BTC", "ETH", "TON")


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_paper(paths):
    """Круги из копий paper.db (только чтение): ts_start, ts_stage, монеты, итог."""
    rows = []
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        con = _ro(path)
        try:
            for r in con.execute("SELECT ts_start, ts_stage, buy_asset, sell_asset, result FROM cycles"):
                rows.append({"ts_start": r[0], "ts_stage": r[1], "buy_asset": r[2], "sell_asset": r[3],
                             "result": r[4], "db": os.path.basename(path)})
        finally:
            con.close()
    return rows


def load_history(path):
    if not path or not os.path.exists(path):
        return []
    con = _ro(path)
    try:
        return [{"ts": r[0], "asset_buy": r[1], "asset_sell": r[2], "ref": r[3]}
                for r in con.execute("SELECT ts, asset_buy, asset_sell, ref FROM history ORDER BY ts")]
    finally:
        con.close()


def episodes(ts_list, gap_s=600):
    """Склеить метки сигналов (с) в эпизоды: разрыв больше gap_s — новый эпизод. [(начало, конец)]."""
    out = []
    for t in sorted(ts_list):
        if out and t - out[-1][1] <= gap_s:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [tuple(e) for e in out]


class Series:
    """Цена в момент ts (open часовой свечи) и фандинг перпа за (t0, t1] долей цены."""

    def __init__(self, klines):
        self.open = {k[0]: k[1] for k in klines}
        self.close = {k[0]: k[4] for k in klines}
        self.ts = sorted(self.open)

    def at(self, ts):
        p = self.open.get(ts)
        return p if p is not None else self.close.get(ts - H)


def _funding_index(segments):
    rows = sorted((f[0] - f[0] % H, f[1]) for s in segments for f in s["funding"])
    ts = [r[0] for r in rows]
    pref = [0.0]
    for r in rows:
        pref.append(pref[-1] + r[1])
    return ts, pref


def window_samples(spot_klines, perp_segments, since_ms, hours):
    """Окна длиной `hours` ч от каждого часа с since_ms: (движение спота, движение перпа, фандинг шорта)."""
    spot = Series(spot_klines)
    fts, fpref = _funding_index(perp_segments)
    out = []
    for seg in perp_segments:
        perp = Series(seg["klines"])
        for t in perp.ts:
            if t < since_ms:
                continue
            t1 = t + hours * H
            if t1 >= seg["end"]:
                break
            s0, s1, f0, f1 = spot.at(t), spot.at(t1), perp.at(t), perp.at(t1)
            if None in (s0, s1, f0, f1):
                continue
            lo, hi = bisect.bisect_right(fts, t), bisect.bisect_right(fts, t1)
            out.append((t, s1 / s0 - 1, f1 / f0 - 1, fpref[hi] - fpref[lo]))
    return out


def _dist(xs):
    xs = list(xs)
    if not xs:
        return {"n": 0}
    ab = [abs(x) for x in xs]
    return {"n": len(xs), "mean_pct": metrics.fnum(metrics.mean(xs) * 100, 4),
            "sigma_pct": metrics.fnum(metrics.stdev(xs) * 100, 4),
            "p1_pct": metrics.fnum(metrics.percentile(xs, 1) * 100, 4),
            "p5_pct": metrics.fnum(metrics.percentile(xs, 5) * 100, 4),
            "abs_p90_pct": metrics.fnum(metrics.percentile(ab, 90) * 100, 4),
            "abs_p95_pct": metrics.fnum(metrics.percentile(ab, 95) * 100, 4),
            "abs_p99_pct": metrics.fnum(metrics.percentile(ab, 99) * 100, 4),
            "worst_pct": metrics.fnum(min(xs) * 100, 4)}


def evaluate_window(samples_1h, samples_h, minutes, coin, p, taker):
    """Статистика для окна `minutes`: < 60 — по часовым окнам с масштабом √(T/60); иначе — по окнам samples_h."""
    buf = p.buffers.get(coin, 0.5) / 100
    slip = p.slippage.get(coin, 0.05) / 100
    fixed_cost = 2 * taker * p.fee_mult / 100 + 2 * slip
    if minutes < 60:
        k = math.sqrt(minutes / 60)
        rows = [(m * k, m - pm, f * minutes / 60) for _, m, pm, f in samples_1h]
        method = "оценка: часовое движение × √(T/60), базис — как за час"
    else:
        rows = [(m, m - pm, f) for _, m, pm, f in samples_h]
        method = "измерено по часовым свечам"
    if not rows:
        return {"minutes": minutes, "n": 0}
    unhedged = [r[0] for r in rows]
    costs = [fixed_cost - r[2] for r in rows]                 # фандинг шорта: + получили → дешевле
    hedged = [r[1] - c for r, c in zip(rows, costs)]
    su, sh = metrics.stdev(unhedged), metrics.stdev(hedged)
    cost_mean = metrics.mean(costs)
    return {"minutes": minutes, "method": method, "n": len(rows),
            "unhedged": _dist(unhedged), "hedged": _dist(hedged),
            "loss_over_buffer_share": metrics.fnum(sum(u < -buf for u in unhedged) / len(rows), 4),
            "hedged_loss_over_buffer_share": metrics.fnum(sum(h < -buf for h in hedged) / len(rows), 4),
            "cost_mean_pct": metrics.fnum(cost_mean * 100, 4),
            "cost_p95_pct": metrics.fnum(metrics.percentile(costs, 95) * 100, 4),
            "fees_and_slippage_pct": metrics.fnum(fixed_cost * 100, 4),
            "cost_to_buffer": metrics.fnum(cost_mean / buf, 3),
            "sigma_ratio": metrics.fnum(sh / su if su else None, 3)}


def lot_coefficient(amount_rub, rub_per_usdt, price, spec):
    """Коэффициент хеджа после округления лота: шорт / монеты круга. None — меньше минимального номинала."""
    qty = amount_rub / rub_per_usdt / price
    step = spec.get("qty_step") or 0
    hedge = round(qty / step) * step if step else qty
    hedge = max(hedge, spec.get("min_qty") or 0)
    if hedge * price < (spec.get("min_notional") or 0):
        return None
    return hedge / qty


def lot_check(spot_klines, spec, amounts, rub, band, since_ms):
    """Доля дней (цена закрытия дня за период) с коэффициентом в полосе — для каждой суммы круга."""
    closes = [k[4] for k in spot_klines if k[0] >= since_ms and (k[0] // H) % 24 == 23]
    out = {}
    for amt in amounts:
        coefs = [lot_coefficient(amt, rub, px, spec) for px in closes]
        ok = [c for c in coefs if c is not None and band[0] <= c <= band[1]]
        last = coefs[-1] if coefs else None
        out[str(amt)] = {"days": len(coefs), "in_band_share": metrics.fnum(len(ok) / len(coefs) if coefs else 0.0),
                         "last_coef": metrics.fnum(last), "min_coef": metrics.fnum(min((c for c in coefs if c), default=None)),
                         "max_coef": metrics.fnum(max((c for c in coefs if c), default=None))}
    return out


def run(dataset, paper_paths=(), history_path=None, p=None, log=print):
    p = p or Params()
    paper = load_paper(paper_paths)
    hist = load_history(history_path)
    refs = sorted(h["ref"] for h in hist if h.get("ref"))
    rub = p.rub_per_usdt or (metrics.percentile(refs, 50) if refs else 92.0)
    durations = [(c["ts_stage"] - c["ts_start"]) / 60 for c in paper
                 if c["result"] == "done" and c["ts_stage"] and c["ts_start"]]
    paper_median = metrics.percentile(durations, 50) if durations else None
    windows = sorted(set(p.windows_min) | ({max(1, round(paper_median))} if paper_median else set()))
    res = {"params": dataclasses.asdict(p), "rub_per_usdt": metrics.fnum(rub, 2),
           "paper": {"cycles": len(paper), "done": len(durations),
                     "duration_min": {"median": metrics.fnum(paper_median, 1),
                                      "p90": metrics.fnum(metrics.percentile(durations, 90), 1) if durations else None},
                     "note": "длительность — симуляция бумаги (PAPER_PAY_MINUTES + PAPER_TRANSFER_MINUTES), не реальный круг"},
           "windows_min": windows, "coins": {}}
    for coin in p.coins:
        c = dataset["coins"].get(coin)
        coin_paper = [x for x in paper if coin in (x["buy_asset"], x["sell_asset"])]
        sig = [h["ts"] for h in hist if coin in (h["asset_buy"], h["asset_sell"])]
        eps = episodes(sig)
        lifetimes = [(e[1] - e[0]) / 60 for e in eps]
        info = {"paper_cycles": len(coin_paper), "history_signals": len(sig), "history_episodes": len(eps),
                "history_span_days": metrics.fnum((max(sig) - min(sig)) / 86400 if sig else 0.0, 2),
                "episode_lifetime_min": {"median": metrics.fnum(metrics.percentile(lifetimes, 50), 1) if eps else None,
                                         "p90": metrics.fnum(metrics.percentile(lifetimes, 90), 1) if eps else None}}
        if not c or not c["perp"] or not c["spot"]["klines"]:
            res["coins"][coin] = {**info, "ok": False, "reason": "нет рыночной истории"}
            continue
        log(f"хедж {coin}")
        end = max(s["klines"][-1][0] for s in c["perp"] if s["klines"])
        since = end - p.lookback_days * DAY_MS
        s1 = window_samples(c["spot"]["klines"], c["perp"], since, 1)
        samples = {m: s1 if m <= 60 else window_samples(c["spot"]["klines"], c["perp"], since, m // 60)
                   for m in windows}
        by_venue = {venue: [evaluate_window(s1, samples[m], m, coin, p, taker) for m in windows]
                    for venue, taker in (("bybit", p.bybit_taker), ("bingx", p.bingx_taker))}
        cheaper = "bingx" if p.bingx_taker <= p.bybit_taker else "bybit"
        main = next(w for w in by_venue[cheaper] if w["minutes"] == p.main_window)
        x1 = evaluate_window(s1, samples.get(p.main_window, s1), p.main_window, coin,
                             dataclasses.replace(p, fee_mult=1.0), min(p.bybit_taker, p.bingx_taker))
        # моменты сигналов из history.db: окно 60 мин от начала часа эпизода
        sig_hours = {int(e[0] * 1000) // H * H for e in eps}
        s_sig = [s for s in s1 if s[0] in sig_hours]
        sig_eval = evaluate_window(s_sig, s_sig, 60, coin, p, min(p.bybit_taker, p.bingx_taker)) if s_sig else {"n": 0}
        lots = {}
        for venue, segs in (("bybit", c["perp"][-1:]), ("bingx", c["bingx"][-1:])):
            if segs:
                lots[venue] = lot_check(c["spot"]["klines"], segs[0]["spec"], p.amounts_rub, rub, p.coef_band, since)
        best_lot = max((min(v[str(a)]["in_band_share"] for a in p.amounts_rub) for v in lots.values()), default=0.0)
        gates = {
            "cost_le_0.6_buffer": main["cost_to_buffer"] is not None and main["cost_to_buffer"] <= p.cost_ratio_max,
            "sigma_ratio_le_0.5": main["sigma_ratio"] is not None and main["sigma_ratio"] <= p.sigma_ratio_max,
            "lot_coef_in_band_95": best_lot >= p.coef_share_min,
        }
        res["coins"][coin] = {**info, "ok": True, "buffer_pct": p.buffers.get(coin), "cheaper_venue": cheaper,
                              "windows": by_venue, "main": main, "main_fees_x1": x1, "signal_windows": sig_eval,
                              "lots": lots, "gates": gates, "gates_pass": all(gates.values())}
    return res


def summary_ru(res):
    lines = [f"Бумага: кругов {res['paper']['cycles']}, завершённых {res['paper']['done']}, медиана длительности "
             f"{res['paper']['duration_min']['median']} мин (симуляция)."]
    for coin, c in res["coins"].items():
        if not c.get("ok"):
            lines.append(f"{coin}: нет оценки ({c.get('reason')}); кругов в бумаге {c['paper_cycles']}")
            continue
        m = c["main"]
        lines.append(
            f"{coin}: кругов с монетой в бумаге {c['paper_cycles']}, сигналов в history {c['history_signals']} "
            f"({c['history_episodes']} эпизодов за {c['history_span_days']} дн.); окно {m['minutes']} мин, n={m['n']}: "
            f"без хеджа σ {m['unhedged']['sigma_pct']}%, |движение| p95 {m['unhedged']['abs_p95_pct']}%, "
            f"убыток больше запаса {c['buffer_pct']}% в {m['loss_over_buffer_share']:.1%} окон; с хеджем σ "
            f"{m['hedged']['sigma_pct']}%, стоимость {m['cost_mean_pct']}% = {m['cost_to_buffer']} запаса "
            f"(комиссии ×1: {c['main_fees_x1']['cost_mean_pct']}%); пороги: {c['gates']}")
    return lines
