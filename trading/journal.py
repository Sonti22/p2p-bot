"""Журнал ордеров торгового ядра — data/trading.db, по схеме журнала выплат (payouts.py).

- Намерение со свежим клиентским id (Bybit orderLinkId / BingX clientOrderId) пишется в журнал ДО любого запроса,
  вместе с ровно теми бизнес-параметрами, что уйдут на биржу (повтор — те же параметры и тот же id; меняются только
  время и подпись).
- Состояния: prepared → sending → open / filled → closed; rejected (биржа точно не создала ордер); unknown (исход неясен
  или ответ не совпал с намерением — тревога владельцу). Переходы — только по таблице TRANSITIONS; у строки версия
  (version): каждая запись её увеличивает, сверка и повтор отправки идут только по неизменной версии (compare-and-set).
- Неясный исход (таймаут, 5xx, 3xx, не JSON, незнакомый код) — пауза, запрос ордера по клиентскому id; «не найден» —
  повтор с тем же id (не больше MAX_RESEND раз; только если строку за это время никто не трогал: владелец не разобрал
  её, не запросил отмену). Повтор открытия — только пока торговля включена и только после НОВОЙ проверки символа
  (снимок, чужое, _guard): появилось чужое или что-то не прошло — повтора нет, строка unknown, событие владельцу. Точный
  отказ тоже сверяется запросом по id: «не найден» и неясностей не было — rejected.
- Любой unknown (и брошенный prepared/sending) блокирует новые открытия (`blocking`). Строка, которую сейчас отправляет
  бот, помечена в самой базе (inflight_until): владелец из другого процесса её не разбирает; принятый биржей повтор уже
  разобранной строки — событие владельцу, не молча.
- Открытие (`submit(purpose="open")`) само проверяет главное, не полагаясь на precheck вызывающего: выключатель и режим
  (не выше порогов gates: бэктест, бумага и реальная торговля — только из локальных файлов владельца в data/, реальная
  ещё и сверена с журналом; словари вызывающего не принимаются), проверенный ключ (keys), unknown, частоту ордеров,
  свежую сверку позиций и результата дня (watch), неподтверждённые закрытия биржей (любой стратегии), стопы позиций
  бота на месте, чужое по символу (ownership: позиция, все ордера и исполнения символа с биржи против журнала; у бота
  позиции нет — исполнения за последние WATERMARK_SLACK с: сделка владельца секунды назад откладывает открытие) по
  снимку не старше SNAPSHOT_MAX_AGE, BingX — кросс-позиции владельца на аккаунте, свои встречные позиции и стопы, шаги
  инструмента, итоговый размер (minlot — 50 USDT) по свежим ценам всех позиций бота, фактическое плечо и режим маржи с
  биржи, запас до ликвидации, кросс-маржу, дневной лимит (min(лимит режима, 2% капитала), реализованный +
  нереализованный убыток), худший убыток по стопам итоговой позиции ключа вместе с остальными стопами, и стопами
  ожидающих открытий (risk.guard_open). Направленная — без метки группы (ключ журнала — нога биржа:символ).
- Закрытие и стоп — только своей позиции бота: снимок символа (позиция, ордера, исполнения с отметки сверки) — вся
  позиция символа должна быть бота (ownership.owned_whole); на бирже пусто — позицию бота закрыла биржа или владелец:
  сальдо обнуляется (sync_flat, консервативный убыток дня) и отказ; иначе — отказ и событие владельцу. Не больше позиции
  ключа (стратегия, группа) без уже отправленных закрытий; без стратегии — только если ключ на символе один. Символ
  биржи — из журнала (TON → GRAM не зависит от свежести проверки символов). Работают и при выключенной торговле.
- Стоп позиции бота — отдельный условный reduceOnly-ордер бота (его клиентский id) на размер ключа по цене стопа ключа
  (стоп последнего открытия или set_stop — только к входу и только если ключ на символе один): ставится после
  исполнения открытия (и открытия, закончившегося частичным исполнением), переставляется после закрытий, смены стопа и
  сверкой (`_sync_stops`). Стопов всей позиции символа бот не ставит. Стоп больше позиции своего ключа сверка снимает
  до любой проверки; на символе чужое и не доказать, что стоп закроет только долю бота, — стопы бота снимаются
  (снятие своего ордера по своему id владельца не касается), владельцу — «позиция бота без стопа»; на бирже пусто, а у
  бота по журналу позиция — тоже. Стоп не подтверждён биржей, а цена уже за ним — программный стоп: закрытие рынком.
- Результат дня (таблица pnl, день МСК): исполнения ордеров бота (приращение — по своей цене; без цены биржи — худшее
  с поправкой, когда цена придёт; уменьшающий ордер ключ через ноль не переводит), комиссии биржи или оценка, фандинг
  (консервативно — как расход), консервативный убыток позиции, закрытой не ботом (flat_worst: изолированная маржа или
  весь номинал, не расстояние до стопа — ликвидация за стопом); такое закрытие уточняется по closed-pnl Bybit или
  владельцем (`settle_venue_close`), до этого открытия всех стратегий запрещены. Пыль спота (остаток меньше минимума
  биржи) списывается поправкой.
- Замки: открытия по одному символу биржи идут по очереди (symbol_lock — снимок символа и отправка); короткий общий
  state_lock — только синхронные части (проверки, намерение, переходы состояний), сеть и паузы — вне него. Закрытие,
  стоп и сверка не ждут чужих повторов отправки; сверка ордеров символа перед закрытием — параллельно и не дольше
  REFRESH_DEADLINE; стопы одного символа переставляются по очереди (stop_lock).
- События владельцу — outbox в той же транзакции, что и смена состояния (не теряются при сбое или отмене задачи); повтор
  той же записи (тот же текст в той же «эпохе» состояния строки — state_ver) не пишется, повторное событие после смены
  состояния (владелец разобрал строку) — пишется. Бот доставляет их `pending_events` → `mark_delivered`.
- База: одно соединение на файл (WAL), схема и индексы — один раз; сальдо, средний вход и стоп ключей бота
  материализованы (keys) в той же транзакции, что и изменение строки: проверки не читают всю историю.
- `resume()` при старте: prepared/sending → unknown. `reconcile()` — сверка незавершённых ордеров с биржей любого
  возраста (снимок под коротким замком, запросы вне его, запись — только если строка не изменилась) и сверка символов с
  позициями и ордерами бота (`watch`): закрытие биржей (только если ордера бота по символу сверены), чужое (событие
  владельцу, снятие висящих ордеров бота на открытие и стопов), стопы (до фандинга: сбой начислений защиту не
  пропускает), фандинг (своя отметка начислений), пыль спота, отметка свежести для открытий.
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
from trading import gates, keys, ownership, risk, switch, venues

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
SNAPSHOT_MAX_AGE = 5  # сек: снимок символа старше — открытие не уходит
FEED_MAX_AGE = 300    # сек: сверка позиций бота и результата дня (watch) старше — открытий нет
REFRESH_DEADLINE = 2  # сек: сверка ордеров символа перед закрытием/стопом — не дольше (закрытие — приоритет)
FILL_TRIES = 3        # запросов исполнения после открытия/закрытия (рынок/IOC исполняются сразу)
WATERMARK_SLACK = 5   # сек до первого намерения позиции: с этого момента все исполнения символа должны быть бота
WATERMARK_LAG = 60    # сек: отметку сверки исполнений держим позади (биржа показывает исполнения с задержкой)
FLAT_GRACE = 15       # сек после последнего исполнения бота по символу: пустая позиция на бирже ещё не значит «закрыла
                      # биржа» (ответ о позиции мог отстать от исполнения) — обнуления сальдо нет, действий тоже
FEE_ESTIMATE = Decimal("0.001")   # доля номинала: комиссия исполнения, если биржа её не сказала (тейкер ~0.055%)
DB_TIMEOUT = 2        # сек ожидания занятой базы (скрипт владельца) — цикл бота дольше не стоит
INFLIGHT_TTL = 120    # сек: отметка «в отправке» в самой строке (видна владельцу из другого процесса); обновляется
                      # перед каждым повтором, снимается по окончании отправки
UNKNOWN_LONG = 600    # сек: unknown дольше — нарушение для порога «кнопка → авто» (AUTO_COMMON)
MISMATCH = "ответ биржи не совпал с намерением"
NOT_FOUND = "биржа не находит ордер по клиентскому id"
CHANGED = "строку журнала изменили параллельно (владелец, отмена или сверка) — повтора нет, исход выяснит сверка"
_ORDER_COLUMNS = {
    "created_ts": "REAL", "updated_ts": "REAL", "venue": "TEXT", "category": "TEXT", "symbol": "TEXT", "side": "TEXT",
    "order_type": "TEXT", "qty": "TEXT", "price": "TEXT DEFAULT ''", "reduce_only": "INTEGER DEFAULT 0",
    "stop_loss": "TEXT DEFAULT ''", "purpose": "TEXT", "strategy": "TEXT DEFAULT ''", "mode": "TEXT DEFAULT ''",
    "notional": "TEXT DEFAULT ''", "method": "TEXT", "path": "TEXT", "params": "TEXT",
    "venue_order_id": "TEXT DEFAULT ''", "status": "TEXT DEFAULT ''", "filled": "TEXT DEFAULT ''",
    "avg_price": "TEXT DEFAULT ''", "fee": "TEXT DEFAULT ''",
    "posts": "INTEGER DEFAULT 0", "create_kind": "TEXT DEFAULT ''", "note": "TEXT DEFAULT ''", "grp": "TEXT DEFAULT ''",
    "version": "INTEGER DEFAULT 0", "cancel_requested": "INTEGER DEFAULT 0", "trigger": "TEXT DEFAULT ''",
    "venue_sym": "TEXT DEFAULT ''", "state_ver": "INTEGER DEFAULT 0", "exec_ts": "REAL", "acct": "TEXT DEFAULT ''",
    "inflight_until": "REAL DEFAULT 0"}

# --- замки и идущие отправки (на цикл событий; процесс бота — один) ---

_locks = {"loop": None, "state": None, "symbols": {}, "stops": {}}
_inflight = set()     # client_id, которые сейчас ведёт submit этого процесса: сверка и владелец их не трогают


def _loop_locks():
    loop = asyncio.get_running_loop()
    if _locks["loop"] is not loop:
        _locks.update(loop=loop, state=asyncio.Lock(), symbols={}, stops={})
    _locks.setdefault("stops", {})
    return _locks


def state_lock():
    """Короткий общий замок: только синхронные части (проверки, намерение, переходы). Сеть и паузы — вне него."""
    return _loop_locks()["state"]


def symbol_lock(venue, symbol):
    """Открытия (и смена плеча/маржи) по одному символу биржи — по очереди: снимок символа и отправка первого
    ордера видны второму. Закрытия и стопы его не ждут."""
    return _loop_locks()["symbols"].setdefault((venue, symbol), asyncio.Lock())


def _stop_lock(venue, symbol):
    """Перестановка стопов одного символа — по очереди (два снятия/постановки одновременно дали бы два стопа)."""
    return _loop_locks()["stops"].setdefault((venue, symbol), asyncio.Lock())


# --- база ---

_CONNS = {}          # путь -> соединение (одно на файл; последние _MAX_CONNS)
_MAX_CONNS = 4


def _schema(con):
    """Таблицы, колонки поздних версий и индексы — один раз на соединение; сальдо ключей — пересчёт из истории, если
    таблицы ключей ещё не было."""
    con.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT UNIQUE NOT "
                "NULL, state TEXT)")
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
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS watermarks (venue TEXT, category TEXT, symbol TEXT, ts REAL, fund REAL, "
                "PRIMARY KEY (venue, category, symbol))")
    if "fund" not in {r[1] for r in con.execute("PRAGMA table_info(watermarks)")}:
        con.execute("ALTER TABLE watermarks ADD COLUMN fund REAL")   # отметка начислений фандинга (своя)
    con.execute("CREATE TABLE IF NOT EXISTS provisional (ref TEXT PRIMARY KEY, venue TEXT, category TEXT, symbol TEXT, "
                "venue_sym TEXT, strategy TEXT, grp TEXT, qty TEXT, amount TEXT, ts REAL, since REAL, "
                "settled INTEGER DEFAULT 0, real TEXT DEFAULT '', note TEXT DEFAULT '')")
    fresh = not con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='keys'").fetchone()
    con.execute("CREATE TABLE IF NOT EXISTS keys (venue TEXT, category TEXT, symbol TEXT, strategy TEXT, grp TEXT, "
                "net TEXT, cost TEXT, stop TEXT DEFAULT '', venue_sym TEXT DEFAULT '', updated_ts REAL, "
                "mode TEXT DEFAULT '', PRIMARY KEY (venue, category, symbol, strategy, grp))")
    if "mode" not in {r[1] for r in con.execute("PRAGMA table_info(keys)")}:
        con.execute("ALTER TABLE keys ADD COLUMN mode TEXT DEFAULT ''")   # самый строгий режим открытий ключа
    for sql in ("CREATE INDEX IF NOT EXISTS orders_sym ON orders (venue, category, symbol)",
                "CREATE INDEX IF NOT EXISTS orders_state ON orders (state)",
                "CREATE INDEX IF NOT EXISTS orders_created ON orders (created_ts)",
                "CREATE INDEX IF NOT EXISTS outbox_delivered ON outbox (delivered)",
                "CREATE INDEX IF NOT EXISTS pnl_ts ON pnl (ts)"):
        con.execute(sql)
    if fresh:
        with _tx(con):
            _rebuild_keys(con)


def _connect(path=None):
    """Соединение с базой (одно на файл, WAL; схема — при первом открытии). Создаёт файл."""
    path = path or DB_PATH
    con = _CONNS.pop(path, None)
    if con is None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, timeout=DB_TIMEOUT, isolation_level=None)   # транзакции — явно (_tx)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        _schema(con)
        while len(_CONNS) >= _MAX_CONNS:
            _CONNS.pop(next(iter(_CONNS))).close()
    _CONNS[path] = con
    return con


def close_db():
    """Закрыть соединения с базой (остановка бота, тесты)."""
    while _CONNS:
        _CONNS.popitem()[1].close()


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
    path = path or DB_PATH
    if path not in _CONNS and not os.path.exists(path):   # чтение базу не создаёт
        return []
    return [dict(r) for r in _connect(path).execute(sql, args)]


def get(client_id, path=None):
    rows = _rows("SELECT * FROM orders WHERE client_id=?", (client_id,), path)
    return rows[0] if rows else None


def _get_meta(k, path=None):
    rows = _rows("SELECT v FROM meta WHERE k=?", (k,), path)
    return rows[0]["v"] if rows else None


def _set_meta(k, v, path=None):
    con = _connect(path)
    with _tx(con):
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))


def _emit_in(con, event, client_id="", state="", note="", dedup=None):
    """Событие владельцу в outbox — в текущей транзакции. Повтор того же (dedup) не пишется: → событие или None."""
    key = dedup or f"{client_id}|{event}|{note}"
    cur = con.execute("INSERT OR IGNORE INTO outbox (ts, client_id, event, state, note, dedup) VALUES (?, ?, ?, ?, ?, ?)",
                      (time.time(), client_id, event, state, note, key))
    return event if cur.rowcount == 1 else None


# --- сальдо ключей бота (материализовано) и результат дня ---

def _filled(r):
    """Исполненное количество строки: filled; для filled без числа — всё количество; отклонённая — 0."""
    if r["state"] == "rejected":
        return D0
    f = venues.dec(r["filled"])
    if r["state"] == "filled" and f is None:
        return venues.dec(r["qty"]) or D0
    return f if f is not None and f > 0 else D0


def _coin_fee(r):
    """Спот-покупка: монета, которую биржа удержала комиссией (не меньше SPOT_FEE_BUFFER исполненного — как в
    spot_inventory), её у бота нет. Иначе 0."""
    if not r or r.get("category") != "spot" or r.get("side") != "buy":
        return D0
    f = _filled(r)
    if f <= 0:
        return D0
    fee = venues.dec(r.get("fee"))
    return min(f, max(abs(fee) if fee is not None else D0, f * SPOT_FEE_BUFFER))


def _price_hint(r):
    for v in (r.get("avg_price"), r.get("price")):
        d = venues.dec(v)
        if d and d > 0:
            return d
    n, q = venues.dec(r.get("notional")), venues.dec(r.get("qty"))
    return n / q if n and q else None


def _fee_usdt(r):
    """Комиссия исполненного строки в USDT: биржевая (спот-покупка Bybit — в монете × цена) или оценка FEE_ESTIMATE."""
    if not r or not r.get("venue"):
        return D0
    f = _filled(r)
    if f <= 0:
        return D0
    px = _price_hint(r) or D0
    fee = venues.dec(r.get("fee"))
    if fee is None:
        return f * px * FEE_ESTIMATE
    if r.get("category") == "spot" and r.get("side") == "buy":
        return abs(fee) * px
    return abs(fee)


def _key_id(r):
    return (r["venue"], r["category"], r["symbol"], r.get("strategy") or "", r.get("grp") or "")


def _load_key(con, kid):
    k = con.execute("SELECT * FROM keys WHERE venue=? AND category=? AND symbol=? AND strategy=? AND grp=?",
                    kid).fetchone()
    if k is None:
        return {"net": D0, "cost": D0, "stop": None, "venue_sym": "", "mode": ""}
    return {"net": Decimal(k["net"]), "cost": Decimal(k["cost"]), "stop": venues.dec(k["stop"]),
            "venue_sym": k["venue_sym"] or "", "mode": k["mode"] or ""}


def _save_key(con, kid, k):
    """Ключ с нулевым сальдо не хранится (таблица — только открытые позиции бота)."""
    if k["net"] == 0:
        con.execute("DELETE FROM keys WHERE venue=? AND category=? AND symbol=? AND strategy=? AND grp=?", kid)
        return
    con.execute("INSERT OR REPLACE INTO keys (venue, category, symbol, strategy, grp, net, cost, stop, venue_sym, "
                "updated_ts, mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*kid, venues.fmt(k["net"]), venues.fmt(k["cost"]), "" if k["stop"] is None else venues.fmt(k["stop"]),
                 k["venue_sym"], time.time(), k.get("mode") or ""))


def _stricter_mode(a, b):
    """Более строгий из двух режимов открытия ("" — нет)."""
    known = [m for m in (a, b) if m in switch.RANK]
    return min(known, key=switch.RANK.get) if known else (a or b or "")


def _symbol_net(con, venue, category, symbol):
    return sum((Decimal(r["net"]) for r in con.execute("SELECT net FROM keys WHERE venue=? AND category=? AND "
                                                       "symbol=?", (venue, category, symbol))), D0)


def _apply_trade(k, d, px, trade):
    """Изменение сальдо ключа k ({net, cost}) на d по цене px — средним входом. trade — исполнение (реализованный
    результат при уменьшении позиции); иначе поправка строки (сальдо двигается по среднему входу, результата нет).
    → реализованный результат (Decimal) или None — цены выхода нет."""
    net, cost = k["net"], k["cost"]
    avg = cost / net if net else None
    new = net + d
    if not trade:   # поправка: результата нет; вход — средний ключа (позиции не было или знак сменился — цена строки)
        if new == 0:
            k["net"], k["cost"] = D0, D0
        elif net and (new > 0) == (net > 0):
            k["net"], k["cost"] = new, new * avg
        else:
            k["net"], k["cost"] = new, new * (px if px is not None else (avg or D0))
        return D0
    if net == 0 or (net > 0) == (d > 0):   # позиция ключа растёт
        k["net"], k["cost"] = new, cost + d * (px if px is not None else (avg or D0))
        return D0
    closing = min(abs(d), abs(net))
    sgn = 1 if net > 0 else -1
    realized = None if px is None else closing * (px - avg) * sgn
    rest = abs(d) - closing
    if rest > 0:   # переворот позиции ключа
        k["net"] = (1 if d > 0 else -1) * rest
        k["cost"] = k["net"] * (px if px is not None else avg)
    else:
        k["net"] = net - sgn * closing
        k["cost"] = k["net"] * avg
    return realized


def _add_pnl_in(con, venue, symbol, amount, kind, ref, ts):
    cur = con.execute("INSERT OR IGNORE INTO pnl (ts, venue, symbol, amount, kind, ref) VALUES (?, ?, ?, ?, ?, ?)",
                      (ts, venue, symbol, str(amount), kind, str(ref)))
    return cur.rowcount == 1


def _acct(r):
    """Учёт цены исполнений строки (колонка acct): pq/pv — количество и стоимость исполненного, уже оценённого по
    средней цене биржи; u — уменьшения позиции, записанные без цены биржи: [количество, вход ключа, знак позиции,
    записанный результат] — их результат уточняется поправкой, когда средняя цена придёт."""
    try:
        a = json.loads((r or {}).get("acct") or "{}")
        return {"pq": Decimal(str(a.get("pq", "0"))), "pv": Decimal(str(a.get("pv", "0"))), "u": list(a.get("u") or [])}
    except (TypeError, ValueError, ArithmeticError, AttributeError):
        return {"pq": D0, "pv": D0, "u": []}


def _unpriced_px(acct, filled, avg):
    """Цена исполненного, которое ещё не оценено (всё, кроме pq): (исполнено × средняя − pv) / (исполнено − pq) —
    приращение по своей цене, а не по накопленной средней."""
    left = filled - acct["pq"]
    if left <= 0:
        return avg
    px = (filled * avg - acct["pv"]) / left
    return px if px > 0 else avg


def _reprice(acct, px):
    """Поправка результата уменьшений, записанных без цены (худший или по цене лимитки), по цене px."""
    total = D0
    for q, e, s, booked in acct["u"]:
        total += Decimal(q) * (px - Decimal(e)) * int(s) - Decimal(booked)
    return total


def _reduce_keys(con, row, own, keys_of, q, px, acct):
    """Исполнение уменьшающего ордера бота (закрытие, стоп) на q: сначала позиция своего ключа — не дальше нуля (ключ
    не переворачивается), излишек — другим ключам бота на символе с той же стороной позиции (стоп одного ключа закрыл
    долю другого), остаток — не бота (событие). → (реализованный результат, [(ключ, количество)] — чужие ключи,
    излишек не бота)."""
    sd = q if row["side"] == "buy" else -q   # знак исполнения: buy уменьшает шорт
    others = [(r["strategy"] or "", r["grp"] or "") for r in con.execute(
        "SELECT strategy, grp FROM keys WHERE venue=? AND category=? AND symbol=? ORDER BY strategy, grp",
        own[:3])]
    order = [own] + [(*own[:3], st, g) for st, g in others if (*own[:3], st, g) != own]
    realized, moved, left = D0, [], q
    lim = venues.dec(row.get("price")) if row.get("order_type") == "limit" else None
    for kid in order:
        if left <= 0:
            break
        k = keys_of(kid)
        net = k["net"]
        if not net or (net > 0) == (sd > 0):
            continue
        take = min(left, abs(net))
        e, s = k["cost"] / net, (1 if net > 0 else -1)
        if px is not None:
            r = take * (px - e) * s
        else:   # цены нет: по цене лимитки (не хуже для бота) или худшее — по стопу ключа, без стопа весь номинал
            r = take * (lim - e) * s if lim and lim > 0 else \
                -(risk.key_worst("buy" if s > 0 else "sell", take, e, k["stop"]) if k["stop"] is not None else take * e)
            acct["u"].append([venues.fmt(take), venues.fmt(e), s, str(r)])
        realized += r
        k["net"] = net - s * take
        k["cost"] = k["net"] * e
        k["dirty"] = True
        if kid != own:
            moved.append((kid, take))
        left -= take
    return realized, moved, left


def _book_fill(con, cur, row, book_pnl=True):
    """Изменение исполненного строки → сальдо, средний вход и стоп ключей, результат дня (сделка, комиссия), отметка
    сверки исполнений символа. В транзакции вызывающего. Приращение исполнения — по своей цене ((исполнено × средняя −
    уже оценённое) / неоценённое), не по накопленной средней. Уменьшающий ордер (закрытие, стоп) ключ через ноль не
    переводит: излишек — другим ключам бота той же стороны символа, остаток — событие владельцу. Уменьшение без цены
    биржи — худший результат до прихода цены, потом поправка (acct)."""
    if not row.get("venue") or row.get("side") not in ("buy", "sell"):
        return
    sgn = 1 if row["side"] == "buy" else -1
    f_old, f_new = (_filled(cur) if cur else D0), _filled(row)
    df = f_new - f_old
    dcoin = _coin_fee(row) - _coin_fee(cur)
    fee_d = _fee_usdt(row) - _fee_usdt(cur)
    acct = _acct(cur)
    acct0 = json.dumps(acct, default=str, sort_keys=True)
    avg_new = venues.dec(row.get("avg_price"))
    avg_new = avg_new if avg_new is not None and avg_new > 0 else None
    reprice = bool(acct["u"]) and avg_new is not None and df <= 0 and f_new > 0
    if df == 0 and dcoin == 0 and fee_d == 0 and not reprice:
        return
    kid = _key_id(row)
    venue, category, symbol = kid[:3]
    before = _symbol_net(con, venue, category, symbol)
    loaded = {}

    def keys_of(key_id):
        if key_id not in loaded:
            loaded[key_id] = _load_key(con, key_id)
        return loaded[key_id]
    k = keys_of(kid)
    realized, moved, extra = D0, [], D0
    if df > 0:
        px = None
        if avg_new is not None:
            px = _unpriced_px(acct, f_new, avg_new)
            realized += _reprice(acct, px)   # раньше записанные без цены — по той же цене
            acct["u"] = []
        if row.get("purpose") in ("close", "stop"):
            r, moved, extra = _reduce_keys(con, row, kid, keys_of, df, px, acct)
            realized += r
        else:
            net0, stop0 = k["net"], k["stop"]
            avg0 = k["cost"] / net0 if net0 else None
            r = _apply_trade(k, sgn * df, px if px is not None else _price_hint(row), True)
            if r is None:   # цены выхода нет — худшее: по стопу ключа с проскальзыванием, без стопа — весь номинал
                closing = min(df, abs(net0))
                r = -(risk.key_worst("buy" if net0 > 0 else "sell", closing, avg0, stop0) if stop0 is not None
                      else closing * avg0)
            realized += r
            k["dirty"] = True
            if row.get("purpose") == "open" and k["net"]:
                if row.get("stop_loss"):
                    k["stop"] = venues.dec(row["stop_loss"])   # стоп ключа — стоп последнего открытия (tpslMode=Full)
                k["mode"] = _stricter_mode(k.get("mode") or row.get("mode") or "", row.get("mode") or "")
        if avg_new is not None:
            acct.update(pq=f_new, pv=f_new * avg_new)
    elif reprice:
        realized += _reprice(acct, _unpriced_px(acct, f_new, avg_new))
        acct.update(pq=f_new, pv=f_new * avg_new, u=[])
    if df < 0:   # исполненное строки уменьшилось (разбор, отказ) — поправка своего ключа по среднему входу
        _apply_trade(k, sgn * df, None, False)
        k["dirty"] = True
    if dcoin:    # монета комиссии спот-покупки — не у бота
        _apply_trade(k, -dcoin, None, False)
        k["dirty"] = True
    for key_id, kk in loaded.items():
        if kk.pop("dirty", False):
            if not kk["venue_sym"]:
                kk["venue_sym"] = _venue_sym(row)
            _save_key(con, key_id, kk)
    if book_pnl:
        ts = row.get("exec_ts") or time.time()
        tag = f"{row['client_id']}:v{row['version']}"
        if realized:
            _add_pnl_in(con, venue, symbol, realized, "trade", f"{tag}:trade", ts)
        if fee_d:
            _add_pnl_in(con, venue, symbol, -fee_d, "fee", f"{tag}:fee", ts)
        if json.dumps(acct, default=str, sort_keys=True) != acct0:
            con.execute("UPDATE orders SET acct=? WHERE client_id=?",
                        (json.dumps({"pq": venues.fmt(acct["pq"]), "pv": venues.fmt(acct["pv"]), "u": acct["u"]},
                                    separators=(",", ":")), row["client_id"]))
        if moved or extra:
            what = ", ".join(f"{st or '—'}/{g or '—'} {venues.fmt(q)}" for (_, _, _, st, g), q in moved)
            note = (f"{venue} {symbol}: {row['purpose']} {row['strategy'] or '—'}/{row['grp'] or '—'} исполнен больше "
                    f"позиции своего ключа" + (f" — излишек закрыл позицию бота {what}" if what else "") +
                    (f"; ещё {venues.fmt(extra)} — не позиция бота по журналу (позиция владельца?), проверьте кабинет"
                     if extra else ""))
            _emit_in(con, "overfill" if extra else "cross_key", row["client_id"], row["state"], note,
                     dedup=f"overfill|{row['client_id']}|{venues.fmt(f_new)}")
    after = _symbol_net(con, venue, category, symbol)
    if category != "spot" and after != before:
        if before == 0 and after != 0:
            ts = (row.get("created_ts") or time.time()) - WATERMARK_SLACK
            con.execute("INSERT OR REPLACE INTO watermarks (venue, category, symbol, ts, fund) VALUES (?, ?, ?, ?, ?)",
                        (venue, category, symbol, ts, ts))
        elif after == 0:
            con.execute("DELETE FROM watermarks WHERE venue=? AND category=? AND symbol=?", (venue, category, symbol))


def _rebuild_keys(con):
    """Сальдо ключей из истории (база ранней версии ядра): все строки по порядку, затем поправки. Результат дня не
    пересчитывается."""
    con.execute("DELETE FROM keys")
    for r in con.execute("SELECT * FROM orders ORDER BY id").fetchall():
        _book_fill(con, None, dict(r), book_pnl=False)
    for a in con.execute("SELECT * FROM adjust ORDER BY id").fetchall():
        q = venues.dec(a["qty"]) or D0
        kid = (a["venue"], a["category"], a["symbol"], a["strategy"] or "", a["grp"] or "")
        k = _load_key(con, kid)
        _apply_trade(k, q, None, False)
        if k["net"] == 0:
            k["stop"] = None
        _save_key(con, kid, k)


def _transition(client_id, path=None, *, expect=None, event=None, **fields):
    """Изменить строку и (если event) записать событие в outbox — одной транзакцией; там же — сальдо ключа и результат
    дня (_book_fill). Смена state — только по TRANSITIONS (иначе ValueError, ничего не меняется). expect — версия, с
    которой работал вызывающий: не совпала — (None, None), ничего не меняется. → (строка, записанное событие или None —
    такое уже было в этой «эпохе» состояния)."""
    con = _connect(path)
    with _tx(con):
        cur = con.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        if cur is None:
            raise ValueError(f"нет ордера {client_id}")
        cur = dict(cur)
        if expect is not None and cur["version"] != expect:
            return None, None
        if "state" in fields and fields["state"] not in TRANSITIONS.get(cur["state"], set()):
            raise ValueError(f"переход {cur['state']} → {fields['state']} запрещён")
        if fields.get("state", cur["state"]) != cur["state"]:
            fields["state_ver"] = cur["version"] + 1   # новая «эпоха» состояния: событие того же текста — снова пишется
        fields["updated_ts"] = time.time()
        con.execute(f"UPDATE orders SET {', '.join(f'{k}=?' for k in fields)}, version=version+1 "
                    f"WHERE client_id=?", (*fields.values(), client_id))
        row = dict(con.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone())
        _book_fill(con, cur, row)
        emitted = None
        if event:
            emitted = _emit_in(con, event, client_id, row["state"], row["note"],
                               dedup=f"{client_id}|{event}|{row['note']}|s{row['state_ver']}")
    return row, emitted


def _update(client_id, path=None, **fields):
    """Обновить строку без события и без сверки версии; смена state — только по TRANSITIONS (иначе ValueError)."""
    return _transition(client_id, path, **fields)[0]


def emit(event, note, client_id="", dedup=None, path=None):
    """Событие владельцу, не привязанное к смене состояния ордера (позицию закрыла биржа, чужое, сбой сверки)."""
    con = _connect(path)
    with _tx(con):
        return _emit_in(con, event, client_id, "", note, dedup)


def pending_events(limit=50, path=None):
    """Недоставленные события владельцу — по порядку."""
    return _rows("SELECT * FROM outbox WHERE delivered=0 ORDER BY id LIMIT ?", (limit,), path)


def mark_delivered(ids, path=None):
    """Отметить события доставленными (после отправки владельцу)."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    con = _connect(path)
    with _tx(con):
        cur = con.execute(f"UPDATE outbox SET delivered=1, delivered_ts=? WHERE id IN ({','.join('?' * len(ids))})",
                          (time.time(), *ids))
    return cur.rowcount


def new_client_id(now=None):
    """Свежий клиентский id: "t" + ГГММДДччммсс (МСК) + 8 hex — 21 символ [a-z0-9], годится обеим биржам."""
    d = datetime.fromtimestamp(time.time() if now is None else now, MSK)
    return f"t{d:%y%m%d%H%M%S}{secrets.token_hex(4)}"


def _insert_intent(order, purpose, strategy, mode, notional, path=None, group="", venue_sym=None):
    """Намерение (state=prepared) со свежим id и ровно теми параметрами, что уйдут на биржу — до любого запроса.
    venue_sym — символ биржи позиции бота из журнала (закрытие, стоп)."""
    con = _connect(path)
    for _ in range(5):
        cid = new_client_id()
        method, api_path, params = venues.create_call(order, cid, venue_sym=venue_sym)
        try:
            with _tx(con):
                con.execute(
                    "INSERT INTO orders (client_id, created_ts, updated_ts, venue, category, symbol, side, "
                    "order_type, qty, price, reduce_only, stop_loss, purpose, strategy, mode, notional, "
                    "method, path, params, state, grp, trigger, venue_sym) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)",
                    (cid, time.time(), time.time(), order.venue, order.category, order.symbol, order.side,
                     order.order_type, venues.fmt(order.qty),
                     "" if order.price is None else venues.fmt(order.price), int(order.reduce_only),
                     "" if order.stop_loss is None else venues.fmt(order.stop_loss), purpose,
                     strategy, mode, "" if notional is None else str(notional), method, api_path,
                     json.dumps(params, separators=(",", ":")), str(group or ""),
                     "" if order.trigger is None else venues.fmt(order.trigger), params["symbol"]))
            break
        except sqlite3.IntegrityError:   # совпал id — берём другой
            continue
    else:
        raise RuntimeError("не удалось выбрать свободный клиентский id")
    return get(cid, path)


# --- запросы к журналу ---

def _marks(states):
    return ",".join("?" * len(states))


def active(now=None, path=None):
    """Незавершённые ордера (open/unknown) ЛЮБОГО возраста — их сверяет reconcile (старые не выпадают молча: не
    найден на бирже — unknown и блок открытий до разбора владельцем)."""
    return _rows(f"SELECT * FROM orders WHERE state IN ({_marks(ACTIVE)}) ORDER BY id", ACTIVE, path)


def blocking(path=None, exclude=None):
    """Ордера, из-за которых новые открытия запрещены: unknown любого возраста и брошенные prepared/sending (не те,
    что сейчас отправляет этот процесс). exclude — строка, которую сейчас решают повторить (повтор открытия)."""
    rows = _rows(f"SELECT * FROM orders WHERE state IN ({_marks(BLOCKING)}) ORDER BY id", BLOCKING, path)
    return [r for r in rows if r["client_id"] != exclude
            and (r["state"] == "unknown" or r["client_id"] not in _inflight)]


def _key_book():
    return {"net": D0, "pending_open": D0, "pending_side": None, "pending_px": None, "pending_stop_price": None,
            "pending_reduce": {"buy": D0, "sell": D0}, "pending_stop": D0, "stops": [], "stop": None, "px": None,
            "venue_sym": "", "mode": ""}


def _worse_stop(side, a, b):
    """Худший из стопов позиции стороны side (long — ниже, short — выше); None — нет."""
    vals = [x for x in (a, b) if x is not None]
    if not vals:
        return None
    return min(vals) if side in ("long", "buy") else max(vals)


class _BotIds:
    """Клиентские id бота на бирже: «этот id — наш» — запрос по индексу, без чтения всех id истории."""
    __slots__ = ("venue", "path")

    def __init__(self, venue, path=None):
        self.venue, self.path = venue, path

    def __contains__(self, cid):
        return bool(cid) and bool(_rows("SELECT 1 FROM orders WHERE client_id=? AND venue=? LIMIT 1",
                                        (str(cid), self.venue), self.path))


def bot_book(venue, category, symbol, path=None, exclude=None):
    """Позиция бота по символу биржи — по журналу: {net (знаковое сальдо: исполненные ордера ± поправки), keys
    {(стратегия, группа): {net, pending_open, pending_side, pending_stop_price (худший стоп ожидающих открытий),
    pending_reduce {buy, sell} (закрытия), pending_stop (остаток стопов), stops (id стопов), stop (цена стопа ключа или
    None), px (средний вход), venue_sym, mode (самый строгий режим открытий ключа)}}, client_ids
    (id бота на этой бирже: `cid in` — запрос по индексу), active (open/unknown), uncertain (prepared/sending/
    unknown), stops (id висящих стопов бота), pending_reduce {buy, sell}}. Сальдо — из материализованной таблицы
    ключей, ожидающее — из незавершённых строк: длина истории не важна. exclude — строка, которую не считать (повтор
    открытия)."""
    book = {"net": D0, "keys": {}, "client_ids": _BotIds(venue, path), "active": [], "uncertain": [], "stops": [],
            "pending_reduce": {"buy": D0, "sell": D0}}
    for k in _rows("SELECT * FROM keys WHERE venue=? AND category=? AND symbol=?", (venue, category, symbol), path):
        kb = book["keys"].setdefault((k["strategy"] or "", k["grp"] or ""), _key_book())
        net, cost = Decimal(k["net"]), Decimal(k["cost"])
        kb.update(net=net, px=cost / net if net else None, stop=venues.dec(k["stop"]), venue_sym=k["venue_sym"] or "",
                  mode=k["mode"] or "")
        book["net"] += net
    live = _rows(f"SELECT * FROM orders WHERE state IN ({_marks(UNSETTLED)}) AND venue=? AND category=? AND symbol=? "
                 f"ORDER BY id", (*UNSETTLED, venue, category, symbol), path)
    for r in live:
        if r["client_id"] == exclude:
            continue
        kb = book["keys"].setdefault((r["strategy"] or "", r["grp"] or ""), _key_book())
        rem = max((venues.dec(r["qty"]) or D0) - _filled(r), D0)
        if r["state"] in ACTIVE:
            book["active"].append(r["client_id"])
        if r["state"] in BLOCKING:
            book["uncertain"].append(r["client_id"])
        if r["purpose"] == "open":
            kb["pending_open"] += rem
            kb["pending_side"] = r["side"]
            kb["pending_px"] = _price_hint(r) or kb["pending_px"]
            if rem > 0 and r["stop_loss"]:   # стоп ожидающего открытия встанет на весь ключ — в бюджет худшего убытка
                kb["pending_stop_price"] = _worse_stop(r["side"], kb["pending_stop_price"],
                                                       venues.dec(r["stop_loss"]))
        elif r["purpose"] == "stop":
            kb["pending_stop"] += rem
            kb["stops"].append(r["client_id"])
            if r["state"] in ACTIVE:
                book["stops"].append(r["client_id"])
        else:
            kb["pending_reduce"][r["side"]] += rem
            book["pending_reduce"][r["side"]] += rem
    return book


def _exposure_symbols(path=None):
    """(биржа, категория, символ) с позициями бота или ожидающими открытиями."""
    seen = {(r["venue"], r["category"], r["symbol"]) for r in _rows("SELECT venue, category, symbol FROM keys", (),
                                                                     path)}
    seen |= {(r["venue"], r["category"], r["symbol"]) for r in
             _rows(f"SELECT venue, category, symbol FROM orders WHERE state IN ({_marks(UNSETTLED)}) "
                   f"AND purpose='open'", UNSETTLED, path)}
    return sorted(seen)


def exposure(prices=None, path=None, exclude=None):
    """Открытое ботом и ожидающие ордера на открытие — строки для risk (ctx.positions / guard_open): по (биржа,
    категория, символ, стратегия, группа) с ненулевым размером: {venue, category, symbol, strategy, group, side long|
    short, qty (|сальдо| + ожидающие открытия), net, pending, px (цена оценки), entry (средний вход), notional,
    unrealized (сальдо × (цена − вход)), stop (есть стоп: ключа или ожидающего открытия), stop_price (худший из них —
    стоп открытия встанет на весь ключ)}. prices — {(биржа, категория, символ) или
    (биржа, символ): цена} — свежая цена вместо среднего входа из журнала."""
    out = []
    prices = prices or {}
    for venue, category, symbol in _exposure_symbols(path):
        book = bot_book(venue, category, symbol, path, exclude=exclude)
        for (strategy, group), k in book["keys"].items():
            qty = abs(k["net"]) + k["pending_open"]
            if qty <= 0:
                continue
            side = ("long" if k["net"] > 0 else "short") if k["net"] else \
                ("long" if k["pending_side"] == "buy" else "short")
            px = prices.get((venue, category, symbol)) or prices.get((venue, symbol)) or k["px"] or k["pending_px"]
            if not px:
                raise ValueError(f"{venue} {symbol}: нет цены для оценки позиции бота")
            entry = k["px"] or px
            # стоп итоговой позиции: стоп ключа и стопы ожидающих открытий (встанут на весь ключ) — худший
            stop = _worse_stop(side, k["stop"], k["pending_stop_price"] if k["pending_open"] > 0 else None)
            out.append({"venue": venue, "category": category, "symbol": symbol, "strategy": strategy, "group": group,
                        "side": side, "qty": qty, "net": k["net"], "pending": k["pending_open"], "px": px,
                        "entry": entry, "notional": qty * px, "unrealized": k["net"] * (px - entry),
                        "stop": stop is not None, "stop_price": stop})
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
    with _tx(con):
        return _add_pnl_in(con, venue, symbol, amount, kind, ref, time.time() if ts is None else ts)


def pnl_today(now=None, path=None):
    """Сумма реализованного результата за день МСК (убыток — отрицательный)."""
    rows = _rows("SELECT amount FROM pnl WHERE ts>=?", (day_start(now),), path)
    return sum((Decimal(r["amount"]) for r in rows), D0)


def unsettled(strategy=None, venue=None, path=None):
    """Закрытия биржей с ещё не подтверждённым результатом (provisional): пока есть хоть одно — открытия всех стратегий
    запрещены (настоящий убыток дня неизвестен, лимит дня общий)."""
    rows = _rows("SELECT * FROM provisional WHERE settled=0 ORDER BY ts", (), path)
    return [r for r in rows if (strategy is None or r["strategy"] == strategy)
            and (venue is None or r["venue"] == venue)]


def flat_worst(net, avg, stop, margin_mode=None, leverage=None):
    """Худший убыток позиции ключа, закрытой не ботом (ликвидация — по определению за стопом, ADL, владелец): без стопа
    — весь номинал; со стопом — изолированная маржа при известном плече: max(худший по стопу, номинал / плечо +
    комиссии); кросс или режим/плечо неизвестны — весь номинал (не меньше худшего по стопу)."""
    qty = abs(net)
    notional = qty * avg
    side = "buy" if net > 0 else "sell"
    by_stop = risk.key_worst(side, qty, avg, stop) if stop is not None else None
    lev = venues.dec(leverage) if leverage is not None else None
    if by_stop is not None and margin_mode == "isolated" and lev is not None and lev >= 1:
        return max(by_stop, notional / lev + notional * risk.WORST_FEES)
    return max(notional, by_stop or D0)


def sync_flat(venue, category, symbol, note="позицию бота закрыла биржа (ликвидация, ADL) или владелец", path=None,
              margin_mode=None, leverage=None):
    """На бирже по символу пусто, у бота по журналу — позиция (ownership.flat_external): одной транзакцией — поправки
    обнуляют сальдо каждого ключа бота; консервативный убыток дня (flat_worst: margin_mode/leverage — режим маржи и
    плечо символа с биржи, не узнали — весь номинал) и запись «не подтверждено» (провизорно: открытия всех стратегий
    запрещены до уточнения по бирже или владельцем); событие владельцу. → записанное событие или None."""
    con = _connect(path)
    now = time.time()
    with _tx(con):
        wm = con.execute("SELECT ts FROM watermarks WHERE venue=? AND category=? AND symbol=?",
                         (venue, category, symbol)).fetchone()
        since = wm["ts"] if wm else now - 86400
        parts, total = [], D0
        for k in con.execute("SELECT * FROM keys WHERE venue=? AND category=? AND symbol=?",
                             (venue, category, symbol)).fetchall():
            net = Decimal(k["net"])
            if not net:
                continue
            avg, stop = Decimal(k["cost"]) / net, venues.dec(k["stop"])
            loss = flat_worst(net, avg, stop, margin_mode, leverage)
            ref = f"flat:{venue}:{category}:{symbol}:{k['strategy']}:{k['grp']}:{now!r}"
            con.execute("INSERT INTO adjust (ts, venue, category, symbol, strategy, grp, qty, note) VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?)", (now, venue, category, symbol, k["strategy"], k["grp"],
                                                     venues.fmt(-net), note))
            _add_pnl_in(con, venue, symbol, -loss, "venue_close", ref, now)
            con.execute("INSERT INTO provisional (ref, venue, category, symbol, venue_sym, strategy, grp, qty, amount, "
                        "ts, since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (ref, venue, category, symbol, k["venue_sym"], k["strategy"], k["grp"], venues.fmt(abs(net)),
                         str(-loss), now, since))
            con.execute("DELETE FROM keys WHERE venue=? AND category=? AND symbol=? AND strategy=? AND grp=?",
                        (venue, category, symbol, k["strategy"], k["grp"]))
            parts.append(f"{k['strategy'] or '—'}/{k['grp'] or '—'} {venues.fmt(net)}")
            total += loss
        con.execute("DELETE FROM watermarks WHERE venue=? AND category=? AND symbol=?", (venue, category, symbol))
        if not parts:
            return None
        return _emit_in(con, "closed_by_venue", "", "",
                        f"{venue} {symbol}: {note} (было {', '.join(parts)}); в результат дня записан худший убыток "
                        f"{total:.2f} USDT до уточнения — все открытия запрещены до подтверждения",
                        dedup=f"flat|{venue}|{category}|{symbol}|{now!r}")


def settle_venue_close(ref, amount, note, path=None):
    """Подтвердить результат закрытия биржей (владелец по кабинету или сверка по closed-pnl): поправка результата дня
    (реальный − консервативный, днём закрытия), запись подтверждена — открытия стратегии снова возможны."""
    amount = Decimal(str(amount))
    if not amount.is_finite():
        raise ValueError("результат не число")
    con = _connect(path)
    with _tx(con):
        p = con.execute("SELECT * FROM provisional WHERE ref=? AND settled=0", (ref,)).fetchone()
        if p is None:
            raise ValueError("нет неподтверждённого закрытия с таким ref")
        _add_pnl_in(con, p["venue"], p["symbol"], amount - Decimal(p["amount"]), "venue_close_settle", f"{ref}:settle",
                    p["ts"])
        con.execute("UPDATE provisional SET settled=1, real=?, note=? WHERE ref=?", (str(amount), str(note), ref))
        _emit_in(con, "settled", "", "", f"{p['venue']} {p['symbol']}: результат закрытия биржей "
                                         f"{venues.fmt(amount)} USDT ({note})", dedup=f"settled|{ref}")


def _watermark(venue, category, symbol, path=None):
    """Отметка сверки исполнений символа (сек): с неё все исполнения должны быть бота. Нет — от первого намерения
    позиции бота (база ранней версии) и записать."""
    rows = _rows("SELECT ts FROM watermarks WHERE venue=? AND category=? AND symbol=?", (venue, category, symbol), path)
    if rows:
        return rows[0]["ts"]
    first = _rows("SELECT MIN(created_ts) AS t FROM orders WHERE venue=? AND category=? AND symbol=? AND "
                  "state != 'rejected'", (venue, category, symbol), path)
    ts = (first[0]["t"] if first and first[0]["t"] else time.time()) - WATERMARK_SLACK
    con = _connect(path)
    with _tx(con):
        con.execute("INSERT OR IGNORE INTO watermarks (venue, category, symbol, ts) VALUES (?, ?, ?, ?)",
                    (venue, category, symbol, ts))
    return ts


def _exec_since(venue, category, symbol, path=None):
    """Начало окна исполнений (мс) для доказательства «позиция бота цела» или None — у бота по символу позиции нет."""
    if category == "spot" or bot_book(venue, category, symbol, path)["net"] == 0:
        return None
    return int(_watermark(venue, category, symbol, path) * 1000)


def _advance_watermark(venue, category, symbol, snap_ts, path=None):
    con = _connect(path)
    with _tx(con):
        con.execute("UPDATE watermarks SET ts=MAX(ts, ?) WHERE venue=? AND category=? AND symbol=?",
                    (snap_ts - WATERMARK_LAG, venue, category, symbol))


def resume(path=None):
    """При старте: ордера, прерванные перезапуском посреди отправки (prepared/sending), — в unknown; их выяснит
    reconcile по клиентскому id. Возвращает эти строки (событие владельцу — в outbox)."""
    rows = _rows("SELECT client_id FROM orders WHERE state IN ('prepared', 'sending')", (), path)
    out = [_transition(r["client_id"], path, state="unknown", note="бот перезапустился во время отправки",
                       event="unknown")[0] for r in rows]
    con = _connect(path)
    with _tx(con):   # после перезапуска этот процесс ничего не отправляет — отметки «в отправке» сняты
        con.execute("UPDATE orders SET inflight_until=0 WHERE inflight_until>0")
    return out


def _clear_inflight(cid, path=None):
    """Отправка закончилась: снять отметку «в отправке» (процесс и строка)."""
    if not cid:
        return
    _inflight.discard(cid)
    try:
        con = _connect(path)
        with _tx(con):
            con.execute("UPDATE orders SET inflight_until=0 WHERE client_id=?", (cid,))
    except sqlite3.Error as e:   # база занята — отметка истечёт сама (INFLIGHT_TTL)
        logger.warning("отметка отправки %s не снята: %s", cid, type(e).__name__)


def resolve(client_id, state, note, filled=None, avg_price=None, path=None):
    """Разбор unknown владельцем на ПК (после проверки в кабинете биржи): unknown → closed/rejected/filled/open.
    filled/closed — только с исполненным количеством (filled, 0..qty; у filled — больше 0) и средней ценой (если
    исполнено больше 0): без них вклад строки в позицию бота угадывался бы. rejected — без исполнения. Строку,
    которую сейчас отправляет submit (этот процесс или бот из другого: отметка inflight_until в строке), разобрать
    нельзя; разбор меняет версию — отправка её больше не повторит."""
    if client_id in _inflight:
        raise ValueError("ордер ещё отправляется — разбор после окончания отправки")
    row = get(client_id, path)
    if row is not None and (row.get("inflight_until") or 0) > time.time():
        raise ValueError("ордер ещё отправляется (бот повторяет отправку) — разбор после окончания отправки")
    if row is None or row["state"] != "unknown" or state not in ("closed", "rejected", "filled", "open"):
        raise ValueError("разобрать можно только unknown")
    fields = {}
    f = None if filled is None else venues.dec(filled)
    if filled is not None and (f is None or f < 0 or f > Decimal(row["qty"])):
        raise ValueError("исполнено — число от 0 до количества ордера")
    if state in ("filled", "closed") and f is None:
        raise ValueError(f"разбор в {state} — только с исполненным количеством (filled) из кабинета биржи")
    if state == "filled" and f == 0:
        raise ValueError("filled с нулевым исполнением — это closed или rejected")
    if state == "rejected" and f:
        raise ValueError("rejected — ордер не исполнялся")
    if f is not None:
        fields["filled"] = venues.fmt(f)
        if f > 0:
            px = None if avg_price is None else venues.dec(avg_price)
            if px is None or px <= 0:
                raise ValueError("исполнено больше 0 — нужна средняя цена исполнения (avg_price)")
            fields["avg_price"] = venues.fmt(px)
    new, _ = _transition(client_id, path, expect=row["version"], state=state, note=f"владелец: {note}",
                         event="resolved", **fields)
    if new is None:
        raise ValueError("строка изменилась во время разбора — посмотрите ещё раз")
    return new


def close_out(client_id, path=None):
    """Позицию, открытую исполненным ордером, закрыли: filled → closed (вызывает стратегия). Вклад строки в позицию
    бота не меняется: исполненное записывается явно (у filled без числа — всё количество)."""
    row = get(client_id, path)
    if row is None or row["state"] != "filled":
        raise ValueError("закрыть можно только исполненный ордер (filled)")
    return _update(client_id, path, state="closed", filled=row["filled"] or row["qty"])


# --- сверка ответа биржи с намерением ---

def _venue_sym(row):
    """Символ биржи ордера — ровно из параметров создания (TONUSDT → GRAMUSDT решён в момент отправки)."""
    return row.get("venue_sym") or json.loads(row["params"])["symbol"]


def journal_venue_sym(venue, category, symbol, path=None):
    """Символ биржи позиции и ордеров бота по символу — из журнала (закрытие, стоп, плечо не зависят от свежести
    проверки TON → GRAM) или None — у бота по символу ничего нет. Разные символы — ValueError (не угадываем)."""
    syms = {r["venue_sym"] for r in _rows("SELECT venue_sym FROM keys WHERE venue=? AND category=? AND symbol=?",
                                          (venue, category, symbol), path)}
    syms |= {_venue_sym(r) for r in _rows(f"SELECT venue_sym, params FROM orders WHERE state IN "
                                          f"({_marks(UNSETTLED)}) AND venue=? AND category=? AND symbol=?",
                                          (*UNSETTLED, venue, category, symbol), path)}
    syms.discard("")
    if len(syms) > 1:
        raise ValueError(f"у бота по {symbol} разные символы биржи {sorted(syms)} — не угадываем")
    return next(iter(syms), None)


def _mismatch(row, view):
    """Поля ордера с биржи, не совпавшие с намерением (пусто — совпало). Количество, цена и цена срабатывания — как
    числа. Символ — ровно символ биржи из параметров создания: чужой, неизвестный (например «GRAM-USDT» BingX — другой
    токен) или пустой — несовпадение."""
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
    if row.get("trigger") and view.get("trigger") is not None and view["trigger"] != Decimal(row["trigger"]):
        bad.append("цена срабатывания")
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
              "fee": "" if view.get("fee") is None else venues.fmt(view["fee"]),
              "exec_ts": float(view["ts"]) if view.get("ts") else row.get("exec_ts")}
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
    """Итог submit; filled — исполнено ордером (открытие, закончившееся closed с частичным исполнением, открыло
    позицию на filled — стратегия видит открытую часть)."""
    return {"state": state, "row": row, "reason": reason, "event": event, "filled": _filled(row) if row else D0}


def _changed(cid):
    row = get(cid)
    return _result(row["state"] if row else "unknown", row, CHANGED, None)


def _late(cid, what):
    """Биржа приняла/показала ордер, а строка журнала уже завершена (владелец разобрал её, пока шёл повтор): не молча —
    событие владельцу (позиция на бирже может быть бота, журнал её не знает — символ станет «чужим»)."""
    row = get(cid)
    if row is not None and row["state"] in FINAL:
        emit("mismatch", f"{row['venue']} {row['symbol']}: {what} ордер бота {cid}, а в журнале он уже {row['state']} "
                         f"— позиция на бирже может быть бота, журнал её не знает; проверьте кабинет"[:500],
             client_id=cid, dedup=f"late|{cid}|{what}")
    return _changed(cid)


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


LIVE_COUNT = {"hedge": "count", "directional": "trades", "funding": "cycles"}   # число реальных сделок в gates


def live_stats(strategy, base=None, now=None, path=None):
    """Статистика реальной торговли стратегии по журналу (для порога «кнопка → авто»): days — дней с первого
    исполненного открытия в живом режиме, число сделок (хедж — count, направленная — trades, фандинг — cycles: группы),
    unknown_over_10min — ордеров стратегии, бывших unknown дольше UNKNOWN_LONG (по событиям outbox и нынешнему
    состоянию), limit_violations — дней МСК с убытком больше потолка дня. base — статистика из локального файла
    владельца (gates.load_live): что журнал считает сам, из файла берётся не лучше журнала."""
    now = time.time() if now is None else now
    opens = _rows("SELECT client_id, grp, created_ts, state, filled, qty FROM orders WHERE strategy=? AND "
                  "purpose='open' AND mode IN ('minlot', 'confirm', 'auto') ORDER BY created_ts", (strategy,), path)
    done = [r for r in opens if _filled(r) > 0]
    days = int((now - done[0]["created_ts"]) // 86400) if done else 0
    count = len({r["grp"] for r in done}) if strategy == "funding" else len(done)
    spans = {}   # client_id -> [начало unknown или None, было ли дольше]
    for e in _rows("SELECT o.client_id, o.ts, o.state FROM outbox o JOIN orders r ON r.client_id = o.client_id "
                   "WHERE r.strategy=? AND o.state != '' ORDER BY o.id", (strategy,), path):
        sp = spans.setdefault(e["client_id"], [None, False])
        if e["state"] == "unknown":
            sp[0] = e["ts"] if sp[0] is None else sp[0]
        elif sp[0] is not None:
            sp[1] = sp[1] or e["ts"] - sp[0] > UNKNOWN_LONG
            sp[0] = None
    for r in _rows("SELECT client_id, updated_ts FROM orders WHERE strategy=? AND state='unknown'", (strategy,), path):
        sp = spans.setdefault(r["client_id"], [None, False])
        start = sp[0] if sp[0] is not None else r["updated_ts"]
        sp[1] = sp[1] or (start is not None and now - start > UNKNOWN_LONG)
    long_unknown = sum(1 for _, was in spans.values() if was)
    violations = len(_rows("SELECT CAST((ts + 10800) / 86400 AS INTEGER) AS d, SUM(CAST(amount AS REAL)) AS s FROM pnl "
                           "GROUP BY d HAVING s < ?", (-float(risk.HARD["daily_loss_usdt"]),), path))
    out = dict(base) if isinstance(base, dict) else {}

    def not_better(key, mine, higher_is_better):
        try:
            theirs = float(out[key])
        except (KeyError, TypeError, ValueError):
            out[key] = mine
            return
        out[key] = min(theirs, mine) if higher_is_better else max(theirs, mine)
    not_better("days", days, True)
    if strategy in LIVE_COUNT:
        not_better(LIVE_COUNT[strategy], count, True)
    not_better("unknown_over_10min", long_unknown, False)
    not_better("limit_violations", violations, False)
    return out


def _gate_mode(strategy):
    """Самый рискованный режим, который разрешают пороги gates. Всё — только из локальных файлов владельца в data/ (не
    из git): бэктест (gates.load_backtest), бумага (gates.load_paper), реальная торговля (gates.load_live) — ещё и
    сверенная с журналом (live_stats: дни, сделки, unknown дольше 10 мин, нарушения лимитов). Словари вызывающего не
    принимаются. Нет ничего — paper."""
    bt, _ = gates.load_backtest(strategy)
    paper, _ = gates.load_paper(strategy)
    live, _ = gates.load_live(strategy)
    return gates.max_mode(strategy, paper.stats if paper is not None else None, bt,
                          live_stats(strategy, live.stats) if live is not None else None)


def _feed_problem(now=None, path=None):
    """Результат дня и позиции бота сверены недавно (watch не старше FEED_MAX_AGE), если у бота есть что сверять.
    → причина отказа или ""."""
    if not _rows("SELECT 1 FROM keys LIMIT 1", (), path) and not active(path=path):
        return ""
    ts = venues.dec(_get_meta("watch_ok", path))
    now = time.time() if now is None else now
    if ts is None or not 0 <= now - float(ts) <= FEED_MAX_AGE:
        return (f"сверка позиций бота и результата дня (reconcile) не проходила дольше {FEED_MAX_AGE} с — новые "
                f"открытия запрещены")
    return ""


def _pre_open(order, creds, strategy, mode, resend=None):
    """Проверки открытия без сети (до снимка символа и ещё раз под замком после): → (причина или "", режим). resend —
    строка, повтор которой решается (она уже в журнале: её не считаем ни блокирующей, ни лишним ордером)."""
    ok, why = switch.can_open()
    if not ok:
        return why, None
    if strategy not in risk.STRATEGIES:
        return f"стратегия {strategy!r} неизвестна — открытие только от стратегии ядра", None
    eff = _effective_mode(mode)
    gm = _gate_mode(strategy)
    if switch.RANK.get(gm, 0) < switch.RANK[eff]:
        eff = gm if gm in switch.RANK else "paper"
    if eff not in risk.LIVE_MODES:
        return f"режим {eff}: реальных открытий нет (пороги gates для {strategy} разрешают только {gm})", None
    if not creds:
        return f"{order.venue}: нет торгового ключа", None
    why = keys.check_status(order.venue, creds)[0]
    if why:
        return f"{order.venue}: {why}", None
    try:
        venues.prepare(order.venue, *venues.create_call(order, new_client_id()), creds, timestamp=0)
    except ValueError as e:
        return accounts._scrub(str(e), *creds), None
    b = blocking(exclude=resend)
    if b:
        return f"есть ордера с неясным исходом ({len(b)}) — новые открытия запрещены", None
    why = _feed_problem()
    if why:
        return why, None
    left = unsettled()
    if left:
        where = ", ".join(sorted({p["venue"] + " " + p["symbol"] for p in left}))
        return (f"закрытие позиции бота биржей не подтверждено ({len(left)}: {where}; settle_venue_close или сверка) "
                f"— убыток дня неизвестен, открытия всех стратегий запрещены"), None
    lim = risk.limits(strategy, eff)
    minute, day = order_counts()
    extra = 0 if resend else 1
    if minute + extra > lim["orders_per_min"]:
        return f"лимит ордеров в минуту: {lim['orders_per_min']}", None
    if day + extra > lim["orders_per_day"]:
        return f"лимит ордеров в день: {lim['orders_per_day']}", None
    return "", eff


def _uncovered_stops(path=None):
    """Ключи бота со стопом, у которых висящих стопов бота меньше позиции (стоп не встал, снят, частично) — описания."""
    out = []
    for k in _rows("SELECT * FROM keys WHERE stop != ''", (), path):
        net = Decimal(k["net"])
        book = bot_book(k["venue"], k["category"], k["symbol"], path)
        kb = book["keys"].get((k["strategy"] or "", k["grp"] or ""), _key_book())
        if kb["pending_stop"] < abs(net):
            out.append(f"{k['venue']} {k['symbol']} {k['strategy']}/{k['grp'] or '—'}: стоп "
                       f"{venues.fmt(kb['pending_stop'])} из {venues.fmt(abs(net))}")
    return out


def _guard(order, snap, strategy, group, mode, resend=None):
    """Проверки открытия по свежему снимку символа (под state_lock): возраст снимка, чужое (позиция, ордера, исполнения
    с отметки сверки; позиции бота нет — исполнения за последние WATERMARK_SLACK с), данные биржи, стопы позиций бота
    на месте, BingX — кросс-позиции владельца на аккаунте, risk.guard_open по свежим ценам всех позиций бота, капиталу и
    результату дня. → (причина или "", позиция бота закрыта биржей — нужна сверка)."""
    age = time.time() - snap.ts
    if not 0 <= age <= SNAPSHOT_MAX_AGE:
        return f"снимок символа устарел ({age:.1f} с > {SNAPSHOT_MAX_AGE} с) — открытие не отправлено", False
    book = bot_book(order.venue, order.category, order.symbol, exclude=resend)
    if order.category != "spot" and ownership.flat_external(snap, book):
        return ("на бирже по символу пусто, у бота по журналу позиция — её закрыла биржа или владелец: журнал "
                "сверяется, открытие отменено"), True
    why = ownership.unknown_why(snap, book)
    if why:
        return "данные биржи по символу не прочитаны — открытие запрещено: " + why[:300], False
    foreign = ownership.foreign(snap, book)
    if foreign:
        return f"{risk.FOREIGN}: " + "; ".join(foreign)[:300], False
    if snap.errors:
        return "данные биржи по символу не прочитаны — открытие запрещено: " + "; ".join(snap.errors)[:300], False
    if order.category != "spot" and book["net"] == 0:
        # сделка владельца за секунды до открытия: после исполнения бота она попала бы в окно отметки сверки и символ
        # стал бы «чужим» навсегда (без стопа, без закрытия) — открытие не отправляем
        if snap.execs is None:
            return "исполнения символа перед открытием не прочитаны — открытие запрещено", False
        recent = ownership.foreign_executions(snap.execs, book)
        if recent:
            return (f"{risk.FOREIGN}: только что было исполнение без id бота ({recent[-1]['kind']} "
                    f"{recent[-1]['side']} {venues.fmt(recent[-1]['qty'])}) — открытие отложено"), False
    bare = _uncovered_stops()
    if bare:
        return "позиция бота без полного стопа — открытия запрещены, пока стоп не встанет: " + "; ".join(bare)[:300], \
            False
    perp_books = None
    if order.category != "spot" and (snap.margin_mode == "cross" or order.venue == venues.BINGX):
        perp_books = {sym: bot_book(order.venue, order.category, sym)["net"] for sym in venues.SYMBOLS}
    if order.venue == venues.BINGX and order.category != "spot":
        # BingX: режим маржи — по символу; кросс-позиция владельца на другом символе живёт на том же балансе USDT-M,
        # с которого берётся изолированная маржа бота
        cross = ownership.owner_cross(snap, perp_books)
        if cross is None:
            return "BingX: позиции аккаунта не прочитаны — не проверить ваши кросс-позиции; открытие запрещено", False
        if cross:
            return (f"{risk.FOREIGN}: на аккаунте BingX ваши позиции с кросс-маржой ({'; '.join(cross)[:200]}) — маржа "
                    f"бота берётся из того же баланса; открытие запрещено"), False
    facc = None
    if snap.margin_mode == "cross":
        facc = ownership.foreign_account(snap, perp_books)
    try:
        exp = exposure(snap.marks or {}, exclude=resend)
    except ValueError as e:
        return f"позиции бота не оценить по свежим ценам: {e}", False
    missing = [f"{r['venue']} {r['symbol']}" for r in exp if (r["venue"], r["category"], r["symbol"]) not in
               (snap.marks or {})]
    if missing:
        return "нет свежей цены позиций бота: " + ", ".join(sorted(set(missing))), False
    unreal = sum((r["unrealized"] for r in exp), D0)
    rows = (snap.position or {}).get("rows") or []
    est_liq = rows[0]["liq"] if rows and book["net"] != 0 else None
    reasons = risk.guard_open(order, strategy, group, mode, mark=snap.mark, instrument=snap.instrument,
                              leverage=snap.leverage, margin_mode=snap.margin_mode, foreign=foreign,
                              foreign_account=facc, exposure=exp, realized_today=pnl_today(), unrealized=unreal,
                              capital=snap.capital, est_liq=est_liq)
    return "; ".join(reasons), False


def _pick_key(book, strategy, group):
    """Ключ закрытия/стопа: со стратегией — он; без стратегии — единственный ключ с позицией на символе (иначе причина:
    закрытие без стратегии оставило бы две встречные «ключевые» позиции). Направленная — без метки группы (ключ
    журнала — нога биржа:символ); метка берётся, только если такой ключ уже есть (база ранней версии). → (стратегия,
    группа, причина)."""
    if strategy == "directional" and group and (strategy, group) not in book["keys"]:
        group = ""
    if strategy:
        return strategy, group, ""
    nz = [(s, g) for (s, g), k in book["keys"].items() if k["net"] != 0]
    if len(nz) == 1:
        return nz[0][0], nz[0][1], ""
    if not nz:
        return "", "", ("у бота нет своей позиции по символу (по журналу) — закрывать нечего; чужие позиции бот не "
                        "трогает")
    return "", "", ("по символу несколько позиций бота (" + ", ".join(f"{s}/{g or '—'}" for s, g in nz) +
                    ") — закрытие и стоп только с указанием стратегии и группы")


def _spot_key(venue, symbol, strategy, group):
    if strategy:
        return strategy, group, ""
    nz = [(k["strategy"], k["grp"]) for k in _rows("SELECT strategy, grp FROM keys WHERE venue=? AND "
                                                   "category='spot' AND symbol=?", (venue, symbol))]
    if len(nz) == 1:
        return nz[0][0], nz[0][1], ""
    if not nz:
        return strategy, group, ""
    return "", "", "по споту несколько ключей бота — продажа только с указанием стратегии и группы"


def _reduce_problem(order, strategy, group, purpose="close", book=None):
    """Закрытие/стоп — только своей позиции бота по журналу: против её стороны и не больше позиции ключа (стратегия,
    группа) и символа без уже отправленных закрытий; стоп — не больше позиции ключа без уже висящих стопов. Спот —
    только купленное ботом."""
    if order.category == "spot":
        held = spot_inventory(order.venue, order.symbol)
        if order.qty > held:
            return (f"продать на споте можно только купленное ботом: {venues.fmt(held)} {order.symbol[:-4]} по "
                    f"журналу, в ордере {venues.fmt(order.qty)}")
        return ""
    book = book or bot_book(order.venue, order.category, order.symbol)
    k = book["keys"].get((strategy, str(group or "")))
    if not k or k["net"] == 0 or book["net"] == 0:
        return "у бота нет своей позиции по символу (по журналу) — закрывать нечего; чужие позиции бот не трогает"
    side = "sell" if k["net"] > 0 else "buy"
    if order.side != side or (book["net"] > 0) != (k["net"] > 0):
        return "закрытие — только против своей позиции бота"
    if purpose == "stop":
        room = abs(k["net"]) - k["pending_stop"]
    else:
        room = min(abs(book["net"]) - book["pending_reduce"][side], abs(k["net"]) - k["pending_reduce"][side])
    v = risk.check_close(order.qty, room, True)
    return "" if v.ok else "; ".join(v.reasons)


def _ownership_problem(snap, book):
    """Закрытие/стоп/настройки символа: вся позиция символа — бота. → причина или ""."""
    why = ownership.unknown_why(snap, book)
    if why:
        return "позиция и ордера символа не прочитаны — ничего не трогаем: " + why[:300]
    foreign = ownership.foreign(snap, book)
    if foreign:
        return f"{risk.FOREIGN}: " + "; ".join(foreign)[:300]
    if book["net"] == 0:
        return "у бота нет своей позиции по символу (по журналу) — закрывать нечего; чужие позиции бот не трогает"
    if not ownership.owned_whole(snap, book):
        return "на бирже позиции нет, а у бота по журналу есть (ордера с неясным исходом) — сначала сверка"
    return ""


def _foreign_event(venue, symbol, what, why):
    emit("foreign", f"{venue} {symbol}: {what} — {why}"[:500],
         dedup=f"foreign|{venue}|{symbol}|{what}|{why[:200]}|{int(time.time() // 3600)}")


async def _create(s, row, creds):
    params = json.loads(row["params"])   # ровно записанные параметры, в том же порядке
    try:
        status, j = await venues.call(s, row["venue"], row["method"], row["path"], params, creds)
    except Exception as e:   # таймаут, обрыв — ордер мог и уйти
        return "ambiguous", None, accounts.api_error_text(e)
    kind, data, _, msg = venues.outcome(row["venue"], status, j, creds)
    return kind, data, msg


async def submit(s, order, creds, *, purpose="open", strategy="", group="", mode="", notional=None, precheck=None):
    """Отправить ордер. Один вызов — не больше одного нового клиентского id (плюс условный стоп позиции бота после
    исполнения открытия со стопом и перестановка стопа после закрытия).

    Открытие: проверки без сети (выключатель, режим не выше порогов gates — бэктест, бумага и реальная торговля только
    из локальных файлов владельца, реальная ещё и по журналу: _gate_mode; стратегия, проверенный ключ, сборка
    параметров, unknown, частота, свежая сверка, неподтверждённые закрытия биржей) → под замком символа: сверка
    незавершённых ордеров бота по символу, снимок символа
    с биржи → под state_lock те же проверки ещё раз + _guard + precheck() вызывающего → намерение (prepared → sending).
    Закрытие/стоп: снимок символа (вся позиция — бота) + своя позиция по журналу (_reduce_problem) → намерение.
    Затем запрос (вне state_lock):
    - ok — принят (Bybit: open; BingX: по ответу), ответ не совпал с намерением — unknown + "mismatch";
    - точный отказ / «id занят» — запрос по id: найден — принимаем; «не найден» и неясностей не было — rejected;
    - неясный исход — unknown, пауза, запрос по id: найден — принимаем; «не найден» — повтор с тем же id и теми же
      параметрами (≤ MAX_RESEND; открытие — только пока торговля включена и после новой проверки символа; только если
      строку никто не менял); иначе unknown (разберёт reconcile).
    После открытия, если у ключа есть стоп (stop_loss ордера или стоп ключа), — исполнение и стоп бота на весь ключ;
    после закрытия — стоп ключа под оставшуюся позицию.
    notional вызывающего не нужен: номинал открытия ядро считает само по цене биржи. Направленная — без метки группы
    (ключ журнала — нога биржа:символ, как группа в risk; метка вызывающего отбрасывается).
    Возвращает {"state": refused|rejected|open|filled|closed|unknown, "row", "reason", "event", "filled"}; filled —
    исполнено (открытие, закончившееся closed с частичным исполнением, открыло позицию на filled)."""
    if purpose not in PURPOSES:
        raise ValueError(f"purpose {purpose!r}")
    if purpose == "open" and order.reducing:
        raise ValueError("ордер на открытие не может быть уменьшающим")
    if purpose != "open" and not order.reducing:
        raise ValueError("закрытие/стоп — только уменьшающий ордер (reduceOnly; на споте — продажа)")
    if (purpose == "stop") != (order.order_type == "stop"):
        raise ValueError("условный стоп (order_type stop) — только с purpose stop, и наоборот")
    group = str(group or "")
    if purpose == "open":
        if strategy == "directional":
            group = ""
        return await _submit_open(s, order, creds, strategy, group, mode, precheck)
    return await _submit_reduce(s, order, creds, purpose, strategy, group, precheck)


async def _open_snapshot(s, order, venue_sym, creds):
    """Снимок символа для открытия. Исполнения: у бота позиция — с отметки сверки; позиции нет — за последние
    WATERMARK_SLACK с (с этого момента после исполнения бота пойдёт отметка сверки: чужое в этом окне — не
    открываем)."""
    since = _exec_since(order.venue, order.category, order.symbol)
    if since is None and order.category != "spot":
        since = int((time.time() - WATERMARK_SLACK) * 1000)
    others = []
    for venue, category, symbol in _exposure_symbols():
        if (venue, category, symbol) == (order.venue, order.category, order.symbol):
            continue
        vs = journal_venue_sym(venue, category, symbol)
        if vs:
            others.append((venue, category, symbol, vs))
    return await ownership.fetch(s, order.venue, order.category, order.symbol, venue_sym, creds, market=True,
                                 settings=True, exec_since=since, others=others, capital=True)


async def _submit_open(s, order, creds, strategy, group, mode, precheck):
    why, _ = _pre_open(order, creds, strategy, mode)
    if why:
        return _result("refused", reason=why)
    venue_sym = venues.create_call(order, new_client_id())[2]["symbol"]
    cid = None
    async with symbol_lock(order.venue, order.symbol):
        try:
            await refresh_symbol(s, order.venue, order.category, order.symbol, creds)
            try:
                snap = await _open_snapshot(s, order, venue_sym, creds)
            except ValueError as e:
                return _result("refused", reason=f"снимок не собрать: {e}")
            flat = False
            async with state_lock():
                why, eff = _pre_open(order, creds, strategy, mode)   # за время сети всё могло измениться
                if not why:
                    why, flat = _guard(order, snap, strategy, group, eff)
                if not why and precheck is not None:
                    why = precheck() or ""
                if not why:
                    entry = order.price if order.order_type == "limit" else snap.mark
                    row = _insert_intent(order, "open", strategy, eff, order.qty * entry, group=group)
                    cid = row["client_id"]
                    _inflight.add(cid)
                    row = _update(cid, state="sending", posts=1, inflight_until=time.time() + INFLIGHT_TTL)
            if why:
                logger.info("открытие %s %s отклонено: %s", order.venue, order.symbol, why[:200])
                if flat:
                    await _settle_flat(s, order.venue, order.category, order.symbol, venue_sym, creds, snap)
                return _result("refused", reason=why)

            async def recheck(resend_cid):
                """Перед повтором открытия — новый снимок символа и все проверки (строка уже в журнале)."""
                try:
                    fresh = await _open_snapshot(s, order, venue_sym, creds)
                except ValueError as e:
                    return f"снимок не собрать: {e}"
                async with state_lock():
                    reason, eff2 = _pre_open(order, creds, strategy, mode, resend=resend_cid)
                    if not reason:
                        reason, _ = _guard(order, fresh, strategy, group, eff2, resend=resend_cid)
                return reason

            res = await _drive(s, row, creds, "open", recheck=recheck)
        finally:
            _clear_inflight(cid)
        # стоп — любой позиции от этого открытия: и open/filled, и closed/unknown с частичным исполнением (IOC отменила
        # остаток, BingX сразу ответил финальным статусом, ордер нашёлся по id после таймаута)
        if order.category != "spot" and res["row"] is not None \
                and (res["state"] in ("open", "filled") or _filled(res["row"]) > 0):
            fresh = await _after_open(s, res["row"], creds)
            if fresh is not None and fresh["state"] != res["state"]:
                res = _result(fresh["state"], fresh, res["reason"], res["event"])
            elif fresh is not None:
                res = _result(res["state"], fresh, res["reason"], res["event"])
        return res


async def _await_fill(s, row, creds):
    """Исполнение только что принятого ордера (рынок и IOC исполняются сразу): несколько запросов по id, пока строка
    open без полного исполнения. → свежая строка."""
    for i in range(FILL_TRIES):
        cur = get(row["client_id"])
        if cur is None or cur["state"] != "open" or _filled(cur) >= (venues.dec(cur["qty"]) or D0):
            return cur
        if i:
            await asyncio.sleep(RETRY_DELAY)
        await _reconcile_rows(s, [cur], lambda v: creds)
    return get(row["client_id"])


async def _after_open(s, row, creds):
    """После открытия перпа: если у ключа есть стоп (стоп ордера или ключа) — исполнение и стоп бота на весь ключ; не
    встал — событие владельцу (открытия заблокированы, пока стоп не встанет: _uncovered_stops; сверка повторит).
    → свежая строка ордера или None (стопа у ключа нет — ничего не делали)."""
    if not row["stop_loss"] and not _rows("SELECT 1 FROM keys WHERE venue=? AND category=? AND symbol=? AND strategy=? "
                                          "AND grp=? AND stop != ''", _key_id(row)):
        return None
    try:
        await _await_fill(s, row, creds)
        why = await _sync_stops(s, row["venue"], row["category"], row["symbol"], row["strategy"] or "",
                                row["grp"] or "", creds)
    except Exception as e:   # noqa: BLE001 — сбой постановки стопа — тревога, сверка повторит
        why = f"{type(e).__name__}: {e}"
    if why:
        emit("stop_missing", f"{row['venue']} {row['symbol']}: стоп позиции бота не поставлен — {why}"[:500],
             client_id=row["client_id"], dedup=f"stop_missing|{row['client_id']}|{why[:200]}")
    return get(row["client_id"])


async def _flat_settings(s, venue, venue_sym, creds, snap):
    """Режим маржи и плечо символа с биржи — для худшего убытка позиции, закрытой не ботом (sync_flat). Не узнали —
    None: весь номинал."""
    try:
        margin, _ = await venues.margin_mode(s, venue, venue_sym, creds)
        if venue == venues.BYBIT:
            lev = (snap.position or {}).get("leverage")
        else:
            lev, _ = await venues.symbol_leverage(s, venue_sym, creds)
    except Exception:   # noqa: BLE001 — не узнали — худшее
        return None, None
    return margin, lev


def _last_key_update(venue, category, symbol):
    last = _rows("SELECT MAX(updated_ts) AS t FROM keys WHERE venue=? AND category=? AND symbol=?",
                 (venue, category, symbol))
    return last[0]["t"] if last and last[0]["t"] is not None else None


async def _settle_flat(s, venue, category, symbol, venue_sym, creds, snap):
    """На бирже пусто, у бота по журналу позиция: сначала сверка незавершённых ордеров бота по символу (стоп бота мог
    только что исполниться — его исполнение попадёт в журнал). sync_flat — только если каждая такая строка сверена
    в этом проходе (запрос не удался — не угадываем: исполнение стопа бота, записанное после обнуления, дало бы
    фантомную встречную позицию), в исполнениях символа нет исполнения ещё не завершённого ордера бота, всё ещё пусто и
    последнее исполнение бота по символу старше FLAT_GRACE (ответ о позиции не отстал). Худший убыток — по режиму маржи
    и плечу символа с биржи (flat_worst). Потом снятие висящих стопов бота (иначе они сработали бы по будущей позиции
    владельца). → ([(событие, строка символа)], сверка прошла)."""
    async with state_lock():
        rows = [r for r in active() if (r["venue"], r["category"], r["symbol"]) == (venue, category, symbol)
                and r["client_id"] not in _inflight]
        vers = {r["client_id"]: r["version"] for r in rows}
    await _reconcile_rows(s, rows, lambda v: creds)
    margin, lev = await _flat_settings(s, venue, venue_sym, creds, snap)
    ev, why = None, ""
    async with state_lock():
        book = bot_book(venue, category, symbol)
        stale = [c for c, v in vers.items() if (g := get(c)) is not None and g["state"] in ACTIVE
                 and g["version"] == v]
        active_ids = set(book["active"])
        mine = [e for e in snap.execs or () if e["client_id"] and e["client_id"] in active_ids
                and e["kind"] not in venues.NO_POSITION_EXEC]
        last = _last_key_update(venue, category, symbol)
        settled = last is None or snap.ts - last >= FLAT_GRACE
        if stale:
            why = "ордера бота по символу не сверены (" + ", ".join(stale) + ") — закрытие биржей не записываем"
        elif mine:
            why = "есть исполнение ордера бота, ещё не записанное в журнал — сначала сверка"
        elif ownership.flat_external(snap, book) and settled:
            ev = sync_flat(venue, category, symbol, margin_mode=margin, leverage=lev)
    if why:
        emit("watch_error", f"{venue} {symbol}: на бирже пусто, у бота по журналу позиция — {why}"[:500],
             dedup=f"watch|{venue}|{symbol}|flat|{why[:80]}|{int(time.time() // 3600)}")
        return [], False
    if ev:
        await _cancel_rows(s, venue, category, symbol, creds, purpose="stop")
    return ([(ev, _symbol_row(venue, category, symbol))] if ev else []), True


def _symbol_row(venue, category, symbol):
    """«Строка» события сверки символа (не ордера) — для вызывающего reconcile: те же ключи, client_id пустой."""
    return {"client_id": "", "venue": venue, "category": category, "symbol": symbol}


async def _cancel_rows(s, venue, category, symbol, creds, purpose=None, strategy=None, group=None):
    """Снять висящие ордера бота по символу (purpose — только такие; strategy/group — только ключа) и сверить их.
    Снимаются и строки с неясным исходом (unknown): снятие своего ордера по своему id владельца не касается, а
    неподтверждённый стоп мог встать. → строки, что остались незавершёнными."""
    def pick():
        return [r for r in _rows(f"SELECT * FROM orders WHERE state IN ({_marks(UNSETTLED)}) AND venue=? AND "
                                 f"category=? AND symbol=?", (*UNSETTLED, venue, category, symbol))
                if (purpose is None or r["purpose"] == purpose) and r["client_id"] not in _inflight
                and (strategy is None or ((r["strategy"] or ""), (r["grp"] or "")) == (strategy, group))]
    rows = pick()
    for r in rows:
        if r["state"] in ("open", "unknown"):
            try:
                await cancel(s, r["client_id"], creds)
            except Exception as e:   # noqa: BLE001
                logger.warning("снятие %s: %s", r["client_id"], type(e).__name__)
    if rows:
        await _reconcile_rows(s, [get(r["client_id"]) for r in rows], lambda v: creds)
    return pick()


async def _sync_stops(s, venue, category, symbol, strategy, group, creds):
    """Стоп ключа бота: ровно один висящий условный стоп бота на |сальдо ключа| по цене стопа ключа. Не так — снять все
    стопы ключа (и сверить: стоп мог исполниться), потом поставить новый на оставшуюся позицию (через _submit_reduce —
    с проверкой, что вся позиция символа бота). Стопа у ключа нет или позиция закрыта — только снять. → причина
    неудачи или "". Стоп не подтверждён на бирже (неясный исход, отказ, не поставлен) — программный стоп: цена уже за
    стопом ключа — закрыть позицию ключа рынком (_breach_close)."""
    async with _stop_lock(venue, symbol):
        book = bot_book(venue, category, symbol)
        k = book["keys"].get((strategy, group), _key_book())
        want = abs(k["net"]) if k["stop"] is not None else D0
        stops = [get(c) for c in k["stops"]]
        if any(r["state"] in BLOCKING for r in stops):
            return await _breach_close(s, venue, category, symbol, strategy, group, k, creds,
                                       "у стопа ключа неясный исход — сначала сверка")
        close_side = "sell" if k["net"] > 0 else "buy"
        if want and len(stops) == 1 and stops[0]["state"] == "open" and stops[0]["side"] == close_side \
                and Decimal(stops[0]["qty"]) - _filled(stops[0]) == want \
                and venues.dec(stops[0]["trigger"]) == k["stop"]:
            return ""
        if not want and not stops:
            return ""
        left = await _cancel_rows(s, venue, category, symbol, creds, purpose="stop", strategy=strategy, group=group)
        if left:
            return "старый стоп ключа не снят: " + ", ".join(r["client_id"] for r in left)
        book = bot_book(venue, category, symbol)
        k = book["keys"].get((strategy, group), _key_book())
        want = abs(k["net"]) if k["stop"] is not None else D0
        if not want:
            return ""
        close_side = "sell" if k["net"] > 0 else "buy"
        order = venues.Order(venue, category, symbol, close_side, "stop", want, reduce_only=True, trigger=k["stop"])
        res = await _submit_reduce(s, order, creds, "stop", strategy, group, None)
        if res["state"] == "open":
            return ""
        # стоп не подтверждён (отказ — цена могла уже пройти стоп; неясный исход; отказ ядра) — программный стоп
        return await _breach_close(s, venue, category, symbol, strategy, group, k, creds,
                                   f"стоп не встал ({res['state']}): {res['reason']}"[:300])


async def _breach_close(s, venue, category, symbol, strategy, group, k, creds, why):
    """Программный стоп: подтверждённого живого стопа ключа нет, а свежая цена уже за стопом ключа — закрыть позицию
    ключа рынком (reduceOnly, через _submit_reduce: вся позиция символа бота), как сделал бы стоп; событие владельцу.
    Цена не за стопом или её нет — why. Замок стопов символа уже взят вызывающим. → причина неудачи или ""."""
    if not k["net"] or k["stop"] is None:
        return why
    try:
        mark, _ = await venues.mark_price(s, venue, category, journal_venue_sym(venue, category, symbol))
    except Exception:   # noqa: BLE001
        mark = None
    if mark is None or not ((k["net"] > 0 and mark <= k["stop"]) or (k["net"] < 0 and mark >= k["stop"])):
        return why
    side = "sell" if k["net"] > 0 else "buy"
    close = venues.Order(venue, category, symbol, side, "market", abs(k["net"]), reduce_only=True)
    done = await _submit_reduce(s, close, creds, "close", strategy, group, None, after=False)
    if done["state"] == "refused":   # закрытие не ушло (чужое на символе, данные не прочитаны) — тревога вызывающего
        return (f"{why}; цена {venues.fmt(mark)} уже за стопом {venues.fmt(k['stop'])}, закрыть рынком нельзя: "
                f"{done['reason']}")[:300]
    emit("stop_breached", f"{venue} {symbol}: цена {venues.fmt(mark)} уже за стопом {venues.fmt(k['stop'])}, а стоп "
                          f"бота на бирже не подтверждён ({why[:120]}) — позиция ключа закрыта рынком "
                          f"({done['state']})"[:500],
         dedup=f"breach|{venue}|{symbol}|{strategy}|{group}|{int(time.time() // 60)}")
    if done["state"] in ("open", "filled") or done.get("filled", D0) > 0:
        return ""
    return f"стоп не встал, цена за стопом, закрытие: {done['state']} {done['reason']}"[:300]


async def _submit_reduce(s, order, creds, purpose, strategy, group, precheck, after=True):
    """Закрытие/стоп (см. submit). after — после закрытия переставить стоп ключа (нет — при аварийном закрытии из
    _sync_stops: замок стопов уже взят)."""
    if not creds:
        return _result("refused", reason=f"{order.venue}: нет торгового ключа")
    if order.category == "spot":
        return await _submit_spot_sell(s, order, creds, purpose, strategy, group, precheck)
    try:
        venue_sym = journal_venue_sym(order.venue, order.category, order.symbol)
        if venue_sym is None:
            return _result("refused", reason="у бота нет своей позиции по символу (по журналу) — закрывать нечего; "
                                             "чужие позиции бот не трогает")
        venues.prepare(order.venue, *venues.create_call(order, new_client_id(), venue_sym=venue_sym), creds,
                       timestamp=0)
    except ValueError as e:
        return _result("refused", reason=accounts._scrub(str(e), *creds))
    await refresh_symbol(s, order.venue, order.category, order.symbol, creds)   # свежие исполнения своих ордеров
    snap = await ownership.fetch(s, order.venue, order.category, order.symbol, venue_sym, creds, market=False,
                                 settings=False, exec_since=_exec_since(order.venue, order.category, order.symbol))
    async with state_lock():
        flat = ownership.flat_external(snap, bot_book(order.venue, order.category, order.symbol))
    if flat:
        await _settle_flat(s, order.venue, order.category, order.symbol, venue_sym, creds, snap)
        return _result("refused", reason="на бирже по символу пусто, у бота по журналу позиция — её закрыла биржа или "
                                         "владелец (журнал сверяется, событие владельцу): закрывать нечего")
    cid = None
    try:
        async with state_lock():
            book = bot_book(order.venue, order.category, order.symbol)
            why = _ownership_problem(snap, book)
            if why and book["net"] != 0:
                _foreign_event(order.venue, order.symbol, "закрытие/стоп не отправлены", why)
            if not why:
                strategy, group, why = _pick_key(book, strategy, group)
            if not why:
                why = _reduce_problem(order, strategy, group, purpose, book)
            if not why and precheck is not None:
                why = precheck() or ""
            if why:
                return _result("refused", reason=why)
            row = _insert_intent(order, purpose, strategy, switch.mode(), None, group=group, venue_sym=venue_sym)
            cid = row["client_id"]
            _inflight.add(cid)
            row = _update(cid, state="sending", posts=1, inflight_until=time.time() + INFLIGHT_TTL)

        async def recheck(resend_cid):
            """Перед повтором закрытия/стопа — новый снимок символа: вся позиция по-прежнему бота и своя позиция ключа
            (строка уже в журнале — её не считаем)."""
            fresh = await ownership.fetch(s, order.venue, order.category, order.symbol, venue_sym, creds, market=False,
                                          settings=False,
                                          exec_since=_exec_since(order.venue, order.category, order.symbol))
            async with state_lock():
                book2 = bot_book(order.venue, order.category, order.symbol, exclude=resend_cid)
                reason = _ownership_problem(fresh, book2)
                if not reason:
                    reason = _reduce_problem(order, strategy, group, purpose, book2)
            return reason

        res = await _drive(s, row, creds, purpose, recheck=recheck)
    finally:
        _clear_inflight(cid)
    if purpose == "close" and res["state"] in ("open", "filled", "closed"):
        if not after:
            fresh = await _await_fill(s, res["row"], creds)
            return _result(fresh["state"], fresh, res["reason"], res["event"]) if fresh else res
        try:
            fresh = await _await_fill(s, res["row"], creds)
            if fresh is not None and fresh["state"] != res["state"]:
                res = _result(fresh["state"], fresh, res["reason"], res["event"])
            why = await _sync_stops(s, order.venue, order.category, order.symbol, strategy, group, creds)
        except Exception as e:   # noqa: BLE001
            why = f"{type(e).__name__}: {e}"
        if why:
            emit("stop_missing", f"{order.venue} {order.symbol}: после закрытия стоп ключа не переставлен — "
                                 f"{why}"[:500],
                 client_id=res["row"]["client_id"], dedup=f"stop_resize|{res['row']['client_id']}|{why[:200]}")
    return res


async def _submit_spot_sell(s, order, creds, purpose, strategy, group, precheck):
    """Продажа на споте — только купленное ботом (spot_inventory); без стратегии — единственный спот-ключ символа."""
    try:
        venue_sym = journal_venue_sym(order.venue, order.category, order.symbol)
        venues.prepare(order.venue, *venues.create_call(order, new_client_id(), venue_sym=venue_sym), creds,
                       timestamp=0)
    except ValueError as e:
        return _result("refused", reason=accounts._scrub(str(e), *creds))
    await refresh_symbol(s, order.venue, order.category, order.symbol, creds)
    cid = None
    try:
        async with state_lock():
            strategy, group, why = _spot_key(order.venue, order.symbol, strategy, group)
            if not why:
                why = _reduce_problem(order, strategy, group, purpose)
            if not why and precheck is not None:
                why = precheck() or ""
            if why:
                return _result("refused", reason=why)
            row = _insert_intent(order, purpose, strategy, switch.mode(), None, group=group, venue_sym=venue_sym)
            cid = row["client_id"]
            _inflight.add(cid)
            row = _update(cid, state="sending", posts=1, inflight_until=time.time() + INFLIGHT_TTL)
        return await _drive(s, row, creds, purpose)
    finally:
        _clear_inflight(cid)


async def _drive(s, row, creds, purpose, recheck=None):
    """Запрос создания и разбор исхода (см. submit). Сеть и паузы — вне state_lock; каждая запись — под ним. recheck —
    проверка перед повтором (новый снимок символа: чужое, своя позиция, для открытия — все проверки открытия):
    причина — повтора нет, строка unknown, событие."""
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
                return _late(cid, "биржа приняла") if kind == "ok" else _changed(cid)
            ver = row["version"]
        logger.warning("ордер %s: %s (%s)", cid, "исход неясен" if kind != "rejected" else "отказ", msg)
        if kind == "ambiguous":
            await asyncio.sleep(RETRY_DELAY * posts)   # запрос точно закончился; даём бирже время
        fkind, view, _ = await venues.find_order(s, row["venue"], row["category"], _venue_sym(row), cid, creds)
        async with state_lock():
            cur = get(cid)
            try:
                if fkind == "found":
                    if cur["state"] in FINAL:
                        live = view.get("state") in ("open", "filled") or (view.get("filled") or D0) > 0
                        return _late(cid, "биржа нашла живым/исполненным") if live else _changed(cid)
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
        if recheck is not None:   # повтор — только после новой проверки символа
            why = await recheck(cid)
            if why:
                async with state_lock():
                    cur = get(cid)
                    if cur["version"] != ver or cur["state"] != "unknown":
                        return _changed(cid)
                    row, _ = _transition(cid, note=f"{cur['note']}; повтор не отправлен: {why}"[:600], event="unknown")
                return _result("unknown", row, row["note"], "unknown")
        async with state_lock():   # решение о повторе — по той же версии строки и прямо перед отправкой
            cur = get(cid)
            if cur["version"] != ver or cur["state"] != "unknown" or cur["cancel_requested"]:
                return _changed(cid)
            ok, why = _may_send(purpose)
            if not ok:
                row, _ = _transition(cid, note=f"{cur['note']}; {why} — повтор не отправлен", event="unknown")
                return _result("unknown", row, row["note"], "unknown")
            posts += 1
            row, _ = _transition(cid, expect=ver, posts=posts, inflight_until=time.time() + INFLIGHT_TTL)
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
        if row is None:
            continue
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


async def reconcile(s, creds_for, now=None, positions=True):
    """Сверка с биржей: брошенные prepared/sending (не идущие сейчас) → unknown; каждый open/unknown любого возраста —
    запрос по клиентскому id. Снимок строк — под коротким замком, запросы — вне его, запись — только если строка не
    изменилась. positions — затем сверка символов с позициями и ордерами бота (watch: может снять ордера бота и
    переставить стоп позиции бота; ордеров на открытие и повторов отправки тут нет). [(событие, строка)] — что нового
    (то же уже лежит в outbox; у событий символа строка — {client_id "", venue, category, symbol}). Ошибка запроса —
    строка не меняется."""
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
    events += await _reconcile_rows(s, rows, creds_for)
    if positions:
        events += await watch(s, creds_for)
    return events


async def refresh_symbol(s, venue, category, symbol, creds, deadline=None):
    """Сверить незавершённые ордера бота по одному символу (перед открытием/закрытием: свежие исполнения) — параллельно
    и не дольше REFRESH_DEADLINE: не успели — дальше по журналу (он консервативен: неисполненное — ожидающее)."""
    async with state_lock():
        rows = [r for r in active() if (r["venue"], r["category"], r["symbol"]) == (venue, category, symbol)
                and r["client_id"] not in _inflight]
    if not rows:
        return []
    try:
        got = await asyncio.wait_for(asyncio.gather(*(_reconcile_rows(s, [r], lambda v: creds) for r in rows)),
                                     REFRESH_DEADLINE if deadline is None else deadline)
    except asyncio.TimeoutError:
        logger.warning("сверка %s %s не уложилась в %s с — дальше по журналу", venue, symbol, REFRESH_DEADLINE)
        return []
    return [e for g in got for e in g]


def _watch_symbols(path=None):
    """Перп-символы, где у бота позиция или незавершённые ордера."""
    seen = {(r["venue"], r["category"], r["symbol"]) for r in _rows("SELECT venue, category, symbol FROM keys", (),
                                                                     path) if r["category"] != "spot"}
    seen |= {(r["venue"], r["category"], r["symbol"]) for r in active(path=path) if r["category"] != "spot"}
    return sorted(seen)


async def _book_funding(s, venue, category, symbol, venue_sym, snap, creds):
    """Фандинг позиции бота в результат дня — консервативно как расход (−|сумма|: знак начисления у бирж не сверен,
    TODO(api)). Bybit — исполнения Funding из снимка, BingX — начисления FUNDING_FEE с отметки сверки."""
    """Фандинг позиции бота в результат дня — консервативно как расход (−|сумма|: знак начисления у бирж не сверен,
    TODO(api)). Bybit — исполнения Funding из снимка, BingX — начисления FUNDING_FEE со своей отметки (fund в
    watermarks: сбой чтения начислений не держит отметку сверки исполнений и ничего не теряет). → причина сбоя
    или ""."""
    if venue == venues.BYBIT:
        items = [(f"funding:{e['exec_id']}", e["fee"], e["ts"]) for e in snap.execs or ()
                 if e["kind"] in venues.NO_POSITION_EXEC and e["fee"] is not None]
    else:
        since = _rows("SELECT COALESCE(fund, ts) AS t FROM watermarks WHERE venue=? AND category=? AND symbol=?",
                      (venue, category, symbol))
        if not since or since[0]["t"] is None:
            return ""
        got, why = await venues.funding_income(s, venue_sym, int(since[0]["t"] * 1000), int(snap.ts * 1000), creds)
        if why:
            return f"начисления фандинга не прочитаны: {why}"
        items = [(f"funding:{g['ref']}", g["amount"], g["ts"]) for g in got]
    con = _connect()
    with _tx(con):
        for ref, amount, ts in items:
            if amount:
                _add_pnl_in(con, venue, symbol, -abs(amount), "funding", ref, ts / 1000)
        if venue != venues.BYBIT:
            con.execute("UPDATE watermarks SET fund=MAX(COALESCE(fund, ts), ?) WHERE venue=? AND category=? AND "
                        "symbol=?", (snap.ts - WATERMARK_LAG, venue, category, symbol))
    return ""


def _stop_rows(k):
    return [r for r in (get(c) for c in k["stops"]) if r is not None and r["client_id"] not in _inflight]


async def _drop_excess_stops(s, venue, category, symbol, creds):
    """Стопы бота больше позиции своего ключа (частичное закрытие, а перестановка не удалась; ключ без позиции; не та
    сторона) — снять ДО любой проверки символа и раннего выхода: снятие своего ордера по своему id владельца не
    касается, а лишний reduceOnly-стоп сработал бы по позиции владельца. → [(ключ, остались ли неснятые)] — ключи с
    позицией, чьи стопы сняты."""
    out = []
    async with _stop_lock(venue, symbol):
        book = bot_book(venue, category, symbol)
        for (strategy, group), k in list(book["keys"].items()):
            rows = _stop_rows(k)
            if not rows:
                continue
            close_side = "sell" if k["net"] > 0 else "buy"
            rem = sum((max((venues.dec(r["qty"]) or D0) - _filled(r), D0) for r in rows), D0)
            if k["net"] != 0 and rem <= abs(k["net"]) and all(r["side"] == close_side for r in rows):
                continue
            left = await _cancel_rows(s, venue, category, symbol, creds, purpose="stop", strategy=strategy,
                                      group=group)
            if k["net"] != 0:
                out.append(((strategy, group), bool(left)))
    return out


def _bot_share_intact(snap, book):
    """На символе чужое, но доля бота доказуемо цела — стоп бота на |сальдо ключа| закрывает только её: позиция,
    ордера и исполнения прочитаны, неясных ордеров бота нет, позиция на бирже той же стороны и не меньше бота, после
    отметки сверки все исполнения без id бота — в сторону позиции бота (владелец только добавлял; ликвидация, ADL,
    закрытие владельцем — всегда в обратную) и сальдо бота плюс они — ровно позиция на бирже."""
    if snap.position is None or snap.execs is None or book["uncertain"] or book["net"] == 0:
        return False
    if any(e.startswith(("позиция:", "ордера:", "исполнения:")) for e in snap.errors):
        return False
    net, bot = snap.position["net"], book["net"]
    if net == 0 or (net > 0) != (bot > 0) or abs(net) < abs(bot):
        return False
    added = D0
    for e in ownership.foreign_executions(snap.execs, book):
        signed = e["qty"] if e["side"] == "buy" else -e["qty"] if e["side"] == "sell" else None
        if signed is None or signed == 0 or (signed > 0) != (bot > 0):
            return False
        added += signed
    return bot + added == net


def _unprotected(venue, category, symbol, dropped, why):
    """Событие владельцу: стоп больше позиции ключа снят (_drop_excess_stops), а новый поставить нельзя — позиция
    ключа без стопа. dropped — [(ключ, остались ли неснятые)]. → события."""
    if not dropped:
        return []
    what = ", ".join(f"{st or '—'}/{g or '—'}" + (" (снялся не весь)" if left else "") for (st, g), left in dropped)
    ev = emit("stop_removed", f"{venue} {symbol}: стоп бота был больше позиции ключа ({what}) и снят; {why} — позиция "
                              f"ключа БЕЗ СТОПА, открытия запрещены, пока стоп не встанет"[:500],
              dedup=f"stop_removed|{venue}|{symbol}|excess|{what[:80]}|{int(time.time() // 3600)}")
    return [(ev, _symbol_row(venue, category, symbol))] if ev else []


async def _drop_stops(s, venue, category, symbol, creds, why):
    """Снять все стопы бота по символу (не доказать, что они закроют только долю бота) и сказать владельцу, что позиция
    бота теперь без стопа: бот её не трогает, открытия запрещены (_uncovered_stops), пока символ не станет снова только
    бота (тогда сверка поставит стоп). → события."""
    async with _stop_lock(venue, symbol):
        book = bot_book(venue, category, symbol)
        had = [c for k in book["keys"].values() for c in k["stops"] if c not in _inflight]
        if not had:
            return []
        left = await _cancel_rows(s, venue, category, symbol, creds, purpose="stop")
    keys_left = ", ".join(f"{st or '—'}/{g or '—'} {venues.fmt(k['net'])}" for (st, g), k in book["keys"].items()
                          if k["net"] != 0) or "—"
    note = (f"{venue} {symbol}: {why} — стопы бота сняты (иначе сработали бы по вашей позиции); позиция бота "
            f"({keys_left}) теперь БЕЗ СТОПА, бот её не трогает, открытия запрещены — проверьте кабинет"
            + (f"; не снялись: {', '.join(r['client_id'] for r in left)}" if left else ""))
    ev = emit("stop_removed", note[:500], dedup=f"stop_removed|{venue}|{symbol}|{why[:80]}|{len(left)}|"
                                                f"{int(time.time() // 3600)}")
    return [(ev, _symbol_row(venue, category, symbol))] if ev else []


async def _watch_symbol(s, venue, category, symbol, creds):
    """Сверка одного символа с позициями/ордерами бота. Сначала — стопы бота больше позиции своего ключа снимаются (до
    любой проверки и раннего выхода). Чужое на символе — событие, снятие ордеров бота на открытие и всех стопов бота,
    если не доказать, что они закроют только долю бота (_bot_share_intact). На бирже пусто, а у бота по журналу
    позиция при неясных ордерах — стопы бота снимаются (защищать нечего, сработали бы по будущей позиции владельца).
    Иначе — стопы ключей (до фандинга: сбой начислений защиту не пропускает), фандинг, отметка сверки. → (события,
    прошла ли)."""
    try:
        venue_sym = journal_venue_sym(venue, category, symbol)
    except ValueError as e:
        emit("watch_error", f"{venue} {symbol}: {e}", dedup=f"watch|{venue}|{symbol}|sym|{int(time.time() // 3600)}")
        return [], False
    if venue_sym is None:
        return [], True
    dropped = await _drop_excess_stops(s, venue, category, symbol, creds)
    snap = await ownership.fetch(s, venue, category, symbol, venue_sym, creds, market=False, settings=False,
                                 exec_since=_exec_since(venue, category, symbol))
    async with state_lock():
        book = bot_book(venue, category, symbol)
        flat = ownership.flat_external(snap, book)
    if flat:
        return await _settle_flat(s, venue, category, symbol, venue_sym, creds, snap)
    async with state_lock():
        book = bot_book(venue, category, symbol)
        why = ownership.unknown_why(snap, book)
        foreign = None if why else ownership.foreign(snap, book)
        intact = bool(foreign) and _bot_share_intact(snap, book)
        last = _last_key_update(venue, category, symbol)
        empty = not why and not foreign and book["net"] != 0 and snap.position is not None \
            and snap.position["net"] == 0 and (last is None or snap.ts - last >= FLAT_GRACE)
    events = []
    if why:
        emit("watch_error", f"{venue} {symbol}: сверка символа не прошла — {why}"[:500],
             dedup=f"watch|{venue}|{symbol}|{why[:120]}|{int(time.time() // 3600)}")
        events += _unprotected(venue, category, symbol, dropped, "символ не прочитан — новый стоп не ставим")
        return events, False
    if foreign:
        _foreign_event(venue, symbol, "на символе с позицией/ордерами бота появилось чужое", "; ".join(foreign))
        await _cancel_rows(s, venue, category, symbol, creds, purpose="open")   # висящие ордера бота на открытие
        if not intact:
            events += await _drop_stops(s, venue, category, symbol, creds, "на символе чужое, и не доказать, что "
                                                                          "позиция бота цела")
        else:
            events += _unprotected(venue, category, symbol, dropped, "на символе чужое — новый стоп не ставим")
        return [("foreign", _symbol_row(venue, category, symbol))] + events, True
    if empty:
        events += await _drop_stops(s, venue, category, symbol, creds, "на бирже по символу пусто, у бота по журналу "
                                                                      "позиция (ордера с неясным исходом)")
        return events, True
    if book["net"] != 0:
        _advance_watermark(venue, category, symbol, snap.ts)   # чужого нет — исполнения до этого места сверены
    problems = []
    for (strategy, group), k in list(book["keys"].items()):
        if k["net"] != 0 or k["stops"]:
            why = await _sync_stops(s, venue, category, symbol, strategy, group, creds)
            if why:
                problems.append(why)
    if problems:
        emit("stop_missing", f"{venue} {symbol}: стоп позиции бота не на месте — " + "; ".join(problems)[:400],
             dedup=f"stops|{venue}|{symbol}|{problems[0][:120]}|{int(time.time() // 3600)}")
    ok = True
    if book["net"] != 0:
        why = await _book_funding(s, venue, category, symbol, venue_sym, snap, creds)
        if why:
            ok = False
            emit("watch_error", f"{venue} {symbol}: {why}"[:500],
                 dedup=f"watch|{venue}|{symbol}|funding|{why[:80]}|{int(time.time() // 3600)}")
    return events, ok


async def _refine_venue_closes(s, creds_for):
    """Bybit: неподтверждённые закрытия биржей уточнить по closed-pnl — только если в окне закрытые результаты не бота
    ровно на размер позиции бота (иначе — владелец, settle_venue_close). TODO(api): BingX — только владелец."""
    events = []
    for p in unsettled(venue=venues.BYBIT):
        creds = creds_for(p["venue"])
        if not creds or len([q for q in unsettled(venue=p["venue"]) if q["symbol"] == p["symbol"]]) != 1:
            continue
        rows, why = await venues.closed_pnl(s, p["venue_sym"], int(p["since"] * 1000), int(p["ts"] * 1000) + 60000,
                                            creds)
        if why:
            continue
        ours = {r["venue_order_id"] for r in _rows("SELECT venue_order_id FROM orders WHERE venue=? AND category=? AND "
                                                   "symbol=? AND created_ts>=?", (p["venue"], p["category"],
                                                                                   p["symbol"], p["since"]))}
        theirs = [r for r in rows if r["order_id"] not in ours]
        if theirs and sum((r["qty"] for r in theirs), D0) == Decimal(p["qty"]):
            settle_venue_close(p["ref"], sum((r["pnl"] for r in theirs), D0), "по closed-pnl Bybit")
            events.append(("settled", _symbol_row(p["venue"], p["category"], p["symbol"])))
    return events


async def _spot_dust(s):
    """Спот: остаток ключа бота меньше минимума биржи (количество или номинал; комиссия в монете, продажа по шагу) —
    продать его нельзя никогда, а как открытая позиция он вечно держал бы группу стратегии (предел групп): списать
    поправкой с событием владельцу (монета остаётся на балансе и в spot_inventory). Только без незавершённых
    спот-ордеров бота по символу. → события."""
    events = []
    for venue, symbol in sorted({(k["venue"], k["symbol"]) for k in _rows("SELECT venue, symbol FROM keys WHERE "
                                                                           "category='spot'")}):
        vs = journal_venue_sym(venue, "spot", symbol)
        inst, _ = await venues.instrument(s, venue, "spot", vs) if vs else (None, "")
        if inst is None:
            continue
        async with state_lock():
            if _rows(f"SELECT 1 FROM orders WHERE state IN ({_marks(UNSETTLED)}) AND venue=? AND category='spot' AND "
                     f"symbol=? LIMIT 1", (*UNSETTLED, venue, symbol)):
                continue
            con = _connect()
            now = time.time()
            with _tx(con):
                for k in con.execute("SELECT * FROM keys WHERE venue=? AND category='spot' AND symbol=?",
                                     (venue, symbol)).fetchall():
                    net = Decimal(k["net"])
                    if net <= 0:
                        continue
                    px = Decimal(k["cost"]) / net
                    if net >= inst.min_qty and net * px >= inst.min_notional:
                        continue
                    note = (f"остаток {venues.fmt(net)} меньше минимума биржи (количество {venues.fmt(inst.min_qty)}, "
                            f"номинал {venues.fmt(inst.min_notional)} USDT) — продать нельзя, списан из позиций бота")
                    con.execute("INSERT INTO adjust (ts, venue, category, symbol, strategy, grp, qty, note) VALUES "
                                "(?, ?, 'spot', ?, ?, ?, ?, ?)", (now, venue, symbol, k["strategy"], k["grp"],
                                                                  venues.fmt(-net), note))
                    con.execute("DELETE FROM keys WHERE venue=? AND category='spot' AND symbol=? AND strategy=? AND "
                                "grp=?", (venue, symbol, k["strategy"], k["grp"]))
                    ev = _emit_in(con, "spot_dust", "", "", f"{venue} {symbol} спот {k['strategy'] or '—'}/"
                                                            f"{k['grp'] or '—'}: {note} (монета остаётся на балансе)",
                                  dedup=f"dust|{venue}|{symbol}|{k['strategy']}|{k['grp']}|{now!r}")
                    if ev:
                        events.append((ev, _symbol_row(venue, "spot", symbol)))
    return events


async def watch(s, creds_for):
    """Периодическая сверка символов с позициями и ордерами бота (из reconcile): закрытие позиции биржей или владельцем
    (sync_flat + снятие стопов), чужое на символе (событие владельцу + снятие висящих ордеров бота на открытие и стопов,
    которые могли бы закрыть не долю бота), стопы ключей (_sync_stops), фандинг, уточнение закрытий биржей, пыль спота.
    Всё прошло — отметка свежести (без неё открытий нет)."""
    ok, events = True, []
    for venue, category, symbol in _watch_symbols():
        creds = creds_for(venue)
        if not creds:
            ok = False
            continue
        try:
            got, good = await _watch_symbol(s, venue, category, symbol, creds)
            events += [e for e in got if e[0]]
            ok = ok and good
        except Exception as e:   # noqa: BLE001 — сбой одного символа не останавливает остальные
            ok = False
            logger.warning("сверка символа %s %s: %s", venue, symbol, type(e).__name__)
            emit("watch_error", f"{venue} {symbol}: сверка символа не удалась: {type(e).__name__}",
                 dedup=f"watch|{venue}|{symbol}|{type(e).__name__}|{int(time.time() // 3600)}")
    try:
        events += await _refine_venue_closes(s, creds_for)
    except Exception as e:   # noqa: BLE001
        logger.warning("уточнение закрытий биржей: %s", type(e).__name__)
    try:
        events += await _spot_dust(s)
    except Exception as e:   # noqa: BLE001
        logger.warning("пыль спота: %s", type(e).__name__)
    if ok:
        _set_meta("watch_ok", time.time())
    return events


# --- плечо, режим маржи, стоп позиции: только если на символе нет чужого ---

def _perp_category(venue):
    return "linear" if venue == venues.BYBIT else "swap"


def own_positions(positions, path=None):
    """Позиции с биржи (venues.positions — все, и владельца) с пометкой, что из них бота (ownership.annotate): для
    сопровождения (risk.position_actions сокращает только owned и не больше bot_size; расхождение — тревога)."""
    books = {(p["venue"], p["symbol"]): bot_book(p["venue"], _perp_category(p["venue"]), p["symbol"], path)["net"]
             for p in positions or ()}
    return ownership.annotate(positions, books)


async def _symbol_check(s, venue, symbol, venue_sym, creds, market=False):
    """Снимок символа (позиция, ордера, исполнения, плечо, режим маржи) и сверка с журналом: → (снимок, книга бота,
    причина отказа или "")."""
    category = _perp_category(venue)
    await refresh_symbol(s, venue, category, symbol, creds)
    snap = await ownership.fetch(s, venue, category, symbol, venue_sym, creds, market=market,
                                 exec_since=_exec_since(venue, category, symbol))
    async with state_lock():
        book = bot_book(venue, category, symbol)
        why = ownership.unknown_why(snap, book)
        foreign = None if why else ownership.foreign(snap, book)
    if why:
        return snap, book, "позиции и ордера символа не прочитаны — ничего не меняем: " + why[:200]
    if foreign:
        return snap, book, f"{risk.FOREIGN}: " + "; ".join(foreign)[:300]
    if book["uncertain"]:
        return snap, book, "у бота по символу ордера с неясным исходом — сначала сверка"
    if book["net"] != 0 and not ownership.owned_whole(snap, book):
        return snap, book, "на бирже позиции нет, а у бота по журналу есть — сначала сверка"
    return snap, book, ""


def _settings_symbol(venue, symbol):
    """Символ биржи для настройки символа: из журнала (позиция бота), иначе — свежая проверка (как у открытия)."""
    category = _perp_category(venue)
    return journal_venue_sym(venue, category, symbol) or venues.venue_symbol(venue, category, symbol)


async def set_leverage(s, venue, symbol, leverage, creds, strategy="", mode=""):
    """Плечо символа (целое 1..3; иначе ValueError): только если по символу нет чужой позиции и чужих ордеров (плечо —
    на всю позицию символа), не выше потолка стратегии в режиме (strategy — стратегия будущего открытия; у позиций бота
    на символе — их стратегии), у позиции бота — запас до ликвидации при новом плече и стоп раньше новой ликвидации.
    Режим каждой стратегии — как у открытия: не выше .env и порогов gates (_gate_mode), у позиции бота — ещё и не выше
    режима её открытий (позиция minlot — потолки minlot); торговля выключена — самые строгие потолки (minlot).
    Bybit 110043 «не изменилось» — успех. → (вид, текст): ok / refused / rejected / ambiguous …"""
    try:
        venue_sym = _settings_symbol(venue, symbol)
    except ValueError as e:
        return "refused", str(e)
    method, path, params = venues.leverage_call(venue, venue_sym, leverage)   # ValueError — вне 1..3
    async with symbol_lock(venue, symbol):
        snap, book, why = await _symbol_check(s, venue, symbol, venue_sym, creds)
        if why:
            return "refused", why
        rows = [(st, "buy" if k["net"] > 0 else "sell", k["px"], k["stop"], k["mode"] or "minlot")
                for (st, _), k in book["keys"].items() if k["net"] != 0]
        if strategy:
            rows.append((strategy, None, None, None, ""))
        if not rows:
            return "refused", "плечо меняется только под стратегию (strategy) — её потолок плеча"
        live, _ = switch.can_open()
        base = _effective_mode(mode) if live else "paper"   # paper — потолки minlot (risk.limits)
        reasons = []
        for st, side, px, stop, key_mode in rows:
            m = base
            if st in risk.STRATEGIES:
                m = _stricter_mode(m, _gate_mode(st))
            m = _stricter_mode(m, key_mode) if key_mode else m
            reasons += risk.check_leverage_change([(st, side, px, stop)], m, leverage)
        if reasons:
            return "refused", "; ".join(reasons)
        try:
            status, j = await venues.call(s, venue, method, path, params, creds)
        except Exception as e:
            return "ambiguous", accounts.api_error_text(e)
        kind, _, _, msg = venues.leverage_outcome(venue, status, j, creds)
        return kind, msg


async def set_margin_isolated(s, symbol, creds):
    """BingX: изолированная маржа символа — только если по символу нет чужой позиции и чужих ордеров."""
    try:
        venue_sym = _settings_symbol(venues.BINGX, symbol)
    except ValueError as e:
        return "refused", str(e)
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


async def set_stop(s, venue, symbol, stop, creds, strategy="", group=""):
    """Стоп позиции ключа бота (стратегия, группа; без стратегии — единственный ключ символа): условный reduceOnly-ордер
    бота на размер ключа (Bybit и BingX). Только если вся позиция символа — бота; стоп — с правильной стороны от mark,
    раньше ликвидации, уже стоящий — только к входу (risk.check_stop_move); цена — по шагу к входу. Замка символа не
    ждёт (стоп — приоритет). Символ биржи — из журнала. Отказ — ("refused", причина), не исключение."""
    category = _perp_category(venue)
    try:
        stop = Decimal(str(stop))
        if not stop.is_finite() or stop <= 0:
            raise ValueError("стоп — положительное число")
        venue_sym = journal_venue_sym(venue, category, symbol)
    except (ValueError, ArithmeticError) as e:
        return "refused", str(e)
    if venue_sym is None:
        return "refused", "у бота нет своей позиции по символу — стоп ставить не на что"
    snap, book, why = await _symbol_check(s, venue, symbol, venue_sym, creds, market=True)
    if why:
        return "refused", why
    async with state_lock():
        book = bot_book(venue, category, symbol)
        why = _ownership_problem(snap, book)
        strategy, group, why2 = _pick_key(book, strategy, str(group or ""))
        why = why or why2
        if not why and (snap.mark is None or snap.instrument is None):
            why = "нет mark или шагов инструмента — стоп не проверить"
        if why:
            return "refused", why
        k = book["keys"].get((strategy, group))
        if not k or k["net"] == 0:
            return "refused", "у ключа нет позиции — стоп ставить не на что"
        others = [f"{st or '—'}/{g or '—'}" for (st, g), kk in book["keys"].items()
                  if (st, g) != (strategy, group) and (kk["net"] != 0 or kk["pending_open"] > 0)]
        if others:   # как risk.netting_conflicts: стоп одного ключа сработал бы и по доле другого (сальдирование)
            return "refused", ("на символе ещё позиции бота (" + ", ".join(others) + ") — стоп действует на всю "
                               "позицию символа, перекрывающиеся стопы запрещены")
        side = "buy" if k["net"] > 0 else "sell"
        tick = snap.instrument.tick
        stop = risk._ceil(stop, tick) if side == "buy" else risk._floor(stop, tick)   # к входу
        pos = snap.position["rows"][0] if snap.position and snap.position["rows"] else {}
        reasons = risk.check_stop_move(side, k["px"], stop, k["stop"], snap.mark, pos.get("liq"), snap.leverage)
        if reasons:
            return "refused", "; ".join(reasons)
        con = _connect()
        with _tx(con):
            kid = (venue, category, symbol, strategy, group)
            kk = _load_key(con, kid)
            if kk["net"] != k["net"]:
                return "refused", "позиция ключа изменилась — повторите"
            kk["stop"] = stop
            _save_key(con, kid, kk)
    why = await _sync_stops(s, venue, category, symbol, strategy, group, creds)
    return ("ok", "") if not why else ("unknown", why)
