import datetime
import sqlite3
import time

import trades
from helpers import make_ad


def deal(profit=2.0):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def test_log_and_stats_within_periods(tmp_path):
    db = str(tmp_path / "trades.db")
    trades.log_trade(deal(2.0), 50000, path=db, ts=time.time())
    trades.log_trade(deal(4.0), 100000, path=db, ts=time.time() - 3 * 86400)     # неделя, не день
    trades.log_trade(deal(6.0), 200000, path=db, ts=time.time() - 40 * 86400)    # старее месяца
    st = trades.stats(path=db)
    assert st["day"]["count"] == 1 and st["day"]["amount"] == 50000
    assert st["week"]["count"] == 2 and st["week"]["amount"] == 150000
    assert st["week"]["avg_profit"] == 3.0
    assert st["month"]["count"] == 2         # запись 40-дневной давности не входит


def test_stats_day_is_calendar_day_msk_not_rolling_24h(tmp_path):
    db = str(tmp_path / "trades.db")
    now = datetime.datetime(2026, 9, 25, 0, 30, tzinfo=trades.MSK).timestamp()          # чуть после полуночи МСК
    yesterday_late = datetime.datetime(2026, 9, 24, 22, 30, tzinfo=trades.MSK).timestamp()  # 2 ч назад, но вчера
    today_early = datetime.datetime(2026, 9, 25, 0, 10, tzinfo=trades.MSK).timestamp()      # тот же день МСК
    trades.log_trade(deal(), 10000, path=db, ts=yesterday_late)
    trades.log_trade(deal(), 20000, path=db, ts=today_early)
    st = trades.stats(path=db, now=now)
    assert st["day"]["count"] == 1 and st["day"]["amount"] == 20000    # вчерашняя не попала, хотя ей всего 2 ч


def test_stats_month_is_calendar_month_not_rolling_30_days(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    month_start = trades._month_start(now)
    trades.log_trade(deal(), 10000, path=db, ts=month_start + 3600)   # начало текущего календарного месяца
    trades.log_trade(deal(), 20000, path=db, ts=month_start - 3600)   # конец прошлого месяца, но < 30 дней назад
    st = trades.stats(path=db, now=now)
    assert st["month"]["count"] == 1 and st["month"]["amount"] == 10000


def test_stats_empty_db_missing_file(tmp_path):
    st = trades.stats(path=str(tmp_path / "none.db"))
    assert all(s == {"count": 0, "amount": 0.0, "avg_profit": 0.0,
                     "fact_count": 0, "avg_fact": None, "avg_diff": None} for s in st.values())


def test_sbp_bank_detects_known_names():
    assert trades.sbp_bank(["T-Bank", "Bank Account"]) == "T-Bank"
    assert trades.sbp_bank(["SBP - Fast Bank Transfer"]) == "SBP"
    assert trades.sbp_bank(["Bank Account", "Payeer"]) == ""


def test_log_trade_warns_once_when_bank_crosses_limit(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    id1, _, total1, crossed1 = trades.log_trade(deal(), 60000, path=db, ts=now)      # 60к — ниже лимита
    assert total1 == 60000 and not crossed1
    id2, bank2, total2, crossed2 = trades.log_trade(deal(), 60000, path=db, ts=now)  # 120к — пересекли 100к
    assert bank2 == "T-Bank" and total2 == 120000 and crossed2
    assert id2 != id1                                                           # каждая сделка — свой id
    _, _, total3, crossed3 = trades.log_trade(deal(), 10000, path=db, ts=now)   # уже выше лимита, не повторяем
    assert total3 == 130000 and not crossed3


def test_log_trade_no_bank_when_pay_method_unknown(tmp_path):
    db = str(tmp_path / "trades.db")
    d = (2.0, make_ad("Bybit", "buy", 85.0, pays=("Payeer",)), make_ad("MEXC", "sell", 90.0), "маршрут")
    _, bank, total, crossed = trades.log_trade(d, 500000, path=db, ts=time.time())
    assert bank == "" and total == 500000 and not crossed


def test_banks_over_limit_only_lists_banks_at_or_above_limit(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    trades.log_trade(deal(), 60000, path=db, ts=now)          # T-Bank 60к — ниже лимита
    trades.log_trade(deal(), 60000, path=db, ts=now)          # T-Bank 120к — уже выше
    assert trades.banks_over_limit(["T-Bank", "SBP", ""], path=db, now=now) == {"T-Bank"}


def test_bank_month_total_excludes_previous_month(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    prev_month = trades._month_start(now) - 86400   # день из прошлого календарного месяца
    trades.log_trade(deal(), 90000, path=db, ts=prev_month)
    assert trades.bank_month_total("T-Bank", path=db, now=now) == 0.0
    trades.log_trade(deal(), 40000, path=db, ts=now)
    assert trades.bank_month_total("T-Bank", path=db, now=now) == 40000


def test_parse_fact_percent():
    assert trades.parse_fact("+1.2%", 50000) == 1.2
    assert trades.parse_fact("-0.5%", 50000) == -0.5
    assert trades.parse_fact("1,2%", 50000) == 1.2       # запятая как разделитель
    assert trades.parse_fact("2%", 50000) == 2.0


def test_parse_fact_bare_number_is_percent_when_small():
    assert trades.parse_fact("1.2", 50000) == 1.2
    assert trades.parse_fact("-0.5", 50000) == -0.5
    assert trades.parse_fact("50", 50000) == 50.0        # граница — ещё проценты


def test_parse_fact_rubles_explicit_and_bare_large_number():
    assert trades.parse_fact("650 ₽", 50000) == 650 / 50000 * 100
    assert trades.parse_fact("650р", 50000) == 650 / 50000 * 100
    assert trades.parse_fact("-300 руб", 50000) == -300 / 50000 * 100
    assert trades.parse_fact("-300", 50000) == -300 / 50000 * 100   # без единицы, но крупное число — ₽


def test_parse_fact_rubles_without_amount_is_none():
    assert trades.parse_fact("650 ₽", 0) is None
    assert trades.parse_fact("-300", None) is None


def test_parse_fact_garbage_is_none():
    for garbage in ("", "ерунда", "1.2.3", "%", "₽", "abc%", None):
        assert trades.parse_fact(garbage, 50000) is None


def test_get_trade_and_set_fact(tmp_path):
    db = str(tmp_path / "trades.db")
    trade_id, *_ = trades.log_trade(deal(2.5), 50000, path=db)
    assert trades.get_trade(trade_id, path=db) == {"amount": 50000.0, "profit": 2.5}
    assert trades.get_trade(999, path=db) is None
    assert trades.set_fact(trade_id, 3.0, path=db) is True
    assert trades.get_trade(trade_id, path=db)["profit"] == 2.5   # факт не трогает расчётный %
    con = sqlite3.connect(db)
    fact, = con.execute("SELECT fact FROM trades WHERE id = ?", (trade_id,)).fetchone()
    con.close()
    assert fact == 3.0
    assert trades.set_fact(999, 1.0, path=db) is False


def test_stats_reports_calc_vs_fact(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    id1, *_ = trades.log_trade(deal(2.0), 50000, path=db, ts=now)
    trades.log_trade(deal(4.0), 50000, path=db, ts=now)   # без факта
    trades.set_fact(id1, 1.5, path=db)                    # факт хуже расчёта на 0.5 п.п.
    st = trades.stats(path=db, now=now)
    assert st["day"]["count"] == 2
    assert st["day"]["fact_count"] == 1
    assert st["day"]["avg_fact"] == 1.5
    assert round(st["day"]["avg_diff"], 4) == -0.5


def test_unmatched_lists_only_trades_without_fact_within_window(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    id1, *_ = trades.log_trade(deal(2.0), 50000, path=db, ts=now)
    id2, *_ = trades.log_trade(deal(3.0), 50000, path=db, ts=now - 2 * 86400)  # старее since
    id3, *_ = trades.log_trade(deal(4.0), 50000, path=db, ts=now)
    trades.set_fact(id3, 4.0, path=db)                                        # факт уже есть
    out = trades.unmatched(path=db, since=now - 86400)
    assert [t["id"] for t in out] == [id1]


def test_unmatched_missing_db_is_empty_list(tmp_path):
    assert trades.unmatched(path=str(tmp_path / "none.db")) == []


def _trade_row(ts, buy_ex="Bybit", sell_ex="MEXC", asset="USDT", amount=50000.0, profit=2.0):
    return {"id": 1, "ts": ts, "buy_ex": buy_ex, "buy_asset": asset, "sell_ex": sell_ex, "sell_asset": asset,
            "amount": amount, "profit": profit}


def test_match_fact_computes_realized_spread_from_both_legs():
    now = time.time()
    trade = _trade_row(now, amount=50000.0)
    hist_by_ex = {
        "bybit": [{"kind": "trade", "asset": "USDT", "side": "buy", "amount": 588.24, "price": 85.0, "ts": now}],
        "mexc": [{"kind": "trade", "asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0, "ts": now + 60}],
    }
    fact = trades.match_fact(trade, hist_by_ex)
    assert fact == (90.0 / 85.0 - 1) * 100


def test_match_fact_none_when_exchange_history_not_fetched_this_poll():
    trade = _trade_row(time.time())
    assert trades.match_fact(trade, {"mexc": []}) is None        # bybit не опрашивался в этом цикле


def test_match_fact_none_when_asset_or_side_does_not_match():
    now = time.time()
    trade = _trade_row(now)
    hist_by_ex = {
        "bybit": [{"asset": "BTC", "side": "buy", "amount": 1.0, "price": 85.0, "ts": now}],   # не та монета
        "mexc": [{"asset": "USDT", "side": "buy", "amount": 588.24, "price": 90.0, "ts": now}],  # не та сторона
    }
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_ignores_deposit_without_price():
    now = time.time()
    trade = _trade_row(now)
    hist_by_ex = {
        "bybit": [{"kind": "deposit", "asset": "USDT", "amount": 588.24, "ts": now}],  # нет цены — не факт
        "mexc": [{"kind": "trade", "asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0, "ts": now}],
    }
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_none_outside_time_window():
    now = time.time()
    trade = _trade_row(now)
    hist_by_ex = {
        "bybit": [{"asset": "USDT", "side": "buy", "amount": 588.24, "price": 85.0,
                   "ts": now - trades.AUTO_MATCH_WINDOW - 60}],
        "mexc": [{"asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0, "ts": now}],
    }
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_none_when_fiat_amount_too_different():
    now = time.time()
    trade = _trade_row(now, amount=50000.0)
    hist_by_ex = {
        # 10 USDT * 85 ₽ = 850 ₽ — совсем не похоже на круг в 50 000 ₽
        "bybit": [{"asset": "USDT", "side": "buy", "amount": 10.0, "price": 85.0, "ts": now}],
        "mexc": [{"asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0, "ts": now}],
    }
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_picks_closest_candidate_by_time():
    now = time.time()
    trade = _trade_row(now, amount=50000.0)
    hist_by_ex = {
        "bybit": [
            {"asset": "USDT", "side": "buy", "amount": 588.24, "price": 84.0, "ts": now - 600},   # дальше
            {"asset": "USDT", "side": "buy", "amount": 588.24, "price": 85.0, "ts": now + 30},     # ближе
        ],
        "mexc": [{"asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0, "ts": now}],
    }
    fact = trades.match_fact(trade, hist_by_ex)
    assert fact == (90.0 / 85.0 - 1) * 100
