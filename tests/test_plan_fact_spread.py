"""Расхождение план → факт в /stats: медиана, худший дециль, доля хуже допуска, худшие сделки (trades.plan_fact_spread,
bot.spread_lines)."""
import bot as B
import p2p
import trades
from helpers import arun, make_ad

T = 1_790_000_000.0


def d(buy="Bybit", sell="MEXC", profit=3.0, asset="USDT"):
    return profit, make_ad(buy, "buy", 85.0, asset=asset), make_ad(sell, "sell", 90.0, asset=asset), "route"


def log(db, deal, amount=10000, fact=None, source=trades.FACT_MANUAL, ts=T):
    tid = trades.log_trade(deal, amount, path=db, ts=ts)[0]
    if fact is not None:
        trades.set_fact(tid, fact, path=db, source=source)
    return tid


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def test_plan_fact_spread_basic_stats(tmp_path):
    db = str(tmp_path / "trades.db")
    for i, diff in enumerate((-1.5, -0.5, 0.0, 0.5, 1.0)):
        log(db, d(profit=3.0), fact=3.0 + diff, ts=T + i)
    data = trades.plan_fact_spread(0.0, path=db)
    assert data["n"] == 5
    assert data["median"] == 0.0
    assert data["p10"] == -1.5             # минимум при n < 10
    assert data["bad_n"] == 1              # -0.5 ровно на границе — НЕ плохая (строгое <)
    assert data["share_bad"] == 0.2


def test_plan_fact_spread_p10_formula_for_25_trades(tmp_path):
    db = str(tmp_path / "trades.db")
    for i in range(25):
        log(db, d(profit=0.0), fact=float(i), ts=T + i)   # diff = 0..24, отсортировано
    data = trades.plan_fact_spread(0.0, path=db)
    diffs = sorted(float(i) for i in range(25))
    assert data["n"] == 25
    assert data["p10"] == diffs[2]         # (n - 1) // 10 = 2


def test_plan_estimates_and_missing_fact_excluded(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d(profit=3.0), fact=2.0, ts=T)
    log(db, d(profit=3.0), fact=2.5, ts=T + 1)
    log(db, d(profit=3.0), fact=2.8, ts=T + 2)
    log(db, d(profit=3.0), fact=3.5, ts=T + 3, source=trades.FACT_PLAN)       # «как расчёт» — не факт
    log(db, d(profit=3.0), fact=4.5, ts=T + 4, source=trades.FACT_PLAN_SHIFT)  # «±0.5 п.п.» — не факт
    log(db, d(profit=3.0), ts=T + 5)                                           # без факта
    log(db, d(profit=3.0), fact=2.9, ts=T - 200 * 86400)                       # за пределами since
    data = trades.plan_fact_spread(T - 30 * 86400, path=db)
    assert data["n"] == 3
    ids_used = {w["id"] for w in data["worst"]}
    assert len(ids_used) == 3
    # fact_source=None (старые записи) и FACT_AUTO — попадают
    log(db, d(profit=3.0), fact=2.0, ts=T + 6, source=trades.FACT_AUTO)
    log(db, d(profit=3.0), fact=2.0, ts=T + 7, source=None)
    data2 = trades.plan_fact_spread(T - 30 * 86400, path=db)
    assert data2["n"] == 5


def test_plan_fact_spread_small_n_and_missing_db(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d(profit=3.0), fact=2.0, ts=T)
    log(db, d(profit=3.0), fact=2.5, ts=T + 1)
    assert trades.plan_fact_spread(0.0, path=db) is None          # n == 2
    assert trades.plan_fact_spread(0.0, path=str(tmp_path / "none.db")) is None
    log(db, d(profit=3.0), fact=3.0, ts=T + 2)
    assert trades.plan_fact_spread(0.0, path=db) is not None       # n == 3


def test_profit_null_not_counted(tmp_path):
    import sqlite3
    db = str(tmp_path / "trades.db")
    log(db, d(profit=3.0), fact=2.0, ts=T)
    log(db, d(profit=3.0), fact=2.5, ts=T + 1)
    tid = log(db, d(profit=3.0), fact=2.9, ts=T + 2)
    con = sqlite3.connect(db)
    con.execute("UPDATE trades SET profit = NULL WHERE id = ?", (tid,))
    con.commit()
    con.close()
    assert trades.plan_fact_spread(0.0, path=db) is None           # осталось 2 полноценных


def test_worst_sorted_ascending_and_limited(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d(profit=3.0), fact=1.0, ts=T)       # diff -2.0
    log(db, d(profit=3.0), fact=2.0, ts=T + 1)   # diff -1.0
    log(db, d(profit=3.0), fact=3.0, ts=T + 2)   # diff 0.0
    log(db, d(profit=3.0), fact=4.0, ts=T + 3)   # diff +1.0
    data = trades.plan_fact_spread(0.0, path=db, worst=2)
    assert len(data["worst"]) == 2
    assert [round(w["diff"], 2) for w in data["worst"]] == [-2.0, -1.0]
    for w in data["worst"]:
        assert set(w) == {"id", "ts", "buy_ex", "sell_ex", "diff"}


def test_worst_tie_break_by_id(tmp_path):
    db = str(tmp_path / "trades.db")
    ids = [log(db, d(profit=3.0), fact=2.0, ts=T + i) for i in range(3)]   # одинаковый diff = -1.0
    data = trades.plan_fact_spread(0.0, path=db, worst=3)
    assert [w["id"] for w in data["worst"]] == sorted(ids)


def test_since_excludes_old_trades(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d(profit=3.0), fact=2.0, ts=T - 40 * 86400)
    log(db, d(profit=3.0), fact=2.5, ts=T)
    log(db, d(profit=3.0), fact=3.0, ts=T + 1)
    log(db, d(profit=3.0), fact=3.5, ts=T + 2)
    data = trades.plan_fact_spread(T - 30 * 86400, path=db)
    assert data["n"] == 3


def test_spread_lines_none():
    assert B.spread_lines(None) == ["", "План → факт: мало сделок с фактом (меньше 3)"]


def test_spread_lines_formats_and_negative_checks():
    data = {"n": 5, "median": -0.2, "p10": -1.5, "bad_n": 1, "share_bad": 0.2,
            "worst": [{"id": 12, "ts": T, "buy_ex": "Bybit", "sell_ex": "Rapira", "diff": -1.4},
                      {"id": 9, "ts": T, "buy_ex": "A&B", "sell_ex": "MEXC", "diff": -0.6},
                      {"id": 3, "ts": T, "buy_ex": "Bybit", "sell_ex": "MEXC", "diff": 0.3}]}
    lines = B.spread_lines(data)
    text = "\n".join(lines)
    assert "-0.20" in text and "20% (1 из 5)" in text and "-0.5" in text
    assert text.count("#") == 2                        # только отрицательные diff
    assert "→" in text and "->" not in text and "nan" not in text
    assert "A&amp;B" in text                            # html.escape


def test_spread_lines_no_worst_line_when_all_non_negative():
    data = {"n": 3, "median": 0.1, "p10": 0.0, "bad_n": 0, "share_bad": 0.0,
            "worst": [{"id": 1, "ts": T, "buy_ex": "Bybit", "sell_ex": "MEXC", "diff": 0.1},
                      {"id": 2, "ts": T, "buy_ex": "Bybit", "sell_ex": "MEXC", "diff": 0.2}]}
    lines = B.spread_lines(data)
    assert not any(line.startswith("Худшие:") for line in lines)


def test_stats_view_shows_plan_fact_block():
    log(trades.DB_PATH, d(profit=3.0), fact=1.0, ts=None)
    log(trades.DB_PATH, d(profit=3.0), fact=2.0, ts=None)
    log(trades.DB_PATH, d(profit=3.0), fact=3.0, ts=None)
    bot = Stub(p2p.Config())
    arun(bot.handle("/stats"))
    text = texts(bot)[-1]
    assert "План → факт за месяц" in text and "#" in text


def test_stats_view_no_trades_shows_few_facts_message():
    bot = Stub(p2p.Config())
    arun(bot.handle("/stats"))
    text = texts(bot)[-1]
    assert "мало сделок с фактом (меньше 3)" in text
