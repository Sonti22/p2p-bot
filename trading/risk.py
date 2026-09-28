"""Лимиты риска торгового ядра — таблица «Лимиты риска» плана; основной аккаунт владельца, поэтому консервативно.

- Всё в Decimal. Любая ошибка проверки (нет данных, мусор, исключение) — запрет открытия (fail closed).
- Жёсткие потолки — в коде (HARD); переменные .env (ENV_CAPS) могут их только понизить: значение выше потолка —
  потолок, мусор или отрицательное — 0 (то есть открытий нет).
- Режим minlot (решение владельца 2026-09-27: «кнопка с минимальным лотом» сразу после хорошего бэктеста): ≤ 50 USDT
  на позицию, ≤ 5 USDT дневного убытка, плечо ≤ 2, у направленной — только изолированная маржа.
- Чужое не трогаем (решение владельца: ключи на ОСНОВНОМ аккаунте): по символу с ручной позицией или ордером владельца
  (ctx.foreign — trading/ownership.py) открытий нет; внутри бота — ни встречной стороны, ни второго ключа со стопом на
  одном символе биржи.
- Размер — по ИТОГОВОЙ позиции: открытое + ожидающие ордера на открытие (ctx.positions — journal.exposure()) + этот
  ордер; на ногу (биржа, символ, стратегия), суммарно (по свежим ценам), хедж ≤ монета круга × 1.05 — по всей группе.
  Группы считаются так, чтобы меткой их не обойти: направленная — по ноге (биржа, символ), метка не в счёт, и весь
  номинал стратегии ≤ потолка позиции; метка хеджа или фандинга — только одной монеты.
- Плечо и режим маржи — с биржи (ctx.leverage, ctx.margin_mode), не из запроса; не узнали или плечо выше потолка —
  отказ. Направленная — только изолированная; хедж/фандинг под кросс-маржой — только в minlot, со стопом, и без чужих
  позиций на аккаунте (ctx.foreign_account).
- Худший убыток по стопам — по ИТОГОВОЙ позиции ключа (стратегия, группа): уже открытое + ожидающее + этот ордер по
  среднему входу, со стопом ордера (он встанет на всю позицию ключа), плюс худшие убытки всех остальных стопов бота —
  вместе не больше остатка дневного лимита (направленная и кросс).
- Направленная: стоп обязателен, срабатывает раньше ликвидации (изолированная формула и оценка биржи).
- Смена стопа (`check_stop_move`) — только к входу, раньше ликвидации; смена плеча (`check_leverage_change`) — не выше
  потолка стратегий на символе, запас до ликвидации и стоп раньше новой ликвидации.
- Количество, цена и стоп — по шагам инструмента (ctx.instrument, `quantize`): размер только вниз, стоп — к цене
  входа (никогда не за неё), минимум количества и номинала биржи.
- Дневной стоп — по дню МСК: реализованный результат дня + нереализованный убыток ≥ лимита → новые открытия стоп.
- Закрытие и уменьшение (`check_close`) — только своей позиции бота (по журналу) и не больше её; дневной стоп, unknown
  и выключатель его не блокируют.
"""
import os
from collections import namedtuple
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

D = Decimal
STRATEGIES = ("hedge", "funding", "directional")
LIVE_MODES = ("minlot", "confirm", "auto")
CATEGORIES = ("linear", "spot", "swap")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "TONUSDT")
FOREIGN = "по символу есть ваша ручная позиция/ордер — бот не трогает"

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
                                "capital unknown orders_min orders_day margin_mode leverage instrument foreign "
                                "foreign_account")
# margin_mode "isolated" | "cross" | None; leverage — фактическое плечо символа с биржи; instrument — venues.Instrument;
# foreign — чужое по символу (ownership.foreign: () — ничего), foreign_account — чужие позиции аккаунта (для кросс);
# None — не узнали: открытие запрещено
Context.__new__.__defaults__ = (None, None, None, None, None)


# --- шаги инструмента ---

def _floor(v, step):
    return (v / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(v, step):
    return (v / step).to_integral_value(rounding=ROUND_CEILING) * step


def _on_step(v, step):
    return v % step == 0


def quantize(req, inst, mark):
    """Запрос → (запрос по шагам инструмента, причины отказа). Количество — вниз к шагу (никогда вверх: лимиты не
    превысить); цена лимитки — покупка вниз, продажа вверх (не хуже задуманного); стоп — к цене входа (long — вверх,
    short — вниз: убыток по стопу не больше задуманного) и после округления строго с правильной стороны. Меньше
    минимального количества или номинала биржи, больше максимума — причина."""
    try:
        qty, mark = _d(req.qty, "количество"), _d(mark, "mark")
        step, tick = _d(inst.qty_step, "шаг количества"), _d(inst.tick, "шаг цены")
        if step <= 0 or tick <= 0 or mark <= 0:
            return req, ("шаги инструмента и mark должны быть > 0",)
        qty = _floor(qty, step)
        price = None
        if req.order_type == "limit":
            p = _d(req.price, "цена")
            price = _floor(p, tick) if req.side == "buy" else _ceil(p, tick)
        entry = price if price is not None else mark
        stop = None
        if req.stop_loss is not None:
            s = _d(req.stop_loss, "стоп")
            stop = _ceil(s, tick) if req.side == "buy" else _floor(s, tick)
    except (ValueError, ArithmeticError, AttributeError) as e:
        return req, (f"округление по шагам: {e}",)
    out = req._replace(qty=qty, price=price, stop_loss=stop)
    return out, tuple(_instrument_problems(out.side, qty, price, stop, entry, inst, quantized=True))


def _instrument_problems(side, qty, price, stop, entry, inst, quantized=False):
    """Причины, по которым ордер не проходит шаги/минимумы инструмента (пусто — проходит)."""
    bad = []
    step, tick = _d(inst.qty_step, "шаг количества"), _d(inst.tick, "шаг цены")
    if not quantized:
        if not _on_step(qty, step):
            bad.append(f"количество {qty} не кратно шагу {step} — сначала risk.quantize")
        if price is not None and not _on_step(price, tick):
            bad.append(f"цена {price} не кратна шагу {tick} — сначала risk.quantize")
        if stop is not None and not _on_step(stop, tick):
            bad.append(f"стоп {stop} не кратен шагу {tick} — сначала risk.quantize")
    if qty < _d(inst.min_qty, "мин. количество"):
        bad.append(f"количество {qty} меньше минимума биржи {inst.min_qty}")
    if inst.max_qty is not None and qty > _d(inst.max_qty, "макс. количество"):
        bad.append(f"количество {qty} больше максимума биржи {inst.max_qty}")
    if qty * entry < _d(inst.min_notional, "мин. номинал"):
        bad.append(f"номинал {qty * entry:.2f} USDT меньше минимума биржи {inst.min_notional}")
    if stop is not None and ((side == "buy" and not 0 < stop < entry) or (side == "sell" and stop <= entry)):
        bad.append("стоп после округления не с той стороны от входа")
    return bad


# --- ликвидация, стоп, дневной лимит ---

def isolated_liq(side, entry, leverage, mmr=None):
    """Цена ликвидации по изолированной формуле: long ≈ вход × (1 − 1/плечо + mmr), short ≈ вход × (1 + 1/плечо − mmr)."""
    mmr = HARD["mmr"] if mmr is None else mmr
    k = D(1) / leverage - mmr
    return entry * (1 - k) if side == "buy" else entry * (1 + k)


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


def worst_stop_loss(side, qty, entry, stop):
    """Худший убыток позиции со стопом: qty × |вход − стоп| × STOP_SLIPPAGE + номинал × WORST_FEES. Стоп не с той
    стороны — None (убыток не ограничен)."""
    if (side == "buy" and not 0 < stop < entry) or (side == "sell" and stop <= entry):
        return None
    return qty * abs(entry - stop) * STOP_SLIPPAGE + qty * entry * WORST_FEES


def key_worst(side, qty, entry, stop):
    """Худший убыток позиции бота со стопом (side — сторона входа: buy — лонг): qty × max(вход − стоп, 0) ×
    STOP_SLIPPAGE + номинал × WORST_FEES; стоп за входом (в прибыли) — только комиссии."""
    loss = (entry - stop) if side == "buy" else (stop - entry)
    return qty * max(loss, D(0)) * STOP_SLIPPAGE + qty * entry * WORST_FEES


def _stop_before_liq(side, stop, entry, leverage, est_liq):
    """Стоп срабатывает раньше ликвидации: строго между ценой ликвидации (изолированная формула и оценка биржи — худшая)
    и входом."""
    liqs = [isolated_liq(side, entry, leverage)] + ([est_liq] if est_liq is not None else [])
    return stop > max(liqs) if side == "buy" else stop < min(liqs)


# --- позиции бота: итоговый размер, группы, встречные стороны ---

def _gid(venue, symbol, group, strategy=None):
    """Группа позиции: хедж — круг, фандинг — пара ног; без группы — нога (биржа:символ). Направленная — всегда нога
    (биржа:символ): метка вызывающего не объединяет позиции разных символов в «одну направленную»."""
    if strategy == "directional" or group in (None, ""):
        return f"{venue}:{symbol}"
    return str(group)


def _key_of(p):
    """(биржа, перп?, символ, стратегия, группа) строки journal.exposure."""
    return (p.get("venue"), _perp(p.get("category")), p.get("symbol"), p.get("strategy"),
            _gid(p.get("venue"), p.get("symbol"), p.get("group"), p.get("strategy")))


def _row_entry(p):
    """Средний вход строки exposure (entry) или цена оценки (px)."""
    for k in ("entry", "px"):
        v = p.get(k)
        if v is not None:
            v = _d(v, "вход позиции бота")
            if v > 0:
                return v
    q = _d(p.get("qty"), "размер позиции бота")
    return _d(p.get("notional"), "номинал позиции") / q if q > 0 else D(0)


def _row_worst(p):
    """Худший убыток строки exposure со стопом (0 — стопа нет). Стоп есть, а цены его нет — весь номинал."""
    stop = p.get("stop_price")
    if stop is None:
        return _d(p.get("notional"), "номинал позиции") if p.get("stop") else D(0)
    side = "buy" if _row_side(p) == "long" else "sell"
    return key_worst(side, _d(p.get("qty"), "размер позиции бота"), _row_entry(p), _d(stop, "стоп позиции бота"))


def stop_budget(rows, venue, category, symbol, strategy, group, side, qty, entry, stop):
    """Худший убыток по стопам после открытия: (итоговая позиция ключа — уже открытое и ожидающее того же ключа + этот
    ордер по среднему входу, со стопом ордера или, если его нет, стопом ключа; остальные позиции бота со стопами).
    Итогового стопа нет — (None, остальные)."""
    me = (venue, _perp(category), symbol, strategy, _gid(venue, symbol, group, strategy))
    have_q, cost, key_stop, others = D(0), D(0), None, D(0)
    for p in rows or ():
        q = _d(p.get("qty"), "размер позиции бота")
        if q <= 0:
            continue
        if _key_of(p) == me:
            have_q += q
            cost += q * _row_entry(p)
            if p.get("stop_price") is not None:
                key_stop = _d(p.get("stop_price"), "стоп позиции бота")
        else:
            others += _row_worst(p)
    stop = stop if stop is not None else key_stop
    total = have_q + qty
    if stop is None or total <= 0:
        return None, others
    return key_worst(side, total, (cost + qty * entry) / total, stop), others


def _budget_problems(rows, key, category, stop, qty, entry, room, what):
    """Итоговая позиция ключа со стопом + все остальные стопы бота ≤ остатка дневного лимита. key — (биржа, символ,
    стратегия, группа, сторона)."""
    venue, symbol, strategy, group, side = key
    mine, others = stop_budget(rows, venue, category, symbol, strategy, group, side, qty, entry, stop)
    if mine is None:
        return []
    if mine + others > room:
        return [f"{what}: худший убыток по стопам {mine + others:.2f} USDT (итоговая позиция ключа {mine:.2f} + "
                f"остальные {others:.2f}) > остатка дневного лимита {room:.2f} USDT"]
    return []


def _combined_worst(rows, key, category, stop, qty, entry):
    """Для кросс-маржи: худший убыток итоговой позиции ключа + всех остальных стопов бота; стопа ордера нет или он не с
    той стороны — None. key — (биржа, символ, стратегия, группа, сторона)."""
    venue, symbol, strategy, group, side = key
    if stop is None or worst_stop_loss(side, qty, entry, stop) is None:
        return None
    mine, others = stop_budget(rows, venue, category, symbol, strategy, group, side, qty, entry, stop)
    return mine + others


def _row_side(p):
    side = p.get("side")
    if side not in ("long", "short"):
        raise ValueError(f"сторона позиции {side!r}")
    return side


def _perp(category):
    return category in ("linear", "swap")


def netting_conflicts(rows, venue, symbol, strategy, group, side, has_stop):
    """Причины не открывать перп из-за своих же перп-позиций бота на том же символе биржи (односторонний режим
    сальдирует; спот — отдельные монеты, не сальдируется): другая стратегия/группа со встречной стороной —
    сальдирование; с той же стороной, если у кого-то есть стоп, — два ключа со стопами на одной позиции символа
    (перекрывающиеся стопы: чей стоп закроет чью часть, биржа не знает)."""
    want = "long" if side == "buy" else "short"
    me = (strategy, _gid(venue, symbol, group, strategy))
    out = []
    for p in rows or ():
        if p.get("venue") != venue or p.get("symbol") != symbol or not _perp(p.get("category")):
            continue
        if (p.get("strategy"), _gid(p.get("venue"), p.get("symbol"), p.get("group"), p.get("strategy"))) == me:
            continue
        if _d(p.get("qty"), "размер позиции бота") <= 0:
            continue
        if _row_side(p) != want:
            out.append(f"встречная позиция бота на том же символе ({p.get('strategy')}) — сальдирование запрещено")
        elif p.get("stop") or has_stop:
            out.append(f"на символе уже есть позиция бота ({p.get('strategy')}): стоп действует на всю позицию символа "
                       f"— перекрывающиеся стопы запрещены")
    return out


def _size_problems(rows, venue, symbol, strategy, group, qty, notional, lim, hedge_ref_qty=None, need_ref=True,
                   category="linear"):
    """Итоговый размер: нога (биржа, спот/перп, символ, стратегия) и всё открытое — с позициями и ожидающими
    открытиями бота; число групп стратегии (направленная — по ногам биржа:символ, метка группы не в счёт; метка хеджа
    или фандинга — только одной монеты); направленная — ещё весь номинал стратегии ≤ потолка позиции; хедж — вся
    группа ≤ монета круга × 1.05."""
    bad = []
    rows = list(rows or ())
    leg = sum((_d(p.get("notional"), "номинал позиции") for p in rows if p.get("venue") == venue
               and p.get("symbol") == symbol and p.get("strategy") == strategy
               and _perp(p.get("category")) == _perp(category)), D(0)) + notional
    if leg > lim["position_usdt"]:
        bad.append(f"итоговая позиция {leg:.2f} USDT (с открытой и ожидающими ордерами) > {lim['position_usdt']} USDT")
    total = sum((_d(p.get("notional"), "номинал позиции") for p in rows), D(0)) + notional
    if total > lim["total_usdt"]:
        bad.append(f"суммарно открыто {total:.2f} USDT > {lim['total_usdt']} USDT")
    live = [p for p in rows if p.get("strategy") == strategy and _d(p.get("qty"), "размер позиции бота") > 0]
    if strategy == "directional":
        whole = sum((_d(p.get("notional"), "номинал позиции") for p in live), D(0)) + notional
        if whole > lim["position_usdt"]:
            bad.append(f"направленная: весь номинал стратегии {whole:.2f} USDT > {lim['position_usdt']} USDT")
    me = _gid(venue, symbol, group, strategy)
    groups = {_gid(p.get("venue"), p.get("symbol"), p.get("group"), strategy) for p in live}
    if me not in groups and len(groups) + 1 > lim["max_groups"]:
        bad.append(f"уже открыто {len(groups)} ({strategy}), предел {lim['max_groups']}")
    if strategy != "directional" and group not in (None, ""):
        coins = {p.get("symbol") for p in live if _gid(p.get("venue"), p.get("symbol"), p.get("group"), strategy) == me}
        if coins - {symbol}:
            bad.append(f"метка группы {group!r} уже у другой монеты ({', '.join(sorted(map(str, coins)))}) — группа "
                       f"хеджа/фандинга только одной монеты")
    if strategy == "hedge" and need_ref:
        if hedge_ref_qty is None:
            bad.append("хедж без размера круга")
        else:
            group_qty = sum((_d(p.get("qty"), "размер позиции бота") for p in rows if p.get("strategy") == "hedge"
                             and _gid(p.get("venue"), p.get("symbol"), p.get("group"), "hedge") == me), D(0)) + qty
            if group_qty > _d(hedge_ref_qty, "монета круга") * HARD["hedge_qty_ratio"]:
                bad.append(f"хедж группы {group_qty} больше монеты круга × {HARD['hedge_qty_ratio']}")
    return bad


def _margin_problems(strategy, mode, margin_mode, stop_worst, room, foreign_account):
    """Режим маржи: не узнали — отказ; направленная — только изолированная; кросс (общий залог основного аккаунта) —
    только хедж/фандинг в minlot, со стопом, и худший убыток итоговой позиции ключа вместе со всеми остальными стопами
    бота (stop_worst — _combined_worst) ≤ остатка дневного лимита, и без чужих позиций на аккаунте. → (причины,
    заметки)."""
    bad, notes = [], []
    if margin_mode not in ("isolated", "cross"):
        return [f"режим маржи неизвестен ({margin_mode!r}) — открытие запрещено"], notes
    if margin_mode == "isolated":
        return bad, notes
    notes.append(CROSS_NOTE)
    if strategy == "directional":
        bad.append("направленная — только изолированная маржа")
    elif mode != "minlot":
        bad.append("кросс-маржа основного аккаунта — только в режиме minlot; иначе нужна изолированная")
    else:
        if stop_worst is None:
            bad.append("кросс-маржа: нужен стоп с правильной стороны — иначе убыток не ограничен")
        elif stop_worst > room:
            bad.append(f"кросс-маржа: худший убыток по стопу {stop_worst:.2f} USDT > остатка дневного лимита "
                       f"{room:.2f} USDT")
        if foreign_account is None:
            bad.append("кросс-маржа: позиции аккаунта не проверены — открытие запрещено")
        elif foreign_account:
            bad.append("кросс-маржа: на аккаунте есть ваши позиции (общий залог) — только изолированная: "
                       + "; ".join(map(str, foreign_account))[:200])
    return bad, notes


def _foreign_problems(foreign):
    if foreign is None:
        return ["позиции и ордера символа на бирже не проверены — открытие запрещено"]
    if foreign:
        return [f"{FOREIGN}: " + "; ".join(map(str, foreign))[:300]]
    return []


# --- проверка открытия ---

def check_open(req, ctx, mode, environ=None):
    """Можно ли открыть позицию: Verdict(ok, причины, номинал в USDT). Все проверки сразу — причины списком."""
    try:
        return _check_open(req, ctx, mode, environ)
    except Exception as e:   # noqa: BLE001 — любой сбой проверки — запрет
        return Verdict(False, (f"ошибка проверки риска: {type(e).__name__}: {e}"[:200],), None)


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
    stop = None if req.stop_loss is None else _d(req.stop_loss, "стоп")
    perp = req.category != "spot"

    # чужое, данные и блокировки
    bad += _foreign_problems(ctx.foreign)
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
    room = limit - loss
    if loss >= limit:
        bad.append(f"дневной стоп: убыток {loss} USDT ≥ {limit} USDT (день МСК)")

    # шаги инструмента, цена и глубина
    if ctx.instrument is None:
        bad.append("нет шагов инструмента (instruments info) — ордер не собрать по правилам биржи")
    else:
        bad += _instrument_problems(req.side, qty, price, stop, entry, ctx.instrument)
    if price is not None:
        if abs(price - mark) / mark > HARD["limit_band"]:
            bad.append(f"лимитка дальше ±{HARD['limit_band'] * 100}% от mark")
    elif not _depth_ok(req.side, qty, mark, ctx.book, HARD["depth_band"]):
        bad.append(f"рынок: глубины в пределах {HARD['depth_band'] * 100}% от mark не хватает на объём")

    # итоговый размер, группы, свои встречные позиции и стопы
    bad += _size_problems(ctx.positions, req.venue, req.symbol, req.strategy, req.group, qty, notional, lim,
                          req.hedge_ref_qty, category=req.category)
    if perp:
        bad += netting_conflicts(ctx.positions, req.venue, req.symbol, req.strategy, req.group, req.side,
                                 stop is not None)

    # плечо (фактическое, с биржи), маржа, ликвидация, стоп
    notes = []
    lev = None
    if req.category == "spot":
        if req.side != "buy" or req.strategy != "funding":
            bad.append("спот — только покупка ноги фандинга")
    else:
        want = _d(req.leverage, "плечо")
        if want < 1 or want > lim["leverage"]:
            bad.append(f"плечо {want} вне 1..{lim['leverage']}")
        if ctx.leverage is None:
            bad.append("фактическое плечо символа не прочитано с биржи — открытие запрещено")
        else:
            lev = _d(ctx.leverage, "плечо символа")
            if lev < 1 or lev > lim["leverage"]:
                bad.append(f"плечо символа на бирже {lev} вне 1..{lim['leverage']}")
                lev = None
        if lev is not None:
            est = None if ctx.est_liq is None else _d(ctx.est_liq, "цена ликвидации")
            dist = liq_distance(req.side, entry, lev, est)
            if dist < lim["liq_open"]:
                bad.append(f"запас до ликвидации {dist:.3f} < {lim['liq_open']}")
        key = (req.venue, req.symbol, req.strategy, req.group, req.side)
        mb, notes = _margin_problems(req.strategy, mode, ctx.margin_mode,
                                     _combined_worst(ctx.positions, key, req.category, stop, qty, entry), room,
                                     ctx.foreign_account)
        bad += mb
    if req.strategy == "directional":
        if stop is None:
            bad.append("направленная — стоп обязателен")
        elif worst_stop_loss(req.side, qty, entry, stop) is None:
            bad.append("стоп не с той стороны от входа")
        else:
            bad += _budget_problems(ctx.positions, (req.venue, req.symbol, req.strategy, req.group, req.side),
                                    req.category, stop, qty, entry, room, "направленная")
            if perp and lev is not None:
                est = None if ctx.est_liq is None else _d(ctx.est_liq, "цена ликвидации")
                if not _stop_before_liq(req.side, stop, entry, lev, est):
                    bad.append("стоп за ценой ликвидации — сработает позже ликвидации")
    if req.strategy == "hedge" and (req.side != "sell" or req.category == "spot"):
        bad.append("хедж — только шорт перпа")
    return Verdict(not bad, tuple(bad), notional, tuple(notes))


def guard_open(order, strategy, group, mode, *, mark, instrument, leverage, margin_mode, foreign, foreign_account,
               exposure, realized_today, unrealized=None, capital=None, est_liq=None, environ=None):
    """Жёсткие проверки открытия внутри journal.submit (защита в глубину: не заменяют check_open вызывающего, а
    повторяют главное по свежим данным биржи): чужое по символу, свои встречные позиции и стопы, шаги инструмента,
    коридор лимитки ±0.5% от mark, итоговый размер (minlot — 50 USDT) по свежим ценам, дневной лимит (min(лимит режима,
    2% капитала); реализованный + нереализованный убыток), фактическое плечо, запас до ликвидации (est_liq — цена
    ликвидации позиции символа с биржи), режим маржи и кросс, худший убыток по стопам итоговой позиции ключа вместе со
    всеми остальными стопами бота, стоп направленной раньше ликвидации. Нет капитала или нереализованного — отказ.
    → причины."""
    try:
        return _guard_open(order, strategy, group, mode, mark, instrument, leverage, margin_mode, foreign,
                           foreign_account, exposure, realized_today, unrealized, capital, est_liq, environ)
    except Exception as e:   # noqa: BLE001
        return [f"ошибка проверки риска: {type(e).__name__}: {e}"[:200]]


def _guard_open(order, strategy, group, mode, mark, instrument, leverage, margin_mode, foreign, foreign_account,
                exposure, realized_today, unrealized, capital, est_liq, environ):
    if mode not in LIVE_MODES or strategy not in STRATEGIES:
        return [f"режим {mode!r} / стратегия {strategy!r}: открытие запрещено"]
    bad = _foreign_problems(foreign)
    lim = limits(strategy, mode, environ)
    qty = _d(order.qty, "количество")
    if mark is None or instrument is None:
        return bad + ["нет цены или шагов инструмента с биржи — открытие запрещено"]
    mark = _d(mark, "mark")
    price = _d(order.price, "цена") if order.order_type == "limit" else None
    entry = price if price is not None else mark
    stop = None if order.stop_loss is None else _d(order.stop_loss, "стоп")
    notional = qty * entry
    bad += _instrument_problems(order.side, qty, price, stop, entry, instrument)
    if price is not None and abs(price - mark) / mark > HARD["limit_band"]:
        bad.append(f"лимитка дальше ±{HARD['limit_band'] * 100}% от mark")
    bad += _size_problems(exposure, order.venue, order.symbol, strategy, group, qty, notional, lim,
                          need_ref=False, category=order.category)   # хедж ≤ монета круга × 1.05 — в check_open вызывающего (круг знает он)
    cap = _d(capital, "капитал")
    if cap <= 0:
        raise ValueError("капитал неизвестен")
    limit = min(lim["daily_loss_usdt"], cap * HARD["daily_loss_share"])
    loss = max(D(0), -(_d(realized_today, "результат дня") + min(_d(unrealized, "нереализованный"), D(0))))
    room = limit - loss
    if loss >= limit:
        bad.append(f"дневной стоп: убыток дня {loss:.2f} USDT ≥ {limit:.2f} USDT (реализованный + нереализованный)")
    if order.category == "spot":
        if order.side != "buy" or strategy != "funding":
            bad.append("спот — только покупка ноги фандинга")
        return bad
    bad += netting_conflicts(exposure, order.venue, order.symbol, strategy, group, order.side, stop is not None)
    lev = None
    if leverage is None:
        bad.append("фактическое плечо символа не прочитано с биржи — открытие запрещено")
    elif not 1 <= _d(leverage, "плечо символа") <= lim["leverage"]:
        bad.append(f"плечо символа на бирже {leverage} вне 1..{lim['leverage']}")
    else:
        lev = _d(leverage, "плечо символа")
    est = None if est_liq is None else _d(est_liq, "цена ликвидации")
    if lev is not None:
        dist = liq_distance(order.side, entry, lev, est)
        if dist < lim["liq_open"]:
            bad.append(f"запас до ликвидации {dist:.3f} < {lim['liq_open']}")
    key = (order.venue, order.symbol, strategy, group, order.side)
    bad += _margin_problems(strategy, mode, margin_mode,
                            _combined_worst(exposure, key, order.category, stop, qty, entry), room, foreign_account)[0]
    if strategy == "directional":
        if stop is None or worst_stop_loss(order.side, qty, entry, stop) is None:
            bad.append("направленная — нужен стоп с правильной стороны")
        else:
            bad += _budget_problems(exposure, key, order.category, stop, qty, entry, room, "направленная")
            if lev is not None and not _stop_before_liq(order.side, stop, entry, lev, est):
                bad.append("стоп за ценой ликвидации — сработает позже ликвидации")
    if strategy == "hedge" and order.side != "sell":
        bad.append("хедж — только шорт перпа")
    return bad


def check_stop_move(side, entry, stop, old_stop, mark, liq, leverage):
    """Новый стоп позиции бота (journal.set_stop); side — сторона входа позиции (buy — лонг): стоп с правильной стороны
    от mark и раньше ликвидации (оценка биржи и изолированная формула при фактическом плече); уже стоящий стоп двигать
    можно только к входу и дальше в прибыль (худший убыток только меньше того, что проверили при открытии); первый стоп
    позиции без стопа — только уменьшает риск. → причины."""
    try:
        bad = []
        stop, mark = _d(stop, "стоп"), _d(mark, "mark")
        if (side == "buy" and not 0 < stop < mark) or (side == "sell" and not stop > mark):
            bad.append("стоп не с той стороны от mark")
        liqs = [] if leverage is None else [isolated_liq(side, _d(entry, "вход"), _d(leverage, "плечо"))]
        liqs += [] if liq is None else [_d(liq, "цена ликвидации")]
        if not liqs:
            bad.append("ни цены ликвидации, ни плеча — стоп против ликвидации не проверить")
        elif (side == "buy" and stop <= max(liqs)) or (side == "sell" and stop >= min(liqs)):
            bad.append("стоп за ценой ликвидации — сработает позже ликвидации")
        if old_stop is not None:
            old = _d(old_stop, "стоп")
            if (side == "buy" and stop < old) or (side == "sell" and stop > old):
                bad.append("стоп позиции двигается только к входу (худший убыток не растёт)")
        return bad
    except Exception as e:   # noqa: BLE001
        return [f"ошибка проверки стопа: {type(e).__name__}: {e}"[:200]]


def check_leverage_change(keys, mode, leverage, environ=None):
    """Смена плеча символа (journal.set_leverage): keys — [(стратегия, сторона входа или None, вход, стоп или None)]
    ключей бота на символе (и стратегия будущего открытия — со стороной None). Плечо — не выше потолка каждой из
    стратегий в режиме; у позиции бота — запас до ликвидации при новом плече ≥ порога открытия и стоп раньше новой
    ликвидации. → причины."""
    try:
        lev = _d(leverage, "плечо")
        bad = []
        for strategy, side, entry, stop in keys:
            if strategy not in STRATEGIES:
                return [f"стратегия {strategy!r} неизвестна — плечо не меняем"]
            lim = limits(strategy, mode, environ)
            if not 1 <= lev <= lim["leverage"]:
                bad.append(f"плечо {lev} вне 1..{lim['leverage']} ({strategy}, {mode})")
                continue
            if side is None:
                continue
            e = _d(entry, "вход")
            if liq_distance(side, e, lev) < lim["liq_open"]:
                bad.append(f"{strategy}: запас до ликвидации при плече {lev} < {lim['liq_open']}")
            if stop is not None and not _stop_before_liq(side, _d(stop, "стоп"), e, lev, None):
                bad.append(f"{strategy}: стоп позиции за новой ценой ликвидации (плечо {lev})")
        return bad
    except Exception as e:   # noqa: BLE001
        return [f"ошибка проверки плеча: {type(e).__name__}: {e}"[:200]]


def check_spot_sell(qty, bot_bought):
    """Продажа на споте: не больше того, что купил сам бот (journal.spot_inventory) — монеты владельца не трогаем."""
    try:
        q, held = _d(qty, "количество"), _d(bot_bought, "куплено ботом")
    except ValueError as e:
        return Verdict(False, (str(e),), None)
    if q <= 0 or q > held:
        return Verdict(False, (f"продать можно только купленное ботом: {held}, в ордере {q}",), None)
    return Verdict(True, (), None)


def check_close(qty, bot_size, reducing):
    """Закрытие/уменьшение: только уменьшающий ордер и не больше СВОЕЙ позиции бота по журналу (bot_size — без уже
    отправленных закрытий; чужую часть позиции символа бот не трогает). Дневной стоп, unknown и выключатель его не
    блокируют (сопровождение и закрытие работают всегда)."""
    try:
        q, size = _d(qty, "количество"), _d(bot_size, "позиция бота")
    except ValueError as e:
        return Verdict(False, (str(e),), None)
    if not reducing:
        return Verdict(False, ("закрытие — только уменьшающим ордером",), None)
    if q <= 0 or q > size:
        return Verdict(False, (f"количество {q} вне (0, {size}] — не больше своей позиции бота",), None)
    return Verdict(True, (), None)


def _liq_dist(p):
    """Запас до ликвидации со стороны позиции: long — (mark − liq)/mark, short — (liq − mark)/mark; ValueError — нет
    данных."""
    side = p.get("side")
    if side not in ("long", "short"):
        raise ValueError(f"сторона {side!r}")
    mark, liq = _d(p.get("mark"), "mark"), p.get("liq")
    if liq is None:
        raise ValueError("нет цены ликвидации")
    liq = _d(liq, "ликвидация")
    return (mark - liq) / mark if side == "long" else (liq - mark) / mark


def position_actions(positions):
    """Позиции с биржи (journal.own_positions) → [(позиция, действие, причина)]: у позиции БОТА (owned=True — вся
    позиция символа бота) "reduce" при запасе до ликвидации < 12%, "alert" < 20%, если цены ликвидации нет, она не с
    той стороны от mark или данные битые (не угадываем); сокращать — не больше bot_size. Позиция, где у бота по журналу
    своя часть, а на бирже по символу другое (mismatch / sign_suspect — владелец добавил, позицию бота закрыла биржа,
    знак BingX не сходится), — всегда "alert" с запасом до ликвидации и причиной (без сокращения: чья там позиция, не
    доказано) — сопровождение молча не отключается. Позиции без доли бота — позиции владельца: их бот не трогает и не
    комментирует."""
    out = []
    for p in positions or ():
        if not isinstance(p, dict):
            continue
        if p.get("owned") is not True:
            if p.get("mismatch") is True or p.get("sign_suspect") is True:
                why = ("знак позиции BingX не сходится с журналом бота" if p.get("sign_suspect") is True else
                       f"позиция на бирже не совпадает с журналом бота (у бота {p.get('bot_net')})")
                try:
                    why += f"; до ликвидации {_liq_dist(p):.3f}"
                except (ValueError, ArithmeticError) as e:
                    why += f"; запас до ликвидации не посчитать: {e}"
                out.append((p, "alert", why + " — бот не сокращает, проверьте вручную"))
            continue
        try:
            if p.get("liq") is None:
                _d(p.get("mark"), "mark")
                if p.get("side") not in ("long", "short"):
                    raise ValueError(f"сторона {p.get('side')!r}")
                out.append((p, "alert", "нет цены ликвидации"))
                continue
            dist = _liq_dist(p)
        except (ValueError, ArithmeticError) as e:
            out.append((p, "alert", f"данные позиции: {e}"))
            continue
        if dist <= 0:
            out.append((p, "alert", "цена ликвидации не с той стороны от mark — данные битые"))
        elif dist < HARD["liq_reduce"]:
            out.append((p, "reduce", f"до ликвидации {dist:.3f} < {HARD['liq_reduce']}"))
        elif dist < HARD["liq_alert"]:
            out.append((p, "alert", f"до ликвидации {dist:.3f} < {HARD['liq_alert']}"))
    return out


def startup_warnings(margin_modes, key_checks=None):
    """Предупреждения владельцу при старте: кросс-маржа на основном аккаунте, режим маржи не прочитан, ключ без
    привязки к IP или не прошедший проверку. margin_modes — {биржа: режим}, key_checks — {биржа: keys.KeyCheck}."""
    out = []
    for venue, mm in sorted((margin_modes or {}).items()):
        if mm == "cross":
            out.append(f"{venue}: {CROSS_NOTE}. Под кросс-маржой бот открывает только хедж/фандинг в minlot, со "
                       f"стопом в пределах остатка дневного лимита и без ваших позиций на аккаунте")
        elif mm != "isolated":
            out.append(f"{venue}: режим маржи не прочитан ({mm!r}) — открытия запрещены")
    for venue, kc in sorted((key_checks or {}).items()):
        if kc is None or not getattr(kc, "ok", False):
            out.append(f"{venue}: торговля по ключу запрещена" + (f": {kc.detail}" if kc is not None and kc.detail
                                                                   else ""))
        elif not getattr(kc, "ip_bound", False):
            out.append(f"{venue}: торговый ключ без привязки к IP (принято владельцем) — утечка ключа = доступ к "
                       f"торговле с любого адреса")
    return out
