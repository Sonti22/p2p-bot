"""Индекс надёжности 0–10 на карточке и риск «обменник → обменник» (круги сухого прогона 25.09: BestChange →
BestChange с планом 7–8% — выгодные курсы обменников часто с условиями и AML-заморозкой)."""
import cards
import p2p

PNG = b"\x89PNG\r\n\x1a\n"


def ad(ex, side, price, orders=1000, rate=100.0, terms=""):
    return p2p.Ad(ex, side, price, 1000, 500000, 10000, ["SBP"], f"{ex}-{side}", orders, rate, "", "USDT", "", terms)


def snap(refs=None):
    return p2p.Snapshot(88.0, "t", refs or {"USDT": 88.0}, {}, [], {}, {}, {})


def test_clean_deal_scores_ten():
    d = (2.5, ad("HTX", "buy", 87.8), ad("KuCoin", "sell", 90.0), "перевод −1 USDT (TRC20) на KuCoin")
    assert p2p.reliability(d, p2p.Config(), snap()) == (p2p.RELIABLE, [])
    assert p2p.reliability_index(d, p2p.Config(), snap()) == 10


def test_weights_lower_the_index():
    cfg = p2p.Config()
    # покупка на 3.9% ниже ориентира (≥80% отсева 4% — вес 2), мерчант у порога (1), спред ≥5% (2)
    d = (6.0, ad("Bybit", "buy", 84.6, orders=120), ad("MEXC", "sell", 89.0), "перевод на MEXC")
    label, reasons = p2p.reliability(d, cfg, snap())
    assert label == p2p.TRAP and len(reasons) == 3
    assert p2p.reliability_index(d, cfg, snap()) == 10 - (2 + 1 + 2)


def test_exchanger_to_exchanger_is_a_risk_and_three_reasons_make_a_trap():
    cfg = p2p.Config()
    b, s = ad("BestChange", "buy", 84.9), ad("BestChange", "sell", 91.88)
    d = (7.4, b, s, "перевод −1 USDT (TRC20) на BestChange")
    label, reasons = p2p.reliability(d, cfg, snap())
    assert any("обменник → обменник" in r for r in reasons)
    assert label == p2p.TRAP                                     # такие круги сухой прогон по умолчанию не берёт
    one_side = (3.0, ad("Bybit", "buy", 87.5), ad("BestChange", "sell", 90.3), "перевод на BestChange")
    assert not any("обменник → обменник" in r for r in p2p.reliability(one_side, cfg, snap())[1])


def test_caption_and_card_show_index():
    cfg = p2p.Config()
    d = (2.5, ad("HTX", "buy", 87.8), ad("KuCoin", "sell", 90.0), "перевод на KuCoin")
    assert "надёжность 10/10" in p2p.fmt_deal(d, cfg, snap())
    assert cards.deal_card(d, cfg, rel=(p2p.RELIABLE, [], 10))[:8] == PNG
    assert cards.deal_card(d, cfg, rel=(p2p.TRAP, ["спред 6.5% ≥5%"], 3))[:8] == PNG
    assert cards.deal_card(d, cfg, rel=(p2p.RISKY, ["x"]))[:8] == PNG   # старый формат без индекса


def test_ranking_is_unchanged_by_the_refactor():
    """Сортировка сканера по-прежнему «прибыль − штраф × число причин» (веса на неё не влияют)."""
    cfg = p2p.Config()
    d = (6.0, ad("Bybit", "buy", 84.6, orders=120), ad("MEXC", "sell", 89.0), "перевод на MEXC")
    assert len(p2p.reliability(d, cfg, snap())[1]) == len(p2p._risks(d, cfg, snap()))
