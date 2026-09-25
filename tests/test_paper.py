import os
import time

import paper
from helpers import make_ad


def test_settings_defaults(monkeypatch):
    for k in ("PAPER", "PAPER_AMOUNT", "PAPER_PAY_MINUTES", "PAPER_TRANSFER_MINUTES", "PAPER_MAX_OPEN"):
        monkeypatch.delenv(k, raising=False)
    s = paper.settings()
    assert s == {"on": False, "amount": 10000.0, "pay_minutes": 5.0, "transfer_minutes": 3.0, "max_open": 1}


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


def test_check_buy_stage_fails_when_price_worse():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "nick", "buy_price": 85.0}
    ad = make_ad("Bybit", "buy", 86.0)   # тот же мерчант, но цена выросла — хуже плана покупки
    action, note = paper.check_buy_stage(cycle, _cycle_snap(ads=[ad]), pay_minutes=5, now=1400.0)
    assert action == "fail" and "цена ушла" in note


def test_check_buy_stage_ignores_different_merchant():
    cycle = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "buy_nick": "other", "buy_price": 85.0}
    ad = make_ad("Bybit", "buy", 84.0)   # цена ок, но не тот мерчант — не считается тем же объявлением
    action, note = paper.check_buy_stage(cycle, _cycle_snap(ads=[ad]), pay_minutes=5, now=1400.0)
    assert action == "fail" and "исчезло" in note


def test_finish_cycle_missing_id_returns_false_and_no_balance_change(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.init_balance(10000, path=db)
    assert paper.finish_cycle(999, "done", realized_pct=5.0, path=db) is False
    assert paper.get_balance(path=db) == 10000
