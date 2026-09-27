"""Журнал ордеров торгового ядра — data/trading.db, по схеме журнала выплат (payouts.py).

- Намерение со свежим клиентским id (Bybit orderLinkId / BingX clientOrderId) пишется в журнал ДО любого запроса,
  вместе с ровно теми бизнес-параметрами, что уйдут на биржу (повтор — те же параметры и тот же id; меняются только
  время и подпись).
- Состояния: prepared → sending → open / filled → closed; rejected (биржа точно не создала ордер); unknown (исход неясен
  или ответ не совпал с намерением — тревога владельцу). Переходы — только по таблице TRANSITIONS; у строки версия
  (version): каждая запись её увеличивает, сверка и повтор отправки идут только по неизменной версии (compare-and-set).
- Неясный исход (таймаут, 5xx, 3xx, не JSON, незнакомый код) — пауза, запрос ордера по клиентскому id; «не найден» —
  повтор с тем же id (не больше MAX_RESEND раз; для ордеров на открытие — только пока торговля включена; и только если
  строку за это время никто не трогал: владелец не разобрал её, не запросил отмену); иначе unknown. Точный отказ тоже
  сверяется запросом по id: «не найден» и неясностей не было — rejected.
- Любой unknown (и брошенный prepared/sending) блокирует новые открытия (`blocking`).
- Открытие (`submit(purpose="open")`) само проверяет главное, не полагаясь на precheck вызывающего: выключатель и режим,
  проверенный ключ (keys), unknown, частоту ордеров, чужое по символу (ownership: позиция и все ордера символа с
  биржи против журнала), свои встречные позиции и стопы, шаги инструмента, итоговый размер (minlot — 50 USDT),
  фактическое плечо и режим маржи с биржи, кросс-маржу (risk.guard_open).
- Закрытие и стоп — только своей позиции бота по журналу и не больше её (без уже отправленных закрытий); работают и
  при выключенной торговле.
- Замки: открытия по одному символу биржи идут по очереди (symbol_lock — снимок символа и отправка); короткий общий
  state_lock — только синхронные части (проверки, намерение, переходы состояний), сеть и паузы — вне него. Закрытие,
  стоп и сверка не ждут чужих повторов отправки.
- События владельцу — outbox в той же транзакции, что и смена состояния (не теряются при сбое или отмене задачи,
  повтор не дублируется: dedup); бот доставляет их `pending_events` → `mark_delivered`.
- `resume()` при старте: prepared/sending → unknown. `reconcile()` — сверка незавершённых ордеров с биржей любого
  возраста (снимок под коротким замком, запросы вне его, запись — только если строка не изменилась).
"""
import asyncio
import contextlib
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import accounts
from trading import keys, ownership, risk, switch, venues

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "trading.db")
MSK = timezone(timedelta(hours=3))
D0 = Decimal(0)

STATES = ("prepared", "sending", "open", "filled", "closed", "rejected", "unknown")
TRANSITIONS = {
    "prepared": {"sending", "unknown"},
    "sending": {"open", "filled", "closed", "rejected", "unknown"},
    "unknown": {"unknown", "open", "filled", "closed", "rejected"},
    "open": {"open", "filled", "closed", "rejected", "unknown"},
    "filled": {"filled", "closed", "unknown"},   # filled → closed: позицию от этого ордера закрыли (стратегия)
    "closed": set(),
    "rejected": set(),
}
FINAL = ("closed", "rejected")
ACTIVE = ("open", "unknown")                     # их опрашивает reconcile (любого возраста)
BLOCKING = ("unknown", "prepared", "sending")    # любой такой ордер (кроме идущей сейчас отправки) — открытий нет
UNSETTLED = ("prepared", "sending", "open", "unknown")   # может ещё исполниться
PURPOSES = ("open", "close", "stop")
MAX_RESEND = 2        # повторов с тем же id после неясного исхода и «не найден»
RETRY_DELAY = 3       # сек × номер попытки: пауза перед запросом статуса и перед повтором
MISMATCH = "ответ биржи не совпал с намерением"
NOT_FOUND = "биржа не находит ордер по клиентскому id"
CHANGED = "строку журнала изменили параллельно (владелец, отмена или сверка) — повтора нет, исход выяснит сверка"
_ORDER_COLUMNS = {"grp": "TEXT DEFAULT ''", "version": "INTEGER DEFAULT 0", "cancel_requested": "INTEGER DEFAULT 0"}

# --- замки и идущие отправки (на цикл событий; процесс бота — один) ---

_locks = {"loop": None, "state": None, "symbols": {}}
_inflight = set()     # client_id, которые сейчас ведёт submit этого процесса: сверка и владелец их не трогают


def _loop_locks():
    loop = asyncio.get_running_loop()
    if _locks["loop"] is not loop:
        _locks.update(loop=loop, state=asyncio.Lock(), symbols={})
    return _locks


def state_lock():
    """Короткий общий замок: только синхронные части (проверки, намерение, переходы). Сеть и паузы — вне него."""
    return _loop_locks()["state"]


def symbol_lock(venue, symbol):
    """Открытия (и смена плеча/маржи) по одному символу биржи — по очереди: снимок символа и отправка первого
    ордера видны второму. Закрытия и стопы его не ждут."""
    return _loop_locks()["symbols"].setdefault((venue, symbol), asyncio.Lock())


# --- база ---

def _connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)   # транзакции — явно (_tx)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT UNIQUE NOT NULL, created_ts REAL, updated_ts REAL, "
        "venue TEXT, category TEXT, symbol TEXT, side TEXT, order_type TEXT, qty TEXT, price TEXT DEFAULT '', "
        "reduce_only INTEGER DEFAULT 0, stop_loss TEXT DEFAULT '', "
        "purpose TEXT, strategy TEXT DEFAULT '', mode TEXT DEFAULT '', notional TEXT DEFAULT '', "
        "method TEXT, path TEXT, params TEXT, state TEXT, venue_order_id TEXT DEFAULT '', status TEXT DEFAULT '', "
        "filled TEXT DEFAULT '', avg_price TEXT DEFAULT '', fee TEXT DEFAULT '', posts INTEGER DEFAULT 0, "
        "create_kind TEXT DEFAULT '', note TEXT DEFAULT '', grp TEXT DEFAULT '', version INTEGER DEFAULT 0, "
        "cancel_requested INTEGER DEFAULT 0)")
    have = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    for col, decl in _ORDER_COLUMNS.items():   # база ранней версии ядра
        if col not in have:
            con.execute(f"ALTER TABLE orders ADD COLUMN {col} {decl}")
    con.execute("CREATE TABLE IF NOT EXISTS pnl (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, venue TEXT, "
                "symbol TEXT, amount TEXT, kind TEXT, ref TEXT, UNIQUE(venue, ref))")
    con.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                "client_id TEXT DEFAULT '', event TEXT, state TEXT DEFAULT '', note TEXT DEFAULT '', "
                "dedup TEXT UNIQUE, delivered INTEGER DEFAULT 0, delivered_ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS adjust (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, venue TEXT, "
                "category TEXT, symbol TEXT, strategy TEXT, grp TEXT, qty TEXT, note TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS stops (venue TEXT, symbol TEXT, price TEXT, ts REAL, "
                "PRIMARY KEY (venue, symbol))")
    return con


@contextlib.contextmanager
def _tx(con):
    """Транзакция BEGIN IMMEDIATE: чтение-проверка-запись атомарно и против другого процесса (скрипт владельца)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _rows(sql, args=(), path=None):
    if not os.path.exists(path or DB_PATH):   # чтение базу не создаёт
        return []
    con = _connect(path)
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def get(client_id, path=None):
    rows = _rows("SELECT * FROM orders WHERE client_id=?", (client_id,), path)
    return rows[0] if rows else None


def _emit_in(con, event, client_id="", state="", note="", dedup=None):
    """Событие владельцу в outbox — в текущей транзакции. Повтор того же (dedup) не пишется: → событие или None."""
    key = dedup or f"{client_id}|{event}|{note}"
    cur = con.execute("INSERT OR IGNORE INTO outbox (ts, client_id, event, state, note, dedup) VALUES (?, ?, ?, ?, ?, ?)",
                      (time.time(), client_id, event, state, note, key))
    return event if cur.rowcount == 1 else None


def _set_stop(con, venue, symbol, price):
    con.execute("INSERT OR REPLACE INTO stops (venue, symbol, price, ts) VALUES (?, ?, ?, ?)",
                (venue, symbol, str(price), time.time()))


def _transition(client_id, path=None, *, expect=None, event=None, **fields):
    """Изменить строку и (если event) записать событие в outbox — одной транзакцией. Смена state — только по
    TRANSITIONS (иначе ValueError, ничего не меняется). expect — версия, с которой работал вызывающий: не совпала —
    (None, None), ничего не меняется. → (строка, записанное событие или None — такое уже было)."""
    con = _connect(path)
    try:
        with _tx(con):
            cur = con.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
            if cur is None:
                raise ValueError(f"нет ордера {client_id}")
            if expect is not None and cur["version"] != expect:
                return None, None
            if "state" in fields and fields["state"] not in TRANSITIONS.get(cur["state"], set()):
                raise ValueError(f"переход {cur['state']} → {fields['state']} запрещён")
            fields["updated_ts"] = time.time()
            con.execute(f"UPDATE orders SET {', '.join(f'{k}=?' for k in fields)}, version=version+1 "
                        f"WHERE client_id=?", (*fields.values(), client_id))
            row = dict(con.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone())
            if row["purpose"] == "open" and row["stop_loss"] and row["state"] in ("open", "filled") \
                    and cur["state"] not in ("open", "filled"):
                _set_stop(con, row["venue"], row["symbol"], row["stop_loss"])   # биржа приняла ордер со стопом
            emitted = _emit_in(con, event, client_id, row["state"], row["note"]) if event else None
        return row, emitted
    finally:
        con.close()


def _update(client_id, path=None, **fields):
    """Обновить строку без события и без сверки версии; смена state — только по TRANSITIONS (иначе ValueError)."""
    return _transition(client_id, path, **fields)[0]


def emit(event, note, client_id="", dedup=None, path=None):
    """Событие владельцу, не привязанное к смене состояния ордера (позицию закрыла биржа, сбой сверки)."""
    con = _connect(path)
    try:
        with _tx(con):
            return _emit_in(con, event, client_id, "", note, dedup)
    finally:
        con.close()


def pending_events(limit=50, path=None):
    """Недоставленные события владельцу — по порядку."""
    return _rows("SELECT * FROM outbox WHERE delivered=0 ORDER BY id LIMIT ?", (limit,), path)


def mark_delivered(ids, path=None):
    """Отметить события доставленными (после отправки владельцу)."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    con = _connect(path)
    try:
        with _tx(con):
            cur = con.execute(f"UPDATE outbox SET delivered=1, delivered_ts=? WHERE id IN ({','.join('?' * len(ids))})",
                              (time.time(), *ids))
        return cur.rowcount
    finally:
        con.close()


def new_client_id(now=None):
    """Свежий клиентский id: "t" + ГГММДДччммсс (МСК) + 8 hex — 21 символ [a-z0-9], годится обеим биржам."""
    d = datetime.fromtimestamp(time.time() if now is None else now, MSK)
    return f"t{d:%y%m%d%H%M%S}{secrets.token_hex(4)}"


def _insert_intent(order, purpose, strategy, mode, notional, path=None, group=""):
    """Намерение (state=prepared) со свежим id и ровно теми параметрами, что уйдут на биржу — до любого запроса."""
    con = _connect(path)
    try:
        for _ in range(5):
            cid = new_client_id()
            method, api_path, params = venues.create_call(order, cid)
            try:
                with _tx(con):
                    con.execute(
                        "INSERT INTO orders (client_id, created_ts, updated_ts, venue, category, symbol, side, "
                        "order_type, qty, price, reduce_only, stop_loss, purpose, strategy, mode, notional, "
                        "method, path, params, state, grp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, 'prepared', ?)",
                        (cid, time.time(), time.time(), order.venue, order.category, order.symbol, order.side,
                         order.order_type, venues.fmt(order.qty),
                         "" if order.price is None else venues.fmt(order.price), int(order.reduce_only),
                         "" if order.stop_loss is None else venues.fmt(order.stop_loss), purpose,
                         strategy, mode, "" if notional is None else str(notional), method, api_path,
                         json.dumps(params, separators=(",", ":")), str(group or "")))
                break
            except sqlite3.IntegrityError:   # совпал id — берём другой
                continue
        else:
            raise RuntimeError("не удалось выбрать свободный клиентский id")
    finally:
        con.close()
    return get(cid, path)


# --- запросы к журналу ---

def active(now=None, path=None):
    """Незавершённые ордера (open/unknown) ЛЮБОГО возраста — их сверяет reconcile (старые не выпадают молча: не
    найден на бирже — unknown и блок открытий до разбора владельцем)."""
    marks = ",".join("?" * len(ACTIVE))
    return _rows(f"SELECT * FROM orders WHERE state IN ({marks}) ORDER BY id", ACTIVE, path)


def blocking(path=None):
    """Ордера, из-за которых новые открытия запрещены: unknown любого возраста и брошенные prepared/sending (не те,
    что сейчас отправляет этот процесс)."""
    marks = ",".join("?" * len(BLOCKING))
    rows = _rows(f"SELECT * FROM orders WHERE state IN ({marks}) ORDER BY id", BLOCKING, path)
    return [r for r in rows if r["state"] == "unknown" or r["client_id"] not in _inflight]


def _filled(r):
    """Исполненное количество строки: filled; для filled без числа — всё количество; отклонённая — 0."""
    if r["state"] == "rejected":
        return D0
    f = venues.dec(r["filled"])
    if r["state"] == "filled" and f is None:
        return venues.dec(r["qty"]) or D0
    return f if f is not None and f > 0 else D0


def _key_book():
    return {"net": D0, "pending_open": D0, "pending_side": None, "pending_reduce": {"buy": D0, "sell": D0},
            "stop": False, "px": None}


def _price_hint(r):
    for v in (r["avg_price"], r["price"]):
        d = venues.dec(v)
        if d and d > 0:
            return d
    n, q = venues.dec(r["notional"]), venues.dec(r["qty"])
    return n / q if n and q else None


def bot_book(venue, category, symbol, path=None):
    """Позиция бота по символу биржи — по журналу: {net (знаковое сальдо: исполненные ордера ± поправки), keys
    {(стратегия, группа): {net, pending_open, pending_side, pending_reduce {buy, sell}, stop, px}}, client_ids (все id
    бота по символу), active (open/unknown), uncertain (prepared/sending/unknown), pending_reduce {buy, sell}, stop
    (последний стоп позиции бота или None)}."""
    rows = _rows("SELECT * FROM orders WHERE venue=? AND category=? AND symbol=? ORDER BY id", (venue, category, symbol),
                 path)
    adj = _rows("SELECT * FROM adjust WHERE venue=? AND category=? AND symbol=? ORDER BY id", (venue, category, symbol),
                path)
    stop = _rows("SELECT price FROM stops WHERE venue=? AND symbol=?", (venue, symbol), path)
    book = {"net": D0, "keys": {}, "client_ids": set(), "active": [], "uncertain": [],
            "pending_reduce": {"buy": D0, "sell": D0}, "stop": venues.dec(stop[0]["price"]) if stop else None}
    for r in rows:
        book["client_ids"].add(r["client_id"])
        if r["state"] == "rejected":
            continue
        k = book["keys"].setdefault((r["strategy"] or "", r["grp"] or ""), _key_book())
        f = _filled(r)
        signed = f if r["side"] == "buy" else -f
        k["net"] += signed
        book["net"] += signed
        k["px"] = _price_hint(r) or k["px"]
        if r["state"] in ACTIVE:
            book["active"].append(r["client_id"])
        if r["state"] in BLOCKING:
            book["uncertain"].append(r["client_id"])
        if r["state"] in UNSETTLED:
            rem = max((venues.dec(r["qty"]) or D0) - f, D0)
            if r["purpose"] == "open":
                k["pending_open"] += rem
                k["pending_side"] = r["side"]
            else:
                k["pending_reduce"][r["side"]] += rem
                book["pending_reduce"][r["side"]] += rem
        if r["purpose"] == "open" and r["stop_loss"]:
            k["stop"] = True
    for a in adj:
        q = venues.dec(a["qty"]) or D0
        book["keys"].setdefault((a["strategy"] or "", a["grp"] or ""), _key_book())["net"] += q
        book["net"] += q
    return book


def exposure(prices=None, path=None):
    """Открытое ботом и ожидающие ордера на открытие — строки для risk (ctx.positions / guard_open): по (биржа,
    категория, символ, стратегия, группа) с ненулевым размером: {venue, category, symbol, strategy, group, side long|
    short, qty (|сальдо| + ожидающие открытия), net, pending, px, notional, stop}. prices — {(биржа, символ): цена}
    свежая цена вместо цены из журнала."""
    out = []
    seen = {(r["venue"], r["category"], r["symbol"]) for r in _rows("SELECT DISTINCT venue, category, symbol FROM "
                                                                      "orders", (), path)}
    seen |= {(r["venue"], r["category"], r["symbol"]) for r in _rows("SELECT DISTINCT venue, category, symbol FROM "
                                                                       "adjust", (), path)}
    for venue, category, symbol in sorted(seen):
        book = bot_book(venue, category, symbol, path)
        for (strategy, group), k in book["keys"].items():
            qty = abs(k["net"]) + k["pending_open"]
            if qty <= 0:
                continue
            side = ("long" if k["net"] > 0 else "short") if k["net"] else \
                ("long" if k["pending_side"] == "buy" else "short")
            px = (prices or {}).get((venue, symbol)) or k["px"]
            if not px:
                raise ValueError(f"{venue} {symbol}: нет цены для оценки позиции бота")
            out.append({"venue": venue, "category": category, "symbol": symbol, "strategy": strategy, "group": group,
                        "side": side, "qty": qty, "net": k["net"], "pending": k["pending_open"], "px": px,
                        "notional": qty * px, "stop": k["stop"]})
    return out


SPOT_FEE_BUFFER = Decimal("0.002")   # доля купленного, которую считаем удержанной комиссией, если биржа не сказала больше


def spot_inventory(venue, symbol, path=None):
    """Сколько монеты на споте купил сам бот и ещё не продал — по журналу, консервативно (для продажи на споте:
    монеты владельца бот не трогает). Покупки — только исполненное количество минус комиссия (не меньше
    SPOT_FEE_BUFFER): у Bybit комиссия спот-покупки удерживается в самой монете. Продажи — полное количество, пока
    ордер не завершён (закрытая — исполненная часть, отклонённая — 0). Ордера с неясным исходом: покупка — 0,
    продажа — полностью."""
    rows = _rows("SELECT side, qty, state, filled, fee FROM orders WHERE venue=? AND category='spot' AND symbol=?",
                 (venue, symbol), path)
    held = D0
    for r in rows:
        qty, filled, fee = (venues.dec(r["qty"]) or D0, venues.dec(r["filled"]), venues.dec(r["fee"]))
        if r["side"] == "buy":
            if r["state"] in ("open", "filled", "closed") and filled and filled > 0:
                held += filled - max(fee or D0, filled * SPOT_FEE_BUFFER)
        elif r["state"] == "rejected":
            continue
        elif r["state"] == "closed" and filled is not None:
            held -= filled
        else:
            held -= qty
    return max(held, D0)


def history(limit=20, path=None):
    return _rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,), path)


def day_start(now=None):
    d = datetime.fromtimestamp(time.time() if now is None else now, MSK)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def order_counts(now=None, path=None):
    """(ордеров за последние 60 с, ордеров за день МСК) — все записанные намерения, в том числе отклонённые."""
    now = time.time() if now is None else now
    minute = _rows("SELECT COUNT(*) AS n FROM orders WHERE created_ts>?", (now - 60,), path)
    day = _rows("SELECT COUNT(*) AS n FROM orders WHERE created_ts>=?", (day_start(now),), path)
    return (minute[0]["n"] if minute else 0), (day[0]["n"] if day else 0)


def add_pnl(venue, symbol, amount, kind, ref, ts=None, path=None):
    """Реализованный результат (сделка, комиссия, фандинг) для дневного стопа; повтор (venue, ref) — не дублируется.
    True — записано, False — уже было."""
    amount = Decimal(str(amount))
    if not amount.is_finite():
        raise ValueError("pnl не число")
    con = _connect(path)
    try:
        with _tx(con):
            cur = con.execute("INSERT OR IGNORE INTO pnl (ts, venue, symbol, amount, kind, ref) VALUES (?, ?, ?, ?, ?, ?)",
                              (time.time() if ts is None else ts, venue, symbol, str(amount), kind, str(ref)))
        return cur.rowcount == 1
    finally:
        con.close()


def pnl_today(now=None, path=None):
    """Сумма реализованного результата за день МСК (убыток — отрицательный)."""
    rows = _rows("SELECT amount FROM pnl WHERE ts>=?", (day_start(now),), path)
    return sum((Decimal(r["amount"]) for r in rows), D0)


def sync_flat(venue, category, symbol, note="позицию бота закрыла биржа (стоп или ликвидация)", path=None):
    """На бирже по символу пусто, у бота по журналу — позиция (ownership.flat_external): поправки обнуляют сальдо
    каждой стратегии/группы бота + событие владельцу. Одной транзакцией."""
    book = bot_book(venue, category, symbol, path)
    con = _connect(path)
    try:
        with _tx(con):
            for (strategy, group), k in book["keys"].items():
                if k["net"]:
                    con.execute("INSERT INTO adjust (ts, venue, category, symbol, strategy, grp, qty, note) VALUES "
                                "(?, ?, ?, ?, ?, ?, ?, ?)", (time.time(), venue, category, symbol, strategy, group,
                                                             str(-k["net"]), note))
            _emit_in(con, "closed_by_venue", "", "", f"{venue} {symbol}: {note} (было {venues.fmt(book['net'])})",
                     dedup=f"flat|{venue}|{category}|{symbol}|{time.time()!r}")
    finally:
        con.close()


def resume(path=None):
    """При старте: ордера, прерванные перезапуском посреди отправки (prepared/sending), — в unknown; их выяснит
    reconcile по клиентскому id. Возвращает эти строки (событие владельцу — в outbox)."""
    rows = _rows("SELECT client_id FROM orders WHERE state IN ('prepared', 'sending')", (), path)
    return [_transition(r["client_id"], path, state="unknown", note="бот перезапустился во время отправки",
                        event="unknown")[0] for r in rows]


def resolve(client_id, state, note, path=None):
    """Разбор unknown владельцем на ПК (после проверки в кабинете биржи): unknown → closed/rejected/filled/open.
    Строку, которую сейчас отправляет submit, разобрать нельзя; разбор меняет версию — отправка её больше не повторит."""
    if client_id in _inflight:
        raise ValueError("ордер ещё отправляется — разбор после окончания отправки")
    row = get(client_id, path)
    if row is None or row["state"] != "unknown" or state not in ("closed", "rejected", "filled", "open"):
        raise ValueError("разобрать можно только unknown")
    new, _ = _transition(client_id, path, expect=row["version"], state=state, note=f"владелец: {note}",
                         event="resolved")
    if new is None:
        raise ValueError("строка изменилась во время разбора — посмотрите ещё раз")
    return new


def close_out(client_id, path=None):
    """Позицию, открытую исполненным ордером, закрыли: filled → closed (вызывает стратегия)."""
    return _update(client_id, path, state="closed")


# --- сверка ответа биржи с намерением ---

def _venue_sym(row):
    """Символ биржи ордера — ровно из параметров создания (TONUSDT → GRAMUSDT решён в момент отправки)."""
    return json.loads(row["params"])["symbol"]


def _mismatch(row, view):
    """Поля ордера с биржи, не совпавшие с намерением (пусто — совпало). Количество и цена — как числа. Символ — ровно
    символ биржи из параметров создания: чужой, неизвестный (например «GRAM-USDT» BingX — другой токен) или пустой —
    несовпадение."""
    bad = []
    if view.get("client_id") != row["client_id"]:
        bad.append("id")
    raw = view.get("raw_symbol")
    if (raw is not None and str(raw).upper() != _venue_sym(row).upper()) \
            or (view.get("symbol") is not None and view["symbol"] != row["symbol"]):
        bad.append("символ")
    if view.get("side") and view["side"] != row["side"]:
        bad.append("сторона")
    if view.get("type") and view["type"] != row["order_type"]:
        bad.append("тип")
    if view.get("qty") is not None and view["qty"] != Decimal(row["qty"]):
        bad.append("количество")
    if row["order_type"] == "limit" and view.get("price") and view["price"] != Decimal(row["price"]):
        bad.append("цена")
    if view.get("reduce_only") is not None and view["reduce_only"] != bool(row["reduce_only"]):
        bad.append("reduceOnly")
    return bad


def _apply_view(row, view, expect=None):
    """Ордер, найденный на бирже (запрос или ответ создания с полями), → строка журнала и событие ("mismatch", "found",
    "filled", "closed", "rejected" или None — не новое). Несовпадение — unknown + тревога (outbox, одна на текст).
    expect — версия (сверка): строку изменили — (None, None)."""
    bad = _mismatch(row, view)
    fields = {"venue_order_id": view.get("order_id") or row["venue_order_id"], "status": view.get("status") or "",
              "filled": "" if view.get("filled") is None else venues.fmt(view["filled"]),
              "avg_price": "" if not view.get("avg_price") else venues.fmt(view["avg_price"]),
              "fee": "" if view.get("fee") is None else venues.fmt(view["fee"])}
    cid = row["client_id"]
    if bad:
        return _transition(cid, expect=expect, state="unknown", note=f"{MISMATCH}: {', '.join(bad)}",
                           event="mismatch", **fields)
    state = view.get("state")
    if state is None:   # незнакомый статус — ничего не утверждаем
        return _transition(cid, expect=expect, state="unknown", note=f"незнакомый статус ордера: {view.get('status')!r}",
                           event="mismatch", **fields)
    if state not in TRANSITIONS[row["state"]]:
        return _transition(cid, expect=expect, state="unknown", note=f"статус {view.get('status')} после {row['state']}",
                           event="mismatch", **fields)
    event = "found" if row["state"] == "unknown" else None
    if state != row["state"] and state in ("filled", "closed", "rejected"):
        event = event or state
    return _transition(cid, expect=expect, state=state, note="" if row["state"] == "unknown" else row["note"],
                       event=event, **fields)


def _accepted(row, data):
    """HTTP 200 + код 0 на создание. Bybit отвечает только {orderId, orderLinkId} (дальше — reconcile), BingX — ордером.
    Id не наш или поля не совпали — unknown + тревога."""
    cid = row["client_id"]
    if row["venue"] == venues.BYBIT:
        if not isinstance(data, dict) or str(data.get("orderLinkId") or "") != cid or not data.get("orderId"):
            return _transition(cid, state="unknown", note=f"{MISMATCH}: id", event="mismatch")
        return _transition(cid, state="open", venue_order_id=str(data["orderId"]), status="accepted")
    view = venues.order_view(venues.BINGX, data)
    if view is None:
        return _transition(cid, state="unknown", note=f"{MISMATCH}: нет ордера в ответе", event="mismatch")
    if view["state"] is None and not view["status"] and not _mismatch(row, view):
        view = dict(view, state="open")   # BingX вернул ордер без статуса — принят, остальное скажет сверка
    return _apply_view(row, view)


def _not_found(row):
    """Сверка не нашла ордер. Создание получало только точные отказы и id биржи ни разу не было — ордера нет:
    rejected. Иначе unknown (в блоке открытий) и одно событие "notfound": владелец проверяет кабинет."""
    cid, ver = row["client_id"], row["version"]
    if row["create_kind"] == "error" and not row["venue_order_id"] and row["state"] == "unknown":
        return _transition(cid, expect=ver, state="rejected", event="rejected")
    if NOT_FOUND in (row["note"] or ""):
        return row, None
    note = f"{row['note']}; {NOT_FOUND}" if row["note"] else NOT_FOUND
    return _transition(cid, expect=ver, state="unknown", note=note, event="notfound")


# --- отправка ---

def _result(state, row=None, reason="", event=None):
    return {"state": state, "row": row, "reason": reason, "event": event}


def _changed(cid):
    row = get(cid)
    return _result(row["state"] if row else "unknown", row, CHANGED, None)


def _may_send(purpose):
    """Можно ли отправить (и повторить) ордер: открытие — только при TRADING=1 и живом режиме; закрытие и стоп
    позиции работают и при выключенной торговле (план: сопровождение, стопы и закрытие не останавливаются)."""
    if purpose == "open":
        return switch.can_open()
    return True, ""


def _effective_mode(mode):
    """Режим открытия: запрошенный вызывающим, но не выше режима выключателя (.env на ПК)."""
    cur = switch.mode()
    m = str(mode or cur).strip().lower()
    return min(m, cur, key=switch.RANK.get) if m in switch.RANK else "paper"


def _pre_open(order, creds, strategy, mode):
    """Проверки открытия без сети (до снимка символа и ещё раз под замком после): → (причина или "", режим)."""
    ok, why = switch.can_open()
    if not ok:
        return why, None
    if strategy not in risk.STRATEGIES:
        return f"стратегия {strategy!r} неизвестна — открытие только от стратегии ядра", None
    eff = _effective_mode(mode)
    if eff not in risk.LIVE_MODES:
        return f"режим {eff}: реальных открытий нет", None
    if not creds:
        return f"{order.venue}: нет торгового ключа", None
    why = keys.check_status(order.venue, creds)[0]
    if why:
        return f"{order.venue}: {why}", None
    try:
        venues.prepare(order.venue, *venues.create_call(order, new_client_id()), creds, timestamp=0)
    except ValueError as e:
        return accounts._scrub(str(e), *creds), None
    b = blocking()
    if b:
        return f"есть ордера с неясным исходом ({len(b)}) — новые открытия запрещены", None
    lim = risk.limits(strategy, eff)
    minute, day = order_counts()
    if minute + 1 > lim["orders_per_min"]:
        return f"лимит ордеров в минуту: {lim['orders_per_min']}", None
    if day + 1 > lim["orders_per_day"]:
        return f"лимит ордеров в день: {lim['orders_per_day']}", None
    return "", eff


def _guard(order, snap, strategy, group, mode):
    """Проверки открытия по свежему снимку символа (под state_lock): чужое, данные биржи, risk.guard_open."""
    book = bot_book(order.venue, order.category, order.symbol)
    if order.category != "spot" and ownership.flat_external(snap, book):
        sync_flat(order.venue, order.category, order.symbol)
        book = bot_book(order.venue, order.category, order.symbol)
    foreign = ownership.foreign(snap, book)
    if foreign:
        return f"{risk.FOREIGN}: " + "; ".join(foreign)[:300]
    if foreign is None or snap.errors:
        return "данные биржи по символу не прочитаны — открытие запрещено: " + "; ".join(snap.errors)[:300]
    facc = None
    if snap.margin_mode == "cross":
        books = {sym: bot_book(order.venue, order.category, sym)["net"] for sym in venues.SYMBOLS}
        facc = ownership.foreign_account(snap, books)
    try:
        exp = exposure({(order.venue, order.symbol): snap.mark})
    except ValueError as e:
        return f"позиции бота не оценить: {e}"
    reasons = risk.guard_open(order, strategy, group, mode, mark=snap.mark, instrument=snap.instrument,
                              leverage=snap.leverage, margin_mode=snap.margin_mode, foreign=foreign,
                              foreign_account=facc, exposure=exp, realized_today=pnl_today())
    return "; ".join(reasons)


def _reduce_problem(order, strategy, group):
    """Закрытие/стоп — только своей позиции бота: против её стороны и не больше её (без уже отправленных закрытий);
    со стратегией — ещё и не больше позиции этой стратегии/группы. Спот — только купленное ботом."""
    if order.category == "spot":
        held = spot_inventory(order.venue, order.symbol)
        if order.qty > held:
            return (f"продать на споте можно только купленное ботом: {venues.fmt(held)} {order.symbol[:-4]} по "
                    f"журналу, в ордере {venues.fmt(order.qty)}")
        return ""
    book = bot_book(order.venue, order.category, order.symbol)
    scopes = [(book["net"], book["pending_reduce"])]
    if strategy:
        k = book["keys"].get((strategy, str(group or "")))
        scopes.append((k["net"], k["pending_reduce"]) if k else (D0, {"buy": D0, "sell": D0}))
    rooms = []
    for net, pending in scopes:
        if net == 0:
            return "у бота нет своей позиции по символу (по журналу) — закрывать нечего; чужие позиции бот не трогает"
        side = "sell" if net > 0 else "buy"
        if order.side != side:
            return "закрытие — только против своей позиции бота"
        rooms.append(abs(net) - pending[side])
    v = risk.check_close(order.qty, min(rooms), True)
    return "" if v.ok else "; ".join(v.reasons)


async def _create(s, row, creds):
    params = json.loads(row["params"])   # ровно записанные параметры, в том же порядке
    try:
        status, j = await venues.call(s, row["venue"], row["method"], row["path"], params, creds)
    except Exception as e:   # таймаут, обрыв — ордер мог и уйти
        return "ambiguous", None, accounts.api_error_text(e)
    kind, data, _, msg = venues.outcome(row["venue"], status, j, creds)
    return kind, data, msg


async def submit(s, order, creds, *, purpose="open", strategy="", group="", mode="", notional=None, precheck=None):
    """Отправить ордер. Один вызов — не больше одного нового клиентского id.

    Открытие: проверки без сети (выключатель, стратегия, режим, проверенный ключ, сборка параметров, unknown, частота)
    → под замком символа: сверка незавершённых ордеров бота по символу, снимок символа с биржи → под state_lock те же
    проверки ещё раз + чужое/свои встречные/шаги/итоговый размер/плечо/маржа (_guard) + precheck() вызывающего →
    намерение (prepared → sending). Закрытие/стоп: только своя позиция бота (_reduce_problem) → намерение.
    Затем запрос (вне state_lock):
    - ok — принят (Bybit: open; BingX: по ответу), ответ не совпал с намерением — unknown + "mismatch";
    - точный отказ / «id занят» — запрос по id: найден — принимаем; «не найден» и неясностей не было — rejected;
    - неясный исход — unknown, пауза, запрос по id: найден — принимаем; «не найден» — повтор с тем же id и теми же
      параметрами (≤ MAX_RESEND; открытие — только пока торговля включена; только если строку никто не менял);
      иначе unknown (разберёт reconcile).
    notional вызывающего не нужен: номинал открытия ядро считает само по цене биржи.
    Возвращает {"state": refused|rejected|open|filled|closed|unknown, "row", "reason", "event"}."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose {purpose!r}")
    if purpose == "open" and order.reducing:
        raise ValueError("ордер на открытие не может быть уменьшающим")
    if purpose != "open" and not order.reducing:
        raise ValueError("закрытие/стоп — только уменьшающий ордер (reduceOnly; на споте — продажа)")
    group = str(group or "")
    if purpose == "open":
        return await _submit_open(s, order, creds, strategy, group, mode, precheck)
    return await _submit_reduce(s, order, creds, purpose, strategy, group, precheck)


async def _submit_open(s, order, creds, strategy, group, mode, precheck):
    why, _ = _pre_open(order, creds, strategy, mode)
    if why:
        return _result("refused", reason=why)
    venue_sym = venues.create_call(order, new_client_id())[2]["symbol"]
    cid = None
    async with symbol_lock(order.venue, order.symbol):
        try:
            await refresh_symbol(s, order.venue, order.category, order.symbol, creds)
            snap = await ownership.fetch(s, order.venue, order.category, order.symbol, venue_sym, creds)
            async with state_lock():
                why, eff = _pre_open(order, creds, strategy, mode)   # за время сети всё могло измениться
                if not why:
                    why = _guard(order, snap, strategy, group, eff)
                if not why and precheck is not None:
                    why = precheck() or ""
                if why:
                    logger.info("открытие %s %s отклонено: %s", order.venue, order.symbol, why[:200])
                    return _result("refused", reason=why)
                entry = order.price if order.order_type == "limit" else snap.mark
                row = _insert_intent(order, "open", strategy, eff, order.qty * entry, group=group)
                cid = row["client_id"]
                _inflight.add(cid)
                row = _update(cid, state="sending", posts=1)
            return await _drive(s, row, creds, "open")
        finally:
            _inflight.discard(cid)


async def _submit_reduce(s, order, creds, purpose, strategy, group, precheck):
    if not creds:
        return _result("refused", reason=f"{order.venue}: нет торгового ключа")
    try:
        venues.prepare(order.venue, *venues.create_call(order, new_client_id()), creds, timestamp=0)
    except ValueError as e:
        return _result("refused", reason=accounts._scrub(str(e), *creds))
    await refresh_symbol(s, order.venue, order.category, order.symbol, creds)   # свежие исполнения своих ордеров
    cid = None
    try:
        async with state_lock():
            why = _reduce_problem(order, strategy, group)
            if not why and precheck is not None:
                why = precheck() or ""
            if why:
                return _result("refused", reason=why)
            row = _insert_intent(order, purpose, strategy, switch.mode(), None, group=group)
            cid = row["client_id"]
            _inflight.add(cid)
            row = _update(cid, state="sending", posts=1)
        return await _drive(s, row, creds, purpose)
    finally:
        _inflight.discard(cid)


async def _drive(s, row, creds, purpose):
    """Запрос создания и разбор исхода (см. submit). Сеть и паузы — вне state_lock; каждая запись — под ним."""
    cid = row["client_id"]
    logger.info("ордер %s: отправка %s %s %s %s", cid, row["venue"], row["symbol"], row["side"], row["qty"])
    kind, data, msg = await _create(s, row, creds)
    posts, ambiguous = 1, False
    while True:
        async with state_lock():
            try:
                if kind == "ok":
                    row, event = _accepted(get(cid), data)
                    logger.info("ордер %s: %s", cid, row["state"])
                    return _result(row["state"], row, row["note"], event)
                ambiguous = ambiguous or kind in ("ambiguous", "duplicate")
                row = _update(cid, state="unknown", note=msg, create_kind="ambiguous" if ambiguous else "error")
            except ValueError:   # строку уже перевёл кто-то другой (владелец/сверка)
                return _changed(cid)
            ver = row["version"]
        logger.warning("ордер %s: %s (%s)", cid, "исход неясен" if kind != "rejected" else "отказ", msg)
        if kind == "ambiguous":
            await asyncio.sleep(RETRY_DELAY * posts)   # запрос точно закончился; даём бирже время
        fkind, view, _ = await venues.find_order(s, row["venue"], row["category"], _venue_sym(row), cid, creds)
        async with state_lock():
            cur = get(cid)
            try:
                if fkind == "found":
                    row, event = _apply_view(cur, view)
                    return _result(row["state"], row, row["note"], event or "found")
                if cur["version"] != ver:
                    return _changed(cid)
                if fkind == "notfound" and kind == "rejected" and not ambiguous:
                    row = _update(cid, state="rejected", note=msg)
                    logger.info("ордер %s: отклонён", cid)
                    return _result("rejected", row, msg)
                if fkind != "notfound" or kind != "ambiguous" or posts > MAX_RESEND:
                    row, _ = _transition(cid, event="unknown")
                    return _result("unknown", row, row["note"], "unknown")
            except ValueError:
                return _changed(cid)
        await asyncio.sleep(RETRY_DELAY * posts)
        async with state_lock():   # решение о повторе — по той же версии строки и прямо перед отправкой
            cur = get(cid)
            if cur["version"] != ver or cur["state"] != "unknown" or cur["cancel_requested"]:
                return _changed(cid)
            ok, why = _may_send(purpose)
            if not ok:
                row, _ = _transition(cid, note=f"{cur['note']}; {why} — повтор не отправлен", event="unknown")
                return _result("unknown", row, row["note"], "unknown")
            posts += 1
            row, _ = _transition(cid, expect=ver, posts=posts)
            if row is None:
                return _changed(cid)
        kind, data, msg = await _create(s, row, creds)   # тот же id, те же параметры; новые время и подпись


async def cancel(s, client_id, creds):
    """Снять ордер из нашего журнала по клиентскому id (ордера владельца бот не трогает: чужой id — ValueError):
    (вид, текст) по venues.outcome. Сначала отметка «запрошена отмена» (новая версия строки — идущая отправка этот
    ордер больше не повторит), итог в журнале выставит reconcile."""
    row = get(client_id)
    if row is None:
        raise ValueError(f"ордера {client_id!r} нет в журнале ядра")
    method, api_path, params = venues.cancel_call(row["venue"], row["category"], _venue_sym(row), client_id)
    async with state_lock():
        _update(client_id, cancel_requested=1)
    try:
        status, j = await venues.call(s, row["venue"], method, api_path, params, creds)
    except Exception as e:
        return "ambiguous", accounts.api_error_text(e)
    kind, _, _, msg = venues.outcome(row["venue"], status, j, creds)
    return kind, msg


# --- сверка ---

async def _reconcile_one(s, row, creds):
    kind, view, _ = await venues.find_order(s, row["venue"], row["category"], _venue_sym(row), row["client_id"],
                                            creds)
    async with state_lock():
        cur = get(row["client_id"])
        if cur is None or cur["version"] != row["version"] or cur["client_id"] in _inflight:
            return None   # строку изменили за время запроса — разберёт следующая сверка по свежим данным
        if kind == "found":
            new, event = _apply_view(cur, view, expect=cur["version"])
        elif kind == "notfound" and cur["state"] == "unknown":
            new, event = _not_found(cur)
        elif kind == "notfound":   # открытый ордер пропал из всех списков биржи — не угадываем
            new, event = _transition(cur["client_id"], expect=cur["version"], state="unknown",
                                     note=f"{NOT_FOUND} (был {cur['state']})", event="notfound")
        else:
            return None
    return (event, new) if event and new else None


async def _reconcile_rows(s, rows, creds_for):
    events = []
    for row in rows:
        try:
            creds = creds_for(row["venue"])
            if not creds:
                continue
            got = await _reconcile_one(s, row, creds)
            if got:
                events.append(got)
        except Exception as e:   # noqa: BLE001 — сбой одной строки не останавливает сверку остальных
            logger.warning("сверка %s: %s", row["client_id"], type(e).__name__)
            try:
                emit("reconcile_error", f"сверка ордера не удалась: {type(e).__name__}", client_id=row["client_id"])
            except Exception:   # noqa: BLE001
                logger.warning("сверка %s: событие не записано", row["client_id"])
    return events


async def reconcile(s, creds_for, now=None):
    """Сверка с биржей: брошенные prepared/sending (не идущие сейчас) → unknown; каждый open/unknown любого возраста —
    запрос по клиентскому id. Снимок строк — под коротким замком, запросы — вне его, запись — только если строка не
    изменилась. [(событие, строка)] — что нового (то же уже лежит в outbox). Ошибка запроса — строка не меняется.
    Повторов отправки тут нет."""
    events = []
    async with state_lock():
        for r in _rows("SELECT client_id, version FROM orders WHERE state IN ('prepared', 'sending') ORDER BY id"):
            if r["client_id"] in _inflight:
                continue
            row, event = _transition(r["client_id"], expect=r["version"], state="unknown", note="отправка прервана",
                                     event="unknown")
            if row and event:
                events.append((event, row))
        rows = [r for r in active() if r["client_id"] not in _inflight]
    return events + await _reconcile_rows(s, rows, creds_for)


async def refresh_symbol(s, venue, category, symbol, creds):
    """Сверить незавершённые ордера бота по одному символу (перед открытием/закрытием: свежие исполнения)."""
    async with state_lock():
        rows = [r for r in active() if (r["venue"], r["category"], r["symbol"]) == (venue, category, symbol)
                and r["client_id"] not in _inflight]
    return await _reconcile_rows(s, rows, lambda v: creds)


# --- плечо, режим маржи, стоп позиции: только если на символе нет чужого ---

def _perp_category(venue):
    return "linear" if venue == venues.BYBIT else "swap"


def own_positions(positions, path=None):
    """Позиции с биржи (venues.positions — все, и владельца) с пометкой, что из них бота (ownership.annotate): для
    сопровождения (risk.position_actions трогает только owned и не больше bot_size)."""
    books = {(p["venue"], p["symbol"]): bot_book(p["venue"], _perp_category(p["venue"]), p["symbol"], path)["net"]
             for p in positions or ()}
    return ownership.annotate(positions, books)


async def _symbol_check(s, venue, symbol, venue_sym, creds):
    """Снимок символа (без рынка) и сверка с журналом: → (снимок, книга бота, причина отказа или "")."""
    category = _perp_category(venue)
    await refresh_symbol(s, venue, category, symbol, creds)
    snap = await ownership.fetch(s, venue, category, symbol, venue_sym, creds, market=False)
    async with state_lock():
        book = bot_book(venue, category, symbol)
        foreign = ownership.foreign(snap, book)
    if foreign is None:
        return snap, book, "позиции и ордера символа не прочитаны — ничего не меняем: " + "; ".join(snap.errors)[:200]
    if foreign:
        return snap, book, f"{risk.FOREIGN}: " + "; ".join(foreign)[:300]
    if book["uncertain"]:
        return snap, book, "у бота по символу ордера с неясным исходом — сначала сверка"
    return snap, book, ""


async def set_leverage(s, venue, symbol, leverage, creds):
    """Плечо символа (целое 1..3): только если по символу нет чужой позиции и чужих ордеров (плечо — на всю позицию
    символа). Bybit 110043 «не изменилось» — успех. → (вид, текст): ok / refused / rejected / ambiguous …"""
    venue_sym = venues.venue_symbol(venue, _perp_category(venue), symbol)
    method, path, params = venues.leverage_call(venue, venue_sym, leverage)   # ValueError — вне 1..3
    async with symbol_lock(venue, symbol):
        _, _, why = await _symbol_check(s, venue, symbol, venue_sym, creds)
        if why:
            return "refused", why
        try:
            status, j = await venues.call(s, venue, method, path, params, creds)
        except Exception as e:
            return "ambiguous", accounts.api_error_text(e)
        kind, _, _, msg = venues.leverage_outcome(venue, status, j, creds)
        return kind, msg


async def set_margin_isolated(s, symbol, creds):
    """BingX: изолированная маржа символа — только если по символу нет чужой позиции и чужих ордеров."""
    venue_sym = venues.venue_symbol(venues.BINGX, "swap", symbol)
    method, path, params = venues.margin_isolated_call(venue_sym)
    async with symbol_lock(venues.BINGX, symbol):
        _, _, why = await _symbol_check(s, venues.BINGX, symbol, venue_sym, creds)
        if why:
            return "refused", why
        try:
            status, j = await venues.call(s, venues.BINGX, method, path, params, creds)
        except Exception as e:
            return "ambiguous", accounts.api_error_text(e)
        kind, _, _, msg = venues.outcome(venues.BINGX, status, j, creds)
        return kind, msg


async def set_stop(s, venue, symbol, stop, creds):
    """Стоп позиции (Bybit trading-stop, tpslMode=Full — на ВСЮ позицию символа): только если вся позиция символа —
    бота и чужих ордеров нет; стоп — с правильной стороны от mark и раньше ликвидации. Замка символа не ждёт (стоп —
    приоритет). Успех — стоп записан в журнал (по нему ownership узнаёт стоп позиции бота)."""
    if venue != venues.BYBIT:
        return "refused", "BingX: стоп позиции меняется только новым ордером — в ядре не поддерживается"
    stop = Decimal(str(stop))
    venue_sym = venues.venue_symbol(venue, "linear", symbol)
    method, path, params = venues.stop_call(venue_sym, stop)
    snap, book, why = await _symbol_check(s, venue, symbol, venue_sym, creds)
    if why:
        return "refused", why
    if not ownership.owned_whole(snap, book):
        return "refused", "у бота нет своей позиции по символу — стоп ставить не на что"
    pos = snap.position["rows"][0]
    mark, liq, long = pos["mark"], pos["liq"], snap.position["net"] > 0
    if not mark:
        return "refused", "нет mark позиции — стоп не проверить"
    if (long and not stop < mark) or (not long and not stop > mark):
        return "refused", "стоп не с той стороны от mark"
    if liq is not None and ((long and stop <= liq) or (not long and stop >= liq)):
        return "refused", "стоп за ценой ликвидации — сработает позже ликвидации"
    try:
        status, j = await venues.call(s, venue, method, path, params, creds)
    except Exception as e:
        return "ambiguous", accounts.api_error_text(e)
    kind, _, _, msg = venues.outcome(venue, status, j, creds)
    if kind == "ok":
        con = _connect()
        try:
            with _tx(con):
                _set_stop(con, venue, symbol, venues.fmt(stop))
        finally:
            con.close()
    return kind, msg
