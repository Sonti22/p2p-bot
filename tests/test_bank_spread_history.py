"""История спреда связок по банкам оплаты (history.bank_spreads) и /banks без монеты — какие банки выгоднее за 7 дней."""
import functools
import sqlite3

import bot as B
import history
import p2p
from helpers import arun, make_ad

BASE = 1_790_000_000.0


def snap(deals):
    return p2p.Snapshot(88.0, "test", {}, {}, deals, {}, {}, {})


def record_at(monkeypatch, db, ts, deals):
    history._last["t"] = 0.0
    monkeypatch.setattr(history.time, "time", lambda: ts)
    assert history.record(snap(deals), 50000, path=db) is True


def rows(db):
    con = sqlite3.connect(db)
    out = con.execute("SELECT ts, side, bank, profit FROM bank_spreads ORDER BY side, bank").fetchall()
    con.close()
    return out


def test_ad_banks_canonical_names_sbp_and_no_duplicates():
    ad = make_ad(pays=("Tinkoff", "Т-Банк", "SBP", "Sberbank", "Mobile top-up"))
    assert history.ad_banks(ad) == ["T-Bank", "SBP", "Sberbank"]


def test_record_writes_best_profit_per_bank_and_side(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    deals = [
        (3.0, make_ad("Bybit", "buy", 85.0, pays=("Tinkoff", "SBP")), make_ad("MEXC", "sell", 90.0, pays=("Sberbank",)),
         "r1"),
        (1.0, make_ad("HTX", "buy", 86.0, pays=("Tinkoff",)), make_ad("MEXC", "sell", 89.0, pays=("Tinkoff",)), "r2"),
        (-0.5, make_ad("HTX", "buy", 87.0, pays=("Alfa-bank",)), make_ad("KuCoin", "sell", 88.0, pays=("Cash",)), "r3"),
    ]
    record_at(monkeypatch, db, BASE, deals)
    assert rows(db) == [(BASE, "buy", "Alfa-bank", -0.5), (BASE, "buy", "SBP", 3.0), (BASE, "buy", "T-Bank", 3.0),
                        (BASE, "sell", "Sberbank", 3.0), (BASE, "sell", "T-Bank", 1.0)]


def test_bank_spread_stats_averages_window_and_sorts(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    t_buy = lambda p, bank: (p, make_ad("Bybit", "buy", 85.0, pays=(bank,)), make_ad("MEXC", "sell", 90.0, pays=()), "r")
    record_at(monkeypatch, db, BASE - 10 * 86400, [t_buy(9.0, "Alfa-bank")])       # старше 7 дней — не в окне
    record_at(monkeypatch, db, BASE - 3600, [t_buy(2.0, "Tinkoff"), t_buy(-1.0, "Alfa-bank")])
    record_at(monkeypatch, db, BASE, [t_buy(1.0, "Tinkoff"), t_buy(0.5, "Alfa-bank")])
    stats = history.bank_spread_stats(7, path=db, now=BASE)
    assert list(stats) == ["buy"]
    (b1, n1, avg1, best1, pos1), (b2, n2, avg2, best2, pos2) = stats["buy"]
    assert (b1, n1, best1, pos1) == ("T-Bank", 2, 2.0, 1.0) and abs(avg1 - 1.5) < 1e-9
    assert (b2, n2, best2, pos2) == ("Alfa-bank", 2, 0.5, 0.5) and abs(avg2 + 0.25) < 1e-9
    assert history.bank_spread_stats(7, path=str(tmp_path / "none.db")) == {}


def test_bank_history_view_lists_both_sides_with_names():
    stats = {"buy": [("T-Bank", 12, 1.5, 2.0, 1.0), ("SBP", 3, 0.2, 0.9, 2 / 3)],
             "sell": [("Sberbank", 5, -0.1, 0.4, 0.2)]}
    text = B.bank_history_view(7, stats=stats)
    assert "Спред связок по банкам за 7 дн." in text
    assert text.index("Платим мерчанту") < text.index("Т-Банк") < text.index("СБП (банк не указан)") \
        < text.index("Получаем") < text.index("Сбер")
    assert "• Т-Банк: в среднем +1.50%, лучшая +2.00%, в плюсе 100% срезов (12)" in text
    assert "в плюсе 67% срезов (3)" in text


def test_bank_history_view_empty_and_row_limit():
    assert "Истории ещё нет" in B.bank_history_view(7, stats={})
    many = {"buy": [(f"Bank{i}", 1, 1.0 - i / 10, 1.0, 1.0) for i in range(11)]}
    text = B.bank_history_view(7, stats=many)
    assert "Bank7" in text and "Bank8" not in text and "…ещё 3" in text


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def test_banks_command_without_coin_shows_history(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    record_at(monkeypatch, db, BASE, [(1.2, make_ad("Bybit", "buy", 85.0, pays=("Tinkoff",)),
                                       make_ad("MEXC", "sell", 90.0, pays=()), "r")])
    monkeypatch.setattr(B.history, "bank_spread_stats",
                        functools.partial(history.bank_spread_stats, path=db, now=BASE))
    bot = Stub(p2p.Config())
    arun(bot.banks(""))
    text = [p["text"] for m, p in bot.out if m == "sendMessage"][-1]
    assert "Т-Банк: в среднем +1.20%" in text
