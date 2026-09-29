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


def _texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def test_traps_and_status_show_the_log():
    p2p.record_terms_hits(p2p._terms_entries([_ad("m<1>", "пишите в телеграм", ex="HTX", side="sell")], ts=1000.0))
    text = B.terms_view()
    assert "Отсеяны стоп-фразами в условиях" in text and "HTX m&lt;1&gt; (продажа USDT)" in text
    assert "зовёт на связь вне площадки, фраза «телеграм», сканов 1" in text
    assert "стоп-фразами" not in B.traps_view()                       # раздел — отдельным сообщением
    bot = Stub(p2p.Config())
    bot.last = snap([])
    assert "Отсеяно стоп-фразами в условиях: 1 мерчантов с запуска (зовёт на связь вне площадки 1) — /traps" \
        in bot.status_view()
    arun(bot.handle("/traps"))
    first, second = _texts(bot)
    assert "Ловушки" in first and second == text


def test_empty_log_changes_nothing():
    assert B.terms_view() is None
    bot = Stub(p2p.Config())
    bot.last = snap([])
    assert "стоп-фразами" not in bot.status_view()
    arun(bot.handle("/traps"))
    (only,) = _texts(bot)                                            # журнала нет — второго сообщения нет
    assert "Ловушки" in only


def test_live_scan_records_fixture_hits(offline):
    """Живой конвейер на фикстурах: scan кладёт срабатывания в журнал (если в фикстурах есть такие условия)."""
    s = arun(p2p.scan(None, p2p.Config(assets=["USDT"])))
    assert len(p2p.terms_log()) == len({(h["ex"], h["nick"], h["label"]) for h in s.terms_hits})


def test_merchant_with_two_reasons_counted_once():
    p2p.record_terms_hits(p2p._terms_entries([_ad("m1", "третьи лица, пишите в телеграм")], ts=1000.0))
    assert p2p.terms_summary() == {"оплата от третьих лиц": 1, "зовёт на связь вне площадки": 1}
    assert "— 1 мерчантов с запуска" in B.terms_view()
    bot = Stub(p2p.Config())
    bot.last = snap([])
    assert "Отсеяно стоп-фразами в условиях: 1 мерчантов" in bot.status_view()


def _full_traps():
    """Полный журнал ловушек (TRAPS_LOG_SIZE = 30) реальными записями _trap_entry: BTC — самые длинные цены."""
    e = p2p._trap_entry(make_ad("KuCoin", "sell", 9123456.78, asset="BTC"), ref=8123456.78, cfg=p2p.Config())
    return [dict(e, ts=1000.0 + i) for i in range(p2p.TRAPS_LOG_SIZE)]


def test_traps_full_journals_go_as_two_messages_within_telegram_limit(monkeypatch):
    """Полный журнал ловушек + TERMS_LOG_SHOW строк стоп-фраз одним сообщением длиннее 4096 — Telegram ответил бы 400
    «message is too long», и /traps молча не ответил бы. Раздел стоп-фраз уходит вторым сообщением, оба целиком."""
    monkeypatch.setattr(B, "traps_log", _full_traps)
    for i in range(B.TERMS_LOG_SHOW):
        p2p.record_terms_hits(p2p._terms_entries([_ad(f"m{i}" * 5, "третьи лица " + "y" * 40)], ts=1000.0 + i))
    bot = Stub(p2p.Config())
    arun(bot.handle("/traps"))
    traps, terms = _texts(bot)
    assert len(traps) + 1 + len(terms) > 4096                        # одним сообщением — не влезло бы
    assert len(traps) <= B.TRAPS_TEXT_MAX <= 4096 and len(terms) <= B.TRAPS_TEXT_MAX
    assert traps.count("выше рынка") == p2p.TRAPS_LOG_SIZE and "не влезли" not in traps
    assert terms.count("сканов 1") == B.TERMS_LOG_SHOW and "не влезли" not in terms


def test_fit_lines_cuts_by_lines_with_a_note():
    rows = [f"строка {i} " + "z" * 190 for i in range(40)]         # 40 × ~200 = ~8000 символов
    text = B.fit_lines(["заголовок", ""], rows)
    assert len(text) <= B.TRAPS_TEXT_MAX
    shown = text.count("z" * 190)
    assert 0 < shown < 40 and text.endswith(f"… и ещё {40 - shown} — не влезли в сообщение Telegram")
    assert f"строка {shown - 1} " in text and f"строка {shown} " not in text   # целыми строками, по порядку
    assert B.fit_lines(["h"], ["a", "b"]) == "h\na\nb"                 # влезает — без пометки
    for room in range(60, 140):                                       # на границе: с пометкой не длиннее room
        assert len(B.fit_lines(["h"], ["x" * 20] * 5, room=room)) <= room


def test_traps_view_cut_when_reasons_are_huge(monkeypatch):
    long_reason = "купить USDT на Bybit по 84,12 ₽ — на 5.3% ниже рынка " + "x" * 200
    monkeypatch.setattr(B, "traps_log", lambda: [{"ts": 1000.0 + i, "reason": long_reason} for i in range(30)])
    text = B.traps_view()
    assert len(text) <= B.TRAPS_TEXT_MAX and "— не влезли в сообщение Telegram" in text


def test_terms_hits_cached_by_text():
    p2p._terms_hits_cached.cache_clear()
    ads = [_ad(f"m{i}", "третьи лица") for i in range(50)] + [_ad("n", "всё честно")]
    p2p.assemble(p2p.Config(), ads, ref=85.0, ts=1000.0)
    info = p2p._terms_hits_cached.cache_info()
    assert info.misses == 2 and info.hits >= 49 and info.maxsize == p2p.TERMS_HITS_CACHE
    got = p2p.terms_hits("третьи лица")
    got.append(("x", "y"))                                           # список вызывающего — не кэш
    assert p2p.terms_hits("третьи лица") == [("оплата от третьих лиц", "третьи лица")]
    assert p2p.terms_hits(None) == []


def test_lean_snapshot_drops_terms_hits():
    s = p2p.assemble(p2p.Config(), [_ad("m1", "обнал")], ref=85.0, ts=1000.0)
    assert s.terms_hits
    lean = B._lean(s)
    assert lean.terms_hits == [] and s.terms_hits                    # запомненный снимок — без журнала скана
    only_hits = snap([])
    only_hits.terms_hits = [{"ex": "Bybit"}]
    assert B._lean(only_hits).terms_hits == []
