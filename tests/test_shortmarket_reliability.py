"""Offline collection reliability; all public GETs are replaced by controlled responses."""
import asyncio
import copy
import json
import sqlite3
import time

import aiohttp
import pytest

import shortmarket as M
import shorts as S
from helpers import arun
from test_shorts import NOW, market, open_position, position


@pytest.fixture
def collector(tmp_path, monkeypatch):
    path = str(tmp_path / 'shorts.db')
    monkeypatch.setattr(S, 'DB_PATH', path)
    monkeypatch.setattr(M, 'REQUEST_SPACING', 0)
    S.initialize(50, 'fixture observed RUB/USDT', NOW, path)
    return M.Collector(path)


def instrument():
    out = dict(market()['terms']['instrument'])
    out.update(lotSizeFilter={'qtyStep': '.001', 'minOrderQty': '.001',
                              'minNotionalValue': '5', 'maxMktOrderQty': '100'},
               priceFilter={'tickSize': '.01'}, leverageFilter={'minLeverage': '1', 'maxLeverage': '50'},
               fundingInterval=480, lowerFundingRate='-.003')
    return out


def observed_ticker():
    return dict(market()['terms']['ticker'], symbol='ALTUSDT', markPrice='100', indexPrice='100',
                fundingRate='.0001', nextFundingTime=str((NOW+1000)*1000))


def prime(collector):
    collector.instruments = {'ALTUSDT': instrument()}
    collector.groups = {'ALTUSDT': 'Altcoin'}
    collector.risk['ALTUSDT'] = (NOW, market()['tiers'])
    collector.series[('ALTUSDT', 'minutes')] = (NOW, market()['minutes'])
    collector.samples['ALTUSDT'] = [[NOW-61, '100'], [NOW, '100']]
    collector.watched = {'ALTUSDT'}


def quote_get(calls, *, funding_error=False):
    async def fake(session, endpoint, **params):
        calls.append(endpoint)
        if endpoint == 'tickers':
            return {'list': [observed_ticker()]}, NOW
        if endpoint == 'orderbook':
            return {'b': [['100', '100']], 'a': [['100.1', '100']], 'ts': NOW*1000, 'u': 1}, NOW
        if endpoint == 'funding/history':
            if funding_error:
                raise ValueError('funding unavailable')
            return {'list': []}, NOW
        raise AssertionError('Unexpected optional endpoint: ' + endpoint)
    return fake


def test_public_get_rejects_nonfinite_server_time(monkeypatch):
    async def fake(session, url):
        return {'retCode': 0, 'result': {}, 'time': 'NaN'}
    monkeypatch.setattr(M.perp, '_get', fake)
    with pytest.raises(ValueError, match='время'):
        arun(M.get(None, 'tickers', category='linear'))


def test_catalog_has_hard_page_limit(monkeypatch):
    calls = []
    async def fake(session, endpoint, **params):
        calls.append(params['cursor'])
        return {'list': [{'symbol': 'ALTUSDT'}], 'nextPageCursor': str(len(calls))}, NOW
    monkeypatch.setattr(M, 'CATALOG_MAX_PAGES', 3)
    monkeypatch.setattr(M, 'get', fake)
    with pytest.raises(ValueError, match='предел страниц'):
        arun(M.catalog(None))
    assert calls == ['', '1', '2']


def test_request_retries_transient_errors_but_not_invalid_data(collector, monkeypatch):
    calls = []
    async def transient(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('connection reset')
        return {'list': []}, NOW
    monkeypatch.setattr(M, 'get', transient)
    assert arun(collector.request(None, 'tickers')) == ({'list': []}, NOW)
    assert len(calls) == 2 and collector.diagnostics['retries'] == 1
    async def invalid(*args, **kwargs):
        calls.append(1)
        raise ValueError('malformed response')
    monkeypatch.setattr(M, 'get', invalid)
    with pytest.raises(ValueError):
        arun(collector.request(None, 'tickers'))
    assert len(calls) == 3


def test_rate_limit_cools_down_without_hammering(collector, monkeypatch):
    calls = []
    async def limited(*args, **kwargs):
        calls.append(1)
        raise aiohttp.ClientResponseError(None, (), status=429)
    monkeypatch.setattr(M, 'get', limited)
    with pytest.raises(aiohttp.ClientResponseError):
        arun(collector.request(None, 'tickers', timeout=.1))
    with pytest.raises(TimeoutError):
        arun(collector.request(None, 'tickers', timeout=.1))
    assert len(calls) == 1
    assert 0 < collector.diagnostics_snapshot()['cooldown_seconds'] <= 5


def test_bounded_concurrency_and_timeout_release_slots(collector, monkeypatch):
    inflight, peak = 0, 0
    async def fake(*args, **kwargs):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(.01)
            return {}, NOW
        finally:
            inflight -= 1
    monkeypatch.setattr(M, 'get', fake)
    async def run():
        await asyncio.gather(*(collector.request(None, 'tickers') for _ in range(15)))
        with pytest.raises(TimeoutError):
            await collector.request(None, 'tickers', attempts=1, timeout=.001)
        await collector.request(None, 'tickers')
    arun(run())
    assert peak <= M.REQUEST_CONCURRENCY and inflight == 0


def test_sample_diagnostics_explain_gaps_without_inventing_warmup(collector, monkeypatch):
    monkeypatch.setattr(M.time, 'time', lambda: NOW+61)
    collector.watched = {'ALTUSDT'}
    for i in range(62):
        collector.add_samples({'ALTUSDT': {'markPrice': '100'}}, NOW+i, NOW+i)
    assert collector.diagnostics_snapshot()['samples']['ALTUSDT']['ready_60s']
    collector.add_samples({'ALTUSDT': {'markPrice': '100'}}, NOW+66, NOW+66)
    diagnostics = collector.diagnostics_snapshot()['samples']['ALTUSDT']
    assert diagnostics['count'] == 1 and not diagnostics['ready_60s']
    assert diagnostics['last_reset']['reason'] == 'gap_over_3s'
    collector.add_samples({'ALTUSDT': {'markPrice': '100'}}, NOW+66, NOW+66)
    assert len(collector.samples['ALTUSDT']) == 1


def test_invalid_sample_isolated_and_diagnostics_are_immutable(collector):
    collector.watched = {'ALTUSDT', 'OTHERUSDT'}
    collector.add_samples({'ALTUSDT': {'markPrice': 'NaN'}, 'OTHERUSDT': {'markPrice': '100'}}, NOW, NOW)
    assert 'ALTUSDT' not in collector.samples and collector.samples['OTHERUSDT']
    snapshot = collector.diagnostics_snapshot()
    snapshot['last_errors'].append('user mutation')
    assert not collector.diagnostics['last_errors']
    json.dumps(snapshot, allow_nan=False)


def test_open_position_skips_optional_histories_and_funding_outage_preserves_quote(collector, monkeypatch):
    prime(collector)
    monkeypatch.setattr(M.time, 'time', lambda: NOW)
    calls = []
    monkeypatch.setattr(M, 'get', quote_get(calls, funding_error=True))
    sym, observed = arun(collector.market(None, 'ALTUSDT', False, NOW-5))
    assert sym == 'ALTUSDT' and observed['mark'] == '100'
    assert set(calls) == {'tickers', 'orderbook', 'funding/history'}
    assert not observed['funding_complete']
    assert 'Расчёт финансирования не подтверждён' in observed['data_errors']
    assert observed['fee'] != '0'


def test_open_position_unknown_group_uses_only_explicit_frozen_nonzero_fee(collector, monkeypatch):
    prime(collector)
    collector.groups['ALTUSDT'] = 'unclassified'
    collector._opened_terms['ALTUSDT'] = {'fee': '.0022', 'fee_source': 'fixture frozen nonzero'}
    monkeypatch.setattr(M.time, 'time', lambda: NOW)
    calls = []
    monkeypatch.setattr(M, 'get', quote_get(calls))
    _, observed = arun(collector.market(None, 'ALTUSDT', True, NOW-5))
    assert S.dec(observed['fee']) == S.dec('.0022') and not observed['candidate']
    assert observed['fee_source'] == 'fixture frozen nonzero'
    assert any('Группа комиссии' in error for error in observed['data_errors'])
    collector._opened_terms['ALTUSDT']['fee'] = '0'
    with pytest.raises(ValueError, match='замороженного тарифа'):
        arun(collector.market(None, 'ALTUSDT', False, NOW-5))


def test_open_position_observed_before_slow_optional_catalog(collector, monkeypatch):
    open_position(collector.path)
    order = []
    async def catalog(session, now):
        order.append('catalog')
        raise ValueError('metadata unavailable')
    async def observed(session, sym, candidate, opened):
        order.append('protection')
        out = market(NOW+5)
        out.update(candidate=False, mark='106', index='106')
        return sym, out
    monkeypatch.setattr(collector, 'universe', catalog)
    monkeypatch.setattr(collector, 'market', observed)
    monkeypatch.setattr(M.time, 'time', lambda: NOW+5)
    result = arun(collector.refresh(None))
    assert order == ['protection', 'catalog'] and result['ALTUSDT']['mark'] == '106'
    assert result['ALTUSDT']['observed_at'] == NOW+5
    assert position(collector.path)['stage'] == 'exit'
    assert 'Каталог' in collector.diagnostics['last_errors'][0]


def test_partial_candidate_failure_does_not_hide_other_observations(collector, monkeypatch):
    collector.instruments = {'ALTUSDT': instrument(), 'OTHERUSDT': dict(instrument(), symbol='OTHERUSDT')}
    collector.tickers = {'ALTUSDT': observed_ticker(), 'OTHERUSDT': observed_ticker()}
    async def universe(*args):
        return None
    async def observed(session, sym, candidate, opened):
        if sym == 'OTHERUSDT':
            raise ValueError('missing depth')
        return sym, dict(market(), candidate=False)
    monkeypatch.setattr(collector, 'universe', universe)
    monkeypatch.setattr(collector, 'market', observed)
    monkeypatch.setattr(M.time, 'time', lambda: NOW)
    result = arun(collector.refresh(None))
    assert set(result) == {'ALTUSDT'}
    assert 'OTHERUSDT' in collector.diagnostics['symbol_errors']
    assert collector.diagnostics['candidates'] == 2 and collector.diagnostics['observed'] == 1


def test_total_refresh_deadline_fails_closed_and_reports_reason(collector, monkeypatch):
    collector.instruments = {'ALTUSDT': instrument()}
    collector.tickers = {'ALTUSDT': observed_ticker()}
    async def universe(*args):
        return None
    async def slow(*args):
        await asyncio.sleep(10)
    monkeypatch.setattr(collector, 'universe', universe)
    monkeypatch.setattr(collector, 'market', slow)
    monkeypatch.setattr(M, 'REFRESH_TIMEOUT', .01)
    monkeypatch.setattr(M.time, 'time', lambda: NOW)
    assert arun(collector.refresh(None)) == {}
    assert 'превышен срок' in collector.diagnostics['last_errors'][0]
    assert not S.status(collector.path)['positions']


def test_bar_volume_and_geometry_reject_untrusted_data():
    with pytest.raises(ValueError, match='объём'):
        M.bars([[NOW*1000, '100', '101', '99', '100', '-1']], True)
    with pytest.raises(ValueError, match='геометрия'):
        M.bars([[NOW*1000, '100', '90', '99', '100', '1']], True)


@pytest.mark.parametrize('group', ['G1(Major Coins)', 'G2(High Growth)', 'G3(Mid-Tier Liquidity)',
                                  'G4(Mid-Tier Activation)', 'G5(Long Tail)'])
def test_current_catalog_standard_groups_are_not_misclassified(collector, monkeypatch, group):
    prime(collector)
    collector.groups['ALTUSDT'] = group
    monkeypatch.setattr(M.time, 'time', lambda: NOW)
    calls = []
    monkeypatch.setattr(M, 'get', quote_get(calls))
    _, observed = arun(collector.market(None, 'ALTUSDT', False, NOW-5))
    assert observed['fee'] == M.FEE and observed['fee_source'] == M.FEE_SOURCE
    assert not any('Группа комиссии' in error for error in observed['data_errors'])


def test_catalog_version_stamped_when_both_sources_were_observed(collector, monkeypatch):
    observations = []
    async def fake_catalog(session, request=None):
        observations.append('catalog')
        return {'ALTUSDT': instrument()}, NOW-2
    async def fake_request(session, endpoint, **params):
        if endpoint == 'fee-group-info':
            observations.append('groups')
            return {'list': [{'groupName': 'G5(Long Tail)', 'symbols': ['ALTUSDT']}]}, NOW-1
        return {'list': [observed_ticker()]}, NOW
    monkeypatch.setattr(M, 'catalog', fake_catalog)
    monkeypatch.setattr(collector, 'request', fake_request)
    monkeypatch.setattr(M.time, 'time', lambda: NOW)
    arun(collector.universe(None, NOW))
    with S.connect(collector.path) as con:
        row = con.execute('SELECT ts,state FROM catalog').fetchone()
    state = json.loads(M.zlib.decompress(row['state']))
    assert observations == ['catalog', 'groups']
    assert row['ts'] == NOW and state['observed'] == NOW
    assert state['catalog_server_ts'] == NOW-2 and state['fee_group_server_ts'] == NOW-1


def test_sampler_has_reserved_slot_during_slow_history_collection(collector, monkeypatch):
    occupied, release = asyncio.Event(), asyncio.Event()
    histories = 0
    async def fake(session, endpoint, **params):
        nonlocal histories
        if endpoint == 'kline':
            histories += 1
            if histories == M.REQUEST_CONCURRENCY-1:
                occupied.set()
            await release.wait()
        return {'list': []}, NOW
    monkeypatch.setattr(M, 'get', fake)
    async def run():
        tasks = [asyncio.create_task(collector.request(None, 'kline'))
                 for _ in range(M.REQUEST_CONCURRENCY-1)]
        await occupied.wait()
        assert await collector.request(None, 'tickers', sampling=True, timeout=.1) == ({'list': []}, NOW)
        release.set()
        await asyncio.gather(*tasks)
    arun(run())


def test_market_observation_time_matches_tick_before_later_collection(collector, monkeypatch):
    clock = [NOW]
    collector.instruments = {'ALTUSDT': instrument(), 'OTHERUSDT': dict(instrument(), symbol='OTHERUSDT')}
    collector.tickers = {'ALTUSDT': observed_ticker(), 'OTHERUSDT': observed_ticker()}
    ticks = []
    async def universe(*args):
        return None
    async def observed(session, sym, candidate, opened):
        if sym == 'OTHERUSDT':
            await asyncio.sleep(.01)
            clock[0] = NOW+20
        return sym, dict(market(clock[0], sym), candidate=False)
    def tick(markets, now, path, allow_entries):
        ticks.append((now, copy.deepcopy(markets)))
        return []
    monkeypatch.setattr(collector, 'universe', universe)
    monkeypatch.setattr(collector, 'market', observed)
    monkeypatch.setattr(collector, 'now', lambda: clock[0])
    monkeypatch.setattr(S, 'tick', tick)
    result = arun(collector.refresh(None))
    assert result['ALTUSDT']['observed_at'] == NOW
    assert result['OTHERUSDT']['observed_at'] == NOW+20
    assert ticks[0][0] == ticks[0][1]['ALTUSDT']['observed_at'] == NOW
    assert ticks[-1][0] == ticks[-1][1]['OTHERUSDT']['observed_at'] == NOW+20
    assert result['ALTUSDT']['ticker_ts'] == NOW  # Source timestamp is distinct from lab submission time.


def test_legacy_snapshot_chronology_index_migration_preserves_rows(tmp_path):
    path = str(tmp_path / 'legacy_shorts.db')
    with sqlite3.connect(path) as legacy:
        legacy.execute('CREATE TABLE snapshots (symbol TEXT, ts REAL, state TEXT, PRIMARY KEY(symbol,ts))')
        legacy.executemany('INSERT INTO snapshots VALUES(?,?,?)',
                           [('OTHERUSDT', NOW+3, b'legacy compressed bytes'),
                            ('ALTUSDT', NOW+1, b'unchanged snapshot'),
                            ('ALTUSDT', NOW+3, b'same time distinct symbol')])
        before = legacy.execute('SELECT rowid,symbol,ts,state FROM snapshots ORDER BY rowid').fetchall()
    for _ in range(2):
        with S.connect(path) as migrated:
            after = [tuple(row) for row in migrated.execute(
                'SELECT rowid,symbol,ts,state FROM snapshots ORDER BY rowid')]
            assert after == before
            indexes = migrated.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='snapshots_chronology'").fetchall()
            assert len(indexes) == 1
            assert [row['name'] for row in migrated.execute('PRAGMA index_info(snapshots_chronology)')] == ['ts', 'symbol']


def test_chronological_pages_use_index_ranges_without_sorting(tmp_path):
    path = str(tmp_path / 'indexed_shorts.db')
    with S.connect(path) as con:
        con.executemany('INSERT INTO snapshots VALUES(?,?,?)',
                        [(symbol, NOW+at, '{}') for at in reversed(range(1024))
                         for symbol in ('ALTUSDT', 'OTHERUSDT')])
        cutoff = con.execute('SELECT MAX(rowid) FROM snapshots').fetchone()[0]
        con.execute('INSERT INTO snapshots VALUES(?,?,?)', ('LATEUSDT', NOW+110, '{}'))
        page = ('SELECT rowid,symbol,ts,state FROM snapshots WHERE rowid<=? AND ts<=? '
                'AND (ts,symbol)>(?,?) ORDER BY ts,symbol,rowid LIMIT ?')
        range_page = ('SELECT rowid,symbol,ts,state FROM snapshots WHERE rowid<=? AND ts>=? '
                      'AND ts<=? ORDER BY ts,symbol,rowid LIMIT ?')
        for query, params in ((page, (cutoff, NOW+120, NOW+100, 'ALTUSDT', 30)),
                              (range_page, (cutoff, NOW+100, NOW+120, 30))):
            plan = [row['detail'] for row in con.execute('EXPLAIN QUERY PLAN '+query, params)]
            assert any('SEARCH snapshots USING INDEX snapshots_chronology' in detail for detail in plan)
            assert not any('TEMP B-TREE' in detail for detail in plan)
            rows = con.execute(query, params).fetchall()
            assert rows and all(row['rowid'] <= cutoff and row['symbol'] != 'LATEUSDT' for row in rows)
            assert [(row['ts'], row['symbol']) for row in rows] == sorted(
                (row['ts'], row['symbol']) for row in rows)
