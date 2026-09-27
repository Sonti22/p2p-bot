"""Общие метрики бэктестов: перцентили, просадка, дневные ряды, Sharpe, profit factor. Только stdlib."""
import math

DAY_MS = 86_400_000


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def percentile(xs, q):
    """Перцентиль q ∈ [0, 100] с линейной интерполяцией (как numpy по умолчанию)."""
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def max_drawdown(values):
    """Наибольшая просадка ряда капитала как доля от предыдущего пика (0.05 = 5%)."""
    peak, dd = None, 0.0
    for v in values:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd


def daily_last(points):
    """[(ts_ms, value)] по возрастанию → [(начало суток UTC, последнее значение за сутки)]."""
    out = []
    for ts, v in points:
        day = ts - ts % DAY_MS
        if out and out[-1][0] == day:
            out[-1] = (day, v)
        else:
            out.append((day, v))
    return out


def returns(values):
    return [(b - a) / a for a, b in zip(values, values[1:]) if a]


def sharpe(rets, periods_per_year=365):
    s = stdev(rets)
    return mean(rets) / s * math.sqrt(periods_per_year) if s > 0 else 0.0


def profit_factor(pnls):
    gain = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    if loss == 0:
        return float("inf") if gain > 0 else 0.0
    return gain / loss


def equity_stats(points, capital):
    """Сводка по ряду капитала [(ts, equity)]: итог, просадка, худший день, дневной Sharpe."""
    if not points:
        return {"max_dd_pct": 0.0, "worst_day_pct": 0.0, "sharpe": 0.0, "days": 0}
    daily = daily_last(points)
    vals = [capital] + [v for _, v in daily]
    rets = returns(vals)
    return {"max_dd_pct": round(max_drawdown([capital] + [v for _, v in points]) * 100, 3),
            "worst_day_pct": round(min(rets) * 100, 3) if rets else 0.0,
            "sharpe": round(sharpe(rets), 3), "days": len(daily)}


def fnum(x, nd=3):
    """Округление для JSON: inf/nan → строка, чтобы json.dump не падал и отчёт читался."""
    if x is None:
        return None
    if isinstance(x, float) and (math.isinf(x) or math.isnan(x)):
        return str(x)
    return round(x, nd)
