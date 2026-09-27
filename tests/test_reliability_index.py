"""Индекс надёжности 0–10 на карточке и риск «обменник → обменник» (круги сухого прогона 25.09: BestChange →
BestChange с планом 7–8% — выгодные курсы обменников часто с условиями и AML-заморозкой)."""
import asyncio

import pytest

import cards
import p2p
from helpers import make_ad

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


def test_score_uses_the_same_weights_as_the_index():
    """score = прибыль − RISK_PENALTY × сумма весов рисков — та же сумма, что снимает индекс с 10."""
    cfg = p2p.Config(risk_penalty=1.5)
    d = (6.0, ad("Bybit", "buy", 84.6, orders=120), ad("MEXC", "sell", 89.0), "перевод на MEXC")
    weight = 2 + 1 + 2
    assert p2p.risk_weight(d, cfg, snap()) == weight
    assert p2p.reliability_index(d, cfg, snap()) == 10 - weight
    assert p2p.score(d, cfg, snap()) == pytest.approx(6.0 - 1.5 * weight)
    clean = (2.5, ad("HTX", "buy", 87.8), ad("KuCoin", "sell", 90.0), "перевод на KuCoin")
    assert p2p.score(clean, cfg, snap()) == 2.5


def test_card_caption_and_top_text_show_score():
    cfg = p2p.Config()
    d = (6.0, ad("Bybit", "buy", 84.6, orders=120), ad("MEXC", "sell", 89.0), "перевод на MEXC")
    shown = f"оценка {p2p.score(d, cfg, snap()):+.2f}"
    assert shown == "оценка -1.50"
    assert shown in p2p.fmt_signal(d, cfg, snap()) and shown in p2p.fmt_deal(d, cfg, snap())


def test_scan_ranks_by_risk_weights_not_by_reason_count(offline, monkeypatch):
    """По одной причине у обеих связок, но у «W» (≈2.9%) она тяжёлая (реквизиты в чате, вес 2), у «V» (≈2.0%) —
    лёгкая (мерчант у порога, вес 1). По числу причин W стояла бы выше, по весам (как индекс) — V."""
    ref = 88.15   # ориентир USDT из фикстуры Rapira

    async def fake_w(s, cfg, side, asset):
        return [make_ad("W", side, ref * 0.986) if side == "buy" else
                make_ad("W", side, ref * 1.015, terms="реквизиты в чат")]

    async def fake_v(s, cfg, side, asset):
        return [make_ad("V", side, ref * 0.99) if side == "buy" else make_ad("V", side, ref * 1.01, rate=0.5)]

    monkeypatch.setitem(p2p.FETCHERS, "w", fake_w)
    monkeypatch.setitem(p2p.FETCHERS, "v", fake_v)
    c = p2p.Config(exchanges=["w", "v"], assets=["USDT"], min_orders=0, min_rate=0)
    s = asyncio.run(p2p.scan(None, c))
    w = next(d for d in s.deals if d[1].ex == "W" and d[2].ex == "W")
    v = next(d for d in s.deals if d[1].ex == "V" and d[2].ex == "V")
    assert len(p2p.reliability(w, c, s)[1]) == len(p2p.reliability(v, c, s)[1]) == 1
    assert p2p.risk_weight(w, c, s) == 2 and p2p.risk_weight(v, c, s) == 1
    assert w[0] - c.risk_penalty > v[0] - c.risk_penalty          # «прибыль − штраф × число причин»: W впереди
    assert s.deals.index(v) < s.deals.index(w)                    # по весам — V впереди
    assert p2p.score(v, c, s) > p2p.score(w, c, s)
