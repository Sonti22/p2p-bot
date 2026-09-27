"""research/directional_bt.py на синтетических свечах: сигналы EMA, стоп, комиссии, фандинг, walk-forward, базы."""
import math

import pytest

from research import directional_bt as d

H = d.H
T0 = 59027 * 8 * H
SLIP = 0.0002          # BTC: 0.02% на сторону
FEE = 0.00055 * 2      # taker 0.055% × 2


def bars_from(prices, start=T0, spread=0.002):
    out, prev = [], prices[0]
    for i, c in enumerate(prices):
        o = prev
        out.append([start + i * H, o, max(o, c) * (1 + spread), min(o, c) * (1 - spread), c, 1.0])
        prev = c
    return out


def seg(prices, funding=(), symbol="BTCUSDT"):
    b = bars_from(prices)
    return {"symbol": symbol, "start": b[0][0], "end": b[-1][0] + H, "klines": b, "funding": list(funding)}


TREND = [100.0] * 150 + [100 + 0.5 * i for i in range(1, 101)] + [150 - 0.5 * i for i in range(1, 201)]


def test_ema_and_atr():
    assert d.ema([5.0] * 10, 3) == [5.0] * 10
    e = d.ema([0.0, 10.0], 3)
    assert e[1] == pytest.approx(5.0)
    a = d.atr([[0, 10, 11, 9, 10, 0]] * 20, 14)
    assert a[12] is None and a[13] == pytest.approx(2.0) and a[19] == pytest.approx(2.0)


def test_cross_entry_next_open_reverse_and_costs():
    tr = d.simulate(seg(TREND), "BTC", d.Params(), 2.0)
    assert [t["side"] for t in tr] == [1, -1] and [t["reason"] for t in tr] == ["cross", "end"]
    t = tr[0]
    assert t["i"] == 151 and t["entry"] == pytest.approx(100.5 * (1 + SLIP))   # сигнал на закрытии 150, вход по open 151
    b = seg(TREND)["klines"]
    assert t["exit"] == pytest.approx(b[t["j"]][1] * (1 - SLIP))
    assert t["fees"] == pytest.approx(FEE * (1 + t["exit"] / t["entry"]))
    assert t["ret"] == pytest.approx(t["gross"] - t["fees"] + t["funding"]) and t["funding"] == 0
    assert tr[1]["i"] == t["j"]                                                 # разворот в той же свече


def test_stop_inside_bar_and_gap():
    prices = [100.0] * 150 + [100.5, 101.0, 101.5, 95.0] + [95.0] * 20
    s = seg(prices)
    tr = d.simulate(s, "BTC", d.Params(), 2.0)
    t = tr[0]
    assert t["reason"] == "stop" and t["j"] == 153
    stop = t["entry"] * (1 - t["stop_frac"])
    assert t["exit"] == pytest.approx(stop * (1 - SLIP))
    s["klines"][153][1] = 94.0                                                   # гэп: открылись ниже стопа
    t = d.simulate(s, "BTC", d.Params(), 2.0)[0]
    assert t["reason"] == "stop" and t["exit"] == pytest.approx(94.0 * (1 - SLIP))


def test_long_pays_positive_funding():
    fund = [[T0 + h * H, 0.001] for h in range(0, 600, 8)]
    s = seg(TREND, fund)
    t = d.simulate(s, "BTC", d.Params(), 2.0)[0]
    opens = {b[0]: b[1] for b in s["klines"]}
    paid = sum(0.001 * opens[ts] for ts, _ in fund if t["entry_ts"] < ts <= t["exit_ts"])
    assert t["funding"] == pytest.approx(-paid / t["entry"]) and t["funding"] < 0
    short = d.simulate(s, "BTC", d.Params(), 2.0)[1]
    assert short["funding"] > 0                                                  # шорт получает


def test_equity_fixed_risk_and_leverage_cap():
    trade = {"entry_ts": T0 + 10 * H, "exit_ts": T0 + 20 * H, "ret": 0.01, "stop_frac": 0.02, "side": 1,
             "entry": 100.0}
    s = seg([100.0] * 40)
    pts, pnls = d.equity([trade], [s], d.Params(), (T0, T0 + 40 * H))
    assert pnls == [pytest.approx(500 * 0.01)]          # номинал = 1% × 1000 / 2% = 500
    trade["stop_frac"] = 0.001                          # номинал был бы 10 000 — режем до 2× капитала
    pts, pnls = d.equity([trade], [s], d.Params(), (T0, T0 + 40 * H))
    assert pnls == [pytest.approx(2000 * 0.01)] and pts[-1][1] == pytest.approx(1020)


def _wave(n, period=240, amp=0.1):
    return [100 * (1 + amp * math.sin(2 * math.pi * i / period)) for i in range(n)]


def test_walk_forward_windows_no_overlap_and_grid_k():
    s = seg(_wave(24 * 30 * 15))
    wf = d.walk_forward([s], "BTC", d.Params())
    folds = wf["folds"]
    assert len(folds) >= 3 and all(f["k"] in (1.5, 2.0, 3.0) for f in folds)
    assert folds[1]["oos_start"] - folds[0]["oos_start"] == 3 * d.MONTH_MS
    assert folds[0]["oos_start"] - folds[0]["is_start"] == 6 * d.MONTH_MS
    assert wf["oos"] and all(wf["window"][0] <= t["entry_ts"] < wf["window"][1] for t in wf["oos"])
    for a, b in zip(wf["oos"], wf["oos"][1:]):
        assert b["entry_ts"] >= a["exit_ts"]
    assert sum(f["oos_trades"] for f in folds) == len(wf["oos"])


def test_random_baseline_extremes_and_determinism():
    s = seg(_wave(24 * 60))
    p = d.Params(sims=200)
    win = (T0 + 200 * H, T0 + 24 * 50 * H)
    pools = {"BTC": d.RandomPool([s], "BTC", p, win)}
    good = [{"ret": 0.05, "hold_h": 24, "side": 1}] * 40
    bad = [{"ret": -0.05, "hold_h": 24, "side": -1}] * 40
    r_good = d.random_baseline(pools, {"BTC": good}, p)
    assert r_good["p_value"] == pytest.approx(1 / 201, abs=1e-4)
    assert d.random_baseline(pools, {"BTC": bad}, p)["p_value"] == 1.0
    assert d.random_baseline(pools, {"BTC": good}, p) == r_good                 # фиксированный seed


def test_buy_and_hold():
    kl = bars_from([100.0 + i for i in range(101)])
    r = d.buy_and_hold(kl, (T0, T0 + 101 * H), 1000.0, 0.0)
    assert r["ok"] and r["total_return_pct"] == pytest.approx(100.0, abs=0.01)   # от закрытия первой свечи
    assert r["max_dd_pct"] == 0


def test_run_structure_and_summary():
    n = 24 * 30 * 13
    ds = {"coins": {c: {"spot": {"klines": bars_from(_wave(n, 300 + k * 37))},
                        "perp": [seg(_wave(n, 300 + k * 37), symbol=c + "USDT")], "bingx": []}
                    for k, c in enumerate(("BTC", "ETH"))}}
    res = d.run(ds, d.Params(sims=30), log=lambda *a: None)
    assert res["coins"]["BTC"]["ok"] and res["coins"]["ETH"]["ok"]
    pf = res["portfolio"]
    assert pf["trades"] == res["coins"]["BTC"]["walk_forward"]["trades"] + res["coins"]["ETH"]["walk_forward"]["trades"]
    assert 0 < res["random"]["p_value"] <= 1 and res["random"]["sims"] == 30
    assert "fees_x1_portfolio" in res and "buy_and_hold_portfolio" in res
    lines = d.summary_ru(res)
    assert len(lines) == 3 and lines[-1].startswith("Портфель")
