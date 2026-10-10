"""Conservative, offline bank account ledger for paper execution only.

All mutations use the portfolio's SQLite transaction. No bank API or credentials.
An unknown personal limit/permission is a rejection, never an unlimited allowance.
"""
import calendar
import datetime as dt
import json
import math
import os
from contextvars import ContextVar
from decimal import Decimal, ROUND_UP, ROUND_DOWN

CENT = Decimal('0.01')
ZERO = Decimal(0)
MSK = dt.timezone(dt.timedelta(hours=3))
PROFILE_PATH = os.path.join(os.path.dirname(__file__), 'data', 'paper_bank_profiles.json')
SCENARIO = ContextVar('paper_bank_scenario', default=None)


def mode():
    return 'strict' if SCENARIO.get() is not None else os.getenv('PAPER_BANK_MODEL', 'strict')


class Blocked(ValueError):
    pass


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('Invalid nonnegative bank amount')
    return result


def schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS bank_accounts (id TEXT PRIMARY KEY, cash TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS bank_payments (id INTEGER PRIMARY KEY, account TEXT, scope TEXT, '
                'ts REAL, direction TEXT, method TEXT, amount TEXT, fee TEXT, run_id INTEGER)')
    con.execute('CREATE TABLE IF NOT EXISTS bank_reservations (run_id INTEGER PRIMARY KEY, account TEXT, '
                'scope TEXT, method TEXT, amount TEXT, ts REAL)')
    con.execute('CREATE TABLE IF NOT EXISTS bank_events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, details TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS bank_transfers (id INTEGER PRIMARY KEY, source TEXT, target TEXT, '
                'amount TEXT, fee TEXT, method TEXT, sent REAL, due REAL, state TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS bank_expenses (id INTEGER PRIMARY KEY, account TEXT, '
                'period REAL, amount TEXT, kind TEXT, UNIQUE(account,period,kind))')


def event(con, kind, details, now):
    con.execute('INSERT INTO bank_events(ts,kind,details) VALUES (?,?,?)',
                (now, kind, json.dumps(details, ensure_ascii=False)))


def load(path=None):
    if path is None and SCENARIO.get() is not None:
        return SCENARIO.get()['profiles']
    try:
        with open(path or os.getenv('PAPER_BANK_PROFILES') or PROFILE_PATH, encoding='utf-8-sig') as f:
            data = json.load(f)
        if data['version'] != 1 or not isinstance(data['accounts'], list):
            raise ValueError('Invalid bank profile document')
        ids = [p['id'] for p in data['accounts']]
        if len(ids) != len(set(ids)) or not ids or data['start_account'] not in ids:
            raise ValueError('Invalid bank account identifiers')
        scopes = {}
        for p in data['accounts']:
            if not isinstance(p['id'], str) or not isinstance(p['bank'], str):
                raise ValueError('Invalid account identity')
            if p.get('scope'):
                signature = (p['bank'], p.get('methods'), p.get('billing_day'), p.get('aggregate'))
                if p['scope'] in scopes and scopes[p['scope']] != signature:
                    raise ValueError('Conflicting rules for shared bank scope')
                scopes[p['scope']] = signature
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Blocked('Нет корректного реестра банковских профилей') from exc


def validate(profile, now):
    if SCENARIO.get() is not None:
        from scenarios import validate_profile
        return validate_profile(profile, now)
    names = {'tariff_confirmed': 'персональный тариф', 'limits_confirmed': 'персональные лимиты',
             'shared_limits_confirmed': 'общие лимиты клиента и счетов',
             'ownership_confirmed': 'принадлежность счёта владельцу',
             'p2p_conditions_confirmed': 'условия использования счёта для P2P'}
    for key in names:
        if profile.get(key) is not True:
            raise Blocked('Не подтверждено: ' + names[key])
    if profile.get('status') != 'active':
        raise Blocked('Счёт недоступен или ограничен банком')
    if not profile.get('sources') or not profile.get('scope'):
        raise Blocked('Нет источника условий или группы общих лимитов')
    checked = float(profile.get('checked_at', 0))
    expires = float(profile.get('valid_until', 0))
    if not all(math.isfinite(x) for x in (checked, expires)) or not 0 < checked <= now <= expires:
        raise Blocked('Проверка персональных условий отсутствует или устарела')
    service = profile.get('service', {})
    if service.get('confirmed') is not True:
        raise Blocked('Не подтверждены расходы обслуживания счёта')
    try:
        for charge in service.get('charges', []):
            period = float(charge['period'])
            if not math.isfinite(period) or period < 0:
                raise ValueError('Invalid charge date')
            money(charge['amount'])
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked('Некорректные подтверждённые расходы обслуживания') from exc


def period_start(now, period, billing_day=1):
    date = dt.datetime.fromtimestamp(now, MSK)
    if period == 'day':
        return date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if period == 'month':
        return date.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    if period != 'billing' or not isinstance(billing_day, int) or not 1 <= billing_day <= 31:
        raise Blocked('Неизвестен расчётный период')
    year, month = date.year, date.month
    day = min(billing_day, calendar.monthrange(year, month)[1])
    if date.day < day:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    day = min(billing_day, calendar.monthrange(year, month)[1])
    return dt.datetime(year, month, day, tzinfo=MSK).timestamp()


def usage(con, profile, direction, method, now, period, exclude_run=None, reservations=True):
    since = period_start(now, period, profile.get('billing_day'))
    rule = profile['aggregate'][direction] if method == '*' else profile['methods'][method][direction]
    baseline = rule.get('baseline', {}).get(period)
    if not baseline or baseline.get('period_start') != since:
        raise Blocked('Не подтверждён использованный лимит: ' + period)
    raw_count = baseline['count']
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        raise Blocked('Некорректное число уже выполненных операций')
    total, count = money(baseline['amount']), raw_count
    if count < 0:
        raise ValueError('Invalid baseline count')
    condition, params = ('', ()) if method == '*' else (' AND method=?', (method,))
    for amount, in con.execute('SELECT amount FROM bank_payments WHERE scope=? AND direction=? '
                              'AND ts>=? AND ts<=?' + condition,
                              (profile['scope'], direction, since, now) + params):
        total += money(amount)
        count += 1
    if direction == 'out' and reservations:
        for run, amount in con.execute('SELECT run_id,amount FROM bank_reservations WHERE scope=?' + condition,
                                       (profile['scope'],) + params):
            if run != exclude_run:
                total += money(amount)
                count += 1
    return total, count


def quote(con, profile, method, direction, amount, now, run_id=None):
    import settlement
    if not settlement.available(con, profile['id'], profile['scope']):
        raise Blocked('Счёт ограничен; срок проверки не означает снятие ограничения')
    if SCENARIO.get() is not None:
        from scenarios import quote
        return quote(con, profile, method, direction, amount, now, run_id)
    validate(profile, now)
    amount = money(amount).quantize(CENT, rounding=ROUND_UP if direction == 'out' else ROUND_DOWN)
    try:
        for charge in profile['service'].get('charges', []):
            if (charge.get('confirmed') is True and float(charge['period']) <= now
                    and money(charge['amount']) > 0 and not con.execute(
                        'SELECT 1 FROM bank_expenses WHERE account=? AND period=? AND kind=?',
                        (profile['id'], float(charge['period']), 'service')).fetchone()):
                raise Blocked('Не списаны подтверждённые расходы обслуживания')
        rule = profile['methods'][method][direction]
        if rule.get('confirmed') is not True:
            raise Blocked('Условия способа перевода не подтверждены')
        if amount <= 0 or amount > money(rule['per_operation']):
            raise Blocked('Лимит одной операции')
        for period in ('day', 'month', 'billing'):
            limit = rule['limits'][period]
            used, count = usage(con, profile, direction, method, now, period, run_id)
            if isinstance(limit['count'], bool) or int(limit['count']) != limit['count']:
                raise Blocked('Некорректный лимит количества операций')
            if used + amount > money(limit['amount']) or count + 1 > int(limit['count']):
                raise Blocked('Превышен лимит: ' + period)
            shared = profile['aggregate'][direction]['limits'][period]
            all_used, all_count = usage(con, profile, direction, '*', now, period, run_id)
            if all_used + amount > money(shared['amount']) or all_count + 1 > int(shared['count']):
                raise Blocked('Превышен общий лимит клиента/счёта: ' + period)
        tariff = rule['fee']
        if (money(tariff['percent']) > 100 or money(tariff['minimum']) > money(tariff['maximum'])
                or (money(tariff['percent']) > 0 and money(tariff['maximum']) == 0)):
            raise Blocked('Некорректная комиссия банка')
        used, _ = usage(con, profile, direction, method, now, tariff['period'], run_id, reservations=False)
        free = money(tariff['free'])
        chargeable = max(ZERO, used + amount - free) - max(ZERO, used - free)
        fee = chargeable * money(tariff['percent']) / 100
        if chargeable:
            fee = max(fee, money(tariff['minimum']))
            fee = min(fee, money(tariff['maximum']))
        fee += money(tariff['fixed'])
        return {'amount': str(amount), 'fee': str(fee.quantize(CENT, rounding=ROUND_UP)),
                'account': profile['id'], 'scope': profile['scope'], 'method': method,
                'direction': direction, 'profile_revision': profile['revision']}
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, Blocked):
            raise
        raise Blocked('Неполные или некорректные персональные условия') from exc


def affordable(con, profile, method, budget, now, run_id):
    """Maximum principal whose separate bank fee fits the reserved RUB budget."""
    low, high = 0, int(money(budget) / CENT)
    while low < high:
        mid = (low + high + 1) // 2
        try:
            q = quote(con, profile, method, 'out', mid * CENT, now, run_id)
            fits = money(q['amount']) + money(q['fee']) <= money(budget)
        except Blocked:
            fits = False
        if fits:
            low = mid
        else:
            high = mid - 1
    return low * CENT


def compatible(profile, pays):
    import trades
    banks = {trades.bank_of(p) for p in pays} - {''}
    if profile['bank'] in banks and 'intra' in profile.get('methods', {}):
        return 'intra'
    if any(trades.is_sbp(p) for p in pays) and 'sbp' in profile.get('methods', {}):
        return 'sbp'
    if SCENARIO.get() is not None:
        text = ' '.join(pays).lower()
        if any(s in text for s in ('card number', 'по номеру карты')) and 'card_number' in profile['methods']:
            return 'card_number'
        if any(s in text for s in ('bank requisites', 'по реквизитам')) and 'requisites' in profile['methods']:
            return 'requisites'
    return None  # A named foreign bank is not proof that an ad accepts SBP.


def select(con, data, pays, direction, amount, now, run_id=None, venue=None):
    reasons = []
    accounts = data['accounts']
    if SCENARIO.get() is not None and direction == 'out':
        def cost(profile):
            method = compatible(profile, pays)
            try:
                return money(quote(con, profile, method, direction, amount, now, run_id)['fee'])
            except (Blocked, ValueError):
                return Decimal('Infinity')
        accounts = sorted(accounts, key=cost)
    for profile in accounts:
        method = compatible(profile, pays)
        if not method:
            continue
        try:
            if venue is not None and profile.get('venues', {}).get(venue) is not True:
                raise Blocked('Не подтверждены KYC и собственный платёжный счёт на площадке: ' + venue)
            q = quote(con, profile, method, direction, amount, now, run_id)
            if direction == 'out':
                row = con.execute('SELECT cash FROM bank_accounts WHERE id=?', (profile['id'],)).fetchone()
                if not row or money(row[0]) < money(amount):
                    raise Blocked('Недостаточно денег на выбранном счёте')
            return profile, method, q
        except (Blocked, ValueError, TypeError, KeyError) as exc:
            reasons.append(profile['id'] + ': ' + str(exc))
    raise Blocked('; '.join(reasons) or 'Нет подтверждённого совместимого банковского счёта')


def initialize(con, data, now):
    """One-time attribution of existing free RUB; never add a second capital deposit."""
    schema(con)
    import settlement
    settlement.schema(con)
    for profile in data['accounts']:
        con.execute('INSERT OR REPLACE INTO account_scopes VALUES (?,?)', (profile['id'],profile['scope']))
    if con.execute('SELECT 1 FROM bank_accounts LIMIT 1').fetchone():
        for profile in data['accounts']:
            if not con.execute('SELECT 1 FROM bank_accounts WHERE id=?', (profile['id'],)).fetchone():
                con.execute('INSERT INTO bank_accounts VALUES (?,?)', (profile['id'], '0'))
                event(con, 'account_added', {'account': profile['id']}, now)
        return
    row = con.execute('SELECT cash FROM wallet').fetchone()
    cash = row[0] if row else '50000.00'
    for profile in data['accounts']:
        con.execute('INSERT INTO bank_accounts VALUES (?,?)',
                    (profile['id'], cash if profile['id'] == data['start_account'] else '0'))
    depleted = {}
    for raw, qty in con.execute('SELECT key,qty FROM consumed').fetchall():
        try:
            fields = json.loads(raw)
        except ValueError:
            fields = None
        if isinstance(fields, list) and len(fields) == 7:
            key = json.dumps(['unreplenished'] + fields[:5])
        elif raw.startswith('spot:') and len(raw.split(':')) == 6:
            parts = raw.split(':')
            parts[3] = 'unreplenished'
            key = ':'.join(parts)
        else:
            continue
        depleted[key] = depleted.get(key, ZERO) + money(qty)
    for key, qty in depleted.items():
        con.execute('INSERT OR IGNORE INTO consumed VALUES (?,?)', (key, str(qty)))
    event(con, 'migration', {'initial_cash': cash, 'start_account': data['start_account'],
                           'accounts': {p['id']: cash if p['id'] == data['start_account'] else '0'
                                        for p in data['accounts']}}, now)


def cash(con, account, delta, now):
    row = con.execute('SELECT cash FROM bank_accounts WHERE id=?', (account,)).fetchone()
    if not row or Decimal(row[0]) + delta < 0:
        raise Blocked('Недостаточно денег на банковском счёте')
    con.execute('UPDATE bank_accounts SET cash=? WHERE id=?', (str(Decimal(row[0]) + delta), account))
    event(con, 'cash', {'account': account, 'delta': str(delta)}, now)


def wallet_cash(con, delta):
    current = money(con.execute('SELECT cash FROM wallet').fetchone()[0])
    if current + delta < 0:
        raise Blocked('Недостаточно свободных рублей')
    con.execute('UPDATE wallet SET cash=?', (str(current + delta),))


def transfer(con, data, source, target, amount, now, delay_seconds):
    """Explicit own-account transfer. No automatic account rotation to bypass limits."""
    if source == target or isinstance(delay_seconds, bool) or not 0 < delay_seconds <= 7 * 86400:
        raise Blocked('Некорректный собственный перевод или срок зачисления')
    profiles = {p['id']: p for p in data['accounts']}
    a, b = profiles[source], profiles[target]
    outgoing = quote(con, a, 'self_sbp', 'out', amount, now)
    incoming = quote(con, b, 'self_sbp', 'in', amount, now)
    if money(incoming['fee']):
        raise Blocked('Перевод с комиссией зачисления требует отдельной подтверждённой модели')
    spent = money(outgoing['amount']) + money(outgoing['fee'])
    cash(con, source, -spent, now)
    wallet_cash(con, -spent)
    payment(con, outgoing, now, None)
    cur = con.execute('INSERT INTO bank_transfers(source,target,amount,fee,method,sent,due,state) '
                      "VALUES (?,?,?,?,?,?,?,'pending')",
                      (source, target, outgoing['amount'], outgoing['fee'], 'self_sbp', now, now + delay_seconds))
    con.execute('INSERT INTO bank_expenses(account,period,amount,kind) VALUES (?,?,?,?)',
                (source, now, outgoing['fee'], 'transfer:' + str(cur.lastrowid)))
    event(con, 'transfer_sent', {'id': cur.lastrowid, 'source': source, 'target': target,
                               'amount': outgoing['amount'], 'fee': outgoing['fee']}, now)
    return cur.lastrowid


def settle(con, data, now):
    profiles = {p['id']: p for p in data['accounts']}
    for tid, target, amount in con.execute("SELECT id,target,amount FROM bank_transfers WHERE state='pending' "
                                          'AND due<=?', (now,)).fetchall():
        try:
            q = quote(con, profiles[target], 'self_sbp', 'in', amount, now)
            if money(q['fee']):
                continue
            payment(con, q, now, None)
            cash(con, target, money(amount), now)
            wallet_cash(con, money(amount))
            con.execute("UPDATE bank_transfers SET state='settled' WHERE id=?", (tid,))
            event(con, 'transfer_received', {'id': tid, 'target': target, 'amount': amount}, now)
        except (Blocked, KeyError):
            continue  # Unconfirmed/blocked receipt stays in transit, never credited twice.


def service_charges(con, data, now):
    """Charge only explicitly confirmed amounts for specific billing periods.

    No inferred exemption from a temporary 50k balance or unknown external assets.
    """
    for p in data['accounts']:
        try:
            validate(p, now)
            service = p['service']
            for charge in service.get('charges', []):
                period = float(charge['period'])
                if period > now or charge.get('confirmed') is not True:
                    continue
                if con.execute('SELECT 1 FROM bank_expenses WHERE account=? AND period=? AND kind=?',
                               (p['id'], period, 'service')).fetchone():
                    continue
                amount = money(charge['amount'])
                cash(con, p['id'], -amount, now)
                wallet_cash(con, -amount)
                con.execute('INSERT INTO bank_expenses(account,period,amount,kind) VALUES (?,?,?,?)',
                            (p['id'], period, str(amount), 'service'))
                event(con, 'service_fee', {'account': p['id'], 'period': period, 'amount': str(amount)}, now)
        except Blocked:
            continue


def template():
    import trades
    accounts = []
    for bank in trades.BANK_NAMES:
        if bank == 'SBP':
            continue
        accounts.append({'id': bank, 'bank': bank, 'scope': bank + ':owner',
                         'tariff': 'Black без подписки' if bank == 'T-Bank' else 'не подтверждён',
                         'revision': 'pending-1', 'status': 'pending', 'checked_at': 0, 'valid_until': 0,
                         'tariff_confirmed': False, 'limits_confirmed': False, 'shared_limits_confirmed': False,
                         'ownership_confirmed': False,
                         'p2p_conditions_confirmed': False, 'billing_day': None, 'sources': [],
                         'methods': {}, 'aggregate': {}, 'venues': {}, 'exchange_fees': {}, 'offer_reviews': {},
                         'service': {'confirmed': False, 'charges': []}})
    return {'version': 1, 'start_account': 'T-Bank', 'accounts': accounts}


def reserve(con, run_id, profile, method, amount, now):
    cash(con, profile['id'], -money(amount), now)
    con.execute('INSERT INTO bank_reservations VALUES (?,?,?,?,?,?)',
                (run_id, profile['id'], profile['scope'], method, str(amount), now))


def payment(con, q, now, run_id):
    con.execute('INSERT INTO bank_payments(account,scope,ts,direction,method,amount,fee,run_id) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (q['account'], q['scope'], now, q['direction'], q['method'], q['amount'], q['fee'], run_id))
    event(con, 'payment', dict(q, run_id=run_id), now)


def reconcile(con):
    rows = con.execute('SELECT cash FROM bank_accounts').fetchall()
    if not rows:
        return
    actual = sum((money(r[0]) for r in rows), ZERO)
    expected = money(con.execute('SELECT cash FROM wallet').fetchone()[0])
    if actual != expected:
        raise ValueError('Bank cash does not reconcile with portfolio cash')


def replay(con):
    balances = {}
    for kind, raw in con.execute('SELECT kind,details FROM bank_events ORDER BY id'):
        d = json.loads(raw)
        if kind == 'migration':
            balances = {k: money(v) for k, v in d['accounts'].items()}
        elif kind == 'cash':
            balances[d['account']] += Decimal(d['delta'])
        elif kind == 'account_added':
            balances[d['account']] = ZERO
    return {k: str(v) for k, v in balances.items()}


def report(con, data, now):
    lines = ['🏦 <b>Банковская модель: строгая</b>', '', 'Неизвестные условия блокируют исполнение.']
    for profile in data['accounts']:
        row = con.execute('SELECT cash FROM bank_accounts WHERE id=?', (profile['id'],)).fetchone()
        label = profile['bank'] + ' / ' + profile.get('tariff', 'неизвестный тариф')
        try:
            validate(profile, now)
            status = 'профиль подтверждён; лимиты проверяются для каждой операции'
        except (Blocked, ValueError, TypeError) as exc:
            status = str(exc)
        import html
        from display import rubles
        lines.extend(['', '<b>' + html.escape(label) + '</b>',
                      'Свободно: ' + rubles(row[0] if row else '0'),
                      'Статус: ' + html.escape(status)])
        if status.startswith('профиль подтверждён'):
            for direction, name in (('out', 'исходящие'), ('in', 'входящие')):
                try:
                    amount, count = usage(con, profile, direction, '*', now, 'month')
                    limit = profile['aggregate'][direction]['limits']['month']
                    lines.append(html.escape(f"  {name} за месяц: {amount} / {limit['amount']} ₽; "
                                             f"операций {count} / {limit['count']}"))
                except (Blocked, KeyError, ValueError):
                    lines.append('  Использованный месячный лимит не подтверждён.')
    lines.append('')
    lines.append('Карты одного счёта не создают новые лимиты. Это не гарантия отсутствия блокировки.')
    return lines
