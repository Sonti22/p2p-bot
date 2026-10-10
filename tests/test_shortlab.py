import copy
import csv
import sqlite3
import asyncio
import pytest

import shortlab
import shortlogic
import shorts
from helpers import arun
from test_shorts import NOW, market


def test_explanations_close_candles_without_future_leakage():
    m = market()
    before = shortlogic.explain(m, NOW)
    assert before['entry_ready']
    m['bars'].append([int(NOW // 900) * 900, '100', '9000', '1', '8000', '100000'])
    after = shortlogic.explain(m, NOW)
    assert after['metrics'] == before['metrics']
    assert after['hypotheses'] == before['hypotheses']


def test_incomplete_data_explained_without_entry():
    result = shortlogic.explain({'symbol': '<coin>'}, NOW)
    assert not result['entry_ready'] and result['gates']
    assert result['model_role'] == 'shadow_only'


def test_journal_updates_window_and_keeps_transitions(tmp_path):
    path = str(tmp_path / 'lab.db')
    m = market()
    shortlab.record({'ALTUSDT': m}, {}, NOW, path)
    shortlab.record({'ALTUSDT': m}, {}, NOW + 1, path)
    assert shortlab.read(path)['count'] == 1
    m['fee_source'] = ''
    shortlab.record({'ALTUSDT': m}, {}, NOW + 2, path)
    con = sqlite3.connect(path)
    try:
        assert con.execute('SELECT COUNT(*) FROM transitions').fetchone()[0] == 2
        assert con.execute('SELECT observations FROM decisions').fetchone()[0] == 3
    finally:
        con.close()


def test_reports_read_missing_journal_without_creating_it(tmp_path):
    path = str(tmp_path / 'absent.db')
    assert 'пока пуст' in shortlab.report('decisions', path)
    assert 'ещё не выполнено' in shortlab.report('learning', path)
    assert not (tmp_path / 'absent.db').exists()


def test_csv_explains_skipped_candidates(tmp_path):
    path = str(tmp_path / 'lab.db')
    m = market(); m['fee_source'] = ''
    shortlab.record({'ALTUSDT': m}, {}, NOW, path)
    target = str(tmp_path / 'report.csv')
    shortlab.export(target, path)
    with open(target, encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 2 and 'комиссии' in rows[1][-1]


def test_shadow_analysis_never_changes_ledger(tmp_path):
    ledger = str(tmp_path / 'shorts.db')
    lab = str(tmp_path / 'lab.db')
    shorts.initialize(50, 'fixture', NOW, ledger)
    before = copy.deepcopy(shorts.status(ledger))
    shortlab.record({'ALTUSDT': market()}, {}, NOW, lab)
    assert shorts.status(ledger) == before


def test_command_buttons_and_guest_gate(tmp_path, monkeypatch):
    from test_bot import Stub, texts
    from test_guests import Stub as Guest
    import p2p
    monkeypatch.setattr(shortlab, 'DB_PATH', str(tmp_path / 'lab.db'))
    b = Stub(p2p.Config())
    for command in ('knowledge', 'decisions', 'learning', 'feed', 'labexport'):
        arun(b.cmd_shorts(command))
    assert any('Логика' in text for text in texts(b))
    assert any(method == 'sendDocument' for method, _ in b.out)
    buttons = {item['callback_data'] for row in b.shorts_markup()['inline_keyboard'] for item in row}
    assert {'shorts:knowledge', 'shorts:decisions', 'shorts:learning', 'shorts:feed'} <= buttons
    calls = []
    async def command(section):
        calls.append(section)
    monkeypatch.setattr(b, 'cmd_shorts', command)
    for action in ('knowledge', 'decisions', 'learning', 'feed', 'labexport'):
        arun(b.on_update({'callback_query': {'id': '1', 'from': {'id': 1}, 'data': 'shorts:' + action,
                         'message': {'chat': {'id': 1, 'type': 'private'}, 'message_id': 3}}}))
    assert calls == ['knowledge', 'decisions', 'learning', 'feed', 'labexport']
    guest = Guest(p2p.Config(), guests=['42'])
    arun(guest.on_update({'callback_query': {'id': '1', 'data': 'shorts:learning',
                         'message': {'chat': {'id': 42}, 'message_id': 3}}}))
    assert [method for method, _ in guest.out] == ['answerCallbackQuery']


def test_feed_is_human_readable_and_escapes_symbols(tmp_path):
    path = str(tmp_path / 'lab.db')
    shortlab.record({}, {'requests': 42, 'retries': 2, 'refresh_seconds': .5,
                         'sampling': {'<ALT>': {'seconds': 30, 'age': 1, 'reset_reason': 'gap'}}}, NOW, path)
    report = shortlab.report('feed', path)
    assert 'Запросов: 42' in report and '30 с из 60' in report
    assert '&lt;ALT&gt;' in report and '<ALT>' not in report


def test_background_jobs_are_bounded_and_finish_before_shutdown(tmp_path, monkeypatch):
    calls = []
    entered = asyncio.Event()
    released = asyncio.Event()
    async def slow_record(self, markets, diagnostics, now):
        entered.set()
        await released.wait()
        calls.append(markets['ALTUSDT']['mark'])
    async def train(self):
        calls.append('train')
    monkeypatch.setattr(shortlab.Worker, '_record', slow_record)
    monkeypatch.setattr(shortlab.Worker, '_train', train)
    async def run():
        worker = shortlab.Worker(path=str(tmp_path / 'lab.db'))
        m = market()
        worker.submit({'ALTUSDT': m}, {}, NOW)
        await entered.wait()
        m['mark'] = '999'
        for _ in range(5):
            worker.submit({'ALTUSDT': m}, {}, NOW + 1)
        assert worker.skipped_records == 5
        released.set()
        await worker.close()
    arun(run())
    assert calls.count('train') == 1 and '100' in calls and '999' not in calls


def test_training_schedule_survives_restart(tmp_path):
    import time
    path = str(tmp_path / 'lab.db')
    shortlab.save_training({'ready': False, 'reason': 'fixture'}, time.time(), path)
    worker = shortlab.Worker(path=path)
    assert worker.next_train > time.monotonic() + shortlab.TRAIN_EVERY - 10


def test_insufficient_learning_never_writes_fake_model(tmp_path):
    source = str(tmp_path / 'source.db')
    journal = str(tmp_path / 'lab.db')
    target = str(tmp_path / 'model.json')
    shorts.initialize(50, 'fixture', NOW, source)
    result = shortlab.train(source, journal, target)
    assert not result['ready'] and not result['trained']
    assert not (tmp_path / 'model.json').exists()
    assert not shortlab.read(journal)['training']['ready']


def test_paths_cannot_replace_or_contaminate_ledger(tmp_path):
    source = str(tmp_path / 'source.db')
    with pytest.raises(ValueError, match='разные пути'):
        shortlab.train(source, source, str(tmp_path / 'model.json'))
    with pytest.raises(ValueError, match='разные пути'):
        shortlab.Worker(source, str(tmp_path / 'lab.db'), source)
    assert not (tmp_path / 'source.db').exists()


def test_extreme_html_escaping_stays_below_message_limit(tmp_path):
    path = str(tmp_path / 'lab.db')
    m = market()
    m['data_errors'] = ['"' * 200] * 5
    shortlab.record({'ALTUSDT': m}, {}, NOW, path)
    assert max(map(len, shortlab.report('decisions', path).split('\n\n'))) < 3800


def test_stale_model_outcomes_and_failed_revalidation_disable_scores(tmp_path, monkeypatch):
    import shortlearn
    import time
    journal = str(tmp_path / 'lab.db')
    model = str(tmp_path / 'model.json')
    (tmp_path / 'model.json').write_text('{}', encoding='utf-8')
    artifact = {'ready': True, 'created_at': time.time(),
                'evaluation': {'partitions': {'test': {'end': time.time() - 10 * 86400}}}}
    monkeypatch.setattr(shortlearn, 'load', lambda path: artifact)
    shortlab.save_training({'ready': True}, time.time(), journal)
    worker = shortlab.Worker(path=journal, model_path=model)
    worker._record_scored({'ALTUSDT': market()}, {}, NOW)
    assert 'prediction' not in shortlab.read(journal)['decisions'][0]
    artifact['evaluation']['partitions']['test']['end'] = time.time()
    shortlab.save_training({'ready': False}, time.time(), journal)
    worker._record_scored({'ALTUSDT': market()}, {}, NOW + 1)
    assert 'prediction' not in shortlab.read(journal)['decisions'][0]


def test_explanation_uses_observation_time_not_late_collection_completion(tmp_path):
    path = str(tmp_path / 'lab.db')
    m = market(); m['observed_at'] = NOW
    shortlab.record({'ALTUSDT': m}, {}, NOW + 40, path)
    decision = shortlab.read(path)['decisions'][0]
    assert decision['entry_ready'] and decision['observed_at'] == NOW
    assert decision['recorded_at'] == NOW + 40


def test_incomplete_training_coverage_and_worker_errors_are_visible(tmp_path):
    path = str(tmp_path / 'lab.db')
    shortlab.record({}, {'lab_error': '<database error>', 'model_error': 'old model'}, NOW, path)
    shortlab.save_training({'ready': False, 'reason': 'неполные исходы',
                           'dataset_diagnostics': {'label_coverage': {'known': 2, 'attempted': 10,
                                                                   'known_fraction': .2}}}, NOW, path)
    text = shortlab.report('learning', path)
    assert '2 из 10' in text and '20.0%' in text and '≥80%' in text
    feed = shortlab.report('feed', path)
    assert '&lt;database error&gt;' in feed and 'old model' in feed
