"""/export (выгрузка журнала в CSV), /safety («🛡 Безопасность») и «Сухой прогон vs реальные сделки» в /paper report.
Всё без сети: сделки и круги пишутся во временную data/ (tests/conftest.py)."""
import asyncio
import csv
import os
import re
import sqlite3
from datetime import datetime, timezone

import bot as B
import p2p
import paper
import trades
from helpers import make_ad

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=trades.MSK).timestamp()


class Stub(B.Bot):
    """Бот без сети: вызовы Telegram и отправленные файлы пишутся в self.out."""
    def __init__(self, cfg, guests=()):
        super().__init__(None, "x", "1", cfg)
        self.guests = set(guests)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.out)}}

    async def send_document(self, path, caption="", topic=None, chat_id=None):
        self.out.append(("sendDocument", {"path": path, "caption": caption, "chat_id": self.chat_for(chat_id)}))
        return {"ok": True}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def docs(bot):
    return [p for m, p in bot.out if m == "sendDocument"]


def msg(chat, text):
    return {"message": {"chat": {"id": chat}, "text": text, "from": {"first_name": "Вася"}}}


def deal(profit=3.0, buy_ex="Bybit", sell_ex="MEXC", asset="USDT"):
    return (profit, make_ad(buy_ex, "buy", 85.0, asset=asset), make_ad(sell_ex, "sell", 90.0, asset=asset),
            "перевод −0.2 USDT (BEP20) на MEXC")


def log(ts, amount, fact=None, path=None, **kw):
    """Записать сделку в журнал (по умолчанию — в data/ теста) и, если задан, её факт в %."""
    extra = {"path": path} if path else {}
    trade_id, *_ = trades.log_trade(deal(**kw), amount, ts=ts, **extra)
    if fact is not None:
        trades.set_fact(trade_id, fact, **extra)
    return trade_id


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def msk(*args):
    return datetime(*args, tzinfo=trades.MSK).timestamp()


# --- /export: модуль trades ---

def test_period_start_month_and_year_by_msk():
    assert trades.period_start("month", NOW) == msk(2026, 9, 1)
    assert trades.period_start("year", NOW) == msk(2026, 1, 1)
    # 31.12 22:30 UTC — по МСК уже 1 января: новый месяц и новый год
    new_year = datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc).timestamp()
    assert trades.period_start("year", new_year) == msk(2027, 1, 1) == trades.period_start("month", new_year)


def test_export_rows_month_vs_year(tmp_path):
    db = str(tmp_path / "trades.db")
    log(msk(2026, 9, 10, 12, 30), 50000, path=db)          # этот месяц
    log(msk(2026, 3, 5, 9, 0), 30000, path=db)             # этот год, другой месяц
    log(msk(2025, 12, 31, 23, 0), 20000, path=db)          # прошлый год
    month = trades.export_rows(trades.period_start("month", NOW), path=db)
    year = trades.export_rows(trades.period_start("year", NOW), path=db)
    assert [r["amount"] for r in month] == [50000]
    assert [r["amount"] for r in year] == [30000, 50000]    # по времени
    assert trades.export_rows(0, path=str(tmp_path / "none.db")) == []


def test_export_csv_columns_and_rows(tmp_path):
    db, out = str(tmp_path / "trades.db"), str(tmp_path / "export.csv")
    log(msk(2026, 9, 10, 12, 30), 50000, fact=2.0, path=db)
    log(msk(2026, 9, 11, 8, 5), 20000, profit=1.5, buy_ex="HTX", sell_ex="Bybit", path=db)
    rows = trades.export_rows(0, path=db)
    for r in rows:                       # колонки ников могут уже быть в базе — этот тест про выгрузку без них
        r.pop("buy_nick", None)
        r.pop("sell_nick", None)
    trades.write_export_csv(rows, path=out)
    header, first, second = read_csv(out)
    assert header == list(trades.EXPORT_COLUMNS) + ["Маршрут"]
    assert first == ["2026-09-10 12:30", "Bybit", "USDT", "MEXC", "USDT", "50000,00", "3,00", "2,00", "1000,00", "",
                     "Т-Банк", "внутри банка", "перевод −0.2 USDT (BEP20) на MEXC"]   # запятая — числа в русском Excel
    assert second[:9] == ["2026-09-11 08:05", "HTX", "USDT", "Bybit", "USDT", "20000,00", "1,50", "", ""]


def test_export_csv_includes_merchant_nicks_when_columns_exist(tmp_path):
    db, out = str(tmp_path / "trades.db"), str(tmp_path / "export.csv")
    trade_id = log(msk(2026, 9, 10, 12, 30), 50000, path=db)
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
    for col in ("buy_nick", "sell_nick"):
        if col not in cols:              # колонки добавляет другая ветка; здесь — только если их ещё нет
            con.execute(f"ALTER TABLE trades ADD COLUMN {col} TEXT DEFAULT ''")
    con.execute("UPDATE trades SET buy_nick = 'seller1', sell_nick = '=SUM(A1)' WHERE id = ?", (trade_id,))
    con.commit()
    con.close()
    trades.write_export_csv(trades.export_rows(0, path=db), path=out)
    header, row = read_csv(out)
    assert header[-3:] == ["Мерчант покупки", "Мерчант продажи", "Маршрут"]
    assert row[-3:-1] == ["seller1", "'=SUM(A1)"]            # ник-«формула» не исполнится в Excel


def test_export_summary_numbers():
    rows = [{"amount": 50000.0, "fact": 2.0}, {"amount": 30000.0, "fact": -0.5}, {"amount": 20000.0, "fact": None}]
    assert trades.export_summary(rows) == {"count": 3, "amount": 100000.0, "result": 850.0, "no_fact": 1,
                                           "estimated": 0}
    assert trades.export_summary([]) == {"count": 0, "amount": 0, "result": 0, "no_fact": 0, "estimated": 0}


def _log_sourced(ts, amount, fact, source, path=None, **kw):
    """Сделка с фактом и его источником (trades.fact_source): plan / plan± / manual / auto, None — до колонки."""
    extra = {"path": path} if path else {}
    trade_id, *_ = trades.log_trade(deal(**kw), amount, ts=ts, **extra)
    trades.set_fact(trade_id, fact, source=source, **extra)
    return trade_id


def test_export_csv_marks_plan_estimate_not_as_fact(tmp_path):
    """«как расчёт»/«±0.5 п.п.» — оценка: в CSV не в «Факт, %» и не в «Результат по факту, ₽», а в своей колонке."""
    db, out = str(tmp_path / "trades.db"), str(tmp_path / "export.csv")
    _log_sourced(msk(2026, 9, 10, 10, 0), 50000, 3.0, trades.FACT_PLAN, path=db)          # расчёт был 3.0
    _log_sourced(msk(2026, 9, 10, 11, 0), 50000, 2.5, trades.FACT_PLAN_SHIFT, path=db)
    _log_sourced(msk(2026, 9, 10, 12, 0), 40000, 1.5, trades.FACT_MANUAL, path=db)
    _log_sourced(msk(2026, 9, 10, 13, 0), 20000, 2.0, trades.FACT_AUTO, path=db)
    _log_sourced(msk(2026, 9, 10, 14, 0), 10000, 1.0, None, path=db)                      # факт до колонки источника
    log(msk(2026, 9, 10, 15, 0), 10000, path=db)                                          # без факта
    trades.write_export_csv(trades.export_rows(0, path=db), path=out)
    header, *rows = read_csv(out)
    col = {name: header.index(name) for name in ("Факт, %", "Результат по факту, ₽", "Оценка (не факт), %")}
    got = [(r[col["Факт, %"]], r[col["Результат по факту, ₽"]], r[col["Оценка (не факт), %"]]) for r in rows]
    assert got == [("", "", "3,00"), ("", "", "2,50"),
                   ("1,50", "600,00", ""), ("2,00", "400,00", ""), ("1,00", "100,00", ""), ("", "", "")]


def test_export_summary_does_not_count_estimates_as_fact():
    rows = [{"amount": 50000.0, "fact": 2.0, "fact_source": "manual"},
            {"amount": 30000.0, "fact": 3.0, "fact_source": trades.FACT_PLAN},
            {"amount": 20000.0, "fact": 2.5, "fact_source": trades.FACT_PLAN_SHIFT},
            {"amount": 10000.0, "fact": 1.0, "fact_source": None},
            {"amount": 10000.0, "fact": None, "fact_source": None}]
    assert trades.export_summary(rows) == {"count": 5, "amount": 120000.0, "result": 1100.0, "no_fact": 3,
                                           "estimated": 2}
    assert trades.is_estimate(rows[1]) and trades.is_estimate(rows[2])
    assert not any(trades.is_estimate(r) for r in (rows[0], rows[3], rows[4]))
    assert trades.fact_rub(rows[1]) is None and trades.fact_rub(rows[0]) == 1000.0


# --- /export: команда бота ---

def test_export_command_marks_estimates():
    month = trades.period_start("month")
    log(month + 60, 50000, fact=2.0)                                  # введён числом до колонки источника
    _log_sourced(month + 120, 30000, 3.0, trades.FACT_PLAN)           # «как расчёт»
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/export"))
    header, *rows = read_csv(docs(bot)[-1]["path"])
    est = header.index("Оценка (не факт), %")
    assert [r[est] for r in rows] == ["", "3,00"] and rows[1][header.index("Факт, %")] == ""
    text = texts(bot)[-1]
    assert "Результат по факту: +1 000 ₽" in text                      # оценка +900 ₽ в результат не вошла
    assert "Без факта: 1 из 2" in text and "Из них 1 — с оценкой «как расчёт» (не факт)" in text

def test_export_command_sends_csv_then_summary():
    month = trades.period_start("month")
    log(month + 60, 50000, fact=2.0)
    log(month + 120, 30000, fact=-0.5)
    log(month + 180, 20000)
    log(trades.period_start("year") - 86400, 90000, fact=5.0)   # прошлый год — не в выгрузке
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/export"))
    methods = [m for m, _ in bot.out]
    assert methods == ["sendDocument", "sendMessage"]            # сначала файл, потом сводка
    doc = docs(bot)[0]
    assert doc["path"] == trades.EXPORT_CSV_PATH and doc["chat_id"] == "1"
    assert len(read_csv(doc["path"])) == 1 + 3
    text = texts(bot)[-1]
    assert "Сделок: 3, оборот 100 000 ₽" in text
    assert "Результат по факту: +850 ₽" in text
    assert "Без факта: 1 из 3" in text
    assert B.EXPORT_NOTE in text and "не налоговая консультация" in text


def test_export_year_includes_earlier_months():
    month, year = trades.period_start("month"), trades.period_start("year")
    log(month + 60, 50000, fact=1.0)
    log(month - 86400, 40000, fact=1.0)                         # прошлый месяц (в январе — уже прошлый год)
    log(year - 86400, 90000, fact=1.0)                          # прошлый год
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/export year"))
    expected = 2 if month > year else 1
    assert len(read_csv(docs(bot)[-1]["path"])) == 1 + expected
    assert f"Сделок: {expected}," in texts(bot)[-1] and "Факт указан у всех сделок." in texts(bot)[-1]
    assert "с 01.01." in texts(bot)[-1]


def test_export_empty_period_and_bad_arg():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/export"))
    assert "сделок в журнале нет" in texts(bot)[-1] and not docs(bot)
    asyncio.run(bot.handle("/export week"))
    assert texts(bot)[-1] == B.EXPORT_HELP and not docs(bot)


def test_export_denied_to_guest():
    log(trades.period_start("month") + 60, 50000, fact=2.0)
    bot = Stub(p2p.Config(), guests=["42"])
    for cmd in ("/export", "/export year"):
        asyncio.run(bot.on_update(msg(42, cmd)))
        last = [p for m, p in bot.out if m == "sendMessage"][-1]
        assert last["chat_id"] == "42" and last["text"] == B.GUEST_DENIED, cmd
    assert not docs(bot) and "/export" not in B.GUEST_CMDS


# --- /safety ---

SAFETY_MARKERS = ("115-ФЗ", "TXID", "161-ФЗ", "ФинЦЕРТ", "обжалуй", "ОД-2506", "01.01.2026", "200 000 ₽", "СБП",
                  "187 УК РФ", "05.07.2025", "своих карт", "третьих лиц", "деньги реально пришли", "282-ФЗ",
                  "30.06.2027", "01.07.2027", "реестра Банка России")


def test_safety_text_markers_and_length():
    for marker in SAFETY_MARKERS:
        assert marker in B.SAFETY, marker
    assert len(re.sub(r"<[^>]+>", "", B.SAFETY)) <= 1500


def test_safety_for_owner_by_command_and_menu_button():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/safety"))
    assert texts(bot)[-1] == B.SAFETY
    asyncio.run(bot.handle("🛡 Безопасность"))
    assert texts(bot)[-1] == B.SAFETY
    menu = [b["text"] for row in B.MENU["keyboard"] for b in row]
    assert "🛡 Безопасность" in menu and B.BUTTONS["🛡 Безопасность"] == "/safety"
    assert any(c["command"] == "safety" for c in B.COMMANDS) and any(c["command"] == "export" for c in B.COMMANDS)


def test_safety_for_guest():
    bot = Stub(p2p.Config(), guests=["42"])
    for text in ("/safety", "🛡 Безопасность"):
        asyncio.run(bot.on_update(msg(42, text)))
        last = [p for m, p in bot.out if m == "sendMessage"][-1]
        assert last["chat_id"] == "42" and last["text"] == B.SAFETY, text
    assert "/safety" in B.GUEST_CMDS and "/safety" in B.GUEST_DENIED
    assert "🛡 Безопасность" in [b["text"] for row in B.GUEST_MENU["keyboard"] for b in row]


# --- сухой прогон vs реальные сделки ---

def cycle(ts, realized=None, buy_ex="Bybit", sell_ex="MEXC", result="done"):
    cid = paper.start_cycle(10000, make_ad(buy_ex, "buy", 85.0), make_ad(sell_ex, "sell", 90.0), "route", 2.0, ts=ts)
    paper.finish_cycle(cid, result, realized_pct=realized or 0.0, ts=ts + 600)
    return cid


def test_first_start_and_facts_by_pair(tmp_path):
    db, tdb, t0 = str(tmp_path / "paper.db"), str(tmp_path / "trades.db"), msk(2026, 9, 20, 10, 0)
    assert paper.first_start(path=db) is None and trades.facts_by_pair(path=tdb) == {}
    for ts in (t0 + 1000, t0):
        paper.start_cycle(10000, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "r", 2.0, path=db, ts=ts)
    assert paper.first_start(path=db) == t0
    log(t0 - 500, 10000, fact=9.0, path=tdb)                # раньше окна
    log(t0 + 500, 10000, fact=1.0, path=tdb)
    log(t0 + 600, 10000, fact=2.0, path=tdb)
    log(t0 + 700, 10000, path=tdb)                          # без факта
    log(t0 + 800, 10000, fact=4.0, buy_ex="HTX", path=tdb)
    got = trades.facts_by_pair(t0, path=tdb)
    assert got == {("Bybit", "USDT", "MEXC", "USDT"): {"count": 2, "avg_fact": 1.5},
                   ("HTX", "USDT", "MEXC", "USDT"): {"count": 1, "avg_fact": 4.0}}


def test_paper_report_compares_with_real_trades():
    t0 = msk(2026, 9, 20, 10, 0)
    cycle(t0, realized=2.5)
    cycle(t0 + 3600, realized=1.5)
    cycle(t0 + 7200, buy_ex="HTX", result="failed_buy")   # по HTX→MEXC ни один круг не исполнился
    log(t0 - 86400, 50000, fact=9.0)                        # до первого круга — не в сравнении
    log(t0 + 100, 50000, fact=1.0)
    log(t0 + 200, 50000, fact=2.0)
    log(t0 + 300, 50000)                                    # без факта — не в сравнении
    log(t0 + 400, 50000, fact=0.5, buy_ex="HTX")
    log(t0 + 500, 50000, fact=3.0, buy_ex="KuCoin")         # связки нет в прогоне — не в сравнении
    text = Stub(p2p.Config()).paper_report_view(paper.report_rows())
    assert "<b>Сухой прогон vs реальные сделки</b> (с 20.09.2026):" in text
    assert ("Bybit→MEXC (USDT→USDT): прогон +2.00% (2 кругов) / реальные +1.50% (2 сделок), "
            "разница -0.50 п.п.") in text
    assert "HTX→MEXC (USDT→USDT): прогон — не исполнилось ни одного из 1 кругов / реальные +0.50% (1 сделок)" in text
    assert "KuCoin" not in text


def test_paper_report_without_real_facts_is_one_line():
    t0 = msk(2026, 9, 20, 10, 0)
    cycle(t0, realized=2.5)
    log(t0 + 100, 50000)                                    # сделка есть, но без факта
    text = Stub(p2p.Config()).paper_report_view(paper.report_rows())
    lines = [ln for ln in text.splitlines() if "Сухой прогон vs реальные сделки" in ln]
    assert lines == ["<b>Сухой прогон vs реальные сделки</b> (с 20.09.2026): реальных сделок с фактом за это время нет."]
    assert "реальные +" not in text


def test_paper_report_real_trades_only_on_other_pairs():
    t0 = msk(2026, 9, 20, 10, 0)
    cycle(t0, realized=2.5)
    log(t0 + 100, 50000, fact=1.0, buy_ex="KuCoin")
    text = Stub(p2p.Config()).paper_report_view(paper.report_rows())
    assert "реальные сделки с фактом (1) были по другим связкам." in text


def test_paper_report_command_still_owner_only():
    bot = Stub(p2p.Config(), guests=["42"])
    asyncio.run(bot.on_update(msg(42, "/paper report")))
    assert texts(bot)[-1] == B.GUEST_DENIED and not docs(bot)
    assert "paper_report" not in B.GUEST_CALLBACKS
