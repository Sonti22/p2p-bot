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
from trading import gates, hedge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import trading_gates_stats as tgs  # noqa: E402

DAY = 86400
NOW = time.time()
HEDGE = "hedge"
PAPER_OK = {"days": 14, "count": 50, "ratio_ok_share": 0.95, "cost_to_buffer": 0.6, "sigma_ratio": 0.5}
BT_OK = dict(PAPER_OK)
BT_WEAK = dict(PAPER_OK, cost_to_buffer=0.61)


BT_BUFFER = "BTC:1.0,ETH:0.5,TON:0.7"   # запас в synthetic_result() (BTC 1%) и по умолчанию у research (ETH, TON)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(gates, "_FLAGS", {"short_paper": False})
    for name in ("HEDGE_ASSETS", "HEDGE_MIN_AMOUNT_RUB", "RISK_BUFFER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RISK_BUFFER", BT_BUFFER)      # не ниже запаса бэктеста — иначе скрипт бэктест не запишет


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

def _hedge(db, ts_open, asset="BTC", amount=10000, x=0.0, closed=True, fees=0.1, ref_close=90.0):
    """Круг с хеджем (как tests/test_hedge_gates.py): план 1%, факт 1 + x, хедж вернул −0.9·x п.п. ref_close=None —
    курса ₽/USDT в записи нет: стоимость хеджа неизвестна (simperp.fact_cost_pct → None)."""
    b = make_ad("Bybit", "buy", 6_000_000.0, asset=asset)
    cid = paper.start_cycle(amount, b, make_ad("Bybit", "sell", 90.0), "спот", 1.0, path=db, ts=ts_open, planned_raw=1.0)
    st = {"status": "closed" if closed else "open", "ts_open": ts_open, "ratio": 1.0, "exp_cost_pct": 0.0,
          "spread_usdt": 0.0}
    if ref_close is not None:
        st["ref_close"] = ref_close
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


def fill_none(db, n, asset="BTC", amount=10000):
    """Круги, по которым хедж не строился (статус none: плана нет)."""
    for _ in range(n):
        b = make_ad("Bybit", "buy", 6_000_000.0, asset=asset)
        cid = paper.start_cycle(amount, b, make_ad("Bybit", "sell", 90.0), "спот", 1.0, path=db, ts=NOW - DAY,
                                planned_raw=1.0)
        con = sqlite3.connect(db)
        con.execute("UPDATE cycles SET hedge_state = ? WHERE id = ?",
                    (json.dumps({"status": "none", "note": "нет плана"}), cid))
        con.commit()
        con.close()


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# --- разбор настроек и чистые функции ---------------------------------------------------------------------------------

INF = float("inf")
# те же входы, что в tests/trading/test_trading_hedge.py (settings/min_amount) — ответ должен совпасть с ядром слово в слово
MIN_AMOUNT_CASES = [
    (None, {"ETH": 20000.0}),                                            # переменной нет — умолчание ядра
    ("", {}),                                                            # пустая строка — без минимума (не умолчание!)
    ("ETH:abc", {"ETH": INF}),
    ("ETH:25000, BTC:abc,TON:5 000,junk", {"ETH": 25000.0, "BTC": INF, "TON": 5000.0, "JUNK": INF}),
    (":5000,ETH:20000", {"ETH": 20000.0}),
    ("ETH", {"ETH": INF}), ("ETH=20000", {"ETH": INF}), ("eth:", {"ETH": INF}), ("ETH:20 000 руб", {"ETH": INF}),
    ("BTC:5000,ETH", {"BTC": 5000.0, "ETH": INF}),
]


@pytest.mark.parametrize("raw,expected", MIN_AMOUNT_CASES)
def test_min_amounts_are_exactly_what_the_core_parses(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("HEDGE_MIN_AMOUNT_RUB", raising=False)
    else:
        monkeypatch.setenv("HEDGE_MIN_AMOUNT_RUB", raw)
    assets, mins = tgs.hedge_config()
    assert mins == expected == hedge.settings()["min_amount"]
    assert assets == hedge.settings()["assets"]
    assert not hasattr(tgs, "parse_min_amounts")                          # своего разбора нет — расходиться нечему


def test_eligible_follows_core_offer_rule():
    assets = ["BTC", "ETH"]
    assert tgs._eligible("ETH", 20000, assets, {"ETH": 20000.0}) and not tgs._eligible("ETH", 19999.99, assets, {"ETH": 20000.0})
    assert tgs._eligible("btc", 1, assets, {"ETH": 20000.0}) and not tgs._eligible("SOL", 10 ** 9, assets, {})
    assert not tgs._eligible("ETH", 10 ** 12, assets, {"ETH": INF})       # мусор в настройке — монету не хеджируем
    assert tgs._eligible("ETH", 5, assets, {}) and not tgs._eligible("ETH", None, assets, {"ETH": 1.0})


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
    per, info = tgs.paper_stats(db, ["BTC", "ETH", "TON"], {"ETH": 20000.0}, now=NOW)
    assert info == {"db": True, "kept": 5, "dropped": 8}
    assert list(per) == ["BTC", "ETH", "TON"] and "SOL" not in per          # по каждой монете отдельно
    assert (per["BTC"]["count"], per["ETH"]["count"], per["TON"]["count"]) == (3, 2, 0)
    assert per["TON"]["days"] == 0.0 and per["TON"]["ratio_ok_share"] is None
    assert sha(db) == before                             # оригинал не менялся
    per2, info2 = tgs.paper_stats(db, ["BTC", "ETH", "TON"], {}, now=NOW)   # без минимума ETH — все ETH в счёт
    assert info2["kept"] == 11 and per2["ETH"]["count"] == 8
    per3, info3 = tgs.paper_stats(db, ["BTC"], {"ETH": 20000.0}, now=NOW)
    assert info3["kept"] == 3 and list(per3) == ["BTC"] and per3["BTC"]["count"] == 3


def test_paper_stats_missing_db_is_not_created(tmp_path):
    db = str(tmp_path / "sub" / "paper.db")
    per, info = tgs.paper_stats(db, ["BTC"], {}, now=NOW)
    assert info == {"db": False, "kept": 0, "dropped": 0} and per["BTC"]["count"] == 0 and per["BTC"]["days"] == 0.0
    assert not os.path.exists(db) and not os.path.exists(os.path.dirname(db))


def test_paper_stats_counts_hedged_and_none_circles(tmp_path):
    db = str(tmp_path / "paper.db")
    fill(db, 6, 5, asset="BTC")
    fill(db, 2, 5, asset="BTC", closed=False)            # открытые хеджи — хеджированы, но ещё без итога
    fill_none(db, 3, asset="BTC")
    fill_none(db, 4, asset="ETH", amount=10000)           # ETH меньше 20 000 ₽ — не в счёт вовсе
    per, info = tgs.paper_stats(db, ["BTC", "ETH"], {"ETH": 20000.0}, now=NOW)
    assert per["BTC"]["count"] == 6 and per["BTC"]["hedged"] == 8 and per["BTC"]["none"] == 3
    assert per["ETH"]["none"] == 0 and info["kept"] == 11 and info["dropped"] == 4
    assert "3 из 11 (27%)" in tgs.none_note(per)
    assert "пока нет" in tgs.none_note({"BTC": {"none": 0, "hedged": 0}})


def test_aggregate_paper_worst_coin_and_few_data_block_thresholds():
    good = {"count": 60, "days": 20.0, "ratio_ok_share": 0.99, "cost_to_buffer": 0.2, "sigma_ratio": 0.1}
    bad = {"count": 8, "days": 16.0, "ratio_ok_share": 0.96, "cost_to_buffer": 0.9, "sigma_ratio": 0.4}
    few = {"count": 4, "days": 30.0, "ratio_ok_share": 0.1, "cost_to_buffer": 9.0, "sigma_ratio": 9.0}
    out, few_coins = tgs.aggregate_paper({"BTC": good, "ETH": bad})
    assert few_coins == []
    assert out == {"count": 68, "days": 16.0, "ratio_ok_share": 0.96, "cost_to_buffer": 0.9, "sigma_ratio": 0.4}   # худшая монета
    out, few_coins = tgs.aggregate_paper({"BTC": good, "ETH": bad, "TON": few})
    assert few_coins == ["TON"]                                            # мало данных у монеты, которую бот хеджирует
    assert out == {"count": 68, "days": 16.0}                              # count и days честные, а ключей порогов НЕТ
    assert not set(tgs.PAPER_KEYS) & set(out)
    assert tgs.aggregate_paper({"BTC": few}) == ({"count": 0, "days": 0.0}, ["BTC"])   # ни одной монеты с данными
    assert tgs.aggregate_paper({}) == ({"count": 0, "days": 0.0}, [])
    no_sigma = dict(bad, sigma_ratio=None)                                 # у монеты в счёте нет числа — ключа нет
    assert "sigma_ratio" not in tgs.aggregate_paper({"BTC": good, "ETH": no_sigma})[0]
    assert tgs.aggregate_paper({"BTC": dict(good, count=5)})[1] == []      # ровно 5 — уже в счёт
    assert tgs.aggregate_paper({"BTC": dict(good, count=4)})[1] == ["BTC"]


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
    # файл бумаги не стареет сам — скрипт прямо говорит про generated_at и про перезапуск перед confirm
    assert "generated_at = " in out and "перезапускай перед включением confirm; файл сам не стареет" in out
    assert "gates_backtest.json не тронут: без --backtest пишется только gates_paper.json" in out


def test_main_below_thresholds_stays_paper(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")                       # данные только по BTC — ETH и TON бот не хеджирует
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
    assert "BTC: закрытых 10" in out and "мало данных" not in out
    assert "чуть оптимистичнее" in out
    assert "круги без хеджа (none): 0 из 10 (0%) — в ratio_ok_share не входят" in out   # эффект выжившего — справкой


def test_main_above_thresholds_allows_confirm(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 50, 15)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    p, why, b, _ = load(root)
    assert p is not None, why
    assert p.stats["count"] == 50 and p.stats["days"] >= 14 and p.stats["ratio_ok_share"] == 1.0
    assert p.stats["cost_to_buffer"] <= 0.6 and p.stats["sigma_ratio"] <= 0.5
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "confirm"
    assert "confirm: РЕАЛЬНЫЕ ордера по вашей кнопке" in out and "верьте боту" not in out
    assert "❌" not in out.split("Пороги «бумага → кнопка»")[1].split("Бэктеста нет")[0]
    assert "бот разрешит confirm сразу, без minlot" in out and "мало данных" not in out and "⛔" not in out


def test_main_trust_the_bot_when_its_mode_differs_from_the_numbers(tmp_path, capsys, monkeypatch):
    # по цифрам скрипта — confirm, а бот (gates.max_mode по файлам) считает иначе: последнее слово за ботом
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 50, 15)
    monkeypatch.setattr(tgs.gates, "max_mode", lambda *a, **kw: "paper")
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "⚠ Расчёт по цифрам даёт confirm, бот — paper: верьте боту." in out
    assert "Разрешённый режим по порогам сейчас: paper: только бумага" in out   # итоговая строка — режим бота


def test_main_no_trust_line_when_bot_did_not_accept_paper_file(tmp_path, capsys, monkeypatch):
    # файл бумаги бот не принял → «статистики для него нет», сравнивать режимы не с чем: строки «верьте боту» нет
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 50, 15)
    monkeypatch.setattr(tgs.gates, "load_paper", lambda *a, **kw: (None, "тест: файл не принят"))
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "Бот принял gates_paper.json: НЕТ — тест: файл не принят" in out
    assert "бот не принял файл бумаги" in out and "верьте боту" not in out


def test_main_eligibility_notes_and_env(tmp_path, capsys, monkeypatch):
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 6, 5, asset="BTC")
    fill(db, 4, 5, asset="ETH", amount=10000)
    fill(db, 6, 5, asset="ETH", amount=20000)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "подходящих хеджей в счёт — 12, не в счёт — 4" in out and "ETH от 20000 ₽" in out
    assert data_file(root, "gates_paper.json")["strategies"][HEDGE]["count"] == 12
    monkeypatch.setenv("HEDGE_MIN_AMOUNT_RUB", "ETH:5000,BTC:xx")      # BTC:xx — бот эту монету не хеджирует вовсе
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "для BTC значение непонятно — бот эту монету не хеджирует" in out and "не в счёт — 6" in out
    assert "Хеджируемые монеты (HEDGE_ASSETS): ETH;" in out
    assert data_file(root, "gates_paper.json")["strategies"][HEDGE]["count"] == 10    # только ETH, от 5000 ₽


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
    assert "minlot: РЕАЛЬНЫЕ ордера, позиция не больше 50 USDT" in out and "Бот принял gates_backtest.json: да" in out
    assert "не больше 50 USDT" in out and "Бэктеста нет" not in out
    assert tgs.BACKTEST_UNLOCK in out and "⚠⚠⚠ ВНИМАНИЕ" in out                 # громкое предупреждение: файл открывает minlot
    assert "generated_at = " in out and "файл сам не стареет" in out
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
    assert tgs.BACKTEST_UNLOCK not in out                                      # порогов нет — и открывать нечего


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
    assert "--dry-run: файлы не записаны" in out and "minlot: РЕАЛЬНЫЕ ордера" in out
    assert "разрешил бы реальные ордера minlot (до 50 USDT)" in out and "файл не записан" in out
    assert tgs.BACKTEST_UNLOCK not in out                                      # в сухом прогоне — только «разрешил бы»
    assert os.listdir(os.path.join(root, "data")) == []


def test_runner_not_called_without_backtest_flag(tmp_path):
    root = bot_root(tmp_path)

    def runner(cmd, cwd):
        raise AssertionError("research.report без --backtest не запускается")
    assert run_main(root, runner=runner) == 0


# --- худшая монета: одна плохая монета не прячется в среднем -------------------------------------------------------------

def test_one_bad_coin_flips_confirm_to_paper(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")                      # пока бот хеджирует только BTC
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 50, 15, asset="BTC")                                  # BTC: 50 хороших хеджей за 15 дней — пороги проходят
    assert run_main(root) == 0
    p, why, b, _ = load(root)
    assert p is not None, why
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "confirm"
    capsys.readouterr()
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")                  # добавили ETH
    fill(db, 6, 15, asset="ETH", amount=20000, fees=2.5)          # ETH: 6 хеджей, комиссия ≈ 1.1% круга, а запас на курс 0.5%
    per, _ = tgs.paper_stats(db, ["BTC", "ETH"], {"ETH": 20000.0}, now=NOW)
    assert per["ETH"]["cost_to_buffer"] > 0.6 > per["BTC"]["cost_to_buffer"]
    pooled = simperp.gate_stats(db, now=NOW)
    assert pooled["cost_to_buffer"] <= 0.6                         # старый подсчёт «по всем сразу»: плохая ETH тонет в BTC
    assert run_main(root) == 0
    out = capsys.readouterr().out
    p, why, b, _ = load(root)
    assert p is not None, why
    assert p.stats["count"] == 56 and p.stats["cost_to_buffer"] == per["ETH"]["cost_to_buffer"]   # худшая монета, не среднее
    assert p.stats["cost_to_buffer"] > 0.6
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "paper"                            # было confirm
    assert "❌ стоимость хеджа / запас на курс" in out and "ETH: закрытых 6" in out
    assert "paper: только бумага" in out.split("Разрешённый режим по порогам сейчас:")[1]


def test_coin_with_few_hedges_blocks_thresholds_and_is_listed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 50, 15, asset="BTC")
    fill(db, 4, 15, asset="ETH", amount=20000, fees=2.5)          # 4 < 5: мало данных — не в count, но БЛОКИРУЕТ пороги
    assert run_main(root) == 0
    out = capsys.readouterr().out
    p, why, b, _ = load(root)
    assert p is not None, why
    assert p.stats["count"] == 50 and not set(tgs.PAPER_KEYS) & set(p.stats)   # 4 плохих хеджа ETH не в count; порогов нет
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "paper"        # раньше было confirm по одной BTC
    assert "ETH: мало данных — закрытых хеджей 4 (нужно ≥ 5)" in out and "BTC: мало данных" not in out
    assert "⛔⛔ ПОРОГИ БУМАГИ НЕ ПРОЙДУТ: по ETH мало данных" in out
    assert "Подождите" in out and "уберите её из HEDGE_ASSETS" in out
    assert "Не записаны ключи: ratio_ok_share, cost_to_buffer, sigma_ratio" in out
    assert "❌ доля хеджей с коэффициентом 0.9–1.1: нет данных" in out and "не пишется: по ETH мало данных" in out
    fill(db, 1, 15, asset="ETH", amount=20000, fees=2.5)                        # пятый хедж — ETH уже в счёте (и плохая)
    assert run_main(root) == 0
    out = capsys.readouterr().out
    p, _, b, _ = load(root)
    assert p.stats["count"] == 55 and p.stats["cost_to_buffer"] > 0.6 and "мало данных" not in out
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "paper"        # теперь из-за стоимости ETH, а не из-за блокировки


def test_paper_report_shows_none_share_next_to_ratio(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 6, 5, asset="BTC")
    fill_none(db, 4, asset="BTC")
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "BTC: закрытых 6" in out and "без хеджа none: 4 из 10" in out
    assert "круги без хеджа (none): 4 из 10 (40%) — в ratio_ok_share не входят" in out
    assert "✅ доля хеджей с коэффициентом 0.9–1.1: 100.0% (нужно ≥ 95%) · круги без хеджа (none): 4 из 10" in out
    assert data_file(root, "gates_paper.json")["strategies"][HEDGE]["count"] == 6   # none в файл не идут


# --- бэктест: запас на курс, таймаут, окружение ------------------------------------------------------------------------

def _runner(cmd, cwd):
    with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
        json.dump({"hedge": synthetic_result()}, f)      # запас бэктеста по BTC — 1.0%
    return 0


def _strong_file_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    assert run_main(root, "--backtest", runner=_runner) == 0                   # RISK_BUFFER = запас бэктеста — пишется
    b, why = gates.load_backtest(HEDGE, root=root)
    assert b is not None and gates.evaluate(gates.BACKTEST_STRONG[HEDGE], b.stats).passed, why
    return root


def test_lower_risk_buffer_refuses_and_disarms_old_file(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setenv("RISK_BUFFER", "BTC:0.5")                                # ниже 1.0% бэктеста
    assert run_main(root, "--backtest", "--dry-run", runner=_runner) == 2
    out = capsys.readouterr().out
    assert "⛔ BTC: RISK_BUFFER = 0.5% ниже запаса бэктеста (1%)" in out and "не записан" in out
    assert "--dry-run: старый gates_backtest.json не тронут" in out
    assert gates.load_backtest(HEDGE, root=root)[0].stats["count"] > 900         # dry-run старое не трогает
    assert run_main(root, "--backtest", runner=_runner) == 2                     # без dry-run
    out = capsys.readouterr().out
    assert "Старый gates_backtest.json заменён пустым" in out
    b, why = gates.load_backtest(HEDGE, root=root)
    assert b is not None, why
    assert b.stats == {} and gates.max_mode(HEDGE, paper={"count": 0, "days": 0.0}, backtest=b) == "paper"   # minlot закрыт
    assert data_file(root, "gates_paper.json")["strategies"][HEDGE]["count"] == 0    # бумага в этот раз не пишется (fail closed)


def test_lower_risk_buffer_without_old_file_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    monkeypatch.setenv("RISK_BUFFER", "BTC:0.3")
    root = bot_root(tmp_path)
    assert run_main(root, "--backtest", runner=_runner) == 2
    out = capsys.readouterr().out
    assert "ниже запаса бэктеста" in out and "Старого gates_backtest.json нет" in out
    assert os.listdir(os.path.join(root, "data")) == []


def test_higher_or_missing_risk_buffer_only_warns(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    monkeypatch.setenv("RISK_BUFFER", "BTC:1.4")                                # выше 1.0% бэктеста, но не выше 5 × 0.3%
    root = bot_root(tmp_path)
    assert run_main(root, "--backtest", runner=_runner) == 0
    out = capsys.readouterr().out
    assert "⚠ BTC: RISK_BUFFER = 1.4% выше запаса бэктеста (1%)" in out and "⛔" not in out
    assert os.path.exists(os.path.join(root, "data", "gates_backtest.json"))
    monkeypatch.setenv("RISK_BUFFER", "ETH:0.5")                                # BTC в RISK_BUFFER нет — бот возьмёт запас круга
    assert run_main(root, "--backtest", runner=_runner) == 0
    out = capsys.readouterr().out
    assert "⚠ BTC: в RISK_BUFFER запаса нет" in out and "⛔" not in out


def test_malformed_risk_buffer_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    monkeypatch.setenv("RISK_BUFFER", "BTC:abc")
    root = bot_root(tmp_path)
    assert run_main(root, "--backtest", runner=_runner) == 2                   # до запуска research.report и до бумаги
    assert "RISK_BUFFER не разобрать" in capsys.readouterr().out
    assert os.listdir(os.path.join(root, "data")) == []
    fill(os.path.join(root, "data", "paper.db"), 6, 3)                         # и без --backtest — тоже ничего не пишем
    assert run_main(root) == 2
    assert "⛔ RISK_BUFFER не разобрать" in capsys.readouterr().out
    assert sorted(os.listdir(os.path.join(root, "data"))) == ["paper.db"]


def test_buffer_check_units():
    res = {"coins": {"BTC": {"buffer_pct": 0.3}, "ETH": {}, "TON": {"buffer_pct": "nan"}}}
    refuse, warn = tgs.buffer_check(res, ["BTC", "ETH", "TON"], {"BTC": 0.3, "ETH": 0.4, "TON": 0.7})
    assert [r.split(":")[0] for r in refuse] == ["ETH"]                          # ETH: по умолчанию research 0.5, в .env 0.4
    assert not warn                                                              # BTC равен, TON: buffer_pct мусор — умолчание 0.7
    refuse, warn = tgs.buffer_check(res, ["BTC"], {"BTC": 0.5})
    assert not refuse and len(warn) == 1 and "выше" in warn[0]
    refuse, warn = tgs.buffer_check(res, ["BTC"], {})
    assert not refuse and len(warn) == 1 and "запаса нет" in warn[0]


def test_backtest_timeout_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    seen = {}

    def slow(cmd, **kw):
        seen.update(kw)
        raise tgs._ProcessTimeout(cmd, kw.get("timeout"))
    monkeypatch.setattr(tgs, "_run_process", slow)
    assert run_main(root, "--backtest") == 2                                    # runner не задан — настоящий _run_report
    out = capsys.readouterr().out
    assert seen["timeout"] == 900 == tgs.REPORT_TIMEOUT and seen["check"] is False and seen["cwd"] == root
    assert "не уложился в 900 с" in out and "⛔ бэктест не получился" in out
    assert os.listdir(os.path.join(root, "data")) == []                          # даже бумага не пишется: fail closed


def test_child_env_is_minimal_no_keys(monkeypatch):
    for name, value in (("BYBIT_API_KEY", "k-test"), ("BYBIT_API_SECRET", "s-test"), ("TELEGRAM_BOT_TOKEN", "t-test"),
                        ("FOO", "bar"), ("PYTHONPATH", "x"), ("PATH", "p-test"), ("SYSTEMROOT", "r-test"), ("TEMP", "t1"),
                        ("TMP", "t2"), ("USERPROFILE", "u")):
        monkeypatch.setenv(name, value)
    env = tgs._child_env()
    assert set(env) <= set(tgs.CHILD_ENV_KEYS) | {"PYTHONIOENCODING"}
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PATH"] == "p-test" and env["USERPROFILE"] == "u"
    assert not any(k for k in env if any(w in k.upper() for w in ("KEY", "SECRET", "TOKEN", "PYTHONPATH", "FOO")))
    assert tgs._child_env({"PATH": "a", "ZZZ": "b"}) == {"PATH": "a", "PYTHONIOENCODING": "utf-8"}   # что нет в окружении — не выдумываем


NET_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def test_child_env_passes_proxy_and_cert_vars_but_no_secrets():
    # research/data.py берёт прокси из окружения (getproxies_environment): без них бэктест за прокси истории не скачает
    src = {}
    for name in NET_NAMES:
        src[name] = f"v-{name}"
        src[name.lower()] = f"v-{name.lower()}"
    src.update(BYBIT_API_KEY="k-test", BYBIT_API_SECRET="s-test", TELEGRAM_BOT_TOKEN="t-test", FOO="bar", PATH="p-test",
               PROXY_PASSWORD="x", MY_PROXY="y", CURL_CA_BUNDLE="z")
    env = tgs._child_env(src)
    for name in NET_NAMES:
        assert env[name] == f"v-{name}" and env[name.lower()] == f"v-{name.lower()}"       # оба регистра
    assert env["PATH"] == "p-test" and env["PYTHONIOENCODING"] == "utf-8"
    assert set(env) == set(NET_NAMES) | {n.lower() for n in NET_NAMES} | {"PATH", "PYTHONIOENCODING"}   # ничего лишнего
    assert not any(k for k in env if any(w in k.upper() for w in ("KEY", "SECRET", "TOKEN", "PASSWORD", "FOO")))


def test_default_runner_passes_minimal_env_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    monkeypatch.setenv("BYBIT_API_KEY", "k-test")
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.setenv("HTTPS_PROXY", "proxy-test")
    monkeypatch.setenv("SSL_CERT_FILE", "cert-test")
    root = bot_root(tmp_path)
    got = {}

    class Done:
        returncode = 0

    def fake_run(cmd, **kw):
        got.update(cmd=cmd, **kw)
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": synthetic_result()}, f)
        return Done()
    monkeypatch.setattr(tgs, "_run_process", fake_run)
    assert run_main(root, "--backtest") == 0
    assert got["timeout"] == 900 and got["cwd"] == root and got["cmd"][:3] == [sys.executable, "-m", "research.report"]
    assert "BYBIT_API_KEY" not in got["env"] and "FOO" not in got["env"] and "PYTHONPATH" not in got["env"]
    assert got["env"]["PYTHONIOENCODING"] == "utf-8"
    assert got["env"]["HTTPS_PROXY"] == "proxy-test" and got["env"]["SSL_CERT_FILE"] == "cert-test"   # сеть бэктеста цела


def test_backtest_report_says_depth_of_history_not_independent_hedges(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    assert run_main(root, "--backtest", runner=_runner) == 0
    out = capsys.readouterr().out
    n = data_file(root, "gates_backtest.json")["strategies"][HEDGE]["count"]
    assert f"count и days — глубина истории, не независимые хеджи: 60-минутные окна перекрываются, {n} окон — это не {n} хеджей" in out
    assert "— окон истории, не независимые хеджи" in out and "— глубина истории" in out    # и в строках порогов


def test_backtest_file_warning_is_loud_and_only_when_strong(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    assert run_main(root, "--backtest", runner=_runner) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "ВНИМАНИЕ" in x)
    assert line.startswith("⚠⚠⚠ ВНИМАНИЕ: бэктест разрешает реальные ордера minlot (до 50 USDT) при TRADING=1 и TRADING_MODE=minlot")
    assert "TRADING_MODE=paper" in line
    weak = synthetic_result()
    weak["coins"]["BTC"]["windows"]["bybit"][1]["cost_to_buffer"] = 0.9        # бэктест не сильный — тревоги нет
    weak_root = bot_root(tmp_path / "weak")

    def weak_runner(cmd, cwd):
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": weak}, f)
        return 0
    assert run_main(weak_root, "--backtest", runner=weak_runner) == 0
    assert "ВНИМАНИЕ" not in capsys.readouterr().out


def test_old_strong_backtest_file_is_flagged_without_backtest_flag(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    capsys.readouterr()
    assert run_main(root) == 0                                                  # без --backtest: файл от прошлого запуска
    out = capsys.readouterr().out
    assert "старый gates_backtest.json действует: бэктест разрешает реальные ордера minlot" in out
    assert "gates_backtest.json не тронут: без --backtest пишется только gates_paper.json" in out
    assert "(файл от прошлого запуска, он продолжает действовать)" in out

# --- раунд 3: старый проходящий файл не переживает неудачный --backtest -----------------------------------------------------

def _fail_runner(case, root):
    def runner(cmd, cwd):
        report = os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json")
        if case == "exit":
            return 1
        if case == "exception":
            raise RuntimeError("boom")
        if case == "timeout":
            raise tgs._ProcessTimeout(cmd, 900)
        if case == "garbage_json":
            with open(report, "w", encoding="utf-8") as f:
                f.write("{oops")
            return 0
        if case == "no_hedge":
            with open(report, "w", encoding="utf-8") as f:
                f.write('{"funding": {}}')
            return 0
        if case == "sha_mismatch":                                             # код research/ сменился посреди расчёта
            with open(os.path.join(root, "research", "hedge_bt.py"), "ab") as f:
                f.write(b"Q = 4\n")
            with open(report, "w", encoding="utf-8") as f:
                json.dump({"hedge": synthetic_result()}, f)
            return 0
        raise AssertionError(case)
    return runner


def _bt_doc(root):
    return data_file(root, "gates_backtest.json")["strategies"][HEDGE]


def _mode(root):
    p, _, b, _ = load(root)
    return gates.max_mode(HEDGE, paper=p.stats if p else None, backtest=b)


@pytest.mark.parametrize("case", ["exit", "timeout", "exception", "garbage_json", "no_hedge", "sha_mismatch"])
def test_failed_backtest_invalidates_old_passing_file(tmp_path, capsys, monkeypatch, case):
    root = _strong_file_root(tmp_path, monkeypatch)
    assert _mode(root) == "minlot" and _bt_doc(root)["count"] > 900             # старый файл открывает реальные ордера
    capsys.readouterr()
    assert run_main(root, "--backtest", runner=_fail_runner(case, root)) == 2
    out = capsys.readouterr().out
    assert "⛔ бэктест не получился" in out and "Старый gates_backtest.json заменён пустым" in out
    assert _bt_doc(root) == {}                                                   # файл не удалён, а заменён пустым без порогов
    assert _mode(root) != "minlot" and _mode(root) == "paper"                   # настоящий загрузчик trading.gates


def test_real_process_timeout_invalidates_old_passing_file(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    capsys.readouterr()

    def slow(cmd, **kw):
        raise tgs._ProcessTimeout(cmd, kw.get("timeout"))
    monkeypatch.setattr(tgs, "_run_process", slow)
    assert run_main(root, "--backtest") == 2                                    # настоящий _run_report, таймаут
    assert "не уложился в 900 с" in capsys.readouterr().out
    assert _bt_doc(root) == {} and _mode(root) == "paper"


def test_old_file_is_invalidated_before_the_process_starts(tmp_path, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    seen = {}

    def killed(cmd, cwd):
        seen["doc"] = _bt_doc(root)                                              # что лежит на диске, пока идёт research.report
        seen["mode"] = _mode(root)
        raise KeyboardInterrupt                                                  # процесс убит посреди расчёта
    with pytest.raises(KeyboardInterrupt):
        run_main(root, "--backtest", runner=killed)
    assert seen == {"doc": {}, "mode": "paper"}
    assert _bt_doc(root) == {} and _mode(root) == "paper"
    assert run_main(root, "--backtest", runner=_runner) == 0                     # следующий успешный запуск пишет свежий файл
    assert _mode(root) == "minlot" and _bt_doc(root)["count"] > 900


def test_dry_run_backtest_failure_leaves_old_file_untouched(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    path = os.path.join(root, "data", "gates_backtest.json")
    before = open(path, "rb").read()
    assert run_main(root, "--backtest", "--dry-run", runner=_fail_runner("exit", root)) == 2
    assert open(path, "rb").read() == before and _mode(root) == "minlot"         # dry-run ничего не пишет
    assert "заменён пустым" not in capsys.readouterr().out


def test_backtest_file_the_bot_rejects_is_replaced_with_empty_one(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    with open(os.path.join(root, ".git", "index"), "wb") as f:                  # gates_backtest.json в git — бот его не примет
        f.write(git_index(["research/hedge_bt.py", "README.md", "data/gates_backtest.json"]))
    capsys.readouterr()
    assert run_main(root, "--backtest", runner=_runner) == 2
    out = capsys.readouterr().out
    assert "Бот принял gates_backtest.json: нет" in out and "Бот не принял только что записанный gates_backtest.json" in out
    assert _bt_doc(root) == {}                                                   # свежий непринятый файл тоже обезврежен


def test_risk_buffer_below_research_default_refuses_before_the_process(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    monkeypatch.setenv("RISK_BUFFER", "BTC:0.1")                                # ниже умолчания research (0.3)

    def never(cmd, cwd):
        raise AssertionError("research.report при таком RISK_BUFFER не запускается")
    capsys.readouterr()
    assert run_main(root, "--backtest", "--dry-run", runner=never) == 2
    out = capsys.readouterr().out
    assert "research.report не запускался" in out and "ниже запаса бэктеста (0.3%)" in out
    assert _mode(root) == "minlot"                                               # dry-run старый файл не трогает
    assert run_main(root, "--backtest", runner=never) == 2
    assert _bt_doc(root) == {} and _mode(root) == "paper"


# --- RISK_BUFFER: не число и бессмыслица -------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["BTC:inf", "BTC:-inf", "BTC:nan", "BTC:0", "BTC:-0.5", "BTC:1.51", "BTC:1e9"])
def test_nonsense_risk_buffer_refuses_and_disarms_old_file(tmp_path, capsys, monkeypatch, raw):
    root = _strong_file_root(tmp_path, monkeypatch)
    monkeypatch.setenv("RISK_BUFFER", raw)

    def never(cmd, cwd):
        raise AssertionError("research.report при таком RISK_BUFFER не запускается")
    capsys.readouterr()
    assert run_main(root, "--backtest", runner=never) == 2
    out = capsys.readouterr().out
    assert "⛔ BTC: RISK_BUFFER" in out and "RISK_BUFFER непригоден" in out
    assert _bt_doc(root) == {} and _mode(root) == "paper"


@pytest.mark.parametrize("raw", ["BTC:inf", "BTC:0"])
def test_nonsense_risk_buffer_writes_nothing_without_backtest_flag(tmp_path, capsys, monkeypatch, raw):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    monkeypatch.setenv("RISK_BUFFER", raw)
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 6, 3)
    assert run_main(root) == 2
    assert "⛔ BTC: RISK_BUFFER" in capsys.readouterr().out
    assert sorted(os.listdir(os.path.join(root, "data"))) == ["paper.db"]        # ни бумаги, ни бэктеста


def test_buffer_nonsense_units():
    assert tgs.buffer_nonsense({"BTC": 0.3, "ETH": 0.5}, ["BTC", "ETH"]) == []
    assert tgs.buffer_nonsense({"BTC": 1.5}, ["BTC"]) == []                       # ровно 5 × 0.3 — ещё можно
    assert "выше разумного предела 1.5%" in tgs.buffer_nonsense({"BTC": 1.51}, ["BTC"])[0]
    assert "не конечное число" in tgs.buffer_nonsense({"BTC": INF}, ["BTC"])[0]
    assert "не конечное число" in tgs.buffer_nonsense({"BTC": float("nan")}, ["BTC"])[0]
    assert "больше 0" in tgs.buffer_nonsense({"BTC": 0.0}, ["BTC"])[0]
    assert "больше 0" in tgs.buffer_nonsense({"BTC": -1.0}, ["BTC"])[0]
    assert tgs.buffer_nonsense({"DOGE": 5.0}, ["DOGE"]) == []                     # монеты нет у research: предел 5%
    assert tgs.buffer_nonsense({"DOGE": 5.1}, ["DOGE"]) != []
    assert tgs.buffer_nonsense({"BTC": 0.3, "TON": INF}, ["BTC"]) == []           # монету не хеджируем — её запас не важен
    assert tgs.buffer_nonsense({}, ["BTC"]) == []                                 # нет в RISK_BUFFER — только предупреждение
    refuse, _ = tgs.buffer_check({}, ["BTC"], {"BTC": INF})
    assert len(refuse) == 1 and "не конечное число" in refuse[0]                  # buffer_check тоже отказывает


# --- монета без данных блокирует пороги бумаги -----------------------------------------------------------------------------

def test_btc_only_paper_cannot_unlock_confirm_while_eth_is_hedged(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 60, 15, asset="BTC")                                                  # BTC: 60 хороших хеджей
    fill(db, 3, 15, asset="ETH", amount=20000)                                     # ETH: 3 < 5, а бот её хеджирует
    assert run_main(root) == 0
    out = capsys.readouterr().out
    st = data_file(root, "gates_paper.json")["strategies"][HEDGE]
    assert st["count"] == 60 and st["days"] >= 14                                  # честные count и days по BTC
    assert not set(tgs.PAPER_KEYS) & set(st)                                       # но ни одного ключа порога
    p, why, b, _ = load(root)
    assert p is not None, why
    assert not gates.evaluate(gates.PAPER_TO_BUTTON[HEDGE], p.stats).passed
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "paper"             # confirm не открыт
    assert "⛔⛔ ПОРОГИ БУМАГИ НЕ ПРОЙДУТ: по ETH мало данных" in out and "уберите её из HEDGE_ASSETS" in out
    assert "paper: только бумага" in out.split("Разрешённый режим по порогам сейчас:")[1]
    fill(db, 2, 15, asset="ETH", amount=20000)                                     # выход 1: у ETH набралось 5 хеджей
    assert run_main(root) == 0
    p, _, b, _ = load(root)
    assert p.stats["count"] == 65 and set(tgs.PAPER_KEYS) <= set(p.stats)
    assert gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "confirm"
    capsys.readouterr()
    root2 = bot_root(tmp_path / "two")
    fill(os.path.join(root2, "data", "paper.db"), 60, 15, asset="BTC")
    fill(os.path.join(root2, "data", "paper.db"), 3, 15, asset="ETH", amount=20000)
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")                                      # выход 2: ETH убрана из HEDGE_ASSETS
    assert run_main(root2) == 0
    p, _, b, _ = load(root2)
    assert p.stats["count"] == 60 and gates.max_mode(HEDGE, paper=p.stats, backtest=b) == "confirm"


def test_paper_block_follows_the_real_hedge_settings(tmp_path, capsys, monkeypatch):
    def prepare(name):
        root = bot_root(tmp_path / name)
        db = os.path.join(root, "data", "paper.db")
        fill(db, 60, 15, asset="BTC")
        fill(db, 3, 15, asset="ETH", amount=20000)
        return root

    root = prepare("default")                                                       # HEDGE_ASSETS не задан: BTC, ETH, TON
    assert run_main(root) == 0
    assert "по ETH, TON мало данных" in capsys.readouterr().out and _mode(root) == "paper"
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,SOL")                                   # не зашито BTC/ETH/TON: блокирует SOL
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "по SOL мало данных" in out and "ETH: " not in out and _mode(root) == "paper"
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")
    monkeypatch.setenv("HEDGE_MIN_AMOUNT_RUB", "ETH:abc")                           # значение непонятно — бот ETH не хеджирует
    assert run_main(root) == 0
    out = capsys.readouterr().out
    assert "мало данных" not in out and _mode(root) == "confirm"
    monkeypatch.setenv("HEDGE_MIN_AMOUNT_RUB", "ETH:50000")                         # хеджирует от 50 000 ₽: наши круги не в счёт, данных 0
    assert run_main(root) == 0
    assert "по ETH мало данных" in capsys.readouterr().out and _mode(root) == "paper"


# --- значения вне диапазона считаются битыми --------------------------------------------------------------------------------

OUT_OF_RANGE = [("ratio_ok_share", 1.5), ("ratio_ok_share", 1.0001), ("ratio_ok_share", -0.1), ("cost_to_buffer", -0.5),
                ("cost_to_buffer", INF), ("sigma_ratio", -0.2), ("sigma_ratio", float("nan"))]


@pytest.mark.parametrize("key,bad", OUT_OF_RANGE)
def test_paper_out_of_range_number_is_dropped(key, bad):
    good = {"count": 60, "days": 20.0, "ratio_ok_share": 0.99, "cost_to_buffer": 0.2, "sigma_ratio": 0.1}
    st = dict(good, **{key: bad})
    out, _ = tgs.aggregate_paper({"BTC": st})
    assert key not in out and set(out) == {"count", "days", *tgs.PAPER_KEYS} - {key}
    doc = tgs.build_paper(st, NOW)["strategies"][HEDGE]                             # и на втором рубеже — при записи файла
    assert key not in doc and set(doc) == {"count", "days", *tgs.PAPER_KEYS} - {key}
    assert not gates.evaluate(gates.PAPER_TO_BUTTON[HEDGE], doc).passed


def test_paper_range_boundaries_are_valid():
    edge = {"count": 60, "days": 20.0, "ratio_ok_share": 1.0, "cost_to_buffer": 0.0, "sigma_ratio": 0.0}
    assert tgs.aggregate_paper({"BTC": edge})[0] == {"count": 60, "days": 20.0, "ratio_ok_share": 1.0, "cost_to_buffer": 0.0,
                                                     "sigma_ratio": 0.0}
    assert tgs.build_paper(edge, NOW)["strategies"][HEDGE] == {"count": 60, "days": 20.0, "ratio_ok_share": 1.0,
                                                                "cost_to_buffer": 0.0, "sigma_ratio": 0.0}
    zero = dict(edge, ratio_ok_share=0.0)
    assert tgs.build_paper(zero, NOW)["strategies"][HEDGE]["ratio_ok_share"] == 0.0


def test_main_drops_negative_cost_from_paper_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    fill(os.path.join(root, "data", "paper.db"), 50, 15)
    real = simperp.gate_stats
    monkeypatch.setattr(tgs.simperp, "gate_stats", lambda *a, **kw: dict(real(*a, **kw), cost_to_buffer=-1.0))
    assert run_main(root) == 0
    out = capsys.readouterr().out
    st = data_file(root, "gates_paper.json")["strategies"][HEDGE]
    assert "cost_to_buffer" not in st and st["count"] == 50 and "ratio_ok_share" in st
    assert "Не записаны ключи: cost_to_buffer" in out and "вне допустимого диапазона" in out
    assert _mode(root) == "paper"                                                   # -1 ≤ 0.6 не проходит «как есть»


@pytest.mark.parametrize("where,key,bad", [("windows", "cost_to_buffer", -0.1), ("windows", "sigma_ratio", -0.1),
                                            ("lots", "in_band_share", 1.2), ("lots", "in_band_share", -0.1)])
def test_build_backtest_out_of_range_number_omits_the_key(where, key, bad):
    c = coin()
    if where == "windows":
        c["windows"]["bybit"][1][key] = bad
        omitted = key
    else:
        c["lots"]["bybit"]["10000"][key] = bad
        omitted = "ratio_ok_share"
    st, notes = tgs.build_backtest(result(BTC=c), ["BTC"], {}, ("bybit",), "s", NOW)
    st = st["strategies"][HEDGE]
    assert omitted not in st and {"count", "days"} <= set(st)
    assert any(omitted in n and "не записан" in n for n in notes)
    assert not gates.evaluate(gates.BACKTEST_STRONG[HEDGE], st).passed


# --- неизвестная стоимость хеджа --------------------------------------------------------------------------------------------

def test_unknown_cost_share_above_ten_percent_blocks_cost_to_buffer(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 44, 15)
    fill(db, 6, 15, ref_close=None)                                                 # 6 из 50 (12%): курса нет, стоимость неизвестна
    per, _ = tgs.paper_stats(db, ["BTC"], {}, now=NOW)
    assert per["BTC"]["count"] == 50 and per["BTC"]["cost_unknown"] == 6
    assert per["BTC"]["cost_to_buffer"] is not None                                 # simperp усредняет лишь известные — в этом подвох
    assert run_main(root) == 0
    out = capsys.readouterr().out
    st = data_file(root, "gates_paper.json")["strategies"][HEDGE]
    assert "cost_to_buffer" not in st and st["count"] == 50 and "ratio_ok_share" in st and "sigma_ratio" in st
    assert _mode(root) == "paper"
    assert "стоимость хеджа неизвестна у 12% закрытых хеджей" in out and "cost_to_buffer НЕ пишется" in out
    assert "стоимость неизвестна у 6 из 50" in out and "не пишется: стоимость неизвестна" in out


def test_unknown_cost_share_of_exactly_ten_percent_is_allowed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    db = os.path.join(root, "data", "paper.db")
    fill(db, 45, 15)
    fill(db, 5, 15, ref_close=None)                                                 # 5 из 50 = 10%: не больше порога
    assert run_main(root) == 0
    out = capsys.readouterr().out
    st = data_file(root, "gates_paper.json")["strategies"][HEDGE]
    assert "cost_to_buffer" in st and _mode(root) == "confirm"
    assert "стоимость неизвестна у 5 из 50" in out and "неизвестна у 10%" not in out


def test_unknown_cost_is_counted_only_for_closed_hedges_with_result_and_needs_a_buffer(tmp_path):
    db = str(tmp_path / "paper.db")
    fill(db, 6, 5)
    fill(db, 3, 5, closed=False, ref_close=None)                                    # открытые в count не идут — и здесь тоже
    per, _ = tgs.paper_stats(db, ["BTC"], {}, now=NOW, buffers={"BTC": 0.3})
    assert per["BTC"]["count"] == 6 and per["BTC"]["cost_unknown"] == 0
    per, _ = tgs.paper_stats(db, ["BTC"], {}, now=NOW, buffers={})                  # у монеты нет запаса — стоимость в среднее не идёт
    assert per["BTC"]["count"] == 6 and per["BTC"]["cost_unknown"] == 6 and per["BTC"]["cost_to_buffer"] is None
    assert tgs.cost_gaps({"BTC": {"count": 6, "cost_unknown": 1}}) == {"BTC": 1 / 6}
    assert tgs.cost_gaps({"BTC": {"count": 4, "cost_unknown": 4}}) == {}            # мало данных — это уже другая блокировка
    assert tgs.cost_gaps({"BTC": {"count": 20, "cost_unknown": 2}}) == {}           # ровно 10%


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
    assert "confirm: РЕАЛЬНЫЕ ордера по вашей кнопке" in text
    assert "бот разрешит confirm сразу, без minlot" in text and "confirm не ставьте, не увидев" in text
    only_bt = "\n".join(tgs.verdict_lines(tgs.verdict({"count": 0, "days": 0.0}, BT_OK, False)))
    assert "minlot: РЕАЛЬНЫЕ ордера, позиция не больше 50 USDT" in only_bt
    assert "БЕЗ единого дня бумажной истории" in only_bt and "минуя minlot" in only_bt   # лестница режимов без прикрас
    assert "trading_hedge_check.py → TRADING_MODE=paper → --backtest → TRADING_MODE=minlot" in only_bt
    idle = "\n".join(tgs.verdict_lines(tgs.verdict(PAPER_OK, BT_WEAK, True)))
    assert "не действует: бэктест не сильный или его нет" in idle


def test_script_imports_no_network_and_no_research():
    src = open(os.path.join(ROOT, "scripts", "trading_gates_stats.py"), encoding="utf-8").read()
    for bad in ("import socket", "import requests", "import urllib", "import aiohttp", "import http", "import research",
                "from research", "import_module"):
        assert bad not in src, bad


# --- раунд 4: --venues без Bybit, сбой обезвреживания, код выхода, границы неизвестной стоимости --------------------------

def _mixed_runner(calls, bybit_cost=2.0, bingx_cost=0.2):
    """research.report-заглушка: на Bybit стоимость хеджа плохая, на BingX хорошая — бэктест по одной BingX выглядел бы
    сильным. Каждый вызов пишется в calls."""
    def runner(cmd, cwd):
        calls.append(cmd)
        c = coin(cost=bingx_cost)
        c["windows"]["bybit"][1]["cost_to_buffer"] = bybit_cost
        res = result(BTC=c)
        res["coins"]["BTC"]["buffer_pct"] = 1.0                                  # как RISK_BUFFER из _env: запас не ругается
        with open(os.path.join(cmd[cmd.index("--out") + 1], "backtest_report.json"), "w", encoding="utf-8") as f:
            json.dump({"hedge": res}, f)
        return 0
    return runner


@pytest.mark.parametrize("raw", ["bingx", "BingX", "bingx,bingx"])
def test_backtest_without_bybit_is_refused_before_the_process(tmp_path, capsys, monkeypatch, raw):
    root = _strong_file_root(tmp_path, monkeypatch)                              # старый сильный файл: minlot
    assert _mode(root) == "minlot"
    capsys.readouterr()
    calls = []
    assert run_main(root, "--backtest", "--venues", raw, runner=_mixed_runner(calls)) == 2
    out = capsys.readouterr().out
    assert calls == []                                                           # research.report не запускался
    assert f"⛔ --venues {raw}: для записи gates_backtest.json в бэктесте обязательно bybit" in out
    assert "research.report не запускался" in out and "с --dry-run" in out
    assert _bt_doc(root) == {} and _mode(root) == "paper"                        # бэктест по BingX minlot не открыл


def test_backtest_without_bybit_and_without_old_file_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    calls = []
    assert run_main(root, "--backtest", "--venues", "bingx", runner=_mixed_runner(calls)) == 2
    out = capsys.readouterr().out
    assert calls == [] and "Старого gates_backtest.json нет" in out and "обязательно bybit" in out
    assert os.listdir(os.path.join(root, "data")) == []                          # ни бумаги, ни бэктеста
    assert gates.load_backtest(HEDGE, root=root)[0] is None and _mode(root) == "paper"


def test_backtest_with_both_venues_still_works_and_takes_the_worst(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC")
    root = bot_root(tmp_path)
    calls = []
    assert run_main(root, "--backtest", "--venues", "bybit,bingx", runner=_mixed_runner(calls)) == 0
    assert len(calls) == 1 and "обязательно" not in capsys.readouterr().out
    assert _bt_doc(root)["cost_to_buffer"] == 2.0 and _mode(root) == "paper"      # худшая площадка, а не «по BingX»
    assert run_main(root, "--backtest", "--venues", " BingX , bybit", runner=_runner) == 0   # порядок и регистр не важны
    assert _bt_doc(root)["count"] > 900 and _mode(root) == "minlot"


def test_dry_run_with_bingx_only_prints_the_verdict_but_writes_nothing(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    data = os.path.join(root, "data")
    path = os.path.join(data, "gates_backtest.json")
    before, names = open(path, "rb").read(), sorted(os.listdir(data))
    capsys.readouterr()
    calls = []
    assert run_main(root, "--backtest", "--dry-run", "--venues", "bingx", runner=_mixed_runner(calls)) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1 and "--dry-run: файлы не записаны" in out and "обязательно" not in out
    assert "разрешил бы реальные ордера minlot" in out                          # по одной BingX цифры красивые — потому и без записи
    assert open(path, "rb").read() == before and sorted(os.listdir(data)) == names
    assert _mode(root) == "minlot"                                               # старое не тронуто и не заменено
    root2 = bot_root(tmp_path / "two")
    assert run_main(root2, "--backtest", "--dry-run", "--venues", "bingx", runner=_mixed_runner([])) == 0
    assert os.listdir(os.path.join(root2, "data")) == []


def test_failed_invalidation_refuses_before_the_process_and_keeps_old_bytes(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)
    path = os.path.join(root, "data", "gates_backtest.json")
    before = open(path, "rb").read()
    real = tgs.jsonstore.write_dict

    def deny(p, doc):
        if os.path.basename(p) == gates.BACKTEST_FILE:
            raise PermissionError("access denied")
        return real(p, doc)
    monkeypatch.setattr(tgs.jsonstore, "write_dict", deny)
    capsys.readouterr()
    calls = []
    assert run_main(root, "--backtest", runner=_mixed_runner(calls)) == 2
    out = capsys.readouterr().out
    assert calls == []                                                           # без обезвреживания расчёт не начинается
    assert "⛔ старый gates_backtest.json не удалось обезвредить: PermissionError: access denied" in out
    assert "бэктест не запускаю" in out
    assert open(path, "rb").read() == before                                     # старые байты на месте, файл не тронут


def test_strong_report_with_nonzero_exit_code_is_refused(tmp_path, capsys, monkeypatch):
    root = _strong_file_root(tmp_path, monkeypatch)

    def runner(cmd, cwd):
        _runner(cmd, cwd)                                                        # валидный сильный отчёт лежит на месте...
        return 1                                                                 # ...но процесс закончился с ошибкой
    capsys.readouterr()
    assert run_main(root, "--backtest", runner=runner) == 2
    out = capsys.readouterr().out
    assert "⛔ бэктест не получился: research.report завершился с кодом 1" in out
    assert _bt_doc(root) == {} and _mode(root) == "paper"


def test_cost_gap_and_unknown_cost_boundaries():
    assert tgs.cost_gaps({"BTC": {"count": 5, "cost_unknown": 2}}) == {"BTC": 0.4}      # ровно MIN_COIN_HEDGES: уже считаем
    assert tgs.cost_gaps({"BTC": {"count": 4, "cost_unknown": 2}}) == {}                # мало данных — другая блокировка
    closed_no_pnl = [(1, "BTC", 10000, json.dumps({"status": "closed"}), 0.1, 0.0)]
    assert tgs._cost_unknown(closed_no_pnl, "BTC", {"BTC": 1.0}) == 0                   # закрыт без итога: gate_stats его не считает
    no_rate = [(1, "BTC", 10000, json.dumps({"status": "closed", "pnl_pct": 0.1}), 0.1, 0.0)]
    assert tgs._cost_unknown(no_rate, "BTC", {"BTC": 1.0}) == 1                         # итог есть, курса нет — стоимость неизвестна
    with_rate = [(1, "BTC", 10000, json.dumps({"status": "closed", "pnl_pct": 0.1, "ref_close": 90.0}), 0.1, 0.0)]
    assert tgs._cost_unknown(with_rate, "BTC", {"BTC": 1.0}) == 0
    assert tgs._cost_unknown(with_rate, "BTC", {}) == 1                                 # у монеты нет запаса — в среднее не идёт
    still_open = [(1, "BTC", 10000, json.dumps({"status": "open", "pnl_pct": 0.1}), 0.1, 0.0)]
    assert tgs._cost_unknown(still_open, "BTC", {"BTC": 1.0}) == 0
