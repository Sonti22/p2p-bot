import json
import os

import pytest

import p2p

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

# подстрока URL -> файл фикстуры (урезанные живые ответы площадок)
ROUTES = [
    # спот-тикеры — раньше общих правил по доменам htx.com / kucoin.com
    ("api.htx.com/market/tickers", "spot_htx.json"), ("api.kucoin.com/api/v1/market/allTickers", "spot_kucoin.json"),
    ("queryAllPaymentList", "bybit_pay.json"), ("otc/item/online", "bybit_ads.json"),
    ("htx.com", "htx_ads.json"), ("kucoin.com", "kucoin_ads.json"),
    ("payment/method", "mexc_pay.json"), ("common/coins", "mexc_coins.json"),
    ("p2p.mexc.com/api/market", "mexc_ads.json"), ("bitpapa.com", "bitpapa_ads.json"),
    ("api.bybit.com/v5/market/tickers", "spot_bybit.json"), ("api.mexc.com/api/v3/ticker", "spot_mexc.json"),
    ("rapira.net", "rapira.json"),
]


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def offline(monkeypatch):
    """Все запросы площадок отвечают фикстурами; сеть в тестах не нужна."""
    async def fake_json(s, method, url, body=None):
        for part, name in ROUTES:
            if part in url:
                return load(name)
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(p2p, "_json", fake_json)
    for cache in (p2p._bybit_pay, p2p._mexc_pay, p2p._mexc_coins):
        cache.clear()
    p2p._alt.update(t=0.0, ads=[], errors={})
    return fake_json
