import asyncio
import html
import re

import pytest

import bot as B
import p2p
from helpers import make_ad


def groups(*ads):
    """Собрать snap.groups так же, как это делает scan(): по (ex, side, asset), уже отсортировано по цене."""
    g = {}
    for a in ads:
        g.setdefault((a.ex, a.side, a.asset), []).append(a)
    for key, grp in g.items():
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
    return g


def test_sell_ad_undercuts_best_buy_group_by_tick():
    g = groups(make_ad("MEXC", "buy", 90.0), make_ad("MEXC", "buy", 90.5), make_ad("MEXC", "sell", 92.0))
    price, counter_price, spread = p2p.maker_quote(g, "MEXC", "USDT", "sell_ad")
    assert price == 90.0 - p2p.MAKER_TICK
    assert counter_price == 92.0
    assert spread == pytest.approx((92.0 - (90.0 - p2p.MAKER_TICK)) / 92.0 * 100)


def test_buy_ad_outbids_best_sell_group_by_tick():
    g = groups(make_ad("MEXC", "sell", 92.0), make_ad("MEXC", "sell", 91.5), make_ad("MEXC", "buy", 90.0))
    price, counter_price, spread = p2p.maker_quote(g, "MEXC", "USDT", "buy_ad")
    assert price == 92.0 + p2p.MAKER_TICK
    assert counter_price == 90.0
    assert spread == pytest.approx(((92.0 + p2p.MAKER_TICK) - 90.0) / 90.0 * 100)


def test_bybit_maker_fee_added_only_to_buy_ad():
    g = groups(make_ad("Bybit", "buy", 90.0), make_ad("Bybit", "sell", 92.0))
    _, _, sell_spread = p2p.maker_quote(g, "Bybit", "USDT", "sell_ad")
    _, _, buy_spread = p2p.maker_quote(g, "Bybit", "USDT", "buy_ad")
    mexc_g = groups(make_ad("MEXC", "buy", 90.0), make_ad("MEXC", "sell", 92.0))
    _, _, mexc_buy_spread = p2p.maker_quote(mexc_g, "MEXC", "USDT", "buy_ad")
    assert sell_spread == pytest.approx((92.0 - (90.0 - p2p.MAKER_TICK)) / 92.0 * 100)   # без комиссии мейкера
    assert buy_spread == pytest.approx(mexc_buy_spread + 0.3)   # +0.3% Bybit только на покупку


def test_missing_side_returns_none():
    g = groups(make_ad("MEXC", "buy", 90.0))
    assert p2p.maker_quote(g, "MEXC", "USDT", "sell_ad") is None
    assert p2p.maker_quote(g, "MEXC", "USDT", "buy_ad") is None
    assert p2p.maker_quote({}, "MEXC", "USDT", "sell_ad") is None


def test_unknown_post_side_raises():
    with pytest.raises(ValueError):
        p2p.maker_quote(groups(make_ad("MEXC", "buy", 90.0)), "MEXC", "USDT", "swap")


# --- место в стакане, конкуренты рядом и спред для /maker ---

def test_maker_place_equal_price_competitor_stays_ahead():
    q = [make_ad("MEXC", "buy", p) for p in (90.0, 90.2, 90.2, 90.5)]   # продавцы: дешевле — выше
    assert p2p.maker_place(q, 90.2, "sell_ad") == (4, 5, pytest.approx(0.2))   # с равной ценой — за ними
    assert p2p.maker_place(q, 89.99, "sell_ad") == (1, 5, 0.0)
    assert p2p.maker_place(q, 91.0, "sell_ad")[:2] == (5, 5)


def test_maker_place_buy_ad_higher_price_goes_first():
    q = [make_ad("MEXC", "sell", p) for p in (92.0, 91.5, 91.0)]   # покупатели: дороже — выше
    place, total, gap = p2p.maker_place(q, 91.6, "buy_ad")
    assert (place, total) == (2, 4) and gap == pytest.approx(0.4)
    assert p2p.maker_place([], 91.6, "buy_ad") == (1, 1, 0.0)
    with pytest.raises(ValueError):
        p2p.maker_place(q, 91.6, "swap")


def test_maker_neighbors_window_around_own_place():
    q = [make_ad("MEXC", "buy", 90 + i / 10) for i in range(10)]
    assert [n for n, _ in p2p.maker_neighbors(q, 1)] == [2, 3, 4, 5, 6]     # я первый — пятеро за мной
    assert [n for n, _ in p2p.maker_neighbors(q, 4)] == [2, 3, 5, 6, 7]     # двое выше, трое ниже
    assert [n for n, _ in p2p.maker_neighbors(q, 11)] == [6, 7, 8, 9, 10]   # я последний
    assert [n for n, _ in p2p.maker_neighbors(q[:2], 2)] == [1, 3]


def test_book_spread_and_maker_round_fee():
    ask, bid, pct = p2p.book_spread(groups(make_ad("MEXC", "buy", 92.0), make_ad("MEXC", "sell", 90.0)), "MEXC", "USDT")
    assert (ask, bid) == (92.0, 90.0) and pct == pytest.approx(2 / 90 * 100)
    assert p2p.book_spread(groups(make_ad("MEXC", "buy", 92.0)), "MEXC", "USDT") is None
    assert p2p.maker_round_fee("Bybit") == pytest.approx(0.3)   # 0.3% на покупку + 0 на продажу
    assert p2p.maker_round_fee("MEXC") is None                  # нет в MAKER_FEE — неизвестна, а не 0


def _tg_len(text):
    """Длина, которую считает Telegram: видимый текст без HTML-тегов, в UTF-16."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", text))
    return len(plain.encode("utf-16-le")) // 2


def _offline_snap():
    cfg = p2p.Config(assets=["USDT"])
    return cfg, asyncio.run(p2p.scan(None, cfg))


def test_scan_keeps_whole_book_including_filtered_ads(offline):
    cfg, snap = _offline_snap()
    book = [a.price for a in snap.book[("Bybit", "buy", "USDT")]]
    assert book == sorted(book) and 84.5 in book                      # продавцы: дешевле — выше
    assert 84.5 not in [a.price for a in snap.groups[("Bybit", "buy", "USDT")]]   # 2 сделки — отсеян своим фильтром
    assert not [k for k in snap.book if k[0] == "BestChange"]


def test_maker_book_counts_ads_hidden_by_own_filters_and_gap(offline):
    cfg, snap = _offline_snap()
    price, _, _ = p2p.maker_quote(snap.groups, "Bybit", "USDT", "sell_ad")   # 85.00 − шаг = 84.99
    text = "\n".join(B.maker_book_lines(snap, cfg, "Bybit", "USDT", "sell_ad", price))
    assert "Место в стакане: 3-е из 5, до 1-го 0.49 ₽ (0.58%)" in text   # впереди 84.50 и 84.84 из выдачи
    lines = text.splitlines()
    mine = lines.index("▶ 3. 84.99 ₽ — ты")
    assert lines[mine - 2].startswith("1. 84.50 ₽") and lines[mine - 1].startswith("2. 84.84 ₽")
    assert lines[mine + 1].startswith("4. 85.00 ₽") and "384 сд/100%" in lines[mine + 1]
    assert "Спред Bybit: купить 85.00 / продать 89.90 ₽ → -5.45%; после комиссии мейкера за круг (0.30%) " \
           "остаётся -5.75%" in text


def test_maker_book_marks_limits_not_overlapping_amount(offline):
    cfg, snap = _offline_snap()
    text = "\n".join(B.maker_book_lines(snap, cfg, "BitPapa", "USDT", "sell_ad", 86.99))
    rivals = [ln for ln in text.splitlines() if ln[:1].isdigit()]
    assert [ln.split(" ₽")[0] for ln in rivals] == ["1. 85.85", "3. 87.00", "4. 90.00", "5. 92.10"]
    warned = [ln for ln in rivals if ln.endswith("⚠️ лимиты не пересекаются")]
    assert len(warned) == 3                                      # 85 ₽, 600 ₽, 2 000–6 283 ₽ — мимо 50 000
    assert "20 000–103 486 ₽" in rivals[-1] and "⚠️" not in rivals[-1]
    assert "Спред" not in text                                   # у BitPapa в фикстуре нет второй стороны


def test_maker_view_picks_best_variant_on_fixture_books(offline):
    cfg, snap = _offline_snap()
    text = B.maker_view(snap, cfg, "USDT")
    assert "📍 <b>Стакан KuCoin: продать по 90.99 ₽</b>" in text     # наименьший спред из всех вариантов
    assert "Место в стакане: 1-е из 5, отрыв от 2-го 0.01 ₽" in text
    assert "Конкуренты рядом (сумма 50 000 ₽):" in text and "▶ 1. 90.99 ₽ — ты" in text
    assert "Спред KuCoin: купить 91.00 / продать 88.30 ₽ → 3.06%; комиссия мейкера KuCoin неизвестна" in text
    assert _tg_len(text) < 1500


def _full_book(pays, min_amt, max_amt, avail, orders):
    """Пять площадок, по 20 объявлений на сторону; свои фильтры пропустили не всех (groups — с 4-го)."""
    g, book = {}, {}
    for ex in ("Bybit", "MEXC", "HTX", "KuCoin", "BitPapa"):
        for side in ("buy", "sell"):
            ads = [make_ad(ex, side, 92.0 + i * 0.07 if side == "buy" else 90.0 - i * 0.07, pays=pays,
                           orders=orders, rate=99.4, min_amt=min_amt, max_amt=max_amt, avail=avail) for i in range(20)]
            book[(ex, side, "USDT")], g[(ex, side, "USDT")] = ads, ads[3:]
    return p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, groups=g, book=book)


def test_maker_view_length_with_all_venues():
    cfg = p2p.Config(exchanges=["bybit", "mexc", "htx", "kucoin", "bitpapa"])
    typical = B.maker_view(_full_book(("Tinkoff", "Sberbank", "SBP"), 10000, 300000, 3500, 1234), cfg, "USDT")
    assert "Место в стакане: 4-е из 21" in typical and typical.count("\n5. ") == 1
    assert _tg_len(typical) < 1500
    worst = B.maker_view(_full_book(("Raiffeisenbank", "SBP - Fast Bank Transfer", "Tinkoff", "Sberbank"),
                                    100000, 1500000, 123456.78, 123456), cfg, "USDT")
    assert worst.count("⚠️ лимиты не пересекаются") == 5 and _tg_len(worst) < 4096


def test_remembered_deals_do_not_keep_the_full_book():
    """Полный стакан (Snapshot.book) нужен /maker только по свежему снимку — 200 запомненных сделок его не держат."""
    import bot as B
    from test_bot import Stub
    bot = Stub(p2p.Config())
    ad = p2p.Ad("Bybit", "buy", 85.0, 1000, 500000, 1e4, ["SBP"], "m", 1000, 100.0)
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, book={("Bybit", "buy", "USDT"): [ad]})
    deal_id = bot.remember_deal((2.0, ad, ad, "r"), snap=snap)
    kept = bot.deals_by_id[deal_id][2]
    assert kept.book == {} and snap.book and kept.groups is snap.groups
