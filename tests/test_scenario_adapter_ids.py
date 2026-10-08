import copy

import pytest

import p2p
from helpers import arun


@pytest.mark.parametrize('venue', ['bybit', 'htx', 'kucoin', 'mexc'])
def test_adapter_preserves_public_ad_id(offline, monkeypatch, venue):
    original = p2p._json

    async def response(*args, **kwargs):
        data = copy.deepcopy(await original(*args, **kwargs))
        rows = ((data.get('result') or {}).get('items') or data.get('items') or data.get('data'))
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                if isinstance(row, dict) and 'price' in row:
                    row['id'] = 'public-ad-' + str(index)
                elif isinstance(row, dict) and 'floatPrice' in row:
                    row['id'] = 'public-ad-' + str(index)
        return data

    monkeypatch.setattr(p2p, '_json', response)
    ads = arun(p2p.FETCHERS[venue](None, p2p.Config(), 'buy', 'USDT'))
    assert ads and all(a.ad_id.startswith('public-ad-') for a in ads)


@pytest.mark.parametrize('item', [{}, {'userId': 'merchant'}, {'id': None}, {'id': True}, {'id': {}}])
def test_missing_or_invalid_id_is_not_fabricated(item):
    assert p2p._public_ad_id(item) == ''
