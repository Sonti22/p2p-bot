import dataclasses
import datetime
import sqlite3
import time

import pytest

import p2p
import trades
from helpers import make_ad


def deal(profit=2.0, pays=("T-Bank",)):
    return profit, make_ad("Bybit", "buy", 85.0, pays=pays), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def sbp_deal(profit=2.0):
    """Мерчант принимает только СБП — платим по СБП со своего банка, лимит тратится."""
    return deal(profit, pays=("SBP - Fast Bank Transfer",))


def test_log_and_stats_within_periods(tmp_path):
    db = str(tmp_path / "trades.db")
    now = datetime.datetime(2026, 9, 15, 12, 0).timestamp()    # середина месяца: «3 дня назад» не падает на 1–3 число в прошлый месяц
    trades.log_trade(deal(2.0), 50000, path=db, ts=now)
    trades.log_trade(deal(4.0), 100000, path=db, ts=now - 3 * 86400)     # неделя, не день
    trades.log_trade(deal(6.0), 200000, path=db, ts=now - 40 * 86400)    # старее месяца
    st = trades.stats(path=db, now=now)
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
                     "fact_count": 0, "avg_fact": None, "avg_diff": None, "plan_facts": 0} for s in st.values())


def test_bank_of_knows_names_from_all_venues():
    for pay, bank in (("Tinkoff", "T-Bank"), ("T-Bank", "T-Bank"), ("Т-Банк", "T-Bank"), ("Sberbank", "Sberbank"),
                      ("VTB Bank", "VTB"), ("OZON Bank", "Ozon Bank"), ("Promsvyazbank", "PSB"), ("PSB", "PSB"),
                      ("Rosselkhozbank", "Rosselkhozbank"), ("Raiffeisen Bank", "Raiffeisen"),
                      ("Yandex Bank", "Yandex Bank"), ("Sovkombank", "Sovcombank"), ("Sovcombank", "Sovcombank"),
                      ("Alfa-bank", "Alfa-bank"), ("МТС Банк", "MTS Bank")):
        assert trades.bank_of(pay) == bank, pay
    for pay in ("SBP - Fast Bank Transfer", "Bank Transfer", "Bank Account", "Cash Deposit to Bank", "Payeer"):
        assert trades.bank_of(pay) == "", pay
    assert trades.is_sbp("SBP - Fast Bank Transfer") and trades.is_sbp("СБП") and not trades.is_sbp("Tinkoff")


def test_pay_plan_intra_sbp_and_unknown():
    own = (["T-Bank", "Sberbank"], False)
    assert trades.pay_plan(["Tinkoff"], own=own) == ("intra", "T-Bank")                 # свой банк — внутри банка
    assert trades.pay_plan(["SBP - Fast Bank Transfer"], own=own) == ("sbp", "T-Bank")  # только СБП — с первого
    assert trades.pay_plan(["SBP"], over={"T-Bank"}, own=own) == ("sbp", "Sberbank")    # лимит исчерпан — следующий
    assert trades.pay_plan(["SBP"], over={"T-Bank", "Sberbank"}, own=own) == ("sbp", "T-Bank")   # все за лимитом
    assert trades.pay_plan(["Raiffeisen Bank"], own=own) == ("sbp", "T-Bank")          # чужой банк — по СБП
    assert trades.pay_plan(["Raiffeisen Bank"], own=(["T-Bank"], True)) == ("intra", "Raiffeisen")   # «*»
    assert trades.pay_plan(["Payeer", "Bank Transfer"], own=own) == ("", "")


def test_free_limit_defaults_and_owner_tariffs(monkeypatch):
    monkeypatch.delenv("SBP_FREE_LIMITS", raising=False)
    assert trades.free_limit("T-Bank") == 100000 and trades.free_limit("VTB") == 300000
    monkeypatch.setenv("SBP_FREE_LIMITS", "T-Bank:300000,VTB:inf,кривое,Sberbank:")
    assert trades.free_limit("T-Bank") == 300000 and trades.free_limit("VTB") == float("inf")
    assert trades.free_limit("Sberbank") == 100000


def test_intra_bank_trade_does_not_use_sbp_limit(tmp_path):
    db = str(tmp_path / "trades.db")
    _, bank, total, crossed = trades.log_trade(deal(pays=("Tinkoff",)), 150000, path=db, ts=time.time())
    assert bank == "T-Bank" and total == 0 and not crossed
    assert trades.bank_month_total("T-Bank", path=db) == 0


def test_log_trade_warns_once_when_bank_crosses_limit(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    id1, _, total1, crossed1 = trades.log_trade(sbp_deal(), 60000, path=db, ts=now)      # 60к — ниже лимита
    assert total1 == 60000 and not crossed1
    id2, bank2, total2, crossed2 = trades.log_trade(sbp_deal(), 60000, path=db, ts=now)  # 120к — пересекли 100к
    assert bank2 == "T-Bank" and total2 == 120000 and crossed2
    assert id2 != id1                                                               # каждая сделка — свой id
    _, bank3, total3, crossed3 = trades.log_trade(sbp_deal(), 10000, path=db, ts=now)   # Т-Банк за лимитом —
    assert bank3 == "Sberbank" and total3 == 10000 and not crossed3                      # платим со следующего


def test_log_trade_no_bank_when_pay_method_unknown(tmp_path):
    db = str(tmp_path / "trades.db")
    d = (2.0, make_ad("Bybit", "buy", 85.0, pays=("Payeer",)), make_ad("MEXC", "sell", 90.0), "маршрут")
    _, bank, total, crossed = trades.log_trade(d, 500000, path=db, ts=time.time())
    assert bank == "" and total == 0 and not crossed


def test_banks_over_limit_only_lists_banks_at_or_above_limit(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    trades.log_trade(sbp_deal(), 60000, path=db, ts=now)      # СБП с Т-Банка 60к — ниже лимита
    trades.log_trade(sbp_deal(), 60000, path=db, ts=now)      # 120к — уже выше
    assert trades.banks_over_limit(["T-Bank", "Sberbank", "VTB", ""], path=db, now=now) == {"T-Bank"}


def test_bank_month_total_excludes_previous_month(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    prev_month = trades._month_start(now) - 86400   # день из прошлого календарного месяца
    trades.log_trade(sbp_deal(), 90000, path=db, ts=prev_month)
    assert trades.bank_month_total("T-Bank", path=db, now=now) == 0.0
    trades.log_trade(sbp_deal(), 40000, path=db, ts=now)
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


def _trade_row(ts, buy_ex="Bybit", sell_ex="Bybit", asset="USDT", amount=50000.0, profit=2.0, route="внутри биржи",
               sell_asset=None):
    return {"id": 1, "ts": ts, "buy_ex": buy_ex, "buy_asset": asset, "sell_ex": sell_ex,
            "sell_asset": sell_asset or asset, "amount": amount, "profit": profit, "route": route}


def leg(side, amount, price, ts, asset="USDT"):
    """Запись истории как у accounts.bybit_p2p_orders: P2P-ордер, цена в ₽, есть fiat, нет kind."""
    return {"id": f"{side}{ts}", "side": side, "asset": asset, "fiat": "RUB", "amount": amount, "price": price, "ts": ts}


def test_match_fact_computes_result_from_both_p2p_legs():
    now = time.time()
    trade = _trade_row(now, amount=50000.0)                        # Bybit → Bybit, «внутри биржи»: издержек нет
    hist_by_ex = {"bybit": [leg("buy", 588.24, 85.0, now), leg("sell", 588.24, 90.0, now + 60)]}
    assert trades.match_fact(trade, hist_by_ex) == pytest.approx((90.0 / 85.0 - 1) * 100)


def test_match_fact_is_net_of_bank_fee_and_withdrawal_the_plan_used():
    now = time.time()
    route = "комиссия банка −0.5% → перевод −1 USDT (TRC20) на HTX → запас на курс −0.3%"
    trade = _trade_row(now, sell_ex="HTX", route=route)
    # будь у площадки продажи в истории P2P-ордера — вычитаются вывод 1 USDT и 0.5% банка, запас на курс — нет
    hist_by_ex = {"bybit": [leg("buy", 585.3, 85.0, now)], "htx": [leg("sell", 584.3, 90.0, now + 900)]}
    fact = trades.match_fact(trade, hist_by_ex)
    assert fact == pytest.approx(((585.3 - 1) * 90.0 / (585.3 * 85.0 / 0.995) - 1) * 100)
    assert fact < (90.0 / 85.0 - 1) * 100 - 0.5                     # меньше валового спреда на издержки


def test_match_fact_at_plan_prices_equals_plan_profit():
    """Факт по ордерам ровно по ценам и объёму расчёта (без запаса на курс) совпадает с чистым расчётом p2p._route —
    те же комиссия банка и вывод, разобранные из строки маршрута."""
    cfg = p2p.Config(pay_fee=0.5, risk_buffer={})
    b, s = make_ad("MEXC", "buy", 85.0), make_ad("Bybit", "sell", 90.0)
    profit, route = p2p._route(b, s, cfg, {"MEXC": {"USDT": (1.0, 1.0)}, "Bybit": {"USDT": (1.0, 1.0)}})
    bank, fees = trades.route_costs(route)
    assert bank == 0.5 and fees.get("USDT", 0) > 0                  # в маршруте есть и комиссия банка, и вывод
    now = time.time()
    qty = cfg.amount * (1 - bank / 100) / b.price
    trade = _trade_row(now, buy_ex="MEXC", route=route, profit=profit)
    hist_by_ex = {"mexc": [leg("buy", qty, b.price, now)],
                  "bybit": [leg("sell", qty - fees["USDT"], s.price, now + 600)]}
    assert trades.match_fact(trade, hist_by_ex) == pytest.approx(profit)


def test_route_costs_parses_route_labels():
    assert trades.route_costs("внутри биржи") == (0.0, {})
    assert trades.route_costs("перевод USDT без комиссии (TON) на MEXC") == (0.0, {})
    assert trades.route_costs("комиссия банка −0.5% (лимит СБП Т-Банк исчерпан) → через Bybit: перевод −0.8 USDT "
                              "(TRC20) ×2 → запас на курс −0.3%") == (0.5, {"USDT": 0.8})
    assert trades.route_costs("перевод −5e-05 BTC (BTC) на MEXC") == (0.0, {"BTC": 5e-05})
    assert trades.route_costs(None) == (0.0, {})


def test_match_fact_spot_trades_of_other_venues_are_not_p2p_legs():
    """У MEXC/KuCoin в истории спот-сделки (kind=trade, цена к USDT) — ногой P2P-продажи за рубли они не бывают;
    раньше такая запись «сопоставлялась» и давала выдуманный факт."""
    now = time.time()
    trade = _trade_row(now, sell_ex="MEXC")
    hist_by_ex = {"bybit": [leg("buy", 588.24, 85.0, now)],
                  "mexc": [{"kind": "trade", "asset": "USDT", "side": "sell", "amount": 588.24, "price": 90.0,
                            "ts": now + 60}]}
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_cross_asset_trade_stays_unmatched():
    now = time.time()
    trade = _trade_row(now, sell_asset="ETH", route="спот USDT→ETH на Bybit (−0.1%)")
    hist_by_ex = {"bybit": [leg("buy", 588.24, 85.0, now), leg("sell", 0.2, 250000.0, now + 60, asset="ETH")]}
    assert trades.match_fact(trade, hist_by_ex) is None             # валового % между разными монетами нет


def test_match_fact_none_when_withdrawal_eats_the_coin():
    now = time.time()
    trade = _trade_row(now, sell_ex="HTX", route="перевод −600 USDT (TRC20) на HTX")
    hist_by_ex = {"bybit": [leg("buy", 588.24, 85.0, now)], "htx": [leg("sell", 588.24, 90.0, now)]}
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_none_when_exchange_history_not_fetched_this_poll():
    trade = _trade_row(time.time(), sell_ex="MEXC")
    assert trades.match_fact(trade, {"mexc": []}) is None        # bybit не опрашивался в этом цикле


def test_match_fact_none_when_asset_or_side_does_not_match():
    now = time.time()
    trade = _trade_row(now)
    hist_by_ex = {"bybit": [leg("buy", 1.0, 85.0, now, asset="BTC"),    # не та монета
                            leg("buy", 588.24, 90.0, now)]}             # продажи нет — только вторая покупка
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_ignores_deposit_without_price():
    now = time.time()
    trade = _trade_row(now)
    hist_by_ex = {"bybit": [{"kind": "deposit", "asset": "USDT", "amount": 588.24, "ts": now},   # нет цены — не факт
                            leg("sell", 588.24, 90.0, now)]}
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_none_outside_time_window():
    now = time.time()
    trade = _trade_row(now)
    hist_by_ex = {"bybit": [leg("buy", 588.24, 85.0, now - trades.AUTO_MATCH_WINDOW - 60),
                            leg("sell", 588.24, 90.0, now)]}
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_none_when_fiat_amount_too_different():
    now = time.time()
    trade = _trade_row(now, amount=50000.0)
    # 10 USDT * 85 ₽ = 850 ₽ — совсем не похоже на круг в 50 000 ₽
    hist_by_ex = {"bybit": [leg("buy", 10.0, 85.0, now), leg("sell", 588.24, 90.0, now)]}
    assert trades.match_fact(trade, hist_by_ex) is None


def test_match_fact_picks_closest_candidate_by_time():
    now = time.time()
    trade = _trade_row(now, amount=50000.0)
    hist_by_ex = {"bybit": [leg("buy", 588.24, 84.0, now - 600),   # дальше
                            leg("buy", 588.24, 85.0, now + 30),    # ближе
                            leg("sell", 588.24, 90.0, now)]}
    assert trades.match_fact(trade, hist_by_ex) == pytest.approx((90.0 / 85.0 - 1) * 100)


def _fact_row(db, trade_id):
    con = sqlite3.connect(db)
    row = con.execute("SELECT fact, fact_source FROM trades WHERE id = ?", (trade_id,)).fetchone()
    con.close()
    return row


def test_old_db_gets_fact_source_column_and_old_facts_stay_counted(tmp_path):
    db = str(tmp_path / "trades.db")
    con = sqlite3.connect(db)   # журнал прошлой версии: без fact_source
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, buy_ex TEXT, "
                "buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, amount REAL, profit REAL, bank TEXT DEFAULT '', "
                "fact REAL DEFAULT NULL, kind TEXT DEFAULT '', buy_nick TEXT DEFAULT '', sell_nick TEXT DEFAULT '')")
    con.execute("INSERT INTO trades (ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit, fact) "
                "VALUES (?, 'r', 'Bybit', 'USDT', 'MEXC', 'USDT', 10000, 2.0, 1.5)", (time.time(),))
    con.commit()
    con.close()
    st = trades.stats(path=db)["day"]
    assert st["fact_count"] == 1 and st["avg_fact"] == 1.5 and st["plan_facts"] == 0   # откуда факт — неизвестно
    assert _fact_row(db, 1) == (1.5, None)


def test_plan_facts_are_excluded_from_calibration(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    ids = [trades.log_trade(deal(2.0), 50000, path=db, ts=now)[0] for _ in range(5)]
    trades.set_fact(ids[0], 2.0, path=db, source=trades.FACT_PLAN)          # «как расчёт»
    trades.set_fact(ids[1], 2.5, path=db, source=trades.FACT_PLAN_SHIFT)    # «+0.5 п.п.»
    trades.set_fact(ids[2], 1.0, path=db, source=trades.FACT_MANUAL)
    trades.set_fact(ids[3], 0.0, path=db, source=trades.FACT_AUTO)
    assert _fact_row(db, ids[0]) == (2.0, "plan") and _fact_row(db, ids[1]) == (2.5, "plan±")
    st = trades.stats(path=db, now=now)["day"]
    assert st["count"] == 5 and st["fact_count"] == 2 and st["plan_facts"] == 2
    assert st["avg_fact"] == 0.5 and st["avg_diff"] == -1.5              # только manual и auto
    assert trades.facts_by_pair(path=db) == {("Bybit", "USDT", "MEXC", "USDT"): {"count": 2, "avg_fact": 0.5}}
    # автосопоставление берёт и сделки с фактом «как расчёт»/±0.5 — найденные ордера лучше расчёта
    assert sorted(t["id"] for t in trades.unmatched(path=db)) == [ids[0], ids[1], ids[4]]
    assert {t["id"]: t["fact_source"] for t in trades.unmatched(path=db)}[ids[0]] == "plan"


def _cp_deal(buy_nick="b", sell_nick="s", pays=("T-Bank",), buy_nicks=(), sell_ex="MEXC"):
    """Связка с заданными никами мерчантов; buy_nicks — покупка собрана из нескольких объявлений стакана."""
    b = dataclasses.replace(make_ad("Bybit", "buy", 85.0, pays=pays), nick=buy_nick, nicks=tuple(buy_nicks))
    s = dataclasses.replace(make_ad(sell_ex, "sell", 90.0), nick=sell_nick)
    return 2.0, b, s, "маршрут"


def test_log_trade_stores_merchant_nicks_and_stacked_nicks(tmp_path):
    db = str(tmp_path / "trades.db")
    trades.log_trade(_cp_deal("Вася", "Петя"), 10000, path=db, ts=time.time())
    trades.log_trade(_cp_deal("2 объявл.", "Петя", buy_nicks=("a", "b", "a")), 10000, path=db, ts=time.time())
    con = sqlite3.connect(db)
    rows = con.execute("SELECT buy_nick, sell_nick FROM trades ORDER BY id").fetchall()
    con.close()
    assert rows == [("Вася", "Петя"), ("a, b", "Петя")]              # вместо «2 объявл.» — ники стакана


def test_old_trades_db_is_migrated_with_nick_columns(tmp_path):
    db = str(tmp_path / "trades.db")
    con = sqlite3.connect(db)   # журнал прошлой версии: без buy_nick/sell_nick
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, buy_ex TEXT, "
                "buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, amount REAL, profit REAL, bank TEXT DEFAULT '', "
                "fact REAL DEFAULT NULL, kind TEXT DEFAULT '')")
    con.execute("INSERT INTO trades (ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit, bank, kind) "
                "VALUES (?, 'r', 'Bybit', 'USDT', 'MEXC', 'USDT', 10000, 2.0, 'T-Bank', 'intra')", (time.time(),))
    con.commit()
    con.close()
    assert trades.counterparties("T-Bank", path=db) == (0, 0)          # старая запись без ников — не в счёт
    trades.log_trade(_cp_deal("Вася", "Петя"), 10000, path=db, ts=time.time())
    assert trades.counterparties("T-Bank", path=db) == (2, 2)
    assert trades.stats(path=db)["day"]["count"] == 2                   # старая сделка в журнале осталась


def test_counterparties_dedup_and_bank_filter(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    trades.log_trade(_cp_deal("Вася", "Петя"), 10000, path=db, ts=now)
    trades.log_trade(_cp_deal("Вася", "Петя"), 10000, path=db, ts=now)                  # те же двое — не новые
    trades.log_trade(_cp_deal("Вася", "Петя", sell_ex="HTX"), 10000, path=db, ts=now)   # Петя на HTX — другой
    trades.log_trade(_cp_deal("Коля", "Женя", pays=("Sberbank",)), 10000, path=db, ts=now)  # другая карта
    assert trades.counterparties("T-Bank", path=db, now=now) == (3, 3)
    assert trades.counterparties("Sberbank", path=db, now=now) == (2, 2)
    assert trades.counterparties("VTB", path=db, now=now) == (0, 0)
    assert trades.counterparties("", path=db, now=now) == (0, 0)
    assert trades.counterparties("T-Bank", path=str(tmp_path / "none.db")) == (0, 0)
    assert trades.month_banks(path=db, now=now) == ["Sberbank", "T-Bank"]


def test_counterparties_day_msk_vs_calendar_month(tmp_path):
    db = str(tmp_path / "trades.db")
    def at(*a):
        return datetime.datetime(*a, tzinfo=trades.MSK).timestamp()

    now = at(2026, 9, 25, 0, 30)                                           # чуть после полуночи МСК
    trades.log_trade(_cp_deal("сегодня1", "сегодня2"), 10000, path=db, ts=at(2026, 9, 25, 0, 10))
    trades.log_trade(_cp_deal("вчера", "сегодня2"), 10000, path=db, ts=at(2026, 9, 24, 23, 50))   # 40 мин назад
    trades.log_trade(_cp_deal("август", "август2"), 10000, path=db, ts=at(2026, 8, 31, 23, 50))   # прошлый месяц
    assert trades.counterparties("T-Bank", path=db, now=now) == (2, 3)
    assert trades.month_banks(path=db, now=at(2026, 10, 1, 0, 5)) == []                 # новый месяц — пусто


def test_counterparties_counts_each_nick_of_stacked_ad(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    trades.log_trade(_cp_deal("3 объявл.", "Петя", buy_nicks=("a", "b", "c")), 10000, path=db, ts=now)
    trades.log_trade(_cp_deal("a", "Петя"), 10000, path=db, ts=now)                     # «a» уже был в стакане
    assert trades.counterparties("T-Bank", path=db, now=now) == (4, 4)
