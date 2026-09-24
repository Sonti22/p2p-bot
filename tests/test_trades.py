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


def test_stats_empty_db_missing_file(tmp_path):
    st = trades.stats(path=str(tmp_path / "none.db"))
    assert all(s == {"count": 0, "amount": 0.0, "avg_profit": 0.0} for s in st.values())


def test_sbp_bank_detects_known_names():
    assert trades.sbp_bank(["T-Bank", "Bank Account"]) == "T-Bank"
    assert trades.sbp_bank(["SBP - Fast Bank Transfer"]) == "SBP"
    assert trades.sbp_bank(["Bank Account", "Payeer"]) == ""


def test_log_trade_warns_once_when_bank_crosses_limit(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    _, total1, crossed1 = trades.log_trade(deal(), 60000, path=db, ts=now)      # 60к — ниже лимита
    assert total1 == 60000 and not crossed1
    bank2, total2, crossed2 = trades.log_trade(deal(), 60000, path=db, ts=now)  # 120к — пересекли 100к
    assert bank2 == "T-Bank" and total2 == 120000 and crossed2
    _, total3, crossed3 = trades.log_trade(deal(), 10000, path=db, ts=now)      # уже выше лимита, не повторяем
    assert total3 == 130000 and not crossed3


def test_log_trade_no_bank_when_pay_method_unknown(tmp_path):
    db = str(tmp_path / "trades.db")
    d = (2.0, make_ad("Bybit", "buy", 85.0, pays=("Payeer",)), make_ad("MEXC", "sell", 90.0), "маршрут")
    bank, total, crossed = trades.log_trade(d, 500000, path=db, ts=time.time())
    assert bank == "" and total == 500000 and not crossed


def test_bank_month_total_excludes_previous_month(tmp_path):
    db = str(tmp_path / "trades.db")
    now = time.time()
    prev_month = trades._month_start(now) - 86400   # день из прошлого календарного месяца
    trades.log_trade(deal(), 90000, path=db, ts=prev_month)
    assert trades.bank_month_total("T-Bank", path=db, now=now) == 0.0
    trades.log_trade(deal(), 40000, path=db, ts=now)
    assert trades.bank_month_total("T-Bank", path=db, now=now) == 40000
