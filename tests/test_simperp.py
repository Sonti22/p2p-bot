"""simperp.py: бумажный хедж кругов шортом перпа — выбор площадки, лот, фандинг в окне, закрытие, отчёт, HEDGE_PLAN."""
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
from helpers import arun, make_ad
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
    perp._instr[("BingX", "GRAMTONUSDT")] = perp.Instrument("BingX", "GRAMTONUSDT", False, note="снят")
    plan, note = simperp.choose("TON", 100, 10000, RUB, 110, now=NOW)   # TON → перп GRAMUSDT
    assert plan is None and "BingX: GRAMTONUSDT не торгуется (снят)" in note and "Bybit: нет свежей котировки" in note
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


def test_every_paper_reader_is_name_based_whatever_the_alter_order(tmp_path):
    """Живая data/paper.db могла получить колонки в другом порядке (ветки мигрируют по-разному): здесь hedge_* и
    чужая колонка стоят ДО bank/label/…/planned_raw/route_hops. Все чтения paper.py, calibration и simperp берут
    значения по именам — ни одно поле не съезжает."""
    import calibration
    db = str(tmp_path / "paper.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, buy_ex TEXT, "
                "buy_asset TEXT, buy_price REAL, buy_nick TEXT, sell_ex TEXT, sell_asset TEXT, sell_price REAL, "
                "sell_nick TEXT, route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, realized_pct REAL DEFAULT NULL, "
                "result TEXT DEFAULT NULL, note TEXT DEFAULT '')")
    for col, ddl in (*paper.HEDGE_COLUMNS, ("other_branch_col", "TEXT DEFAULT 'x'")):
        con.execute(f"ALTER TABLE cycles ADD COLUMN {col} {ddl}")
    con.commit()
    con.close()
    cid = _cycle(db)   # _connect допишет bank, label, …, planned_raw, route_hops — в конец, после хеджа
    names = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(cycles)")]
    assert names.index("hedge_state") < names.index("other_branch_col") < names.index("planned_raw")
    c = paper.get_cycle(cid, path=db)
    assert (c["amount"], c["buy_asset"], c["planned_pct"], c["planned_raw"], c["sell_qty"], c["stage"]) == \
        (10000.0, "BTC", 0.5, 0.6, 0.0013, "buy")
    assert (c["hedge_venue"], c["hedge_qty"], c["other_branch_col"]) == ("", None, "x")
    assert paper.open_cycles(path=db)[0]["planned_raw"] == 0.6
    _btc_quotes(rate_bingx=0.0)
    plan, _ = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW)
    simperp.open_hedge(cid, plan, "", now=NOW, path=db)
    paper.finish_cycle(cid, "done", 0.8, path=db, ts=NOW + 600)
    assert simperp.tick(RUB, now=NOW + 60, path=db)[0]["id"] == cid
    assert paper.stats(path=db, now=NOW + 600)["all"]["avg_diff"] == pytest.approx(0.8 - 0.6)
    row = paper.report_rows(path=db)[0]
    assert (row["buy_asset"], row["avg_planned_pct"], row["avg_realized_pct"]) == ("BTC", 0.6, 0.8)
    assert row["avg_duration_min"] == pytest.approx(10.0)
    assert paper.balance_change(path=db) == pytest.approx(10000 * 0.8 / 100)
    assert paper.first_start(path=db) == NOW
    s = calibration.paper_samples(db)[0]
    assert s.done and s.buy_asset == "BTC" and s.diff == pytest.approx(0.2) and s.duration == pytest.approx(600)
    r = simperp.report(db)
    assert r["closed"] == 1 and r["pairs"] == 1


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
    arun(bot.maybe_start_paper_cycle([d], snap))
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
    arun(bot.maybe_start_paper_cycle([d], TB.snap_groups([d])))
    assert len(paper.open_cycles(path=db)) == 1


def test_bot_hedge_error_after_choose_does_not_block_cycle_or_signal(monkeypatch, tmp_path):
    """Сбой где угодно в хедже (здесь — строка карточки) не прерывает maybe_start_paper_cycle: он стоит в notify
    перед отправкой сигналов. План круга — без хеджа."""
    db = str(tmp_path / "paper.db")
    _patch(monkeypatch, db)
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(simperp.time, "time", lambda: NOW)
    for k, v in (("PAPER", "1"), ("PAPER_AMOUNT", "10000"), ("HEDGE_PLAN", "1")):
        monkeypatch.setenv(k, v)
    _btc_quotes()

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(B.simperp, "card_line", boom)
    bot = TB.Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    d = _btc_deal()
    snap = TB.snap_groups([d])
    snap.ref = RUB
    arun(bot.maybe_start_paper_cycle([d], snap))
    c = paper.open_cycles(path=db)[0]
    assert c["hedge_state"] == "" and any("Сухой прогон" in t for t in TB.texts(bot))


def test_settings_typos_fall_back_and_perps_off_means_no_hedge(monkeypatch):
    monkeypatch.setenv("HEDGE_RATIO", "1,0")
    monkeypatch.setenv("HEDGE_MAX_HOURS", "six")
    st = simperp.settings()
    assert st["ratio"] == 1.0 and st["max_hours"] == 6
    _btc_quotes()
    monkeypatch.setenv("PERPS", "0")   # перпы не опрашиваются — без хеджа и без «нет котировки» в каждом круге
    assert simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW) == (None, "")


def test_bot_hedges_bought_coins_when_route_qty_missing(monkeypatch, tmp_path):
    """Нет выхода маршрута — хеджируем купленную монету (сумма / цена), а не весь объём объявления продажи."""
    db = str(tmp_path / "paper.db")
    _patch(monkeypatch, db)
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(simperp.time, "time", lambda: NOW)
    monkeypatch.setattr(B, "_route_qty", lambda *a, **k: None)
    for k, v in (("PAPER", "1"), ("PAPER_AMOUNT", "10000")):
        monkeypatch.setenv(k, v)
    _btc_quotes()
    bot = TB.Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    d = _btc_deal()
    snap = TB.snap_groups([d])
    snap.ref = RUB
    arun(bot.maybe_start_paper_cycle([d], snap))
    c = paper.open_cycles(path=db)[0]
    st = json.loads(c["hedge_state"])
    assert st["coin_qty"] == pytest.approx(10000 / 7_000_000.0) and c["hedge_qty"] == pytest.approx(0.0014)


def test_tick_skips_broken_row_and_closes_others(tmp_path):
    db = str(tmp_path / "paper.db")
    _btc_quotes(rate_bingx=0.0)
    ids = []
    for _ in range(2):
        cid = _cycle(db)
        plan, _ = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW)
        simperp.open_hedge(cid, plan, "", now=NOW, path=db)
        paper.finish_cycle(cid, "done", 0.2, path=db, ts=NOW + 60)
        ids.append(cid)
    con = sqlite3.connect(db)
    con.execute("UPDATE cycles SET hedge_state = '{broken' WHERE id = ?", (ids[0],))
    con.commit()
    con.close()
    closed = simperp.tick(RUB, now=NOW + 60, path=db)
    assert [c["id"] for c in closed] == [ids[1]]
    assert simperp.report(db)["closed"] == 1   # битую запись отчёт пропускает


def test_fact_cost_counts_spread_like_the_plan(tmp_path):
    """Цена не двигалась, ставка 0: факт стоимости (комиссии + спред − фандинг) совпадает с ожидаемой."""
    db = str(tmp_path / "paper.db")
    _btc_quotes(rate_bingx=0.0)
    cid = _cycle(db)
    plan, _ = simperp.choose("BTC", 0.0013, 10000, RUB, 84000 * RUB, now=NOW)
    simperp.open_hedge(cid, plan, "", now=NOW, path=db)
    paper.finish_cycle(cid, "done", 0.2, path=db, ts=NOW + 60)
    simperp.tick(RUB, now=NOW + 60, path=db)
    r = simperp.report(db)
    assert r["fact_cost"] == pytest.approx(r["exp_cost"]) and r["exp_cost"] == pytest.approx(plan["cost_pct"])


def test_cost_model_matches_hedge_backtest():
    """Сверка с research/hedge_bt.py на одинаковых входах: без движения цены и фандинга стоимость хеджа simperp
    (тейкер на входе и выходе + пересечение спреда по стакану) = фиксированная стоимость бэктеста при комиссиях ×1 и
    проскальзывании = полспреда на сторону. «0.8 запаса» из бэктеста BTC — это комиссии ×2 и 0.02%/сторону."""
    from research import hedge_bt
    mid, spread, taker = 84000.0, 16.8, 0.05            # спред 0.02% — по 0.01% на сторону
    install(quote("BingX", "BTCUSDT", mid=mid, spread=spread, size=1.0, rate=0.0, lot=0.0001, min_qty=0.0001,
                  fee=taker, ts=NOW),
            quote("Bybit", "BTCUSDT", mid=mid, spread=spread, size=1.0, rate=0.0, lot=0.0001, min_qty=0.0001,
                  fee=taker, ts=NOW))
    qty = 0.01
    amount = qty * mid * RUB                             # коэффициент хеджа 1: сумма круга = номинал шорта
    plan, _ = simperp.choose("BTC", qty, amount, RUB, mid * RUB, now=NOW)
    half = spread / 2 / mid * 100                         # % на сторону
    zero = [(0, 0.0, 0.0, 0.0)] * 3                       # окна без движения цены и без фандинга
    bt = hedge_bt.evaluate_window(zero, zero, 60, "BTC", hedge_bt.Params(fee_mult=1.0, slippage={"BTC": half}), taker)
    assert plan["cost_pct"] == pytest.approx(bt["cost_mean_pct"], abs=1e-4)
    stress = hedge_bt.evaluate_window(zero, zero, 60, "BTC", hedge_bt.Params(), 0.05)   # ×2 и 0.02%/сторону
    assert stress["cost_mean_pct"] == pytest.approx(0.24) and stress["cost_to_buffer"] == pytest.approx(0.8)


def test_ton_hedges_with_per_venue_symbols():
    """TON (он же GRAM): на Bybit — GRAMUSDT, на BingX — GRAMTON-USDT; интервал фандинга 4 ч."""
    install(quote("Bybit", "GRAMUSDT", mid=1.59, spread=0.001, size=10000, rate=0.00005, lot=0.1, min_qty=0.1,
                  interval_h=4, ts=NOW, asset="TON"),
            quote("BingX", "GRAMTONUSDT", mid=1.588, spread=0.003, size=10000, rate=0.00005, lot=0.001, min_qty=1.26,
                  fee=0.05, interval_h=4, ts=NOW, asset="TON"))
    plan, _ = simperp.choose("TON", 70.0, 10000, RUB, 1.59 * RUB, now=NOW)
    assert plan["symbol"] == {"Bybit": "GRAMUSDT", "BingX": "GRAMTONUSDT"}[plan["venue"]]
    assert set(plan["alt"]) == {"Bybit", "BingX"} - {plan["venue"]}
