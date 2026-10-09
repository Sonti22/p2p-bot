"""Chronological research on dated, recorded markets. No synthetic historical liquidity."""
from contextlib import closing, contextmanager
from itertools import groupby
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zlib

import shorts


def decode(raw):
    return json.loads(zlib.decompress(raw) if isinstance(raw, bytes) else raw)


def candle_exit(stop, target, high, low):
    """Conservative diagnostic only; never fabricates an executable orderbook."""
    if shorts.dec(high) >= shorts.dec(stop):
        return 'stop'
    if shorts.dec(low) <= shorts.dec(target):
        return 'target'
    return None


@contextmanager
def _source(path=None):
    """Research never creates or changes the collector's database."""
    con = sqlite3.connect(Path(path or shorts.DB_PATH).resolve().as_uri() + '?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def _coverage(con):
    state = shorts.meta(con)
    row = con.execute('SELECT MIN(ts),MAX(ts),COUNT(*) FROM snapshots').fetchone()
    catalogs = con.execute('SELECT COUNT(*) FROM catalog').fetchone()[0]
    return {'start': row[0], 'end': row[1], 'snapshots': row[2], 'catalogs': catalogs,
            'days': (row[1]-row[0])/86400 if row[0] is not None else 0,
            'capital': state['initial'] if state else None}


def coverage(path=None):
    if not Path(path or shorts.DB_PATH).is_file():
        return {'start': None, 'end': None, 'snapshots': 0, 'catalogs': 0, 'days': 0, 'capital': None}
    with _source(path) as con:
        return _coverage(con)


def summary(path=None):
    c = coverage(path)
    return ('📚 <b>Исследование шортов</b>\n\n'
            f"Наблюдений: {c['snapshots']}\nДней независимой истории: требуется проверка пропусков\n"
            f"Диапазон записанных дат: {c['days']:.2f} дней\n"
            f"Снимков каталога: {c['catalogs']}\n\n"
            'Порог анализа: 90 дней наблюдений и минимум 50 исполненных сделок в финальных 20% истории.\n'
            'Хронологическое разделение: 60% наблюдение, 20% проверка гипотез, 20% итоговая проверка.\n'
            'Накопленные данные позволяют воспроизводимый реплей. Недостающие стаканы, снятые монеты и интервалы связи не восстанавливаются.\n'
            'Гипотезы: возврат к EMA20, пробой минимума, разворот с верхней тенью.\n'
            'Новые гипотезы проверяются отдельно; рабочие правила не переключаются автоматически.\n'
            'Свечной результат не подтверждает доходность реального исполнения. /shorts research')


VARIANTS = ('strategy', 'random', 'flat', 'no_filters', 'fees_x2', 'worse_execution', 'breakdown', 'wick_reversal')


def _history_quality(con):
    first, last, dated, legacy, corrupt = None, None, 0, 0, 0
    for row in con.execute('SELECT ts,state FROM snapshots ORDER BY ts'):
        try:
            raw = decode(row['state'])
        except (ValueError, TypeError, zlib.error):
            corrupt += 1
            continue
        if not isinstance(raw, dict) or raw.get('observed_at') != row['ts']:
            legacy += 1
            continue
        dated += 1
        first = row['ts'] if first is None else first
        last = row['ts']
    return {'dated_observations': dated, 'legacy_observations': legacy,
            'corrupt_observations': corrupt, 'verified_start': first, 'verified_end': last,
            'verified_days': (last-first)/86400 if first is not None else 0}


def _variant_market(raw, variant):
    # The simulator only reads markets. Copy changed containers, rather than copying all candle history eight times.
    m = dict(raw)
    if variant == 'fees_x2':
        m['fee'] = str(shorts.dec(m['fee']) * 2)
    elif variant == 'worse_execution':
        m['bids'] = [[str(shorts.dec(p) * shorts.D('.997')), q] for p, q in m['bids']]
        m['asks'] = [[str(shorts.dec(p) * shorts.D('1.003')), q] for p, q in m['asks']]
    return m


def _variant_result(db, quality):
    s = shorts.status(db)
    closed = [p for p in s['positions'] if p['stage'] == 'closed' and p.get('opened')]
    pnl = [shorts.dec(p['pnl']) for p in closed]
    with _source(db) as con:
        missing = con.execute("SELECT COUNT(*) FROM events WHERE kind IN ('data_gap','unobserved_interval')").fetchone()[0]
    return {'trades': len(closed), 'realized': s['realized'], 'equity': s['equity'],
            'drawdown': s['drawdown'], 'worst_result': str(min(pnl, default=0)),
            'stress_losses': sum(bool(p.get('stress')) for p in closed),
            'unverified_stress': sum(bool(p.get('stress')) for p in s['positions']),
            'open_positions': sum(p['stage'] != 'closed' for p in s['positions']),
            'missing_intervals': missing,
            'trades_by_signal': {p['symbol']+':'+str(p['signal_ts']): p['pnl'] for p in closed},
            'rejections': s['rejections'], **quality,
            'unverified_positions': sum(bool(p.get('gap') or p.get('funding_unverified')) for p in s['positions'])}


def _replay(con, original, start, end, directory, include_end=False):
    """One ordered scan, one dated catalog cursor, one tick per observation timestamp."""
    os.makedirs(directory)
    dbs = {name: os.path.join(directory, name + '.db') for name in VARIANTS}
    for db in dbs.values():
        shorts.initialize(original['rub_rate'], original['rate_source'], start, db)
    catalogs = iter(con.execute('SELECT ts,state FROM catalog ORDER BY ts'))
    next_catalog, known = next(catalogs, None), None
    comparison = '<=' if include_end else '<'
    rows = con.execute('SELECT ts,symbol,state FROM snapshots WHERE ts>=? AND ts' + comparison + '? ORDER BY ts,symbol', (start, end))
    markets = {}
    quality = {'invalid_universe_observations': 0, 'legacy_observations': 0,
               'corrupt_observations': 0, 'corrupt_catalogs': 0,
               'observation_batches': 0, 'observations': 0}
    for stamp, batch in groupby(rows, key=lambda row: float(row['ts'])):
        while next_catalog is not None and next_catalog['ts'] <= stamp:
            try:
                known = decode(next_catalog['state'])
                if not isinstance(known, dict) or not isinstance(known.get('instruments'), dict):
                    raise ValueError('неверный каталог')
            except (ValueError, TypeError, zlib.error):
                known = None
                quality['corrupt_catalogs'] += 1
            next_catalog = next(catalogs, None)
        for row in batch:
            sym = row['symbol']
            try:
                raw = decode(row['state'])
            except (ValueError, TypeError, zlib.error):
                quality['corrupt_observations'] += 1
                markets.pop(sym, None)
                continue
            if not isinstance(raw, dict) or raw.get('observed_at') != stamp:
                quality['legacy_observations'] += 1
                markets.pop(sym, None)
                continue
            if known is None or sym not in known.get('instruments', {}):
                quality['invalid_universe_observations'] += 1
                markets.pop(sym, None)
                continue
            markets[sym] = raw
            quality['observations'] += 1
        # A catalog removal must also invalidate a cached entry candidate without borrowing a later catalog.
        batch_markets = {}
        for sym, raw in list(markets.items()):
            if stamp - raw['ticker_ts'] > 10:
                # An obsolete quote cannot manage positions or justify an entry; do not retain its candle arrays.
                markets.pop(sym)
                continue
            inst = known.get('instruments', {}).get(sym) if known else None
            m = dict(raw)
            m['candidate'] = bool(inst and raw.get('candidate') and shorts.eligible(inst, raw['terms']['ticker'], stamp))
            batch_markets[sym] = m
        quality['observation_batches'] += 1
        for variant, db in dbs.items():
            prepared = {sym: _variant_market(m, variant) for sym, m in batch_markets.items()}
            shorts.tick(prepared, stamp, db, allow_entries=variant != 'flat',
                        filters=variant != 'no_filters', random_entry=variant == 'random',
                        strategy=variant if variant in ('breakdown', 'wick_reversal') else 'ema_retest')
    return {name: _variant_result(db, quality) for name, db in dbs.items()}


def evaluate(path=None, minimum_days=90):
    c = coverage(path)
    if c['days'] < minimum_days or not c['catalogs'] or c['capital'] is None or not c['snapshots']:
        return {'ready': False, 'reason': 'недостаточно независимой истории и датированных каталогов', 'coverage': c}
    # Back up from a read-only connection: a stable replay never holds a long read lock on the live journal.
    with tempfile.TemporaryDirectory() as temp:
        frozen = os.path.join(temp, 'source.db')
        with _source(path) as live, closing(sqlite3.connect(frozen)) as destination:
            live.backup(destination)
        with _source(frozen) as con:
            c, original = _coverage(con), shorts.meta(con)
            c.update(_history_quality(con))
            if (c['days'] < minimum_days or original.get('version') != shorts.VERSION
                    or original.get('policy') != shorts.POLICY):
                return {'ready': False, 'reason': 'история или версия правил не совместима с текущим реплеем', 'coverage': c}
            boundary = c['start'] + (c['end']-c['start']) * .6
            final = c['start'] + (c['end']-c['start']) * .8
            result = {'ready': False, 'coverage': c, 'split': boundary, 'version': shorts.VERSION,
                      'training': [c['start'], boundary], 'validation': [boundary, final],
                      'verification': [final, c['end']], 'untouched_test': [final, c['end']],
                      'training_role': 'наблюдение фиксированных гипотез; параметры не подбираются',
                      'initial_capital': original['initial'], 'frozen_policy': original['policy'],
                      'rate': original['rub_rate'], 'rate_source': original['rate_source'],
                      'real_money_ready': False,
                      'limitations': ['Реплей использует только записанные стаканы и датированные каталоги.',
                                      'Свечной результат и положительная прибыль не подтверждают реальное исполнение.',
                                      'Гипотезы фиксируются до реплея; итоговая часть не выбирает и не переключает рабочие правила.']}
            result['validation_variants'] = _replay(con, original, boundary, final, os.path.join(temp, 'validation'))
            result['variants'] = _replay(con, original, final, c['end'], os.path.join(temp, 'test'), include_end=True)
    baseline = result['variants']['strategy']
    result['ready'] = (c['verified_days'] >= minimum_days
                       and baseline['trades'] >= 50 and baseline['unverified_positions'] == 0
                       and baseline['open_positions'] == 0 and baseline['missing_intervals'] == 0
                       and baseline['invalid_universe_observations'] == 0
                       and baseline['legacy_observations'] == baseline['corrupt_observations'] == baseline['corrupt_catalogs'] == 0
                       and all(result['variants'][name]['unverified_stress'] == 0
                               for name in ('strategy', 'fees_x2', 'worse_execution')))
    result['evidence_ready'] = result['ready']
    result['reason'] = ('достаточно записанных исходов для сценарного сравнения; не разрешение на реальные деньги'
                        if result['ready'] else 'недостаточно завершённых подтверждённых сделок в итоговой части')
    result['filter_effect'] = {'blocked': result['variants']['strategy']['rejections'],
                              'with_filters': result['variants']['strategy'],
                              'without_filters': result['variants']['no_filters']}
    excluded = {key:pnl for key,pnl in result['variants']['no_filters']['trades_by_signal'].items()
                if key not in result['variants']['strategy']['trades_by_signal']}
    result['filter_effect'].update(avoided_losing_signals=sum(shorts.dec(pnl)<0 for pnl in excluded.values()),
                                  missed_profitable_signals=sum(shorts.dec(pnl)>0 for pnl in excluded.values()),
                                  attribution='paired signals; portfolio interactions are not causal proof')
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=shorts.DB_PATH)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with open(args.output, 'w', encoding='utf-8') as out:
        json.dump(evaluate(args.db), out, ensure_ascii=False, indent=2)
