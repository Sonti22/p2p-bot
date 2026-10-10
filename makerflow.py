"""Virtual advertiser queue. Competing ads never imply demand for our ad."""
from decimal import Decimal
import reality


def create(order_id, side, price, quantity, now, access, level='general'):
    if access not in ('confirmed', 'scenario_assumed'):
        raise ValueError('Допуск рекламодателя неизвестен')
    price, quantity = Decimal(str(price)), Decimal(str(quantity))
    if side not in ('buy', 'sell') or price <= 0 or quantity <= 0:
        raise ValueError('Некорректное объявление')
    return {'id': str(order_id), 'side': side, 'price': str(price), 'remaining': str(quantity),
            'created': now, 'state': 'waiting_buyer', 'access': access,
            'fee_rate': str(reality.p2p_fee('maker', side, level, now)),
            'fee_currency': 'unverified', 'events': [], 'matched': [], 'filled': '0'}


def observe(order, competing_ads, now):
    side = order['side']
    ahead = sum(1 for ad in competing_ads if
                (Decimal(str(ad.price)) < Decimal(order['price']) if side == 'sell'
                 else Decimal(str(ad.price)) > Decimal(order['price'])))
    order['queue_ahead'] = ahead
    order['observed'] = now
    # No arrival-rate assumption and no artificial fill from queue position.
    return order


def match(order, match_id, quantity, evidence, now):
    if match_id in order['matched']:
        return False
    if order['state'] not in ('waiting_buyer', 'partially_filled'):
        raise ValueError('Объявление недоступно')
    if evidence != 'scenario_counterparty_arrival':
        raise ValueError('Нет наблюдения прихода контрагента; конкуренты не являются исполнениями')
    quantity = Decimal(str(quantity))
    if quantity <= 0 or quantity > Decimal(order['remaining']):
        raise ValueError('Объём не помещается в резерв')
    order['matched'].append(match_id)
    order['state'] = 'payment_pending'
    order['pending'] = str(quantity)
    order['events'].append({'kind': 'counterparty_arrival', 'ts': now, 'id': match_id,
                            'quantity': str(quantity), 'evidence': evidence})
    return True


def cancel(order, now):
    if order['state'] == 'payment_pending':
        raise ValueError('Отмена после начала оплаты требует разрешения спора')
    order['state'] = 'cancelled'
    order['events'].append({'kind': 'cancelled', 'ts': now})
