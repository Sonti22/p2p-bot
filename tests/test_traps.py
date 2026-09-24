import p2p
from helpers import make_ad


def cfg(**kw):
    c = p2p.Config()
    c.risk_buffer, c.pay_fee = {}, 0.0
    c.max_dev = 4.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_attractive_buy_deviation_is_a_trap():
    # покупка втрое дешевле рынка — типичная приманка
    ad = make_ad(ex="Bybit", side="buy", price=60.0, asset="USDT")
    trap = p2p._trap_entry(ad, ref=90.0, cfg=cfg())
    assert trap is not None
    assert trap["ex"] == "Bybit" and trap["side"] == "buy"
    assert "купить" in trap["reason"] and "ниже рынка" in trap["reason"]


def test_attractive_sell_deviation_is_a_trap():
    # продажа сильно дороже рынка — тоже приманка
    ad = make_ad(ex="MEXC", side="sell", price=120.0, asset="USDT")
    trap = p2p._trap_entry(ad, ref=90.0, cfg=cfg())
    assert trap is not None
    assert "продать" in trap["reason"] and "выше рынка" in trap["reason"]


def test_unattractive_deviation_is_not_a_trap():
    # покупка дороже рынка или продажа дешевле — невыгодно нам, никого не заманит
    assert p2p._trap_entry(make_ad(side="buy", price=120.0), ref=90.0, cfg=cfg()) is None
    assert p2p._trap_entry(make_ad(side="sell", price=60.0), ref=90.0, cfg=cfg()) is None


def test_traps_log_keeps_recent_first_and_capped():
    p2p.TRAPS_LOG.clear()
    c = cfg()
    for price in (50.0, 40.0, 30.0):
        trap = p2p._trap_entry(make_ad(side="buy", price=price), ref=90.0, cfg=c)
        p2p.TRAPS_LOG.append(trap)
    rows = p2p.traps_log()
    assert [r["price"] for r in rows] == [30.0, 40.0, 50.0]

    p2p.TRAPS_LOG.clear()
    for i in range(p2p.TRAPS_LOG_SIZE + 5):
        p2p.TRAPS_LOG.append(p2p._trap_entry(make_ad(side="buy", price=10.0 + i), ref=90.0, cfg=c))
    assert len(p2p.traps_log()) == p2p.TRAPS_LOG_SIZE
