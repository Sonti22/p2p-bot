import asyncio

import pytest

import cards
import p2p
from helpers import make_ad


@pytest.mark.parametrize("text,blocked,notes", [
    ("Принимаю от третьих лиц, любые карты", ["оплата от третьих лиц"], []),
    ("От третьих лиц НЕ принимаю!!! Только с Т-Банка, чек на почту", [], ["нужен чек (на почту/в чат)", "принимает только с одного банка"]),
    ("Строго от первого лица / однофамильцев. Платёж не делим", [], ["одним платежом / кратные суммы"]),
    ("first, contact me on Telegram with @Dostovcoin", ["зовёт на связь вне площадки"], []),
    ("чек присылайте на почту ivan.petrov@mail.ru", [], ["нужен чек (на почту/в чат)"]),      # e-mail ≠ телеграм-хендл
    ("Реквизиты в чате. Обнал, дропы — мимо", ["обещает «обнал/обход банка» — серая схема"], ["реквизиты в чате"]),
    ("Оплата на расчётный счёт ИП, без блокировок", [], ["обещает «без блокировок»", "оплата на счёт ИП/юрлица"]),
    ("Только для верифицированных, KYC", [], ["требует верификацию"]),
    ("Звоните +7 (999) 123-45-67", [], ["в условиях указан телефон"]),
    ("", [], []),
])
def test_terms_flags(text, blocked, notes):
    assert p2p.terms_flags(text) == (blocked, notes)


def test_usable_drops_blocked_terms():
    c = p2p.Config()
    assert p2p.usable(make_ad(terms="Только с Т-Банка, чек на почту"), c)
    assert not p2p.usable(make_ad(terms="Принимаю от третьих лиц"), c)
    assert not p2p._signal_ok(make_ad(terms="Пишите в WhatsApp"), c)


def test_adapters_fill_terms(offline):
    for name in ("bybit", "kucoin", "mexc", "bitpapa"):
        ads = asyncio.run(p2p.FETCHERS[name](None, p2p.Config(), "buy", "USDT"))
        assert any(a.terms for a in ads), name
    bp = asyncio.run(p2p.bitpapa(None, p2p.Config(), "buy", "USDT"))
    assert any("[только верифицированные]" in a.terms for a in bp)   # флаг for_identified_people → заметка
    assert any("требует верификацию" in p2p.terms_flags(a.terms)[1] for a in bp)


def test_scan_offline_excludes_offplatform_ads(offline):
    c = p2p.Config(exchanges=["bitpapa"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    for a in snap.best.values():
        assert "telegram" not in a.terms.lower()


def test_fmt_ad_and_card_show_notes():
    a = make_ad(terms="Реквизиты в чате, чек на почту")
    txt = p2p.fmt_ad(a)
    assert "⚠" in txt and "реквизиты в чате" in txt
    d = (3.0, a, make_ad("MEXC", "sell", 90.0), "перевод −1 USDT (TRC20) на MEXC")
    assert cards.deal_card(d, p2p.Config())[:8] == b"\x89PNG\r\n\x1a\n"


def test_reliability_mentions_risky_terms():
    a = make_ad(terms="Оплата на счёт ИП, реквизиты в чате")
    d = (3.0, a, make_ad("MEXC", "sell", 90.0), "перевод −1 USDT (TRC20) на MEXC")
    snap = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [d], {}, {}, {})
    label, reasons = p2p.reliability(d, p2p.Config(), snap)
    assert any("условия" in r and "реквизиты в чате" in r and "ИП" in r for r in reasons)


RISKY_TERMS = "Оплата на счёт ИП, реквизиты в чате"


def test_stack_keeps_terms_of_single_ad():
    stacked = p2p._stack([make_ad(terms=RISKY_TERMS)], 50000)
    assert stacked.nick == "nick" and stacked.terms == RISKY_TERMS
    assert p2p._stack([make_ad()], 50000).terms == ""


def test_stack_merges_terms_of_used_ads_only():
    ads = [make_ad(price=85.0, max_amt=20000, avail=20000 / 85.0, terms="Реквизиты в чате"),
           make_ad(price=86.0, max_amt=40000, avail=40000 / 86.0, terms="Оплата на счёт ИП"),
           make_ad(price=87.0, terms="Звоните +7 (999) 123-45-67")]   # сумма набрана раньше — не используется
    stacked = p2p._stack(ads, 50000)
    assert stacked.nick == "2 объявл."
    notes = p2p.terms_flags(stacked.terms)[1]
    assert "реквизиты в чате" in notes and "оплата на счёт ИП/юрлица" in notes
    assert "в условиях указан телефон" not in notes


def test_scan_keeps_risky_terms_after_stacking(offline, monkeypatch):
    async def fake_r(s, cfg, side, asset):
        return [make_ad("R", side, 88.15 if side == "buy" else 89.5, orders=500, rate=100.0, terms=RISKY_TERMS)]

    monkeypatch.setitem(p2p.FETCHERS, "r", fake_r)
    c = p2p.Config(exchanges=["r"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    d = snap.deals[0]
    assert d[1].terms == RISKY_TERMS and d[2].terms == RISKY_TERMS
    label, reasons = p2p.reliability(d, c, snap)
    assert label == p2p.RISKY and sum("условия" in r for r in reasons) == 2   # покупка + продажа
    text = p2p.fmt_deal(d, c, snap)
    assert "⚠" in text and "реквизиты в чате" in text
