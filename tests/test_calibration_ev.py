"""План 2.6 «калибровка и ожидаемая прибыль»: p исполнения по корзинам (мерчанты, Лаплас, нейтральный априор),
признаки связки скана, порядок по EV при EV_RANK=1 — в боте (/top, /best, сигналы, «EV … (p=…)» в карточке) и в
replay.py (--set ev_rank=1); EV_RANK=0 — вывод тот же и базы не читаются; запас на курс по монетам из снимков против
RISK_BUFFER и хеджа — только в /calibration."""
import asyncio
import inspect
import logging
import os
import pathlib
import re
import threading
import time

import pytest

import bot as B
import calibration as C
import cards
import p2p
import paper
import replay
import simperp
import snapshots
from helpers import make_ad
from test_bot import Stub
from test_replay import _cfg, _saved_scan

PNG = b"\x89PNG\r\n\x1a\n"


def S(diff=0.0, done=True, bex="MEXC", sex="BestChange", rel="⚠️", depth="d?", merch="m?", asset="USDT",
      rtype="same", duration=None):
    return C.Sample("paper", rtype, asset, bex, sex, done, diff if done else None, None, rel, depth, duration, merch)


def ad(ex, side, price, orders=1000, rate=100.0, avail=10000, asset="USDT"):
    return p2p.Ad(ex, side, price, 1000, 500000, avail, ["T-Bank"], f"{ex}-{side}", orders, rate, "", asset, "", "")


def deal_x():
    """Лучшая по оценке (3%), но её класс Bybit→MEXC стабильно теряет 5 п.п. к плану."""
    return 3.0, ad("Bybit", "buy", 85.0), ad("MEXC", "sell", 88.0), "перевод на MEXC"


def deal_y():
    return 2.0, ad("HTX", "buy", 86.0), ad("KuCoin", "sell", 88.0), "перевод на KuCoin"


KX, KY = ("Bybit", "USDT", "MEXC", "USDT"), ("HTX", "USDT", "KuCoin", "USDT")


def snap_xy():
    return p2p.Snapshot(87.0, "test", {"USDT": 87.0}, {}, [deal_x(), deal_y()], {}, {}, {})


def cal_xy():
    """Круги с индексом 8+ и сильными мерчантами — как у связок X и Y (ни одной причины риска)."""
    return C.Calibration([S(-5.0, bex="Bybit", sex="MEXC", rel="i8+", merch="m+") for _ in range(40)]
                         + [S(0.0, bex="HTX", sex="KuCoin", rel="i8+", merch="m+") for _ in range(40)])


# --- p исполнения ----------------------------------------------------------------------------------------------

def test_fill_merchant_buckets_laplace_and_neutral_prior():
    samples = [S(done=i < 8, merch="m-") for i in range(10)] + [S(done=True, merch="m+") for _ in range(10)]
    cal = C.Calibration(samples, min_bucket=10)
    assert cal.fill("⚠️", "MEXC", "BestChange", "d?", "m-") == (pytest.approx(9 / 12),
                                                                ("rvdm", "⚠️", "MEXC→BestChange", "d?", "m-"), 10)
    assert cal.fill("⚠️", "MEXC", "BestChange", "d?", "m+")[0] == pytest.approx(11 / 12)
    # мерчанты неизвестны — корзина без них (все 20 кругов этих площадок)
    assert cal.fill("⚠️", "MEXC", "BestChange") == (pytest.approx(19 / 22), ("rvd", "⚠️", "MEXC→BestChange", "d?"), 20)
    # площадки новые, мерчанты известны — «надёжность × мерчанты» раньше «только надёжность»
    assert cal.fill("⚠️", "Bybit", "HTX", "d?", "m-") == (pytest.approx(9 / 12), ("rm", "⚠️", "m-"), 10)
    # мало кругов и во всех — нейтральный априор, а не доля по трём сорванным
    few = C.Calibration([S(done=False) for _ in range(3)], min_bucket=10)
    assert few.fill("⚠️", "MEXC", "BestChange") == (0.5, ("prior",), 3)
    assert C.Calibration([S(done=False) for _ in range(10)]).fill("⚠️", "MEXC", "BestChange")[0] == pytest.approx(1 / 12)


def test_merchant_bucket():
    assert C.merchant_bucket(None, 99.0) == "m?" and C.merchant_bucket(500, None) == "m?"
    assert C.merchant_bucket(300, 97.0) == "m+" and C.merchant_bucket(299, 100.0) == "m-"
    assert C.merchant_bucket(5000, 96.9) == "m-"


def test_paper_samples_read_merchant_stats(tmp_path):
    path = str(tmp_path / "paper.db")
    strong, weak = make_ad("MEXC", "buy", 80.0, orders=900, rate=99.0), make_ad("MEXC", "buy", 80.0, orders=50)
    sell = make_ad("BestChange", "sell", 84.0, orders=400, rate=98.0)
    for b in (strong, weak):
        cid = paper.start_cycle(10000, b, sell, "перевод", 4.0, path=path, ts=100.0, over=frozenset(), planned_raw=4.0)
        paper.finish_cycle(cid, "done", 4.0, path=path, ts=700.0)
    assert [s.merch for s in C.paper_samples(path)] == ["m+", "m-"]


# --- признаки связки и порядок по EV ---------------------------------------------------------------------------

def test_deal_inputs_match_what_paper_stores():
    cfg = p2p.Config(risk_buffer={"BTC": 1.0})
    b, s = ad("Bybit", "buy", 85.0, orders=250), ad("MEXC", "sell", 88.0, rate=99.0, avail=500)
    snap = p2p.Snapshot(87.0, "t", {"USDT": 87.0}, {}, [], {}, {}, {},
                        groups={("Bybit", "buy", "USDT"): [b], ("MEXC", "sell", "USDT"): [s]})
    d = (2.0, b, s, "перевод")
    f = C.deal_inputs(d, cfg, snap)
    assert f["planned_raw"] == 2.0                                     # у USDT запаса нет — план тот же
    assert f["depth"] == paper.depth_margin(snap, b, s, cfg.amount, s.avail) and f["depth"] is not None
    assert f["merch"] == "m-" and f["index"] == p2p.reliability_index(d, cfg, snap)
    btc = (1.0, ad("Bybit", "buy", 9e6, asset="BTC"), ad("MEXC", "sell", 9.1e6, asset="BTC"), "перевод")
    assert C.deal_inputs(btc, cfg, snap)["planned_raw"] == pytest.approx((1.01 / 0.99 - 1) * 100)


def test_rank_snapshot_orders_by_ev_keeps_the_set():
    cfg, cal, snap = p2p.Config(), cal_xy(), snap_xy()
    before = list(snap.deals)
    assert C.rank_snapshot(cal, snap, cfg) is snap
    assert [d[1].ex for d in snap.deals] == ["HTX", "Bybit"] and sorted(map(id, snap.deals)) == sorted(map(id, before))
    ev_y, p_y = snap.ev[KY]
    assert ev_y == pytest.approx(C.deal_estimate(cal, deal_y(), cfg, snap)["ev"]) and p_y == pytest.approx(41 / 42)
    assert snap.ev[KX][0] < 0 < ev_y
    # EV = p × (план + поправка) − (1 − p) × цена срыва: поправка X = 40/60 × (−5) + 20/60 × 0 (сосед Y)
    assert snap.ev[KX][0] == pytest.approx(41 / 42 * (3.0 - 40 / 60 * 5) - 1 / 42 * 0.5)
    # калибровка не активна — порядок и EV не трогаем
    idle = snap_xy()
    C.rank_snapshot(C.Calibration(cal.samples[:10]), idle, cfg)
    assert [d[1].ex for d in idle.deals] == ["Bybit", "HTX"] and idle.ev == {}
    # равные EV — прежний порядок (score)
    tie = C.Calibration([S(0.0, rtype="cross", asset="ETH") for _ in range(30)])
    same = p2p.Snapshot(87.0, "t", {"USDT": 87.0}, {}, [(2.0, *deal_x()[1:]), (2.0, *deal_y()[1:])], {}, {}, {})
    C.rank_snapshot(tie, same, cfg)
    assert [d[1].ex for d in same.deals] == ["Bybit", "HTX"] and same.ev[KX] == pytest.approx(same.ev[KY])


def test_card_top_and_chart_show_ev_only_when_ranked(monkeypatch):
    cfg, snap = p2p.Config(), snap_xy()
    plain = p2p.fmt_signal(deal_x(), cfg, snap) + p2p.fmt_deal(deal_x(), cfg, snap) + p2p.fmt_top(snap, cfg)
    assert "EV" not in plain
    snap.ev = {KX: (-0.34, 0.976)}
    assert "📐 EV -0.34% (p=0.98)" in p2p.fmt_signal(deal_x(), cfg, snap)
    assert "📐 EV -0.34% (p=0.98)" in p2p.fmt_deal(deal_x(), cfg, snap)
    assert p2p.fmt_top(snap, cfg).count("📐 EV") == 1                  # у Y своего EV нет — строки нет
    seen = []
    monkeypatch.setattr(cards, "fmt_ev", lambda ev: seen.append(ev) or "EV")
    assert cards.top_chart(snap, cfg)[:8] == PNG and seen == [(-0.34, 0.976)]


def test_snapshot_stores_ev_only_when_ranked():
    cfg, snap = p2p.Config(), snap_xy()
    assert all("ev" not in d for d in snapshots.collect(snap, cfg)["scan"]["deals"])
    C.rank_snapshot(cal_xy(), snap, cfg)
    first = snapshots.collect(snap, cfg)["scan"]["deals"][0]
    assert first["buy"]["ex"] == "HTX" and first["ev"] == list(snap.ev[KY])


def test_ev_rank_env_flag(monkeypatch):
    monkeypatch.delenv("EV_RANK", raising=False)
    assert p2p.Config.from_env().ev_rank is False and p2p.Config().ev_rank is False
    monkeypatch.setenv("EV_RANK", "1")
    assert p2p.Config.from_env().ev_rank is True


# --- бот ------------------------------------------------------------------------------------------------------

def _bot(monkeypatch, ev_rank=False, calls=None):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "top_chart", lambda snap, c: b"png")
    monkeypatch.delenv("PAPER", raising=False)
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)

    def build(*a, **k):
        if calls is not None:
            calls.append(threading.get_ident())
        return cal_xy()
    monkeypatch.setattr(C, "build", build)

    async def fake_scan(s, cfg, force_alt=False):
        return snap_xy()
    monkeypatch.setattr(B, "scan", fake_scan)
    bot = Stub(p2p.Config(min_profit=1.0, ev_rank=ev_rank))
    bot.live_scans = 1
    return bot


def _outputs(bot, snap):
    bot.next_deal_id = 1   # id кнопок — от времени запуска бота; у обоих ботов одинаковые
    asyncio.run(bot.notify(snap))
    asyncio.run(bot.show_top(snap))
    asyncio.run(bot.show_best(snap))
    return bot.out


def captions(bot):
    return [p["caption"] for m, p in bot.out if m == "sendPhoto"]


def test_ev_rank_off_output_identical_and_no_db_reads(monkeypatch):
    """EV_RANK=0 (по умолчанию): калибровка активна, но снимок после скана тот же, вывод бота — байт в байт как без
    этой задачи (тот же снимок мимо fresh_scan), базы калибровки не читаются."""
    calls = []
    baseline = _outputs(_bot(monkeypatch, calls=calls), snap_xy())
    bot = _bot(monkeypatch, calls=calls)
    snap = asyncio.run(bot.fresh_scan())
    assert snap.deals == snap_xy().deals and snap.ev == {}
    assert _outputs(bot, snap) == baseline and baseline
    assert p2p.fmt_top(snap, bot.cfg) == p2p.fmt_top(snap_xy(), bot.cfg)
    assert calls == [] and bot.cal is None
    assert all("EV" not in (p.get("caption") or p.get("text") or "") for _, p in baseline)


def test_ev_rank_on_orders_signals_top_and_card(monkeypatch):
    calls = []
    bot = _bot(monkeypatch, ev_rank=True, calls=calls)
    bot.max_signals = 1
    snap = asyncio.run(bot.fresh_scan())
    assert [d[1].ex for d in snap.deals] == ["HTX", "Bybit"]
    assert calls and calls[0] != threading.get_ident()                 # базы читаются не в цикле событий
    asyncio.run(bot.notify(snap))
    (cap,) = captions(bot)                                             # единственный слот — лучшей по EV, не по оценке
    assert "HTX → KuCoin" in cap and "📐 " + p2p.fmt_ev(snap.ev[KY]) in cap
    asyncio.run(bot.show_best(snap))
    assert "HTX → KuCoin" in captions(bot)[-1]
    top = p2p.fmt_top(snap, bot.cfg)
    assert top.index("HTX") < top.index("Bybit") and top.count("📐 EV") == 2
    # второй скан в пределах calibration.REFRESH — калибровка из памяти; позже — пересборка
    asyncio.run(bot.fresh_scan())
    assert len(calls) == 1
    bot.cal_ts -= C.REFRESH
    asyncio.run(bot.fresh_scan())
    assert len(calls) == 2


def test_ev_rank_failure_keeps_scan_order(monkeypatch, caplog):
    bot = _bot(monkeypatch, ev_rank=True)
    monkeypatch.setattr(C, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    with caplog.at_level(logging.WARNING):
        snap = asyncio.run(bot.fresh_scan())
    assert [d[1].ex for d in snap.deals] == ["Bybit", "HTX"] and snap.ev == {}
    assert "ev rank" in caplog.text


def test_scan_loop_and_calc_rank_before_use(monkeypatch):
    bot = _bot(monkeypatch, ev_rank=True)
    bot.chat_id = ""   # без чата — ни алертов, ни сигналов
    monkeypatch.setattr(B.history, "record", lambda *a, **k: False)

    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(B.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.scan_loop())
    assert [d[1].ex for d in bot.last.deals] == ["HTX", "Bybit"] and KY in bot.last.ev
    seen = []

    async def calc_scan(s, cfg, force_alt=False):
        seen.append((cfg.amount, force_alt))
        return snap_xy()
    monkeypatch.setattr(B, "scan", calc_scan)
    bot.chat_id = "1"
    asyncio.run(bot.calc("20000"))
    assert seen == [(20000, True)] and "HTX → KuCoin" in captions(bot)[-1]


def test_calibration_command_in_thread_with_ev_rank(monkeypatch):
    seen = {}

    def report(**kw):
        seen.update(kw, thread=threading.get_ident())
        return "CAL-REPORT"
    monkeypatch.setattr(C, "report_text", report)
    monkeypatch.setenv("CALIBRATION", "1")
    bot = _bot(monkeypatch, ev_rank=True)
    bot.last = snap_xy()
    asyncio.run(bot.handle("/calibration"))
    assert seen["ev_rank"] is True and seen["snap"] is bot.last and seen["thread"] != threading.get_ident()
    assert ("1", "CAL-REPORT") in [(p["chat_id"], p["text"]) for m, p in bot.out if m == "sendMessage"]


# --- запас на курс из данных против хеджа ------------------------------------------------------------------------

def _snapshots_db(path, n=60, step=300.0):
    t0 = time.time() - n * step
    cfg = p2p.Config()
    for i in range(n):
        snap = p2p.Snapshot(90.0, "t", {"USDT": 90.0, "BTC": 9_000_000.0 * 1.01 ** (i // 2)}, {}, [], {}, {}, {},
                            ts=t0 + i * step)
        snapshots.save(snap, cfg, path=path)
    return t0


def test_coin_series_from_snapshots_readonly(tmp_path):
    path = str(tmp_path / "snapshots.db")
    t0 = _snapshots_db(path)
    before = pathlib.Path(path).read_bytes()
    got = C.coin_series(path, step=300)
    assert set(got) == {"USDT", "BTC"} and len(got["BTC"]) == 60 and got["USDT"][0] == (pytest.approx(t0), 90.0)
    assert C.moves_p90(got["BTC"], 600)[0] == pytest.approx(1.0)      # за 10 мин — ровно 1%
    assert pathlib.Path(path).read_bytes() == before                    # только чтение
    assert len(C.coin_series(path, step=600)["BTC"]) == 30              # не чаще step
    last = C.coin_series(path, step=1, limit=10)["BTC"]
    assert len(last) == 10 and last[-1][0] == pytest.approx(t0 + 59 * 300)   # самые свежие
    missing = tmp_path / "nope.db"
    assert C.coin_series(str(missing)) == {} and not missing.exists()
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database" * 100)
    assert C.coin_series(str(broken)) == {}


def _paper_with_hedges(path):
    con = paper._connect(path)
    closed = ('{"status": "closed", "ref_close": 90.0, "spread_usdt": 0.2, "exp_cost_pct": 0.3, "pnl_pct": -0.5, '
              '"ratio": 1.0}')
    rows = [("BTC", 10000.0, 0.5, 0.1, closed), ("BTC", 10000.0, 0.1, 0.0, '{"status": "open", "ratio": 1.0}'),
            ("ETH", 10000.0, None, None, '{"status": "none", "note": "лот"}'), ("TON", 10000.0, 1.0, 0.0, "{битый")]
    with con:
        con.executemany("INSERT INTO cycles (buy_asset, amount, hedge_fees, hedge_funding, hedge_state, result) "
                        "VALUES (?, ?, ?, ?, ?, 'done')", rows)
    con.close()
    return path


def test_hedge_costs_same_formula_as_paper_report(tmp_path):
    path = _paper_with_hedges(str(tmp_path / "paper.db"))
    got = C.hedge_costs(path)
    fact = (0.5 + 0.2 - 0.1) * 90 / 10000 * 100
    assert got == {"BTC": {"n": 1, "exp": pytest.approx(0.3), "fact": pytest.approx(fact)}}
    assert simperp.report(path)["fact_cost"] == pytest.approx(fact)     # /paper report считает так же
    assert C.hedge_costs(str(tmp_path / "nope.db")) == {}


def test_hedge_now_uses_live_perp_model(monkeypatch):
    snap = p2p.Snapshot(90.0, "t", {"USDT": 90.0, "BTC": 9_000_000.0}, {}, [], {}, {}, {})
    assert C.hedge_now(None) == {} and C.hedge_now(snap, 10000) == {}   # котировок перпов в тестах нет
    calls = []
    monkeypatch.setattr(simperp, "choose", lambda asset, qty, amount, ref, rub, **k:
                        calls.append((asset, qty, amount, ref, rub)) or ({"cost_pct": 0.12, "venue": "Bybit"}, ""))
    assert C.hedge_now(snap, 10000) == {"BTC": (0.12, "Bybit")}         # ETH/TON без курса — мимо
    assert calls == [("BTC", 10000 / 9_000_000.0, 10000, 90.0, 9_000_000.0)]


def test_fx_rows_and_report(monkeypatch):
    monkeypatch.delenv("RISK_BUFFER", raising=False)
    series = [(i * 300.0, 100 * 1.01 ** (i // 2)) for i in range(60)]
    closed = {"BTC": {"n": 2, "exp": 0.3, "fact": 0.25}}
    rows = C.fx_rows(600, {"BTC": series, "ETH": [(0.0, 1.0)]}, usdt=series, now={"BTC": (0.1, "Bybit")},
                     closed=closed)
    assert [r["asset"] for r in rows] == ["USDT", "BTC"]                # ETH: ни пар, ни хеджа
    usdt, btc = rows
    assert usdt["p90"] == pytest.approx(1.0) and usdt["pairs"] == 58 and usdt["current"] == 0.0
    assert btc["current"] == 0.3 and btc["now"] == (0.1, "Bybit") and btc["paper"] == closed["BTC"]
    cal = C.Calibration([S(0.0, duration=600.0) for _ in range(30)])
    text = C.report_text(cal, series=series, coins={"BTC": series}, hedges=({"BTC": (0.1, "Bybit")}, closed),
                         ev_rank=False)
    assert "Запас на курс из данных" in text and "(10 мин)" in text
    assert ("• BTC: p90 1.00% (пар 58) · RISK_BUFFER 0.3% · хедж сейчас 0.10% (Bybit) · хедж по кругам 0.25% "
            "(ждали 0.30%, 2)") in text
    assert "• USDT: p90 1.00% (пар 58) · RISK_BUFFER 0%" in text and "EV_RANK=0" in text
    few = C.report_text(cal, series=[], coins={"ETH": [(0.0, 1.0)]}, hedges=({}, closed), ev_rank=True)
    assert "• BTC: мало данных (пар 0)" in few and "EV_RANK=1" in few and "пока калибровка" not in few
    idle = C.report_text(C.Calibration([S(0.0) for _ in range(3)]), series=[], ev_rank=True)
    assert "пока калибровка не активна" in idle and "Запас на курс" not in idle


def test_settings_documented_in_env_example():
    names = set(re.findall(r"(?:getenv|_env_num|_flag)\(\"([A-Z][A-Z_]+)\"", inspect.getsource(C)))
    assert {"CALIBRATION", "CAL_MIN_N", "CAL_FAIL_COST", "CAL_SHRINK_K", "CAL_MIN_BUCKET"} <= names
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".env.example"), encoding="utf-8") as f:
        documented = set(re.findall(r"^([A-Z_]+)=", f.read(), re.M))
    assert (names | {"EV_RANK"}) - documented == set()


# --- replay.py --set ev_rank=1 --------------------------------------------------------------------------------

def _replay_cal(scan):
    """Все круги связки, первой в сигналах A, сорвались; остальные корзины — пополам. Поправки ни у кого нет."""
    cfg = replay.cfg_of(scan)
    first = replay.signals(replay.build_side(scan, cfg), cfg, 1)[0]
    venue = (first[1].ex, first[2].ex)
    return C.Calibration([S(done=False, bex=venue[0], sex=venue[1], rel="x") for _ in range(40)]
                         + [S(0.0, bex="A", sex="B", rel="x", rtype="cross", asset="ZZZ") for _ in range(40)]), first


def test_replay_set_ev_rank_changes_signals_not_the_set(offline):
    assert replay.override(p2p.Config(), ["ev_rank=1"]).ev_rank is True
    scan, _snap = _saved_scan(_cfg())
    cal, first = _replay_cal(scan)
    res = replay.compare([scan], ["ev_rank=1"], cal=cal, top=1)
    assert res["a"]["deals"] == res["b"]["deals"] and res["lost"] == res["new"] == 0   # набор связок тот же
    assert res["sig_changed"] == 1 and res["a"]["sig"] == res["b"]["sig"] == 1
    assert res["b"]["sig_ev"][0] > res["a"]["sig_ev"][0]                # по EV — лучше по EV
    text = replay.fmt_summary(res)
    assert "сред. EV, %" in text and "другой набор сигналов в 1 из 1 снимков" in text and "не активна" not in text
    same = replay.compare([scan], [], cal=cal, top=1)                    # без правки — B = A
    assert same["sig_changed"] == 0 and same["a"]["sig_ev"] == same["b"]["sig_ev"]


def test_replay_ev_rank_with_idle_calibration(offline):
    scan, _snap = _saved_scan(_cfg())
    res = replay.compare([scan], ["ev_rank=1"])                          # живые базы пусты — калибровка не активна
    assert res["sig_changed"] == 0 and res["a"]["sig_ev"] == res["b"]["sig_ev"] == []
    assert "калибровка не активна — 0 из 30" in replay.fmt_summary(res)
