import os
import time

import pytest

import p2p
import paper
from helpers import make_ad


def test_settings_defaults(monkeypatch):
    for k in ("PAPER", "PAPER_AMOUNT", "PAPER_PAY_MINUTES", "PAPER_TRANSFER_MINUTES", "PAPER_MAX_OPEN"):
        monkeypatch.delenv(k, raising=False)
    s = paper.settings()
    assert s == {"on": False, "amount": 10000.0, "pay_minutes": 5.0, "transfer_minutes": 3.0, "max_open": 1,
                 "traps": False}


def test_settings_reads_env_each_call(monkeypatch):
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "20000")
    s = paper.settings()
    assert s["on"] is True and s["amount"] == 20000.0


def test_init_balance_then_get(tmp_path):
    db = str(tmp_path / "paper.db")
    assert paper.get_balance(path=db) is None    # ещё не заведён
    paper.init_balance(10000, path=db)
    assert paper.get_balance(path=db) == 10000
    paper.init_balance(20000, path=db)            # уже есть — не переписывает
    assert paper.get_balance(path=db) == 10000


def test_apply_result_changes_balance(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.init_balance(10000, path=db)
    paper.apply_result(2.0, 10000, path=db)        # +2% от 10000 = +200
    assert paper.get_balance(path=db) == 10200
    paper.apply_result(-1.0, 10000, path=db)        # -1% от 10000 = -100
    assert paper.get_balance(path=db) == 10100


def test_start_cycle_stores_ads_and_stage(tmp_path):
    db = str(tmp_path / "paper.db")
    buy = make_ad("Bybit", "buy", 85.0)
    sell = make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "перевод USDT (BEP20)", 2.5, path=db, ts=1000.0)
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "buy" and c["result"] is None and c["realized_pct"] is None
    assert c["buy_ex"] == "Bybit" and c["buy_price"] == 85.0 and c["buy_nick"] == "nick"
    assert c["sell_ex"] == "MEXC" and c["sell_price"] == 90.0
    assert c["route"] == "перевод USDT (BEP20)" and c["planned_pct"] == 2.5
    assert c["amount"] == 10000 and c["ts_start"] == 1000.0 and c["ts_stage"] == 1000.0


def test_start_cycle_stores_bank_from_buy_pays(tmp_path):
    db = str(tmp_path / "paper.db")
    buy = make_ad("Bybit", "buy", 85.0, pays=("T-Bank",))
    sell = make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=1000.0)
    assert paper.get_cycle(cid, path=db)["bank"] == "T-Bank"


def test_start_cycle_bank_empty_when_pay_method_unknown(tmp_path):
    db = str(tmp_path / "paper.db")
    buy = make_ad("Bybit", "buy", 85.0, pays=("Qiwi",))
    sell = make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=1000.0)
    assert paper.get_cycle(cid, path=db)["bank"] == ""


def test_bank_month_total_sums_cycles_of_that_bank_this_month(tmp_path):
    db = str(tmp_path / "paper.db")
    now = time.time()
    buy_tb = make_ad("Bybit", "buy", 85.0, pays=("T-Bank",))
    buy_alfa = make_ad("Bybit", "buy", 85.0, pays=("Alfa-bank",))
    sell = make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(40000, buy_tb, sell, "route", 2.0, path=db, ts=now)
    paper.start_cycle(15000, buy_tb, sell, "route", 2.0, path=db, ts=now)
    paper.start_cycle(60000, buy_alfa, sell, "route", 2.0, path=db, ts=now)
    assert paper.bank_month_total("T-Bank", path=db, now=now) == 55000
    assert paper.bank_month_total("Alfa-bank", path=db, now=now) == 60000
    assert paper.bank_month_total("VTB", path=db, now=now) == 0.0
    assert paper.bank_month_total("", path=db, now=now) == 0.0


def test_bank_month_total_excludes_previous_month(tmp_path):
    db = str(tmp_path / "paper.db")
    now = time.time()
    prev_month = paper._month_start(now) - 86400   # день из прошлого календарного месяца
    buy = make_ad("Bybit", "buy", 85.0, pays=("T-Bank",))
    sell = make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(90000, buy, sell, "route", 2.0, path=db, ts=prev_month)
    assert paper.bank_month_total("T-Bank", path=db, now=now) == 0.0
    paper.start_cycle(40000, buy, sell, "route", 2.0, path=db, ts=now)
    assert paper.bank_month_total("T-Bank", path=db, now=now) == 40000


def test_banks_this_month_lists_only_banks_with_cycles(tmp_path):
    db = str(tmp_path / "paper.db")
    assert paper.banks_this_month(path=db) == {}
    now = time.time()
    buy_tb = make_ad("Bybit", "buy", 85.0, pays=("T-Bank",))
    buy_unknown = make_ad("Bybit", "buy", 85.0, pays=("Qiwi",))
    sell = make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(110000, buy_tb, sell, "route", 2.0, path=db, ts=now)
    paper.start_cycle(5000, buy_unknown, sell, "route", 2.0, path=db, ts=now)
    assert paper.banks_this_month(path=db, now=now) == {"T-Bank": 110000}


def test_banks_this_month_excludes_previous_month(tmp_path):
    db = str(tmp_path / "paper.db")
    now = time.time()
    prev_month = paper._month_start(now) - 86400
    buy = make_ad("Bybit", "buy", 85.0, pays=("T-Bank",))
    sell = make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(50000, buy, sell, "route", 2.0, path=db, ts=prev_month)
    assert paper.banks_this_month(path=db, now=now) == {}


def test_get_cycle_missing_returns_none(tmp_path):
    db = str(tmp_path / "paper.db")
    assert paper.get_cycle(1, path=db) is None
    assert paper.get_cycle(1, path=str(tmp_path / "none.db")) is None


def test_open_cycles_excludes_finished(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    a = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    b = paper.start_cycle(10000, buy, sell, "route", 3.0, path=db)
    assert {c["id"] for c in paper.open_cycles(path=db)} == {a, b}
    paper.finish_cycle(a, "done", realized_pct=2.0, path=db)
    open_ids = {c["id"] for c in paper.open_cycles(path=db)}
    assert open_ids == {b}


def test_set_stage_advances_open_cycle(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.set_stage(cid, "transfer", path=db, ts=2000.0)
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "transfer" and c["ts_stage"] == 2000.0 and c["result"] is None


def test_finish_cycle_done_updates_balance_and_record(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.init_balance(10000, path=db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    ok = paper.finish_cycle(cid, "done", realized_pct=1.8, note="ок", path=db, ts=3000.0)
    assert ok is True
    c = paper.get_cycle(cid, path=db)
    assert c["result"] == "done" and c["realized_pct"] == 1.8 and c["note"] == "ок" and c["ts_stage"] == 3000.0
    assert paper.get_balance(path=db) == 10000 + 10000 * 1.8 / 100


def test_finish_cycle_failed_defaults_to_zero_realized(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.init_balance(10000, path=db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid, "failed_buy", note="объявление исчезло", path=db)
    c = paper.get_cycle(cid, path=db)
    assert c["result"] == "failed_buy" and c["realized_pct"] == 0.0
    assert paper.get_balance(path=db) == 10000    # срыв — баланс не поменялся


def _cycle_snap(buy_ex="Bybit", buy_asset="USDT", ads=()):
    import p2p
    groups = {(buy_ex, "buy", buy_asset): list(ads)} if ads else {}
    return p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=groups)


def test_check_buy_stage_waits_before_pay_minutes():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "nick", "buy_price": 85.0}
    action, note = paper.check_buy_stage(cycle, _cycle_snap(), pay_minutes=5, now=1100.0)   # прошло 100с < 300с
    assert action == "wait" and note == ""


def test_check_buy_stage_advances_when_ad_still_there_at_same_or_better_price():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "nick", "buy_price": 85.0}
    ad = make_ad("Bybit", "buy", 84.5)   # nick по умолчанию "nick", цена даже лучше плана
    action, note = paper.check_buy_stage(cycle, _cycle_snap(ads=[ad]), pay_minutes=5, now=1400.0)
    assert action == "advance" and note == ""


def test_check_buy_stage_fails_when_ad_gone():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "nick", "buy_price": 85.0}
    action, note = paper.check_buy_stage(cycle, _cycle_snap(), pay_minutes=5, now=1400.0)
    assert action == "fail" and "исчезло" in note


def test_check_buy_stage_ignores_price_change_of_same_merchant():
    """Цена в ордере фиксируется при создании: рост цены объявления потом круг не срывает."""
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "nick", "buy_price": 85.0}
    ad = make_ad("Bybit", "buy", 86.0)
    action, note = paper.check_buy_stage(cycle, _cycle_snap(ads=[ad]), pay_minutes=5, now=1400.0)
    assert action == "advance" and note == ""


def test_check_buy_stage_ignores_different_merchant():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "other", "buy_price": 85.0}
    ad = make_ad("Bybit", "buy", 84.0)   # цена ок, но не тот мерчант — не считается тем же объявлением
    action, note = paper.check_buy_stage(cycle, _cycle_snap(ads=[ad]), pay_minutes=5, now=1400.0)
    assert action == "fail" and "исчезло" in note


def _cfg():
    import p2p
    c = p2p.Config()
    c.risk_buffer, c.pay_fee = {}, 0.0
    return c


def test_check_transfer_stage_waits_before_transfer_minutes():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT"}
    action, note = paper.check_transfer_stage(cycle, _cfg(), transfer_minutes=3, now=1100.0)   # 100с < 180с
    assert action == "wait" and note == ""


def test_check_transfer_stage_advances_when_withdraw_open():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT"}
    action, note = paper.check_transfer_stage(cycle, _cfg(), transfer_minutes=3, now=1300.0)
    assert action == "advance" and note == ""   # сведений о закрытии нет — не мешаем


def test_check_transfer_stage_fails_when_withdraw_closed():
    import netstatus
    netstatus._apply("Bybit", "USDT", {n: {"dep": True, "wd": False, "fee": 1.0}
                                        for n in ("TRC20", "BEP20", "ERC20", "TON")})
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT"}
    action, note = paper.check_transfer_stage(cycle, _cfg(), transfer_minutes=3, now=1300.0)
    assert action == "fail" and "закрыт" in note


def _sell_cycle(sell_ex="MEXC", sell_asset="USDT", sell_nick="nick", sell_price=90.0,
                buy_price=85.0, amount=10000.0):
    return {"sell_ex": sell_ex, "sell_asset": sell_asset, "sell_nick": sell_nick,
            "sell_price": sell_price, "buy_price": buy_price, "amount": amount, "planned_pct": 2.0}


def _sell_snap(sell_ex="MEXC", sell_asset="USDT", ads=()):
    import p2p
    groups = {(sell_ex, "sell", sell_asset): list(ads)} if ads else {}
    return p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=groups)


def test_check_sell_stage_advances_when_ad_still_there_at_same_or_better_price():
    cycle = _sell_cycle()
    ad = make_ad("MEXC", "sell", 91.0, avail=10000)   # цена даже лучше плана, глубины хватает
    action, note, price = paper.check_sell_stage(cycle, _sell_snap(ads=[ad]))
    assert action == "advance" and price == 91.0 and "+1.11%" in note


def test_check_sell_stage_fails_only_when_nobody_buys_the_volume():
    cycle = _sell_cycle()
    action, note, price = paper.check_sell_stage(cycle, _sell_snap())
    assert action == "fail" and "глубины" in note and price is None
    small = make_ad("MEXC", "sell", 90.0, avail=10, max_amt=900)   # берёт 10 монет из ~117
    assert paper.check_sell_stage(cycle, _sell_snap(ads=[small]))[0] == "fail"


def test_check_sell_stage_sells_at_worse_price_and_realizes_less():
    """Круг #1 25.09: курс 91,88 → 91,31 — не срыв, а продажа по 91,31 с фактом ниже плана."""
    cycle = _sell_cycle()
    ad = make_ad("MEXC", "sell", 89.0)
    action, note, price = paper.check_sell_stage(cycle, _sell_snap(ads=[ad]))
    assert action == "advance" and price == 89.0 and "-1.11%" in note
    assert 0 < paper.realized_pct(cycle, price) < cycle["planned_pct"]
    crash = make_ad("MEXC", "sell", 85.0)   # ниже безубытка — факт уходит в минус, это тоже итог круга
    assert paper.realized_pct(cycle, paper.check_sell_stage(cycle, _sell_snap(ads=[crash]))[2]) < 0


def test_check_sell_stage_uses_other_merchants_and_averages_depth():
    """Плановый мерчант ушёл — продаём тем, кто есть; объём не влезает в одно объявление — средняя цена."""
    cycle = _sell_cycle()   # 10 000 ₽ / 85 ≈ 117,6 монеты
    a = p2p.Ad("MEXC", "sell", 91.0, 100, 5000, 50, ["T-Bank"], "other1", 200, 100.0, "", "USDT", "", "")
    b = p2p.Ad("MEXC", "sell", 89.0, 100, 500000, 1000, ["T-Bank"], "other2", 200, 100.0, "", "USDT", "", "")
    action, note, price = paper.check_sell_stage(cycle, _sell_snap(ads=[a, b]))
    qty = 10000.0 / 85.0
    take_a = min(qty, 50, 5000 / 91.0)
    expected = (take_a * 91.0 + (qty - take_a) * 89.0) / qty
    assert action == "advance" and abs(price - expected) < 1e-9


def test_check_sell_stage_fails_when_depth_insufficient():
    cycle = _sell_cycle()
    ad = make_ad("MEXC", "sell", 90.0, min_amt=1000, max_amt=1000, avail=1000 / 90.0)   # глубины мало
    action, note, price = paper.check_sell_stage(cycle, _sell_snap(ads=[ad]))
    assert action == "fail" and "глубины" in note and price is None


def test_realized_pct_matches_planned_when_sell_price_unchanged():
    cycle = _sell_cycle(sell_price=90.0)
    assert paper.realized_pct(cycle, 90.0) == pytest.approx(2.0)


def test_realized_pct_higher_when_sell_price_better():
    cycle = _sell_cycle(sell_price=90.0)
    rp = paper.realized_pct(cycle, 91.0)   # продали дороже плана — факт лучше плана
    assert rp > 2.0
    assert rp == pytest.approx((1.02 * 91.0 / 90.0 - 1) * 100)


def test_finish_cycle_missing_id_returns_false_and_no_balance_change(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.init_balance(10000, path=db)
    assert paper.finish_cycle(999, "done", realized_pct=5.0, path=db) is False
    assert paper.get_balance(path=db) == 10000


def test_stats_empty_db(tmp_path):
    db = str(tmp_path / "paper.db")
    st = paper.stats(path=db)
    assert st["day"] == {"total": 0, "done": 0, "failed": 0, "failed_by_reason": {}, "avg_diff": None}
    assert st["week"]["total"] == 0 and st["all"]["total"] == 0


def test_stats_counts_done_and_failed(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    now = time.time()
    cid1 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.finish_cycle(cid1, "done", realized_pct=2.5, path=db, ts=now)   # факт лучше плана на 0.5 п.п.
    cid2 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.finish_cycle(cid2, "failed_sell", realized_pct=0.0, note="цена ушла", path=db, ts=now)
    st = paper.stats(path=db, now=now)
    assert st["day"] == {"total": 2, "done": 1, "failed": 1,
                         "failed_by_reason": {"failed_sell": 1}, "avg_diff": pytest.approx(0.5)}
    assert st["all"] == st["day"]   # оба круга сегодня же


def test_stats_day_excludes_older_cycle(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    now = time.time()
    old = now - 8 * 86400   # больше недели назад — не попадает ни в день, ни в неделю
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=old)
    paper.finish_cycle(cid, "done", realized_pct=2.0, path=db, ts=old)
    st = paper.stats(path=db, now=now)
    assert st["day"]["total"] == 0 and st["week"]["total"] == 0
    assert st["all"]["total"] == 1   # но за всё время — виден


def test_stats_open_cycle_not_counted(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)   # ещё открыт, result IS NULL
    st = paper.stats(path=db)
    assert st["all"]["total"] == 0


def test_balance_change_no_db():
    assert paper.balance_change(path="/nonexistent/paper.db") == 0.0


def test_balance_change_sums_finished_cycles(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.init_balance(10000, path=db)
    cid1 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid1, "done", realized_pct=2.0, path=db)      # +200
    cid2 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid2, "failed_buy", realized_pct=0.0, path=db)  # +0
    assert paper.balance_change(path=db) == pytest.approx(200.0)
    assert paper.get_balance(path=db) == pytest.approx(10200.0)


def _fill_cycles(db, done=18, failed=2, diff=0.0, ts=None):
    """Завести done+failed завершённых кругов: done — план 2.0%, факт 2.0+diff% (для медианы),
    failed — failed_sell. Все с сегодняшним ts (или заданным) по умолчанию."""
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    for _ in range(done):
        cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=ts)
        paper.finish_cycle(cid, "done", realized_pct=2.0 + diff, path=db, ts=ts)
    for _ in range(failed):
        cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=ts)
        paper.finish_cycle(cid, "failed_sell", realized_pct=0.0, note="цена ушла", path=db, ts=ts)


def test_ladder_suggestion_none_without_db():
    assert paper.ladder_suggestion(path="/nonexistent/paper.db") is None


def test_ladder_suggestion_none_below_min_cycles(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _fill_cycles(db, done=15, failed=2)   # 17 < 20
    assert paper.ladder_suggestion(path=db) is None


def test_ladder_suggestion_up_when_criteria_met(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _fill_cycles(db, done=18, failed=2)   # 20 всего, срывов 10%, факт == план (diff 0)
    assert paper.ladder_suggestion(path=db) == {"action": "up", "amount": 20000.0}


def test_ladder_suggestion_none_when_too_many_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _fill_cycles(db, done=15, failed=5)   # 20 всего, срывов 25% > 20%
    assert paper.ladder_suggestion(path=db) is None


def test_ladder_suggestion_none_when_median_too_low(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _fill_cycles(db, done=18, failed=2, diff=-0.5)   # медиана факт-план -0.5 п.п. < -0.3
    assert paper.ladder_suggestion(path=db) is None


def test_ladder_suggestion_none_when_already_at_high(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "20000")
    db = str(tmp_path / "paper.db")
    _fill_cycles(db, done=18, failed=2)   # критерии повышения выполнены, но уже на 20000
    assert paper.ladder_suggestion(path=db) is None


def test_ladder_suggestion_down_when_week_failure_high(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "20000")
    db = str(tmp_path / "paper.db")
    now = time.time()
    _fill_cycles(db, done=2, failed=3, ts=now)   # 5 за неделю, срывов 60% > 40%
    assert paper.ladder_suggestion(path=db, now=now) == {"action": "down", "amount": 10000.0}


def test_ladder_suggestion_none_when_week_failure_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_AMOUNT", "20000")
    db = str(tmp_path / "paper.db")
    now = time.time()
    _fill_cycles(db, done=4, failed=1, ts=now)   # 5 за неделю, срывов 20% <= 40%
    assert paper.ladder_suggestion(path=db, now=now) is None


def test_report_rows_empty_without_db():
    assert paper.report_rows(path="/nonexistent/paper.db") == []


def test_report_rows_groups_by_venue_pair_and_computes_plan_vs_fact(tmp_path):
    db = str(tmp_path / "paper.db")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    now = time.time()
    c1 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.finish_cycle(c1, "done", realized_pct=2.5, path=db, ts=now + 360)   # 6 мин
    c2 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.finish_cycle(c2, "done", realized_pct=1.5, path=db, ts=now + 240)   # 4 мин
    c3 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.finish_cycle(c3, "failed_sell", realized_pct=0.0,
                       note="не хватает глубины стакана продажи", path=db, ts=now)
    c4 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.finish_cycle(c4, "failed_buy", realized_pct=0.0, note="объявление покупки исчезло",
                       path=db, ts=now)
    other_buy, other_sell = make_ad("HTX", "buy", 85.0), make_ad("KuCoin", "sell", 90.0)
    c5 = paper.start_cycle(10000, other_buy, other_sell, "route", 1.0, path=db, ts=now)
    paper.finish_cycle(c5, "done", realized_pct=1.0, path=db, ts=now + 120)
    cid_open = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)   # открыт — не считается

    rows = paper.report_rows(path=db)
    assert len(rows) == 2   # (Bybit,MEXC) и (HTX,KuCoin), открытый круг c_open не попал
    row = next(r for r in rows if r["buy_ex"] == "Bybit")
    assert row["buy_asset"] == "USDT" and row["sell_ex"] == "MEXC" and row["sell_asset"] == "USDT"
    assert row["total"] == 4 and row["done"] == 2 and row["failed"] == 2
    assert row["failed_by_reason"] == {"failed_sell": 1, "failed_buy": 1}
    assert row["depth_shortfall"] == 1
    assert row["avg_planned_pct"] == 2.0
    assert row["avg_realized_pct"] == 2.0   # (2.5+1.5)/2
    assert row["avg_duration_min"] == 5.0   # (6+4)/2


def test_write_report_csv_writes_expected_columns(tmp_path):
    db = str(tmp_path / "paper.db")
    csv_path = str(tmp_path / "report.csv")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid, "done", realized_pct=2.5, path=db)
    rows = paper.report_rows(path=db)
    out_path = paper.write_report_csv(rows, path=csv_path)
    assert out_path == csv_path
    text = open(csv_path, encoding="utf-8").read()
    assert "buy_ex,buy_asset,sell_ex,sell_asset,total,done,failed" in text
    assert "Bybit,USDT,MEXC,USDT,1,1,0" in text
