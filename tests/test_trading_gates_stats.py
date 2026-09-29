"""scripts/trading_gates_stats.py: статистика хеджа для порогов — бумага из paper.db (по копии), бэктест из research.report,
запись data/gates_paper.json и data/gates_backtest.json ровно в формате trading.gates, вердикт по порогам. Всё офлайн:
research.report подменяет runner-заглушка, базы — во временной папке, «папка бота» — свой корень с research/ и индексом git."""
import hashlib
import json
import math
import os
import sqlite3
import sys
import time

import pytest

import paper
import simperp
from helpers import make_ad
from research import hedge_bt as hb
from trading import gates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import trading_gates_stats as tgs  # noqa: E402

DAY = 86400
NOW = time.time()
HEDGE = "hedge"
PAPER_OK = {"days": 14, "count": 50, "ratio_ok_share": 0.95, "cost_to_buffer": 0.6, "sigma_ratio": 0.5}
BT_OK = dict(PAPER_OK)
BT_WEAK = dict(PAPER_OK, cost_to_buffer=0.61)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(gates, "_FLAGS", {"short_paper": False})
    for name in ("HEDGE_ASSETS", "HEDGE_MIN_AMOUNT_RUB", "RISK_BUFFER"):
        monkeypatch.delenv(name, raising=False)


# --- «папка бота»: research/, data/, индекс git ------------------------------------------------------------------------

def git_index(paths):
    """Индекс git v2 (index-format.txt): записи по 62 байта + путь с выравниванием до 8, SHA-1 в конце."""
    out = bytearray(b"DIRC" + (2).to_bytes(4, "big") + len(paths).to_bytes(4, "big"))
    for path in sorted(paths):
        name = path.encode()
        head = bytes(24) + (0o100644).to_bytes(4, "big") + bytes(12) + hashlib.sha1(name).digest() + \
            min(len(name), 0xFFF).to_bytes(2, "big")
        size = ((62 + len(name) + 8) // 8) * 8
        out += head + name + b"\0" * (size - 62 - len(name))
    return bytes(out) + hashlib.sha1(bytes(out)).digest()


def bot_root(tmp_path):
    root = tmp_path / "bot"
    (root / "research").mkdir(parents=True)
    (root / "research" / "hedge_bt.py").write_bytes(b"X = 1\n")
    (root / "data").mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "index").write_bytes(git_index(["research/hedge_bt.py", "README.md"]))
    return str(root)


def write_stats(root, paper_stats=None, bt_stats=None, ts=None):
    """Файлы статистики руками (как их пишет скрипт), чтобы сверить вердикт с gates.max_mode."""
    ts = NOW - 60 if ts is None else ts
    if paper_stats is not None:
        with open(os.path.join(root, "data", "gates_paper.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "generated_at": ts, "strategies": {HEDGE: paper_stats}}, f)
    if bt_stats is not None:
        with open(os.path.join(root, "data", "gates_backtest.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "generated_at": ts, "research_sha": gates.research_sha(root),
                       "strategies": {HEDGE: bt_stats}}, f)


def load(root):
    p, why_p = gates.load_paper(HEDGE, root=root)
    b, why_b = gates.load_backtest(HEDGE, root=root)
    return p, why_p, b, why_b


def data_file(root, name):
    with open(os.path.join(root, "data", name), encoding="utf-8") as f:
        return json.load(f)


def run_main(root, *extra, runner=None):
    return tgs.main(["--bot-dir", root, *extra], runner=runner, env_loader=lambda path: None, now=NOW)


# --- бумажная база ---------------------------------------------------------------------------------------------------

def _hedge(db, ts_open, asset="BTC", amount=10000, x=0.0, closed=True, fees=0.1):
    """Круг с хеджем (как tests/test_hedge_gates.py): план 1%, факт 1 + x, хедж вернул −0.9·x п.п."""
    b = make_ad("Bybit", "buy", 6_000_000.0, asset=asset)
    cid = paper.start_cycle(amount, b, make_ad("Bybit", "sell", 90.0), "спот", 1.0, path=db, ts=ts_open, planned_raw=1.0)
    st = {"status": "closed" if closed else "open", "ts_open": ts_open, "ratio": 1.0, "exp_cost_pct": 0.0,
          "ref_close": 90.0, "spread_usdt": 0.0}
    if closed:
        st["pnl_pct"] = -0.9 * x
    con = sqlite3.connect(db)
    con.execute("UPDATE cycles SET hedge_state = ?, hedge_qty = 0.0015, hedge_fees = ?, hedge_funding = 0.0, "
                "result = ?, realized_pct = ? WHERE id = ?",
                (json.dumps(st), fees, "done" if closed else None, 1.0 + x if closed else None, cid))
    con.commit()
    con.close()


def fill(db, n, days, **kw):
    for i in range(n):
        _hedge(db, NOW - days * DAY + i * 600, x=(1.0 if i % 2 else -1.0), **kw)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --- разбор настроек и чистые функции ---------------------------------------------------------------------------------

def test_parse_min_amounts():
    assert tgs.parse_min_amounts(None) == ({"ETH": 20000.0}, [])
    assert tgs.parse_min_amounts("") == ({"ETH": 20000.0}, [])
    assert tgs.parse_min_amounts("eth:5000, TON:1000") == ({"ETH": 5000.0, "TON": 1000.0}, [])
    got, bad = tgs.parse_min_amounts("ETH:abc,BTC:nan,:5,TON,SOL:-1,BTC:300")
    assert got == {"BTC": 300.0} and bad == ["ETH:abc", "BTC:nan", ":5", "TON", "SOL:-1"]
    assert tgs.parse_min_amounts("garbage") == ({"ETH": 20000.0}, ["garbage"])   # ни одной понятной — умолчание


def test_build_paper_writes_zero_and_omits_missing():
    empty = simperp.gate_stats(os.path.join(os.sep, "no", "such", "paper.db"), now=NOW)
    assert empty["count"] == 0 and empty["days"] == 0.0 and empty["ratio_ok_share"] is None
    doc = tgs.build_paper(empty, NOW)
    assert doc == {"version": 1, "generated_at": NOW, "strategies": {HEDGE: {"count": 0, "days": 0.0}}}
    junk = {"count": 5, "days": 3.0, "ratio_ok_share": float("nan"), "cost_to_buffer": "0.1", "sigma_ratio": True,
            "pairs": 4}
    assert tgs.build_paper(junk, NOW)["strategies"][HEDGE] == {"count": 5, "days": 3.0}
    full = {"count": 7, "days": 2.5, "ratio_ok_share": 0.9, "cost_to_buffer": 0.2, "sigma_ratio": 0.3, "pairs": 6}
    assert tgs.build_paper(full, NOW)["strategies"][HEDGE] == {"count": 7, "days": 2.5, "ratio_ok_share": 0.9,
                                                               "cost_to_buffer": 0.2, "sigma_ratio": 0.3}
    assert json.dumps(tgs.build_paper(junk, NOW), allow_nan=False)   # в файл не попадает NaN


def test_paper_stats_filters_cycles_and_never_touches_source(tmp_path):
    db = str(tmp_path / "paper.db")
    fill(db, 3, 5, asset="BTC", amount=10000)
    fill(db, 4, 5, asset="ETH", amount=10000)          # ETH меньше 20 000 ₽ — не хеджируется
    fill(db, 2, 5, asset="ETH", amount=20000)          # от 20 000 — в счёт
    fill(db, 2, 5, asset="ETH", amount=19999.5)        # чуть меньше — нет
    fill(db, 2, 5, asset="SOL", amount=50000)          # монеты нет в HEDGE_ASSETS
    before = sha(db)
    st, info = tgs.paper_stats(db, ["BTC", "ETH", "TON"], {"ETH": 20000.0}, now=NOW)
    assert info == {"db": True, "kept": 5, "dropped": 8} and st["count"] == 5
    assert sha(db) == before                             # оригинал не менялся
    st2, info2 = tgs.paper_stats(db, ["BTC", "ETH", "TON"], {}, now=NOW)   # без минимума ETH — все ETH в счёт
    assert info2["kept"] == 11 and st2["count"] == 11
    st3, info3 = tgs.paper_stats(db, ["BTC"], {"ETH": 20000.0}, now=NOW)
    assert info3["kept"] == 3 and st3["count"] == 3


def test_paper_stats_missing_db_is_not_created(tmp_path):
    db = str(tmp_path / "sub" / "paper.db")
    st, info = tgs.paper_stats(db, ["BTC"], {}, now=NOW)
    assert info == {"db": False, "kept": 0, "dropped": 0} and st["count"] == 0 and st["days"] == 0.0
    assert not os.path.exists(db) and not os.path.exists(os.path.dirname(db))


# --- бумага целиком: main -> файл -> gates -----------------------------------------------------------------------------

def test_main_without_paper_db_writes_honest_zero(tmp_path, capsys):
    root = bot_root(tmp_path)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert not os.path.exists(os.path.join(root, "data", "paper.db"))          # чужую базу не заводим
    assert not os.path.exists(os.path.join(root, "data", "gates_backtest.json"))
    p, why_p, b, why_b = load(root)
    assert p is not None, why_p
    assert p.stats == {"count": 0, "days": 0.0}                                # ничего не выдумано
    assert b is None and "нет локального файла" in why_b
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "paper"
    assert "count 0" in out and "нет данных" in out and "paper: только бумага" in out
    assert "Бэктеста нет" in out and "gates_live.json" in out and "TRADING_SHORT_PAPER=1" in out
    assert "50 USDT" in out


def test_main_below_thresholds_stays_paper(tmp_path, capsys):
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 10, 3)
    before = sha(db)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert sha(db) == before
    p, why, _, _ = load(root)
    assert p is not None, why
    assert p.stats["count"] == 10 and 2.9 < p.stats["days"] < 3.1 and p.stats["ratio_ok_share"] == 1.0
    assert gates.max_mode(HEDGE, paper=p.stats) == "paper"
    assert "❌ дней бумаги: 3.0 (нужно ≥ 14)" in out and "❌ хеджей: 10 (нужно ≥ 50)" in out
    assert "✅ доля хеджей с коэффициентом 0.9–1.1: 100.0%" in out
    assert "Закрытых с итогом: 10" in out and "чуть оптимистичнее" in out


def test_main_above_thresholds_allows_confirm(tmp_path, capsys):
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 50, 15)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    p, why, b, _ = load(root)
    assert p is not None, why
    assert p.stats["count"] == 50 and p.stats["days"] >= 14 and p.stats["ratio_ok_share"] == 1.0
    assert p.stats["cost_to_buffer"] <= 0.6 and p.stats["sigma_ratio"] <= 0.5
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "confirm"
    assert "confirm: открытие по вашей кнопке" in out and "верьте боту" not in out
    assert "❌" not in out.split("Пороги «бумага → кнопка»")[1].split("Бэктеста нет")[0]


def test_main_eligibility_notes_and_env(tmp_path, capsys, monkeypatch):
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 3, 5, asset="BTC")
    fill(db, 4, 5, asset="ETH", amount=10000)
    fill(db, 2, 5, asset="ETH", amount=20000)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "подходящих хеджей в счёт — 5, не в счёт — 4" in out and "ETH от 20000 ₽" in out
    assert data_file(root, "gates_paper.json")["strategies"][HEDGE]["count"] == 5
    monkeypatch.setenv("HEDGE_MIN_AMOUNT_RUB", "ETH:5000,BTC:xx")
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "непонятно, пропущено: BTC:xx" in out and "не в счёт — 0" in out
    assert data_file(root, "gates_paper.json")["strategies"][HEDGE]["count"] == 9


def test_dry_run_writes_nothing(tmp_path, capsys):
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 5, 2)
    assert run_main(root, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "--dry-run: файлы не записаны" in out
    assert sorted(os.listdir(os.path.join(root, "data"))) == ["paper.db"]


def test_bad_arguments(tmp_path, capsys):
    root = bot_root(tmp_path)
    assert run_main(root, "--venues", "okx") == 2
    assert run_main(root, "--start", "01.01.2023") == 2
    assert "⛔" in capsys.readouterr().out
    assert os.listdir(os.path.join(root, "data")) == []


# --- атомарная запись --------------------------------------------------------------------------------------------------

def test_atomic_write_keeps_old_file_on_failure(tmp_path, capsys, monkeypatch):
    root = bot_root(tmp_path)
    path = os.path.join(root, "data", "gates_paper.json")
    assert run_main(root) == 0
    old = open(path, "rb").read()
    fill(os.path.join(root, "data", "paper.db"), 5, 2)

    def boom(src, dst):
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        assert run_main(root) == 2
    assert "⛔ файл не записан" in capsys.readouterr().out
    assert open(path, "rb").read() == old                                    # старый файл цел
    assert not [n for n in os.listdir(os.path.join(root, "data")) if n.startswith(".tmp-")]
    assert run_main(root) == 0 and open(path, "rb").read() != old            # после сбоя запись снова работает


def test_written_files_have_no_temp_leftovers_and_are_valid_json(tmp_path):
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 3, 2)
    assert run_main(root) == 0
    names = sorted(os.listdir(os.path.join(root, "data")))
    assert names == ["gates_paper.json", "paper.db"]
    doc = data_file(root, "gates_paper.json")
    assert doc["version"] == 1 and isinstance(doc["generated_at"], float) and list(doc["strategies"]) == [HEDGE]


# --- бэктест ---------------------------------------------------------------------------------------------------------

def coin(cost=0.2, sigma=0.1, n=1000, shares=None, venues=("bybit", "bingx"), ok=True):
    if not ok:
        return {"ok": False, "reason": "нет рыночной истории"}
    shares = shares or {"10000": 1.0, "20000": 1.0}
    return {"ok": True,
            "windows": {v: [{"minutes": 30, "n": 9, "cost_to_buffer": 9, "sigma_ratio": 9},
                            {"minutes": 60, "n": n, "cost_to_buffer": cost, "sigma_ratio": sigma}] for v in venues},
            "lots": {v: {a: {"in_band_share": s} for a, s in shares.items()} for v in venues}}


def result(**coins):
    return {"params": {"main_window": 60, "amounts_rub": [10000, 20000]}, "coins": coins}


def test_build_backtest_takes_worst_case_over_coins_and_venues():
    res = result(BTC=coin(cost=0.2, sigma=0.1, n=8000), ETH=coin(cost=0.4, sigma=0.3, n=480,
                                                                  shares={"10000": 0.5, "20000": 0.97}))
    doc, notes = tgs.build_backtest(res, ["BTC", "ETH"], {"ETH": 20000.0}, ("bybit",), "sha1", NOW)
    assert doc["research_sha"] == "sha1" and doc["version"] == 1 and doc["generated_at"] == NOW
    st = doc["strategies"][HEDGE]
    assert st == {"count": 480, "days": 20.0, "cost_to_buffer": 0.4, "sigma_ratio": 0.3, "ratio_ok_share": 0.97}
    assert any("cost_to_buffer" in n and "ETH" in n for n in notes)
    # 10 000 ₽ у ETH не считается (хедж только от 20 000 ₽); без минимума считалось бы худшее 0.5
    doc2, _ = tgs.build_backtest(res, ["BTC", "ETH"], {}, ("bybit",), "sha1", NOW)
    assert doc2["strategies"][HEDGE]["ratio_ok_share"] == 0.5
    assert gates.evaluate(gates.BACKTEST_STRONG[HEDGE], st).passed is True


def test_build_backtest_both_venues_takes_max():
    c = coin(cost=0.2, sigma=0.1)
    c["windows"]["bingx"][1].update(cost_to_buffer=0.55, sigma_ratio=0.45)
    c["lots"]["bingx"]["20000"]["in_band_share"] = 0.96
    res = result(BTC=c)
    st = tgs.build_backtest(res, ["BTC"], {}, ("bybit", "bingx"), "s", NOW)[0]["strategies"][HEDGE]
    assert (st["cost_to_buffer"], st["sigma_ratio"], st["ratio_ok_share"]) == (0.55, 0.45, 0.96)
    only = tgs.build_backtest(res, ["BTC"], {}, ("bybit",), "s", NOW)[0]["strategies"][HEDGE]
    assert (only["cost_to_buffer"], only["sigma_ratio"], only["ratio_ok_share"]) == (0.2, 0.1, 1.0)


def test_build_backtest_missing_data_omits_keys_fail_closed():
    doc, notes = tgs.build_backtest(result(BTC=coin(), ETH=coin(ok=False)), ["BTC", "ETH"], {}, ("bybit",), "s", NOW)
    assert doc["strategies"][HEDGE] == {}                      # нет монеты — нет ни одного порога, а не «по BTC»
    assert any("ETH" in n and "нет рыночной истории" in n for n in notes) and any("HEDGE_ASSETS" in n for n in notes)
    doc, _ = tgs.build_backtest(result(BTC=coin()), ["BTC", "TON"], {}, ("bybit",), "s", NOW)
    assert doc["strategies"][HEDGE] == {}                      # TON вообще нет в результате
    bad = coin()
    bad["windows"]["bybit"][1]["cost_to_buffer"] = "nan"       # metrics.fnum пишет nan строкой
    bad["windows"]["bybit"][1]["sigma_ratio"] = None
    bad["lots"]["bybit"]["10000"]["in_band_share"] = float("inf")
    st, notes = tgs.build_backtest(result(BTC=bad), ["BTC"], {}, ("bybit",), "s", NOW)
    st = st["strategies"][HEDGE]
    assert set(st) == {"count", "days"} and any("cost_to_buffer" in n and "не записан" in n for n in notes)
    assert gates.evaluate(gates.BACKTEST_STRONG[HEDGE], st).passed is False
    lots_missing = coin()
    del lots_missing["lots"]["bybit"]
    st = tgs.build_backtest(result(BTC=lots_missing), ["BTC"], {}, ("bybit",), "s", NOW)[0]["strategies"][HEDGE]
    assert "ratio_ok_share" not in st and "cost_to_buffer" in st
    no_amount, _ = tgs.build_backtest(result(ETH=coin()), ["ETH"], {"ETH": 50000.0}, ("bybit",), "s", NOW)
    assert "ratio_ok_share" not in no_amount["strategies"][HEDGE]   # ни одна сумма круга не дотягивает до минимума
    assert tgs.build_backtest(result(), [], {}, ("bybit",), "s", NOW)[0]["strategies"][HEDGE] == {}


T0 = hb.T0 if hasattr(hb, "T0") else 59027 * 8 * hb.H


def _kl(n, price):
    return [[T0 + i * hb.H, price(i), price(i), price(i), price(i), 1.0] for i in range(n)]


def _perp(n, price, spec=None):
    return {"symbol": "BTCUSDT", "start": T0, "end": T0 + n * hb.H, "klines": _kl(n, price),
            "funding": [[T0 + i * hb.H, 0.0] for i in range(0, n, 8)],
            "spec": spec or {"qty_step": 0.0001, "min_qty": 0.0001, "min_notional": 2}}


def synthetic_result(n=24 * 40):
    """Настоящий hedge_bt.run на маленьком наборе: перп = спот (базиса нет), запас на курс 1% — бэктест сильный."""
    wave = lambda i: 100000 * (1 + 0.01 * math.sin(i / 3))      # noqa: E731
    ds = {"coins": {"BTC": {"spot": {"klines": _kl(n, wave)}, "perp": [_perp(n, wave)], "bingx": [_perp(n, wave)]}}}
    return hb.run(ds, (), None, hb.Params(coins=("BTC",), buffers={"BTC": 1.0}), log=lambda *a: None)


def test_build_backtest_from_real_hedge_bt_run():
    res = synthetic_result()
    w = next(x for x in res["coins"]["BTC"]["windows"]["bybit"] if x["minutes"] == 60)
    doc, _ = tgs.build_backtest(res, ["BTC"], {"ETH": 20000.0}, ("bybit",), "s", NOW)
    st = doc["strategies"][HEDGE]
    assert st["count"] == w["n"] > 900 and st["days"] == round(w["n"] / 24, 2)
    assert st["cost_to_buffer"] == w["cost_to_buffer"] and 0 < st["cost_to_buffer"] <= 0.6
    assert st["sigma_ratio"] == w["sigma_ratio"] <= 0.5 and st["ratio_ok_share"] == 1.0
    assert gates.evaluate(gates.BACKTEST_STRONG[HEDGE], st).passed
    json.dumps(doc, allow_nan=False)


def test_main_backtest_roundtrip_minlot_then_confirm(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    calls = []

    def runner(cmd, cwd):
        calls.append((cmd, cwd))
        out = cmd[cmd.index("--out") + 1]
        with open(os.path.join(out, "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": synthetic_result()}, f)
        return 0
    assert run_main(root, "--backtest", "--offline", "--cache", str(tmp_path / "cache"), "--start", "2024-01-01",
                    runner=runner) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd[:3] == [sys.executable, "-m", "research.report"] and cwd == root
    assert "--offline" in cmd and cmd[cmd.index("--start") + 1] == "2024-01-01"
    assert cmd[cmd.index("--cache") + 1] == str(tmp_path / "cache") and "--paper-db" not in cmd   # базы нет — не передаём
    p, why_p, b, why_b = load(root)
    assert p is not None, why_p
    assert b is not None, why_b                                                # бот принял: вне git, sha сошёлся
    assert b.research_sha == gates.research_sha(root) and b.stats["count"] > 900
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "minlot"
    assert "minlot: минимальный лот" in out and "Бот принял gates_backtest.json: да" in out
    assert "не больше 50 USDT" in out and "Бэктеста нет" not in out
    # тонкая бумага + сильный бэктест: TRADING_SHORT_PAPER для хеджа ничего не сокращает (SHORT_PAPER days == 14)
    assert gates.SHORT_PAPER[HEDGE]["days"] == 14 and "ничего не сокращает" in out
    fill(os.path.join(root, "data", "paper.db"), 50, 15)                       # набрана бумага — confirm
    assert run_main(root, "--backtest", runner=runner) == 0
    p, _, b, _ = load(root)
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "confirm"
    cmd = calls[-1][0]
    assert cmd[cmd.index("--paper-db") + 1] == os.path.join(root, "data", "paper.db") and "--offline" not in cmd


def test_main_backtest_missing_coin_leaves_no_thresholds(tmp_path, capsys):
    root = bot_root(tmp_path)          # HEDGE_ASSETS по умолчанию BTC,ETH,TON, а данные только по BTC

    def runner(cmd, cwd):
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": synthetic_result()}, f)
        return 0
    assert run_main(root, "--backtest", runner=runner) == 0
    out = capsys.readouterr().out
    _, _, b, why_b = load(root)
    assert b is not None, why_b
    assert b.stats == {}                                                       # ни одного порога — не по одной BTC
    assert gates.max_mode(HEDGE, paper=data_file(root, "gates_paper.json")["strategies"][HEDGE], backtest=b) == "paper"
    assert "Нет данных по ETH, TON" in out and "уберите их из HEDGE_ASSETS" in out


def test_stale_research_sha_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)

    def runner(cmd, cwd):
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": synthetic_result()}, f)
        return 0
    assert run_main(root, "--backtest", runner=runner) == 0
    assert load(root)[2] is not None
    with open(os.path.join(root, "research", "hedge_bt.py"), "ab") as f:      # код research/ изменился после расчёта
        f.write(b"Y = 2\n")
    bt, why = gates.load_backtest(HEDGE, root=root)
    assert bt is None and "sha не совпал" in why
    p, _, _, _ = load(root)
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=bt) == "paper"


def test_research_changed_during_run_is_refused(tmp_path, capsys):
    root = bot_root(tmp_path)

    def runner(cmd, cwd):
        with open(os.path.join(root, "research", "hedge_bt.py"), "ab") as f:
            f.write(b"Z = 3\n")
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": synthetic_result()}, f)
        return 0
    assert run_main(root, "--backtest", runner=runner) == 2
    assert "код research/ изменился" in capsys.readouterr().out
    assert os.listdir(os.path.join(root, "data")) == []                        # ничего не записано


@pytest.mark.parametrize("case", ["exit", "no_report", "bad_json", "no_hedge"])
def test_backtest_failures_write_nothing(tmp_path, capsys, case):
    root = bot_root(tmp_path)

    def runner(cmd, cwd):
        report = os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json")
        if case == "bad_json":
            open(report, "w").write("{oops")
        elif case == "no_hedge":
            open(report, "w").write('{"funding": {}}')
        return 1 if case == "exit" else 0
    assert run_main(root, "--backtest", runner=runner) == 2
    assert "⛔ бэктест не получился" in capsys.readouterr().out
    assert os.listdir(os.path.join(root, "data")) == []


def test_backtest_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)

    def runner(cmd, cwd):
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": synthetic_result()}, f)
        return 0
    assert run_main(root, "--backtest", "--dry-run", runner=runner) == 0
    out = capsys.readouterr().out
    assert "--dry-run: файлы не записаны" in out and "minlot: минимальный лот" in out
    assert os.listdir(os.path.join(root, "data")) == []


def test_runner_not_called_without_backtest_flag(tmp_path):
    root = bot_root(tmp_path)

    def runner(cmd, cwd):
        raise AssertionError("research.report без --backtest не запускается")
    assert run_main(root, runner=runner) == 0


# --- вердикт против gates.max_mode -------------------------------------------------------------------------------------

PAPER_VARIANTS = [None, {"count": 0, "days": 0.0}, PAPER_OK, dict(PAPER_OK, days=13.9), dict(PAPER_OK, count=49),
                  dict(PAPER_OK, cost_to_buffer=0.61), {k: v for k, v in PAPER_OK.items() if k != "sigma_ratio"}]
BT_VARIANTS = [None, BT_OK, BT_WEAK, {}]


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("bi", range(len(BT_VARIANTS)))
@pytest.mark.parametrize("pi", range(len(PAPER_VARIANTS)))
def test_verdict_equals_gates_max_mode(tmp_path, monkeypatch, pi, bi, short):
    monkeypatch.setattr(gates, "_FLAGS", {"short_paper": short})
    root = bot_root(tmp_path)
    write_stats(root, PAPER_VARIANTS[pi], BT_VARIANTS[bi])
    p, _, b, _ = load(root)
    v = tgs.verdict(p.stats if p else None, b.stats if b else None, short)
    assert v["mode"] == gates.max_mode(HEDGE, paper=p.stats if p else None, backtest=b)
    assert v["mode"] in ("paper", "minlot", "confirm")


def test_verdict_lines_are_honest_about_thresholds_and_flag():
    weak = tgs.verdict({"count": 3, "days": 1.2, "ratio_ok_share": 1.0, "cost_to_buffer": 0.7}, None, False)
    text = "\n".join(tgs.verdict_lines(weak))
    assert "❌ дней бумаги: 1.2 (нужно ≥ 14)" in text and "❌ хеджей: 3 (нужно ≥ 50)" in text
    assert "✅ доля хеджей с коэффициентом 0.9–1.1: 100.0% (нужно ≥ 95%)" in text
    assert "❌ стоимость хеджа / запас на курс: 0.700 (нужно ≤ 0.6)" in text
    assert "❌ σ(факт − план) с хеджем / без хеджа: нет данных (нужно ≤ 0.5)" in text
    assert "paper: только бумага" in text and "Бэктеста нет" in text
    assert "TRADING_SHORT_PAPER=1" in text and "50 USDT" in text and "gates_live.json" in text
    strong = tgs.verdict(PAPER_OK, BT_OK, True)
    text = "\n".join(tgs.verdict_lines(strong))
    assert strong["short_active"] and "TRADING_SHORT_PAPER=1 включён и бэктест сильный" in text
    assert "confirm: открытие по вашей кнопке" in text
    idle = "\n".join(tgs.verdict_lines(tgs.verdict(PAPER_OK, BT_WEAK, True)))
    assert "не действует: бэктест не сильный или его нет" in idle


def test_script_imports_no_network_and_no_research():
    src = open(os.path.join(ROOT, "scripts", "trading_gates_stats.py"), encoding="utf-8").read()
    for bad in ("import socket", "import requests", "import urllib", "import aiohttp", "import http", "import research",
                "from research", "import_module"):
        assert bad not in src, bad
