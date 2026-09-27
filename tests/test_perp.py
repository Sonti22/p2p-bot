"""perp.py: разбор публичных ответов Bybit/BingX, опрос с бэкоффом, стакан, лоты, точный учёт фандинга."""
import asyncio
import inspect

import pytest

import p2p
import perp
from perpfx import api, install, make_get, quote

NOW = 1790494000.0


@pytest.fixture(autouse=True)
def _clean_perp():
    perp.reset()
    yield
    perp.reset()


def test_parse_bybit_ticker_and_instrument():
    j = api()
    d = perp.parse_bybit_ticker(j["bybit_ticker_BTCUSDT"], "BTCUSDT")
    assert d["mark"] == 84477.45 and d["index"] == 84515.90 and d["last"] == 84477.20
    assert d["bid"] == 84477.10 and d["ask"] == 84477.20
    assert d["funding_rate"] == pytest.approx(0.00002264) and d["next_funding"] == 1790496000.0
    assert d["interval_h"] == 8
    inst = perp.parse_bybit_instrument(j["bybit_instr_BTCUSDT"], "BTCUSDT")
    assert inst.active and inst.lot == 0.001 and inst.min_qty == 0.001 and inst.min_notional == 5
    assert inst.interval_h == 8
    ton = perp.parse_bybit_instrument(j["bybit_instr_TONUSDT"], "TONUSDT")
    assert not ton.active and "Closed" in ton.note
    empty = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": []}}
    assert not perp.parse_bybit_instrument(empty, "TONUSDT").active


def test_parse_bybit_rejects_error_and_spot_category():
    with pytest.raises(ValueError):
        perp.parse_bybit_ticker({"retCode": 10001, "retMsg": "params error"}, "BTCUSDT")
    spot = {"retCode": 0, "result": {"category": "spot", "list": [{"symbol": "BTCUSDT", "bid1Price": "1"}]}}
    with pytest.raises(ValueError):   # спот-тикер вместо линейного — не выдавать за перп
        perp.parse_bybit_ticker(spot, "BTCUSDT")


def test_parse_bingx_contracts_premium_depth():
    j = api()
    inst = perp.parse_bingx_contracts(j["bingx_contracts"], ["BTCUSDT", "ETHUSDT", "TONUSDT"])
    assert inst["BTCUSDT"].active and inst["BTCUSDT"].lot == 0.0001 and inst["BTCUSDT"].min_notional == 2
    assert inst["BTCUSDT"].taker_fee == pytest.approx(0.05)
    assert not inst["TONUSDT"].active and "нет" in inst["TONUSDT"].note
    d = perp.parse_bingx_premium(j["bingx_premium_BTC-USDT"])
    assert d["mark"] == 84474.5 and d["funding_rate"] == pytest.approx(0.000024) and d["interval_h"] == 8
    bids, asks = perp.parse_bingx_depth(j["bingx_depth_BTC-USDT"])
    assert bids[0] == (84474.5, 0.001)                     # количество в монете (bidsCoin), не контракты
    assert [p for p, _ in asks] == sorted(p for p, _ in asks)   # аски по возрастанию, даже если пришли вразнобой
    with pytest.raises(ValueError):
        perp.parse_bingx_premium(j["bingx_premium_missing"])


def test_walk_and_lots():
    levels = ((100.0, 1.0), (101.0, 2.0))
    assert perp.walk(levels, 0.5) == 100.0
    assert perp.walk(levels, 2.0) == pytest.approx((100 + 101) / 2)
    assert perp.walk(levels, 3.5) is None                 # глубины не хватает
    assert perp.round_lot(0.00142, 0.001, 0.001) == 0.001
    assert perp.round_lot(0.00162, 0.001, 0.001) == 0.002
    assert perp.round_lot(0.0004, 0.001, 0.001) == 0.0     # меньше минимального лота
    assert perp.floor_lot(0.0199, 0.01, 0.01) == 0.01


def test_refresh_builds_quotes_and_skips_closed_symbols():
    calls = []
    errors = asyncio.run(perp.refresh(None, make_get(calls=calls), now=NOW))
    assert errors == {}
    q = perp._quotes[("Bybit", "BTCUSDT")]
    assert q.mark == 84477.45 and q.bid == 84477.10 and q.ask == 84477.20 and q.lot == 0.001
    assert q.taker_fee == pytest.approx(0.055) and q.next_funding == 1790496000.0
    bx = perp._quotes[("BingX", "BTCUSDT")]
    assert bx.funding_rate == pytest.approx(0.000024) and bx.lot == 0.0001 and bx.taker_fee == pytest.approx(0.05)
    assert bx.bids[0] == (84474.5, 0.001)
    assert ("Bybit", "TONUSDT") not in perp._quotes and ("BingX", "TONUSDT") not in perp._quotes
    assert not any("TON" in u and ("tickers" in u or "premiumIndex" in u) for u in calls)   # закрытый не опрашиваем
    assert perp._spot[("Bybit", "BTCUSDT")].kind == "spot"
    assert perp.klines("Bybit", "BTCUSDT")[0][0] < perp.klines("Bybit", "BTCUSDT")[-1][0]
    assert all(u.split("/")[2] in perp.HOSTS for u in calls)
    assert "TONUSDT" in str(perp.status()["closed"])


def test_backoff_per_venue_and_recovery():
    errors = asyncio.run(perp.refresh(None, make_get(fail=("open-api.bingx.com",)), now=NOW))
    assert "BingX" in errors and ("Bybit", "BTCUSDT") in perp._quotes
    assert perp._backoff["BingX"]["delay"] == perp.BACKOFF_BASE
    calls = []
    asyncio.run(perp.refresh(None, make_get(calls=calls), now=NOW + 1))   # BingX на паузе — не опрашиваем
    assert not any("bingx" in u for u in calls)
    assert "BingX" in perp.status(now=NOW + 1)["paused"]
    asyncio.run(perp.refresh(None, make_get(), now=NOW + perp.BACKOFF_BASE + 1))
    assert "BingX" not in perp._backoff and ("BingX", "BTCUSDT") in perp._quotes


def test_refresh_if_due_interval_and_switch(monkeypatch):
    calls = []
    assert asyncio.run(perp.refresh_if_due(None, make_get(calls=calls), now=NOW)) == {}
    n = len(calls)
    assert asyncio.run(perp.refresh_if_due(None, make_get(calls=calls), now=NOW + 5)) is None
    assert len(calls) == n
    monkeypatch.setenv("PERPS", "0")
    assert asyncio.run(perp.refresh_if_due(None, make_get(calls=calls), now=NOW + 100)) is None


def test_get_refuses_other_hosts():
    with pytest.raises(ValueError):
        asyncio.run(perp._get(None, "https://api.mexc.com/api/v3/time"))   # домен разрешён guard, но не perp


def test_quote_freshness():
    install(quote(ts=NOW))
    assert perp.quote("Bybit", "BTCUSDT", now=NOW + 10) is not None
    assert perp.quote("Bybit", "BTCUSDT", now=NOW + 1000) is None
    assert perp.quote("BingX", "BTCUSDT", now=NOW) is None


def test_settle_uses_rate_seen_before_settlement():
    t = NOW + 600
    st = {}
    assert perp.settle(st, quote(rate=0.0001, mark=84000, next_funding=t, ts=NOW), now=NOW) == []
    assert perp.settle(st, quote(rate=0.0002, mark=84100, next_funding=t, ts=NOW + 300), now=NOW + 300) == []
    after = quote(rate=-0.0005, mark=85000, next_funding=t + 8 * 3600, ts=t + 20)   # после расчёта — новая ставка
    ev = perp.settle(st, after, now=t + 20)
    assert ev == [(t, 0.0002, 84100, False)]            # списана ставка из последней котировки до расчёта
    assert st["next"] == t + 8 * 3600 and st["rate"] == -0.0005
    stale = quote(rate=0.0009, mark=1.0, next_funding=t, ts=t - 5)   # старая котировка про прошедший расчёт
    assert perp.settle(st, stale, now=t + 30) == [] and st["rate"] == -0.0005


def test_settle_missed_windows_are_approx():
    t = NOW + 60
    st = {}
    perp.settle(st, quote(rate=0.0001, mark=100.0, next_funding=t, ts=NOW), now=NOW)
    ev = perp.settle(st, None, now=t + 17 * 3600)   # бот стоял 17 ч: три расчёта
    assert [e[0] for e in ev] == [t, t + 8 * 3600, t + 16 * 3600]
    assert [e[3] for e in ev] == [False, True, True]


def test_funding_windows():
    q = quote(next_funding=NOW + 3600)
    assert perp.funding_windows(q, NOW, 0.5) == 0
    assert perp.funding_windows(q, NOW, 2) == 1
    assert perp.funding_windows(q, NOW, 17) == 3


def test_scan_attaches_perps(offline):
    install(quote())
    snap = asyncio.run(p2p.scan(None, p2p.Config(assets=["USDT"], exchanges=["bybit"])))
    assert ("Bybit", "BTCUSDT") in snap.perps and snap.perps[("Bybit", "BTCUSDT")].mid == 84000.0


def test_public_get_only():
    src = inspect.getsource(perp)
    assert ".post(" not in src and "order/create" not in src and "X-BAPI-SIGN" not in src
