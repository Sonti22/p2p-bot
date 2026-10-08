import copy
import json
from decimal import Decimal

import pytest

import execution_review as er
import bankmodel as bm
import portfolio as pf
import p2p
from test_portfolio import ad, snapshot


def profile(account='T-Bank', bank='T-Bank'):
    periods = ('day', 'month', 'billing')
    rule = {'confirmed': True, 'per_operation': '1000000',
            'limits': {p: {'amount': '5000000', 'count': 100} for p in periods},
            'baseline': {p: {'period_start': bm.period_start(1000, p, 1), 'amount': '0', 'count': 0}
                         for p in periods},
            'fee': {'period': 'month', 'free': '100000', 'percent': '0.5', 'minimum': '0',
                    'maximum': '1500', 'fixed': '0'}}
    incoming = copy.deepcopy(rule)
    incoming['fee']['percent'] = '0'
    return {'id': account, 'bank': bank, 'scope': bank + ':owner', 'tariff': 'Test fixture',
            'revision': '1', 'status': 'active', 'checked_at': 1, 'valid_until': 100000,
            'tariff_confirmed': True, 'limits_confirmed': True, 'shared_limits_confirmed': True, 'ownership_confirmed': True,
            'p2p_conditions_confirmed': True, 'billing_day': 1, 'sources': ['offline fixture'],
            'methods': {'sbp': {'out': rule, 'in': incoming},
                        'self_sbp': {'out': copy.deepcopy(rule), 'in': copy.deepcopy(incoming)}},
            'aggregate': {'out': copy.deepcopy(rule), 'in': copy.deepcopy(incoming)},
            'offer_reviews': {er.key(ad(side)): {'terms_hash': er.terms_hash(ad(side)),
                              'eligible': True, 'identity_match': True, 'no_third_party': True,
                              'p2p_fee_confirmed': True, 'p2p_fee': '0', 'checked_at': 1,
                              'valid_until': 100000, 'payment_seconds': 0, 'release_seconds': 0}
                              for side in ('buy', 'sell')},
            'venues': {'Bybit': True}, 'service': {'confirmed': True, 'charges': []}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('PAPER_BANK_MODEL', 'strict')
    monkeypatch.setenv('PAPER_PAY_MINUTES', '0')
    monkeypatch.setenv('PAPER_TRANSFER_MINUTES', '0')
    p = profile()
    data = {'version': 1, 'start_account': p['id'], 'accounts': [p]}
    source = tmp_path / 'profiles.json'
    def save():
        source.write_text(json.dumps(data), encoding='utf-8')
    save()
    monkeypatch.setenv('PAPER_BANK_PROFILES', str(source))
    path = str(tmp_path / 'portfolio.db')
    con = pf.connect(path)
    con.execute("INSERT INTO wallet VALUES (1,'50000.00','50000.00')")
    bm.initialize(con, data, 1000)
    con.commit()
    con.close()
    return path, data, save


def start(path, amount=10000):
    hops = {'hops': [{'frm': 'Bybit', 'to': 'Bybit', 'asset': 'USDT', 'fee': 0}]}
    return pf.start(amount, ad('buy'), ad('sell', 110), hops, 10, path=path, now=1000)


def cycle(path):
    cfg = p2p.Config()
    pf.tick(snapshot(ad('buy')), cfg, path, now=1000)
    for t in (1001, 1002, 1003):
        pf.tick(snapshot(now=t), cfg, path, now=t)
    pf.tick(snapshot(ad('sell', 110, ts=1004), now=1004), cfg, path, now=1004)


def test_fee_only_on_excess_and_cap(setup):
    path, data, _ = setup
    p = data['accounts'][0]
    p['methods']['sbp']['out']['baseline']['month']['amount'] = '98000'
    con = pf.connect(path)
    assert bm.quote(con, p, 'sbp', 'out', 10000, 1000)['fee'] == '40.00'
    p['methods']['sbp']['out']['per_operation'] = '5000000'
    assert bm.quote(con, p, 'sbp', 'out', 1000000, 1000)['fee'] == '1500.00'
    con.close()


@pytest.mark.parametrize('field', ['tariff_confirmed', 'limits_confirmed', 'ownership_confirmed',
                                   'p2p_conditions_confirmed'])
def test_unknown_blocks_and_preserves_money(setup, field):
    path, data, save = setup
    data['accounts'][0][field] = False
    save()
    assert start(path) is None
    assert pf.summary(path)['cash'] == '50000.00'
    assert not pf.runs(path)


def test_pending_template_never_enables_any_bank():
    data = bm.template()
    assert data['start_account'] == 'T-Bank'
    assert all(not p['tariff_confirmed'] and p['status'] == 'pending' for p in data['accounts'])


def test_complete_cycle_account_reconciliation_and_replay(setup):
    path, _, _ = setup
    assert start(path)
    cycle(path)
    s = pf.summary(path)
    assert s['cash'] == '51000.00'
    assert s['realized'] == '1000.00'
    assert Decimal(pf.replay(path)['bank_accounts']['T-Bank']) == Decimal(s['cash'])
    con = pf.connect(path)
    assert con.execute('SELECT count(*) FROM bank_payments').fetchone()[0] == 2
    assert not con.execute('SELECT 1 FROM bank_reservations').fetchone()
    bm.reconcile(con)
    con.close()


def test_fee_in_budget_and_cost_basis(setup):
    path, data, save = setup
    data['accounts'][0]['methods']['sbp']['out']['baseline']['month']['amount'] = '98000'
    save()
    assert start(path)
    cycle(path)
    r = pf.runs(path)[0]
    assert Decimal(r['spent']) <= 10000
    assert Decimal(r['realized']) < 1000
    assert Decimal(pf.replay(path)['cash']) == Decimal(pf.summary(path)['cash'])


def test_50000_budget_never_overdrawn(setup):
    path, data, save = setup
    data['accounts'][0]['methods']['sbp']['out']['baseline']['month']['amount'] = '100000'
    save()
    assert start(path, 50000)
    pf.tick(snapshot(ad('buy')), p2p.Config(), path, now=1000)
    assert Decimal(pf.summary(path)['cash']) >= 0
    assert Decimal(pf.runs(path)[0]['spent']) <= 50000


def test_concurrent_reservations_respect_scope_limits(setup):
    path, data, save = setup
    p = data['accounts'][0]
    p['methods']['sbp']['out']['limits']['day']['amount'] = '15000'
    save()
    assert start(path)
    hops = {'hops': [{'frm': 'Bybit', 'to': 'Bybit', 'asset': 'USDT', 'fee': 0}]}
    assert pf.start(10000, ad('buy'), ad('sell'), hops, 1, path=path, now=1000, max_open=2) is None
    assert pf.summary(path)['cash'] == '40000.00'


def test_shared_scope_rejects_conflicting_card_rules(setup):
    _, data, save = setup
    second = copy.deepcopy(data['accounts'][0])
    second['id'] = 'second-card'
    second['methods']['sbp']['out']['limits']['day']['amount'] = '9999999'
    data['accounts'].append(second)
    save()
    with pytest.raises(bm.Blocked):
        bm.load()


def test_unconfirmed_receipt_keeps_coins(setup):
    path, data, save = setup
    start(path)
    pf.tick(snapshot(ad('buy')), p2p.Config(), path, now=1000)
    data['accounts'][0]['methods']['sbp']['in']['confirmed'] = False
    save()
    for t in (1001, 1002, 1003, 1004):
        pf.tick(snapshot(ad('sell', 110, ts=t), now=t), p2p.Config(), path, now=t)
    r = pf.runs(path)[0]
    assert Decimal(r['qty']) > 0
    assert r['realized'] == '0'
    assert pf.summary(path)['equity'] is None


def test_venue_identity_confirmation_required(setup):
    path, data, save = setup
    data['accounts'][0]['venues'] = {}
    save()
    assert start(path) is None


def test_no_implicit_sbp_for_foreign_bank():
    assert bm.compatible(profile(), ['Sberbank']) is None


def test_changed_profile_cancels_unpaid_purchase(setup):
    path, data, save = setup
    start(path)
    data['accounts'][0]['status'] = 'blocked'
    save()
    pf.tick(snapshot(ad('buy')), p2p.Config(), path, now=1000)
    assert pf.runs(path)[0]['stage'] == 'cancelled'
    assert pf.summary(path)['cash'] == '50000.00'


def test_self_transfer_pending_settlement_and_restart(setup):
    path, data, save = setup
    data['accounts'].append(profile('Sberbank', 'Sberbank'))
    save()
    con = pf.connect(path)
    con.execute("INSERT INTO bank_accounts VALUES ('Sberbank','0')")
    con.commit()
    con.close()
    pf.own_transfer('T-Bank', 'Sberbank', 10000, 60, path, now=1000)
    assert pf.summary(path)['cash'] == '40000.00'
    assert pf.summary(path)['bank_in_transit'] == '10000.00'
    assert Decimal(pf.replay(path)['cash']) == 40000
    for t in (1030, 1060, 1061):
        pf.tick(snapshot(now=t), p2p.Config(), path, now=t)
    assert Decimal(pf.summary(path)['cash']) == 50000
    con = pf.connect(path)
    assert Decimal(con.execute("SELECT cash FROM bank_accounts WHERE id='Sberbank'").fetchone()[0]) == 10000
    assert con.execute("SELECT count(*) FROM bank_payments WHERE direction='in'").fetchone()[0] == 1
    con.close()


def test_service_expense_once_and_replay(setup):
    path, data, save = setup
    data['accounts'][0]['service']['charges'] = [{'period': 1000, 'amount': '99', 'confirmed': True}]
    save()
    for t in (1000, 1001):
        pf.tick(snapshot(now=t), p2p.Config(), path, now=t)
    s = pf.summary(path)
    assert Decimal(s['cash']) == 49901
    assert Decimal(s['bank_expenses']) == 99
    assert Decimal(s['net_realized']) == -99
    assert Decimal(pf.replay(path)['cash']) == 49901


def test_old_history_preserved_during_migration(tmp_path, monkeypatch):
    monkeypatch.setenv('PAPER_BANK_MODEL', 'legacy')
    path = str(tmp_path / 'p.db')
    start(path)
    before = pf.runs(path)
    con = pf.connect(path)
    bm.initialize(con, {'accounts': [profile()], 'start_account': 'T-Bank'}, 1001)
    con.commit()
    bm.reconcile(con)
    con.close()
    assert pf.runs(path) == before
    assert pf.summary(path)['cash'] == '40000.00'


def test_unknown_period_baseline_blocks(setup):
    path, data, save = setup
    data['accounts'][0]['methods']['sbp']['out']['baseline']['month']['period_start'] = 123
    save()
    assert start(path) is None


def test_month_boundary_msk():
    import datetime as dt
    now = dt.datetime(2026, 10, 1, 0, 0, tzinfo=bm.MSK).timestamp()
    assert bm.period_start(now, 'month') == now
    assert bm.period_start(now - 1, 'month') < now
    assert bm.period_start(now, 'billing', 31) < now


def test_valuation_does_not_spend_real_limit(setup):
    path, _, _ = setup
    start(path)
    pf.tick(snapshot(ad('buy')), p2p.Config(), path, now=1000)
    pf.tick(snapshot(ad('sell', 110, ts=1001), now=1001), p2p.Config(), path, now=1001)
    con = pf.connect(path)
    assert con.execute("SELECT count(*) FROM bank_payments WHERE direction='in'").fetchone()[0] == 0
    con.close()


def test_reset_archives_bank_state(setup):
    path, _, _ = setup
    start(path)
    archive = pf.reset(path)
    con = pf.connect(path)
    assert not con.execute('SELECT 1 FROM bank_accounts').fetchone()
    con.close()
    con = pf.connect(archive)
    assert con.execute('SELECT 1 FROM bank_accounts').fetchone()
    con.close()


def test_escrow_delay_prevents_transfer(setup):
    path, data, save = setup
    data['accounts'][0]['offer_reviews'][er.key(ad('buy'))]['release_seconds'] = 60
    save()
    start(path)
    pf.tick(snapshot(ad('buy')), p2p.Config(), path, now=1000)
    assert pf.runs(path)[0]['stage'] == 'release'
    assert pf.runs(path)[0]['in_transit'] is True
    pf.tick(snapshot(now=1030), p2p.Config(), path, now=1030)
    assert pf.runs(path)[0]['stage'] == 'release'
    pf.tick(snapshot(now=1060), p2p.Config(), path, now=1060)
    assert pf.runs(path)[0]['stage'] == 'route'


@pytest.mark.parametrize('change', ['unknown', 'third_party', 'changed_terms', 'fee'])
def test_offer_requirements_fail_closed(setup, change):
    path, data, save = setup
    reviews = data['accounts'][0]['offer_reviews']
    review = reviews[er.key(ad('buy'))]
    if change == 'unknown':
        reviews.clear()
    elif change == 'third_party':
        review['no_third_party'] = False
    elif change == 'changed_terms':
        review['terms_hash'] = 'different'
    else:
        review['p2p_fee'] = '0.1'
    save()
    assert start(path) is None
    assert pf.summary(path)['cash'] == '50000.00'
    assert not pf.runs(path)


def test_pending_reservation_does_not_charge_fee_prematurely(setup):
    path, data, _ = setup
    p = data['accounts'][0]
    p['methods']['sbp']['out']['baseline']['month']['amount'] = '95000'
    con = pf.connect(path)
    bm.reserve(con, 999, p, 'sbp', 10000, 1000)
    assert bm.quote(con, p, 'sbp', 'out', 5000, 1000, run_id=1)['fee'] == '0.00'
    con.close()


def test_shared_limit_across_payment_methods(setup):
    path, data, _ = setup
    p = data['accounts'][0]
    p['aggregate']['out']['limits']['day']['amount'] = '15000'
    con = pf.connect(path)
    bm.payment(con, bm.quote(con, p, 'self_sbp', 'out', 10000, 1000), 1000, None)
    with pytest.raises(bm.Blocked):
        bm.quote(con, p, 'sbp', 'out', 10000, 1000)
    con.close()


def test_incoming_count_limit(setup):
    path, data, _ = setup
    p = data['accounts'][0]
    p['methods']['sbp']['in']['limits']['day']['count'] = 1
    con = pf.connect(path)
    bm.payment(con, bm.quote(con, p, 'sbp', 'in', 10000, 1000), 1000, None)
    with pytest.raises(bm.Blocked):
        bm.quote(con, p, 'sbp', 'in', 10000, 1001)
    con.close()


def test_failed_transfer_rolls_back(setup):
    path, data, save = setup
    data['accounts'].append(profile('Sberbank', 'Sberbank'))
    save()
    with pytest.raises(bm.Blocked):
        pf.own_transfer('T-Bank', 'Sberbank', 60000, 60, path, now=1000)
    assert pf.summary(path)['cash'] == '50000.00'
    assert Decimal(pf.summary(path)['bank_in_transit']) == 0


def test_blocked_target_keeps_transfer_pending(setup):
    path, data, save = setup
    data['accounts'].append(profile('Sberbank', 'Sberbank'))
    save()
    pf.own_transfer('T-Bank', 'Sberbank', 10000, 60, path, now=1000)
    data['accounts'][1]['status'] = 'blocked'
    save()
    pf.tick(snapshot(now=1060), p2p.Config(), path, now=1060)
    assert Decimal(pf.summary(path)['bank_in_transit']) == 10000
    assert Decimal(pf.replay(path)['cash']) == 40000


def test_export_includes_banking_journal(setup, tmp_path):
    path, _, _ = setup
    start(path)
    destination = str(tmp_path / 'all.csv')
    pf.export(destination, path)
    text = open(destination, encoding='utf-8-sig').read()
    assert 'bank:migration' in text and 'bank:cash' in text


def test_added_account_replay_starts_at_zero(setup):
    path, data, _ = setup
    data['accounts'].append(profile('Sberbank', 'Sberbank'))
    con = pf.connect(path)
    bm.initialize(con, data, 1001)
    assert Decimal(bm.replay(con)['Sberbank']) == 0
    bm.reconcile(con)
    con.close()


def test_new_snapshot_does_not_replenish_virtual_offer(setup):
    path, _, _ = setup
    start(path)
    pf.tick(snapshot(ad('buy', avail=30)), p2p.Config(), path, now=1000)
    pf.tick(snapshot(ad('buy', avail=30, ts=1001), now=1001), p2p.Config(), path, now=1001)
    assert Decimal(pf.runs(path)[0]['qty']) == 30
    # A larger observed balance permits only the unconsumed difference.
    pf.tick(snapshot(ad('buy', avail=50, ts=1002), now=1002), p2p.Config(), path, now=1002)
    assert Decimal(pf.runs(path)[0]['qty']) == 50


def test_changed_terms_after_reservation_do_not_spend_money(setup):
    path, _, _ = setup
    start(path)
    a = ad('buy')
    a.terms = 'New KYC requirements'
    pf.tick(snapshot(a), p2p.Config(), path, now=1000)
    assert pf.runs(path)[0]['spent'] == '0'


def test_empty_profile_path_uses_default(monkeypatch, tmp_path):
    monkeypatch.setenv('PAPER_BANK_PROFILES', '')
    p = tmp_path / 'p.json'
    p.write_text(json.dumps(bm.template()), encoding='utf-8')
    monkeypatch.setattr(bm, 'PROFILE_PATH', str(p))
    assert bm.load()['start_account'] == 'T-Bank'


def test_incoming_fee_cannot_hide_loss_before_timeout(setup):
    path, data, save = setup
    data['accounts'][0]['methods']['sbp']['in']['fee']['fixed'] = '1500'
    save()
    start(path)
    cycle(path)
    assert pf.runs(path)[0]['realized'] == '0'
    assert Decimal(pf.runs(path)[0]['qty']) == 100
    pf.tick(snapshot(ad('sell', 110, ts=3000), now=3000), p2p.Config(), path, now=3000)
    assert Decimal(pf.runs(path)[0]['realized']) == -500


def test_nonzero_spot_fee_currency_not_assumed(setup):
    path, _, _ = setup
    hops = {'hops': [{'frm': 'Bybit', 'to': 'Bybit', 'asset': 'USDT', 'fee': 0},
                     {'frm': 'Bybit', 'to': 'Bybit', 'asset': 'ETH', 'fee': 0}]}
    assert pf.start(10000, ad('buy'), ad('sell', asset='ETH'), hops, 10, path=path, now=1000) is None
    assert pf.summary(path)['cash'] == '50000.00'
