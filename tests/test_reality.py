import copy
import csv
import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import p2p
import reality as R
import settlement as E
import shorts as S
from test_shorts import market, open_position, position, NOW

T = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc).timestamp()
COSTS = {'wallet_in_usdt':'0', 'wallet_out_usdt':'0', 'incoming_rub':'0',
         'source':'controlled fixture verified costs', 'checked':T, 'valid_until':T+60*86400}


def snapshot(now, buy=100, sell=99, available=1000):
    groups = {}
    for side, price in [('buy', buy), ('sell', sell)]:
        groups[('Bybit', side, 'USDT')] = [p2p.Ad('Bybit', side, price, 100, 100000,
             available, ['SBP'], 'fixture', 100, 99, fetched_ts=now, ad_id=side+'-id')]
    return SimpleNamespace(ts=now, groups=groups)


@pytest.fixture
def ledger(tmp_path):
    dest = str(tmp_path/'roundtrip.db')
    R.initialize(T, dest, costs=COSTS)
    return dest


def advance(dest, now, snap=True):
    R.tick(snapshot(now) if snap else None, {}, now, dest)
    with R.connect(dest) as con:
        R.verify(con, R.load(con))
    return R.status(dest)


def test_full_round_trip_spread_service_and_restore(ledger, tmp_path):
    assert R.status(ledger)['cash_rub'] == '49901'
    assert advance(ledger, T+300)['stage'] == 'release'
    assert advance(ledger, T+1199)['stage'] == 'release'
    assert advance(ledger, T+1200)['stage'] == 'wallet_in'
    s = advance(ledger, T+1260)
    assert s['stage'] == 'trade'
    child = s['strategy']
    assert S.status(child)['initial'] == '499.01000000'
    assert S.status(child)['funding_model'] == 'exchange-wallet-v2'
    assert not R.initialize(T+1300, ledger)
    advance(ledger, T+1260+86400)
    advance(ledger, T+1260+86460)
    s = advance(ledger, T+1260+86760)
    assert s['stage'] == 'receipt' and D(s['cash_rub']) == 0
    assert D(s['pending_rub']) == D('49401.99')
    s = advance(ledger, T+1260+87060)
    assert s['stage'] == 'done' and D(s['realized_rub']) == D('-598.01')
    count = None
    with R.connect(ledger) as con:
        count = con.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    advance(ledger, T+1260+87061)
    with R.connect(ledger) as con:
        assert con.execute('SELECT COUNT(*) FROM events').fetchone()[0] == count
    output = str(tmp_path/'full.csv')
    R.export(output, ledger)
    rows = list(csv.reader(open(output, encoding='utf-8-sig')))
    assert {r[0] for r in rows[1:]} == {'rub', 'usdt'}


@pytest.mark.parametrize('days', [1, 7, 30])
def test_bank_review_never_unblocks_by_timer(ledger, days):
    with R.connect(ledger) as con:
        E.set_state(con, 'tbank-black', 'review_115', {'kind':'stress'}, T, days)
    s = advance(ledger, T+days*86400+1)
    assert s['stage'] == 'buy' and not s['bank_available']
    with R.connect(ledger) as con:
        E.set_state(con, 'tbank-black', 'available', {'kind':'scenario_review_passed'}, T+days*86400+2)
    s = advance(ledger, T+days*86400+3)
    if T+days*86400+3 <= R.VALID_UNTIL:
        assert s['stage'] == 'release'


def test_missing_and_expired_ad_never_creates_crypto(ledger):
    assert advance(ledger, T+300, False)['stage'] == 'buy'
    snap = snapshot(T)
    R.tick(snap, {}, T+301, ledger)
    assert D(R.status(ledger)['usdt']) == 0
    snap = snapshot(T+302)
    snap.groups[('Bybit', 'buy', 'USDT')][0].terms = 'third party payment'
    R.tick(snap, {}, T+302, ledger)
    assert D(R.status(ledger)['usdt']) == 0


def test_partial_sale_requires_new_liquidity(ledger):
    for stamp in (T+300,T+1200,T+1260,T+87660,T+87720):
        advance(ledger, stamp)
    R.tick(snapshot(T+88020, available=100), {}, T+88020, ledger)
    assert D(R.status(ledger)['usdt']) == D('399.01000000')
    advance(ledger, T+88320)
    R.tick(snapshot(T+88620, available=100), {}, T+88620, ledger)
    assert R.status(ledger)['stage'] == 'sell'
    assert 'Продажа невозможна' in R.status(ledger)['last_reason']


def test_child_crash_recovery_does_not_fund_twice(ledger):
    advance(ledger,T+300)
    advance(ledger,T+1200)
    s = R.status(ledger)
    child = ledger.removesuffix('.db')+'_shorts_v2.db'
    S.initialize(100, 'child committed before crash', T+1260, child,
                 capital=s['usdt'], funding_model='exchange-wallet-v2')
    advance(ledger,T+1260)
    with S.connect(child) as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE kind='initialize'").fetchone()[0] == 1


@pytest.mark.parametrize('used,amount,expected', [('99000','2000','5.00'),('100000','50000','250.00'),
                                               ('0','50000','0.00'),('100000','1000000','1500.00')])
def test_sbp_threshold_is_fee_not_turnover_limit(used,amount,expected):
    assert R.bank_fee(D(amount),D(used)) == D(expected)


@pytest.mark.parametrize('level,fee', [('general','.003'),('verified','.00275'),('block','.0025')])
def test_maker_role_fee_distinguished(level,fee):
    assert R.p2p_fee('maker','sell',level,T) == D(fee)
    assert R.p2p_fee('maker','buy',level,T) == 0
    assert R.p2p_fee('taker','sell',level,T) == 0
    with pytest.raises(ValueError):
        R.p2p_fee('taker','sell',level,R.VALID_UNTIL+1)


@pytest.mark.parametrize('change', ['paid_flag','third_party','underpaid','overpaid','disputed','reversed'])
def test_bank_evidence_and_owner_checks(change):
    evidence = {'kind':'bank_credit','payer':'buyer','recipient':'seller','amount':'1000','reference':'id'}
    assert E.check_payment(evidence,1000,'buyer','seller') is None
    if change == 'paid_flag': evidence['kind']='order_paid'
    if change == 'third_party': evidence['payer']='stranger'
    if change == 'underpaid': evidence['amount']='999'
    if change == 'overpaid': evidence['amount']='1001'
    if change in ('disputed','reversed'): evidence[change]=True
    assert E.check_payment(evidence,1000,'buyer','seller')


def test_wallet_funding_debits_cash_once_and_preserves_equation(tmp_path):
    dest = str(tmp_path/'wallet.db')
    S.initialize(50,'fixture',NOW,dest, funding_model='exchange-wallet-v2')
    open_position(dest)
    before = S.status(dest)
    p = position(dest)
    m = market(NOW+4)
    m['candidate'] = False
    m['funding_history'] = [{'ts':NOW+3,'rate':'-.01','mark':'100'}]
    S.tick({'ALTUSDT':m},NOW+4,dest)
    after = S.status(dest)
    payment = D(p['qty'])*D('-1')
    assert D(after['cash']) == D(before['cash'])+payment
    assert position(dest)['held'] == p['held']
    S.tick({'ALTUSDT':m},NOW+4,dest)
    assert S.status(dest)['cash'] == after['cash']
    with S.connect(dest) as con:
        S.verify(con,S.meta(con))


def test_funding_deficit_is_not_silently_capped(tmp_path):
    dest = str(tmp_path/'wallet.db')
    S.initialize(50,'fixture',NOW,dest,funding_model='exchange-wallet-v2')
    open_position(dest)
    p = position(dest)
    m = market(NOW+4)
    m.update(candidate=False, funding_history=[{'ts':NOW+3,'rate':'-10000','mark':'100'}])
    S.tick({'ALTUSDT':m},NOW+4,dest)
    s = S.status(dest)
    assert D(s['funding']) == D(p['qty'])*D('-1000000')
    assert D(s['funding_debt']) > 0 and s['halt']
    with S.connect(dest) as con:
        S.verify(con,S.meta(con))


def test_reports_and_telegram_commands(tmp_path, monkeypatch):
    import bot
    import executionfacts
    from test_bot import Stub, texts
    from helpers import arun
    monkeypatch.setattr(R,'ROOT',str(tmp_path))
    b = Stub(p2p.Config())
    arun(b.cmd_shorts('cycle'))
    arun(b.cmd_paper('risks'))
    assert all(len(x)<4096 for x in texts(b))
    assert 'Полный рублёвый цикл' in '\n'.join(texts(b))
    assert all(v['personal_kyc']=='unknown' for v in executionfacts.matrix()['venues'])


def test_p2p_receipt_is_not_available_or_realized_until_settlement(tmp_path, monkeypatch):
    import scenarios as sc
    import portfolio as pf
    from test_scenarios import start, to_sell, tick, NOW as PN
    from test_portfolio import ad
    monkeypatch.setattr(sc, 'ROOT', str(tmp_path))
    monkeypatch.setenv('PAPER_REALITY','1')
    monkeypatch.setenv('PAPER_TRANSFER_MINUTES','0')
    cfg = p2p.Config()
    start(cfg)
    sale = to_sell(cfg)
    at = sale+300
    tick(cfg,at,ad('sell',110,ts=at))
    before = pf.summary(sc.path('base'))
    assert before['runs'][0]['stage'] == 'receipt'
    assert D(before['realized']) == 0 and D(before['pending_receipts']) == 10000
    account = before['runs'][0]['receipts'][0]['account']
    with pf.connect(sc.path('base')) as con:
        E.set_state(con,account,'review_115',{'kind':'stress'},at,1)
    tick(cfg,at+300)
    assert D(pf.summary(sc.path('base'))['realized']) == 0
    with pf.connect(sc.path('base')) as con:
        E.set_state(con,account,'available',{'kind':'scenario_review_passed'},at+301)
    tick(cfg,at+302)
    after = pf.summary(sc.path('base'))
    assert after['runs'][0]['stage'] == 'done'
    assert D(after['realized']) == 1000 and D(after['cash']) == 50901
    tick(cfg,at+303)
    assert pf.summary(sc.path('base'))['cash'] == after['cash']


def test_shared_restriction_and_unavailable_cash(tmp_path,monkeypatch):
    import scenarios as sc
    import portfolio as pf
    monkeypatch.setattr(sc,'ROOT',str(tmp_path))
    sc.maintain('base',p2p.Config(),T)
    with pf.connect(sc.path('base')) as con:
        E.set_state(con,'tbank-black','fraud_161',{'kind':'stress'},T)
    s = pf.summary(sc.path('base'))
    assert D(s['restricted']) == 49901 and D(s['available_cash']) == 0
    with sc.context('base',p2p.Config(),T) as data, pf.connect(sc.path('base')) as con:
        import bankmodel
        with pytest.raises(bankmodel.Blocked,match='ограничен'):
            bankmodel.quote(con,data['profiles']['accounts'][0],'sbp','out',1000,T)


def test_maker_queue_does_not_invent_buyers():
    import makerflow as M
    with pytest.raises(ValueError):
        M.create('x','sell',100,10,T,'unknown')
    order = M.create('x','sell',100,10,T,'scenario_assumed')
    M.observe(order,[],T+60)
    assert order['queue_ahead'] == 0 and order['filled'] == '0'
    with pytest.raises(ValueError):
        M.match(order,'m',1,'competing_ad_disappeared',T+61)
    assert M.match(order,'m',1,'scenario_counterparty_arrival',T+62)
    assert not M.match(order,'m',1,'scenario_counterparty_arrival',T+63)
    with pytest.raises(ValueError):
        M.cancel(order,T+64)


@pytest.mark.parametrize('jump', ['2','4','16'])
def test_rocket_stress_cannot_replenish_from_free_cash(tmp_path,jump):
    dest = str(tmp_path/'wallet.db')
    S.initialize(50,'fixture',NOW,dest,funding_model='exchange-wallet-v2')
    open_position(dest)
    before = S.status(dest)
    m = market(NOW+4)
    mark = str(D(100)*D(jump))
    m.update(candidate=False,mark=mark,index=mark,asks=[[mark,'100']])
    S.tick({'ALTUSDT':m},NOW+4,dest)
    after = S.status(dest)
    assert after['cash'] == before['cash']
    assert position(dest)['stress'] and position(dest)['stage'] == 'closed'
    assert 'не подтверждён' in S.report('results',dest)
    with S.connect(dest) as con:
        S.verify(con,S.meta(con))


def test_restriction_applies_to_all_accounts_in_bank_scope(tmp_path,monkeypatch):
    import scenarios as sc
    import portfolio as pf
    import bankmodel as bm
    monkeypatch.setattr(sc,'ROOT',str(tmp_path))
    with sc.context('base',p2p.Config(),T) as data, pf.connect(sc.path('base')) as con:
        first = data['profiles']['accounts'][0]
        other = copy.deepcopy(first)
        other['id'] = 'extra-card'
        data['profiles']['accounts'].append(other)
        bm.initialize(con,data['profiles'],T)
        E.set_state(con,first['scope'],'fraud_161',{'kind':'stress'},T)
        assert not E.available(con,first['id']) and not E.available(con,other['id'])
        with pytest.raises(bm.Blocked):
            bm.quote(con,other,'sbp','out',1000,T)


def test_preparation_preserves_old_ledgers_and_settings(tmp_path):
    from scripts.prepare_reality import prepare_model,digest
    data = tmp_path/'data'
    data.mkdir()
    old = data/'old.db'
    with sqlite3.connect(old) as con:
        con.execute('CREATE TABLE original (value TEXT)')
        con.execute("INSERT INTO original VALUES ('50000')")
    before = digest(old)
    (tmp_path/'.env').write_text('TG_TOKEN=fixture-private-token\nALT_SHORTS=1\nTRADING=0\nPAYOUTS=0\n',encoding='utf-8')
    old_env = (tmp_path/'.env').read_bytes()
    result = prepare_model(tmp_path,T)
    assert digest(old) == before
    backup = __import__('pathlib').Path(result['backup'])
    assert digest(backup/'old.db') == before
    assert 'TG_TOKEN=fixture-private-token' in (tmp_path/'.env').read_text(encoding='utf-8')
    new = R.status(str(data/'rub_roundtrips.db'))
    assert new['cash_rub'] == '49901' and new['service'] == '99'
    assert (tmp_path/'.env').read_bytes() == old_env
    again = prepare_model(tmp_path,T+1)
    assert R.status(str(data/'rub_roundtrips.db'))['service'] == '99'
    assert again['mode'] == 'independent_virtual_cycle_prepared_not_enabled'


def test_preparation_never_changes_existing_switches(tmp_path):
    from scripts.prepare_reality import prepare_model
    (tmp_path/'.env').write_text('TRADING=1\n',encoding='utf-8')
    original = (tmp_path/'.env').read_bytes()
    prepare_model(tmp_path,T)
    assert (tmp_path/'.env').read_bytes() == original


def test_unknown_costs_are_not_zero(tmp_path):
    dest = str(tmp_path/'unknown.db')
    R.initialize(T,dest)
    advance(dest,T+300)
    s = R.status(dest)
    assert s['stage'] == 'buy' and s['cash_rub'] == '49901'
    assert 'неизвестные расходы' in s['last_reason']
    with pytest.raises(ValueError):
        R.configure_costs(dict(COSTS,wallet_in_usdt=None),dest,T)
    R.configure_costs(COSTS,dest,T)
    assert advance(dest,T+301)['stage'] == 'release'


def test_nonzero_transfer_and_receipt_costs_reduce_final_cash(ledger):
    R.configure_costs(dict(COSTS,wallet_in_usdt='1',wallet_out_usdt='2',incoming_rub='3'),ledger,T)
    for stamp in (T+300,T+1200,T+1260,T+87660,T+87720,T+88020,T+88320):
        advance(ledger,stamp)
    s = R.status(ledger)
    assert s['stage'] == 'done'
    assert D(s['realized_rub']) == D('-898.01')
    assert D(s['internal_fees_usdt']) == 3 and D(s['bank_fees']) == 3


def test_frozen_exchange_cannot_return_funds(ledger):
    for stamp in (T+300,T+1200,T+1260):
        advance(ledger,stamp)
    with R.connect(ledger) as con:
        E.set_state(con,'Bybit:UTA','funds_restricted',{'kind':'stress'},T+1261)
    s = advance(ledger,T+87660)
    assert s['stage'] == 'trade' and not s['venue_available']
    assert s['pending_rub'] == '0'
