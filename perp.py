"""Публичные данные бессрочных фьючерсов (перпов) Bybit и BingX — только чтение, без ключей и без ордеров.

Нужны бумажным симуляциям (simperp — хедж кругов, simfunding — фандинг, simdirectional — тренд). Источники:
  Bybit v5:  /v5/market/tickers (linear: mark/index/last/bid/ask, fundingRate, nextFundingTime),
             /v5/market/instruments-info (шаг лота, минимумы, интервал фандинга, статус),
             /v5/market/orderbook (linear и spot, спот — по символам с живым перпом), /v5/market/time,
             /v5/market/kline (1ч свечи для simdirectional);
  BingX swap: /openApi/swap/v2/quote/premiumIndex (mark/index/ставка/следующий расчёт),
             /openApi/swap/v2/quote/contracts?symbol= (шаг лота, минимумы, комиссия), /openApi/swap/v2/quote/depth,
             /openApi/swap/v2/server/time.
Только GET без редиректов и только на два хоста. Опрос раз в PERP_INTERVAL секунд (по умолчанию 30), у каждой
площадки свой бэкофф после ошибки (как у P2P-площадок: 30 с → 60 → … до 10 мин). Символ не торгуется — котировки
нет, это не ошибка площадки. Монеты — как в боте (BTC, ETH, TON), символ перпа на каждой площадке — одна таблица
VENUE_SYMBOLS (venue_symbol): TON с 15.06.2026 называется GRAM (1:1) — Bybit GRAMUSDT (TONUSDT закрыт), BingX
GRAMTON-USDT; интервал фандинга у GRAM 4 ч, у BTC/ETH 8 ч — берётся из ответов площадок.

Настройки .env: PERPS=0 — не опрашивать; PERP_INTERVAL; PERP_ASSETS (BTC,ETH,TON);
PERP_TAKER_FEES (Bybit:0.055,BingX:0.05 — % тейкера; у BingX по умолчанию берётся из справочника контрактов);
PERP_MAX_AGE — сколько секунд котировка считается свежей (90).
"""
import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)

BYBIT = "https://api.bybit.com"
BINGX = "https://open-api.bingx.com"
HOSTS = ("api.bybit.com", "open-api.bingx.com")
VENUES = ("Bybit", "BingX")
DEFAULT_ASSETS = "BTC,ETH,TON"
# монета бота → символ перпа на площадке (без дефиса; дефис BingX ставит bingx_symbol). TON переименован в GRAM 1:1
# (15.06.2026): Bybit — GRAMUSDT (TONUSDT закрыт), BingX swap — GRAMTON-USDT. BingX GRAM-USDT — другой, снятый токен:
# для TON не брать. Остальные монеты — <МОНЕТА>USDT.
VENUE_SYMBOLS = {"TON": {"Bybit": "GRAMUSDT", "BingX": "GRAMTONUSDT"}}
DEFAULT_TAKER = {"Bybit": 0.055, "BingX": 0.05}   # % тейкера перпа без VIP-уровня
DEPTH = 50            # уровней стакана
INFO_TTL = 6 * 3600   # справочник контрактов
TIME_TTL = 600        # время сервера (сдвиг часов)
KLINE_TTL = 60        # 1ч свечи: докачка последних
KLINE_FULL = 1000     # первая загрузка свечей — хватает на прогрев EMA(100)
BACKOFF_BASE = 30
BACKOFF_MAX = 600
SETTLE_STALE = 600    # сек: ставка из котировки старше этого до расчёта — расчёт помечается оценочным
REQUEST_TIMEOUT = 10  # сек на один публичный GET
KLINE_GRACE = 3       # сек после закрытия часа: свечи, загруженные раньше, про эту свечу ещё неполные
ROLL_GAP = 600        # сек: сдвиг следующего расчёта между опросами не дальше этого — это один интервал фандинга
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_warned = set()


def _on(name, default="1"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def env_float(name, default, lo=None, hi=None):
    """Число из .env (и для симуляций): пусто — по умолчанию; опечатка — по умолчанию с предупреждением в лог (один
    раз), а не исключение — ошибка в настройке бумаги не должна ронять опрос, скан P2P или сигналы. «1,5» = 1.5."""
    raw = os.getenv(name, "").strip()
    try:
        v = float(raw.replace(",", ".")) if raw else float(default)
        if not math.isfinite(v):
            raise ValueError(raw)
    except ValueError:
        if (name, raw) not in _warned:
            _warned.add((name, raw))
            logger.warning("%s=%r — не число, беру %s", name, raw, default)
        v = float(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _assets(spec):
    """Монеты через запятую; в URL идут только буквы/цифры (строка из .env не допишет параметры запроса)."""
    out = [x.strip().upper() for x in spec.split(",") if x.strip()]
    return [x for x in out if re.fullmatch(r"[A-Z0-9]{1,20}", x)]


def settings():
    """Читать при каждом обращении (после load_env), не при импорте."""
    fees = dict(DEFAULT_TAKER)
    explicit = set()
    for part in os.getenv("PERP_TAKER_FEES", "").split(","):
        k, _, v = part.partition(":")
        try:
            fees[k.strip()] = float(v)
            explicit.add(k.strip())
        except ValueError:
            continue
    return {"on": _on("PERPS"), "interval": env_float("PERP_INTERVAL", 30, lo=10.0),
            "assets": _assets(os.getenv("PERP_ASSETS", DEFAULT_ASSETS)),
            "taker": fees, "taker_explicit": explicit, "max_age": env_float("PERP_MAX_AGE", 90, lo=1.0)}


def _hours(v):
    """Интервал фандинга, ч: только правдоподобный (1–24), иначе None — берётся другой источник или 8 ч."""
    h = _f(v)
    return h if 1 <= h <= 24 else None


def bingx_symbol(symbol):
    """BTCUSDT → BTC-USDT."""
    return symbol[:-4] + "-" + symbol[-4:] if symbol.endswith("USDT") else symbol


def venue_symbol(venue, asset):
    """Символ перпа монеты бота на площадке: BTC → BTCUSDT; TON → GRAMUSDT на Bybit, GRAMTONUSDT на BingX."""
    a = asset.upper()
    return VENUE_SYMBOLS.get(a, {}).get(venue, a + "USDT")


def venue_symbols(venue, assets):
    """{символ площадки: монета бота} для опроса."""
    return {venue_symbol(venue, a): a for a in assets}


@dataclass
class Instrument:
    venue: str
    symbol: str
    active: bool             # контракт торгуется (открыт для новых позиций)
    lot: float = 0.0            # шаг количества (в монете)
    min_qty: float = 0.0
    min_notional: float = 0.0   # USDT
    interval_h: float = 8.0     # часов между расчётами фандинга
    taker_fee: float = None     # % из справочника площадки (BingX), None — по умолчанию
    note: str = ""


@dataclass
class PerpQuote:
    venue: str
    symbol: str
    mark: float
    index: float
    last: float
    bid: float
    ask: float
    funding_rate: float     # доля за расчёт (0.0001 = 0.01%); положительная — лонг платит шорту
    next_funding: float     # unix-сек (время сервера) ближайшего расчёта
    interval_h: float
    ts: float               # когда получено (локальные часы)
    skew: float = 0.0       # сек: локальные часы − серверные
    bids: tuple = ()        # ((цена, кол-во в монете), …) — лучшие первыми
    asks: tuple = ()
    lot: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 0.0
    taker_fee: float = 0.05   # %
    kind: str = "perp"        # perp | spot
    asset: str = ""           # монета бота (TON для GRAMUSDT)

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def age(self, now=None):
        return (time.time() if now is None else now) - self.ts

    def server_now(self, now=None):
        return (time.time() if now is None else now) - (self.skew or 0.0)


_quotes = {}        # (площадка, символ) -> PerpQuote
_spot = {}          # (площадка, символ) -> PerpQuote(kind="spot") — стакан спота Bybit для simfunding
_instr = {}         # (площадка, символ) -> Instrument
_klines = {}        # (площадка, символ) -> [(начало, o, h, l, c)] по возрастанию времени
_kline_srv = {}     # (площадка, символ) -> время сервера, когда свечи загружены (свеча закрыта раньше — она полная)
_skew = {}          # площадка -> сдвиг часов, сек
_backoff = {}       # площадка -> {"delay", "until"}
_meta = {"t": 0.0, "info_t": {}, "time_t": {}, "kline_t": {}, "errors": {}, "ok_t": {}}


def reset():
    """Для тестов: забыть всё состояние модуля."""
    for d in (_quotes, _spot, _instr, _klines, _kline_srv, _skew, _backoff):
        d.clear()
    _meta.update(t=0.0, info_t={}, time_t={}, kline_t={}, errors={}, ok_t={})


def _f(v, default=0.0):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _levels(rows, reverse):
    """[[цена, кол-во], …] → ((цена, кол-во), …), лучшие первыми, без нулевых."""
    out = [(_f(r[0]), _f(r[1])) for r in rows or () if len(r) >= 2]
    out = [(p, q) for p, q in out if p > 0 and q > 0]
    out.sort(key=lambda x: x[0], reverse=reverse)
    return tuple(out)


# --- разбор ответов (чистые функции, тесты на фикстурах) ---

def _bybit_ok(j):
    if not isinstance(j, dict) or j.get("retCode") != 0:
        raise ValueError(f"Bybit: {(j or {}).get('retMsg') or (j or {}).get('retCode')}")
    return j.get("result") or {}


def _bingx_ok(j):
    if not isinstance(j, dict) or j.get("code") != 0:
        raise ValueError(f"BingX: {(j or {}).get('msg') or (j or {}).get('code')}")
    return j.get("data")


def parse_bybit_instrument(j, symbol):
    res = _bybit_ok(j)
    row = next((x for x in res.get("list") or [] if x.get("symbol") == symbol), None)
    if row is None:
        return Instrument("Bybit", symbol, False, note="нет в списке контрактов")
    lot = row.get("lotSizeFilter") or {}
    status = row.get("status") or ""
    return Instrument("Bybit", symbol, status == "Trading" and row.get("contractType") == "LinearPerpetual",
                      lot=_f(lot.get("qtyStep")), min_qty=_f(lot.get("minOrderQty")),
                      min_notional=_f(lot.get("minNotionalValue")),
                      interval_h=_hours(_f(row.get("fundingInterval")) / 60) or 8.0,
                      note="" if status == "Trading" else f"статус {status or '?'}")


def parse_bybit_ticker(j, symbol):
    res = _bybit_ok(j)
    if res.get("category") not in (None, "linear"):
        raise ValueError(f"Bybit: не та категория {res.get('category')}")
    row = next((x for x in res.get("list") or [] if x.get("symbol") == symbol), None)
    if row is None:
        raise ValueError(f"Bybit: нет тикера {symbol}")
    if "markPrice" not in row or "fundingRate" not in row:
        raise ValueError("Bybit: в тикере нет markPrice/fundingRate")
    return {"mark": _f(row.get("markPrice")), "index": _f(row.get("indexPrice")), "last": _f(row.get("lastPrice")),
            "bid": _f(row.get("bid1Price")), "ask": _f(row.get("ask1Price")),
            "funding_rate": _f(row.get("fundingRate")), "next_funding": _f(row.get("nextFundingTime")) / 1000,
            "interval_h": _hours(row.get("fundingIntervalHour"))}


def parse_bybit_book(j):
    res = _bybit_ok(j)
    return _levels(res.get("b"), True), _levels(res.get("a"), False)


def parse_bybit_time(j):
    res = _bybit_ok(j)
    if res.get("timeNano"):
        return int(res["timeNano"]) / 1e9
    return _f(res.get("timeSecond")) or _f(j.get("time")) / 1000


def parse_bybit_klines(j):
    """[[start_ms, o, h, l, c, vol, turnover], …] (новые первыми) → [(start_s, o, h, l, c)] по возрастанию."""
    res = _bybit_ok(j)
    out = []
    for r in res.get("list") or []:
        if len(r) >= 5:
            out.append((_f(r[0]) / 1000, _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4])))
    return sorted(out)


def parse_bingx_contracts(j, symbols):
    data = _bingx_ok(j) or []
    by = {x.get("symbol"): x for x in data if isinstance(x, dict)}
    out = {}
    for sym in symbols:
        row = by.get(bingx_symbol(sym))
        if row is None:
            out[sym] = Instrument("BingX", sym, False, note="нет в списке контрактов")
            continue
        ok = str(row.get("status")) == "1" and str(row.get("apiStateOpen", "true")).lower() == "true"
        fee = row.get("takerFeeRate", row.get("feeRate"))
        out[sym] = Instrument("BingX", sym, ok, lot=_f(row.get("size")) or 10 ** -int(row.get("quantityPrecision") or 0),
                              min_qty=_f(row.get("tradeMinQuantity")), min_notional=_f(row.get("tradeMinUSDT")),
                              taker_fee=_f(fee) * 100 if fee not in (None, "") else None,
                              note="" if ok else f"статус {row.get('status')}")
    return out


def parse_bingx_premium(j):
    d = _bingx_ok(j)
    if isinstance(d, list):
        d = d[0] if d else {}
    if not d or "markPrice" not in d:
        raise ValueError("BingX: пустой premiumIndex")
    return {"mark": _f(d.get("markPrice")), "index": _f(d.get("indexPrice")),
            "funding_rate": _f(d.get("lastFundingRate")), "next_funding": _f(d.get("nextFundingTime")) / 1000,
            "interval_h": _hours(d.get("fundingIntervalHours"))}


def parse_bingx_depth(j):
    """Количество — в монете: bidsCoin/asksCoin, если есть (у BingX так надёжнее), иначе bids/asks."""
    d = _bingx_ok(j) or {}
    bids = d.get("bidsCoin") or d.get("bids")
    asks = d.get("asksCoin") or d.get("asks")
    return _levels(bids, True), _levels(asks, False)


def parse_bingx_time(j):
    d = _bingx_ok(j) or {}
    return _f(d.get("serverTime")) / 1000


# --- исполнение по стакану ---

def walk(levels, qty):
    """Средняя цена исполнения qty по уровням (лучшие первыми); глубины не хватает — None."""
    if qty <= 0:
        return None
    left, cost = qty, 0.0
    for price, size in levels:
        take = min(left, size)
        cost += take * price
        left -= take
        if left <= qty * 1e-9:
            return cost / qty
    return None


def round_lot(qty, lot, min_qty=0.0):
    """До ближайшего шага лота (коэффициент хеджа ближе к 1); меньше минимума — 0."""
    if lot and lot > 0:
        qty = round(round(qty / lot) * lot, 12)
    return qty if qty >= (min_qty or 0) and qty > 0 else 0.0


def floor_lot(qty, lot, min_qty=0.0):
    """Вниз до шага лота (не больше заданного объёма); меньше минимума — 0."""
    if lot and lot > 0:
        qty = round(int(qty / lot + 1e-9) * lot, 12)
    return qty if qty >= (min_qty or 0) and qty > 0 else 0.0


# --- фандинг: точный учёт по живым котировкам ---

def settle(st, q, now=None):
    """Наступившие расчёты фандинга для одной ноги. st — словарь ноги (хранится у симуляции): next — время
    сервера ближайшего расчёта, rate/mark — ставка и mark из последней котировки ДО этого расчёта (именно она
    и спишется), interval_h. q — текущая котировка (или None). Котировка обновляет ставку, только если говорит
    о том же ближайшем расчёте (после расчёта у площадки уже новая ставка на следующий). Пропущенные расчёты
    (бот не работал) считаются по последней известной ставке, как и расчёт, до которого свежей котировки не было
    дольше SETTLE_STALE, — такие помечаются approx.
    Котировка, полученная уже после своего «ближайшего» расчёта (площадка ещё не сдвинула время, а ставка могла
    уже стать ставкой следующего периода) или с временем расчёта дальше суток, ставку не задаёт.
    Возвращает [(время расчёта, ставка, mark, approx)]; st меняется на месте."""
    now = time.time() if now is None else now
    q = q if _ahead(q) else None   # только котировка «до расчёта»: остальное — как будто её нет

    def take(q):   # котировка о том же ближайшем расчёте — её ставка и спишется
        if q is not None and abs(q.next_funding - st["next"]) < 1:
            st.update(rate=q.funding_rate, mark=q.mark, interval_h=q.interval_h or st.get("interval_h") or 8,
                      used=False, seen=q.ts - (q.skew or 0.0))

    if q is not None:
        st["skew"] = q.skew or 0.0
        if not st.get("next"):
            st["next"] = q.next_funding
    if st.get("next"):
        take(q)
    server_now = now - (st.get("skew") or 0.0)
    out = []
    while st.get("next") and server_now >= st["next"]:
        if st.get("rate") is not None and st.get("mark"):
            stale = st["next"] - (st.get("seen") or st["next"]) > SETTLE_STALE
            out.append((st["next"], st["rate"], st["mark"], bool(st.get("used")) or stale))
        st["used"] = True   # следующий расчёт по этой же ставке — уже оценка
        nxt = st["next"] + (_hours(st.get("interval_h")) or 8) * 3600   # шаг ≥ 1 ч: цикл конечен при любых данных
        if q is not None and st["next"] < q.next_funding <= nxt + 1:
            nxt = q.next_funding   # площадка знает точное время (интервал мог смениться)
        st["next"] = nxt
        take(q)
    return out


def _ahead(q):
    """Котировка про будущий расчёт: получена (по часам сервера) раньше него и не больше чем за сутки."""
    if q is None or not q.next_funding:
        return False
    ahead = q.next_funding - (q.ts - (q.skew or 0.0))
    return 0 < ahead <= 25 * 3600


def funding_windows(q, start, hours):
    """Сколько расчётов фандинга попадёт в окно [start, start + hours] (время сервера)."""
    if q is None or not q.next_funding:
        return 0
    step = (_hours(q.interval_h) or 8) * 3600
    t = q.next_funding
    if t < start:   # расчёт в прошлом (котировка до него) — ближайший следующий, без цикла по шагам
        t += math.ceil((start - t) / step) * step
    end = start + hours * 3600
    return 0 if t > end else int((end - t) // step) + 1


# --- доступ к состоянию ---

def quotes():
    """Копия последних котировок {(площадка, символ площадки): PerpQuote} — для Snapshot.perps."""
    return dict(_quotes)


def _fresh(q, now, max_age):
    if q is None:
        return None
    limit = settings()["max_age"] if max_age is None else max_age
    return q if q.age(now) <= limit else None


def quote(venue, symbol, now=None, max_age=None):
    """Свежая котировка перпа по символу площадки или None (нет, не торгуется, старше PERP_MAX_AGE)."""
    return _fresh(_quotes.get((venue, symbol)), now, max_age)


def quote_for(venue, asset, now=None, max_age=None):
    """Свежая котировка перпа монеты бота на площадке (TON → GRAMUSDT/GRAMTONUSDT)."""
    return quote(venue, venue_symbol(venue, asset), now, max_age)


def last_for(venue, asset):
    """Последняя котировка перпа монеты любой давности (для учёта фандинга) или None."""
    return _quotes.get((venue, venue_symbol(venue, asset)))


def spot_quote(venue, symbol, now=None, max_age=None):
    return _fresh(_spot.get((venue, symbol)), now, max_age)


def spot_for(venue, asset, now=None, max_age=None):
    """Стакан спота монеты (Bybit, тот же символ, что у перпа: GRAMUSDT для TON)."""
    return spot_quote(venue, venue_symbol(venue, asset), now, max_age)


def instrument(venue, symbol):
    return _instr.get((venue, symbol))


def klines(venue, symbol):
    return list(_klines.get((venue, symbol)) or [])


def kline_time(venue, symbol):
    """Время сервера последней загрузки свечей (None — не загружались): свеча, закрывшаяся позже, в кеше неполная."""
    return _kline_srv.get((venue, symbol))


def status(now=None):
    """Для /funding и /futures: ошибки, паузы, время последнего ответа, неторгуемые символы."""
    now = time.time() if now is None else now
    return {"errors": dict(_meta["errors"]),
            "paused": {v: st["until"] for v, st in _backoff.items() if st["until"] > now},
            "ok_t": dict(_meta["ok_t"]),
            "closed": {k: i.note for k, i in _instr.items() if not i.active}}


# --- сеть ---

async def _get(s, url):
    """Публичный GET только по https на api.bybit.com / open-api.bingx.com (проверка до запроса), без редиректов,
    со своим таймаутом."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS or parts.port or parts.username or parts.password:
        raise ValueError(f"адрес не разрешён: {parts.netloc or url[:40]}")
    async with s.get(url, headers=HEADERS, allow_redirects=False,
                     timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as r:
        r.raise_for_status()
        if r.status != 200:   # 3xx: редирект не выполняем
            raise ValueError(f"HTTP {r.status}")
        return await r.json(content_type=None)


def _due(kind, venue, ttl, now):
    return now - _meta[kind].get(venue, 0.0) >= ttl


def _taker(venue, inst, cfg):
    if venue in cfg["taker_explicit"] or inst is None or inst.taker_fee is None:
        return cfg["taker"].get(venue, DEFAULT_TAKER.get(venue, 0.06))
    return inst.taker_fee


async def _sync_time(s, get, venue, now):
    if not _due("time_t", venue, TIME_TTL, now):
        return
    t0 = time.time()
    try:   # время сервера — только поправка часов: сбой не отменяет котировки, сдвиг остаётся прежним
        if venue == "Bybit":
            srv = parse_bybit_time(await get(s, f"{BYBIT}/v5/market/time"))
        else:
            srv = parse_bingx_time(await get(s, f"{BINGX}/openApi/swap/v2/server/time"))
    except Exception as e:
        logger.warning("перпы %s: время сервера не получено (%s)", venue, type(e).__name__)
        return
    local = (t0 + time.time()) / 2
    _skew[venue] = local - srv if srv else 0.0
    _meta["time_t"][venue] = now


async def _bybit_symbol(s, get, sym, asset, cfg):
    inst = _instr.get(("Bybit", sym))
    t, book = await asyncio.gather(get(s, f"{BYBIT}/v5/market/tickers?category=linear&symbol={sym}"),
                                   get(s, f"{BYBIT}/v5/market/orderbook?category=linear&symbol={sym}&limit={DEPTH}"))
    d = parse_bybit_ticker(t, sym)
    bids, asks = parse_bybit_book(book)
    if not bids or not asks:   # без стакана нет ни цены исполнения, ни mid — котировку не подменяем нулями
        raise ValueError(f"Bybit: пустой стакан {sym}")
    _quotes[("Bybit", sym)] = PerpQuote(
        "Bybit", sym, d["mark"], d["index"], d["last"], bids[0][0], asks[0][0], d["funding_rate"],
        d["next_funding"], d["interval_h"] or inst.interval_h,
        time.time(), _skew.get("Bybit", 0.0), bids, asks, inst.lot, inst.min_qty, inst.min_notional,
        _taker("Bybit", inst, cfg), asset=asset)


async def _bybit_spot(s, get, sym, asset):
    bids, asks = parse_bybit_book(await get(s, f"{BYBIT}/v5/market/orderbook?category=spot&symbol={sym}&limit={DEPTH}"))
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        _spot[("Bybit", sym)] = PerpQuote("Bybit", sym, mid, mid, mid, bids[0][0], asks[0][0], 0.0, 0.0, 0.0,
                                          time.time(), _skew.get("Bybit", 0.0), bids, asks, kind="spot", asset=asset)


def _new_hour(key, now):
    """Час закрылся после последней загрузки свечей — докачать сразу, не дожидаясь KLINE_TTL."""
    srv = now - _skew.get(key[0], 0.0)
    edge = srv - srv % 3600 + KLINE_GRACE
    return srv >= edge and _kline_srv.get(key, 0.0) < edge


async def _bybit_klines(s, get, sym, now):
    key = ("Bybit", sym)
    if not (_due("kline_t", key, KLINE_TTL, now) or _new_hour(key, now)):
        return
    have = _klines.get(key) or []
    limit = 5 if len(have) >= 200 else KLINE_FULL
    rows = parse_bybit_klines(await get(s, f"{BYBIT}/v5/market/kline?category=linear&symbol={sym}&interval=60"
                                           f"&limit={limit}"))
    merged = {r[0]: r for r in have}
    merged.update({r[0]: r for r in rows})
    _klines[key] = sorted(merged.values())[-KLINE_FULL:]
    _meta["kline_t"][key] = now
    _kline_srv[key] = now - _skew.get("Bybit", 0.0)   # момент запроса: всё, что закрылось раньше, пришло целиком


def _live(venue, syms):
    """Символы площадки, что торгуются по справочнику; котировки неторгуемых убираем."""
    live = {x: a for x, a in syms.items() if _instr[(venue, x)].active}
    for x in syms:
        if x not in live:
            _quotes.pop((venue, x), None)
            _spot.pop((venue, x), None)
    return live


async def _refresh_bybit(s, get, cfg, now):
    syms = venue_symbols("Bybit", cfg["assets"])
    await _sync_time(s, get, "Bybit", now)
    if _due("info_t", "Bybit", INFO_TTL, now) or any(("Bybit", x) not in _instr for x in syms):
        res = await asyncio.gather(*(get(s, f"{BYBIT}/v5/market/instruments-info?category=linear&symbol={x}")
                                     for x in syms))
        for sym, j in zip(syms, res):
            _instr[("Bybit", sym)] = parse_bybit_instrument(j, sym)
        _meta["info_t"]["Bybit"] = now
    live = _live("Bybit", syms)
    jobs = ([(f"Bybit/{x}", _bybit_symbol(s, get, x, a, cfg)) for x, a in live.items()]
            + [(f"Bybit/spot/{x}", _bybit_spot(s, get, x, a)) for x, a in live.items()]
            + [(f"Bybit/kline/{x}", _bybit_klines(s, get, x, now)) for x in live])
    res = await asyncio.gather(*(c for _, c in jobs), return_exceptions=True)
    if live and all(isinstance(r, Exception) for r in res[:len(live)]):
        raise res[0]
    return {k: f"{type(e).__name__}: {e}"[:120] for (k, _), e in zip(jobs, res) if isinstance(e, Exception)}


BINGX_NOT_LISTED = (109425, 109418)   # символа нет / снят с торгов — не ошибка площадки


def parse_bingx_contract(j, symbol):
    """Справочник одного контракта (contracts?symbol=): нет или снят — неторгуемый Instrument с причиной."""
    if isinstance(j, dict) and j.get("code") in BINGX_NOT_LISTED:
        return Instrument("BingX", symbol, False, note=str(j.get("msg") or j.get("code"))[:80])
    return parse_bingx_contracts(j, [symbol])[symbol]


async def _bingx_symbol(s, get, sym, asset, cfg):
    inst = _instr.get(("BingX", sym))
    bx = bingx_symbol(sym)
    p, book = await asyncio.gather(get(s, f"{BINGX}/openApi/swap/v2/quote/premiumIndex?symbol={bx}"),
                                   get(s, f"{BINGX}/openApi/swap/v2/quote/depth?symbol={bx}&limit={DEPTH}"))
    d = parse_bingx_premium(p)
    bids, asks = parse_bingx_depth(book)
    if not bids or not asks:
        raise ValueError(f"BingX: пустой стакан {bx}")
    mid = (bids[0][0] + asks[0][0]) / 2
    _quotes[("BingX", sym)] = PerpQuote(
        "BingX", sym, d["mark"], d["index"], mid, bids[0][0], asks[0][0], d["funding_rate"], d["next_funding"],
        d["interval_h"] or _bingx_interval(sym, d["next_funding"]) or inst.interval_h, time.time(),
        _skew.get("BingX", 0.0), bids, asks, inst.lot, inst.min_qty, inst.min_notional, _taker("BingX", inst, cfg),
        asset=asset)


def _bingx_interval(sym, next_funding):
    """В premiumIndex нет fundingIntervalHours: интервал — на сколько сдвинулся следующий расчёт между соседними
    опросами (не дальше ROLL_GAP — ровно один расчёт); сдвига ещё не было — прежний из котировки. None — неизвестно
    (справочник BingX интервала не даёт — тогда 8 ч)."""
    prev = _quotes.get(("BingX", sym))
    if prev is None:
        return None
    step = next_funding - prev.next_funding
    if step > 1 and time.time() - prev.ts <= ROLL_GAP:
        return _hours(step / 3600) or prev.interval_h
    return prev.interval_h


async def _refresh_bingx(s, get, cfg, now):
    syms = venue_symbols("BingX", cfg["assets"])
    await _sync_time(s, get, "BingX", now)
    if _due("info_t", "BingX", INFO_TTL, now) or any(("BingX", x) not in _instr for x in syms):
        res = await asyncio.gather(*(get(s, f"{BINGX}/openApi/swap/v2/quote/contracts?symbol={bingx_symbol(x)}")
                                     for x in syms))
        for sym, j in zip(syms, res):
            _instr[("BingX", sym)] = parse_bingx_contract(j, sym)
        _meta["info_t"]["BingX"] = now
    live = _live("BingX", syms)
    res = await asyncio.gather(*(_bingx_symbol(s, get, x, a, cfg) for x, a in live.items()), return_exceptions=True)
    errors = [r for r in res if isinstance(r, Exception)]
    if live and len(errors) == len(live):
        raise errors[0]
    return {f"BingX/{x}": f"{type(e).__name__}: {e}"[:120] for x, e in zip(live, res) if isinstance(e, Exception)}


REFRESHERS = {"Bybit": _refresh_bybit, "BingX": _refresh_bingx}


def _paused(venue, now):
    st = _backoff.get(venue)
    return st is not None and st["until"] > now


def _fail(venue, now):
    st = _backoff.setdefault(venue, {"delay": 0, "until": 0.0})
    st["delay"] = BACKOFF_BASE if not st["delay"] else min(st["delay"] * 2, BACKOFF_MAX)
    st["until"] = now + st["delay"]
    logger.warning("перпы %s: ошибка, пропускаю %d с", venue, st["delay"])


def _ok(venue):
    if _backoff.pop(venue, None):
        logger.info("перпы %s: снова отвечает, бэкофф сброшен", venue)


async def refresh(s, get=None, now=None):
    """Обновить котировки по всем площадкам, что не на паузе. get(s, url) — GET-запрос (в тестах подменяется).
    Возвращает {ключ: ошибка}."""
    cfg = settings()
    get = get or _get
    now = time.time() if now is None else now
    venues = [v for v in VENUES if not _paused(v, now)]
    res = await asyncio.gather(*(REFRESHERS[v](s, get, cfg, now) for v in venues), return_exceptions=True)
    errors = {}
    for v, r in zip(venues, res):
        if isinstance(r, Exception):
            _fail(v, now)
            errors[v] = f"{type(r).__name__}: {r}"[:120]
        else:
            _ok(v)
            _meta["ok_t"][v] = now
            errors.update(r)
    _meta["errors"] = errors
    return errors


async def refresh_if_due(s, get=None, now=None):
    cfg = settings()
    now = time.time() if now is None else now
    if not cfg["on"] or now - _meta["t"] < cfg["interval"]:
        return None
    _meta["t"] = now   # сначала отметка: при сбое не долбим биржи чаще интервала
    return await refresh(s, get, now)
