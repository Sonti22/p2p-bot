"""Торговое ядро, risk: жёсткие потолки плана, .env только понижает, minlot, дневной стоп (МСК), ликвидация, лимитки
±0.5% и глубина для рынка, частота ордеров, свежесть данных, unknown, режим маржи и плечо с биржи, итоговый размер с
ожидающими ордерами, чужое по символу, свои встречные позиции, шаги инструмента, стоп направленной, закрытие — только
своей позиции."""
from decimal import Decimal as D

import pytest

from trading import risk, venues

INST = venues.Instrument(D("0.000001"), D("0.000001"), None, D("0.1"), D("5"))

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
                margin_mode="isolated", leverage=D(2), instrument=INST, foreign=(), foreign_account=())
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


def pos(strategy="funding", notional="10", group=None, venue="bingx", symbol="ETHUSDT", side="short", qty="1",
        stop=False):
    """Строка journal.exposure: позиция бота или ожидающий ордер на открытие."""
    return {"venue": venue, "category": "swap", "symbol": symbol, "strategy": strategy, "group": group, "side": side,
            "qty": D(qty), "notional": D(notional), "stop": stop}


def test_total_open_cap_2000_and_group_counts():
    rows = [pos(notional="900", group="f1"), pos(notional="900", group="f1", venue="bybit", side="long")]
    bad(req(qty=D("0.0035"), hedge_ref_qty=D("0.0035")), ctx(positions=rows), "confirm", why="суммарно")  # 1800+227.5
    ok(req(qty=D("0.003"), hedge_ref_qty=D("0.003")), ctx(positions=rows), "confirm")                    # 1995
    hedges = [pos("hedge", group=f"h{i}") for i in range(3)]
    bad(req(), ctx(positions=hedges), "confirm", why="предел 3")
    ok(req(), ctx(positions=hedges[:2]), "confirm")
    pairs = [pos(group=g, venue=v) for g in ("a", "b") for v in ("bybit", "bingx")]
    fund = dict(strategy="funding", side="sell", hedge_ref_qty=None)
    bad(req(**fund), ctx(positions=pairs), "confirm", why="предел 2")
    ok(req(group="b", symbol="ETHUSDT", stop_loss=None, **fund), ctx(positions=pairs), "confirm")   # нога той же пары
    bad(req(group="b", **fund), ctx(positions=pairs), "confirm", why="другой монеты")   # метка пары ETH на BTC — нет
    one = [pos("directional", group=None)]
    bad(req(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None), ctx(positions=one),
        "confirm", why="предел 1")
    ok(req(), ctx(positions=[pos("hedge", group=f"h{i}", qty="0") for i in range(3)]), "confirm")   # пустые не в счёт


def test_resulting_position_includes_open_and_pending():
    """Лимит — на итоговую позицию ноги (биржа, символ, стратегия): открытое + ожидающие открытия + этот ордер; повтор
    группы счётчик не обнуляет."""
    leg = [pos("hedge", notional="30", group="c1", venue="bybit", symbol="BTCUSDT", qty="0.0005")]
    bad(req(), ctx(positions=leg), "minlot", why="итоговая позиция")                 # 30 + 45.5 > 50
    ok(req(qty=D("0.0003"), group="c1", hedge_ref_qty=D("0.0008")), ctx(positions=leg), "minlot")   # 30 + 19.5
    bad(req(qty=D("0.0003"), hedge_ref_qty=D("0.0008")), ctx(positions=leg), "minlot",
        why="перекрывающиеся стопы")                                                  # другой круг со стопом
    bad(req(qty=D("0.0003"), group="c1", hedge_ref_qty=D("0.1")), ctx(positions=leg * 2), "minlot",
        why="итоговая позиция")                                                       # та же группа: 60 + 19.5
    other_leg = [pos("hedge", notional="30", group="c1", venue="bingx", symbol="BTCUSDT")]
    ok(req(), ctx(positions=other_leg), "minlot")                                    # другая биржа — другая нога
    big = [pos("directional", notional="190", venue="bybit", symbol="BTCUSDT", side="long", stop=True)]
    bad(req(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None, qty=D("0.0002")),
        ctx(positions=big), "confirm", why="итоговая позиция")                        # 190 + 13 > 200


def test_hedge_ratio_on_whole_group():
    group = [pos("hedge", group="c1", venue="bybit", symbol="BTCUSDT", qty="0.0006", notional="39")]
    ok(req(qty=D("0.0004"), hedge_ref_qty=D("0.001"), group="c1"), ctx(positions=group), "confirm")    # 0.001
    bad(req(qty=D("0.0005"), hedge_ref_qty=D("0.001"), group="c1"), ctx(positions=group), "confirm",
        why="× 1.05")                                                                  # 0.0011 > 0.00105
    ok(req(qty=D("0.0005"), hedge_ref_qty=D("0.001"), group="c2", stop_loss=None), ctx(positions=group), "confirm")


def test_bot_netting_on_one_symbol():
    short = [pos("hedge", group="c1", venue="bybit", symbol="BTCUSDT", qty="0.001", notional="65")]
    d = dict(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None)
    bad(req(**d), ctx(positions=short), "confirm", why="встречная")
    bad(req(strategy="funding", hedge_ref_qty=None), ctx(positions=short), "confirm", why="перекрывающиеся")
    ok(req(strategy="funding", hedge_ref_qty=None, stop_loss=None), ctx(positions=short), "confirm")
    ok(req(group="c1", hedge_ref_qty=D("0.002")), ctx(positions=short), "confirm")      # тот же круг
    ok(req(**d), ctx(positions=[dict(short[0], venue="bingx")]), "confirm")              # другая биржа


def test_spot_leg_does_not_net_with_perp_leg():
    """Фандинг «спот + шорт перпа» на одной бирже и символе: спот — отдельные монеты, не сальдируется с перпом; нога
    считается отдельно (≤ 1000 USDT на ногу)."""
    spot = [dict(pos("funding", notional="900", group="f1", venue="bybit", symbol="BTCUSDT", side="long"),
                 category="spot")]
    fund = dict(strategy="funding", hedge_ref_qty=None, group="f2", qty=D("0.0015"), stop_loss=None)   # 97.5 USDT
    ok(req(**fund), ctx(positions=spot), "confirm")
    bad(req(**fund), ctx(positions=[dict(spot[0], category="linear")]), "confirm", why="встречная")
    ok(req(**fund), ctx(positions=[dict(spot[0], notional=D("950"))]), "confirm")        # спот — другая нога
    bad(req(**fund), ctx(positions=[dict(spot[0], category="linear", side="short", notional=D("950"))]), "confirm",
        why="итоговая позиция")                                                        # та же нога перпа: 950 + 97.5


@pytest.mark.parametrize("foreign,why", [(None, "позиции и ордера символа на бирже не проверены"),
                                         (("позиция long 1",), risk.FOREIGN)])
def test_foreign_on_symbol_refuses(foreign, why):
    v = bad(req(), ctx(foreign=foreign), why=why)
    assert v.reasons[0].startswith(why)


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
    ok(req(leverage=D(3)), ctx(leverage=D(3)), "confirm")
    bad(req(), ctx(leverage=D(3)), "minlot", why="плечо символа на бирже 3")             # факт с биржи, не запрос
    bad(req(), ctx(leverage=None), why="не прочитано")
    bad(req(), ctx(leverage=D(5)), "confirm", why="плечо символа на бирже 5")
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
    o = dict(owned=True, side="short")
    rows = [dict(o, symbol="BTCUSDT", mark=D(100), liq=D(125)),             # 25% — ничего
            dict(o, symbol="ETHUSDT", mark=D(100), liq=D(119)),             # 19% — тревога
            dict(o, symbol="TONUSDT", mark=D(100), liq=D(111)),             # 11% — сокращать
            dict(o, symbol="BTCUSDT", mark=D(100), liq=None),               # нет цены — тревога
            dict(o, symbol="BTCUSDT", mark=None, liq=D(5))]                 # битые данные — тревога
    acts = [(p["symbol"], a) for p, a, _ in risk.position_actions(rows)]
    assert acts == [("ETHUSDT", "alert"), ("TONUSDT", "reduce"), ("BTCUSDT", "alert"), ("BTCUSDT", "alert")]
    lng = dict(owned=True, side="long", mark=D(100))
    assert [a for _, a, _ in risk.position_actions([dict(lng, liq=D(88))])] == ["alert"]   # ровно 12%
    assert [a for _, a, _ in risk.position_actions([dict(lng, liq=D("88.1"))])] == ["reduce"]
    assert [a for _, a, _ in risk.position_actions([dict(lng, liq=D(80))])] == []


def test_position_actions_side_aware_and_only_bot_positions():
    """Запас — со стороны позиции: лонг с ликвидацией выше mark (или шорт — ниже) — битые данные, тревога, а не
    «запаса много». Позиции не бота (owned не True) бот не трогает — ни тревог, ни сокращения."""
    long_up = risk.position_actions([dict(owned=True, side="long", mark=D(100), liq=D(125))])
    assert [(a, w) for _, a, w in long_up] == [("alert", "цена ликвидации не с той стороны от mark — данные битые")]
    assert [a for _, a, _ in risk.position_actions([dict(owned=True, side="short", mark=D(100), liq=D(80))])] == [
        "alert"]
    assert risk.position_actions([dict(side="short", mark=D(100), liq=D(101)),
                                  dict(owned=False, side="short", mark=D(100), liq=D(101)),
                                  dict(owned="yes", side="short", mark=D(100), liq=D(101))]) == []
    assert [a for _, a, _ in risk.position_actions([dict(owned=True, side="both", mark=D(100), liq=D(99))])] == [
        "alert"]


# --- шаги инструмента ---

BTC = venues.Instrument(D("0.001"), D("0.001"), D("100"), D("0.1"), D("5"))


def test_quantize_floors_size_and_moves_stop_toward_entry():
    q, why = risk.quantize(req(qty=D("0.00179"), stop_loss=D("66000.07")), BTC, D("65000"))   # шорт
    assert why == () and q.qty == D("0.001") and q.stop_loss == D("66000.0")                  # стоп вниз — к входу
    q, why = risk.quantize(req(side="buy", strategy="directional", qty=D("0.0019"), stop_loss=D("64000.01"),
                               order_type="limit", price=D("65000.09")), BTC, D("65000"))
    assert why == () and q.qty == D("0.001") and q.price == D("65000.0") and q.stop_loss == D("64000.1")
    q, _ = risk.quantize(req(order_type="limit", price=D("65000.01")), BTC, D("65000"))        # продажа — вверх
    assert q.price == D("65000.1")
    _, why = risk.quantize(req(qty=D("0.0009")), BTC, D("65000"))
    assert any("меньше минимума биржи" in w for w in why)
    _, why = risk.quantize(req(qty=D("150")), BTC, D("65000"))
    assert any("больше максимума" in w for w in why)                                          # вниз к максимуму не режем
    _, why = risk.quantize(req(side="buy", stop_loss=D("64999.95")), BTC, D("65000"))          # округлился бы на вход
    assert any("не с той стороны" in w for w in why)
    _, why = risk.quantize(req(qty=D("0.001")), venues.Instrument(D("0.001"), D("0.001"), None, D("0.1"), D("100")),
                           D("65000"))
    assert any("номинал" in w for w in why)
    assert risk.quantize(req(), BTC, None)[1]                                                 # нет mark — причина


def test_check_open_needs_quantized_request_and_instrument():
    c = ctx(instrument=BTC)
    bad(req(qty=D("0.0015"), hedge_ref_qty=D("0.002")), c, "confirm", why="не кратно шагу")
    bad(req(qty=D("0.001"), hedge_ref_qty=D("0.002"), stop_loss=D("66000.05")), c, "confirm", why="не кратен шагу")
    ok(req(qty=D("0.001"), hedge_ref_qty=D("0.002"), stop_loss=D("66000")), c, "confirm")
    bad(req(), ctx(instrument=None), why="нет шагов инструмента")


# --- направленная: стоп, дневной лимит, ликвидация ---

def test_directional_stop_fits_daily_room_and_triggers_before_liquidation():
    d = dict(strategy="directional", side="buy", hedge_ref_qty=None, book=None)
    d.pop("book")
    c = ctx(book=[(D("65010"), D("1"))])
    ok(req(stop_loss=D("60000"), **d), c, "confirm")                              # 0.0007×5000×1.5+0.091 ≈ 5.34
    bad(req(stop_loss=D("60000"), **d), ctx(book=[(D("65010"), D("1"))], realized_today=D("-46")), "confirm",
        why="остатка дневного лимита")                                             # остаток 4
    bad(req(stop_loss=D("33000"), **d), c, "confirm", why="стоп за ценой ликвидации")   # изолированная ≈ 33150
    ok(req(stop_loss=D("33200"), **d), c, "confirm")
    assert risk.isolated_liq("buy", D(100), D(2)) == D("51") and risk.isolated_liq("sell", D(100), D(2)) == D("149")


# --- ревью 2: итоговая позиция ключа по стопам, метки групп, guard_open, смена стопа и плеча, тревоги ---

def key_row(strategy="directional", venue="bybit", symbol="ETHUSDT", side="long", qty="0.02", entry="4000", stop=None,
            group=None, category="linear"):
    """Строка journal.exposure позиции бота (с входом и ценой стопа ключа)."""
    return {"venue": venue, "category": category, "symbol": symbol, "strategy": strategy, "group": group, "side": side,
            "qty": D(qty), "net": D(qty) if side == "long" else -D(qty), "pending": D(0), "px": D(entry),
            "entry": D(entry), "notional": D(qty) * D(entry), "unrealized": D(0), "stop": stop is not None,
            "stop_price": None if stop is None else D(stop)}


ETH4000 = dict(mark=D("4000"), book=[(D("4001"), D("10"))], instrument=INST)
DIR_ETH = dict(strategy="directional", symbol="ETHUSDT", side="buy", qty=D("0.02"), stop_loss=D("2800"),
               hedge_ref_qty=None)


def test_worst_loss_vs_daily_room_is_resulting_key_position_not_per_order():
    """0.02 ETH по 4000 со стопом 2800 — худший 36.16 ≤ 50; вторая такая же добавка к тому же ключу — итог 0.04 со
    стопом 2800: 72.32 > 50 — отказ (раньше считалось по ордеру и проходило)."""
    ok(req(**DIR_ETH), ctx(**ETH4000), "confirm")
    v = bad(req(**DIR_ETH), ctx(positions=[key_row(stop="2800")], **ETH4000), "confirm", why="худший убыток по стопам")
    assert any("72.32" in r for r in v.reasons)
    other = key_row(symbol="BTCUSDT", qty="0.0005", entry="65000", stop="40000")   # чужой ключ со стопом: 18.81
    bad(req(**DIR_ETH), ctx(positions=[other], **ETH4000), "confirm", why="худший убыток по стопам")   # 36.16+18.81
    mine = risk.stop_budget([key_row(stop="2800")], "bybit", "linear", "ETHUSDT", "directional", None, "buy", D("0.02"),
                            D("4000"), D("3500"))[0]
    assert mine == D("0.04") * 500 * risk.STOP_SLIPPAGE + D("0.32")      # новый стоп — на весь ключ
    exp = [key_row(stop="2800")]
    guard = risk.guard_open(venues.Order("bybit", "linear", "ETHUSDT", "buy", "market", "0.02", stop_loss="2800"),
                            "directional", "", "confirm", mark=D("4000"), instrument=INST, leverage=D(2),
                            margin_mode="isolated", foreign=[], foreign_account=None, exposure=exp,
                            realized_today=D(0), unrealized=D(0), capital=D("10000"))
    assert any("худший убыток по стопам" in r for r in guard), guard


def test_cross_minlot_hedges_sum_of_stops_within_room():
    """Кросс в minlot: три хеджа по 4.58 каждый ≤ 5, но вместе — нет: считается сумма всех стопов бота."""
    cross = ctx(margin_mode="cross")
    one = key_row("hedge", symbol="BTCUSDT", side="short", qty="0.0007", entry="65000", stop="68800", group="c1")
    assert risk.key_worst("sell", D("0.0007"), D("65000"), D("68800")) < 5
    ok(req(stop_loss=D("68800")), cross, "minlot")
    bad(req(stop_loss=D("68800"), group="c2"), ctx(margin_mode="cross", positions=[
        dict(one, venue="bingx")]), "minlot", why="худший убыток")


def test_one_directional_cap_not_bypassed_by_reusing_group_label():
    """Направленная ETH на 200 USDT с меткой 'dir' открыта; новая направленная BTC с той же меткой — отказ (одна
    направленная и весь номинал стратегии ≤ 200); метку хеджа нельзя переиспользовать на другой монете."""
    eth = [key_row(qty="0.05", entry="4000", stop="3900", group="dir")]
    d = dict(strategy="directional", side="buy", stop_loss=D("64000"), hedge_ref_qty=None, group="dir")
    v = bad(req(**d), ctx(positions=eth, book=[(D("65010"), D("1"))]), "confirm", why="предел 1")
    assert any("весь номинал стратегии" in r for r in v.reasons)
    hedges = [key_row("hedge", symbol="ETHUSDT", side="short", qty="0.1", entry="3000", group="c1")]
    bad(req(group="c1", hedge_ref_qty=D("0.001")), ctx(positions=hedges), "confirm", why="другой монеты")


def test_guard_open_checks_liquidation_distance_and_limit_band():
    """guard_open внутри submit: запас до ликвидации по цене ликвидации с биржи и коридор лимитки ±0.5% от mark."""
    order = venues.Order("bybit", "linear", "BTCUSDT", "sell", "market", "0.0007")
    base = dict(mark=D("65000"), instrument=INST, leverage=D(3), margin_mode="isolated", foreign=[],
                foreign_account=None, exposure=[], realized_today=D(0), unrealized=D(0), capital=D("10000"))
    assert risk.guard_open(order, "hedge", "c1", "confirm", **base) == []
    near = risk.guard_open(order, "hedge", "c1", "confirm", est_liq=D("66000"), **base)
    assert any("запас до ликвидации 0.015" in r for r in near), near
    lim = venues.Order("bybit", "linear", "BTCUSDT", "sell", "limit", "0.0007", price="64600")
    assert any("±0.5" in r for r in risk.guard_open(lim, "hedge", "c1", "confirm", **base))
    assert any("капитал" in r for r in risk.guard_open(order, "hedge", "c1", "confirm", **dict(base, capital=None)))
    loss = risk.guard_open(order, "hedge", "c1", "minlot", **dict(base, realized_today=D("-3"),
                                                                  unrealized=D("-2")))
    assert any("дневной стоп" in r for r in loss), loss                  # 3 + 2 ≥ 5 (minlot)
    small = risk.guard_open(order, "hedge", "c1", "confirm", **dict(base, capital=D("1000"), realized_today=D("-20")))
    assert any("дневной стоп" in r for r in small)                         # 2% от 1000 = 20 < 50


def test_check_stop_move_and_leverage_change():
    """Стоп — только к входу и раньше ликвидации; плечо — не выше потолка стратегий на символе, стоп раньше новой
    ликвидации."""
    assert risk.check_stop_move("buy", D("65000"), D("62000"), D("60000"), D("64000"), None, D(2)) == []
    assert any("только к входу" in r for r in risk.check_stop_move("buy", D("65000"), D("34000"), D("60000"),
                                                                  D("64000"), None, D(2)))
    assert any("ликвидации" in r for r in risk.check_stop_move("buy", D("65000"), D("33000"), None, D("64000"), None,
                                                              D(2)))
    assert any("не с той стороны" in r for r in risk.check_stop_move("sell", D("65000"), D("64000"), None, D("64500"),
                                                                    None, D(2)))
    assert risk.check_stop_move("sell", D("65000"), D("66000"), None, D("64500"), D("96000"), None) == []
    assert risk.check_leverage_change([("hedge", None, None, None)], "confirm", 3) == []
    assert any("вне 1..2" in r for r in risk.check_leverage_change([("hedge", None, None, None)], "minlot", 3))
    assert any("вне 1..2" in r for r in risk.check_leverage_change([("directional", "buy", D("65000"), D("34000"))],
                                                                   "confirm", 3))
    late = risk.check_leverage_change([("hedge", "sell", D("65000"), D("97000"))], "confirm", 3)
    assert any("стоп позиции за новой ценой ликвидации" in r for r in late), late
    assert risk.check_leverage_change([("arb", None, None, None)], "confirm", 2)


def test_position_actions_alert_for_bot_position_that_does_not_match_venue():
    """У бота по журналу позиция, на бирже по символу другое (владелец добавил) или знак BingX не сходится — тревога с
    запасом до ликвидации, без сокращения; позиции без доли бота — тишина."""
    rows = [dict(owned=False, mismatch=True, bot_net=D("-0.001"), side="short", mark=D(100), liq=D("101.5")),
            dict(owned=False, sign_suspect=True, mismatch=True, bot_net=D("-0.5"), side="long", mark=D(100),
                 liq=D("98.5")),
            dict(owned=False, mismatch=False, side="short", mark=D(100), liq=D(101))]
    acts = risk.position_actions(rows)
    assert [a for _, a, _ in acts] == ["alert", "alert"]
    assert "до ликвидации 0.015" in acts[0][2] and "не сокращает" in acts[0][2]
    assert "знак позиции BingX" in acts[1][2]
