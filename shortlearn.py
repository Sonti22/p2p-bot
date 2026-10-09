"""Versioned, offline-only learning for independent virtual alt shorts.

Public contract: ``build_dataset`` reads recorded markets without changing the
ledger; ``fit`` returns an artifact, including an honest untrained artifact when
history is insufficient; ``predict`` explains a shadow score. ``save``/``load``
persist bounded, validated JSON atomically. No score changes execution or risk.

The target is a fully priced, fixed-horizon short of a small fixed notional,
NOT the profit of the stop/target strategy or a promise of exchange execution.
Only new, recorded fresh books are used after the one-second execution delay.
Partial fills, missing coverage, unknown charges and exhausted isolated capital
are unknown targets rather than convenient zeroes or fabricated liquidity.
"""
from bisect import bisect_right
from collections import Counter
from decimal import ROUND_FLOOR
import hashlib
import html
import json
import math
import os
import sqlite3
import statistics
import tempfile
import time
import zlib

import shorts

VERSION = 'alt-shadow-logit-v1'
FEATURE_VERSION = 'observed-reversal-v1'
LABEL_VERSION = 'fresh-book-fixed-horizon-v1'
FEATURES = ('growth_24h', 'hour_volume_ratio', 'ema_distance_atr', 'atr_pct',
            'body_atr', 'upper_wick_atr', 'mark_return_60s', 'mark_index_basis',
            'funding_rate', 'spread', 'log_turnover')
DEFAULT_PATH = os.path.join(os.path.dirname(__file__), 'data', 'short_learning_model.json')
MAX_ARTIFACT_BYTES = 262144
MAX_RECORD_BYTES = 1048576
MAX_CATALOG_BYTES = 8388608
MAX_OBSERVATIONS = 200000
MAX_SAMPLES = 5000
MAX_CATALOGS = 10000
READ_PAGE = 128
LEGACY_READ_LIMIT = 5000
DEFAULT_REQUIREMENTS = {'minimum_days': 90, 'minimum_train': 100,
                        'minimum_validation': 40, 'minimum_test': 50,
                        'minimum_class': 10, 'minimum_test_selections': 20,
                        'minimum_known_coverage': .8}
LABEL_NAMES = {'growth_24h': 'рост за 24ч', 'hour_volume_ratio': 'всплеск объёма',
               'ema_distance_atr': 'отклонение от EMA', 'atr_pct': 'волатильность ATR',
               'body_atr': 'тело свечи', 'upper_wick_atr': 'верхняя тень',
               'mark_return_60s': 'ускорение mark', 'mark_index_basis': 'mark/index',
               'funding_rate': 'финансирование', 'spread': 'спред',
               'log_turnover': 'оборот'}


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a measurement')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('non-finite measurement')
    return value


def _decode(raw, maximum=MAX_RECORD_BYTES):
    if isinstance(raw, bytes):
        if len(raw) > maximum:
            raise ValueError('compressed record too large')
        decoder = zlib.decompressobj()
        raw = decoder.decompress(raw, maximum + 1)
        if len(raw) > maximum or not decoder.eof or decoder.unused_data:
            raise ValueError('invalid or oversized compressed record')
    elif not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise ValueError('invalid or oversized record')
    out = json.loads(raw)
    if not isinstance(out, dict):
        raise ValueError('record must be an object')
    return out


def _read_all(con, sql, parameters=()):
    """Materialize a bounded page and release its SQLite read lock before CPU work."""
    cursor = con.execute(sql, parameters)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _catalog_at(con, now, cutoff):
    rows = _read_all(con, 'SELECT ts,state FROM catalog WHERE rowid<=? AND ts<=? ORDER BY ts DESC LIMIT 1', (cutoff, now))
    return rows[0] if rows else None


def _has_chronology_index(con):
    for name, in _read_all(con, "SELECT name FROM pragma_index_list('snapshots')"):
        columns = _read_all(con, 'SELECT name FROM pragma_index_info(?) ORDER BY seqno', (name,))
        if [row[0] for row in columns[:2]] == ['ts', 'symbol']:
            return True
    return False


def _snapshot_pages(con, cutoff, end, count, chronology_index=True):
    """Frozen append-only row population, paged without a long read transaction."""
    if not chronology_index:
        # Old ledgers remain read only. A bounded prefix of immutable insertion
        # IDs is sorted once, then fetched by primary rowid, never prefix-resorted.
        keys = _read_all(con, 'SELECT rowid,symbol,ts FROM snapshots WHERE rowid<=? AND ts<=? '
                         'ORDER BY rowid LIMIT ?', (cutoff, end, min(count, LEGACY_READ_LIMIT+1)))
        keys.sort(key=lambda row: (row[2], row[1]))
        for offset in range(0, len(keys), READ_PAGE):
            group = keys[offset:offset+READ_PAGE]
            ids = [row[0] for row in group]
            placeholders = ','.join('?' for _ in ids)
            fetched = _read_all(con, 'SELECT rowid,symbol,ts,state FROM snapshots WHERE rowid IN ('+placeholders+')', ids)
            by_id = {row[0]: row[1:] for row in fetched}
            for ident, symbol, ts in group:
                if ident in by_id:
                    yield by_id[ident]
        return
    last_ts, last_symbol, remaining = -math.inf, '', count
    while remaining:
        page = _read_all(con, 'SELECT symbol,ts,state FROM snapshots WHERE rowid<=? AND ts<=? '
            'AND (ts,symbol)>(?,?) ORDER BY ts,symbol LIMIT ?',
            (cutoff, end, last_ts, last_symbol, min(READ_PAGE, remaining)))
        if not page:
            return
        for row in page:
            # The cursor is already closed: learning cannot hold up a DELETE-mode commit.
            yield row
        remaining -= len(page)
        last_symbol, last_ts = page[-1][0], page[-1][1]


def extract_features(market, now):
    """Return deterministic observed features or raise ValueError.

    Open candles and future mark samples are ignored. A future observation,
    stale ticker/book, discontinuous history or missing tariff is refused.
    Indicators describe history; they never guarantee protection from a pump.
    """
    try:
        now = _number(now)
        if market.get('observed_at') is not None and _number(market['observed_at']) > now:
            raise ValueError('future observation')
        if not market.get('active') or market.get('data_errors'):
            raise ValueError('inactive or incomplete market')
        if not 0 <= now - _number(market['ticker_ts']) <= 10 or not shorts.book_fresh(market, now):
            raise ValueError('stale market')
        if not market.get('fee_source') or not 0 <= shorts.dec(market['fee']) < 1:
            raise ValueError('unknown fee')
        bars = shorts.contiguous(market['bars'], 900, now, 35)
        hours = shorts.contiguous(market['hours'], 3600, now, 21)
        minutes = shorts.contiguous(market['minutes'], 60, now, 3)
        if not bars or not hours or not minutes:
            raise ValueError('missing continuous closed candles')
        samples = [s for s in market['mark_samples'] if _number(s[0]) <= now]
        old = [s for s in samples if 60 <= now - _number(s[0]) <= 65]
        if not old or not samples or now - _number(samples[-1][0]) > 10:
            raise ValueError('missing mark window')
        if any(b[0] <= a[0] or b[0] - a[0] > 3 for a, b in zip(samples, samples[1:])):
            raise ValueError('discontinuous mark window')
        a = shorts.atr(bars)
        median = statistics.median(shorts.dec(b[5]) for b in hours[-21:-1])
        b = bars[-1]
        mark, index = shorts.dec(market['mark']), shorts.dec(market['index'])
        bid, ask = shorts.dec(market['bids'][0][0]), shorts.dec(market['asks'][0][0])
        turnover = shorts.dec(market['terms']['ticker']['turnover24h'])
        if min(a, median, mark, index, bid, turnover) <= 0 or ask < bid:
            raise ValueError('invalid positive market inputs')
        features = {
            'growth_24h': shorts.dec(market['growth']),
            'hour_volume_ratio': shorts.dec(hours[-1][5]) / median,
            'ema_distance_atr': (shorts.dec(b[4]) - shorts.ema([v[4] for v in bars])[-1]) / a,
            'atr_pct': a / mark, 'body_atr': (shorts.dec(b[4]) - shorts.dec(b[1])) / a,
            'upper_wick_atr': (shorts.dec(b[2]) - max(shorts.dec(b[1]), shorts.dec(b[4]))) / a,
            'mark_return_60s': mark / shorts.dec(old[-1][1]) - 1,
            'mark_index_basis': mark / index - 1,
            'funding_rate': shorts.dec(market['funding_rate']),
            'spread': (ask - bid) / bid, 'log_turnover': math.log1p(float(turnover))}
        return {key: _number(features[key]) for key in FEATURES}
    except (KeyError, TypeError, ArithmeticError, IndexError) as exc:
        raise ValueError('incomplete observed features') from exc


def _unknown(sample, reason, now=None):
    sample['row'].update(label=None, label_status='unknown', reason=reason,
                         outcome_at=now, net_return=None)
    return sample['row']


def _check_book(market):
    for side, descending in (('bids', True), ('asks', False)):
        levels = market[side]
        if not isinstance(levels, list) or not 1 <= len(levels) <= 1000:
            raise ValueError('invalid displayed depth')
        prices = []
        for level in levels:
            if len(level) != 2 or shorts.dec(level[0]) <= 0 or shorts.dec(level[1]) < 0:
                raise ValueError('invalid displayed level')
            prices.append(shorts.dec(level[0]))
        if any((a <= b if descending else a >= b) for a, b in zip(prices, prices[1:])):
            raise ValueError('unsorted displayed depth')
    if shorts.dec(market['asks'][0][0]) < shorts.dec(market['bids'][0][0]):
        raise ValueError('crossed displayed book')


def _new_sample(m, now, features, horizon, notional):
    _check_book(m)
    step = shorts.dec(m['step'])
    if step <= 0:
        raise ValueError('invalid quantity step')
    qty = (notional / shorts.dec(m['bids'][0][0]) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if step <= 0 or qty < shorts.dec(m['min_qty']) or qty > shorts.dec(m['max_qty']) or qty * shorts.dec(m['bids'][0][0]) < shorts.dec(m['min_notional']):
        raise ValueError('notional outside instrument requirements')
    return {'row': {'symbol': m['symbol'], 'ts': now, 'features': features,
                    'feature_version': FEATURE_VERSION, 'label_version': LABEL_VERSION,
                    'horizon': horizon, 'label': None, 'label_status': 'pending'},
            'qty': qty, 'step': step, 'last': now, 'decision_book': str(m['book_id']),
            'entry': None, 'funding': {}, 'max_mark': None}


def _advance(sample, m, now, max_gap):
    """Consume one subsequent recorded observation; never fills on decision book."""
    try:
        row = sample['row']
        previous_at = sample['last']
        if now - sample['last'] > max_gap:
            return _unknown(sample, 'unobserved_interval', now)
        sample['last'] = now
        if m.get('observed_at') != now or not 0 <= now - _number(m['ticker_ts']) <= 10 or m.get('data_errors'):
            return _unknown(sample, 'unverified_market_interval', now)
        if not m.get('active'):
            return _unknown(sample, 'contract_inactive', now)
        if not m.get('fee_source') or not 0 <= shorts.dec(m['fee']) < 1:
            return _unknown(sample, 'unknown_fee', now)
        if shorts.dec(m['step']) != sample['step']:
            return _unknown(sample, 'instrument_step_changed', now)
        if sample['entry'] is None:
            if now > row['ts'] + 60:
                return _unknown(sample, 'entry_window_expired', now)
            if now < row['ts'] + 1 or not shorts.book_fresh(m, now) or m['book_ts'] < row['ts']+1 or str(m['book_id']) == sample['decision_book']:
                return None
            _check_book(m)
            qty, price = shorts.walk(m['bids'], sample['qty'], shorts.dec(m['step']))
            row['entry_filled_qty'] = str(qty)
            if qty != sample['qty']:
                return _unknown(sample, 'partial_entry', now)
            if qty < shorts.dec(m['min_qty']) or qty > shorts.dec(m['max_qty']) or qty*price < shorts.dec(m['min_notional']):
                return _unknown(sample, 'entry_requirements_changed', now)
            if (shorts.dec(m['bids'][0][0]) - price) / shorts.dec(m['bids'][0][0]) > shorts.D('.003'):
                return _unknown(sample, 'entry_slippage', now)
            interval = _number(m['funding_interval']) * 60
            next_at = _number(m['next_funding'])
            if interval <= 0 or next_at <= now:
                return _unknown(sample, 'unknown_funding_schedule', now)
            sample.update(entry=price, entry_at=now, entry_fee=qty*price*shorts.dec(m['fee']),
                          entry_book=str(m['book_id']), funding_interval=interval,
                          next_funding=next_at, exit_due=now+row['horizon']+1,
                          max_mark=shorts.dec(m['mark']))
            row.update(entry_at=now, qty=str(qty), entry_price=str(price), fee_source=m['fee_source'])
            return None
        if _number(m['funding_interval']) * 60 != sample['funding_interval']:
            return _unknown(sample, 'funding_schedule_changed', now)
        observed_marks = [s for s in m.get('mark_samples', []) if previous_at-3 <= _number(s[0]) <= now]
        if (not observed_marks or observed_marks[0][0] > previous_at+3 or now-observed_marks[-1][0] > 3
                or any(b[0] <= a[0] or b[0]-a[0] > 3 for a, b in zip(observed_marks, observed_marks[1:]))):
            return _unknown(sample, 'unobserved_mark_interval', now)
        sample['max_mark'] = max(sample['max_mark'], shorts.dec(m['mark']),
                                 max(shorts.dec(s[1]) for s in observed_marks if s[0] >= sample['entry_at']))
        # Funding is not inferred from the current predicted rate.
        for funding in m.get('funding_history', []):
            settled = _number(funding['ts'])
            if not sample['entry_at'] < settled <= now:
                continue
            if funding.get('mark') is None:
                return _unknown(sample, 'unknown_funding_price', now)
            recorded = (shorts.dec(funding['rate']), shorts.dec(funding['mark']))
            if recorded[1] <= 0:
                return _unknown(sample, 'invalid_funding_price', now)
            if settled in sample['funding'] and sample['funding'][settled] != recorded:
                return _unknown(sample, 'conflicting_funding', now)
            sample['funding'][settled] = recorded
        # A fixed-horizon return must not pretend an isolated 2x position survives bankruptcy.
        if sample['max_mark'] >= sample['entry'] * shorts.D('1.5'):
            return _unknown(sample, 'isolated_capital_exhaustion', now)
        tier = shorts.risk_tier(m.get('tiers', []), sample['qty']*shorts.dec(m['mark']))
        if tier is None or not 0 < shorts.dec(tier['maintenanceMargin']) < 1:
            return _unknown(sample, 'unknown_maintenance_margin', now)
        negative_funding = min(shorts.D(0), sum((sample['qty']*rate*mark for rate, mark in sample['funding'].values()), shorts.D(0)))
        balance = sample['qty']*sample['entry']/2 - sample['entry_fee'] + negative_funding + sample['qty']*(sample['entry']-shorts.dec(m['mark']))
        maintenance = max(shorts.D(0), sample['qty']*shorts.dec(m['mark'])*shorts.dec(tier['maintenanceMargin'])-shorts.dec(tier.get('mmDeduction') or 0))
        if balance <= maintenance + sample['qty']*shorts.dec(m['mark'])*shorts.dec(m['fee']):
            return _unknown(sample, 'possible_isolated_liquidation', now)
        if now < sample['exit_due']:
            return None
        if now > sample['exit_due'] + max_gap:
            return _unknown(sample, 'missing_exit_execution', now)
        if not shorts.book_fresh(m, now) or m['book_ts'] < sample['exit_due'] or str(m['book_id']) == sample['entry_book']:
            return None
        _check_book(m)
        qty, price = shorts.walk(m['asks'], sample['qty'], shorts.dec(m['step']))
        row['exit_filled_qty'] = str(qty)
        if qty != sample['qty']:
            return _unknown(sample, 'partial_exit', now)
        required, settled = [], sample['next_funding']
        while settled <= now:
            required.append(settled)
            if len(required) > 2000:
                return _unknown(sample, 'unbounded_funding_schedule', now)
            settled += sample['funding_interval']
        if required and (not m.get('funding_complete') or any(at not in sample['funding'] for at in required)):
            return _unknown(sample, 'unknown_funding_charge', now)
        if any(at not in required for at in sample['funding']):
            return _unknown(sample, 'inconsistent_funding_schedule', now)
        payment = sum((sample['qty']*rate*mark for rate, mark in sample['funding'].values()), shorts.D(0))
        exit_fee = qty*price*shorts.dec(m['fee'])
        fees = sample['entry_fee'] + exit_fee
        pnl = qty*(sample['entry']-price) - fees + payment
        net = pnl/(qty*sample['entry'])
        row.update(label=int(pnl > 0), label_status='valid', reason=None, outcome_at=now,
                   exit_price=str(price), fees=str(fees), funding=str(payment), pnl=str(pnl),
                   net_return=_number(net), max_adverse_return=_number(sample['max_mark']/sample['entry']-1),
                   funding_price_model='recorded mark; collector may use minute-open approximation',
                   coverage='continuous recorded observations; displayed book depth only')
        return row
    except (KeyError, ValueError, TypeError, ArithmeticError, IndexError) as exc:
        return _unknown(sample, 'invalid_outcome_inputs:' + type(exc).__name__, now)


def _windowed_rows(con, catalogs, start, end, horizon, limit, max_observations, notional, max_gap,
                   diagnostics, snapshot_cutoff, catalog_cutoff):
    """Predeclared spaced windows keep years of raw ticks bounded and unbiased by outcomes.

    Choose the first observable eligible symbol, in timestamp/symbol order, in
    each decision window. Only then read its future. First recorded books in
    eight-second buckets retain their ORIGINAL timestamps, prices and sizes.
    Gaps introduced by sparse coverage stay unknown. Continuous subsecond mark
    samples in each record detect an intervening spike; absent samples are not
    substituted. Window selection never depends on later profitability.
    """
    window_length = horizon+100
    bucket = min(8, int(max_gap))
    budget_per_window = math.ceil(window_length/bucket)+42
    windows = min(limit, max(1, max_observations//budget_per_window),
                  max(1, int((end-start)//window_length)))
    usable_span = max(0, end-start-window_length)
    points = [start+usable_span*i/max(1, windows-1) for i in range(windows)]
    times = [row[0] for row in catalogs] if catalogs is not None else []
    cached_catalog = None
    diagnostics.update(sampling='spaced_observed_windows_v1', planned_windows=windows,
                       source_start=start, source_end=end, bucket_seconds=bucket,
                       sampling_rule='first observed eligible symbol; no outcome selection')
    rows = []
    for point in points:
        sample = None
        candidates = _read_all(con, 'SELECT symbol,ts,state FROM snapshots WHERE rowid<=? AND ts>=? AND ts<=? ORDER BY ts,symbol LIMIT 40',
                               (snapshot_cutoff, point, min(end, point+30)))
        for symbol, raw_ts, raw in candidates:
            if diagnostics['observations'] >= max_observations:
                diagnostics['truncated'] = True
                return rows
            diagnostics['observations'] += 1
            try:
                now, m = _number(raw_ts), _decode(raw)
                if m.get('observed_at') != now or m.get('symbol') != symbol:
                    raise ValueError('unverified observation')
                if not m.get('candidate'):
                    continue
                if catalogs is None:
                    dated = _catalog_at(con, now, catalog_cutoff)
                    if dated is None:
                        raise ValueError('unknown historical universe')
                    if cached_catalog is None or cached_catalog[0] != dated[0]:
                        cached_catalog = (dated[0], _decode(dated[1], MAX_CATALOG_BYTES)['instruments'])
                    instruments = cached_catalog[1]
                else:
                    at = bisect_right(times, now)-1
                    instruments = catalogs[at][1] if at >= 0 else {}
                if symbol not in instruments or not shorts.eligible(instruments[symbol], m['terms']['ticker'], now):
                    raise ValueError('unknown or ineligible historical universe')
                sample = _new_sample(m, now, extract_features(m, now), horizon, notional)
                break
            except (ValueError, KeyError, TypeError, ArithmeticError, IndexError, zlib.error, UnicodeError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else 'invalid candidate record'
                counts = diagnostics['candidate_rejections']
                counts[reason] = counts.get(reason, 0)+1
        if sample is None:
            continue
        decision = sample['row']['ts']
        symbol = sample['row']['symbol']
        query = ('SELECT ts,state FROM snapshots WHERE symbol=? AND rowid<=? AND ts>? AND ts IN '
                 '(SELECT MIN(ts) FROM snapshots WHERE symbol=? AND rowid<=? AND ts>? AND ts<=? '
                 'GROUP BY CAST(ts / ? AS INTEGER)) ORDER BY ts LIMIT ?')
        finished = None
        last_ts = decision
        while finished is None:
            remaining = max_observations-diagnostics['observations']
            if remaining <= 0:
                diagnostics['truncated'] = True
                rows.append(_unknown(sample, 'bounded_read_limit'))
                return rows
            page = _read_all(con, query, (symbol, snapshot_cutoff, last_ts, symbol, snapshot_cutoff,
                             decision, min(end, decision+window_length), bucket, min(READ_PAGE, remaining)))
            if not page:
                break
            for raw_ts, raw in page:
                diagnostics['observations'] += 1
                try:
                    now, m = _number(raw_ts), _decode(raw)
                    if m.get('symbol') != symbol:
                        raise ValueError('inconsistent symbol')
                    finished = _advance(sample, m, now, max_gap)
                except (ValueError, zlib.error, UnicodeError):
                    diagnostics['invalid_records'] += 1
                    finished = _unknown(sample, 'invalid_record', raw_ts)
                if finished is not None:
                    break
            last_ts = page[-1][0]
        rows.append(finished or _unknown(sample, 'history_ends_before_outcome'))
    diagnostics['sampled_windows'] = len(rows)
    return rows


def build_dataset(path=None, horizon=3600, limit=MAX_SAMPLES, max_observations=MAX_OBSERVATIONS,
                  notional='10', max_gap=30):
    """Stream bounded, read-only dated observations into fixed-horizon labels.

    At most one pending sample per symbol. Catalog membership is resolved at
    decision time; today's universe is never substituted. Unknown samples are
    retained with a reason. ``truncated`` prevents claims of research readiness.
    Missing historical execution books cannot be reconstructed from candles.
    Large histories use predeclared spaced windows instead of the first hours
    of a LIMIT query; sampling counts and original time coverage are reported.
    A brief metadata capture freezes append-only rowid/time cutoffs; bounded
    pages release all read cursors before decoding or modelling. Late rows are
    excluded, including inserts with an old timestamp, without holding up the
    live ledger for the duration of a research run.
    Label books may be up to 30 seconds apart because market candidates are
    collected less often than mark samples. The embedded observed mark series
    must still cover every intervening interval with gaps at most 3 seconds.
    This allowance changes neither active execution nor stop/risk policies.
    """
    if not isinstance(horizon, int) or not 60 <= horizon <= 86400:
        raise ValueError('horizon must be 60..86400 seconds')
    if not isinstance(limit, int) or not 1 <= limit <= MAX_SAMPLES or not isinstance(max_observations, int) or not 1 <= max_observations <= MAX_OBSERVATIONS:
        raise ValueError('invalid bounded dataset size')
    notional = shorts.dec(notional)
    if not 0 < notional <= 100 or not 1 <= max_gap <= 60:
        raise ValueError('invalid label notional or coverage gap')
    diagnostics = {'observations': 0, 'catalogs': 0, 'invalid_records': 0,
                   'candidate_rejections': {}, 'unknown_labels': {}, 'truncated': False}
    rows, pending, cooldown = [], {}, {}
    path = path or shorts.DB_PATH
    if not os.path.isfile(path):
        return {'version': VERSION, 'rows': rows, 'diagnostics': diagnostics,
                'horizon': horizon, 'notional': str(notional)}
    con = sqlite3.connect('file:' + os.path.abspath(path).replace('\\', '/') + '?mode=ro', uri=True,
                          isolation_level=None)
    try:
        con.execute('BEGIN')
        source_start, source_end, source_count, snapshot_cutoff = _read_all(con,
            'SELECT MIN(ts),MAX(ts),COUNT(*),MAX(rowid) FROM snapshots')[0]
        catalog_count, catalog_cutoff = _read_all(con, 'SELECT COUNT(*),MAX(rowid) FROM catalog')[0]
        con.commit()  # Only metadata is captured transactionally; never decoding or fitting.
        diagnostics['source_observations'] = source_count
        diagnostics['catalogs'] = catalog_count
        diagnostics['source_cutoff'] = {'snapshot_rowid': snapshot_cutoff, 'catalog_rowid': catalog_cutoff,
                                        'end': source_end}
        chronology_index = _has_chronology_index(con)
        diagnostics['chronology_index'] = chronology_index
        if not chronology_index:
            max_observations = min(max_observations, LEGACY_READ_LIMIT)
            diagnostics['legacy_read_limit'] = max_observations
            diagnostics['legacy_read_rule'] = 'first append rows, sorted once; source remains read only'
        if chronology_index and source_count > max_observations and source_end-source_start >= 2*86400:
            # Only the dated catalogs actually used by spaced decision windows are decoded.
            # A frequent catalog refresh must not impose a false 70-day history ceiling.
            rows = _windowed_rows(con, None, source_start, source_end, horizon, limit,
                                  max_observations, notional, max_gap, diagnostics,
                                  snapshot_cutoff, catalog_cutoff)
            rows.sort(key=lambda row: (row['ts'], row['symbol']))
            diagnostics.update(valid_labels=sum(r['label_status'] == 'valid' for r in rows),
                unknown_labels=dict(Counter(r.get('reason') for r in rows if r['label_status'] != 'valid')),
                label_coverage=_label_coverage(rows), book_interval_limit=max_gap, mark_interval_limit=3)
            return {'version': VERSION, 'rows': rows, 'diagnostics': diagnostics,
                    'horizon': horizon, 'notional': str(notional), 'label_version': LABEL_VERSION,
                    'target': 'fully priced fixed-horizon shadow short; not the active stop/target strategy'}
        cached_catalog = None
        diagnostics['sampling'] = 'full_bounded_stream_v1'
        for symbol, raw_ts, raw in _snapshot_pages(con, snapshot_cutoff or 0, source_end or 0,
                                                max_observations+1, chronology_index):
            if diagnostics['observations'] >= max_observations:
                diagnostics['truncated'] = True
                break
            diagnostics['observations'] += 1
            try:
                now, m = _number(raw_ts), _decode(raw)
                if m.get('observed_at') != now or m.get('symbol') != symbol:
                    raise ValueError('unverified observation timestamp or symbol')
            except (ValueError, zlib.error, UnicodeError):
                diagnostics['invalid_records'] += 1
                if symbol in pending:
                    rows.append(_unknown(pending.pop(symbol), 'invalid_record', raw_ts))
                continue
            if symbol in pending:
                outcome = _advance(pending[symbol], m, now, max_gap)
                if outcome is not None:
                    rows.append(outcome)
                    pending.pop(symbol)
                    cooldown[symbol] = now
            if len(rows) + len(pending) >= limit:
                diagnostics['truncated'] = True
                continue
            if symbol in pending or not m.get('candidate') or now <= cooldown.get(symbol, -1):
                continue
            try:
                dated = _catalog_at(con, now, catalog_cutoff or 0)
                if dated is None:
                    raise ValueError('unknown historical universe')
                if cached_catalog is None or cached_catalog[0] != dated[0]:
                    cached_catalog = (dated[0], _decode(dated[1], MAX_CATALOG_BYTES)['instruments'])
                instruments = cached_catalog[1]
                if symbol not in instruments:
                    raise ValueError('unknown historical universe')
                if not shorts.eligible(instruments[symbol], m['terms']['ticker'], now):
                    raise ValueError('ineligible historical instrument')
                features = extract_features(m, now)
                pending[symbol] = _new_sample(m, now, features, horizon, notional)
            except (ValueError, KeyError, TypeError, ArithmeticError, IndexError, zlib.error, UnicodeError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else 'invalid instrument data'
                counts = diagnostics['candidate_rejections']
                counts[reason] = counts.get(reason, 0)+1
        for sample in pending.values():
            rows.append(_unknown(sample, 'history_ends_before_outcome'))
    finally:
        con.close()
    rows.sort(key=lambda row: (row['ts'], row['symbol']))
    diagnostics.update(valid_labels=sum(r['label_status'] == 'valid' for r in rows),
                       unknown_labels=dict(Counter(r.get('reason') for r in rows if r['label_status'] != 'valid')),
                       label_coverage=_label_coverage(rows), book_interval_limit=max_gap, mark_interval_limit=3)
    return {'version': VERSION, 'rows': rows, 'diagnostics': diagnostics,
            'horizon': horizon, 'notional': str(notional), 'label_version': LABEL_VERSION,
            'target': 'fully priced fixed-horizon shadow short; not the active stop/target strategy'}


def _sigmoid(value):
    return 1/(1+math.exp(-max(-40, min(40, value))))


def _score(model, values):
    z = [(values[k]-model['means'][i])/model['scales'][i] for i, k in enumerate(FEATURES)]
    terms = [w*x for w, x in zip(model['weights'], z)]
    return _sigmoid(model['intercept']+sum(terms)), terms


def _independent(rows):
    out, until = [], -math.inf
    for row in rows:
        # Global thinning also avoids pretending simultaneous coins are independent trials.
        if row['ts'] > until:
            out.append(row)
            until = row['outcome_at']
    return out


def _label_coverage(rows):
    known = sum(row.get('label_status') == 'valid' for row in rows)
    total = len(rows)
    return {'attempted': total, 'known': known, 'unknown': total-known,
            'known_fraction': known/total if total else 0,
            'unknown_fraction': (total-known)/total if total else None,
            'scope': 'feature-eligible label attempts; missing outcomes are not removed'}


def _metrics(model, rows, threshold, baseline_probability):
    scored = [(_score(model, row['features'])[0], row) for row in rows]
    eligible = [(p, row) for p, row in scored if all(lo <= row['features'][k] <= hi
                for k, (lo, hi) in zip(FEATURES, model['bounds']))]
    selected = [row for p, row in eligible if p >= threshold]
    n = len(rows)
    bins = []
    for lo in (0, .2, .4, .6, .8):
        bucket = [(p, r) for p, r in scored if lo <= p < lo+.2 or lo == .8 and p == 1]
        bins.append({'from': lo, 'to': lo+.2, 'count': len(bucket),
                     'mean_probability': statistics.mean(p for p, _ in bucket) if bucket else None,
                     'observed_positive_rate': statistics.mean(r['label'] for _, r in bucket) if bucket else None})
    returns = [r['net_return'] for r in selected]
    return {'count': n, 'positive': sum(r['label'] for r in rows),
            'eligible': len(eligible), 'abstained_outside_domain': n-len(eligible),
            'brier': statistics.mean((p-r['label'])**2 for p, r in scored) if n else None,
            'baseline_brier': statistics.mean((baseline_probability-r['label'])**2 for r in rows) if n else None,
            'selected': len(selected), 'mean_selected_return': statistics.mean(returns) if returns else None,
            'sum_selected_return': sum(returns),
            'baseline_all_mean_return': statistics.mean(r['net_return'] for r in rows) if n else None,
            'flat_return': 0, 'worst_selected_return': min(returns) if returns else None,
            'calibration': bins}


def fit(dataset, requirements=None, now=None):
    """Fit deterministic logistic regression with untouched chronological test.

    Features and standardization use training data only. A fixed threshold grid
    is selected on validation only. Training labels must finish before the
    following partition starts; an additional whole horizon is embargoed.
    Default evidence floors are conservative hypotheses, versioned here.
    ``ready`` means qualified for shadow research, never permission for money.
    """
    req = dict(DEFAULT_REQUIREMENTS)
    if requirements:
        if set(requirements) - set(req):
            raise ValueError('unknown evidence requirement')
        req.update(requirements)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in req.values()):
        raise ValueError('evidence requirements must be positive')
    if req['minimum_known_coverage'] > 1:
        raise ValueError('known outcome coverage must not exceed one')
    created = time.time() if now is None else _number(now)
    artifact = {'version': VERSION, 'feature_version': FEATURE_VERSION, 'label_version': LABEL_VERSION,
                'created_at': created, 'trained': False, 'ready': False, 'shadow_only': True,
                'live_permission': False, 'requirements': req, 'training_end': None,
                'reason': 'недостаточно независимой истории', 'model': None,
                'target': 'positive net fixed-horizon shadow return; not active strategy PnL',
                'dataset_diagnostics': dataset.get('diagnostics', {}), 'evaluation': {}}
    if dataset.get('version') != VERSION:
        artifact['reason'] = 'несовместимая версия набора данных'
        return artifact
    valid = []
    if not isinstance(dataset.get('rows', []), list) or len(dataset.get('rows', [])) > MAX_SAMPLES:
        artifact['reason'] = 'неверный или чрезмерный размер набора данных'
        return artifact
    coverage = _label_coverage(dataset.get('rows', []))
    artifact['evaluation'].update(label_coverage={'all': coverage},
                                   known_label_coverage=coverage['known_fraction'],
                                   unknown_label_ratio=coverage['unknown_fraction'])
    for row in dataset.get('rows', [])[:MAX_SAMPLES]:
        try:
            if row.get('label_status') != 'valid':
                continue
            if row.get('feature_version') != FEATURE_VERSION or row.get('label_version') != LABEL_VERSION:
                raise ValueError('incompatible row version')
            if row['label'] not in (0, 1) or isinstance(row['label'], bool):
                raise ValueError('invalid target')
            clean = dict(row, features={k: _number(row['features'][k]) for k in FEATURES},
                         ts=_number(row['ts']), outcome_at=_number(row['outcome_at']),
                         net_return=_number(row['net_return']))
            if clean['outcome_at'] <= clean['ts'] or clean['outcome_at'] > created:
                raise ValueError('invalid or unfinished future outcome')
            valid.append(clean)
        except (ValueError, KeyError, TypeError):
            artifact['reason'] = 'повреждённые или несовместимые метки'
            return artifact
    valid.sort(key=lambda row: (row['ts'], row['symbol']))
    independent = _independent(valid)
    artifact['evaluation'].update(valid_outcomes=len(valid), independent_outcomes=len(independent),
                                  overlapping_removed=len(valid)-len(independent))
    if coverage['known_fraction'] < req['minimum_known_coverage']:
        artifact['reason'] = 'недостаточная полнота исходов; неизвестные результаты нельзя исключать из оценки'
        return artifact
    if len(independent) < 3:
        return artifact
    start, end = independent[0]['ts'], independent[-1]['outcome_at']
    b1, b2 = start+(end-start)*.6, start+(end-start)*.8
    embargo = _number(dataset.get('horizon', 3600))
    train = [r for r in independent if r['outcome_at'] < b1-embargo]
    validation = [r for r in independent if r['ts'] >= b1 and r['outcome_at'] < b2-embargo]
    test = [r for r in independent if r['ts'] >= b2]
    splits = {'train': train, 'validation': validation, 'test': test}
    coverage_partitions = {'train': [], 'validation': [], 'test': []}
    for row in dataset['rows']:
        ts = _number(row['ts'])
        coverage_partitions['train' if ts < b1 else 'validation' if ts < b2 else 'test'].append(row)
    artifact['evaluation']['label_coverage'].update({name: _label_coverage(part)
        for name, part in coverage_partitions.items()})
    artifact['evaluation'].update(days=(end-start)/86400, embargo_seconds=embargo,
        partitions={name: {'count': len(part), 'start': part[0]['ts'] if part else None,
                          'end': part[-1]['outcome_at'] if part else None,
                          'positive': sum(r['label'] for r in part)} for name, part in splits.items()},
        embargoed=len(independent)-sum(map(len, splits.values())))
    artifact['training_end'] = train[-1]['outcome_at'] if train else None
    if any(_label_coverage(part)['known_fraction'] < req['minimum_known_coverage']
           for part in coverage_partitions.values()):
        artifact['reason'] = 'низкая полнота исходов в отдельном хронологическом периоде'
        return artifact
    if dataset.get('diagnostics', {}).get('truncated'):
        artifact['reason'] = 'ограниченный срез истории; обучение отложено'
        return artifact
    if (end-start)/86400 < req['minimum_days'] or any(len(splits[name]) < req[key] for name, key in
           (('train', 'minimum_train'), ('validation', 'minimum_validation'), ('test', 'minimum_test'))):
        return artifact
    if any(min(sum(r['label'] for r in part), len(part)-sum(r['label'] for r in part)) < req['minimum_class'] for part in splits.values()):
        artifact['reason'] = 'недостаточно прибыльных и убыточных независимых исходов'
        return artifact
    means = [statistics.mean(r['features'][k] for r in train) for k in FEATURES]
    scales = [max(1e-9, statistics.pstdev(r['features'][k] for r in train)) for k in FEATURES]
    bounds = [[min(r['features'][k] for r in train), max(r['features'][k] for r in train)] for k in FEATURES]
    base = statistics.mean(r['label'] for r in train)
    model = {'means': means, 'scales': scales, 'bounds': bounds,
             'weights': [0.0]*len(FEATURES), 'intercept': math.log(base/(1-base)),
             'training_samples': len(train), 'threshold': .5, 'regularization': .1}
    vectors = [[(r['features'][k]-means[i])/scales[i] for i, k in enumerate(FEATURES)] for r in train]
    for _ in range(240):
        errors = [_sigmoid(model['intercept']+sum(w*x for w, x in zip(model['weights'], z)))-row['label'] for z, row in zip(vectors, train)]
        model['intercept'] -= .05*statistics.mean(errors)
        model['weights'] = [w-.05*(statistics.mean(error*z[i] for error, z in zip(errors, vectors))+.1*w)
                            for i, w in enumerate(model['weights'])]
    # Threshold is frozen before evaluating the test set.
    choices = [(_metrics(model, validation, t, base), t) for t in (.5, .6, .7)]
    supported = [(metric, threshold) for metric, threshold in choices if metric['selected'] >= req['minimum_class']]
    if not supported:
        artifact['reason'] = 'недостаточная поддержка порога в валидации'
        return artifact
    _, threshold = max(supported, key=lambda pair: (pair[0]['mean_selected_return'], -pair[1]))
    model['threshold'] = threshold
    evaluation = {name: _metrics(model, part, threshold, base) for name, part in splits.items()}
    for name, part in splits.items():
        evaluation[name]['outside_training_domain'] = sum(any(not lo <= r['features'][k] <= hi for k, (lo, hi) in zip(FEATURES, bounds)) for r in part)
    artifact['evaluation'].update(evaluation)
    signature = json.dumps([(r['ts'], r['symbol'], r['features'], r['label'], r['net_return']) for r in train], sort_keys=True, separators=(',', ':'))
    artifact.update(trained=True, model=model, training_digest=hashlib.sha256(signature.encode()).hexdigest())
    t, v = evaluation['test'], evaluation['validation']
    artifact['ready'] = (t['selected'] >= req['minimum_test_selections'] and t['mean_selected_return'] > 0
                         and v['mean_selected_return'] > 0 and t['brier'] < t['baseline_brier']
                         and t['outside_training_domain'] == 0 and v['outside_training_domain'] == 0)
    artifact['reason'] = ('модель проверена только для теневых оценок' if artifact['ready'] else
                          'модель обучена; независимая проверка качества не пройдена')
    return artifact


def predict(artifact, market, now):
    """Shadow-only probability and signed log-odds contributions, never an order.

    ``risk_score`` is the complement of this model's positive-return probability;
    it is NOT a liquidation probability. Outside the observed training domain or
    without complete fresh inputs, return ``available=False`` and an explanation.
    """
    _validate(artifact)
    out = {'available': False, 'shadow_only': True, 'ready': artifact['ready'],
           'probability': None, 'risk_score': None, 'sample_support': 0,
           'reason': artifact['reason'], 'explanation': []}
    observation = _number(now)
    if market.get('observed_at') is not None:
        observation = min(observation, _number(market['observed_at']))
    test = artifact.get('evaluation', {}).get('partitions', {}).get('test', {})
    cutoffs = (artifact['created_at'], artifact.get('training_end'), test.get('end'),
               artifact.get('evaluation', {}).get('test', {}).get('end'))
    if any(cutoff is not None and _number(cutoff) > observation for cutoff in cutoffs):
        out['reason'] = 'модель или проверочные исходы ещё не были доступны в момент наблюдения'
        return out
    if not artifact['trained']:
        return out
    try:
        values = extract_features(market, now)
    except ValueError as exc:
        out['reason'] = str(exc)
        return out
    model = artifact['model']
    outside = [k for k, (lo, hi) in zip(FEATURES, model['bounds']) if not lo <= values[k] <= hi]
    if outside:
        out.update(reason='признаки вне обучающей области', outside_domain=outside)
        return out
    probability, terms = _score(model, values)
    explanation = [{'feature': k, 'name': LABEL_NAMES[k], 'value': values[k], 'log_odds': term}
                   for k, term in zip(FEATURES, terms)]
    explanation.sort(key=lambda row: (-abs(row['log_odds']), row['feature']))
    out.update(available=True, probability=probability, risk_score=1-probability,
               sample_support=model['training_samples'], explanation=explanation[:4],
               above_threshold=probability >= model['threshold'], target=artifact['target'],
               reason='теневая оценка; правила и лимиты исполнения не меняются')
    return out


def _validate(artifact):
    _json_shape(artifact)
    if not isinstance(artifact, dict) or artifact.get('version') != VERSION or artifact.get('feature_version') != FEATURE_VERSION or artifact.get('label_version') != LABEL_VERSION:
        raise ValueError('unsupported model artifact')
    if artifact.get('shadow_only') is not True or artifact.get('live_permission') is not False:
        raise ValueError('artifact must remain shadow only')
    for key in ('trained', 'ready'):
        if not isinstance(artifact.get(key), bool):
            raise ValueError('invalid model status')
    _number(artifact['created_at'])
    if not isinstance(artifact.get('reason'), str) or not isinstance(artifact.get('target'), str) or not isinstance(artifact.get('evaluation'), dict):
        raise ValueError('invalid model description')
    if artifact['ready'] and not artifact['trained']:
        raise ValueError('untrained artifact cannot be ready')
    if artifact['trained']:
        model = artifact.get('model')
        if not isinstance(model, dict) or not isinstance(model.get('training_samples'), int) or model['training_samples'] < 1:
            raise ValueError('invalid sample support')
        for key in ('means', 'scales', 'weights', 'bounds'):
            if not isinstance(model.get(key), list) or len(model[key]) != len(FEATURES):
                raise ValueError('invalid model dimensions')
        for value in model['means']+model['weights']:
            if abs(_number(value)) > 1e12:
                raise ValueError('model parameter out of bounds')
        if any(not 0 < _number(s) <= 1e12 for s in model['scales']):
            raise ValueError('invalid model scale')
        for bounds in model['bounds']:
            if not isinstance(bounds, list) or len(bounds) != 2 or not -1e12 <= _number(bounds[0]) <= _number(bounds[1]) <= 1e12:
                raise ValueError('invalid observed domain')
        if abs(_number(model['intercept'])) > 100 or not 0 < _number(model['threshold']) < 1:
            raise ValueError('invalid model score')
        if _number(artifact['training_end']) > artifact['created_at']:
            raise ValueError('future training cutoff')
    elif artifact.get('model') is not None:
        raise ValueError('untrained model must not contain coefficients')
    return artifact


def _json_shape(value, depth=0):
    if depth > 12:
        raise ValueError('artifact nesting limit')
    if isinstance(value, dict):
        if len(value) > 200 or any(not isinstance(k, str) or len(k) > 256 for k in value):
            raise ValueError('artifact object limit')
        for child in value.values():
            _json_shape(child, depth+1)
    elif isinstance(value, list):
        if len(value) > MAX_SAMPLES:
            raise ValueError('artifact list limit')
        for child in value:
            _json_shape(child, depth+1)
    elif isinstance(value, str):
        if len(value) > 8192:
            raise ValueError('artifact string limit')
    elif value is not None and not isinstance(value, bool):
        if not isinstance(value, (int, float)) or abs(_number(value)) > 1e14:
            raise ValueError('invalid artifact number')


def save(artifact, path=None):
    """Validate, fsync and atomically replace an explicit JSON artifact."""
    _validate(artifact)
    raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('model artifact too large')
    path = path or DEFAULT_PATH
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=folder, prefix='.short-model-', suffix='.json')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


def load(path=None):
    """Missing artifact returns None; corrupt/oversized/incompatible ones fail closed."""
    try:
        with open(path or DEFAULT_PATH, 'rb') as source:
            raw = source.read(MAX_ARTIFACT_BYTES+1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('model artifact too large')
    try:
        artifact = json.loads(raw)
        return _validate(artifact)
    except (KeyError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError('invalid model artifact') from exc


def report(artifact=None, path=None):
    """Compact HTML-safe laboratory status; no unearned income projection."""
    if artifact is None:
        try:
            artifact = load(path)
        except (ValueError, OSError):
            return '🧠 <b>Обучение шортов</b>\n\nАртефакт недоступен или повреждён. Теневые оценки отключены.'
    if artifact is None:
        return '🧠 <b>Обучение шортов</b>\n\nМодель ещё не обучена. Нужна независимая история свежих стаканов и проверенных расходов.'
    _validate(artifact)
    ev = artifact['evaluation']
    lines = ['🧠 <b>Обучение шортов · теневой режим</b>', '',
             'Статус: '+html.escape(artifact['reason']),
             f"Независимых исходов: {ev.get('independent_outcomes', 0)}",
             f"История: {ev.get('days', 0):.1f} дней",
             f"Полнота меток: {ev.get('known_label_coverage', 0)*100:.1f}%",
             f"Неизвестных исходов: {ev.get('label_coverage', {}).get('all', {}).get('unknown', 0)}",
             '', '<b>Хронологическая проверка</b>']
    for key, label in (('train', 'Обучение'), ('validation', 'Валидация'), ('test', 'Нетронутый тест')):
        part = ev.get('partitions', {}).get(key, {})
        lines.append(f"{label}: {part.get('count', 0)} исходов")
    lines += ['', 'Метки: часовой шорт после комиссий и известного финансирования.',
              'Неполные исполнения и пропуски данных исключаются из обучения.',
              'Вероятность модели не гарантирует прибыль и не разрешает вход.',
              'Рабочие правила и ограничения капитала сохраняются.']
    return '\n'.join(lines)
