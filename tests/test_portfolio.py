import dataclasses
import json
import sqlite3
from types import SimpleNamespace

import pytest

import p2p
import paper
import portfolio as pf


def ad(side, price=100, avail=1000, ex="Bybit", asset="USDT", ts=1000, minimum=0):
    return p2p.Ad(ex, side, price, minimum, 100000, avail, ["SBP"], "merchant", 100, 99,
                  asset=asset, fetched_ts=ts, ad_id="offer")


def snapshot(*ads, spot=None, now=1000):
    groups = {}
    for a in ads:
        groups.setdefault((a.ex, a.side, a.asset), []).append(a)
    return SimpleNamespace(groups=groups, errors={}, spot=spot or {}, ts=now)


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "0")
    monkeypatch.setenv("PAPER_TRANSFER_MINUTES", "0")
    return p2p.Config()


def start(path, cfg, amount=10000, **kw):
    b, s = ad("buy"), ad("sell", 110)
    hops = {"venues": [], "hops": [{"frm": "Bybit", "to": "Bybit", "asset": "USDT", "fee": 0,
                                     "frm_net": "", "to_net": ""}]}
    return pf.start(amount, b, s, hops, 10, path=path, now=1000, **kw)


def ready_sell(path, cfg):
    pf.tick(snapshot(ad("buy")), cfg, path, now=1000)
    pf.tick(snapshot(), cfg, path, now=1001)
    pf.tick(snapshot(), cfg, path, now=1002)
    pf.tick(snapshot(), cfg, path, now=1003)


def test_reserved_funds_cannot_be_spent_twice(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    assert start(path, cfg, 30000, max_open=5)
    assert start(path, cfg, 30000, max_open=5) is None
    assert pf.summary(path)["cash"] == "20000.00"


def test_partial_sale_preserves_cost_and_restart(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    ready_sell(path, cfg)
    offer = ad("sell", 110, 40, ts=1004)
    pf.tick(snapshot(offer), cfg, path, now=1004)
    r = pf.runs(path)[0]
    assert pf.dec(r["qty"]) == 60
    assert pf.dec(r["cost"]) == 6000
    assert pf.dec(r["realized"]) == 400
    assert pf.dec(pf.summary(path)["cash"]) == 44400
    # Reprocessing after reopening the database cannot reuse the same cached offer.
    pf.tick(snapshot(offer), cfg, path, now=1005)
    assert pf.dec(pf.runs(path)[0]["qty"]) == 60
    pf.tick(snapshot(ad("sell", 110, 60, ts=1006)), cfg, path, now=1006)
    assert pf.runs(path)[0]["stage"] == "done"
    assert pf.dec(pf.summary(path)["cash"]) == 51000


def test_purchase_timeout_releases_only_unspent_cash(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    pf.tick(snapshot(ad("buy", avail=40)), cfg, path, now=1000)
    pf.tick(snapshot(), cfg, path, now=2800)
    s = pf.summary(path)
    assert pf.dec(s["cash"]) == 46000
    assert pf.dec(s["reserved"]) == 0
    assert pf.dec(s["runs"][0]["qty"]) == 40


def test_no_purchase_with_stale_or_missing_timestamp(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    pf.tick(snapshot(ad("buy", ts=0), ad("buy", ts=500)), cfg, path, now=1000)
    assert pf.runs(path)[0]["qty"] == "0"


def test_loss_after_wait_is_realized(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    ready_sell(path, cfg)
    pf.tick(snapshot(ad("sell", 90, ts=1004)), cfg, path, now=1004)
    assert pf.runs(path)[0]["stage"] == "sell"
    pf.tick(snapshot(ad("sell", 90, ts=2804)), cfg, path, now=2804)
    assert pf.dec(pf.summary(path)["realized"]) == -1000
    assert pf.dec(pf.summary(path)["cash"]) == 49000


def test_unknown_network_retains_inventory_then_attempts_local_sale(tmp_path, cfg, monkeypatch):
    path = str(tmp_path / "wallet.db")
    b, s = ad("buy"), ad("sell", ex="MEXC")
    hops = {"hops": [{"frm": "Bybit", "to": "MEXC", "asset": "USDT", "fee": 1, "to_net": "TRC20"}]}
    pf.start(10000, b, s, hops, 1, path=path, now=1000)
    monkeypatch.setattr(paper, "_hop_state", lambda h: ([], ["unknown"]))
    pf.tick(snapshot(b), cfg, path, now=1000)
    pf.tick(snapshot(), cfg, path, now=1001)
    r = pf.runs(path)[0]
    assert pf.dec(r["qty"]) == 100 and not r["in_transit"]
    pf.tick(snapshot(), cfg, path, now=2801)
    assert pf.runs(path)[0]["stage"] == "sell"
    pf.tick(snapshot(ad("sell", 95, ts=2802)), cfg, path, now=2802)
    assert pf.dec(pf.summary(path)["cash"]) == 49500


def test_spot_executes_at_each_leg_once(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    b, s = ad("buy"), ad("sell", 2000, asset="ETH")
    hops = {"venues": ["Bybit"], "hops": [
        {"frm": "Bybit", "to": "Bybit", "asset": "USDT", "fee": 0},
        {"frm": "Bybit", "to": "Bybit", "asset": "ETH", "fee": 0}]}
    pf.start(10000, b, s, hops, 1, path=path, now=1000)
    pf.tick(snapshot(b), cfg, path, now=1000)
    for now in (1001, 1002, 1003):
        pf.tick(snapshot(spot={"Bybit": {"ETH": (19, 20)}}), cfg, path, now=now)
    assert pf.runs(path)[0]["asset"] == "ETH"
    qty = pf.runs(path)[0]["qty"]
    pf.tick(snapshot(spot={"Bybit": {"ETH": (39, 40)}}), cfg, path, now=1004)
    assert pf.runs(path)[0]["qty"] == qty
    assert pf.runs(path)[0]["assumptions"]


def test_atomic_rollback_after_execution_failure(tmp_path, cfg, monkeypatch):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    before = pf.summary(path)
    monkeypatch.setattr(pf, "_mark", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        pf.tick(snapshot(ad("buy")), cfg, path, now=1000)
    assert pf.summary(path) == before
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM consumed").fetchone()[0] == 0


def test_unknown_valuation_replaces_old_complete_mark(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    ready_sell(path, cfg)
    pf.tick(snapshot(ad("sell", 90, ts=1004)), cfg, path, now=1004)
    assert pf.summary(path)["equity"] == "49000.00"
    pf.tick(snapshot(), cfg, path, now=1005)
    assert pf.summary(path)["equity"] is None


def test_archive_and_csv(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    pf.tick(snapshot(ad("buy")), cfg, path, now=1000)
    destination = str(tmp_path / "journal.csv")
    pf.export(destination, path)
    assert "buy" in open(destination, encoding="utf-8-sig").read()
    archive = pf.reset(path)
    assert pf.dec(pf.runs(archive)[0]["qty"]) == 100
    assert pf.summary(path)["cash"] == "50000.00"
    assert not pf.runs(path)


def test_ad_minimum_is_not_bypassed(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg)
    pf.tick(snapshot(ad("buy", avail=1, minimum=1000)), cfg, path, now=1000)
    assert pf.runs(path)[0]["qty"] == "0"


def test_bank_fee_and_journal_reconstruction(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg, pay_fee=1)
    ready_sell(path, cfg)
    assert pf.dec(pf.runs(path)[0]["qty"]) == 99
    pf.tick(snapshot(ad("sell", 110, ts=1004)), cfg, path, now=1004)
    assert pf.dec(pf.summary(path)["cash"]) == 50890
    assert pf.dec(pf.replay(path)["cash"]) == pf.dec(pf.summary(path)["cash"])
    assert pf.replay(path)["runs"] == pf.runs(path)


def test_concurrent_reservations(tmp_path, cfg):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / "wallet.db")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(path, cfg, 30000, max_open=2), range(2)))
    assert sum(r is not None for r in results) == 1
    assert pf.dec(pf.summary(path)["cash"]) == 20000


def test_transfer_fee_and_in_transit_survive_restart(tmp_path, cfg, monkeypatch):
    path = str(tmp_path / "wallet.db")
    b, s = ad("buy"), ad("sell", ex="MEXC")
    hops = {"hops": [{"frm": "Bybit", "to": "MEXC", "asset": "USDT", "fee": 1, "to_net": "TRC20"}]}
    pf.start(10000, b, s, hops, 1, path=path, now=1000)
    monkeypatch.setattr(paper, "_hop_state", lambda h: ([], []))
    monkeypatch.setattr(p2p, "_hop_detail", lambda *a, **kw: (2, "", "TRC20"))
    pf.tick(snapshot(b), cfg, path, now=1000)
    pf.tick(snapshot(), cfg, path, now=1001)
    assert pf.runs(path)[0]["in_transit"]
    assert pf.dec(pf.runs(path)[0]["qty"]) == 98
    pf.tick(snapshot(), cfg, path, now=1002)
    assert pf.dec(pf.runs(path)[0]["qty"]) == 98
    pf.tick(snapshot(), cfg, path, now=1182)
    assert not pf.runs(path)[0]["in_transit"]
    assert pf.runs(path)[0]["venue"] == "MEXC"
    pf.tick(snapshot(), cfg, path, now=1183)
    pf.tick(snapshot(ad("sell", 110, ex="MEXC", ts=1184)), cfg, path, now=1184)
    assert pf.runs(path)[0]["stage"] == "done"
    assert pf.dec(pf.summary(path)["cash"]) == 50780


def test_shared_quote_depth_not_counted_twice_for_valuation(tmp_path, cfg):
    path = str(tmp_path / "wallet.db")
    start(path, cfg, max_open=2)
    start(path, cfg, max_open=2)
    # A single fresh purchase offer covers both runs.
    pf.tick(snapshot(ad("buy")), cfg, path, now=1000)
    pf.tick(snapshot(ad("sell", 90, avail=150, ts=1001)), cfg, path, now=1001)
    assert pf.summary(path)["equity"] is None


def test_bot_uses_new_portfolio_and_exports_it(cfg, monkeypatch):
    import time
    import bot as B
    from helpers import arun
    from test_bot import Stub, texts

    monkeypatch.setenv("PAPER_ENGINE", "ledger")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    now = time.time()
    b, s = ad("buy", ts=now), ad("sell", 102, ts=now)
    deal = (2, b, s, "same venue")
    snap = p2p.Snapshot(100, "t", {"USDT": 100}, {}, [deal], {}, {}, {},
                        groups={("Bybit", "buy", "USDT"): [b], ("Bybit", "sell", "USDT"): [s]}, ts=now)
    bot = Stub(dataclasses.replace(cfg, min_profit=0.1, risk_buffer={"USDT": 0.0}))
    bot.live_scans = 1
    arun(bot.maybe_start_paper_cycle([deal], snap))
    assert len(pf.runs()) == 1
    assert not paper.open_cycles()
    assert pf.dec(pf.summary()["cash"]) == 40000
    assert "покупка ещё не исполнена" in texts(bot)[-1]
    for _ in range(5):
        arun(bot.process_paper_cycles(snap))
    assert pf.runs()[0]["stage"] == "done"
    assert "50200.00" in bot.paper_view()
    arun(bot.cmd_paper("report"))
    assert any(kind == "sendDocument" for kind, _ in bot.out)


def test_two_venue_conversion_keeps_intermediate_usdt(tmp_path, cfg, monkeypatch):
    path = str(tmp_path / "wallet.db")
    b, s = ad("buy", asset="BTC"), ad("sell", asset="ETH", ex="MEXC")
    hops = {"venues": ["Bybit", "MEXC"], "hops": [
        {"frm": "Bybit", "to": "Bybit", "asset": "BTC", "fee": 0},
        {"frm": "Bybit", "to": "MEXC", "asset": "USDT", "fee": 1, "to_net": "TRC20"},
        {"frm": "MEXC", "to": "MEXC", "asset": "ETH", "fee": 0}]}
    monkeypatch.setenv("PAPER_NET_MINUTES", "TRC20:0")
    monkeypatch.setattr(paper, "_hop_state", lambda h: ([], []))
    monkeypatch.setattr(p2p, "_hop_detail", lambda *a, **kw: (1, "", "TRC20"))
    pf.start(10000, b, s, hops, 1, path=path, now=1000, spot_fees={"Bybit": 0.1, "MEXC": 0.1})
    pf.tick(snapshot(b), cfg, path, now=1000)
    for now in (1001, 1002, 1003):
        pf.tick(snapshot(spot={"Bybit": {"BTC": (20, 21)}}, now=now), cfg, path, now=now)
    assert pf.runs(path)[0]["asset"] == "USDT"
    assert pf.dec(pf.runs(path)[0]["qty"]) == 1998
    # Missing the second venue's quote cannot erase the intermediate asset.
    for now in (1004, 1005, 1006):
        pf.tick(snapshot(now=now), cfg, path, now=now)
    assert pf.runs(path)[0]["asset"] == "USDT"
    assert pf.dec(pf.runs(path)[0]["qty"]) == 1997
    pf.tick(snapshot(spot={"MEXC": {"ETH": (9, 10)}}, now=1007), cfg, path, now=1007)
    assert pf.runs(path)[0]["asset"] == "ETH"
    assert pf.dec(pf.runs(path)[0]["qty"]) == pf.dec("199.5003")
    assert pf.dec(pf.replay(path)["cash"]) == pf.dec(pf.summary(path)["cash"])


def test_daily_backup_includes_new_wallet(tmp_path, cfg, monkeypatch):
    import backup
    path = str(tmp_path / "paper_portfolio.db")
    start(path, cfg)
    ready_sell(path, cfg)
    monkeypatch.setattr(backup, "_state", {"tried": 0})
    monkeypatch.setenv("BACKUP_KEEP", "7")
    destination, files = backup.run(data_dir=str(tmp_path))
    assert "paper_portfolio.db" in files
    copied = str(__import__("pathlib").Path(destination) / "paper_portfolio.db")
    assert pf.runs(copied) == pf.runs(path)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", -1, 0])
def test_invalid_capital_does_not_create_wallet(tmp_path, cfg, amount):
    path = str(tmp_path / "wallet.db")
    with pytest.raises(ValueError):
        start(path, cfg, amount)
    assert not __import__("os").path.exists(path)
