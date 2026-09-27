"""Публичные данные бессрочных фьючерсов (перпов) Bybit и BingX — только чтение, без ключей и без ордеров.

Нужны бумажным симуляциям (simperp — хедж кругов, simfunding — фандинг, simdirectional — тренд). Источники:
  Bybit v5:  /v5/market/tickers (linear: mark/index/last/bid/ask, fundingRate, nextFundingTime),
             /v5/market/instruments-info (шаг лота, минимумы, интервал фандинга, статус),
             /v5/market/orderbook (linear и spot — стакан для исполнения по глубине), /v5/market/time,
             /v5/market/kline (1ч свечи для simdirectional);
  BingX swap: /openApi/swap/v2/quote/premiumIndex (mark/index/ставка/следующий расчёт),
             /openApi/swap/v2/quote/contracts (шаг лота, минимумы, комиссия), /openApi/swap/v2/quote/depth,
             /openApi/swap/v2/server/time.
Только GET без редиректов и только на два хоста. Опрос раз в PERP_INTERVAL секунд (по умолчанию 30), у каждой
площадки свой бэкофф после ошибки (как у P2P-площадок: 30 с → 60 → … до 10 мин). Символ не торгуется — котировки
нет, это не ошибка площадки. TON с 15.06.2026 называется GRAM (1:1): перп TONUSDT на Bybit закрыт, торгуется
GRAMUSDT (фандинг раз в 4 ч); на BingX нет ни TON, ни GRAM.

Настройки .env: PERPS=0 — не опрашивать; PERP_INTERVAL; PERP_SYMBOLS (BTCUSDT,ETHUSDT,GRAMUSDT);
PERP_TAKER_FEES (Bybit:0.055,BingX:0.05 — % тейкера; у BingX по умолчанию берётся из справочника контрактов);
PERP_MAX_AGE — сколько секунд котировка считается свежей (90).
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

BYBIT = "https://api.bybit.com"
BINGX = "https://open-api.bingx.com"
HOSTS = ("api.bybit.com", "open-api.bingx.com")
VENUES = ("Bybit", "BingX")
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT,GRAMUSDT"
# монета P2P → базовый актив перпа: TON переименован в GRAM 1:1 (15.06.2026), перп TONUSDT на Bybit закрыт
ASSET_ALIASES = {"TON": "GRAM"}
DEFAULT_TAKER = {"Bybit": 0.055, "BingX": 0.05}   # % тейкера перпа без VIP-уровня
DEPTH = 50            # уровней стакана
INFO_TTL = 6 * 3600   # справочник контрактов
TIME_TTL = 600        # время сервера (сдвиг часов)
KLINE_TTL = 60        # 1ч свечи: докачка последних
KLINE_FULL = 1000     # первая загрузка свечей — хватает на прогрев EMA(100)
BACKOFF_BASE = 30
BACKOFF_MAX = 600
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _on(name, default="1"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


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
    return {"on": _on("PERPS"), "interval": max(10.0, float(os.getenv("PERP_INTERVAL", 30))),
            "symbols": [x.strip().upper() for x in os.getenv("PERP_SYMBOLS", DEFAULT_SYMBOLS).split(",") if x.strip()],
            "taker": fees, "taker_explicit": explicit, "max_age": float(os.getenv("PERP_MAX_AGE", 90))}


def bingx_symbol(symbol):
    """BTCUSDT → BTC-USDT."""
    return symbol[:-4] + "-" + symbol[-4:] if symbol.endswith("USDT") else symbol


def asset_symbol(asset):
    """Монета P2P → символ перпа: BTC → BTCUSDT, TON → GRAMUSDT (ASSET_ALIASES)."""
    a = asset.upper()
    return ASSET_ALIASES.get(a, a) + "USDT"


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
_skew = {}          # площадка -> сдвиг часов, сек
_backoff = {}       # площадка -> {"delay", "until"}
_meta = {"t": 0.0, "info_t": {}, "time_t": {}, "kline_t": {}, "errors": {}, "ok_t": {}}


def reset():
    """Для тестов: забыть всё состояние модуля."""
    for d in (_quotes, _spot, _instr, _klines, _skew, _backoff):
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
                      interval_h=_f(row.get("fundingInterval"), 480.0) / 60,
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
            "interval_h": _f(row.get("fundingIntervalHour"), 0.0) or None}


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
            "interval_h": _f(d.get("fundingIntervalHours"), 0.0) or None}


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
    (бот не работал) считаются по последней известной ставке — помечаются approx.
    Возвращает [(время расчёта, ставка, mark, approx)]; st меняется на месте."""
    now = time.time() if now is None else now

    def take(q):   # котировка о том же ближайшем расчёте — её ставка и спишется
        if q is not None and q.next_funding and abs(q.next_funding - st["next"]) < 1:
            st.update(rate=q.funding_rate, mark=q.mark, interval_h=q.interval_h or st.get("interval_h") or 8,
                      used=False)

    if q is not None:
        st["skew"] = q.skew or 0.0
        if q.next_funding and not st.get("next"):
            st["next"] = q.next_funding
    if st.get("next"):
        take(q)
    server_now = now - (st.get("skew") or 0.0)
    out = []
    while st.get("next") and server_now >= st["next"]:
        if st.get("rate") is not None and st.get("mark"):
            out.append((st["next"], st["rate"], st["mark"], bool(st.get("used"))))
        st["used"] = True   # следующий расчёт по этой же ставке — уже оценка
        nxt = st["next"] + (st.get("interval_h") or 8) * 3600
        if q is not None and q.next_funding and st["next"] < q.next_funding <= nxt + 1:
            nxt = q.next_funding   # площадка знает точное время (интервал мог смениться)
        st["next"] = nxt
        take(q)
    return out


def funding_windows(q, start, hours):
    """Сколько расчётов фандинга попадёт в окно [start, start + hours] (время сервера)."""
    if q is None or not q.next_funding:
        return 0
    step = (q.interval_h or 8) * 3600
    n, t = 0, q.next_funding
    while t < start:
        t += step
    while t <= start + hours * 3600:
        n += 1
        t += step
    return n


# --- доступ к состоянию ---

def quotes():
    """Копия последних котировок {(площадка, символ): PerpQuote} — для Snapshot.perps."""
    return dict(_quotes)


def quote(venue, symbol, now=None, max_age=None):
    """Свежая котировка перпа или None (нет, символ не торгуется, старше PERP_MAX_AGE)."""
    q = _quotes.get((venue, symbol))
    if q is None:
        return None
    limit = settings()["max_age"] if max_age is None else max_age
    return q if q.age(now) <= limit else None


def spot_quote(venue, symbol, now=None, max_age=None):
    q = _spot.get((venue, symbol))
    if q is None:
        return None
    limit = settings()["max_age"] if max_age is None else max_age
    return q if q.age(now) <= limit else None


def instrument(venue, symbol):
    return _instr.get((venue, symbol))


def klines(venue, symbol):
    return list(_klines.get((venue, symbol)) or [])


def status(now=None):
    """Для /funding и /futures: ошибки, паузы, возраст данных."""
    now = time.time() if now is None else now
    return {"errors": dict(_meta["errors"]),
            "paused": {v: st["until"] for v, st in _backoff.items() if st["until"] > now},
            "ok_t": dict(_meta["ok_t"]),
            "closed": {k: i.note for k, i in _instr.items() if not i.active}}


# --- сеть ---

async def _get(s, url):
    """Публичный GET только на api.bybit.com / open-api.bingx.com, без редиректов."""
    host = url.split("/")[2]
    if host not in HOSTS:
        raise ValueError(f"хост не разрешён: {host}")
    async with s.get(url, headers=HEADERS, allow_redirects=False) as r:
        r.raise_for_status()
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
    if venue == "Bybit":
        srv = parse_bybit_time(await get(s, f"{BYBIT}/v5/market/time"))
    else:
        srv = parse_bingx_time(await get(s, f"{BINGX}/openApi/swap/v2/server/time"))
    local = (t0 + time.time()) / 2
    _skew[venue] = local - srv if srv else 0.0
    _meta["time_t"][venue] = now


async def _bybit_symbol(s, get, sym, now, cfg):
    inst = _instr.get(("Bybit", sym))
    t, book = await asyncio.gather(get(s, f"{BYBIT}/v5/market/tickers?category=linear&symbol={sym}"),
                                   get(s, f"{BYBIT}/v5/market/orderbook?category=linear&symbol={sym}&limit={DEPTH}"))
    d = parse_bybit_ticker(t, sym)
    bids, asks = parse_bybit_book(book)
    _quotes[("Bybit", sym)] = PerpQuote(
        "Bybit", sym, d["mark"], d["index"], d["last"], bids[0][0] if bids else d["bid"],
        asks[0][0] if asks else d["ask"], d["funding_rate"], d["next_funding"], d["interval_h"] or inst.interval_h,
        time.time(), _skew.get("Bybit", 0.0), bids, asks, inst.lot, inst.min_qty, inst.min_notional,
        _taker("Bybit", inst, cfg))


async def _bybit_spot(s, get, sym, now):
    bids, asks = parse_bybit_book(await get(s, f"{BYBIT}/v5/market/orderbook?category=spot&symbol={sym}&limit={DEPTH}"))
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        _spot[("Bybit", sym)] = PerpQuote("Bybit", sym, mid, mid, mid, bids[0][0], asks[0][0], 0.0, 0.0, 0.0,
                                          time.time(), _skew.get("Bybit", 0.0), bids, asks, kind="spot")


async def _bybit_klines(s, get, sym, now):
    if not _due("kline_t", ("Bybit", sym), KLINE_TTL, now):
        return
    have = _klines.get(("Bybit", sym)) or []
    limit = 5 if len(have) >= 200 else KLINE_FULL
    rows = parse_bybit_klines(await get(s, f"{BYBIT}/v5/market/kline?category=linear&symbol={sym}&interval=60"
                                           f"&limit={limit}"))
    merged = {r[0]: r for r in have}
    merged.update({r[0]: r for r in rows})
    _klines[("Bybit", sym)] = sorted(merged.values())[-KLINE_FULL:]
    _meta["kline_t"][("Bybit", sym)] = now


async def _refresh_bybit(s, get, cfg, now):
    await _sync_time(s, get, "Bybit", now)
    if _due("info_t", "Bybit", INFO_TTL, now) or any(("Bybit", x) not in _instr for x in cfg["symbols"]):
        res = await asyncio.gather(*(get(s, f"{BYBIT}/v5/market/instruments-info?category=linear&symbol={x}")
                                     for x in cfg["symbols"]))
        for sym, j in zip(cfg["symbols"], res):
            _instr[("Bybit", sym)] = parse_bybit_instrument(j, sym)
        _meta["info_t"]["Bybit"] = now
    live = [x for x in cfg["symbols"] if _instr[("Bybit", x)].active]
    for x in cfg["symbols"]:
        if x not in live:
            _quotes.pop(("Bybit", x), None)
    jobs = ([(f"Bybit/{x}", _bybit_symbol(s, get, x, now, cfg)) for x in live]
            + [(f"Bybit/spot/{x}", _bybit_spot(s, get, x, now)) for x in cfg["symbols"]]
            + [(f"Bybit/kline/{x}", _bybit_klines(s, get, x, now)) for x in live])
    res = await asyncio.gather(*(c for _, c in jobs), return_exceptions=True)
    if live and all(isinstance(r, Exception) for r in res[:len(live)]):
        raise res[0]
    return {k: f"{type(e).__name__}: {e}"[:120] for (k, _), e in zip(jobs, res) if isinstance(e, Exception)}


async def _bingx_symbol(s, get, sym, now, cfg):
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
        d["interval_h"] or inst.interval_h, time.time(), _skew.get("BingX", 0.0), bids, asks, inst.lot,
        inst.min_qty, inst.min_notional, _taker("BingX", inst, cfg))


async def _refresh_bingx(s, get, cfg, now):
    await _sync_time(s, get, "BingX", now)
    if _due("info_t", "BingX", INFO_TTL, now) or any(("BingX", x) not in _instr for x in cfg["symbols"]):
        for sym, inst in parse_bingx_contracts(await get(s, f"{BINGX}/openApi/swap/v2/quote/contracts"),
                                               cfg["symbols"]).items():
            _instr[("BingX", sym)] = inst
        _meta["info_t"]["BingX"] = now
    live = [x for x in cfg["symbols"] if _instr[("BingX", x)].active]
    for x in cfg["symbols"]:
        if x not in live:
            _quotes.pop(("BingX", x), None)
    res = await asyncio.gather(*(_bingx_symbol(s, get, x, now, cfg) for x in live), return_exceptions=True)
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
