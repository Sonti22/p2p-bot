"""Public spot depth and instrument constraints for virtual execution only."""
import asyncio
import re
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext

import p2p

REQUEST_TIMEOUT = 5


def number(value):
    n = Decimal(str(value))
    if not n.is_finite() or n < 0:
        raise ValueError("Invalid nonnegative market value")
    return n


def step(digits):
    count = int(digits)
    if not 0 <= count <= 18:
        raise ValueError("Unsupported instrument precision")
    return Decimal(1).scaleb(-count)


def urls(venue, asset):
    if not re.fullmatch(r"[A-Z0-9]{1,20}", asset) or asset == "USDT":
        raise ValueError("Invalid spot asset")
    if venue == "Bybit":
        return (f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={asset}USDT&limit=200",
                f"https://api.bybit.com/v5/market/instruments-info?category=spot&symbol={asset}USDT")
    if venue == "MEXC":
        return (f"https://api.mexc.com/api/v3/depth?symbol={asset}USDT&limit=200",
                f"https://api.mexc.com/api/v3/exchangeInfo?symbol={asset}USDT")
    if venue == "HTX":
        return (f"https://api.htx.com/market/depth?symbol={asset.lower()}usdt&type=step0&depth=20",
                "https://api.htx.com/v1/common/symbols")
    if venue == "KuCoin":
        return (f"https://api.kucoin.com/api/v1/market/orderbook/level2_100?symbol={asset}-USDT",
                f"https://api.kucoin.com/api/v2/symbols/{asset}-USDT")
    raise ValueError("Unsupported spot venue")


def normalize(venue, asset, depth, instrument, received):
    """Fail closed on schema changes, inactive symbols or invalid/crossed books."""
    base = asset + "USDT"
    if venue == "Bybit":
        if depth.get("retCode") != 0 or instrument.get("retCode") != 0:
            raise ValueError("Bybit public data error")
        book = depth["result"]
        if book["s"] != base:
            raise ValueError("Wrong book symbol")
        info = next(x for x in instrument["result"]["list"] if x["symbol"] == base)
        lot = info["lotSizeFilter"]
        rules = {"base_step": lot["basePrecision"], "quote_step": lot["quotePrecision"],
                 "min_base": "0", "min_quote": lot["minOrderAmt"],
                 "max_base": lot.get("maxMarketOrderQty"), "max_quote": None,
                 "buy": info["status"] == "Trading", "sell": info["status"] == "Trading"}
        bids, asks, stamp, sequence = book["b"], book["a"], book["ts"] / 1000, book["u"]
    elif venue == "MEXC":
        info = next(x for x in instrument["symbols"] if x["symbol"] == base)
        active = str(info["status"]) in ("1", "TRADING", "ENABLED") and info["isSpotTradingAllowed"] is True
        side = str(info.get("tradeSideType", "1"))
        rules = {"base_step": str(step(info["baseAssetPrecision"])),
                 "quote_step": str(step(info.get("quoteAssetPrecision", info["quotePrecision"]))),
                 "min_base": info["baseSizePrecision"],
                 "min_quote": info.get("quoteAmountPrecisionMarket", info["quoteAmountPrecision"]),
                 "max_base": None, "max_quote": info.get("maxQuoteAmountMarket", info.get("maxQuoteAmount")),
                 "buy": active and side in ("1", "2"), "sell": active and side in ("1", "3")}
        bids, asks, stamp, sequence = depth["bids"], depth["asks"], received, depth["lastUpdateId"]
    elif venue == "HTX":
        if depth["status"] != "ok" or instrument["status"] != "ok":
            raise ValueError("HTX public data error")
        info = next(x for x in instrument["data"] if x["symbol"] == base.lower())
        active = info["state"] == "online" and info.get("api-trading", "enabled") == "enabled"
        rules = {"base_step": str(step(info["amount-precision"])), "quote_step": str(step(info["value-precision"])),
                 "min_base": info["sell-market-min-order-amt"], "min_quote": info["min-order-value"],
                 "max_base": info.get("sell-market-max-order-amt"), "max_quote": info.get("buy-market-max-order-value"),
                 "buy": active, "sell": active}
        book = depth["tick"]
        bids, asks, stamp = book["bids"], book["asks"], depth["ts"] / 1000
        sequence = book.get("version", book.get("ts", depth["ts"]))
    elif venue == "KuCoin":
        if depth["code"] != "200000" or instrument["code"] != "200000":
            raise ValueError("KuCoin public data error")
        info = instrument["data"]
        if info["symbol"] != asset + "-USDT":
            raise ValueError("Wrong instrument symbol")
        rules = {"base_step": info["baseIncrement"], "quote_step": info["quoteIncrement"],
                 "min_base": info["baseMinSize"], "min_quote": info["minFunds"],
                 "max_base": info.get("baseMaxSize"), "max_quote": info.get("quoteMaxSize"),
                 "buy": info["enableTrading"] is True, "sell": info["enableTrading"] is True}
        book = depth["data"]
        bids, asks, stamp, sequence = book["bids"], book["asks"], book["time"] / 1000, book["sequence"]
    else:
        raise ValueError("Unknown venue")
    for key in ("base_step", "quote_step", "min_base", "min_quote", "max_base", "max_quote"):
        if rules[key] is not None:
            rules[key] = str(number(rules[key]))
    if number(rules["base_step"]) <= 0 or number(rules["quote_step"]) <= 0:
        raise ValueError("Zero instrument increment")
    def levels(values, reverse):
        out = {}
        for price, qty, *_ in values:
            price, qty = number(price), number(qty)
            if price <= 0 or qty <= 0:
                raise ValueError("Invalid book level")
            if price in out:
                raise ValueError("Duplicate price level")
            out[price] = qty
        return [[str(p), str(out[p])] for p in sorted(out, reverse=reverse)]
    bids, asks = levels(bids, True), levels(asks, False)
    if not bids or not asks or number(bids[0][0]) >= number(asks[0][0]):
        raise ValueError("Empty or crossed spot book")
    if not received - 15 <= stamp <= received + 2:
        raise ValueError("Stale exchange book")
    return {"venue": venue, "asset": asset, "id": str(sequence), "received": received, "ts": stamp,
            "bids": bids, "asks": asks, "rules": rules}


async def load(session, pairs):
    """Bounded public-only requests; only pairs required by active paper legs."""
    semaphore = asyncio.Semaphore(3)
    async def fetch(pair):
        venue, asset = pair
        async with semaphore:
            addresses = urls(venue, asset)
            result = await asyncio.gather(*(p2p._json(session, "GET", url) for url in addresses), return_exceptions=True)
            for response in result:
                if isinstance(response, Exception):
                    raise response
            return normalize(venue, asset, *result, time.time())
    async def one(pair):
        try:
            return pair, await asyncio.wait_for(fetch(pair), REQUEST_TIMEOUT), None
        except Exception as error:
            return pair, None, type(error).__name__
    result = await asyncio.gather(*(one(pair) for pair in sorted(set(pairs))))
    return ({key: book for key, book, error in result if book is not None},
            {key: error for key, book, error in result if error is not None})


def level_key(book, side, price):
    return "spot:" + ":".join((book["venue"], book["asset"], book["id"], side, str(price)))


def fill(book, source, amount, used):
    """FOK-style depth model. Return exact consumed source and leave rounding dust."""
    with localcontext() as ctx:
        ctx.prec = 40
        return _fill(book, source, number(amount), used)


def _fill(book, source, amount, used):
    rules = book["rules"]
    buying = source == "USDT"
    if source not in ("USDT", book["asset"]) or not rules["buy" if buying else "sell"]:
        return None
    base_step, quote_step = number(rules["base_step"]), number(rules["quote_step"])
    side = "asks" if buying else "bids"
    levels = [(number(p), max(Decimal(0), number(q) - number(used.get(level_key(book, side, p), 0))))
              for p, q in book[side]]
    def floor(value, increment):
        return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    if buying:
        budget = floor(amount, quote_step)
        remaining, possible = budget, Decimal(0)
        for price, available in levels:
            take = min(available, remaining / price)
            possible += take
            remaining -= take * price
        # Insufficient depth cannot be presented as a whole-order execution.
        if remaining > Decimal("0.000000000000000001"):
            return None
        target = floor(possible, base_step)
    else:
        target = floor(amount, base_step)
    if target <= 0 or ((not buying or book["venue"] != "HTX") and target < number(rules["min_base"])):
        return None
    if rules["max_base"] is not None and target > number(rules["max_base"]):
        return None
    remaining, quote, takes = target, Decimal(0), []
    for price, available in levels:
        take = min(remaining, available)
        if take:
            quote += take * price
            takes.append((level_key(book, side, price), str(take), str(price)))
            remaining -= take
    if remaining > 0:
        return None
    quote = (quote / quote_step).to_integral_value(rounding=ROUND_UP if buying else ROUND_DOWN) * quote_step
    if quote < number(rules["min_quote"]) or (rules["max_quote"] is not None and quote > number(rules["max_quote"])):
        return None
    spent, gross = (quote, target) if buying else (target, quote)
    if spent > amount:
        return None
    return {"spent": str(spent), "gross": str(gross), "base": str(target), "quote": str(quote),
            "takes": takes, "average": str(quote / target), "rules": rules}
