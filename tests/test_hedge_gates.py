"""Пороги «бумага → кнопка» бумажного хеджа (simperp.gate_stats/gate_check/gate_lines) по paper.db: число закрытых
хеджей и дни, доля кругов с коэффициентом 1 ± 0.1, стоимость хеджа против RISK_BUFFER, σ(факт − план) с хеджем и без;
строка «выполнено X из Y» в /paper report."""
import json
import sqlite3

import p2p
import paper
import simperp
from helpers import make_ad
from test_bot import Stub

DAY = 86400
NOW = 1_790_000_000.0


def _hedge(db, ts_open, ratio=1.0, closed=True, x=0.0, fees=0.1, asset="BTC", result="done"):
    """Круг с хеджем: план 1%, факт 1 + x (без хеджа), хедж вернул −0.9·x п.п. — с хеджем отклонение 0.1·x."""
    b = make_ad("Bybit", "buy", 6_000_000.0, asset=asset)
    cid = paper.start_cycle(10000, b, make_ad("Bybit", "sell", 90.0), "спот", 1.0, path=db, ts=ts_open,
                            planned_raw=1.0)
    st = {"status": "closed" if closed else "open", "ts_open": ts_open, "ratio": ratio, "exp_cost_pct": 0.0,
          "ref_close": 90.0, "spread_usdt": 0.0}
    if closed:
        st["pnl_pct"] = -0.9 * x
    con = sqlite3.connect(db)
    con.execute("UPDATE cycles SET hedge_state = ?, hedge_qty = 0.0015, hedge_fees = ?, hedge_funding = 0.0, "
                "result = ?, realized_pct = ? WHERE id = ?",
                (json.dumps(st), fees, result if closed else None, 1.0 + x if closed else None, cid))
    con.commit()
    con.close()


def test_all_thresholds_passed(tmp_path):
    db = str(tmp_path / "paper.db")
    for i in range(50):                                   # 50 закрытых за 15 дней
        _hedge(db, NOW - 15 * DAY + i * 600, x=(1.0 if i % 2 else -1.0))
    st = simperp.gate_stats(db, now=NOW, buffers={"BTC": 0.3})
    assert st["count"] == 50 and 15 <= st["days"] < 15.5 and st["ratio_ok_share"] == 1.0
    assert abs(st["cost_to_buffer"] - 0.09 / 0.3) < 1e-9   # 0.1 USDT × 90 ₽ / 10 000 ₽ = 0.09% круга
    assert abs(st["sigma_ratio"] - 0.1) < 1e-9 and st["pairs"] == 50
    lines = simperp.gate_lines(db, now=NOW)
    assert lines[0] == "пороги бумага → кнопка: выполнено 5 из 5" and all(x.startswith("✅") for x in lines[1:])


def test_each_threshold_can_fail(tmp_path):
    db = str(tmp_path / "paper.db")
    for i in range(9):
        _hedge(db, NOW - 2 * DAY + i * 600, x=0.0, fees=0.5)       # σ без хеджа 0 — отношения нет
    _hedge(db, NOW - DAY, ratio=0.7, x=0.0, fees=0.5)             # коэффициент вне полосы: 90% в полосе
    st = simperp.gate_stats(db, now=NOW, buffers={"BTC": 0.3})
    assert st["count"] == 10 and st["ratio_ok_share"] == 0.9 and st["cost_to_buffer"] > 0.6
    assert st["sigma_ratio"] is None
    checks = simperp.gate_check(st)
    assert [ok for ok, _ in checks] == [False, False, False, False, False]
    assert "σ(факт − план) с хеджем нет данных" in checks[4][1]
    assert simperp.gate_lines(db, now=NOW)[0] == "пороги бумага → кнопка: выполнено 0 из 5"


def test_open_hedges_count_for_ratio_and_days_but_not_count(tmp_path):
    db = str(tmp_path / "paper.db")
    _hedge(db, NOW - 20 * DAY, closed=False)
    _hedge(db, NOW - DAY, ratio=1.05)
    st = simperp.gate_stats(db, now=NOW, buffers={"BTC": 0.3})
    assert st["count"] == 1 and st["days"] >= 20 and st["ratio_ok_share"] == 1.0
    assert simperp.gate_stats(db, now=NOW, buffers={})["cost_to_buffer"] is None   # запас монеты неизвестен


def test_coin_buffer_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RISK_BUFFER", "BTC:0.18")
    db = str(tmp_path / "paper.db")
    _hedge(db, NOW - DAY)
    assert abs(simperp.gate_stats(db, now=NOW)["cost_to_buffer"] - 0.5) < 1e-9


def test_paper_report_shows_gate_line(tmp_path):
    _hedge(paper.DB_PATH, NOW - DAY)
    text = Stub(p2p.Config()).paper_report_view(paper.report_rows())
    assert "пороги бумага → кнопка: выполнено" in text and "❌ закрытых хеджей 1 (нужно ≥ 50)" in text


def test_no_hedges_no_gate_lines(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.start_cycle(10000, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 86.0), "r", 1.0, path=db)
    assert simperp.report_lines(db) == []
    assert simperp.gate_stats(db)["count"] == 0
