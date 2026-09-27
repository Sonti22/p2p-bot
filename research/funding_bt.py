"""Бэктест арбитража фандинга (только история, без ордеров): (а) спот Bybit в лонг + шорт перпа Bybit,
(б) перп–перп Bybit против BingX.

Правила заданы заранее и не подбирались по результату:
  • сигнал — сумма ставок фандинга за последние 72 ч, в годовых на номинал (только уже выплаченные ставки);
  • вход, если сигнал ≥ 15% годовых (для «спот+перп» ещё и последняя ставка > 0); выход, если < 3%;
  • решения — только в моменты выплат, после выплаты: открылись в t — первая выплата в следующий раз;
  • плечо перпа 2×, номинал 1000 USDT на ногу; капитал «спот+перп» = 1000 + 500, «перп–перп» = 500 + 500;
  • комиссии taker на обе ноги при входе и выходе × 2 (запас), проскальзывание на каждую ногу и сторону;
  • цена ушла на ±25% от последней ребалансировки — ноги закрываются и открываются заново (полная стоимость,
    для перп–перп ещё перевод 1 USDT между биржами): так моделируем запас маржи без ликвидаций;
  • отрезок перпа закончился (поставка TONUSDT) — принудительное закрытие;
  • переоценка базиса каждый час (разница цен ног), просадка и худший день — по этой переоценке.
Цены: open часовой свечи в момент t (≈ цена в момент t). У BingX до 2025 г. свечей нет — берём markPrice
из истории фандинга (только в моменты выплат).
"""
import bisect
import dataclasses

from research import metrics

H = 3_600_000
YEAR_MS = 365 * 24 * H


@dataclasses.dataclass
class Params:
    entry_apr: float = 15.0          # % годовых на номинал по фандингу за окно
    exit_apr: float = 3.0
    lookback_h: int = 72
    leverage: float = 2.0
    notional: float = 1000.0         # USDT на ногу
    spot_taker: float = 0.10         # %, Bybit спот
    bybit_taker: float = 0.055       # %, Bybit перп
    bingx_taker: float = 0.05        # %, BingX перп
    fee_mult: float = 2.0            # запас на комиссии
    slippage: dict = dataclasses.field(default_factory=lambda: {"BTC": 0.02, "ETH": 0.02, "TON": 0.05})
    rebalance_move: float = 25.0     # %
    transfer_fee: float = 1.0        # USDT за перевод между биржами при ребалансировке перп–перп
    earn_apr: float = 5.0            # % годовых, база сравнения: USDT в earn
    always_on: bool = False          # справочно: держать позицию всегда (без правил входа/выхода)


class Book:
    """Цена в момент ts: open часовой свечи, начавшейся в ts; иначе close предыдущей; иначе запасные точки."""

    def __init__(self, klines, extra=None):
        self.open = {k[0]: k[1] for k in klines}
        self.close = {k[0]: k[4] for k in klines}
        self.extra = extra or {}

    def at(self, ts):
        p = self.open.get(ts)
        if p is None:
            p = self.close.get(ts - H)
        if p is None:
            p = self.extra.get(ts)
        return p


class Trailing:
    """Сумма ставок за окно (ts − L, ts] по отсортированным выплатам — через префиксные суммы."""

    def __init__(self, funding):
        self.ts = [f[0] for f in funding]
        self.pref = [0.0]
        for f in funding:
            self.pref.append(self.pref[-1] + f[1])

    def sum(self, ts, lookback_ms):
        hi = bisect.bisect_right(self.ts, ts)
        lo = bisect.bisect_right(self.ts, ts - lookback_ms)
        return self.pref[hi] - self.pref[lo]


def _apr(rate_sum, lookback_h):
    return rate_sum * (365 * 24 / lookback_h) * 100


def _hour(ts):
    return ts - ts % H


def _funding(rows):
    """Ставки по часу выплаты: {ts: rate}; метки округляем вниз до часа (выплата в hh:00:00.xxx)."""
    return {_hour(r[0]): r[1] for r in rows}


class Ledger:
    """Учёт: реализованное (по статьям), число выплат, входов, ребалансировок, часы в позиции, ряд капитала."""

    def __init__(self, capital):
        self.capital = capital
        self.items = {"funding": 0.0, "fees": 0.0, "slippage": 0.0, "basis": 0.0, "transfer": 0.0}
        self.settlements = 0
        self.entries = 0
        self.rebalances = 0
        self.forced = 0
        self.hours_in = 0
        self.points = []
        self.first_eval = None
        self.last_ts = None
        self.funding_by_ts = []      # (ts, начисление) — для отчёта о выплатах

    @property
    def realized(self):
        return sum(self.items.values())

    def mark(self, ts, mtm):
        self.points.append((ts, self.capital + self.realized + mtm))
        self.last_ts = ts


def _costs(notional_legs, p, coin):
    """Комиссии и проскальзывание одной стороны сделки по ногам [(номинал, taker %)]."""
    slip = p.slippage.get(coin, 0.05)
    fees = sum(n * t * p.fee_mult for n, t in notional_legs) / 100
    sl = sum(n * slip for n, _ in notional_legs) / 100
    return fees, sl


def _summary(led, p, kind, coin, extra=None):
    if led.first_eval is None or led.last_ts is None:
        return {"kind": kind, "coin": coin, "ok": False, "reason": "нет данных для оценки"}
    years = max((led.last_ts - led.first_eval) / YEAR_MS, 1e-9)
    net = led.realized
    apr = net / led.capital / years * 100
    st = metrics.equity_stats(led.points, led.capital)
    res = {
        "kind": kind, "coin": coin, "ok": True,
        "period": {"start": led.first_eval, "end": led.last_ts, "years": round(years, 3)},
        "capital_usdt": led.capital, "notional_usdt": p.notional,
        "net_pnl_usdt": round(net, 2), "net_apr_pct": round(apr, 2),
        "items_usdt": {k: round(v, 2) for k, v in led.items.items()},
        "settlements_in_position": led.settlements, "entries": led.entries, "rebalances": led.rebalances,
        "forced_closes": led.forced,
        "days_in_position": round(led.hours_in / 24, 1),
        "exposure_pct": round(led.hours_in * H / max(led.last_ts - led.first_eval, 1) * 100, 1),
        "max_dd_pct": st["max_dd_pct"], "worst_day_pct": st["worst_day_pct"], "sharpe": st["sharpe"],
        "earn_apr_pct": p.earn_apr, "excess_vs_earn_pp": round(apr - p.earn_apr, 2),
    }
    if extra:
        res.update(extra)
    return res


def spot_perp(coin, spot_klines, perp_segments, p=None):
    """(а) Спот Bybit в лонг + шорт перпа Bybit на то же количество монет."""
    p = p or Params()
    spot = Book(spot_klines)
    led = Ledger(p.notional * (1 + 1 / p.leverage))
    look = p.lookback_h * H
    pos = None

    def open_(ts, s, f):
        nonlocal pos
        q = p.notional / s
        fees, sl = _costs([(q * s, p.spot_taker), (q * f, p.bybit_taker)], p, coin)
        led.items["fees"] -= fees
        led.items["slippage"] -= sl
        pos = {"q": q, "s0": s, "f0": f, "anchor": f, "ts": ts}

    def close(s, f):
        nonlocal pos
        q = pos["q"]
        led.items["basis"] += q * ((s - pos["s0"]) - (f - pos["f0"]))
        fees, sl = _costs([(q * s, p.spot_taker), (q * f, p.bybit_taker)], p, coin)
        led.items["fees"] -= fees
        led.items["slippage"] -= sl
        pos = None

    for seg in perp_segments:
        perp = Book(seg["klines"])
        fund = _funding(seg["funding"])
        trail = Trailing(sorted([ts, r] for ts, r in fund.items()))
        eval_from = seg["start"] + look
        last = None
        for k in seg["klines"]:
            ts = k[0]
            s, f = spot.at(ts), perp.at(ts)
            if s is None or f is None:
                continue
            last = (ts, s, f)
            if pos is not None:
                led.hours_in += 1
            if ts in fund and ts >= eval_from:
                if led.first_eval is None:
                    led.first_eval = ts
                rate = fund[ts]
                if pos is not None:
                    got = pos["q"] * f * rate       # шорт получает при положительной ставке
                    led.items["funding"] += got
                    led.settlements += 1
                    led.funding_by_ts.append((ts, got))
                apr = _apr(trail.sum(ts, look), p.lookback_h)
                if pos is not None:
                    if abs(f / pos["anchor"] - 1) * 100 >= p.rebalance_move:
                        close(s, f)
                        open_(ts, s, f)
                        led.rebalances += 1
                    elif not p.always_on and apr < p.exit_apr:
                        close(s, f)
                elif p.always_on or (apr >= p.entry_apr and rate > 0):
                    open_(ts, s, f)
                    led.entries += 1
            mtm = pos["q"] * ((s - pos["s0"]) - (f - pos["f0"])) if pos else 0.0
            if led.first_eval is not None:
                led.mark(ts, mtm)
        if pos is not None and last:
            close(last[1], last[2])
            led.forced += 1
            led.mark(last[0], 0.0)
    return _summary(led, p, "spot_perp", coin, {"venues": "Bybit спот + Bybit перп",
                                                "segments": [s["symbol"] for s in perp_segments]})


def perp_perp(coin, bybit_segments, bingx_segments, p=None):
    """(б) Шорт перпа на бирже с большим фандингом, лонг — на другой (Bybit против BingX)."""
    p = p or Params()
    led = Ledger(2 * p.notional / p.leverage)
    look = p.lookback_h * H
    pos = None
    overlaps = []

    def costs(qa, qb, pa, pb):
        fees, sl = _costs([(qa * pa, p.bybit_taker), (qb * pb, p.bingx_taker)], p, coin)
        led.items["fees"] -= fees
        led.items["slippage"] -= sl

    def open_(ts, d, pa, pb):
        nonlocal pos
        q = p.notional / pa
        costs(q, q, pa, pb)
        pos = {"d": d, "q": q, "a0": pa, "b0": pb, "anchor": pa, "ts": ts}

    def mtm(pa, pb):
        return pos["d"] * pos["q"] * ((pos["a0"] - pa) + (pb - pos["b0"]))

    def close(pa, pb):
        nonlocal pos
        led.items["basis"] += mtm(pa, pb)
        costs(pos["q"], pos["q"], pa, pb)
        pos = None

    for a in bybit_segments:
        for b in bingx_segments:
            start, end = max(a["start"], b["start"]), min(a["end"], b["end"])
            if end - start <= look:
                continue
            overlaps.append({"bybit": a["symbol"], "bingx": b["symbol"], "start": start, "end": end})
            fa, fb = _funding(a["funding"]), _funding(b["funding"])
            ta = Trailing(sorted([ts, r] for ts, r in fa.items()))
            tb = Trailing(sorted([ts, r] for ts, r in fb.items()))
            book_a = Book(a["klines"])
            book_b = Book(b["klines"], extra={_hour(r[0]): r[2] for r in b["funding"] if len(r) > 2 and r[2] > 0})
            eval_from = start + look
            last_pa = last_pb = None
            last = None
            for ts in range(_hour(start + H - 1), end, H):
                pa, pb = book_a.at(ts), book_b.at(ts)
                last_pa = pa or last_pa
                last_pb = pb or last_pb
                if pos is not None:
                    led.hours_in += 1
                    if ts in fa and last_pa:
                        got = pos["d"] * pos["q"] * last_pa * fa[ts]
                        led.items["funding"] += got
                        led.settlements += 1
                        led.funding_by_ts.append((ts, got))
                    if ts in fb and last_pb:
                        got = -pos["d"] * pos["q"] * last_pb * fb[ts]
                        led.items["funding"] += got
                        led.settlements += 1
                        led.funding_by_ts.append((ts, got))
                if pa is None or pb is None:
                    continue
                last = (ts, pa, pb)
                if (ts in fa or ts in fb) and ts >= eval_from:
                    if led.first_eval is None:
                        led.first_eval = ts
                    diff = _apr(ta.sum(ts, look) - tb.sum(ts, look), p.lookback_h)
                    if pos is not None:
                        if abs(pa / pos["anchor"] - 1) * 100 >= p.rebalance_move:
                            d = pos["d"]
                            close(pa, pb)
                            open_(ts, d, pa, pb)
                            led.items["transfer"] -= p.transfer_fee
                            led.rebalances += 1
                        elif not p.always_on and pos["d"] * diff < p.exit_apr:
                            close(pa, pb)
                    elif p.always_on or abs(diff) >= p.entry_apr:
                        open_(ts, 1 if diff >= 0 else -1, pa, pb)
                        led.entries += 1
                if led.first_eval is not None:
                    led.mark(ts, mtm(pa, pb) if pos else 0.0)
            if pos is not None and last:
                close(last[1], last[2])
                led.forced += 1
                led.mark(last[0], 0.0)
    return _summary(led, p, "perp_perp", coin, {"venues": "Bybit перп ↔ BingX перп", "overlaps": overlaps})


def funding_profile(perp_segments, since_ms=None):
    """Справочно: средний фандинг (годовых на номинал) и доля положительных выплат по всей истории."""
    rates, first, last = [], None, None
    for seg in perp_segments:
        for ts, r in (x[:2] for x in seg["funding"]):
            if since_ms is None or ts >= since_ms:
                rates.append(r)
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
    if not rates or last == first:
        return {"n": len(rates)}
    years = (last - first) / YEAR_MS
    return {"n": len(rates), "apr_pct": round(sum(rates) / years * 100, 2),
            "positive_share": round(sum(r > 0 for r in rates) / len(rates), 3)}


def since(segments, start_ms):
    """Отрезки, обрезанные слева по start_ms (для оценки последних 12 месяцев); пустые отбрасываются."""
    out = []
    for s in segments:
        if s["end"] <= start_ms:
            continue
        cut = dict(s, start=max(s["start"], start_ms),
                   klines=[k for k in s["klines"] if k[0] >= start_ms],
                   funding=[f for f in s["funding"] if f[0] >= start_ms])
        if cut["klines"]:
            out.append(cut)
    return out


def _last12(r):
    keys = ("net_apr_pct", "excess_vs_earn_pp", "max_dd_pct", "worst_day_pct", "entries", "settlements_in_position")
    return {k: r.get(k) for k in keys} if r.get("ok") else {"ok": False, "reason": r.get("reason")}


def run(dataset, p=None, log=print):
    """Все варианты по всем монетам: основные правила (комиссии ×2); справочно — комиссии ×1, «всегда в позиции»
    и те же правила только на последних 12 месяцах (смена режима рынка)."""
    p = p or Params()
    p1 = dataclasses.replace(p, fee_mult=1.0)
    pon = dataclasses.replace(p, always_on=True)
    out = {"params": dataclasses.asdict(p), "spot_perp": {}, "perp_perp": {}, "profile": {}}
    for coin, c in dataset["coins"].items():
        log(f"фандинг {coin}")
        out["profile"][coin] = {"bybit": funding_profile(c["perp"]), "bingx": funding_profile(c["bingx"])}
        ends = [s["klines"][-1][0] for s in c["perp"] if s["klines"]]
        cut = (max(ends) - YEAR_MS - p.lookback_h * H) if ends else 0
        if c["perp"] and c["spot"]["klines"]:
            r = spot_perp(coin, c["spot"]["klines"], c["perp"], p)
            r["fees_x1_apr_pct"] = spot_perp(coin, c["spot"]["klines"], c["perp"], p1).get("net_apr_pct")
            r["always_on_apr_pct"] = spot_perp(coin, c["spot"]["klines"], c["perp"], pon).get("net_apr_pct")
            r["last_12m"] = _last12(spot_perp(coin, [k for k in c["spot"]["klines"] if k[0] >= cut],
                                              since(c["perp"], cut), p))
            out["spot_perp"][coin] = r
        if c["perp"] and c["bingx"]:
            r = perp_perp(coin, c["perp"], c["bingx"], p)
            if r.get("ok"):
                r["fees_x1_apr_pct"] = perp_perp(coin, c["perp"], c["bingx"], p1).get("net_apr_pct")
                r["always_on_apr_pct"] = perp_perp(coin, c["perp"], c["bingx"], pon).get("net_apr_pct")
                r["last_12m"] = _last12(perp_perp(coin, since(c["perp"], cut), since(c["bingx"], cut), p))
            out["perp_perp"][coin] = r
    return out


def summary_ru(res):
    """Короткая сводка по-русски для отчёта."""
    lines = []
    for kind, title in (("spot_perp", "спот+шорт перпа Bybit"), ("perp_perp", "перп–перп Bybit/BingX")):
        for coin, r in res.get(kind, {}).items():
            if not r.get("ok"):
                lines.append(f"{title}, {coin}: нет оценки ({r.get('reason')})")
                continue
            lines.append(
                f"{title}, {coin}: {r['period']['years']:.2f} г., чистая APR {r['net_apr_pct']:+.2f}% на капитал "
                f"(earn {r['earn_apr_pct']:.1f}%), просадка {r['max_dd_pct']:.2f}%, худший день {r['worst_day_pct']:+.2f}%, "
                f"входов {r['entries']}, выплат в позиции {r['settlements_in_position']}; "
                f"с комиссиями ×1: {r.get('fees_x1_apr_pct')}%, всегда в позиции: {r.get('always_on_apr_pct')}%")
    return lines
