"""Public Bybit GET collector for paper shorts; never accepts credentials."""
import asyncio
import datetime
import json
import os
import re
import time
import zlib
from urllib.parse import urlencode

import perp
import shorts

API = 'https://api.bybit.com/v5/market/'
FEE_SOURCE = 'Bybit published Non-VIP taker 0.055%; conservative paper multiplier 2; checked 2026-10-09'
FEE = '0.0011'


async def get(session, endpoint, **params):
    raw = await perp._get(session, API + endpoint + '?' + urlencode(params))
    if raw.get('retCode') != 0 or not isinstance(raw.get('result'), dict):
        raise ValueError('Bybit: неверный публичный ответ')
    return raw['result'], float(raw['time']) / 1000


async def catalog(session):
    cursor, seen, out = '', set(), {}
    while True:
        result, ts = await get(session, 'instruments-info', category='linear', limit=1000, cursor=cursor)
        for item in result['list']:
            symbol = item['symbol']
            if not re.fullmatch(r'[A-Z0-9]{2,40}USDT', symbol):
                continue
            out[symbol] = item
        cursor = result.get('nextPageCursor') or ''
        if not cursor:
            return out, ts
        if cursor in seen:
            raise ValueError('Bybit: повтор страницы каталога')
        seen.add(cursor)


def bars(rows, volume=False):
    out = []
    for row in rows:
        ts = int(row[0]) / 1000
        values = [str(shorts.dec(v)) for v in row[1:5]]
        if any(shorts.dec(v) <= 0 for v in values):
            raise ValueError('неположительная свеча')
        if shorts.dec(values[1]) < max(shorts.dec(values[0]), shorts.dec(values[3])) or shorts.dec(values[2]) > min(shorts.dec(values[0]), shorts.dec(values[3])):
            raise ValueError('неверная геометрия свечи')
        out.append([ts, *values, str(shorts.dec(row[5])) if volume else '0'])
    out.sort(key=lambda b: b[0])
    if len({b[0] for b in out}) != len(out):
        raise ValueError('повтор свечи')
    return out


class Collector:
    def __init__(self, path=None):
        self.path = path
        self.instruments = {}
        self.catalog_ts = 0
        self.tickers = {}
        self.ticker_ts = 0
        self.series = {}
        self.risk = {}
        self.funding = {}
        self.marks = {}
        self.samples = {}
        self.groups = {}
        self.errors = {}
        self.semaphore = asyncio.Semaphore(4)
        self.last_observed = 0
        self.observed = {}
        self.pending_ends = {}
        self.watched = set()
        self.sample_task = None
        self.clock_offset = 0
        self.clock_checked = None
        if os.path.exists(path or shorts.DB_PATH):
            with shorts.connect(path) as con:
                row = con.execute('SELECT ts,state FROM catalog ORDER BY ts DESC LIMIT 1').fetchone()
                if row:
                    raw = json.loads(zlib.decompress(row['state']))
                    self.instruments = raw['instruments']
                    self.catalog_ts = row['ts']
                    for group in raw.get('fee_groups', {}).get('list', []):
                        for symbol in group['symbols']:
                            self.groups[symbol] = group['groupName']

    def now(self):
        return time.time()-self.clock_offset

    async def calibrate(self, session):
        if self.clock_checked is not None and time.monotonic()-self.clock_checked < 300:
            return
        measurements = []
        for _ in range(3):
            start, monotonic = time.time(), time.monotonic()
            try:
                _, server = await get(session, 'time')
            except Exception:
                continue
            elapsed = time.monotonic()-monotonic
            if elapsed <= 1 and abs(time.time()-start-elapsed) <= .1:
                measurements.append((elapsed, start+elapsed/2-server))
        if not measurements:
            raise ValueError('нет точного измерения часов Bybit')
        uncertainty, offset = min(measurements)
        if abs(offset) > 5:
            raise ValueError('сдвиг часов больше 5с: нужна проверка компьютера')
        self.clock_offset, self.clock_checked = offset, time.monotonic()

    def add_samples(self, tickers, ts, now):
        """Only observed prices; any outage/reordering invalidates the warm-up window."""
        if not 0 <= now-ts <= 2:
            self.samples.clear()
            return
        for sym in self.watched:
            ticker = tickers.get(sym)
            if not ticker or shorts.dec(ticker.get('markPrice', '0')) <= 0:
                self.samples.pop(sym, None)
                continue
            samples = self.samples.setdefault(sym, [])
            if samples and ts == samples[-1][0] and ticker['markPrice'] == samples[-1][1]:
                continue  # The same cached snapshot is not a new observation or a data gap.
            if samples and (ts <= samples[-1][0] or ts-samples[-1][0] > 3):
                samples.clear()
            samples.append([ts, ticker['markPrice']])
            samples[:] = [x for x in samples if ts-x[0] <= 120]

    async def sample_loop(self, session):
        while self.watched:
            started = time.monotonic()
            try:
                # One public request for all contracts, not one per watched coin.
                result, ts = await get(session, 'tickers', category='linear')
                self.add_samples({t['symbol']: t for t in result['list']}, ts, self.now())
            except Exception:
                self.samples.clear()
            await asyncio.sleep(max(.1, 1-(time.monotonic()-started)))

    async def close(self):
        if self.sample_task is not None:
            self.sample_task.cancel()
            await asyncio.gather(self.sample_task, return_exceptions=True)
            self.sample_task = None

    async def universe(self, session, now):
        if now-self.catalog_ts >= 600 or not self.instruments:
            instruments, ts = await catalog(session)
            groups, _ = await get(session, 'fee-group-info', productType='contract')
            mapping = {}
            for group in groups['list']:
                for symbol in group['symbols']:
                    mapping[symbol] = group['groupName']
            shorts.record_catalog({'instruments': instruments, 'fee_groups': groups,
                                   'source': 'Bybit public V5', 'observed': ts}, ts, self.path)
            self.instruments, self.groups, self.catalog_ts = instruments, mapping, now
        if now-self.ticker_ts >= 15 or not self.tickers:
            result, ts = await get(session, 'tickers', category='linear')
            self.tickers = {t['symbol']: t for t in result['list']}
            self.ticker_ts = ts

    async def cached(self, session, sym, key, interval, now, volume=True):
        stored = self.series.get((sym, key))
        ttl = 10 if key == 'minutes' else 15
        if stored and now-stored[0] < ttl:
            return stored[1]
        result, ts = await get(session, 'mark-price-kline' if key == 'minutes' else 'kline',
                               category='linear', symbol=sym, interval=interval, limit=200)
        parsed = bars(result['list'], volume)
        parsed = [b for b in parsed if b[0] + int(interval)*60 <= ts]
        self.series[(sym, key)] = (ts, parsed)
        return parsed

    async def funding_history(self, session, sym, opened, now):
        stored = self.funding.get(sym)
        previous_next = stored[2] if stored and len(stored) > 2 else 0
        if stored and now-stored[0] < 30 and not stored[0] < previous_next <= now:
            return stored[1]
        result, ts = await get(session, 'funding/history', category='linear', symbol=sym,
                               startTime=int((opened-1)*1000), endTime=int(now*1000), limit=200)
        history = []
        for item in result['list']:
            at = int(item['fundingRateTimestamp'])/1000
            mark = self.marks.get((sym, at))
            if mark is None:
                candle, _ = await get(session, 'mark-price-kline', category='linear', symbol=sym,
                                      interval=1, start=int(at*1000), end=int((at+60)*1000)-1, limit=1)
                rows = bars(candle['list'])
                if rows and rows[0][0] == at:
                    mark = rows[0][1]
                    self.marks[(sym, at)] = mark
            history.append({'ts': at, 'rate': str(shorts.dec(item['fundingRate'])), 'mark': mark})
        complete = len(history) < 200 and all(h['mark'] is not None for h in history)
        self.funding[sym] = (ts, (history, complete), int(self.tickers.get(sym, {}).get('nextFundingTime') or 0)/1000)
        return history, complete

    async def market(self, session, sym, candidate, opened):
        async with self.semaphore:
            now = self.now()
            inst = self.instruments.get(sym)
            if inst is None:
                raise ValueError('символ отсутствует в полном каталоге: итог делистинга неизвестен')
            # The catalog's fee groups are used to exclude pre-listing/TradFi/unclassified products.
            group = self.groups.get(sym, '')
            if group not in ('Altcoin', 'Major Coins'):
                raise ValueError('неподтверждённая группа комиссии ' + group)
            stored = self.risk.get(sym)
            if stored is None or now-stored[0] > 600:
                try:
                    result, ts = await get(session, 'risk-limit', category='linear', symbol=sym)
                except Exception:
                    if not opened:
                        raise
                    result, ts = {'list': []}, now
                if result.get('nextPageCursor'):
                    raise ValueError('неполные ступени риска')
                tiers = result['list']
                if not tiers and not opened:
                    raise ValueError('нет поддерживающей маржи')
                for tier in tiers:
                    for key in ('riskLimitValue', 'maintenanceMargin'):
                        if shorts.dec(tier[key]) < 0:
                            raise ValueError('неверный риск-тир')
                if tiers:
                    self.risk[sym] = (ts, tiers)
            else:
                tiers = stored[1]
            series = await asyncio.gather(self.cached(session, sym, 'bars', 15, now),
                                           self.cached(session, sym, 'hours', 60, now),
                                           self.cached(session, sym, 'minutes', 1, now, False), return_exceptions=True)
            errors = []
            for i, key in enumerate(('bars', 'hours', 'minutes')):
                if isinstance(series[i], Exception):
                    if not opened:
                        raise series[i]
                    errors.append('Нет свежих данных: ' + key)
                    series[i] = self.series.get((sym, key), (0, []))[1]
            try:
                hist, complete = await self.funding_history(session, sym, opened, min(now,self.pending_ends.get(sym,now))) if opened else ([], True)
            except Exception:
                hist, complete = [], False
            # Diagnostic OI and public prints are observations, never substitutes for executed fills.
            oi = self.series.get((sym, 'oi'))
            if not oi or now-oi[0] >= 300:
                try:
                    result, ts = await get(session, 'open-interest', category='linear', symbol=sym, intervalTime='5min', limit=5)
                    self.series[(sym, 'oi')] = (ts, result['list'])
                except Exception:
                    self.series[(sym, 'oi')] = (now, [])
            # Fetch ticker and depth last: expensive history loading cannot make an old book look fresh.
            ticker_result, ticker_ts = await get(session, 'tickers', category='linear', symbol=sym)
            ticker = ticker_result['list'][0]
            book, server_ts = await get(session, 'orderbook', category='linear', symbol=sym, limit=200)
            if abs(self.now()-server_ts) > 2:
                raise ValueError('часы сервера/локальные отличаются больше 2с')
            samples = self.samples.get(sym, [])
            if not samples or self.now()-samples[-1][0] > 3:
                samples = []
            if opened and not samples:
                errors.append('Разрыв секундных наблюдений mark: защита за 60с не проверена')
            lot, price = inst['lotSizeFilter'], inst['priceFilter']
            leverage = inst['leverageFilter']
            if not shorts.dec(leverage['minLeverage']) <= 2 <= shorts.dec(leverage['maxLeverage']):
                raise ValueError('инструмент не допускает плечо 2×')
            fee = FEE
            # Retain the observed depth needed for the entire paper account, never synthesize extra size.
            # This is much more than any allowed single position and keeps the research journal manageable.
            state = shorts.status(self.path)
            max_notional = shorts.dec(state['initial']) * 2 if state else shorts.D('10000')
            def bounded_depth(levels):
                total, kept = shorts.D(0), []
                for price, qty in levels:
                    kept.append([price, qty])
                    total += shorts.dec(price)*shorts.dec(qty)
                    if total >= max_notional:
                        break
                return kept
            m = {'symbol': sym, 'active': inst['status'] == 'Trading' and not inst.get('isPreListing'),
                 'candidate': candidate and shorts.eligible(inst, ticker, ticker_ts), 'growth': ticker['price24hPcnt'], 'ticker_ts': ticker_ts,
                 'mark': ticker['markPrice'], 'index': ticker['indexPrice'],
                 'funding_rate': ticker['fundingRate'], 'funding_interval': inst['fundingInterval'],
                 'lower_funding': inst['lowerFundingRate'], 'next_funding': int(ticker['nextFundingTime'])/1000,
                 'bids': bounded_depth(book['b']), 'asks': bounded_depth(book['a']), 'book_ts': float(book['ts'])/1000,
                 'book_id': str(book['u']) + ':' + str(book['ts']),
                 'step': lot['qtyStep'], 'tick': price['tickSize'], 'min_qty': lot['minOrderQty'],
                 'min_notional': lot['minNotionalValue'], 'max_qty': lot['maxMktOrderQty'],
                 'fee': fee, 'fee_source': FEE_SOURCE if time.time() < datetime.datetime(2026,11,8,tzinfo=datetime.timezone.utc).timestamp() else '', 'tiers': tiers,
                 'bars': series[0], 'hours': series[1], 'minutes': series[2], 'mark_samples': list(samples),
                 'funding_history': hist, 'funding_complete': complete,
                 'open_interest': self.series[(sym, 'oi')][1],
                 'data_errors': errors,
                 'terms': {'instrument': inst, 'tiers': tiers, 'fee': fee, 'fee_source': FEE_SOURCE,
                           'fee_group': group, 'catalog_ts': self.catalog_ts, 'version': shorts.VERSION,
                           'ticker': ticker, 'clock_offset': self.clock_offset,
                           'clock_source': 'Bybit public time; minimum RTT of 3; RTT <=1s'}}
            for key in ('mark', 'index', 'step', 'tick', 'min_qty', 'min_notional', 'max_qty', 'funding_interval'):
                if shorts.dec(m[key]) <= 0:
                    raise ValueError('неверный параметр ' + key)
            return sym, m

    async def refresh(self, session):
        if session is not None:
            try:
                await self.calibrate(session)
            except Exception as exc:
                shorts.record_error('Калибровка времени: ' + type(exc).__name__, self.path)
                shorts.tick({}, self.now(), self.path, allow_entries=False)
                return {}
        now = self.now()
        state = shorts.status(self.path)
        opened = {p['symbol']: p.get('opened', p['submitted']) for p in state['positions'] if p['stage'] != 'closed'} if state else {}
        self.pending_ends = {p['symbol']: p['closed'] for p in state['positions'] if p['stage'] == 'funding'} if state else {}
        catalog_error = ''
        try:
            await self.universe(session, now)
        except Exception as exc:
            catalog_error = 'Каталог/тикеры: ' + type(exc).__name__
            if not opened:
                shorts.record_error(catalog_error, self.path)
                shorts.tick({}, now, self.path, allow_entries=False)
                return {}
        candidates = [sym for sym, inst in self.instruments.items()
                      if not catalog_error and shorts.eligible(inst, self.tickers.get(sym, {}), now)]
        candidates.sort(key=lambda sym: shorts.dec(self.tickers[sym]['price24hPcnt']), reverse=True)
        candidates = candidates[:20]
        self.watched = set(opened) | set(candidates)
        self.samples = {key: value for key, value in self.samples.items() if key in self.watched}
        if session is not None and self.watched and (self.sample_task is None or self.sample_task.done()):
            self.sample_task = asyncio.create_task(self.sample_loop(session))
        async def observe(sym):
            result = await self.market(session, sym, sym in candidates, opened.get(sym))
            self.observed[sym] = result[1]
            # Process each fresh book immediately rather than aging it while other symbols load.
            active_markets = {key: value for key, value in self.observed.items()
                              if key in opened or key in candidates}
            shorts.tick(active_markets, self.now(), self.path, allow_entries=not catalog_error)
            return result
        results = await asyncio.gather(*(observe(sym)
                                        for sym in dict.fromkeys([*opened, *candidates])), return_exceptions=True)
        markets, errors = {}, [catalog_error] if catalog_error else []
        for sym, result in zip(dict.fromkeys([*opened, *candidates]), results):
            if isinstance(result, Exception):
                errors.append(sym + ': ' + type(result).__name__ + ' ' + str(result)[:120])
            else:
                markets[result[0]] = result[1]
                errors.extend(result[0] + ': ' + error for error in result[1].get('data_errors', []))
        shorts.record_error('; '.join(errors), self.path)
        if not markets:
            shorts.tick({}, self.now(), self.path, allow_entries=False)
        return markets
