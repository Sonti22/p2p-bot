"""Чьё это: позиция и ордера символа на бирже против журнала бота.

Решение владельца 2026-09-27: торговые ключи — на ОСНОВНОМ аккаунте, бот никогда не трогает ручные позиции и ордера
владельца. В одностороннем режиме биржа сальдирует всё по символу: шорт хеджа бота против лонга владельца просто
уменьшил бы его позицию, стоп tpslMode=Full / stopLoss заменил бы стоп всей позиции, смена плеча или маржи поменяла бы
его позицию. Поэтому перед открытием, сменой стопа, плеча или режима маржи по символу journal читает с биржи позицию и
ВСЕ открытые ордера этого символа (`fetch`) и сверяет их с журналом бота (`foreign`):
- ордер с клиентским id из журнала бота — бота; стоп позиции без id (так биржа показывает tpslMode=Full у Bybit и
  stopLoss у BingX) — бота, только если вся позиция символа — бота, стоп закрывающей стороны и цена срабатывания ровно
  последний стоп бота из журнала; любой другой ордер — владельца;
- позиция символа — бота, только если её знаковый размер ровно равен сальдо бота по журналу; 0 на бирже — чужого нет
  (если у бота по журналу не 0 — позицию закрыла биржа: стоп или ликвидация, `flat_external`);
- что-то не прочитали — `foreign` None: не знаем — не открываем и не трогаем.
Для кросс-маржи — все USDT-перп позиции аккаунта (`foreign_account`): у кросса общий залог, чужие позиции других монет —
тоже риск для позиции бота и наоборот.
Здесь только чтение (GET через venues) и чистые сравнения; решения — journal/risk.
"""
from collections import namedtuple
from decimal import Decimal

from trading import venues

Snapshot = namedtuple("Snapshot", "venue category symbol venue_sym position orders leverage margin_mode mark "
                                  "instrument account errors")


async def fetch(s, venue, category, symbol, venue_sym, creds, market=True):
    """Снимок символа с биржи: позиция (перпы), все открытые ордера, фактическое плечо и режим маржи (перпы), при
    кросс-марже — позиции всего аккаунта; market — ещё цена и шаги инструмента (для открытия). Ошибки — в errors
    (снимок с ошибками для «чьё это» — неполный: foreign вернёт None)."""
    errors = []
    position = leverage = margin = mark = inst = account = None
    orders, why = await venues.open_orders(s, venue, category, venue_sym, creds)
    if why:
        errors.append(f"ордера: {why}")
    if category != "spot":
        position, why = await venues.symbol_positions(s, venue, venue_sym, creds)
        if why:
            errors.append(f"позиция: {why}")
        if venue == venues.BYBIT:
            leverage = position["leverage"] if position else None
        else:
            leverage, why = await venues.symbol_leverage(s, venue_sym, creds)
            if why:
                errors.append(f"плечо: {why}")
        margin, why = await venues.margin_mode(s, venue, venue_sym, creds)
        if why:
            errors.append(f"режим маржи: {why}")
        if margin == "cross":
            account, why = await venues.account_positions(s, venue, creds)
            if why:
                errors.append(f"позиции аккаунта: {why}")
    if market:
        mark, why = await venues.mark_price(s, venue, category, venue_sym)
        if why:
            errors.append(f"цена: {why}")
        inst, why = await venues.instrument(s, venue, category, venue_sym)
        if why:
            errors.append(f"инструмент: {why}")
    return Snapshot(venue, category, symbol, venue_sym, position, orders, leverage, margin, mark, inst, account,
                    tuple(errors))


def _net(snap):
    return snap.position["net"] if snap.position else Decimal(0)


def foreign(snap, book):
    """Чужое по символу: список описаний ([] — всё на символе бота или пусто) или None — позицию или ордера не прочитали
    (не знаем — не трогаем). book — journal.bot_book этого символа."""
    if snap.orders is None or (snap.category != "spot" and snap.position is None) \
            or any(e.startswith(("ордера:", "позиция:")) for e in snap.errors):
        return None
    out = []
    net, bot_net = _net(snap), book["net"]
    if net != 0 and net != bot_net:
        out.append(f"позиция {'long' if net > 0 else 'short'} {venues.fmt(abs(net))} на бирже, у бота по журналу "
                   f"{venues.fmt(bot_net)}")
    whole = net != 0 and net == bot_net
    close_side = "sell" if net > 0 else "buy"
    for o in snap.orders:
        cid = o["client_id"]
        if cid:
            if cid in book["client_ids"]:
                continue
            out.append(f"ордер {cid}")
        elif o["stop_type"] and whole and book["stop"] is not None and o["trigger"] == book["stop"] \
                and o["side"] == close_side:
            continue   # стоп позиции бота (tpslMode=Full / stopLoss) — ровно последний стоп бота
        else:
            out.append(f"ордер {o['order_id'] or 'без id'} ({o['stop_type'] or o['type'] or '?'})")
    return out


def owned_whole(snap, book):
    """Вся позиция символа на бирже — бота (не ноль и ровно сальдо журнала), чужих ордеров нет."""
    return foreign(snap, book) == [] and _net(snap) != 0 and _net(snap) == book["net"]


def flat_external(snap, book):
    """На бирже по символу пусто, а у бота по журналу позиция и нет его незавершённых ордеров: её закрыла биржа (стоп
    позиции или ликвидация) — journal.sync_flat обнуляет сальдо бота с событием владельцу."""
    return (snap.position is not None and _net(snap) == 0 and book["net"] != 0 and not book["active"]
            and not book["uncertain"])


def annotate(positions, books):
    """Позиции с биржи (venues.positions — там ВСЕ позиции аккаунта, и владельца тоже) → те же словари + owned (вся
    позиция символа — бота: знаковый размер ровно сальдо журнала) и bot_size (своя часть бота, не больше позиции; при
    встречном сальдо — 0). books — {(биржа, канонический символ): сальдо бота по журналу}. risk.position_actions
    сокращает только owned и не больше bot_size."""
    out = []
    for p in positions or ():
        signed = p["size"] if p["side"] == "long" else -p["size"]
        bot = books.get((p["venue"], p["symbol"])) or Decimal(0)
        same = bot != 0 and (bot > 0) == (signed > 0)
        out.append(dict(p, owned=bot != 0 and signed == bot, bot_size=min(abs(bot), p["size"]) if same else Decimal(0)))
    return out


def foreign_account(snap, books):
    """Для кросс-маржи: чужие позиции всего аккаунта ([] — нет) или None — не прочитали. books — {канонический символ:
    сальдо бота по журналу на этой бирже}; позиция не нашей монеты — всегда чужая."""
    if snap.account is None:
        return None
    out = []
    for p in snap.account:
        bot = books.get(p["symbol"]) if p["symbol"] else None
        if bot is None or p["signed"] != bot:
            out.append(f"{p['raw_symbol']} {venues.fmt(p['signed'])}")
    return out
