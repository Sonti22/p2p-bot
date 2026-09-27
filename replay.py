"""Перепрогон снимков (этап 1 «измерения»): снимки сканов из data/snapshots.db (snapshots.py) заново собираются
p2p.assemble — без сети — при других настройках, и печатается короткая A/B-сводка.

A — настройки, с которыми снимок записан (как сканировал бот), B — они же с правками --set. По каждому варианту:
связки выше порога (своего у варианта) по меткам надёжности, уникальные связки, средняя лучшая прибыль; B против A —
сколько связок ушло (и сколько из них не ловушки — «потеря хороших»), сколько новых, как изменилось число ловушек.
Сверка A с живым сканом (связки выше порога, которые записал бот) показывает, насколько перепрогону можно верить:
в снимке только топ-20 объявлений группы, комиссии вывода — текущий fees.json, справочник сетей — из снимка.

Запуск:  python replay.py --hours 24 --set max_dev=3 --set min_orders=200 [--every 3] [--limit 500] [--db путь]
"""
import argparse
import contextlib
import dataclasses
import sys
import time

import netstatus
import p2p
import snapshots

MSK_OFFSET = 3 * 3600
LABELS = (p2p.RELIABLE, p2p.RISKY, p2p.TRAP)


def cfg_of(scan):
    """Config снимка: поля, которых в этой версии нет, пропускаем, новых в снимке нет — по умолчанию."""
    names = {f.name for f in dataclasses.fields(p2p.Config)}
    return p2p.Config(**{k: v for k, v in (scan.get("cfg") or {}).items() if k in names})


def _value(name, text):
    """Значение поля Config из строки — по типу поля (как в .env: списки через запятую, словари «A:1,B:2»)."""
    kind = {f.name: getattr(f.type, "__name__", f.type) for f in dataclasses.fields(p2p.Config)}[name]
    text = text.strip()
    if kind == "bool":
        return text.lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "dict":
        return p2p._fees(text, upper=name != "spot_fees")
    if kind == "list":
        items = [x.strip() for x in text.split(",") if x.strip()]
        return [x.upper() for x in items] if name == "assets" else items
    return text


def override(cfg, specs):
    """Правки вида «поле=значение» (поля p2p.Config) поверх cfg; ValueError — поле неизвестно или значение не то."""
    names = {f.name for f in dataclasses.fields(p2p.Config)}
    changes = {}
    for spec in specs or ():
        name, sep, text = spec.partition("=")
        name = name.strip()
        if not sep or name not in names:
            raise ValueError(f"не понял правку «{spec}»: нужно поле=значение, поля: {', '.join(sorted(names))}")
        changes[name] = _value(name, text)
    return dataclasses.replace(cfg, **changes)


@contextlib.contextmanager
def stored_networks(net):
    """Справочник сетей netstatus на время сборки — сохранённый в снимке, потом прежний."""
    saved = dict(netstatus.STATUS)
    netstatus.STATUS.clear()
    netstatus.STATUS.update({(venue, asset): nets for venue, asset, nets in net or ()})
    try:
        yield
    finally:
        netstatus.STATUS.clear()
        netstatus.STATUS.update(saved)


def rebuild(scan, cfg):
    """Снимок p2p.Snapshot из сохранённого скана при настройках cfg (объявления каждый раз свежие — фильтры
    меняют у них способы оплаты)."""
    spot = {v: {a: tuple(p) for a, p in q.items()} for v, q in (scan.get("spot") or {}).items()}
    with stored_networks(scan.get("net")):
        return p2p.assemble(cfg, snapshots.ads_of(scan), ref=scan.get("ref") or None, ref_src=scan.get("ref_src", "-"),
                            spot=spot or None, errors=scan.get("errors"),
                            blocked=frozenset(tuple(x) for x in scan.get("blocked") or ()),
                            over_banks=frozenset(scan.get("over_banks") or ()), ts=scan.get("ts", 0.0))


def above(snap, cfg):
    """{(buy_ex, монета, sell_ex, монета): (прибыль, метка)} — связки выше порога cfg."""
    return {(d[1].ex, d[1].asset, d[2].ex, d[2].asset): (d[0], p2p.reliability(d, cfg, snap)[0])
            for d in snap.deals if d[0] >= cfg.min_profit}


def _stored_above(scan, cfg):
    return {(d["buy"]["ex"], d["buy"]["asset"], d["sell"]["ex"], d["sell"]["asset"])
            for d in scan.get("deals") or () if d["profit"] >= cfg.min_profit}


def _side():
    return {"deals": 0, "labels": dict.fromkeys(LABELS, 0), "keys": set(), "best": []}


def _add(side, found):
    side["deals"] += len(found)
    for _profit, label in found.values():
        side["labels"][label] = side["labels"].get(label, 0) + 1
    side["keys"] |= set(found)
    if found:
        side["best"].append(max(p for p, _ in found.values()))


def compare(scans, specs):
    """A/B по снимкам scans (словари snapshots.load): A — настройки снимка, B — они же с правками specs."""
    res = {"scans": 0, "first": None, "last": None, "specs": list(specs or ()), "a": _side(), "b": _side(),
           "live": 0, "live_hit": 0, "lost": 0, "lost_good": 0, "new": 0}
    for scan in scans:
        cfg_a = cfg_of(scan)
        cfg_b = override(cfg_a, specs)
        a = above(rebuild(scan, cfg_a), cfg_a)
        b = above(rebuild(scan, cfg_b), cfg_b)
        live = _stored_above(scan, cfg_a)
        res["scans"] += 1
        res["first"] = scan["ts"] if res["first"] is None else min(res["first"], scan["ts"])
        res["last"] = scan["ts"] if res["last"] is None else max(res["last"], scan["ts"])
        res["live"] += len(live)
        res["live_hit"] += len(live & set(a))
        _add(res["a"], a)
        _add(res["b"], b)
        lost = set(a) - set(b)
        res["lost"] += len(lost)
        res["lost_good"] += sum(1 for k in lost if a[k][1] != p2p.TRAP)
        res["new"] += len(set(b) - set(a))
    return res


def _when(ts):
    return time.strftime("%d.%m %H:%M", time.gmtime(ts + MSK_OFFSET))


def _pct(part, whole):
    return f"{part / whole * 100:.0f}%" if whole else "—"


def fmt_summary(res):
    if not res["scans"]:
        return "Перепрогон: снимков за этот период нет (data/snapshots.db пишет бот после каждого скана)."
    a, b = res["a"], res["b"]
    lines = [f"Перепрогон снимков: {res['scans']}, {_when(res['first'])} — {_when(res['last'])} МСК",
             "Правки B: " + (", ".join(res["specs"]) or "нет (B = A)"),
             f"A против живого скана: воспроизведено {_pct(res['live_hit'], res['live'])} связок выше порога "
             f"({res['live_hit']} из {res['live']})", "",
             f"{'':<18}{'A':>8}{'B':>8}",
             f"{'связок ≥ порога':<18}{a['deals']:>8}{b['deals']:>8}"]
    lines += [f"{'  ' + label:<18}{a['labels'].get(label, 0):>8}{b['labels'].get(label, 0):>8}" for label in LABELS]
    avg = (lambda xs: f"{sum(xs) / len(xs):.2f}" if xs else "—")
    lines += [f"{'уникальных':<18}{len(a['keys']):>8}{len(b['keys']):>8}",
              f"{'сред. лучшая, %':<18}{avg(a['best']):>8}{avg(b['best']):>8}", ""]
    good_a = a["deals"] - a["labels"].get(p2p.TRAP, 0)
    traps_a, traps_b = a["labels"].get(p2p.TRAP, 0), b["labels"].get(p2p.TRAP, 0)
    traps = f"{(traps_b - traps_a) / traps_a * 100:+.0f}%" if traps_a else f"{traps_a} → {traps_b}"
    lines.append(f"B против A: ушло {res['lost']}, из них не ловушек {res['lost_good']} "
                 f"({_pct(res['lost_good'], good_a)} хороших связок A), новых {res['new']}; ловушки {traps}")
    return "\n".join(lines)


def load_scans(path=None, hours=24.0, every=1, limit=None, now=None):
    """Снимки за последние hours часов (каждый every-й, не больше limit — самые свежие)."""
    path = path or snapshots.DB_PATH
    now = time.time() if now is None else now
    sids = snapshots.ids(path, since=now - hours * 3600)[::max(1, every)]
    if limit:
        sids = sids[-limit:]
    for sid in sids:
        scan = snapshots.load(sid, path)
        if scan:
            yield scan


def main(argv=None):
    ap = argparse.ArgumentParser(description="Перепрогон снимков сканов при других настройках (A/B).")
    ap.add_argument("--hours", type=float, default=24.0, help="за сколько последних часов (24)")
    ap.add_argument("--set", dest="specs", action="append", default=[], metavar="поле=значение",
                    help="правка настроек для B (поле p2p.Config), можно несколько")
    ap.add_argument("--every", type=int, default=1, help="брать каждый N-й снимок (1)")
    ap.add_argument("--limit", type=int, default=None, help="не больше N самых свежих снимков")
    ap.add_argument("--db", default=None, help="путь к snapshots.db (data/snapshots.db)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # консоль Windows без UTF-8 — не падать на эмодзи меток
    try:
        override(p2p.Config(), args.specs)   # опечатку в правке — сразу, а не на первом снимке
    except ValueError as e:
        print(e)
        return 2
    print(fmt_summary(compare(load_scans(args.db, args.hours, args.every, args.limit), args.specs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
