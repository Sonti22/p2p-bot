"""/stats hours|dow|banks|coins: разбивка реальных сделок журнала по часу (МСК), дню недели, банку оплаты и монете
(trades.breakdown) — и её текстовое оформление (bot.breakdown_lines)."""
import datetime

import bot as B
import p2p
import trades
from helpers import arun, make_ad

MON_2330_UTC = datetime.datetime(2026, 9, 21, 23, 30, tzinfo=datetime.timezone.utc).timestamp()
MON_2059_UTC = datetime.datetime(2026, 9, 21, 20, 59, tzinfo=datetime.timezone.utc).timestamp()


def d(buy, sell, profit, buy_asset="USDT", sell_asset="USDT", pays=("T-Bank",)):
    return (profit, make_ad(buy, "buy", 85.0, asset=buy_asset, pays=pays),
            make_ad(sell, "sell", 90.0, asset=sell_asset), "route")


def log(db, deal, amount, ts, fact=None, source=trades.FACT_MANUAL):
    tid = trades.log_trade(deal, amount, path=db, ts=ts)[0]
    if fact is not None:
        trades.set_fact(tid, fact, path=db, source=source)
    return tid


# --- trades.breakdown -------------------------------------------------------------------------------------------

def test_hour_and_dow_cross_midnight(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 1.0), 10000, MON_2330_UTC)      # 23:30 UTC Пн -> 02:30 МСК Вт
    log(db, d("Bybit", "MEXC", 1.0), 10000, MON_2059_UTC)      # 20:59 UTC Пн -> 23:59 МСК Пн
    hours = trades.breakdown("hour", path=db)
    assert [r["key"] for r in hours] == [2, 23]                # отсортировано по key
    dows = trades.breakdown("dow", path=db)
    assert [r["key"] for r in dows] == [0, 1]


def test_bank_groups_and_unspecified(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 1.0, pays=("Sberbank",)), 10000, MON_2330_UTC)
    log(db, d("Bybit", "MEXC", 1.0, pays=("Sberbank",)), 10000, MON_2330_UTC)
    log(db, d("Bybit", "MEXC", 1.0, pays=("Tinkoff",)), 10000, MON_2330_UTC)
    log(db, d("Bybit", "MEXC", 1.0, pays=("Cash",)), 10000, MON_2330_UTC)
    rows = trades.breakdown("bank", path=db)
    assert [(r["key"], r["count"]) for r in rows] == [("Сбер", 2), ("Т-Банк", 1), ("не указан", 1)]


def test_coin_same_and_cross(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 1.0), 10000, MON_2330_UTC)
    log(db, d("Bybit", "MEXC", 1.0, buy_asset="USDT", sell_asset="USDC"), 10000, MON_2330_UTC)
    log(db, d("Bybit", "MEXC", 1.0, buy_asset="USDT", sell_asset="USDC"), 10000, MON_2330_UTC)
    rows = trades.breakdown("coin", path=db)
    assert [(r["key"], r["count"]) for r in rows] == [("USDT→USDC", 2), ("USDT", 1)]
    assert "->" not in "".join(r["key"] for r in rows)


def test_real_fact_excludes_plan_estimates(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC, fact=1.5, source=trades.FACT_MANUAL)
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC, fact=2.0, source=trades.FACT_PLAN)
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC, fact=2.0, source=trades.FACT_PLAN_SHIFT)
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC)     # без факта
    rows = trades.breakdown("coin", path=db)
    row = rows[0]
    assert row["count"] == 4 and row["real_n"] == 1
    assert abs(row["avg_fact"] - 1.5) < 1e-9
    assert abs(row["avg_diff"] - (1.5 - 2.0)) < 1e-9
    assert abs(row["rub"] - 10000 * 1.5 / 100) < 1e-6
    assert abs(row["avg_plan"] - 2.0) < 1e-9          # по всем четырём


def test_auto_and_null_source_count_as_real(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC, fact=1.0, source=trades.FACT_AUTO)
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC, fact=1.0, source=None)
    rows = trades.breakdown("coin", path=db)
    assert rows[0]["real_n"] == 2


def test_no_real_fact_group_has_none_fields(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 2.0), 10000, MON_2330_UTC, fact=2.0, source=trades.FACT_PLAN)
    row = trades.breakdown("coin", path=db)[0]
    assert row["avg_fact"] is None and row["avg_diff"] is None and row["rub"] is None
    lines = B.breakdown_lines("coin", trades.breakdown("coin", path=db))
    assert any("факта нет" in l for l in lines)
    assert any("мало данных" in l for l in lines)   # real_n=0 < BREAKDOWN_MIN_FACTS


def test_since_kind_and_missing_db(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 1.0), 10000, MON_2330_UTC)
    assert trades.breakdown("coin", since=MON_2330_UTC + 3600, path=db) == []
    try:
        trades.breakdown("weird", path=db)
        assert False, "должен быть ValueError"
    except ValueError:
        pass
    missing = str(tmp_path / "none.db")
    assert trades.breakdown("coin", path=missing) == []
    import os
    assert not os.path.exists(missing)


# --- bot.breakdown_lines -----------------------------------------------------------------------------------------

def test_breakdown_lines_limit_and_empty():
    rows = [{"key": f"COIN{i}", "count": 30 - i, "amount": 1000.0, "avg_plan": 1.0, "real_n": 5, "avg_fact": 1.0,
             "avg_diff": 0.0, "rub": 100.0} for i in range(30)]
    lines = B.breakdown_lines("coin", rows)
    group_lines = [l for l in lines if l.startswith("•") and "…ещё" not in l]
    assert len(group_lines) == B.BREAKDOWN_LINES
    assert lines[-1] == f"• …ещё {30 - B.BREAKDOWN_LINES}"
    assert B.breakdown_lines("coin", []) == [f"Сделок за {B.BREAKDOWN_DAYS} дн. нет"]


def test_breakdown_lines_escapes_bank_label():
    rows = [{"key": "<b>Evil</b>", "count": 1, "amount": 100.0, "avg_plan": 1.0, "real_n": 0, "avg_fact": None,
             "avg_diff": None, "rub": None}]
    lines = B.breakdown_lines("bank", rows)
    text = "\n".join(lines)
    assert "<b>Evil</b>" not in text and "&lt;b&gt;" in text


# --- bot integration ---------------------------------------------------------------------------------------------

class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def _last_text(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"][-1]


def test_stats_view_empty_arg_unchanged():
    bot = Stub(p2p.Config())
    assert bot.stats_view() == bot.stats_view("")


def test_handle_stats_hours_and_banks():
    log(trades.DB_PATH, d("Bybit", "MEXC", 1.0, pays=("Sberbank",)), 10000, None)
    bot = Stub(p2p.Config())
    arun(bot.handle("/stats hours"))
    text = _last_text(bot)
    assert ":00" in text and "МСК" in text
    bot2 = Stub(p2p.Config())
    arun(bot2.handle("/stats banks"))
    assert "Сбер" in _last_text(bot2)


def test_handle_stats_unknown_arg_hint():
    bot = Stub(p2p.Config())
    arun(bot.handle("/stats xyz"))
    assert "hours|dow|banks|coins" in _last_text(bot)


def test_handle_stats_escapes_argument():
    bot = Stub(p2p.Config())
    arun(bot.handle("/stats <b>"))
    text = _last_text(bot)
    assert "<b>" not in text and "&lt;b&gt;" in text
