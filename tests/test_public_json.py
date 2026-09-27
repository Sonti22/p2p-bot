"""p2p._json — публичные запросы без ключей только из p2p.JSON_ALLOWED (пин списка — в tests/test_trading_surface.py).

Адреса здесь склеиваются из частей (SCHEME + хост): это данные теста, а не новые домены для guard."""
import urllib.parse

import pytest

import netstatus
import p2p
from helpers import arun

REAL_JSON = p2p._json   # настоящий _json — до фикстуры offline, которая его подменяет
SCHEME = "https" + "://"


class _Resp:
    def __init__(self, answer, method, url, body):
        self.args = answer, method, url, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        answer, method, url, body = self.args
        return await answer(None, method, url, body)


class _Session:
    """Записывает, что ушло бы в сеть; отвечает фикстурами площадок (answer — подмена из фикстуры offline)."""

    def __init__(self, answer=None):
        self.answer, self.sent = answer, []

    def request(self, method, url, json=None, headers=None):
        self.sent.append((method, url))
        return _Resp(self.answer, method, url, json)


def _entry(method, url):
    u = urllib.parse.urlsplit(url)
    return {(m, h, p) for m, h, p in p2p.JSON_ALLOWED
            if m == method and h == u.netloc and (u.path == p or p.endswith("/") and u.path.startswith(p))}


def test_every_public_request_of_the_bot_is_allowed_and_every_entry_is_used(offline, monkeypatch):
    """Все настоящие вызовы _json (площадки, курс Rapira, спот, сети монет netstatus) проходят список, и каждая
    запись списка кем-то используется — лишнего в нём нет."""
    monkeypatch.setattr(p2p, "_json", REAL_JSON)
    s = _Session(offline)
    cfg = p2p.Config()

    async def go():
        for name, fetch in p2p.FETCHERS.items():
            if name == "bestchange":   # выгрузка BestChange идёт мимо _json (свой GET BC_URL)
                continue
            for side in ("buy", "sell"):
                assert await fetch(s, cfg, side, "USDT") is not None
        await p2p.rapira_mid(s)
        await p2p.spot_prices(s, ["USDT", "BTC"])
        return await netstatus.refresh(s, ["USDT", "TON"], ["htx", "kucoin"], p2p._json)

    errors = arun(go())
    assert not [e for e in errors.values() if "JSON_ALLOWED" in e], errors
    assert s.sent and all(p2p.json_allowed(m, u) for m, u in s.sent), s.sent
    used = set().union(*(_entry(m, u) for m, u in s.sent))
    assert used == p2p.JSON_ALLOWED, sorted(p2p.JSON_ALLOWED - used)


@pytest.mark.parametrize("method, url", [
    ("POST", SCHEME + "api2.bybit.com/fiat/otc/item/online"),
    ("GET", SCHEME + "api.kucoin.com/api/v3/currencies/TON"),
    ("GET", SCHEME + "api.kucoin.com/api/v3/currencies/USDT"),
    ("GET", SCHEME + "api.htx.com/v2/reference/currencies?currency=usdt"),
    ("GET", SCHEME + "www.htx.com/-/x/otc/v1/data/trade-market?coinId=2&currency=11"),
])
def test_json_allows_listed_requests(method, url):
    assert p2p.json_allowed(method, url)


@pytest.mark.parametrize("method, url", [
    ("GET", SCHEME + "api2.bybit.com/fiat/otc/item/online"),                 # метод не тот
    ("get", SCHEME + "api.htx.com/market/tickers"),                          # метод пишется как в списке
    ("POST", "http" + "://api2.bybit.com/fiat/otc/item/online"),             # не https
    ("POST", SCHEME + "x@api2.bybit.com/fiat/otc/item/online"),              # логин в адресе
    ("POST", SCHEME + "x:y@api2.bybit.com/fiat/otc/item/online"),
    ("POST", SCHEME + "api2.bybit.com:8443/fiat/otc/item/online"),           # порт
    ("POST", SCHEME + "api2.bybit.com:443/fiat/otc/item/online"),
    ("POST", SCHEME + "API2.bybit.com/fiat/otc/item/online"),                # хост — ровно как в списке
    ("POST", SCHEME + "api2.bybit.com./fiat/otc/item/online"),
    ("POST", SCHEME + "api2.bybit.com.example.org/fiat/otc/item/online"),    # чужой хост с похожим началом
    ("GET", SCHEME + "api.bybit.com/v5/order/realtime"),                     # другой путь того же хоста
    ("GET", SCHEME + "api.bybit.com/v5/market/tickers/x"),
    ("GET", SCHEME + "api.bybit.com/v5/market/tickers/../../v5/order/realtime"),
    ("GET", SCHEME + "api.kucoin.com/api/v3/currencies/"),                   # префикс без монеты
    ("GET", SCHEME + "api.kucoin.com/api/v3/currencies/USDT/../../../api/v1/orders"),
    ("GET", SCHEME + "api.kucoin.com/api/v3/currencies/%2e%2e"),
    ("GET", SCHEME + "api.kucoin.com/api/v3/currencies/.."),
    ("GET", SCHEME + "api.htx.com/market/tickers#frag"),                     # фрагмент
    ("GET", SCHEME + "api.htx.com/market/tickers\t"),                        # управляющие символы и пробелы
    ("GET", SCHEME + "api.htx.com/market/tickers?x= 1"),
    ("GET", SCHEME + "api.htx.com\\@example.org/market/tickers"),            # «\» — разбор адреса мог бы разойтись
    ("GET", SCHEME + "api.htx.com/market/tickers?x=й"),                      # не ASCII
    ("GET", "//api.htx.com/market/tickers"),
    ("GET", "api.htx.com/market/tickers"),
    ("GET", None),
    (None, SCHEME + "api.htx.com/market/tickers"),
])
def test_json_refuses_anything_else_before_sending(method, url):
    """Не из списка — ValueError до отправки: сессия запроса не видит вовсе."""
    s = _Session()
    with pytest.raises(ValueError, match="JSON_ALLOWED"):
        arun(REAL_JSON(s, method, url))
    assert s.sent == [] and not p2p.json_allowed(method, url)
