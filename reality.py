"""Independent RUB round trips on public snapshots. Never sends an order/payment.

The USDT strategy journal is a child of the round trip, not additional capital.
All receipts below are scenario evidence, never personal banking confirmation.
"""
import calendar
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import csv
import datetime as dt
import html
import json
import os
import sqlite3
import time

import p2p
import shorts
import settlement
import trades

ROOT = os.path.join(os.path.dirname(__file__), 'data')
VERSION = 'rub-roundtrip-v2'
CHECKED = '2026-10-10'
VALID_UNTIL = dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc).timestamp()
D = Decimal
CENT = D('.01')
POLICY = {'version': VERSION, 'payment_seconds': 300, 'release_seconds': 900,
          'wallet_seconds': 60, 'observation_seconds': 86400, 'initial_rub': '50000',
          'bank': 'tbank-black', 'service_rub': '99', 'role': 'taker',
          'bank_permission': 'unknown', 'personal_eligibility': 'assumed',
          'tax_status': 'unknown', 'funding_model': 'exchange-wallet-v2'}
UNKNOWN = ['Допуск личного аккаунта и разрешение банка на регулярный P2P неизвестны',
           'Задержки и банковские зачисления сценарные, не подтверждения операций',
           'Проверки банка и споры не имеют подтверждённых вероятностей',
           'Налоговый режим не подтверждён; результат до налогов',
           'Исторические стаканы и банковские события не восстановлены из свечей']


def path():
    return os.path.join(ROOT, 'rub_roundtrips.db')


@contextmanager
def connect(destination=None):
    destination = destination or path()
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    con = sqlite3.connect(destination, timeout=15)
    con.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, data TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, details TEXT)')
    settlement.schema(con)
    con.commit()
    con.execute('BEGIN IMMEDIATE')
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def event(con, now, kind, details):
    con.execute('INSERT INTO events(ts,kind,details) VALUES (?,?,?)',
                (now, kind, json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)))


def load(con):
    row = con.execute('SELECT data FROM state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def save(con, state):
    con.execute('INSERT OR REPLACE INTO state VALUES (1,?)',
                (json.dumps(state, ensure_ascii=False, sort_keys=True),))


def costs_valid(costs, now):
    try:
        return bool(costs and costs['source'] and costs['checked'] <= now <= costs['valid_until']
                    and all(shorts.dec(costs[k]) >= 0 for k in ('wallet_in_usdt','wallet_out_usdt','incoming_rub')))
    except (KeyError, TypeError, ValueError):
        return False


def initialize(now=None, destination=None, costs=None):
    now = time.time() if now is None else now
    with connect(destination) as con:
        if load(con):
            return False
        s = {'version': VERSION, 'policy': dict(POLICY), 'created': now, 'stage': 'buy',
             'due': now + POLICY['payment_seconds'], 'cash_rub': '49901', 'usdt': '0',
             'pending_rub': '0', 'service': '99', 'service_debt': '0', 'bank_fees': '0',
             'p2p_fees_usdt': '0', 'realized_rub': None, 'paused': False,
             'service_periods': [month_key(now)], 'bank_usage': {}, 'consumed': {},
             'last_reason': '', 'assumptions': list(UNKNOWN), 'strategy': None}
        s['costs'] = costs
        s['internal_fees_usdt'] = '0'
        save(con, s)
        event(con, now, 'initialize', {'initial_rub': '50000', 'policy': POLICY, 'source_checked': CHECKED})
        event(con, now, 'service', {'rub': '99', 'free_conditions': 'not_verified'})
        return True


def month_key(now):
    return dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=3))).strftime('%Y-%m')


def p2p_fee(role, side, level='general', now=None):
    now = time.time() if now is None else now
    if now > VALID_UNTIL or role not in ('maker', 'taker') or side not in ('buy', 'sell'):
        raise ValueError('Нет свежей модели комиссии P2P')
    if role == 'taker' or side == 'buy':
        return D(0)
    rates = {'general': D('.003'), 'verified': D('.00275'), 'block': D('.0025')}
    if level not in rates:
        raise ValueError('Неизвестен уровень рекламодателя')
    return rates[level]


def bank_fee(amount, used):
    # SBP regulatory maximum: free threshold is NOT an allowed turnover limit.
    excess = max(D(0), D(used) + D(amount) - D(100000)) - max(D(0), D(used) - D(100000))
    return min(D(1500), excess * D('.005')).quantize(CENT, rounding=ROUND_UP)


def offers(snap, side, now):
    if snap is None or not 0 <= now - getattr(snap, 'ts', 0) <= 120:
        return []
    ads = snap.groups.get(('Bybit', side, 'USDT'), [])
    return sorted((a for a in ads if a.ad_id and not a.stale and 0 < a.fetched_ts <= now
                   and now-a.fetched_ts <= 120 and D(str(a.price)) > 0
                   and any(trades.is_sbp(p) for p in a.pays)
                   and not any(x in a.terms.lower() for x in ('third party', 'третьих лиц', 'только ип', 'комиссия'))),
                  key=lambda a: a.price, reverse=side == 'sell')


def quote(s, ad, wanted, side, now):
    price = D(str(ad.price))
    used = D(s['consumed'].get(side + ':' + ad.ad_id, '0'))
    qty = min(D(wanted), max(D(0), D(str(ad.avail))-used), D(str(ad.max_amt))/price)
    qty = qty.quantize(D('.00000001'), rounding=ROUND_DOWN)
    if qty <= 0 or qty*price < D(str(ad.min_amt)):
        return None
    return {'id': ad.ad_id, 'quantity': str(qty), 'price': str(price), 'side': side,
            'fetched': ad.fetched_ts, 'terms': ad.terms, 'payment_channel': 'sbp',
            'evidence': 'public_ad_scenario_eligibility', 'tariff_checked': CHECKED,
            'p2p_fee': str(p2p_fee('taker', side, now=now))}


def consume(s, q):
    key = q['side'] + ':' + q['id']
    s['consumed'][key] = str(D(s['consumed'].get(key, '0')) + D(q['quantity']))


def block(con, s, now, reason):
    if s['last_reason'] != reason:
        event(con, now, 'blocked', {'reason': reason})
    s['last_reason'] = reason


def tick(snap=None, markets=None, now=None, destination=None):
    """Durable one-stage progression. Missing data keeps capital at its current stage."""
    now = time.time() if now is None else now
    with connect(destination) as con:
        s = load(con)
        if not s:
            return
        stage = s['stage']
        # Charge every anniversary month once, including missed periods after downtime.
        start = dt.datetime.fromtimestamp(s['created'], dt.timezone.utc)
        current = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        year, month = start.year, start.month
        while (year, month) <= (current.year, current.month):
            day = min(start.day, calendar.monthrange(year, month)[1])
            anniversary = start.replace(year=year, month=month, day=day).timestamp()
            key = month_key(anniversary)
            if anniversary <= now and key not in s['service_periods']:
                s['service_periods'].append(key)
                charge = min(D(s['cash_rub']), D(99))
                s['cash_rub'] = str(D(s['cash_rub'])-charge)
                s['service_debt'] = str(D(s['service_debt'])+D(99)-charge)
                s['service'] = str(D(s['service'])+D(99))
                event(con, now, 'service', {'rub': '99', 'period': key, 'unpaid_rub': str(D(99)-charge)})
            month += 1
            if month == 13:
                year, month = year+1, 1
        bank_ok = settlement.available(con, POLICY['bank'])
        venue_ok = settlement.available(con, 'Bybit:UTA')
        if stage in ('buy', 'sell', 'receipt') and not bank_ok:
            block(con, s, now, 'Банковские средства ограничены; нужен отдельный факт снятия ограничения')
        elif stage in ('buy','wallet_in','wallet_out','receipt') and not costs_valid(s.get('costs'),now):
            block(con, s, now, 'Не подтверждена стоимость внутренних переводов и входящего платежа; неизвестные расходы не равны нулю')
        elif stage in ('wallet_in','wallet_out') and not settlement.available(con,'Bybit:Funding'):
            block(con, s, now, 'Средства Funding ограничены; перевод не подтверждён')
        elif stage in ('wallet_in','trade','wallet_out') and not venue_ok:
            if s['strategy']:
                shorts.tick({},now,s['strategy'],allow_entries=False)
            block(con,s,now,'Торговый кошелёк ограничен; исполнения и перевод не подтверждены')
        elif now < s['due']:
            pass
        elif stage == 'buy' and not s['paused']:
            if now > VALID_UNTIL:
                block(con, s, now, 'Истёк срок проверки тарифов')
            else:
                usage = D(s['bank_usage'].get(month_key(now), '0'))
                budget = D(s['cash_rub'])
                low, high = 0, int(budget/CENT)
                while low < high:
                    mid = (low+high+1)//2
                    if mid*CENT + bank_fee(mid*CENT, usage) <= budget:
                        low = mid
                    else:
                        high = mid-1
                principal = low*CENT
                for ad in offers(snap, 'buy', now):
                    q = quote(s, ad, principal/D(str(ad.price)), 'buy', now)
                    if not q:
                        continue
                    qty = D(q['quantity'])
                    paid = (qty*D(q['price'])).quantize(CENT, rounding=ROUND_UP)
                    fee = bank_fee(paid, usage)
                    if paid+fee > budget:
                        continue
                    consume(s, q)
                    s.update(stage='release', due=now+POLICY['release_seconds'], usdt=str(qty),
                             cash_rub=str(budget-paid-fee), bank_fees=str(D(s['bank_fees'])+fee),
                             last_reason='', entry_quote=q)
                    s['bank_usage'][month_key(now)] = str(usage+paid)
                    event(con, now, 'bank_payment', dict(q, rub=str(paid), bank_fee=str(fee)))
                    break
                else:
                    block(con, s, now, 'Нет свежего совместимого объявления или достаточной ликвидности')
        elif stage == 'release':
            s.update(stage='wallet_in', due=now+POLICY['wallet_seconds'])
            event(con, now, 'escrow_release', {'usdt': s['usdt'], 'evidence': 'scenario_delay'})
        elif stage == 'wallet_in':
            fee = D(s['costs']['wallet_in_usdt'])
            if fee >= D(s['usdt']):
                block(con,s,now,'Остаток меньше стоимости перевода в торговый кошелёк')
                save(con,s)
                return
            net = D(s['usdt'])-fee
            # Initialization is idempotent if the child commit survived a parent crash.
            child = os.path.splitext(destination or path())[0] + '_shorts_v2.db'
            rate = D(s['entry_quote']['price'])
            shorts.initialize(rate, 'Fresh Bybit P2P buy quote; service and bank fee recorded in RUB journal',
                              now, child, capital=net, funding_model='exchange-wallet-v2')
            initialized = shorts.status(child)
            if D(initialized['initial']) != net or initialized.get('funding_model') != 'exchange-wallet-v2':
                raise ArithmeticError('Existing child journal belongs to a different capital/model')
            s.update(stage='trade', due=now+POLICY['observation_seconds'], strategy=child,usdt=str(net),
                     internal_fees_usdt=str(D(s['internal_fees_usdt'])+fee))
            event(con, now, 'wallet_credit', {'usdt': s['usdt'], 'wallet': 'strategy', 'child': os.path.basename(child),
                                             'fee_usdt': str(fee), 'cost_source':s['costs']['source'],
                                             'evidence': 'scenario_internal_transfer'})
        elif stage == 'trade':
            # Before due, process trades too (handled below); after due forbid new entries.
            shorts.tick(markets or {}, now, s['strategy'], allow_entries=False)
            account = shorts.status(s['strategy'])
            if not any(p['stage'] != 'closed' for p in account['positions']):
                if D(account.get('funding_debt', '0')) or any(p.get('funding_unverified') for p in account['positions']):
                    block(con, s, now, 'Не сверены обязательства funding; вывод не моделируется')
                else:
                    s.update(stage='wallet_out', due=now+POLICY['wallet_seconds'], usdt=account['cash'])
                    event(con, now, 'wallet_return', {'usdt': s['usdt'], 'realized_usdt': account['realized'],
                                                    'fees_usdt': account['fees'], 'funding_usdt': account['funding'],
                                                    'stress_outcomes': sum(bool(p.get('stress')) for p in account['positions'])})
        elif stage == 'wallet_out':
            fee = D(s['costs']['wallet_out_usdt'])
            if fee > D(s['usdt']):
                block(con,s,now,'Остаток меньше стоимости возврата в Funding')
                save(con,s)
                return
            s['usdt'] = str(D(s['usdt'])-fee)
            s['internal_fees_usdt'] = str(D(s['internal_fees_usdt'])+fee)
            s.update(stage='sell', due=now+POLICY['payment_seconds'])
            event(con, now, 'funding_wallet_credit', {'usdt': s['usdt'], 'fee_usdt':str(fee),
                                                    'cost_source':s['costs']['source'], 'evidence': 'scenario_internal_transfer'})
        elif stage == 'sell':
            if now > VALID_UNTIL:
                block(con, s, now, 'Истёк срок проверки комиссии выхода')
            elif D(s['usdt']) == 0:
                s.update(stage='receipt', due=now)
            else:
                for ad in offers(snap, 'sell', now):
                    q = quote(s, ad, s['usdt'], 'sell', now)
                    if not q:
                        continue
                    consume(s, q)
                    proceeds = (D(q['quantity'])*D(q['price'])).quantize(CENT, rounding=ROUND_DOWN)
                    s.update(stage='receipt', due=now+POLICY['payment_seconds'],
                             usdt=str(D(s['usdt'])-D(q['quantity'])), pending_rub=str(proceeds), exit_quote=q)
                    event(con, now, 'sell_escrow', dict(q, pending_rub=str(proceeds)))
                    break
                else:
                    block(con, s, now, 'Продажа невозможна: нет свежего объявления с подходящими лимитами')
        elif stage == 'receipt':
            received = D(s['pending_rub'])
            fee = D(s['costs']['incoming_rub']) if received else D(0)
            if fee > received:
                block(con,s,now,'Комиссия получения превышает платёж')
                save(con,s)
                return
            s['bank_fees'] = str(D(s['bank_fees'])+fee)
            received -= fee
            debt = min(D(s['service_debt']), received)
            s.update(cash_rub=str(D(s['cash_rub'])+received-debt), pending_rub='0',
                     service_debt=str(D(s['service_debt'])-debt), last_reason='')
            event(con, now, 'bank_receipt', {'rub': str(received), 'service_debt_paid': str(debt),
                                           'incoming_fee':str(fee),
                                           'evidence': 'scenario_credit_not_bank_confirmation'})
            if D(s['usdt']) > 0:
                s.update(stage='sell', due=now+POLICY['payment_seconds'])
            else:
                s.update(stage='done', due=now, completed=now,
                         realized_rub=str(D(s['cash_rub'])-D(s['service_debt'])-D(50000)))
                event(con, now, 'complete', {'available_rub': s['cash_rub'], 'net_before_tax_rub': s['realized_rub']})
        if s['stage'] == 'trade' and now < s['due'] and venue_ok:
            shorts.tick(markets or {}, now, s['strategy'], allow_entries=not s['paused'] and D(s['service_debt']) == 0)
        if s['stage'] == 'done':
            s['realized_rub'] = str(D(s['cash_rub'])-D(s['service_debt'])-D(50000))
        verify(con, s)
        save(con, s)


def verify(con, s):
    if any(D(s[k]) < 0 for k in ('cash_rub', 'usdt', 'pending_rub', 'service_debt')):
        raise ArithmeticError('Negative round-trip holding')
    cash = D(50000)
    pending = D(0)
    for kind, raw in con.execute('SELECT kind,details FROM events ORDER BY id'):
        d = json.loads(raw)
        if kind == 'service':
            cash -= D(d['rub'])-D(d.get('unpaid_rub', '0'))
        elif kind == 'bank_payment':
            cash -= D(d['rub'])+D(d['bank_fee'])
        elif kind == 'sell_escrow':
            pending += D(d['pending_rub'])
        elif kind == 'bank_receipt':
            cash += D(d['rub'])-D(d['service_debt_paid'])
            pending -= D(d['rub'])+D(d.get('incoming_fee','0'))
    if cash != D(s['cash_rub']) or pending != D(s['pending_rub']):
        raise ArithmeticError('RUB event journal does not reconcile')


def status(destination=None):
    if not os.path.exists(destination or path()):
        return None
    with connect(destination) as con:
        s = load(con)
        if s:
            s['bank_available'] = settlement.available(con, POLICY['bank'])
            s['venue_available'] = settlement.available(con, 'Bybit:UTA')
        return s


def control(action, destination=None):
    if action not in ('pause', 'resume'):
        raise ValueError('Неизвестное действие')
    with connect(destination) as con:
        s = load(con)
        if s:
            s['paused'] = action == 'pause'
            if s['strategy'] and s['stage'] == 'trade':
                shorts.control(action,s['strategy'])
            save(con, s)
            event(con, time.time(), action, {'entries_only': True})


def configure_costs(costs, destination=None, now=None):
    now = time.time() if now is None else now
    if not costs_valid(costs,now):
        raise ValueError('Нужны суммы, источник и действующий срок модели расходов')
    with connect(destination) as con:
        s = load(con)
        if not s:
            raise ValueError('Цикл не инициализирован')
        s['costs'] = dict(costs)
        save(con,s)
        event(con,now,'cost_model',costs)


def report(destination=None):
    s = status(destination)
    if not s:
        return ('🔄 <b>Полный рублёвый цикл шортов · v2</b>\n\n'
                'Отдельный прогон ещё не активирован. Старый USDT-портфель сохранён.\n'
                'Начало: 50 000 ₽ → покупка USDT → шорты → продажа USDT → рубли.\n'
                'Результат до налогов. Личные допуски не подтверждены.')
    lines = ['🔄 <b>Полный рублёвый цикл шортов · v2</b>', '',
             'Это самостоятельная альтернатива капитала; баланс дочернего USDT-журнала не суммируется.', '',
             '<b>Доступность</b>', 'Этап: '+html.escape(s['stage']),
             f"Доступные рубли: {D(s['cash_rub']) if s['bank_available'] else D(0):.2f} ₽",
             f"Ограниченные рубли: {D(0) if s['bank_available'] else D(s['cash_rub']):.2f} ₽",
             f"Ожидается зачисление: {D(s['pending_rub']):.2f} ₽", '',
             '<b>Расходы и результат</b>', f"Обслуживание: {D(s['service']):.2f} ₽",
             f"Банковские комиссии: {D(s['bank_fees']):.2f} ₽",
             f"Внутренние перемещения: {D(s.get('internal_fees_usdt','0')):.4f} USDT",
             f"Неоплаченное обслуживание: {D(s['service_debt']):.2f} ₽",
             'Заработанные рубли: '+(f"{D(s['realized_rub']):+.2f} ₽" if s['realized_rub'] is not None else 'цикл не завершён'),
             'Нереализованная оценка не включена в заработанные рубли.']
    if s['strategy']:
        account = shorts.status(s['strategy'])
        lines += ['', '<b>Торговый кошелёк USDT</b>', f"Свободно: {D(account['cash']):.4f}",
                  f"Реализовано после расходов: {D(account['realized']):+.4f}",
                  f"Комиссии: {D(account['fees']):.4f} · funding: {D(account['funding']):+.4f}",
                  'После возврата это архив торгового этапа, а не дополнительный доступный баланс.']
    lines += ['', '<b>Неподтверждённые условия</b>'] + ['• '+html.escape(x) for x in s['assumptions']]
    if s['last_reason']:
        lines += ['', 'Отказ: '+html.escape(s['last_reason'])]
    return '\n'.join(lines)


def export(destination, source=None):
    with connect(source) as con, open(destination, 'w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['journal', 'id', 'timestamp_utc', 'event', 'details'])
        writer.writerows(('rub', *row) for row in con.execute('SELECT * FROM events ORDER BY id'))
        s = load(con)
        if s and s['strategy']:
            with shorts.connect(s['strategy']) as child:
                writer.writerows(('usdt', *tuple(row)) for row in child.execute('SELECT * FROM events ORDER BY id'))
