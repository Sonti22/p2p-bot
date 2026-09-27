"""Заглушки публичного API перпов для тестов: GET по URL отдаёт урезанные живые ответы из fixtures/perp_api.json;
синтетические котировки для симуляций. Сеть не нужна."""
import json
import os
import re

import perp

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "perp_api.json")


def api():
    with open(FIX, encoding="utf-8") as f:
        return json.load(f)


def route(url):
    sym = (re.search(r"symbol=([A-Za-z-]+)", url) or [None, ""])[1]
    if "api.bybit.com" in url:
        if "/v5/market/time" in url:
            return "bybit_time"
        for part, key in (("instruments-info", "instr"), ("tickers", "ticker"), ("kline", "kline")):
            if part in url:
                return f"bybit_{key}_{sym}"
        if "orderbook" in url:
            return f"bybit_{'spot' if 'category=spot' in url else 'book'}_{sym}"
    if "open-api.bingx.com" in url:
        if "server/time" in url:
            return "bingx_time"
        if "quote/contracts" in url:
            return "bingx_contracts"
        if "premiumIndex" in url:
            return f"bingx_premium_{sym}"
        if "quote/depth" in url:
            return f"bingx_depth_{sym}"
    raise AssertionError(f"unexpected URL in test: {url}")


def make_get(overrides=None, calls=None, fail=()):
    """get(s, url) для perp.refresh: overrides — {ключ фикстуры: ответ}; fail — подстроки URL, на которых ошибка."""
    data = api()
    data.update(overrides or {})

    async def get(s, url):
        if calls is not None:
            calls.append(url)
        if any(part in url for part in fail):
            raise ConnectionError(f"boom {url}")
        return data[route(url)]
    return get


def book(mid, spread, size, levels=5, step=None):
    """Стакан вокруг mid: levels уровней по size монеты, шаг step (по умолчанию = spread)."""
    step = step or spread
    bids = tuple((mid - spread / 2 - i * step, size) for i in range(levels))
    asks = tuple((mid + spread / 2 + i * step, size) for i in range(levels))
    return bids, asks


def quote(venue="Bybit", symbol="BTCUSDT", mid=84000.0, spread=1.0, size=1.0, rate=0.0001, next_funding=None,
          ts=None, lot=0.001, min_qty=0.001, fee=0.055, interval_h=8.0, kind="perp", mark=None, skew=0.0, now=None):
    now = 1790494000.0 if now is None else now
    bids, asks = book(mid, spread, size)
    return perp.PerpQuote(venue, symbol, mark or mid, mid, mid, bids[0][0], asks[0][0], rate,
                          now + 3600 if next_funding is None else next_funding, interval_h,
                          now if ts is None else ts, skew, bids, asks, lot, min_qty, 5.0, fee, kind)


def install(*quotes, spot=()):
    """Положить котировки в состояние perp (как после refresh)."""
    for q in quotes:
        perp._quotes[(q.venue, q.symbol)] = q
    for q in spot:
        perp._spot[(q.venue, q.symbol)] = q
