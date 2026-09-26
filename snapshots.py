"""Снимки сканов (этап 1 «измерения»): SQLite data/snapshots.db — сжатый снимок каждого скана для разбора,
калибровки и перепрогона (replay.py).

scans — строка на скан: id — мс от эпохи начала скана (известен до записи: круг сухого прогона запоминает его
        на старте, а снимок пишется после сигналов), ts, размер, zlib(JSON): замеры запросов (время, «из кэша» и
        возраст кэша, ошибка), ошибки площадок, ориентиры и спот, настройки скана, связки с меткой/индексом/
        причинами надёжности и серией «живости» бота, ссылки на группы объявлений и справочник сетей.
blobs — группы объявлений (топ-20 по цене на площадку/сторону/монету/сеть, до фильтров) и справочник сетей
        netstatus: zlib(JSON), ключ — хэш содержимого без времени получения (оно у группы в строке скана: все
        объявления группы пришли одним запросом). Неизменившаяся группа заново не пишется — только отметка last
        (когда её последний раз использовал скан).
Хранится RETENTION (14 дней) и не больше SNAPSHOT_MAX_MB (по умолчанию 500, 0 — не писать): сверх лимита
удаляются самые старые сканы. Пишет бот после сигналов в отдельном потоке; ошибка записи скан не ломает.
"""
import dataclasses
import hashlib
import json
import os
import sqlite3
import time
import zlib

import netstatus
import p2p

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "snapshots.db")
VERSION = 1
TOP_N = 20              # объявлений на группу — лучшие по цене для бота
DEALS_MAX = 100         # связок в снимке не больше — в порядке сканера (прибыль × надёжность)
DEALS_MIN = 0.0         # связки от этой прибыли, % (или от порога сигнала, если он ниже): ниже не нужны
RETENTION = 14 * 86400
PRUNE_EVERY = 600       # сек: удаление по сроку — не чаще
DEFAULT_MAX_MB = 500
# поля объявления в группе — список в этом порядке (короче словаря); pays — до фильтра способов оплаты
AD_FIELDS = ("price", "min_amt", "max_amt", "avail", "pays", "nick", "orders", "rate", "ad_id", "online",
             "terms", "net", "url")

_state = {"pruned": 0.0}   # время последнего удаления по сроку


def scan_id(snap):
    """id снимка скана — мс от эпохи начала скана; None — снимок собран не сканом (нет времени)."""
    ts = getattr(snap, "ts", 0.0) if snap is not None else 0.0
    return int(ts * 1000) if ts else None


def max_bytes():
    """Потолок базы в байтах из SNAPSHOT_MAX_MB (по умолчанию 500 МБ; 0 или мусор меньше нуля — не писать)."""
    try:
        mb = float(os.getenv("SNAPSHOT_MAX_MB", DEFAULT_MAX_MB))
    except ValueError:
        mb = DEFAULT_MAX_MB
    return max(0.0, mb) * 1024 * 1024


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, ts REAL, size INTEGER, blob BLOB)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans (ts)")
    con.execute("CREATE TABLE IF NOT EXISTS blobs (h TEXT PRIMARY KEY, last REAL, blob BLOB)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_blobs_last ON blobs (last)")
    con.commit()
    return con


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _hash(raw):
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


def _ad_row(a):
    pays = a.all_pays if a.all_pays is not None else a.pays   # до фильтра: replay фильтрует своими настройками
    return [a.price, a.min_amt, a.max_amt, a.avail, list(pays), a.nick, a.orders, a.rate, a.ad_id, a.online,
            a.terms, a.net, a.url]


def ad_from_row(ex, side, asset, row, fetched_ts=0.0):
    """Объявление (p2p.Ad) из строки группы снимка — для replay."""
    d = dict(zip(AD_FIELDS, row))
    return p2p.Ad(ex, side, d["price"], d["min_amt"], d["max_amt"], d["avail"], list(d["pays"]), d["nick"],
                  d["orders"], d["rate"], d["url"], asset, d["net"], d["terms"], fetched_ts=fetched_ts,
                  ad_id=d["ad_id"], online=d["online"])


def _groups(ads):
    """Топ-TOP_N объявлений по цене (лучшая для бота первой) на (площадка, сторона, монета, сеть) — до фильтров
    мерчанта и отсева аномалий. [(ключ, время получения, сколько было в группе, строки AD_FIELDS)]."""
    by = {}
    for a in ads:
        by.setdefault((a.ex, a.side, a.asset, a.net or ""), []).append(a)
    out = []
    for key, grp in sorted(by.items()):
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
        top = grp[:TOP_N]
        out.append((list(key), min(a.fetched_ts for a in top), len(grp), [_ad_row(a) for a in top]))
    return out


def _side(a):
    """Сторона связки (стек объявлений _combined): total — объём в фиате, qty — в монете."""
    return {"ex": a.ex, "asset": a.asset, "price": a.price, "nick": a.nick, "nicks": list(a.nicks), "net": a.net,
            "parts": a.parts, "orders": a.orders, "rate": a.rate, "total": a.min_amt, "qty": a.avail,
            "ft": a.fetched_ts}


def _deal(d, cfg, snap, live):
    profit, b, s, route = d
    label, reasons = p2p.reliability(d, cfg, snap)
    rec = (live or {}).get((b.ex, b.asset, s.ex, s.asset)) or {}
    return {"profit": profit, "buy": _side(b), "sell": _side(s), "route": route, "label": label,
            "index": p2p.reliability_index(d, cfg, snap), "reasons": reasons,
            "streak": rec.get("streak", 0), "first": rec.get("first")}


def collect(snap, cfg, live=None):
    """Данные снимка из скана — только списки и словари: дальше их можно писать в другом потоке, пока следующий
    скан меняет объявления. live — Bot.live, серия «живости» связок {(ex, asset, ex, asset): {"first", "streak"}}."""
    ts = snap.ts or time.time()
    floor = min(DEALS_MIN, cfg.min_profit)   # порог сигнала ниже нуля — храним и связки от него
    deals = [_deal(d, cfg, snap, live) for d in snap.deals if d[0] >= floor][:DEALS_MAX]
    end = max((j.get("t1", ts) for j in snap.jobs), default=ts)
    scan = {"v": VERSION, "ts": ts, "dur": round(end - ts, 3), "cfg": dataclasses.asdict(cfg),
            "ref": snap.ref, "ref_src": snap.ref_src, "refs": dict(snap.refs),
            "spot": {v: {a: list(p) for a, p in q.items()} for v, q in snap.spot.items()},
            "errors": dict(snap.errors), "dropped": dict(snap.dropped), "jobs": [dict(j) for j in snap.jobs],
            "over_banks": sorted(snap.over_banks), "blocked": sorted(list(x) for x in snap.blocked),
            "deals": deals}
    net = [[v, a, nets] for (v, a), nets in sorted(netstatus.STATUS.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))]
    return {"id": int(ts * 1000), "ts": ts, "scan": scan, "groups": _groups(snap.ads), "net": net}


def _used(con):
    """Сколько байт база реально занимает (без свободных страниц — их SQLite переиспользует)."""
    pages, = con.execute("PRAGMA page_count").fetchone()
    free, = con.execute("PRAGMA freelist_count").fetchone()
    size, = con.execute("PRAGMA page_size").fetchone()
    return (pages - free) * size


def _drop_orphans(con, now):
    """Группы, которые не использует ни один оставшийся скан: last меньше времени самого старого скана."""
    oldest, = con.execute("SELECT MIN(ts) FROM scans").fetchone()
    con.execute("DELETE FROM blobs WHERE last < ?", (oldest if oldest is not None else now + 1,))


def _prune(con, now, cap):
    """Удалить сканы старше RETENTION (не чаще PRUNE_EVERY) и, пока база больше cap, — самые старые (по 10%)."""
    if now - _state["pruned"] >= PRUNE_EVERY:
        _state["pruned"] = now
        with con:
            con.execute("DELETE FROM scans WHERE ts < ?", (now - RETENTION,))
            _drop_orphans(con, now)
    while _used(con) > cap:
        n, = con.execute("SELECT COUNT(*) FROM scans").fetchone()
        if not n:
            break
        cut, = con.execute("SELECT ts FROM scans ORDER BY ts LIMIT 1 OFFSET ?", (max(1, n // 10) - 1,)).fetchone()
        with con:
            con.execute("DELETE FROM scans WHERE ts <= ?", (cut,))
            _drop_orphans(con, now)


def write(data, path=DB_PATH, now=None):
    """Записать снимок (collect) в базу: группы — только новые по хэшу (у старых — отметка last), скан — одной
    строкой; затем удаление старых. Возвращает id скана, None — снимки выключены (SNAPSHOT_MAX_MB=0)."""
    cap = max_bytes()
    if not cap:
        return None
    now = time.time() if now is None else now
    ts, blobs, refs = data["ts"], {}, []
    for key, ft, total, rows in data["groups"]:
        raw = _dumps(rows)
        h = _hash(raw)
        blobs[h] = raw
        refs.append({"k": key, "h": h, "ft": ft, "n": total})
    net_raw = _dumps(data["net"])
    net_h = _hash(net_raw)
    blobs[net_h] = net_raw
    blob = zlib.compress(_dumps(dict(data["scan"], groups=refs, net=net_h)))
    con = _connect(path)
    try:
        hashes = list(blobs)
        have = set()
        for i in range(0, len(hashes), 500):
            part = hashes[i:i + 500]
            have |= {h for h, in con.execute(f"SELECT h FROM blobs WHERE h IN ({','.join('?' * len(part))})", part)}
        with con:
            con.executemany("INSERT OR IGNORE INTO blobs (h, last, blob) VALUES (?, ?, ?)",
                            [(h, ts, zlib.compress(raw)) for h, raw in blobs.items() if h not in have])
            con.executemany("UPDATE blobs SET last = ? WHERE h = ? AND last < ?", [(ts, h, ts) for h in have])
            con.execute("INSERT OR REPLACE INTO scans (id, ts, size, blob) VALUES (?, ?, ?, ?)",
                        (data["id"], ts, len(blob), blob))
        _prune(con, now, cap)
    finally:
        con.close()
    return data["id"]


def save(snap, cfg, live=None, path=DB_PATH):
    """collect + write одним вызовом (для тестов и разовых скриптов; бот пишет write в отдельном потоке)."""
    return write(collect(snap, cfg, live), path)


def _blob(con, h):
    row = con.execute("SELECT blob FROM blobs WHERE h = ?", (h,)).fetchone()
    return json.loads(zlib.decompress(row[0])) if row else None


def load(sid, path=DB_PATH):
    """Снимок по id: словарь скана, у групп (groups) — строки объявлений "ads" (AD_FIELDS), справочник сетей —
    "net" списком [площадка, монета, сети]. None — снимка нет."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    try:
        row = con.execute("SELECT blob FROM scans WHERE id = ?", (sid,)).fetchone()
        if not row:
            return None
        scan = json.loads(zlib.decompress(row[0]))
        for g in scan["groups"]:
            g["ads"] = _blob(con, g["h"]) or []
        scan["net"] = _blob(con, scan["net"]) or []
    finally:
        con.close()
    scan["id"] = sid
    return scan


def ids(path=DB_PATH, since=0.0, until=None):
    """id снимков по времени скана (since ≤ ts < until)."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    rows = con.execute("SELECT id FROM scans WHERE ts >= ? AND ts < ? ORDER BY ts",
                       (since, until if until is not None else float("inf"))).fetchall()
    con.close()
    return [r[0] for r in rows]


def ads_of(scan):
    """Все объявления снимка (p2p.Ad) из его групп — время получения у каждого своё, от группы."""
    return [ad_from_row(ex, side, asset, row, g["ft"]) for g in scan["groups"]
            for ex, side, asset, _net in [g["k"]] for row in g["ads"]]


def stats(path=DB_PATH):
    """{"scans": сколько, "bytes": занято, "first"/"last": время первого/последнего скана} — для отчёта."""
    if not os.path.exists(path):
        return {"scans": 0, "bytes": 0, "first": None, "last": None}
    con = _connect(path)
    n, first, last = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM scans").fetchone()
    used = _used(con)
    con.close()
    return {"scans": n, "bytes": used, "first": first, "last": last}
