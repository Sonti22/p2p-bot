"""Independent alternative paper portfolios. Never authorizes real bank operations."""
import calendar
import copy
import dataclasses
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, ROUND_UP, ROUND_DOWN
import html
import json
import os
import time

import bankcatalog
import bankmodel as bm
import p2p
import portfolio as pf

ROOT = os.path.join(os.path.dirname(__file__), 'data')
VARIANTS = {'fast': (60, 120, 10, 1), 'base': (300, 900, 60, 1),
            'stress': (1800, 3600, 600, 2)}
LABELS = {'fast': 'Быстрый', 'base': 'Базовый', 'stress': 'Стрессовый'}
REVISION = 'standard-2026-10-08-v1'
HOLIDAYS_2026 = {(1, d) for d in range(1, 12)} | {
    (2, 23), (3, 9), (5, 1), (5, 11), (6, 12), (11, 4), (12, 31)}
ASSUMPTIONS = ['Стандартный сценарий: личные лимиты и разрешение P2P неизвестны',
               'KYC, принадлежность счёта и допуск объявления предполагаются',
               'Внешний банковский оборот равен нулю; лимиты без источника неизвестны',
               'P2P-комиссия предполагается нулевой; спотовая ставка из конфигурации',
               'Задержки сценарные, не подтверждения платежей',
               'Платные SMS, подписки и дополнительные карты не подключены']


def enabled():
    return os.getenv('PAPER_SCENARIOS', '0') == '1'


def path(name):
    if name not in VARIANTS:
        raise ValueError('Unknown scenario')
    return os.path.join(ROOT, 'paper_scenario_' + name + '.db')


def _profiles(cfg):
    accounts = []
    for product_id in ('tbank-black', 'vtb-debit', 'psb-cashback', 'alfa-debit'):
        product = next(p for p in bankcatalog.PRODUCTS if p['id'] == product_id)
        methods = {'sbp': {}, 'self_sbp': {}}
        if product['channels'].get('intra'):
            methods['intra'] = {}
        for channel in ('card_number', 'requisites'):
            if product['channels'].get(channel):
                methods[channel] = product['channels'][channel]
        accounts.append({'id': product_id, 'bank': product['bank'], 'scope': product['bank'] + ':owner',
                         'product_terms': copy.deepcopy(product),
                         'tariff': product['product'], 'revision': REVISION, 'scenario_only': True,
                         'sources': [product['source'], bankcatalog.SOURCE], 'methods': methods,
                         'venues': {v: True for v in ('Bybit', 'MEXC', 'HTX', 'KuCoin')},
                         'exchange_fees': {v: {'confirmed': True, 'currency': 'received',
                                              'percent': str(p2p._spot_fee(cfg, v)), 'evidence': 'scenario_assumption'}
                                           for v in ('Bybit', 'MEXC', 'HTX', 'KuCoin')},
                         'service': {'confirmed': False, 'charges': []}})
    return {'version': 1, 'start_account': 'tbank-black', 'accounts': accounts}


def initialize(name, cfg=None, now=None):
    now = time.time() if now is None else now
    con = pf.connect(path(name))
    try:
        con.execute('BEGIN IMMEDIATE')
        con.execute('CREATE TABLE IF NOT EXISTS scenario_meta (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT)')
        row = con.execute('SELECT data FROM scenario_meta').fetchone()
        if row:
            return json.loads(row[0])
        pay, release, own, network = VARIANTS[name]
        data = {'name': name, 'revision': REVISION, 'created': now,
                'payment_seconds': pay, 'release_seconds': release, 'own_seconds': own,
                'network_multiplier': network, 'assumptions': ASSUMPTIONS,
                'profiles': _profiles(cfg or p2p.Config())}
        con.execute("INSERT OR IGNORE INTO wallet VALUES (1,'50000.00','50000.00')")
        bm.initialize(con, data['profiles'], now)
        con.execute('INSERT INTO scenario_meta VALUES (1,?)', (json.dumps(data, ensure_ascii=False),))
        pf._event(con, None, 'scenario_started', data, now)
        con.commit()
        return data
    finally:
        con.close()


@contextmanager
def context(name, cfg=None, now=None):
    data = initialize(name, cfg, now)
    token = bm.SCENARIO.set(data)
    try:
        yield data
    finally:
        bm.SCENARIO.reset(token)


def validate_profile(profile, now):
    data = bm.SCENARIO.get()
    if not data or profile not in data['profiles']['accounts'] or not profile.get('scenario_only'):
        raise bm.Blocked('Профиль не принадлежит сценарному портфелю')
    date = dt.datetime.fromtimestamp(now, bm.MSK).date()
    terms = profile['product_terms']
    if not dt.date.fromisoformat(terms['checked_at']) <= date <= dt.date.fromisoformat(terms['valid_until']):
        raise bm.Blocked('Каталог тарифов требует обновления')


def _usage(con, profile, direction, method, now, period):
    return sum((bm.money(v) for v, in con.execute(
        'SELECT amount FROM bank_payments WHERE scope=? AND direction=? AND method=? AND ts>=? AND ts<=?',
        (profile['scope'], direction, method, bm.period_start(now, period), now))), Decimal(0))


def quote(con, profile, method, direction, amount, now, run_id=None):
    validate_profile(profile, now)
    if method not in profile['methods'] or direction not in ('in', 'out'):
        raise bm.Blocked('Нет проверенной модели канала платежа')
    amount = bm.money(amount).quantize(bm.CENT, rounding=ROUND_UP if direction == 'out' else ROUND_DOWN)
    if amount <= 0:
        raise bm.Blocked('Нулевая сумма')
    if method in ('sbp', 'self_sbp') and amount > Decimal('30000000' if method == 'self_sbp' else '1000000'):
        raise bm.Blocked('Превышен максимальный размер операции СБП')
    pending_service = service_due(con, now)
    if direction == 'out' and profile['id'] == 'tbank-black' and pending_service:
        raise bm.Blocked('Не оплачено обслуживание Black')
    used = _usage(con, profile, direction, method, now, 'month')
    fee = Decimal(0)
    basis = 'incoming_zero_fee_assumption' if direction == 'in' else 'published_intrabank_zero_fee'
    if direction == 'out' and method in ('sbp', 'self_sbp'):
        try:
            q = bankcatalog.estimate(profile['id'], amount, used, own_account=method == 'self_sbp',
                                     used_day=_usage(con, profile, direction, method, now, 'day'),
                                     today=dt.datetime.fromtimestamp(now, bm.MSK).date(), terms=profile['product_terms'])
        except ValueError as exc:
            raise bm.Blocked(str(exc)) from exc
        fee, basis = bm.money(q['fee']), q['basis']
    elif direction == 'out' and method in ('card_number', 'requisites'):
        tariff = profile['methods'][method]
        if 'fee' in tariff:
            fee = bm.money(tariff['fee'])
        else:
            fee = max(amount * bm.money(tariff['percent']) / 100, bm.money(tariff['minimum']))
            if tariff.get('maximum') is not None:
                fee = min(fee, bm.money(tariff['maximum']))
        fee = fee.quantize(bm.CENT, rounding=ROUND_UP)
        basis = 'published_channel_fee'
    return {'account': profile['id'], 'scope': profile['scope'], 'method': method,
            'direction': direction, 'amount': str(amount), 'fee': str(fee),
            'profile_revision': profile['revision'], 'evidence': basis,
            'unknown_limits': True, 'scenario_only': True}


def review_offer(profile, ad, now):
    validate_profile(profile, now)
    aggregate = ad.parts > 1 and bool(ad.nicks) and bool(ad.legs)
    if (not ad.ad_id and not aggregate) or ad.ex not in profile['venues']:
        raise bm.Blocked('Нет устойчивого объявления или модели площадки')
    # Known restrictive language is not replaced by an assumed acceptance.
    terms = (ad.terms or '').lower()
    if any(s in terms for s in ('third party accepted', 'third-party accepted', 'только ип',
                                 'только юрид', 'комиссия', 'additional fee', 'commission')):
        raise bm.Blocked('Ограничения или комиссия объявления требуют проверки')
    data = bm.SCENARIO.get()
    method = bm.compatible(profile, ad.pays)
    delay = data['payment_seconds']
    if method == 'requisites':
        # Conservative business-day model; not the actual BESP operating schedule.
        date = dt.datetime.fromtimestamp(now, bm.MSK)
        days = 1
        while True:
            target = date + dt.timedelta(days=days)
            if target.year != 2026:
                raise bm.Blocked('Нет проверенного календаря переводов по реквизитам для года')
            if target.weekday() < 5 and (target.month, target.day) not in HOLIDAYS_2026:
                break
            days += 1
        delay = max(delay, days * 86400)
    return {'payment_seconds': delay, 'release_seconds': data['release_seconds'],
            'evidence': 'scenario_assumption', 'terms': ad.terms or ''}


def service_due(con, now):
    row = con.execute('SELECT data FROM scenario_meta').fetchone()
    if not row:
        return []
    created = json.loads(row[0])['created']
    first = dt.datetime.fromtimestamp(created, bm.MSK)
    periods = [created]
    year, month = first.year, first.month
    while True:
        month += 1
        if month > 12:
            year, month = year + 1, 1
        due = first.replace(year=year, month=month, day=min(first.day, calendar.monthrange(year, month)[1])).timestamp()
        if due > now:
            break
        periods.append(due)
    return [due for due in periods if not con.execute(
        "SELECT 1 FROM bank_expenses WHERE account='tbank-black' AND period=? AND kind='service'", (due,)).fetchone()]


def maintain(name, cfg=None, now=None):
    now = time.time() if now is None else now
    with context(name, cfg, now) as data:
        con = pf.connect(path(name))
        try:
            con.execute('BEGIN IMMEDIATE')
            bm.settle(con, data['profiles'], now)
            due_periods = service_due(con, now)
            available = bm.money(con.execute("SELECT cash FROM bank_accounts WHERE id='tbank-black'").fetchone()[0])
            needed = Decimal(99) * len(due_periods)
            if available < needed:
                if not con.execute("SELECT 1 FROM bank_transfers WHERE state='pending' LIMIT 1").fetchone():
                    for source in data['profiles']['accounts']:
                        if source['id'] == 'tbank-black':
                            continue
                        cash = bm.money(con.execute('SELECT cash FROM bank_accounts WHERE id=?', (source['id'],)).fetchone()[0])
                        if cash:
                            principal = bm.affordable(con, source, 'self_sbp', min(cash, needed - available), now, None)
                            if principal > 0:
                                bm.transfer(con, data['profiles'], source['id'], 'tbank-black', principal, now, data['own_seconds'])
                                break
                pf._verify(con)
                con.commit()
                return
            for due in due_periods:
                bm.cash(con, 'tbank-black', Decimal('-99'), now)
                bm.wallet_cash(con, Decimal('-99'))
                con.execute("INSERT INTO bank_expenses VALUES (NULL,'tbank-black',?,'99','service')", (due,))
                bm.event(con, 'service_fee', {'account': 'tbank-black', 'period': due, 'amount': '99'}, now)
            pf._verify(con)
            con.commit()
        except bm.Blocked:
            con.rollback()
        finally:
            con.close()


def prepare(name, buy, sell, budget, cfg=None, now=None, fund=False):
    """Cost estimate and funding; only settled account money may start a cycle."""
    now = time.time() if now is None else now
    maintain(name, cfg, now)
    with context(name, cfg, now) as data:
        con = pf.connect(path(name))
        try:
            con.execute('BEGIN IMMEDIATE')
            if service_due(con, now):
                raise bm.Blocked('Есть неоплаченное обслуживание; новые круги остановлены')
            candidates = []
            for profile in data['profiles']['accounts']:
                method = bm.compatible(profile, buy.pays)
                if not method:
                    continue
                try:
                    review_offer(profile, buy, now)
                    q = quote(con, profile, method, 'out', budget, now)
                    bm.select(con, data['profiles'], sell.pays, 'in', budget, now, venue=sell.ex)
                    cash = bm.money(con.execute('SELECT cash FROM bank_accounts WHERE id=?', (profile['id'],)).fetchone()[0])
                    candidates.append((bm.money(q['fee']), -cash, profile, method, cash))
                except bm.Blocked:
                    continue
            if not candidates:
                raise bm.Blocked('Нет совместимого продукта с проверенной стоимостью')
            _, _, profile, method, cash = min(candidates, key=lambda x: (x[0], x[1], x[2]['id']))
            if cash < bm.money(budget):
                if not fund:
                    principal = bm.affordable(con, profile, method, budget, now, None)
                    q = quote(con, profile, method, 'out', principal, now)
                    con.commit()
                    return {'budget': str(budget), 'principal': str(principal), 'bank_fee': q['fee'],
                            'incoming_fee': '0', 'account': profile['id'], 'method': method}
                pending = con.execute("SELECT 1 FROM bank_transfers WHERE state='pending' LIMIT 1").fetchone()
                if not pending:
                    for source in data['profiles']['accounts']:
                        available = bm.money(con.execute('SELECT cash FROM bank_accounts WHERE id=?', (source['id'],)).fetchone()[0])
                        needed = bm.money(budget) - cash
                        if source['id'] != profile['id'] and available > 0:
                            transfer_budget = min(available, needed)
                            principal = bm.affordable(con, source, 'self_sbp', transfer_budget, now, None)
                            if principal <= 0:
                                continue
                            bm.transfer(con, data['profiles'], source['id'], profile['id'], principal, now, data['own_seconds'])
                            break
                con.commit()
                return None
            principal = bm.affordable(con, profile, method, budget, now, None)
            q = quote(con, profile, method, 'out', principal, now)
            receipt, receipt_method, incoming = bm.select(con, data['profiles'], sell.pays, 'in', principal, now, venue=sell.ex)
            review_offer(receipt, sell, now)
            con.commit()
            return {'budget': str(budget), 'principal': str(principal), 'bank_fee': q['fee'],
                    'incoming_fee': incoming['fee'], 'account': profile['id'], 'method': method}
        except bm.Blocked as exc:
            con.rollback()
            last = con.execute("SELECT details FROM events WHERE kind='scenario_blocked' ORDER BY id DESC LIMIT 1").fetchone()
            if not last or json.loads(last[0])['reason'] != str(exc):
                pf._event(con, None, 'scenario_blocked', {'reason': str(exc)}, now)
            con.commit()
            return None
        finally:
            con.close()


def report(name='base'):
    maintain(name)
    with context(name):
        s = pf.summary(path(name))
        lines = [f'🧪 {LABELS[name]} сценарий — отдельные 50 000 ₽, варианты не суммируются.']
        lines.extend(line.replace('Прибыль новых кругов строгого этапа', 'Прибыль сценарных кругов')
                     for line in pf.report_lines(path(name)))
        active = [r for r in s['runs'] if r['stage'] not in ('done', 'cancelled')]
        if not s['runs']:
            lines.append('Исполнимых новых сигналов пока нет; прибыль от продаж равна нулю.')
        cost = sum((pf.dec(r['cost']) + sum((pf.dec(d['cost']) for d in r.get('dust', [])), Decimal(0)) for r in s['runs']), Decimal(0))
        lines.append(f'Себестоимость оставшихся активов: {cost:.2f} ₽; активных кругов: {len(active)}.')
        durations = [r['stage_ts'] - r['start'] for r in s['runs'] if r['stage'] == 'done']
        if durations:
            lines.append(f'Среднее время завершённого круга: {sum(durations) / len(durations) / 60:.1f} мин.')
        lines.extend(html.escape(x) for x in ASSUMPTIONS)
        con = pf.connect(path(name))
        try:
            paid_service = sum((pf.dec(v) for v, in con.execute("SELECT amount FROM bank_expenses WHERE kind='service'")), Decimal(0))
            bank_fees = sum((pf.dec(v) for v, in con.execute('SELECT fee FROM bank_payments')), Decimal(0))
            lines.append(f'Справочно, уже учтено: обслуживание {paid_service:.2f} ₽; банковские комиссии {bank_fees:.2f} ₽.')
            coins = {}
            for kind, raw in con.execute("SELECT kind,details FROM events WHERE kind IN ('spot','transfer_sent')"):
                d = json.loads(raw)
                asset = d['target'] if kind == 'spot' else d['hop']['asset']
                key = ('спот' if kind == 'spot' else 'сеть', asset)
                coins[key] = coins.get(key, Decimal(0)) + pf.dec(d['fee'])
            for (kind, asset), amount in coins.items():
                lines.append(html.escape(f'Учтённая комиссия {kind}: {amount} {asset}.'))
            outstanding = Decimal(99) * len(service_due(con, time.time()))
            if outstanding:
                lines.append(f'Неоплаченное обслуживание: {outstanding:.2f} ₽; '
                             f"чистый результат с учётом долга: {pf.dec(s['net_realized']) - outstanding:+.2f} ₽. "
                             'Новые круги остановлены до оплаты.')
            last = con.execute("SELECT details FROM events WHERE kind IN ('bank_blocked','scenario_blocked') ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                lines.append('Последний отказ: ' + html.escape(json.loads(last[0])['reason']))
        finally:
            con.close()
        return lines


def blocked(name, reason, now=None):
    now = time.time() if now is None else now
    con = pf.connect(path(name))
    try:
        con.execute('BEGIN IMMEDIATE')
        last = con.execute("SELECT details FROM events WHERE kind='scenario_blocked' ORDER BY id DESC LIMIT 1").fetchone()
        if not last or json.loads(last[0])['reason'] != reason:
            pf._event(con, None, 'scenario_blocked', {'reason': reason}, now)
        con.commit()
    finally:
        con.close()


def account_lines(name='base'):
    maintain(name)
    with context(name) as data:
        con = pf.connect(path(name))
        try:
            lines = [f'🏦 Счета: {LABELS[name]} сценарий. Личные разрешения и неизвестные лимиты не подтверждены.']
            for profile in data['profiles']['accounts']:
                cash = con.execute('SELECT cash FROM bank_accounts WHERE id=?', (profile['id'],)).fetchone()[0]
                used = _usage(con, profile, 'out', 'sbp', time.time(), 'month')
                lines.append(html.escape(f"{profile['bank']} / {profile['tariff']}: свободно {cash} ₽; "
                                         f"СБП контрагентам за месяц {used} ₽."))
                terms = profile['product_terms']
                lines.append(html.escape('Каналы: ' + ', '.join(profile['methods']) +
                                         '; суточный предел СБП: ' + str(terms.get('per_day') or 'неизвестен') +
                                         '; предел числа операций: неизвестен.'))
            lines.append('/paper verified — строгий портфель и личные банковские профили.')
            return lines
        finally:
            con.close()


def reset_all():
    archives = []
    for name in VARIANTS:
        archive = pf.reset(path(name))
        if archive:
            archives.append(archive)
            con = pf.connect(path(name))
            try:
                con.execute('DELETE FROM scenario_meta')
                con.commit()
            finally:
                con.close()
        maintain(name)
    return archives


def market_snapshot(name, snap, cfg=None, now=None):
    """Planning depth excludes unsupported and already consumed offers.

    Only the planner sees adjusted availability; execution consumes original
    observed quantities, avoiding subtracting the same depletion twice.
    """
    now = time.time() if now is None else now
    with context(name, cfg, now) as data:
        con = pf.connect(path(name))
        try:
            groups = {}
            for key, offers in snap.groups.items():
                allowed = []
                for offer in offers:
                    if not offer.ad_id or not 0 < offer.fetched_ts <= now <= offer.fetched_ts + 120:
                        continue
                    compatible = False
                    for profile in data['profiles']['accounts']:
                        try:
                            if bm.compatible(profile, offer.pays):
                                review_offer(profile, offer, now)
                                compatible = True
                                break
                        except bm.Blocked:
                            continue
                    if not compatible:
                        continue
                    row = con.execute('SELECT qty FROM consumed WHERE key=?', (pf._offer_key(offer),)).fetchone()
                    remaining = max(Decimal(0), bm.money(offer.avail) - (bm.money(row[0]) if row else Decimal(0)))
                    if remaining > 0:
                        allowed.append(dataclasses.replace(offer, avail=float(remaining)))
                groups[key] = allowed
            return dataclasses.replace(snap, groups=groups, over_banks=frozenset())
        finally:
            con.close()
