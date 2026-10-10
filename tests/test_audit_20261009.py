import io
import json
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest
import backup
import bankcatalog
import health_http as health
import p2p
import portfolio
import shorts
import shortresearch
import strategyreview
from helpers import arun
from test_shorts import market, NOW
from test_shorts import open_position, position, assert_capital
import shortmarket


def test_health_requires_recent_real_scan(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(health.time, 'time', lambda: clock[0])
    health.start_metrics()
    assert arun(health.handle_health(None)).status == 503
    health.update_scan_status(2, {'Bybit': 'private error'})
    clock[0] += 10
    health.set_metric('paper_balance', 0)
    assert arun(health.handle_health(None)).status == 200
    assert health.get_status()['uptime_seconds'] == 10
    assert health.get_status()['paper_balance'] == '0'
    assert 'private error' not in json.dumps(health.get_status())
    clock[0] += 301
    assert arun(health.handle_health(None)).status == 503


def test_health_auth_header_and_safe_logging(caplog):
    req = SimpleNamespace(app={'password': 'secret'}, headers={}, method='GET',
                          path='/status', query={'password': 'secret'})
    assert arun(health.protect(req, health.handle_status)).status == 401
    req.headers['Authorization'] = 'Bearer secret'
    assert arun(health.protect(req, health.handle_status)).status == 200
    assert 'secret' not in caplog.text


def test_public_health_requires_auth(monkeypatch):
    monkeypatch.setenv('HEALTH_CHECK_HOST', '0.0.0.0')
    monkeypatch.delenv('HEALTH_CHECK_PASSWORD', raising=False)
    with pytest.raises(ValueError, match='password'):
        arun(health.init_app(8080))


def test_missing_portfolio_research(tmp_path):
    report = strategyreview.p2p_evidence(portfolio.summary(str(tmp_path / 'missing.db')))
    assert report is not None


def test_backup_preserves_new_ledgers(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, '_state', {'tried': 0})
    monkeypatch.setenv('BACKUP_KEEP', '7')
    names = ['alt_shorts.db', 'paper_scenario_fast.db', 'paper_scenario_base.db', 'paper_scenario_stress.db',
             'sim_maker.db', 'sim_funding.db', 'sim_directional.db']
    for name in names:
        with sqlite3.connect(tmp_path / name) as con:
            con.execute('CREATE TABLE ledger(amount TEXT)')
            con.execute("INSERT INTO ledger VALUES('50000')")
    dest, done = backup.run(1791504001, str(tmp_path))
    assert set(names) <= set(done)
    for name in names:
        with sqlite3.connect(str(dest) + '/' + name) as con:
            assert con.execute('SELECT amount FROM ledger').fetchone()[0] == '50000'


def test_bestchange_zip_bomb_rejected_before_read(monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('bm_rates.dat', 'a' * 1000)
    monkeypatch.setattr(p2p, 'BC_MAX_UNPACKED', 500)
    with pytest.raises(ValueError, match='unpacked size'):
        p2p._bc_parse(stream.getvalue())


def test_short_snapshot_uses_observation_time(tmp_path):
    path = str(tmp_path / 'shorts.db')
    shorts.initialize(50, 'fixture', NOW, path)
    shorts.tick({'ALTUSDT': market()}, NOW + 1, path, allow_entries=False)
    with shorts.connect(path) as con:
        row = con.execute('SELECT ts,state FROM snapshots').fetchone()
    assert row['ts'] == NOW + 1
    assert shortresearch.decode(row['state'])['observed_at'] == NOW + 1


def test_repeated_market_is_not_recorded_as_new_observation(tmp_path):
    path = str(tmp_path / 'shorts.db')
    shorts.initialize(50, 'fixture', NOW, path)
    for at in (NOW + 1, NOW + 2):
        shorts.tick({'ALTUSDT': market()}, at, path, allow_entries=False)
    with shorts.connect(path) as con:
        assert con.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 1


def test_clock_failure_records_gap_without_changing_capital(tmp_path, monkeypatch):
    path = str(tmp_path / 'shorts.db')
    shorts.initialize(50, 'fixture', NOW, path)
    open_position(path)
    before = shorts.status(path)['cash']
    collector = shortmarket.Collector(path)
    async def failed(session):
        raise OSError('fixture offline')
    monkeypatch.setattr(collector, 'calibrate', failed)
    monkeypatch.setattr(collector, 'now', lambda: NOW + 3)
    assert arun(collector.refresh(object())) == {}
    assert position(path)['gap']
    assert shorts.status(path)['cash'] == before
    assert_capital(path)


def test_business_acquiring_limit_not_applied_to_personal_card():
    product = next(p for p in bankcatalog.PRODUCTS if p['id'] == 'alfa-debit')
    assert product['per_day'] is None
    assert product['channels']['sbp']['per_day'] is None
