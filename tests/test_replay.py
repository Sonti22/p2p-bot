"""replay.py: снимки из data/snapshots.db заново собираются p2p.assemble при других настройках — A/B-сводка."""
import asyncio

import pytest

import netstatus
import p2p
import replay
import snapshots
from helpers import make_ad


def _cfg(**kw):
    base = dict(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0,
                min_profit=-100.0)
    base.update(kw)
    return p2p.Config(**base)


def _saved_scan(cfg):
    snap = asyncio.run(p2p.scan(None, cfg))
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
