"""Торговое ядро, risk: жёсткие потолки плана, .env только понижает, minlot, дневной стоп (МСК), ликвидация, лимитки
±0.5% и глубина для рынка, частота ордеров, свежесть данных, unknown, режим маржи с биржи, закрытие всегда можно."""
from decimal import Decimal as D

import pytest

from trading import risk

NOW = 1_700_000_000


def req(**over):
    base = dict(strategy="hedge", venue="bybit", category="linear", symbol="BTCUSDT", side="sell",
                order_type="market", qty=D("0.0007"), leverage=D(2), stop_loss=D("66000"), hedge_ref_qty=D("0.0007"))
    base.update(over)
    return risk.OpenRequest(**base)


def ctx(**over):
    base = dict(now=NOW, mark=D("65000"), ticker_ts=NOW - 1, clock_skew=D("0.2"),
                book=[(D("64990"), D("0.5")), (D("64960"), D("1"))], est_liq=None, positions=[],
                realized_today=D(0), unrealized=D(0), capital=D("5000"), unknown=0, orders_min=0, orders_day=0,
                margin_mode="isolated")
    base.update(over)
    return risk.Context(**base)


def ok(r, c, mode="minlot", env=None):
    v = risk.check_open(r, c, mode, env or {})
    assert v.ok, v.reasons
    return v


def bad(r, c, mode="minlot", env=None, why=""):
    v = risk.check_open(r, c, mode, env or {})
    assert not v.ok and any(why in x for x in v.reasons), v.reasons
    return v


def test_hard_caps_are_the_plan_table():
    h = risk.HARD
    assert h["leverage"] == {"hedge": 3, "funding": 3, "directional": 2} and risk.DEFAULT_LEVERAGE == 2
    assert h["position_usdt"] == {"hedge": 1000, "funding": 1000, "directional": 200} and h["total_usdt"] == 2000
    assert (h["daily_loss_usdt"], h["daily_loss_share"]) == (50, D("0.02"))
    assert h["max_groups"] == {"hedge": 3, "funding": 2, "directional": 1}
    assert h["liq_open"] == {"hedge": D("0.30"), "funding": D("0.30"), "directional": D("0.15")}
    assert (h["liq_alert"], h["liq_reduce"]) == (D("0.20"), D("0.12"))
    assert (h["limit_band"], h["depth_band"], h["orders_per_min"], h["orders_per_day"]) == (D("0.005"), D("0.001"),
                                                                                              10, 100)
    assert (h["ticker_age_s"], h["clock_skew_s"], h["hedge_qty_ratio"]) == (3, 1, D("1.05"))
    assert risk.MINLOT == {"position_usdt": 50, "daily_loss_usdt": 5, "leverage": 2}
    assert all(isinstance(v, D) for v in (h["total_usdt"], h["daily_loss_usdt"], h["limit_band"]))


def test_base_request_passes_in_every_live_mode():
    for mode in ("minlot", "confirm", "auto"):
        v = ok(req(), ctx(), mode)
        assert v.notional == D("45.5000") and v.notes == ()


@pytest.mark.parametrize("mode", ["paper", "", None, "AUTO", "turbo"])
def test_paper_or_unknown_mode_never_opens(mode):
    bad(req(), ctx(), mode, why="реальных открытий нет")


# --- .env только понижает ---

@pytest.mark.parametrize("env,key,value", [
    ({"TRADING_MAX_POSITION_USDT": "100000"}, "position_usdt", D(1000)),    # выше потолка — потолок
    ({"TRADING_MAX_POSITION_USDT": "300"}, "position_usdt", D(300)),         # ниже — понижает
    ({"TRADING_MAX_POSITION_USDT": "мусор"}, "position_usdt", D(0)),         # мусор — 0 (открытий нет)
    ({"TRADING_MAX_POSITION_USDT": "-5"}, "position_usdt", D(0)),
    ({"TRADING_MAX_POSITION_USDT": "inf"}, "position_usdt", D(0)),
    ({"TRADING_MAX_POSITION_USDT": "NaN"}, "position_usdt", D(0)),
    ({"TRADING_MAX_POSITION_USDT": ""}, "position_usdt", D(1000)),           # пусто — потолок
    ({"TRADING_MAX_LEVERAGE": "10"}, "leverage", D(3)), ({"TRADING_MAX_LEVERAGE": "1"}, "leverage", D(1)),
    ({"TRADING_MAX_TOTAL_USDT": "99999"}, "total_usdt", D(2000)),
    ({"TRADING_DAILY_LOSS_USDT": "1000"}, "daily_loss_usdt", D(50)),
    ({"TRADING_DAILY_LOSS_USDT": "20"}, "daily_loss_usdt", D(20)),
    ({"TRADING_MAX_ORDERS_PER_MIN": "600"}, "orders_per_min", D(10)),
    ({"TRADING_MAX_ORDERS_PER_DAY": "5"}, "orders_per_day", D(5)),
])
def test_env_can_only_lower(env, key, value):
    assert risk.limits("hedge", "confirm", env)[key] == value


def test_env_can_only_lower_minlot_and_directional_ceiling():
    lim = risk.limits("hedge", "minlot", {"TRADING_MINLOT_POSITION_USDT": "500", "TRADING_MINLOT_DAILY_LOSS_USDT": "50",
                                          "TRADING_MAX_LEVERAGE": "3"})
    assert (lim["position_usdt"], lim["daily_loss_usdt"], lim["leverage"]) == (D(50), D(5), D(2))
    lim = risk.limits("hedge", "minlot", {"TRADING_MINLOT_POSITION_USDT": "20", "TRADING_MINLOT_DAILY_LOSS_USDT": "2"})
    assert (lim["position_usdt"], lim["daily_loss_usdt"]) == (D(20), D(2))
    assert risk.limits("directional", "auto", {"TRADING_MAX_LEVERAGE": "3", "TRADING_MAX_POSITION_USDT": "1000"})[
        "leverage"] == D(2)
    assert risk.limits("directional", "auto", {"TRADING_MAX_POSITION_USDT": "1000"})["position_usdt"] == D(200)


def test_env_from_process_by_default(monkeypatch):
    monkeypatch.setenv("TRADING_MAX_POSITION_USDT", "40")
    assert risk.limits("hedge", "confirm")["position_usdt"] == D(40)
    v = risk.check_open(req(), ctx(), "confirm")                     # environ не передан → os.environ
    assert not v.ok and any("позиция" in x for x in v.reasons)       # 45.5 > 40


def test_garbage_env_blocks_opens():
    bad(req(), ctx(), "confirm", {"TRADING_MAX_POSITION_USDT": "много"}, why="позиция")


# --- размеры ---

def test_minlot_position_cap_50():
    ok(req(qty=D("0.00076"), hedge_ref_qty=D("0.001")), ctx(), "minlot")   # 49.4 USDT
    bad(req(qty=D("0.00077"), hedge_ref_qty=D("0.001")), ctx(), "minlot", why="позиция")   # 50.05 > 50
    ok(req(qty=D("0.00077"), hedge_ref_qty=D("0.001")), ctx(), "confirm")


def test_strategy_position_caps_and_total():
    ok(req(qty=D("0.0153"), hedge_ref_qty=D("0.0153")), ctx(), "confirm")   # 994.5
    bad(req(qty=D("0.0154"), hedge_ref_qty=D("0.0154")), ctx(), "confirm", why="позиция")


def test_directional_position_cap_200():
    d = dict(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None)
    ok(req(qty=D("0.003"), **d), ctx(), "confirm")                          # 195
    bad(req(qty=D("0.0031"), **d), ctx(), "confirm", why="позиция")          # 201.5


def test_total_open_cap_2000_and_group_counts():
    pos = [{"strategy": "funding", "notional": D("900"), "group": "f1"},
           {"strategy": "funding", "notional": D("900"), "group": "f1"}]
    bad(req(qty=D("0.0035"), hedge_ref_qty=D("0.0035")), ctx(positions=pos), "confirm", why="суммарно")   # 1800+227.5
    ok(req(qty=D("0.003"), hedge_ref_qty=D("0.003")), ctx(positions=pos), "confirm")                     # 1995
    hedges = [{"strategy": "hedge", "notional": D("10"), "group": f"h{i}"} for i in range(3)]
    bad(req(), ctx(positions=hedges), "confirm", why="предел 3")
    ok(req(), ctx(positions=hedges[:2]), "confirm")
    pairs = [{"strategy": "funding", "notional": D("10"), "group": g} for g in ("a", "a", "b", "b")]
    fund = dict(strategy="funding", side="sell", hedge_ref_qty=None)
    bad(req(**fund), ctx(positions=pairs), "confirm", why="предел 2")
    ok(req(group="b", **fund), ctx(positions=pairs), "confirm")             # вторая нога уже открытой пары
    one = [{"strategy": "directional", "notional": D("10")}]
    bad(req(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None), ctx(positions=one),
        "confirm", why="предел 1")


def test_hedge_rules():
    bad(req(hedge_ref_qty=None), ctx(), why="без размера круга")
    ok(req(qty=D("0.00105"), hedge_ref_qty=D("0.001")), ctx(), "confirm")   # ровно × 1.05
    bad(req(qty=D("0.001051"), hedge_ref_qty=D("0.001")), ctx(), "confirm", why="× 1.05")
    bad(req(side="buy", stop_loss=D("64000")), ctx(), why="только шорт")
    bad(req(category="spot", leverage=D(1)), ctx(), why="только шорт")


# --- дневной стоп ---

def test_daily_loss_limit_min_of_50_and_2pct_capital():
    c = dict(capital=D("1000"))                                              # 2% = 20 < 50
    ok(req(), ctx(realized_today=D("-19.99"), **c), "confirm")
    bad(req(), ctx(realized_today=D("-20"), **c), "confirm", why="дневной стоп")
    bad(req(), ctx(realized_today=D("-5"), unrealized=D("-15"), **c), "confirm", why="дневной стоп")
    ok(req(), ctx(realized_today=D("-19"), unrealized=D("500"), **c), "confirm")   # прибыль не гасит убыток дня
    ok(req(), ctx(realized_today=D("-49"), capital=D("100000")), "confirm")
    bad(req(), ctx(realized_today=D("-50"), capital=D("100000")), "confirm", why="дневной стоп")


def test_minlot_daily_loss_5():
    ok(req(), ctx(realized_today=D("-4.99")), "minlot")
    bad(req(), ctx(realized_today=D("-5")), "minlot", why="дневной стоп")
    ok(req(), ctx(realized_today=D("-5")), "confirm")


@pytest.mark.parametrize("capital", [None, D(0), D(-1), "мусор", 1000.0])
def test_unknown_capital_fails_closed(capital):
    bad(req(), ctx(capital=capital), why="")


# --- данные, частота, unknown ---

@pytest.mark.parametrize("over,why", [
    ({"ticker_ts": NOW - 3.5}, "тикер устарел"), ({"ticker_ts": NOW + 2}, "тикер устарел"),
    ({"now": NOW + 0.25, "ticker_ts": NOW - 2.8}, "тикер устарел"),          # float из time.time() — тоже можно
    ({"clock_skew": D("1.5")}, "часы"), ({"clock_skew": D("-1.01")}, "часы"),
    ({"orders_min": 10}, "в минуту"), ({"orders_day": 100}, "в день"), ({"unknown": 1}, "неясным исходом"),
])
def test_data_rate_and_unknown_block(over, why):
    bad(req(), ctx(**over), why=why)


def test_data_rate_boundaries_pass():
    ok(req(), ctx(ticker_ts=NOW - 3, clock_skew=D(1), orders_min=9, orders_day=99))
    ok(req(), ctx(orders_min=4), env={"TRADING_MAX_ORDERS_PER_MIN": "5"})
    bad(req(), ctx(orders_min=5), env={"TRADING_MAX_ORDERS_PER_MIN": "5"}, why="в минуту")


# --- цена и глубина ---

def test_limit_band_half_percent():
    ok(req(order_type="limit", price=D("65325"), stop_loss=D("66500")), ctx())   # +0.5%
    bad(req(order_type="limit", price=D("65326"), stop_loss=D("66500")), ctx(), why="±0.5")
    ok(req(order_type="limit", price=D("64675")), ctx())
    bad(req(order_type="limit", price=D("64674")), ctx(), why="±0.5")


def test_market_depth_within_0_1_percent():
    book = [(D("64990"), D("0.0005")), (D("64935"), D("0.0005")), (D("64900"), D("5"))]   # 0.1% → ≥ 64935
    ok(req(qty=D("0.001"), hedge_ref_qty=D("0.001")), ctx(book=book), "confirm")
    bad(req(qty=D("0.0011"), hedge_ref_qty=D("0.0011")), ctx(book=book), "confirm", why="глубины")
    bad(req(), ctx(book=[]), why="глубины")
    bad(req(), ctx(book=None), why="глубины")
    buy_book = [(D("65010"), D("1"))]
    d = dict(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None)
    ok(req(**d), ctx(book=buy_book), "confirm")
    bad(req(**d), ctx(book=[(D("65066"), D("1"))]), "confirm", why="глубины")


# --- плечо, ликвидация, стоп ---

def test_leverage_caps():
    ok(req(leverage=D(3)), ctx(), "confirm")
    bad(req(leverage=D(4)), ctx(), "confirm", why="плечо")
    bad(req(leverage=D(3)), ctx(), "minlot", why="плечо")                    # minlot — не выше 2
    bad(req(leverage=D("0.5")), ctx(), why="плечо")
    bad(req(strategy="directional", side="buy", leverage=D(3), stop_loss=D("64000"), hedge_ref_qty=None), ctx(),
        "confirm", why="плечо")


def test_liquidation_distance():
    assert risk.liq_distance("sell", D(100), D(2)) == D("0.49")
    assert risk.liq_distance("sell", D(100), D(2), D(125)) == D("0.25")
    assert risk.liq_distance("buy", D(100), D(2), D(101)) == 0                 # цена ликвидации не с той стороны
    ok(req(), ctx(est_liq=D("84500")))                                        # 30% ровно
    bad(req(), ctx(est_liq=D("84400")), why="ликвидации")
    d = dict(strategy="directional", side="buy", stop_loss=D("60000"), hedge_ref_qty=None)
    ok(req(**d), ctx(est_liq=D("55250")), "confirm")                          # 15% ровно
    bad(req(**d), ctx(est_liq=D("55300")), "confirm", why="ликвидации")
    bad(req(**dict(d, stop_loss=D("55000"))), ctx(est_liq=D("55250")), "confirm", why="стоп за ценой ликвидации")


def test_directional_stop_rules():
    d = dict(strategy="directional", hedge_ref_qty=None)
    bad(req(side="buy", stop_loss=None, **d), ctx(), "confirm", why="стоп обязателен")
    bad(req(side="buy", stop_loss=D("65000"), **d), ctx(), "confirm", why="не с той стороны")
    bad(req(side="sell", stop_loss=D("64000"), **d), ctx(), "confirm", why="не с той стороны")
    ok(req(side="sell", stop_loss=D("66000"), **d), ctx(book=[(D("64990"), D("1"))]), "confirm")


# --- режим маржи с биржи ---

@pytest.mark.parametrize("mm", [None, "portfolio", "ISOLATED", "", "мусор"])
def test_unknown_margin_mode_refuses(mm):
    bad(req(), ctx(margin_mode=mm), why="режим маржи неизвестен")


def test_cross_margin_rules_main_account():
    cross = ctx(margin_mode="cross")
    d = dict(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None)
    bad(req(**d), cross, "minlot", why="только изолированная")
    bad(req(), cross, "confirm", why="только в режиме minlot")
    v = ok(req(), cross, "minlot")                                     # стоп 66000: 0.0007×1000×1.5+0.091 = 1.141
    assert v.notes == (risk.CROSS_NOTE,) and "все средства" in v.notes[0]
    bad(req(stop_loss=None), cross, "minlot", why="нужен стоп")
    bad(req(stop_loss=D("64000")), cross, "minlot", why="нужен стоп")         # стоп шорта ниже входа
    bad(req(stop_loss=D("71000")), cross, "minlot", why="худший убыток")      # 0.0007×6000×1.5+0.091 = 6.39 > 5
    ok(req(stop_loss=D("69000")), cross, "minlot")                            # 4.2 + 0.091 = 4.291 ≤ 5
    bad(req(), ctx(margin_mode="cross", realized_today=D("-4")), "minlot", why="худший убыток")   # остаток 1
    fund = dict(strategy="funding", side="sell", hedge_ref_qty=None, stop_loss=D("66000"))
    ok(req(**fund), cross, "minlot")
    ok(req(category="spot", strategy="funding", side="buy", leverage=D(1), hedge_ref_qty=None, stop_loss=None),
       ctx(margin_mode=None, book=[(D("65010"), D("1"))]), "minlot")         # спот — без плеча и режима маржи


def test_worst_stop_loss_math():
    assert risk.worst_stop_loss("sell", D("0.001"), D("65000"), D("66000")) == D("1.5") + D("0.13")
    assert risk.worst_stop_loss("buy", D(2), D(100), D(90)) == D(30) + D("0.4")
    assert risk.worst_stop_loss("buy", D(2), D(100), D(100)) is None and risk.worst_stop_loss("sell", D(1), D(9), D(8)) \
        is None


def test_spot_only_funding_buy():
    spot = dict(category="spot", leverage=D(1), hedge_ref_qty=None, stop_loss=None)
    buy_book = ctx(book=[(D("65010"), D("1"))])
    ok(req(strategy="funding", side="buy", **spot), buy_book)
    bad(req(strategy="funding", side="sell", **spot), ctx(), why="спот")
    bad(req(strategy="directional", side="buy", **spot), buy_book, why="спот")


# --- fail closed ---

@pytest.mark.parametrize("r,c", [
    (req(qty=0.001), ctx()), (req(qty=D(0)), ctx()), (req(qty=D(-1)), ctx()), (req(), ctx(mark=65000.0)),
    (req(), ctx(mark=None)), (req(), ctx(ticker_ts=None)), (req(), ctx(positions=[{"strategy": "hedge"}])),
    (req(), ctx(book=[("x", "1")])), (req(symbol="DOGEUSDT"), ctx()), (req(strategy="arb"), ctx()),
    (req(order_type="limit", price=None), ctx()), (req(leverage=None), ctx()), (req(), ctx(unknown=None)),
    (req(side="both"), ctx()), (req(category="inverse"), ctx()),
])
def test_bad_inputs_fail_closed(r, c):
    v = risk.check_open(r, c, "confirm", {})
    assert not v.ok and v.reasons


# --- закрытие, спот-продажа, сопровождение позиций ---

def test_close_always_allowed_if_reducing_and_within_position():
    assert risk.check_close(D("0.5"), D("1"), True).ok
    assert risk.check_close(D("1"), D("1"), True).ok
    assert not risk.check_close(D("1.1"), D("1"), True).ok
    assert not risk.check_close(D("0.5"), D("1"), False).ok
    assert not risk.check_close(D(0), D("1"), True).ok
    assert not risk.check_close(0.5, D("1"), True).ok


def test_spot_sell_only_bot_bought():
    assert risk.check_spot_sell(D("0.998"), D("0.998")).ok
    assert not risk.check_spot_sell(D("0.999"), D("0.998")).ok
    assert not risk.check_spot_sell(D("1"), D(0)).ok and not risk.check_spot_sell(D("1"), None).ok


def test_position_actions_thresholds():
    pos = [{"symbol": "BTCUSDT", "mark": D(100), "liq": D(125)},             # 25% — ничего
           {"symbol": "ETHUSDT", "mark": D(100), "liq": D(119)},             # 19% — тревога
           {"symbol": "TONUSDT", "mark": D(100), "liq": D(111)},             # 11% — сокращать
           {"symbol": "BTCUSDT", "mark": D(100), "liq": None},               # нет цены — тревога
           {"symbol": "BTCUSDT", "mark": None, "liq": D(5)}]                 # битые данные — тревога
    acts = [(p["symbol"], a) for p, a, _ in risk.position_actions(pos)]
    assert acts == [("ETHUSDT", "alert"), ("TONUSDT", "reduce"), ("BTCUSDT", "alert"), ("BTCUSDT", "alert")]
    assert [a for _, a, _ in risk.position_actions([{"mark": D(100), "liq": D(88)}])] == ["alert"]   # ровно 12%
    assert [a for _, a, _ in risk.position_actions([{"mark": D(100), "liq": D("88.1")}])] == ["reduce"]
    assert [a for _, a, _ in risk.position_actions([{"mark": D(100), "liq": D(80)}])] == []
