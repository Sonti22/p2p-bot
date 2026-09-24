import json
import os

import pytest

import netstatus
import p2p

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _htx_currency(url):
    """Справочник валют HTX: фикстура хранит ответы по монетам, отдаём нужную по ?currency=."""
    d = load("htx_currencies.json").get(url.rsplit("currency=", 1)[-1].upper())
    return {"code": 200, "data": [d] if d else []}


def _kucoin_currency(url):
    d = load("kucoin_currencies.json").get(url.rstrip("/").rsplit("/", 1)[-1].upper())
    return {"code": "200000", "data": d or {}}


# подстрока URL -> файл фикстуры (урезанные живые ответы площадок) или функция от URL
ROUTES = [
    # спот-тикеры и справочники сетей — раньше общих правил по доменам htx.com / kucoin.com
    ("api.htx.com/market/tickers", "spot_htx.json"), ("api.kucoin.com/api/v1/market/allTickers", "spot_kucoin.json"),
    ("api.htx.com/v2/reference/currencies", _htx_currency), ("api.kucoin.com/api/v3/currencies/", _kucoin_currency),
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
                return name(url) if callable(name) else load(name)
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(p2p, "_json", fake_json)
    netstatus.reset()
    for cache in (p2p._bybit_pay, p2p._mexc_pay, p2p._mexc_coins):
        cache.clear()
    p2p._alt.update(t=0.0, ads=[], errors={})
    return fake_json


@pytest.fixture(autouse=True)
def _clean_netstatus():
    """Статусы сетей — модульное состояние; каждый тест начинает с пустой таблицы."""
    netstatus.reset()
    yield
    netstatus.reset()
