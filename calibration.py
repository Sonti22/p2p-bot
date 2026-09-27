"""Калибровка плана и ожидаемая прибыль (план, этап 2, п. 6 / 2.6). Только расчёт и отчёт — ни сделок, ни настроек.

Источники (только чтение, mode=ro: ни файлов, ни таблиц, ни колонок не создаём; нет базы/колонки — пропускаем):
  • data/paper.db — завершённые круги сухого прогона: факт (realized_pct) против плана без запаса на курс
    (planned_raw, у старых кругов — planned_pct), итог done/failed_*, метка надёжности, индекс, запас глубины и
    сделки/% мерчантов на старте (колонки этапа 1, если уже есть), бумажный хедж закрытых кругов (hedge_*);
  • data/trades.db — реальные сделки с введённым фактом (fact − расчёт без запаса); факт «как расчёт»
    (fact_source = plan / plan±, если колонка есть) не информация — в калибровку не идёт;
  • data/snapshots.db — ориентиры монет к рублю (refs) по снимкам сканов, data/history.db — ориентир USDT/RUB.

Что считаем:
  • класс связки — (тип маршрута same/spot/cross/relay, монета покупки, площадка покупки, площадка продажи);
    поправка = n/(n+k)·среднее (факт − план) класса + k/(n+k)·априор, k = CAL_SHRINK_K (20). Априор — та же формула
    по кругам вне класса на уровень грубее (без площадки покупки → тип+монета → тип → все круги), за последним
    уровнем — 0: свои круги в априор не входят, и без соседей поправка ровно n/(n+k)·среднее класса;
  • вероятность исполнения p по корзинам (надёжность/индекс, площадки, запас глубины, мерчанты) — сглаживание
    Лапласа (исполнилось + 1) / (кругов + 2); в корзине меньше CAL_MIN_BUCKET кругов — берём корзину грубее, мало и во
    всех кругах — нейтральный априор 0.5;
  • EV = p × (план без запаса + поправка) − (1 − p) × цена срыва; цена срыва — наблюдаемая, но не ниже
    CAL_FAIL_COST; до CAL_MIN_N завершённых кругов прогона EV = None (калибровка не активна);
  • EV_RANK=1 (p2p.Config.ev_rank): rank_snapshot — связки скана по убыванию EV, snap.ev — EV и p для карточки;
  • запас на курс из данных — p90 |движения курса монеты к рублю| за медианное время круга рядом с RISK_BUFFER и
    стоимостью бумажного хеджа (simperp: сейчас по котировкам перпов и по закрытым кругам) — только сравнение;
  • report_text — отчёт /calibration; в bot.py только за флагом CALIBRATION=1 и не в GUEST_CMDS (гостям закрыт).
"""
import bisect
import dataclasses
import html
import json
import math
import os
import pathlib
import sqlite3
import statistics
import zlib
from typing import Optional

import history
import p2p
import paper
import simperp
import snapshots
import trades

SHRINK_K = 20              # CAL_SHRINK_K: сила сжатия поправки класса, в кругах
MIN_BUCKET = 10            # CAL_MIN_BUCKET: меньше кругов в корзине p — берём корзину грубее
DEFAULT_MIN_N = 30         # CAL_MIN_N: с какого числа завершённых кругов прогона EV активна
DEFAULT_FAIL_COST = 0.5    # CAL_FAIL_COST, п.п. суммы круга: цена срыва, если наблюдаемая меньше
NEUTRAL_P = 0.5            # p без данных — сглаживание Лапласа при нуле кругов
PLAN_FACTS = trades.PLAN_SOURCES   # fact_source «как расчёт» / «±0.5 п.п.» — расчёт, а не факт
RISK_MIN_PAIRS = 20        # меньше пар точек — p90 движения курса не считаем
MERCH_ORDERS = 300         # корзина мерчантов m+: у слабейшего из двух мерчантов связки от стольких сделок…
MERCH_RATE = 97.0          # …и от стольких % успешных; иначе m-
REFRESH = 300              # сек: бот пересобирает калибровку для EV_RANK из баз не чаще
FX_MAX_POINTS = 2000       # точек ряда курса монеты по снимкам — не больше (самые свежие)
FX_ASSETS = tuple(p2p.DEFAULT_ASSETS.split(","))   # порядок монет в отчёте
_ROOT = ()                 # ключ поправки «все круги» (длина 0 — не пересекается с уровнями класса)


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
    merch: str = "m?"                  # корзина мерчантов (merchant_bucket)


def _flag(name):
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def enabled():
    """Команда /calibration включена (CALIBRATION=1 в .env); по умолчанию выключена."""
    return _flag("CALIBRATION")


def _env_num(name, default, cast=float, lo=None):
    """Число из .env: кривое или не конечное — по умолчанию, меньше lo — lo."""
    try:
        value = cast(os.getenv(name, default))
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(value):
        return default
    return value if lo is None else max(lo, value)


def settings():
    """Настройки калибровки из .env — читать при каждом обращении."""
    return {"min_n": _env_num("CAL_MIN_N", DEFAULT_MIN_N, int, 1),
            "fail_cost": _env_num("CAL_FAIL_COST", DEFAULT_FAIL_COST, float, 0.0),
            "k": _env_num("CAL_SHRINK_K", SHRINK_K, float, 0.0),
            "min_bucket": _env_num("CAL_MIN_BUCKET", MIN_BUCKET, int, 1)}


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


def merchant_bucket(orders=None, rate=None):
    """Корзина мерчантов связки по слабейшему из двух (сделки и % успешных — меньшие у покупки и продажи): m+ — от
    MERCH_ORDERS сделок и MERCH_RATE %, m- — меньше; нет данных — m?."""
    if orders is None or rate is None:
        return "m?"
    return "m+" if orders >= MERCH_ORDERS and rate >= MERCH_RATE else "m-"


def _weakest(a, b):
    return None if a is None or b is None else min(a, b)


def class_chain(rtype, buy_asset, buy_ex, sell_ex):
    """Ключи класса от точного к грубому (длины разные — уровни не пересекаются); за ними — все круги (_ROOT)."""
    return ((rtype, buy_asset, buy_ex, sell_ex), (rtype, buy_asset, sell_ex), (rtype, buy_asset), (rtype,))


def bucket_chain(rel, buy_ex, sell_ex, depth="d?", merch="m?"):
    """Корзины p от точной к грубой; корень — все круги. Мерчанты известны — ещё две корзины: самая точная (с ними)
    и «надёжность × мерчанты» перед «только надёжность»."""
    venue = f"{buy_ex}→{sell_ex}"
    known = merch != "m?"
    return ((("rvdm", rel, venue, depth, merch),) if known else ()) + (
        ("rvd", rel, venue, depth), ("rv", rel, venue), ("v", venue)) + (
        (("rm", rel, merch),) if known else ()) + (("r", rel),)


def shrink(n, mean, prior, k=SHRINK_K):
    """Среднее по n кругам, сжатое к prior: (n·mean + k·prior) / (n + k) = n/(n+k)·mean + k/(n+k)·prior; n = 0 — prior."""
    return (n * mean + k * prior) / (n + k) if n + k else prior


def _ro(path):
    """Соединение только на чтение — файл не создаётся и не мигрирует."""
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _read(path, table, wanted, where="", distinct=False):
    """Строки таблицы словарями; нет базы/таблицы — []; нет колонки — None (кроме колонок из where).
    distinct — только разные строки (SELECT DISTINCT)."""
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
        select = "SELECT DISTINCT" if distinct else "SELECT"
        return [dict(r) for r in con.execute(f"{select} {exprs} FROM {table} {where}")]
    except sqlite3.Error:
        return []   # битая база или нет колонки из where — как пустая
    finally:
        con.close()


_PAPER_COLS = ("buy_ex", "buy_asset", "sell_ex", "sell_asset", "route", "planned_pct", "planned_raw",
               "realized_pct", "result", "label", "ts_start", "ts_stage", "index_start", "depth_margin",
               "buy_orders", "buy_rate", "sell_orders", "sell_rate")
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
        merch = merchant_bucket(_weakest(r["buy_orders"], r["sell_orders"]), _weakest(r["buy_rate"], r["sell_rate"]))
        out.append(Sample("paper", route_type(r["buy_asset"], r["sell_asset"], r["route"]), r["buy_asset"] or "",
                          r["buy_ex"] or "", r["sell_ex"] or "", done, diff, real,
                          rel_bucket(r["label"], r["index_start"]), depth_bucket(r["depth_margin"]), dur, merch))
    return out


def _raw_plan(profit, buy_asset, sell_asset, risk):
    """Расчёт без запаса на курс: p2p._route умножает выход маршрута на (1 − запас монеты), факт — без него."""
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
        self.global_bias = statistics.fmean(s.diff for s in diffs) if diffs else 0.0   # сырое среднее — для отчёта
        self._bias = {}   # ключ класса (и _ROOT) -> [n, сумма разниц]
        for s in diffs:
            for key in class_chain(s.rtype, s.buy_asset, s.buy_ex, s.sell_ex) + (_ROOT,):
                acc = self._bias.setdefault(key, [0, 0.0])
                acc[0] += 1
                acc[1] += s.diff
        self._fill = {}   # ключ корзины -> [исполнилось, всего]
        for s in runs:
            for key in bucket_chain(s.rel, s.buy_ex, s.sell_ex, s.depth, s.merch):
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
        """(поправка п.п., кругов в точном классе) = n/(n+k)·среднее класса + k/(n+k)·априор. Априор — та же формула
        по «кольцу» уровнем грубее (круги уровня минус круги уровня точнее), и так до всех кругов; за ними — 0. Свои
        круги в априор не входят (не считаются дважды): без соседей поправка ровно n/(n+k)·среднее класса, класса нет
        — оценка по соседям."""
        levels = [self._bias.get(key, (0, 0.0)) for key in class_chain(rtype, buy_asset, buy_ex, sell_ex) + (_ROOT,)]
        est = 0.0
        for i in reversed(range(len(levels))):
            n, total = levels[i]
            if i:   # кольцо: без кругов уровня точнее — они учтены ниже по цепочке
                n, total = n - levels[i - 1][0], total - levels[i - 1][1]
            est = shrink(n, total / n if n else 0.0, est, self.k)
        return est, levels[0][0]

    def fill(self, rel, buy_ex, sell_ex, depth="d?", merch="m?"):
        """(p, ключ корзины, кругов в ней): первая корзина цепочки с ≥ min_bucket кругов, иначе все круги, если их
        ≥ min_bucket, иначе нейтральный априор 0.5. Лаплас: (исполнилось + 1) / (кругов + 2)."""
        for key in bucket_chain(rel, buy_ex, sell_ex, depth, merch):
            done, total = self._fill.get(key, (0, 0))
            if total >= self.min_bucket:
                return (done + 1) / (total + 2), key, total
        if self.n >= self.min_bucket:
            return (self.n_done + 1) / (self.n + 2), ("all",), self.n
        return NEUTRAL_P, ("prior",), self.n

    def estimate(self, planned, rtype, buy_asset, buy_ex, sell_ex, rel="—", depth="d?", merch="m?"):
        """{"ev", "p", "corrected", "adj", "n_class", "bucket", "n_bucket"}: EV = p × (план + поправка) − (1 − p) ×
        цена срыва, п.п. План — без запаса на курс (поправка меряется против него). Не активна или плана нет — None."""
        if not self.active or planned is None:
            return None
        adj, n_class = self.bias(rtype, buy_asset, buy_ex, sell_ex)
        p, bucket, n_bucket = self.fill(rel, buy_ex, sell_ex, depth, merch)
        corrected = planned + adj
        return {"ev": p * corrected - (1 - p) * self.fail_cost, "p": p, "corrected": corrected, "adj": adj,
                "n_class": n_class, "bucket": bucket, "n_bucket": n_bucket}

    def ev(self, planned, rtype, buy_asset, buy_ex, sell_ex, rel="—", depth="d?", merch="m?"):
        """Ожидаемая прибыль, п.п. (estimate); калибровка не активна или плана нет — None."""
        e = self.estimate(planned, rtype, buy_asset, buy_ex, sell_ex, rel, depth, merch)
        return None if e is None else e["ev"]

    def classes(self):
        """Точные классы по убыванию числа кругов: [{"key", "n", "mean", "bias"}]."""
        out = []
        for key, (n, total) in self._bias.items():
            if len(key) == 4:
                out.append({"key": key, "n": n, "mean": total / n, "bias": self.bias(*key)[0]})
        return sorted(out, key=lambda c: (-c["n"], c["key"]))

    def buckets(self, level="rv"):
        """Корзины p одного уровня по убыванию числа кругов: [{"key", "n", "done", "p"}] (p — Лаплас самой корзины,
        без перехода к грубой)."""
        out = [{"key": key[1:], "n": total, "done": done, "p": (done + 1) / (total + 2)}
               for key, (done, total) in self._fill.items() if key[0] == level]
        return sorted(out, key=lambda b: (-b["n"], b["key"]))


def build(paper_paths=None, trades_path=None, **kw):
    """Калибровка по живым базам (или переданным путям) с настройками из .env; kw перекрывает настройки."""
    opts = settings()
    opts.update(kw)
    return Calibration(load_samples(paper_paths, trades_path), **opts)


def deal_ev(cal, deal, planned_raw=None, label="", index=None, depth=None, merch="m?"):
    """EV связки p2p (profit, buy Ad, sell Ad, маршрут); planned_raw — план без запаса на курс
    (p2p.profit_breakdown), нет — profit. None — калибровка не активна."""
    profit, b, s, route = deal
    plan = profit if planned_raw is None else planned_raw
    return cal.ev(plan, route_type(b.asset, s.asset, route), b.asset, b.ex, s.ex,
                  rel_bucket(label, index), depth_bucket(depth), merch)


def deal_inputs(deal, cfg, snap):
    """Признаки связки скана для EV — те же, что круг прогона пишет на старте: план без запаса на курс (_raw_plan по
    cfg.risk_buffer), индекс надёжности (p2p.reliability_index), запас глубины на сумму круга cfg.amount
    (paper.depth_margin; выход маршрута — объём стека продажи s.avail, _match собирает его без запаса) и корзина
    мерчантов (слабейший из двух)."""
    profit, b, s, _route = deal
    return {"planned_raw": _raw_plan(profit, b.asset, s.asset, cfg.risk_buffer),
            "index": p2p.reliability_index(deal, cfg, snap),
            "depth": paper.depth_margin(snap, b, s, cfg.amount, s.avail),
            "merch": merchant_bucket(min(b.orders, s.orders), min(b.rate, s.rate))}


def deal_estimate(cal, deal, cfg, snap):
    """Calibration.estimate для связки скана (deal_inputs); калибровка не активна — None."""
    if not cal.active:
        return None
    _profit, b, s, route = deal
    f = deal_inputs(deal, cfg, snap)
    return cal.estimate(f["planned_raw"], route_type(b.asset, s.asset, route), b.asset, b.ex, s.ex,
                        rel_bucket("", f["index"]), depth_bucket(f["depth"]), f["merch"])


def _deal_key(deal):
    _, b, s, _ = deal
    return b.ex, b.asset, s.ex, s.asset


def rank_snapshot(cal, snap, cfg):
    """EV_RANK=1: связки снимка по убыванию EV (равные — в прежнем порядке p2p.score), snap.ev[(ex, монета, ex,
    монета)] = (EV п.п., p) — для карточки. Набор связок тот же, меняется только порядок; новые список и словарь
    присваиваются целиком. Калибровка не активна — снимок не трогаем."""
    if not cal.active or not snap.deals:
        return snap
    scored, ev = [], {}
    for i, d in enumerate(snap.deals):
        e = deal_estimate(cal, d, cfg, snap)
        ev[_deal_key(d)] = (e["ev"], e["p"])
        scored.append((-e["ev"], i, d))
    scored.sort(key=lambda t: (t[0], t[1]))
    snap.deals = [d for _, _, d in scored]
    snap.ev = ev
    return snap


def rank_deals(cal, deals, labels=None):
    """Связки по убыванию EV по одним меткам, без снимка (при равенстве — прежний порядок); калибровка не активна —
    порядок как был. labels — метки надёжности в том же порядке (p2p.reliability), нет — «—». Скан бот ранжирует
    rank_snapshot (с индексом, глубиной и мерчантами)."""
    deals = list(deals)
    if not cal.active:
        return deals
    labels = list(labels) if labels is not None else [""] * len(deals)
    scored = [(deal_ev(cal, d, label=lab), i, d) for i, (d, lab) in enumerate(zip(deals, labels))]
    return [d for _, _, d in sorted(scored, key=lambda t: (-t[0], t[1]))]


def usdt_rub_series(path=None):
    """Ряд ориентира USDT/RUB [(ts, цена)] из data/history.db (snap.ref, раз в 5 мин); нет базы — [].
    Ориентир один на запись, а строк в записи — по паре площадок: читаем только разные (ts, ref)."""
    rows = _read(path or history.DB_PATH, "history", ("ts", "ref"), "WHERE ref > 0 ORDER BY ts", distinct=True)
    return [(r["ts"], r["ref"]) for r in rows if r["ts"] is not None]


def coin_series(path=None, step=60.0, limit=FX_MAX_POINTS):
    """Ряды ориентиров монет к рублю {монета: [(ts, ₽)]} по снимкам сканов (data/snapshots.db, поле refs строки scans;
    пакеты объявлений не читаем) — только чтение. Снимки не чаще раза в step сек, не больше limit самых свежих. Нет
    базы или таблицы — {}; битый снимок пропускаем."""
    path = path or snapshots.DB_PATH
    if not os.path.exists(path):
        return {}
    try:
        con = _ro(path)
    except sqlite3.Error:
        return {}
    out = {}
    try:
        picked, last = [], None
        for sid, ts in con.execute("SELECT id, ts FROM scans ORDER BY ts"):
            if ts is not None and (last is None or ts - last >= step):
                picked.append(sid)
                last = ts
        picked = picked[-limit:] if limit else picked
        for i in range(0, len(picked), 500):
            part = picked[i:i + 500]
            for ts, blob in con.execute(f"SELECT ts, blob FROM scans WHERE id IN ({','.join('?' * len(part))})", part):
                try:
                    refs = json.loads(zlib.decompress(blob)).get("refs") or {}
                    items = list(refs.items())
                except (zlib.error, ValueError, TypeError, AttributeError):
                    continue
                for asset, price in items:
                    if isinstance(price, (int, float)) and price > 0:
                        out.setdefault(asset, []).append((ts, float(price)))
    except sqlite3.Error:
        return {}   # битая база или чужая схема — как пустая
    finally:
        con.close()
    for points in out.values():
        points.sort()
    return out


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


_HEDGE_COLS = ("buy_asset", "amount", "hedge_fees", "hedge_funding", "hedge_state")


def hedge_costs(path=None):
    """Стоимость бумажного хеджа закрытых кругов по монете круга (simperp.fact_cost_pct: комиссии + спред − фандинг,
    % суммы круга): {монета: {"n", "exp", "fact"}} — средние ожидаемой и фактической. Нет базы/колонок — {}."""
    acc = {}
    for r in _read(path or paper.DB_PATH, "cycles", _HEDGE_COLS, "WHERE hedge_state != ''"):
        try:
            st = json.loads(r["hedge_state"] or "{}")
        except ValueError:
            continue
        if not isinstance(st, dict) or st.get("status") != "closed":
            continue
        fact = simperp.fact_cost_pct(r, st)
        if fact is None:
            continue
        a = acc.setdefault(r["buy_asset"] or "?", [0, 0.0, 0.0])
        a[0] += 1
        a[1] += st.get("exp_cost_pct") or 0.0
        a[2] += fact
    return {asset: {"n": n, "exp": exp / n, "fact": fact / n} for asset, (n, exp, fact) in acc.items()}


def hedge_now(snap, amount=None):
    """Ожидаемая стоимость бумажного хеджа круга сейчас — simperp.choose по последним котировкам перпов в памяти (та
    же модель, что research/hedge_bt.py с комиссиями ×1), % суммы круга (PAPER_AMOUNT): {монета: (%, площадка)}. Нет
    снимка, курса или котировки — монеты нет; ни сети, ни базы."""
    out = {}
    if snap is None or not snap.ref:
        return out
    if amount is None:
        try:
            amount = paper.settings()["amount"]
        except ValueError:   # опечатка в PAPER_AMOUNT — сумма прогона по умолчанию
            amount = 10000.0
    for asset in simperp.settings()["assets"]:
        rub = snap.refs.get(asset)
        if not rub or rub <= 0 or not amount:
            continue
        try:
            plan, _note = simperp.choose(asset, amount / rub, amount, snap.ref, rub)
        except Exception:   # отчёт, а не расчёт круга: сбой хеджа — просто нет строки
            plan = None
        if plan:
            out[asset] = (plan["cost_pct"], plan["venue"])
    return out


def fx_rows(horizon, coins, usdt=(), risk=None, now=None, closed=None):
    """Запас на курс из данных по монетам: [{"asset", "p90", "pairs", "current", "now", "paper"}] — p90 |движения|
    к рублю за horizon сек (у USDT — по снимкам или по history.db, где пар больше), текущий RISK_BUFFER монеты и
    стоимость хеджа сейчас (hedge_now) и по закрытым кругам (hedge_costs) — для сравнения. Монета без ряда и без
    хеджа — мимо."""
    risk = p2p._fees(os.getenv("RISK_BUFFER", p2p.DEFAULT_RISK)) if risk is None else risk
    now, closed, coins = now or {}, closed or {}, coins or {}
    rows = []
    for asset in FX_ASSETS + tuple(sorted(set(coins) - set(FX_ASSETS))):
        cands = [coins.get(asset) or []] + ([list(usdt)] if asset == "USDT" else [])
        p90, pairs = max((moves_p90(s, horizon) for s in cands), key=lambda t: t[1])
        if not pairs and asset not in now and asset not in closed:
            continue
        rows.append({"asset": asset, "p90": p90, "pairs": pairs, "current": risk.get(asset, 0.0),
                     "now": now.get(asset), "paper": closed.get(asset)})
    return rows


def _pp(x):
    return f"{x:+.2f}"


def _fx_line(r):
    esc = html.escape
    head = (f"• {esc(r['asset'])}: p90 {r['p90']:.2f}% (пар {r['pairs']})" if r["p90"] is not None
            else f"• {esc(r['asset'])}: мало данных (пар {r['pairs']})")
    parts = [head, f"RISK_BUFFER {r['current']:g}%"]
    if r["now"]:
        parts.append(f"хедж сейчас {r['now'][0]:.2f}% ({esc(r['now'][1])})")
    if r["paper"]:
        parts.append(f"хедж по кругам {r['paper']['fact']:.2f}% (ждали {r['paper']['exp']:.2f}%, {r['paper']['n']})")
    return " · ".join(parts)


def report_text(cal=None, series=None, limit=10, ev_rank=None, snap=None, coins=None, hedges=None):
    """Отчёт /calibration (HTML) — только владельцу: круги, активность EV и EV_RANK, поправки по классам, p по
    корзинам, цена срыва и запас на курс из данных по монетам рядом с RISK_BUFFER и стоимостью хеджа. cal, series
    (USDT/RUB из history.db), coins (ряды монет по снимкам) и hedges ((сейчас, по кругам)) не переданы — по живым
    базам; стоимость хеджа сейчас — по снимку snap (последний скан бота). ev_rank — p2p.Config.ev_rank (None — не
    показывать)."""
    cal = build() if cal is None else cal
    esc = html.escape
    lines = ["📐 <b>Калибровка плана</b>",
             f"Кругов прогона: {cal.n} (исполнилось {cal.n_done}, сорвалось {cal.n - cal.n_done})"
             f" · фактов сделок: {cal.n_facts}"]
    if cal.active:
        lines.append(f"✅ EV активна: кругов ≥ {cal.min_n} (CAL_MIN_N)")
    else:
        lines.append(f"⏳ EV не активна: {cal.n} из {cal.min_n} кругов (CAL_MIN_N)")
    if ev_rank:
        lines.append("📊 EV_RANK=1: /top, /best и сигналы — по убыванию EV, в карточке «EV … (p=…)»"
                     + ("" if cal.active else " — пока калибровка не активна, порядок прежний"))
    elif ev_rank is not None:
        lines.append("EV_RANK=0: порядок связок прежний (оценка с RISK_PENALTY), EV — только здесь")
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
    horizon = cal.median_duration
    if horizon:
        coins = coin_series(step=max(30.0, horizon / 3)) if coins is None else coins
        series = usdt_rub_series() if series is None else series
        now, closed = (hedge_now(snap), hedge_costs()) if hedges is None else hedges
        rows = fx_rows(horizon, coins, series, now=now, closed=closed)
        if rows:
            lines += ["", f"<b>Запас на курс из данных</b> — p90 движения к ₽ за медианное время круга "
                          f"({horizon / 60:.0f} мин) против RISK_BUFFER и стоимости хеджа, % круга; только сравнение, "
                          f"в расчёт не подключено:"]
            lines += [_fx_line(r) for r in rows]
    lines += ["", f"Поправка = n/(n+k)·среднее класса + k/(n+k)·соседние классы (k = {cal.k:g}, CAL_SHRINK_K); "
                  f"p — Лаплас, корзина < {cal.min_bucket} кругов → грубее. "
                  f"EV = p × (план без запаса + поправка) − (1 − p) × цена срыва."]
    return "\n".join(lines)
