"""research/hedge_bt.py: окна удержания, стоимость хеджа против запаса, округление лота, чтение копий баз."""
import math
import sqlite3

import pytest

from research import hedge_bt as hb

H = hb.H
T0 = 59027 * 8 * H


def kl(n, price, start=T0):
    return [[start + i * H, price(i), price(i), price(i), price(i), 1.0] for i in range(n)]


def perp_seg(n, price, rate=0.0, step_h=8, spec=None):
    return {"symbol": "BTCUSDT", "start": T0, "end": T0 + n * H, "klines": kl(n, price),
            "funding": [[T0 + i * H, rate] for i in range(0, n, step_h)],
            "spec": spec or {"qty_step": 0.0001, "min_qty": 0.0001, "min_notional": 2}}


def test_episodes_merge_close_signals():
    assert hb.episodes([0, 300, 600, 2000, 2100]) == [(0, 600), (2000, 2100)]
    assert hb.episodes([]) == []


def test_window_samples_moves_and_funding():
    n = 48
    spot = kl(n, lambda i: 100.0 * (1.01 ** i))
    perp = perp_seg(n, lambda i: 100.0 * (1.01 ** i) + 0.1, rate=0.0002)
    s = hb.window_samples(spot, [perp], T0, 8)
    assert s[0][0] == T0 and s[0][1] == pytest.approx(1.01 ** 8 - 1)
    assert s[0][3] == pytest.approx(0.0002)                   # одна выплата в (t, t+8ч]
    assert s[1][3] == pytest.approx(0.0002) and len(s) == n - 8
    assert hb.window_samples(spot, [perp], T0 + 10 * H, 8)[0][0] == T0 + 10 * H


def test_evaluate_window_cost_and_sigma():
    p = hb.Params()
    rows = [(T0 + i * H, 0.01 * (-1) ** i, 0.01 * (-1) ** i, 0.0) for i in range(100)]   # перп = спот
    w = hb.evaluate_window(rows, rows, 60, "BTC", p, 0.05)
    fixed = (2 * 0.05 * 2 + 2 * 0.02) / 100
    assert w["cost_mean_pct"] == pytest.approx(fixed * 100)
    assert w["cost_to_buffer"] == pytest.approx(fixed / 0.003, abs=1e-3)
    assert w["sigma_ratio"] == 0.0 and w["hedged"]["mean_pct"] == pytest.approx(-fixed * 100)
    assert w["loss_over_buffer_share"] == 0.5                 # половина окон −1% < −0.3%
    ws = hb.evaluate_window(rows, rows, 15, "BTC", p, 0.05)   # < 60 мин — масштаб √(15/60) = 0.5
    assert ws["unhedged"]["abs_p99_pct"] == pytest.approx(0.5, abs=1e-6) and "√" in ws["method"]
    rows_f = [(t, m, pm, 0.001) for t, m, pm, _ in rows]      # шорт получил фандинг 0.1% — хедж дешевле
    assert hb.evaluate_window(rows_f, rows_f, 60, "BTC", p, 0.05)["cost_mean_pct"] == pytest.approx(fixed * 100 - 0.1)


def test_lot_coefficient():
    spec = {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5}
    # 10 000 ₽ / 92 ₽ / 100 000 $ = 0.001087 BTC → шорт 0.001
    assert hb.lot_coefficient(10000, 92, 100000, spec) == pytest.approx(0.001 / (10000 / 92 / 100000))
    # меньше шага — всё равно минимум 0.001 (перехедж)
    assert hb.lot_coefficient(3000, 92, 100000, spec) == pytest.approx(0.001 / (3000 / 92 / 100000))
    assert hb.lot_coefficient(100, 92, 1.0, {"qty_step": 0.1, "min_qty": 0.1, "min_notional": 5}) is None


def _paper_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY, ts_start REAL, ts_stage REAL, buy_asset TEXT,"
                " sell_asset TEXT, result TEXT)")
    con.executemany("INSERT INTO cycles (ts_start, ts_stage, buy_asset, sell_asset, result) VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()


def _history_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, ts REAL, buy_ex TEXT, sell_ex TEXT, asset_buy TEXT,"
                " asset_sell TEXT, profit REAL, ref REAL, amount REAL)")
    con.executemany("INSERT INTO history (ts, buy_ex, sell_ex, asset_buy, asset_sell, profit, ref, amount)"
                    " VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def test_run_gates_and_sample_sizes(tmp_path):
    n = 24 * 40
    wave = lambda i: 100000 * (1 + 0.01 * math.sin(i / 3))      # noqa: E731
    ds = {"coins": {"BTC": {"spot": {"klines": kl(n, wave)}, "perp": [perp_seg(n, wave)],
                            "bingx": [perp_seg(n, wave, spec={"qty_step": 0.0001, "min_qty": 0.0001,
                                                              "min_notional": 2})]}}}
    pdb, hdb = str(tmp_path / "paper.db"), str(tmp_path / "history.db")
    t = T0 / 1000
    _paper_db(pdb, [(t, t + 540, "USDT", "USDT", "done"), (t, t + 600, "BTC", "USDT", "done"),
                    (t, t + 60, "USDT", "USDT", "failed_buy")])
    _history_db(hdb, [(t + 3600 * 5, "MEXC", "Bybit", "USDT", "BTC", 3.0, 92.0, None),
                      (t + 3600 * 5 + 300, "MEXC", "Bybit", "USDT", "BTC", 3.1, 92.0, None),
                      (t + 3600 * 30, "MEXC", "Bybit", "BTC", "USDT", 2.0, 92.0, None)])
    res = hb.run(ds, [pdb, str(tmp_path / "missing.db")], hdb, hb.Params(coins=("BTC", "TON")), log=lambda *a: None)
    assert res["paper"]["cycles"] == 3 and res["paper"]["done"] == 2 and res["rub_per_usdt"] == 92.0
    assert res["windows_min"] == [10, 30, 60, 120, 360]           # медиана завершённых кругов — 10 мин
    btc = res["coins"]["BTC"]
    assert btc["paper_cycles"] == 1 and btc["history_signals"] == 3 and btc["history_episodes"] == 2
    assert btc["main"]["minutes"] == 60 and btc["cheaper_venue"] == "bingx"
    assert btc["gates"]["sigma_ratio_le_0.5"] and not btc["gates"]["cost_le_0.6_buffer"]   # 0.24% > 0.18%
    assert btc["main_fees_x1"]["cost_mean_pct"] < btc["main"]["cost_mean_pct"]
    assert btc["signal_windows"]["n"] == 2 and btc["lots"]["bingx"]["10000"]["in_band_share"] == 1.0
    assert not res["coins"]["TON"]["ok"]
    lines = hb.summary_ru(res)
    assert lines[0].startswith("Бумага") and any(x.startswith("BTC") for x in lines)
