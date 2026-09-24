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
