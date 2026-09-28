"""Чьё это: позиция, ордера и исполнения символа на бирже против журнала бота.

Решение владельца 2026-09-27: торговые ключи — на ОСНОВНОМ аккаунте, бот никогда не трогает ручные позиции и ордера
владельца. В одностороннем режиме биржа сальдирует всё по символу: шорт хеджа бота против лонга владельца просто
уменьшил бы его позицию, смена плеча или маржи поменяла бы его позицию. Поэтому перед открытием, закрытием, стопом,
сменой плеча или режима маржи — и периодически (journal.reconcile) — journal читает с биржи позицию и ВСЕ открытые
ордера символа (`fetch`) и сверяет их с журналом бота (`foreign`):
- ордер бота — только с клиентским id из журнала бота; стоп бота — тоже такой ордер (условный reduceOnly на размер
  позиции бота); любой ордер без нашего id (в том числе стоп позиции без id) — владельца;
- позиция символа — бота, только если её знаковый размер ровно равен сальдо бота по журналу; 0 на бирже — чужого нет
  (если у бота по журналу не 0 — позицию закрыла биржа или владелец: `flat_external`);
- равенство размеров ещё не доказывает, что позиция — бота (ликвидация или владелец закрыли её, владелец открыл такую
  же): пока у бота по журналу позиция, нужны исполнения символа с отметки сверки (journal: watermark) — исполнение без
  id бота (ликвидация, ADL, ручная сделка владельца) — чужое (`foreign_executions`);
- BingX, односторонний режим: знак positionAmt (TODO(api)) — размер как у бота, а знак обратный — не угадываем;
- что-то не прочитали — `foreign` None (`unknown_why` — почему): не знаем — не открываем и не трогаем.
Для кросс-маржи — все позиции аккаунта на общем залоге (`foreign_account`).
Здесь только чтение (GET через venues) и чистые сравнения; решения — journal/risk.
"""
import asyncio
import time
from collections import namedtuple
from decimal import Decimal

from trading import venues

Snapshot = namedtuple("Snapshot", "venue category symbol venue_sym position orders leverage margin_mode mark "
                                  "instrument account errors ts capital marks execs")
# ts — time.time() перед чтением цены, ордеров, позиции и исполнений: по нему journal отказывает по старому снимку;
# capital — капитал аккаунта (лимит дня 2%); marks — {(биржа, категория, символ): цена} других символов с позициями
# бота; execs — исполнения символа с отметки сверки (None — не читали или не прочитали)
Snapshot.__new__.__defaults__ = (None, None, None, None)


async def fetch(s, venue, category, symbol, venue_sym, creds, market=True, settings=True, exec_since=None,
                others=(), capital=False):
    """Снимок символа с биржи. Сначала медленное и редко меняющееся: режим позиций (BingX), фактическое плечо и режим
    маржи (settings, перпы), при кросс-марже — позиции всего аккаунта; market — шаги инструмента, капитал (capital) и
    цены других символов с позициями бота (others — [(биржа, категория, символ, символ биржи)]). Последними и вместе —
    цена (market), все открытые ордера, позиция (перпы) и исполнения символа с exec_since (мс; None — не читать); ts —
    время перед ними. Ошибки — в errors (снимок с ошибками для «чьё это» — неполный: foreign вернёт None)."""
    errors = []
    perp = category != "spot"
    position = leverage = margin = mark = inst = account = cap = execs = None
    marks = {}
    if perp and venue == venues.BINGX:
        _, why = await venues.position_mode(s, venue, creds)
        if why:
            errors.append(f"позиция: {why}")
    if perp and settings:
        if venue == venues.BINGX:
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
        inst, why = await venues.instrument(s, venue, category, venue_sym)
        if why:
            errors.append(f"инструмент: {why}")
        if capital:
            cap, why = await venues.capital(s, venue, creds)
            if why:
                errors.append(f"капитал: {why}")
        for v, cat, sym, vs in others:
            px, why = await venues.mark_price(s, v, cat, vs)
            if why:
                errors.append(f"цена {v} {sym}: {why}")
            else:
                marks[(v, cat, sym)] = px
    ts = time.time()
    jobs = {"orders": venues.open_orders(s, venue, category, venue_sym, creds)}
    if market:
        jobs["mark"] = venues.mark_price(s, venue, category, venue_sym)
    if perp:
        jobs["position"] = venues.symbol_positions(s, venue, venue_sym, creds)
        if exec_since is not None:
            jobs["execs"] = venues.executions(s, venue, category, venue_sym, int(exec_since), int(ts * 1000), creds)
    got = dict(zip(jobs, await asyncio.gather(*jobs.values())))
    orders, why = got["orders"]
    if why:
        errors.append(f"ордера: {why}")
    if market:
        mark, why = got["mark"]
        if why:
            errors.append(f"цена: {why}")
    if perp:
        position, why = got["position"]
        if why:
            errors.append(f"позиция: {why}")
        if venue == venues.BYBIT and settings:
            leverage = position["leverage"] if position else None
        if "execs" in got:
            execs, why = got["execs"]
            if why:
                errors.append(f"исполнения: {why}")
    if mark is not None:
        marks[(venue, category, symbol)] = mark
    return Snapshot(venue, category, symbol, venue_sym, position, orders, leverage, margin, mark, inst, account,
                    tuple(errors), ts, cap, marks, execs)


def _net(snap):
    return snap.position["net"] if snap.position else Decimal(0)


def _perp(snap):
    return snap.category != "spot"


def unknown_why(snap, book):
    """Почему «чьё это» не решить ("" — решить можно): не прочитаны ордера или позиция; BingX — размер как у бота, а
    знак обратный (TODO(api): знак positionAmt в одностороннем режиме); у бота по журналу позиция, на бирже столько же
    — а исполнения символа не прочитаны (не доказать, что позиция бота цела)."""
    if snap.orders is None or any(e.startswith("ордера:") for e in snap.errors):
        return "ордера символа не прочитаны: " + "; ".join(snap.errors)[:200]
    if not _perp(snap):
        return ""
    if snap.position is None or any(e.startswith("позиция:") for e in snap.errors):
        return "позиция символа не прочитана: " + "; ".join(snap.errors)[:200]
    net, bot = _net(snap), book["net"]
    if snap.venue == venues.BINGX and bot != 0 and net == -bot:
        return "знак позиции BingX не сходится с журналом бота — не угадываем, проверьте кабинет"
    if bot != 0 and net == bot and (snap.execs is None or any(e.startswith("исполнения:") for e in snap.errors)):
        return "исполнения символа не прочитаны — не доказать, что позиция бота цела: " + "; ".join(snap.errors)[:200]
    return ""


def foreign_executions(execs, book):
    """Исполнения символа без id бота (кроме начислений фандинга, размер позиции не меняющих): ликвидация и ADL биржи,
    ручная сделка владельца. [] — все исполнения — бота."""
    return [e for e in execs or () if e["kind"] not in venues.NO_POSITION_EXEC
            and not (e["client_id"] and e["client_id"] in book["client_ids"])]


def foreign(snap, book):
    """Чужое по символу: список описаний ([] — всё на символе бота или пусто) или None — не знаем (unknown_why): не
    трогаем. book — journal.bot_book этого символа."""
    if unknown_why(snap, book):
        return None
    out = []
    net, bot = _net(snap), book["net"]
    if _perp(snap) and net != 0 and net != bot:
        out.append(f"позиция {'long' if net > 0 else 'short'} {venues.fmt(abs(net))} на бирже, у бота по журналу "
                   f"{venues.fmt(bot)}")
    for o in snap.orders:
        cid = o["client_id"]
        if cid and cid in book["client_ids"]:
            continue
        out.append(f"ордер {cid or o['order_id'] or 'без id'} ({o['stop_type'] or o['type'] or '?'})")
    if _perp(snap) and bot != 0 and net == bot:
        for e in foreign_executions(snap.execs, book):
            out.append(f"исполнение без id бота: {e['kind']} {e['side']} {venues.fmt(e['qty'])} (мс {e['ts']})")
    return out


def owned_whole(snap, book):
    """Вся позиция символа на бирже — бота (не ноль, ровно сальдо журнала, после отметки сверки только исполнения
    бота), чужих ордеров нет."""
    return foreign(snap, book) == [] and _net(snap) != 0 and _net(snap) == book["net"]


def flat_external(snap, book):
    """На бирже по символу пусто, а у бота по журналу позиция и нет его незавершённых ордеров, кроме висящих стопов: её
    закрыла биржа (ликвидация, ADL) или владелец — journal обнуляет сальдо бота с событием владельцу. Висящие стопы бота
    журнал перед этим сверяет (стоп мог только что исполниться) и снимает."""
    return (snap.position is not None and not any(e.startswith("позиция:") for e in snap.errors)
            and _net(snap) == 0 and book["net"] != 0 and not book["uncertain"]
            and set(book["active"]) <= set(book.get("stops", ())))


def annotate(positions, books):
    """Позиции с биржи (venues.positions — там ВСЕ позиции аккаунта, и владельца тоже) → те же словари + owned (вся
    позиция символа — бота: знаковый размер ровно сальдо журнала), bot_size (своя часть бота, не больше позиции; при
    встречном сальдо — 0), bot_net (сальдо бота по журналу), mismatch (у бота по журналу позиция, а на бирже по символу
    другое — сопровождение только тревогой) и sign_suspect (BingX: размер как у бота, знак обратный — TODO(api)).
    books — {(биржа, канонический символ): сальдо бота по журналу}. risk.position_actions сокращает только owned и не
    больше bot_size."""
    out = []
    for p in positions or ():
        signed = p["size"] if p["side"] == "long" else -p["size"]
        bot = books.get((p["venue"], p["symbol"])) or Decimal(0)
        same = bot != 0 and (bot > 0) == (signed > 0)
        owned = bot != 0 and signed == bot
        out.append(dict(p, owned=owned, bot_size=min(abs(bot), p["size"]) if same else Decimal(0), bot_net=bot,
                        mismatch=bot != 0 and not owned,
                        sign_suspect=p["venue"] == venues.BINGX and bot != 0 and signed == -bot))
    return out


def foreign_account(snap, books):
    """Для кросс-маржи: чужие позиции всего аккаунта на общем залоге ([] — нет) или None — не прочитали. books —
    {канонический символ: сальдо бота по журналу на этой бирже}; позиция не нашей монеты или категории (USDC-перпы,
    inverse, опционы, займы спот-маржи) — всегда чужая."""
    if snap.account is None:
        return None
    out = []
    for p in snap.account:
        bot = books.get(p["symbol"]) if p["symbol"] else None
        if bot is None or p["signed"] != bot:
            out.append(f"{p['raw_symbol']} {venues.fmt(p['signed'])}")
    return out
