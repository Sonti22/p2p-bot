"""Торговое ядро, gates: пороги плана «бумага → кнопка → автомат», укороченная бумага при сильном бэктесте
(решение владельца), minlot сразу после сильного бэктеста, автомат — только при пройденных порогах; математика."""
import math

import pytest

from trading import gates, switch

HEDGE_PAPER = {"days": 14, "count": 50, "ratio_ok_share": 0.95, "cost_to_buffer": 0.6, "sigma_ratio": 0.5}
HEDGE_LIVE = {"days": 21, "count": 30, "model_divergence_pp": 0.05, "unknown_over_10min": 0, "limit_violations": 0}
HEDGE_BT = {"days": 90, "count": 50, "ratio_ok_share": 0.95, "cost_to_buffer": 0.6, "sigma_ratio": 0.5}
FUND_PAPER = {"days": 60, "payments": 90, "apr_over_earn_pp": 3, "max_drawdown": 0.02}
FUND_LIVE = {"days": 30, "cycles": 20, "within_paper_1sigma": True, "unknown_over_10min": 0, "limit_violations": 0}
FUND_BT = {"days": 180, "payments": 540, "apr_over_earn_pp": 3, "max_drawdown": 0.02}
DIR_BT = {"months_oos": 12, "trades": 200, "sharpe": 1.0, "profit_factor": 1.2, "p_value_vs_random": 0.049}
DIR_PAPER = {"days": 90, "trades": 50, "in_backtest_ci90": True}
DIR_LIVE = {"days": 60, "trades": 30, "max_drawdown": 0.099, "unknown_over_10min": 0, "limit_violations": 0}
MAKER_PAPER = {"days": 30, "net_after_fee": 0.01}
MAKER_LIVE = {"confirmed_edits": 100, "corridor_exits": 0, "unknown_over_10min": 0, "limit_violations": 0}


def test_modes_match_switch():
    assert gates.MODES == switch.MODES


@pytest.mark.parametrize("strategy,paper", [("hedge", HEDGE_PAPER), ("funding", FUND_PAPER), ("maker", MAKER_PAPER)])
def test_paper_to_button_exact_thresholds_pass(strategy, paper):
    assert gates.paper_to_button(strategy, paper).passed


@pytest.mark.parametrize("strategy,paper,key,value", [
    ("hedge", HEDGE_PAPER, "days", 13.9), ("hedge", HEDGE_PAPER, "count", 49),
    ("hedge", HEDGE_PAPER, "ratio_ok_share", 0.9499), ("hedge", HEDGE_PAPER, "cost_to_buffer", 0.61),
    ("hedge", HEDGE_PAPER, "sigma_ratio", 0.51), ("funding", FUND_PAPER, "days", 59),
    ("funding", FUND_PAPER, "payments", 89), ("funding", FUND_PAPER, "apr_over_earn_pp", 2.99),
    ("funding", FUND_PAPER, "max_drawdown", 0.021), ("maker", MAKER_PAPER, "net_after_fee", 0),
    ("maker", MAKER_PAPER, "days", 29),
    ("hedge", HEDGE_PAPER, "count", None), ("hedge", HEDGE_PAPER, "count", float("nan")),
    ("hedge", HEDGE_PAPER, "count", "много"), ("hedge", HEDGE_PAPER, "count", True),
])
def test_paper_to_button_each_threshold_fails(strategy, paper, key, value):
    g = gates.paper_to_button(strategy, dict(paper, **{key: value}))
    assert not g.passed and any(key in f or "нужно" in f for f in g.failures)


@pytest.mark.parametrize("strategy,paper", [("hedge", HEDGE_PAPER), ("funding", FUND_PAPER)])
def test_missing_stat_fails_closed(strategy, paper):
    for key in paper:
        g = gates.paper_to_button(strategy, {k: v for k, v in paper.items() if k != key})
        assert not g.passed and any("нет данных" in f and key in f for f in g.failures)
    assert not gates.paper_to_button(strategy, None).passed and not gates.paper_to_button(strategy, []).passed


def test_directional_needs_strong_backtest_and_ci():
    assert not gates.paper_to_button("directional", DIR_PAPER).passed               # без бэктеста — нет
    assert gates.paper_to_button("directional", DIR_PAPER, DIR_BT).passed is True   # укороченная (30 дней) — пройдена
    full_bt = {"days": 90, "trades": 50, "in_backtest_ci90": True}
    assert gates.paper_to_button("directional", full_bt, DIR_BT).passed
    for key, value in (("months_oos", 11), ("trades", 199), ("sharpe", 0.99), ("profit_factor", 1.19),
                       ("p_value_vs_random", 0.05)):
        g = gates.paper_to_button("directional", DIR_PAPER, dict(DIR_BT, **{key: value}))
        assert not g.passed and "бэктест не прошёл" in g.failures[0]
    assert not gates.paper_to_button("directional", dict(DIR_PAPER, in_backtest_ci90=False), DIR_BT).passed
    assert not gates.paper_to_button("directional", dict(DIR_PAPER, in_backtest_ci90=1), DIR_BT).passed


def test_short_paper_with_strong_backtest():
    """Сильный бэктест: бумага хеджа/фандинга — 14 дней, направленной — 30 (решение владельца); качество — как в плане."""
    short_f = {"days": 14, "payments": 42, "apr_over_earn_pp": 3, "max_drawdown": 0.02}
    assert not gates.paper_to_button("funding", short_f).passed
    assert gates.paper_to_button("funding", short_f, FUND_BT).passed
    assert not gates.paper_to_button("funding", dict(short_f, days=13), FUND_BT).passed
    assert not gates.paper_to_button("funding", dict(short_f, apr_over_earn_pp=2.5), FUND_BT).passed
    assert not gates.paper_to_button("funding", short_f, dict(FUND_BT, days=179)).passed   # бэктест слабый
    short_d = {"days": 30, "trades": 15, "in_backtest_ci90": True}
    assert gates.paper_to_button("directional", short_d, DIR_BT).passed
    assert not gates.paper_to_button("directional", dict(short_d, days=29), DIR_BT).passed
    assert not gates.paper_to_button("directional", dict(short_d, trades=14), DIR_BT).passed
    assert not gates.backtest_strong("maker", MAKER_PAPER).passed                # мейкеру бэктест бумагу не заменяет


@pytest.mark.parametrize("strategy,live,key,value", [
    ("hedge", HEDGE_LIVE, "days", 20), ("hedge", HEDGE_LIVE, "count", 29),
    ("hedge", HEDGE_LIVE, "model_divergence_pp", 0.051), ("hedge", HEDGE_LIVE, "unknown_over_10min", 1),
    ("hedge", HEDGE_LIVE, "limit_violations", 1), ("funding", FUND_LIVE, "cycles", 19),
    ("funding", FUND_LIVE, "within_paper_1sigma", False), ("directional", DIR_LIVE, "max_drawdown", 0.10),
    ("directional", DIR_LIVE, "trades", 29), ("maker", MAKER_LIVE, "corridor_exits", 1),
    ("maker", MAKER_LIVE, "confirmed_edits", 99), ("funding", FUND_LIVE, "unknown_over_10min", 2),
])
def test_button_to_auto_thresholds(strategy, live, key, value):
    assert gates.button_to_auto(strategy, live).passed
    assert not gates.button_to_auto(strategy, dict(live, **{key: value})).passed


def test_auto_common_required_for_every_strategy():
    for strategy, live in (("hedge", HEDGE_LIVE), ("funding", FUND_LIVE), ("directional", DIR_LIVE),
                           ("maker", MAKER_LIVE)):
        g = gates.button_to_auto(strategy, {k: v for k, v in live.items() if k != "limit_violations"})
        assert not g.passed and any("limit_violations" in f for f in g.failures)


def test_max_mode_ladder_and_allow():
    assert gates.max_mode("hedge") == "paper"
    assert gates.max_mode("hedge", backtest=HEDGE_BT) == "minlot"                # сразу после сильного бэктеста
    assert gates.max_mode("hedge", HEDGE_PAPER) == "confirm"
    assert gates.max_mode("hedge", HEDGE_PAPER, live=HEDGE_LIVE) == "auto"
    assert gates.max_mode("hedge", backtest=HEDGE_BT, live=HEDGE_LIVE) == "minlot"   # автомат — только после бумаги
    assert gates.max_mode("directional", DIR_PAPER) == "paper"
    assert gates.max_mode("directional", DIR_PAPER, DIR_BT, DIR_LIVE) == "auto"
    assert gates.max_mode("maker", MAKER_PAPER, backtest={"anything": 1}) == "confirm"
    g = gates.allow("hedge", "auto", HEDGE_PAPER, live=dict(HEDGE_LIVE, unknown_over_10min=1))
    assert not g.passed and "только confirm" in g.failures[0] and any("unknown" in f for f in g.failures)
    assert gates.allow("hedge", "minlot", backtest=HEDGE_BT).passed
    assert not gates.allow("hedge", "minlot").passed and "нет бэктеста" in gates.allow("hedge", "minlot").failures
    assert gates.allow("hedge", "paper").passed
    assert not gates.allow("hedge", "turbo").passed
    assert gates.allow("unknown", "paper").passed                                  # бумага разрешена всегда
    assert not gates.allow("arb", "minlot", backtest=HEDGE_BT).passed


def test_effective_mode_with_switch(monkeypatch):
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "auto")
    assert switch.effective_mode(gates.max_mode("hedge", backtest=HEDGE_BT)) == "minlot"
    monkeypatch.setenv("TRADING_MODE", "minlot")
    assert switch.effective_mode(gates.max_mode("hedge", HEDGE_PAPER, live=HEDGE_LIVE)) == "minlot"


# --- математика ---

def test_math_helpers():
    assert gates.mean([1, 2, 3]) == 2 and gates.mean([]) is None
    assert gates.stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.138089935299395)
    assert gates.stdev([1]) is None
    assert gates.sharpe([0.01, 0.02, 0.03], 252) == pytest.approx(0.02 / 0.01 * math.sqrt(252))
    assert gates.sharpe([0.01, 0.01], 252) is None
    assert gates.profit_factor([10, -5, 5, -5]) == 1.5
    assert gates.profit_factor([1, 2]) == math.inf and gates.profit_factor([]) is None
    assert gates.profit_factor([-1]) == 0
    assert gates.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert gates.max_drawdown([100, 101]) == 0
    with pytest.raises(ValueError):
        gates.max_drawdown([100, 0])
    lo, hi = gates.ci90(0.5, 2.0, 16)
    assert (lo, hi) == pytest.approx((0.5 - 1.6448536 * 0.5, 0.5 + 1.6448536 * 0.5))
    assert gates.within_ci90(1.3, 0.5, 2.0, 16) and not gates.within_ci90(1.33, 0.5, 2.0, 16)
    assert not gates.within_ci90(0.5, 0.5, None, 16) and not gates.within_ci90(0.5, 0.5, 1, 0)
    assert gates.within_sigma(1.09, 1.0, 0.1) and gates.within_sigma(0.91, 1.0, 0.1)
    assert not gates.within_sigma(1.11, 1.0, 0.1)
    assert not gates.within_sigma(1.0, 1.0, None)
