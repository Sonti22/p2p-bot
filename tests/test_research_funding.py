"""research/funding_bt.py на синтетической истории: учёт выплат, комиссий ×2, базиса, ребалансировки, отрезков."""
import pytest

from research import funding_bt as fb
from research import metrics

H = fb.H
T0 = 59027 * 8 * H          # кратно 8 ч — выплаты в свечах


def kl(start, n, price=lambda i: 100.0):
    return [[start + i * H, price(i), price(i), price(i), price(i), 1.0] for i in range(n)]


def fund(start, n_hours, rate, step_h=8, mark=None):
    rows = []
    for i in range(0, n_hours, step_h):
        ts = start + i * H
        r = rate(i) if callable(rate) else rate
        rows.append([ts, r] + ([mark(i)] if mark else []))
    return rows


def seg(sym, start, n, price=lambda i: 100.0, rate=0.0003, step_h=8, mark=None):
    return {"symbol": sym, "start": start, "end": start + n * H, "klines": kl(start, n, price),
            "funding": fund(start, n, rate, step_h, mark), "spec": {"funding_interval_min": step_h * 60}}


def test_no_entry_below_threshold():
    r = fb.spot_perp("BTC", kl(T0, 24 * 60), [seg("BTCUSDT", T0, 24 * 60, rate=0.0001)])   # ≈ 11% годовых
    assert r["ok"] and r["entries"] == 0 and r["net_pnl_usdt"] == 0 and r["net_apr_pct"] == 0


def test_spot_perp_accounting_exact():
    """Ставка 0.03% за 8 ч (≈ 33% годовых), цены стоят: чистый итог = выплаты − комиссии×2 − проскальзывание."""
    n = 24 * 60
    r = fb.spot_perp("BTC", kl(T0, n), [seg("BTCUSDT", T0, n, rate=0.0003)])
    # вход на первой выплате после 72 ч окна (t = 72 ч), выплаты с 80 ч до последней (< 60 дней)
    paid = len(range(80, n, 8))
    assert r["entries"] == 1 and r["forced_closes"] == 1 and r["settlements_in_position"] == paid
    per_side = (1000 * 0.10 + 1000 * 0.055) * 2 / 100 + 1000 * 0.02 * 2 / 100    # 3.1 + 0.4
    assert r["items_usdt"]["funding"] == pytest.approx(paid * 0.3, abs=0.01)
    assert r["items_usdt"]["fees"] == pytest.approx(-6.2, abs=0.01)
    assert r["items_usdt"]["slippage"] == pytest.approx(-0.8, abs=0.01)
    assert r["net_pnl_usdt"] == pytest.approx(paid * 0.3 - 2 * per_side, abs=0.01)
    assert r["capital_usdt"] == 1500
    assert r["max_dd_pct"] > 0          # вход сразу списывает издержки
    # комиссии ×1 дают больше
    r1 = fb.spot_perp("BTC", kl(T0, n), [seg("BTCUSDT", T0, n, rate=0.0003)], fb.Params(fee_mult=1.0))
    assert r1["net_pnl_usdt"] == pytest.approx(r["net_pnl_usdt"] + 3.1, abs=0.01)


def test_exit_when_funding_fades():
    n = 24 * 60
    rate = lambda i: 0.0003 if i < 24 * 30 else 0.0      # noqa: E731
    r = fb.spot_perp("ETH", kl(T0, n), [seg("ETHUSDT", T0, n, rate=rate)])
    assert r["entries"] == 1 and r["forced_closes"] == 0
    # 30 дней выплат по 0.3 минус выплаты до входа; выход, когда в окне 72 ч не осталось ненулевых ставок
    assert r["items_usdt"]["funding"] == pytest.approx(len(range(80, 24 * 30, 8)) * 0.3, abs=0.01)
    assert r["days_in_position"] < 34


def test_basis_widening_costs_the_short_leg():
    n = 24 * 20
    spot = kl(T0, n)
    perp = seg("BTCUSDT", T0, n, price=lambda i: 100.0 + (0.5 if i > 200 else 0.0), rate=0.0005)
    r = fb.spot_perp("BTC", spot, [perp])
    assert r["items_usdt"]["basis"] == pytest.approx(-10 * 0.5, abs=1e-6)    # q=10 монет × 0.5
    assert r["max_dd_pct"] > 0.3


def test_rebalance_after_big_move():
    n = 24 * 30
    price = lambda i: 100.0 if i < 300 else 130.0        # noqa: E731
    r = fb.spot_perp("BTC", kl(T0, n, price), [seg("BTCUSDT", T0, n, price=price, rate=0.0005)])
    assert r["rebalances"] == 1 and r["entries"] == 1
    assert r["items_usdt"]["fees"] < -6.2 * 1.5           # закрыли и открыли заново — ещё комиссии


def test_segments_force_close_and_reenter():
    """TON: TONUSDT закрылся (поставка), GRAMUSDT открылся через неделю — позиция закрыта и открыта снова."""
    n1, gap, n2 = 24 * 20, 24 * 7, 24 * 20
    s2 = T0 + (n1 + gap) * H
    spot = kl(T0, n1 + gap + n2)
    segs = [seg("TONUSDT", T0, n1, rate=0.0005, step_h=4), seg("GRAMUSDT", s2, n2, rate=0.0005, step_h=4)]
    r = fb.spot_perp("TON", spot, segs)
    assert r["entries"] == 2 and r["forced_closes"] == 2 and r["segments"] == ["TONUSDT", "GRAMUSDT"]
    assert r["settlements_in_position"] == len(range(76, n1, 4)) + len(range(76, n2, 4))


def test_perp_perp_direction_and_accounting():
    n = 24 * 40
    a = seg("BTCUSDT", T0, n, rate=0.0004)
    b = seg("BTC-USDT", T0, n, rate=0.0)
    r = fb.perp_perp("BTC", [a], [b])
    paid = len(range(80, n, 8))
    assert r["entries"] == 1 and r["capital_usdt"] == 1000
    assert r["items_usdt"]["funding"] == pytest.approx(paid * 0.4, abs=0.01)   # шорт на Bybit получает
    assert r["items_usdt"]["fees"] == pytest.approx(-2 * (0.055 + 0.05) * 2 * 10, abs=0.01)
    # обратная ситуация: фандинг выше на BingX — шорт там, выплаты те же
    r2 = fb.perp_perp("BTC", [seg("BTCUSDT", T0, n, rate=0.0)], [seg("BTC-USDT", T0, n, rate=0.0004)])
    assert r2["items_usdt"]["funding"] == pytest.approx(paid * 0.4, abs=0.01)


def test_perp_perp_uses_mark_price_when_bingx_has_no_klines():
    n = 24 * 30
    a = seg("BTCUSDT", T0, n, rate=0.0004)
    b = seg("BTC-USDT", T0, n, rate=0.0, mark=lambda i: 100.2)
    b["klines"] = []
    r = fb.perp_perp("BTC", [a], [b])
    assert r["ok"] and r["entries"] == 1
    assert r["items_usdt"]["basis"] == pytest.approx(0.0, abs=1e-9)        # разница цен не менялась
    assert r["items_usdt"]["funding"] > 0


def test_perp_perp_no_overlap_reports_reason():
    a = seg("BTCUSDT", T0, 24 * 10)
    b = seg("BTC-USDT", T0 + 24 * 20 * H, 24 * 10)
    r = fb.perp_perp("BTC", [a], [b])
    assert not r["ok"] and r["reason"]


def test_run_and_summary():
    n = 24 * 40
    ds = {"coins": {"BTC": {"spot": {"klines": kl(T0, n)}, "perp": [seg("BTCUSDT", T0, n, rate=0.0004)],
                            "bingx": [seg("BTC-USDT", T0, n, rate=0.0001, mark=lambda i: 100.0)]}}}
    res = fb.run(ds, log=lambda *a: None)
    assert res["spot_perp"]["BTC"]["ok"] and res["perp_perp"]["BTC"]["ok"]
    assert res["spot_perp"]["BTC"]["fees_x1_apr_pct"] > res["spot_perp"]["BTC"]["net_apr_pct"]
    assert res["profile"]["BTC"]["bybit"]["positive_share"] == 1.0
    lines = fb.summary_ru(res)
    assert len(lines) == 2 and all("BTC" in x for x in lines)


def test_metrics_helpers():
    assert metrics.max_drawdown([100, 110, 99, 120, 60]) == pytest.approx(0.5)
    assert metrics.percentile([1, 2, 3, 4], 50) == 2.5
    assert metrics.profit_factor([2, -1, 3, -1]) == 2.5
    d = metrics.daily_last([(0, 1), (3600_000, 2), (86_400_000, 3)])
    assert d == [(0, 2), (86_400_000, 3)]


def test_since_cuts_segments_and_last_12m_matches_short_history():
    a, b = seg("TONUSDT", T0, 100), seg("GRAMUSDT", T0 + 200 * H, 100)
    cut = fb.since([a, b], T0 + 50 * H)
    assert [s["symbol"] for s in cut] == ["TONUSDT", "GRAMUSDT"]
    assert cut[0]["start"] == T0 + 50 * H and cut[0]["klines"][0][0] == T0 + 50 * H
    assert all(f[0] >= T0 + 50 * H for f in cut[0]["funding"]) and cut[1] == b
    assert [s["symbol"] for s in fb.since([a, b], T0 + 150 * H)] == ["GRAMUSDT"]
    n = 24 * 40
    ds = {"coins": {"BTC": {"spot": {"klines": kl(T0, n)}, "perp": [seg("BTCUSDT", T0, n, rate=0.0004)],
                            "bingx": []}}}
    r = fb.run(ds, log=lambda *a: None)["spot_perp"]["BTC"]
    assert r["last_12m"]["net_apr_pct"] == r["net_apr_pct"]    # истории меньше года — те же числа
