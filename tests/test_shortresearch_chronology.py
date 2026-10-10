import hashlib
import json
from pathlib import Path

import pytest

import shortresearch as research
import shorts
from test_shorts import NOW, market


def source(tmp_path, batches, catalogs=None):
    path = str(tmp_path / 'source.db')
    shorts.initialize(50, 'fixture observed RUB/USDT', NOW, path)
    catalogs = catalogs or [(NOW - 1, ['ALTUSDT', 'OTHERUSDT'])]
    with shorts.connect(path, True) as con:
        for stamp, symbols in catalogs:
            state = {'instruments': {sym: market(stamp, sym)['terms']['instrument'] for sym in symbols}}
            con.execute('INSERT INTO catalog VALUES(?,?)', (stamp, json.dumps(state)))
        for stamp, symbols, dated in batches:
            for sym in symbols:
                m = market(stamp, sym)
                if dated:
                    m['observed_at'] = stamp
                con.execute('INSERT INTO snapshots VALUES(?,?,?)', (sym, stamp, json.dumps(m)))
    return path


def recorder(monkeypatch):
    calls = []

    def tick(markets, stamp, path, **kwargs):
        calls.append((Path(path).parent.name, Path(path).stem, stamp, markets))
        return []

    monkeypatch.setattr(shorts, 'tick', tick)
    return calls


def test_replay_batches_same_timestamp_and_preserves_readonly_source(tmp_path, monkeypatch):
    path = source(tmp_path, [(NOW, ['ALTUSDT'], True),
                             (NOW + 100, ['ALTUSDT', 'OTHERUSDT'], True),
                             (NOW + 120, ['ALTUSDT', 'OTHERUSDT'], True)])
    digest = hashlib.sha256(Path(path).read_bytes()).digest()
    calls = recorder(monkeypatch)
    result = research.evaluate(path, minimum_days=0)
    strategy = [call for call in calls if call[0:2] == ('test', 'strategy')]
    assert len(strategy) == 2
    assert all(set(call[3]) == {'ALTUSDT', 'OTHERUSDT'} for call in strategy)
    assert result['variants']['strategy']['observation_batches'] == 2
    assert result['variants']['strategy']['observations'] == 4
    assert hashlib.sha256(Path(path).read_bytes()).digest() == digest
    assert result['training'][1] == result['validation'][0]
    assert result['validation'][1] == result['untouched_test'][0]
    assert result['verification'] == result['untouched_test']
    assert result['initial_capital'] == '1000'
    assert not result['real_money_ready']


def test_catalog_never_comes_from_future_and_cached_candidate_is_removed(tmp_path, monkeypatch):
    path = source(tmp_path, [(NOW, ['ALTUSDT'], True),
                             (NOW + 100, ['ALTUSDT'], True),
                             (NOW + 102, ['OTHERUSDT'], True),
                             (NOW + 120, ['ALTUSDT'], True)],
                  [(NOW + 101, ['ALTUSDT', 'OTHERUSDT']), (NOW + 103, ['OTHERUSDT'])])
    calls = recorder(monkeypatch)
    result = research.evaluate(path, minimum_days=0)
    strategy = [call for call in calls if call[0:2] == ('test', 'strategy')]
    assert strategy[0][2] == NOW + 100 and strategy[0][3] == {}
    assert result['variants']['strategy']['invalid_universe_observations'] == 2
    assert set(strategy[1][3]) == {'OTHERUSDT'}
    assert strategy[-1][3] == {}  # stale cached quotes are discarded rather than revived
    assert not result['ready']


def test_legacy_rows_are_counted_and_usable_rows_still_replayed(tmp_path, monkeypatch):
    path = source(tmp_path, [(NOW, ['ALTUSDT'], False),
                             (NOW + 100, ['ALTUSDT'], False),
                             (NOW + 120, ['ALTUSDT'], True)])
    calls = recorder(monkeypatch)
    result = research.evaluate(path, minimum_days=0)
    assert result['coverage']['legacy_observations'] == 2
    assert result['coverage']['dated_observations'] == 1
    assert result['coverage']['verified_days'] == 0
    assert result['variants']['strategy']['legacy_observations'] == 1
    assert result['variants']['strategy']['observations'] == 1
    assert any(call[0:2] == ('test', 'strategy') and call[2] == NOW + 120 and call[3]
               for call in calls)
    assert not result['ready']


@pytest.mark.parametrize('uncertainty', ['open', 'funding', 'gap', 'stress'])
def test_sufficient_trades_do_not_hide_unfinished_or_unverified_results(tmp_path, monkeypatch, uncertainty):
    path = source(tmp_path, [(NOW, ['ALTUSDT'], True), (NOW + 120, ['ALTUSDT'], True)])
    recorder(monkeypatch)
    positions = [{'stage': 'closed', 'opened': NOW, 'symbol': 'ALTUSDT',
                  'signal_ts': NOW + i, 'pnl': '1'} for i in range(50)]
    if uncertainty in ('open', 'funding'):
        positions.append({'stage': uncertainty, 'opened': NOW, 'symbol': 'ALTUSDT', 'pnl': '0'})
    else:
        positions[-1][uncertainty] = True
    monkeypatch.setattr(shorts, 'status', lambda path: {'positions': positions, 'realized': '50',
                        'equity': '1050', 'drawdown': '0', 'rejections': {}})
    result = research.evaluate(path, minimum_days=0)
    assert result['variants']['strategy']['trades'] == 50
    assert not result['ready'] and not result['evidence_ready']
    assert not result['real_money_ready']


def test_missing_research_database_is_not_created(tmp_path):
    path = tmp_path / 'absent.db'
    assert research.coverage(str(path))['snapshots'] == 0
    assert not research.evaluate(str(path))['ready']
    assert not path.exists()


def test_replay_does_not_silently_change_recorded_risk_policy(tmp_path):
    path = source(tmp_path, [(NOW, ['ALTUSDT'], True), (NOW + 120, ['ALTUSDT'], True)])
    with shorts.connect(path, True) as con:
        state = shorts.meta(con)
        state['policy']['leverage'] = '10'
        shorts.save_meta(con, state)
    result = research.evaluate(path, minimum_days=0)
    assert not result['ready']
    assert 'версия правил' in result['reason']


def test_same_timestamp_ranking_uses_all_candidates_before_allocation(tmp_path, monkeypatch):
    symbols = ['AUSDT', 'BUSDT', 'CUSDT']
    path = source(tmp_path, [(NOW, ['AUSDT'], True),
                             (NOW + 38, symbols, True), (NOW + 44, symbols, True)],
                  [(NOW - 1, symbols)])
    with shorts.connect(path, True) as con:
        for row in list(con.execute('SELECT symbol,ts,state FROM snapshots')):
            m = research.decode(row['state'])
            m['growth'] = {'AUSDT': '.3', 'BUSDT': '.5', 'CUSDT': '.8'}[row['symbol']]
            con.execute('UPDATE snapshots SET state=? WHERE symbol=? AND ts=?',
                        (json.dumps(m), row['symbol'], row['ts']))
    real_tick, allocated = shorts.tick, []

    def tick(markets, stamp, db, **kwargs):
        events = real_tick(markets, stamp, db, **kwargs)
        if Path(db).parent.name == 'test' and Path(db).stem == 'strategy':
            allocated.append([p['symbol'] for p in shorts.status(db)['positions']])
        return events

    monkeypatch.setattr(shorts, 'tick', tick)
    result = research.evaluate(path, minimum_days=0)
    assert allocated[0] == ['CUSDT', 'BUSDT']
    assert result['variants']['strategy']['open_positions'] == 2
    assert not result['ready']


def test_legacy_date_span_does_not_count_as_verified_ninety_day_history(tmp_path, monkeypatch):
    path = source(tmp_path, [(NOW, ['ALTUSDT'], False),
                             (NOW + 90 * 86400, ['ALTUSDT'], True),
                             (NOW + 100 * 86400, ['ALTUSDT'], True)])
    recorder(monkeypatch)
    positions = [{'stage': 'closed', 'opened': NOW, 'symbol': 'ALTUSDT',
                  'signal_ts': NOW + i, 'pnl': '1'} for i in range(50)]
    monkeypatch.setattr(shorts, 'status', lambda path: {'positions': positions, 'realized': '50',
                        'equity': '1050', 'drawdown': '0', 'rejections': {}})
    result = research.evaluate(path)
    assert result['coverage']['days'] == 100
    assert result['coverage']['verified_days'] == 10
    assert result['variants']['strategy']['trades'] == 50
    assert not result['ready']
