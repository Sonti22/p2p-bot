"""Сухой прогон повторяет сигналы владельца: только подтверждённые связки, без «🪤 ловушек» (по умолчанию),
с меткой надёжности в круге и фактом продажи по реальной цене стакана."""
import time

import bot as B
import p2p
import paper
from test_bot import Stub, texts
from helpers import arun


def ad(ex, side, price, orders=1000, avail=10000):
    return p2p.Ad(ex, side, price, 1000, 500000, avail, ["T-Bank"], f"{ex}-{side}", orders, 100.0, "", "USDT", "", "")


def trap_deal():
    """Покупка на 3,4% ниже ориентира, продажа на 4,5% выше, спред ≥5% — три причины → «🪤 ловушка»."""
    b, s = ad("Bybit", "buy", 85.0), ad("MEXC", "sell", 92.0)
    return 8.0, b, s, "перевод на MEXC"


def good_deal():
    b, s = ad("HTX", "buy", 87.5), ad("KuCoin", "sell", 91.0)
    return 3.9, b, s, "перевод на KuCoin"


def snap_of(deals):
    groups = {}
    for _, b, s, _ in deals:
        groups.setdefault((b.ex, "buy", b.asset), []).append(b)
        groups.setdefault((s.ex, "sell", s.asset), []).append(s)
    return p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, list(deals), {}, {}, {}, groups=groups)


def _on(monkeypatch, **env):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    monkeypatch.setenv("PAPER_MAX_OPEN", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_reliability_labels_used_in_tests_are_as_intended():
    s = snap_of([trap_deal(), good_deal()])
    cfg = p2p.Config(min_profit=2.0)
    assert p2p.reliability(p2p.deal_for_amount(trap_deal(), cfg, s, 10000), cfg, s)[0] == p2p.TRAP
    assert p2p.reliability(p2p.deal_for_amount(good_deal(), cfg, s, 10000), cfg, s)[0] != p2p.TRAP


def test_unconfirmed_deal_is_not_taken_until_signal(monkeypatch):
    _on(monkeypatch)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 2
    s = snap_of([good_deal()])
    bot.track_liveness(s)
    arun(bot.notify(s))                      # первый скан — сигнала ещё нет, круга тоже
    assert not paper.open_cycles()
    bot.track_liveness(s)
    arun(bot.notify(s))                      # держится 2 скана — сигнал и круг
    cycles = paper.open_cycles()
    assert len(cycles) == 1 and cycles[0]["buy_ex"] == "HTX"


def test_trap_is_skipped_by_default_and_next_deal_taken(monkeypatch):
    _on(monkeypatch)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    arun(bot.notify(snap_of([trap_deal(), good_deal()])))
    (c,) = paper.open_cycles()
    assert c["buy_ex"] == "HTX" and c["label"] != p2p.TRAP
    card = [t for t in texts(bot) if "Сухой прогон" in t][0]
    assert c["label"] in card and "план" in card


def test_trap_taken_when_owner_allows_and_reasons_shown(monkeypatch):
    _on(monkeypatch, PAPER_TRAPS="1")
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    arun(bot.notify(snap_of([trap_deal()])))   # единственная связка — у ловушки оценка хуже любой чистой
    (c,) = paper.open_cycles()
    assert c["buy_ex"] == "Bybit" and c["label"] == p2p.TRAP
    card = [t for t in texts(bot) if "Сухой прогон" in t][0]
    assert "🪤" in card and "от ориентира" in card
    assert not [m for m in bot.out if m[0] == "sendPhoto"]   # в прогон взята, но сигналом не пришла (SIGNAL_TRAPS=0)


def test_paper_picks_best_score_not_first_in_list(monkeypatch):
    """PAPER_TRAPS=1: и ловушка, и чистая связка в кандидатах — прогон берёт ту, у которой выше p2p.score на
    PAPER_AMOUNT, а не первую в списке (у ловушки прибыль больше, но веса рисков её перевешивают)."""
    _on(monkeypatch, PAPER_TRAPS="1")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    s = snap_of([trap_deal(), good_deal()])
    trap, good = (p2p.deal_for_amount(d, bot.cfg, s, 10000) for d in (trap_deal(), good_deal()))
    assert trap[0] > good[0] and p2p.score(trap, bot.cfg, s) < p2p.score(good, bot.cfg, s)
    arun(bot.notify(s))
    (c,) = paper.open_cycles()
    assert c["buy_ex"] == "HTX"
    card = [t for t in texts(bot) if "Сухой прогон" in t][0]
    assert f"оценка {p2p.score(good, bot.cfg, s):+.2f}" in card


def test_sell_stage_records_fact_price_and_note(monkeypatch):
    b, s = ad("Bybit", "buy", 85.0), ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, b, s, "route", 2.0, ts=time.time() - 400, label=p2p.RELIABLE)
    paper.set_stage(cid, "sell")
    bot = Stub(p2p.Config(min_profit=2.0))
    lower = ad("MEXC", "sell", 89.0)
    snap = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups={("MEXC", "sell", "USDT"): [lower]})
    arun(bot.process_paper_cycles(snap))
    c = paper.get_cycle(cid)
    assert c["result"] == "done" and c["sell_fact"] == 89.0 and 0 < c["realized_pct"] < 2.0
    assert "вместо 90" in c["note"]
    msg = [t for t in texts(bot) if "завершён" in t][0]
    assert "теоретический результат" in msg and "вместо 90" in msg
    assert "выручка 10086.67 ₽" in msg and "+86.67 ₽" in msg


def test_report_shows_results_by_label(monkeypatch):
    b, s = ad("Bybit", "buy", 85.0), ad("MEXC", "sell", 90.0)
    for label, result, rp in ((p2p.RELIABLE, "done", 1.5), (p2p.RISKY, "failed_sell", 0.0)):
        cid = paper.start_cycle(10000, b, s, "route", 2.0, label=label)
        paper.finish_cycle(cid, result, rp)
    text = Stub(p2p.Config()).paper_report_view(paper.report_rows())
    assert "По метке надёжности" in text
    assert f"{p2p.RELIABLE}: 1 кругов, исполнилось 1" in text and f"{p2p.RISKY}: 1 кругов, исполнилось 0" in text


def test_old_paper_db_gets_new_columns(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, "
                "buy_ex TEXT, buy_asset TEXT, buy_price REAL, buy_nick TEXT, sell_ex TEXT, sell_asset TEXT, "
                "sell_price REAL, sell_nick TEXT, route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, "
                "realized_pct REAL DEFAULT NULL, result TEXT DEFAULT NULL, note TEXT DEFAULT '', bank TEXT DEFAULT '')")
    con.execute("INSERT INTO cycles (ts_start, amount, stage, ts_stage) VALUES (1, 10000, 'buy', 1)")
    con.commit()
    con.close()
    c = paper.get_cycle(1, path=db)
    assert c["label"] == "" and c["sell_fact"] is None


def test_ladder_button_answers_callback_once(monkeypatch):
    bot = Stub(p2p.Config())
    arun(bot.on_callback({"id": "1", "data": "paper_ladder:20000", "message": {"message_id": 9}}))
    assert [m for m, _ in bot.out].count("answerCallbackQuery") == 1


# These regressions explicitly exercise the preserved historical engine.
import pytest as _compat_pytest
pytestmark = _compat_pytest.mark.usefixtures("legacy_paper_engine")
