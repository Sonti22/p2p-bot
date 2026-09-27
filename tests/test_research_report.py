"""research/report.py: вердикты против порогов плана и сквозной прогон отчёта на синтетических данных (без сети)."""
import json
import math
import os

from research import data, report

H = 3_600_000
T0 = 59027 * 8 * H


def _fund_result(apr, dd=1.0, years=2.0, settlements=200, ok=True):
    if not ok:
        return {"ok": False, "reason": "нет данных для оценки"}
    return {"ok": True, "period": {"years": years}, "settlements_in_position": settlements, "net_apr_pct": apr,
            "earn_apr_pct": 5.0, "max_dd_pct": dd}


def test_verdicts_follow_plan_gates():
    fund = {"spot_perp": {"BTC": _fund_result(9.0), "TON": _fund_result(9.0, years=0.5)},
            "perp_perp": {"BTC": _fund_result(7.9), "ETH": _fund_result(0, ok=False)}}
    v = report.funding_verdicts(fund)
    assert v["spot_perp:BTC"]["go"] and not v["spot_perp:TON"]["go"]
    assert not v["perp_perp:BTC"]["go"] and v["perp_perp:ETH"] == {"go": False, "checks": {}, "reason": "нет данных для оценки"}
    assert not report.funding_verdicts({"spot_perp": {"BTC": _fund_result(9.0, dd=2.5)}})["spot_perp:BTC"]["go"]

    good = {"portfolio": {"months": 20, "trades": 250, "sharpe": 1.3, "profit_factor": "inf", "mean_ret_pct": 0.1},
            "random": {"p_value": 0.01, "p_value_by_coin": {"BTC": 0.02}},
            "coins": {"BTC": {"ok": True, "walk_forward": {"sharpe": 1.3, "profit_factor": 1.5}}}}
    dv = report.directional_verdict(good)
    assert dv["go"] and dv["edge"] and dv["by_coin"]["BTC"]["p_lt_0.05"]
    good["random"]["p_value"] = 0.2
    assert not report.directional_verdict(good)["go"]
    assert not report.directional_verdict({})["go"]

    hres = {"coins": {"TON": {"ok": True, "paper_cycles": 0, "history_signals": 0,
                              "gates": {"cost_le_0.6_buffer": True, "sigma_ratio_le_0.5": True,
                                        "lot_coef_in_band_95": True}},
                      "ETH": {"ok": False, "reason": "нет рыночной истории"}}}
    hv = report.hedge_verdicts(hres)
    assert hv["TON"]["go"] and not hv["TON"]["applicable"] and not hv["ETH"]["go"]


def _kl(n, price, start=T0):
    return [[start + i * H, price(i), price(i) * 1.002, price(i) * 0.998, price(i + 1), 1.0] for i in range(n)]


def _dataset(n=24 * 30 * 8):
    wave = lambda i: 100 * (1 + 0.1 * math.sin(2 * math.pi * i / 400))   # noqa: E731
    spec = {"qty_step": 0.1, "min_qty": 0.1, "min_notional": 5, "funding_interval_min": 480}
    perp = {"venue": "bybit", "symbol": "BTCUSDT", "start": T0, "end": T0 + n * H, "spec": spec,
            "klines": _kl(n, wave), "funding": [[T0 + i * H, 0.0004 if (i // 720) % 2 else 0.00005]
                                                for i in range(0, n, 8)]}
    bingx = dict(perp, venue="bingx", symbol="BTC-USDT",
                 funding=[[T0 + i * H, 0.0001, wave(i)] for i in range(0, n, 8)])
    return {"meta": {"start": T0, "end": T0 + n * H, "notes": ["синтетика"]},
            "coins": {"BTC": {"spot": {"venue": "bybit", "symbol": "BTCUSDT", "klines": _kl(n, wave)},
                              "perp": [perp], "bingx": [bingx]}}}


def _tables_consistent(md):
    block = []
    for line in md.splitlines() + [""]:
        if line.startswith("|"):
            block.append(line.count("|"))
        else:
            if block and len(set(block)) != 1:
                return False
            block = []
    return True


def test_main_end_to_end_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "load_dataset", lambda http, start, end, **kw: _dataset())
    out = tmp_path / "rep"
    assert report.main(["--out", str(out), "--offline", "--sims", "20"]) == 0
    rep = json.loads((out / "backtest_report.json").read_text(encoding="utf-8"))
    assert set(rep["verdicts"]) == {"funding", "directional", "hedge"}
    assert rep["args"]["sims"] == 20 and rep["data"]["coverage"]["BTC"]["spot"]["n"] == 24 * 30 * 8
    assert "spot_perp:BTC" in rep["verdicts"]["funding"] and rep["directional"]["random"]["sims"] == 20
    md = (out / "backtest_report.md").read_text(encoding="utf-8")
    for part in ("## Итог", "## Данные", "## Арбитраж фандинга", "## Направленная стратегия", "## Хедж P2P-кругов",
                 "## Сомнения и ограничения", "синтетика"):
        assert part in md
    assert ("**GO**" in md) or ("**NO-GO**" in md)
    assert _tables_consistent(md)
    assert not any(name.endswith(".tmp") for name in os.listdir(out))


def test_jsonable_handles_inf_and_tuples():
    assert report._jsonable({"a": float("inf"), "b": (1, float("nan"))}) == {"a": "inf", "b": [1, "nan"]}
