"""simperp.py: бумажный хедж кругов шортом перпа — выбор площадки, лот, фандинг в окне, закрытие, отчёт, HEDGE_PLAN."""
import asyncio
import functools
import json
import sqlite3

import pytest

import bot as B
import p2p
import paper
import perp
import simperp
import test_bot as TB
from helpers import make_ad
from perpfx import install, quote

NOW = 1790494000.0
RUB = 90.0   # ₽ за USDT


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    perp.reset()
    for k in ("PAPER_HEDGE", "HEDGE_PLAN", "HEDGE_RATIO", "HEDGE_RATIO_BAND", "HEDGE_MAX_HOURS", "HEDGE_RESIDUAL",
              "HEDGE_HOLD_MINUTES", "HEDGE_ASSETS", "PERP_MAX_AGE"):
        monkeypatch.delenv(k, raising=False)
    yield
    perp.reset()


def _btc_quotes(rate_bybit=0.0001, rate_bingx=0.0001, next_funding=NOW + 3600):
    install(quote("Bybit", "BTCUSDT", mid=84000, spread=1.0, rate=rate_bybit, lot=0.001, min_qty=0.001, fee=0.055,
                  next_funding=next_funding, ts=NOW),
            quote("BingX", "BTCUSDT", mid=84000, spread=2.0, rate=rate_bingx, lot=0.0001, min_qty=0.0001, fee=0.05,
                  next_funding=next_funding, ts=NOW))


def test_choose_skips_venue_with_coarse_lot():
    _btc_quotes()
    coin = 10000 / RUB / 84000   # ≈0.00132 BTC на круг 10 000 ₽
    plan, note = simperp.choose("BTC", coin, 10000, RUB, 84000 * RUB, risk=0.3, now=NOW)
    assert plan["venue"] == "BingX" and plan["qty"] == pytest.approx(0.0013)
    assert 0.9 <= plan["ratio"] <= 1.1
    assert "Bybit: лот 0.001" in note   # 0.001 BTC = коэффициент 0.76 — вне полосы
    assert plan["residual_pct"] == pytest.approx(0.1 + abs(1 - plan["ratio"]) * 0.3)


def test_choose_picks_cheaper_venue_and_counts_funding():
    install(quote("Bybit", "ETHUSDT", mid=2700, spread=0.01, rate=0.0, lot=0.01, min_qty=0.01, fee=0.055, ts=NOW),
            quote("BingX", "ETHUSDT", mid=2700, spread=0.01, rate=0.0, lot=0.01, min_qty=0.01, fee=0.05, ts=NOW))
    plan, _ = simperp.choose("ETH", 0.5, 120000, RUB, 2700 * RUB, now=NOW)
    assert plan["venue"] == "BingX" and "Bybit" in plan["alt"] and plan["alt"]["Bybit"] > plan["cost_pct"]
    # положительная ставка и расчёт внутри окна круга — шорт получает, стоимость ниже
    perp.reset()
    install(quote("Bybit", "ETHUSDT", mid=2700, spread=0.01, rate=0.001, lot=0.01, fee=0.055, ts=NOW,
                  next_funding=NOW + 600),
            quote("BingX", "ETHUSDT", mid=2700, spread=0.01, rate=0.0, lot=0.01, fee=0.05, ts=NOW))
    plan2, _ = simperp.choose("ETH", 0.5, 120000, RUB, 2700 * RUB, now=NOW)
    assert plan2["venue"] == "Bybit" and plan2["windows"] == 1 and plan2["funding"] > 0


def test_choose_without_quotes_explains_why():
    perp._instr[("Bybit", "TONUSDT")] = perp.Instrument("Bybit", "TONUSDT", False, note="статус Closed")
    perp._instr[("BingX", "TONUSDT")] = perp.Instrument("BingX", "TONUSDT", False, note="нет в списке контрактов")
    plan, note = simperp.choose("TON", 100, 10000, RUB, 110, now=NOW)
    assert plan is None and "не торгуется" in note and "Closed" in note
    assert simperp.choose("USDT", 100, 10000, RUB, 90, now=NOW) == (None, "")   # стейблкоин не хеджируем


def test_hedged_plan_and_card_line():
    _btc_quotes()
    plan, note = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, risk=0.3, now=NOW)
    assert simperp.hedged_plan(1.5, plan) == pytest.approx(1.5 - plan["cost_pct"] - plan["residual_pct"])
    assert "шорт 0.0013 BTC на BingX" in simperp.card_line(plan, note, "BTC")
    assert simperp.card_line(None, "", "USDT") == ""


def _cycle(db, asset="BTC", amount=10000.0, buy=84000 * RUB, sell=84500 * RUB, qty=0.0013):
    b = make_ad("Bybit", "buy", buy, asset=asset)
    s = make_ad("MEXC", "sell", sell, asset=asset)
    return paper.start_cycle(amount, b, s, "перевод", 0.5, path=db, ts=NOW, sell_qty=qty, over=set(), planned_raw=0.6)


def test_lifecycle_funding_close_and_report(tmp_path):
    db = str(tmp_path / "paper.db")
    _btc_quotes(rate_bingx=0.0002, next_funding=NOW + 600)
    cid = _cycle(db)
    plan, note = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW)
    simperp.open_hedge(cid, plan, note, now=NOW, path=db)
    c = paper.get_cycle(cid, path=db)
    assert c["hedge_venue"] == "BingX" and c["hedge_qty"] == pytest.approx(0.0013)
    assert c["hedge_open"] == pytest.approx(83999.0)          # бид лучшего уровня (mid − спред/2)
    # расчёт фандинга наступил: ставка из котировки до расчёта, шорт получает rate × mark × qty
    install(quote("BingX", "BTCUSDT", mid=83000, spread=2.0, rate=-0.0005, lot=0.0001, fee=0.05,
                  next_funding=NOW + 600 + 8 * 3600, ts=NOW + 620))
    assert simperp.tick(RUB, now=NOW + 620, path=db) == []
    c = paper.get_cycle(cid, path=db)
    assert c["hedge_funding"] == pytest.approx(0.0002 * 84000 * 0.0013)
    # круг исполнен — откуп по аскам
    paper.finish_cycle(cid, "done", 0.2, path=db, ts=NOW + 700)
    closed = simperp.tick(RUB, now=NOW + 700, path=db)
    assert len(closed) == 1 and closed[0]["reason"] == "круг исполнен"
    c = paper.get_cycle(cid, path=db)
    assert c["hedge_close"] == pytest.approx(83001.0)
    fees = 0.0005 * (83999.0 + 83001.0) * 0.0013
    pnl = (83999.0 - 83001.0) * 0.0013 - fees + 0.0002 * 84000 * 0.0013
    assert c["hedge_fees"] == pytest.approx(fees) and c["hedge_pnl"] == pytest.approx(pnl)
    st = json.loads(c["hedge_state"])
    assert st["status"] == "closed" and st["pnl_pct"] == pytest.approx(pnl * RUB / 10000 * 100)
    assert simperp.tick(RUB, now=NOW + 800, path=db) == []   # закрытый не трогаем
    r = simperp.report(db)
    assert r["closed"] == 1 and r["pairs"] == 1 and r["ratio_ok"] == 1.0 and r["fundings"] == 1
    assert any("исполненных кругов с хеджем пока 1" in x for x in simperp.report_lines(db))


def test_timeout_and_stale_close(tmp_path, monkeypatch):
    db = str(tmp_path / "paper.db")
    monkeypatch.setenv("HEDGE_MAX_HOURS", "6")
    _btc_quotes()
    cid = _cycle(db)
    plan, _ = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW)
    simperp.open_hedge(cid, plan, "", now=NOW, path=db)
    t = NOW + 6 * 3600 + 5
    assert simperp.tick(RUB, now=t, path=db) == []           # срок вышел, но котировка старая — ждём
    assert simperp.tick(RUB, now=t + simperp.STALE_CLOSE + 1, path=db)[0]["reason"] == "таймаут 6 ч"
    st = json.loads(paper.get_cycle(cid, path=db)["hedge_state"])
    assert st["stale"] is True and st["status"] == "closed"


def test_sigma_hedged_vs_unhedged(tmp_path):
    db = str(tmp_path / "paper.db")
    for i, (realized, move) in enumerate(((1.0, -500.0), (0.2, 300.0), (0.6, -100.0))):
        perp.reset()
        _btc_quotes(rate_bingx=0.0)
        cid = _cycle(db)
        plan, _ = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW)
        simperp.open_hedge(cid, plan, "", now=NOW, path=db)
        install(quote("BingX", "BTCUSDT", mid=84000 + move, spread=2.0, rate=0.0, lot=0.0001, fee=0.05, ts=NOW + 60))
        paper.finish_cycle(cid, "done", realized, path=db, ts=NOW + 60)
        simperp.tick(RUB, now=NOW + 60, path=db)
    r = simperp.report(db)
    assert r["pairs"] == 3 and r["sigma_u"] is not None and r["sigma_h"] is not None
    assert "σ(факт − план) по 3" in "\n".join(simperp.report_lines(db))


def test_none_status_is_reported(tmp_path):
    db = str(tmp_path / "paper.db")
    cid = _cycle(db, asset="TON", buy=110.0, sell=112.0, qty=90)
    simperp.open_hedge(cid, None, "Bybit: TONUSDT не торгуется", now=NOW, path=db)
    assert simperp.report(db)["none"] == {"Bybit: TONUSDT не торгуется": 1}
    assert any("без хеджа 1×" in x for x in simperp.report_lines(db))


def test_paper_migration_and_rows_by_name(tmp_path):
    """Старая база без колонок хеджа получает их; get_cycle берёт значения по именам, а не по позиции (у другой
    ветки колонка могла встать раньше колонок хеджа)."""
    db = str(tmp_path / "paper.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, buy_ex TEXT, "
                "buy_asset TEXT, buy_price REAL, buy_nick TEXT, sell_ex TEXT, sell_asset TEXT, sell_price REAL, "
                "sell_nick TEXT, route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, realized_pct REAL, "
                "result TEXT, note TEXT DEFAULT '', other_branch_col TEXT DEFAULT 'x')")
    con.execute("INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, stage, ts_stage) "
                "VALUES (1, 10000, 'Bybit', 'BTC', 'buy', 1)")
    con.commit()
    con.close()
    c = paper.get_cycle(1, path=db)
    assert c["other_branch_col"] == "x" and c["hedge_venue"] == "" and c["hedge_qty"] is None
    assert c["amount"] == 10000 and paper.open_cycles(path=db)[0]["buy_asset"] == "BTC"


def _patch(monkeypatch, db):
    TB._patch_paper_db(monkeypatch, db)
    monkeypatch.setattr(B.paper, "get_cycle", functools.partial(B.paper.get_cycle, path=db))
    for name in ("open_hedge", "tick", "report_lines"):
        monkeypatch.setattr(B.simperp, name, functools.partial(getattr(simperp, name), path=db))


def _btc_deal():
    b = make_ad("Bybit", "buy", 7_000_000.0, asset="BTC", avail=1.0, max_amt=5_000_000)
    s = make_ad("Bybit", "sell", 7_300_000.0, asset="BTC", avail=1.0, max_amt=5_000_000)
    return 4.0, b, s, "внутри биржи"


def test_bot_starts_cycle_with_hedge_plan(monkeypatch, tmp_path):
    db = str(tmp_path / "paper.db")
    _patch(monkeypatch, db)
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(simperp.time, "time", lambda: NOW)
    for k, v in (("PAPER", "1"), ("PAPER_AMOUNT", "10000"), ("HEDGE_PLAN", "1")):
        monkeypatch.setenv(k, v)
    _btc_quotes()
    bot = TB.Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    d = _btc_deal()
    snap = TB.snap_groups([d])
    snap.ref = RUB
    asyncio.run(bot.maybe_start_paper_cycle([d], snap))
    c = paper.open_cycles(path=db)[0]
    assert c["hedge_venue"] == "BingX" and c["buy_asset"] == "BTC"
    st = json.loads(c["hedge_state"])
    assert c["planned_pct"] == pytest.approx(c["planned_raw"] - st["exp_cost_pct"] - st["residual_pct"])
    assert any("🛡 хедж (бумага): шорт" in t for t in TB.texts(bot))


def test_bot_hedge_failure_does_not_block_cycle(monkeypatch, tmp_path):
    db = str(tmp_path / "paper.db")
    _patch(monkeypatch, db)
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(B.simperp, "choose", boom)
    bot = TB.Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    d = _btc_deal()
    asyncio.run(bot.maybe_start_paper_cycle([d], TB.snap_groups([d])))
    assert len(paper.open_cycles(path=db)) == 1
