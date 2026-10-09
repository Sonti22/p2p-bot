"""Independent Decimal paper shorts. This module has no exchange order API."""
from contextlib import contextmanager
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import csv
import html
import json
import os
import sqlite3
import statistics
import time
import zlib

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'alt_shorts.db')
VERSION = 'pump-reversal-v1'
D = Decimal
POLICY = {'risk': '0.005', 'total_risk': '0.01', 'allocation': '0.02',
          'total_allocation': '0.04', 'leverage': '2', 'daily_loss': '0.02',
          'drawdown': '0.05', 'delay': 1, 'book_age': 2, 'cooldown': 21600,
          'hold': 86400, 'max_positions': 2, 'margin_mode': 'isolated', 'auto_margin': False}


def dec(v):
    if isinstance(v, bool) or v is None:
        raise ValueError('число отсутствует')
    x = D(str(v))
    if not x.is_finite():
        raise ValueError('не конечное число')
    return x


def dump(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


@contextmanager
def connect(path=None, write=False):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.executescript('''
      CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, symbol TEXT, state TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, details TEXT);
      CREATE TABLE IF NOT EXISTS snapshots (symbol TEXT, ts REAL, state TEXT, PRIMARY KEY(symbol,ts));
      CREATE TABLE IF NOT EXISTS catalog (ts REAL PRIMARY KEY, state TEXT);
    ''')
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def event(con, now, kind, details):
    con.execute('INSERT INTO events(ts,kind,details) VALUES(?,?,?)', (now, kind, dump(details)))


def initialize(rate, source, now=None, path=None):
    now = time.time() if now is None else now
    rate = dec(rate)
    if rate <= 0 or not source:
        raise ValueError('нужен положительный наблюдаемый курс и источник')
    with connect(path, True) as con:
        if con.execute('SELECT 1 FROM meta').fetchone():
            return False
        capital = D(50000) / rate
        state = {'initial': str(capital), 'cash': str(capital), 'realized': '0', 'fees': '0',
                 'funding': '0', 'rub_rate': str(rate), 'rate_source': str(source), 'start': now,
                 'peak': str(capital), 'equity': str(capital), 'drawdown': '0', 'paused': False,
                 'halt': False, 'day': int(now // 86400), 'day_start': str(capital), 'day_pnl': '0',
                 'version': VERSION, 'policy': POLICY, 'last_error': '', 'rejections': {}, 'watch': []}
        con.execute('INSERT INTO meta VALUES(1,?)', (dump(state),))
        event(con, now, 'initialize', state)
        return True


def meta(con):
    row = con.execute('SELECT state FROM meta').fetchone()
    return json.loads(row[0]) if row else None


def save_meta(con, state):
    con.execute('UPDATE meta SET state=? WHERE id=1', (dump(state),))


def positions(con, active=False):
    rows = [dict(json.loads(row['state']), id=row['id']) for row in con.execute('SELECT * FROM positions ORDER BY id')]
    return [p for p in rows if p['stage'] != 'closed'] if active else rows


def save_position(con, p):
    con.execute('UPDATE positions SET state=? WHERE id=?', (dump(p), p['id']))


def verify(con, state):
    held = sum((dec(p['held']) for p in positions(con, True)), D(0))
    actual = dec(state['cash']) + held
    expected = dec(state['initial']) + dec(state['realized'])
    if abs(actual - expected) > D('0.000000000000000000001') or dec(state['cash']) < 0 or held < 0:
        raise ArithmeticError('нарушение сохранения капитала шортов')


def reject(con, state, reason, now):
    counts = state['rejections']
    counts[reason] = counts.get(reason, 0) + 1
    if state.get('last_rejection') != reason:
        event(con, now, 'rejected', {'reason': reason})
    state['last_rejection'] = reason


def contiguous(bars, interval, now, minimum):
    rows = [b for b in bars if float(b[0]) + interval <= now]
    if len(rows) < minimum or rows[-1][0] + interval < now - interval - 5:
        return []
    if any(b[0] - a[0] != interval for a, b in zip(rows, rows[1:])):
        return []
    return rows


def ema(values, length=20):
    out, x = [], dec(values[0])
    alpha = D(2) / (length + 1)
    for value in values:
        x += alpha * (dec(value) - x)
        out.append(x)
    return out


def atr(bars):
    tr = []
    for prev, b in zip(bars, bars[1:]):
        tr.append(max(dec(b[2]) - dec(b[3]), abs(dec(b[2]) - dec(prev[4])), abs(dec(b[3]) - dec(prev[4]))))
    return sum(tr[-14:], D(0)) / 14


def eligible(inst, ticker, now):
    try:
        return (inst['status'] == 'Trading' and inst['contractType'] == 'LinearPerpetual'
                and inst['quoteCoin'] == inst['settleCoin'] == 'USDT'
                and not inst.get('isPreListing') and not inst.get('marketRegion')
                and not inst.get('underlyingTicker')
                and inst['baseCoin'] not in ('BTC', 'ETH', 'USDC', 'USDT', 'DAI', 'USDE', 'FDUSD', 'TUSD', 'PYUSD')
                and now - int(inst['launchTime']) / 1000 >= 30 * 86400
                and dec(ticker['turnover24h']) >= 10000000
                and dec(ticker['price24hPcnt']) >= D('.20')
                and (dec(ticker['ask1Price']) - dec(ticker['bid1Price'])) / dec(ticker['bid1Price']) <= D('.002'))
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return False


def protection(m, now):
    if not 0 <= now - m['ticker_ts'] <= 10:
        return 'устаревшая котировка'
    bars = contiguous(m.get('minutes', []), 60, now, 3)
    samples = m.get('mark_samples', [])
    if not bars or len(samples) < 2 or now - samples[-1][0] > 10:
        return 'нет непрерывных минутных данных mark'
    older = [s for s in samples if 60 <= now - s[0] <= 65]
    if not older:
        return 'не прогрето окно mark 60 секунд'
    if dec(m['mark']) / dec(older[-1][1]) - 1 > D('.05'):
        return 'ускорение mark >5% за 60с'
    if abs(dec(m['mark']) / dec(m['index']) - 1) > D('.01'):
        return 'расхождение mark/index >1%'
    if dec(m['funding_rate']) < D('-.001'):
        return 'фандинг ниже −0,1%'
    return None


def signal(m, now):
    hours = contiguous(m.get('hours', []), 3600, now, 21)
    bars = contiguous(m.get('bars', []), 900, now, 35)
    if not hours or not bars:
        return None, 'нет непрерывных закрытых свечей'
    volumes = [dec(b[5]) for b in hours[-21:-1]]
    median = statistics.median(volumes)
    if median <= 0 or dec(hours[-1][5]) < 3 * median:
        return None, 'часовой объём меньше 3× медианы'
    if now - (bars[-1][0] + 900) > 60:
        return None, 'окно входа 60с истекло'
    closes = [b[4] for b in bars]
    means = ema(closes)
    j = len(bars) - 1
    crossed = any(dec(bars[i - 1][4]) >= means[i - 1] and dec(bars[i][4]) < means[i]
                  for i in range(max(1, j - 4), j))
    b = bars[-1]
    a = atr(bars)
    if not crossed or a <= 0 or not means[-1] - a / 4 <= dec(b[2]) <= means[-1] + a / 4:
        return None, 'нет неудачного возврата к EMA20'
    if dec(b[4]) >= means[-1] or dec(b[4]) >= dec(b[1]):
        return None, 'разворот не подтверждён'
    if dec(b[2]) > max(dec(r[2]) for r in bars[-7:-1]):
        return None, 'обновлён максимум шести свечей'
    stop = max(dec(r[2]) for r in bars[-6:]) + a / 4
    return {'stop': str(stop), 'signal_ts': b[0] + 900,
            'reason': 'памп ≥20%, объём ≥3×, пробой EMA20 и неудачный возврат'}, None


def walk(levels, qty, step, consumed=None, side=''):
    qty, filled, value = dec(qty), D(0), D(0)
    consumed = consumed if consumed is not None else {}
    for raw_price, raw_qty in levels:
        price, size = dec(raw_price), dec(raw_qty)
        if price <= 0 or size < 0:
            raise ValueError('неверный стакан')
        key = side + ':' + str(raw_price)
        available = max(D(0), size - consumed.get(key, D(0)))
        take = min(qty - filled, available)
        take = (take / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if take <= 0:
            continue
        filled += take
        value += take * price
        consumed[key] = consumed.get(key, D(0)) + take
        if filled >= qty:
            break
    return filled, value / filled if filled else D(0)


def book_fresh(m, now):
    return 0 <= now - m['book_ts'] <= 2 and m.get('bids') and m.get('asks')


def size_order(state, opened, m, stop):
    equity = min(dec(state['equity']), dec(state['cash']) + sum((dec(p['held']) for p in opened), D(0)))
    if equity <= 0:
        return None
    bid = dec(m['bids'][0][0])
    stop = (dec(stop) / dec(m['tick'])).to_integral_value(rounding=ROUND_CEILING) * dec(m['tick'])
    dist = stop - bid
    if not D('.02') <= dist / bid <= D('.12'):
        return None
    risk_used = sum((dec(p['risk']) for p in opened), D(0))
    held = sum((dec(p['held']) for p in opened), D(0))
    budget = min(equity * D('.02'), equity * D('.04') - held, dec(state['cash']))
    fee = dec(m['fee'])
    # Reserve entry fee and a whole day's worst published negative funding plus close fee at stop.
    funding_reserve = max(D(0), -dec(m['lower_funding'])) * D(1440) / dec(m['funding_interval']) * bid
    per_qty = bid / 2 + fee * (bid + stop) + funding_reserve
    risk_budget = min(equity * D('.005'), equity * D('.01') - risk_used)
    risk_per_qty = dist + fee * (bid + stop) + funding_reserve
    qty = min(budget / per_qty, risk_budget / risk_per_qty, dec(m['max_qty']))
    step = dec(m['step'])
    qty = (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if qty < dec(m['min_qty']) or qty * bid < dec(m['min_notional']):
        return None
    filled, px = walk(m['bids'], qty, step)
    if filled != qty or (bid - px) / bid > D('.003'):
        return None
    tier = risk_tier(m.get('tiers', []), qty*px)
    if tier is None or not 0 < dec(tier['maintenanceMargin']) < 1:
        return None
    return {'qty': str(qty), 'stop': str(stop), 'held': str(qty * per_qty),
            'risk': str(qty * risk_per_qty), 'entry_est': str(px)}


def tick(markets, now=None, path=None, allow_entries=True, filters=True, random_entry=False):
    now = time.time() if now is None else now
    with connect(path, True) as con:
        state = meta(con)
        if state is None:
            return []
        before = con.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        if int(now // 86400) != state['day']:
            state.update(day=int(now // 86400), day_start=state['equity'], day_pnl='0')
        for sym, m in markets.items():
            con.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)',
                        (sym, m['ticker_ts'], zlib.compress(dump(m).encode('utf-8'))))
        consumed = {}
        for p in positions(con, True):
            m = markets.get(p['symbol'])
            if m is None or not 0 <= now - m['ticker_ts'] <= 10:
                if not p.get('gap'):
                    event(con, now, 'data_gap', {'position': p['id'], 'symbol': p['symbol']})
                p['gap'] = True
                save_position(con, p)
                continue
            if p.get('last_ts') and now - p['last_ts'] > 10:
                p['gap'] = True
                event(con, now, 'unobserved_interval', {'position': p['id'], 'from': p['last_ts'], 'to': now})
            if m.get('data_errors'):
                p['gap'] = True
            p['last_ts'] = now
            qty, held = dec(p['qty']), dec(p['held'])
            if p['stage'] == 'funding':
                if _funding(con, state, p, m, p['closed']):
                    state['cash'] = str(dec(state['cash']) + dec(p['held']))
                    p.update(stage='closed', held='0', reserve='0')
                    event(con, now, 'funding_reconciled', {'position': p['id']})
                save_position(con, p)
                continue
            if p['stage'] == 'entry':
                if now > p['signal_ts'] + 60:
                    _cancel(con, state, p, now, 'истекло окно входа')
                    continue
                err = protection(m, now)
                if not filters and err in ('ускорение mark >5% за 60с', 'расхождение mark/index >1%', 'фандинг ниже −0,1%'):
                    err = None
                if err or not m['active']:
                    _cancel(con, state, p, now, err or 'контракт не торгуется')
                    continue
                if now < p['due'] or not book_fresh(m, now) or p.get('book_id') == m['book_id']:
                    save_position(con, p)
                    continue
                fill, px = walk(m['bids'], p['order_qty'], dec(m['step']), consumed.setdefault(p['symbol'], {}), 'bid')
                # Partial entry is kept; remainder is cancelled, never increases the position later.
                if fill == 0:
                    _cancel(con, state, p, now, 'нет ликвидности входа')
                    continue
                if (dec(m['bids'][0][0]) - px) / dec(m['bids'][0][0]) > D('.003') or not D('.02') <= (dec(p['stop']) - px) / px <= D('.12'):
                    _cancel(con, state, p, now, 'изменились стоимость или расстояние стопа')
                    continue
                fee = fill * px * dec(m['fee'])
                margin = fill * px / 2
                reserve = fill * dec(p['reserve_per_qty'])
                actual_held = margin + reserve
                if actual_held + fee > held or fill * (dec(p['stop']) - px) + fee + reserve > dec(p['risk']):
                    _cancel(con, state, p, now, 'изменилась стоимость входа')
                    continue
                state['cash'] = str(dec(state['cash']) + held - actual_held - fee)
                _realize(state, -fee, fee=fee)
                p.update(stage='open', qty=str(fill), initial_qty=str(fill), entry=str(px), opened=now,
                         held=str(actual_held), allocated=str(actual_held + fee), margin=str(margin),
                         risk=str(fill * (dec(p['stop'])-px) + fee + reserve),
                         reserve=str(reserve), fees=str(fee), funding='0', pnl=str(-fee),
                         r=str(dec(p['stop']) - px), target=str(px - 2 * (dec(p['stop']) - px)),
                         book_id=m['book_id'], funding_to=now, liquidation='не проверялась')
                event(con, now, 'entry_fill', dict(p, partial=fill < dec(p['order_qty']), delay=now-p['submitted']))
                save_position(con, p)
                continue
            funding_ok = _funding(con, state, p, m, now)
            qty, held = dec(p['qty']), dec(p['held'])
            mark, entry = dec(m['mark']), dec(p['entry'])
            p['unrealized'] = str((entry - mark) * qty)
            tier = risk_tier(m.get('tiers', []), qty * mark)
            if tier is None or not funding_ok:
                p['liquidation'] = 'неподтверждённые параметры / финансирование'
            else:
                maintenance = max(D(0), qty * mark * dec(tier['maintenanceMargin']) - dec(tier.get('mmDeduction') or '0'))
                close_fee = qty * mark * dec(m['fee'])
                p['liquidation'] = 'проверена по mark и опубликованному риск-тиру'
                if dec(p['margin']) + min(D(0), dec(p['reserve'])) + dec(p['unrealized']) <= maintenance + close_fee:
                    # Bankruptcy outcome is bounded by the paper allocation, with an explicit unverified result.
                    _stress_close(con, state, p, now, 'ликвидация: точный биржевой исход не подтверждён')
                    continue
            if held + dec(p['unrealized']) <= 0:
                _stress_close(con, state, p, now, 'исчерпан выделенный капитал: стрессовый исход')
                continue
            reason = p.get('exit_reason')
            if not m['active']:
                reason = 'делистинг: попытка выхода, итог зависит от ликвидности'
            elif mark >= dec(p['stop']):
                reason = 'стоп по mark'
            elif protection(m, now) == 'ускорение mark >5% за 60с':
                reason = 'аварийный выход: ракета'
            elif mark <= dec(p['target']):
                reason = 'цель 2R'
            elif now - p['opened'] >= 86400:
                reason = '24 часа удержания'
            elif mark <= entry - dec(p['r']) and not p.get('breakeven'):
                # Funds spent on fees and net negative funding must also be recovered.
                cost = max(D(0), dec(p['fees']) - dec(p['funding']))
                be = (entry - cost / qty) / (1 + dec(m['fee']))
                p['stop'] = str(min(dec(p['stop']), (be / dec(m['tick'])).to_integral_value(rounding=ROUND_FLOOR) * dec(m['tick'])))
                p['breakeven'] = True
                event(con, now, 'breakeven', {'position': p['id'], 'stop': p['stop']})
            if reason and p['stage'] != 'exit':
                p.update(stage='exit', due=now + 1, exit_reason=reason)
                event(con, now, 'exit_requested', {'position': p['id'], 'reason': reason})
            if p['stage'] == 'exit' and now >= p['due'] and book_fresh(m, now) and p.get('book_id') != m['book_id']:
                fill, px = walk(m['asks'], qty, dec(m['step']), consumed.setdefault(p['symbol'], {}), 'ask')
                if fill:
                    fee = fill * px * dec(m['fee'])
                    pnl = fill * (entry - px) - fee
                    release = held * fill / qty
                    pending = min(release, max(D(0), dec(p['reserve']))) if fill == qty and not funding_ok else D(0)
                    release -= pending
                    # The paper allocation caps losses even when executable asks jump past bankruptcy.
                    if release + pnl < 0:
                        _stress_close(con, state, p, now, 'выход за пределом капитала: стрессовый исход')
                        continue
                    state['cash'] = str(dec(state['cash']) + release + pnl)
                    _realize(state, pnl, fee=fee)
                    p.update(held=str(held-release), margin=str(dec(p['margin']) * (qty-fill)/qty),
                             reserve=str(dec(p['reserve']) * (qty-fill)/qty), qty=str(qty-fill),
                             fees=str(dec(p['fees'])+fee), pnl=str(dec(p['pnl'])+pnl),
                             unrealized=str((entry-mark)*(qty-fill)),
                             risk=str(dec(p['risk']) * (qty-fill)/qty), book_id=m['book_id'], due=now+1)
                    event(con, now, 'exit_fill', {'position': p['id'], 'qty': str(fill), 'price': str(px), 'fee': str(fee), 'pnl': str(pnl), 'reason': p['exit_reason']})
                    if qty == fill:
                        p.update(stage='funding' if pending else 'closed', closed=now,
                                 unrealized='0', reserve=str(pending), held=str(pending))
            save_position(con, p)
        opened = positions(con, True)
        unreal = sum((dec(p.get('unrealized', '0')) for p in opened), D(0))
        equity = dec(state['cash']) + sum((dec(p['held']) for p in opened), D(0)) + unreal
        state['equity'] = str(equity)
        state['peak'] = str(max(dec(state['peak']), equity))
        dd = 1 - equity / dec(state['peak'])
        state['drawdown'] = str(max(dec(state['drawdown']), dd))
        if dd >= D('.05'):
            if not state['halt']:
                event(con, now, 'drawdown_halt', {'drawdown': str(dd)})
            state['halt'] = True
        daily = (dec(state['day_start']) - equity) / dec(state['day_start']) >= D('.02')
        unresolved = any(p.get('funding_unverified') for p in positions(con))
        if allow_entries and not state['paused'] and not state['halt'] and not daily and not unresolved:
            candidates = sorted(markets.items(), key=lambda item: dec(item[1].get('growth', '0')), reverse=True)
            for sym, m in candidates:
                if len(opened) >= 2:
                    break
                if not m.get('candidate') or any(p['symbol'] == sym for p in opened):
                    continue
                history = [p for p in positions(con) if p['symbol'] == sym]
                if any(now - p.get('closed', now) < 21600 for p in history if p['stage'] == 'closed'):
                    continue
                err = protection(m, now)
                if not filters and err in ('ускорение mark >5% за 60с', 'расхождение mark/index >1%', 'фандинг ниже −0,1%'):
                    err = None
                sig, why = signal(m, now)
                if random_entry:
                    import hashlib
                    edge = int(now // 900) * 900
                    chosen = int(hashlib.sha256(f'{sym}:{edge}'.encode()).hexdigest()[:8], 16) % 50 == 0
                    sig = {'stop': str(dec(m['mark']) * D('1.04')), 'signal_ts': edge, 'reason': 'случайный контроль'} if chosen and now-edge <= 60 else None
                    why = 'нет случайного входа'
                if err or not sig or not book_fresh(m, now) or not m.get('fee_source'):
                    reject(con, state, err or why or 'нет свежего стакана или тарифа', now)
                    continue
                if any(p.get('signal_ts') == sig['signal_ts'] for p in history):
                    continue
                spec = size_order(state, opened, m, sig['stop'])
                if spec is None:
                    reject(con, state, 'минимумы, риск, капитал или ликвидность не позволяют вход', now)
                    continue
                fee = dec(m['fee'])
                reserve_per = (dec(spec['held']) / dec(spec['qty']) - dec(m['bids'][0][0]) / 2 - fee * dec(m['bids'][0][0]))
                p = dict(spec, symbol=sym, stage='entry', qty='0', order_qty=spec['qty'],
                         signal_ts=sig['signal_ts'], reason=sig['reason'], due=now+1, submitted=now,
                         book_id=m['book_id'], reserve_per_qty=str(reserve_per), terms=m['terms'],
                         gap=False, unrealized='0')
                state['cash'] = str(dec(state['cash']) - dec(p['held']))
                cursor = con.execute('INSERT INTO positions(symbol,state) VALUES(?,?)', (sym, dump(p)))
                p['id'] = cursor.lastrowid
                save_position(con, p)
                opened.append(p)
                event(con, now, 'entry_requested', p)
        state['daily_blocked'] = daily
        state['last_tick'] = now
        state['watch'] = [{'symbol': sym, 'growth': m.get('growth'), 'reason': signal(m, now)[1]}
                          for sym, m in markets.items() if m.get('candidate')][:20]
        verify(con, state)
        save_meta(con, state)
        return [dict(row) for row in con.execute('SELECT * FROM events WHERE id>?', (before,))]


def _realize(state, pnl, fee=D(0), funding=D(0)):
    state['realized'] = str(dec(state['realized']) + pnl)
    state['day_pnl'] = str(dec(state['day_pnl']) + pnl)
    state['fees'] = str(dec(state['fees']) + fee)
    state['funding'] = str(dec(state['funding']) + funding)


def _cancel(con, state, p, now, reason):
    state['cash'] = str(dec(state['cash']) + dec(p['held']))
    p.update(stage='closed', held='0', qty='0', closed=now, exit_reason=reason, pnl='0', unrealized='0')
    event(con, now, 'entry_cancelled', {'position': p['id'], 'reason': reason})
    save_position(con, p)


def risk_tier(tiers, notional):
    valid = [t for t in tiers if dec(t['riskLimitValue']) >= notional]
    return min(valid, key=lambda t: dec(t['riskLimitValue'])) if valid else None


def _funding(con, state, p, m, now):
    complete = m.get('funding_complete', False)
    for f in sorted(m.get('funding_history', []), key=lambda f: f['ts']):
        if not p['funding_to'] < f['ts'] <= now:
            continue
        if f.get('mark') is None:
            complete = False
            break
        qty = dec(p['initial_qty'])
        for row in con.execute("SELECT ts,details FROM events WHERE kind='exit_fill' AND ts<?", (f['ts'],)):
            fill = json.loads(row['details'])
            if fill['position'] == p['id']:
                qty -= dec(fill['qty'])
        payment = qty * dec(f['mark']) * dec(f['rate'])
        if dec(p['held']) + payment < 0:
            payment = -dec(p['held'])
        p['held'] = str(dec(p['held']) + payment)
        p['reserve'] = str(dec(p['reserve']) + payment)
        p['funding'] = str(dec(p['funding']) + payment)
        p['pnl'] = str(dec(p['pnl']) + payment)
        p['funding_to'] = f['ts']
        _realize(state, payment, funding=payment)
        event(con, now, 'funding', {'position': p['id'], 'settled': f['ts'], 'amount': str(payment), 'mark_model': 'minute-open'})
    p['funding_unverified'] = not complete
    if not complete:
        state['funding_unverified'] = True
    return complete


def _stress_close(con, state, p, now, reason):
    loss = -dec(p['held'])
    _realize(state, loss)
    p.update(stage='closed', held='0', qty='0', closed=now, exit_reason=reason,
             pnl=str(dec(p['pnl']) + loss), unrealized='0', stress=True)
    event(con, now, 'stress_loss', {'position': p['id'], 'loss': str(loss), 'reason': reason, 'exchange_outcome_verified': False})
    save_position(con, p)


def control(action, path=None, now=None):
    if action not in ('pause', 'resume'):
        raise ValueError('неизвестное действие')
    with connect(path, True) as con:
        state = meta(con)
        if state is None:
            return
        state['paused'] = action == 'pause'
        if action == 'resume':
            state['halt'] = False
            state['peak'] = state['equity']
        event(con, now or time.time(), action, {})
        save_meta(con, state)


def status(path=None):
    with connect(path) as con:
        state = meta(con)
        if state:
            state['positions'] = positions(con)
        return state


def report(section='', path=None):
    state = status(path)
    if state is None:
        return '📉 <b>Шорты альтов · виртуально</b>\n\nОжидаю активацию и свежий стартовый курс RUB/USDT.'
    lines = ['📉 <b>Шорты альтов · Bybit · виртуально</b>', '',
             'Режим: ' + ('⏸ Входы остановлены' if state['paused'] or state['halt'] or state.get('daily_blocked') else '▶️ Автономный прогон'), '']
    uncertain = any(p.get('funding_unverified') or p.get('gap') or p.get('stress') for p in state['positions'])
    if uncertain:
        lines.extend(['⚠️ Есть непроверенные промежутки, расходы или стрессовые исходы. Итог сценарный, не подтверждённый биржей.', ''])
    if section in ('', 'risk', 'results'):
        held = sum((dec(p['held']) for p in state['positions'] if p['stage'] != 'closed'), D(0))
        lines += ['<b>Баланс USDT</b>', f"Начальный: {dec(state['initial']):.4f}",
                  f"Свободно: {dec(state['cash']):.4f}", f'Выделено позициям: {held:.4f}', '',
                  '<b>Результат после расходов</b>', f"Реализовано: {dec(state['realized']):+.4f} USDT",
                  f"Комиссии, уже учтены: {dec(state['fees']):.4f} USDT",
                  f"Финансирование, уже учтено: {dec(state['funding']):+.4f} USDT",
                  f"Оценка с открытыми позициями: {dec(state['equity']):.4f} USDT",
                  f"Максимальная просадка: {dec(state['drawdown'])*100:.2f}%", '',
                  '<b>Защита капитала</b>', 'Риск по стопу: ≤0,5% / суммарно ≤1%',
                  'Выделенный капитал: ≤2% / суммарно ≤4%', 'Максимум 2 позиции · плечо ≤2×',
                  'Изолированная маржа · автопополнение выключено', 'Усреднение и увеличение позиции запрещены', '',
                  f"Стартовый курс: {html.escape(state['rub_rate'])} ₽/USDT",
                  'Источник: ' + html.escape(state['rate_source'])]
    if section in ('', 'positions'):
        lines += ['', '<b>Позиции</b>']
        opened = [p for p in state['positions'] if p['stage'] != 'closed']
        if not opened:
            lines.append('Открытых позиций нет.')
        for p in opened:
            lines += ['', '<b>' + html.escape(p['symbol']) + '</b>',
                      'Этап: ' + {'entry': 'ожидание входа', 'open': 'открыт шорт', 'exit': 'закрытие', 'funding': 'сверка расходов; актив закрыт'}.get(p['stage'], p['stage']),
                      'Причина: ' + html.escape(p['reason']),
                      f"Количество: {p['qty']} · стоп: {p['stop']}",
                      f"Цель: {p.get('target', 'после исполнения входа')}",
                      f"Риск: {dec(p['risk']):.4f} USDT · выделено: {dec(p['held']):.4f}",
                      'Состояние защиты: ' + ('⚠️ Был непроверенный промежуток' if p.get('gap') else 'наблюдаемые снимки'),
                      'Ликвидация: ' + p.get('liquidation', 'нет позиции')]
        closed = [p for p in state['positions'] if p['stage'] == 'closed' and p.get('opened')][-3:]
        if closed:
            lines.extend(['', '<b>Последние выходы</b>'])
            for p in closed:
                lines.extend(['', '<b>' + html.escape(p['symbol']) + '</b>',
                              'Причина: ' + html.escape(p['exit_reason']),
                              f"Результат после расходов: {dec(p['pnl']):+.4f} USDT",
                              'Исход: ' + ('стрессовый, биржевой результат не подтверждён' if p.get('stress') else 'виртуальное исполнение')])
    if section == 'candidates':
        lines += ['<b>Кандидаты</b>']
        for p in state['watch']:
            lines += ['', '<b>' + html.escape(p['symbol']) + '</b>',
                      f"Рост 24ч: {dec(p['growth'])*100:.2f}%", html.escape(p['reason'] or 'подтверждение получено')]
        if not state['watch']:
            lines.append('Подходящих кандидатов нет.')
    if section == 'results':
        losses = [p for p in state['positions'] if p.get('stress')]
        lines += ['', f'Потерь при отказе защиты: {len(losses)}',
                  'Исторический анализ: недостаточно независимых снимков; /shorts research.']
    if state.get('last_rejection'):
        lines += ['', '<b>Последний отказ</b>', html.escape(state['last_rejection'])]
    if state.get('last_error'):
        lines += ['', '<b>Данные</b>', html.escape(state['last_error'])]
    lines += ['', 'Результат сценарный. Стоп и индикаторы не гарантируют защиту.']
    return '\n'.join(lines)


def export(destination, path=None):
    with connect(path) as con, open(destination, 'w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['id', 'timestamp_utc', 'event', 'details'])
        writer.writerows(tuple(row) for row in con.execute('SELECT * FROM events ORDER BY id'))


def record_catalog(items, now, path=None):
    with connect(path, True) as con:
        con.execute('INSERT OR IGNORE INTO catalog VALUES(?,?)', (now, zlib.compress(dump(items).encode('utf-8'))))


def record_error(reason, path=None):
    with connect(path, True) as con:
        state = meta(con)
        if state:
            state['last_error'] = str(reason)
            save_meta(con, state)
