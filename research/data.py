"""Публичная рыночная история Bybit v5 и BingX swap для бэктестов: свечи 1 ч, история фандинга, спецификации.

Только GET без ключей и подписей. Постраничная загрузка окнами на фиксированной сетке (повторный запуск
берёт те же адреса из кеша), повторы с паузой при сетевых сбоях, вежливый интервал между запросами.
Кеш — `%TEMP%/p2p_research_cache` (вне репозитория): закрытые окна хранятся всегда, текущие — час.

Пути (официальная документация):
  Bybit  GET /v5/market/kline                 category, symbol, interval, start, end, limit ≤ 1000 (новые сверху)
         GET /v5/market/funding/history       category, symbol, startTime, endTime, limit ≤ 200 (новые сверху)
         GET /v5/market/instruments-info      category, symbol
  BingX  GET /openApi/swap/v3/quote/klines    symbol, interval, startTime, endTime, limit ≤ 1000
         GET /openApi/swap/v2/quote/fundingRate  symbol, startTime, endTime, limit ≤ 1000 (есть markPrice)
         GET /openApi/swap/v2/quote/contracts

Toncoin в 2026 переименован в GRAM: перп TONUSDT на Bybit закрыт 15.06.2026, GRAMUSDT торгуется с 22.06.2026;
спот GRAMUSDT хранит всю историю TON. На BingX старого TON-USDT в справочнике нет, GRAMTON-USDT — с 03.07.2026.
"""
import datetime
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BYBIT = "https://api.bybit.com"
BINGX = "https://open-api.bingx.com"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
CACHE_DIR = os.path.join(tempfile.gettempdir(), "p2p_research_cache")
LIVE_TTL = 3600            # с, срок кеша для окна, которое ещё не закрылось
USER_AGENT = "p2p-research/1.0 (public market data, backtests)"

KLINE_LIMIT = 1000
BYBIT_FUNDING_LIMIT = 200
BINGX_FUNDING_LIMIT = 1000
FUNDING_WINDOW_MS = 60 * DAY_MS   # окно истории фандинга; не влезло в лимит — делим пополам

COINS = ("BTC", "ETH", "TON")
BYBIT_SPOT = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "TON": "GRAMUSDT"}
BYBIT_PERP = {"BTC": ("BTCUSDT",), "ETH": ("ETHUSDT",), "TON": ("TONUSDT", "GRAMUSDT")}
BINGX_PERP = {"BTC": ("BTC-USDT",), "ETH": ("ETH-USDT",), "TON": ("GRAMTON-USDT",)}

# коды «слишком часто» — такие ответы повторяем, прочие ошибки площадки сразу наверх
BYBIT_RATE_CODES = {10006, 10018}
BINGX_RATE_CODES = {100410, 100429}


class DataError(RuntimeError):
    pass


def _opener():
    """Прокси — только из переменных окружения. Системный прокси Windows из реестра не берём: urllib не умеет
    SOCKS, а там бывает socks-адрес VPN-клиента, который отвечает отказом (так же поступает launcher.py)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler(urllib.request.getproxies_environment()))


def urllib_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with _opener().open(req, timeout=timeout) as r:
        return r.read()


def _bybit_check(d):
    code = d.get("retCode") if isinstance(d, dict) else None
    if code == 0:
        return None
    return ("retry" if code in BYBIT_RATE_CODES else "fail"), f"bybit retCode={code} {d.get('retMsg') if isinstance(d, dict) else d}"


def _bingx_check(d):
    code = d.get("code") if isinstance(d, dict) else None
    if code == 0:
        return None
    return ("retry" if code in BINGX_RATE_CODES else "fail"), f"bingx code={code} {d.get('msg') if isinstance(d, dict) else d}"


class Http:
    """GET JSON с кешем на диске, паузой между запросами и повторами. `getter(url, timeout) -> bytes` подменяется
    в тестах; `offline=True` — только кеш (нет в кеше — DataError)."""

    def __init__(self, cache_dir=CACHE_DIR, throttle=0.35, retries=4, getter=None, offline=False,
                 clock=time.time, sleep=time.sleep, backoff=2.0):
        self.cache_dir, self.throttle, self.retries = cache_dir, throttle, retries
        self.getter = getter or urllib_get
        self.offline, self.clock, self.sleep, self.backoff = offline, clock, sleep, backoff
        self._last = 0.0
        self.requests = 0      # сколько реально ушло в сеть
        self.cache_hits = 0

    def _path(self, url):
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".json")

    def get_json(self, base, path, params, final=True, check=None, ttl=LIVE_TTL):
        url = base + path + "?" + urllib.parse.urlencode(sorted(params.items()))
        cpath = self._path(url)
        if os.path.exists(cpath):
            try:
                with open(cpath, encoding="utf-8") as f:
                    cached = json.load(f)
                if cached.get("final") or self.clock() - cached.get("fetched", 0) < ttl or self.offline:
                    self.cache_hits += 1
                    return cached["data"]
            except (OSError, ValueError, KeyError):
                pass
        if self.offline:
            raise DataError(f"нет в кеше (offline): {url}")
        err = None
        for attempt in range(self.retries + 1):
            wait = self.throttle - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = self.clock()
            self.requests += 1
            try:
                data = json.loads(self.getter(url, 20))
            except urllib.error.HTTPError as e:
                err = f"HTTP {e.code}: {url}"
                if e.code not in (408, 418, 429) and e.code < 500:
                    raise DataError(err) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
                err = f"{type(e).__name__}: {e}: {url}"
            else:
                bad = check(data) if check else None
                if bad is None:
                    self._store(cpath, url, data, final)
                    return data
                kind, err = bad
                if kind != "retry":
                    raise DataError(err)
            if attempt < self.retries:
                self.sleep(min(30.0, self.backoff * 2 ** attempt))
        raise DataError(f"не удалось после {self.retries + 1} попыток: {err}")

    def _store(self, cpath, url, data, final):
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = cpath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"url": url, "fetched": self.clock(), "final": bool(final), "data": data}, f)
        os.replace(tmp, cpath)


def _now_ms(http):
    return int(http.clock() * 1000)


def _grid(start_ms, end_ms, span):
    """Окна [ws, we] (we включительно) на сетке кратной span от эпохи, покрывающие [start_ms, end_ms)."""
    ws = start_ms - start_ms % span
    while ws < end_ms:
        yield ws, ws + span - 1
        ws += span


def _closed_bar(ts, now_ms):
    return ts + HOUR_MS <= now_ms


def bybit_klines(http, category, symbol, start_ms, end_ms):
    """Свечи 1 ч Bybit [start_ms, end_ms) по возрастанию: [ts, open, high, low, close, volume]; только закрытые."""
    now = _now_ms(http)
    out = {}
    for ws, we in _grid(start_ms, end_ms, KLINE_LIMIT * HOUR_MS):
        d = http.get_json(BYBIT, "/v5/market/kline",
                          {"category": category, "symbol": symbol, "interval": "60", "start": ws, "end": we,
                           "limit": KLINE_LIMIT}, final=we + HOUR_MS < now, check=_bybit_check)
        for row in d["result"].get("list") or []:
            ts = int(row[0])
            if start_ms <= ts < end_ms and _closed_bar(ts, now):
                out[ts] = [ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
    return [out[k] for k in sorted(out)]


def _split_windows(fetch, ws, we, limit, now, min_span=HOUR_MS):
    """fetch(ws, we, final) -> список; упёрлись в лимит — окно делим пополам (детерминированно, для кеша)."""
    rows = fetch(ws, we, we < now - HOUR_MS)
    if len(rows) >= limit and we - ws > min_span:
        mid = ws + (we - ws + 1) // 2
        return (_split_windows(fetch, ws, mid - 1, limit, now, min_span)
                + _split_windows(fetch, mid, we, limit, now, min_span))
    return rows


def bybit_funding(http, symbol, start_ms, end_ms, category="linear"):
    """История фандинга Bybit [start_ms, end_ms): [[ts, rate]] по возрастанию (rate — доля, не %)."""
    now = _now_ms(http)

    def fetch(ws, we, final):
        d = http.get_json(BYBIT, "/v5/market/funding/history",
                          {"category": category, "symbol": symbol, "startTime": ws, "endTime": we,
                           "limit": BYBIT_FUNDING_LIMIT}, final=final, check=_bybit_check)
        return d["result"].get("list") or []

    out = {}
    for ws, we in _grid(start_ms, end_ms, FUNDING_WINDOW_MS):
        for r in _split_windows(fetch, ws, we, BYBIT_FUNDING_LIMIT, now):
            ts = int(r["fundingRateTimestamp"])
            if start_ms <= ts < end_ms:
                out[ts] = [ts, float(r["fundingRate"])]
    return [out[k] for k in sorted(out)]


def bybit_instrument(http, category, symbol):
    d = http.get_json(BYBIT, "/v5/market/instruments-info", {"category": category, "symbol": symbol},
                      final=False, check=_bybit_check, ttl=DAY_MS // 1000)
    lst = d["result"].get("list") or []
    return lst[0] if lst else None


def bingx_klines(http, symbol, start_ms, end_ms):
    """Свечи 1 ч BingX swap [start_ms, end_ms) по возрастанию, формат как у bybit_klines. История ~с 2025 года."""
    now = _now_ms(http)
    out = {}
    for ws, we in _grid(start_ms, end_ms, KLINE_LIMIT * HOUR_MS):
        d = http.get_json(BINGX, "/openApi/swap/v3/quote/klines",
                          {"symbol": symbol, "interval": "1h", "startTime": ws, "endTime": we, "limit": KLINE_LIMIT},
                          final=we + HOUR_MS < now, check=_bingx_check)
        for k in d.get("data") or []:
            ts = int(k["time"])
            if start_ms <= ts < end_ms and _closed_bar(ts, now):
                out[ts] = [ts, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]),
                           float(k.get("volume") or 0)]
    return [out[k] for k in sorted(out)]


def bingx_funding(http, symbol, start_ms, end_ms):
    """История фандинга BingX [start_ms, end_ms): [[ts, rate, mark_price]] по возрастанию."""
    now = _now_ms(http)

    def fetch(ws, we, final):
        d = http.get_json(BINGX, "/openApi/swap/v2/quote/fundingRate",
                          {"symbol": symbol, "startTime": ws, "endTime": we, "limit": BINGX_FUNDING_LIMIT},
                          final=final, check=_bingx_check)
        return d.get("data") or []

    out = {}
    for ws, we in _grid(start_ms, end_ms, FUNDING_WINDOW_MS):
        for r in _split_windows(fetch, ws, we, BINGX_FUNDING_LIMIT, now):
            ts = int(r["fundingTime"])
            if start_ms <= ts < end_ms:
                out[ts] = [ts, float(r["fundingRate"]), float(r.get("markPrice") or 0)]
    return [out[k] for k in sorted(out)]


def bingx_contracts(http):
    d = http.get_json(BINGX, "/openApi/swap/v2/quote/contracts", {}, final=False, check=_bingx_check,
                      ttl=DAY_MS // 1000)
    return {c["symbol"]: c for c in d.get("data") or []}


def _hour_ceil(ms):
    return -(-ms // HOUR_MS) * HOUR_MS


def _bybit_perp_spec(inst):
    lot = inst.get("lotSizeFilter") or {}
    return {"qty_step": float(lot.get("qtyStep") or 0), "min_qty": float(lot.get("minOrderQty") or 0),
            "min_notional": float(lot.get("minNotionalValue") or 0),
            "funding_interval_min": int(inst.get("fundingInterval") or 480), "status": inst.get("status"),
            "launch": int(inst.get("launchTime") or 0), "delivery": int(inst.get("deliveryTime") or 0)}


def _bingx_spec(c):
    return {"qty_step": float(c.get("size") or 0), "min_qty": float(c.get("tradeMinQuantity") or 0),
            "min_notional": float(c.get("tradeMinUSDT") or 0), "taker_fee": float(c.get("takerFeeRate") or 0),
            "launch": int(c.get("launchTime") or 0), "status": c.get("status")}


def load_dataset(http, start_ms, end_ms, coins=COINS, log=print):
    """Всё нужное бэктестам по монетам: спот Bybit, отрезки перпа Bybit и BingX (свечи, фандинг, спецификация).
    Отрезок — один символ в пределах [запуск, поставка] ∩ [start_ms, end_ms)."""
    end_ms = min(end_ms, _now_ms(http) // HOUR_MS * HOUR_MS)
    out = {"meta": {"start": start_ms, "end": end_ms, "fetched_at": _now_ms(http), "notes": []}, "coins": {}}
    contracts = None
    for coin in coins:
        log(f"{coin}: спот Bybit {BYBIT_SPOT[coin]}")
        spot = bybit_klines(http, "spot", BYBIT_SPOT[coin], start_ms, end_ms)
        c = {"spot": {"venue": "bybit", "symbol": BYBIT_SPOT[coin], "klines": spot}, "perp": [], "bingx": []}
        for sym in BYBIT_PERP[coin]:
            inst = bybit_instrument(http, "linear", sym)
            if not inst:
                out["meta"]["notes"].append(f"Bybit {sym}: нет в справочнике")
                continue
            spec = _bybit_perp_spec(inst)
            s = max(start_ms, _hour_ceil(spec["launch"]))
            e = min(end_ms, spec["delivery"]) if spec["delivery"] else end_ms
            if s >= e:
                continue
            log(f"{coin}: перп Bybit {sym} {_iso(s)}…{_iso(e)}")
            c["perp"].append({"venue": "bybit", "symbol": sym, "start": s, "end": e, "spec": spec,
                              "klines": bybit_klines(http, "linear", sym, s, e),
                              "funding": bybit_funding(http, sym, s, e)})
        for sym in BINGX_PERP[coin]:
            if contracts is None:
                contracts = bingx_contracts(http)
            con = contracts.get(sym)
            if not con:
                out["meta"]["notes"].append(f"BingX {sym}: нет в справочнике")
                continue
            spec = _bingx_spec(con)
            s = max(start_ms, _hour_ceil(spec["launch"]))
            if s >= end_ms:
                continue
            log(f"{coin}: перп BingX {sym} {_iso(s)}…{_iso(end_ms)}")
            c["bingx"].append({"venue": "bingx", "symbol": sym, "start": s, "end": end_ms, "spec": spec,
                               "klines": bingx_klines(http, sym, s, end_ms),
                               "funding": bingx_funding(http, sym, s, end_ms)})
        out["coins"][coin] = c
    return out


def _iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def series_coverage(rows, step_ms=HOUR_MS):
    """Покрытие ряда [ts, ...]: первая/последняя метка, число точек и пропущенных шагов (для свечей)."""
    if not rows:
        return {"n": 0}
    ts = [r[0] for r in rows]
    missing = sum(max(0, (b - a) // step_ms - 1) for a, b in zip(ts, ts[1:])) if step_ms else None
    return {"n": len(ts), "first": _iso(ts[0]), "last": _iso(ts[-1]),
            "days": round((ts[-1] - ts[0]) / DAY_MS, 1), "missing_steps": missing}


def coverage(dataset):
    """Сводка покрытия данных по монетам для отчёта."""
    cov = {}
    for coin, c in dataset["coins"].items():
        cov[coin] = {
            "spot": {"symbol": c["spot"]["symbol"], **series_coverage(c["spot"]["klines"])},
            "perp": [{"symbol": s["symbol"], "klines": series_coverage(s["klines"]),
                      "funding": series_coverage(s["funding"], None),
                      "funding_interval_min": s["spec"].get("funding_interval_min")} for s in c["perp"]],
            "bingx": [{"symbol": s["symbol"], "klines": series_coverage(s["klines"]),
                       "funding": series_coverage(s["funding"], None)} for s in c["bingx"]],
        }
    return cov
