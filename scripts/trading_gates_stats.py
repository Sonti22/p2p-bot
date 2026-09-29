"""Пороги хеджа: считает статистику бумаги и бэктеста и пишет локальные файлы data/gates_*.json — их читает бот.

Зачем: хедж (шорт перпа на Bybit против монеты круга) не выйдет из режима paper, пока нет файлов
data/gates_paper.json (и, для minlot, data/gates_backtest.json): trading/gates.py берёт статистику только оттуда, а
записать их некому. Этот скрипт запускает ВЛАДЕЛЕЦ на ПК (не облачная рутина, не бот); файлы не в git.

    python scripts/trading_gates_stats.py                  # бумага: paper.db -> ТОЛЬКО data/gates_paper.json
    python scripts/trading_gates_stats.py --backtest       # ещё и бэктест -> data/gates_backtest.json (интернет, долго)
    python scripts/trading_gates_stats.py --dry-run        # только показать, ничего не записывать
Параметры: --bot-dir ПАПКА_БОТА, --paper-db, --history-db, --venues bybit[,bingx], для бэктеста --cache ПАПКА,
--offline (только кеш), --start ГГГГ-ММ-ДД. Живая статистика (gates_live.json) здесь не считается: она нужна на
этапе «кнопка → авто».

Какие круги и монеты считаются (худший случай по монетам, а не среднее по всем)
- Только монеты, которые бот вообще хеджирует: HEDGE_ASSETS (по умолчанию BTC, ETH, TON).
- Настройки берутся у самого бота: trading.hedge.settings() (свой разбор здесь не держим — иначе разойдётся с ботом).
  ETH хеджируется только с кругов от 20 000 ₽ (решение владельца 29.09; HEDGE_MIN_AMOUNT_RUB=ETH:20000, формат
  «МОНЕТА:сумма,…»); пустое значение — без минимума; запись монеты с мусором («ETH:abc») — эту монету бот не хеджирует
  вовсе, и в статистику она не идёт. Меньшие круги в статистику бумаги не идут, а в бэктесте ETH проверяется только на
  суммах круга от минимума.

Бумага -> data/gates_paper.json (simperp.gate_stats по копии paper.db; в копии оставлены только подходящие круги)
Считается ОТДЕЛЬНО по каждой монете, чтобы плохая монета (например, ETH) не пряталась в среднем по BTC/ETH/TON:
  монета с числом закрытых хеджей меньше 5 — «мало данных»: в count не идёт, а главное — БЛОКИРУЕТ пороги (см. ниже);
  count           сумма закрытых хеджей с итогом по монетам с данными (0 — честный ноль)
  days            МИНИМУМ по монетам: дней с первого подходящего хеджа
  ratio_ok_share  МИНИМУМ по монетам: доля хеджей с коэффициентом 1 ± 0.1
  cost_to_buffer  МАКСИМУМ по монетам: средняя стоимость хеджа / запас на курс (RISK_BUFFER монеты)
  sigma_ratio     МАКСИМУМ по монетам: σ(факт − план) с хеджем / без хеджа (нужно ≥ 2 исполненных круга на монету)
  Нет данных хотя бы у одной монеты с данными — ключа нет (кроме count и days). Цифры не округляются в лучшую сторону
  и не выдумываются. Число вне допустимого диапазона (ratio_ok_share не в 0–1, cost_to_buffer или sigma_ratio меньше 0)
  считается битым: ключа нет. Рядом с ratio_ok_share печатается доля кругов без хеджа (статус none: плана нет) — они в
  долю не входят (эффект выжившего), это справка, порогом не проверяется.
  gates.load_paper не проверяет возраст файла: перезапускайте скрипт перед включением confirm и раз в неделю.
  БЛОКИРОВКА. Если ХОТЬ ОДНА монета, которую бот реально хеджирует (HEDGE_ASSETS и минимальный круг — из
  trading.hedge.settings(), а не из этого скрипта), в «мало данных», то ratio_ok_share, cost_to_buffer и sigma_ratio в
  gates_paper.json НЕ пишутся: порог не пройден (ядро лишних полей не читает, поэтому блокирует именно отсутствие
  настоящих ключей). Иначе бумага прошла бы по одной BTC, а ETH/TON бот хеджировал бы всерьёз без единого закрытого
  хеджа. Скрипт печатает, какая монета блокирует; выход — подождать данных или убрать монету из HEDGE_ASSETS.
  Стоимость хеджа неизвестна (нет курса или суммы круга, нет запаса монеты в RISK_BUFFER) у более чем 10% закрытых хеджей
  монеты — cost_to_buffer не пишется (simperp.gate_stats усредняет только известные стоимости, а count считает все).

Бэктест -> data/gates_backtest.json (research.hedge_bt по публичной истории Bybit/BingX за 12 месяцев; сам бэктест
запускается отдельным процессом `python -m research.report` — скриптам research импортировать нельзя; процесс
получает минимальное окружение и живёт не дольше 15 минут). Окно хеджа — 60 минут (главное окно research); площадки —
только те, что в --venues (по умолчанию bybit: боевой хедж — Bybit).
  cost_to_buffer  МАКСИМУМ по монетам и площадкам: стоимость хеджа в окне 60 мин / запас на курс монеты
  sigma_ratio     МАКСИМУМ по монетам и площадкам: σ с хеджем / σ без хеджа в окне 60 мин
  ratio_ok_share  МИНИМУМ по монетам, площадкам и подходящим суммам круга (10 000 и 20 000 ₽; ETH — только от 20 000):
                  доля дней, когда после округления до шага лота коэффициент хеджа в полосе 0.9–1.1
  count           МИНИМУМ по монетам: число смоделированных 60-минутных окон (это не реальные хеджи)
  days            МИНИМУМ по монетам: сколько суток истории эти окна покрывают (часов окон / 24)
  research_sha    хеш кода research/: пересчитали research/ — файл бот не примет, бэктест надо повторить.
Честная оговорка: count и days бэктеста — глубина истории, не независимые хеджи (окна перекрываются); пороги по
стоимости, σ и лоту — настоящие проверки. Значение "nan", строка или пустота -> ключа нет.
Запас на курс: бэктест считает по запасам research (BTC 0.3, ETH 0.5, TON 0.7), а бот — по RISK_BUFFER из .env. Если
в .env запас хоть по одной монете НИЖЕ запаса бэктеста (сверка по умолчаниям research — ДО запуска research.report, и
ещё раз по запасу самого расчёта), не число (inf, nan), не больше 0, выше 5 запасов research (для монеты без запаса
research — выше 5%) или RISK_BUFFER не разобрать — файл не пишется (fail closed); выше запаса бэктеста, но в пределах
5 запасов — только предупреждение. Значения вне диапазона (стоимость или σ меньше 0, доля вне 0–1) — как «нет числа».
Старый файл. ЛЮБОЙ запуск с --backtest (кроме --dry-run) заменяет gates_backtest.json пустым (без порогов) СРАЗУ,
ДО расчёта, — даже если процесс убьют посреди research.report, старый проходящий файл не останется. Свежий файл пишется
только после успешного расчёта, сверки запаса и проверки ботом (gates.load_backtest его принял); любой сбой (код ≠ 0,
таймаут, исключение, битый отчёт, смена sha research/, RISK_BUFFER) оставляет пустой файл. Запуск без --backtest
gates_backtest.json не трогает.

ВАЖНО: файл gates_backtest.json сам по себе (без дня бумажной истории) открывает РЕАЛЬНЫЕ ордера minlot (до 50 USDT) при
TRADING=1 и TRADING_MODE=minlot. Скрипт печатает об этом громкое предупреждение, когда пишет проходящий бэктест.

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
from trading import gates, hedge  # noqa: E402

HEDGE = gates.HEDGE
MIN_AMOUNT_ENV = "HEDGE_MIN_AMOUNT_RUB"
VENUE_CHOICES = ("bybit", "bingx")
DEFAULT_START = "2023-01-01"   # как у research/report.py: кеш публичных данных общий
MINLOT_POSITION_USDT = 50      # trading/risk.py MINLOT: в режиме minlot позиция не больше 50 USDT
PAPER_KEYS = ("ratio_ok_share", "cost_to_buffer", "sigma_ratio")   # кроме count и days: без данных ключа нет
KEY_RANGES = {"ratio_ok_share": (0.0, 1.0), "cost_to_buffer": (0.0, None), "sigma_ratio": (0.0, None)}   # вне — битое число
MIN_COIN_HEDGES = 5            # монета с меньшим числом закрытых хеджей — «мало данных»: не в count и блокирует пороги бумаги
COST_UNKNOWN_MAX = 0.10        # доля закрытых хеджей монеты с неизвестной стоимостью, выше которой cost_to_buffer не пишем
RISK_BUFFER_MAX_FACTOR = 5.0   # RISK_BUFFER монеты выше 5 запасов research — бессмыслица: порог «стоимость / запас» пустеет
RISK_BUFFER_MAX_ABS = 5.0      # %: то же для монеты, у которой запаса research нет
REPORT_TIMEOUT = 900           # сек: research.report дольше не ждём (таймаут — ничего не пишем)
CHILD_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE")   # всё окружение бэктесту не нужно (ключи, токены)
NET_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
# ^ прокси и сертификаты (и в нижнем регистре): research/data.py берёт прокси из getproxies_environment() — без них бэктест
#   за прокси историю не скачает. Ключей и токенов среди них нет.
BUFFER_ADVICE = ("поставьте RISK_BUFFER не ниже запаса бэктеста (BTC 0.3, ETH 0.5, TON 0.7 — значения по умолчанию) и не выше "
                 "5 таких запасов или уберите переменную и повторите.")
BACKTEST_UNLOCK = (f"бэктест разрешает реальные ордера minlot (до {MINLOT_POSITION_USDT} USDT) при TRADING=1 и "
                   "TRADING_MODE=minlot")


# --- разбор настроек --------------------------------------------------------------------------------------------------

def hedge_config():
    """(монеты HEDGE_ASSETS, {монета: минимальный круг ₽}) — ровно то, что видит бот: trading.hedge.settings(). Свой разбор
    HEDGE_MIN_AMOUNT_RUB здесь не держим (раньше расходился с ядром): пустое значение — {} (без минимума), «ETH:abc» и
    другая запись монеты с мусором — inf (монету бот не хеджирует), запись без монеты — пропуск."""
    st = hedge.settings()
    return list(st["assets"]), dict(st["min_amount"])


def _finite(v):
    """Число для файла: int/float (не bool, не nan/inf) или None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _ranged(key, v):
    """_finite и допустимый диапазон ключа порога (KEY_RANGES): вне диапазона — как «нет числа» (None). Ядро принимает
    отрицательную стоимость и долю больше 1 как прошедшие порог, поэтому проверяем здесь."""
    v = _finite(v)
    if v is None or key not in KEY_RANGES:
        return v
    lo, hi = KEY_RANGES[key]
    return None if (lo is not None and v < lo) or (hi is not None and v > hi) else v


def _eligible(asset, amount, assets, mins):
    """Хеджировал бы бот этот круг — те же условия, что в trading.hedge._offer: монета в HEDGE_ASSETS, круг не меньше
    минимума монеты (минимума нет — любой; inf — монету не хеджируем вовсе)."""
    asset = (asset or "").upper()
    if asset not in assets:
        return False
    need = mins.get(asset)
    if need is None:
        return True
    value = _finite(amount)
    return value is not None and value >= need


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


def _blank_coin():
    return {"count": 0, "days": 0.0, "ratio_ok_share": None, "cost_to_buffer": None, "sigma_ratio": None,
            "pairs": 0, "hedged": 0, "none": 0, "cost_unknown": 0}


def _hedge_state(state):
    try:
        st = json.loads(state or "{}")
    except (TypeError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def _hedge_status(state):
    return _hedge_state(state).get("status")


def _cost_unknown(rows, coin, buffers):
    """Сколько закрытых хеджей с итогом (их simperp.gate_stats считает в count) не попало в cost_to_buffer: нет курса или
    суммы круга (simperp.fact_cost_pct → None) или у монеты нет запаса в RISK_BUFFER. rows — (id, монета, сумма, hedge_state,
    hedge_fees, hedge_funding)."""
    buf, n = buffers.get(coin, 0.0), 0
    for r in rows:
        st = _hedge_state(r[3])
        if st.get("status") != "closed" or st.get("pnl_pct") is None:
            continue
        fact = simperp.fact_cost_pct({"amount": r[2], "hedge_fees": r[4], "hedge_funding": r[5]}, st)
        if fact is None or not buf > 0:
            n += 1
    return n


def paper_stats(db, assets, mins, now=None, buffers=None):
    """({монета: simperp.gate_stats по подходящим кругам монеты + "hedged" (открытые и закрытые), "none" (круги без хеджа) и
    "cost_unknown" (закрытых хеджей без известной стоимости)}, {"db": есть ли файл, "kept": хеджей в счёт, "dropped": не в
    счёт}). Каждая монета — отдельно (плохая монета не прячется в среднем по всем). Считает по КОПИИ базы: paper._connect
    дописывает колонки, а чужую базу трогать нельзя. buffers — RISK_BUFFER (по умолчанию из окружения)."""
    per = {coin: _blank_coin() for coin in assets}
    if not os.path.exists(db):   # gate_stats по несуществующему пути базы не создаёт: нули и None
        return per, {"db": False, "kept": 0, "dropped": 0}
    buffers = simperp.risk_buffers() if buffers is None else buffers
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "paper.db")
        _copy_db(db, base)
        con = paper._connect(base)
        try:
            rows = con.execute("SELECT id, buy_asset, amount, hedge_state, hedge_fees, hedge_funding FROM cycles "
                               "WHERE hedge_state != ''").fetchall()
        finally:
            con.close()
        kept = 0
        for coin in assets:
            mine = [r for r in rows if (r[1] or "").upper() == coin and _eligible(coin, r[2], assets, mins)]
            kept += len(mine)
            ids = {r[0] for r in mine}
            copy = os.path.join(tmp, f"{coin}.db")
            _copy_db(base, copy)
            out = sqlite3.connect(copy)
            try:
                out.executemany("DELETE FROM cycles WHERE id = ?", [(r[0],) for r in rows if r[0] not in ids])
                out.commit()
            finally:
                out.close()
            stats = simperp.gate_stats(copy, now=now, buffers=buffers)
            statuses = [_hedge_status(r[3]) for r in mine]
            per[coin] = {**_blank_coin(), **stats,
                         "hedged": sum(s in ("open", "closed") for s in statuses), "none": statuses.count("none"),
                         "cost_unknown": _cost_unknown(mine, coin, buffers)}
    return per, {"db": True, "kept": kept, "dropped": len(rows) - kept}


def cost_gaps(per, min_count=MIN_COIN_HEDGES):
    """{монета: доля} — монеты с данными, у которых стоимость хеджа неизвестна больше чем у COST_UNKNOWN_MAX закрытых хеджей.
    simperp.gate_stats усредняет cost_to_buffer только по известным стоимостям, а count считает все: при большой доле
    неизвестных среднее описывает лишь малую часть хеджей."""
    out = {}
    for coin, s in per.items():
        count = _finite(s.get("count")) or 0
        if count < min_count:
            continue
        share = (_finite(s.get("cost_unknown")) or 0) / count
        if share > COST_UNKNOWN_MAX + 1e-9:
            out[coin] = share
    return out


def aggregate_paper(per, min_count=MIN_COIN_HEDGES):
    """(статистика для gates_paper.json, [монеты «мало данных»]). В счёт — только монеты с числом закрытых хеджей не
    меньше min_count: count = сумма, days и ratio_ok_share — минимум, cost_to_buffer и sigma_ratio — максимум (худшая
    монета, как в бэктесте). Нет числа (или оно вне диапазона) хотя бы у одной монеты в счёте — этого ключа нет (порог
    не пройден). БЛОКИРОВКА: есть монета «мало данных» (бот её хеджирует, а данных нет) — ключей порогов нет вовсе;
    стоимость неизвестна у большой доли хеджей монеты (cost_gaps) — нет cost_to_buffer."""
    used = {c: s for c, s in per.items() if (_finite(s.get("count")) or 0) >= min_count}
    few = [c for c in per if c not in used]
    gaps = cost_gaps(per, min_count)
    out = {"count": int(sum(s["count"] for s in used.values())),
           "days": min((_finite(s.get("days")) or 0.0 for s in used.values()), default=0.0)}
    for key, pick in (("ratio_ok_share", min), ("cost_to_buffer", max), ("sigma_ratio", max)):
        if few or (key == "cost_to_buffer" and gaps):
            continue
        vals = [_ranged(key, s.get(key)) for s in used.values()]
        if vals and all(v is not None for v in vals):
            out[key] = pick(vals)
    return out, few


def none_note(per):
    """Справка про эффект выжившего: доля подходящих кругов, где хедж не строился (статус none). Круг ratio_ok_share
    считается только по кругам с хеджем."""
    none, total = sum(s["none"] for s in per.values()), sum(s["hedged"] + s["none"] for s in per.values())
    if not total:
        return "кругов с хеджем и без него пока нет"
    return f"круги без хеджа (none): {none} из {total} ({none / total * 100:.0f}%) — в ratio_ok_share не входят"


def _none_share_text(s):
    total = s["hedged"] + s["none"]
    return f"без хеджа none: {s['none']} из {total}" if total else "кругов нет"


def paper_lines(per, few):
    """Русский отчёт по монетам: цифры каждой; «мало данных» и неизвестная стоимость — громко, с причиной и выходом."""
    gaps = cost_gaps(per)
    lines = ["Бумага по монетам (каждая отдельно; в файл идёт худшая, а не среднее):"]
    for coin, s in per.items():
        if coin in few:
            lines.append(f"  {coin}: мало данных — закрытых хеджей {s['count']} (нужно ≥ {MIN_COIN_HEDGES}); "
                         "в count и в худший случай не входит, но БЛОКИРУЕТ пороги бумаги")
            continue
        lines.append(f"  {coin}: закрытых {s['count']}, дней {s['days']:.1f}, коэффициент в полосе "
                     f"{_fmt('ratio_ok_share', _ranged('ratio_ok_share', s['ratio_ok_share']))} ({_none_share_text(s)}), "
                     f"стоимость/запас {_fmt('cost_to_buffer', _ranged('cost_to_buffer', s['cost_to_buffer']))} "
                     f"(стоимость неизвестна у {s.get('cost_unknown', 0)} из {s['count']}), "
                     f"σ {_fmt('sigma_ratio', _ranged('sigma_ratio', s['sigma_ratio']))} (кругов {s['pairs']})")
    if few:
        lines.append(f"⛔⛔ ПОРОГИ БУМАГИ НЕ ПРОЙДУТ: по {', '.join(few)} мало данных (закрытых хеджей меньше "
                     f"{MIN_COIN_HEDGES}), а бот эти монеты хеджирует всерьёз (HEDGE_ASSETS и минимальный круг). "
                     "ratio_ok_share, cost_to_buffer и sigma_ratio в gates_paper.json НЕ пишутся. Подождите, пока по каждой "
                     f"из них наберётся ≥ {MIN_COIN_HEDGES} закрытых хеджей, или уберите её из HEDGE_ASSETS в .env и "
                     "пересчитайте.")
    for coin, share in gaps.items():
        lines.append(f"⛔ {coin}: стоимость хеджа неизвестна у {share * 100:.0f}% закрытых хеджей (нет курса или суммы круга, "
                     f"или монеты нет в RISK_BUFFER) — больше {COST_UNKNOWN_MAX * 100:.0f}%: cost_to_buffer НЕ пишется, "
                     "порог стоимости не пройден.")
    lines.append(f"  Справка: {none_note(per)}; чем их больше, тем хуже ratio_ok_share описывает все круги.")
    return lines


def paper_notes(per, few):
    """Пояснения к строкам вердикта {ключ: текст}: доля кругов без хеджа у ratio_ok_share и причины блокировки."""
    notes = {"ratio_ok_share": " · " + none_note(per)}
    if few:
        for key in PAPER_KEYS:
            notes[key] = notes.get(key, "") + f" — не пишется: по {', '.join(few)} мало данных"
    gaps = cost_gaps(per)
    if gaps:
        notes["cost_to_buffer"] = notes.get("cost_to_buffer", "") + (
            f" — не пишется: стоимость неизвестна у более чем {COST_UNKNOWN_MAX * 100:.0f}% хеджей ({', '.join(gaps)})")
    return notes


def build_paper(stats, now=None):
    """Содержимое data/gates_paper.json (формат gates.load_paper). count и days пишутся всегда (0 — честный ноль), остальные
    ключи — только если по ним есть конечное число: нет данных — нет ключа, порог не пройден."""
    now = time.time() if now is None else now
    days = _finite(stats.get("days"))
    count = _finite(stats.get("count"))
    out = {"count": int(count) if count is not None and count > 0 else 0,
           "days": round(days, 4) if days is not None and days > 0 else 0.0}
    for key in PAPER_KEYS:
        v = _ranged(key, stats.get(key))
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
    return _ranged("ratio_ok_share", cell.get("in_band_share")) if isinstance(cell, dict) else None


def _aggregate(name, samples, pick, notes, omitted, fmt=lambda x: f"{x:g}"):
    """samples — [(подпись, число или None)]; хоть одно None — ключ не пишем; иначе — худшее (pick = max или min)."""
    if not samples:
        omitted[name] = "нечего считать"
        notes.append(f"  {name}: нет данных (нечего считать) — ключ не записан")
        return None
    missing = [label for label, v in samples if v is None]
    if missing:
        omitted[name] = "нет числа: " + ", ".join(missing)
        notes.append(f"  {name}: нет числа по {', '.join(missing)} (пусто, nan, inf или вне допустимого диапазона) — ключ не "
                     "записан (порог не пройден)")
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
        notes.append(f"  days = {stats['days']:g} — те же окна, часов / 24")
        notes.append("  count и days — глубина истории, не независимые хеджи: 60-минутные окна перекрываются, "
                     f"{stats['count']} окон — это не {stats['count']} хеджей.")
    for key in ("cost_to_buffer", "sigma_ratio"):
        v = _aggregate(key, [(f"{coin} на {venue}", _ranged(key, (wins[(coin, venue)] or {}).get(key)))
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


_run_process = subprocess.run              # процессы запускаются только здесь; тесты подменяют эту ссылку
_ProcessTimeout = subprocess.TimeoutExpired


def _child_env(environ=None):
    """Окружение для research.report: PATH, SYSTEMROOT, TEMP/TMP, USERPROFILE, прокси и сертификаты (NET_ENV_KEYS в верхнем и
    нижнем регистре — research/data.py берёт прокси из окружения) и PYTHONIOENCODING. Остальное (ключи, токены, любые
    чужие переменные из .env) бэктесту не нужно и в дочерний процесс не идёт."""
    src = os.environ if environ is None else environ
    allowed = set(CHILD_ENV_KEYS) | set(NET_ENV_KEYS) | {k.lower() for k in NET_ENV_KEYS}
    env = {k: v for k, v in src.items() if k in allowed}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_report(cmd, cwd):
    """Запуск research.report отдельным процессом (вывод — в консоль владельца) с минимальным окружением и таймаутом
    REPORT_TIMEOUT; код возврата. Таймаут — ValueError: процесс остановлен, ничего не записывается."""
    try:
        return _run_process(cmd, cwd=cwd, check=False, env=_child_env(), timeout=REPORT_TIMEOUT).returncode
    except _ProcessTimeout:
        raise ValueError(f"research.report не уложился в {REPORT_TIMEOUT} с и остановлен — ничего не записано")


def _buffer_problem(coin, raw, default):
    """Текст, если запас монеты в RISK_BUFFER — бессмыслица, иначе None: не конечное число (inf, nan), не больше 0 или выше
    RISK_BUFFER_MAX_FACTOR запасов research (у монеты без запаса research — выше RISK_BUFFER_MAX_ABS %). Огромный запас
    обнуляет «стоимость / запас» (inf даёт 0) и открывает порог стоимости без настоящей экономии."""
    v = _finite(raw)
    if v is None:
        return f"{coin}: RISK_BUFFER = {raw} — не конечное число"
    if v <= 0:
        return f"{coin}: RISK_BUFFER = {v:g}% — запас на курс должен быть больше 0"
    cap = default[coin] * RISK_BUFFER_MAX_FACTOR if coin in default else RISK_BUFFER_MAX_ABS
    if v > cap + 1e-9:
        return (f"{coin}: RISK_BUFFER = {v:g}% — выше разумного предела {cap:g}%: такой запас обесценивает порог "
                "«стоимость / запас»")
    return None


def buffer_nonsense(env_buffers, assets):
    """[строки] по монетам из assets с бессмысленным запасом в RISK_BUFFER (см. _buffer_problem). Монеты в RISK_BUFFER нет —
    не бессмыслица (бот возьмёт запас самого круга): это только предупреждение в buffer_check."""
    default = p2p._fees(p2p.DEFAULT_RISK)
    out = []
    for coin in assets:
        if coin in env_buffers:
            problem = _buffer_problem(coin, env_buffers[coin], default)
            if problem:
                out.append(problem)
    return out


def buffer_check(result, assets, env_buffers):
    """([отказы], [предупреждения]) по запасу на курс. Бэктест считает по запасу монеты из research (buffer_pct в его итоге;
    result пуст — по умолчаниям research), бот — по RISK_BUFFER. Отказ: запас в .env бессмыслен (не конечное число, ≤ 0,
    слишком большой) или НИЖЕ бэктестового по какой-то монете (бэктест был бы слишком оптимистичен относительно того, что
    проверит бот). Выше бэктестового (в пределах разумного) или монеты в RISK_BUFFER нет — только предупреждение."""
    default = p2p._fees(p2p.DEFAULT_RISK)
    coins = result.get("coins") or {}
    refuse, warn = [], []
    for coin in assets:
        if coin in env_buffers:
            problem = _buffer_problem(coin, env_buffers[coin], default)
            if problem:
                refuse.append(problem)
                continue
        info = coins.get(coin) if isinstance(coins.get(coin), dict) else {}
        used = _finite(info.get("buffer_pct"))
        used = default.get(coin) if used is None else used
        if used is None or used <= 0:
            continue
        env = _finite(env_buffers.get(coin))
        if env is None:
            warn.append(f"{coin}: в RISK_BUFFER запаса нет — бот возьмёт запас самого круга; бэктест считал по {used:g}%")
        elif env < used - 1e-9:
            refuse.append(f"{coin}: RISK_BUFFER = {env:g}% ниже запаса бэктеста ({used:g}%) — стоимость / запас в боте выйдет "
                          "хуже, чем в бэктесте")
        elif env > used + 1e-9:
            warn.append(f"{coin}: RISK_BUFFER = {env:g}% выше запаса бэктеста ({used:g}%) — бэктест считал по меньшему "
                        "запасу; это не опасно, но цифры не совпадают с ботом")
    return refuse, warn


def _sha_or_empty(bot_dir):
    try:
        return gates.research_sha(bot_dir)
    except (OSError, ValueError):
        return ""


def _invalidate_backtest(path, sha, now):
    """Запуск с --backtest, который не кончится свежей проверенной записью: старый gates_backtest.json (проходящий, посчитанный
    при другом RISK_BUFFER или другим кодом) заменяем пустым без порогов атомарно — он не должен продолжать открывать minlot.
    Нет файла — ничего не пишем. Возвращает True, если файл был заменён."""
    if not os.path.exists(path):
        return False
    jsonstore.write_dict(path, {"version": gates.BACKTEST_VERSION, "generated_at": now, "research_sha": sha,
                                "strategies": {HEDGE: {}}})
    return True


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
    res = rep.get("hedge") if isinstance(rep, dict) else None
    if not isinstance(res, dict) or not isinstance(res.get("coins"), dict):
        raise ValueError("в backtest_report.json нет раздела hedge")
    return res, sha


# --- вердикт ----------------------------------------------------------------------------------------------------------

OPS = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "="}
BACKTEST_ROW_NOTES = {"count": " — окон истории, не независимые хеджи", "days": " — глубина истории"}


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


def _rows(rules, stats, overrides=None, notes=None):
    rows = []
    for rule in rules:
        key, op, limit, what = rule
        limit = (overrides or {}).get(key, limit)
        value = _finite(stats.get(key)) if isinstance(stats, dict) else None
        ok = gates.evaluate([rule], stats, overrides).passed
        rows.append((ok, f"{what}: {_fmt(key, value)} (нужно {OPS[op]} {_fmt(key, limit, True)})"
                         + (notes or {}).get(key, "")))
    return rows


def verdict(paper, backtest=None, short_paper=False, paper_notes=None):
    """Итог по порогам хеджа из чужих цифр: строки «значение против порога» и режим, который разрешают пороги
    (paper / minlot / confirm; auto — только с живой статистикой, здесь её нет). Логика — как у gates.max_mode: пороги
    бумаги пройдены (со сроком короче при сильном бэктесте и флаге) — сразу confirm, БЕЗ minlot; иначе сильный бэктест
    сам по себе — minlot (реальные ордера до 50 USDT без бумажной истории); иначе paper."""
    strong_rules, paper_rules = gates.BACKTEST_STRONG[HEDGE], gates.PAPER_TO_BUTTON[HEDGE]
    strong = backtest is not None and gates.evaluate(strong_rules, backtest).passed
    overrides = gates.SHORT_PAPER.get(HEDGE) if strong and short_paper else None
    confirm = gates.evaluate(paper_rules, paper, overrides).passed
    return {"mode": "confirm" if confirm else ("minlot" if strong else "paper"),
            "strong": strong, "has_backtest": backtest is not None, "short_paper": bool(short_paper),
            "short_active": overrides is not None,
            "paper_rows": _rows(paper_rules, paper, overrides, paper_notes),
            "backtest_rows": _rows(strong_rules, backtest, notes=BACKTEST_ROW_NOTES) if backtest is not None else []}


def _normal_days():
    return next(limit for key, _, limit, _ in gates.PAPER_TO_BUTTON[HEDGE] if key == "days")


MODE_TEXT = {
    "paper": "paper: только бумага, боевых ордеров нет",
    "minlot": f"minlot: РЕАЛЬНЫЕ ордера, позиция не больше {MINLOT_POSITION_USDT} USDT",
    "confirm": "confirm: РЕАЛЬНЫЕ ордера по вашей кнопке, полные потолки из .env",
    "auto": "auto: автомат",
}
LADDER = [
    "Лестница режимов (какой режим откроют пороги; реальные ордера есть во всех, кроме paper):",
    "  paper   — только бумага;",
    f"  minlot  — РЕАЛЬНЫЕ ордера до {MINLOT_POSITION_USDT} USDT. Открывает один сильный бэктест (gates_backtest.json) — БЕЗ "
    "единого дня бумажной истории;",
    "  confirm — РЕАЛЬНЫЕ ордера до потолка из .env (советуем 250 USDT), каждый по вашей кнопке. Открывают пороги бумаги "
    "(≥ 14 дней, ≥ 50 хеджей…) — СРАЗУ confirm, минуя minlot;",
    "  auto    — автомат: нужна живая статистика, здесь она не считается.",
    "Режим бота = меньший из TRADING_MODE в .env и режима порогов; при TRADING≠1 — всегда бумага.",
]
PATH_TEXT = ("Советуем идти так: trading_hedge_check.py → TRADING_MODE=paper → --backtest → TRADING_MODE=minlot на первые "
             "10–20 реальных кругов → только потом confirm (confirm не ставьте, не увидев, что minlot работает).")


def verdict_lines(v, mode=None, source=""):
    """Русский текст вердикта. mode/source — режим, который разрешит сам бот (gates.max_mode по файлам), и откуда он."""
    mode = mode or v["mode"]
    lines = ["", "Пороги «бумага → кнопка» (статистика бумаги, худший случай по монетам):"]
    lines += [f"  {'✅' if ok else '❌'} {text}" for ok, text in v["paper_rows"]]
    if v["has_backtest"]:
        lines += ["", f"Пороги «сильный бэктест» (один он открывает РЕАЛЬНЫЕ ордера minlot до {MINLOT_POSITION_USDT} USDT):"]
        lines += [f"  {'✅' if ok else '❌'} {text}" for ok, text in v["backtest_rows"]]
    else:
        lines += ["", "Бэктеста нет (или бот его не принял) — режим minlot недоступен. Запустите с --backtest."]
    lines += ["", f"Разрешённый режим по порогам сейчас: {MODE_TEXT[mode]}." + (f" ({source})" if source else "")]
    lines += LADDER
    if mode == "confirm":
        lines.append("Пороги бумаги пройдены: бот разрешит confirm сразу, без minlot. Держите TRADING_MODE=minlot, пока не "
                     "увидите 10–20 реальных кругов, и только потом ставьте confirm.")
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
    lines.append(PATH_TEXT)
    return lines


# --- запуск -----------------------------------------------------------------------------------------------------------

def _load_env(path):
    p2p.load_env(path)


def _fail(msg):
    print(f"⛔ {msg}")
    return 2


def _stamp(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
    if a.backtest and not a.dry_run:
        # Любой запуск с --backtest, который не кончится свежей проверенной записью, не должен оставить старый (возможно
        # проходящий) gates_backtest.json: обезвреживаем ДО расчёта — даже если процесс убьют посреди research.report.
        try:
            cleared = _invalidate_backtest(bt_path, _sha_or_empty(bot_dir), now)
        except OSError as e:
            return _fail(f"старый gates_backtest.json не удалось обезвредить: {type(e).__name__}: {e} — бэктест не запускаю")
        print("Старый gates_backtest.json заменён пустым (без порогов) ДО расчёта: по нему minlot больше не откроется. Новый "
              "файл появится только после успешного бэктеста, принятого ботом." if cleared else
              "Старого gates_backtest.json нет — заменять нечего.")
    cfg_assets, mins = hedge_config()
    off = [c for c in cfg_assets if mins.get(c) == math.inf]
    assets = [c for c in cfg_assets if c not in off]
    print("Хедж: пороги «бумага → кнопка» и режим, который они разрешают")
    print(f"Хеджируемые монеты (HEDGE_ASSETS): {', '.join(assets) or 'нет'}; минимальный круг: "
          + (", ".join(f"{c} от {x:g} ₽" for c, x in sorted(mins.items()) if x != math.inf) or "без ограничений"))
    if off:
        print(f"⚠ В {MIN_AMOUNT_ENV} для {', '.join(off)} значение непонятно — бот эту монету не хеджирует (формат "
              "МОНЕТА:сумма,…); в статистику и бэктест она не идёт.")

    try:
        env_buf = simperp.risk_buffers()
    except ValueError as e:   # p2p._fees: RISK_BUFFER не разобрать
        return _fail(f"RISK_BUFFER не разобрать: {e}")
    bad_buf = buffer_nonsense(env_buf, assets)
    if bad_buf:
        for r in bad_buf:
            print(f"⛔ {r}")
        return _fail("RISK_BUFFER непригоден — ничего не записано (порог «стоимость / запас» стал бы пустым): " + BUFFER_ADVICE)
    try:
        per, info = paper_stats(paper_db, assets, mins, now=now, buffers=env_buf)
    except (sqlite3.Error, OSError) as e:
        return _fail(f"paper.db не прочитана: {type(e).__name__}: {e}")
    stats, few = aggregate_paper(per)
    doc_paper = build_paper(stats, now)
    got = doc_paper["strategies"][HEDGE]
    if not info["db"]:
        print(f"Бумага: файла {paper_db} нет — данных нет, в файл идёт count 0.")
    else:
        print(f"Бумага: подходящих хеджей в счёт — {info['kept']}, не в счёт — {info['dropped']} (монета не из HEDGE_ASSETS "
              f"или круг меньше минимума).")
        print("\n".join(paper_lines(per, few)))
        print(f"  В файл: count {got['count']} (сумма по монетам с данными), дней {got['days']:.1f} (минимум).")
        if got["count"] == 0:
            print("  Закрытых хеджей с итогом нет — цифр по стоимости и σ нет, порогов не пройти. Ничего не выдумываем.")
        else:
            skipped = [k for k in PAPER_KEYS if k not in got]
            if skipped:
                print(f"  Не записаны ключи: {', '.join(skipped)} — нет данных, число вне допустимого диапазона или "
                      "блокировка (см. выше); порог по ним не пройден.")
    print("  Бумажный хедж выбирает более дешёвую из Bybit/BingX (обычно BingX), боевой — только Bybit: стоимость на "
          "бумаге чуть оптимистичнее.")

    doc_bt, notes, sha = None, [], ""
    if a.backtest:
        pre_refuse, _ = buffer_check({}, assets, env_buf)   # по запасам research по умолчанию — ещё до запуска процесса
        if pre_refuse:
            for r in pre_refuse:
                print(f"⛔ {r}")
            print("⛔ research.report не запускался, gates_backtest.json не записан: " + BUFFER_ADVICE)
            print("--dry-run: старый gates_backtest.json не тронут." if a.dry_run else
                  "gates_backtest.json остаётся пустым (без порогов): по нему minlot не откроется.")
            return 2
        print("Бэктест: запускаю research.report (публичные данные бирж, может занять много минут)...")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result, sha = run_research(bot_dir, tmp, paper_db, history_db, a.cache, a.offline, a.start, runner)
        except Exception as e:   # любой сбой расчёта: старый файл уже обезврежен выше, свежий не пишем
            return _fail("бэктест не получился: " + (str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}"))
        doc_bt, notes = build_backtest(result, assets, mins, tuple(venues), sha, now)
        print("\n".join(notes))
        refuse, warn = buffer_check(result, assets, env_buf)
        for w in warn:
            print(f"⚠ {w}")
        if refuse:
            for r in refuse:
                print(f"⛔ {r}")
            print("⛔ gates_backtest.json не записан: " + BUFFER_ADVICE)
            print("--dry-run: старый gates_backtest.json не тронут." if a.dry_run else
                  "gates_backtest.json остаётся пустым (без порогов): по нему minlot не откроется.")
            return 2

    if a.dry_run:
        print("\n--dry-run: файлы не записаны.")
        bt_stats = doc_bt["strategies"][HEDGE] if doc_bt else None
        v = verdict(got, bt_stats, gates.short_paper_enabled(), paper_notes(per, few))
        if v["strong"]:
            print(f"⚠⚠ --dry-run, файл не записан: при записи {BACKTEST_UNLOCK.replace('разрешает', 'разрешил бы')}.")
        print("\n".join(verdict_lines(v, source="расчёт по цифрам выше; бот файлов не читал")))
        return 0

    try:
        jsonstore.write_dict(paper_path, doc_paper)
        print(f"\nЗаписано: {paper_path}")
        print(f"generated_at = {_stamp(now)}. Напоминание: перезапускай перед включением confirm; файл сам не стареет "
              "(бот не проверяет его возраст) — и запускай скрипт заново раз в неделю.")
        if doc_bt is not None:
            jsonstore.write_dict(bt_path, doc_bt)
            print(f"Записано: {bt_path}")
            if gates.evaluate(gates.BACKTEST_STRONG[HEDGE], doc_bt["strategies"][HEDGE]).passed:
                print(f"⚠⚠⚠ ВНИМАНИЕ: {BACKTEST_UNLOCK}, без единого дня бумажной истории. Пока не готовы — держите "
                      "TRADING_MODE=paper (TRADING_MODE=confirm или auto с этим файлом тоже даст реальные ордера minlot).")
        else:
            print("gates_backtest.json не тронут: без --backtest пишется только gates_paper.json.")
    except OSError as e:
        return _fail(f"файл не записан: {type(e).__name__}: {e}")

    # что видит сам бот: те же загрузчики, что у journal (файл вне git, sha research/ совпал, время не из будущего)
    p_obj, why_p = gates.load_paper(HEDGE, path=paper_path, root=bot_dir)
    b_obj, why_b = gates.load_backtest(HEDGE, path=bt_path, root=bot_dir)
    print(f"Бот принял gates_paper.json: {'да' if p_obj else 'НЕТ — ' + why_p}")
    print(f"Бот принял gates_backtest.json: {'да' if b_obj else 'нет — ' + why_b}"
          + (" (файл от прошлого запуска, он продолжает действовать)" if b_obj and doc_bt is None else ""))
    if doc_bt is not None and not b_obj:   # свежий файл, который бот не принял, — не «проверенная запись»: обезвреживаем
        print(f"⛔ Бот не принял только что записанный gates_backtest.json ({why_b}) — заменяю его пустым.")
        try:
            _invalidate_backtest(bt_path, sha, now)
        except OSError as e:
            return _fail(f"gates_backtest.json не удалось обезвредить: {type(e).__name__}: {e}")
        return 2
    v = verdict(got, b_obj.stats if b_obj else None, gates.short_paper_enabled(), paper_notes(per, few))
    if doc_bt is None and v["strong"]:
        print(f"⚠⚠⚠ ВНИМАНИЕ: старый gates_backtest.json действует: {BACKTEST_UNLOCK}.")
    bot_mode = gates.max_mode(HEDGE, paper=p_obj.stats if p_obj else None, backtest=b_obj)
    src = "по файлам, как считает бот" if p_obj else "бот не принял файл бумаги — статистики для него нет"
    print("\n".join(verdict_lines(v, bot_mode, src)))
    if p_obj and bot_mode != v["mode"]:
        print(f"⚠ Расчёт по цифрам даёт {v['mode']}, бот — {bot_mode}: верьте боту.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
