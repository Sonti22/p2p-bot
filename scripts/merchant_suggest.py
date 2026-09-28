"""Предложение MERCHANT_MIN по снимкам сканов (data/snapshots.db): пороги «сделок/%» по площадкам из того, какие мерчанты
реально стоят в стаканах. Только читает базу (открывает её только на чтение), .env не меняет — строку владелец
переносит сам.

    python scripts/merchant_suggest.py data/snapshots.db [--days 14] [--min-merchants 5]

Метод — тот же, что у MERCHANT_MIN_SUGGESTED в p2p.py (там он посчитан по tests/fixtures): уникальные мерчанты площадки
по всем монетам и обеим сторонам (у мерчанта — последние увиденные сделки и %); сделок — 25-й перцентиль, вниз до одной
значащей цифры; % — 10-й перцентиль без выбросов ниже 90%, вниз до целого. Предложение не мягче общих 100/95
(CLAUDE.md: фильтры мерчантов не ослаблять) — мягче может задать только сам владелец. BestChange пропускается: у
обменников вместо сделок — отзывы. Площадка, где мерчантов меньше --min-merchants, — «мало данных», в строку не идёт
(у неё останется общий порог)."""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshots  # noqa: E402  (AD_FIELDS — формат строк объявления в снимке)

FLOOR_ORDERS, FLOOR_RATE = 100, 95.0   # не мягче общих порогов
RATE_OUTLIER = 90.0                    # % ниже — выброс (новичок, спор), в перцентиль не берём
SKIP_VENUES = {"BestChange"}           # у обменников отзывы, а не сделки
I_NICK, I_ORDERS, I_RATE = (snapshots.AD_FIELDS.index(f) for f in ("nick", "orders", "rate"))


def percentile(values, q):
    """Перцентиль q (0–100) с линейной интерполяцией между соседними значениями (как numpy по умолчанию)."""
    xs = sorted(values)
    if not xs:
        return None
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def floor_sig1(x):
    """Вниз до одной значащей цифры: 77 → 70, 308 → 300, 1732 → 1000; 0 и меньше — 0."""
    if x is None or x <= 0:
        return 0
    step = 10 ** math.floor(math.log10(x))
    return int(x // step * step)


def merchants(path, since=0.0):
    """{(площадка, ник): (сделок, %)} — последние увиденные у мерчанта значения по всем группам снимков с ts ≥ since."""
    con = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    try:
        refs = {}   # пакет -> {номер группы в пакете: (площадка, время последнего скана с ней)}
        for ts, blob in con.execute("SELECT ts, blob FROM scans WHERE ts >= ? ORDER BY ts", (since,)):
            for g in json.loads(zlib.decompress(blob)).get("groups", []):
                if isinstance(g, dict) and "p" in g and "i" in g and g.get("k"):   # снимки версии 1 — без групп
                    refs.setdefault(g["p"], {})[g["i"]] = (g["k"][0], ts)
        out, seen = {}, {}
        for pack in sorted(refs):   # каждый пакет разжимаем один раз
            row = con.execute("SELECT blob FROM packs WHERE id = ?", (pack,)).fetchone()
            items = json.loads(zlib.decompress(row[0])) if row else []
            for i, (venue, ts) in refs[pack].items():
                if not 0 <= i < len(items):
                    continue
                for ad in items[i]:
                    key = (venue, ad[I_NICK])
                    if ad[I_ORDERS] is None or ad[I_RATE] is None or seen.get(key, -1.0) > ts:
                        continue
                    out[key], seen[key] = (float(ad[I_ORDERS]), float(ad[I_RATE])), ts
        return out
    finally:
        con.close()


def suggest(found, min_merchants=5):
    """По мерчантам (merchants) — ({площадка: (сделок, %)} для строки MERCHANT_MIN, [строки пояснения])."""
    by = {}
    for (venue, _nick), (orders, rate) in found.items():
        if venue not in SKIP_VENUES:
            by.setdefault(venue, []).append((orders, rate))
    result, notes = {}, []
    for venue in sorted(by):
        rows = by[venue]
        if len(rows) < min_merchants:
            notes.append(f"{venue}: мало данных ({len(rows)} мерчантов < {min_merchants}) — общий порог")
            continue
        orders = [o for o, _ in rows]
        rates = [r for _, r in rows if r >= RATE_OUTLIER]
        p25, p50 = percentile(orders, 25), percentile(orders, 50)
        p10 = percentile(rates, 10)
        o = max(floor_sig1(p25), FLOOR_ORDERS)
        r = max(math.floor(p10) if p10 is not None else 0, FLOOR_RATE)
        result[venue] = (o, r)
        dropped = len(rows) - len(rates)
        notes.append(f"{venue}: n={len(rows)}, сделок p25 {p25:.0f}, p50 {p50:.0f}; % p10 "
                     + (f"{p10:.1f}" if p10 is not None else "—") + (f" (без {dropped} ниже {RATE_OUTLIER:g}%)"
                                                                     if dropped else "")
                     + f" → {o}/{r:g}")
    return result, notes


def line(result):
    return ",".join(f"{v}:{o}/{r:g}" for v, (o, r) in result.items())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Предложение MERCHANT_MIN по снимкам сканов")
    ap.add_argument("db", help="путь к snapshots.db")
    ap.add_argument("--days", type=float, default=0, help="только последние N дней (0 — все снимки)")
    ap.add_argument("--min-merchants", type=int, default=5, help="минимум мерчантов на площадку")
    args = ap.parse_args(argv)
    if not os.path.isfile(args.db):
        print(f"нет файла {args.db}", file=sys.stderr)
        return 2
    since = time.time() - args.days * 86400 if args.days > 0 else 0.0
    result, notes = suggest(merchants(args.db, since), args.min_merchants)
    print("\n".join(notes))
    if not result:
        print("Предложения нет: ни на одной площадке не хватило данных.")
        return 1
    print(f"MERCHANT_MIN={line(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
