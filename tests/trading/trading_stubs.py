"""Заглушки для тестов торгового ядра: сеть — только эта «сессия» (ответ по методу и пути), ключи — фиктивные.

Запрос не к api.bybit.com / open-api.bingx.com или без заготовленного ответа — ошибка теста: настоящих запросов нет.
"""
import asyncio
import json
from decimal import Decimal

from yarl import URL

import accounts

KEY, SECRET = "FAKEKEY0000", "FAKESECRET0000000000"
CREDS = (KEY, SECRET)
HOSTS = {accounts.BYBIT_BASE: "bybit", accounts.BINGX_BASE: "bingx"}


class Resp:
    """Ответ: статус и тело (объект → JSON, bytes — как есть) или исключение при входе (таймаут, обрыв)."""
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.exc = status, exc
        self.raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body, default=str).encode()

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self.raw


class Gated(Resp):
    """Ответ, который приходит, только когда тест откроет ворота (событие)."""
    def __init__(self, gate, inner):
        super().__init__(inner.status, inner.raw, inner.exc)
        self.gate = gate

    async def __aenter__(self):
        await self.gate.wait()
        return await super().__aenter__()


class Session:
    """Ответ по (метод, путь): Resp, список Resp (по очереди, последний повторяется) или функция от записанного
    запроса. Каждый запрос записывается: метод, URL как отправлен, путь, query (раскодированный), заголовки, тело."""
    def __init__(self, routes=None):
        self.routes, self.calls = dict(routes or {}), []

    def _answer(self, method, url, headers=None, data=None, allow_redirects=True):
        raw = str(url)
        base = next((b for b in HOSTS if raw.startswith(b + "/")), None)
        assert base is not None, f"запрос не к бирже ядра: {raw}"
        u = URL(raw, encoded=True)
        call = {"method": method, "url": raw, "venue": HOSTS[base], "path": u.path, "query": dict(u.query),
                "headers": dict(headers or {}), "data": data, "redirects": allow_redirects}
        self.calls.append(call)
        r = self.routes.get((method, call["path"]))
        if r is None:
            raise AssertionError(f"unexpected request: {method} {call['path']}")
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        return r(call) if callable(r) else r

    def get(self, url, headers=None, allow_redirects=True):
        return self._answer("GET", url, headers, allow_redirects=allow_redirects)

    def post(self, url, headers=None, data=None, allow_redirects=True):
        return self._answer("POST", url, headers, data, allow_redirects)

    def delete(self, url, headers=None, allow_redirects=True):
        return self._answer("DELETE", url, headers, allow_redirects=allow_redirects)

    def sent(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


def run(coro):
    return asyncio.run(coro)


def body(call):
    """Тело запроса Bybit POST → dict (порядок ключей сохранён)."""
    return json.loads(call["data"].decode("utf-8"))


def business(call):
    """Бизнес-параметры запроса BingX (без timestamp и signature)."""
    return {k: v for k, v in call["query"].items() if k not in ("timestamp", "signature")}


def bybit_ok(result):
    return Resp(200, {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "time": 1700000000000})


def bybit_err(code, msg="error"):
    return Resp(200, {"retCode": code, "retMsg": msg, "result": {}, "retExtInfo": {}, "time": 1700000000000})


def bingx_ok(data):
    return Resp(200, {"code": 0, "msg": "", "data": data})


def bingx_err(code, msg="error"):
    return Resp(200, {"code": code, "msg": msg, "data": {}})


class Book:
    """«Биржа» с ордерами по клиентскому id: ответ создания и ответы на запросы статуса по id."""
    def __init__(self):
        self.orders = {}   # client_id -> вид для ответа
        self.next_id = 1321003749386327552

    # --- Bybit ---
    def bybit_create(self, status="New", filled="0", **over):
        def answer(call):
            b = body(call)
            cid = b["orderLinkId"]
            if cid in self.orders:   # как биржа: id уже был — второй ордер не создаётся
                return bybit_err(110072, "OrderLinkedID is duplicate")
            self.next_id += 1
            self.orders[cid] = dict({"orderId": str(self.next_id), "orderLinkId": cid, "symbol": b["symbol"],
                                     "side": b["side"], "orderType": b["orderType"], "qty": b["qty"],
                                     "price": b.get("price", "0"), "orderStatus": status, "cumExecQty": filled,
                                     "avgPrice": "", "reduceOnly": b.get("reduceOnly", False)}, **over)
            return bybit_ok({"orderId": str(self.next_id), "orderLinkId": cid})
        return answer

    def bybit_query(self, call):
        cid = call["query"].get("orderLinkId")
        o = self.orders.get(cid)
        return bybit_ok({"category": call["query"].get("category"), "list": [o] if o else [], "nextPageCursor": ""})

    def bybit_store(self, cid, **fields):
        self.orders.setdefault(cid, {"orderLinkId": cid}).update(fields)

    # --- BingX ---
    def bingx_create(self, status="NEW", **over):
        def answer(call):
            q = call["query"]
            cid = q["clientOrderId"]
            if cid in self.orders:
                return bingx_err(101481, "clientOrderID has already been used")
            self.next_id += 1
            order = dict({"symbol": q["symbol"], "orderId": self.next_id, "side": q["side"],
                          "positionSide": q["positionSide"], "type": q["type"], "origQty": q["quantity"],
                          "price": q.get("price", "0"), "executedQty": "0", "status": status,
                          "clientOrderId": cid}, **over)
            self.orders[cid] = order
            return bingx_ok({"order": order})
        return answer

    def bingx_query(self, call):
        cid = call["query"].get("clientOrderId")
        o = self.orders.get(cid)
        return bingx_ok({"order": o}) if o else bingx_err(109421, "order not exist")


D = Decimal
