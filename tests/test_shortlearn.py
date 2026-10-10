import copy
import json
import math
import os
import sqlite3
import zlib
from decimal import Decimal

import pytest

import shortlearn as L
import shorts as S
from test_shorts import market, NOW


def observed(now, candidate=True, price='100', **changes):
    m = market(now)
    m.update(observed_at=float(now), candidate=candidate, mark=price, index=price,
             bids=[[price, '100']], asks=[[str(Decimal(price)+Decimal('.1')), '100']],
             mark_samples=[[now-i, price] for i in range(65, -1, -1)])
    m.update(changes)
    return m


def ledger(tmp_path, observations, catalogs=True):
    path = str(tmp_path/'labels.db')
    with S.connect(path, True) as con:
        if catalogs:
            m = observations[0]
            con.execute('INSERT INTO catalog VALUES(?,?)',
                        (min(row['observed_at'] for row in observations)-1, S.dump({'instruments': {'ALTUSDT': m['terms']['instrument']}})))
        for m in observations:
            con.execute('INSERT INTO snapshots VALUES(?,?,?)',
                        (m['symbol'], m['observed_at'], zlib.compress(S.dump(m).encode())))
    return path


def observations(price='98', **changes):
    data = [observed(NOW+i, candidate=i == 0, price=price if i >= 64 else '100')
            for i in range(0, 67, 2)]
    for m in data:
        m.update(changes)
    return data


def outcomes(count=400):
    rows = []
    for i in range(count):
        features = {k: .01 for k in L.FEATURES}
        positive = i % 2
        features['growth_24h'] = .3 if positive else .4
        ts = NOW+i*86400
        rows.append({'symbol': 'ALTUSDT', 'ts': ts, 'outcome_at': ts+3602,
                     'features': features, 'feature_version': L.FEATURE_VERSION,
                     'label_version': L.LABEL_VERSION, 'label_status': 'valid',
                     'label': positive, 'net_return': .02 if positive else -.01})
    return {'version': L.VERSION, 'rows': rows, 'horizon': 3600, 'diagnostics': {}}


def trained():
    data = outcomes()
    return L.fit(data, now=data['rows'][-1]['outcome_at']+1)


def test_features_ignore_open_candles_and_future_samples():
    m = observed(NOW)
    before = L.extract_features(m, NOW)
    altered = copy.deepcopy(m)
    altered['bars'].append([NOW+900, '1', '999999', '0', '999999', '999999'])
    altered['hours'].append([NOW+3600, '1', '999999', '0', '999999', '999999'])
    altered['mark_samples'].append([NOW+1, '999999'])
    assert L.extract_features(altered, NOW) == before
    altered['observed_at'] = NOW+1
    with pytest.raises(ValueError, match='future'):
        L.extract_features(altered, NOW)


@pytest.mark.parametrize('mutation', ['fee', 'gap', 'book', 'future_ticker', 'mark_gap'])
def test_features_refuse_missing_or_stale_data(mutation):
    m = observed(NOW)
    if mutation == 'fee':
        m['fee_source'] = ''
    elif mutation == 'gap':
        m['bars'].pop(3)
    elif mutation == 'book':
        m['book_ts'] = NOW-3
    elif mutation == 'future_ticker':
        m['ticker_ts'] = NOW+1
    else:
        m['mark_samples'] = [[NOW-61, '100'], [NOW, '100']]
    with pytest.raises(ValueError):
        L.extract_features(m, NOW)


def test_fresh_execution_label_includes_two_fees_and_preserves_source(tmp_path):
    path = ledger(tmp_path, observations())
    before = open(path, 'rb').read()
    data = L.build_dataset(path, horizon=60)
    row, = data['rows']
    assert row['label_status'] == 'valid' and row['label'] == 1
    assert row['entry_at'] == NOW+2 and row['outcome_at'] == NOW+64
    assert Decimal(row['qty']) == Decimal('.1')
    assert Decimal(row['fees']) == Decimal('.1')*(100+Decimal('98.1'))*Decimal('.0011')
    assert Decimal(row['funding']) == 0
    assert Decimal(row['pnl']) == Decimal('.1')*(100-Decimal('98.1'))-Decimal(row['fees'])
    assert open(path, 'rb').read() == before


def test_flat_price_is_loss_after_spread_and_fees(tmp_path):
    data = L.build_dataset(ledger(tmp_path, observations('100')), horizon=60)
    row, = data['rows']
    assert row['label'] == 0 and row['net_return'] < 0


@pytest.mark.parametrize('failure,reason', [('gap', 'unobserved_interval'),
    ('partial_entry', 'partial_entry'), ('partial_exit', 'partial_exit'),
    ('fees', 'unknown_fee'), ('rocket', 'isolated_capital_exhaustion'),
    ('old_book', 'entry_window_expired'), ('delist', 'contract_inactive')])
def test_unknown_outcomes_are_not_zero_profit_targets(tmp_path, failure, reason):
    data = observations()
    if failure == 'gap':
        data = [m for m in data if not NOW+20 <= m['observed_at'] <= NOW+50]
    elif failure == 'partial_entry':
        data[1]['bids'] = [['100', '.01']]
    elif failure == 'partial_exit':
        data[-2]['asks'] = [['98', '.01']]
    elif failure == 'fees':
        data[-2]['fee_source'] = ''
    elif failure == 'rocket':
        data[3]['mark'] = '1600'
    elif failure == 'old_book':
        for m in data:
            m['book_id'] = 'same'
    else:
        data[-2]['active'] = False
    row, = L.build_dataset(ledger(tmp_path, data), horizon=60)['rows']
    assert row['label_status'] == 'unknown' and row['label'] is None
    assert row['reason'] == reason and row['net_return'] is None


def test_funding_uses_settled_rates_not_current_prediction(tmp_path):
    data = observations()
    for m in data:
        m.update(next_funding=NOW+32 if m['observed_at'] < NOW+32 else NOW+92,
                 funding_interval=1, funding_rate='-.1')
        if m['observed_at'] >= NOW+32:
            m['funding_history'] = [{'ts': NOW+32, 'mark': '100', 'rate': '.001'}]
    row, = L.build_dataset(ledger(tmp_path, data), horizon=60)['rows']
    assert row['label_status'] == 'valid'
    assert Decimal(row['funding']) == Decimal('.01')
    assert 'minute-open' in row['funding_price_model']


def test_unknown_or_inconsistent_funding_refuses_label(tmp_path):
    data = observations()
    for m in data:
        m.update(next_funding=NOW+32 if m['observed_at'] < NOW+32 else NOW+92,
                 funding_interval=1, funding_complete=False)
    row, = L.build_dataset(ledger(tmp_path, data), horizon=60)['rows']
    assert row['reason'] == 'unknown_funding_charge' and row['label'] is None


def test_historical_universe_and_catalog_never_backfilled(tmp_path):
    path = ledger(tmp_path, observations(), catalogs=False)
    with S.connect(path, True) as con:
        m = observed(NOW)
        con.execute('INSERT INTO catalog VALUES(?,?)', (NOW+1, S.dump({'instruments': {'ALTUSDT': m['terms']['instrument']}})))
    result = L.build_dataset(path, horizon=60)
    assert not result['rows']
    assert result['diagnostics']['candidate_rejections']['unknown historical universe'] == 1


def test_bounded_decompression_and_read_limit(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        L._decode(zlib.compress(b'x'*(L.MAX_RECORD_BYTES+1)))
    with pytest.raises(ValueError):
        L._decode(zlib.compress(b'{}')+b'trailing')
    data = L.build_dataset(ledger(tmp_path, observations()), horizon=60, max_observations=2)
    assert data['diagnostics']['observations'] == 2
    assert data['diagnostics']['truncated']
    assert data['rows'][0]['label_status'] == 'unknown'


def test_catalog_has_separate_bounded_decoder_budget(tmp_path):
    large = json.dumps({'instruments': {}, 'padding': 'x'*(L.MAX_RECORD_BYTES+1)})
    with pytest.raises(ValueError):
        L._decode(large)
    assert L._decode(large, L.MAX_CATALOG_BYTES)['instruments'] == {}
    with pytest.raises(ValueError):
        L._decode(zlib.compress(b'x'*(L.MAX_CATALOG_BYTES+1)), L.MAX_CATALOG_BYTES)


def test_fit_is_deterministic_with_purged_chronological_holdout():
    data = outcomes()
    now = data['rows'][-1]['outcome_at']+1
    a, b = L.fit(data, now=now), L.fit(data, now=now)
    assert a == b and a['trained'] and a['shadow_only'] and not a['live_permission']
    parts = a['evaluation']['partitions']
    assert parts['train']['end'] < parts['validation']['start']-3600
    assert parts['validation']['end'] < parts['test']['start']-3600
    assert a['training_end'] == parts['train']['end']
    assert a['evaluation']['test']['brier'] < a['evaluation']['test']['baseline_brier']
    assert a['evaluation']['test']['calibration']
    assert a['evaluation']['test']['flat_return'] == 0


def test_test_outcomes_never_change_fitted_weights_or_threshold():
    data = outcomes()
    now = data['rows'][-1]['outcome_at']+1
    baseline = L.fit(data, now=now)
    altered = copy.deepcopy(data)
    test_start = baseline['evaluation']['partitions']['test']['start']
    for row in altered['rows']:
        if row['ts'] >= test_start:
            row['label'] = 1-row['label']
            row['net_return'] = -row['net_return']
    changed = L.fit(altered, now=now)
    assert baseline['model'] == changed['model']
    assert baseline['training_digest'] == changed['training_digest']
    assert baseline['evaluation']['test'] != changed['evaluation']['test']


@pytest.mark.parametrize('failure', ['history', 'overlap', 'future', 'truncated', 'single_class'])
def test_insufficient_or_invalid_history_never_claims_trained(failure):
    data = outcomes()
    now = data['rows'][-1]['outcome_at']+1
    if failure == 'history':
        data['rows'] = data['rows'][:20]
    elif failure == 'overlap':
        for row in data['rows']:
            row['outcome_at'] += 100*86400
        now += 100*86400
    elif failure == 'future':
        now = data['rows'][-1]['ts']
    elif failure == 'truncated':
        data['diagnostics']['truncated'] = True
    else:
        for row in data['rows']:
            row['label'] = 1
    result = L.fit(data, now=now)
    assert not result['trained'] and not result['ready'] and result['model'] is None


def test_prediction_explains_shadow_probability_and_refuses_domain(monkeypatch):
    artifact = trained()
    values = {k: .01 for k in L.FEATURES}
    values['growth_24h'] = .3
    monkeypatch.setattr(L, 'extract_features', lambda m, now: dict(values))
    out = L.predict(artifact, {}, artifact['created_at']+1)
    assert out['available'] and out['probability'] > .5 and out['risk_score'] == 1-out['probability']
    assert out['sample_support'] == artifact['model']['training_samples']
    assert len(out['explanation']) == 4 and out['shadow_only']
    values['growth_24h'] = 15
    out = L.predict(artifact, {}, artifact['created_at']+1)
    assert not out['available'] and out['probability'] is None and 'growth_24h' in out['outside_domain']


def test_atomic_save_load_validates_bounds_and_preserves_previous(tmp_path, monkeypatch):
    path = str(tmp_path/'model.json')
    artifact = trained()
    assert L.save(artifact, path) == path and L.load(path) == artifact
    before = open(path, 'rb').read()
    def fail_replace(*args):
        raise OSError('fixture crash')
    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError):
        L.save(artifact, path)
    assert open(path, 'rb').read() == before
    assert not list(tmp_path.glob('.short-model-*'))
    invalid = copy.deepcopy(artifact)
    invalid['model']['weights'][0] = math.nan
    with pytest.raises(ValueError):
        L.save(invalid, path)
    assert open(path, 'rb').read() == before
    artifact['live_permission'] = True
    with pytest.raises(ValueError, match='shadow'):
        L.save(artifact, path)


def test_load_refuses_oversize_corruption_future_cutoff_and_wrong_versions(tmp_path):
    path = str(tmp_path/'model.json')
    assert L.load(path) is None
    with open(path, 'wb') as out:
        out.write(b'x'*(L.MAX_ARTIFACT_BYTES+1))
    with pytest.raises(ValueError, match='large'):
        L.load(path)
    artifact = trained()
    artifact['training_end'] = artifact['created_at']+1
    with open(path, 'w', encoding='utf-8') as out:
        json.dump(artifact, out)
    with pytest.raises(ValueError, match='future'):
        L.load(path)
    artifact['version'] = 'future-format'
    with open(path, 'w', encoding='utf-8') as out:
        json.dump(artifact, out)
    with pytest.raises(ValueError, match='unsupported'):
        L.load(path)


def test_report_distinguishes_untrained_and_test_results(tmp_path):
    assert 'ещё не обучена' in L.report(path=str(tmp_path/'missing.json'))
    untrained = L.fit(outcomes(20), now=NOW+500*86400)
    assert 'Нетронутый тест' in L.report(untrained)
    assert 'Рабочие правила' in L.report(trained())


def test_large_history_samples_dated_windows_not_only_first_hours(tmp_path, monkeypatch):
    data = []
    for day in range(4):
        for second in range(180):
            now = NOW+day*86400+second
            data.append(observed(now, price='98' if second >= 64 else '100'))
    path = ledger(tmp_path, data)
    with S.connect(path, True) as con:
        for day in range(1, 4):
            con.execute('INSERT INTO catalog VALUES(?,?)', (NOW+day*86400-1,
                S.dump({'instruments': {'ALTUSDT': data[0]['terms']['instrument']}})))
    # Spaced decisions use indexed historical catalogs and do not hit the streaming ceiling.
    monkeypatch.setattr(L, 'MAX_CATALOGS', 1)
    result = L.build_dataset(path, horizon=60, max_observations=200)
    d = result['diagnostics']
    assert d['sampling'] == 'spaced_observed_windows_v1'
    assert d['source_end']-d['source_start'] > 3*86400
    assert d['observations'] <= 200 and not d['truncated']
    assert d['catalogs'] == 4
    assert len(result['rows']) >= 2
    assert result['rows'][-1]['ts']-result['rows'][0]['ts'] >= 3*86400
    for row in result['rows']:
        assert row['label_status'] == 'valid'


def test_recorded_intrabucket_spike_is_unknown_not_profitable_recovery(tmp_path):
    data = observations()
    for m in data:
        if NOW+34 <= m['observed_at'] <= NOW+60:
            for point in m['mark_samples']:
                if point[0] == NOW+33:
                    point[1] = '1600'
    row, = L.build_dataset(ledger(tmp_path, data), horizon=60)['rows']
    assert row['label'] is None and row['reason'] == 'isolated_capital_exhaustion'


@pytest.mark.parametrize('mutation,reason', [('tier', 'unknown_maintenance_margin'),
    ('funding', 'possible_isolated_liquidation'), ('mark_gap', 'unobserved_mark_interval'),
    ('step', 'instrument_step_changed'), ('depth', 'invalid_outcome_inputs:ValueError')])
def test_unverified_liquidation_inputs_and_invalid_books_block_labels(tmp_path, mutation, reason):
    data = observations()
    if mutation == 'tier':
        data[3]['tiers'] = []
    elif mutation == 'funding':
        for m in data:
            m.update(next_funding=NOW+32 if m['observed_at'] < NOW+32 else NOW+92,
                     funding_interval=1)
            if m['observed_at'] >= NOW+32:
                m['funding_history'] = [{'ts': NOW+32, 'mark': '100', 'rate': '-1'}]
    elif mutation == 'mark_gap':
        data[3]['mark_samples'] = []
    elif mutation == 'step':
        data[1]['step'] = '.01'
    else:
        data[1]['bids'] = [['100', '1'], ['101', '1']]
    row, = L.build_dataset(ledger(tmp_path, data), horizon=60)['rows']
    assert row['label'] is None and row['reason'] == reason


def test_ood_rows_cannot_select_threshold_or_test_returns():
    artifact = trained()
    values = {k: .01 for k in L.FEATURES}
    values['growth_24h'] = -15  # Positive model output but entirely outside observed domain.
    row = {'features': values, 'label': 1, 'net_return': 100}
    metric = L._metrics(artifact['model'], [row], .5, .5)
    assert metric['eligible'] == 0 and metric['selected'] == 0
    assert metric['sum_selected_return'] == 0 and metric['mean_selected_return'] is None
    assert metric['abstained_outside_domain'] == 1 and metric['brier'] is not None


def test_artifact_nested_nan_or_missing_description_refuses_save(tmp_path):
    artifact = trained()
    artifact['evaluation']['test']['brier'] = float('nan')
    with pytest.raises(ValueError):
        L.save(artifact, str(tmp_path/'model.json'))
    artifact = trained()
    del artifact['reason']
    with pytest.raises(ValueError):
        L.save(artifact, str(tmp_path/'model.json'))


def test_live_delete_mode_writer_commits_during_decode_and_late_rows_are_excluded(tmp_path, monkeypatch):
    original = observations()
    path = ledger(tmp_path, original)
    original_decode = L._decode
    committed = []
    monkeypatch.setattr(L, 'READ_PAGE', 3)  # Exercise page boundaries, not one preloaded result.

    def decode_while_live_writer_runs(raw, maximum=L.MAX_RECORD_BYTES):
        if not committed:
            with sqlite3.connect(path, timeout=.05) as writer:
                assert writer.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
                # Both are later inserts with HISTORICAL timestamps. MAX(ts) alone is insufficient.
                late = observed(NOW+1, candidate=False)
                writer.execute('INSERT INTO snapshots VALUES(?,?,?)',
                    (late['symbol'], late['observed_at'], zlib.compress(S.dump(late).encode())))
                writer.execute('INSERT INTO catalog VALUES(?,?)', (NOW-.5, S.dump({'instruments': {}})))
                later = observed(NOW+1000, candidate=False)
                writer.execute('INSERT INTO snapshots VALUES(?,?,?)',
                    (later['symbol'], later['observed_at'], zlib.compress(S.dump(later).encode())))
                writer.commit()  # Would fail against the previous long BEGIN read transaction.
            committed.append(True)
        return original_decode(raw, maximum)

    monkeypatch.setattr(L, '_decode', decode_while_live_writer_runs)
    result = L.build_dataset(path, horizon=60)
    assert committed
    row, = result['rows']
    assert row['label_status'] == 'valid' and row['label'] == 1
    assert row['entry_at'] == NOW+2  # The appended historical book at NOW+1 is excluded.
    assert result['diagnostics']['source_observations'] == len(original)
    assert result['diagnostics']['observations'] == len(original)
    assert result['diagnostics']['catalogs'] == 1


def test_window_reads_release_cursor_before_live_writer_processing(tmp_path, monkeypatch):
    data = [observed(NOW+day*86400+second, price='98' if second >= 64 else '100')
            for day in range(4) for second in range(180)]
    path = ledger(tmp_path, data)
    original_decode = L._decode
    commits = []

    def decode_with_commit(raw, maximum=L.MAX_RECORD_BYTES):
        # Simulate a write on every decode, including catalogs and window label pages.
        with sqlite3.connect(path, timeout=.05) as writer:
            writer.execute('INSERT INTO events(ts,kind,details) VALUES(?,?,?)', (NOW, 'concurrent_fixture', '{}'))
            writer.commit()
        commits.append(True)
        return original_decode(raw, maximum)

    monkeypatch.setattr(L, '_decode', decode_with_commit)
    result = L.build_dataset(path, horizon=60, max_observations=200)
    assert len(commits) > 10
    assert result['diagnostics']['sampling'] == 'spaced_observed_windows_v1'
    assert any(row['label_status'] == 'valid' for row in result['rows'])


@pytest.mark.parametrize('marks_present', [True, False])
def test_fifteen_second_real_books_require_continuous_one_second_marks(tmp_path, marks_present):
    data = [observed(NOW+second, candidate=second == 0, price='98' if second >= 90 else '100')
            for second in range(0, 106, 15)]
    if not marks_present:
        data[2]['mark_samples'] = []
    result = L.build_dataset(ledger(tmp_path, data), horizon=60)
    row, = result['rows']
    if marks_present:
        assert row['label_status'] == 'valid' and row['label'] == 1
        assert row['entry_at'] == NOW+15 and row['outcome_at'] == NOW+90
        assert result['diagnostics']['book_interval_limit'] == 30
        assert result['diagnostics']['mark_interval_limit'] == 3
    else:
        assert row['label'] is None and row['reason'] == 'unobserved_mark_interval'


def test_high_unknown_outcome_rate_cannot_be_hidden_from_training():
    data = outcomes()
    for row in data['rows'][::2]:
        row.update(label_status='unknown', label=None, net_return=None, reason='partial_exit')
    artifact = L.fit(data, now=data['rows'][-1]['outcome_at']+1)
    assert not artifact['trained'] and not artifact['ready']
    assert artifact['requirements']['minimum_known_coverage'] == .8
    assert artifact['evaluation']['unknown_label_ratio'] == .5
    assert artifact['evaluation']['label_coverage']['all']['unknown'] == 200
    assert 'полнота' in artifact['reason']


def test_unknown_outcomes_concentrated_in_test_block_training_despite_global_coverage():
    data = outcomes()
    # Ten percent unknown globally, but every other outcome in the final test is absent.
    for row in data['rows'][320::2]:
        row.update(label_status='unknown', label=None, net_return=None, reason='no_liquidity')
    artifact = L.fit(data, now=data['rows'][-1]['outcome_at']+1)
    assert artifact['evaluation']['known_label_coverage'] == .9
    assert not artifact['trained'] and not artifact['ready']
    assert artifact['evaluation']['label_coverage']['test']['known_fraction'] < .8
    assert 'хронологическом' in artifact['reason']


@pytest.mark.parametrize('future_field', ['created_at', 'test_end', 'training_end', 'observed_at'])
def test_model_cannot_score_an_observation_before_it_or_its_outcomes_existed(monkeypatch, future_field):
    artifact = trained()
    now = artifact['created_at']+20
    m = {}
    if future_field == 'created_at':
        artifact['created_at'] = now+20
    elif future_field == 'test_end':
        artifact['evaluation']['partitions']['test']['end'] = now+1
    elif future_field == 'training_end':
        artifact['training_end'] = now+1
        artifact['created_at'] = now+2  # A valid saved artifact, but both dates follow observation.
    else:
        m['observed_at'] = artifact['created_at']-1
    monkeypatch.setattr(L, 'extract_features', lambda *a: pytest.fail('future model must not inspect features'))
    out = L.predict(artifact, m, now)
    assert not out['available'] and out['probability'] is None
    assert 'момент наблюдения' in out['reason']


def test_legacy_chronology_fallback_is_read_only_bounded_and_sorted_once(tmp_path, monkeypatch):
    original = observations()
    path = ledger(tmp_path, list(reversed(original)))  # Insertion order need not be chronological.
    with sqlite3.connect(path) as con:
        for name, in con.execute("SELECT name FROM pragma_index_list('snapshots')").fetchall():
            if name == 'snapshots_chronology':
                con.execute('DROP INDEX snapshots_chronology')
    before = open(path, 'rb').read()
    data = L.build_dataset(path, horizon=60)
    row, = data['rows']
    assert row['label_status'] == 'valid' and row['entry_at'] == NOW+2
    assert not data['diagnostics']['chronology_index']
    assert data['diagnostics']['legacy_read_limit'] == L.LEGACY_READ_LIMIT
    assert open(path, 'rb').read() == before
    monkeypatch.setattr(L, 'LEGACY_READ_LIMIT', 3)
    partial = L.build_dataset(path, horizon=60)
    assert partial['diagnostics']['observations'] == 3
    assert partial['diagnostics']['truncated']


def test_indexed_tuple_pagination_does_not_resort_historical_prefix(tmp_path):
    path = ledger(tmp_path, observations())
    with sqlite3.connect(path) as con:
        assert L._has_chronology_index(con)
        plan = con.execute('EXPLAIN QUERY PLAN SELECT symbol,ts,state FROM snapshots '
            'WHERE rowid<=? AND ts<=? AND (ts,symbol)>(?,?) ORDER BY ts,symbol LIMIT ?',
            (1000, NOW+1000, NOW, '', 128)).fetchall()
    assert not any('TEMP B-TREE' in row[-1].upper() for row in plan)
