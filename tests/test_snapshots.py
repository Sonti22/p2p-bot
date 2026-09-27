"""Снимки сканов (snapshots.py) и замеры запросов скана: время получения объявлений, «из кэша», id объявлений,
запись/чтение снимка, пакеты групп и дедупликация, срок хранения и потолок размера (14 дней при настройках
.env.example), испорченная база, запись из scan_loop после сигналов — каждый SNAPSHOT_EVERY-й скан
и скан старта круга."""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import zlib

import pytest

import bot as B
import netstatus
import p2p
import snapshots
from helpers import make_ad


def _cfg(**kw):
    base = dict(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0,
                min_profit=-100.0)
    base.update(kw)
    return p2p.Config(**base)


@pytest.fixture(autouse=True)
def _fresh_prune(monkeypatch):
    monkeypatch.setitem(snapshots._state, "pruned", 0.0)
    monkeypatch.setattr(snapshots, "_seen", {})


def _count(path, table):
    con = sqlite3.connect(path)
    n, = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    con.close()
    return n


# --- объявления: id, онлайн, время получения ---

def test_lbank_ad_id_and_online_from_fixture(offline):
    ads = asyncio.run(p2p.lbank(None, p2p.Config(), "sell", "USDT"))
    assert [(a.ad_id, a.online) for a in ads] == [
        ("a0edc7f8-0872-49bd-a879-799f33d0749a", False), ("923f8d60-f54a-4063-90d0-e4543031311b", False),
        ("86a68d36-615d-4e34-b678-e878a1b80c98", False), ("1bc231f5-d32d-4944-8830-e22a81b2490b", True)]
    buy = asyncio.run(p2p.lbank(None, p2p.Config(), "buy", "USDT"))
    assert [a.ad_id for a in buy] == ["FP_4fad88b0-724e-41f7-8418-a252f8843ae9"]   # uuid как есть, с префиксом
    bybit = asyncio.run(p2p.bybit(None, p2p.Config(), "buy", "USDT"))
    assert bybit and all(a.ad_id == "" and a.online is None for a in bybit)   # в выдаче Bybit id нет


def test_meta_fields_do_not_change_ad_equality():
    a, b = make_ad(), make_ad()
    b.fetched_ts, b.ad_id, b.online = 123.0, "x", True
    assert a == b


def test_combined_keeps_oldest_fetched_ts_and_single_ad_id():
    a1 = make_ad(price=85.0, max_amt=30000, avail=400)
    a2 = make_ad(price=86.0, max_amt=30000, avail=400)
    a1.fetched_ts, a2.fetched_ts, a1.ad_id = 200.0, 100.0, "id1"
    st = p2p._stack([a1, a2], 50000)
    assert st.parts == 2 and st.fetched_ts == 100.0 and st.ad_id == ""
    one = p2p._stack([a1], 20000)
    assert one.ad_id == "id1" and one.fetched_ts == 200.0


def test_scan_sets_fetched_ts_and_job_timings(offline):
    t_before = time.time()
    snap = asyncio.run(p2p.scan(None, _cfg(exchanges=["bybit", "lbank"])))
    t_after = time.time()
    assert t_before <= snap.ts <= t_after
    assert snap.ads and all(t_before <= a.fetched_ts <= t_after for a in snap.ads)
    jobs = {(j["ex"], j.get("side"), j.get("asset")): j for j in snap.jobs}
    for key in (("bybit", "buy", "USDT"), ("bybit", "sell", "USDT"), ("lbank", "buy", "USDT"), ("lbank", "sell", "USDT")):
        j = jobs[key]
        assert j["t0"] <= j["t1"] and j["cached"] is False and j["age"] == 0 and not j.get("err")
        assert j["n"] == sum(1 for a in snap.ads if (a.ex.lower(), a.side, a.asset) == key)
    assert {"rapira", "spot", "networks"} <= {j["ex"] for j in snap.jobs}


def test_scan_job_error_recorded(offline, monkeypatch):
    async def boom(s, cfg, side, asset):
        raise RuntimeError("down")
    monkeypatch.setitem(p2p.FETCHERS, "fake", boom)
    snap = asyncio.run(p2p.scan(None, _cfg(exchanges=["fake"])))
    j = next(j for j in snap.jobs if j["ex"] == "fake")
    assert "RuntimeError: down" in j["err"] and j["n"] == 0 and j["cached"] is False and j["t1"] >= j["t0"]


def _fake(venue, calls):
    async def fetch(s, cfg, side, asset):
        calls.append((venue, side, asset))
        price = {"USDT": (85.0, 90.0), "ETH": (200_000.0, 230_000.0)}[asset]
        return [make_ad(venue, side, price[0 if side == "buy" else 1], asset=asset)]
    return fetch


def test_alt_cache_jobs_marked_cached_with_age(offline, monkeypatch):
    calls = []
    monkeypatch.setitem(p2p.FETCHERS, "fake", _fake("Fake", calls))
    cfg = _cfg(exchanges=["fake"], assets=["USDT", "ETH"], alt_interval=60)
    first = asyncio.run(p2p.scan(None, cfg))
    eth_ts = {a.fetched_ts for a in first.ads if a.asset == "ETH"}
    j1 = [j for j in first.jobs if j.get("asset") == "ETH"]
    assert len(j1) == 2 and not any(j["cached"] for j in j1)
    calls.clear()
    second = asyncio.run(p2p.scan(None, cfg))
    assert not [c for c in calls if c[2] == "ETH"]                     # ETH из кэша _alt
    j2 = [j for j in second.jobs if j.get("asset") == "ETH"]
    assert len(j2) == 2 and all(j["cached"] and j["age"] >= 0 for j in j2)
    assert {j["t1"] for j in j2} == {j["t1"] for j in j1}             # замер того запроса, которым собран кэш
    assert {a.fetched_ts for a in second.ads if a.asset == "ETH"} == eth_ts
    usdt = [j for j in second.jobs if j.get("asset") == "USDT"]
    assert usdt and not any(j["cached"] for j in usdt)


def test_bestchange_from_cache_flag_and_age(offline, monkeypatch):
    old = time.time() - 50
    ad = p2p.Ad("BestChange", "buy", 85.0, 1000, 500000, 10000, ["Sberbank"], "X [TRC20]", 500, 99.0,
                asset="USDT", net="TRC20", fetched_ts=old)
    monkeypatch.setitem(p2p._bc, "ads", [ad])
    monkeypatch.setitem(p2p._bc, "t", old)
    snap = asyncio.run(p2p.scan(None, _cfg(exchanges=["bestchange"], bc_refresh=120)))
    j = next(j for j in snap.jobs if j["ex"] == "bestchange" and j["side"] == "buy")
    assert j["cached"] is True and 49 <= j["age"] <= 60 and j["n"] == 1
    empty = next(j for j in snap.jobs if j["ex"] == "bestchange" and j["side"] == "sell")
    assert empty["cached"] is True and empty["n"] == 0                 # объявлений нет — время выгрузки из _bc


# --- запись и чтение снимка ---

def _scan_snap(offline_cfg=None):
    return asyncio.run(p2p.scan(None, offline_cfg or _cfg()))


def test_save_and_load_roundtrip(offline):
    cfg = _cfg()
    snap = _scan_snap(cfg)
    assert snap.deals
    top = snap.deals[0]
    key = (top[1].ex, top[1].asset, top[2].ex, top[2].asset)
    sid = snapshots.save(snap, cfg, live={key: {"first": 111.0, "streak": 3}})
    assert sid == snapshots.scan_id(snap) == int(snap.ts * 1000)
    got = snapshots.load(sid)
    assert got["ts"] == snap.ts and got["ref"] == snap.ref and got["refs"] == snap.refs
    assert got["cfg"]["amount"] == cfg.amount and got["errors"] == snap.errors
    assert {j["ex"] for j in got["jobs"]} >= {"bybit", "htx", "spot"}
    d = got["deals"][0]
    label, reasons = p2p.reliability(top, cfg, snap)
    assert d["profit"] == top[0] and d["label"] == label and d["reasons"] == reasons
    assert d["index"] == p2p.reliability_index(top, cfg, snap) and d["streak"] == 3 and d["first"] == 111.0
    assert d["buy"]["ex"] == top[1].ex and d["sell"]["price"] == top[2].price and d["route"] == top[3]
    keys = {tuple(g["k"]) for g in got["groups"]}
    assert ("Bybit", "buy", "USDT", "") in keys and ("MEXC", "sell", "USDT", "") in keys
    for g in got["groups"]:
        assert g["ft"] > 0 and 0 < len(g["ads"]) <= snapshots.TOP_N and g["n"] >= len(g["ads"])
        prices = [r[0] for r in g["ads"]]
        assert prices == sorted(prices, reverse=g["k"][1] == "sell")
    ads = snapshots.ads_of(got)
    assert len(ads) == len(snap.ads) and all(a.fetched_ts > 0 for a in ads)
    orig = sorted((a.ex, a.side, a.price, a.nick) for a in snap.ads)
    assert sorted((a.ex, a.side, a.price, a.nick) for a in ads) == orig
    assert snapshots.ids() == [sid]


def test_perps_stored_compact_and_restored(_isolated_data):
    """Котировки перпов скана (Snapshot.perps) — в снимке: все поля, стакан — PERP_LEVELS уровней; снимок без них
    (replay, старые снимки, PERPS=0) — пустой список."""
    import dataclasses
    from perpfx import book, quote
    bids, asks = book(5.0, 0.001, 100.0, levels=50)                     # как у живого опроса — perp.DEPTH уровней
    ton = dataclasses.replace(quote(symbol="GRAMUSDT", mid=5.0, asset="TON", skew=0.4), bids=bids, asks=asks)
    btc = quote(venue="BingX", symbol="BTC-USDT", asset="BTC", fee=0.05, interval_h=4.0)
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=1000.0,
                        perps={("Bybit", "GRAMUSDT"): ton, ("BingX", "BTC-USDT"): btc})
    got = snapshots.load(snapshots.write(snapshots.collect(snap, p2p.Config()), now=1000.0))
    assert [r[:2] for r in got["perps"]] == [["BingX", "BTC-USDT"], ["Bybit", "GRAMUSDT"]]
    assert all(len(r) == len(snapshots.PERP_FIELDS) for r in got["perps"])
    back = snapshots.perps_of(got)
    lv = snapshots.PERP_LEVELS
    assert back == {("Bybit", "GRAMUSDT"): dataclasses.replace(ton, bids=bids[:lv], asks=asks[:lv]),
                    ("BingX", "BTC-USDT"): btc}                           # у btc 5 уровней — все
    assert back[("Bybit", "GRAMUSDT")].asset == "TON" and back[("Bybit", "GRAMUSDT")].mid == ton.mid
    empty = snapshots.collect(p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=1001.0), p2p.Config())
    assert empty["scan"]["perps"] == [] and snapshots.perps_of({"groups": []}) == {}


def test_deals_below_floor_and_other_streaks_not_stored():
    b, s = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    deals = [(3.0, b, s, "r"), (-1.0, b, make_ad("HTX", "sell", 84.0), "r")]
    snap = p2p.Snapshot(88.0, "t", {}, {}, deals, {}, {}, {}, ts=1000.0)
    data = snapshots.collect(snap, p2p.Config(min_profit=1.0), live={})
    assert [d["profit"] for d in data["scan"]["deals"]] == [3.0]         # ниже 0% не храним
    assert data["scan"]["deals"][0]["streak"] == 0 and data["scan"]["deals"][0]["first"] is None
    low = snapshots.collect(snap, p2p.Config(min_profit=-5.0), live={})  # порог ниже нуля — храним от порога
    assert len(low["scan"]["deals"]) == 2


def test_top_n_by_price_and_pays_before_filter():
    ads = [make_ad("Bybit", "buy", 80.0 + i, pays=("T-Bank", "Cash")) for i in range(25)]
    ads += [make_ad("Bybit", "sell", 90.0 - i) for i in range(3)]
    for a in ads:
        a.fetched_ts = 500.0
    cfg = p2p.Config()
    for a in ads:
        p2p._pays(a, cfg)   # фильтр оплаты сканера уже отработал: a.pays без наличных
    assert ads[0].pays == ["T-Bank"]
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=1000.0, ads=ads)
    groups = {tuple(k): (ft, n, rows) for k, ft, n, rows in snapshots.collect(snap, cfg)["groups"]}
    ft, n, rows = groups[("Bybit", "buy", "USDT", "")]
    assert n == 25 and len(rows) == 20 and ft == 500.0
    assert [r[0] for r in rows] == [80.0 + i for i in range(20)]        # покупка — дешёвые первыми
    assert rows[0][snapshots.AD_FIELDS.index("pays")] == ["T-Bank", "Cash"]
    _, _, sell = groups[("Bybit", "sell", "USDT", "")]
    assert [r[0] for r in sell] == [90.0, 89.0, 88.0]                   # продажа — дорогие первыми


def _data(ts, prices, net_status=None):
    ads = [make_ad("Bybit", "buy", p) for p in prices]
    for a in ads:
        a.fetched_ts = ts
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=ts, ads=ads)
    return snapshots.collect(snap, p2p.Config())


def _pack_items(path):
    con = sqlite3.connect(path)
    rows = con.execute("SELECT id, blob FROM packs ORDER BY id").fetchall()
    con.close()
    return {pid: json.loads(zlib.decompress(blob)) for pid, blob in rows}


def test_unchanged_groups_not_rewritten(_isolated_data):
    path = snapshots.DB_PATH
    snapshots.write(_data(1000.0, [85.0, 86.0]), now=1000.0)
    assert [len(v) for v in _pack_items(path).values()] == [2]          # группа + справочник сетей одним пакетом
    snapshots.write(_data(1020.0, [85.0, 86.0]), now=1020.0)             # то же содержимое, другое время
    assert _count(path, "packs") == 1 and _count(path, "scans") == 2
    first, second = snapshots.load(1000000), snapshots.load(1020000)
    assert (first["groups"][0]["p"], first["groups"][0]["i"]) == (second["groups"][0]["p"], second["groups"][0]["i"])
    assert second["groups"][0]["ads"] == first["groups"][0]["ads"] and second["net"] == first["net"]
    assert second["groups"][0]["ft"] == 1020.0                           # время получения — своё у скана
    snapshots.write(_data(1040.0, [85.0, 87.0]), now=1040.0)             # цена сменилась — новая группа
    packs = _pack_items(path)
    assert len(packs) == 2 and len(packs[max(packs)]) == 1               # в новом пакете только она, сети — старые
    third = snapshots.load(1040000)
    assert [r[0] for r in third["groups"][0]["ads"]] == [85.0, 87.0] and third["net"] == first["net"]


def test_retention_drops_old_scans_and_orphan_groups(_isolated_data):
    path = snapshots.DB_PATH
    now = 100 * 86400.0
    old = now - snapshots.RETENTION - 3600
    snapshots.write(_data(old, [70.0]), now=old)                         # группа 70.0 и сети — только у старого скана
    netstatus.STATUS[("HTX", "USDT")] = {"TRC20": {"dep": True, "wd": True, "fee": 1.0, "min": 10.0}}
    snapshots.write(_data(old + 10, [85.0]), now=old + 10)
    snapshots._state["pruned"] = 0.0
    snapshots.write(_data(now, [85.0]), now=now)                         # та же группа 85.0 и сети — живы
    assert snapshots.ids() == [int(now * 1000)]
    got = snapshots.load(int(now * 1000))
    assert got["groups"][0]["ads"] and got["groups"][0]["ads"][0][0] == 85.0 and got["net"][0][:2] == ["HTX", "USDT"]
    assert _count(path, "packs") == 1                                    # пакет старого скана удалён, 85.0 остался


def test_group_reused_after_restart_is_stored_again(_isolated_data, monkeypatch):
    """После рестарта бота (память о записанном пуста) или пропажи старого пакета группа пишется заново."""
    path = snapshots.DB_PATH
    snapshots.write(_data(1000.0, [85.0]), now=1000.0)
    monkeypatch.setattr(snapshots, "_seen", {})
    snapshots.write(_data(1020.0, [85.0]), now=1020.0)
    assert _count(path, "packs") == 2 and snapshots.load(1020000)["groups"][0]["ads"][0][0] == 85.0
    con = sqlite3.connect(path)
    con.execute("DELETE FROM packs")                                    # пакета, на который ссылается память, нет
    con.commit()
    con.close()
    snapshots.write(_data(1040.0, [85.0]), now=1040.0)
    assert snapshots.load(1040000)["groups"][0]["ads"][0][0] == 85.0


def test_size_cap_drops_oldest_scans(_isolated_data, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_MAX_MB", "0.04")
    cap = snapshots.max_bytes()
    t = 1_000_000.0
    for i in range(60):   # у каждого скана свои цены — группы не совпадают, база растёт
        snapshots.write(_data(t + i, [50.0 + i + k / 100 for k in range(20)]), now=t + i)
    st = snapshots.stats()
    assert st["bytes"] <= cap and 0 < st["scans"] < 60
    assert st["last"] == t + 59                                          # новые на месте, удалены старые
    assert snapshots.load(int((t + 59) * 1000))["groups"][0]["ads"]


def test_snapshot_max_mb_zero_disables(_isolated_data, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_MAX_MB", "0")
    assert snapshots.write(_data(1000.0, [85.0])) is None
    assert not os.path.exists(snapshots.DB_PATH)


def test_netstatus_stored_and_deduped(_isolated_data):
    netstatus.STATUS[("HTX", "USDT")] = {"TRC20": {"dep": True, "wd": False, "fee": 1.0, "min": 10.0}}
    sid = snapshots.write(_data(1000.0, [85.0]), now=1000.0)
    got = snapshots.load(sid)
    assert got["net"] == [["HTX", "USDT", {"TRC20": {"dep": True, "wd": False, "fee": 1.0, "min": 10.0}}]]


def test_db_path_is_isolated(_isolated_data):
    assert snapshots.DB_PATH == str(_isolated_data / "snapshots.db")
    assert snapshots.write.__defaults__[0] == str(_isolated_data / "snapshots.db")


def test_snapshot_every_setting(monkeypatch):
    monkeypatch.delenv("SNAPSHOT_EVERY", raising=False)
    assert snapshots.every() == snapshots.DEFAULT_EVERY == 3
    for raw, want in (("1", 1), ("6", 6), ("0", 3), ("-2", 3), ("abc", 3)):
        monkeypatch.setenv("SNAPSHOT_EVERY", raw)
        assert snapshots.every() == want


# --- испорченная база ---

def _junk_db(path, junk):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(junk)


def test_corrupt_db_set_aside_and_new_one_written(_isolated_data, caplog):
    """«file is not a database» — раньше каждый скан писал эту ошибку в лог и снимков не было до ручной чистки."""
    path = snapshots.DB_PATH
    junk = b"this is not a database " * 200
    _junk_db(path, junk)
    with caplog.at_level(logging.WARNING, logger="snapshots"):
        assert snapshots.write(_data(1000.0, [85.0]), now=1000.0) == 1000000
    with open(path + ".corrupt-1000", "rb") as f:
        assert f.read() == junk                                          # испорченный файл отложен как есть
    assert "snapshots.db.corrupt-1000" in caplog.text
    assert snapshots.write(_data(1020.0, [85.0]), now=1020.0) == 1020000 # следующие сканы — в новую базу
    assert snapshots.ids() == [1000000, 1020000] and snapshots.load(1020000)["groups"][0]["ads"]


def test_malformed_db_pages_set_aside(_isolated_data):
    path = snapshots.DB_PATH
    for i in range(30):
        snapshots.write(_data(1000.0 + i, [50.0 + i + k / 100 for k in range(20)]), now=1000.0 + i)
    with open(path, "rb") as f:
        raw = f.read()
    _junk_db(path, raw[:4096] + b"\x5a" * (len(raw) - 4096))            # заголовок цел, страницы данных — мусор
    snapshots._seen.clear()
    assert snapshots.write(_data(2000.0, [1.0]), now=2000.0) == 2000000
    assert os.path.exists(path + ".corrupt-2000") and snapshots.ids() == [2000000]


def test_busy_or_io_errors_do_not_set_db_aside(_isolated_data, monkeypatch):
    path = snapshots.DB_PATH
    snapshots.write(_data(1000.0, [85.0]), now=1000.0)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(snapshots, "_write", locked)
    with pytest.raises(sqlite3.OperationalError):
        snapshots.write(_data(1020.0, [85.0]), now=1020.0)
    assert os.listdir(os.path.dirname(path)) == ["snapshots.db"]


# --- объём: 14 дней при настройках по умолчанию ---

def _changed_series(data, n, t0, step):
    """n снимков подряд, где каждая группа каждый раз новая (цены сдвинуты на копейку) — худший случай для базы:
    в жизни группы монет кроме USDT и BestChange обновляются раз в ALT_INTERVAL/BC_REFRESH и заново не пишутся."""
    out = []
    for i in range(n):
        ts = t0 + i * step
        groups = [(key, ts, total, [[round(r[0] + 0.01 * (i + 1), 2)] + r[1:] for r in rows])
                  for key, _ft, total, rows in data["groups"]]
        out.append({"id": int(ts * 1000), "ts": ts, "scan": dict(data["scan"], ts=ts), "groups": groups,
                    "net": data["net"]})
    return out


def _example():
    """Настройки из .env.example — то, с чем бот работает «по умолчанию» у владельца."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            name, sep, value = line.strip().partition("=")
            if sep and not name.startswith("#"):
                out[name] = value
    return out


def test_default_cap_holds_14_days_at_default_interval(offline, monkeypatch):
    example = _example()
    assert int(example["SNAPSHOT_EVERY"]) == snapshots.DEFAULT_EVERY
    assert float(example["SNAPSHOT_MAX_MB"]) == snapshots.DEFAULT_MAX_MB
    interval = int(example["INTERVAL"])                                  # 10 с (у p2p.Config — 20, запас вдвое)
    assert interval <= p2p.Config().interval
    monkeypatch.delenv("SNAPSHOT_MAX_MB", raising=False)
    monkeypatch.delenv("SNAPSHOT_EVERY", raising=False)
    cfg = p2p.Config()                                                   # все площадки и монеты фикстур
    data = snapshots.collect(asyncio.run(p2p.scan(None, cfg)), cfg)
    assert len(data["groups"]) > 50
    step = interval * snapshots.every()
    n = 12
    for d in _changed_series(data, n, 1_000_000.0, step):
        snapshots.write(d, now=d["ts"])
    per_scan = snapshots.stats()["bytes"] / n                            # ~26 КБ на фикстуре
    days = snapshots.max_bytes() / (per_scan * 86400 / step)
    assert days >= 14, f"{per_scan:.0f} байт на снимок — {days:.1f} дней в лимите"
    assert snapshots.RETENTION == 14 * 86400


# --- запись из цикла бота ---

class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True}


def _loop_once(bot, monkeypatch, fake_scan):
    monkeypatch.setattr(B, "scan", fake_scan)
    monkeypatch.setattr(B.history, "record", lambda snap, amount=None: False)

    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(B.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.scan_loop())


def test_scan_loop_saves_snapshot_after_notify_in_thread(monkeypatch):
    order, threads = [], []
    ts = time.time()
    ads = [make_ad("Bybit", "buy", 85.0)]
    ads[0].fetched_ts = ts

    async def fake_scan(s, cfg):
        return p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=ts, ads=ads)

    bot = Stub(p2p.Config())
    for name in ("check_venues", "check_alerts", "check_networks", "process_paper_cycles", "check_paper_ladder",
                 "update_market_status"):
        async def noop(*a, **k):
            return None
        monkeypatch.setattr(bot, name, noop)

    async def tick(snap):
        order.append("notify")
    monkeypatch.setattr(bot, "quiet_and_pause_tick", tick)
    real_write = snapshots.write

    def write(data, *a, **k):
        order.append("write")
        threads.append(threading.get_ident())
        return real_write(data, *a, **k)
    monkeypatch.setattr(snapshots, "write", write)
    _loop_once(bot, monkeypatch, fake_scan)
    assert order == ["notify", "write"]
    assert threads[0] != threading.get_ident()                           # запись не в цикле событий
    assert snapshots.ids() == [int(ts * 1000)]


def test_snapshot_write_error_is_logged_not_raised(monkeypatch, caplog):
    async def fake_scan(s, cfg):
        return p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=1234.5)

    def broken(data, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(snapshots, "write", broken)
    bot = Stub(p2p.Config())
    bot.chat_id = ""
    with caplog.at_level(logging.WARNING, logger="bot"):
        _loop_once(bot, monkeypatch, fake_scan)
    assert bot.last is not None and "snapshot: disk I/O error" in caplog.text
    assert "scan error" not in caplog.text


def test_failed_scan_writes_no_snapshot(monkeypatch):
    async def fake_scan(s, cfg):
        raise RuntimeError("net down")
    calls = []
    monkeypatch.setattr(snapshots, "write", lambda *a, **k: calls.append(a))
    bot = Stub(p2p.Config())
    bot.chat_id = ""
    _loop_once(bot, monkeypatch, fake_scan)
    assert not calls


def test_save_snapshot_every_nth_scan_and_paper_start_scan(monkeypatch):
    monkeypatch.setenv("SNAPSHOT_EVERY", "3")
    bot = Stub(p2p.Config())
    t0 = float(int(time.time()) - 100)
    snaps = [p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=t0 + 10 * i) for i in range(8)]
    sid = [snapshots.scan_id(s) for s in snaps]
    got = []
    for i, s in enumerate(snaps):
        if i in (4, 6):                                                  # на этих сканах стартовал круг сухого прогона
            bot.snapshot_keep.add(sid[i])
        got.append(asyncio.run(bot.save_snapshot(s)))
    assert snapshots.ids() == [sid[0], sid[3], sid[4], sid[6]]           # 0, 3, 6 — по очереди; 4 — ради круга
    assert got == [sid[0], None, None, sid[3], sid[4], None, sid[6], None] and not bot.snapshot_keep


def test_lean_snapshot_drops_ads_and_jobs():
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ads=[make_ad()], jobs=[{"ex": "bybit"}],
                        perps={("Bybit", "BTCUSDT"): object()})
    lean = B._lean(snap)
    assert lean.ads == [] and lean.jobs == [] and lean.perps == {} and snap.ads and snap.perps
