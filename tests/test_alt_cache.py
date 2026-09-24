import asyncio
import dataclasses

import p2p
from helpers import make_ad


def _fetch_for(venue, calls, assets=("USDT", "ETH"), fail=()):
    async def fetch(s, cfg, side, asset):
        calls.append((venue, side, asset))
        if asset in fail:
            raise RuntimeError("boom")
        if asset not in assets:
            return []
        price = {"USDT": (85.0, 90.0), "ETH": (200_000.0, 230_000.0), "BTC": (7_000_000.0, 7_100_000.0)}[asset]
        return [make_ad(venue, side, price[0 if side == "buy" else 1], asset=asset, pays=("T-Bank",))]
    return fetch


def _cfg(**kw):
    base = dict(exchanges=["fake"], assets=["USDT", "ETH"], min_orders=0, min_rate=0, amount=50000, alt_interval=60)
    base.update(kw)
    return p2p.Config(**base)


def _assets_in_deals(snap):
    return {a.asset for _, b, s, _ in snap.deals for a in (b, s)}


def _venues_in_deals(snap):
    return {a.ex for _, b, s, _ in snap.deals for a in (b, s)}


def test_last_alt_disabled_drops_cache(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls))
    c = _cfg()
    assert "ETH" in _assets_in_deals(asyncio.run(p2p.scan(None, c)))
    c.assets.remove("ETH")   # как bot._toggle / пресет «USDT без переводов»
    snap = asyncio.run(p2p.scan(None, c))
    assert "ETH" not in _assets_in_deals(snap)
    assert not [k for k in snap.groups if k[2] == "ETH"]
    assert not p2p._alt["ads"] and not p2p._alt["errors"]


def test_stale_alt_errors_cleared(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls, fail=("ETH",)))
    c = _cfg()
    assert "fake/ETH" in asyncio.run(p2p.scan(None, c)).errors
    c.assets.remove("ETH")
    p2p._venue_backoff.clear()   # пауза площадки после ошибки — отдельная тема
    assert "fake/ETH" not in asyncio.run(p2p.scan(None, c)).errors


def test_disabled_venue_refetched_immediately(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls))
    monkeypatch.setitem(p2p.FETCHERS, "fake2", _fetch_for("Fake2", calls))
    c = _cfg(exchanges=["fake", "fake2"])
    assert "Fake2" in _venues_in_deals(asyncio.run(p2p.scan(None, c)))
    c.exchanges.remove("fake2")
    calls.clear()
    snap = asyncio.run(p2p.scan(None, c))
    assert ("Fake", "buy", "ETH") in calls             # ETH переопрошен сразу, не через alt_interval
    assert "Fake2" not in _venues_in_deals(snap)
    assert not [k for k in snap.groups if k[0] == "Fake2"]


def test_enabling_alt_polls_immediately(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls, assets=("USDT", "ETH", "BTC")))
    c = _cfg()
    asyncio.run(p2p.scan(None, c))
    c.assets.append("BTC")
    calls.clear()
    asyncio.run(p2p.scan(None, c))
    assert ("Fake", "buy", "BTC") in calls


def test_backoff_pause_does_not_reset_cache(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls))
    monkeypatch.setitem(p2p.FETCHERS, "fake2", _fetch_for("Fake2", calls, fail=("USDT", "ETH")))
    c = _cfg(exchanges=["fake", "fake2"])
    asyncio.run(p2p.scan(None, c))                      # fake2 упал → пауза
    t1 = p2p._alt["t"]
    calls.clear()
    snap = asyncio.run(p2p.scan(None, c))               # fake2 на паузе, но в настройках остался
    assert "fake2" in snap.errors and not [x for x in calls if x[0] == "Fake2"]
    assert p2p._alt["t"] == t1                          # кэш не сброшен
    assert not [x for x in calls if x[2] == "ETH"]      # и ETH не переопрошен внутри alt_interval


def test_force_alt_does_not_touch_cache_key(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls))
    c = _cfg()
    asyncio.run(p2p.scan(None, c))
    key, ads = p2p._alt["key"], p2p._alt["ads"]
    asyncio.run(p2p.scan(None, dataclasses.replace(c, assets=["USDT"], amount=20000), force_alt=True))
    assert p2p._alt["key"] == key and p2p._alt["ads"] is ads
    calls.clear()
    asyncio.run(p2p.scan(None, c))                      # обычный скан после /calc: кэш не пересобирается
    assert not [x for x in calls if x[2] == "ETH"]


def test_amount_change_refetches_alts(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fetch_for("Fake", calls))
    c = _cfg()
    asyncio.run(p2p.scan(None, c))
    calls.clear()
    asyncio.run(p2p.scan(None, c))
    assert not [x for x in calls if x[2] == "ETH"]      # внутри alt_interval — из кэша
    c.amount = 100000                                   # /amount или пресет: площадки отдают объявления под сумму
    asyncio.run(p2p.scan(None, c))
    assert ("Fake", "buy", "ETH") in calls
