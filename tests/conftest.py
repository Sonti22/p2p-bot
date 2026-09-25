import json
import logging
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


def _bybit_ads(body):
    """Объявления Bybit различаются по стороне запроса (`side` в теле POST): "1" — бот покупает
    (площадке отдаём объявления продавцов), "0" — бот продаёт (объявления покупателей), у них разные
    цены и мерчанты, как в реальном стакане."""
    return load("bybit_ads_sell.json" if (body or {}).get("side") == "0" else "bybit_ads.json")


def _htx_ads(url):
    """HTX кодирует сторону бота в query `tradeType` (значение — противоположная сторона стакана)."""
    return load("htx_ads_sell.json" if "tradeType=buy" in url else "htx_ads.json")


def _kucoin_ads(url):
    return load("kucoin_ads_sell.json" if "side=BUY" in url else "kucoin_ads.json")


def _mexc_ads(url):
    return load("mexc_ads_sell.json" if "tradeType=BUY" in url else "mexc_ads.json")


def _bitpapa_ads(url):
    return load("bitpapa_ads_sell.json" if "type=buy" in url else "bitpapa_ads.json")


# подстрока URL -> файл фикстуры (урезанные живые ответы площадок) или функция от URL
ROUTES = [
    # спот-тикеры и справочники сетей — раньше общих правил по доменам htx.com / kucoin.com
    ("api.htx.com/market/tickers", "spot_htx.json"), ("api.kucoin.com/api/v1/market/allTickers", "spot_kucoin.json"),
    ("api.htx.com/v2/reference/currencies", _htx_currency), ("api.kucoin.com/api/v3/currencies/", _kucoin_currency),
    ("queryAllPaymentList", "bybit_pay.json"),
    ("htx.com", _htx_ads), ("kucoin.com", _kucoin_ads),
    ("payment/method", "mexc_pay.json"), ("common/coins", "mexc_coins.json"),
    ("p2p.mexc.com/api/market", _mexc_ads), ("bitpapa.com", _bitpapa_ads),
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
        if "otc/item/online" in url:   # тело POST несёт сторону запроса — отдельно от ROUTES (там только url)
            return _bybit_ads(body)
        for part, name in ROUTES:
            if part in url:
                return name(url) if callable(name) else load(name)
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(p2p, "_json", fake_json)
    netstatus.reset()
    for cache in (p2p._bybit_pay, p2p._mexc_pay, p2p._mexc_coins):
        cache.clear()
    p2p._alt.update(t=0.0, ads=[], errors={}, key=None)
    p2p.TRAPS_LOG.clear()
    p2p._venue_backoff.clear()
    return fake_json


@pytest.fixture(autouse=True)
def _clean_netstatus():
    """Статусы сетей — модульное состояние; каждый тест начинает с пустой таблицы."""
    netstatus.reset()
    yield
    netstatus.reset()


@pytest.fixture(autouse=True)
def _clean_logging():
    """Тесты setup_logging открывают файл в tmp_path — закрыть хендлер, чтобы Windows не держал файл."""
    yield
    root = logging.getLogger()
    for h in list(p2p._log_handlers):
        root.removeHandler(h)
        h.close()
    p2p._log_handlers.clear()
