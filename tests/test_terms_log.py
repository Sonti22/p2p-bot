"""Журнал отсева по стоп-фразам условий объявлений (p2p.terms_hits/TERMS_LOG): кто и какой фразой отсеян, по мерчанту
без раздувания счёта сканами; раздел в /traps и строка в /status."""
import pytest

import bot as B
import p2p
from helpers import arun, make_ad
from test_bot import Stub, snap


@pytest.fixture(autouse=True)
def _clean_terms_log():
    p2p.TERMS_LOG.clear()
    yield
    p2p.TERMS_LOG.clear()


def _ad(nick, terms, ex="Bybit", side="buy", asset="USDT"):
    a = make_ad(ex, side, 85.0, asset=asset, terms=terms)
    a.nick = nick
    return a


def test_terms_hits_match_terms_flags_and_skip_negations():
    assert p2p.terms_hits("Оплата с любых карт, пишите в Telegram") == [
        ("оплата от третьих лиц", "с любых карт"), ("зовёт на связь вне площадки", "telegram")]
    assert p2p.terms_hits("От третьих лиц не принимаю") == []          # отрицание — норма, как в terms_flags
    for text in ("третьи лица ок", "пишите @some_handle", "обнал"):
        assert [label for label, _ in p2p.terms_hits(text)] == p2p.terms_flags(text)[0]


def test_assemble_collects_hits_of_all_ads_and_scan_logs_them():
    ads = [_ad("m1", "третьи лица можно", side="buy"), _ad("m2", "всё честно"),
           _ad("m3", "только whatsapp", ex="MEXC", side="sell")]
    s = p2p.assemble(p2p.Config(), ads, ref=85.0, ts=1000.0)
    assert [(h["ex"], h["nick"], h["label"]) for h in s.terms_hits] == [
        ("Bybit", "m1", "оплата от третьих лиц"), ("MEXC", "m3", "зовёт на связь вне площадки")]
    assert s.terms_hits[0]["phrase"] == "третьи лица" and s.terms_hits[0]["ts"] == 1000.0


def test_log_counts_scans_per_merchant_not_ads():
    hits = p2p._terms_entries([_ad("m1", "третьи лица"), _ad("m1", "третьи лица", asset="BTC")], ts=1000.0)
    p2p.record_terms_hits(hits)
    p2p.record_terms_hits(p2p._terms_entries([_ad("m1", "третьи лица")], ts=1030.0))
    (row,) = p2p.terms_log()
    assert row["scans"] == 2 and row["first"] == 1000.0 and row["last"] == 1030.0
    assert p2p.terms_summary() == {"оплата от третьих лиц": 1}


def test_log_is_capped_forgetting_oldest(monkeypatch):
    monkeypatch.setattr(p2p, "TERMS_LOG_SIZE", 3)
    for i in range(5):
        p2p.record_terms_hits(p2p._terms_entries([_ad(f"m{i}", "обнал")], ts=1000.0 + i))
    assert [r["nick"] for r in p2p.terms_log()] == ["m4", "m3", "m2"]


def test_traps_and_status_show_the_log():
    p2p.record_terms_hits(p2p._terms_entries([_ad("m<1>", "пишите в телеграм", ex="HTX", side="sell")], ts=1000.0))
    text = B.traps_view()
    assert "Отсеяны стоп-фразами в условиях" in text and "HTX m&lt;1&gt; (продажа USDT)" in text
    assert "зовёт на связь вне площадки, фраза «телеграм», сканов 1" in text
    bot = Stub(p2p.Config())
    bot.last = snap([])
    assert "Отсеяно стоп-фразами в условиях: 1 мерчантов с запуска (зовёт на связь вне площадки 1) — /traps" \
        in bot.status_view()


def test_empty_log_changes_nothing():
    assert B.terms_log_lines() == []
    bot = Stub(p2p.Config())
    bot.last = snap([])
    assert "стоп-фразами" not in bot.status_view()


def test_live_scan_records_fixture_hits(offline):
    """Живой конвейер на фикстурах: scan кладёт срабатывания в журнал (если в фикстурах есть такие условия)."""
    s = arun(p2p.scan(None, p2p.Config(assets=["USDT"])))
    assert len(p2p.terms_log()) == len({(h["ex"], h["nick"], h["label"]) for h in s.terms_hits})


def test_merchant_with_two_reasons_counted_once():
    p2p.record_terms_hits(p2p._terms_entries([_ad("m1", "третьи лица, пишите в телеграм")], ts=1000.0))
    assert p2p.terms_summary() == {"оплата от третьих лиц": 1, "зовёт на связь вне площадки": 1}
    assert "— 1 мерчантов с запуска" in B.traps_view()
    bot = Stub(p2p.Config())
    bot.last = snap([])
    assert "Отсеяно стоп-фразами в условиях: 1 мерчантов" in bot.status_view()


def test_traps_message_stays_within_telegram_limit(monkeypatch):
    """Полный журнал ловушек (30 длинных строк) + журнал стоп-фраз — /traps всё равно одним сообщением ≤ 4096."""
    long_reason = "купить USDT на Bybit по 84,12 ₽ — на 5.3% ниже рынка (ориентир 88,50 ₽, отсев >4%) " + "x" * 15
    monkeypatch.setattr(B, "traps_log", lambda: [{"ts": 1000.0 + i, "reason": long_reason} for i in range(30)])
    for i in range(10):
        p2p.record_terms_hits(p2p._terms_entries([_ad(f"m{i}" * 5, "третьи лица " + "y" * 40)], ts=1000.0 + i))
    text = B.traps_view()
    assert 3000 < len(text) <= 4096 and "Отсеяны стоп-фразами" in text   # раздел урезан, но влез
    monkeypatch.setattr(B, "traps_log", lambda: [{"ts": 1000.0, "reason": "коротко"}])
    assert "сканов 1" in B.traps_view()                               # место есть — раздел на месте
