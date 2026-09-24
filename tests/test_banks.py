import p2p
from helpers import make_ad


def groups(*ads):
    """Собрать snap.groups так же, как это делает scan(): по (ex, side, asset), отсортировано по цене."""
    g = {}
    for a in ads:
        g.setdefault((a.ex, a.side, a.asset), []).append(a)
    for key, grp in g.items():
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
    return g


def test_counts_ads_and_sums_volume_per_bank():
    g = groups(
        make_ad("MEXC", "buy", 90.0, pays=("T-Bank",), max_amt=50000, avail=1000),
        make_ad("MEXC", "buy", 90.5, pays=("T-Bank",), max_amt=30000, avail=1000),
        make_ad("MEXC", "buy", 91.0, pays=("Sberbank",), max_amt=20000, avail=1000),
    )
    liq = p2p.bank_liquidity(g, "MEXC", "USDT")
    assert liq["buy"]["T-Bank"] == (2, 80000.0)
    assert liq["buy"]["Sberbank"] == (1, 20000.0)
    assert "sell" not in liq


def test_volume_capped_by_available_balance():
    a = make_ad("MEXC", "sell", 92.0, pays=("Sberbank",), max_amt=500000, avail=100)
    liq = p2p.bank_liquidity(groups(a), "MEXC", "USDT")
    assert liq["sell"]["Sberbank"] == (1, 100 * 92.0)   # avail*price=9200 меньше max_amt=500000


def test_multiple_pay_methods_counted_under_each_bank():
    g = groups(make_ad("MEXC", "buy", 90.0, pays=("T-Bank", "Sberbank"), max_amt=10000, avail=1000))
    liq = p2p.bank_liquidity(g, "MEXC", "USDT")
    assert liq["buy"]["T-Bank"] == (1, 10000.0)
    assert liq["buy"]["Sberbank"] == (1, 10000.0)


def test_missing_exchange_or_asset_returns_empty():
    assert p2p.bank_liquidity({}, "MEXC", "USDT") == {}
    g = groups(make_ad("MEXC", "buy", 90.0))
    assert p2p.bank_liquidity(g, "Bybit", "USDT") == {}
