import datetime as dt

import pytest

import bankcatalog as catalog


def test_crossing_free_tier_charges_only_excess():
    quote = catalog.estimate('vtb-debit', 50000, 90000, today=catalog.CHECKED)
    assert quote['fee'] == '200.00'
    assert quote['executable'] is False
    assert 'p2p_permission' in quote['unknown']


def test_card_count_does_not_change_shared_scope():
    basic = catalog.estimate('vtb-debit', 50000, 100000, today=catalog.CHECKED)
    package = catalog.estimate('vtb-multicard', 50000, 100000, today=catalog.CHECKED)
    assert basic['bank_scope'] == package['bank_scope'] == 'VTB'
    assert basic['fee'] == '250.00'
    assert package['fee'] == '0.00'


def test_own_account_is_not_counterparty_allowance():
    assert catalog.estimate('vtb-debit', 50000, 100000, own_account=True,
                            today=catalog.CHECKED)['fee'] == '0.00'
    assert catalog.estimate('vtb-debit', 50000, 100000,
                            today=catalog.CHECKED)['fee'] == '250.00'


def test_published_daily_limit_blocks_cumulative_payments():
    with pytest.raises(ValueError, match='суточный'):
        catalog.estimate('vtb-multicard', 50000, used_day=475000, today=catalog.CHECKED)


def test_credit_fee_is_not_interest_or_free_capital():
    quote = catalog.estimate('alfa-credit', 50000, today=catalog.CHECKED)
    assert quote['fee'] == '2950.00'
    assert 'financing_cost' in quote['unknown']
    with pytest.raises(ValueError, match='собственные'):
        catalog.estimate('alfa-credit', 50000, own_account=True, today=catalog.CHECKED)


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0'])
def test_invalid_amount(value):
    with pytest.raises(ValueError):
        catalog.estimate('vtb-debit', value, today=catalog.CHECKED)


def test_stale_catalog_blocks_quote():
    with pytest.raises(ValueError, match='обновления'):
        catalog.estimate('vtb-debit', 50000, today=catalog.CHECKED + dt.timedelta(days=31))
