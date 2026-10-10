"""Local evidence and restrictions for virtual payments; no banking API."""
import json
from decimal import Decimal

STATES = {'available', 'rejected', 'confirmation_required', 'review_115',
          'fraud_161', 'authority_restriction', 'funds_restricted', 'unavailable'}


def schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS account_scopes (account TEXT PRIMARY KEY, scope TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS account_restrictions '
                '(account TEXT PRIMARY KEY, state TEXT, evidence TEXT, since REAL, review_after REAL)')
    con.execute('CREATE TABLE IF NOT EXISTS restriction_events '
                '(id INTEGER PRIMARY KEY, account TEXT, state TEXT, evidence TEXT, ts REAL)')


def set_state(con, account, state, evidence, now, days=None):
    if state not in STATES or not account or not evidence:
        raise ValueError('Restriction requires an account, known state and evidence')
    if days is not None and days not in (1, 7, 30):
        raise ValueError('Stress review intervals: 1, 7, 30 days')
    schema(con)
    old = con.execute('SELECT state,evidence FROM account_restrictions WHERE account=?', (account,)).fetchone()
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    if old and tuple(old) == (state, encoded):
        return
    con.execute('INSERT OR REPLACE INTO account_restrictions VALUES (?,?,?,?,?)',
                (account, state, encoded, now, now + days * 86400 if days else None))
    con.execute('INSERT INTO restriction_events(account,state,evidence,ts) VALUES (?,?,?,?)',
                (account, state, encoded, now))


def available(con, account, scope=None):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='account_restrictions'").fetchone():
        return True
    if scope is None and con.execute("SELECT 1 FROM sqlite_master WHERE name='account_scopes'").fetchone():
        row = con.execute('SELECT scope FROM account_scopes WHERE account=?', (account,)).fetchone()
        scope = row[0] if row else None
    rows = con.execute('SELECT state FROM account_restrictions WHERE account IN (?,?)', (account,scope)).fetchall()
    return all(row[0] == 'available' for row in rows)


def restricted_cash(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='account_restrictions'").fetchone():
        return Decimal(0)
    return sum((Decimal(amount) for account, amount in con.execute('SELECT id,cash FROM bank_accounts')
                if not available(con, account)), Decimal(0))


def check_payment(evidence, expected_amount, buyer, seller):
    """Never accept a receipt image or an order's paid flag as bank evidence."""
    if not isinstance(evidence, dict) or evidence.get('kind') != 'bank_credit':
        return 'Нет подтверждения банковского зачисления'
    if not buyer or not seller or evidence.get('payer') != buyer or evidence.get('recipient') != seller:
        return 'Не подтверждено совпадение владельцев; платёж третьего лица запрещён'
    try:
        amount = Decimal(str(evidence.get('amount')))
        if not amount.is_finite() or amount != Decimal(str(expected_amount)):
            return 'Неполная или избыточная оплата; требуется сверка'
    except Exception:
        return 'Некорректная сумма зачисления'
    if evidence.get('disputed') or evidence.get('reversed'):
        return 'Платёж оспорен или возвращён'
    if not evidence.get('reference'):
        return 'Отсутствует идентификатор банковской операции'
    return None


def pending(run):
    return sum((Decimal(x['cost']) for x in run.get('receipts', [])), Decimal(0))


def settle(con, run, now, credit, emit):
    remaining = []
    for item in run.get('receipts', []):
        if now < item['due'] or not available(con, item.get('account')):
            remaining.append(item)
            continue
        if item.get('status') != 'scenario_credit':
            remaining.append(item)
            continue
        proceeds, cost = Decimal(item['rub']), Decimal(item['cost'])
        credit(con, proceeds, item.get('account'), now)
        run['realized'] = str(Decimal(run['realized']) + proceeds - cost)
        run['proceeds'] = str(Decimal(run['proceeds']) + proceeds)
        emit(con, run['id'], 'bank_receipt', dict(item, evidence='scenario_delay_not_bank_confirmation'), now)
    run['receipts'] = remaining
    if not remaining and run['stage'] == 'receipt':
        run['stage'], run['stage_ts'] = 'done', now
