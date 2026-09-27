"""Журнал ордеров торгового ядра — data/trading.db, по схеме журнала выплат (payouts.py).

- Намерение со свежим клиентским id (Bybit orderLinkId / BingX clientOrderId) пишется в журнал ДО любого запроса,
  вместе с ровно теми бизнес-параметрами, что уйдут на биржу (повтор — те же параметры и тот же id; меняются только
  время и подпись).
- Состояния: prepared → sending → open / filled → closed; rejected (биржа точно не создала ордер); unknown (исход неясен
  или ответ не совпал с намерением — тревога владельцу). Переходы — только по таблице TRANSITIONS.
- Неясный исход (таймаут, 5xx, 3xx, не JSON, незнакомый код) — пауза, запрос ордера по клиентскому id; «не найден» —
  повтор с тем же id (не больше MAX_RESEND раз и, для ордеров на открытие, только пока торговля включена); иначе
  unknown. Точный отказ тоже сверяется запросом по id: «не найден» и неясностей не было — rejected.
- Любой unknown (и оставшийся после перезапуска prepared/sending) блокирует новые открытия (`blocking`, risk.py).
- `resume()` при старте: prepared/sending → unknown. `reconcile()` — сверка незавершённых ордеров с биржей.
- Отправка и сверка — под одним asyncio.Lock на цикл событий: одна операция за раз.
"""
import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import accounts
from trading import switch, venues

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "trading.db")
MSK = timezone(timedelta(hours=3))

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
ACTIVE = ("open", "unknown")                     # их опрашивает reconcile
BLOCKING = ("unknown", "prepared", "sending")    # любой такой ордер — новых открытий нет
PURPOSES = ("open", "close", "stop")
MAX_RESEND = 2        # повторов с тем же id после неясного исхода и «не найден»
RETRY_DELAY = 3       # сек × номер попытки: пауза перед запросом статуса и перед повтором
RECONCILE_DAYS = 7    # старше — не опрашиваем (но unknown всё равно блокирует открытия)
MISMATCH = "ответ биржи не совпал с намерением"
NOT_FOUND = "биржа не находит ордер по клиентскому id"

_lock = {"loop": None, "lock": None}


def lock():
    """Один asyncio.Lock на цикл событий — общий для отправки и сверки."""
    loop = asyncio.get_running_loop()
    if _lock["loop"] is not loop:
        _lock.update(loop=loop, lock=asyncio.Lock())
    return _lock["lock"]


# --- база ---

def _connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT UNIQUE NOT NULL, created_ts REAL, updated_ts REAL, "
        "venue TEXT, category TEXT, symbol TEXT, side TEXT, order_type TEXT, qty TEXT, price TEXT DEFAULT '', "
        "reduce_only INTEGER DEFAULT 0, stop_loss TEXT DEFAULT '', "
        "purpose TEXT, strategy TEXT DEFAULT '', mode TEXT DEFAULT '', notional TEXT DEFAULT '', "
        "method TEXT, path TEXT, params TEXT, state TEXT, venue_order_id TEXT DEFAULT '', status TEXT DEFAULT '', "
        "filled TEXT DEFAULT '', avg_price TEXT DEFAULT '', fee TEXT DEFAULT '', posts INTEGER DEFAULT 0, "
        "create_kind TEXT DEFAULT '', "
        "note TEXT DEFAULT '')")
    con.execute("CREATE TABLE IF NOT EXISTS pnl (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, venue TEXT, "
                "symbol TEXT, amount TEXT, kind TEXT, ref TEXT, UNIQUE(venue, ref))")
    return con


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


def _update(client_id, path=None, **fields):
    """Обновить строку; смена state — только по TRANSITIONS (иначе ValueError, строка не меняется)."""
    if "state" in fields:
        cur = get(client_id, path)
        if cur is None:
            raise ValueError(f"нет ордера {client_id}")
        if fields["state"] not in TRANSITIONS.get(cur["state"], set()):
            raise ValueError(f"переход {cur['state']} → {fields['state']} запрещён")
    fields["updated_ts"] = time.time()
    con = _connect(path)
    try:
        with con:
            con.execute(f"UPDATE orders SET {', '.join(f'{k}=?' for k in fields)} WHERE client_id=?",
                        (*fields.values(), client_id))
    finally:
        con.close()
    return get(client_id, path)


def new_client_id(now=None):
    """Свежий клиентский id: "t" + ГГММДДччммсс (МСК) + 8 hex — 21 символ [a-z0-9], годится обеим биржам."""
    d = datetime.fromtimestamp(time.time() if now is None else now, MSK)
    return f"t{d:%y%m%d%H%M%S}{secrets.token_hex(4)}"


def _insert_intent(order, purpose, strategy, mode, notional, path=None):
    """Намерение (state=prepared) со свежим id и ровно теми параметрами, что уйдут на биржу — до любого запроса."""
    con = _connect(path)
    try:
        for _ in range(5):
            cid = new_client_id()
            method, api_path, params = venues.create_call(order, cid)
            try:
                with con:
                    con.execute(
                        "INSERT INTO orders (client_id, created_ts, updated_ts, venue, category, symbol, side, "
                        "order_type, qty, price, reduce_only, stop_loss, purpose, strategy, mode, notional, "
                        "method, path, params, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, 'prepared')",
                        (cid, time.time(), time.time(), order.venue, order.category, order.symbol, order.side,
                         order.order_type, venues.fmt(order.qty),
                         "" if order.price is None else venues.fmt(order.price), int(order.reduce_only),
                         "" if order.stop_loss is None else venues.fmt(order.stop_loss), purpose,
                         strategy, mode, "" if notional is None else str(notional), method, api_path,
                         json.dumps(params, separators=(",", ":"))))
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
    """Незавершённые ордера (open/unknown) не старше RECONCILE_DAYS — их сверяет reconcile."""
    since = (time.time() if now is None else now) - RECONCILE_DAYS * 86400
    marks = ",".join("?" * len(ACTIVE))
    return _rows(f"SELECT * FROM orders WHERE state IN ({marks}) AND created_ts>=? ORDER BY id", (*ACTIVE, since), path)


def blocking(path=None):
    """Ордера, из-за которых новые открытия запрещены: unknown любого возраста и брошенные prepared/sending."""
    marks = ",".join("?" * len(BLOCKING))
    return _rows(f"SELECT * FROM orders WHERE state IN ({marks}) ORDER BY id", BLOCKING, path)


SPOT_FEE_BUFFER = Decimal("0.002")   # доля купленного, которую считаем удержанной комиссией, если биржа не сказала больше


def spot_inventory(venue, symbol, path=None):
    """Сколько монеты на споте купил сам бот и ещё не продал — по журналу, консервативно (для продажи на споте:
    монеты владельца бот не трогает). Покупки — только исполненное количество минус комиссия (не меньше
    SPOT_FEE_BUFFER): у Bybit комиссия спот-покупки удерживается в самой монете. Продажи — полное количество, пока
    ордер не завершён (закрытая — исполненная часть, отклонённая — 0). Ордера с неясным исходом: покупка — 0,
    продажа — полностью."""
    rows = _rows("SELECT side, qty, state, filled, fee FROM orders WHERE venue=? AND category='spot' AND symbol=?",
                 (venue, symbol), path)
    held = Decimal(0)
    for r in rows:
        qty, filled, fee = (venues.dec(r["qty"]) or Decimal(0), venues.dec(r["filled"]), venues.dec(r["fee"]))
        if r["side"] == "buy":
            if r["state"] in ("open", "filled", "closed") and filled and filled > 0:
                held += filled - max(fee or Decimal(0), filled * SPOT_FEE_BUFFER)
        elif r["state"] == "rejected":
            continue
        elif r["state"] == "closed" and filled is not None:
            held -= filled
        else:
            held -= qty
    return max(held, Decimal(0))


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
        with con:
            cur = con.execute("INSERT OR IGNORE INTO pnl (ts, venue, symbol, amount, kind, ref) VALUES (?, ?, ?, ?, ?, ?)",
                              (time.time() if ts is None else ts, venue, symbol, str(amount), kind, str(ref)))
        return cur.rowcount == 1
    finally:
        con.close()


def pnl_today(now=None, path=None):
    """Сумма реализованного результата за день МСК (убыток — отрицательный)."""
    rows = _rows("SELECT amount FROM pnl WHERE ts>=?", (day_start(now),), path)
    return sum((Decimal(r["amount"]) for r in rows), Decimal(0))


def resume(path=None):
    """При старте: ордера, прерванные перезапуском посреди отправки (prepared/sending), — в unknown; их выяснит
    reconcile по клиентскому id. Возвращает эти строки (для сообщения владельцу)."""
    rows = _rows("SELECT client_id FROM orders WHERE state IN ('prepared', 'sending')", (), path)
    return [_update(r["client_id"], path, state="unknown", note="бот перезапустился во время отправки") for r in rows]


def resolve(client_id, state, note, path=None):
    """Разбор unknown владельцем на ПК (после проверки в кабинете биржи): unknown → closed/rejected/filled/open."""
    row = get(client_id, path)
    if row is None or row["state"] != "unknown" or state not in ("closed", "rejected", "filled", "open"):
        raise ValueError("разобрать можно только unknown")
    return _update(client_id, path, state=state, note=f"владелец: {note}")


def close_out(client_id, path=None):
    """Позицию, открытую исполненным ордером, закрыли: filled → closed (вызывает стратегия)."""
    return _update(client_id, path, state="closed")


# --- сверка ответа биржи с намерением ---

def _mismatch(row, view):
    """Поля ордера с биржи, не совпавшие с намерением (пусто — совпало). Количество и цена — как числа."""
    bad = []
    if view.get("client_id") != row["client_id"]:
        bad.append("id")
    if view.get("symbol") is not None and view["symbol"] != row["symbol"]:
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


def _apply_view(row, view):
    """Ордер, найденный на бирже (запрос или ответ создания с полями), → строка журнала и событие ("mismatch", "found",
    "filled", "closed", "rejected" или None). Несовпадение — unknown + тревога (одна на текст)."""
    bad = _mismatch(row, view)
    fields = {"venue_order_id": view.get("order_id") or row["venue_order_id"], "status": view.get("status") or "",
              "filled": "" if view.get("filled") is None else venues.fmt(view["filled"]),
              "avg_price": "" if not view.get("avg_price") else venues.fmt(view["avg_price"]),
              "fee": "" if view.get("fee") is None else venues.fmt(view["fee"])}
    if bad:
        note = f"{MISMATCH}: {', '.join(bad)}"
        event = None if row["state"] == "unknown" and row["note"] == note else "mismatch"
        return _update(row["client_id"], state="unknown", note=note, **fields), event
    state = view.get("state")
    if state is None:   # незнакомый статус — ничего не утверждаем
        note = f"незнакомый статус ордера: {view.get('status')!r}"
        event = None if row["state"] == "unknown" and row["note"] == note else "mismatch"
        return _update(row["client_id"], state="unknown", note=note, **fields), event
    if state not in TRANSITIONS[row["state"]]:
        note = f"статус {view.get('status')} после {row['state']}"
        return _update(row["client_id"], state="unknown", note=note, **fields), "mismatch"
    event = "found" if row["state"] == "unknown" else None
    if state != row["state"] and state in ("filled", "closed", "rejected"):
        event = event or state
    return _update(row["client_id"], state=state, note="" if row["state"] == "unknown" else row["note"],
                   **fields), event


def _accepted(row, data):
    """HTTP 200 + код 0 на создание. Bybit отвечает только {orderId, orderLinkId} (дальше — reconcile), BingX — ордером.
    Id не наш или поля не совпали — unknown + тревога."""
    if row["venue"] == venues.BYBIT:
        if not isinstance(data, dict) or str(data.get("orderLinkId") or "") != row["client_id"] \
                or not data.get("orderId"):
            return _update(row["client_id"], state="unknown", note=f"{MISMATCH}: id"), "mismatch"
        return _update(row["client_id"], state="open", venue_order_id=str(data["orderId"]), status="accepted"), None
    view = venues.order_view(venues.BINGX, data)
    if view is None:
        return _update(row["client_id"], state="unknown", note=f"{MISMATCH}: нет ордера в ответе"), "mismatch"
    if view["state"] is None and not view["status"] and not _mismatch(row, view):
        view = dict(view, state="open")   # BingX вернул ордер без статуса — принят, остальное скажет сверка
    return _apply_view(row, view)


def _not_found(row):
    """Сверка не нашла ордер. Создание получало только точные отказы и id биржи ни разу не было — ордера нет:
    rejected. Иначе unknown (в блоке открытий) и одно событие "notfound": владелец проверяет кабинет."""
    if row["create_kind"] == "error" and not row["venue_order_id"] and row["state"] == "unknown":
        return _update(row["client_id"], state="rejected"), "rejected"
    if NOT_FOUND in (row["note"] or ""):
        return row, None
    note = f"{row['note']}; {NOT_FOUND}" if row["note"] else NOT_FOUND
    return _update(row["client_id"], state="unknown", note=note), "notfound"


# --- отправка ---

def _result(state, row=None, reason="", event=None):
    return {"state": state, "row": row, "reason": reason, "event": event}


def _may_send(purpose):
    """Можно ли отправить (и повторить) ордер: открытие — только при TRADING=1 и живом режиме; закрытие и стоп
    позиции работают и при выключенной торговле (план: сопровождение, стопы и закрытие не останавливаются)."""
    if purpose == "open":
        return switch.can_open()
    return True, ""


def _venue_sym(row):
    """Символ биржи ордера — ровно из параметров создания (TONUSDT → GRAMUSDT решён в момент отправки)."""
    return json.loads(row["params"])["symbol"]


async def _create(s, row, creds):
    params = json.loads(row["params"])   # ровно записанные параметры, в том же порядке
    try:
        status, j = await venues.call(s, row["venue"], row["method"], row["path"], params, creds)
    except Exception as e:   # таймаут, обрыв — ордер мог и уйти
        return "ambiguous", None, accounts.api_error_text(e)
    kind, data, _, msg = venues.outcome(row["venue"], status, j, creds)
    return kind, data, msg


async def submit(s, order, creds, *, purpose="open", strategy="", mode="", notional=None, precheck=None):
    """Отправить ордер. Один вызов — не больше одного нового клиентского id.

    Под общим замком и без await до самой отправки: выключатель (для открытия), precheck() — синхронная проверка
    вызывающего (риск по свежим данным; строка — отказ), ключ, сборка и проверка параметров. Затем намерение в журнал
    (prepared → sending) и запрос:
    - ok — принят (Bybit: open; BingX: по ответу), ответ не совпал с намерением — unknown + "mismatch";
    - точный отказ / «id занят» — запрос по id: найден — принимаем; «не найден» и неясностей не было — rejected;
    - неясный исход — unknown, пауза, запрос по id: найден — принимаем; «не найден» — повтор с тем же id и теми же
      параметрами (≤ MAX_RESEND, открытие — только пока торговля включена); иначе unknown (разберёт reconcile).
    Возвращает {"state": refused|rejected|open|filled|closed|unknown, "row", "reason", "event"}."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose {purpose!r}")
    if purpose == "open" and order.reducing:
        raise ValueError("ордер на открытие не может быть уменьшающим")
    if purpose != "open" and not order.reducing:
        raise ValueError("закрытие/стоп — только уменьшающий ордер (reduceOnly; на споте — продажа)")
    async with lock():
        ok, why = _may_send(purpose)
        if not ok:
            return _result("refused", reason=why)
        if precheck is not None:
            why = precheck()
            if why:
                return _result("refused", reason=why)
        if not creds:
            return _result("refused", reason=f"{order.venue}: нет торгового ключа")
        if order.category == "spot" and order.side == "sell":
            held = spot_inventory(order.venue, order.symbol)
            if order.qty > held:
                return _result("refused", reason=f"продать на споте можно только купленное ботом: {venues.fmt(held)} "
                                                 f"{order.symbol[:-4]} по журналу, в ордере {venues.fmt(order.qty)}")
        try:
            venues.prepare(order.venue, *venues.create_call(order, new_client_id()), creds, timestamp=0)
        except ValueError as e:
            return _result("refused", reason=accounts._scrub(str(e), *creds))
        row = _insert_intent(order, purpose, strategy, mode, notional)
        cid = row["client_id"]
        row = _update(cid, state="sending", posts=1)
        logger.info("ордер %s: отправка %s %s %s %s", cid, order.venue, order.symbol, order.side, venues.fmt(order.qty))
        kind, data, msg = await _create(s, row, creds)
        posts, ambiguous = 1, False
        while True:
            row = get(cid)
            if kind == "ok":
                row, event = _accepted(row, data)
                logger.info("ордер %s: %s", cid, row["state"])
                return _result(row["state"], row, row["note"], event)
            ambiguous = ambiguous or kind in ("ambiguous", "duplicate")
            row = _update(cid, state="unknown", note=msg, create_kind="ambiguous" if ambiguous else "error")
            logger.warning("ордер %s: %s (%s)", cid, "исход неясен" if kind != "rejected" else "отказ", msg)
            if kind == "ambiguous":
                await asyncio.sleep(RETRY_DELAY * posts)   # запрос точно закончился; даём бирже время
            fkind, view, fmsg = await venues.find_order(s, row["venue"], row["category"], _venue_sym(row), cid,
                                                        creds)
            row = get(cid)
            if fkind == "found":
                row, event = _apply_view(row, view)
                return _result(row["state"], row, row["note"], event or "found")
            if fkind == "notfound" and kind == "rejected" and not ambiguous:
                row = _update(cid, state="rejected", note=msg)
                logger.info("ордер %s: отклонён", cid)
                return _result("rejected", row, msg)
            if fkind != "notfound" or kind != "ambiguous" or posts > MAX_RESEND:
                return _result("unknown", row, row["note"], "unknown")
            await asyncio.sleep(RETRY_DELAY * posts)
            ok, why = _may_send(purpose)   # проверка прямо перед повтором, без await до отправки
            if not ok:
                row = _update(cid, note=f"{row['note']}; {why} — повтор не отправлен")
                return _result("unknown", row, row["note"], "unknown")
            posts += 1
            _update(cid, posts=posts)
            kind, data, msg = await _create(s, row, creds)   # тот же id, те же параметры; новые время и подпись


async def cancel(s, client_id, creds):
    """Снять ордер из нашего журнала по клиентскому id (ордера владельца бот не трогает: чужой id — ValueError):
    (вид, текст) по venues.outcome. Итог в журнале выставит reconcile."""
    row = get(client_id)
    if row is None:
        raise ValueError(f"ордера {client_id!r} нет в журнале ядра")
    method, api_path, params = venues.cancel_call(row["venue"], row["category"], _venue_sym(row), client_id)
    try:
        status, j = await venues.call(s, row["venue"], method, api_path, params, creds)
    except Exception as e:
        return "ambiguous", accounts.api_error_text(e)
    kind, _, _, msg = venues.outcome(row["venue"], status, j, creds)
    return kind, msg


async def reconcile(s, creds_for, now=None):
    """Сверка с биржей под общим замком: брошенные prepared/sending → unknown; каждый open/unknown (не старше
    RECONCILE_DAYS) — запрос по клиентскому id. [(событие, строка)] — что нового для владельца. Ошибка запроса —
    строка не меняется. Повторов отправки тут нет."""
    events = []
    async with lock():
        for r in _rows("SELECT client_id FROM orders WHERE state IN ('prepared', 'sending')"):
            # под замком submit не идёт, значит эти строки — брошенные (отменённая задача, сбой)
            events.append(("unknown", _update(r["client_id"], state="unknown", note="отправка прервана")))
        for row in active(now):
            creds = creds_for(row["venue"])
            if not creds:
                continue
            kind, view, _ = await venues.find_order(s, row["venue"], row["category"], _venue_sym(row),
                                                    row["client_id"], creds)
            row = get(row["client_id"])
            if kind == "found":
                row, event = _apply_view(row, view)
            elif kind == "notfound" and row["state"] == "unknown":
                row, event = _not_found(row)
            elif kind == "notfound":   # открытый ордер пропал из всех списков биржи — не угадываем
                note = f"{NOT_FOUND} (был {row['state']})"
                row, event = _update(row["client_id"], state="unknown", note=note), "notfound"
            else:
                continue
            if event:
                events.append((event, row))
    return events
