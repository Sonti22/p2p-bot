import cards
import p2p
from helpers import make_ad

PNG = b"\x89PNG\r\n\x1a\n"


def test_deal_card_renders():
    d = (6.5, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 91.0, asset="USDC"),
         "спот USDT→USDC на Bybit (−0.1%) → перевод USDC без комиссии (BEP20) на MEXC → запас на курс −0.5%")
    assert cards.deal_card(d, p2p.Config())[:8] == PNG


def test_deal_card_renders_with_amount_breakdown():
    d = (6.5, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 91.0, asset="USDC"), "внутри биржи")
    amounts = {50_000: 1.2, 100_000: 0.9, 300_000: None}
    assert cards.deal_card(d, p2p.Config(), amounts)[:8] == PNG


def test_deal_card_renders_with_reliability_pill():
    d = (6.5, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 91.0, asset="USDC"), "внутри биржи")
    assert cards.deal_card(d, p2p.Config(), rel=("🪤 ловушка", ["спред 6.5% ≥5% — часто плата за риск"]))[:8] == PNG


def test_amounts_line_formats_missing_depth_as_dash():
    line = cards._amounts_line({50_000: 1.23, 300_000: None})
    assert "50к +1.23%" in line and "300к —" in line


def test_portfolio_card_renders():
    rows = [("Bybit", [("USDT", 10.0, 880.0), ("BTC", 0.01, 50_000.0)]), ("MEXC", [("TON", 100.0, None)])]
    assert cards.portfolio_card(rows, 50_880.0)[:8] == PNG


def test_top_chart_renders_empty_and_full():
    empty = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})
    full = p2p.Snapshot(88.0, "t", {}, {}, [(3.0, make_ad(), make_ad(side="sell", price=90.0), "внутри биржи")], {}, {}, {})
    assert cards.top_chart(empty, p2p.Config())[:8] == PNG
    assert cards.top_chart(full, p2p.Config())[:8] == PNG


def test_avatars_render():
    for style in cards.AVATARS:
        assert cards.avatar(128, style)[:8] == PNG
