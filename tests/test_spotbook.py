import copy
import json
from decimal import Decimal

import pytest

import p2p
import portfolio as pf
import spotbook as sb
from helpers import arun
from test_portfolio import ad, snapshot


def book(now=1000, increment="0.01", asks=None):
    return {"venue": "Bybit", "asset": "ETH", "id": "7", "ts": now, "received": now,
            "bids": [["19", "100"]], "asks": asks or [["20", "100"]],
            "rules": {"base_step": increment, "quote_step": "0.01", "min_base": "0.01",
                      "min_quote": "1", "max_base": "1000", "max_quote": "10000", "buy": True, "sell": True}}


def start_spot(path, monkeypatch, amount=10000, asset="USDT"):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "0")
    monkeypatch.setenv("PAPER_SPOT_MODEL", "depth")
    b, s = ad("buy", asset=asset), ad("sell", asset="ETH" if asset == "USDT" else "USDT")
    hops = {"hops": [{"frm": "Bybit", "to": "Bybit", "asset": b.asset, "fee": 0},
                     {"frm": "Bybit", "to": "Bybit", "asset": s.asset, "fee": 0}]}
    pf.start(amount, b, s, hops, 1, path=path, now=1000, spot_fees={"Bybit": 0})
    cfg = p2p.Config()
    pf.tick(snapshot(b), cfg, path, now=1000)
    pf.tick(snapshot(), cfg, path, now=1001)
    pf.tick(snapshot(), cfg, path, now=1002)
    return cfg


def test_multi_level_buy_does_not_use_best_ask_for_whole_amount():
    b = book(asks=[["20", "2"], ["30", "100"]])
    result = sb.fill(b, "USDT", "100", {})
    assert Decimal(result["base"]) == 4
    assert Decimal(result["spent"]) == 100
    assert result["average"] == "25"


def test_multi_level_sell_uses_actual_depth():
    b = book()
    b["bids"] = [["19", "2"], ["18", "100"]]
    result = sb.fill(b, "ETH", "5", {})
    assert Decimal(result["gross"]) == 92


def test_depth_shortfall_is_not_partial_or_ticker_fill():
    b = book(asks=[["20", "2"]])
    assert sb.fill(b, "USDT", "100", {}) is None
    assert sb.fill(b, "ETH", "200", {}) is None
    assert sb.fill(book(asks=[["20", "4.999"]]), "USDT", "100", {}) is None


def test_market_minimum_maximum_and_direction():
    b = book()
    b["rules"]["min_quote"] = "10"
    assert sb.fill(b, "USDT", "5", {}) is None
    b["rules"]["max_base"] = "1"
    assert sb.fill(b, "USDT", "100", {}) is None
    b["rules"]["buy"] = True
    b["rules"]["min_base"] = "10"
    assert sb.fill(b, "USDT", "100", {}) is None
    b["rules"]["max_base"] = "1000"
    b["rules"]["buy"] = False
    assert sb.fill(b, "USDT", "100", {}) is None


def test_cached_book_levels_are_not_reused():
    b = book(asks=[["20", "5"]])
    used = {sb.level_key(b, "asks", "20"): "4"}
    assert sb.fill(b, "USDT", "100", used) is None


def test_rounding_remainder_retains_cost_and_can_be_sold(tmp_path, monkeypatch):
    path = str(tmp_path / "wallet.db")
    cfg = start_spot(path, monkeypatch)
    b = book(now=1003, increment="3")
    pf.tick(snapshot(), cfg, path, now=1003, books={("Bybit", "ETH"): b})
    r = pf.runs(path)[0]
    assert pf.dec(r["qty"]) == 3 and r["asset"] == "ETH"
    assert pf.dec(r["cost"]) == 6000
    assert pf.dec(r["dust"][0]["qty"]) == 40
    assert pf.dec(r["dust"][0]["cost"]) == 4000
    pf.tick(snapshot(ad("sell", 110, ts=1004)), cfg, path, now=1004)
    r = pf.runs(path)[0]
    assert pf.dec(r["dust"][0]["qty"]) == 0
    assert pf.dec(r["realized"]) == 400
    assert pf.dec(pf.replay(path)["cash"]) == pf.dec(pf.summary(path)["cash"])


def test_missing_depth_retains_coins_despite_ticker(tmp_path, monkeypatch):
    path = str(tmp_path / "wallet.db")
    cfg = start_spot(path, monkeypatch)
    pf.tick(snapshot(spot={"Bybit": {"ETH": (19, 20)}}), cfg, path, now=1003)
    assert pf.runs(path)[0]["asset"] == "USDT"
    assert pf.dec(pf.runs(path)[0]["qty"]) == 100


def test_stale_book_does_not_execute(tmp_path, monkeypatch):
    path = str(tmp_path / "wallet.db")
    cfg = start_spot(path, monkeypatch)
    pf.tick(snapshot(), cfg, path, now=1003, books={("Bybit", "ETH"): book(900)})
    assert pf.runs(path)[0]["asset"] == "USDT"


def test_spot_execution_and_consumption_rollback_together(tmp_path, monkeypatch):
    path = str(tmp_path / "wallet.db")
    cfg = start_spot(path, monkeypatch)
    before = pf.runs(path)
    monkeypatch.setattr(pf, "_mark", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        pf.tick(snapshot(), cfg, path, now=1003, books={("Bybit", "ETH"): book(1003)})
    assert pf.runs(path) == before
    con = pf.connect(path)
    assert not con.execute("SELECT * FROM consumed WHERE key LIKE 'spot:%'").fetchall()
    con.close()


def responses(venue):
    bids, asks = [["19", "100"]], [["20", "100"]]
    if venue == "Bybit":
        return ({"retCode": 0, "result": {"s": "ETHUSDT", "b": bids, "a": asks, "ts": 1000000, "u": 7}},
                {"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT", "status": "Trading", "lotSizeFilter": {
                    "basePrecision": "0.01", "quotePrecision": "0.01", "minOrderAmt": "1", "maxMarketOrderQty": "1000"}}]}})
    if venue == "MEXC":
        return ({"bids": bids, "asks": asks, "lastUpdateId": 7}, {"symbols": [{"symbol": "ETHUSDT", "status": "1",
                 "isSpotTradingAllowed": True, "baseAssetPrecision": 2, "quotePrecision": 2,
                 "baseSizePrecision": "0.01", "quoteAmountPrecision": "1"}]})
    if venue == "HTX":
        return ({"status": "ok", "ts": 1000000, "tick": {"bids": bids, "asks": asks, "version": 7}},
                {"status": "ok", "data": [{"symbol": "ethusdt", "state": "online", "amount-precision": 2,
                "value-precision": 2, "min-order-value": "1", "sell-market-min-order-amt": "0.01"}]})
    return ({"code": "200000", "data": {"bids": bids, "asks": asks, "time": 1000000, "sequence": "7"}},
            {"code": "200000", "data": {"symbol": "ETH-USDT", "baseIncrement": "0.01", "quoteIncrement": "0.01",
             "baseMinSize": "0.01", "minFunds": "1", "enableTrading": True}})


@pytest.mark.parametrize("venue", p2p.SPOT_VENUES)
def test_public_adapters_and_exact_allowlist(venue):
    depth, instrument = responses(venue)
    b = sb.normalize(venue, "ETH", depth, instrument, 1000)
    assert b["asset"] == "ETH" and sb.fill(b, "USDT", "100", {}) is not None
    assert all(p2p.json_allowed("GET", url) for url in sb.urls(venue, "ETH"))


@pytest.mark.parametrize("asset", ["../orders", "ETH&category=linear", "USDT", "", "эфир"])
def test_symbol_validation_prevents_url_injection(asset):
    with pytest.raises(ValueError):
        sb.urls("Bybit", asset)


@pytest.mark.parametrize("bad", [[["0", "1"]], [["NaN", "1"]], [["21", "1"]], [["19", "1"], ["19", "2"]]])
def test_invalid_books_are_rejected(bad):
    depth, instrument = responses("Bybit")
    depth["result"]["b"] = bad
    with pytest.raises(ValueError):
        sb.normalize("Bybit", "ETH", depth, instrument, 1000)


def test_async_loader_reads_only_requested_public_pair(monkeypatch):
    calls = []
    depth, instrument = responses("Bybit")
    async def fake(session, method, url, body=None):
        calls.append((method, url, body))
        return depth if "orderbook?" in url else instrument
    monkeypatch.setattr(p2p, "_json", fake)
    monkeypatch.setattr(sb.time, "time", lambda: 1000)
    loaded, errors = arun(sb.load(None, [("Bybit", "ETH"), ("Bybit", "ETH")]))
    assert not errors and set(loaded) == {("Bybit", "ETH")}
    assert len(calls) == 2 and all(m == "GET" and body is None for m, _, body in calls)


def test_async_loader_failure_never_fabricates_a_book(monkeypatch):
    async def fail(*args, **kwargs):
        raise ValueError("schema changed")
    monkeypatch.setattr(p2p, "_json", fail)
    loaded, errors = arun(sb.load(None, [("Bybit", "ETH")]))
    assert loaded == {} and errors == {("Bybit", "ETH"): "ValueError"}


def test_queued_pairs_share_bounded_timeout(monkeypatch):
    import asyncio
    import time
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(p2p, "_json", slow)
    monkeypatch.setattr(sb, "REQUEST_TIMEOUT", 0.01)
    started = time.monotonic()
    pairs = [("Bybit", a) for a in ("BTC", "ETH", "SOL", "XRP", "TON", "DOGE")]
    books, errors = arun(sb.load(None, pairs))
    assert not books and len(errors) == 6 and set(errors.values()) == {"TimeoutError"}
    assert time.monotonic() - started < 1


def test_bot_passes_required_public_book_to_portfolio(monkeypatch):
    import time
    from test_bot import Stub
    monkeypatch.setenv("PAPER_ENGINE", "ledger")
    cfg = start_spot(pf.DB_PATH, monkeypatch)
    calls = []
    async def loaded(session, pairs):
        calls.append(pairs)
        return {("Bybit", "ETH"): book(time.time())}, {}
    monkeypatch.setattr(sb, "load", loaded)
    arun(Stub(cfg).process_paper_cycles(snapshot()))
    assert calls == [{("Bybit", "ETH")}]
    assert pf.runs()[0]["asset"] == "ETH"
    assert pf.dec(pf.runs()[0]["qty"]) == 5
