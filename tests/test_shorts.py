import copy
import json
from decimal import Decimal

import pytest
import shorts as S
import shortmarket as M
import shortresearch as R
from helpers import arun

NOW = 1791504001


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'shorts.db')
    monkeypatch.setattr(S, 'DB_PATH', path)
    S.initialize(50, 'fixture observed RUB/USDT', NOW, path)
    return path


def market(now=NOW, symbol='ALTUSDT'):
    edge = int(now//900)*900
    bs = [[edge-(35-i)*900, '100', '101', '99', '100', '10'] for i in range(35)]
    bs[30][1:5] = ['100', '105', '99', '104']
    bs[31][1:5] = ['104', '104', '98', '99']
    bs[32][1:5] = ['100', '101', '98', '99']
    bs[33][1:5] = ['100', '101', '98', '99']
    bs[34][1:5] = ['100', '100.1', '98', '99']
    hour = int(now//3600)*3600
    hours = [[hour-(21-i)*3600, '100', '101', '99', '100', '10'] for i in range(21)]
    hours[-1][5] = '40'
    minute = int(now//60)*60
    minutes = [[minute-(3-i)*60, '100', '101', '99', '100', '0'] for i in range(3)]
    inst = {'symbol': symbol, 'status': 'Trading', 'contractType': 'LinearPerpetual',
            'quoteCoin': 'USDT', 'settleCoin': 'USDT', 'baseCoin': 'ALT',
            'launchTime': str(int((now-40*86400)*1000))}
    ticker = {'turnover24h': '20000000', 'price24hPcnt': '.30', 'bid1Price': '100', 'ask1Price': '100.1'}
    return {'symbol': symbol, 'active': True, 'candidate': True, 'growth': '.30',
            'mark': '100', 'index': '100', 'ticker_ts': now, 'book_ts': now,
            'book_id': str(now), 'bids': [['100', '100']], 'asks': [['100.1', '100']],
            'fee': '.0011', 'fee_source': 'fixture verified', 'step': '.001', 'tick': '.01',
            'min_qty': '.001', 'min_notional': '5', 'max_qty': '100',
            'funding_rate': '.0001', 'funding_interval': '480', 'lower_funding': '-.003',
            'next_funding': now+1000, 'funding_complete': True, 'funding_history': [],
            'mark_samples': [[now-61, '100'], [now, '100']], 'minutes': minutes,
            'bars': bs, 'hours': hours,
            'tiers': [{'riskLimitValue': '100000', 'maintenanceMargin': '.005', 'mmDeduction': '0'}],
            'terms': {'instrument': inst, 'ticker': ticker, 'fee_source': 'fixture', 'version': S.VERSION}}


def position(db):
    return S.status(db)['positions'][-1]


def open_position(db, monkeypatch=None, symbol='ALTUSDT', now=NOW):
    m = market(now, symbol)
    S.tick({symbol: m}, now, db)
    assert position(db)['stage'] == 'entry'
    m = market(now+2, symbol)
    S.tick({symbol: m}, now+2, db)
    assert position(db)['stage'] == 'open'
    return m


def assert_capital(db):
    with S.connect(db) as con:
        S.verify(con, S.meta(con))


def test_signal_requires_reversal_and_closed_candles():
    m = market()
    sig, why = S.signal(m, NOW)
    assert sig and not why
    m['bars'][-1][4] = '110'
    assert S.signal(m, NOW)[0] is None
    m = market()
    m['bars'].pop(8)
    assert S.signal(m, NOW)[0] is None
    assert S.signal(market(), NOW+61)[0] is None


@pytest.mark.parametrize('field,value', [('baseCoin','BTC'),('marketRegion','US'),('isPreListing',True),('status','PreLaunch')])
def test_universe_exclusions(field,value):
    m = market()
    inst = m['terms']['instrument']
    assert S.eligible(inst, m['terms']['ticker'], NOW)
    inst[field] = value
    assert not S.eligible(inst, m['terms']['ticker'], NOW)


@pytest.mark.parametrize('change,reason', [('rocket','ускорение'),('basis','расхождение'),('funding','фандинг'),('gap','минутных')])
def test_entry_protection(db, change, reason):
    m = market()
    if change == 'rocket': m['mark_samples'][0][1] = '90'
    if change == 'basis': m['index'] = '90'
    if change == 'funding': m['funding_rate'] = '-.002'
    if change == 'gap': m['minutes'].pop(1)
    assert reason in S.protection(m, NOW)
    S.tick({'ALTUSDT':m}, NOW, db)
    assert not S.status(db)['positions']


def test_reservation_delay_fees_and_target(db):
    S.tick({'ALTUSDT': market()}, NOW, db)
    p = position(db)
    assert Decimal(p['held']) <= 20 and Decimal(p['risk']) <= 5
    S.tick({'ALTUSDT': market()}, NOW+.5, db)
    assert position(db)['stage'] == 'entry'
    S.tick({'ALTUSDT': market(NOW+2)}, NOW+2, db)
    p = position(db)
    assert p['stage'] == 'open'
    fees = S.status(db)['fees']
    S.tick({'ALTUSDT': market(NOW+2)}, NOW+2, db)
    assert S.status(db)['fees'] == fees
    m = market(NOW+4)
    m.update(candidate=False, mark='80', index='80', asks=[['80','100']])
    S.tick({'ALTUSDT':m}, NOW+4, db)
    assert position(db)['stage'] == 'exit'
    m.update(ticker_ts=NOW+6, book_ts=NOW+6, book_id='new')
    S.tick({'ALTUSDT':m}, NOW+6, db)
    assert position(db)['stage'] == 'closed' and Decimal(position(db)['pnl']) > 0
    assert_capital(db)


@pytest.mark.parametrize('multiplier', [2,4,16])
@pytest.mark.parametrize('style', ['instant','steps','gradual'])
def test_rockets_never_replenish_from_cash(db, multiplier, style):
    open_position(db)
    state = S.status(db)
    safe_cash = Decimal(state['cash'])
    steps = [multiplier] if style == 'instant' else ([1.01,1.1,multiplier] if style == 'steps' else [1.01,1.02,1.03,1.04,1.1,1.3,multiplier])
    for i, value in enumerate(steps):
        now = NOW+5+i*2
        m = market(now)
        m.update(candidate=False, mark=str(100*value), index=str(100*value), asks=[])  # stop cannot execute
        S.tick({'ALTUSDT':m}, now, db)
    assert position(db)['stage'] == 'closed' and position(db)['stress']
    assert Decimal(S.status(db)['cash']) == safe_cash
    result = S.status(db)['realized']
    S.tick({'ALTUSDT':m}, now, db)
    assert S.status(db)['realized'] == result
    assert abs(Decimal(result)) <= 20
    assert_capital(db)


def test_partial_entry_exit_and_book_reuse(db):
    S.tick({'ALTUSDT':market()}, NOW, db)
    m = market(NOW+2)
    m['bids'] = [['100','.1']]
    S.tick({'ALTUSDT':m}, NOW+2, db)
    assert Decimal(position(db)['qty']) == Decimal('.1')
    m = market(NOW+4)
    m.update(candidate=False, mark='80', index='80', asks=[['80','.04']])
    S.tick({'ALTUSDT':m}, NOW+4, db)
    m.update(ticker_ts=NOW+6,book_ts=NOW+6,book_id='exit1')
    S.tick({'ALTUSDT':m}, NOW+6, db)
    assert Decimal(position(db)['qty']) == Decimal('.06')
    S.tick({'ALTUSDT':m}, NOW+7, db)
    assert Decimal(position(db)['qty']) == Decimal('.06')
    m.update(ticker_ts=NOW+8,book_ts=NOW+8,book_id='exit2',asks=[['80','1']])
    S.tick({'ALTUSDT':m}, NOW+8, db)
    assert position(db)['stage'] == 'closed'
    assert_capital(db)


def test_funding_idempotent_and_unknown_not_zero(db):
    open_position(db)
    m = market(NOW+5)
    m.update(candidate=False,funding_history=[{'ts':NOW+4,'rate':'-.001','mark':'100'}])
    S.tick({'ALTUSDT':m}, NOW+5, db)
    funding = S.status(db)['funding']
    assert Decimal(funding) < 0
    S.tick({'ALTUSDT':m}, NOW+5, db)
    assert S.status(db)['funding'] == funding
    m.update(ticker_ts=NOW+7,book_ts=NOW+7,book_id='f2',funding_complete=False)
    S.tick({'ALTUSDT':m}, NOW+7, db)
    assert position(db)['funding_unverified']
    assert 'неподтверждённые' in position(db)['liquidation']
    assert_capital(db)


def test_pause_missing_data_restart_and_minimum(db):
    S.control('pause', db)
    S.tick({'ALTUSDT':market()}, NOW, db)
    assert not S.status(db)['positions']
    S.control('resume', db)
    m = market()
    m['min_notional'] = '1000'
    S.tick({'ALTUSDT':m}, NOW, db)
    assert not S.status(db)['positions']
    open_position(db)
    S.control('pause', db)
    S.tick({}, NOW+3, db)
    assert position(db)['gap']
    before = S.status(db)
    assert not S.initialize(1,'other',NOW+4,db)
    assert S.status(db)['initial'] == before['initial']
    assert_capital(db)


def test_two_positions_and_liquidation_unknown(db):
    open_position(db)
    second = market(NOW+4,'OTHERUSDT')
    S.tick({'ALTUSDT':market(NOW+4),'OTHERUSDT':second}, NOW+4, db)
    S.tick({'ALTUSDT':market(NOW+6),'OTHERUSDT':market(NOW+6,'OTHERUSDT')}, NOW+6, db)
    state = S.status(db)
    assert len(state['positions']) == 2
    assert sum(Decimal(p['allocated']) for p in state['positions']) <= Decimal('40')
    m = market(NOW+8)
    m.update(candidate=False,tiers=[],mark='106',index='106')
    S.tick({'ALTUSDT':m,'OTHERUSDT':market(NOW+8,'OTHERUSDT')}, NOW+8, db)
    assert 'неподтверждённые' in S.status(db)['positions'][0]['liquidation']
    assert_capital(db)


def test_catalog_pagination_and_fail_closed(monkeypatch):
    calls = []
    async def fake(session,endpoint,**params):
        calls.append(params.get('cursor'))
        return ({'list':[{'symbol':'ALTUSDT'}], 'nextPageCursor':'next'} if len(calls)==1 else {'list':[{'symbol':'OTHERUSDT'}]}, NOW)
    monkeypatch.setattr(M,'get',fake)
    catalog, ts = arun(M.catalog(None))
    assert set(catalog) == {'ALTUSDT','OTHERUSDT'} and calls == ['', 'next']


def test_commands_guest_gate_export_and_research(db, monkeypatch,tmp_path):
    from test_bot import Stub, texts
    from test_guests import Stub as Guest
    import p2p
    b = Stub(p2p.Config())
    arun(b.cmd_shorts('balance'))
    assert 'Изолированная маржа' in texts(b)[-1]
    arun(b.cmd_shorts('export'))
    assert any(method=='sendDocument' for method,_ in b.out)
    assert not R.evaluate(db)['ready']
    g = Guest(p2p.Config(),guests=['42'])
    for data in ('shorts:resume','shorts:positions','shorts:export'):
        g.out.clear()
        arun(g.on_update({'callback_query':{'id':'1','data':data,'message':{'chat':{'id':42},'message_id':3}}}))
        assert [method for method,_ in g.out] == ['answerCallbackQuery']


def test_book_and_snapshot_accounting_rollback(db):
    open_position(db)
    m = market(NOW+4)
    m['mark'] = 'NaN'
    before = S.status(db)
    with pytest.raises(ValueError):
        S.tick({'ALTUSDT':m},NOW+4,db)
    assert S.status(db)==before


def test_breakeven_hold_limit_delisting_and_stale_book(db):
    open_position(db)
    p = position(db)
    m = market(NOW+4)
    value = str(Decimal(p['entry'])-Decimal(p['r'])*Decimal('1.1'))
    m.update(candidate=False,mark=value,index=value)
    S.tick({'ALTUSDT':m},NOW+4,db)
    assert position(db)['breakeven'] and Decimal(position(db)['stop']) < Decimal(p['entry'])
    m = market(NOW+6)
    m.update(candidate=False,active=False,book_ts=NOW)
    S.tick({'ALTUSDT':m},NOW+6,db)
    assert position(db)['stage']=='exit' and 'делистинг' in position(db)['exit_reason']
    S.tick({'ALTUSDT':m},NOW+8,db)
    assert position(db)['stage']=='exit'
    assert_capital(db)


def test_daily_month_boundary_and_manual_halt(db):
    with S.connect(db,True) as con:
        s=S.meta(con)
        s['equity']='970'
        s['day_start']='1000'
        s['cash']='970'
        s['realized']='-30'
        S.save_meta(con,s)
    S.tick({'ALTUSDT':market()},NOW,db)
    assert not S.status(db)['positions'] and S.status(db)['daily_blocked']
    tomorrow=(int(NOW//86400)+1)*86400+1
    S.tick({},tomorrow,db)
    assert not S.status(db)['daily_blocked']
    with S.connect(db,True) as con:
        s=S.meta(con);s.update(cash='940',realized='-60')
        S.save_meta(con,s)
    S.tick({},tomorrow+1,db)
    assert S.status(db)['halt']
    S.control('resume',db)
    assert not S.status(db)['halt']
    assert_capital(db)


def test_partial_exit_late_funding_uses_quantity_at_settlement(db):
    open_position(db)
    initial=Decimal(position(db)['qty'])
    m=market(NOW+4)
    m.update(candidate=False,mark='80',index='80',asks=[['80','.02']])
    S.tick({'ALTUSDT':m},NOW+4,db)
    m.update(ticker_ts=NOW+6,book_ts=NOW+6,book_id='exit')
    S.tick({'ALTUSDT':m},NOW+6,db)
    m.update(ticker_ts=NOW+8,book_ts=NOW+8,book_id='fund',asks=[],
             funding_history=[{'ts':NOW+5,'rate':'-.001','mark':'100'}])
    S.tick({'ALTUSDT':m},NOW+8,db)
    assert Decimal(S.status(db)['funding'])==initial*Decimal('-.1')
    assert_capital(db)


def test_conservative_candle_exit():
    assert R.candle_exit(105,90,110,80)=='stop'
    assert R.candle_exit(105,90,100,80)=='target'


def test_collector_rejects_unknown_fee_group_without_fetch(db,monkeypatch):
    c=M.Collector(db)
    c.instruments={'ALTUSDT':market()['terms']['instrument']}
    c.groups={'ALTUSDT':'Innovation-Zone'}
    with pytest.raises(ValueError,match='группа комиссии'):
        arun(c.market(None,'ALTUSDT',True,None))


def test_snapshot_research_is_dated_and_not_optimistic(db,tmp_path):
    m=market()
    S.record_catalog({'instruments':{'ALTUSDT':m['terms']['instrument']}},NOW-1,db)
    S.tick({'ALTUSDT':m},NOW,db)
    later=market(NOW+120)
    S.tick({'ALTUSDT':later},NOW+120,db)
    result=R.evaluate(db,minimum_days=0)
    assert not result['ready'] and set(result['variants'])=={'strategy','random','flat','no_filters','fees_x2','worse_execution','breakdown','wick_reversal'}
    assert Decimal(result['variants']['flat']['realized'])==0


def test_activation_backups_preserve_existing_journal_and_settings(tmp_path,monkeypatch):
    from scripts import activate_alt_shorts as A
    root=tmp_path/'workspace'
    (root/'data').mkdir(parents=True)
    (root/'.env').write_text('PAPER=1\nPAPER_AMOUNT=50000\nTG_TOKEN=fake-test-token\n',encoding='utf-8')
    with S.connect(str(root/'data'/'prior.db')) as con:
        con.execute("INSERT INTO events(ts,kind,details) VALUES(1,'prior','{}')")
    prior=A.digest(root/'data'/'prior.db')
    monkeypatch.setattr(A,'ROOT',root)
    result=A.activate()
    assert A.digest(root/'data'/'prior.db')==prior
    assert A.digest(__import__('pathlib').Path(result['backup'])/'prior.db')==prior
    env=(root/'.env').read_text(encoding='utf-8')
    assert 'ALT_SHORTS=1' in env and 'TG_TOKEN=fake-test-token' in env and 'PAPER_AMOUNT=50000' in env


def test_unknown_funding_exit_keeps_reserve_until_reconciled(db):
    open_position(db)
    m=market(NOW+4)
    m.update(candidate=False,mark='80',index='80',asks=[['80','100']],funding_complete=False)
    S.tick({'ALTUSDT':m},NOW+4,db)
    m.update(ticker_ts=NOW+6,book_ts=NOW+6,book_id='close')
    S.tick({'ALTUSDT':m},NOW+6,db)
    p=position(db)
    assert p['stage']=='funding' and Decimal(p['held'])>0 and Decimal(p['qty'])==0
    cash=Decimal(S.status(db)['cash'])
    m.update(ticker_ts=NOW+8,book_ts=NOW+8,book_id='reconcile',funding_complete=True,
             funding_history=[{'ts':NOW+5,'rate':'-.001','mark':'100'}])
    S.tick({'ALTUSDT':m},NOW+8,db)
    assert position(db)['stage']=='closed' and not position(db)['funding_unverified']
    assert Decimal(S.status(db)['cash'])>cash
    assert_capital(db)


def test_metadata_outage_does_not_disable_open_position_exit(db,monkeypatch):
    open_position(db)
    c=M.Collector(db)
    c.instruments={'ALTUSDT':market()['terms']['instrument']}
    async def unavailable(*args): raise ValueError('metadata unavailable')
    async def observed(session,sym,candidate,opened):
        m=market(NOW+5)
        m.update(candidate=False,mark='106',index='106',data_errors=['risk unknown'],tiers=[])
        return sym,m
    monkeypatch.setattr(c,'universe',unavailable)
    monkeypatch.setattr(c,'market',observed)
    monkeypatch.setattr(M.time,'time',lambda:NOW+5)
    arun(c.refresh(None))
    assert position(db)['stage']=='exit' and position(db)['gap']
    assert 'Каталог' in S.status(db)['last_error']


def test_seconds_samples_require_new_warmup_after_gap(db):
    c=M.Collector(db)
    c.watched={'ALTUSDT'}
    ticks={'ALTUSDT':{'markPrice':'100'}}
    for at in range(62):
        c.add_samples(ticks,NOW+at,NOW+at+.1)
    m=market(NOW+61)
    m['mark_samples']=c.samples['ALTUSDT']
    assert S.protection(m,NOW+61) is None
    c.add_samples(ticks,NOW+66,NOW+66+.1)
    assert len(c.samples['ALTUSDT'])==1
    m=market(NOW+66)
    m['mark_samples']=c.samples['ALTUSDT']
    assert S.protection(m,NOW+66) is not None


@pytest.mark.parametrize('ts,now',[(NOW,NOW+4),(NOW+2,NOW),(NOW,NOW)])
def test_seconds_samples_reject_stale_future_or_reordered_ticks(db,ts,now):
    c=M.Collector(db)
    c.watched={'ALTUSDT'}
    c.samples={'ALTUSDT':[[NOW,'100']]}
    c.add_samples({'ALTUSDT':{'markPrice':'101'}},ts,now)
    assert len(c.samples.get('ALTUSDT',[]))<=1


def test_seconds_sampler_failure_clears_window_and_close_cancels_task(db,monkeypatch):
    import asyncio
    c=M.Collector(db)
    c.watched={'ALTUSDT'}
    c.samples={'ALTUSDT':[[NOW,'100']]}
    async def fail(*args,**kwargs):
        c.watched.clear()
        raise ValueError('public endpoint unavailable')
    monkeypatch.setattr(M,'get',fail)
    async def run():
        c.sample_task=asyncio.create_task(c.sample_loop(None))
        await asyncio.sleep(.01)
        assert not c.samples
        await c.close()
        assert c.sample_task is None
    arun(run())
