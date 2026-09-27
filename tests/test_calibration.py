"""Калибровка (план 2.6): тип маршрута, сжатие поправки к классу грубее, Beta(1,1) и переход к грубой корзине,
активность EV, знак EV, цена срыва, запас на курс из данных, устойчивость к пустым базам и старым схемам,
отчёт /calibration — только владельцу и только за флагом."""
import asyncio
import os
import pathlib
import sqlite3

import pytest

import bot as B
import calibration as C
import history
import p2p
import paper
import trades
from helpers import make_ad


def S(diff=0.0, done=True, bex="MEXC", sex="BestChange", rel="⚠️", depth="d?", realized=None, rtype="same",
      asset="USDT", source="paper", duration=None):
    return C.Sample(source, rtype, asset, bex, sex, done, diff if done else None, realized, rel, depth, duration)


def make_paper(path, rows, cols=("buy_ex TEXT", "buy_asset TEXT", "sell_ex TEXT", "sell_asset TEXT", "route TEXT",
                                 "planned_pct REAL", "planned_raw REAL", "realized_pct REAL", "result TEXT",
                                 "label TEXT", "ts_start REAL", "ts_stage REAL")):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE cycles (id INTEGER PRIMARY KEY, {', '.join(cols)})")
    for r in rows:
        con.execute(f"INSERT INTO cycles ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))
    con.commit()
    con.close()
    return str(path)


def row(result="done", planned=1.0, raw=None, realized=1.0, bex="MEXC", sex="BestChange", asset="USDT",
        route="перевод −0.1 USDT (BEP20) на BestChange", label="⚠️ риск", t0=1000.0, t1=1600.0):
    r = {"buy_ex": bex, "buy_asset": asset, "sell_ex": sex, "sell_asset": asset, "route": route,
         "planned_pct": planned, "realized_pct": realized, "result": result, "label": label,
         "ts_start": t0, "ts_stage": t1}
    if raw is not None:
        r["planned_raw"] = raw
    return r


def test_route_type():
    assert C.route_type("USDT", "USDT", "перевод −1 USDT (TRC20) на MEXC") == "same"
    assert C.route_type("USDT", "USDT", "через Bybit: перевод −1 USDT (POLYGON)") == "relay"
    assert C.route_type("USDT", "BTC", "перевод → спот USDT→BTC на Bybit (−0.1%)") == "spot"
    assert C.route_type("BTC", "USDT", "") == "spot"
    assert C.route_type("ETH", "BTC", "спот ETH→USDT на Bybit → спот USDT→BTC на Bybit") == "cross"
    assert C.route_type("USDT", "USDT", None) == "same"


def test_buckets():
    assert C.rel_bucket("⚠️ риск") == "⚠️" and C.rel_bucket("") == "—" and C.rel_bucket(None) == "—"
    assert C.rel_bucket("✅ надёжно", index=9) == "i8+" and C.rel_bucket("", 6) == "i5-7" and C.rel_bucket("", 0) == "i0-4"
    assert C.depth_bucket(None) == "d?" and C.depth_bucket(1.2) == "d<1.5"
    assert C.depth_bucket(2.0) == "d1.5-3" and C.depth_bucket(3.0) == "d3+"


def test_shrink_formula():
    assert C.shrink(20, 1.0, 0.0) == pytest.approx(0.5)
    assert C.shrink(0, 5.0, 0.3) == pytest.approx(0.3)
    assert C.shrink(180, 1.0, 0.0) == pytest.approx(0.9)
    assert C.shrink(0, 0.0, 0.2, k=0) == 0.2


def test_bias_shrinks_to_coarser_class_then_global():
    samples = [S(1.0) for _ in range(20)] + [S(-1.0, bex="HTX", sex="BitPapa") for _ in range(20)]
    cal = C.Calibration(samples)
    assert cal.global_bias == pytest.approx(0.0)
    # (same,) и (same, USDT) — среднее 0; (same, USDT, BestChange) — 20 × 1 → 0.5; точный класс → (20 + 10) / 40
    assert cal.bias("same", "USDT", "MEXC", "BestChange") == (pytest.approx(0.75), 20)
    assert cal.bias("same", "USDT", "HTX", "BitPapa") == (pytest.approx(-0.75), 20)
    # класса нет — оценка ближайшего известного грубее (та же площадка продажи и монета)
    assert cal.bias("same", "USDT", "Bybit", "BestChange") == (pytest.approx(0.5), 0)
    # неизвестный тип маршрута — общее среднее
    assert cal.bias("spot", "BTC", "Bybit", "MEXC") == (pytest.approx(0.0), 0)
    # без сжатия — сырое среднее класса
    assert C.Calibration(samples, k=0).bias("same", "USDT", "MEXC", "BestChange")[0] == pytest.approx(1.0)


def test_bias_many_rounds_approach_class_mean():
    samples = [S(0.4) for _ in range(2000)] + [S(-2.0, bex="HTX", sex="BitPapa") for _ in range(5)]
    adj, n = C.Calibration(samples).bias("same", "USDT", "MEXC", "BestChange")
    assert n == 2000 and adj == pytest.approx(0.4, abs=0.01)
    small, _ = C.Calibration(samples).bias("same", "USDT", "HTX", "BitPapa")
    assert -2.0 < small < 0.0                  # 5 кругов — сильно сжаты к общему


def test_fill_beta_prior_and_fallback():
    samples = ([S(done=True) for _ in range(9)] + [S(done=False) for _ in range(3)]
               + [S(done=True, rel="✅") for _ in range(3)])
    cal = C.Calibration(samples, min_bucket=10)
    p, key, n = cal.fill("⚠️", "MEXC", "BestChange")
    assert p == pytest.approx(10 / 14) and key == ("rvd", "⚠️", "MEXC→BestChange", "d?") and n == 12
    p, key, n = cal.fill("✅", "MEXC", "BestChange")          # 3 круга — мало, берём площадки целиком
    assert p == pytest.approx(13 / 17) and key == ("v", "MEXC→BestChange") and n == 15
    p, key, n = cal.fill("🪤", "Bybit", "HTX")                 # ничего похожего — все круги
    assert p == pytest.approx(13 / 17) and key == ("all",) and n == 15
    assert C.Calibration([]).fill("✅", "A", "B")[0] == pytest.approx(0.5)   # Beta(1,1) без данных


def test_fill_depth_bucket_first():
    samples = [S(done=i < 5, depth="d<1.5") for i in range(10)] + [S(done=True, depth="d3+") for _ in range(10)]
    cal = C.Calibration(samples, min_bucket=10)
    assert cal.fill("⚠️", "MEXC", "BestChange", "d<1.5")[0] == pytest.approx(6 / 12)
    assert cal.fill("⚠️", "MEXC", "BestChange", "d3+")[0] == pytest.approx(11 / 12)
    assert cal.fill("⚠️", "MEXC", "BestChange", "d?")[0] == pytest.approx(16 / 22)   # глубина неизвестна — корзина шире


def test_activation_min_n(monkeypatch):
    assert C.Calibration([S() for _ in range(29)]).ev(1.0, "same", "USDT", "MEXC", "BestChange") is None
    ev = C.Calibration([S() for _ in range(30)]).ev(1.0, "same", "USDT", "MEXC", "BestChange")
    assert ev is not None and ev > 0
    # факты сделок в число кругов не идут: 29 кругов + 50 фактов — не активна
    mixed = [S() for _ in range(29)] + [S(source="trade") for _ in range(50)]
    cal = C.Calibration(mixed)
    assert not cal.active and cal.n == 29 and cal.n_facts == 50
    assert cal.ev(1.0, "same", "USDT", "MEXC", "BestChange") is None
    assert C.Calibration([S() for _ in range(30)]).ev(None, "same", "USDT", "MEXC", "BestChange") is None
    monkeypatch.setenv("CAL_MIN_N", "5")
    monkeypatch.setenv("CAL_FAIL_COST", "1.25")
    assert C.settings() == {"min_n": 5, "fail_cost": 1.25}
    monkeypatch.setenv("CAL_MIN_N", "много")
    assert C.settings()["min_n"] == C.DEFAULT_MIN_N


def test_ev_formula_and_sign_cases():
    good = C.Calibration([S(0.0) for _ in range(30)])
    p = 31 / 32
    assert good.ev(1.0, "same", "USDT", "MEXC", "BestChange") == pytest.approx(p * 1.0 - (1 - p) * 0.5)
    assert good.ev(1.0, "same", "USDT", "MEXC", "BestChange") > 0
    # частые срывы и дорогой срыв — EV < 0 при плюсовом плане
    flaky = C.Calibration([S(0.0) for _ in range(10)] + [S(done=False) for _ in range(20)], fail_cost=2.0)
    assert flaky.ev(1.0, "same", "USDT", "MEXC", "BestChange") < 0
    # факт стабильно ниже плана — поправка съедает плановую прибыль
    biased = C.Calibration([S(-1.5) for _ in range(30)])
    assert biased.bias("same", "USDT", "MEXC", "BestChange")[0] == pytest.approx(-1.5)
    assert biased.ev(1.0, "same", "USDT", "MEXC", "BestChange") < 0
    # факт выше плана — EV выше плана
    lucky = C.Calibration([S(0.5) for _ in range(30)], fail_cost=0.0)
    assert lucky.ev(1.0, "same", "USDT", "MEXC", "BestChange") > 1.0 * 31 / 32


def test_fail_cost_observed_or_floor():
    base = [S(0.0) for _ in range(30)]
    assert C.Calibration(base + [S(done=False, realized=-2.0)]).fail_cost == pytest.approx(2.0)
    zero = C.Calibration(base + [S(done=False, realized=0.0)], fail_cost=0.5)
    assert zero.observed_fail_cost == 0.0 and zero.fail_cost == 0.5
    assert C.Calibration(base).observed_fail_cost is None and C.Calibration(base).fail_cost == C.DEFAULT_FAIL_COST
    assert C.Calibration(base + [S(done=False, realized=0.3)], fail_cost=0.0).fail_cost == 0.0   # плюс — не убыток


def test_paper_old_schema_readonly(tmp_path):
    """База прошлой версии: нет planned_raw/label/индекса/глубины — план берём planned_pct; файл не меняется."""
    cols = ("buy_ex TEXT", "buy_asset TEXT", "sell_ex TEXT", "sell_asset TEXT", "route TEXT", "planned_pct REAL",
            "realized_pct REAL", "result TEXT", "ts_start REAL", "ts_stage REAL")
    rows = [{k: v for k, v in row(realized=1.2).items() if k != "label"},
            {k: v for k, v in row("failed_buy", realized=0.0).items() if k != "label"},
            {k: v for k, v in row(None, realized=None).items() if k != "label"},          # открытый круг
            {k: v for k, v in row("strange").items() if k != "label"}]                   # непонятный итог
    path = make_paper(tmp_path / "old.db", rows, cols)
    before = pathlib.Path(path).read_bytes()
    samples = C.paper_samples(path)
    assert [s.done for s in samples] == [True, False]
    assert samples[0].diff == pytest.approx(0.2) and samples[0].rel == "—" and samples[0].depth == "d?"
    assert samples[0].duration == 600 and samples[1].diff is None and samples[1].realized == 0.0
    assert pathlib.Path(path).read_bytes() == before                                     # ни миграций, ни записи
    con = sqlite3.connect(path)
    assert "planned_raw" not in {r[1] for r in con.execute("PRAGMA table_info(cycles)")}
    con.close()


def test_paper_prefers_planned_raw_and_stage1_columns(tmp_path):
    cols = ("buy_ex TEXT", "buy_asset TEXT", "sell_ex TEXT", "sell_asset TEXT", "route TEXT", "planned_pct REAL",
            "planned_raw REAL", "realized_pct REAL", "result TEXT", "label TEXT", "ts_start REAL", "ts_stage REAL",
            "index_start INTEGER", "depth_margin REAL")
    r = row(planned=0.7, raw=1.0, realized=1.1)
    r.update(index_start=9, depth_margin=1.2)
    s, = C.paper_samples(make_paper(tmp_path / "p.db", [r], cols))
    assert s.diff == pytest.approx(0.1) and s.rel == "i8+" and s.depth == "d<1.5" and s.rtype == "same"


def test_missing_empty_and_broken_dbs(tmp_path):
    missing = tmp_path / "nope.db"
    assert C.paper_samples(str(missing)) == [] and C.trade_samples(str(missing)) == []
    assert not missing.exists()                                                          # файл не создан
    empty = make_paper(tmp_path / "empty.db", [])
    assert C.paper_samples(empty) == []
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE something (x INTEGER)")
    con.commit()
    con.close()
    assert C.paper_samples(str(other)) == [] and C.trade_samples(str(other)) == []
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 100)
    assert C.paper_samples(str(broken)) == [] and C.trade_samples(str(broken)) == []
    cal = C.build([str(missing), empty], trades_path=str(missing))
    assert cal.n == 0 and not cal.active and cal.global_bias == 0.0 and cal.median_duration is None
    assert cal.ev(1.0, "same", "USDT", "A", "B") is None
    text = C.report_text(cal, series=[])
    assert "не активна" in text and "Данных пока нет" in text


def test_live_default_paths_empty():
    """Живые пути (в тестах — пустая временная папка): баз нет — калибровка пустая, отчёт не падает."""
    cal = C.build()
    assert cal.n == 0 and cal.n_facts == 0
    assert "не активна" in C.report_text()
    assert not os.path.exists(paper.DB_PATH) and not os.path.exists(trades.DB_PATH)


def test_real_paper_schema(tmp_path):
    """Круги, записанные самим paper.py, читаются как есть."""
    path = str(tmp_path / "paper.db")
    b, s = make_ad("MEXC", "buy", 80.0), make_ad("BestChange", "sell", 84.0)
    cid = paper.start_cycle(10000, b, s, "перевод −0.1 USDT (BEP20) на BestChange", 4.5, path=path, ts=100.0,
                            label="✅ надёжно", over=frozenset(), planned_raw=4.8)
    paper.finish_cycle(cid, "done", 4.6, path=path, ts=700.0)
    cid = paper.start_cycle(10000, b, s, "через Bybit: перевод −1 USDT (POLYGON)", 3.0, path=path, ts=200.0,
                            over=frozenset())
    paper.finish_cycle(cid, "failed_sell", 0.0, path=path, ts=900.0)
    paper.start_cycle(10000, b, s, "перевод", 3.0, path=path, ts=300.0, over=frozenset())   # открыт
    done, failed = C.paper_samples(path)
    assert done.diff == pytest.approx(-0.2) and done.rel == "✅" and done.rtype == "same" and done.duration == 600
    assert not failed.done and failed.rtype == "relay" and failed.rel == "—"


def make_trades(path, facts, with_source=True):
    """Журнал сделок своей схемы: с колонкой fact_source (как сейчас) или без неё (база до этапа 2.5)."""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts REAL, route TEXT, buy_ex TEXT, buy_asset TEXT, "
                "sell_ex TEXT, sell_asset TEXT, amount REAL, profit REAL, fact REAL"
                + (", fact_source TEXT)" if with_source else ")"))
    for i, (asset, profit, fact, source) in enumerate(facts):
        cols = "ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit, fact"
        vals = [float(i), "перевод", "Bybit", asset, "MEXC", asset, 10000.0, profit, fact]
        if with_source:
            cols += ", fact_source"
            vals.append(source)
        con.execute(f"INSERT INTO trades ({cols}) VALUES ({', '.join('?' * len(vals))})", vals)
    con.commit()
    con.close()
    return str(path)


def test_trade_facts_fact_source(tmp_path, monkeypatch):
    monkeypatch.setenv("RISK_BUFFER", "BTC:1")
    facts = [("USDT", 1.0, 1.0, "plan"), ("USDT", 1.0, 1.1, "plan±"), ("USDT", 1.0, 0.5, "manual"),
             ("USDT", 2.0, 1.5, None), ("USDT", 1.0, None, None), ("BTC", 1.0, 2.0, "auto")]
    got = C.trade_samples(make_trades(tmp_path / "t.db", facts))
    assert [round(s.diff, 6) for s in got[:2]] == [-0.5, -0.5]
    raw_btc = ((1 + 1.0 / 100) / (1 - 1 / 100) - 1) * 100                      # расчёт без запаса 1%
    assert got[2].diff == pytest.approx(2.0 - raw_btc) and got[2].buy_asset == "BTC"
    assert all(s.source == "trade" and s.done for s in got)
    # колонки fact_source нет — берём все факты
    old = C.trade_samples(make_trades(tmp_path / "old.db", facts, with_source=False))
    assert len(old) == 5
    # факты — в поправку, но не в p и не в число кругов
    cal = C.Calibration(got + [S(0.0) for _ in range(30)])
    assert cal.n == 30 and cal.n_facts == 3 and cal.n_diffs == 33
    assert cal.fill("⚠️", "MEXC", "BestChange")[0] == pytest.approx(31 / 32)


def test_real_trades_schema(tmp_path):
    """Журнал, записанный самим trades.py: «как расчёт»/«±0.5 п.п.» не берём, введённый и найденный — берём."""
    path = str(tmp_path / "trades.db")
    d = (1.0, make_ad("Bybit", "buy", 80.0), make_ad("MEXC", "sell", 81.0), "перевод −1 USDT (TRC20) на MEXC")
    for fact, source in ((1.0, trades.FACT_PLAN), (1.5, trades.FACT_PLAN_SHIFT), (0.4, trades.FACT_MANUAL),
                         (0.7, trades.FACT_AUTO), (None, None)):
        trade_id = trades.log_trade(d, 10000, path=path, ts=1_790_000_000.0)[0]
        if fact is not None:
            trades.set_fact(trade_id, fact, path=path, source=source)
    got = C.trade_samples(path)
    assert [round(s.diff, 6) for s in got] == [-0.6, -0.3] and all(s.rtype == "same" for s in got)


def test_deal_ev_and_rank():
    samples = [S(-1.0) for _ in range(40)] + [S(0.0, bex="HTX", sex="BitPapa") for _ in range(40)]
    cal = C.Calibration(samples)
    bad = (2.0, make_ad("MEXC", "buy", 80.0), make_ad("BestChange", "sell", 82.0), "перевод на BestChange")
    good = (1.5, make_ad("HTX", "buy", 80.0), make_ad("BitPapa", "sell", 82.0), "перевод на BitPapa")
    assert C.deal_ev(cal, bad) < C.deal_ev(cal, good)
    assert C.deal_ev(cal, bad, planned_raw=3.0) > C.deal_ev(cal, bad)            # план без запаса важнее profit
    assert C.rank_deals(cal, [bad, good], labels=["⚠️ риск", "⚠️ риск"]) == [good, bad]
    assert C.rank_deals(cal, [bad, good]) == [good, bad]
    idle = C.Calibration(samples[:10])
    assert C.deal_ev(idle, bad) is None and C.rank_deals(idle, [bad, good]) == [bad, good]


def test_median_duration():
    cal = C.Calibration([S(duration=d) for d in (500.0, 600.0, 700.0)] + [S(done=False)])
    assert cal.median_duration == 600.0


def test_moves_p90_and_risk_suggestion(monkeypatch):
    # цена растёт на 1% каждые 2 шага по 300 с — за 600 с любое движение ровно 1%
    series = [(i * 300.0, 100 * 1.01 ** (i // 2)) for i in range(60)]
    p90, pairs = C.moves_p90(series + [(0.0, 100.0)], 600)                      # дубль ts — одна точка
    assert p90 == pytest.approx(1.0) and pairs == 58
    assert C.moves_p90([(0, 100.0), (10_000, 120.0)], 600) == (None, 0)          # дыра — не движение
    assert C.moves_p90([(i * 300.0, 100.0) for i in range(10)], 600)[0] is None  # мало пар
    got = C.risk_buffer_suggestion("USDT", series, 600, current=0.0)
    assert got["suggest_pct"] == pytest.approx(1.0) and got["pairs"] == 58 and got["horizon_min"] == 10
    monkeypatch.setenv("RISK_BUFFER", "BTC:0.3,USDT:0.2")
    assert C.risk_buffer_suggestion("USDT", series, 600)["current_pct"] == 0.2
    assert C.risk_buffer_suggestion("USDT", series, None) is None
    assert C.risk_buffer_suggestion("USDT", [], 600) is None


def test_usdt_rub_series(tmp_path):
    path = str(tmp_path / "history.db")
    assert C.usdt_rub_series(path) == [] and not os.path.exists(path)
    history._insert([(200.0, "Bybit", "MEXC", "USDT", "USDT", 1.0, 81.5), (100.0, "HTX", "MEXC", "USDT", "USDT", 1.0, 81.0),
                     (300.0, "HTX", "MEXC", "USDT", "USDT", 1.0, None)], path)
    assert C.usdt_rub_series(path) == [(100.0, 81.0), (200.0, 81.5)]


def test_report_text_content():
    samples = ([S(0.1, duration=540.0) for _ in range(12)] + [S(done=False, realized=0.0)]
               + [S(-0.3, bex="<b>X", sex="BitPapa") for _ in range(3)])
    text = C.report_text(C.Calibration(samples), series=[])
    assert "Кругов прогона: 16 (исполнилось 15, сорвалось 1)" in text
    assert "⏳ EV не активна: 16 из 30" in text
    assert "same USDT MEXC→BestChange: 12 · +0.10" in text
    assert "&lt;b&gt;X→BitPapa" in text and "<b>X" not in text
    assert "⚠️ MEXC→BestChange: 13 · 87%" in text                               # (12 + 1) / (13 + 2)
    assert "мало — берётся корзина грубее" in text
    active = C.report_text(C.Calibration([S(0.0) for _ in range(30)], min_n=30),
                           series=[(i * 300.0, 100 * 1.01 ** (i // 2)) for i in range(60)])
    assert "✅ EV активна" in active and "Запас на курс USDT" not in active     # нет времени круга — нет подсказки


class Stub(B.Bot):
    def __init__(self, guests=()):
        super().__init__(None, "x", "1", p2p.Config())
        self.guests = set(guests)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.out)}}


def texts(bot):
    return [(p["chat_id"], p["text"]) for m, p in bot.out if m == "sendMessage"]


def msg(chat, text):
    return {"message": {"chat": {"id": chat}, "text": text, "from": {"first_name": "Гость"}}}


def test_calibration_command_owner_only(monkeypatch):
    monkeypatch.setattr(C, "report_text", lambda *a, **k: "CAL-REPORT")
    monkeypatch.setenv("CALIBRATION", "1")
    bot = Stub(guests=["42"])
    asyncio.run(bot.on_update(msg(42, "/calibration")))
    assert texts(bot) == [("42", B.GUEST_DENIED)]                                # гостю — отказ, отчёта нет
    asyncio.run(bot.handle("/calibration"))
    assert texts(bot)[-1] == ("1", "CAL-REPORT")
    assert "/calibration" not in B.GUEST_CMDS


def test_calibration_command_off_by_default(monkeypatch):
    monkeypatch.setattr(C, "report_text", lambda *a, **k: "CAL-REPORT")
    monkeypatch.delenv("CALIBRATION", raising=False)
    assert not C.enabled()
    bot = Stub()
    asyncio.run(bot.handle("/calibration"))
    assert texts(bot) and all(t != "CAL-REPORT" for _, t in texts(bot))
