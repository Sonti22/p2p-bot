import dataclasses
import datetime as dt
import json
from decimal import Decimal

import pytest

import bankmodel as bm
import p2p
import portfolio as pf
import scenarios as sc
from test_portfolio import ad, snapshot

NOW = dt.datetime(2026, 10, 8, 12, tzinfo=bm.MSK).timestamp()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, 'ROOT', str(tmp_path))
    monkeypatch.setenv('PAPER_TRANSFER_MINUTES', '0')
    monkeypatch.setenv('PAPER_PAY_MINUTES', '0')
    return p2p.Config()


def start(cfg, name='base', amount=10000):
    sc.maintain(name, cfg, NOW)
    with sc.context(name, cfg, NOW):
        b, s = ad('buy', ts=NOW), ad('sell', 110, ts=NOW)
        hops = {'hops': [{'frm': 'Bybit', 'to': 'Bybit', 'asset': 'USDT', 'fee': 0}]}
        return pf.start(amount, b, s, hops, 10, path=sc.path(name), now=NOW)


def tick(cfg, at, *offers, name='base'):
    sc.maintain(name, cfg, at)
    with sc.context(name, cfg, at):
        return pf.tick(snapshot(*offers, now=at), cfg, path=sc.path(name), now=at)


def to_sell(cfg, name='base'):
    data = sc.initialize(name, cfg, NOW)
    at = NOW + data['payment_seconds']
    tick(cfg, at, ad('buy', ts=at), name=name)
    at += data['release_seconds']
    tick(cfg, at, name=name)
    for _ in range(3):
        at += 1
        tick(cfg, at, name=name)
    assert pf.runs(sc.path(name))[0]['stage'] == 'sell'
    return at


def test_initial_service_and_isolation(cfg):
    for name in sc.VARIANTS:
        sc.maintain(name, cfg, NOW)
        sc.maintain(name, cfg, NOW)
        summary = pf.summary(sc.path(name))
        assert summary['initial'] == '50000.00'
        assert Decimal(summary['cash']) == 49901
        assert Decimal(summary['bank_expenses']) == 99
    assert bm.SCENARIO.get() is None
    with sc.context('base', cfg, NOW) as data:
        profile = data['profiles']['accounts'][0]
        assert profile['service']['confirmed'] is False
        bm.validate(profile, NOW)
    with pytest.raises(bm.Blocked):
        bm.validate(profile, NOW)


@pytest.mark.parametrize('name', sc.VARIANTS)
def test_full_cycle_delays_restart_profit(cfg, name):
    run = start(cfg, name)
    assert run['scenario'] == name and run['assumptions']
    at = NOW + sc.VARIANTS[name][0] - 1
    tick(cfg, at, ad('buy', ts=at), name=name)
    assert Decimal(pf.runs(sc.path(name))[0]['qty']) == 0
    sale = to_sell(cfg, name)
    tick(cfg, sale, ad('sell', 110, ts=sale), name=name)
    assert Decimal(pf.summary(sc.path(name))['realized']) == 0
    at = sale + sc.VARIANTS[name][0]
    tick(cfg, at, ad('sell', 110, ts=at), name=name)
    summary = pf.summary(sc.path(name))
    assert summary['runs'][0]['stage'] == 'done'
    assert Decimal(summary['realized']) == 1000
    assert Decimal(summary['net_realized']) == 901
    tick(cfg, at + 1, ad('sell', 110, ts=at), name=name)
    assert Decimal(pf.summary(sc.path(name))['cash']) == 50901
    con = pf.connect(sc.path(name))
    pf._verify(con)
    con.close()


def test_loss_and_partial_sale_are_preserved(cfg):
    start(cfg)
    sale = to_sell(cfg)
    at = sale + 1800
    tick(cfg, at, ad('sell', 90, 40, ts=at))
    run = pf.runs(sc.path('base'))[0]
    assert Decimal(run['qty']) == 60
    assert Decimal(run['cost']) == 6000
    assert Decimal(run['realized']) == -400
    tick(cfg, at + 1, ad('sell', 90, 40, ts=at + 1))
    assert Decimal(pf.runs(sc.path('base'))[0]['qty']) == 60  # stable ID: no invented refill
    second = dataclasses.replace(ad('sell', 90, 60, ts=at + 2), ad_id='another-offer')
    tick(cfg, at + 2, second)
    assert Decimal(pf.summary(sc.path('base'))['net_realized']) == -1099


def test_missing_or_stale_offer_never_fills(cfg):
    start(cfg)
    tick(cfg, NOW + 300, ad('buy', ts=NOW))
    assert Decimal(pf.runs(sc.path('base'))[0]['qty']) == 0
    tick(cfg, NOW + 2100)
    assert pf.runs(sc.path('base'))[0]['stage'] == 'cancelled'
    assert Decimal(pf.summary(sc.path('base'))['cash']) == 49901


def test_self_transfer_in_transit_restart_and_ledger(cfg):
    sc.maintain('base', cfg, NOW)
    with sc.context('base', cfg, NOW):
        pf.own_transfer('tbank-black', 'vtb-debit', 10000, 60, path=sc.path('base'), now=NOW)
    summary = pf.summary(sc.path('base'))
    assert Decimal(summary['cash']) == 39901
    assert Decimal(summary['bank_in_transit']) == 10000
    sc.maintain('base', cfg, NOW + 59)
    assert Decimal(pf.summary(sc.path('base'))['cash']) == 39901
    sc.maintain('base', cfg, NOW + 60)
    sc.maintain('base', cfg, NOW + 61)
    assert Decimal(pf.summary(sc.path('base'))['cash']) == 49901


def test_fee_crossing_month_boundary_and_no_double_charge(cfg):
    sc.maintain('base', cfg, NOW)
    with sc.context('base', cfg, NOW) as data:
        con = pf.connect(sc.path('base'))
        p = data['profiles']['accounts'][0]
        bm.payment(con, bm.quote(con, p, 'sbp', 'out', 90000, NOW), NOW, None)
        assert bm.quote(con, p, 'sbp', 'out', 50000, NOW)['fee'] == '200.00'
        november = dt.datetime(2026, 11, 1, tzinfo=bm.MSK).timestamp()
        assert bm.quote(con, p, 'sbp', 'out', 50000, november)['fee'] == '0.00'
        con.rollback()
        con.close()


def test_monthly_service_exact_once(cfg):
    sc.maintain('base', cfg, NOW)
    next_month = dt.datetime(2026, 11, 8, 12, tzinfo=bm.MSK).timestamp()
    sc.maintain('base', cfg, next_month - 1)
    assert Decimal(pf.summary(sc.path('base'))['bank_expenses']) == 99
    sc.maintain('base', cfg, next_month)
    sc.maintain('base', cfg, next_month)
    assert Decimal(pf.summary(sc.path('base'))['bank_expenses']) == 198


def test_unknown_channels_and_restrictive_offer_block(cfg):
    sc.maintain('base', cfg, NOW)
    unknown = dataclasses.replace(ad('buy', ts=NOW), pays=['Cash deposit'])
    assert sc.prepare('base', unknown, ad('sell', ts=NOW), 10000, cfg, NOW) is None
    restricted = dataclasses.replace(ad('buy', ts=NOW), terms='Только ИП')
    assert sc.prepare('base', restricted, ad('sell', ts=NOW), 10000, cfg, NOW) is None


def test_auto_funding_does_not_create_cash(cfg):
    sc.maintain('base', cfg, NOW)
    # First exhaust the T-bank free tier so the VTB scenario becomes cheaper.
    with sc.context('base', cfg, NOW) as data:
        con = pf.connect(sc.path('base'))
        q = bm.quote(con, data['profiles']['accounts'][0], 'sbp', 'out', 100000, NOW)
        bm.payment(con, q, NOW, None)
        con.commit()
        con.close()
    b, s = ad('buy', ts=NOW), ad('sell', ts=NOW)
    estimate = sc.prepare('base', b, s, 10000, cfg, NOW)
    assert estimate['account'] != 'tbank-black'
    assert sc.prepare('base', b, s, 10000, cfg, NOW, fund=True) is None
    assert Decimal(pf.summary(sc.path('base'))['bank_in_transit']) == 10000
    sc.maintain('base', cfg, NOW + 60)
    assert sc.prepare('base', b, s, 10000, cfg, NOW + 60, fund=True)
    assert Decimal(pf.summary(sc.path('base'))['cash']) == 49901


def test_card_and_requisites_are_explicit_channels(cfg):
    sc.maintain('base', cfg, NOW)
    with sc.context('base', cfg, NOW) as data:
        psb = next(p for p in data['profiles']['accounts'] if p['bank'] == 'PSB')
        con = pf.connect(sc.path('base'))
        assert bm.quote(con, psb, 'card_number', 'out', 50000, NOW)['fee'] == '995.00'
        assert bm.quote(con, psb, 'requisites', 'out', 50000, NOW)['fee'] == '300.00'
        assert bm.compatible(psb, ['PSB']) == 'intra'
        assert bm.compatible(psb, ['Card number']) == 'card_number'
        assert bm.compatible(psb, ['по реквизитам']) == 'requisites'
        assert bm.compatible(psb, ['VTB']) is None
        con.close()


def test_requisites_waits_over_public_holiday(cfg):
    sc.maintain('base', cfg, NOW)
    at = dt.datetime(2026, 11, 3, 12, tzinfo=bm.MSK).timestamp()
    with sc.context('base', cfg, at) as data:
        profile = data['profiles']['accounts'][0]
        offer = dataclasses.replace(ad('buy', ts=at), pays=['по реквизитам'])
        assert sc.review_offer(profile, offer, at)['payment_seconds'] == 2 * 86400


def test_generic_no_third_party_rule_matches_own_funds_assumption(cfg):
    sc.maintain('base', cfg, NOW)
    with sc.context('base', cfg, NOW) as data:
        offer = dataclasses.replace(ad('buy', ts=NOW), terms='Переводы от третьих лиц запрещены')
        assert sc.review_offer(data['profiles']['accounts'][0], offer, NOW)['evidence'] == 'scenario_assumption'


def test_service_funding_from_other_account_and_shortfall(cfg):
    sc.maintain('base', cfg, NOW)
    with sc.context('base', cfg, NOW):
        pf.own_transfer('tbank-black', 'vtb-debit', 49901, 60, path=sc.path('base'), now=NOW)
    sc.maintain('base', cfg, NOW + 60)
    next_month = dt.datetime(2026, 11, 8, 12, tzinfo=bm.MSK).timestamp()
    # Tariff freshness deliberately extended in saved fixture, not production.
    con = pf.connect(sc.path('base'))
    meta = json.loads(con.execute('SELECT data FROM scenario_meta').fetchone()[0])
    for p in meta['profiles']['accounts']:
        p['product_terms']['valid_until'] = '2026-12-01'
    con.execute('UPDATE scenario_meta SET data=?', (json.dumps(meta),))
    con.commit()
    con.close()
    sc.maintain('base', cfg, next_month)
    assert Decimal(pf.summary(sc.path('base'))['bank_in_transit']) == 99
    assert sc.prepare('base', ad('buy', ts=next_month), ad('sell', ts=next_month),
                      1000, cfg, next_month) is None
    sc.maintain('base', cfg, next_month + 60)
    assert Decimal(pf.summary(sc.path('base'))['bank_expenses']) == 198
    assert Decimal(pf.replay(sc.path('base'))['cash']) == 49802


def test_tariff_snapshot_does_not_follow_catalog_edits(cfg, monkeypatch):
    import copy
    sc.maintain('base', cfg, NOW)
    products = copy.deepcopy(sc.bankcatalog.PRODUCTS)
    products[0]['free_sbp'] = '0'
    monkeypatch.setattr(sc.bankcatalog, 'PRODUCTS', products)
    with sc.context('base', cfg, NOW) as data:
        con = pf.connect(sc.path('base'))
        assert bm.quote(con, data['profiles']['accounts'][0], 'sbp', 'out', 50000, NOW)['fee'] == '0.00'
        con.close()


def test_market_planner_excludes_consumed_depth(cfg):
    start(cfg)
    to_sell(cfg)
    buy = ad('buy', ts=NOW + 1500)
    sn = p2p.Snapshot(100, 'fixture', {}, {}, [], {}, {}, {},
                      groups={('Bybit', 'buy', 'USDT'): [buy]}, ts=NOW + 1500)
    planned = sc.market_snapshot('base', sn, cfg, NOW + 1500)
    assert planned.groups[('Bybit', 'buy', 'USDT')][0].avail == 900
    assert buy.avail == 1000  # execution still receives original observed amount


def test_spot_depth_shortfall_then_conversion_preserves_cost(cfg):
    from test_spotbook import book
    sc.maintain('base', cfg, NOW)
    b, s = ad('buy', ts=NOW), ad('sell', 2500, asset='ETH', ts=NOW)
    hops = {'hops': [{'frm': 'Bybit', 'to': 'Bybit', 'asset': 'USDT', 'fee': 0},
                     {'frm': 'Bybit', 'to': 'Bybit', 'asset': 'ETH', 'fee': 0}]}
    with sc.context('base', cfg, NOW):
        assert pf.start(10000, b, s, hops, 10, path=sc.path('base'), now=NOW)
    tick(cfg, NOW + 300, ad('buy', ts=NOW + 300))
    tick(cfg, NOW + 1200)
    tick(cfg, NOW + 1201)
    tick(cfg, NOW + 1202)
    at = NOW + 1203
    with sc.context('base', cfg, at):
        pf.tick(snapshot(now=at), cfg, path=sc.path('base'), now=at,
                books={('Bybit', 'ETH'): book(now=at, asks=[['20', '2']])})
    run = pf.runs(sc.path('base'))[0]
    assert run['asset'] == 'USDT' and Decimal(run['qty']) == 100
    at += 1
    with sc.context('base', cfg, at):
        pf.tick(snapshot(now=at), cfg, path=sc.path('base'), now=at,
                books={('Bybit', 'ETH'): book(now=at)})
    run = pf.runs(sc.path('base'))[0]
    assert run['asset'] == 'ETH' and Decimal(run['qty']) < 5  # spot fee paid in received asset
    assert Decimal(run['cost']) == 10000
    assert Decimal(pf.replay(sc.path('base'))['cash']) == Decimal(pf.summary(sc.path('base'))['cash'])


def test_telegram_default_comparison_export_and_signal_wiring(cfg, monkeypatch):
    from helpers import arun
    from test_bot import Stub, texts
    import bot as B
    monkeypatch.setenv('PAPER_SCENARIOS', '1')
    monkeypatch.setenv('PAPER_ENGINE', 'ledger')
    monkeypatch.setenv('PAPER', '1')
    monkeypatch.setenv('PAPER_AMOUNT', '50000')
    monkeypatch.setattr(sc.time, 'time', lambda: NOW)
    bot = Stub(cfg)
    monkeypatch.setattr(bot, 'is_confirmed', lambda _: True)
    monkeypatch.setattr(B, 'reliability', lambda *args: (p2p.RELIABLE, []))
    b, s = ad('buy', ts=NOW), ad('sell', 110, ts=NOW)
    d = (10, b, s, 'within venue')
    sn = p2p.Snapshot(100, 'fixture', {'USDT': 100}, {}, [d], {}, {}, {},
                     groups={('Bybit', 'buy', 'USDT'): [b], ('Bybit', 'sell', 'USDT'): [s]}, ts=NOW)
    arun(bot.maybe_start_paper_cycle([d], sn))
    for name in sc.VARIANTS:
        runs = pf.runs(sc.path(name))
        assert len(runs) == 1 and Decimal(runs[0]['reserved']) == 49901
    arun(bot.cmd_paper(''))
    assert 'Базовый сценарий' in texts(bot)[-1]
    arun(bot.cmd_paper('scenarios'))
    assert 'Стрессовый сценарий' in texts(bot)[-1]
    arun(bot.cmd_paper('scenario-report stress'))
    exported = [params['path'] for method, params in bot.out if method == 'sendDocument'][-1]
    assert 'paper_scenario_stress.csv' in exported
    with open(exported, encoding='utf-8-sig') as f:
        raw = f.read()
    assert 'scenario_started' in raw and 'service_fee' in raw and 'reserve' in raw
