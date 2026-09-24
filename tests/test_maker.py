import pytest

import p2p
from helpers import make_ad


def groups(*ads):
    """Собрать snap.groups так же, как это делает scan(): по (ex, side, asset), уже отсортировано по цене."""
    g = {}
    for a in ads:
        g.setdefault((a.ex, a.side, a.asset), []).append(a)
    for key, grp in g.items():
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
    return g


def test_sell_ad_undercuts_best_buy_group_by_tick():
    g = groups(make_ad("MEXC", "buy", 90.0), make_ad("MEXC", "buy", 90.5), make_ad("MEXC", "sell", 92.0))
    price, counter_price, spread = p2p.maker_quote(g, "MEXC", "USDT", "sell_ad")
    assert price == 90.0 - p2p.MAKER_TICK
    assert counter_price == 92.0
    assert spread == pytest.approx((92.0 - (90.0 - p2p.MAKER_TICK)) / 92.0 * 100)


def test_buy_ad_outbids_best_sell_group_by_tick():
    g = groups(make_ad("MEXC", "sell", 92.0), make_ad("MEXC", "sell", 91.5), make_ad("MEXC", "buy", 90.0))
    price, counter_price, spread = p2p.maker_quote(g, "MEXC", "USDT", "buy_ad")
    assert price == 92.0 + p2p.MAKER_TICK
    assert counter_price == 90.0
    assert spread == pytest.approx(((92.0 + p2p.MAKER_TICK) - 90.0) / 90.0 * 100)


def test_bybit_maker_fee_added_only_to_buy_ad():
    g = groups(make_ad("Bybit", "buy", 90.0), make_ad("Bybit", "sell", 92.0))
    _, _, sell_spread = p2p.maker_quote(g, "Bybit", "USDT", "sell_ad")
    _, _, buy_spread = p2p.maker_quote(g, "Bybit", "USDT", "buy_ad")
    mexc_g = groups(make_ad("MEXC", "buy", 90.0), make_ad("MEXC", "sell", 92.0))
    _, _, mexc_buy_spread = p2p.maker_quote(mexc_g, "MEXC", "USDT", "buy_ad")
    assert sell_spread == pytest.approx((92.0 - (90.0 - p2p.MAKER_TICK)) / 92.0 * 100)   # без комиссии мейкера
    assert buy_spread == pytest.approx(mexc_buy_spread + 0.3)   # +0.3% Bybit только на покупку


def test_missing_side_returns_none():
    g = groups(make_ad("MEXC", "buy", 90.0))
    assert p2p.maker_quote(g, "MEXC", "USDT", "sell_ad") is None
    assert p2p.maker_quote(g, "MEXC", "USDT", "buy_ad") is None
    assert p2p.maker_quote({}, "MEXC", "USDT", "sell_ad") is None


def test_unknown_post_side_raises():
    with pytest.raises(ValueError):
        p2p.maker_quote(groups(make_ad("MEXC", "buy", 90.0)), "MEXC", "USDT", "swap")
