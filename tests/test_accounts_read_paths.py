"""Подписанные GET Bybit/MEXC/HTX/KuCoin — только пути чтения из *_READ_PATHS (как BINGX_READ_PATHS): другой путь —
ValueError до подписи и отправки. И каждый путь из списков кодом бота действительно используется, а каждый путь,
который код запрашивает, в списке есть (иначе история или баланс тихо пропадали бы: ошибки там глотаются)."""

import pytest

import accounts
import netstatus
from helpers import arun

READ_LISTS = {"bybit_get": accounts.BYBIT_READ_PATHS, "mexc_get": accounts.MEXC_READ_PATHS,
              "htx_get": accounts.HTX_READ_PATHS, "kucoin_get": accounts.KUCOIN_READ_PATHS}


class _Resp:
    def __init__(self, url):
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        if "/v2/user/uid" in self.url:        # HTX: права ключа — только после uid
            return {"code": 200, "data": 1}
        return {}


class _Session:
    def __init__(self):
        self.sent = []

    def get(self, url, headers=None, **kw):
        self.sent.append(url)
        return _Resp(url)


def _call(getter, s, path, params=None):
    fn = getattr(accounts, getter)
    if getter == "kucoin_get":
        return fn(s, "key", "secret", "pass", path, params)
    return fn(s, "key", "secret", path, params)


@pytest.mark.parametrize("getter, path", [
    ("bybit_get", "/v5/order/create"), ("bybit_get", "/v5/order/realtime"), ("bybit_get", "/v5/position/list"),
    ("bybit_get", "/v5/execution/list"), ("bybit_get", "/v5/asset/withdraw/create"),
    ("bybit_get", "/v5/asset/transfer/inter-transfer"), ("bybit_get", "/v5/account/wallet-balance?accountType=X"),
    ("bybit_get", "/v5/account/wallet-balance/"), ("bybit_get", "/V5/ACCOUNT/WALLET-BALANCE"), ("bybit_get", ""),
    ("mexc_get", "/api/v3/order"), ("mexc_get", "/api/v3/openOrders"), ("mexc_get", "/api/v3/capital/withdraw/apply"),
    ("mexc_get", "/api/v3/capital/withdraw"), ("mexc_get", "/api/v3/capital/transfer"),
    ("htx_get", "/v1/order/orders/place"), ("htx_get", "/v1/dw/withdraw/api/create"), ("htx_get", "/v1/account/transfer"),
    ("htx_get", "/linear-swap-api/v1/swap_position_info"),
    ("kucoin_get", "/api/v1/orders"), ("kucoin_get", "/api/v1/hf/orders"), ("kucoin_get", "/api/v1/margin/order"),
    ("kucoin_get", "/api/v2/accounts/inner-transfer"), ("kucoin_get", "/api/v1/accounts/ledgers"),
])
def test_signed_get_refuses_paths_outside_read_list_before_signing(monkeypatch, getter, path):
    signed = []
    for helper in ("bybit_headers", "mexc_signed_params", "htx_signed_params", "kucoin_headers"):
        monkeypatch.setattr(accounts, helper, lambda *a, **kw: signed.append(a) or {})
    s = _Session()
    with pytest.raises(ValueError, match="не входит в список чтения"):
        arun(_call(getter, s, path))
    assert s.sent == [] and signed == []


@pytest.mark.parametrize("getter", sorted(READ_LISTS))
def test_signed_get_sends_every_listed_path(getter):
    s = _Session()
    for path in sorted(READ_LISTS[getter]):
        assert arun(_call(getter, s, path, {"limit": 1})) == ({"code": 200, "data": 1}
                                                                     if path == "/v2/user/uid" else {})
    assert len(s.sent) == len(READ_LISTS[getter])


def test_read_lists_match_what_the_code_requests(monkeypatch):
    """Что код бота запрашивает подписанным GET (права ключа, проверка, балансы, история, сети монет netstatus), —
    ровно списки чтения: пути нет в списке — запрос не уходит (и история тихо пустеет); путь в списке, а код его
    не запрашивает, — лишний, убрать."""
    seen = {g: [] for g in READ_LISTS}
    for getter in READ_LISTS:
        real = getattr(accounts, getter)

        def spy(*args, _real=real, _getter=getter, **kw):
            seen[_getter].append(args[4] if _getter == "kucoin_get" else args[3])
            return _real(*args, **kw)
        monkeypatch.setattr(accounts, getter, spy)
    monkeypatch.setattr(accounts, "keys", lambda ex: ("key", "secret"))
    monkeypatch.setattr(accounts, "passphrase", lambda ex: "pass")
    s = _Session()

    async def fake_public(sess, method, url):
        return {}

    async def go():
        for ex in ("bybit", "mexc", "htx", "kucoin"):
            await accounts.key_permissions(s, ex)
            await accounts.verify(s, ex)
            if ex != "bybit":   # история Bybit — P2P-ордера через bybit_post
                await accounts.account_history(s, ex)
        await accounts.bybit_balances(s, "key", "secret")
        await accounts.mexc_balances(s, "key", "secret")
        await netstatus.refresh(s, ["USDT"], ["bybit", "mexc"], fake_public)
    arun(go())
    for getter, allowed in READ_LISTS.items():
        assert set(seen[getter]) <= allowed, (getter, sorted(set(seen[getter]) - allowed))
        assert set(seen[getter]) == allowed, (getter, "лишнее в списке:", sorted(allowed - set(seen[getter])))
    assert len(s.sent) == sum(len(v) for v in seen.values())   # всё из списка ушло, ничего не отказано
