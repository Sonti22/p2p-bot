"""Public tariff comparisons, deliberately separate from personal permissions.

Regulatory fee ceilings are costs in a conservative scenario, not bank limits.
No account balance, permissions or borrowing capacity is created here.
"""
from decimal import Decimal, ROUND_UP
import datetime as dt
import html

CHECKED = dt.date(2026, 10, 8)
SOURCE = 'https://cbr.ru/PSystem/payment_system/2024-11-01_02/'
PRODUCTS = (
    {'id': 'tbank-black', 'bank': 'T-Bank', 'product': 'Black без подписки',
     'kind': 'debit', 'service': '99 ₽/расчётный период; бесплатность требует условий',
     'source': 'https://www.tbank.ru/bank/help/debit-cards/tinkoff-black/get-tinkoff-black/service-fee/'},
    {'id': 'vtb-debit', 'bank': 'VTB', 'product': 'Дебетовая карта с бесплатным обслуживанием',
     'kind': 'debit', 'service': '0 ₽', 'free_sbp': '100000',
     'source': 'https://www.vtb.ru/personal/karty/debetovye/debetovaya-karta-s-besplatnym-obsluzhivaniyem/'},
    {'id': 'vtb-multicard', 'bank': 'VTB', 'product': 'ПУ Мультикарта',
     'kind': 'conditional', 'free_sbp': '300000', 'per_operation': '500000',
     'per_day': '500000', 'service': 'требуется отдельная проверка пакета и доступности',
     'source': 'https://www.vtb.ru/personal/online-servisy/perevody-sbp/'},
    {'id': 'gpb-debit', 'bank': 'Gazprombank', 'product': 'Счёт / дебетовый продукт',
     'kind': 'debit', 'free_sbp': '100000', 'service': 'зависит от конкретного продукта',
     'source': 'https://www.gazprombank.ru/personal/page/sbp/'},
    {'id': 'mts-debit', 'bank': 'MTS-Bank', 'product': 'Дебетовый счёт',
     'kind': 'debit', 'free_sbp': '100000', 'service': 'зависит от конкретного продукта',
     'source': 'https://www.mtsbank.ru/chastnim-licam/vse-servici/perevody-po-nomeru/'},
    {'id': 'alfa-credit', 'bank': 'Alfa-bank', 'product': 'Кредитная карта / СБП',
     'kind': 'credit', 'service': 'проценты и обслуживание зависят от договора',
     'out_rate': '0.059', 'out_min': '150', 'incoming_sbp': False,
     'source': 'https://alfabank.ru/everyday/online/sbp/'},
)


def _amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('Сумма должна быть конечной и неотрицательной')
    return result


def estimate(product_id, amount, used_month=0, *, own_account=False, used_day=0, today=None):
    """Quote an outgoing RUB SBP cost; unknown limits stay explicit.

    A credit quote excludes financing cost and cannot become executable profit.
    Shared monthly usage must be supplied across all cards of the same bank.
    """
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    if not CHECKED <= today <= CHECKED + dt.timedelta(days=30):
        raise ValueError('Каталог тарифов требует обновления')
    product = next((p for p in PRODUCTS if p['id'] == product_id), None)
    if product is None:
        raise ValueError('Продукт отсутствует в проверенном каталоге')
    amount, used, daily = map(_amount, (amount, used_month, used_day))
    if amount <= 0:
        raise ValueError('Сумма перевода должна быть положительной')
    if own_account and product['kind'] == 'credit':
        raise ValueError('Кредитные средства не моделируются как собственные')
    if product.get('per_operation') and amount > _amount(product['per_operation']):
        raise ValueError('Превышен опубликованный лимит операции')
    if product.get('per_day') and daily + amount > _amount(product['per_day']):
        raise ValueError('Превышен опубликованный суточный лимит')
    if product['kind'] == 'credit':
        fee = max(amount * _amount(product['out_rate']), _amount(product['out_min']))
        basis = 'published_credit_fee_excluding_interest'
    else:
        free = Decimal('30000000') if own_account else _amount(product.get('free_sbp', '100000'))
        taxable = max(Decimal(0), used + amount - free) - max(Decimal(0), used - free)
        fee = min(taxable * Decimal('0.005'), Decimal('1500'))
        basis = 'regulatory_fee_ceiling'
    return {'product': product_id, 'bank_scope': product['bank'], 'amount': str(amount),
            'fee': str(fee.quantize(Decimal('0.01'), rounding=ROUND_UP)), 'basis': basis,
            'executable': False, 'source': product['source'], 'regulatory_source': SOURCE,
            'unknown': ['personal_limits', 'monthly_operation_count', 'p2p_permission',
                        'offer_acceptance', 'service_waiver', 'financing_cost']
                       if product['kind'] == 'credit' else
                       ['personal_limits', 'monthly_operation_count', 'p2p_permission',
                        'offer_acceptance', 'service_waiver']}


def report_lines():
    import trades
    covered = {p['bank'] for p in PRODUCTS}
    lines = [f'🏦 Каталог стандартных условий — проверка {CHECKED.isoformat()}.',
             'Сценарий, не подтверждение разрешения банка. Капитал не увеличивается.',
             'СБП: базовый бесплатный порог 100 000 ₽/месяц; далее потолок комиссии '
             '0,5% превышения, максимум 1 500 ₽/перевод. Себе: 30 млн ₽/месяц.',
             'Порог комиссии — не лимит оборота. Карты одного банка делят использованный порог.']
    for p in PRODUCTS:
        lines.append(html.escape(f"{p['id']}: {p['product']}; обслуживание: {p['service']}"))
        lines.append(f'<a href="{p["source"]}">Условия продукта</a>')
    missing = [name for bank, name in trades.BANK_NAMES.items() if bank != 'SBP' and bank not in covered]
    lines.append('Пока без проверенного продуктового тарифа: ' + html.escape(', '.join(missing)))
    lines.append('Кредитные, зарплатные и премиальные условия не добавляют бесплатный капитал. '
                 'Неизвестные лимиты и разрешения сохраняются неизвестными.')
    return lines
