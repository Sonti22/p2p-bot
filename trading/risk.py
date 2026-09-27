"""Лимиты риска торгового ядра — таблица «Лимиты риска» плана; основной аккаунт владельца, поэтому консервативно.

- Всё в Decimal. Любая ошибка проверки (нет данных, мусор, исключение) — запрет открытия (fail closed).
- Жёсткие потолки — в коде (HARD); переменные .env (ENV_CAPS) могут их только понизить: значение выше потолка —
  потолок, мусор или отрицательное — 0 (то есть открытий нет).
- Режим minlot (решение владельца 2026-09-27: «кнопка с минимальным лотом» сразу после хорошего бэктеста): ≤ 50 USDT
  на позицию, ≤ 5 USDT дневного убытка, плечо ≤ 2, у направленной — только изолированная маржа.
- Режим маржи берётся с биржи (ctx.margin_mode: venues.margin_mode), не из запроса; не узнали — отказ. Ключи — на
  ОСНОВНОМ аккаунте владельца: при кросс-марже ликвидация рискует всеми средствами аккаунта. Поэтому: направленная —
  только изолированная; хедж/фандинг под кросс-маржой — только в minlot и только со стопом, если худший убыток по стопу
  (с запасом на проскальзывание и комиссию) укладывается в остаток дневного лимита minlot; иначе — только изолированная.
- Дневной стоп — по дню МСК: реализованный результат дня + нереализованный убыток ≥ лимита → новые открытия стоп.
- Закрытие и уменьшение позиции (`check_close`) не блокируются ни дневным стопом, ни unknown, ни выключателем.
"""
import os
from collections import namedtuple
from decimal import Decimal, InvalidOperation

D = Decimal
STRATEGIES = ("hedge", "funding", "directional")
LIVE_MODES = ("minlot", "confirm", "auto")
CATEGORIES = ("linear", "spot", "swap")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "TONUSDT")

HARD = {   # потолки плана; менять — только вручную, мерж после проверки владельцем
    "leverage": {"hedge": D(3), "funding": D(3), "directional": D(2)},        # потолок (по умолчанию 2×)
    "position_usdt": {"hedge": D(1000), "funding": D(1000), "directional": D(200)},
    "total_usdt": D(2000),                     # суммарно открыто
    "daily_loss_usdt": D(50),                  # или 2% капитала — что меньше
    "daily_loss_share": D("0.02"),
    "max_groups": {"hedge": 3, "funding": 2, "directional": 1},   # хеджей / пар фандинга / направленных
    "liq_open": {"hedge": D("0.30"), "funding": D("0.30"), "directional": D("0.15")},   # запас до ликвидации при открытии
    "liq_alert": D("0.20"),                    # тревога
    "liq_reduce": D("0.12"),                   # сокращать
    "limit_band": D("0.005"),                  # лимитка — не дальше ±0.5% от mark
    "depth_band": D("0.001"),                  # рынок — только если глубина в пределах 0.1% покрывает объём
    "orders_per_min": D(10),
    "orders_per_day": D(100),
    "ticker_age_s": D(3),
    "clock_skew_s": D(1),
    "hedge_qty_ratio": D("1.05"),              # хедж ≤ монета круга × 1.05
    "mmr": D("0.01"),                          # консервативная ставка поддерживающей маржи для оценки ликвидации
}
DEFAULT_LEVERAGE = D(2)
MINLOT = {"position_usdt": D(50), "daily_loss_usdt": D(5), "leverage": D(2)}
ENV_CAPS = {   # переменная .env -> ключ потолка; только понижает
    "TRADING_MAX_LEVERAGE": "leverage",
    "TRADING_MAX_POSITION_USDT": "position_usdt",
    "TRADING_MAX_TOTAL_USDT": "total_usdt",
    "TRADING_DAILY_LOSS_USDT": "daily_loss_usdt",
    "TRADING_MAX_ORDERS_PER_MIN": "orders_per_min",
    "TRADING_MAX_ORDERS_PER_DAY": "orders_per_day",
    "TRADING_MINLOT_POSITION_USDT": "minlot_position_usdt",
    "TRADING_MINLOT_DAILY_LOSS_USDT": "minlot_daily_loss_usdt",
}

Verdict = namedtuple("Verdict", "ok reasons notional notes")   # notes — предупреждения (режим маржи и т. п.)
Verdict.__new__.__defaults__ = ((),)
STOP_SLIPPAGE = D("1.5")      # худший убыток по стопу = расстояние до стопа × 1.5 (проскальзывание, гэп)
WORST_FEES = D("0.002")       # + комиссии входа и выхода тейкером с запасом, доля номинала
CROSS_NOTE = ("кросс-маржа на основном аккаунте: при ликвидации рискуют все средства аккаунта, не только эта "
              "позиция")


def _env_cap(name, hard, environ=None):
    """min(потолок, значение .env). Нет переменной — потолок; мусор, бесконечность или < 0 — 0 (fail closed)."""
    raw = (environ if environ is not None else os.environ).get(name)
    if raw is None or not str(raw).strip():
        return hard
    try:
        v = D(str(raw).strip())
    except InvalidOperation:
        return D(0)
    if not v.is_finite() or v < 0:
        return D(0)
    return min(hard, v)


def limits(strategy, mode, environ=None):
    """Действующие лимиты стратегии в режиме: потолки плана, понижения .env, потолки minlot."""
    lev = min(HARD["leverage"][strategy], _env_cap("TRADING_MAX_LEVERAGE", HARD["leverage"][strategy], environ))
    pos = _env_cap("TRADING_MAX_POSITION_USDT", HARD["position_usdt"][strategy], environ)
    loss = _env_cap("TRADING_DAILY_LOSS_USDT", HARD["daily_loss_usdt"], environ)
    if mode == "minlot":
        lev = min(lev, MINLOT["leverage"])
        pos = min(pos, _env_cap("TRADING_MINLOT_POSITION_USDT", MINLOT["position_usdt"], environ))
        loss = min(loss, _env_cap("TRADING_MINLOT_DAILY_LOSS_USDT", MINLOT["daily_loss_usdt"], environ))
    return {"leverage": lev, "position_usdt": pos, "daily_loss_usdt": loss,
            "total_usdt": _env_cap("TRADING_MAX_TOTAL_USDT", HARD["total_usdt"], environ),
            "orders_per_min": _env_cap("TRADING_MAX_ORDERS_PER_MIN", HARD["orders_per_min"], environ),
            "orders_per_day": _env_cap("TRADING_MAX_ORDERS_PER_DAY", HARD["orders_per_day"], environ),
            "max_groups": HARD["max_groups"][strategy], "liq_open": HARD["liq_open"][strategy]}


def _d(v, name):
    """Decimal из Decimal/int/строки; float, bool, None, мусор — ValueError (проверка упадёт в отказ)."""
    if isinstance(v, (bool, float)) or v is None:
        raise ValueError(f"{name}: нет значения или не Decimal")
    try:
        d = D(v) if isinstance(v, (D, int)) else D(str(v).strip())
    except InvalidOperation:
        raise ValueError(f"{name}: не число") from None
    if not d.is_finite():
        raise ValueError(f"{name}: не число")
    return d


def _t(v, name):
    """Время/секунды (time.time() — float): число → Decimal; None, bool, мусор, бесконечность — ValueError."""
    if isinstance(v, float) and v == v and v not in (float("inf"), float("-inf")):
        return D(repr(v))
    return _d(v, name)


OpenRequest = namedtuple("OpenRequest", "strategy venue category symbol side order_type qty price leverage "
                                        "stop_loss group hedge_ref_qty")
# по умолчанию: price, leverage, stop_loss, group, hedge_ref_qty
OpenRequest.__new__.__defaults__ = (None, DEFAULT_LEVERAGE, None, None, None)

Context = namedtuple("Context", "now mark ticker_ts clock_skew book est_liq positions realized_today unrealized "
                                "capital unknown orders_min orders_day margin_mode")
Context.__new__.__defaults__ = (None,)   # margin_mode: "isolated" | "cross" | None (не узнали — отказ)


def liq_distance(side, entry, leverage, est_liq=None, mmr=None):
    """Доля цены до ликвидации от входа. est_liq — оценка биржи, если есть; иначе изолированная маржа:
    long ≈ вход × (1 − 1/плечо + mmr), short ≈ вход × (1 + 1/плечо − mmr). Берётся меньший (худший) запас."""
    mmr = HARD["mmr"] if mmr is None else mmr
    formula = D(1) / leverage - mmr
    if est_liq is None:
        return formula
    actual = abs(entry - est_liq) / entry
    wrong_side = (side == "buy" and est_liq >= entry) or (side == "sell" and est_liq <= entry)
    return D(0) if wrong_side else min(actual, formula)


def _depth_ok(side, qty, mark, book, band):
    """Глубина стакана со стороны исполнения в пределах band от mark покрывает qty. book — [(цена, объём)] асков для
    покупки, бидов для продажи."""
    if not book:
        return False
    edge = mark * (1 + band) if side == "buy" else mark * (1 - band)
    total = D(0)
    for price, size in book:
        p, q = _d(price, "стакан"), _d(size, "стакан")
        if (side == "buy" and p <= edge) or (side == "sell" and p >= edge):
            total += q
    return total >= qty


def daily_loss(ctx):
    """Убыток дня МСК: −(реализованный + нереализованный убыток); прибыль не уменьшает счёт ниже нуля."""
    realized, unrealized = _d(ctx.realized_today, "результат дня"), _d(ctx.unrealized, "нереализованный")
    return max(D(0), -(realized + min(unrealized, D(0))))


def daily_limit(ctx, lim):
    """Лимит дневного убытка: min(лимит режима, 2% капитала); капитала нет — ValueError."""
    capital = _d(ctx.capital, "капитал")
    if capital <= 0:
        raise ValueError("капитал неизвестен")
    return min(lim["daily_loss_usdt"], capital * HARD["daily_loss_share"])


def check_open(req, ctx, mode, environ=None):
    """Можно ли открыть позицию: Verdict(ok, причины, номинал в USDT). Все проверки сразу — причины списком."""
    try:
        return _check_open(req, ctx, mode, environ)
    except Exception as e:   # noqa: BLE001 — любой сбой проверки — запрет
        return Verdict(False, (f"ошибка проверки риска: {type(e).__name__}: {e}"[:200],), None)


def worst_stop_loss(side, qty, entry, stop):
    """Худший убыток позиции со стопом: qty × |вход − стоп| × STOP_SLIPPAGE + номинал × WORST_FEES. Стоп не с той
    стороны — None (убыток не ограничен)."""
    if (side == "buy" and not 0 < stop < entry) or (side == "sell" and stop <= entry):
        return None
    return qty * abs(entry - stop) * STOP_SLIPPAGE + qty * entry * WORST_FEES


def _check_open(req, ctx, mode, environ):
    bad = []
    if mode not in LIVE_MODES:
        return Verdict(False, (f"режим {mode}: реальных открытий нет",), None)
    if req.strategy not in STRATEGIES:
        return Verdict(False, (f"стратегия {req.strategy!r} неизвестна",), None)
    if req.symbol not in SYMBOLS or req.category not in CATEGORIES or req.side not in ("buy", "sell") \
            or req.order_type not in ("market", "limit"):
        return Verdict(False, ("символ/категория/сторона/тип вне списка",), None)
    lim = limits(req.strategy, mode, environ)
    qty, mark = _d(req.qty, "количество"), _d(ctx.mark, "mark")
    if qty <= 0 or mark <= 0:
        return Verdict(False, ("количество и mark должны быть > 0",), None)
    price = _d(req.price, "цена") if req.order_type == "limit" else None
    if price is not None and price <= 0:
        return Verdict(False, ("цена должна быть > 0",), None)
    entry = price if price is not None else mark
    notional = qty * entry

    # данные и блокировки
    if int(ctx.unknown) > 0:
        bad.append(f"есть ордера с неясным исходом ({int(ctx.unknown)}) — новые открытия запрещены")
    now, ts = _t(ctx.now, "время"), _t(ctx.ticker_ts, "время тикера")
    age = now - ts
    if age > HARD["ticker_age_s"] or age < -HARD["clock_skew_s"]:
        bad.append(f"тикер устарел: {age} с (≤ {HARD['ticker_age_s']} с)")
    if abs(_t(ctx.clock_skew, "расхождение часов")) > HARD["clock_skew_s"]:
        bad.append(f"часы расходятся с биржей больше {HARD['clock_skew_s']} с")
    if _d(ctx.orders_min, "ордеров за минуту") + 1 > lim["orders_per_min"]:
        bad.append(f"лимит ордеров в минуту: {lim['orders_per_min']}")
    if _d(ctx.orders_day, "ордеров за день") + 1 > lim["orders_per_day"]:
        bad.append(f"лимит ордеров в день: {lim['orders_per_day']}")
    loss, limit = daily_loss(ctx), daily_limit(ctx, lim)
    if loss >= limit:
        bad.append(f"дневной стоп: убыток {loss} USDT ≥ {limit} USDT (день МСК)")

    # цена и глубина
    if price is not None:
        if abs(price - mark) / mark > HARD["limit_band"]:
            bad.append(f"лимитка дальше ±{HARD['limit_band'] * 100}% от mark")
    elif not _depth_ok(req.side, qty, mark, ctx.book, HARD["depth_band"]):
        bad.append(f"рынок: глубины в пределах {HARD['depth_band'] * 100}% от mark не хватает на объём")

    # размер
    if notional > lim["position_usdt"]:
        bad.append(f"позиция {notional:.2f} USDT > {lim['position_usdt']} USDT")
    open_total = sum((_d(p["notional"], "номинал позиции") for p in ctx.positions or ()), D(0))
    if open_total + notional > lim["total_usdt"]:
        bad.append(f"суммарно открыто {open_total + notional:.2f} USDT > {lim['total_usdt']} USDT")
    groups = {p.get("group") or id(p) for p in ctx.positions or () if p.get("strategy") == req.strategy}
    if (req.group is None or req.group not in groups) and len(groups) + 1 > lim["max_groups"]:
        bad.append(f"уже открыто {len(groups)} ({req.strategy}), предел {lim['max_groups']}")

    # плечо, маржа, ликвидация, стоп
    if req.category == "spot":
        if req.side != "buy" or req.strategy != "funding":
            bad.append("спот — только покупка ноги фандинга")
    else:
        lev = _d(req.leverage, "плечо")
        if lev < 1 or lev > lim["leverage"]:
            bad.append(f"плечо {lev} вне 1..{lim['leverage']}")
        else:
            est = None if ctx.est_liq is None else _d(ctx.est_liq, "цена ликвидации")
            dist = liq_distance(req.side, entry, lev, est)
            if dist < lim["liq_open"]:
                bad.append(f"запас до ликвидации {dist:.3f} < {lim['liq_open']}")
    notes = []
    if req.category != "spot":
        mm = ctx.margin_mode
        if mm not in ("isolated", "cross"):
            bad.append(f"режим маржи неизвестен ({mm!r}) — открытие запрещено")
        elif mm == "cross":
            notes.append(CROSS_NOTE)
            if req.strategy == "directional":
                bad.append("направленная — только изолированная маржа")
            elif mode != "minlot":
                bad.append("кросс-маржа основного аккаунта — только в режиме minlot; иначе нужна изолированная")
            else:
                worst = None if req.stop_loss is None else worst_stop_loss(req.side, qty, entry,
                                                                           _d(req.stop_loss, "стоп"))
                room = limit - loss
                if worst is None:
                    bad.append("кросс-маржа: нужен стоп с правильной стороны — иначе убыток не ограничен")
                elif worst > room:
                    bad.append(f"кросс-маржа: худший убыток по стопу {worst:.2f} USDT > остатка дневного лимита "
                               f"{room:.2f} USDT")
    if req.strategy == "directional":
        if req.stop_loss is None:
            bad.append("направленная — стоп обязателен")
        else:
            stop = _d(req.stop_loss, "стоп")
            if (req.side == "buy" and not 0 < stop < entry) or (req.side == "sell" and stop <= entry):
                bad.append("стоп не с той стороны от входа")
            elif req.category != "spot" and ctx.est_liq is not None:
                liq = _d(ctx.est_liq, "цена ликвидации")
                if (req.side == "buy" and stop <= liq) or (req.side == "sell" and stop >= liq):
                    bad.append("стоп за ценой ликвидации")
    if req.strategy == "hedge":
        if req.side != "sell" or req.category == "spot":
            bad.append("хедж — только шорт перпа")
        if req.hedge_ref_qty is None:
            bad.append("хедж без размера круга")
        elif qty > _d(req.hedge_ref_qty, "монета круга") * HARD["hedge_qty_ratio"]:
            bad.append(f"хедж больше монеты круга × {HARD['hedge_qty_ratio']}")
    return Verdict(not bad, tuple(bad), notional, tuple(notes))


def check_spot_sell(qty, bot_bought):
    """Продажа на споте: не больше того, что купил сам бот (journal.spot_inventory) — монеты владельца не трогаем."""
    try:
        q, held = _d(qty, "количество"), _d(bot_bought, "куплено ботом")
    except ValueError as e:
        return Verdict(False, (str(e),), None)
    if q <= 0 or q > held:
        return Verdict(False, (f"продать можно только купленное ботом: {held}, в ордере {q}",), None)
    return Verdict(True, (), None)


def check_close(qty, position_size, reducing):
    """Закрытие/уменьшение: только уменьшающий ордер и не больше позиции. Дневной стоп, unknown и выключатель его
    не блокируют (сопровождение и закрытие работают всегда)."""
    try:
        q, size = _d(qty, "количество"), _d(position_size, "позиция")
    except ValueError as e:
        return Verdict(False, (str(e),), None)
    if not reducing:
        return Verdict(False, ("закрытие — только уменьшающим ордером",), None)
    if q <= 0 or q > size:
        return Verdict(False, (f"количество {q} вне (0, {size}]",), None)
    return Verdict(True, (), None)


def position_actions(positions):
    """Открытые позиции → [(позиция, действие, причина)]: "reduce" при запасе до ликвидации < 12%, "alert" < 20% или
    если цены ликвидации нет / данные битые (не угадываем)."""
    out = []
    for p in positions or ():
        try:
            mark, liq = _d(p.get("mark"), "mark"), p.get("liq")
            if liq is None:
                out.append((p, "alert", "нет цены ликвидации"))
                continue
            dist = abs(mark - _d(liq, "ликвидация")) / mark
        except (ValueError, ArithmeticError) as e:
            out.append((p, "alert", f"данные позиции: {e}"))
            continue
        if dist < HARD["liq_reduce"]:
            out.append((p, "reduce", f"до ликвидации {dist:.3f} < {HARD['liq_reduce']}"))
        elif dist < HARD["liq_alert"]:
            out.append((p, "alert", f"до ликвидации {dist:.3f} < {HARD['liq_alert']}"))
    return out
