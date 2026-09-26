"""Сухой прогон — поля разбора (этап 1 «измерения»): время каждой стадии, индекс/причины/серия, мерчанты, запас
глубины и id снимка на старте, цена и объём на проверке покупки; миграция старой базы, отчёты и CSV."""
import asyncio
import csv
import json
import sqlite3
import time

import bot as B
import p2p
import paper
import snapshots
from helpers import make_ad


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True}

    async def send_photo(self, png, caption, markup=None):
        self.out.append(("sendPhoto", {"caption": caption}))
        return {"ok": True}


def _ads():
    return make_ad("Bybit", "buy", 85.0, orders=300, rate=98.5), make_ad("MEXC", "sell", 90.0, orders=150, rate=97.0)


def _cols(db):
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(cycles)")]
    con.close()
    return cols


def test_new_and_migrated_db_have_same_column_order(tmp_path):
    new = str(tmp_path / "new.db")
    paper._connect(new).close()
    assert tuple(_cols(new)) == paper._COLUMNS
    old = str(tmp_path / "old.db")
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, "
                "buy_ex TEXT, buy_asset TEXT, buy_price REAL, buy_nick TEXT, sell_ex TEXT, sell_asset TEXT, "
                "sell_price REAL, sell_nick TEXT, route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, "
                "realized_pct REAL DEFAULT NULL, result TEXT DEFAULT NULL, note TEXT DEFAULT '')")
    con.execute("INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, buy_price, buy_nick, sell_ex, sell_asset, "
                "sell_price, sell_nick, route, planned_pct, stage, ts_stage, realized_pct, result) VALUES "
                "(?, 10000, 'Bybit', 'USDT', 87, 'm', 'MEXC', 'USDT', 90, 'k', 'r', 3.0, 'sell', ?, 2.5, 'done')",
                (1000.0, 1600.0))
    con.commit()
    con.close()
    c = paper.get_cycle(1, path=old)
    assert tuple(_cols(old)) == paper._COLUMNS
    assert all(c[k] is None for k in ("ts_buy_done", "ts_sell_done", "index_start", "reasons_start", "streak_start",
                                      "buy_orders", "depth_margin", "buy_check_price", "snapshot_id"))
    (row,) = paper.report_rows(path=old)                                  # старые круги: поля разбора — None
    assert row["avg_buy_min"] is None and row["avg_index_start"] is None and row["avg_duration_min"] == 10.0
    (g,) = paper.label_stats(path=old).values()
    assert g["avg_depth_margin"] is None and g["failed_by_reason"] == {}


def test_start_cycle_stores_start_measures():
    buy, sell = _ads()
    cid = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=1000.0, index=7, reasons=["мерчант у порога"],
                            streak=4, depth=3.25, snapshot_id=1000123)
    c = paper.get_cycle(cid)
    assert c["index_start"] == 7 and json.loads(c["reasons_start"]) == ["мерчант у порога"]
    assert c["streak_start"] == 4 and c["depth_margin"] == 3.25 and c["snapshot_id"] == 1000123
    assert (c["buy_orders"], c["buy_rate"], c["sell_orders"], c["sell_rate"]) == (300, 98.5, 150, 97.0)
    plain = paper.get_cycle(paper.start_cycle(10000, buy, sell, "r", 2.0))   # старый вызов без полей разбора
    assert plain["index_start"] is None and plain["reasons_start"] is None and plain["buy_orders"] == 300


def test_stage_timestamps_on_advance_and_finish():
    buy, sell = _ads()
    cid = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=1000.0)
    paper.set_stage(cid, "transfer", ts=1300.0)
    paper.set_stage(cid, "sell", ts=1500.0)
    paper.finish_cycle(cid, "done", 1.5, ts=1560.0)
    c = paper.get_cycle(cid)
    assert (c["ts_buy_done"], c["ts_transfer_done"], c["ts_sell_done"]) == (1300.0, 1500.0, 1560.0)
    failed = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=2000.0)
    paper.finish_cycle(failed, "failed_buy", ts=2400.0)                  # сорвался на покупке — её время окончания
    f = paper.get_cycle(failed)
    assert f["ts_buy_done"] == 2400.0 and f["ts_transfer_done"] is None and f["ts_sell_done"] is None
    again = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=3000.0)
    paper.set_stage(again, "transfer", ts=3100.0)
    paper.set_stage(again, "transfer", ts=3200.0)                         # повтор не перезаписывает отметку
    assert paper.get_cycle(again)["ts_buy_done"] == 3100.0


def test_depth_margin_min_of_buy_and_sell_cover():
    b, s = make_ad("Bybit", "buy", 100.0, max_amt=30000, avail=200), make_ad("MEXC", "sell", 110.0, max_amt=5500, avail=80)
    b2 = make_ad("Bybit", "buy", 101.0, max_amt=50000, avail=100)          # покрывает 10 100 ₽ (объём монеты)
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {},
                        groups={("Bybit", "buy", "USDT"): [b, b2], ("MEXC", "sell", "USDT"): [s]})
    # покупка: 20 000 + 10 100 = 30 100 ₽ на 10 000 → 3.01; продажа: min(80, 5500/110=50) = 50 монет на 100 → 0.5
    assert paper.depth_margin(snap, b, s, 10000, 100) == 0.5
    assert paper.depth_margin(snap, b, s, 10000, 10) == 3.01
    empty = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, groups={("Bybit", "buy", "USDT"): [b]})
    assert paper.depth_margin(empty, b, s, 10000, 100) is None
    assert paper.depth_margin(snap, b, s, 10000, None) is None


def test_depth_margin_exchanger_same_net_only():
    b = make_ad("Bybit", "buy", 100.0, max_amt=20000, avail=200)
    trc = make_ad("BestChange", "sell", 110.0, net="TRC20", max_amt=11000, avail=100)
    bep = make_ad("BestChange", "sell", 110.0, net="BEP20", max_amt=110000, avail=1000)
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {},
                        groups={("Bybit", "buy", "USDT"): [b], ("BestChange", "sell", "USDT"): [trc, bep]})
    assert paper.depth_margin(snap, b, trc, 10000, 100) == 1.0             # только TRC20: 100 монет на 100


def test_buy_observed_and_recorded():
    buy, sell = _ads()
    cid = paper.start_cycle(10000, buy, sell, "r", 2.0)
    c = paper.get_cycle(cid)
    now_ad = make_ad("Bybit", "buy", 85.5, max_amt=8000, avail=200)       # тот же мерчант "nick", цена сдвинулась
    other = make_ad("Bybit", "buy", 84.0)
    other.nick = "someone"
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, groups={("Bybit", "buy", "USDT"): [other, now_ad]})
    assert paper.buy_observed(c, snap) == (85.5, 8000)
    gone = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, groups={("Bybit", "buy", "USDT"): [other]})
    assert paper.buy_observed(c, gone) == (None, 0.0)
    paper.set_buy_check(cid, 85.5, 8000)
    c = paper.get_cycle(cid)
    assert c["buy_check_price"] == 85.5 and c["buy_check_avail"] == 8000


def test_report_rows_label_stats_and_csv_with_measures(tmp_path):
    buy, sell = _ads()
    a = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=0.0, label="✅", index=10, streak=2, depth=4.0)
    paper.set_buy_check(a, 86.7, 20000)                                   # цена +2% к плану, объём 2× суммы
    paper.set_stage(a, "transfer", ts=300.0)
    paper.set_stage(a, "sell", ts=480.0)
    paper.finish_cycle(a, "done", 1.5, ts=600.0)
    b = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=0.0, label="✅", index=8, streak=4, depth=2.0)
    paper.set_buy_check(b, None, 0.0)                                     # мерчант ушёл
    paper.finish_cycle(b, "failed_buy", ts=420.0)
    (row,) = paper.report_rows()
    assert row["avg_buy_min"] == 6.0                                      # (5 + 7) / 2
    assert row["avg_transfer_min"] == 3.0 and row["avg_sell_min"] == 2.0
    assert row["avg_index_start"] == 9.0 and row["avg_streak_start"] == 3.0 and row["avg_depth_margin"] == 3.0
    assert row["avg_buy_orders"] == 300 and row["avg_sell_rate"] == 97.0
    assert abs(row["avg_buy_check_drift_pct"] - 2.0) < 1e-9                # у ушедшего мерчанта цены нет
    assert row["avg_buy_check_cover"] == 1.0                               # (2.0 + 0.0) / 2
    g = paper.label_stats()["✅"]
    assert g["total"] == 2 and g["failed_by_reason"] == {"failed_buy": 1} and g["avg_index_start"] == 9.0
    path = paper.write_report_csv(paper.report_rows(), path=str(tmp_path / "r.csv"))
    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == paper.REPORT_COLUMNS and rows[0][-1] == "failed_by_reason"
    rec = dict(zip(rows[0], rows[1]))
    assert float(rec["avg_buy_min"]) == 6.0 and rec["failed_by_reason"] == "покупка:1"
    old_style = [{k: row[k] for k in paper.REPORT_COLUMNS[:11]} | {"failed_by_reason": {}}]
    paper.write_report_csv(old_style, path=str(tmp_path / "old.csv"))    # строка без полей разбора — пустые ячейки


# --- бот: запись полей на старте и на проверке покупки ---

def _deal(profit=5.0):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def _snap(deals, ts=0.0):
    b, s = deals[0][1], deals[0][2]
    return p2p.Snapshot(88.0, "test", {}, {}, deals, {}, {}, {}, ts=ts,
                        groups={(b.ex, "buy", b.asset): [b], (s.ex, "sell", s.asset): [s]})


def test_bot_paper_start_records_measures(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    bot = Stub(p2p.Config(min_profit=2.0))
    ds = [_deal()]
    snap = _snap(ds, ts=time.time())
    for _ in range(bot.live_scans):
        bot.track_liveness(snap)                                          # связка держится N сканов
    asyncio.run(bot.notify(snap))
    (c,) = paper.open_cycles()
    d = p2p.deal_for_amount(ds[0], bot.cfg, snap, 10000)
    label, reasons = p2p.reliability(d, bot.cfg, snap)
    assert c["label"] == label and json.loads(c["reasons_start"]) == reasons
    assert c["index_start"] == p2p.reliability_index(d, bot.cfg, snap)
    assert c["streak_start"] == bot.live_scans and c["snapshot_id"] == snapshots.scan_id(snap)
    assert c["depth_margin"] and c["depth_margin"] > 1
    assert c["buy_orders"] == 200 and c["sell_rate"] == 100.0


def test_bot_buy_check_recorded_on_advance_and_fail(monkeypatch):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "5")
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    ok = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(_snap([_deal()])))
    c = paper.get_cycle(ok)
    assert c["stage"] == "transfer" and c["buy_check_price"] == 85.0 and c["buy_check_avail"] == 500000
    assert c["ts_buy_done"] is not None
    gone = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=time.time() - 400)
    s = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, groups={("Bybit", "buy", "USDT"): []})
    asyncio.run(bot.process_paper_cycles(s))
    g = paper.get_cycle(gone)
    assert g["result"] == "failed_buy" and g["buy_check_price"] is None and g["buy_check_avail"] == 0.0
    early = paper.start_cycle(10000, buy, sell, "r", 2.0, ts=time.time())
    asyncio.run(bot.process_paper_cycles(_snap([_deal()])))
    assert paper.get_cycle(early)["buy_check_avail"] is None               # рано — проверки не было
