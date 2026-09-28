"""replay.py: снимки из data/snapshots.db заново собираются p2p.assemble при других настройках — A/B-сводка."""

import pytest

import netstatus
import p2p
import replay
import snapshots
from helpers import arun, make_ad


def _cfg(**kw):
    base = dict(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0,
                min_profit=-100.0)
    base.update(kw)
    return p2p.Config(**base)


def _saved_scan(cfg):
    snap = arun(p2p.scan(None, cfg))
    assert snap.deals
    return snapshots.load(snapshots.save(snap, cfg)), snap


def test_replay_same_cfg_reproduces_live_scan(offline):
    scan, snap = _saved_scan(_cfg())
    res = replay.compare([scan], [])
    assert res["scans"] == 1 and res["live"] > 0 and res["live_hit"] == res["live"]
    assert res["a"]["deals"] == res["b"]["deals"] == len(snap.deals)
    assert res["lost"] == res["new"] == 0
    text = replay.fmt_summary(res)
    assert "воспроизведено 100%" in text and "Правки B: нет (B = A)" in text


def test_replay_stricter_cfg_loses_deals(offline):
    scan, _snap = _saved_scan(_cfg())
    res = replay.compare([scan], ["min_orders=1000000"])
    assert res["a"]["deals"] > 0 and res["b"]["deals"] == 0
    assert res["lost"] == len(res["a"]["keys"]) and res["new"] == 0
    traps = res["a"]["labels"][p2p.TRAP]
    assert res["lost_good"] == res["a"]["deals"] - traps
    assert "Правки B: min_orders=1000000" in replay.fmt_summary(res)


def test_replay_threshold_of_each_variant(offline):
    scan, snap = _saved_scan(_cfg())
    best = snap.deals[0][0]
    res = replay.compare([scan], [f"min_profit={best + 100}"])
    assert res["a"]["deals"] == len(snap.deals) and res["b"]["deals"] == 0


def _venues(snap):
    """Площадки всего, из чего собран снимок: объявления, лучшие цены, стаканы связок и /maker, сами связки."""
    return ({a.ex for a in snap.ads} | {k[0] for k in snap.best} | {k[0] for k in snap.groups}
            | {k[0] for k in snap.book} | {x.ex for d in snap.deals for x in d[1:3]})


def _assets(snap):
    return ({a.asset for a in snap.ads} | {k[2] for k in snap.best} | {k[2] for k in snap.groups}
            | {k[2] for k in snap.book} | {x.asset for d in snap.deals for x in d[1:3]} | set(snap.refs))


def test_replay_disabled_venue_drops_its_routes(offline):
    """B с выключенной площадкой — как живой скан без неё: объявлений, стаканов и связок этой площадки нет (а не
    MEXC в результатах при exchanges=bybit). Регистр ключа — как в .env (там EXCHANGES приводится к нижнему)."""
    assert set(p2p.MERCHANT_VENUES) == set(p2p.FETCHERS)   # ключ площадки → Ad.ex есть у каждой площадки скана
    scan, snap = _saved_scan(_cfg())
    assert "MEXC" in {x.ex for d in snap.deals for x in d[1:3]}
    for spec in ("exchanges=bybit", "exchanges=Bybit"):
        snap_b = replay.rebuild(scan, replay.override(replay.cfg_of(scan), [spec]))
        assert _venues(snap_b) == {"Bybit"}
        res = replay.compare([scan], [spec])
        assert res["b"]["keys"] and all(k[0] == k[2] == "Bybit" for k in res["b"]["keys"])
    live = arun(p2p.scan(None, _cfg(exchanges=["bybit"])))   # тот же ответ площадок, живой скан без MEXC
    assert _venues(live) == {"Bybit"}                                # collect и сам опрашивает только включённые
    assert {replay._key(d) for d in snap_b.deals} == {replay._key(d) for d in live.deals}


def test_replay_excluded_asset_drops_it_from_results_and_books(offline):
    """B без монеты — её нет ни в связках, ни в стаканах и ориентирах (а не USDT→USDT при assets=BTC)."""
    scan, snap = _saved_scan(_cfg(assets=["USDT", "BTC"]))
    assert {x.asset for d in snap.deals for x in d[1:3]} >= {"USDT", "BTC"}
    snap_b = replay.rebuild(scan, replay.override(replay.cfg_of(scan), ["assets=btc"]))
    assert _assets(snap_b) == {"BTC"}
    res = replay.compare([scan], ["assets=BTC"])
    assert res["b"]["keys"] and all(k[1] == k[3] == "BTC" for k in res["b"]["keys"])
    assert res["a"]["deals"] == len(snap.deals) and res["live_hit"] == res["live"]   # A = живой скан, как и был
    live = arun(p2p.scan(None, _cfg(assets=["BTC"])))
    assert _assets(live) == {"BTC"}
    assert {replay._key(d) for d in snap_b.deals} == {replay._key(d) for d in live.deals}


def _rapira_down(monkeypatch):
    async def down(s):
        raise OSError("Rapira недоступна")
    monkeypatch.setattr(p2p, "rapira_mid", down)


def _keys(snap):
    return {replay._key(d) for d in snap.deals}


@pytest.mark.parametrize("venues,max_dev", [(["bybit"], 4.0), (["htx", "kucoin"], 0.5), (["bybit", "kucoin"], 3.0)])
def test_replay_median_ref_recomputed_without_disabled_venues(offline, monkeypatch, venues, max_dev):
    """Rapira не ответила — ориентир снимка «медиана P2P» по всем его площадкам. B без части площадок считает медиану
    заново по оставшимся, как живой скан с тем же набором (иначе ориентир и отсев MAX_DEV B зависят от выключенных)."""
    _rapira_down(monkeypatch)
    scan, _snap = _saved_scan(_cfg())
    assert scan["ref_src"] == p2p.REF_MEDIAN
    specs = ["exchanges=" + ",".join(venues), f"max_dev={max_dev}"]
    snap_b = replay.rebuild(scan, replay.override(replay.cfg_of(scan), specs))
    live = arun(p2p.scan(None, _cfg(exchanges=venues, max_dev=max_dev)))
    assert live.deals and live.ref != scan["ref"]                    # медиана без выключенных площадок другая
    assert snap_b.ref == pytest.approx(live.ref) and snap_b.ref_src == p2p.REF_MEDIAN
    assert _keys(snap_b) == _keys(live)
    assert replay.compare([scan], specs)["b"]["keys"] == _keys(live)


def test_replay_median_ref_recomputed_without_usdt(offline, monkeypatch):
    """assets=BTC при медиане P2P: USDT в B нет — ориентира USDT нет, ориентир BTC — медиана BTC, как у живого скана,
    а не записанная медиана USDT × спот."""
    _rapira_down(monkeypatch)
    scan, _snap = _saved_scan(_cfg(assets=["USDT", "BTC"]))
    snap_b = replay.rebuild(scan, replay.override(replay.cfg_of(scan), ["assets=BTC"]))
    live = arun(p2p.scan(None, _cfg(assets=["BTC"])))
    assert live.ref == snap_b.ref == 0 and live.ref_src == snap_b.ref_src == "-"
    assert snap_b.refs == pytest.approx(live.refs) and _keys(snap_b) == _keys(live)


def test_replay_keeps_stored_ref_when_nothing_dropped(offline, monkeypatch):
    """Площадки и монеты B те же — записанная медиана остаётся (в снимке только топ-20 объявлений группы: пересчёт
    ушёл бы от живого скана); выключена только не-USDT монета — медиана USDT та же, тоже записанная."""
    _rapira_down(monkeypatch)
    scan, _snap = _saved_scan(_cfg(assets=["USDT", "BTC"]))
    seen = []
    real = p2p.assemble

    def spy(cfg, ads, **kw):
        seen.append(kw["ref"])
        return real(cfg, ads, **kw)
    monkeypatch.setattr(p2p, "assemble", spy)
    for specs in ([], ["max_dev=1"], ["assets=USDT"]):
        replay.rebuild(scan, replay.override(replay.cfg_of(scan), specs))
    assert seen == [scan["ref"]] * 3


def test_replay_a_matches_live_when_rapira_down(offline, monkeypatch):
    """Rapira не ответила: A (настройки снимка) берёт записанную медиану и воспроизводит живой скан целиком."""
    _rapira_down(monkeypatch)
    scan, snap = _saved_scan(_cfg())
    res = replay.compare([scan], [])
    assert res["live"] > 0 and res["live_hit"] == res["live"] and res["a"]["deals"] == len(snap.deals)
    assert replay.rebuild(scan, replay.cfg_of(scan)).ref == snap.ref


def test_rebuild_uses_stored_networks_and_restores(monkeypatch):
    b, s = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    for a in (b, s):
        a.fetched_ts = 1000.0
    netstatus.STATUS[("HTX", "USDT")] = {"TRC20": {"dep": True, "wd": True, "fee": 1.0, "min": 10.0}}
    data = snapshots.collect(p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=1000.0, ads=[b, s]), _cfg())
    data["net"] = [["Bybit", "USDT", {"TRC20": {"dep": True, "wd": False, "fee": 1.0, "min": 10.0}}]]
    scan = snapshots.load(snapshots.write(data))
    seen = {}
    real = p2p.assemble

    def spy(cfg, ads, **kw):
        seen["status"] = dict(netstatus.STATUS)
        seen["ads"] = [(a.ex, a.side, a.price) for a in ads]
        seen.update(kw)
        return real(cfg, ads, **kw)
    monkeypatch.setattr(p2p, "assemble", spy)
    replay.rebuild(scan, _cfg())
    assert seen["status"] == {("Bybit", "USDT"): {"TRC20": {"dep": True, "wd": False, "fee": 1.0, "min": 10.0}}}
    assert sorted(seen["ads"]) == [("Bybit", "buy", 85.0), ("MEXC", "sell", 90.0)]
    assert seen["ref"] == 88.0 and seen["ts"] == 1000.0
    assert netstatus.STATUS == {("HTX", "USDT"): {"TRC20": {"dep": True, "wd": True, "fee": 1.0, "min": 10.0}}}


def test_override_types_and_errors():
    cfg = p2p.Config()
    got = replay.override(cfg, ["amount=75000.5", "min_orders=200", "same_venue_only=1", "assets=usdt,btc",
                                "spot_fees=Bybit:0.2", "exchanges=bybit,mexc", "fiat=RUB"])
    assert got.amount == 75000.5 and got.min_orders == 200 and got.same_venue_only is True
    assert got.assets == ["USDT", "BTC"] and got.spot_fees == {"Bybit": 0.2} and got.exchanges == ["bybit", "mexc"]
    assert cfg.min_orders == 100                                         # исходный cfg не тронут
    with pytest.raises(ValueError):
        replay.override(cfg, ["nope=1"])
    with pytest.raises(ValueError):
        replay.override(cfg, ["min_orders"])
    with pytest.raises(ValueError):
        replay.override(cfg, ["min_orders=abc"])


def test_override_pay_filters_lowercased_like_env(monkeypatch):
    """INCLUDE_PAY/EXCLUDE_PAY в .env приводятся к нижнему регистру (p2p._list), а _pays ищет подстроку в p.lower() —
    правка --set с заглавной буквой отбирает (исключает) те же способы оплаты, что и .env."""
    monkeypatch.setenv("INCLUDE_PAY", "Tinkoff")
    monkeypatch.setenv("EXCLUDE_PAY", "Sber")
    env = p2p.Config.from_env()
    got = replay.override(p2p.Config(), ["include_pay=Tinkoff", "exclude_pay=Sber"])
    assert got.include_pay == env.include_pay == ["tinkoff"] and got.exclude_pay == env.exclude_pay == ["sber"]
    assert p2p._pays(make_ad("Bybit", "buy", 85.0, pays=["Tinkoff"]), got) == ["Tinkoff"]
    assert p2p._pays(make_ad("Bybit", "buy", 85.0, pays=["Sberbank", "Tinkoff"]), got) == ["Tinkoff"]


@pytest.mark.parametrize("spec,field", [("include_pay=Bank", "include_pay"), ("exclude_pay=Bank,Cash", "exclude_pay")])
def test_override_pay_filters_in_replay(offline, spec, field):
    """B с --set include_pay/exclude_pay с заглавной буквы — связки как у живого скана с тем же списком из .env (в
    нижнем регистре), а не ноль связок (include) или ничего не исключено (exclude) из-за регистра."""
    scan, _snap = _saved_scan(_cfg())
    snap_b = replay.rebuild(scan, replay.override(replay.cfg_of(scan), [spec]))
    live = arun(p2p.scan(None, _cfg(**{field: spec.partition("=")[2].lower().split(",")})))
    assert live.deals and _keys(snap_b) == _keys(live)


def test_override_dict_bad_format_rejected_clearly():
    cfg = p2p.Config()
    for spec in ("risk_buffer=USDT:abc", "risk_buffer=USDT:0.2,BTC", "spot_fees=Bybit:100/97"):
        with pytest.raises(ValueError, match="нужен формат вида USDT:0.2,BTC:0.5"):
            replay.override(cfg, [spec])
    assert replay.override(cfg, ["risk_buffer=usdt:0.2,btc:0.5"]).risk_buffer == {"USDT": 0.2, "BTC": 0.5}


def test_override_dict_uses_field_parser():
    """У словаря свой формат (MERCHANT_MIN «Bybit:100/97,HTX:300/96» — p2p.parse_merchant_min) — берётся разбор поля
    из replay.DICT_PARSERS; не разобралась хоть одна часть — понятная ошибка, а не молча неполный словарь."""
    assert replay.DICT_PARSERS["merchant_min"] is p2p.parse_merchant_min
    got = replay.override(p2p.Config(), ["merchant_min=Bybit:100/97,HTX:300/96"])
    assert got.merchant_min == {"Bybit": (100, 97.0), "HTX": (300, 96.0)}
    with pytest.raises(ValueError, match="нужен формат вида Bybit:100/97,HTX:300/96"):
        replay.override(p2p.Config(), ["merchant_min=Bybit:100/97,Nowhere:5/5"])
    with pytest.raises(ValueError, match="нужен формат вида USDT:0.2,BTC:0.5"):   # без своего разбора — формат _fees
        replay.override(p2p.Config(), ["transfer_fees=Bybit:100/97,HTX:300/96"])


def test_cfg_of_skips_unknown_fields():
    cfg = replay.cfg_of({"cfg": {"amount": 70000, "gone_field": 1}})
    assert cfg.amount == 70000 and cfg.min_orders == p2p.Config().min_orders


def test_main_prints_summary(offline, capsys):
    _saved_scan(_cfg())
    assert replay.main(["--hours", "1", "--set", "max_dev=2"]) == 0
    out = capsys.readouterr().out
    assert "Перепрогон снимков: 1," in out and "Правки B: max_dev=2" in out and "B против A" in out
    assert replay.main(["--set", "bogus=1"]) == 2
    assert "не понял правку" in capsys.readouterr().out


def test_main_no_snapshots(capsys):
    assert replay.main([]) == 0
    assert "снимков за этот период нет" in capsys.readouterr().out
