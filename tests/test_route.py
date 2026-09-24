import asyncio

import pytest

import p2p
import trades
from conftest import load
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


def test_intermediate_coin_routes_via_usdt_on_one_venue():
    # ни BTC, ни ETH не совпадают, но на Bybit есть обе пары к USDT — маршрут BTC→USDT→ETH одной площадкой
    spot = dict(SPOT, Bybit={**SPOT["Bybit"], "BTC": (7_000_000.0, 7_001_000.0)})
    b, s = make_ad("Bybit", "buy", 7_000_000.0, asset="BTC"), make_ad("Bybit", "sell", 245000.0, asset="ETH")
    profit, route = p2p._route(b, s, cfg(), spot)
    assert "спот BTC→USDT на Bybit (−0.1%)" in route and "спот USDT→ETH на Bybit (−0.1%)" in route
    assert "перевод" not in route
    qty = 50000 / 7_000_000.0
    qty = qty * 7_000_000.0 * (1 - 0.001)   # BTC → USDT
    qty = (qty / 2501.0) * (1 - 0.001)      # USDT → ETH
    assert profit == pytest.approx((qty * 245000.0 / 50000 - 1) * 100)


def test_intermediate_coin_rejected_without_common_venue():
    # BTC есть только на Bybit, ETH — только на MEXC: ни одна площадка не держит обе пары
    spot = {"Bybit": {"USDT": (1.0, 1.0), "BTC": (7_000_000.0, 7_001_000.0)},
            "MEXC": {"USDT": (1.0, 1.0), "ETH": (2499.0, 2500.0)}}
    b, s = make_ad("Bybit", "buy", 7_000_000.0, asset="BTC"), make_ad("MEXC", "sell", 245000.0, asset="ETH")
    assert p2p._route(b, s, cfg(), spot) is None


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


def test_usable_and_signal_ok_respect_blacklist():
    c = p2p.Config()
    ad = make_ad()
    blocked = {(ad.ex, ad.nick)}
    assert not p2p.usable(ad, c, blocked)
    assert not p2p._signal_ok(ad, c, blocked)
    assert p2p.usable(ad, c, {("Bybit", "другой ник")})
    assert p2p._signal_ok(ad, c)


def test_scan_offline_keeps_prices_near_reference(offline):
    c = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert snap.ref > 0 and not snap.errors
    for a in snap.best.values():
        assert abs(a.price / snap.ref - 1) * 100 <= c.max_dev
    scores = [d[0] - c.risk_penalty * len(p2p.reliability(d, c, snap)[1]) for d in snap.deals]
    assert scores == sorted(scores, reverse=True)   # отсортировано по прибыли с поправкой на надёжность


def test_scan_drops_blacklisted_merchant(offline, monkeypatch):
    c = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0)
    before = asyncio.run(p2p.scan(None, c))
    best_nick = before.best[("Bybit", "buy", "USDT")].nick
    monkeypatch.setattr(p2p.blacklist, "blocked", lambda: {("Bybit", best_nick)})
    after = asyncio.run(p2p.scan(None, c))
    assert after.best[("Bybit", "buy", "USDT")].nick != best_nick   # лучшую цену давал именно он


def test_scan_removes_venue_entirely_when_all_merchants_blacklisted(offline, monkeypatch):
    c = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0)
    nicks = {i["nickName"] for i in load("bybit_ads.json")["result"]["items"]}
    monkeypatch.setattr(p2p.blacklist, "blocked", lambda: {("Bybit", n) for n in nicks})
    after = asyncio.run(p2p.scan(None, c))
    assert ("Bybit", "buy", "USDT") not in after.best
    assert ("Bybit", "sell", "USDT") not in after.best


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


def test_same_venue_only_filters_cross_exchange_deals(offline, monkeypatch):
    async def fetcher_a(s, cfg, side, asset):
        return [make_ad("A", side, 85.0 if side == "buy" else 90.0, min_amt=1000, max_amt=500000, avail=10000)]

    async def fetcher_b(s, cfg, side, asset):
        return [make_ad("B", side, 85.0 if side == "buy" else 95.0, min_amt=1000, max_amt=500000, avail=10000)]

    monkeypatch.setitem(p2p.FETCHERS, "a", fetcher_a)
    monkeypatch.setitem(p2p.FETCHERS, "b", fetcher_b)
    c = p2p.Config(exchanges=["a", "b"], assets=["USDT"], min_orders=0, min_rate=0)

    without_filter = asyncio.run(p2p.scan(None, c))
    assert any(b.ex != s.ex for _, b, s, _ in without_filter.deals)   # межбиржевые связки есть без фильтра

    c.same_venue_only = True
    with_filter = asyncio.run(p2p.scan(None, c))
    assert with_filter.deals
    assert all(b.ex == s.ex for _, b, s, _ in with_filter.deals)   # только внутри одной площадки


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


def test_reliability_clean_deal_is_reliable():
    b, s = make_ad("Bybit", "buy", 88.0, orders=500, rate=100.0), make_ad("MEXC", "sell", 89.0, orders=500, rate=100.0)
    snap = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [], {}, {}, {})
    deal = (1.13, b, s, "перевод −0.01 USDT (BEP20) на MEXC")
    label, reasons = p2p.reliability(deal, cfg(), snap)
    assert label == p2p.RELIABLE and reasons == []


def test_reliability_flags_price_deviation_and_weak_merchant():
    b = make_ad("Bybit", "buy", 88.0 * 1.03, orders=100, rate=95.0)   # ~3% от ориентира, порог фильтра по сделкам
    s = make_ad("MEXC", "sell", 89.0, orders=500, rate=100.0)
    snap = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [], {}, {}, {})
    deal = (1.13, b, s, "перевод −0.01 USDT (BEP20) на MEXC")
    label, reasons = p2p.reliability(deal, cfg(), snap)
    assert label == p2p.RISKY
    assert any("ориентира" in r for r in reasons) and any("порога фильтра" in r for r in reasons)


def test_reliability_flags_high_spread_as_reason():
    b, s = make_ad("Bybit", "buy", 85.0, orders=500, rate=100.0), make_ad("MEXC", "sell", 90.0, orders=500, rate=100.0)
    snap = p2p.Snapshot(88.0, "t", {"USDT": 87.0}, {}, [], {}, {}, {})
    deal = (5.88, b, s, "внутри биржи")
    label, reasons = p2p.reliability(deal, cfg(), snap)
    assert any("спред" in r for r in reasons) and label in (p2p.RISKY, p2p.TRAP)


def test_reliability_flags_volatile_coin_and_many_transfers():
    b = make_ad("MEXC", "buy", 88.0, asset="ETH", orders=500, rate=100.0)
    s = make_ad("Bybit", "sell", 89.0, asset="ETH", orders=500, rate=100.0)
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})
    route = "перевод −0.001 ETH (ERC20) на Bybit → спот ETH→USDT на Bybit (−0.1%) → перевод −1 USDT на MEXC"
    deal = (1.5, b, s, route)
    label, reasons = p2p.reliability(deal, cfg(risk_buffer={"ETH": 0.5}), snap)
    assert any("волатильная" in r for r in reasons) and any("перевода/конвертации" in r for r in reasons)
    assert label == p2p.RISKY   # 2 фактора риска — до ловушки (3+) не хватает


def test_fmt_deal_includes_reliability_when_snap_given():
    d = (1.13, make_ad("Bybit", "buy", 88.0, orders=500, rate=100.0), make_ad("MEXC", "sell", 89.0, orders=500, rate=100.0),
         "внутри биржи")
    snap = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [], {}, {}, {})
    text = p2p.fmt_deal(d, cfg(), snap)
    assert p2p.RELIABLE in text
    assert p2p.RELIABLE not in p2p.fmt_deal(d, cfg())   # без snap метка не считается


def test_scan_ranks_deals_by_profit_times_reliability(offline, monkeypatch):
    async def fake_r(s, cfg, side, asset):   # чистая связка: цена рядом с ориентиром (88.15), профит поменьше
        return [make_ad("R", side, 88.15 if side == "buy" else 89.5, orders=500, rate=100.0)]

    async def fake_k(s, cfg, side, asset):   # выше профит, но цена продажи у порога отсева (риск) + спред ≥5%
        return [make_ad("K", side, 87.14 if side == "buy" else 91.5, orders=500, rate=100.0)]

    monkeypatch.setitem(p2p.FETCHERS, "r", fake_r)
    monkeypatch.setitem(p2p.FETCHERS, "k", fake_k)
    c = p2p.Config(exchanges=["r", "k"], assets=["USDT"], min_orders=0, min_rate=0, risk_penalty=3.0)
    snap = asyncio.run(p2p.scan(None, c))
    rel = next(d for d in snap.deals if d[1].ex == "R" and d[2].ex == "R")
    risky = next(d for d in snap.deals if d[1].ex == "K" and d[2].ex == "K")
    assert risky[0] > rel[0]                                  # чистый профит выше у рискованной связки
    assert snap.deals.index(rel) < snap.deals.index(risky)    # но с поправкой на риск она позади


def test_profit_breakdown_same_asset_sums_to_net():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    profit, _ = p2p._route(b, s, cfg(), SPOT)
    out = p2p.profit_breakdown(b, s, cfg(), SPOT)
    labels = [label for label, _ in out]
    assert labels == ["Валовый спред", "− вывод", "− запас на курс", "Чистыми"]
    assert out[0][1] == pytest.approx((90.0 / 88.0 - 1) * 100)   # без издержек — чистый спред цен
    assert out[-1][1] == pytest.approx(profit)                   # последняя стадия равна итогу _route
    assert out[0][1] > out[1][1]                                 # вывод снижает профит
    assert out[1][1] == pytest.approx(out[-1][1])                # нет ни риск-буфера, ни комиссии банка


def test_profit_breakdown_cross_asset_has_spot_stage():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("MEXC", "sell", 245000.0, asset="ETH")
    profit, _ = p2p._route(b, s, cfg(risk_buffer={"ETH": 0.5}), SPOT)
    out = p2p.profit_breakdown(b, s, cfg(risk_buffer={"ETH": 0.5}), SPOT)
    labels = [label for label, _ in out]
    assert labels == ["Валовый спред", "− вывод", "− спот", "− запас на курс", "Чистыми"]
    assert out[-1][1] == pytest.approx(profit)


def test_profit_breakdown_bank_fee_lowers_final_stage_only():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("MEXC", "sell", 88.0)
    out = p2p.profit_breakdown(b, s, cfg(pay_fee=0.5), SPOT)
    assert out[-2][0] == "− запас на курс" and out[-2][1] == pytest.approx(0.0)   # без комиссии банка
    assert out[-1] == ("Чистыми", pytest.approx(-0.5))


def test_profit_breakdown_unroutable_pair_is_none():
    b = make_ad("MEXC", "buy", 7_000_000, asset="BTC")
    s = make_ad("MEXC", "sell", 245000, asset="ETH")
    assert p2p.profit_breakdown(b, s, cfg(), SPOT) is None


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


def test_spot_on_htx_and_kucoin_without_transfer():
    spot = dict(SPOT, HTX={"USDT": (1.0, 1.0), "ETH": (2490.0, 2491.0)}, KuCoin={"USDT": (1.0, 1.0), "ETH": (2495.0, 2496.0)})
    # монета куплена на HTX — спот там же, комиссия HTX 0.2%, переводов нет
    profit, route = p2p._route(make_ad("HTX", "buy", 88.0), make_ad("HTX", "sell", 245000.0, asset="ETH"), cfg(), spot)
    assert "спот USDT→ETH на HTX (−0.2%)" in route and "перевод" not in route
    assert profit == pytest.approx((50000 / 88 / 2491.0 * (1 - 0.002) * 245000 / 50000 - 1) * 100)
    # KuCoin — свой спот, комиссия 0.1%
    _, route = p2p._route(make_ad("KuCoin", "buy", 88.0), make_ad("KuCoin", "sell", 245000.0, asset="ETH"), cfg(), spot)
    assert "спот USDT→ETH на KuCoin (−0.1%)" in route and "перевод" not in route


def test_spot_fee_default_for_venue_missing_in_env():
    c = cfg(spot_fees={"Bybit": 0.1})          # в .env только Bybit — для HTX берётся встроенный дефолт
    assert p2p._spot_fee(c, "HTX") == pytest.approx(0.2)
    assert p2p._spot_fee(c, "Bybit") == pytest.approx(0.1)
