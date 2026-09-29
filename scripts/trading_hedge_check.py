"""Проверка Bybit перед хеджем — ТОЛЬКО ЧТЕНИЕ, запускает владелец на своём ПК. Ордеров, переводов и выводов нет.

Запуск: python scripts/trading_hedge_check.py [--bot-dir ПАПКА_БОТА] [--position-usdt 250] [--leverage 2]
Печатает список ✅/❌ (⚠️ — не блокирует, но прочтите) с понятным «что сделать». Код выхода: 0 — ❌ нет, 1 — есть ❌.

Что проверяется (всё — существующими функциями trading/: venues, ownership, keys; своих запросов и эндпоинтов нет):
1. Торговый ключ Bybit сохранён (scripts/trading_keys.py set bybit) и у него нет лишних прав: вывода, переводов, P2P, earn
   (trading.keys.check — то же правило, по которому бот сам решает, отдавать ли ключ торговле). Итог проверки пишется
   во ВРЕМЕННУЮ папку, а не в data/trading_keycheck.json бота.
2. Привязка ключа к IP (⚠️, если её нет; ключ по IP — рекомендация, не запрет).
3. Режим маржи аккаунта — Isolated (venues.margin_mode). У Bybit UTA он один на весь аккаунт; бот его НЕ меняет —
   переключает владелец сам в настройках Bybit.
4. Режим позиций — односторонний (One-Way): по позициям BTCUSDT/ETHUSDT/TONUSDT (venues.symbol_positions).
5. Деньги на фьючерсном аккаунте против маржи позиции: позиция / плечо (250 USDT при 2× = 125 USDT) — venues.capital.
   Сравнивается ОБЩИЙ капитал единого аккаунта (totalEquity: все монеты в пересчёте на USDT, вместе с уже занятой
   маржой и нереализованным результатом), а не свободный USDT: ядро отдельно доступный баланс не читает, поэтому
   следом идёт постоянное ⚠️ «Свободный USDT не проверен» — глазами в кошельке Bybit.
6. Инструменты ETHUSDT / BTCUSDT / TONUSDT (TON — после проверки символа resolve_symbols): шаг лота, минимальное
   количество, минимальный номинал (venues.instrument + mark_price): помещается ли минимальный ордер в лимит позиции.
7. Ваши ручные позиции и ордера по этим символам (ownership.foreign с пустой книгой бота) — бот их не трогает, но и
   открывать хедж поверх не станет.
8. Часы ПК: отдельного запроса времени биржи в ядре нет, поэтому вывод делается по ответам: код Bybit 10002
   (timestamp/recv_window) — ❌; подписанные запросы приняты — ✅ (окно биржи 5 с; бот сам строже — 1 с, но измерить
   расхождение точно отсюда нельзя).
Ключ и секрет не печатаются никогда (в тексте ответов биржи они вычищаются).
"""
import argparse
import asyncio
import os
import re
import sys
import tempfile
from collections import namedtuple
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import accounts  # noqa: E402
import p2p  # noqa: E402
from trading import keys as trade_keys  # noqa: E402
from trading import ownership, risk, venues  # noqa: E402
import simperp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BYBIT = venues.BYBIT
CATEGORY = "linear"
OK, FAIL, WARN = "ok", "fail", "warn"
MARKS = {OK: "✅", FAIL: "❌", WARN: "⚠️"}
DEFAULT_POSITION_USDT = Decimal(250)     # решение владельца 29.09: TRADING_MAX_POSITION_USDT=250
DEFAULT_LEVERAGE = Decimal(2)            # изолированная маржа, плечо 2×
TOPUP_FROM, TOPUP_TO = Decimal("1.2"), Decimal("1.6")   # запас на комиссии и колебания: 125 -> 150…200 USDT
CLOCK_CODE = "10002"                     # Bybit: timestamp/recv_window — часы ПК разошлись с биржей
IP_CODE = "10010"                        # Bybit: IP не из белого списка ключа
KEY_CODES = ("10003", "10004", "10005")  # ключ не найден / подпись неверна / нет права

Check = namedtuple("Check", "level title detail fix")

# Как боевой режим хеджа включается на самом деле (trading/gates.py: max_mode). Здесь только текст.
LADDER_LINES = [
    "Как включать хедж (в .env: TRADING=1 и TRADING_MODE=…; после каждого обновления бота launcher сбрасывает TRADING в 0 "
    "— включайте заново):",
    "  1) оставьте TRADING_MODE=paper и запустите: python scripts/trading_gates_stats.py --backtest (покажет, что "
    "разрешают цифры);",
    "  2) осторожно: записанный gates_backtest.json САМ разрешает РЕАЛЬНЫЕ ордера minlot (до 50 USDT) — как только в .env "
    "TRADING=1 и TRADING_MODE=minlot (или confirm/auto: они тогда тоже дадут только minlot); бумажной истории для этого не "
    "нужно;",
    "  3) поставьте TRADING_MODE=minlot на первые 10–20 реальных кругов и посмотрите, как они прошли;",
    "  4) только потом confirm (каждый ордер по вашей кнопке). Если пороги бумаги (14 дней, 50 закрытых хеджей…) пройдены, "
    "бот допускает confirm СРАЗУ, минуя minlot — поэтому confirm не ставьте, не увидев, что minlot работает.",
]

SETTINGS_WAY = ("в Bybit откройте настройки торговли (шестерёнка на странице фьючерсов USDT) → режим маржи аккаунта")
FIX_NO_KEY = ("Создайте в Bybit API-ключ: права только Contract (Orders, Positions); вывод, переводы, P2P и Earn "
              "выключены; привязка к IP — этого ПК. Введите его на ПК командой: python scripts/trading_keys.py set "
              "bybit (в чат ключ не присылайте).")
FIX_UNSAFE = ("Создайте НОВЫЙ ключ только с правами Contract (Orders, Positions) — вывод, переводы, P2P, Earn выключены; "
              "старый ключ удалите в Bybit; новый введите: python scripts/trading_keys.py set bybit.")
FIX_UNUSABLE = ("В Bybit откройте ключ и включите Contract → Orders и Positions (ключ «только чтение» торговать не "
                "может); затем заново: python scripts/trading_keys.py set bybit.")
FIX_CLOCK = ("Синхронизируйте часы ПК: Параметры Windows → Время и язык → Дата и время → «Синхронизировать»; часовой "
             "пояс роли не играет, важно точное время. Потом запустите проверку снова.")
FIX_IP = ("Ваш IP изменился или не добавлен: в Bybit откройте ключ → «привязка к IP» → добавьте текущий адрес этого ПК "
          "(или создайте ключ заново). Если IP дома меняется, ключ с привязкой будет так отваливаться при каждой смене.")
FIX_KEY = ("Ключ или секрет неверные, ключ удалён/отключён или у него не хватает прав: создайте ключ заново и введите: "
           "python scripts/trading_keys.py set bybit.")
FIX_NET = "Проверьте интернет на ПК и запустите проверку ещё раз."
FIX_NEED_KEY = "Сначала исправьте пункт про ключ — без него живые данные не прочитать."


def _clean(text, creds):
    """В вывод не попадают ключ и секрет, даже если биржа процитировала их в тексте ошибки."""
    text = str(text)
    for secret in creds or ():
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, "•••")
    return text


def _code(text):
    m = re.search(r"код (\d+)", str(text))
    return m.group(1) if m else None


def _kind(text):
    """Причина сбоя чтения по тексту ядра: clock / ip / key / net / other."""
    text = str(text)
    code = _code(text)
    if code == CLOCK_CODE or "recv_window" in text.lower() or "timestamp" in text.lower():
        return "clock"
    if code == IP_CODE:
        return "ip"
    if code in KEY_CODES:
        return "key"
    if "таймаут" in text or "нет соединения" in text or "ClientConnector" in text:
        return "net"
    return "other"


def _plain(text):
    """Что случилось — по-русски, вместе с исходным текстом биржи."""
    kind = _kind(text)
    head = {"clock": "часы ПК расходятся с часами Bybit", "ip": "IP этого ПК не в списке разрешённых у ключа",
            "key": "Bybit не принял ключ", "net": "нет связи с Bybit"}.get(kind)
    return f"{head} ({text})" if head else str(text)


def _fix_for(text):
    return {"clock": FIX_CLOCK, "ip": FIX_IP, "key": FIX_KEY, "net": FIX_NET}.get(
        _kind(text), "Причина в тексте выше; если непонятно — запустите проверку ещё раз, а затем пришлите этот вывод.")


def _usdt(v):
    return f"{Decimal(v):.2f}"


def _dec(v):
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


# --- пункты проверки: чистые функции над прочитанным -------------------------------------------------------------------

def key_checks(creds, kc):
    """Ключ сохранён; права (нет вывода/переводов); привязка к IP."""
    if not creds:
        return [Check(FAIL, "Торговый ключ Bybit", "не сохранён", FIX_NO_KEY)]
    title = "Права ключа (вывода и переводов нет)"
    if kc.ok:
        out = [Check(OK, title, "ключ сохранён, лишних прав у него нет", "")]
    elif kc.state == "unsafe":
        out = [Check(FAIL, title, kc.detail, FIX_UNSAFE)]
    elif kc.state == "unusable":
        out = [Check(FAIL, title, kc.detail, FIX_UNUSABLE)]
    else:
        why = kc.detail.replace("права не проверить: ", "", 1)
        out = [Check(FAIL, title, "права ключа не проверить: " + _plain(why), _fix_for(why))]
    if kc.ip_bound is True:
        out.append(Check(OK, "Привязка ключа к IP", "ключ привязан к IP-адресу", ""))
    elif kc.ip_bound is False:
        out.append(Check(WARN, "Привязка ключа к IP", "у ключа нет привязки к IP — если он утечёт, им смогут торговать "
                         "с любого адреса", "Рекомендуем: в Bybit откройте ключ → привязка к IP → адрес этого ПК. "
                         "Минус: при смене домашнего IP ключ перестанет работать (код 10010) — тогда обновите адрес."))
    else:
        out.append(Check(WARN, "Привязка ключа к IP", "не удалось определить по ответу биржи",
                         "Посмотрите в Bybit → API: у ключа должен быть указан IP этого ПК."))
    return out


def margin_check(mode, why):
    title = "Режим маржи аккаунта: Isolated"
    if mode is None:
        return Check(FAIL, title, "не прочитан: " + _plain(why), _fix_for(why))
    if mode == "isolated":
        return Check(OK, title, "Isolated Margin", "")
    names = {"cross": "Cross (общий залог: при ликвидации рискуют все средства аккаунта)",
             "portfolio": "Portfolio margin (бот его не поддерживает)"}
    return Check(FAIL, title, f"сейчас {names.get(mode, mode)}",
                 f"Переключите САМИ, бот режим маржи Bybit не меняет: {SETTINGS_WAY} → Isolated Margin. Bybit "
                 f"позволяет это, только когда нет открытых позиций и ордеров.")


def position_mode_check(reads):
    """reads — {монета: (позиция, причина)} из venues.symbol_positions. У Bybit в одностороннем режиме строка positionIdx 0
    есть всегда (в ней плечо), даже при нулевой позиции; в режиме хеджа (Both Sides) её нет."""
    title = "Режим позиций: One-Way"
    fix = f"Переключите САМИ: {SETTINGS_WAY.split(' → ')[0]} → режим позиций → One-Way (без открытых позиций и ордеров)."
    problems = [f"{c}: {why}" for c, (pos, why) in reads.items() if pos is None]
    if not reads:
        return Check(FAIL, title, "не проверено: позиции не читались", FIX_NEED_KEY)
    hedge_mode = [p for p in problems if "хедж" in p]
    if hedge_mode:
        return Check(FAIL, title, "включён режим хеджа позиций (Both Sides): " + "; ".join(hedge_mode), fix)
    if problems:
        return Check(FAIL, title, "позиции не прочитаны: " + "; ".join(_plain(p) for p in problems),
                     _fix_for(" ".join(problems)))
    no_row = [c for c, (pos, _) in reads.items() if pos["leverage"] is None and pos["net"] == 0]
    if no_row:
        return Check(WARN, title, "односторонний режим не подтверждён: у " + ", ".join(no_row) + " нет строки "
                     "positionIdx 0 (Bybit отдаёт её в One-Way даже при нулевой позиции)", fix)
    return Check(OK, title, "строка positionIdx 0 есть у " + ", ".join(reads) + " (режим хеджа позиций не обнаружен)", "")


def funds_check(equity, why, position, leverage):
    """Деньги под маржу: позиция / плечо. equity — капитал единого аккаунта (totalEquity), venues.capital."""
    need = position / leverage
    low, high = need * TOPUP_FROM, need * TOPUP_TO
    title = f"Деньги на фьючерсном аккаунте: маржа {_usdt(need)} USDT"
    basis = (f"для позиции {_usdt(position)} USDT при плече {leverage.normalize():f}× нужно {_usdt(need)} USDT маржи; "
             f"с запасом на комиссии и ход цены — {low:.0f}–{high:.0f} USDT")
    caveat = (" (сравнивается ОБЩИЙ капитал всего единого аккаунта — totalEquity, вместе с любыми монетами на нём и уже "
              "занятой маржой; свободный USDT отдельно не проверялся)")
    fix = (f"Пополните фьючерсный (единый торговый) аккаунт Bybit вручную на {low:.0f}–{high:.0f} USDT: бот переводов "
           f"между счетами не делает.")
    if equity is None:
        return Check(FAIL, title, "капитал не прочитан: " + _plain(why), _fix_for(why))
    if equity < need:
        return Check(FAIL, title, f"на аккаунте {_usdt(equity)} USDT{caveat}; {basis}", fix)
    if equity < low:
        return Check(WARN, title, f"на аккаунте {_usdt(equity)} USDT{caveat} — хватает впритык; {basis}", fix)
    return Check(OK, title, f"на аккаунте {_usdt(equity)} USDT{caveat}; {basis}", "")


def free_usdt_check(position, leverage):
    """Свободный (доступный) USDT под маржу ядро не читает: venues.capital отдаёт только totalEquity. Поэтому это ⚠️ всегда
    — пока ядро не научится читать доступный баланс (своих запросов в этом скрипте нет намеренно)."""
    need = position / leverage
    return Check(WARN, "Свободный USDT под маржу: не проверен",
                 f"ядро читает только общий капитал аккаунта (строка выше), доступный баланс отдельно — нет; часть "
                 f"капитала может лежать в других монетах или уже быть под маржой, тогда свободного USDT меньше",
                 f"Откройте в Bybit кошелёк единого торгового аккаунта и убедитесь глазами, что доступного USDT не меньше "
                 f"{_usdt(need)} (лучше {need * TOPUP_FROM:.0f}–{need * TOPUP_TO:.0f}); иначе переведите USDT из других "
                 f"монет или пополните аккаунт.")


def _inactive(check, coin, active):
    """Монета не в HEDGE_ASSETS — бот её не хеджирует: ❌ по ней не блокирует, остаётся ⚠️."""
    if active is None or coin[:-4] in active or check.level != FAIL:
        return check
    return check._replace(level=WARN, detail=check.detail + f" (монета {coin[:-4]} не в HEDGE_ASSETS — бот её не "
                          "хеджирует, на запуск это не влияет)")


def instrument_check(coin, inst, mark, why, position, minlot, active=None):
    """Лот, минимальное количество и номинал: помещается ли минимальный ордер в лимит позиции (и в лимит minlot)."""
    title = f"Инструмент {coin}"
    if inst is None or mark is None:
        return _inactive(Check(FAIL, title, "справочник Bybit не прочитан: " + _plain(why), _fix_for(why)), coin, active)
    base = coin[:-4] if coin.endswith("USDT") else coin
    min_value = max(inst.min_qty * mark, inst.min_notional)
    step_value = inst.qty_step * mark
    facts = (f"цена {venues.fmt(mark)} USDT; шаг лота {venues.fmt(inst.qty_step)} {base} (≈ {_usdt(step_value)} USDT), "
             f"мин. количество {venues.fmt(inst.min_qty)} {base}, мин. номинал {venues.fmt(inst.min_notional)} USDT; "
             f"наименьший ордер ≈ {_usdt(min_value)} USDT")
    if min_value > position:
        return _inactive(Check(FAIL, title, f"{facts} — больше лимита позиции {_usdt(position)} USDT",
                               f"Эту монету бот хеджировать не сможет: уберите {base} из HEDGE_ASSETS в .env "
                               "(или поднимите TRADING_MAX_POSITION_USDT, если это осознанно)."), coin, active)
    if min_value > minlot:
        return Check(WARN, title, f"{facts} — не помещается в minlot (до {_usdt(minlot)} USDT), помещается в лимит "
                     f"позиции {_usdt(position)} USDT",
                     "Ничего делать не нужно: пока хедж в режиме minlot (максимум 50 USDT), эту монету бот не "
                     "откроет; с режима confirm — откроет.")
    return Check(OK, title, facts, "")


def instrument_missing(coin, why, active=None):
    base = coin[:-4]
    return _inactive(Check(FAIL, f"Инструмент {coin}", "символ на Bybit не подтверждён: " + _plain(why),
                           f"Если монета не нужна — уберите {base} из HEDGE_ASSETS в .env (например HEDGE_ASSETS=BTC,ETH); "
                           "иначе запустите проверку ещё раз."), coin, active)


def foreign_check(coins, snaps):
    """Ваши ручные позиции и ордера по символам хеджа: None — не прочитали, [] — нет, иначе — список."""
    title = "Ваши ручные позиции и ордера по BTC/ETH/TON"
    book = {"net": Decimal(0), "client_ids": frozenset()}
    unknown, found = [], []
    for coin in coins:
        snap = snaps.get(coin)
        res = ownership.foreign(snap, book) if snap is not None else None
        if res is None:
            unknown.append(f"{coin}: {ownership.unknown_why(snap, book) if snap is not None else 'не читалось'}")
        elif res:
            found.append(f"{coin}: " + "; ".join(res))
    if unknown:
        return Check(FAIL, title, "не прочитано: " + "; ".join(_plain(u) for u in unknown), _fix_for(" ".join(unknown)))
    if found:
        return Check(WARN, title, "; ".join(found), "Бот ваши позиции и ордера не трогает, но по такому символу хедж "
                     "не откроет (в одностороннем режиме позиции складываются). Закройте свою позицию или не хеджируйте "
                     "эту монету.")
    return Check(OK, title, "по BTCUSDT, ETHUSDT, TONUSDT у вас ничего нет — символы свободны для бота", "")


def clock_check(errors, signed_ok):
    title = "Часы ПК и биржи"
    if any(_kind(e) == "clock" for e in errors):
        return Check(FAIL, title, "Bybit отказал по времени (код 10002: часы ПК разошлись с биржей больше чем на окно "
                     "5 секунд)", FIX_CLOCK)
    if signed_ok:
        return Check(OK, title, "подписанные запросы Bybit приняты — расхождение часов меньше окна биржи (5 с); "
                     "точную разницу ядро не измеряет (бот сам требует не больше 1 с)", "")
    return Check(FAIL, title, "не проверено: ни один подписанный запрос не прошёл", FIX_NEED_KEY)


# --- чтение ------------------------------------------------------------------------------------------------------------

def _session():   # отдельно, чтобы тесты подменяли сеть
    import aiohttp
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))


async def run_checks(s, position=DEFAULT_POSITION_USDT, leverage=DEFAULT_LEVERAGE, check_path=None, creds=None):
    """Все проверки → (список Check, все тексты ошибок чтения). Только GET через venues; итог проверки ключа пишется в
    check_path (временный файл вызывающего), а не в файл бота."""
    creds = trade_keys.raw_credentials(BYBIT) if creds is None else creds
    errors, checks = [], []
    kc = await trade_keys.check(s, BYBIT, creds, path=check_path) if creds else None
    checks += key_checks(creds, kc)
    private = bool(kc and kc.ok)   # живые данные читаем только ключом, который прошёл проверку прав
    signed_ok = bool(kc and kc.state in ("ok", "unsafe", "unusable"))   # эти итоги — из принятого биржей запроса
    if kc is not None and not kc.ok and kc.detail:
        errors.append(kc.detail)
    skip = "не проверено: " + ("торговый ключ не сохранён" if not creds else "ключ не прошёл проверку прав или Bybit "
                               "не ответил (см. выше)")
    coins = venues.SYMBOLS
    try:
        active = set(simperp.settings()["assets"])   # монеты, которые бот хеджирует (HEDGE_ASSETS)
    except Exception:
        active = None
    await venues.resolve_symbols(s, BYBIT, CATEGORY)   # TON = GRAMUSDT: символ берётся только по свежей проверке

    if private:
        mode, why_m = await venues.margin_mode(s, BYBIT, coins[0], creds)
        checks.append(margin_check(mode, why_m))
        if why_m:
            errors.append(why_m)
    else:
        checks.append(Check(FAIL, "Режим маржи аккаунта: Isolated", skip, FIX_NEED_KEY))

    syms, missing = {}, {}
    for coin in coins:
        try:
            syms[coin] = venues.venue_symbol(BYBIT, CATEGORY, coin)
        except ValueError as e:   # не подтверждён справочником / не торгуется: причина в тексте
            missing[coin] = str(e)
    snaps, reads = {}, {}
    if private:
        for coin in syms:
            snaps[coin] = await ownership.fetch(s, BYBIT, CATEGORY, coin, syms[coin], creds, market=False,
                                                settings=False, capital=False)
            reads[coin] = (snaps[coin].position, "; ".join(snaps[coin].errors))
            errors += snaps[coin].errors
        checks.append(position_mode_check(reads))
        equity, why_c = await venues.capital(s, BYBIT, creds)
        checks.append(funds_check(equity, why_c, position, leverage))
        checks.append(free_usdt_check(position, leverage))
        if why_c:
            errors.append(why_c)
    else:
        checks.append(Check(FAIL, "Режим позиций: One-Way", skip, FIX_NEED_KEY))
        checks.append(Check(FAIL, f"Деньги на фьючерсном аккаунте: маржа {_usdt(position / leverage)} USDT", skip,
                            FIX_NEED_KEY))

    minlot = risk.MINLOT["position_usdt"]
    for coin in coins:
        if coin in missing:
            checks.append(instrument_missing(coin, missing[coin], active))
            continue
        inst, why_i = await venues.instrument(s, BYBIT, CATEGORY, syms[coin])
        mark, why_p = await venues.mark_price(s, BYBIT, CATEGORY, syms[coin])
        checks.append(instrument_check(coin, inst, mark, why_i or why_p, position, minlot, active))
    if private:
        checks.append(foreign_check([c for c in coins if c in snaps], snaps))
    else:
        checks.append(Check(FAIL, "Ваши ручные позиции и ордера по BTC/ETH/TON", skip, FIX_NEED_KEY))
    checks.append(clock_check(errors, signed_ok))
    return checks, errors


def render(checks, creds=None):
    """Строки отчёта: каждая проверка, а ниже — «Что сделать» для ❌ и ⚠️; в конце итог."""
    lines = []
    for c in checks:
        lines.append(f"{MARKS[c.level]} {c.title}: {c.detail}" if c.detail else f"{MARKS[c.level]} {c.title}")
        if c.fix and c.level != OK:
            lines.append(f"   Что сделать: {c.fix}")
    bad, warn = sum(c.level == FAIL for c in checks), sum(c.level == WARN for c in checks)
    lines.append("")
    if bad:
        lines.append(f"Итог: ❌ {bad} — исправьте и запустите проверку снова. Пока есть ❌, TRADING=1 включать рано.")
    else:
        lines.append("Итог: ❌ нет" + (f", ⚠️ {warn} — прочтите «Что сделать» выше. Аккаунт Bybit готов, но есть "
                                       "предупреждения:" if warn else ". Аккаунт Bybit готов к хеджу."))
        for c in checks:   # что именно предупреждает — рядом с вердиктом, чтобы «готов» не прочли без оговорки
            if c.level == WARN:
                lines.append(f"   {MARKS[WARN]} {c.title}" + (f": {c.detail}" if c.detail else ""))
        lines += LADDER_LINES
    lines.append("Скрипт только читает: ордеров, переводов и выводов он не делает. Бот сам перечитывает всё это "
                 "перед каждым открытием — эта проверка его решений не заменяет.")
    return [_clean(x, creds) for x in lines]


def _positive(name, raw, lo=Decimal(0), hi=None):
    d = _dec(raw)
    if d is None or d < lo or (hi is not None and d > hi):
        raise ValueError(f"{name}: нужно число" + (f" от {lo}" if lo else " больше 0")
                         + (f" до {hi}" if hi is not None else ""))
    return d


def main(argv=None, session_factory=_session, env_loader=p2p.load_env):
    try:
        sys.stdout.reconfigure(errors="replace")   # консоль не в UTF-8 — «✅» не должен ронять скрипт
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Проверка Bybit перед хеджем (только чтение)")
    ap.add_argument("--bot-dir", default=ROOT, help="папка бота (там data/keys.json и .env); по умолчанию — эта")
    ap.add_argument("--position-usdt", default=str(DEFAULT_POSITION_USDT), help="лимит позиции хеджа, USDT")
    ap.add_argument("--leverage", default=str(DEFAULT_LEVERAGE), help="плечо хеджа")
    a = ap.parse_args(argv)
    try:
        position = _positive("--position-usdt", a.position_usdt)
        leverage = _positive("--leverage", a.leverage, Decimal(1), risk.HARD["leverage"]["hedge"])
    except ValueError as e:
        print(f"⛔ {e}")
        return 2
    bot_dir = os.path.abspath(a.bot_dir)
    env_loader(os.path.join(bot_dir, ".env"))
    accounts.KEYS_PATH = os.path.join(bot_dir, "data", "keys.json")   # ключи — из папки бота, а не из текущей
    print(f"Проверка Bybit перед хеджем (только чтение): позиция {_usdt(position)} USDT, плечо "
          f"{leverage.normalize():f}×, изолированная маржа")
    creds = trade_keys.raw_credentials(BYBIT)
    with tempfile.TemporaryDirectory() as tmp:   # итог проверки ключа — сюда, файл бота не трогаем
        async def go():
            async with session_factory() as s:
                return await run_checks(s, position, leverage, os.path.join(tmp, "keycheck.json"), creds or ())
        try:
            checks, _ = asyncio.run(go())
        except Exception as e:   # сеть/сессия: показываем тип, а не текст (в нём бывает адрес запроса)
            print(f"⛔ проверка не выполнена: {accounts.api_error_text(e)}")
            return 2
    print("\n".join(render(checks, creds)))
    return 1 if any(c.level == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
