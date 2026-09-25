"""Премия цен объявлений к ориентиру в подписи карточки и новые флаги условий мерчанта."""
import p2p


def ad(ex, side, price, asset="USDT", terms=""):
    return p2p.Ad(ex, side, price, 1000, 500000, 10000, ["SBP"], "m", 1000, 100.0, "", asset, "", terms)


def test_premium_line_in_caption():
    s = p2p.Snapshot(88.0, "Rapira USDT/RUB", {"USDT": 88.0}, {}, [], {}, {}, {})
    d = (3.0, ad("Bybit", "buy", 86.24), ad("MEXC", "sell", 89.76), "перевод на MEXC")
    line = p2p.premium_line(d[1], d[2], s)
    assert line == "К ориентиру (Rapira USDT/RUB): покупка -2.0%, продажа +2.0%"
    assert line in p2p.fmt_deal(d, p2p.Config(), s)


def test_no_premium_without_reference():
    s = p2p.Snapshot(0, "-", {}, {}, [], {}, {}, {})
    d = (3.0, ad("Bybit", "buy", 86.0), ad("MEXC", "sell", 89.0), "r")
    assert p2p.premium_line(d[1], d[2], s) == "" and "К ориентиру" not in p2p.fmt_deal(d, p2p.Config(), s)


def test_new_terms_flags():
    assert "просит селфи/фото документов" in p2p.terms_flags("Перед сделкой пришлите селфи с паспортом")[1]
    assert "просит селфи/фото документов" in p2p.terms_flags("нужно фото документа")[1]
    assert "нужен скриншот оплаты" in p2p.terms_flags("после оплаты скрин в чат")[1]
    assert p2p.terms_flags("быстро, без комментариев") == ([], [])
