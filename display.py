"""Telegram presentation helpers; no accounting or tariff decisions."""
from decimal import Decimal


def rubles(value):
    return format(Decimal(str(value)), ',.2f').replace(',', ' ').replace('.', ',') + ' ₽'


def channel_name(method):
    return {'sbp': 'СБП контрагентам', 'self_sbp': 'СБП между своими счетами',
            'intra': 'Внутрибанковский перевод', 'card_number': 'По номеру карты',
            'requisites': 'По банковским реквизитам'}.get(method, method)
