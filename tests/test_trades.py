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
