"""Калибровка плана и ожидаемая прибыль (план, этап 2, п. 6). Только расчёт и отчёт — ни сделок, ни настроек.

Источники (только чтение, mode=ro: ни файлов, ни таблиц, ни колонок не создаём; нет базы/колонки — пропускаем):
  • data/paper.db — завершённые круги сухого прогона: факт (realized_pct) против плана без запаса на курс
    (planned_raw, у старых кругов — planned_pct), итог done/failed_*, метка надёжности, индекс и запас глубины
    на старте (index_start/depth_margin — колонки этапа 1, если уже есть);
  • data/trades.db — реальные сделки с введённым фактом (fact − расчёт без запаса); факт «как расчёт»
    (fact_source = plan / plan±, если колонка есть) не информация — в калибровку не идёт.

Что считаем:
  • класс связки — (тип маршрута same/spot/cross/relay, монета покупки, площадка покупки, площадка продажи);
    поправка = среднее (факт − план) класса, сжатое к классу грубее с силой SHRINK_K = 20 кругов
    (точный → без площадки покупки → тип+монета → тип → общее среднее);
  • вероятность исполнения p по корзинам (надёжность/индекс, площадки, запас глубины): Beta(1,1) —
    (исполнилось + 1) / (кругов + 2); в корзине меньше MIN_BUCKET кругов — берём корзину грубее;
  • EV = p × (план + поправка) − (1 − p) × цена срыва; цена срыва — наблюдаемая, но не ниже CAL_FAIL_COST;
    до CAL_MIN_N завершённых кругов прогона EV = None (калибровка не активна);
  • запас на курс из данных — p90 |движения курса к рублю| за медианное время круга (подсказка, не подключена);
  • report_text — отчёт /calibration; в bot.py только за флагом CALIBRATION=1 и не в GUEST_CMDS (гостям закрыт).
"""
import bisect
import dataclasses
import html
import os
import pathlib
import sqlite3
import statistics
from typing import Optional

import history
import p2p
import paper
import trades

SHRINK_K = 20              # сила сжатия поправки к классу грубее, в кругах
MIN_BUCKET = 10            # меньше кругов в корзине p — берём корзину грубее
DEFAULT_MIN_N = 30         # CAL_MIN_N: с какого числа завершённых кругов прогона EV активна
DEFAULT_FAIL_COST = 0.5    # CAL_FAIL_COST, п.п. суммы круга: цена срыва, если наблюдаемая меньше
PLAN_FACTS = trades.PLAN_SOURCES   # fact_source «как расчёт» / «±0.5 п.п.» — расчёт, а не факт
RISK_MIN_PAIRS = 20        # меньше пар точек — p90 движения курса не считаем


@dataclasses.dataclass(frozen=True)
class Sample:
    """Один завершённый круг прогона (source=paper) или реальная сделка с фактом (source=trade)."""
    source: str
    rtype: str
    buy_asset: str
    buy_ex: str
    sell_ex: str
    done: bool
    diff: Optional[float] = None       # факт − план без запаса, п.п. (только исполнившиеся)
    realized: Optional[float] = None
    rel: str = "—"                     # корзина надёжности (rel_bucket)
    depth: str = "d?"                  # корзина запаса глубины (depth_bucket)
    duration: Optional[float] = None   # сек от старта до итога (исполнившиеся круги)


def _flag(name):
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def enabled():
    """Команда /calibration включена (CALIBRATION=1 в .env); по умолчанию выключена."""
    return _flag("CALIBRATION")


def _env_num(name, default, cast=float):
    try:
        return cast(os.getenv(name, default))
    except (TypeError, ValueError):
        return default   # кривое значение — по умолчанию


def settings():
    """Настройки калибровки из .env — читать при каждом обращении."""
    return {"min_n": _env_num("CAL_MIN_N", DEFAULT_MIN_N, int),
            "fail_cost": _env_num("CAL_FAIL_COST", DEFAULT_FAIL_COST, float)}


def route_type(buy_asset, sell_asset, route=""):
    """Тип маршрута: same — одна монета без конвертаций, spot — одна конвертация через USDT, cross — две
    конвертации (обе монеты не USDT), relay — обменник → свой кошелёк на Bybit → обменник («через Bybit»)."""
    if "через" in (route or ""):
        return "relay"
    if buy_asset == sell_asset:
        return "same"
    return "spot" if "USDT" in (buy_asset, sell_asset) else "cross"


def rel_bucket(label="", index=None):
    """Корзина надёжности: индекс 0–10 на старте (этап 1), если есть, иначе метка ✅/⚠️/🪤 (p2p.reliability)."""
    if index is not None:
        return "i8+" if index >= 8 else "i5-7" if index >= 5 else "i0-4"
    parts = (label or "").split()
    return parts[0] if parts else "—"


def depth_bucket(margin=None):
    """Корзина запаса глубины (во сколько раз стакан покрывает круг); нет данных — d?."""
    if margin is None:
        return "d?"
    return "d<1.5" if margin < 1.5 else "d1.5-3" if margin < 3 else "d3+"


def class_chain(rtype, buy_asset, buy_ex, sell_ex):
    """Ключи класса от точного к грубому (длины разные — уровни не пересекаются); корень — общее среднее."""
    return ((rtype, buy_asset, buy_ex, sell_ex), (rtype, buy_asset, sell_ex), (rtype, buy_asset), (rtype,))


def bucket_chain(rel, buy_ex, sell_ex, depth="d?"):
    """Корзины p от точной к грубой; корень — все круги."""
    venue = f"{buy_ex}→{sell_ex}"
    return (("rvd", rel, venue, depth), ("rv", rel, venue), ("v", venue), ("r", rel))


def shrink(n, mean, prior, k=SHRINK_K):
    """Среднее по n кругам, сжатое к prior: (n·mean + k·prior) / (n + k); n = 0 — prior."""
    return (n * mean + k * prior) / (n + k) if n + k else prior


def _ro(path):
    """Соединение только на чтение — файл не создаётся и не мигрирует."""
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _read(path, table, wanted, where=""):
    """Строки таблицы словарями; нет базы/таблицы — []; нет колонки — None (кроме колонок из where)."""
    if not path or not os.path.exists(path):
        return []
    try:
        con = _ro(path)
    except sqlite3.Error:
        return []
    try:
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if not have:
            return []
        exprs = ", ".join(c if c in have else f"NULL AS {c}" for c in wanted)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(f"SELECT {exprs} FROM {table} {where}")]
    except sqlite3.Error:
        return []   # битая база или нет колонки из where — как пустая
    finally:
        con.close()


_PAPER_COLS = ("buy_ex", "buy_asset", "sell_ex", "sell_asset", "route", "planned_pct", "planned_raw",
               "realized_pct", "result", "label", "ts_start", "ts_stage", "index_start", "depth_margin")
_TRADE_COLS = ("buy_ex", "buy_asset", "sell_ex", "sell_asset", "route", "profit", "fact", "fact_source")


def paper_samples(path=None):
    """Завершённые круги прогона (done/failed_*) → Sample; открытые и непонятный итог — мимо."""
    out = []
    for r in _read(path or paper.DB_PATH, "cycles", _PAPER_COLS, "WHERE result IS NOT NULL"):
        res = r["result"] or ""
        if res != "done" and not res.startswith("failed"):
            continue
        done = res == "done"
        plan = r["planned_raw"] if r["planned_raw"] is not None else r["planned_pct"]
        real = r["realized_pct"]
        diff = real - plan if done and real is not None and plan is not None else None
        dur = (r["ts_stage"] - r["ts_start"]) if done and r["ts_stage"] and r["ts_start"] else None
        out.append(Sample("paper", route_type(r["buy_asset"], r["sell_asset"], r["route"]), r["buy_asset"] or "",
                          r["buy_ex"] or "", r["sell_ex"] or "", done, diff, real,
                          rel_bucket(r["label"], r["index_start"]), depth_bucket(r["depth_margin"]), dur))
    return out


def _raw_plan(profit, buy_asset, sell_asset, risk):
    """Расчёт сделки без запаса на курс: в журнале profit с запасом (p2p._route), факт — без него."""
    vol = max(risk.get(buy_asset, 0), risk.get(sell_asset, 0))
    return ((1 + profit / 100) / (1 - vol / 100) - 1) * 100 if vol else profit


def trade_samples(path=None):
    """Реальные сделки с фактом → Sample (для поправки; в p не идут — срывы в журнал не пишутся). Факт «как
    расчёт» (fact_source plan/plan±) пропускаем; колонки fact_source нет — берём все факты."""
    risk = p2p._fees(os.getenv("RISK_BUFFER", p2p.DEFAULT_RISK))   # текущий запас — приближение для старых сделок
    out = []
    for r in _read(path or trades.DB_PATH, "trades", _TRADE_COLS, "WHERE fact IS NOT NULL"):
        if (r["fact_source"] or "") in PLAN_FACTS or r["profit"] is None:
            continue
        plan = _raw_plan(r["profit"], r["buy_asset"], r["sell_asset"], risk)
        out.append(Sample("trade", route_type(r["buy_asset"], r["sell_asset"], r["route"]), r["buy_asset"] or "",
                          r["buy_ex"] or "", r["sell_ex"] or "", True, r["fact"] - plan, r["fact"]))
    return out


def load_samples(paper_paths=None, trades_path=None):
    """Круги всех переданных баз прогона (по умолчанию data/paper.db) + факты сделок (data/trades.db;
    trades_path="" — без сделок)."""
    paths = [paper.DB_PATH] if paper_paths is None else list(paper_paths)
    out = [s for p in paths for s in paper_samples(p)]
    if trades_path != "":
        out += trade_samples(trades_path)
    return out


class Calibration:
    """Поправки по классам, p по корзинам и EV по набору Sample."""

    def __init__(self, samples, k=SHRINK_K, min_n=DEFAULT_MIN_N, fail_cost=DEFAULT_FAIL_COST, min_bucket=MIN_BUCKET):
        self.samples = list(samples)
        self.k, self.min_n, self.min_bucket = k, min_n, min_bucket
        runs = [s for s in self.samples if s.source == "paper"]
        self.n = len(runs)                          # завершённые круги прогона — от них активность и p
        self.n_done = sum(1 for s in runs if s.done)
        self.n_facts = sum(1 for s in self.samples if s.source == "trade")
        diffs = [s for s in self.samples if s.diff is not None]
        self.n_diffs = len(diffs)
        self.global_bias = statistics.fmean(s.diff for s in diffs) if diffs else 0.0
        self._bias = {}   # ключ класса -> [n, сумма разниц]
        for s in diffs:
            for key in class_chain(s.rtype, s.buy_asset, s.buy_ex, s.sell_ex):
                acc = self._bias.setdefault(key, [0, 0.0])
                acc[0] += 1
                acc[1] += s.diff
        self._fill = {}   # ключ корзины -> [исполнилось, всего]
        for s in runs:
            for key in bucket_chain(s.rel, s.buy_ex, s.sell_ex, s.depth):
                acc = self._fill.setdefault(key, [0, 0])
                acc[0] += s.done
                acc[1] += 1
        losses = [max(0.0, -s.realized) for s in runs if not s.done and s.realized is not None]
        self.observed_fail_cost = statistics.fmean(losses) if losses else None
        self.fail_cost_floor = fail_cost
        self.fail_cost = max(fail_cost, self.observed_fail_cost or 0.0)
        durs = [s.duration for s in runs if s.done and s.duration and s.duration > 0]
        self.median_duration = statistics.median(durs) if durs else None

    @property
    def active(self):
        return self.n >= self.min_n

    def bias(self, rtype, buy_asset, buy_ex, sell_ex):
        """(поправка п.п., кругов в точном классе): от общего среднего вниз по цепочке, на каждом уровне
        среднее уровня сжимается к оценке уровня выше. Класса нет — оценка ближайшего известного грубее."""
        chain = class_chain(rtype, buy_asset, buy_ex, sell_ex)
        est = self.global_bias
        for key in reversed(chain):
            n, total = self._bias.get(key, (0, 0.0))
            est = shrink(n, total / n if n else 0.0, est, self.k)
        return est, self._bias.get(chain[0], (0, 0.0))[0]

    def fill(self, rel, buy_ex, sell_ex, depth="d?"):
        """(p, ключ корзины, кругов в ней): первая корзина цепочки с ≥ min_bucket кругов, иначе все круги;
        Beta(1,1): (исполнилось + 1) / (кругов + 2)."""
        for key in bucket_chain(rel, buy_ex, sell_ex, depth):
            done, total = self._fill.get(key, (0, 0))
            if total >= self.min_bucket:
                return (done + 1) / (total + 2), key, total
        return (self.n_done + 1) / (self.n + 2), ("all",), self.n

    def ev(self, planned, rtype, buy_asset, buy_ex, sell_ex, rel="—", depth="d?"):
        """Ожидаемая прибыль, п.п.: p × (план + поправка) − (1 − p) × цена срыва. План — без запаса на курс,
        если есть (поправка меряется против него). Калибровка не активна или плана нет — None."""
        if not self.active or planned is None:
            return None
        adj, _ = self.bias(rtype, buy_asset, buy_ex, sell_ex)
        p, _, _ = self.fill(rel, buy_ex, sell_ex, depth)
        return p * (planned + adj) - (1 - p) * self.fail_cost

    def classes(self):
        """Точные классы по убыванию числа кругов: [{"key", "n", "mean", "bias"}]."""
        out = []
        for key, (n, total) in self._bias.items():
            if len(key) == 4:
                out.append({"key": key, "n": n, "mean": total / n, "bias": self.bias(*key)[0]})
        return sorted(out, key=lambda c: (-c["n"], c["key"]))

    def buckets(self, level="rv"):
        """Корзины p одного уровня по убыванию числа кругов: [{"key", "n", "done", "p"}] (p — Beta(1,1)
        самой корзины, без перехода к грубой)."""
        out = [{"key": key[1:], "n": total, "done": done, "p": (done + 1) / (total + 2)}
               for key, (done, total) in self._fill.items() if key[0] == level]
        return sorted(out, key=lambda b: (-b["n"], b["key"]))


def build(paper_paths=None, trades_path=None, **kw):
    """Калибровка по живым базам (или переданным путям) с настройками из .env; kw перекрывает настройки."""
    opts = settings()
    opts.update(kw)
    return Calibration(load_samples(paper_paths, trades_path), **opts)


def deal_ev(cal, deal, planned_raw=None, label="", index=None, depth=None):
    """EV связки p2p (profit, buy Ad, sell Ad, маршрут); planned_raw — план без запаса на курс
    (p2p.profit_breakdown), нет — profit. None — калибровка не активна."""
    profit, b, s, route = deal
    plan = profit if planned_raw is None else planned_raw
    return cal.ev(plan, route_type(b.asset, s.asset, route), b.asset, b.ex, s.ex,
                  rel_bucket(label, index), depth_bucket(depth))


def rank_deals(cal, deals, labels=None):
    """Связки по убыванию EV (при равенстве — прежний порядок); калибровка не активна — порядок как был.
    labels — метки надёжности в том же порядке (p2p.reliability), нет — «—». В бот не подключено."""
    deals = list(deals)
    if not cal.active:
        return deals
    labels = list(labels) if labels is not None else [""] * len(deals)
    scored = [(deal_ev(cal, d, label=lab), i, d) for i, (d, lab) in enumerate(zip(deals, labels))]
    return [d for _, _, d in sorted(scored, key=lambda t: (-t[0], t[1]))]


def usdt_rub_series(path=None):
    """Ряд ориентира USDT/RUB [(ts, цена)] из data/history.db (snap.ref, раз в 5 мин); нет базы — []."""
    rows = _read(path or history.DB_PATH, "history", ("ts", "ref"), "WHERE ref > 0 ORDER BY ts")
    return [(r["ts"], r["ref"]) for r in rows if r["ts"] is not None]


def moves_p90(series, horizon):
    """(p90 |цена(t + horizon) / цена(t) − 1| в %, число пар). Пара — первая точка не раньше t + horizon и не
    позже t + 1.5·horizon (дыра в ряду — не движение). Точки с одним ts — одна. Пар < RISK_MIN_PAIRS — (None, n)."""
    by_ts = {}
    for t, price in series:
        if t is not None and price and price > 0:
            by_ts[t] = price
    ts = sorted(by_ts)
    tol = max(horizon / 2, 60)
    moves = []
    for i, t in enumerate(ts):
        j = bisect.bisect_left(ts, t + horizon, lo=i + 1)
        if j < len(ts) and ts[j] - (t + horizon) <= tol:
            moves.append(abs(by_ts[ts[j]] / by_ts[t] - 1) * 100)
    if len(moves) < RISK_MIN_PAIRS:
        return None, len(moves)
    return statistics.quantiles(moves, n=10, method="inclusive")[-1], len(moves)


def risk_buffer_suggestion(asset, series, horizon, current=None):
    """Запас на курс из данных (только подсказка — в расчёт не подключена): p90 движения курса монеты к рублю
    за horizon сек (медианное время круга). {"asset", "horizon_min", "suggest_pct", "pairs", "current_pct"};
    нет времени круга или мало пар — None. current — текущий RISK_BUFFER монеты (по умолчанию из .env)."""
    if not horizon or horizon <= 0:
        return None
    p90, pairs = moves_p90(series, horizon)
    if p90 is None:
        return None
    if current is None:
        current = p2p._fees(os.getenv("RISK_BUFFER", p2p.DEFAULT_RISK)).get(asset, 0.0)
    return {"asset": asset, "horizon_min": horizon / 60, "suggest_pct": round(p90, 3), "pairs": pairs,
            "current_pct": current}


def _pp(x):
    return f"{x:+.2f}"


def report_text(cal=None, series=None, limit=10):
    """Отчёт /calibration (HTML) — только владельцу: круги, активность EV, поправки по классам, p по корзинам,
    цена срыва и подсказка запаса на курс USDT. cal/series не переданы — по живым базам."""
    cal = build() if cal is None else cal
    esc = html.escape
    lines = ["📐 <b>Калибровка плана</b>",
             f"Кругов прогона: {cal.n} (исполнилось {cal.n_done}, сорвалось {cal.n - cal.n_done})"
             f" · фактов сделок: {cal.n_facts}"]
    if cal.active:
        lines.append(f"✅ EV активна: кругов ≥ {cal.min_n} (CAL_MIN_N)")
    else:
        lines.append(f"⏳ EV не активна: {cal.n} из {cal.min_n} кругов (CAL_MIN_N)")
    if not cal.n and not cal.n_facts:
        lines.append("Данных пока нет — нужны завершённые круги сухого прогона (/paper on).")
        return "\n".join(lines)
    lines.append(f"Смещение факт − план (все): {_pp(cal.global_bias)} п.п. по {cal.n_diffs}")
    seen = "нет" if cal.observed_fail_cost is None else f"{cal.observed_fail_cost:.2f}"
    lines.append(f"Исполнение (все): {(cal.n_done + 1) / (cal.n + 2) * 100:.0f}% · цена срыва "
                 f"{cal.fail_cost:.2f} п.п. (наблюдалась {seen}, CAL_FAIL_COST {cal.fail_cost_floor:g})")
    classes = cal.classes()
    if classes:
        lines += ["", "<b>Классы</b> (кругов · среднее → поправка, п.п.):"]
        for c in classes[:limit]:
            rtype, asset, bex, sex = c["key"]
            lines.append(f"• {rtype} {esc(asset)} {esc(bex)}→{esc(sex)}: {c['n']} · {_pp(c['mean'])} → {_pp(c['bias'])}")
        if len(classes) > limit:
            lines.append(f"… и ещё {len(classes) - limit}")
    buckets = cal.buckets("rv")
    if buckets:
        lines += ["", "<b>Исполнение по корзинам</b> (кругов · p):"]
        for b in buckets[:limit]:
            rel, venue = b["key"]
            mark = "" if b["n"] >= cal.min_bucket else " (мало — берётся корзина грубее)"
            lines.append(f"• {esc(rel)} {esc(venue)}: {b['n']} · {b['p'] * 100:.0f}%{mark}")
    series = usdt_rub_series() if series is None else series
    risk = risk_buffer_suggestion("USDT", series, cal.median_duration or 0)
    if risk:
        lines += ["", f"Запас на курс USDT из данных: p90 за {risk['horizon_min']:.0f} мин — {risk['suggest_pct']:.2f}% "
                      f"(пар {risk['pairs']}), сейчас {risk['current_pct']:g}% — только подсказка"]
    lines += ["", f"Поправка сжата к классу грубее (k = {cal.k}); p — Beta(1,1), корзина < {cal.min_bucket} кругов → "
                  f"грубее. EV = p × (план + поправка) − (1 − p) × цена срыва."]
    return "\n".join(lines)
