import asyncio
import dataclasses

import p2p
from helpers import make_ad


def test_parse_amount_plain_and_spaces():
    assert p2p.parse_amount("20000") == 20000
    assert p2p.parse_amount("20 000") == 20000
    assert p2p.parse_amount(" 100 000 ") == 100000


def test_parse_amount_k_suffix():
    assert p2p.parse_amount("20к") == 20000
    assert p2p.parse_amount("20k") == 20000
    assert p2p.parse_amount("1.5к") == 1500


def test_parse_amount_million_suffix():
    assert p2p.parse_amount("1,5 млн") == 1_500_000
    assert p2p.parse_amount("1.5млн") == 1_500_000
    assert p2p.parse_amount("2m") == 2_000_000


def test_parse_amount_rejects_garbage():
    assert p2p.parse_amount("") is None
    assert p2p.parse_amount("много") is None
    assert p2p.parse_amount("20 тыс тыс") is None
    assert p2p.parse_amount("-5000") is None


def test_parse_amount_rejects_out_of_range():
    assert p2p.parse_amount("500") is None           # ниже 1 000
    assert p2p.parse_amount("10 млн") is None         # выше 5 000 000


def test_scan_amount_limits_ad_usability(offline, monkeypatch):
    async def fake_fetch(s, cfg, side, asset):
        return [make_ad("Fake", side, 85.0 if side == "buy" else 90.0, pays=("T-Bank",),
                        min_amt=15000, max_amt=25000)]

    monkeypatch.setitem(p2p.FETCHERS, "fake", fake_fetch)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0, amount=50000)
    snap = asyncio.run(p2p.scan(None, c))
    assert not snap.deals                          # 50 000 ₽ вне лимита объявления 15–25 тыс.

    c2 = dataclasses.replace(c, amount=20000)
    snap2 = asyncio.run(p2p.scan(None, c2, force_alt=True))
    assert snap2.deals                              # 20 000 ₽ внутри лимита


def test_scan_force_alt_refetches_without_touching_shared_cache(offline, monkeypatch):
    calls = []

    async def fake_fetch(s, cfg, side, asset):
        calls.append((asset, cfg.amount))
        return []

    monkeypatch.setitem(p2p.FETCHERS, "fake", fake_fetch)
    c = p2p.Config(exchanges=["fake"], assets=["USDT", "ETH"], min_orders=0, min_rate=0, amount=50000)
    asyncio.run(p2p.scan(None, c))
    assert [v for a, v in calls if a == "ETH"] == [50000, 50000]
    cached_ads, cached_t = p2p._alt["ads"], p2p._alt["t"]

    c2 = dataclasses.replace(c, amount=20000)
    asyncio.run(p2p.scan(None, c2, force_alt=True))
    assert [v for a, v in calls if a == "ETH"][-2:] == [20000, 20000]   # свежий запрос под новую сумму
    assert p2p._alt["ads"] is cached_ads and p2p._alt["t"] == cached_t   # общий кэш не тронут
