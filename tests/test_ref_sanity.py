"""Санитария ориентира Rapira USDT/RUB: rapira_mid раньше возвращал (ask+bid)/2 без единой проверки — bid=0
(пустая сторона стакана) роняет ориентир вдвое и MAX_DEV отсеивает все объявления молча, NaN в ответе делает отсев
аномалий полностью бесполезным (сравнение с nan всегда False). rapira_quote_mid/ref_vs_median защищают вход и
переключают collect() на запасной ориентир (REF_MEDIAN), когда котировке Rapira нельзя доверять."""
import math

import pytest

import p2p
from helpers import arun


def _cfg(**kw):
    kw.setdefault("exchanges", ["bybit", "htx", "kucoin", "mexc", "bitpapa"])
    return p2p.Config(assets=["USDT"], **kw)


# --- rapira_quote_mid ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bid,ask", [
    (0, 88.2), (88.2, 0), (-1, 88.2), (88.2, 88.1), (float("nan"), 88.2), (88.1, float("inf")),
    ("x", 88.2), (None, 88.2), (88.1, 88.1 * 1.061),
])
def test_rapira_quote_mid_rejects_bad_quotes(bid, ask):
    with pytest.raises(ValueError, match="Rapira: bad quote"):
        p2p.rapira_quote_mid(bid, ask)


def test_rapira_quote_mid_normal_quote():
    assert p2p.rapira_quote_mid(88.1, 88.2) == pytest.approx(88.15)


def test_rapira_quote_mid_spread_exactly_at_border_passes():
    assert p2p.rapira_quote_mid(100, 105) == pytest.approx(102.5)   # ровно 5% — проходит


# --- ref_vs_median -----------------------------------------------------------------------------------------

def test_ref_vs_median_rejects_bad_ref_regardless_of_ads_count():
    for bad in (float("nan"), 0, -1):
        ok, why = p2p.ref_vs_median(bad, [])
        assert ok is False and why


def test_ref_vs_median_trusts_when_not_enough_ads():
    ok, why = p2p.ref_vs_median(176.0, [87.0, 87.5, 88.0])   # меньше REF_MIN_ADS
    assert ok is True and why == ""


def test_ref_vs_median_trusts_close_ref():
    prices = [86.9, 87.0, 87.1, 87.0, 87.2, 86.8]
    ok, why = p2p.ref_vs_median(88.15, prices)
    assert ok is True and why == ""


def test_ref_vs_median_rejects_far_ref_with_both_numbers_in_reason():
    prices = [86.9, 87.0, 87.1, 87.0, 87.2, 86.8]
    ok, why = p2p.ref_vs_median(176.3, prices)
    assert ok is False
    assert "176.3" in why and "87.0" in why


def test_ref_vs_median_rejects_ref_half_of_median():
    prices = [86.9, 87.0, 87.1, 87.0, 87.2, 86.8]
    ok, why = p2p.ref_vs_median(44.0, prices)
    assert ok is False and why


def test_ref_vs_median_deviation_just_below_limit_is_trusted():
    prices = [100.0] * 6
    ok, why = p2p.ref_vs_median(109.0, prices, limit=10.0)   # 9% < 10%
    assert ok is True and why == ""


# --- rapira_mid (offline: p2p._json подмена через обёртку над offline) --------------------------------------

def test_rapira_mid_raises_on_zero_bid(offline, monkeypatch):
    async def fake_json(s, method, url, body=None):
        if "rapira.net" in url:
            return {"data": [{"symbol": "USDT/RUB", "askPrice": 88.2, "bidPrice": 0}]}
        return await offline(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", fake_json)
    with pytest.raises(ValueError):
        arun(p2p.rapira_mid(None))


def test_rapira_mid_normal_fixture(offline):
    assert arun(p2p.rapira_mid(None)) == pytest.approx(88.15)


# --- collect()/scan() интеграция (offline) --------------------------------------------------------------------

def test_scan_survives_zero_bid_rapira(offline, monkeypatch):
    async def fake_json(s, method, url, body=None):
        if "rapira.net" in url:
            return {"data": [{"symbol": "USDT/RUB", "askPrice": 88.2, "bidPrice": 0}]}
        return await offline(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", fake_json)
    snap_bad = arun(p2p.scan(None, _cfg()))
    assert snap_bad.ref_src == p2p.REF_MEDIAN
    assert snap_bad.best

    async def down(s):
        raise OSError("Rapira недоступна")
    monkeypatch.setattr(p2p, "rapira_mid", down)
    snap_down = arun(p2p.scan(None, _cfg()))
    assert set(snap_bad.best) == set(snap_down.best)


def test_scan_distrusts_rapira_far_from_median(offline, monkeypatch):
    async def double(s):
        return 176.3   # x2 к фикстуре 88.15
    monkeypatch.setattr(p2p, "rapira_mid", double)

    snap = arun(p2p.scan(None, _cfg()))
    assert snap.ref_src == p2p.REF_MEDIAN
    assert "rapira" in snap.errors
    assert snap.best
    assert snap.ref < 176.3 / 1.5   # ориентир снимка близок к медиане P2P, не к 176


def test_scan_trusts_rapira_when_not_enough_usdt_ads(offline, monkeypatch):
    async def double(s):
        return 176.3
    monkeypatch.setattr(p2p, "rapira_mid", double)

    async def fake_fetch(s, cfg, side, asset):
        from helpers import make_ad
        return [make_ad("Fake", side, 85.0 if side == "buy" else 90.0, asset=asset)]
    monkeypatch.setitem(p2p.FETCHERS, "fake", fake_fetch)

    snap = arun(p2p.scan(None, _cfg(exchanges=["fake"])))
    assert snap.ref_src == "Rapira USDT/RUB"
    assert "rapira" not in snap.errors


def test_scan_no_usdt_ads_keeps_rapira(offline, monkeypatch):
    async def empty_fetch(s, cfg, side, asset):
        return []
    monkeypatch.setitem(p2p.FETCHERS, "fake", empty_fetch)

    snap = arun(p2p.scan(None, _cfg(exchanges=["fake"])))
    assert snap.ref_src == "Rapira USDT/RUB"
    assert "rapira" not in snap.errors


def test_scan_clean_fixture_unchanged(offline):
    snap = arun(p2p.scan(None, _cfg()))
    assert snap.ref == pytest.approx(88.15)
    assert snap.ref_src == "Rapira USDT/RUB"
    assert "rapira" not in snap.errors


def test_scan_respects_cfg_max_dev_as_sanity_floor(offline, monkeypatch):
    async def dev12(s):
        return 88.15 * 1.12   # ~12% выше медианы фикстуры
    monkeypatch.setattr(p2p, "rapira_mid", dev12)

    snap = arun(p2p.scan(None, _cfg(max_dev=15.0)))
    assert snap.ref_src == "Rapira USDT/RUB"   # порог max(10, 15) = 15 — 12% в пределах, доверие сохраняется
    assert "rapira" not in snap.errors
