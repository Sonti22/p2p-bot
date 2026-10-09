import copy
from decimal import Decimal

import shorts
import strategyreview as review
from test_shorts import market, NOW


def test_research_signals_use_closed_bars_only():
    m=market()
    original=shorts.signal(m,NOW)
    changed=copy.deepcopy(m)
    changed['bars'].append([NOW,'100','1000','1','1','100000'])
    assert shorts.signal(changed,NOW)==original
    for kind in ('breakdown','wick_reversal'):
        assert shorts.signal(changed,NOW,kind)==shorts.signal(m,NOW,kind)


def test_breakdown_requires_actual_break_of_prior_lows():
    m=market()
    assert shorts.signal(m,NOW,'breakdown')[0] is None
    m['bars'][-1][1:5]=['100','100.1','96','97']
    sig,error=shorts.signal(m,NOW,'breakdown')
    assert sig and error is None and Decimal(sig['stop'])>97


def test_reversal_requires_long_upper_wick():
    m=market()
    assert shorts.signal(m,NOW,'wick_reversal')[0] is None
    m['bars'][-1][1:5]=['99.5','101','98','99']
    sig,error=shorts.signal(m,NOW,'wick_reversal')
    assert sig and error is None


def test_p2p_completion_after_split_is_not_training_evidence():
    def run(i,start,finish,stage='done',pnl='10'):
        return dict(id=i,start=start,stage_ts=finish,stage=stage,realized=pnl,spent='1000',
                    buy_ex='A',sell_ex='B',buy_asset='USDT',sell_asset='USDT',pay_kind='sbp')
    s={'runs':[run(1,0,70),run(2,20,30),run(3,100,101,'release')],
       'net_realized':'19','bank_expenses':'1'}
    data=review.p2p_evidence(s)
    g=data['routes'][0]
    assert data['boundary']==60 and g['training']['completed']==1
    assert g['verification']['completed']==1 and g['open']==1
    assert g['verification']['extra_cost_20bp']=='8.000'
    assert not data['verified_readiness'] and not g['enough_observations']
    assert s['runs'][0]['realized']=='10'


def test_empty_p2p_history_does_not_claim_profitability():
    data=review.p2p_evidence({'runs':[],'net_realized':'-99','bank_expenses':'99'})
    assert data['routes']==[] and not data['verified_readiness']


def test_owner_research_buttons_and_commands(monkeypatch,tmp_path):
    from test_bot import Stub,texts
    from helpers import arun
    import p2p,shortresearch,scenarios
    path=str(tmp_path/'shorts.db')
    monkeypatch.setattr(shorts,'DB_PATH',path)
    shorts.initialize(50,'fixture',NOW,path)
    b=Stub(p2p.Config())
    arun(b.cmd_shorts('research'))
    assert 'Дней независимой истории' in texts(b)[-1]
    assert any(x['callback_data']=='shorts:research' for row in b.shorts_markup()['inline_keyboard'] for x in row)
    monkeypatch.setattr(scenarios,'ROOT',str(tmp_path))
    scenarios.initialize('base',p2p.Config(),NOW)
    arun(b.cmd_paper('research'))
    assert 'проверка маршрутов' in texts(b)[-1]


def test_clock_calibration_changes_clock_not_freshness_policy(monkeypatch,tmp_path):
    import shortmarket as M
    from helpers import arun
    c=M.Collector(str(tmp_path/'empty.db'))
    monkeypatch.setattr(M.time,'time',lambda:NOW+1.2)
    async def server(session,endpoint):
        assert endpoint=='time'
        return {},NOW
    monkeypatch.setattr(M,'get',server)
    arun(c.calibrate(object()))
    assert abs(c.now()-NOW)<.01
    c.watched={'ALTUSDT'}
    c.add_samples({'ALTUSDT':{'markPrice':'100'}},NOW,c.now())
    assert c.samples['ALTUSDT']
    c.add_samples({'ALTUSDT':{'markPrice':'100'}},NOW-3,c.now())
    assert not c.samples


def test_equal_cached_mark_snapshot_does_not_reset_warmup(tmp_path):
    import shortmarket as M
    c=M.Collector(str(tmp_path/'empty.db'))
    c.watched={'ALTUSDT'}
    ticks={'ALTUSDT':{'markPrice':'100'}}
    for at in range(62):
        c.add_samples(ticks,NOW+at,NOW+at+.1)
        c.add_samples(ticks,NOW+at,NOW+at+.2)
    assert len(c.samples['ALTUSDT'])==62
    assert c.samples['ALTUSDT'][-1][0]-c.samples['ALTUSDT'][0][0]==61
