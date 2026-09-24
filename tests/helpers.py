import p2p


def make_ad(ex="Bybit", side="buy", price=85.0, asset="USDT", pays=("T-Bank",), orders=200, rate=100.0,
            net="", url="", min_amt=1000, max_amt=500000, avail=10000, terms=""):
    return p2p.Ad(ex, side, price, min_amt, max_amt, avail, list(pays), "nick", orders, rate, url, asset, net, terms)
