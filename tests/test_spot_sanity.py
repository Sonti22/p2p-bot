"""sane_spot/filter_spot: перевёрнутый стакан, широкий спред и выброс площадки не должны попадать в маршруты
(фантомная прибыль от кривой спот-котировки) — только отбрасываются, ничего не «чинится». Без сети."""
import copy
import logging
import math

import pytest

import p2p
from helpers import arun, make_ad

SPOT = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2500.0, 2501.0)},
        "MEXC": {"USDT": (1.0, 1.0), "ETH": (2499.0, 2500.0)}}


def cfg(**kw):
    c = p2p.Config()
    c.risk_buffer, c.pay_fee = {}, 0.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture(autouse=True)
def _clean_spot_rejected():
    p2p.SPOT_REJECTED.clear()
    yield
    p2p.SPOT_REJECTED.clear()


def test_crossed_book():
    spot = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2640.2, 2640.1)}}
    clean, dropped = p2p.sane_spot(spot)
    assert dropped == [("Bybit", "ETH", "crossed")]
    assert clean == {"Bybit": {"USDT": (1.0, 1.0)}}


def test_nonpositive():
    for bad in ((0, 1), (-1, 1), (1, 0)):
        spot = {"Bybit": {"ETH": bad}}
        clean, dropped = p2p.sane_spot(spot)
        assert dropped == [("Bybit", "ETH", "nonpositive")]
        assert clean == {"Bybit": {}}


def test_nonfinite():
    for bad in (float("nan"), float("inf"), None, "bad", (1.0,)):
        spot = {"Bybit": {"USDT": (1.0, 1.0), "ETH": bad}}
        clean, dropped = p2p.sane_spot(spot)
        assert dropped == [("Bybit", "ETH", "nonfinite")], bad
        assert clean == {"Bybit": {"USDT": (1.0, 1.0)}}, bad   # остальные монеты и площадки не задеты


def test_spread_threshold():
    ok, _ = p2p.sane_spot({"Bybit": {"ETH": (100.0, 101.9)}})   # 1.9% — остаётся
    assert ok == {"Bybit": {"ETH": (100.0, 101.9)}}
    bad, dropped = p2p.sane_spot({"Bybit": {"ETH": (100.0, 102.1)}})   # 2.1% — отброшено
    assert bad == {"Bybit": {}} and dropped == [("Bybit", "ETH", "spread")]
    # свой порог параметром
    ok2, _ = p2p.sane_spot({"Bybit": {"ETH": (100.0, 104.0)}}, max_spread_pct=5.0)
    assert ok2 == {"Bybit": {"ETH": (100.0, 104.0)}}


def test_outlier_among_four_venues_drops_only_the_one():
    spot = {"A": {"ETH": (2490.0, 2510.0)}, "B": {"ETH": (2495.0, 2505.0)}, "C": {"ETH": (2500.0, 2500.0)},
            "D": {"ETH": (3000.0, 3000.0)}}   # D — mid 3000, медиана остальных ~2500: +20%
    clean, dropped = p2p.sane_spot(spot)
    assert dropped == [("D", "ETH", "outlier")]
    assert set(clean) == {"A", "B", "C", "D"}
    assert clean["D"] == {}
    assert clean["A"]["ETH"] == (2490.0, 2510.0) and clean["B"]["ETH"] == (2495.0, 2505.0)
    assert clean["C"]["ETH"] == (2500.0, 2500.0)


def test_outlier_among_three_venues():
    spot = {"A": {"ETH": (2500.0, 2500.0)}, "B": {"ETH": (2500.0, 2500.0)}, "C": {"ETH": (3000.0, 3000.0)}}
    clean, dropped = p2p.sane_spot(spot)
    assert dropped == [("C", "ETH", "outlier")]
    assert clean["A"]["ETH"] == (2500.0, 2500.0) and clean["B"]["ETH"] == (2500.0, 2500.0) and clean["C"] == {}


def test_no_outlier_check_below_three_venues():
    spot = {"A": {"ETH": (2000.0, 2000.0)}, "B": {"ETH": (3000.0, 3000.0)}}   # 2 площадки — межплощадочной проверки нет
    clean, dropped = p2p.sane_spot(spot)
    assert dropped == []
    assert clean == spot


def test_no_outlier_check_when_only_two_survive_steps_a_to_d():
    # 3 площадки, одна crossed -> после шагов a-d осталось 2 -> межплощадочная проверка не выполняется
    spot = {"A": {"ETH": (2000.0, 2000.0)}, "B": {"ETH": (3000.0, 3000.0)}, "C": {"ETH": (10.0, 9.0)}}
    clean, dropped = p2p.sane_spot(spot)
    assert dropped == [("C", "ETH", "crossed")]
    assert clean["A"]["ETH"] == (2000.0, 2000.0) and clean["B"]["ETH"] == (3000.0, 3000.0) and clean["C"] == {}


def test_shape_keeps_all_venue_keys_input_untouched_and_idempotent():
    spot = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2500.0, 2501.0)}, "HTX": {"USDT": (1.0, 1.0)}}
    before = copy.deepcopy(spot)
    clean, dropped = p2p.sane_spot(spot)
    assert spot == before   # вход не изменён
    assert dropped == []
    assert set(clean) == {"Bybit", "HTX"}
    assert clean["HTX"] == {"USDT": (1.0, 1.0)}
    assert p2p.sane_spot(clean) == (clean, [])   # идемпотентна


def test_fixtures_pass_filter_without_losses(offline):
    spot = arun(p2p.spot_prices(None, ["USDT", "BTC", "ETH", "USDC"]))
    clean, dropped = p2p.sane_spot(spot)
    assert dropped == []
    assert clean == spot


def test_filter_spot_warns_once_per_change(caplog):
    spot_bad = {"Bybit": {"ETH": (2500.0, 2500.0)}, "MEXC": {"ETH": (2500.0, 2500.0)},
                "HTX": {"ETH": (2500.0, 2500.0)}, "KuCoin": {"ETH": (1990.0, 2000.0)}}
    with caplog.at_level(logging.WARNING, logger="p2p"):
        clean = p2p.filter_spot(spot_bad)
        assert p2p.SPOT_REJECTED == {("KuCoin", "ETH"): "outlier"}
        assert len(caplog.records) == 1
        p2p.filter_spot(spot_bad)   # тот же набор — новых предупреждений нет
        assert len(caplog.records) == 1
        spot_ok = {"Bybit": {"ETH": (2500.0, 2500.0)}, "MEXC": {"ETH": (2500.0, 2500.0)},
                   "HTX": {"ETH": (2500.0, 2500.0)}, "KuCoin": {"ETH": (2500.0, 2500.0)}}
        clean_ok = p2p.filter_spot(spot_ok)   # набор изменился на пустой — без предупреждения
        assert p2p.SPOT_REJECTED == {}
        assert len(caplog.records) == 1
        spot_other_bad = {"Bybit": {"ETH": (2500.0, 2500.0)}, "MEXC": {"ETH": (2500.0, 2500.0)},
                          "HTX": {"ETH": (2500.0, 2500.0)}, "KuCoin": {"ETH": (10.0, 9.0)}}   # crossed теперь
        p2p.filter_spot(spot_other_bad)
        assert p2p.SPOT_REJECTED == {("KuCoin", "ETH"): "crossed"}
        assert len(caplog.records) == 2
    assert clean["KuCoin"] == {}


def test_route_phantom_profit_removed_by_filter():
    spot = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2500.0, 2501.0)},
            "MEXC": {"USDT": (1.0, 1.0), "ETH": (2499.0, 2500.0)},
            "HTX": {"USDT": (1.0, 1.0), "ETH": (2498.0, 2502.0)},
            "KuCoin": {"USDT": (1.0, 1.0), "ETH": (1990.0, 2000.0)}}   # выброс: далеко от медианы ~2500
    b = make_ad("KuCoin", "buy", 88.0)
    s = make_ad("Bybit", "sell", 245000.0, asset="ETH")
    c = cfg()
    raw_profit, _ = p2p._route(b, s, c, spot)
    clean_spot = p2p.filter_spot(spot)
    assert "ETH" not in clean_spot["KuCoin"]
    clean_profit, _ = p2p._route(b, s, c, clean_spot)
    assert raw_profit > clean_profit + 5   # фантомная прибыль от кривой котировки KuCoin заметно выше
    only_healthy = {k: v for k, v in spot.items() if k != "KuCoin"}
    only_healthy_profit, _ = p2p._route(b, s, c, only_healthy)
    assert clean_profit == pytest.approx(only_healthy_profit)


def test_scan_drops_outlier_kucoin_eth_keeps_others(offline, monkeypatch):
    real = p2p._json

    async def pumped_kucoin(s, method, url, body=None):
        j = await real(s, method, url, body)
        if "api.kucoin.com/api/v1/market/allTickers" in url:
            for t in j["data"]["ticker"]:
                if t["symbol"] == "ETH-USDT":
                    t["buy"] = str(float(t["buy"]) * 1.25)
                    t["sell"] = str(float(t["sell"]) * 1.25)
        return j

    monkeypatch.setattr(p2p, "_json", pumped_kucoin)
    snap = arun(p2p.scan(None, p2p.Config()))
    assert "ETH" not in snap.spot["KuCoin"]
    assert "ETH" in snap.spot["Bybit"] and "ETH" in snap.spot["MEXC"] and "ETH" in snap.spot["HTX"]
    for venue in ("Bybit", "MEXC", "HTX", "KuCoin"):
        assert "USDT" in snap.spot[venue]
    assert p2p.SPOT_REJECTED == {("KuCoin", "ETH"): "outlier"}
