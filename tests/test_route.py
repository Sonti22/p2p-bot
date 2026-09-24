import asyncio

import pytest

import p2p
import trades
from helpers import make_ad

SPOT = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2500.0, 2501.0), "USDC": (0.9999, 1.0)},
        "MEXC": {"USDT": (1.0, 1.0), "ETH": (2499.0, 2500.0)}}


def cfg(**kw):
    c = p2p.Config()
    c.risk_buffer, c.pay_fee = {}, 0.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_same_venue_no_transfer():
    profit, route = p2p._route(make_ad("MEXC", "buy", 88.0), make_ad("MEXC", "sell", 89.76), cfg(), SPOT)
    assert route == "внутри биржи"
    assert profit == pytest.approx((89.76 / 88 - 1) * 100)


def test_transfer_uses_cheapest_network():
    profit, route = p2p._route(make_ad("MEXC", "buy", 88.0), make_ad("Bybit", "sell", 90.0), cfg(), SPOT)
    assert "BEP20" in route and "−0.01 USDT" in route
    assert profit == pytest.approx(((50000 / 88 - 0.01) * 90 / 50000 - 1) * 100)


def test_bitpapa_receives_only_trc20():
    _, route = p2p._route(make_ad("MEXC", "buy", 88.0), make_ad("BitPapa", "sell", 94.0), cfg(), SPOT)
    assert "TRC20" in route and "−1 USDT" in route


def test_exchanger_network_is_respected():
    _, route = p2p._route(make_ad("Bybit", "buy", 85.0), make_ad("BestChange", "sell", 89.9, net="ERC20"), cfg(), SPOT)
    assert "ERC20" in route and "−0.8 USDT" in route


def test_spot_on_buy_venue_and_risk_buffer():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("MEXC", "sell", 245000.0, asset="ETH")
    profit, route = p2p._route(b, s, cfg(risk_buffer={"ETH": 0.5}), SPOT)
    assert "спот USDT→ETH на MEXC" in route and "перевод" not in route and "запас на курс −0.5%" in route
    qty = 50000 / 88 / 2500.0 * (1 - 0.001) * (1 - 0.005)
    assert profit == pytest.approx((qty * 245000 / 50000 - 1) * 100)


def test_two_conversions_rejected():
    b, s = make_ad("MEXC", "buy", 7_000_000, asset="BTC"), make_ad("MEXC", "sell", 245000, asset="ETH")
    assert p2p._route(b, s, cfg(), SPOT) is None


def test_pay_fee_applied():
    profit, route = p2p._route(make_ad("MEXC", "buy", 88.0), make_ad("MEXC", "sell", 88.0), cfg(pay_fee=0.5), SPOT)
    assert profit == pytest.approx(-0.5) and "банка" in route


def test_pay_fee_auto_applied_when_bank_over_limit():
    b, s = make_ad("MEXC", "buy", 88.0, pays=("T-Bank",)), make_ad("MEXC", "sell", 88.0)
    profit, route = p2p._route(b, s, cfg(), SPOT, over_banks={"T-Bank"})
    assert profit == pytest.approx(-trades.SBP_OVER_FEE)
    assert "лимит СБП T-Bank исчерпан" in route


def test_pay_fee_auto_skipped_when_bank_under_limit():
    b, s = make_ad("MEXC", "buy", 88.0, pays=("T-Bank",)), make_ad("MEXC", "sell", 88.0)
    profit, route = p2p._route(b, s, cfg(), SPOT, over_banks=set())
    assert profit == pytest.approx(0.0)
    assert "комиссия банка" not in route


def test_manual_pay_fee_not_overridden_by_auto():
    b, s = make_ad("MEXC", "buy", 88.0, pays=("T-Bank",)), make_ad("MEXC", "sell", 88.0)
    profit, route = p2p._route(b, s, cfg(pay_fee=1.0), SPOT, over_banks={"T-Bank"})
    assert profit == pytest.approx(-1.0)
    assert "исчерпан" not in route


def test_usable_filters():
    c = p2p.Config()
    assert p2p.usable(make_ad(pays=("T-Bank",)), c)
    assert not p2p.usable(make_ad(pays=("Mobile Top-up",)), c)
    assert not p2p.usable(make_ad(pays=("реквизиты в чат",)), c)
    assert not p2p.usable(make_ad(orders=10), c)
    assert not p2p.usable(make_ad(min_amt=60000), c)
    assert not p2p.usable(make_ad(avail=1), c)


def test_scan_offline_keeps_prices_near_reference(offline):
    c = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert snap.ref > 0 and not snap.errors
    for a in snap.best.values():
        assert abs(a.price / snap.ref - 1) * 100 <= c.max_dev
    assert all(d[0] == max(x[0] for x in snap.deals) for d in snap.deals[:1])   # отсортировано по убыванию


def test_stack_combines_several_ads_by_price():
    ads = [make_ad(price=85.0, min_amt=1000, max_amt=20000, avail=20000 / 85.0),
           make_ad(price=86.0, min_amt=1000, max_amt=40000, avail=40000 / 86.0)]
    stacked = p2p._stack(ads, 50000)
    assert stacked is not None
    assert "2 объявл." in stacked.nick
    qty = 20000 / 85.0 + 30000 / 86.0
    assert stacked.price == pytest.approx(50000 / qty)


def test_stack_single_ad_matches_original_price():
    ads = [make_ad(price=85.0, min_amt=1000, max_amt=500000, avail=10000)]
    stacked = p2p._stack(ads, 50000)
    assert stacked.price == pytest.approx(85.0) and stacked.nick == "nick"


def test_stack_skips_ad_below_its_minimum_for_remainder():
    ads = [make_ad(price=85.0, min_amt=1000, max_amt=45000, avail=45000 / 85.0),
           make_ad(price=86.0, min_amt=10000, max_amt=40000, avail=40000 / 86.0)]
    assert p2p._stack(ads, 50000) is None   # остаток 5000 меньше min_amt второго объявления


def test_stack_returns_none_when_depth_insufficient():
    ads = [make_ad(price=85.0, min_amt=1000, max_amt=20000, avail=20000 / 85.0)]
    assert p2p._stack(ads, 50000) is None


def test_scan_combines_depth_across_ads_to_form_deal(offline, monkeypatch):
    async def fake_fetcher(s, cfg, side, asset):
        if side == "buy":
            return [make_ad("Fake", "buy", 85.0, min_amt=1000, max_amt=20000, avail=20000 / 85.0),
                    make_ad("Fake", "buy", 86.0, min_amt=1000, max_amt=30000, avail=30000 / 86.0)]
        return [make_ad("Fake", "sell", 90.0, min_amt=1000, max_amt=500000, avail=10000)]

    monkeypatch.setitem(p2p.FETCHERS, "fake", fake_fetcher)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert snap.deals
    qty = 20000 / 85.0 + 30000 / 86.0
    buy_price = 50000 / qty
    assert snap.deals[0][1].price == pytest.approx(buy_price)


def test_scan_drops_deal_when_depth_does_not_cover_amount(offline, monkeypatch):
    async def fake_fetcher(s, cfg, side, asset):
        if side == "buy":
            return [make_ad("Fake", "buy", 85.0, min_amt=1000, max_amt=20000, avail=20000 / 85.0)]
        return [make_ad("Fake", "sell", 90.0, min_amt=1000, max_amt=500000, avail=10000)]

    monkeypatch.setitem(p2p.FETCHERS, "fake", fake_fetcher)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert not snap.deals   # 20 000 доступного объёма не хватает на круг в 50 000


def test_deal_amounts_recomputes_profit_for_other_sums():
    buy_ads = [make_ad("MEXC", "buy", 85.0, min_amt=1000, max_amt=20000, avail=20000 / 85.0)]
    sell_ads = [make_ad("MEXC", "sell", 90.0, min_amt=1000, max_amt=500000, avail=10000)]
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, spot=SPOT,
                        groups={("MEXC", "buy", "USDT"): buy_ads, ("MEXC", "sell", "USDT"): sell_ads})
    deal = (5.0, buy_ads[0], sell_ads[0], "внутри биржи")
    out = p2p.deal_amounts(deal, cfg(), snap, amounts=(10_000, 50_000))
    assert out[10_000] == pytest.approx((90.0 / 85.0 - 1) * 100)   # прибыль не зависит от суммы на одной цене
    assert out[50_000] is None   # у объявления на покупку максимум 20 000 — на 50 000 глубины не хватает


def test_deal_amounts_missing_group_is_none():
    empty = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})
    deal = (5.0, make_ad("MEXC", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "внутри биржи")
    out = p2p.deal_amounts(deal, cfg(), empty, amounts=(50_000,))
    assert out == {50_000: None}


def test_scan_applies_auto_fee_for_bank_over_limit(offline, monkeypatch):
    async def fake_fetcher(s, cfg, side, asset):
        return [make_ad("Fake", side, 85.0 if side == "buy" else 90.0, pays=("T-Bank",))]

    monkeypatch.setitem(p2p.FETCHERS, "fake", fake_fetcher)
    monkeypatch.setattr(trades, "bank_month_total",
                        lambda bank, path=trades.DB_PATH, now=None: 150000.0 if bank == "T-Bank" else 0.0)
    c = p2p.Config(exchanges=["fake"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert snap.deals
    assert "лимит СБП T-Bank исчерпан" in snap.deals[0][3]
