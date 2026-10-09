"""Chronological research on dated, recorded markets. No synthetic historical liquidity."""
import copy
import json
import os
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


def coverage(path=None):
    with shorts.connect(path) as con:
        state = shorts.meta(con)
        row = con.execute('SELECT MIN(ts),MAX(ts),COUNT(*) FROM snapshots').fetchone()
        catalogs = con.execute('SELECT COUNT(*) FROM catalog').fetchone()[0]
        return {'start': row[0], 'end': row[1], 'snapshots': row[2], 'catalogs': catalogs,
                'days': (row[1]-row[0])/86400 if row[0] is not None else 0,
                'capital': state['initial'] if state else None}


def summary(path=None):
    c = coverage(path)
    return ('📚 <b>Исследование шортов</b>\n\n'
            f"Наблюдений: {c['snapshots']}\nДней независимой истории: {c['days']:.2f}\n"
            f"Снимков каталога: {c['catalogs']}\n\n"
            'Порог анализа: 90 дней наблюдений и минимум 50 исполненных сделок в проверочной части.\n'
            'Накопленные данные позволяют воспроизводимый реплей. Недостающие стаканы, снятые монеты и интервалы связи не восстанавливаются.\n'
            'Свечной результат не подтверждает доходность реального исполнения. /shorts research')


def evaluate(path=None, minimum_days=90):
    c = coverage(path)
    if c['days'] < minimum_days or not c['catalogs']:
        return {'ready': False, 'reason': 'недостаточно независимой истории и датированных каталогов', 'coverage': c}
    with shorts.connect(path) as con:
        original = shorts.meta(con)
        observations = [(float(r['ts']), r['symbol'], decode(r['state'])) for r in
                        con.execute('SELECT * FROM snapshots ORDER BY ts,symbol')]
        catalogs = [(float(r['ts']), decode(r['state'])) for r in con.execute('SELECT * FROM catalog ORDER BY ts')]
    # Parameters are fixed before testing; training only records the baseline, never selects test winners.
    boundary = c['start'] + (c['end']-c['start'])*.6
    result = {'ready': False, 'coverage': c, 'split': boundary, 'version': shorts.VERSION,
              'training': [c['start'], boundary], 'verification': [boundary, c['end']], 'variants': {}}
    with tempfile.TemporaryDirectory() as temp:
        for variant in ('strategy', 'random', 'flat', 'no_filters', 'fees_x2', 'worse_execution'):
            db = os.path.join(temp, variant + '.db')
            shorts.initialize(original['rub_rate'], original['rate_source'], boundary, db)
            markets, invalid = {}, 0
            for ts, sym, raw in observations:
                if ts < boundary:
                    continue
                m = copy.deepcopy(raw)
                known = [catalog for at, catalog in catalogs if at <= ts]
                if not known or sym not in known[-1]['instruments']:
                    invalid += 1
                    continue
                inst = known[-1]['instruments'][sym]
                m['candidate'] = m['candidate'] and shorts.eligible(inst, m['terms']['ticker'], ts)
                if variant == 'fees_x2':
                    m['fee'] = str(shorts.dec(m['fee'])*2)
                if variant == 'worse_execution':
                    m['bids'] = [[str(shorts.dec(p)*shorts.D('.997')), q] for p, q in m['bids']]
                    m['asks'] = [[str(shorts.dec(p)*shorts.D('1.003')), q] for p, q in m['asks']]
                markets[sym] = m
                shorts.tick(markets, ts, db, allow_entries=variant != 'flat',
                            filters=variant != 'no_filters', random_entry=variant == 'random')
            s = shorts.status(db)
            closed = [p for p in s['positions'] if p['stage'] == 'closed' and p.get('opened')]
            pnl = [shorts.dec(p['pnl']) for p in closed]
            result['variants'][variant] = {'trades': len(closed), 'realized': s['realized'],
                 'equity': s['equity'], 'drawdown': s['drawdown'], 'worst_trade': str(min(pnl, default=0)),
                 'stress_losses': sum(bool(p.get('stress')) for p in closed),
                 'trades_by_signal': {p['symbol']+':'+str(p['signal_ts']):p['pnl'] for p in closed},
                 'rejections': s['rejections'], 'invalid_universe_observations': invalid,
                 'unverified_positions': sum(bool(p.get('gap') or p.get('funding_unverified')) for p in s['positions'])}
    result['ready'] = (result['variants']['strategy']['trades'] >= 50
                       and result['variants']['strategy']['unverified_positions'] == 0
                       and result['variants']['strategy']['invalid_universe_observations'] == 0)
    result['reason'] = 'сценарное сравнение; не разрешение на реальные деньги' if result['ready'] else 'недостаточно проверочных сделок'
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
