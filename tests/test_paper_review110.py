"""Разбор #110 (2026-09-26): комиссия СБП в круге — как в плане (лимит, исчерпанный реальными сделками), план без
запаса на курс (planned_raw) для сравнения с фактом, /paper reset — база в архив, только владельцу. Что межмонетные
связки снова не берутся в прогон — test_spot_routes_are_not_taken_into_dry_run в test_paper_accuracy.py."""
import asyncio
import dataclasses
import datetime
import os
import sqlite3
import time

import bot as B
import p2p
import paper
import trades
from test_guests import Stub, buttons, msg, sent


def ad(ex, side, price, nick=None, asset="USDT", avail=10000):
    return p2p.Ad(ex, side, price, 1000, 500000, avail, ["SBP"], nick or f"{ex}-{side}", 1000, 100.0, "", asset, "", "")


def start(monkeypatch, deal, sn):
    """Бот заводит круг сухого прогона по связке deal из снимка sn (PAPER_AMOUNT 10 000)."""
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    monkeypatch.setenv("PAPER_TRAPS", "1")   # тест не про метку надёжности
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.maybe_start_paper_cycle([deal], sn))
    return bot


def usdt_deal(over):
    b, s = ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 91.0, "k")
    deal = (3.9, b, s, "r")
    sn = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [deal], {}, {}, {},
                      groups={("HTX", "buy", "USDT"): [b], ("KuCoin", "sell", "USDT"): [s]}, over_banks=frozenset(over))
    return deal, sn


def test_real_sbp_over_limit_fee_in_cycle_matches_plan(monkeypatch):
    """Лимит СБП Т-Банка исчерпан реальными сделками (snap.over_banks), не виртуальным оборотом: план и sell_qty —
    с комиссией 0,5%, и pay_fee_used круга — те же 0,5%, а не 0."""
    monkeypatch.setenv("OWN_BANKS", "T-Bank")
    deal, sn = usdt_deal({"T-Bank"})
    start(monkeypatch, deal, sn)
    (c,) = paper.open_cycles()
    free = p2p.deal_for_amount(deal, p2p.Config(min_profit=2.0), dataclasses.replace(sn, over_banks=frozenset()), 10000)
    assert c["planned_pct"] < free[0] - 0.4                          # план — с комиссией
    assert c["pay_kind"] == "sbp" and c["bank"] == "T-Bank"
    assert c["pay_fee_used"] == trades.SBP_OVER_FEE


def test_pay_bank_is_the_one_the_plan_used(monkeypatch):
    """Т-Банк за лимитом по реальным сделкам, Сбер свободен: план считал оплату со Сбера без комиссии — и круг
    пишет Сбер (виртуальный оборот лимита — на него), а не Т-Банк."""
    monkeypatch.setenv("OWN_BANKS", "T-Bank,Sberbank")
    deal, sn = usdt_deal({"T-Bank"})
    start(monkeypatch, deal, sn)
    (c,) = paper.open_cycles()
    assert c["bank"] == "Sberbank" and c["pay_fee_used"] == 0.0
    assert paper.bank_month_total("Sberbank") == 10000 and paper.bank_month_total("T-Bank") == 0


def test_fact_is_compared_with_plan_without_risk_buffer(monkeypatch):
    """BTC→BTC внутри Bybit: в плане запас на курс 0,3%, в факте его нет. Стакан не изменился — факт равен плану
    без запаса, «факт vs план» ≈ 0, а не +0,3 п.п. Владельцу по-прежнему показан план с запасом."""
    b = ad("Bybit", "buy", 6_000_000.0, "m", asset="BTC")
    s = ad("Bybit", "sell", 6_240_000.0, "k", asset="BTC", avail=1.0)
    deal = (3.5, b, s, "внутри биржи")
    sn = p2p.Snapshot(88.0, "t", {}, {}, [deal], {}, {}, {},
                      groups={("Bybit", "buy", "BTC"): [b], ("Bybit", "sell", "BTC"): [s]})
    bot = start(monkeypatch, deal, sn)
    (c,) = paper.open_cycles()
    vol = bot.cfg.risk_buffer["BTC"]
    assert vol > 0 and c["planned_raw"] > c["planned_pct"] + vol * 0.9
    assert abs((1 + c["planned_raw"] / 100) * (1 - vol / 100) - (1 + c["planned_pct"] / 100)) < 1e-9
    assert f"план {c['planned_pct']:.2f}%" in sent(bot)[-1]["text"]      # в сообщении о старте — план с запасом
    paper.set_stage(c["id"], "sell")
    asyncio.run(bot.process_paper_cycles(sn))                             # тот же стакан на продаже
    done = paper.get_cycle(c["id"])
    assert done["result"] == "done" and abs(done["realized_pct"] - done["planned_raw"]) < 1e-9
    assert abs(paper.stats()["all"]["avg_diff"]) < 1e-9


def _done(planned, raw, realized, n):
    for _ in range(n):
        cid = paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", planned,
                                planned_raw=raw)
        paper.finish_cycle(cid, "done", realized)


def test_ladder_compares_fact_with_plan_without_buffer(monkeypatch):
    """TON: в плане запас 0,7%. Курс к продаже каждый раз уходит на −0,9%: против плана с запасом это −0,2 п.п.
    (лестница предложила бы 20 000), против плана без запаса — −0,9 п.п.: повышать нельзя."""
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    _done(4.7, 5.4, 4.5, paper.LADDER_UP_MIN_CYCLES)
    assert paper.ladder_suggestion() is None
    assert abs(paper.stats()["all"]["avg_diff"] - (-0.9)) < 1e-9


def test_cycles_without_raw_plan_fall_back_to_planned(monkeypatch):
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    _done(4.7, None, 4.5, paper.LADDER_UP_MIN_CYCLES)
    assert paper.ladder_suggestion() == {"action": "up", "amount": paper.LADDER_HIGH}
    assert abs(paper.stats()["all"]["avg_diff"] - (-0.2)) < 1e-9


def test_report_plan_is_without_buffer():
    _done(4.7, 5.4, 4.5, 2)
    _done(3.0, None, 2.0, 1)                                   # круг без planned_raw — план с запасом
    (row,) = paper.report_rows()
    assert abs(row["avg_planned_pct"] - (5.4 * 2 + 3.0) / 3) < 1e-9
    (g,) = paper.label_stats().values()
    assert abs(g["avg_planned_pct"] - (5.4 * 2 + 3.0) / 3) < 1e-9
    assert "без запаса на курс" in Stub(p2p.Config()).paper_report_view([row])


def test_old_db_gets_planned_raw_column(tmp_path):
    db = str(tmp_path / "paper.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, "
                "buy_ex TEXT, buy_asset TEXT, buy_price REAL, buy_nick TEXT, sell_ex TEXT, sell_asset TEXT, "
                "sell_price REAL, sell_nick TEXT, route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, "
                "realized_pct REAL DEFAULT NULL, result TEXT DEFAULT NULL, note TEXT DEFAULT '')")
    con.execute("INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, buy_price, buy_nick, sell_ex, sell_asset, "
                "sell_price, sell_nick, route, planned_pct, stage, ts_stage, realized_pct, result) VALUES "
                "(?, 10000, 'Bybit', 'USDT', 87, 'm', 'MEXC', 'USDT', 90, 'k', 'r', 3.0, 'sell', ?, 2.5, 'done')",
                (time.time(), time.time()))
    con.commit()
    con.close()
    c = paper.get_cycle(1, path=db)
    assert c["planned_raw"] is None and c["pay_fee_used"] == 0 and c["planned_pct"] == 3.0
    assert abs(paper.stats(path=db)["all"]["avg_diff"] - (-0.5)) < 1e-9


def test_reset_archives_db_and_starts_empty(tmp_path):
    db = str(tmp_path / "paper.db")
    paper.init_balance(10000, path=db)
    a = paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0, path=db)
    paper.finish_cycle(a, "done", 2.0, path=db)
    paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0, path=db)   # открытый
    now = datetime.datetime(2026, 9, 26, 15, 30, tzinfo=paper.MSK).timestamp()
    res = paper.reset(path=db, now=now)
    assert res["archive"] == str(tmp_path / "paper-archive-20260926-1530.db")
    assert res["cycles"] == 2 and abs(res["change"] - 200.0) < 1e-9
    assert paper.get_cycle(a, path=res["archive"])["realized_pct"] == 2.0    # архив цел
    assert paper.open_cycles(path=db) == [] and paper.stats(path=db)["all"]["total"] == 0
    assert paper.get_balance(path=db) is None and paper.balance_change(path=db) == 0
    assert paper.reset(path=db, now=now) is None                             # кругов нет — архивировать нечего
    paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0, path=db)
    res2 = paper.reset(path=db, now=now)                                     # та же минута — архив не затираем
    assert res2["archive"] == str(tmp_path / "paper-archive-20260926-1530-2.db")
    assert paper.get_cycle(a, path=res["archive"]) is not None


def _archives():
    return [f for f in os.listdir(os.path.dirname(paper.DB_PATH)) if f.startswith("paper-archive-")]


def test_paper_reset_asks_then_archives_and_keeps_settings():
    B.save_env("PAPER", "1")
    B.save_env("PAPER_AMOUNT", "20000")
    env_before = open(B.ENV_PATH, encoding="utf-8").read()
    cid = paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0)
    paper.finish_cycle(cid, "done", 1.5)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/paper reset"))
    ask = sent(bot)[-1]
    assert [b["callback_data"] for b in buttons(ask["reply_markup"])] == ["paper_reset:yes", "paper_reset:no"]
    assert paper.stats()["all"]["total"] == 1 and not _archives()          # только вопрос — ничего не тронуто
    cq = {"id": "7", "data": "paper_reset:no", "message": {"chat": {"id": 1}, "message_id": bot.paper_reset_ask}}
    asyncio.run(bot.on_update({"callback_query": cq}))
    assert "отменено" in sent(bot, "editMessageText")[-1]["text"]
    assert paper.stats()["all"]["total"] == 1 and not _archives()
    asyncio.run(bot.handle("/paper reset"))
    cq = {"id": "8", "data": "paper_reset:yes", "message": {"chat": {"id": 1}, "message_id": bot.paper_reset_ask}}
    asyncio.run(bot.on_update({"callback_query": cq}))
    text = sent(bot, "editMessageText")[-1]["text"]
    assert "кругов 1" in text and "+150 ₽" in text and "с нуля" in text and "paper-archive-" in text
    assert paper.stats()["all"]["total"] == 0 and len(_archives()) == 1
    assert open(B.ENV_PATH, encoding="utf-8").read() == env_before
    assert paper.settings()["on"] and paper.settings()["amount"] == 20000
    edits = len(sent(bot, "editMessageText"))
    asyncio.run(bot.on_update({"callback_query": cq}))     # двойное нажатие «Да» — итог обнуления не затирается
    assert len(sent(bot, "editMessageText")) == edits and len(_archives()) == 1


def test_paper_reset_button_after_restart_is_stale():
    cid = paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0)
    paper.finish_cycle(cid, "done", 1.5)
    bot = Stub(p2p.Config())                             # вопрос задавал прошлый процесс бота
    cq = {"id": "9", "data": "paper_reset:yes", "message": {"chat": {"id": 1}, "message_id": 77}}
    asyncio.run(bot.on_update({"callback_query": cq}))
    assert "устарела" in sent(bot, "editMessageText")[-1]["text"]
    assert paper.stats()["all"]["total"] == 1 and not _archives()


def test_cycle_gone_after_reset_is_not_reported(monkeypatch):
    cid = paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0)
    bot = Stub(p2p.Config())
    cycles = paper.open_cycles()
    paper.reset()                                        # владелец обнулил, пока цикл обработки шёл
    monkeypatch.setattr(paper, "open_cycles", lambda: cycles)
    monkeypatch.setattr(paper, "check_buy_stage", lambda *a, **k: ("fail", "мерчант ушёл"))
    asyncio.run(bot.process_paper_cycles(p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})))
    assert not [p for m, p in bot.out if m == "sendMessage" and f"круг #{cid}" in p["text"]]


def test_guest_cannot_reset_paper():
    cid = paper.start_cycle(10000, ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0), "r", 3.0)
    paper.finish_cycle(cid, "done", 1.5)
    bot = Stub(p2p.Config(), guests=["42"])
    asyncio.run(bot.on_update(msg(42, "/paper reset")))
    assert sent(bot)[-1]["chat_id"] == "42" and sent(bot)[-1]["text"] == B.GUEST_DENIED
    cq = {"id": "8", "data": "paper_reset:yes", "message": {"chat": {"id": 42}, "message_id": 5}}
    asyncio.run(bot.on_update({"callback_query": cq}))
    assert "владельца" in sent(bot, "answerCallbackQuery")[-1]["text"]
    assert not sent(bot, "editMessageText")
    assert paper.stats()["all"]["total"] == 1 and not _archives()


def test_reset_is_listed_in_paper_help():
    bot = Stub(p2p.Config())
    assert "/paper reset" in bot.paper_view()
    assert "reset" in next(c["description"] for c in B.COMMANDS if c["command"] == "paper")
    assert "reset" not in B.GUEST_DENIED and "/paper" not in B.GUEST_WELCOME
