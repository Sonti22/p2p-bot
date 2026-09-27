"""Пороги мерчантов по площадкам (MERCHANT_MIN): разбор, фильтры usable/_signal_ok, причина «у порога», без MERCHANT_MIN — как раньше."""
import logging

import p2p
from helpers import arun, make_ad

VENUES = ("bybit", "htx", "kucoin", "mexc", "bitpapa", "lbank")


def test_parse_full_partial_and_case():
    assert p2p.parse_merchant_min(" bybit:200/97 , HTX:300 ,kucoin:/98, BestChange:50/90") == {
        "Bybit": (200, 97.0), "HTX": (300, None), "KuCoin": (None, 98.0), "BestChange": (50, 90.0)}
    assert p2p.parse_merchant_min("") == {} and p2p.parse_merchant_min(None) == {}


def test_parse_skips_invalid_parts_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="p2p"):
        got = p2p.parse_merchant_min("Bybit:200/97,foo:1/2,MEXC:abc,HTX,KuCoin:,BitPapa:nan/90,LBank:inf,"
                                     "MEXC:1/2/3,Bybit2:5")
    assert got == {"Bybit": (200, 97.0)}
    for bad in ("foo:1/2", "MEXC:abc", "HTX", "KuCoin:", "BitPapa:nan/90", "LBank:inf", "MEXC:1/2/3", "Bybit2:5"):
        assert any(bad in r.getMessage() for r in caplog.records), bad


def test_parse_clamps_ranges_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="p2p"):
        got = p2p.parse_merchant_min("Bybit:-5/150,HTX:1e9/-1,MEXC:100/97")
    assert got == {"Bybit": (0, 100.0), "HTX": (p2p.MERCHANT_ORDERS_MAX, 0.0), "MEXC": (100, 97.0)}
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "Bybit:-5/150" in msgs and "HTX:1e9/-1" in msgs and "MEXC:100/97" not in msgs


def test_suggested_defaults_parse_cleanly_and_never_soften_global(caplog):
    with caplog.at_level(logging.WARNING, logger="p2p"):
        got = p2p.parse_merchant_min(p2p.MERCHANT_MIN_SUGGESTED)
    assert not caplog.records and set(got) == {"Bybit", "HTX", "KuCoin", "MEXC", "BitPapa"}
    base = p2p.Config()
    assert all(o >= base.min_orders and r >= base.min_rate for o, r in got.values())


def test_from_env(monkeypatch):
    monkeypatch.delenv("MERCHANT_MIN", raising=False)
    assert p2p.Config.from_env().merchant_min == {}
    monkeypatch.setenv("MERCHANT_MIN", "Bybit:200/97,oops")
    assert p2p.Config.from_env().merchant_min == {"Bybit": (200, 97.0)}


def test_thresholds_fall_back_to_global():
    c = p2p.Config(min_orders=100, min_rate=95.0, merchant_min=p2p.parse_merchant_min("HTX:300,KuCoin:/98"))
    assert c.merchant_thresholds("HTX") == (300, 95.0)
    assert c.merchant_thresholds("KuCoin") == (100, 98.0)
    assert c.merchant_thresholds("Bybit") == (100, 95.0)


def test_filters_use_venue_thresholds():
    c = p2p.Config(merchant_min=p2p.parse_merchant_min("Bybit:200/97,MEXC:20"))
    weak_bybit = make_ad("Bybit", orders=150, rate=100.0)      # 150 ≥ общих 100, но < 200 Bybit
    htx = make_ad("HTX", orders=150, rate=100.0)               # HTX не задан — общий порог
    low_rate_bybit = make_ad("Bybit", orders=500, rate=96.0)   # 96% < 97% Bybit
    small_mexc = make_ad("MEXC", orders=30, rate=96.0)         # MEXC: 20 сделок, % общий 95
    for f in (p2p.usable, p2p._signal_ok):
        assert not f(weak_bybit, c) and not f(low_rate_bybit, c)
        assert f(htx, c) and f(small_mexc, c)
        assert not f(small_mexc, p2p.Config())                 # по общим 100/95 не прошёл бы


def _reasons(ad, c):
    other = make_ad("HTX", "sell", 86.0, orders=5000, rate=100.0)
    snap = p2p.Snapshot(85.0, "t", {"USDT": 85.0}, {}, [], {}, {}, {})
    return [r for _, r in p2p._risks((1.0, ad, other, "внутри биржи"), c, snap) if "у порога" in r]


def test_near_threshold_reason_uses_venue_thresholds():
    kucoin = make_ad("KuCoin", orders=1200, rate=100.0)
    assert not _reasons(kucoin, p2p.Config())                  # общий: 1200 ≥ 100 × 1.5
    tight = p2p.Config(merchant_min=p2p.parse_merchant_min("KuCoin:1000"))
    assert _reasons(kucoin, tight) == ["покупка: мерчант у порога фильтра (1200 сделок/100%)"]   # 1200 < 1500
    mexc = make_ad("MEXC", orders=40, rate=100.0)
    assert _reasons(mexc, p2p.Config())                         # общий: 40 < 150
    assert not _reasons(mexc, p2p.Config(merchant_min=p2p.parse_merchant_min("MEXC:20")))   # 40 ≥ 30
    assert _reasons(make_ad("HTX", orders=400, rate=97.5), p2p.Config(merchant_min={"HTX": (None, 97.0)}))


def _fixture_ads():
    c = p2p.Config()
    return [a for ex in VENUES for asset in ("USDT", "BTC", "ETH") for side in ("buy", "sell")
            for a in arun(p2p.FETCHERS[ex](None, c, side, asset))]


def test_default_unchanged_on_fixture_ads(offline):
    """Без MERCHANT_MIN фильтры и причина «у порога» решают ровно как прежняя формула по MIN_ORDERS/MIN_RATE."""
    ads = _fixture_ads()
    assert {a.ex for a in ads} >= {"Bybit", "HTX", "KuCoin", "MEXC", "BitPapa", "LBank"}
    for c in (p2p.Config(), p2p.Config(min_orders=50, min_rate=97.0, amount=10000)):
        for a in ads:
            old = a.orders >= c.min_orders and a.rate >= c.min_rate
            assert p2p._signal_ok(a, c) == (bool(p2p._pays(a, c)) and old and not p2p.terms_flags(a.terms)[0])
            assert p2p.usable(a, c) == (p2p._signal_ok(a, c) and a.min_amt <= c.amount <= a.max_amt
                                        and a.avail * a.price >= c.amount)
            near = a.orders < c.min_orders * 1.5 or a.rate < c.min_rate + 1
            assert bool(_reasons(a, c)) == near


def test_scan_drops_venue_below_its_threshold(offline):
    """Скан: порог одной площадки убирает только её мерчантов; остальные площадки — как без MERCHANT_MIN."""
    base = p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc"], assets=["USDT"])
    before = arun(p2p.scan(None, base))
    assert ("Bybit", "buy", "USDT") in before.best
    tight = p2p.Config(exchanges=base.exchanges, assets=["USDT"], merchant_min={"Bybit": (p2p.MERCHANT_ORDERS_MAX, None)})
    after = arun(p2p.scan(None, tight))
    assert not any(k[0] == "Bybit" for k in after.best)
    assert {k: a.nick for k, a in after.best.items()} == {k: a.nick for k, a in before.best.items() if k[0] != "Bybit"}
