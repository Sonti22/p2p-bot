"""Пороги перехода «бумага → кнопка → автомат» — чистые функции, без сети и файлов (таблица «Пороги перехода» плана).

Режимы: paper < minlot < confirm < auto (trading.switch.MODES).
- minlot (кнопка, минимальный лот) — сразу после сильного бэктеста (решение владельца 2026-09-27, «нужно быстрее»);
- confirm (кнопка, полные потолки) — порог «бумага → кнопка» плана; при сильном бэктесте бумага укорочена
  (SHORT_PAPER: хедж/фандинг 14 дней, направленная 30), остальные условия качества — как в плане;
- auto — confirm пройден И порог «кнопка → авто» по реальным сделкам (сделки minlot/confirm считаются) И 0 unknown
  дольше 10 мин и 0 нарушений лимитов.
Нет нужной цифры, NaN или не число — порог не пройден (fail closed). Статистика на входе — словари, собирает их
код бумаги/бэктеста; здесь только сравнения и простая математика (Sharpe, PF, просадка, 90% ДИ).
"""
import math
from collections import namedtuple

HEDGE, FUNDING, DIRECTIONAL, MAKER = "hedge", "funding", "directional", "maker"
STRATEGIES = (HEDGE, FUNDING, DIRECTIONAL, MAKER)
MODES = ("paper", "minlot", "confirm", "auto")
Z90 = 1.6448536269514722   # двусторонний 90%: Φ⁻¹(0.95)

Gate = namedtuple("Gate", "passed failures")

# (ключ, сравнение, порог, пояснение). Сравнения: ">=", "<=", ">", "<", "==".
PAPER_TO_BUTTON = {
    HEDGE: [("days", ">=", 14, "дней бумаги"), ("count", ">=", 50, "хеджей"),
            ("ratio_ok_share", ">=", 0.95, "доля хеджей с коэффициентом 0.9–1.1"),
            ("cost_to_buffer", "<=", 0.6, "стоимость хеджа / запас на курс"),
            ("sigma_ratio", "<=", 0.5, "σ(факт − план) с хеджем / без хеджа")],
    FUNDING: [("days", ">=", 60, "дней бумаги"), ("payments", ">=", 90, "выплат фандинга"),
              ("apr_over_earn_pp", ">=", 3, "чистая APR − ставка earn, п.п."),
              ("max_drawdown", "<=", 0.02, "просадка")],
    DIRECTIONAL: [("days", ">=", 90, "дней бумаги"), ("trades", ">=", 50, "сделок на бумаге"),
                  ("in_backtest_ci90", "==", True, "результат бумаги внутри 90% ДИ бэктеста")],
    MAKER: [("days", ">=", 30, "дней бумаги"), ("net_after_fee", ">", 0, "итог после комиссии 0.3%")],
}
BUTTON_TO_AUTO = {
    HEDGE: [("days", ">=", 21, "дней реальной торговли"), ("count", ">=", 30, "реальных хеджей"),
            ("model_divergence_pp", "<=", 0.05, "расхождение с моделью, п.п.")],
    FUNDING: [("days", ">=", 30, "дней реальной торговли"), ("cycles", ">=", 20, "циклов"),
              ("within_paper_1sigma", "==", True, "результат в пределах бумаги ± 1σ")],
    DIRECTIONAL: [("days", ">=", 60, "дней реальной торговли"), ("trades", ">=", 30, "реальных сделок"),
                  ("max_drawdown", "<", 0.10, "просадка (стоп автомата — 10%)")],
    MAKER: [("confirmed_edits", ">=", 100, "подтверждённых правок"), ("corridor_exits", "==", 0, "выходов из коридора")],
}
AUTO_COMMON = [("unknown_over_10min", "==", 0, "ордеров unknown дольше 10 мин"),
               ("limit_violations", "==", 0, "нарушений лимитов")]
# «сильный бэктест» — условие minlot и укороченной бумаги. Направленная — критерии плана; хедж/фандинг — те же
# условия качества, что у бумаги, но на истории длиной не меньше BT_MIN_DAYS. TODO(владелец): подтвердить пороги.
BACKTEST_STRONG = {
    HEDGE: [("days", ">=", 90, "дней истории"), ("count", ">=", 50, "хеджей"),
            ("ratio_ok_share", ">=", 0.95, "доля хеджей с коэффициентом 0.9–1.1"),
            ("cost_to_buffer", "<=", 0.6, "стоимость хеджа / запас на курс"),
            ("sigma_ratio", "<=", 0.5, "σ(факт − план) с хеджем / без хеджа")],
    FUNDING: [("days", ">=", 180, "дней истории"), ("payments", ">=", 540, "выплат фандинга"),
              ("apr_over_earn_pp", ">=", 3, "чистая APR − ставка earn, п.п."), ("max_drawdown", "<=", 0.02, "просадка")],
    DIRECTIONAL: [("months_oos", ">=", 12, "месяцев вне выборки"), ("trades", ">=", 200, "сделок"),
                  ("sharpe", ">=", 1, "Sharpe"), ("profit_factor", ">=", 1.2, "profit factor"),
                  ("p_value_vs_random", "<", 0.05, "p-value против случайных входов")],
}
# укороченная бумага при сильном бэктесте (решение владельца: 7–14 дней, направленная 30): берём верхнюю границу.
# Число сделок/выплат — пропорционально сроку; TODO(владелец): подтвердить минимумы.
SHORT_PAPER = {
    HEDGE: {"days": 14, "count": 50},
    FUNDING: {"days": 14, "payments": 42},
    DIRECTIONAL: {"days": 30, "trades": 15},
}


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _holds(value, op, threshold):
    if op == "==" and isinstance(threshold, bool):
        return value is threshold
    v = _num(value)
    if v is None:
        return False
    return {">=": v >= threshold, "<=": v <= threshold, ">": v > threshold, "<": v < threshold,
            "==": v == threshold}[op]


def evaluate(rules, stats, overrides=None):
    """Gate(passed, причины) по правилам; overrides — {ключ: другой порог} (укороченная бумага)."""
    stats = stats if isinstance(stats, dict) else {}
    failures = []
    for key, op, threshold, what in rules:
        threshold = (overrides or {}).get(key, threshold)
        if key not in stats:
            failures.append(f"нет данных: {what} ({key})")
        elif not _holds(stats[key], op, threshold):
            failures.append(f"{what}: {stats[key]!r}, нужно {op} {threshold}")
    return Gate(not failures, tuple(failures))


def backtest_strong(strategy, backtest):
    rules = BACKTEST_STRONG.get(strategy)
    if rules is None:
        return Gate(False, (f"{strategy}: бэктест не заменяет бумагу",))
    return evaluate(rules, backtest)


def paper_to_button(strategy, paper, backtest=None):
    """Порог «бумага → кнопка» (полные потолки). Сильный бэктест — укороченная бумага (SHORT_PAPER), остальное — как
    в плане. Направленной без сильного бэктеста кнопка не положена вовсе (план: бэктест — часть порога)."""
    if strategy not in PAPER_TO_BUTTON:
        return Gate(False, (f"стратегия {strategy!r} неизвестна",))
    bt = backtest_strong(strategy, backtest) if backtest is not None or strategy == DIRECTIONAL else None
    if strategy == DIRECTIONAL and not bt.passed:
        return Gate(False, ("бэктест не прошёл: " + "; ".join(bt.failures),))
    overrides = SHORT_PAPER.get(strategy) if bt is not None and bt.passed else None
    return evaluate(PAPER_TO_BUTTON[strategy], paper, overrides)


def button_to_auto(strategy, live):
    if strategy not in BUTTON_TO_AUTO:
        return Gate(False, (f"стратегия {strategy!r} неизвестна",))
    return evaluate(BUTTON_TO_AUTO[strategy] + AUTO_COMMON, live)


def max_mode(strategy, paper=None, backtest=None, live=None):
    """Самый рискованный режим, который разрешают пороги: auto / confirm / minlot / paper."""
    confirm = paper_to_button(strategy, paper, backtest)
    if confirm.passed:
        return "auto" if button_to_auto(strategy, live).passed else "confirm"
    if backtest is not None and backtest_strong(strategy, backtest).passed:
        return "minlot"
    return "paper"


def allow(strategy, requested, paper=None, backtest=None, live=None):
    """Разрешён ли режим requested: Gate. auto без пройденных порогов — всегда отказ."""
    if requested not in MODES:
        return Gate(False, (f"режим {requested!r} неизвестен",))
    top = max_mode(strategy, paper, backtest, live)
    if MODES.index(requested) <= MODES.index(top):
        return Gate(True, ())
    why = []
    if requested in ("confirm", "auto"):
        why += paper_to_button(strategy, paper, backtest).failures
    if requested == "auto":
        why += button_to_auto(strategy, live).failures
    if requested == "minlot":
        why += backtest_strong(strategy, backtest).failures if backtest is not None else ("нет бэктеста",)
    return Gate(False, (f"пороги разрешают только {top}",) + tuple(why))


# --- математика статистики (float) ---

def mean(xs):
    xs = [float(x) for x in xs]
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    """Выборочное σ (n − 1); меньше двух точек — None."""
    xs = [float(x) for x in xs]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(returns, periods_per_year):
    """Годовой Sharpe без безрисковой ставки: mean/σ × √периодов; σ = 0 или мало точек — None."""
    m, s = mean(returns), stdev(returns)
    if m is None or not s:
        return None
    return m / s * math.sqrt(periods_per_year)


def profit_factor(pnls):
    """Сумма прибылей / |сумма убытков|; убытков нет — inf (если есть прибыль), сделок нет — None."""
    wins = sum(float(p) for p in pnls if float(p) > 0)
    losses = -sum(float(p) for p in pnls if float(p) < 0)
    if losses == 0:
        return math.inf if wins > 0 else None
    return wins / losses


def max_drawdown(equity):
    """Максимальная просадка доли от пика по кривой капитала (значения > 0)."""
    peak, worst = None, 0.0
    for e in equity:
        e = float(e)
        if e <= 0:
            raise ValueError("капитал должен быть > 0")
        peak = e if peak is None else max(peak, e)
        worst = max(worst, (peak - e) / peak)
    return worst


def ci90(bt_mean, bt_std, n):
    """90% ДИ среднего по n сделкам при распределении бэктеста: mean ± 1.645·σ/√n."""
    half = Z90 * float(bt_std) / math.sqrt(n)
    return float(bt_mean) - half, float(bt_mean) + half


def within_ci90(paper_mean, bt_mean, bt_std, n_paper):
    """Средний результат n сделок бумаги внутри 90% ДИ бэктеста (для порога направленной)."""
    if n_paper < 1 or bt_std is None:
        return False
    lo, hi = ci90(bt_mean, bt_std, n_paper)
    return lo <= float(paper_mean) <= hi


def within_sigma(live_mean, paper_mean, paper_std):
    """Результат реальной торговли в пределах бумаги ± 1σ (для порога фандинга)."""
    if paper_std is None:
        return False
    return abs(float(live_mean) - float(paper_mean)) <= float(paper_std)
