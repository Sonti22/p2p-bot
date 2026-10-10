"""Public Bybit GET collector for paper shorts; never accepts credentials."""
import asyncio
import datetime
import json
import math
import os
import re
import time
import zlib
from urllib.parse import urlencode

import aiohttp

import perp
import shorts

API = 'https://api.bybit.com/v5/market/'
FEE_SOURCE = 'Bybit published Non-VIP taker 0.055%; conservative paper multiplier 2; checked 2026-10-09'
FEE = '0.0011'
REQUEST_SPACING = .08       # Shared collector budget, including the one-second sampler.
REQUEST_CONCURRENCY = 6
REQUEST_TIMEOUT = 10
REFRESH_TIMEOUT = 45
CATALOG_MAX_PAGES = 20
# The public groups classify instruments; their Pro/MM rates are not retail tariffs.
STANDARD_GROUPS = ('Altcoin', 'Major Coins', 'G1(Major Coins)', 'G2(High Growth)',
                   'G3(Mid-Tier Liquidity)', 'G4(Mid-Tier Activation)', 'G5(Long Tail)')


class PublicDataError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Bybit: неверный публичный ответ (' + str(code) + ')')


async def get(session, endpoint, **params):
    raw = await perp._get(session, API + endpoint + '?' + urlencode(params))
    if raw.get('retCode') != 0 or not isinstance(raw.get('result'), dict):
        raise PublicDataError(raw.get('retCode'))
    stamp = float(raw['time']) / 1000
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError('Bybit: неверное время ответа')
    return raw['result'], stamp


async def catalog(session, request=None):
    cursor, seen, out = '', set(), {}
    fetch = request or get
    for _ in range(CATALOG_MAX_PAGES):
        result, ts = await fetch(session, 'instruments-info', category='linear', limit=1000, cursor=cursor)
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
    raise ValueError('Bybit: превышен предел страниц каталога')


def bars(rows, volume=False):
    out = []
    for row in rows:
        ts = int(row[0]) / 1000
        values = [str(shorts.dec(v)) for v in row[1:5]]
        if any(shorts.dec(v) <= 0 for v in values):
            raise ValueError('неположительная свеча')
        if shorts.dec(values[1]) < max(shorts.dec(values[0]), shorts.dec(values[3])) or shorts.dec(values[2]) > min(shorts.dec(values[0]), shorts.dec(values[3])):
            raise ValueError('неверная геометрия свечи')
        amount = shorts.dec(row[5]) if volume else shorts.D(0)
        if amount < 0 or not math.isfinite(ts) or ts < 0:
            raise ValueError('неверное время или объём свечи')
        out.append([ts, *values, str(amount)])
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
        self._request_slots = asyncio.Semaphore(REQUEST_CONCURRENCY-1)
        self._sample_slot = asyncio.Semaphore(1)
        self._pace_lock = asyncio.Lock()
        self._next_request = 0
        self._cooldown_until = 0
        self._opened_terms = {}
        self._refresh_cancelled = False
        self.sample_resets = {}
        self.diagnostics = {'requests': 0, 'retries': 0, 'request_errors': 0,
                            'timeouts': 0, 'last_request_ms': 0,
                            'refreshes': 0, 'refresh_ms': 0,
                            'clock_status': 'not_checked', 'clock_rtt_ms': None,
                            'last_errors': [], 'symbol_errors': {}}
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

    def diagnostics_snapshot(self):
        """Finite, bounded observations for the UI; never changes execution policy."""
        now, monotonic = self.now(), time.monotonic()
        samples = {}
        for sym in sorted(self.watched)[:22]:
            rows = self.samples.get(sym, [])
            samples[sym] = {'count': len(rows),
                            'covered_seconds': max(0, rows[-1][0]-rows[0][0]) if rows else 0,
                            'latest_age_seconds': max(0, now-rows[-1][0]) if rows else None,
                            'ready_60s': bool(rows and any(60 <= now-r[0] <= 65 for r in rows)
                                               and 0 <= now-rows[-1][0] <= 3),
                            'last_reset': dict(self.sample_resets.get(sym, {}))}
        sampling = {sym: {'seconds': item['covered_seconds'], 'age': item['latest_age_seconds'],
                          'reset_reason': item['last_reset'].get('reason', '')}
                    for sym, item in samples.items()}
        snapshot = dict(self.diagnostics, samples=samples, sampling=sampling,
                        refresh_seconds=self.diagnostics['refresh_ms']/1000,
                    clock_offset_seconds=self.clock_offset,
                    clock_age_seconds=max(0, monotonic-self.clock_checked) if self.clock_checked is not None else None,
                    cooldown_seconds=max(0, self._cooldown_until-monotonic))
        return json.loads(json.dumps(snapshot, allow_nan=False))

    async def request(self, session, endpoint, *, attempts=2, timeout=REQUEST_TIMEOUT, sampling=False, **params):
        """Pace existing public GETs, retry transient failures only, with a total deadline."""
        deadline = time.monotonic()+timeout
        slots = self._sample_slot if sampling else self._request_slots
        for attempt in range(attempts):
            left = deadline-time.monotonic()
            if left <= 0:
                self.diagnostics['timeouts'] += 1
                raise TimeoutError('истёк срок публичного запроса')
            try:
                await asyncio.wait_for(slots.acquire(), left)
                try:
                    async with self._pace_lock:
                        delay = max(self._next_request, self._cooldown_until)-time.monotonic()
                        if delay >= deadline-time.monotonic():
                            raise TimeoutError('публичный источник на паузе; запрос отложен')
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._next_request = time.monotonic()+REQUEST_SPACING
                    started = time.monotonic()
                    self.diagnostics['requests'] += 1
                    try:
                        result = await asyncio.wait_for(get(session, endpoint, **params),
                                                        max(.001, deadline-started))
                    finally:
                        self.diagnostics['last_request_ms'] = round((time.monotonic()-started)*1000, 3)
                    return result
                finally:
                    slots.release()
            except (aiohttp.ClientError, OSError, TimeoutError, asyncio.TimeoutError, PublicDataError) as exc:
                self.diagnostics['request_errors'] += 1
                if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    self.diagnostics['timeouts'] += 1
                status = exc.status if isinstance(exc, aiohttp.ClientResponseError) else None
                code = exc.code if isinstance(exc, PublicDataError) else None
                limited = status in (403, 429) or code == 10006
                if limited:
                    delay = 600 if status == 403 else 5
                    self._cooldown_until = max(self._cooldown_until, time.monotonic()+delay)
                transient = (status is None and not isinstance(exc, PublicDataError)
                             or status in (429, 500, 502, 503, 504) or code in (10006, 10016))
                if attempt+1 == attempts or not transient or self._cooldown_until >= deadline:
                    if isinstance(exc, asyncio.TimeoutError):
                        raise TimeoutError('истёк срок публичного запроса') from exc
                    raise
                self.diagnostics['retries'] += 1
                await asyncio.sleep(min(.25*(2**attempt), max(0, deadline-time.monotonic())))

    def reset_samples(self, sym, reason, now):
        if not math.isfinite(now):
            now = self.now()
        self.samples.pop(sym, None)
        old = self.sample_resets.get(sym, {})
        self.sample_resets[sym] = {'reason': reason, 'at': now, 'count': old.get('count', 0)+1}
        # Old candidates must not grow diagnostics indefinitely.
        if len(self.sample_resets) > 100:
            oldest = min(self.sample_resets, key=lambda key: self.sample_resets[key]['at'])
            self.sample_resets.pop(oldest)

    async def calibrate(self, session):
        if time.monotonic() < self._cooldown_until:
            self.diagnostics['clock_status'] = 'source_cooldown'
            raise ValueError('источник времени на паузе после ограничения запросов')
        if self.clock_checked is not None and time.monotonic()-self.clock_checked < 300:
            return
        measurements = []
        for _ in range(3):
            start, monotonic = time.time(), time.monotonic()
            try:
                _, server = await asyncio.wait_for(get(session, 'time'), 1)
            except Exception:
                continue
            elapsed = time.monotonic()-monotonic
            if elapsed <= 1 and abs(time.time()-start-elapsed) <= .1:
                measurements.append((elapsed, start+elapsed/2-server))
        if not measurements:
            self.diagnostics['clock_status'] = 'unavailable'
            raise ValueError('нет точного измерения часов Bybit')
        uncertainty, offset = min(measurements)
        if abs(offset) > 5:
            self.diagnostics['clock_status'] = 'offset_rejected'
            raise ValueError('сдвиг часов больше 5с: нужна проверка компьютера')
        self.clock_offset, self.clock_checked = offset, time.monotonic()
        self.diagnostics.update(clock_status='verified', clock_rtt_ms=round(uncertainty*1000, 3))

    def add_samples(self, tickers, ts, now):
        """Only observed prices; any outage/reordering invalidates the warm-up window."""
        if not math.isfinite(ts) or not math.isfinite(now) or not 0 <= now-ts <= 2:
            for sym in self.watched:
                self.reset_samples(sym, 'stale_or_future', now)
            return
        for sym in self.watched:
            ticker = tickers.get(sym)
            try:
                valid = ticker and shorts.dec(ticker.get('markPrice', '0')) > 0
            except (ValueError, ArithmeticError):
                valid = False
            if not valid:
                self.reset_samples(sym, 'missing_or_invalid_mark', now)
                continue
            samples = self.samples.setdefault(sym, [])
            if samples and ts == samples[-1][0] and ticker['markPrice'] == samples[-1][1]:
                continue  # A cached snapshot neither warms up nor extends the window.
            if samples and (ts <= samples[-1][0] or ts-samples[-1][0] > 3):
                self.reset_samples(sym, 'reordered' if ts <= samples[-1][0] else 'gap_over_3s', now)
                samples = self.samples.setdefault(sym, [])
            samples.append([ts, ticker['markPrice']])
            samples[:] = [x for x in samples if ts-x[0] <= 120]

    async def sample_loop(self, session):
        next_due = time.monotonic()
        while self.watched:
            for sym in set(self.samples):
                if self.samples[sym] and self.now()-self.samples[sym][-1][0] > 3:
                    self.reset_samples(sym, 'stale_before_request', self.now())
            try:
                # One public request for all contracts, not one per watched coin.
                result, ts = await self.request(session, 'tickers', category='linear',
                                                attempts=1, timeout=2, sampling=True)
                self.add_samples({t['symbol']: t for t in result['list']}, ts, self.now())
            except Exception as exc:
                for sym in set(self.watched) | set(self.samples):
                    self.reset_samples(sym, 'request_failed:' + type(exc).__name__, self.now())
            next_due += 1
            if next_due <= time.monotonic():
                next_due = time.monotonic()+.1  # Never issue a burst to catch up missed samples.
            await asyncio.sleep(max(.1, next_due-time.monotonic()))

    async def close(self):
        if self.sample_task is not None:
            self.sample_task.cancel()
            await asyncio.gather(self.sample_task, return_exceptions=True)
            self.sample_task = None

    async def universe(self, session, now):
        if now-self.catalog_ts >= 600 or not self.instruments:
            instruments, ts = await catalog(session, self.request)
            self._ensure_refresh_active()
            groups, group_ts = await self.request(session, 'fee-group-info', productType='contract')
            self._ensure_refresh_active()
            mapping = {}
            for group in groups['list']:
                for symbol in group['symbols']:
                    mapping[symbol] = group['groupName']
            observed = self.now()
            self.catalog_record = {'instruments': instruments, 'fee_groups': groups,
                                   'source': 'Bybit public V5', 'observed': observed,
                                   'catalog_server_ts': ts, 'fee_group_server_ts': group_ts}
            shorts.record_catalog(self.catalog_record, observed, self.path)
            self.instruments, self.groups, self.catalog_ts = instruments, mapping, observed
        if now-self.ticker_ts >= 15 or not self.tickers:
            result, ts = await self.request(session, 'tickers', category='linear')
            self.tickers = {t['symbol']: t for t in result['list']}
            self.ticker_ts = ts

    async def cached(self, session, sym, key, interval, now, volume=True, timeout=REQUEST_TIMEOUT):
        stored = self.series.get((sym, key))
        ttl = 10 if key == 'minutes' else 15
        if stored and 0 <= now-stored[0] < ttl:
            return stored[1]
        result, ts = await self.request(session, 'mark-price-kline' if key == 'minutes' else 'kline',
                               category='linear', symbol=sym, interval=interval, limit=200, timeout=timeout)
        parsed = bars(result['list'], volume)
        parsed = [b for b in parsed if b[0] + int(interval)*60 <= ts]
        self.series[(sym, key)] = (ts, parsed)
        return parsed

    async def funding_history(self, session, sym, opened, now):
        stored = self.funding.get(sym)
        previous_next = stored[2] if stored and len(stored) > 2 else 0
        if stored and 0 <= now-stored[0] < 30 and not stored[0] < previous_next <= now:
            return stored[1]
        result, ts = await self.request(session, 'funding/history', category='linear', symbol=sym,
                               startTime=int((opened-1)*1000), endTime=int(now*1000), limit=200, timeout=2, attempts=1)
        history = []
        for item in result['list']:
            at = int(item['fundingRateTimestamp'])/1000
            mark = self.marks.get((sym, at))
            if mark is None:
                candle, _ = await self.request(session, 'mark-price-kline', category='linear', symbol=sym,
                                      interval=1, start=int(at*1000), end=int((at+60)*1000)-1, limit=1, timeout=2, attempts=1)
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
            errors = []
            frozen = self._opened_terms.get(sym, {})
            if opened and not frozen:
                state = shorts.status(self.path)
                frozen = next((p.get('terms', {}) for p in state['positions']
                               if p['symbol'] == sym and p['stage'] != 'closed'), {}) if state else {}
            inst = self.instruments.get(sym)
            missing_instrument = inst is None
            if missing_instrument:
                if not opened or not frozen.get('instrument'):
                    raise ValueError('символ отсутствует в полном каталоге: итог делистинга неизвестен')
                inst = frozen['instrument']
                errors.append('Нет инструмента в каталоге: условия входа заморожены, делистинг не подтверждён')
            # Unknown current tariffs never create a new trade or silently become a zero fee.
            group = self.groups.get(sym, '')
            fee, fee_source = FEE, FEE_SOURCE
            if group not in STANDARD_GROUPS:
                if not opened:
                    raise ValueError('неподтверждённая группа комиссии ' + group)
                if not frozen.get('fee_source') or not 0 < shorts.dec(frozen.get('fee')) < 1:
                    raise ValueError('нет замороженного тарифа для сопровождения позиции')
                fee, fee_source = str(shorts.dec(frozen['fee'])), frozen['fee_source']
                errors.append('Группа комиссии не подтверждена; выход оценён по ненулевому тарифу входа')
            elif time.time() >= datetime.datetime(2026,11,8,tzinfo=datetime.timezone.utc).timestamp():
                if opened and frozen.get('fee_source') and 0 < shorts.dec(frozen.get('fee')) < 1:
                    fee, fee_source = str(shorts.dec(frozen['fee'])), frozen['fee_source']
                    errors.append('Тариф устарел; выход оценён по замороженному тарифу входа')
                else:
                    fee_source = ''
            stored = self.risk.get(sym)
            if stored is None or not 0 <= now-stored[0] <= 600:
                try:
                    result, ts = await self.request(session, 'risk-limit', category='linear', symbol=sym,
                                                    timeout=2 if opened else REQUEST_TIMEOUT,
                                                    attempts=1 if opened else 2)
                    if result.get('nextPageCursor'):
                        raise ValueError('неполные ступени риска')
                    tiers = result['list']
                    if not tiers:
                        raise ValueError('нет поддерживающей маржи')
                    for tier in tiers:
                        for key in ('riskLimitValue', 'maintenanceMargin'):
                            if shorts.dec(tier[key]) < 0:
                                raise ValueError('неверный риск-тир')
                    self.risk[sym] = (ts, tiers)
                except Exception:
                    if not opened:
                        raise
                    tiers = []
                    errors.append('Поддерживающая маржа не подтверждена; ликвидация не проверена')
            else:
                tiers = stored[1]
            self._ensure_refresh_active()
            if opened:
                # Reversal history and OI cannot delay management of an existing position.
                series = [self.series.get((sym, key), (0, []))[1] for key in ('bars', 'hours', 'minutes')]
                try:
                    series[2] = await self.cached(session, sym, 'minutes', 1, now, False, timeout=2)
                except Exception:
                    errors.append('Нет свежих данных: minutes')
            else:
                series = await asyncio.gather(self.cached(session, sym, 'bars', 15, now),
                                               self.cached(session, sym, 'hours', 60, now),
                                               self.cached(session, sym, 'minutes', 1, now, False))
            self._ensure_refresh_active()
            try:
                hist, complete = await asyncio.wait_for(
                    self.funding_history(session, sym, opened, min(now,self.pending_ends.get(sym,now))), 3
                ) if opened else ([], True)
            except Exception:
                hist, complete = self.funding.get(sym, (0, ([], False)))[1][0], False
            if opened and not complete:
                errors.append('Расчёт финансирования не подтверждён')
            # Optional OI is useful for research but never required by position protection.
            oi = self.series.get((sym, 'oi'))
            if not opened and (not oi or not 0 <= now-oi[0] < 300):
                try:
                    result, ts = await self.request(session, 'open-interest', category='linear', symbol=sym,
                                                    intervalTime='5min', limit=5, attempts=1, timeout=2)
                    self.series[(sym, 'oi')] = (ts, result['list'])
                except Exception:
                    self.series[(sym, 'oi')] = (now, [])
            self._ensure_refresh_active()
            # Fetch ticker and depth last: expensive history loading cannot make an old book look fresh.
            ticker_result, ticker_ts = await self.request(session, 'tickers', category='linear', symbol=sym, attempts=1, timeout=2)
            ticker = ticker_result['list'][0]
            self._ensure_refresh_active()
            book, server_ts = await self.request(session, 'orderbook', category='linear', symbol=sym, limit=200, attempts=1, timeout=2)
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
            # Retain the observed depth needed for the entire paper account, never synthesize extra size.
            # This is much more than any allowed single position and keeps the research journal manageable.
            state = shorts.status(self.path)
            max_notional = shorts.dec(state['initial']) * 2 if state else shorts.D('10000')
            def bounded_depth(levels):
                total, kept = shorts.D(0), []
                for price, qty in levels:
                    if shorts.dec(price) <= 0 or shorts.dec(qty) < 0:
                        raise ValueError('неверный уровень стакана')
                    kept.append([price, qty])
                    total += shorts.dec(price)*shorts.dec(qty)
                    if total >= max_notional:
                        break
                return kept
            m = {'symbol': sym, 'active': not missing_instrument and inst['status'] == 'Trading' and not inst.get('isPreListing'),
                 'candidate': not opened and candidate and shorts.eligible(inst, ticker, ticker_ts), 'growth': ticker['price24hPcnt'], 'ticker_ts': ticker_ts,
                 'mark': ticker['markPrice'], 'index': ticker['indexPrice'],
                 'funding_rate': ticker['fundingRate'], 'funding_interval': inst['fundingInterval'],
                 'lower_funding': inst['lowerFundingRate'], 'next_funding': int(ticker['nextFundingTime'])/1000,
                 'bids': bounded_depth(book['b']), 'asks': bounded_depth(book['a']), 'book_ts': float(book['ts'])/1000,
                 'book_id': str(book['u']) + ':' + str(book['ts']),
                 'step': lot['qtyStep'], 'tick': price['tickSize'], 'min_qty': lot['minOrderQty'],
                 'min_notional': lot['minNotionalValue'], 'max_qty': lot['maxMktOrderQty'],
                 'fee': fee, 'fee_source': fee_source, 'tiers': tiers,
                 'bars': series[0], 'hours': series[1], 'minutes': series[2], 'mark_samples': list(samples),
                 'funding_history': hist, 'funding_complete': complete,
                 'open_interest': self.series.get((sym, 'oi'), (0, []))[1],
                 'data_errors': errors,
                 'terms': {'instrument': inst, 'tiers': tiers, 'fee': fee, 'fee_source': fee_source,
                           'fee_group': group, 'catalog_ts': self.catalog_ts, 'version': shorts.VERSION,
                           'ticker': ticker, 'clock_offset': self.clock_offset,
                           'clock_source': 'Bybit public time; minimum RTT of 3; RTT <=1s'}}
            for key in ('mark', 'index', 'step', 'tick', 'min_qty', 'min_notional', 'max_qty', 'funding_interval'):
                if shorts.dec(m[key]) <= 0:
                    raise ValueError('неверный параметр ' + key)
            return sym, m

    def _ensure_refresh_active(self):
        if self._refresh_cancelled:
            raise asyncio.CancelledError

    async def refresh(self, session):
        started = time.monotonic()
        self.diagnostics['refreshes'] += 1
        self._refresh_cancelled = False
        task = asyncio.create_task(self._refresh(session))
        try:
            done, _ = await asyncio.wait({task}, timeout=REFRESH_TIMEOUT)
            if not done:
                # Python 3.10 wait_for can swallow cancellation when an inner task just completed.
                # Explicit wait + a phase guard prevents late observations from changing the journal.
                self._refresh_cancelled = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise TimeoutError('срок сбора истёк')
            return task.result()
        except (TimeoutError, asyncio.TimeoutError):
            message = 'Сбор шортов: превышен срок цикла; новые входы запрещены'
            self.diagnostics['last_errors'] = [message]
            shorts.record_error(message, self.path)
            shorts.tick({}, self.now(), self.path, allow_entries=False)
            return {}
        except asyncio.CancelledError:
            self._refresh_cancelled = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self.diagnostics['refresh_ms'] = round((time.monotonic()-started)*1000, 3)
            self._refresh_cancelled = False

    async def _refresh(self, session):
        state = shorts.status(self.path)
        active = [p for p in state['positions'] if p['stage'] != 'closed'] if state else []
        external_path = getattr(self, 'external_path', None)
        if external_path:
            external = shorts.status(external_path)
            active += [p for p in external['positions'] if p['stage'] != 'closed'] if external else []
        opened = {p['symbol']: p.get('opened', p['submitted']) for p in active}
        self._opened_terms = {p['symbol']: p.get('terms', {}) for p in active}
        self.pending_ends = {p['symbol']: p['closed'] for p in active if p['stage'] == 'funding'}
        clock_error = ''
        if session is not None:
            try:
                await self.calibrate(session)
            except Exception as exc:
                clock_error = 'Калибровка времени: ' + type(exc).__name__
                if not opened or self.clock_checked is None:
                    self.diagnostics['last_errors'] = [clock_error]
                    shorts.record_error(clock_error, self.path)
                    shorts.tick({}, self.now(), self.path, allow_entries=False)
                    return {}
                # A prior offset may only accompany exits; server freshness is still checked below.
                self.diagnostics['clock_status'] = 'cached_for_exits_only'
        self._ensure_refresh_active()
        now = self.now()
        markets, errors, symbol_errors = {}, [clock_error] if clock_error else [], {}
        self.watched.update(opened)

        async def observe(sym, candidate=False, entries=False):
            self._ensure_refresh_active()
            result = await asyncio.wait_for(self.market(session, sym, candidate, opened.get(sym)),
                                            12 if sym in opened else 20)
            self._ensure_refresh_active()
            if clock_error:
                result[1].setdefault('data_errors', []).append('Точность текущих часов не подтверждена')
            decision_at = self.now()
            result[1]['observed_at'] = decision_at
            self.observed[sym] = result[1]
            # Apply a fresh book immediately; other symbols cannot age it while loading histories.
            active_markets = {key: value for key, value in self.observed.items()
                              if key in self.watched}
            shorts.tick(active_markets, decision_at, self.path, allow_entries=entries)
            if external_path:
                shorts.tick(active_markets, decision_at, external_path,
                            allow_entries=entries and getattr(self, 'external_entries', False))
            return result

        def collect(symbols, results):
            for sym, result in zip(symbols, results):
                if isinstance(result, Exception):
                    detail = type(result).__name__ + ' ' + str(result)[:120]
                    symbol_errors[sym] = detail
                    errors.append(sym + ': ' + detail)
                else:
                    markets[result[0]] = result[1]
                    errors.extend(result[0] + ': ' + error for error in result[1].get('data_errors', []))

        # Position protection gets the available cached terms before optional catalogue refresh.
        # A slow catalogue must never hold up an already known position's fresh stop/exit quote.
        if opened:
            collect(list(opened), await asyncio.gather(*(observe(sym) for sym in opened), return_exceptions=True))
        catalog_error = ''
        try:
            await asyncio.wait_for(self.universe(session, self.now()), 10 if opened else 25)
        except Exception as exc:
            catalog_error = 'Каталог/тикеры: ' + type(exc).__name__
            errors.append(catalog_error)
        self._ensure_refresh_active()
        if external_path and getattr(self, 'catalog_record', None):
            shorts.record_catalog(self.catalog_record, self.catalog_ts, external_path)
        candidates = [sym for sym, inst in self.instruments.items()
                      if not catalog_error and not clock_error
                      and shorts.eligible(inst, self.tickers.get(sym, {}), self.now())]
        candidates.sort(key=lambda sym: shorts.dec(self.tickers[sym]['price24hPcnt']), reverse=True)
        candidates = candidates[:20]
        self.watched = set(opened) | set(candidates)
        self.samples = {key: value for key, value in self.samples.items() if key in self.watched}
        self.observed = {key: value for key, value in self.observed.items() if key in self.watched}
        if session is not None and self.watched and (self.sample_task is None or self.sample_task.done()):
            self.sample_task = asyncio.create_task(self.sample_loop(session))
        new = [sym for sym in candidates if sym not in opened]
        collect(new, await asyncio.gather(*(observe(sym, True, not catalog_error and not clock_error)
                                             for sym in new), return_exceptions=True))
        self.diagnostics.update(last_errors=errors[:24], symbol_errors=symbol_errors,
                                candidates=len(candidates), observed=len(markets),
                                open_positions=len(opened),
                                fee_groups={sym: self.groups.get(sym, '') for sym in self.watched})
        shorts.record_error('; '.join(errors), self.path)
        if not markets:
            shorts.tick({}, self.now(), self.path, allow_entries=False)
        return markets
