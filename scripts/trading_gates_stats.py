"""Пороги хеджа: считает статистику бумаги и бэктеста и пишет локальные файлы data/gates_*.json — их читает бот.

Зачем: хедж (шорт перпа на Bybit против монеты круга) не выйдет из режима paper, пока нет файлов
data/gates_paper.json (и, для minlot, data/gates_backtest.json): trading/gates.py берёт статистику только оттуда, а
записать их некому. Этот скрипт запускает ВЛАДЕЛЕЦ на ПК (не облачная рутина, не бот); файлы не в git.

    python scripts/trading_gates_stats.py                  # бумага: paper.db -> data/gates_paper.json
    python scripts/trading_gates_stats.py --backtest       # ещё и бэктест (нужен интернет, идёт долго)
    python scripts/trading_gates_stats.py --dry-run        # только показать, ничего не записывать
Параметры: --bot-dir ПАПКА_БОТА, --paper-db, --history-db, --venues bybit[,bingx], для бэктеста --cache ПАПКА,
--offline (только кеш), --start ГГГГ-ММ-ДД. Живая статистика (gates_live.json) здесь не считается: она нужна на
этапе «кнопка → авто».

Какие круги и монеты считаются (худший случай, а не среднее по всем)
- Только монеты, которые бот вообще хеджирует: HEDGE_ASSETS (по умолчанию BTC, ETH, TON).
- ETH хеджируется только с кругов от 20 000 ₽ (решение владельца 29.09; HEDGE_MIN_AMOUNT_RUB=ETH:20000, формат
  «МОНЕТА:сумма,…»). Меньшие круги ETH в статистику бумаги не идут, а в бэктесте ETH проверяется только на суммах
  круга от этого минимума.
- Нет данных хотя бы по одной хеджируемой монете — ключ в файл НЕ пишется (порог не пройден: «нет данных»), а не
  считается по остальным монетам. Не нужна монета — уберите её из HEDGE_ASSETS.

Бумага -> data/gates_paper.json (simperp.gate_stats по копии paper.db; в копии оставлены только подходящие круги)
  count           закрытых хеджей с итогом (0 — честный ноль)
  days            дней с первого подходящего хеджа
  ratio_ok_share  доля подходящих хеджей с коэффициентом 1 ± 0.1
  cost_to_buffer  средняя стоимость хеджа / запас на курс (RISK_BUFFER монеты)
  sigma_ratio     σ(факт − план) с хеджем / без хеджа (нужно ≥ 2 исполненных круга)
  Нет данных — ключа нет (кроме count и days). Цифры не округляются в лучшую сторону и не выдумываются.

Бэктест -> data/gates_backtest.json (research.hedge_bt по публичной истории Bybit/BingX за 12 месяцев; сам бэктест
запускается отдельным процессом `python -m research.report` — скриптам research импортировать нельзя). Окно хеджа —
60 минут (главное окно research); площадки — только те, что в --venues (по умолчанию bybit: боевой хедж — Bybit).
  cost_to_buffer  МАКСИМУМ по монетам и площадкам: стоимость хеджа в окне 60 мин / запас на курс монеты
  sigma_ratio     МАКСИМУМ по монетам и площадкам: σ с хеджем / σ без хеджа в окне 60 мин
  ratio_ok_share  МИНИМУМ по монетам, площадкам и подходящим суммам круга (10 000 и 20 000 ₽; ETH — только от 20 000):
                  доля дней, когда после округления до шага лота коэффициент хеджа в полосе 0.9–1.1
  count           МИНИМУМ по монетам: число смоделированных 60-минутных окон (это не реальные хеджи)
  days            МИНИМУМ по монетам: сколько суток истории эти окна покрывают (часов окон / 24)
  research_sha    хеш кода research/: пересчитали research/ — файл бот не примет, бэктест надо повторить.
Честная оговорка: count и days бэктеста показывают глубину истории, а не «хеджи владельца»; пороги по стоимости,
σ и лоту — настоящие проверки. Значение "nan", строка или пустота -> ключа нет.

Оговорка про бумагу: бумажный хедж считает шорт на более дешёвой из двух бирж (обычно BingX: тейкер 0.05% против
0.055% у Bybit), а боевой — только на Bybit, поэтому стоимость на бумаге чуть оптимистичнее боевой.

Сеть скрипт сам не трогает (только запуск research.report при --backtest); ордеров и ключей нет.
"""
import argparse
import datetime
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import jsonstore  # noqa: E402
import p2p  # noqa: E402
import paper  # noqa: E402
import simperp  # noqa: E402
from trading import gates  # noqa: E402

HEDGE = gates.HEDGE
DEFAULT_MIN_RUB = {"ETH": 20000.0}   # решение владельца 29.09: ETH хеджируется только с кругов от 20 000 ₽
MIN_AMOUNT_ENV = "HEDGE_MIN_AMOUNT_RUB"
VENUE_CHOICES = ("bybit", "bingx")
DEFAULT_START = "2023-01-01"   # как у research/report.py: кеш публичных данных общий
MINLOT_POSITION_USDT = 50      # trading/risk.py MINLOT: в режиме minlot позиция не больше 50 USDT
PAPER_KEYS = ("ratio_ok_share", "cost_to_buffer", "sigma_ratio")   # кроме count и days: без данных ключа нет


# --- разбор настроек --------------------------------------------------------------------------------------------------

def parse_min_amounts(raw):
    """HEDGE_MIN_AMOUNT_RUB «ETH:20000,TON:5000» -> ({монета: сумма ₽}, [непонятные куски]). Переменной нет или в ней
    нет ни одной понятной пары — по умолчанию ETH:20000; есть — берутся её пары (полностью вместо умолчания)."""
    if raw is None:
        return dict(DEFAULT_MIN_RUB), []
    out, bad = {}, []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        coin, sep, amount = part.partition(":")
        try:
            value = float(amount)
        except ValueError:
            value = None
        if not sep or not coin.strip() or value is None or not math.isfinite(value) or value < 0:
            bad.append(part)
            continue
        out[coin.strip().upper()] = value
    return (out or dict(DEFAULT_MIN_RUB)), bad


def _finite(v):
    """Число для файла: int/float (не bool, не nan/inf) или None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _eligible(asset, amount, assets, mins):
    asset = (asset or "").upper()
    if asset not in assets:
        return False
    need = mins.get(asset, 0.0)
    if need <= 0:
        return True
    value = _finite(amount)
    return value is not None and value + 1e-6 >= need


# --- бумага -----------------------------------------------------------------------------------------------------------

def _copy_db(src, dst):
    """Копия paper.db (sqlite backup) — оригинал открывается только на чтение и не меняется."""
    con = sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    try:
        con.backup(out)
    finally:
        out.close()
        con.close()


def paper_stats(db, assets, mins, now=None):
    """(simperp.gate_stats по подходящим кругам, {"db": есть ли файл, "kept": хеджей в счёт, "dropped": не в счёт}).
    Считает по КОПИИ базы: paper._connect дописывает колонки, а чужую базу трогать нельзя."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "paper.db")
        if not os.path.exists(db):   # gate_stats по несуществующему пути базы не создаёт: нули и None
            return simperp.gate_stats(copy, now=now), {"db": False, "kept": 0, "dropped": 0}
        _copy_db(db, copy)
        con = paper._connect(copy)
        try:
            rows = con.execute("SELECT id, buy_asset, amount FROM cycles WHERE hedge_state != ''").fetchall()
            drop = [(r[0],) for r in rows if not _eligible(r[1], r[2], assets, mins)]
            con.executemany("DELETE FROM cycles WHERE id = ?", drop)
            con.commit()
        finally:
            con.close()
        stats = simperp.gate_stats(copy, now=now)
    return stats, {"db": True, "kept": len(rows) - len(drop), "dropped": len(drop)}


def build_paper(stats, now=None):
    """Содержимое data/gates_paper.json (формат gates.load_paper). count и days пишутся всегда (0 — честный ноль), остальные
    ключи — только если по ним есть конечное число: нет данных — нет ключа, порог не пройден."""
    now = time.time() if now is None else now
    days = _finite(stats.get("days"))
    count = _finite(stats.get("count"))
    out = {"count": int(count) if count is not None and count > 0 else 0,
           "days": round(days, 4) if days is not None and days > 0 else 0.0}
    for key in PAPER_KEYS:
        v = _finite(stats.get(key))
        if v is not None:
            out[key] = v
    return {"version": gates.STATS_VERSION, "generated_at": now, "strategies": {HEDGE: out}}


# --- бэктест ----------------------------------------------------------------------------------------------------------

def _window(coin_res, venue, minutes):
    for w in (coin_res.get("windows") or {}).get(venue) or ():
        if isinstance(w, dict) and w.get("minutes") == minutes:
            return w
    return None


def _lot_share(coin_res, venue, amount):
    row = ((coin_res.get("lots") or {}).get(venue) or {})
    cell = row.get(str(amount))
    if cell is None and isinstance(amount, float) and amount.is_integer():
        cell = row.get(str(int(amount)))
    return _finite(cell.get("in_band_share")) if isinstance(cell, dict) else None


def _aggregate(name, samples, pick, notes, omitted, fmt=lambda x: f"{x:g}"):
    """samples — [(подпись, число или None)]; хоть одно None — ключ не пишем; иначе — худшее (pick = max или min)."""
    if not samples:
        omitted[name] = "нечего считать"
        notes.append(f"  {name}: нет данных (нечего считать) — ключ не записан")
        return None
    missing = [label for label, v in samples if v is None]
    if missing:
        omitted[name] = "нет числа: " + ", ".join(missing)
        notes.append(f"  {name}: нет числа по {', '.join(missing)} — ключ не записан (порог не пройден)")
        return None
    label, value = pick(samples, key=lambda x: x[1])
    notes.append(f"  {name} = {fmt(value)} — худшее среди {len(samples)}: {label}")
    return value


def build_backtest(result, assets, mins, venues=("bybit",), sha="", now=None):
    """(содержимое data/gates_backtest.json, [строки про то, откуда каждая цифра]). result — итог research.hedge_bt.run
    (ключ "hedge" в backtest_report.json). Худший случай по хеджируемым монетам (assets), площадкам (venues) и
    подходящим суммам круга (mins); чего не хватает — ключа нет (см. docstring модуля)."""
    now = time.time() if now is None else now
    params = result.get("params") or {}
    main_min = params.get("main_window") or 60
    amounts = [a for a in (params.get("amounts_rub") or ()) if _finite(a) is not None]
    coins = result.get("coins") or {}
    notes = [f"Бэктест: окно {main_min} мин, площадки: {', '.join(venues)}; монеты: {', '.join(assets) or 'нет'}"]
    omitted = {}
    usable = {}
    for coin in assets:
        c = coins.get(coin)
        if isinstance(c, dict) and c.get("ok"):
            usable[coin] = c
        else:
            why = (c or {}).get("reason") if isinstance(c, dict) else "монеты нет в результате бэктеста"
            omitted[coin] = why or "нет данных"
            notes.append(f"  {coin}: нет оценки ({why}) — хедж по ней бэктестом не подтверждён")
    stats = {}
    if not assets or omitted:
        if not assets:
            notes.append("  HEDGE_ASSETS пуст — хеджировать нечего, ключи не записаны")
        else:
            notes.append("  Нет данных по " + ", ".join(sorted(omitted)) + " — ключи порогов не записаны. Если хедж "
                         "по этим монетам не нужен, уберите их из HEDGE_ASSETS в .env и пересчитайте.")
        doc = {"version": gates.BACKTEST_VERSION, "generated_at": now, "research_sha": sha,
               "strategies": {HEDGE: stats}}
        return doc, notes
    per = [(coin, venue) for coin in assets for venue in venues]
    wins = {(coin, venue): _window(usable[coin], venue, main_min) for coin, venue in per}
    ns = {}
    for coin in assets:
        vals = [_finite((wins[(coin, v)] or {}).get("n")) for v in venues]
        ns[coin] = None if any(x is None for x in vals) else min(vals)
    count = _aggregate("count", [(f"{coin}: {ns[coin]}" if ns[coin] is not None else coin, ns[coin]) for coin in assets],
                       min, notes, omitted, fmt=lambda x: f"{int(x)}")
    if count is not None:
        stats["count"] = int(count)
        stats["days"] = round(count / 24, 2)
        notes.append(f"  days = {stats['days']:g} — те же окна, часов / 24 (глубина истории, а не хеджи владельца)")
    for key in ("cost_to_buffer", "sigma_ratio"):
        v = _aggregate(key, [(f"{coin} на {venue}", _finite((wins[(coin, venue)] or {}).get(key)))
                             for coin, venue in per], max, notes, omitted, fmt=lambda x: f"{x:.3f}")
        if v is not None:
            stats[key] = v
    shares = []
    for coin in assets:
        ok_amounts = [a for a in amounts if a >= mins.get(coin, 0.0)]
        if not ok_amounts:
            shares.append((f"{coin}: нет подходящей суммы круга (минимум {mins.get(coin, 0):g} ₽)", None))
        for amount in ok_amounts:
            for venue in venues:
                shares.append((f"{coin} {amount:g} ₽ на {venue}", _lot_share(usable[coin], venue, amount)))
    v = _aggregate("ratio_ok_share", shares, min, notes, omitted, fmt=lambda x: f"{x:.3f}")
    if v is not None:
        stats["ratio_ok_share"] = v
    doc = {"version": gates.BACKTEST_VERSION, "generated_at": now, "research_sha": sha, "strategies": {HEDGE: stats}}
    return doc, notes


def _run_report(cmd, cwd):
    """Запуск research.report отдельным процессом (вывод — в консоль владельца); код возврата."""
    return subprocess.run(cmd, cwd=cwd, check=False).returncode


def run_research(bot_dir, out_dir, paper_db, history_db, cache=None, offline=False, start=DEFAULT_START, runner=None):
    """Бэктест хеджа -> результат hedge_bt (словарь) или ValueError с причиной. sha кода research/ до и после —
    один и тот же, иначе результат считал уже другой код."""
    sha = gates.research_sha(bot_dir)
    cmd = [sys.executable, "-m", "research.report", "--out", out_dir, "--start", start, "--sims", "10"]
    if cache:
        cmd += ["--cache", cache]
    if offline:
        cmd.append("--offline")
    if paper_db and os.path.exists(paper_db):
        cmd += ["--paper-db", paper_db]
    if history_db and os.path.exists(history_db):
        cmd += ["--history-db", history_db]
    code = (runner or _run_report)(cmd, bot_dir)
    if code != 0:
        raise ValueError(f"research.report завершился с кодом {code}")
    if gates.research_sha(bot_dir) != sha:
        raise ValueError("код research/ изменился во время расчёта — результат не принят, повторите")
    try:
        with open(os.path.join(out_dir, "backtest_report.json"), encoding="utf-8") as f:
            rep = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"нет читаемого backtest_report.json: {e}")
    hedge = rep.get("hedge") if isinstance(rep, dict) else None
    if not isinstance(hedge, dict) or not isinstance(hedge.get("coins"), dict):
        raise ValueError("в backtest_report.json нет раздела hedge")
    return hedge, sha


# --- вердикт ----------------------------------------------------------------------------------------------------------

OPS = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "="}


def _fmt(key, v, limit=False):
    if v is None:
        return "нет данных"
    if key == "ratio_ok_share":
        return f"{v * 100:.0f}%" if limit else f"{v * 100:.1f}%"
    if key == "count":
        return f"{v:g}" if limit else f"{int(v)}"
    if key == "days":
        return f"{v:g}" if limit else f"{v:.1f}"
    return f"{v:g}" if limit else f"{v:.3f}"


def _rows(rules, stats, overrides=None):
    rows = []
    for rule in rules:
        key, op, limit, what = rule
        limit = (overrides or {}).get(key, limit)
        value = _finite(stats.get(key)) if isinstance(stats, dict) else None
        ok = gates.evaluate([rule], stats, overrides).passed
        rows.append((ok, f"{what}: {_fmt(key, value)} (нужно {OPS[op]} {_fmt(key, limit, True)})"))
    return rows


def verdict(paper, backtest=None, short_paper=False):
    """Итог по порогам хеджа из чужих цифр: строки «значение против порога» и режим, который разрешают пороги
    (paper / minlot / confirm; auto — только с живой статистикой, здесь её нет). Логика — как у gates.max_mode: кнопка
    (confirm) — пороги «бумага → кнопка» (со сроком короче при сильном бэктесте и флаге), minlot — сильный бэктест."""
    strong_rules, paper_rules = gates.BACKTEST_STRONG[HEDGE], gates.PAPER_TO_BUTTON[HEDGE]
    strong = backtest is not None and gates.evaluate(strong_rules, backtest).passed
    overrides = gates.SHORT_PAPER.get(HEDGE) if strong and short_paper else None
    confirm = gates.evaluate(paper_rules, paper, overrides).passed
    return {"mode": "confirm" if confirm else ("minlot" if strong else "paper"),
            "strong": strong, "has_backtest": backtest is not None, "short_paper": bool(short_paper),
            "short_active": overrides is not None,
            "paper_rows": _rows(paper_rules, paper, overrides),
            "backtest_rows": _rows(strong_rules, backtest) if backtest is not None else []}


def _normal_days():
    return next(limit for key, _, limit, _ in gates.PAPER_TO_BUTTON[HEDGE] if key == "days")


MODE_TEXT = {
    "paper": "paper: только бумага, боевых ордеров нет",
    "minlot": f"minlot: минимальный лот, позиция не больше {MINLOT_POSITION_USDT} USDT",
    "confirm": "confirm: открытие по вашей кнопке, полные потолки из .env",
    "auto": "auto: автомат",
}


def verdict_lines(v, mode=None, source=""):
    """Русский текст вердикта. mode/source — режим, который разрешит сам бот (gates.max_mode по файлам), и откуда он."""
    mode = mode or v["mode"]
    lines = ["", "Пороги «бумага → кнопка» (статистика бумаги):"]
    lines += [f"  {'✅' if ok else '❌'} {text}" for ok, text in v["paper_rows"]]
    if v["has_backtest"]:
        lines += ["", "Пороги «сильный бэктест» (условие режима minlot):"]
        lines += [f"  {'✅' if ok else '❌'} {text}" for ok, text in v["backtest_rows"]]
    else:
        lines += ["", "Бэктеста нет (или бот его не принял) — режим minlot недоступен. Запустите с --backtest."]
    lines += ["", f"Разрешённый режим по порогам сейчас: {MODE_TEXT[mode]}." + (f" ({source})" if source else "")]
    lines.append(f"В режиме minlot риск-модуль режет позицию до {MINLOT_POSITION_USDT} USDT, поэтому ваш потолок "
                 "(TRADING_MAX_POSITION_USDT) работает только в режиме confirm.")
    short_days, normal = gates.SHORT_PAPER[HEDGE]["days"], _normal_days()
    if v["short_active"]:
        lines.append(f"TRADING_SHORT_PAPER=1 включён и бэктест сильный: срок бумаги для расчёта — {short_days:g} дн.")
    elif v["short_paper"]:
        lines.append("TRADING_SHORT_PAPER=1 включён, но сокращённый срок не действует: бэктест не сильный или его нет.")
    else:
        lines.append("Сократить срок бумаги можно флагом TRADING_SHORT_PAPER=1 в .env на ПК, но только при сильном "
                     "бэктесте" + (" (сейчас он сильный)." if v["strong"] else " (сейчас бэктеста нет или он не сильный)."))
    if short_days >= normal:
        lines.append(f"Для хеджа сокращённый срок в gates.SHORT_PAPER — {short_days:g} дн., как и обычный: флаг сейчас "
                     f"ничего не сокращает, а ≥ 50 закрытых хеджей нужны в любом случае.")
    lines.append("Живая статистика (gates_live.json) здесь не считается — она понадобится позже, на этапе «кнопка → авто».")
    return lines


# --- запуск -----------------------------------------------------------------------------------------------------------

def _load_env(path):
    p2p.load_env(path)


def _fail(msg):
    print(f"⛔ {msg}")
    return 2


def main(argv=None, runner=None, env_loader=_load_env, now=None):
    try:
        sys.stdout.reconfigure(errors="replace")   # консоль не в UTF-8 — «✅» не должен ронять скрипт
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Статистика порогов хеджа -> data/gates_paper.json, gates_backtest.json")
    ap.add_argument("--backtest", action="store_true", help="ещё и бэктест (интернет, долго)")
    ap.add_argument("--dry-run", action="store_true", help="только показать, файлы не писать")
    ap.add_argument("--bot-dir", default=ROOT, help="папка бота (там data/, research/, .env)")
    ap.add_argument("--paper-db", help="paper.db (по умолчанию <папка бота>/data/paper.db)")
    ap.add_argument("--history-db", help="history.db для бэктеста (по умолчанию <папка бота>/data/history.db)")
    ap.add_argument("--venues", default="bybit", help="площадки бэктеста: bybit (боевой хедж), bingx или обе")
    ap.add_argument("--cache", help="папка кеша публичных данных бэктеста")
    ap.add_argument("--offline", action="store_true", help="бэктест только из кеша, без интернета")
    ap.add_argument("--start", default=DEFAULT_START, help="начало истории бэктеста ГГГГ-ММ-ДД")
    a = ap.parse_args(argv)
    venues = [v.strip().lower() for v in a.venues.split(",") if v.strip()]
    if not venues or any(v not in VENUE_CHOICES for v in venues):
        return _fail(f"--venues: только {', '.join(VENUE_CHOICES)} через запятую")
    try:
        datetime.datetime.strptime(a.start, "%Y-%m-%d")
    except ValueError:
        return _fail("--start: дата в виде ГГГГ-ММ-ДД")
    bot_dir = os.path.abspath(a.bot_dir)
    data_dir = os.path.join(bot_dir, "data")
    paper_db = a.paper_db or os.path.join(data_dir, "paper.db")
    history_db = a.history_db or os.path.join(data_dir, "history.db")
    paper_path, bt_path = os.path.join(data_dir, gates.PAPER_FILE), os.path.join(data_dir, gates.BACKTEST_FILE)
    env_loader(os.path.join(bot_dir, ".env"))
    gates.flags_from_file(os.path.join(bot_dir, ".env"))
    now = time.time() if now is None else now
    assets = simperp.settings()["assets"]
    mins, bad = parse_min_amounts(os.getenv(MIN_AMOUNT_ENV))
    print("Хедж: пороги «бумага → кнопка» и режим, который они разрешают")
    print(f"Хеджируемые монеты (HEDGE_ASSETS): {', '.join(assets) or 'нет'}; минимальный круг: "
          + (", ".join(f"{c} от {x:g} ₽" for c, x in sorted(mins.items())) or "без ограничений"))
    if bad:
        print(f"⚠ В {MIN_AMOUNT_ENV} непонятно, пропущено: {', '.join(bad)} (формат МОНЕТА:сумма,…)")

    try:
        stats, info = paper_stats(paper_db, assets, mins, now=now)
    except (sqlite3.Error, OSError) as e:
        return _fail(f"paper.db не прочитана: {type(e).__name__}: {e}")
    doc_paper = build_paper(stats, now)
    got = doc_paper["strategies"][HEDGE]
    if not info["db"]:
        print(f"Бумага: файла {paper_db} нет — данных нет, в файл идёт count 0.")
    else:
        print(f"Бумага: подходящих хеджей в счёт — {info['kept']}, не в счёт — {info['dropped']} (монета не из HEDGE_ASSETS "
              f"или круг меньше минимума). Закрытых с итогом: {got['count']}, дней с первого хеджа: {got['days']:.1f}.")
        if got["count"] == 0:
            print("  Закрытых хеджей с итогом нет — цифр по стоимости и σ нет, порогов не пройти. Ничего не выдумываем.")
        if stats.get("pairs") is not None:
            print(f"  σ считается по {stats['pairs']} исполненным кругам (нужно ≥ 2).")
    print("  Бумажный хедж выбирает более дешёвую из Bybit/BingX (обычно BingX), боевой — только Bybit: стоимость на "
          "бумаге чуть оптимистичнее.")

    doc_bt, notes = None, []
    if a.backtest:
        print("Бэктест: запускаю research.report (публичные данные бирж, может занять много минут)...")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result, sha = run_research(bot_dir, tmp, paper_db, history_db, a.cache, a.offline, a.start, runner)
        except (ValueError, OSError) as e:
            return _fail(f"бэктест не получился: {e}")
        doc_bt, notes = build_backtest(result, assets, mins, tuple(venues), sha, now)
        print("\n".join(notes))

    if a.dry_run:
        print("\n--dry-run: файлы не записаны.")
        bt_stats = doc_bt["strategies"][HEDGE] if doc_bt else None
        v = verdict(got, bt_stats, gates.short_paper_enabled())
        print("\n".join(verdict_lines(v, source="расчёт по цифрам выше; бот файлов не читал")))
        return 0

    try:
        jsonstore.write_dict(paper_path, doc_paper)
        print(f"\nЗаписано: {paper_path}")
        if doc_bt is not None:
            jsonstore.write_dict(bt_path, doc_bt)
            print(f"Записано: {bt_path}")
    except OSError as e:
        return _fail(f"файл не записан: {type(e).__name__}: {e}")

    # что видит сам бот: те же загрузчики, что у journal (файл вне git, sha research/ совпал, время не из будущего)
    p_obj, why_p = gates.load_paper(HEDGE, path=paper_path, root=bot_dir)
    b_obj, why_b = gates.load_backtest(HEDGE, path=bt_path, root=bot_dir)
    print(f"Бот принял gates_paper.json: {'да' if p_obj else 'НЕТ — ' + why_p}")
    print(f"Бот принял gates_backtest.json: {'да' if b_obj else 'нет — ' + why_b}")
    v = verdict(got, b_obj.stats if b_obj else None, gates.short_paper_enabled())
    bot_mode = gates.max_mode(HEDGE, paper=p_obj.stats if p_obj else None, backtest=b_obj)
    src = "по файлам, как считает бот" if p_obj else "бот не принял файл бумаги — статистики для него нет"
    print("\n".join(verdict_lines(v, bot_mode, src)))
    if p_obj and bot_mode != v["mode"]:
        print(f"⚠ Расчёт по цифрам даёт {v['mode']}, бот — {bot_mode}: верьте боту.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
