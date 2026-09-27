"""p2p.scan = сбор по сети (collect) + чистая сборка снимка (assemble): сборка не ходит в сеть и не трогает
TRAPS_LOG, тот же вход с другим cfg даёт другой снимок (для replay.py), scan ведёт себя как раньше."""

import pytest

import p2p
from helpers import arun, make_ad


def _cfg(**kw):
    base = dict(exchanges=["bybit", "mexc"], assets=["USDT"], min_orders=0, min_rate=0, min_profit=-100.0,
                risk_buffer={}, pay_fee=0.0)
    base.update(kw)
    return p2p.Config(**base)


def _raw(ts=1000.0):
    ads = [make_ad("Bybit", "buy", 85.0, orders=50), make_ad("Bybit", "buy", 86.0, orders=500),
           make_ad("MEXC", "sell", 90.0, orders=500),
           make_ad("Bybit", "buy", 60.0, orders=500)]           # на 30% дешевле рынка — ловушка
    return {"ts": ts, "ads": ads, "ref": 88.0, "ref_src": "Rapira USDT/RUB",
            "spot": {"Bybit": {"USDT": (1.0, 1.0)}}, "errors": {"htx/USDT": "down"}, "jobs": [{"ex": "bybit"}]}


def _keys(snap):
    return [(d[1].ex, d[1].asset, d[2].ex, d[2].asset, round(d[0], 6), d[1].price) for d in snap.deals]


def test_assemble_is_pure_no_network_no_traps_log(monkeypatch):
    async def no_net(*a, **k):
        raise AssertionError("assemble не должен ходить в сеть")
    monkeypatch.setattr(p2p, "_json", no_net)
    p2p.TRAPS_LOG.clear()
    snap = p2p.assemble(_cfg(), **_raw())
    assert snap.deals and snap.ts == 1000.0 and snap.errors == {"htx/USDT": "down"} and snap.jobs == [{"ex": "bybit"}]
    assert snap.ref == 88.0 and snap.ref_src == "Rapira USDT/RUB"
    assert len(snap.traps) == 1 and snap.traps[0]["price"] == 60.0 and snap.traps[0]["ts"] == 1000.0
    assert not p2p.TRAPS_LOG                                            # журнал ловушек пополняет только scan
    assert snap.dropped == {"Bybit": 1}


def test_assemble_same_input_other_cfg():
    raw = _raw()
    loose = p2p.assemble(_cfg(), **raw)
    strict = p2p.assemble(_cfg(min_orders=100), **raw)               # мерчант с 50 сделками отсеян
    assert {d[1].price for d in loose.deals} == {85.0}
    assert {d[1].price for d in strict.deals} == {86.0}
    again = p2p.assemble(_cfg(), **raw)
    assert _keys(again) == _keys(loose)                                # детерминирована


def test_assemble_ref_fallback_median_p2p():
    raw = dict(_raw(), ref=None, ref_src="-")
    snap = p2p.assemble(_cfg(max_dev=50.0), **raw)
    assert snap.ref_src == "медиана P2P" and snap.ref == pytest.approx(85.5)


def test_assemble_blocked_and_over_banks():
    raw = _raw()
    snap = p2p.assemble(_cfg(), blocked=frozenset({("Bybit", "nick")}), over_banks={"T-Bank"}, **raw)
    assert not snap.deals and snap.blocked == frozenset({("Bybit", "nick")})
    assert snap.over_banks == frozenset({"T-Bank"})


def test_scan_is_collect_plus_assemble(monkeypatch):
    raw = _raw(ts=2000.0)

    async def fake_collect(s, cfg, force_alt=False, blocked=frozenset()):
        return dict(raw)
    monkeypatch.setattr(p2p, "collect", fake_collect)
    monkeypatch.setattr(p2p.blacklist, "blocked", lambda: frozenset())
    monkeypatch.setattr(p2p.trades, "banks_over_limit", lambda banks: [])
    p2p.TRAPS_LOG.clear()
    snap = arun(p2p.scan(None, _cfg()))
    assert _keys(snap) == _keys(p2p.assemble(_cfg(), **raw))
    assert [t["price"] for t in p2p.traps_log()] == [60.0]            # scan кладёт ловушки в /traps
    assert p2p.traps_log()[0]["ts"] == 2000.0


def test_scan_offline_fixtures_unchanged(offline):
    """Скан по фикстурам: связки есть, сборка из тех же данных даёт их же."""
    cfg = _cfg(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"])
    snap = arun(p2p.scan(None, cfg))
    assert snap.deals and snap.ts > 0
    again = p2p.assemble(cfg, snap.ads, ref=snap.ref, ref_src=snap.ref_src, spot=snap.spot, errors=snap.errors,
                         blocked=snap.blocked, over_banks=snap.over_banks, ts=snap.ts)
    assert _keys(again) == _keys(snap)
