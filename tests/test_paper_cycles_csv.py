"""/paper cycles [дней]: построчная выгрузка кругов сухого прогона в CSV (paper.export_cycles/write_cycles_csv)
и сама команда в bot.py."""
import datetime
import functools
import os
import time

import bot as B
import p2p
import paper
from helpers import arun, make_ad

MSK = paper.MSK


def _cycle(db, ts_start, planned_pct=2.0, planned_raw=None, route="Bybit→MEXC", label="", pays=("T-Bank",)):
    buy, sell = make_ad("Bybit", "buy", 85.0, pays=pays), make_ad("MEXC", "sell", 90.0)
    return paper.start_cycle(10000, buy, sell, route, planned_pct, path=db, ts=ts_start, label=label,
                             planned_raw=planned_raw)


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True}

    async def send_document(self, path, caption="", topic=None, chat_id=None):
        self.out.append(("sendDocument", {"path": path, "caption": caption}))
        return {"ok": True}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def _patch(monkeypatch, db, csv_path):
    monkeypatch.setattr(B.paper, "export_cycles", functools.partial(B.paper.export_cycles, path=db))
    monkeypatch.setattr(B.paper, "write_cycles_csv", functools.partial(B.paper.write_cycles_csv, path=csv_path))


# --- paper.export_cycles / write_cycles_csv --------------------------------------------------------------------

def test_header_matches_columns(tmp_path):
    db, csv_path = str(tmp_path / "paper.db"), str(tmp_path / "cycles.csv")
    _cycle(db, 1_790_000_000.0)
    out = paper.write_cycles_csv(paper.export_cycles(path=db), path=csv_path)
    assert out == csv_path
    with open(csv_path, encoding="utf-8-sig") as f:
        header = f.readline().rstrip("\r\n")
    assert header == ";".join(paper.CYCLES_COLUMNS)
    raw = open(csv_path, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")


def test_rows_sorted_by_start_and_counted(tmp_path):
    db = str(tmp_path / "paper.db")
    _cycle(db, 1_790_000_300.0)
    _cycle(db, 1_790_000_100.0)
    _cycle(db, 1_790_000_200.0)
    rows = paper.export_cycles(path=db)
    assert len(rows) == 3
    starts = [datetime.datetime.strptime(r["Старт (МСК)"], "%d.%m.%Y %H:%M") for r in rows]
    assert starts == sorted(starts)


def test_msk_hour_and_start(tmp_path):
    db = str(tmp_path / "paper.db")
    ts = datetime.datetime(2026, 9, 21, 22, 30, tzinfo=datetime.timezone.utc).timestamp()   # 01:30 МСК след. суток
    _cycle(db, ts)
    row = paper.export_cycles(path=db)[0]
    assert row["Час (МСК)"] == 1
    assert row["Старт (МСК)"] == "22.09.2026 01:30"


def test_done_cycle_diff_uses_planned_raw(tmp_path):
    db = str(tmp_path / "paper.db")
    cid = _cycle(db, 1_790_000_000.0, planned_pct=1.0, planned_raw=1.5)
    paper.finish_cycle(cid, "done", realized_pct=1.2, path=db)
    row = paper.export_cycles(path=db)[0]
    assert row["Факт, %"] == 1.2
    assert abs(row["Факт-план, п.п."] - (-0.3)) < 1e-9
    assert row["Итог"] == "исполнен"

    db2 = str(tmp_path / "paper2.db")
    cid2 = _cycle(db2, 1_790_000_000.0, planned_pct=1.0, planned_raw=None)
    paper.finish_cycle(cid2, "done", realized_pct=0.4, path=db2)
    row2 = paper.export_cycles(path=db2)[0]
    assert abs(row2["Факт-план, п.п."] - (0.4 - 1.0)) < 1e-9   # fallback на planned_pct


def test_failed_cycle_blank_realized_and_reason(tmp_path):
    db = str(tmp_path / "paper.db")
    cid = _cycle(db, 1_790_000_000.0)
    paper.finish_cycle(cid, "failed_transfer", note="перевод закрыт", path=db)
    row = paper.export_cycles(path=db)[0]
    assert row["Факт, %"] is None and row["Факт-план, п.п."] is None
    assert row["Итог"] == "срыв: перевод"
    assert row["Причина срыва"] == paper.REASON_LABELS[paper.fail_reason("перевод закрыт")]


def test_open_cycle_marked_open(tmp_path):
    db = str(tmp_path / "paper.db")
    cid = _cycle(db, 1_790_000_000.0)
    row = paper.export_cycles(path=db)[0]
    assert row["Итог"] == "открыт"
    for col in ("Покупка, мин", "Перевод факт, мин", "Продажа, мин", "Факт, %", "Факт-план, п.п."):
        assert row[col] is None
    paper.set_stage(cid, "transfer", path=db, ts=1_790_000_300.0)
    row2 = paper.export_cycles(path=db)[0]
    assert row2["Покупка, мин"] is not None and abs(row2["Покупка, мин"] - 5.0) < 1e-9
    assert row2["Итог"] == "открыт"


def test_stage_durations_minutes(tmp_path):
    db = str(tmp_path / "paper.db")
    t0 = 1_790_000_000.0
    cid = _cycle(db, t0)
    paper.set_stage(cid, "transfer", path=db, ts=t0 + 120)
    paper.set_stage(cid, "sell", path=db, ts=t0 + 300)
    paper.finish_cycle(cid, "done", realized_pct=2.0, path=db, ts=t0 + 480)
    row = paper.export_cycles(path=db)[0]
    assert abs(row["Покупка, мин"] - 2.0) < 1e-9
    assert abs(row["Перевод факт, мин"] - 3.0) < 1e-9
    assert abs(row["Продажа, мин"] - 3.0) < 1e-9


def test_text_fields_escaped(tmp_path):
    db, csv_path = str(tmp_path / "paper.db"), str(tmp_path / "cycles.csv")
    cid = _cycle(db, 1_790_000_000.0, label="=SUM(A1)")
    paper.finish_cycle(cid, "failed_buy", note="+cmd calc()", path=db)
    rows = paper.export_cycles(path=db)
    assert rows[0]["Метка"] == "=SUM(A1)" and rows[0]["Примечание"] == "+cmd calc()"
    paper.write_cycles_csv(rows, path=csv_path)
    text = open(csv_path, encoding="utf-8-sig").read()
    assert "'=SUM(A1)" in text and "'+cmd calc()" in text


def test_decimal_comma(tmp_path):
    db, csv_path = str(tmp_path / "paper.db"), str(tmp_path / "cycles.csv")
    cid = _cycle(db, 1_790_000_000.0, planned_pct=1.5)
    paper.finish_cycle(cid, "done", realized_pct=1.5, path=db)
    rows = paper.export_cycles(path=db)
    paper.write_cycles_csv(rows, path=csv_path)
    import csv as _csv
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f, delimiter=";")
        row = next(reader)
    for col in paper._CYCLES_NUM_COLUMNS:
        assert "." not in row[col]
    assert row["План с запасом, %"] == "1,50" and row["Факт, %"] == "1,50"


def test_since_filters_old_cycles(tmp_path):
    db = str(tmp_path / "paper.db")
    _cycle(db, 1_790_000_000.0)
    _cycle(db, 1_790_100_000.0)
    rows = paper.export_cycles(since=1_790_050_000.0, path=db)
    assert len(rows) == 1


def test_missing_or_empty_db_returns_empty(tmp_path):
    missing = str(tmp_path / "none.db")
    assert paper.export_cycles(path=missing) == []
    assert not os.path.exists(missing)
    db = str(tmp_path / "paper.db")
    paper._connect(db).close()
    assert paper.export_cycles(path=db) == []


# --- bot.cmd_paper("cycles") ---------------------------------------------------------------------------------

def test_cmd_paper_cycles_sends_document(monkeypatch, tmp_path):
    db, csv_path = str(tmp_path / "paper.db"), str(tmp_path / "cycles.csv")
    _patch(monkeypatch, db, csv_path)
    _cycle(db, time.time() - 10)   # свежий круг — попадает и в окно 30, и в окно 7 дней
    bot = Stub(p2p.Config())
    arun(bot.cmd_paper("cycles"))
    docs = [m for m in bot.out if m[0] == "sendDocument"]
    assert len(docs) == 1 and docs[0][1]["caption"] == "Круги бумаги: 1 шт. за 30 дн."
    assert os.path.exists(csv_path)

    bot2 = Stub(p2p.Config())
    arun(bot2.cmd_paper("cycles 7"))
    docs2 = [m for m in bot2.out if m[0] == "sendDocument"]
    assert docs2[0][1]["caption"] == "Круги бумаги: 1 шт. за 7 дн."


def test_cmd_paper_cycles_empty_sends_text_only(monkeypatch, tmp_path):
    db, csv_path = str(tmp_path / "paper.db"), str(tmp_path / "cycles.csv")
    _patch(monkeypatch, db, csv_path)
    bot = Stub(p2p.Config())
    arun(bot.cmd_paper("cycles"))
    assert "за период кругов нет" in texts(bot)[-1]
    assert not [m for m in bot.out if m[0] == "sendDocument"]


def test_cmd_paper_cycles_days_clamp_and_garbage(monkeypatch, tmp_path):
    db, csv_path = str(tmp_path / "paper.db"), str(tmp_path / "cycles.csv")
    _patch(monkeypatch, db, csv_path)
    _cycle(db, time.time() - 10)   # свежий круг — попадает в любое окно, даже самое узкое (1 дн.)

    def caption(arg):
        b = Stub(p2p.Config())
        arun(b.cmd_paper(arg))
        docs = [m for m in b.out if m[0] == "sendDocument"]
        return docs[0][1]["caption"] if docs else None

    assert caption("cycles 0") == "Круги бумаги: 1 шт. за 1 дн."
    assert caption("cycles 9999") == "Круги бумаги: 1 шт. за 365 дн."
    assert caption("cycles abc") == "Круги бумаги: 1 шт. за 30 дн."
    assert caption("cycles -5") == "Круги бумаги: 1 шт. за 30 дн."
