"""/stats: разбивка сделок журнала по направлению площадок (покупка → продажа) — какие пары реально приносят деньги,
по факту, а не только по расчёту."""
import bot as B
import p2p
import trades
from helpers import arun, make_ad

T = 1_790_000_000.0


def d(buy, sell, profit, asset="USDT"):
    return profit, make_ad(buy, "buy", 85.0, asset=asset), make_ad(sell, "sell", 90.0, asset=asset), "route"


def log(db, deal, amount, fact=None, source=trades.FACT_MANUAL, ts=T):
    tid = trades.log_trade(deal, amount, path=db, ts=ts)[0]
    if fact is not None:
        trades.set_fact(tid, fact, path=db, source=source)
    return tid


def test_by_direction_groups_pairs_and_counts_only_real_facts(tmp_path):
    db = str(tmp_path / "trades.db")
    log(db, d("Bybit", "MEXC", 2.0), 100000, fact=1.5)
    log(db, d("Bybit", "MEXC", 1.0, asset="USDC"), 50000, fact=0.5)          # другая монета — то же направление
    log(db, d("Bybit", "MEXC", 1.0), 50000, fact=1.0, source=trades.FACT_PLAN)  # «как расчёт» — не факт
    log(db, d("HTX", "Bybit", 3.0), 20000, fact=-1.0)
    log(db, d("KuCoin", "HTX", 4.0), 10000)
    log(db, d("MEXC", "HTX", 5.0), 10000, ts=T - 40 * 86400)                   # старше окна
    rows = trades.by_direction(T - 30 * 86400, path=db)
    assert [(r["buy_ex"], r["sell_ex"]) for r in rows] == [("Bybit", "MEXC"), ("HTX", "Bybit"), ("KuCoin", "HTX")]
    bm = rows[0]
    assert (bm["count"], bm["amount"], bm["fact_count"]) == (3, 200000, 2)
    assert abs(bm["avg_profit"] - 4 / 3) < 1e-9 and abs(bm["avg_fact"] - 1.0) < 1e-9
    assert abs(bm["fact_rub"] - 1750.0) < 1e-6                                 # 100 000 × 1.5% + 50 000 × 0.5%
    assert rows[1]["fact_rub"] == -200.0
    assert rows[2]["fact_count"] == 0 and rows[2]["avg_fact"] is None and rows[2]["fact_rub"] is None
    assert trades.by_direction(0, path=str(tmp_path / "none.db")) == []


def test_direction_lines_format_and_limit():
    rows = [{"buy_ex": "Bybit", "sell_ex": "MEXC", "count": 3, "amount": 200000, "avg_profit": 1.3333,
             "fact_count": 2, "avg_fact": 1.0, "fact_rub": 1750.0},
            {"buy_ex": "KuCoin", "sell_ex": "HTX", "count": 1, "amount": 10000, "avg_profit": 4.0,
             "fact_count": 0, "avg_fact": None, "fact_rub": None}]
    lines = B.direction_lines(rows)
    assert lines[1].startswith("<b>По направлениям за месяц</b>")
    assert lines[2] == f"• Bybit → MEXC: 3 сд., {B._money(200000)} ₽, расчёт +1.33%, факт +1.00% (у 2) ≈ +1 750 ₽"
    assert lines[3].endswith("расчёт +4.00%, факта нет")
    assert B.direction_lines([]) == []
    many = B.direction_lines([dict(rows[1], buy_ex=f"V{i}") for i in range(10)], limit=8)
    assert many[-1] == "• …ещё 2" and len(many) == 2 + 8 + 1


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def test_stats_command_shows_directions_for_current_month():
    log(trades.DB_PATH, d("Bybit", "MEXC", 2.0), 100000, fact=1.5, ts=None)
    bot = Stub(p2p.Config())
    arun(bot.handle("/stats"))
    text = [p["text"] for m, p in bot.out if m == "sendMessage"][-1]
    assert "По направлениям за месяц" in text and "• Bybit → MEXC: 1 сд." in text and "≈ +1 500 ₽" in text
