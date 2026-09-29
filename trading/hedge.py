"""Реальный хедж P2P-кругов владельца шортом перпа — этап 5 «🛡 Хедж» (решения владельца 29.09; docs/hedge-design.md
§5–§9, где решение ниже не сказало иначе).

Круг: владелец купил монету (BTC/ETH/TON) за рубли («✅ Сделал» → bot.mark_done) и держит её до продажи — курс монеты к
USDT за это время может уйти. Шорт бессрочного фьючерса той же монеты на тот же объём на время круга снимает этот риск.
Здесь — кнопки, учёт кругов и решения «когда»; ордер уходит ТОЛЬКО через journal.submit (ядро само ещё раз проверяет
выключатель, режим и пороги gates, ключ, лимиты, чужое на символе).

- offer — после «✅ Сделал»: карточка с планом и кнопками, только если реальный хедж возможен прямо сейчас: торговля
  включена (TRADING=1, режим не paper), проверенный ключ площадки, режим хеджа с учётом порогов gates — minlot / confirm
  (auto — тоже по кнопке: автомат-открытия здесь нет), монета в HEDGE_ASSETS, сумма круга не меньше
  HEDGE_MIN_AMOUNT_RUB (ETH — с 20 000 ₽), план simperp.choose на площадках HEDGE_VENUES (пока только Bybit),
  стоимость ≤ 0.6 × запаса на курс монеты (RISK_BUFFER). Иначе — молча (строка в лог и в /hedge): бумажный хедж
  (simperp, hedge_plans) идёт своим чередом.
- «✅ Открыть шорт» — кнопка одноразовая: nonce в базе забирается атомарно (UPDATE … WHERE status='offered' AND
  nonce=?); живёт OFFER_TTL с — старая не открывает, а считает новый план и присылает новую карточку с новым nonce. По
  нажатию всё заново: выключатель из .env, свежий план (не числа карточки), стоимость и коэффициент ≤ 1.05 (journal
  этот коэффициент не проверяет — проверяем здесь), изолированная маржа (режим счёта Bybit UTA меняет только владелец),
  плечо 2× (journal.set_leverage), затем journal.submit(purpose="open", strategy="hedge", group="cycle:<источник>:<id>",
  precheck — по кругу нет другого живого хеджа, позиции и ордера). Стопа на бирже нет (хеджу не нужен). Одно нажатие —
  одна отправка: не больше одного нового клиентского id; отказ, «биржа отклонила» и «неясно» бот не повторяет (§8).
- закрытие — «✅ Продал — закрыть хедж», «🆘 Покупка не состоялась» (тот же путь, срочно), «Закрыть» в /hedge и само
  через HEDGE_MAX_HOURS (страховка: после него курсовой риск круга снова на владельце — так и пишем). Ордер —
  reduceOnly рыночная покупка ровно позиции группы по журналу (journal.bot_book), не числа из карточки. Работает и при
  выключенной торговле; повторное нажатие второго ордера не шлёт. Закрытие исполнилось частично (PartiallyFilledCanceled)
  — хедж снова «открыт», остаток по журналу закрывается следующим ордером; закрытие с неясным исходом не повторяется.
  Срок HEDGE_MAX_HOURS для реального шорта — только 0 < ч ≤ 48, иначе 6 ч (без срока шорт не остаётся).
- tick — каждый цикл wiring (INTERVAL): устаревшие карточки, судьба «неясных» ордеров (10 мин — «реши на ПК»; ордера
  группы нет в журнале час — решаем по позиции группы: есть шорт — ведём как открытый, нет — failed), вход по
  журналу, отложенное закрытие, авто-закрытие (повтор после неудачи — не чаще AUTO_RETRY), позиция, закрытая не ботом
  (closed_by_venue: сверка ядра обнулила ключ группы; само событие ядра владельцу доставляет wiring.deliver — здесь его
  не читаем и не помечаем). Не бросает.
- offer_soon — так зовёт bot.mark_done: offer фоновой задачей, ответ кнопке «✅ Сделал» её не ждёт.
- hedge_command — /hedge: живые хеджи (вход, P&L, до ликвидации), режим и почему, кнопки «Закрыть» и «⛔ Стоп торговли».

Позиция и лимиты (решение 2): потолок позиции — TRADING_MAX_POSITION_USDT в .env. Круг 10 000 ₽ ≈ 110 USDT (BTC/TON), а
ETH хеджируется с 20 000 ₽ ≈ 220 USDT: потолок 120 пропустит только круги до ~10 000 ₽ — хедж ETH будет без
предложения (план больше потолка). В minlot шорт урезается до потолка minlot (проверочный режим, коэффициент < 1).

База — data/hedge_circles.db (свой sqlite: ни trades.db, ни paper.db, ни журнал ядра).
"""
import asyncio
import contextlib
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import deque
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

import perp
import simperp
from trading import journal, keys, risk, switch, venues

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "hedge_circles.db")
STRATEGY = "hedge"
OFFER_TTL = 60                      # сек жизни кнопки «✅ Открыть шорт»
UNKNOWN_LONG = journal.UNKNOWN_LONG  # 600 с: ордер «неясен» дольше — «реши на ПК»
STALE_SENDING = 300                 # сек: «открывается/закрывается» дольше без ордера — перезапуск посреди отправки
AUTO_RETRY = 600                    # сек между попытками авто-закрытия, если закрыть не вышло
ORPHAN_AFTER = 3600                 # сек: «неясно», а ордера группы в журнале нет — дольше решаем по позиции группы
MAX_HOURS_DEFAULT = 6.0             # HEDGE_MAX_HOURS для реального шорта: вне 0 < ч ≤ MAX_HOURS_CAP — это значение
MAX_HOURS_CAP = 48.0
LEVERAGE = 2                        # решение 4: изолированная маржа, плечо 2×, без стопа на бирже
MAX_RATIO = risk.HARD["hedge_qty_ratio"]   # шорт ≤ монета круга × 1.05 — journal.submit это не проверяет
COST_SHARE = 0.6                    # открываем, только если стоимость ≤ 0.6 × запаса на курс монеты
DB_TIMEOUT = 2
TOPIC = "journal"                   # сообщения цикла — в «📒 Журнал», как у ядра
SUPPORTED = {venues.BYBIT: "Bybit"}  # площадка ядра → имя в perp/simperp (решение 5: пока только Bybit)
CATEGORY = {venues.BYBIT: "linear"}
STATUSES = ("offered", "expired", "skipped", "opening", "open", "unknown", "refused", "rejected", "closing", "closed",
            "closed_by_venue", "failed")
LIVE = ("opening", "open", "unknown", "closing")   # по кругу может быть ордер или позиция
NO_POSITION = ("offered", "expired", "skipped", "refused", "rejected", "failed")
CLOSED = ("closed", "closed_by_venue")
CLOSE_CODES = {"s": "sold", "f": "purchase_failed", "m": "manual_hedge_cmd"}   # кнопки закрытия: код → причина
REASONS = {"sold": "монета продана", "purchase_failed": "покупка не состоялась", "auto_timeout": "по сроку",
           "manual_hedge_cmd": "из /hedge"}
ISOLATED_HINT = ("переключи счёт Bybit на «Isolated margin» сам (Деривативы → режим маржи счёта): бот режим маржи "
                 "не меняет, под кросс-маржой хедж без стопа ядро не открывает")
RISK_BACK = "⚠️ Курсовой риск круга снова на тебе: монета дальше без хеджа."
STATUS_TEXT = {"offered": "предложен", "expired": "кнопка устарела", "skipped": "без хеджа", "opening": "открывается",
               "open": "открыт", "unknown": "ордер не подтверждён", "refused": "не открыт", "rejected": "биржа отказала",
               "closing": "закрывается", "closed": "закрыт", "closed_by_venue": "закрыт биржей или вручную",
               "failed": "не открыт"}
_silent = deque(maxlen=5)   # (время, группа, причина) — почему последние круги прошли без предложения (для /hedge)
_busy = {}                  # id хеджа → "open" | "close": сейчас его открывает или закрывает этот процесс (tick мимо)
_offers = set()             # фоновые offer_soon (ссылки держим, пока задача не кончилась)
_warned = set()             # предупреждения о настройках, уже записанные в лог
_COLUMNS = {
    "source": "TEXT DEFAULT ''", "circle_id": "TEXT DEFAULT ''", "asset": "TEXT DEFAULT ''",
    "hedge_ref_qty": "TEXT DEFAULT ''", "amount_rub": "REAL DEFAULT 0", "ref": "REAL DEFAULT 0",
    "risk_buffer_pct": "REAL DEFAULT 0", "mode": "TEXT DEFAULT ''", "plan": "TEXT DEFAULT '{}'",
    "status": "TEXT DEFAULT 'offered'", "nonce": "TEXT DEFAULT ''", "venue": "TEXT DEFAULT ''",
    "symbol": "TEXT DEFAULT ''", "qty": "TEXT DEFAULT ''",
    "created_ts": "REAL", "offered_ts": "REAL", "pressed_ts": "REAL", "sent_ts": "REAL", "filled_ts": "REAL",
    "closing_ts": "REAL", "closed_ts": "REAL", "unknown_ts": "REAL", "auto_ts": "REAL", "updated_ts": "REAL",
    "open_cid": "TEXT DEFAULT ''", "close_cid": "TEXT DEFAULT ''", "entry_price": "TEXT DEFAULT ''",
    "exit_price": "TEXT DEFAULT ''", "filled_qty": "TEXT DEFAULT ''", "fees_usdt": "TEXT DEFAULT ''",
    "pnl_usdt": "TEXT DEFAULT ''", "pnl_pct": "REAL", "refusal_where": "TEXT DEFAULT ''", "refusal": "TEXT DEFAULT ''",
    "close_reason": "TEXT DEFAULT ''", "close_req": "TEXT DEFAULT ''", "asked_pc": "INTEGER DEFAULT 0",
    "notes": "TEXT DEFAULT ''"}


def _wiring():
    """trading.wiring импортирует этот модуль — обратно только внутри функций (reread_switch, creds_for, link_of)."""
    from trading import wiring
    return wiring


def _esc(v):
    return html.escape(str(v))


def _dec(v):
    """Decimal из числа/строки (float — через str: 0.00102 → Decimal('0.00102')) или None."""
    if v is None or isinstance(v, bool) or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _floor(v, step):
    return (v / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _fmt(v, nd=None):
    if v is None or v == "":
        return "—"
    d = _dec(v)
    if d is None:
        return _esc(v)
    if nd is not None:
        return f"{d:.{nd}f}"
    return venues.fmt(d)


def group_label(source, circle_id):
    """Группа ключа журнала ядра: один круг — своя группа (risk считает × 1.05 по каждому кругу отдельно)."""
    return f"cycle:{source}:{circle_id}"


# --- база ---

def migrate(con):
    """Таблица и колонки поздних версий — CREATE TABLE IF NOT EXISTS + ADD COLUMN недостающих (повторять можно)."""
    con.execute("CREATE TABLE IF NOT EXISTS hedges (id INTEGER PRIMARY KEY AUTOINCREMENT, grp TEXT UNIQUE NOT NULL)")
    have = {r[1] for r in con.execute("PRAGMA table_info(hedges)")}
    for col, decl in _COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE hedges ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS hedges_status ON hedges (status)")


@contextlib.contextmanager
def _db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=DB_TIMEOUT, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        migrate(con)
        yield con
    finally:
        con.close()


def _rows(sql, args=(), path=None):
    """Чтение базу не создаёт: её нет — пусто."""
    path = path or DB_PATH
    if not os.path.exists(path):
        return []
    with _db(path) as con:
        return [dict(r) for r in con.execute(sql, args)]


def get(hid, path=None):
    rows = _rows("SELECT * FROM hedges WHERE id=?", (int(hid),), path)
    return rows[0] if rows else None


def by_group(group, path=None):
    return _rows("SELECT * FROM hedges WHERE grp=?", (str(group),), path)


def rows(statuses=None, path=None, limit=None):
    """Строки хеджей (новые первыми), statuses — только эти."""
    sql, args = "SELECT * FROM hedges", ()
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        args = tuple(statuses)
    sql += " ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
    return _rows(sql, args, path)


def _set(hid, expect=None, path=None, **fields):
    """Обновить строку; expect — только если статус сейчас один из них (compare-and-set). → изменена ли."""
    fields["updated_ts"] = time.time()
    sql = f"UPDATE hedges SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?"
    args = [*fields.values(), int(hid)]
    if expect:
        sql += f" AND status IN ({','.join('?' * len(expect))})"
        args += list(expect)
    with _db(path) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            n = con.execute(sql, args).rowcount
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    return n == 1


def _insert(fields, path=None):
    """Новая строка; группа уже есть — None (один хедж на круг)."""
    now = time.time()
    fields = dict(fields, created_ts=now, updated_ts=now)
    with _db(path) as con:
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(f"INSERT INTO hedges ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                              list(fields.values()))
            con.execute("COMMIT")
        except sqlite3.IntegrityError:
            con.execute("ROLLBACK")
            return None
    return cur.lastrowid


def _claim(hid, nonce, now, fresh, path=None):
    """Одноразовая кнопка: одним UPDATE … WHERE nonce=? забрать nonce. fresh — живая кнопка (offered, не старше
    OFFER_TTL) → opening; иначе — устаревшая (offered/expired) → expired, её нажатие пришлёт новую карточку. Второе
    нажатие той же кнопки nonce уже не найдёт. → забрали ли."""
    sql = "UPDATE hedges SET status=?, nonce='', pressed_ts=?, updated_ts=? WHERE id=? AND nonce=? AND nonce!=''"
    if fresh:
        sql += " AND status='offered' AND offered_ts>=?"
        args = ("opening", now, now, int(hid), str(nonce), now - OFFER_TTL)
    else:
        sql += " AND status IN ('offered', 'expired')"
        args = ("expired", now, now, int(hid), str(nonce))
    with _db(path) as con:
        con.execute("BEGIN IMMEDIATE")
        n = con.execute(sql, args).rowcount
        con.execute("COMMIT")
    return n == 1


# --- настройки ---

def _warn_once(text):
    """Предупреждение о настройке — один раз за процесс (settings() зовётся каждый цикл)."""
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def _parse_venues(raw):
    out = []
    for part in str(raw or "").split(","):
        v = part.strip().lower()
        if not v:
            continue
        if v in SUPPORTED and v not in out:
            out.append(v)
        else:
            _warn_once(f"HEDGE_VENUES: {v!r} не поддерживается (пока только bybit) — пропускаю")
    return out


def _parse_min_amount(raw):
    """«ETH:20000,BTC:5000» → {монета: ₽}. Запись с монетой, но без «:» или с не числом («ETH», «ETH=20000»,
    «ETH:abc») — эту монету не хеджируем вовсе (inf): при сомнении не открываем. Запись без монеты — пропуск."""
    out = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        head, sep, val = part.partition(":")
        m = re.match(r"[A-Za-z0-9]+", head.strip())
        if m is None:
            _warn_once(f"HEDGE_MIN_AMOUNT_RUB: {part!r} — нет монеты (нужно МОНЕТА:сумма), пропускаю")
            continue
        coin = m.group(0).upper()
        try:
            if not sep or m.group(0) != head.strip():
                raise ValueError(part)
            v = float(val.strip().replace(" ", ""))
            if v != v or v < 0:
                raise ValueError(val)
        except ValueError:
            _warn_once(f"HEDGE_MIN_AMOUNT_RUB: {part!r} — не МОНЕТА:сумма, {coin} не хеджируем")
            v = float("inf")
        out[coin] = max(v, out.get(coin, 0.0))
    return out


def _max_hours(raw):
    """Срок реального шорта, ч: только 0 < h ≤ MAX_HOURS_CAP; 0, минус, NaN, мусор, больше — MAX_HOURS_DEFAULT с
    предупреждением (реальный шорт без срока — ставка против курса без присмотра)."""
    raw = str(raw if raw is not None else "").strip().replace(",", ".")
    if not raw:
        return MAX_HOURS_DEFAULT
    try:
        h = float(raw)
    except ValueError:
        h = float("nan")
    if h == h and 0 < h <= MAX_HOURS_CAP:
        return h
    _warn_once(f"HEDGE_MAX_HOURS={os.getenv('HEDGE_MAX_HOURS')!r} для реального хеджа не годится (нужно 0 < ч ≤ "
               f"{MAX_HOURS_CAP:g}) — беру {MAX_HOURS_DEFAULT:g} ч")
    return MAX_HOURS_DEFAULT


def settings():
    """HEDGE_VENUES (bybit), HEDGE_MIN_AMOUNT_RUB (ETH:20000), коэффициент min(HEDGE_RATIO, 1.05) (мусор, NaN, минус —
    уже в simperp: по умолчанию / 0 → хеджа нет), полоса, срок HEDGE_MAX_HOURS (для реального — только 0 < ч ≤ 48,
    иначе 6), монеты HEDGE_ASSETS — из simperp.settings (одни настройки для бумаги и реального хеджа)."""
    st = simperp.settings()
    ratio = st["ratio"] if st["ratio"] == st["ratio"] else 0.0
    return {"venues": _parse_venues(os.getenv("HEDGE_VENUES", "bybit")),
            "min_amount": _parse_min_amount(os.getenv("HEDGE_MIN_AMOUNT_RUB", "ETH:20000")),
            "ratio": max(0.0, min(ratio, float(MAX_RATIO))), "band": st["band"],
            "max_hours": _max_hours(os.getenv("HEDGE_MAX_HOURS", str(MAX_HOURS_DEFAULT))),
            "hold_min": st["hold_min"], "residual": st["residual"], "assets": st["assets"],
            "cost_share": COST_SHARE, "leverage": LEVERAGE, "offer_ttl": OFFER_TTL}


def gate_mode(strategy=STRATEGY):
    """Самый рискованный режим, который разрешают пороги gates для стратегии. Публичная обёртка над journal._gate_mode
    (в ядре публичной нет): тот же расчёт, что journal.submit делает перед открытием, — только из локальных файлов
    владельца в data/ и журнала, без словарей вызывающего."""
    return journal._gate_mode(strategy)


def _creds(venue):
    return _wiring().creds_for(venue)


def live_mode():
    """(причина или "", режим): можно ли прямо сейчас реально хеджировать. Режим — меньший из .env и порогов gates
    (switch.effective_mode); годится minlot / confirm / auto (auto — тоже по кнопке)."""
    ok, why = switch.can_open()
    if not ok:
        return why, None
    vs = settings()["venues"]
    if not vs:
        return "HEDGE_VENUES: нет поддерживаемых площадок (пока только bybit)", None
    if not any(_creds(v) for v in vs):
        return "нет проверенного торгового ключа " + ", ".join(SUPPORTED[v] for v in vs), None
    try:
        gm = gate_mode()
    except Exception as e:   # noqa: BLE001 — пороги не прочитать: не открываем
        return f"пороги gates не прочитаны ({type(e).__name__})", None
    eff = switch.effective_mode(gm)
    if eff not in risk.LIVE_MODES:
        return f"режим хеджа {eff}: пороги gates для хеджа разрешают только {gm}", None
    return "", eff


def _buffer(asset, fallback):
    """Запас на курс монеты, % (RISK_BUFFER, как в плане круга); не разобрать — запас круга."""
    try:
        b = simperp.risk_buffers().get(asset)
    except (ValueError, TypeError):
        b = None
    return b if b is not None and b > 0 else (fallback or 0.0)


def build_plan(asset, coin_qty, amount_rub, ref, risk_pct, mode, now=None):
    """План реального шорта: (план или None, причина). База — simperp.choose по площадкам HEDGE_VENUES (свежая
    котировка, лот, минимумы, глубина стакана, ожидаемая стоимость); объём — монета круга × коэффициент, ВНИЗ до шага
    лота (perp.floor_lot) и никогда не больше монеты × 1.05; в minlot — не больше потолка minlot; в confirm/auto —
    не больше потолка позиции (TRADING_MAX_POSITION_USDT) и коэффициент в полосе HEDGE_RATIO_BAND; стоимость по этому
    объёму (simperp.estimate) ≤ 0.6 × запаса на курс монеты."""
    st = settings()
    asset = str(asset or "").upper()
    names = [SUPPORTED[v] for v in st["venues"]]
    if not names:
        return None, "HEDGE_VENUES: нет поддерживаемых площадок (пока только bybit)"
    if not coin_qty or coin_qty <= 0 or not amount_rub or amount_rub <= 0:
        return None, "нет объёма монеты или суммы круга"
    base, note = simperp.choose(asset, coin_qty, amount_rub, ref, amount_rub / coin_qty, risk=risk_pct, now=now,
                                venues=names)
    if base is None:
        return None, note or "монета не хеджируется (HEDGE_ASSETS) или котировок перпа нет (PAPER_HEDGE/PERPS)"
    venue = next(v for v, n in SUPPORTED.items() if n == base["venue"])
    symbol = f"{asset}USDT"
    if symbol not in venues.SYMBOLS:
        return None, f"{symbol} — не символ ядра"
    q = perp.quote(base["venue"], base["symbol"], now)
    ref_qty, lot, min_qty = _dec(coin_qty), _dec(q.lot) if q else None, _dec(q.min_qty) if q else None
    if q is None or not ref_qty or not lot or lot <= 0:
        return None, f"{base['venue']}: нет свежей котировки или шага лота"
    qty = _floor(_dec(perp.floor_lot(coin_qty * st["ratio"], q.lot, q.min_qty)) or Decimal(0), lot)
    mark = _dec(q.mark) or _dec(q.mid)
    if not mark or mark <= 0:
        return None, f"{base['venue']}: нет цены"
    lim = risk.limits(STRATEGY, mode)["position_usdt"]
    capped = False
    if mode not in ("confirm", "auto") and qty * mark > lim:   # minlot: проверочный шорт не больше потолка minlot
        qty, capped = _floor(lim / mark, lot), True
    if qty <= 0 or qty < (min_qty or 0):
        return None, f"{base['venue']}: объём меньше минимального лота {venues.fmt(lot)} {asset}"
    if qty > ref_qty * MAX_RATIO:   # округление вниз — не бывает; проверка на случай правки выше
        return None, f"шорт {venues.fmt(qty)} больше монеты круга × {MAX_RATIO}"
    ratio = qty / ref_qty
    if not capped and abs(float(ratio) - st["ratio"]) > st["band"] + 1e-9:
        return None, f"{base['venue']}: лот {venues.fmt(lot)} {asset} — коэффициент {float(ratio):.2f} вне полосы"
    notional = qty * mark
    if notional < (_dec(q.min_notional) or 0):
        return None, f"{base['venue']}: шорт ≈ {notional:.2f} USDT меньше минимума биржи"
    if notional > lim:
        return None, (f"шорт ≈ {notional:.0f} USDT больше потолка позиции {venues.fmt(lim)} USDT "
                      f"(TRADING_MAX_POSITION_USDT в .env; ETH от 20 000 ₽ ≈ 220 USDT)")
    e = simperp.estimate(q, float(qty), st["hold_min"] / 60, now)
    if e is None:
        return None, f"{base['venue']}: глубины стакана не хватает"
    rub = ref if ref and ref > 0 else base["ref"]
    cost_pct = e["cost"] * rub / amount_rub * 100
    buf = _buffer(asset, risk_pct)
    if buf <= 0:
        return None, f"запас на курс {asset} не задан (RISK_BUFFER)"
    limit_pct = COST_SHARE * buf
    if cost_pct > limit_pct + 1e-12:
        return None, (f"ожидаемая стоимость хеджа {cost_pct:.2f}% дороже {COST_SHARE:g} × запаса на курс "
                      f"{buf:g}% = {limit_pct:.2f}%")
    return {"venue": venue, "venue_name": base["venue"], "symbol": symbol, "perp_symbol": base["symbol"],
            "qty": venues.fmt(qty), "ratio": round(float(ratio), 4), "mark": float(mark), "mid": q.mid,
            "notional": round(float(notional), 4), "cost_pct": round(cost_pct, 4), "cost_usdt": round(e["cost"], 6),
            "funding": round(e["funding"], 6), "residual_pct": round(st["residual"] + abs(1 - float(ratio)) * risk_pct, 4),
            "buffer_pct": buf, "limit_pct": round(limit_pct, 4), "quote_age": round(q.age(now), 1), "mode": mode,
            "capped": capped, "alt": base.get("alt") or {}, "ts": time.time() if now is None else now}, ""


# --- сообщения ---

async def _say(bot, text, kb=None, thread=None):
    """Сообщение владельцу: ответ на кнопку — в её топик (thread), остальное — в «📒 Журнал». Сбой Telegram — лог."""
    try:
        if thread:
            return await bot.send(text, markup=kb, thread=thread)
        return await bot.send(text, markup=kb, topic=TOPIC)
    except Exception as e:   # noqa: BLE001 — сеть/Telegram: учёт хеджа от этого не зависит
        logger.warning("hedge message: %s", type(e).__name__)
        return None


def _quiet(group, why):
    logger.info("hedge: %s без предложения: %s", group, why)
    _silent.append((time.time(), group, str(why)[:300]))
    return None


def close_kb(hid):
    return {"inline_keyboard": [[{"text": "✅ Продал — закрыть хедж", "callback_data": f"trd_hedge_close:{hid}"}],
                                [{"text": "🆘 Покупка не состоялась — закрыть хедж срочно",
                                  "callback_data": f"trd_hedge_close:{hid}:f"}]]}


def offer_kb(hid, nonce):
    return {"inline_keyboard": [[{"text": "✅ Открыть шорт", "callback_data": f"trd_hedge_open:{hid}:{nonce}"},
                                 {"text": "✖️ Без хеджа", "callback_data": f"trd_hedge_skip:{hid}"}]]}


def _mode_text(mode):
    return {"minlot": "минимальный лот, по кнопке", "confirm": "по кнопке", "auto": "по кнопке (автомата нет)"}.get(
        mode, mode)


def _name(row):
    return f"#{_esc(row['circle_id'])}"


def card_text(row, plan):
    asset, st = row["asset"], settings()
    minlot = " (урезан до потолка minlot — проверочный режим)" if plan.get("capped") else ""
    rub = f"{row['amount_rub']:,.0f}".replace(",", " ")
    return (f"🛡 <b>Хедж круга {_name(row)}</b> ({_esc(asset)}, {rub} ₽)\n"
            f"Шорт {plan['qty']} {_esc(asset)} на {_esc(plan['venue_name'])} ({_esc(plan['symbol'])}, рынок) ≈ "
            f"{plan['notional']:.0f} USDT, коэф. {plan['ratio']:.2f}{minlot}\n"
            f"ожидаемая стоимость {plan['cost_pct']:.2f}% круга (порог {plan['limit_pct']:.2f}% = {COST_SHARE:g} × "
            f"запас на курс {plan['buffer_pct']:g}%), фандинг {plan['funding']:+.2f} USDT\n"
            f"плечо {LEVERAGE}×, маржа изолированная, стопа на бирже нет · режим {_esc(plan['mode'])} "
            f"({_mode_text(plan['mode'])})\n"
            f"Кнопка действует {OFFER_TTL} с, один раз. Закрытие — «✅ Продал — закрыть хедж» или само через "
            f"{st['max_hours']:g} ч (тогда курсовой риск снова на тебе).")


def _open_text(row, qty, entry, cid):
    st = settings()
    return (f"✅ <b>Хедж круга {_name(row)} открыт</b>: шорт {_fmt(qty)} {_esc(row['asset'])} на "
            f"{_esc(SUPPORTED.get(row['venue'], row['venue']))} ({_esc(row['symbol'])}), плечо {LEVERAGE}×, "
            f"изолированная маржа\n"
            + (f"вход {_fmt(entry)}" if entry else "вход уточнит сверка")
            + (f" · ордер <code>{_esc(cid)}</code>" if cid else "") + "\n"
            f"Продал монету — «✅ Продал — закрыть хедж». Покупка сорвалась — «🆘 Покупка не состоялась» (шорт без "
            f"монеты — ставка против курса). Само закроется через {st['max_hours']:g} ч.")


# --- предложение ---

async def offer(bot, source, circle_id, asset, coin_qty, amount_rub, ref, risk_pct, now=None):
    """После «✅ Сделал»: карточка реального хеджа круга с кнопками — только если хедж возможен прямо сейчас (см.
    модуль). Иначе молча (лог). Никогда не бросает (bot.mark_done всё равно оборачивает). → id строки или None."""
    try:
        return await _offer(bot, str(source), str(circle_id), str(asset or "").upper(), float(coin_qty or 0),
                            float(amount_rub or 0), float(ref or 0), float(risk_pct or 0), now)
    except Exception as e:   # noqa: BLE001 — круг и журнал сделок идут дальше без хеджа
        logger.error("hedge offer: %s: %s", type(e).__name__, e)
        return None


def offer_soon(bot, source, circle_id, asset, coin_qty, amount_rub, ref, risk_pct):
    """offer фоновой задачей (так зовёт bot.mark_done): ответ кнопке «✅ Сделал» и журнал сделок не ждут чтения ключей
    и Telegram. Ссылка на задачу держится в _offers до её конца; сбой — только в лог. → задача."""
    task = asyncio.ensure_future(offer(bot, source, circle_id, asset, coin_qty, amount_rub, ref, risk_pct))
    _offers.add(task)
    task.add_done_callback(_offer_done)
    return task


def _offer_done(task):
    _offers.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("hedge offer task: %s: %s", type(task.exception()).__name__, task.exception())


async def _offer(bot, source, circle_id, asset, coin_qty, amount_rub, ref, risk_pct, now):
    group = group_label(source, circle_id)
    why, mode = live_mode()
    if why:
        return _quiet(group, why)
    st = settings()
    if asset not in st["assets"]:
        return _quiet(group, f"{asset} не в HEDGE_ASSETS")
    minimum = st["min_amount"].get(asset)
    if minimum is not None and amount_rub < minimum:
        return _quiet(group, f"круг {amount_rub:.0f} ₽ меньше HEDGE_MIN_AMOUNT_RUB для {asset} ({minimum:.0f} ₽)")
    plan, why = build_plan(asset, coin_qty, amount_rub, ref, risk_pct, mode, now)
    if plan is None:
        return _quiet(group, why)
    nonce = secrets.token_hex(4)
    now = time.time() if now is None else now
    hid = _insert({"grp": group, "source": source, "circle_id": circle_id, "asset": asset,
                   "hedge_ref_qty": venues.fmt(_dec(coin_qty)), "amount_rub": amount_rub, "ref": ref,
                   "risk_buffer_pct": _buffer(asset, risk_pct), "mode": mode, "plan": json.dumps(plan),
                   "status": "offered", "nonce": nonce, "venue": plan["venue"], "symbol": plan["symbol"],
                   "qty": plan["qty"], "offered_ts": now})
    if hid is None:
        return _quiet(group, "по кругу хедж уже предлагался")
    r = await _say(bot, card_text(get(hid), plan), offer_kb(hid, nonce))
    if not (isinstance(r, dict) and r.get("ok")):
        _set(hid, expect=("offered",), status="expired", nonce="", notes="карточка не ушла в Telegram")
        return None
    logger.info("hedge: %s предложен шорт %s %s", group, plan["qty"], plan["symbol"])
    return hid


async def _reoffer(bot, hid, now, thread=None):
    """Старая кнопка: свежий план и НОВАЯ карточка с новым nonce (план старой не используется)."""
    row = get(hid)
    st = settings()
    if now - (row["created_ts"] or 0) > st["max_hours"] * 3600:
        await _say(bot, f"⌛ Кнопка хеджа круга {_name(row)} устарела, круг старше {st['max_hours']:g} ч — "
                        "новый хедж не предлагаю.", thread=thread)
        return None
    why, mode = live_mode()
    plan = None
    if not why:
        plan, why = build_plan(row["asset"], float(row["hedge_ref_qty"]), row["amount_rub"], row["ref"],
                               row["risk_buffer_pct"], mode, now)
    if plan is None:
        _set(hid, notes=f"новый план не собран: {why}"[:500])
        await _say(bot, f"⌛ Кнопка хеджа круга {_name(row)} устарела; новый хедж не предлагаю: "
                        f"{_esc(why)}", thread=thread)
        return None
    nonce = secrets.token_hex(4)
    if not _set(hid, expect=("expired",), status="offered", nonce=nonce, offered_ts=now, plan=json.dumps(plan),
                mode=mode, venue=plan["venue"], symbol=plan["symbol"], qty=plan["qty"]):
        return None
    r = await _say(bot, "🔁 План пересчитан по свежей котировке.\n" + card_text(get(hid), plan), offer_kb(hid, nonce),
                   thread=thread)
    if not (isinstance(r, dict) and r.get("ok")):
        _set(hid, expect=("offered",), status="expired", nonce="", notes="карточка не ушла в Telegram")
    return hid


def on_skip(hid):
    if _set(hid, expect=("offered", "expired"), status="skipped", nonce="", close_reason="skip"):
        return "без хеджа"
    return "уже решено"


async def on_open(bot, cq, hid, nonce, now=None):
    """«✅ Открыть шорт»: nonce забирается атомарно (второе нажатие — «уже решено»); кнопка старше OFFER_TTL — новая
    карточка со свежим планом. Живое нажатие — открытие фоновой задачей (ответ кнопке сразу). → текст ответа кнопке."""
    now = time.time() if now is None else now
    if get(hid) is None:
        return "хедж не найден"
    thread = bot.thread_for(None)
    if hid not in _busy and _claim(hid, nonce, now, fresh=True):
        _busy[hid] = "open"
        link = _wiring().link_of(bot)
        task = asyncio.ensure_future(_open(bot, hid, thread))
        link.tasks.add(task)
        task.add_done_callback(link.tasks.discard)
        return "открываю шорт"
    if _claim(hid, nonce, now, fresh=False):
        await _reoffer(bot, hid, now, thread)
        return "кнопка устарела — прислал новый план"
    return "уже решено — кнопка одноразовая"


# --- открытие ---

async def _refuse(bot, hid, where, why, thread=None):
    _set(hid, status="refused", refusal_where=where, refusal=str(why)[:600])
    row = get(hid)
    await _say(bot, f"✖️ Хедж круга {_name(row)} не открыт: {_esc(why)}", thread=thread)


def _margin_problem(reason):
    low = str(reason or "").lower()
    return "кросс-маржа" in low or "режим маржи" in low


async def _venue_symbol(s, venue, symbol):
    """Символ биржи: TON (переименован в GRAM) — только после свежей проверки публичных справочников."""
    cat = CATEGORY[venue]
    try:
        return venues.venue_symbol(venue, cat, symbol)
    except ValueError:
        if symbol not in venues.VERIFY:
            raise
    await venues.resolve_symbols(s, venue, cat)
    return venues.venue_symbol(venue, cat, symbol)


def _key(venue, symbol, group):
    """Ключ (hedge, группа) в журнале ядра: {net, pending_open, pending_reduce …} или None."""
    return journal.bot_book(venue, CATEGORY[venue], symbol)["keys"].get((STRATEGY, str(group)))


def _precheck(hid, group, venue, symbol):
    """precheck journal.submit (под замком ядра): строка всё ещё «открывается» этим нажатием, по кругу нет другого
    живого хеджа и у бота по группе нет ни позиции, ни ожидающего ордера. → причина отказа или ""."""
    row = get(hid)
    if row is None or row["status"] != "opening" or row["open_cid"]:
        return "хедж круга уже решён другим нажатием — второй ордер не отправляю"
    if any(r["id"] != row["id"] and r["status"] in LIVE for r in by_group(group)):
        return "по кругу уже есть живой хедж — второй не открываю"
    k = _key(venue, symbol, group)
    if k and (k["net"] != 0 or k["pending_open"] > 0):
        return "по кругу у бота уже есть позиция или ордер на открытие — второй не отправляю"
    return ""


async def _open(bot, hid, thread=None):
    try:
        await _open_inner(bot, hid, thread)
    except asyncio.CancelledError:
        raise
    except Exception as e:   # noqa: BLE001 — сбой бота, не ядра: исход решает журнал группы (tick)
        logger.error("hedge open #%s: %s: %s", hid, type(e).__name__, e)
        row = get(hid)
        if row is not None and row["status"] == "opening":
            if row["sent_ts"]:
                _set(hid, expect=("opening",), status="unknown", unknown_ts=time.time(),
                     notes=f"сбой отправки: {type(e).__name__}")
                await _say(bot, f"❓ Хедж круга {_name(row)}: сбой при отправке ({_esc(type(e).__name__)}) "
                                f"— исход выяснит сверка, повторно не отправляю.", thread=thread)
            else:
                _set(hid, expect=("opening",), status="failed", refusal_where="bot", refusal=type(e).__name__)
                await _say(bot, f"✖️ Хедж круга {_name(row)} не открыт: сбой бота "
                                f"({_esc(type(e).__name__)}), ордер не отправлялся.", thread=thread)
    finally:
        _busy.pop(hid, None)
    row = get(hid)
    if row is not None and row["status"] == "open" and row["close_req"]:   # «закрыть» нажали, пока шорт открывался
        await close(bot, hid, row["close_req"], thread)


async def _open_inner(bot, hid, thread):
    row = get(hid)
    w = _wiring()
    ok, why = w.reread_switch(w.link_of(bot))   # TRADING=0 в .env (launcher, владелец) — ничего не уходит
    if not ok:
        return await _refuse(bot, hid, "switch", why, thread)
    why, mode = live_mode()
    if why:
        return await _refuse(bot, hid, "mode", why, thread)
    plan, why = build_plan(row["asset"], float(row["hedge_ref_qty"]), row["amount_rub"], row["ref"],
                           row["risk_buffer_pct"], mode)   # свежая котировка, не числа карточки
    if plan is None:
        return await _refuse(bot, hid, "plan", why, thread)
    qty, ref_qty = Decimal(plan["qty"]), Decimal(row["hedge_ref_qty"])
    if qty <= 0 or qty > ref_qty * MAX_RATIO:
        return await _refuse(bot, hid, "ratio", f"шорт {plan['qty']} больше монеты круга × {MAX_RATIO}", thread)
    venue, symbol, group = plan["venue"], plan["symbol"], row["grp"]
    creds = _creds(venue)
    if not creds:
        return await _refuse(bot, hid, "keys", f"нет проверенного торгового ключа {SUPPORTED[venue]}", thread)
    try:
        venue_sym = await _venue_symbol(bot.s, venue, symbol)
    except ValueError as e:
        return await _refuse(bot, hid, "symbol", str(e), thread)
    margin, mwhy = await venues.margin_mode(bot.s, venue, venue_sym, creds)
    if margin != "isolated":
        return await _refuse(bot, hid, "margin", f"режим маржи счёта {SUPPORTED[venue]} — "
                                                 f"{margin or 'не прочитан: ' + str(mwhy)}; {ISOLATED_HINT}", thread)
    kind, msg = await journal.set_leverage(bot.s, venue, symbol, LEVERAGE, creds, strategy=STRATEGY, mode=mode)
    if kind != "ok":
        return await _refuse(bot, hid, "leverage", f"плечо {LEVERAGE}× не выставлено ({kind}): {msg}", thread)
    order = venues.Order(venue, CATEGORY[venue], symbol, "sell", "market", qty)
    if not _set(hid, expect=("opening",), sent_ts=time.time(), mode=mode, plan=json.dumps(plan), venue=venue,
                symbol=symbol, qty=plan["qty"]):
        return None   # строку за это время решили иначе — ордер не отправляем
    res = await journal.submit(bot.s, order, creds, purpose="open", strategy=STRATEGY, group=group, mode=switch.mode(),
                               precheck=lambda: _precheck(hid, group, venue, symbol))
    await _after_open(bot, hid, res, thread)


async def _after_open(bot, hid, res, thread):
    """Итог journal.submit → статус и одно сообщение (§8): отказ ядра и «биржа отклонила» не повторяем; «неясно» —
    ничего не шлём заново, ждём сверку."""
    state, jrow, reason = res.get("state"), res.get("row") or {}, res.get("reason") or ""
    cid, now, row = jrow.get("client_id") or "", time.time(), get(hid)
    name = _name(row)
    if state == "refused":
        _set(hid, status="refused", refusal_where="submit", refusal=reason[:600])
        hint = f"\n{ISOLATED_HINT}" if _margin_problem(reason) else ""
        await _say(bot, f"✖️ Хедж круга {name} не открыт: {_esc(reason)}{_esc(hint)}", thread=thread)
    elif state == "rejected":
        _set(hid, status="rejected", open_cid=cid, refusal_where="venue", refusal=reason[:600])
        await _say(bot, f"↩️ Биржа отклонила шорт хеджа круга {name}: {_esc(reason)}. Повторять не буду — круг без "
                        "хеджа.", thread=thread)
    elif state == "unknown" or not cid:
        _set(hid, status="unknown", open_cid=cid, unknown_ts=now, notes=reason[:600])
        await _say(bot, f"❓ Ордер хеджа круга {name} не подтверждён биржей — проверяю (сверка каждые 30 с). Повторно "
                        f"не отправляю; через 10 мин без ответа попрошу решить на ПК.", thread=thread)
    elif state in ("open", "filled") or (res.get("filled") or 0) > 0:
        entry = _dec(jrow.get("avg_price"))
        filled = res.get("filled") or 0
        _set(hid, status="open", open_cid=cid, filled_ts=now, entry_price=venues.fmt(entry) if entry else "",
             filled_qty=venues.fmt(filled) if filled else "")
        # исполнено частично — в сообщении исполненное, а не заказанное
        await _say(bot, _open_text(get(hid), filled if filled else row["qty"], entry, cid), close_kb(hid),
                   thread=thread)
    else:   # закрыт биржей без исполнения
        _set(hid, status="failed", open_cid=cid, refusal_where="venue", refusal=f"ордер {state} без исполнения")
        await _say(bot, f"✖️ Шорт хеджа круга {name} не исполнился ({_esc(state)}) — круг без хеджа.", thread=thread)


# --- закрытие ---

def request_close(bot, hid, reason):
    """Кнопка закрытия: сразу ответ кнопке, само закрытие — фоновой задачей. Уже закрыт / уже закрывается — ничего не
    шлём (второе нажатие)."""
    row = get(hid)
    if row is None:
        return "хедж не найден"
    if _busy.get(hid) == "close":
        return "закрытие уже идёт"
    if row["status"] in CLOSED:
        return "хедж уже закрыт"
    if row["status"] == "closing" and _close_left(row) is None:   # ордер закрытия ещё в пути или под вопросом
        return "закрытие уже отправлено — жду подтверждения"
    if row["status"] in NO_POSITION:
        if row["status"] in ("offered", "expired"):
            _set(hid, expect=("offered", "expired"), status="skipped", nonce="", close_reason=reason)
        return "шорта по кругу нет — закрывать нечего"
    if _busy.get(hid) == "open":   # закроем сразу после открытия (_open) или в tick
        _set(hid, expect=LIVE, close_req=reason)
        return "шорт ещё открывается — закрою, как только подтвердится"
    _busy[hid] = "close"
    link = _wiring().link_of(bot)
    task = asyncio.ensure_future(_close_guarded(bot, hid, reason, bot.thread_for(None)))
    link.tasks.add(task)
    task.add_done_callback(link.tasks.discard)
    return "закрываю хедж"


async def close(bot, hid, reason, thread=None):
    """Закрыть хедж круга (reason: sold / purchase_failed / auto_timeout / manual_hedge_cmd): reduceOnly рыночная
    покупка ровно позиции группы по журналу. Работает и при выключенной торговле. Повторный вызов второго ордера не
    шлёт. Никогда не бросает. → текст итога (для тестов и логов)."""
    if hid in _busy:
        return "уже идёт открытие или закрытие"
    _busy[hid] = "close"
    return await _close_guarded(bot, hid, reason, thread)


async def _close_guarded(bot, hid, reason, thread=None):
    """_close под отметкой _busy (её ставит вызывающий, здесь снимается). Сбой — лог и сообщение; статус не
    откатываем: «закрывается» без ордера tick сверит с журналом группы."""
    try:
        return await _close(bot, hid, reason, thread)
    except asyncio.CancelledError:
        raise
    except Exception as e:   # noqa: BLE001
        logger.error("hedge close #%s: %s: %s", hid, type(e).__name__, e)
        await _say(bot, f"⚠️ Хедж #{hid}: сбой закрытия ({_esc(type(e).__name__)}) — шорт, возможно, ещё открыт; "
                        f"сверка разберётся, проверь /hedge или кабинет Bybit.", thread=thread)
        return f"сбой закрытия: {type(e).__name__}"
    finally:
        _busy.pop(hid, None)


async def _close(bot, hid, reason, thread=None):
    row = get(hid)
    if row is None:
        return "хедж не найден"
    st, name = row["status"], _name(row)
    if st in CLOSED:
        return "хедж уже закрыт"
    if st == "closing":
        left = _close_left(row)
        if left is None:   # ордер закрытия в пути или исход неясен — второй не шлём
            return "закрытие уже отправлено"
        await _reopen(bot, row, *left, thread=thread)   # прошлое закрытие исполнилось не целиком — закрываем остаток
        row = get(hid)
        st = row["status"]
        if st != "open":
            return "уже решено"
    if st in NO_POSITION:
        if st in ("offered", "expired"):
            _set(hid, expect=("offered", "expired"), status="skipped", nonce="", close_reason=reason)
        return "шорта по кругу нет"
    venue, symbol, group = row["venue"], row["symbol"], row["grp"]
    auto = reason == "auto_timeout"
    warn = f"\n{RISK_BACK}" if auto else ""
    creds = _creds(venue)
    if not creds:
        _set(hid, close_req=reason)
        await _say(bot, f"⚠️ Закрыть хедж круга {name} не могу: нет проверенного торгового ключа "
                        f"{_esc(SUPPORTED.get(venue, venue))} — закрой шорт {_esc(row['qty'])} {_esc(symbol)} вручную "
                        f"в кабинете или проверь ключ на ПК (python scripts/trading_keys.py check).{warn}",
                   thread=thread)
        return "нет ключа"
    if st == "opening":
        _set(hid, close_req=reason)
        return "шорт ещё открывается — закрою, как только подтвердится"
    await journal.refresh_symbol(bot.s, venue, CATEGORY[venue], symbol, creds)   # свежее исполнение своего ордера
    k = _key(venue, symbol, group)
    net = k["net"] if k else Decimal(0)
    if net == 0:
        if (k and k["pending_open"] > 0) or st == "unknown":
            _set(hid, close_req=reason)
            await _say(bot, f"⏳ Шорт хеджа круга {name} ещё не подтверждён биржей — закрою сам, как только сверка "
                            f"увидит исполнение.", thread=thread)
            return "закрою после подтверждения"
        return await _gone(bot, hid, "позиции по кругу у бота нет (по журналу)", thread)
    if net > 0:
        await _say(bot, f"⚠️ Хедж круга {name}: у бота по группе лонг {venues.fmt(net)} — это не шорт хеджа, не "
                        f"трогаю. Проверь /trading.", thread=thread)
        return "не шорт"
    qty = -net - k["pending_reduce"]["buy"]
    if qty <= 0:
        return "закрытие уже отправлено"
    if not _set(hid, expect=("open", "unknown"), status="closing", closing_ts=time.time(), close_reason=reason,
                close_req="", close_cid="", unknown_ts=None, asked_pc=0):
        return "уже закрывается"
    order = venues.Order(venue, CATEGORY[venue], symbol, "buy", "market", qty, reduce_only=True)
    res = await journal.submit(bot.s, order, creds, purpose="close", strategy=STRATEGY, group=group)
    return await _after_close(bot, hid, res, reason, thread)


async def _gone(bot, hid, why, thread=None):
    """Позиции по кругу больше нет, и её закрыл не этот путь: исполнено было — closed_by_venue, не было — failed."""
    row = get(hid)
    oj = journal.get(row["open_cid"]) if row["open_cid"] else None
    was = (oj is not None and (_dec(oj.get("filled")) or 0) > 0) or bool(_dec(row["filled_qty"]))
    new = "closed_by_venue" if was else "failed"
    if not _set(hid, expect=("open", "unknown", "closing"), status=new, closed_ts=time.time(), notes=why[:500]):
        return "уже решено"
    if was:
        await _say(bot, f"⚠️ Хедж круга {_name(row)}: позицию закрыла биржа (ликвидация, ADL) или ты "
                        f"вручную — {_esc(why)}. {RISK_BACK} Итог — в событиях ядра («📒 Журнал»).", thread=thread)
    else:
        await _say(bot, f"✖️ Хедж круга {_name(row)}: шорт не исполнился — {_esc(why)}; круг без хеджа.",
                   thread=thread)
    return new


async def _after_close(bot, hid, res, reason, thread=None):
    state, jrow, why = res.get("state"), res.get("row") or {}, res.get("reason") or ""
    cid, row = jrow.get("client_id") or "", get(hid)
    name = _name(row)
    auto = reason == "auto_timeout"
    warn = f"\n{RISK_BACK}" if auto else ""
    if state == "refused":
        k = _key(row["venue"], row["symbol"], row["grp"])
        if not k or (k["net"] == 0 and k["pending_open"] == 0):
            return await _gone(bot, hid, why or "позиции по кругу у бота нет", thread)
        _set(hid, expect=("closing",), status="open", close_reason="", refusal_where="close", refusal=why[:600])
        await _say(bot, f"✖️ Закрытие хеджа круга {name} не отправлено: {_esc(why)}. Шорт открыт — закрой вручную в "
                        f"кабинете или нажми ещё раз.{warn}", close_kb(hid), thread=thread)
        return "refused"
    if state == "rejected":
        _set(hid, expect=("closing",), status="open", close_reason="", close_cid=cid, refusal_where="close",
             refusal=why[:600])
        await _say(bot, f"↩️ Биржа отклонила закрытие хеджа круга {name}: {_esc(why)}. Шорт открыт.{warn}",
                   close_kb(hid), thread=thread)
        return "rejected"
    _set(hid, close_cid=cid)
    if state == "unknown" or not cid:
        _set(hid, unknown_ts=time.time())
        await _say(bot, f"❓ Закрытие хеджа круга {name} не подтверждено биржей — проверяю, повторно не отправляю.{warn}",
                   thread=thread)
        return "unknown"
    done = await _settle_close(bot, hid, thread)
    if done:
        return done
    row = get(hid)
    left = _close_left(row)
    if left is not None:   # исполнено не целиком (PartiallyFilledCanceled): снова «открыт», остаток закроем
        await _reopen(bot, row, *left, thread=thread)
        return "partial"
    return state


def _close_left(row):
    """«Закрывается», а ордер закрытия в журнале уже завершён (исполнен целиком или частично, снят, отклонён) и
    позиция группы по журналу не ноль — закрытие не довело дело до конца (Bybit PartiallyFilledCanceled). → (сальдо
    группы, исполнено ордером закрытия) или None: ордер ещё в пути или его исход неясен — такой не трогаем и не
    повторяем."""
    if row is None or row["status"] != "closing" or not row["close_cid"]:
        return None
    cj = journal.get(row["close_cid"])
    if cj is None or cj["state"] in journal.UNSETTLED:
        return None
    k = _key(row["venue"], row["symbol"], row["grp"])
    if not k or k["net"] == 0 or k["pending_reduce"]["buy"] > 0:
        return None
    return k["net"], _dec(cj.get("filled")) or Decimal(0)


async def _reopen(bot, row, net, filled, thread=None):
    """Закрытие завершилось, а шорт группы остался: снова «открыт» (CAS с closing), ордер закрытия забыт. Исполнено
    частично — остаток закроем сами (close_req → tick; после неудачной попытки — не чаще AUTO_RETRY), объём — по
    сальдо журнала; не исполнилось вовсе — решает владелец кнопкой. Сообщение — одно (CAS). → переведён ли."""
    hid, name = row["id"], _name(row)
    reason = row["close_reason"] or "sold"
    if not _set(hid, expect=("closing",), status="open", close_cid="", close_req=reason if filled > 0 else "",
                notes=f"закрытие {row['close_cid']} исполнено на {venues.fmt(filled)}, осталось {venues.fmt(-net)}"):
        return False
    if filled > 0:
        text = (f"⚠️ Закрытие хеджа круга {name} исполнилось частично: выкуплено {_fmt(filled)}, остаток шорта "
                f"{_fmt(-net)} {_esc(row['asset'])} — закрываю остаток (или «Закрыть»).")
    else:
        text = f"✖️ Закрытие хеджа круга {name} не исполнилось — шорт {_fmt(-net)} {_esc(row['asset'])} открыт."
    await _say(bot, text, close_kb(hid), thread=thread)
    return True


def _row_fee(r):
    f = _dec((r or {}).get("fee"))
    return abs(f) if f is not None else None


async def _settle_close(bot, hid, thread=None):
    """Закрытие исполнено и ключ группы в журнале пуст — итог: выход, комиссии, P&L в USDT и в % круга. Ещё не пуст
    (исполнение не пришло) — ждём tick. → "closed" или None."""
    row = get(hid)
    k = _key(row["venue"], row["symbol"], row["grp"])
    if k and (k["net"] != 0 or k["pending_reduce"]["buy"] > 0):
        return None
    oj = journal.get(row["open_cid"]) if row["open_cid"] else None
    cj = journal.get(row["close_cid"]) if row["close_cid"] else None
    # все исполненные закрытия группы (частичное + остаток), не только последнее
    fills = [r for r in journal.history(1000) if r.get("grp") == row["grp"] and r.get("strategy") == STRATEGY
             and r.get("purpose") == "close" and (_dec(r.get("filled")) or 0) > 0]
    if cj is not None and cj["client_id"] not in {r["client_id"] for r in fills} and (_dec(cj.get("filled")) or 0) > 0:
        fills.append(cj)
    entry = _dec((oj or {}).get("avg_price")) or _dec(row["entry_price"])
    qty = sum((_dec(r["filled"]) for r in fills), Decimal(0))
    exitp = None
    if fills and all(_dec(r.get("avg_price")) for r in fills):
        exitp = round(sum((_dec(r["filled"]) * _dec(r["avg_price"]) for r in fills), Decimal(0)) / qty, 8)
    qty = qty or _dec(row["filled_qty"]) or _dec(row["qty"]) or Decimal(0)
    fees = [f for f in [_row_fee(oj)] + [_row_fee(r) for r in fills] if f is not None]
    fee = sum(fees, Decimal(0))
    pnl = (entry - exitp) * qty - fee if entry and exitp else None
    pct = float(pnl) * row["ref"] / row["amount_rub"] * 100 if pnl is not None and row["ref"] and row["amount_rub"] \
        else None
    reason = row["close_reason"] or "sold"
    if not _set(hid, expect=("closing", "open", "unknown"), status="closed", closed_ts=time.time(),
                exit_price=venues.fmt(exitp) if exitp else "", entry_price=venues.fmt(entry) if entry else "",
                fees_usdt=venues.fmt(fee), pnl_usdt=f"{pnl:.4f}" if pnl is not None else "", pnl_pct=pct):
        return None
    if oj is not None and oj.get("state") == "filled":
        try:
            journal.close_out(oj["client_id"])   # позицию от этого ордера закрыли (§6)
        except ValueError as e:
            logger.warning("hedge close_out %s: %s", oj["client_id"], e)
    head = (f"⏰ <b>Хедж круга {_name(row)} закрыт автоматически</b>: прошло {settings()['max_hours']:g} ч"
            if reason == "auto_timeout" else
            f"🔒 <b>Хедж круга {_name(row)} закрыт</b> ({_esc(REASONS.get(reason, reason))})")
    res = (f"{pnl:+.2f} USDT" + (f" ({pct:+.2f}% круга)" if pct is not None else "") if pnl is not None
           else "итог уточнит сверка")
    text = (f"{head}: выкуп {_fmt(qty)} {_esc(row['asset'])} по {_fmt(exitp)}, вход {_fmt(entry)} → {res}, "
            f"комиссии {_fmt(fee, 4)} USDT. Фандинг — в результате дня (/trading).")
    if reason == "auto_timeout":
        text += f"\n{RISK_BACK}"
    await _say(bot, text, thread=thread)
    return "closed"


# --- цикл ---

async def tick(bot, now=None):
    """Шаг цикла wiring: устаревшие карточки, «неясные» ордера, вход, отложенное и авто-закрытие, закрытие не ботом.
    Никогда не бросает (лог и дальше)."""
    try:
        await _tick(bot, time.time() if now is None else now)
    except asyncio.CancelledError:
        raise
    except Exception as e:   # noqa: BLE001 — хедж не роняет цикл ядра
        logger.error("hedge tick: %s: %s", type(e).__name__, e)


async def _tick(bot, now):
    if not os.path.exists(DB_PATH):
        return
    with _db() as con:   # карточки старше OFFER_TTL — устарели (nonce остаётся: нажатие пришлёт новый план)
        con.execute("UPDATE hedges SET status='expired', updated_ts=? WHERE status='offered' AND offered_ts<?",
                    (now, now - OFFER_TTL))
    for row in rows(LIVE):
        if row["id"] in _busy:
            continue
        try:
            await _tick_one(bot, row, now)
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001 — одна строка не останавливает остальные
            logger.error("hedge tick #%s: %s: %s", row["id"], type(e).__name__, e)


def _journal_row(group, purpose, since=None):
    """Последний ордер бота этой группы из журнала (purpose open/close; since — не раньше этого времени) или None: так
    находим ордер, если отправка прервалась сбоем или перезапуском до записи его id сюда."""
    for r in journal.history(1000):
        if r.get("grp") == group and r.get("strategy") == STRATEGY and r.get("purpose") == purpose \
                and (since is None or (r.get("created_ts") or 0) >= since):
            return r
    return None


def _journal_done(jr):
    """Состояние ордера журнала для хеджа: "filled" (есть исполнение), "none" (ордера точно нет или закрыт без
    исполнения), "wait" (ещё неясно или не исполнен)."""
    if jr is None:
        return "wait"
    filled = _dec(jr.get("filled")) or Decimal(0)
    if filled > 0 or jr.get("state") == "filled":
        return "filled"
    if jr.get("state") in ("rejected", "closed"):
        return "none"
    return "wait"


async def _tick_one(bot, row, now):
    hid, st, name = row["id"], row["status"], _name(row)
    if st == "opening":
        if now - (row["pressed_ts"] or row["updated_ts"] or 0) < STALE_SENDING:
            return
        jr = _journal_row(row["grp"], "open")
        if jr is None:
            if _set(hid, expect=("opening",), status="failed", refusal_where="bot",
                    refusal="отправка прервалась до ордера (перезапуск бота?)"):
                await _say(bot, f"✖️ Хедж круга {name} не открыт: отправка прервалась до ордера — круг без хеджа.")
            return
        _set(hid, expect=("opening",), status="unknown", open_cid=jr["client_id"], unknown_ts=now)
        return
    if st == "unknown":
        await _tick_unknown(bot, row, now)
    elif st == "open":
        await _tick_open(bot, row, now)
    elif st == "closing":
        await _tick_closing(bot, row, now)


async def _tick_unknown(bot, row, now):
    hid, name = row["id"], _name(row)
    jr = journal.get(row["open_cid"]) if row["open_cid"] else None
    if jr is None:   # id не записан (сбой посреди отправки) или строки нет — ищем ордер группы
        jr = _journal_row(row["grp"], "open")
    if jr is None:
        await _tick_orphan(bot, row, now)
        return
    done = _journal_done(jr)
    if done == "filled":
        entry = _dec(jr.get("avg_price"))
        if _set(hid, expect=("unknown",), status="open", open_cid=jr["client_id"], filled_ts=row["filled_ts"] or now,
                entry_price=venues.fmt(entry) if entry else "", filled_qty=jr.get("filled") or "", asked_pc=0):
            await _say(bot, "✅ Ордер хеджа нашёлся.\n" + _open_text(get(hid), jr.get("filled") or row["qty"], entry,
                                                                      jr["client_id"]), close_kb(hid))
            if row["close_req"]:   # закрыть просили, пока ордер был под вопросом, — закрываем сразу
                await close(bot, hid, row["close_req"])
        return
    if done == "none" and jr is not None:
        if _set(hid, expect=("unknown",), status="rejected" if jr["state"] == "rejected" else "failed",
                open_cid=jr["client_id"], refusal_where="venue", refusal=(jr.get("note") or jr["state"])[:600]):
            await _say(bot, f"✖️ Ордер хеджа круга {name} не исполнен ({_esc(jr['state'])}) — круг без хеджа.")
        return
    if not row["asked_pc"] and now - (row["unknown_ts"] or now) >= UNKNOWN_LONG:
        _set(hid, asked_pc=1)
        cid = row["open_cid"] or "—"
        await _say(bot, f"❓ Ордер хеджа круга {name} не подтверждён 10 мин — реши на ПК: найди ордер <code>"
                        f"{_esc(cid)}</code> в кабинете Bybit и разбери его в журнале ядра (journal.resolve). Бот его "
                        f"не повторяет, новые открытия ядро запрещает до разбора.")


async def _tick_orphan(bot, row, now):
    """«Неясно», а ордера группы в журнале ядра нет (id не записан, строки нет). Ничего не отправляем. Через
    ORPHAN_AFTER решаем по позиции группы в журнале: шорт есть — ведём как открытый хедж («Закрыть» и авто-закрытие
    работают); позиции и ожидающих ордеров нет — failed. До того — как обычный «неясный» (10 мин — «реши на ПК»)."""
    hid, name = row["id"], _name(row)
    since = row["unknown_ts"] or row["sent_ts"] or row["pressed_ts"] or now
    k = _key(row["venue"], row["symbol"], row["grp"]) if row["venue"] and row["symbol"] else None
    if now - since >= ORPHAN_AFTER:
        if k and k["net"] < 0:
            if _set(hid, expect=("unknown",), status="open", filled_ts=row["sent_ts"] or since,
                    filled_qty=venues.fmt(-k["net"]), asked_pc=0,
                    notes="ордер группы не найден в журнале, позиция группы по журналу есть"):
                await _say(bot, f"⚠️ Ордер хеджа круга {name} в журнале ядра не найден, но шорт по кругу у бота есть "
                                f"({_fmt(-k['net'])} {_esc(row['asset'])}) — веду его как открытый хедж: «Закрыть» и "
                                f"авто-закрытие работают.", close_kb(hid))
                if row["close_req"]:
                    await close(bot, hid, row["close_req"])
            return
        if not k or (k["net"] == 0 and k["pending_open"] == 0 and k["pending_reduce"]["buy"] == 0):
            if _set(hid, expect=("unknown",), status="failed", refusal_where="orphan",
                    refusal=f"ордер не найден в журнале за {ORPHAN_AFTER // 60} мин, позиции по кругу нет"):
                await _say(bot, f"✖️ Хедж круга {name}: ордер за {ORPHAN_AFTER // 60} мин так и не нашёлся в журнале "
                                f"ядра, позиции по кругу у бота нет — считаю, что шорта нет, круг без хеджа. Проверь "
                                f"кабинет Bybit.")
            return
    if not row["asked_pc"] and now - since >= UNKNOWN_LONG:
        _set(hid, asked_pc=1)
        await _say(bot, f"❓ Ордер хеджа круга {name} не подтверждён 10 мин и в журнале ядра не найден — реши на ПК: "
                        f"проверь кабинет Bybit. Бот ничего не повторяет; через {ORPHAN_AFTER // 60} мин решу по "
                        f"позиции группы в журнале.")


async def _tick_open(bot, row, now):
    hid = row["id"]
    oj = journal.get(row["open_cid"]) if row["open_cid"] else None
    done = _journal_done(oj)
    if done == "none":
        await _gone(bot, hid, f"ордер {oj['state']} без исполнения")
        return
    if done == "filled" and oj.get("avg_price") and not row["entry_price"]:
        _set(hid, entry_price=venues.fmt(_dec(oj["avg_price"])), filled_qty=oj.get("filled") or "")
    k = _key(row["venue"], row["symbol"], row["grp"])
    # исполнение подтверждено журналом (или шорт принят по позиции группы, когда ордера в журнале нет)
    confirmed = done == "filled" or (oj is None and bool(_dec(row["filled_qty"])))
    if confirmed and (not k or (k["net"] == 0 and k["pending_open"] == 0 and k["pending_reduce"]["buy"] == 0)):
        # исполненный шорт группы сверка обнулила (sync_flat: на бирже пусто) — закрыла биржа или владелец
        await _gone(bot, hid, "сверка ядра не нашла позицию на бирже")
        return
    retry = not row["auto_ts"] or now - row["auto_ts"] >= AUTO_RETRY   # не удалось — снова не чаще AUTO_RETRY
    if row["close_req"] and k and k["net"] != 0:   # закрыть просили, когда позиция ещё не была подтверждена
        if retry:
            _set(hid, auto_ts=now)
            await close(bot, hid, row["close_req"])
        return
    hours = settings()["max_hours"]
    since = row["filled_ts"] or row["sent_ts"]
    if hours > 0 and since and now - since >= hours * 3600 and retry:
        _set(hid, auto_ts=now)
        res = await close(bot, hid, "auto_timeout")
        logger.info("hedge #%s: авто-закрытие по сроку — %s", hid, res)


async def _tick_closing(bot, row, now):
    hid, name = row["id"], _name(row)
    if not row["close_cid"]:
        since = row["closing_ts"] or row["updated_ts"] or 0
        jr = _journal_row(row["grp"], "close", since - 5)
        if jr is not None:   # ордер закрытия ушёл, а его id сюда не записан (сбой) — берём из журнала
            _set(hid, expect=("closing",), close_cid=jr["client_id"])
        elif now - since >= STALE_SENDING:   # сбой или перезапуск посреди закрытия, ордера нет
            _set(hid, expect=("closing",), status="open", notes="закрытие прервалось до ордера")
        return
    cj = journal.get(row["close_cid"])
    if await _settle_close(bot, hid):
        return
    left = _close_left(row)
    if left is not None:   # ордер закрытия завершён, а шорт остался (частично / не исполнен) — снова «открыт»
        await _reopen(bot, row, *left)
        return
    if cj is None and now - (row["closing_ts"] or now) >= ORPHAN_AFTER:   # ордера закрытия в журнале нет
        if _set(hid, expect=("closing",), status="open", close_cid="", notes="ордер закрытия не найден в журнале"):
            await _say(bot, f"⚠️ Закрытие хеджа круга {name}: ордер {_esc(row['close_cid'])} не найден в журнале ядра — "
                            f"шорт считаю открытым; «Закрыть» и авто-закрытие работают.", close_kb(hid))
        return
    if cj is not None and cj["state"] in ("unknown", "prepared", "sending") and not row["asked_pc"] \
            and now - (row["unknown_ts"] or row["closing_ts"] or now) >= UNKNOWN_LONG:
        _set(hid, asked_pc=1)
        await _say(bot, f"❓ Закрытие хеджа круга {name} не подтверждено 10 мин — реши на ПК: ордер <code>"
                        f"{_esc(row['close_cid'])}</code> в кабинете Bybit и журнал ядра (journal.resolve).")


# --- кнопки и /hedge ---

async def callback(bot, cq, data):
    """Кнопки trd_hedge_* — их вызывает wiring.callback, то есть только владелец в личном чате (bot._owner_gate).
    → текст ответа кнопке. Сбой (база хеджей занята, «database is locked» и т. п.) не уходит наверх: wiring всё равно
    ответит кнопке, владелец увидит «повтори»."""
    try:
        return await _callback(bot, cq, data)
    except asyncio.CancelledError:
        raise
    except Exception as e:   # noqa: BLE001
        logger.error("hedge button %s: %s: %s", str(data)[:40], type(e).__name__, e)
        return f"хедж: сбой ({type(e).__name__}) — повтори через минуту"


async def _callback(bot, cq, data):
    parts = str(data).split(":")
    action = parts[0]
    if action == "trd_hedge_view":
        await hedge_command(bot)
        return "обновлено"
    try:
        hid = int(parts[1])
    except (IndexError, ValueError):
        return "кнопка не распознана"
    if action == "trd_hedge_open" and len(parts) == 3:
        return await on_open(bot, cq, hid, parts[2])
    if action == "trd_hedge_skip" and len(parts) == 2:
        return on_skip(hid)
    if action == "trd_hedge_close" and len(parts) in (2, 3):
        reason = CLOSE_CODES.get(parts[2] if len(parts) == 3 else "s")
        if reason is None:
            return "кнопка не распознана"
        return request_close(bot, hid, reason)
    return "кнопка не распознана"


def mode_lines():
    """Режим хеджа и почему: выключатель, .env, пороги gates, ключ площадки."""
    ok, _ = switch.can_open()
    lines = [f"торговля {'включена' if switch.enabled() else 'выключена'}, режим .env {switch.mode()}"]
    try:
        gm = gate_mode()
    except Exception as e:   # noqa: BLE001
        gm = f"не прочитать ({type(e).__name__})"
    lines.append(f"пороги gates для хеджа разрешают: {gm}")
    for v in settings()["venues"]:
        try:
            why, _ = keys.check_status(v, keys.raw_credentials(v))
        except Exception as e:   # noqa: BLE001
            why = f"ключ не прочитать ({type(e).__name__})"
        lines.append(f"ключ {SUPPORTED[v]}: " + ("✅ проверен" if not why else why))
    why, mode = live_mode()
    lines.append(f"→ хедж кругов по кнопке, режим {mode}" if not why else f"→ реальный хедж не предлагается: {why}")
    return lines


def _mark(row):
    q = perp.quote(SUPPORTED.get(row["venue"], ""), perp.venue_symbol(SUPPORTED.get(row["venue"], "Bybit"),
                                                                      row["asset"]))
    return _dec(q.mark) if q else None


def view(bot):
    """(текст, кнопки) /hedge: живые хеджи — вход, P&L по mark, до ликвидации (изолированная, 2×), фандинг по плану;
    режим и почему; последние итоги; кнопки «Закрыть» по каждому и «⛔ Стоп торговли»."""
    lines = ["🛡 <b>Хедж кругов</b> (шорт перпа на время круга)"] + [_esc(x) for x in mode_lines()] + ["",
                                                                                                        "<b>Живые</b>:"]
    kb = []
    live = rows(LIVE)
    for r in live:
        plan = json.loads(r["plan"] or "{}")
        entry, qty, mark = _dec(r["entry_price"]), _dec(r["filled_qty"]) or _dec(r["qty"]), _mark(r)
        parts = [f"#{r['id']} круг {_esc(r['circle_id'])}: {_esc(r['asset'])} {_fmt(qty)} на "
                 f"{_esc(SUPPORTED.get(r['venue'], r['venue']))} · {STATUS_TEXT.get(r['status'], r['status'])}"]
        if entry:
            parts.append(f"вход {_fmt(entry)}")
        if entry and mark and qty:
            parts.append(f"P&L {(entry - mark) * qty:+.2f} USDT по {_fmt(mark)}")
            liq = risk.isolated_liq("sell", entry, Decimal(LEVERAGE))
            parts.append(f"до ликвидации ~{(liq - mark) / mark * 100:.0f}%")
        if plan.get("funding") is not None:
            parts.append(f"фандинг (план) {plan['funding']:+.2f} USDT")
        since = r["filled_ts"] or r["sent_ts"]
        if since:
            parts.append(f"{(time.time() - since) / 3600:.1f} ч из {settings()['max_hours']:g}")
        lines.append("• " + " · ".join(parts))
        if r["status"] in ("open", "unknown"):
            kb.append([{"text": f"🔒 Закрыть #{r['id']}", "callback_data": f"trd_hedge_close:{r['id']}:m"}])
    if not live:
        lines.append("• нет")
    done = [r for r in rows(limit=8) if r["status"] not in LIVE][:3]
    if done:
        lines += ["", "<b>Последние</b>:"]
        for r in done:
            res = f", {float(r['pnl_usdt']):+.2f} USDT" if r["pnl_usdt"] else ""
            why = f": {_esc(r['refusal'][:120])}" if r["refusal"] and r["status"] in ("refused", "rejected", "failed") \
                else ""
            lines.append(f"• #{r['id']} {_esc(r['asset'])} — {STATUS_TEXT.get(r['status'], r['status'])}{res}{why}")
    if _silent:
        _, g, why = _silent[-1]
        lines += ["", f"Последний круг без предложения ({_esc(g)}): {_esc(why)}"]
    kb.append([{"text": "⛔ Стоп торговли", "callback_data": "trd_stop"},
               {"text": "🔄 Обновить", "callback_data": "trd_hedge_view"}])
    return "\n".join(lines), {"inline_keyboard": kb}


async def hedge_command(bot, arg=""):
    """/hedge (только владелец: bot.dispatch при REPLY_CHAT None). Сбой базы — короткий текст с «⛔ Стоп торговли»."""
    try:
        text, kb = view(bot)
    except Exception as e:   # noqa: BLE001
        logger.warning("hedge view: %s", type(e).__name__)
        text = f"🛡 <b>Хедж кругов</b>: подробности сейчас не прочитать ({_esc(type(e).__name__)}) — повторите позже."
        kb = {"inline_keyboard": [[{"text": "⛔ Стоп торговли", "callback_data": "trd_stop"}]]}
    await bot.send(text, markup=kb)
