"""Заглушки для тестов торгового ядра: сеть — только эта «сессия» (ответ по методу и пути), ключи — фиктивные.

Запрос не к api.bybit.com / open-api.bingx.com или без заготовленного ответа — ошибка теста: настоящих запросов нет.
Корутины тестов идут на ОДНОМ цикле событий на весь прогон (`run`), а не asyncio.run на каждый тест: на Windows каждый
новый цикл — пара loopback-сокетов, тысячи тестов исчерпывали буфер сокетов (WinError 10055) при полном прогоне на ПК
владельца. Цикл закрывается при выходе (atexit); задачи, оставленные тестом, отменяются после каждого run.
"""
import asyncio
import atexit
import json
import time
from decimal import Decimal

from yarl import URL

import accounts
from trading import journal, keys

KEY, SECRET = "FAKEKEY0000", "FAKESECRET0000000000"
CREDS = (KEY, SECRET)
HOSTS = {accounts.BYBIT_BASE: "bybit", accounts.BINGX_BASE: "bingx"}
D = Decimal

_LOOP = {"loop": None}


def loop():
    """Общий цикл событий тестов ядра (создаётся один раз)."""
    lp = _LOOP["loop"]
    if lp is None or lp.is_closed():
        lp = _LOOP["loop"] = asyncio.new_event_loop()
    return lp


def run(coro):
    """Выполнить корутину на общем цикле; задачи, которые тест оставил, — отменить и дождаться."""
    lp = loop()
    try:
        return lp.run_until_complete(coro)
    finally:
        left = [t for t in asyncio.all_tasks(lp) if not t.done()]
        for t in left:
            t.cancel()
        if left:
            lp.run_until_complete(asyncio.gather(*left, return_exceptions=True))


def close_loop():
    lp = _LOOP["loop"]
    if lp is not None and not lp.is_closed():
        lp.run_until_complete(lp.shutdown_asyncgens())
        lp.close()


atexit.register(close_loop)


REAL_GATE_MODE = journal._gate_mode         # пороги gates и свежесть сверки — настоящие только в своих тестах
REAL_FEED_PROBLEM = journal._feed_problem


def fresh_journal(monkeypatch, tmp_path, gates=False, feed=False):
    """Журнал в tmp, свежие замки и соединения с базой, без идущих отправок, торговые ключи CREDS проверены (итог — в
    tmp). По умолчанию пороги gates не ограничивают режим (gates=True — настоящие: бэктест из файла владельца), а
    свежесть сверки позиций (watch) не требуется (feed=True — требуется)."""
    monkeypatch.setattr(journal, "DB_PATH", str(tmp_path / "trading.db"))
    monkeypatch.setattr(journal, "_CONNS", {})
    monkeypatch.setattr(journal, "RETRY_DELAY", 0)
    monkeypatch.setattr(journal, "FLAT_GRACE", 0)     # «биржа» заглушки отвечает о позиции без задержки
    monkeypatch.setattr(journal, "_locks", {"loop": None, "state": None, "symbols": {}, "stops": {}})
    monkeypatch.setattr(journal, "_inflight", set())
    monkeypatch.setattr(keys, "CHECK_PATH", str(tmp_path / "trading_keycheck.json"))
    if not gates:
        monkeypatch.setattr(journal, "_gate_mode", lambda strategy, paper=None, live=None: "auto")
    if not feed:
        monkeypatch.setattr(journal, "_feed_problem", lambda now=None, path=None: "")
    for venue in ("bybit", "bingx"):
        keys.save_check(venue, CREDS, keys.KeyCheck(True, "ok", "", True))


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
    запроса. Каждый запрос записывается: метод, URL как отправлен, путь, query (раскодированный), заголовки, тело,
    таймаут."""
    def __init__(self, routes=None):
        self.routes, self.calls = dict(routes or {}), []

    def _answer(self, method, url, headers=None, data=None, allow_redirects=True, timeout=None):
        raw = str(url)
        base = next((b for b in HOSTS if raw.startswith(b + "/")), None)
        assert base is not None, f"запрос не к бирже ядра: {raw}"
        u = URL(raw, encoded=True)
        call = {"method": method, "url": raw, "venue": HOSTS[base], "path": u.path, "query": dict(u.query),
                "headers": dict(headers or {}), "data": data, "redirects": allow_redirects, "timeout": timeout}
        self.calls.append(call)
        r = self.routes.get((method, call["path"]))
        if r is None:
            raise AssertionError(f"unexpected request: {method} {call['path']}")
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        return r(call) if callable(r) else r

    def get(self, url, headers=None, allow_redirects=True, timeout=None):
        return self._answer("GET", url, headers, allow_redirects=allow_redirects, timeout=timeout)

    def post(self, url, headers=None, data=None, allow_redirects=True, timeout=None):
        return self._answer("POST", url, headers, data, allow_redirects, timeout)

    def delete(self, url, headers=None, allow_redirects=True, timeout=None):
        return self._answer("DELETE", url, headers, allow_redirects=allow_redirects, timeout=timeout)

    def sent(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


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


# шаги инструментов «биржи» заглушки: (qty_step, min_qty, tick, min_notional)
BYBIT_INST = {("linear", "BTCUSDT"): ("0.001", "0.001", "0.1", "5"), ("linear", "ETHUSDT"): ("0.01", "0.01", "0.01", "5"),
              ("linear", "GRAMUSDT"): ("0.1", "0.1", "0.0001", "5"),
              ("spot", "BTCUSDT"): ("0.000001", "0.000048", "0.01", "1"),
              ("spot", "ETHUSDT"): ("0.0001", "0.0001", "0.01", "1"), ("spot", "GRAMUSDT"): ("0.01", "0.1", "0.0001", "1")}
BINGX_INST = {"BTC-USDT": (4, 1, "0.0001", "2"), "ETH-USDT": (2, 2, "0.01", "2"), "GRAMTON-USDT": (1, 4, "1", "2")}
MARKS = {"BTCUSDT": "65000", "ETHUSDT": "3000", "GRAMUSDT": "3", "BTC-USDT": "65000", "ETH-USDT": "3000",
         "GRAMTON-USDT": "3"}
_BYBIT_OPEN = ("New", "PartiallyFilled", "Untriggered")
_BINGX_OPEN = ("NEW", "PARTIALLY_FILLED", "PENDING")


def now_ms():
    return int(time.time() * 1000)


class Book:
    """«Биржа»: ордера по клиентскому id (ответ создания и запросы статуса по id), список открытых ордеров символа
    (наши и ордера владельца — foreign), позиции (исполнение ордера двигает позицию), исполнения символа (наши и чужие —
    foreign_execs: ликвидация, ручная сделка владельца), условные стопы (висят, пока trigger не сработает), отмена по
    клиентскому id, плечо, режим маржи, капитал, тикер и шаги инструмента."""
    def __init__(self):
        self.orders = {}          # client_id -> вид для ответа
        self.category = {}        # client_id -> категория Bybit (список открытых ордеров — по категории)
        self.next_id = 1321003749386327552
        self.foreign = []         # открытые ордера владельца / стопы позиции: виды биржи
        self.position = {}        # символ биржи -> знаковый размер (Decimal)
        self.other = []           # ненулевые позиции других монет аккаунта: (символ биржи, знаковый размер)
        self.lists = {}           # Bybit (категория, settleCoin) -> строки позиций (USDC, inverse, option)
        self.borrows = []         # займы спот-маржи UTA: строки кошелька
        self.execs = []           # исполнения: Bybit-вид (execution/list); для BingX — из ордеров
        self.foreign_execs = []   # чужие исполнения (Bybit-вид или BingX-ордер) — добавляет тест
        self.funding = []         # начисления BingX FUNDING_FEE
        self.closed = []          # Bybit closed-pnl
        self.leverage = "2"
        self.margin = {"bybit": "ISOLATED_MARGIN", "bingx": "ISOLATED"}
        self.equity = "10000"
        self.dual = "false"
        self.marks = dict(MARKS)
        self.liq = {}             # символ биржи -> цена ликвидации (строка)

    def _move(self, sym, side, qty, reduce_only):
        signed = D(qty) if side.lower() == "buy" else -D(qty)
        cur = self.position.get(sym, D(0))
        if reduce_only:
            if cur == 0 or (cur > 0) == (signed > 0):
                return D(0)
            signed = signed if abs(signed) <= abs(cur) else -cur   # reduceOnly — не больше позиции символа
        self.position[sym] = cur + signed
        return abs(signed)

    def _exec(self, sym, side, qty, cid, oid, price, kind="Trade"):
        self.execs.append({"symbol": sym, "orderLinkId": cid, "orderId": str(oid), "side": side, "execQty": str(qty),
                           "execPrice": str(price), "execTime": str(now_ms()), "execType": kind,
                           "execId": f"e{len(self.execs) + 1}", "execFee": "0.01"})

    # --- Bybit ---
    def bybit_create(self, status="New", filled="0", **over):
        def answer(call):
            b = body(call)
            cid = b["orderLinkId"]
            if cid in self.orders:   # как биржа: id уже был — второй ордер не создаётся
                return bybit_err(110072, "OrderLinkedID is duplicate")
            self.next_id += 1
            cond = "triggerPrice" in b
            st = "Untriggered" if cond else status
            done = "0" if cond else (b["qty"] if status == "Filled" else filled)
            px = b.get("price") or self.marks.get(b["symbol"], "1")
            self.orders[cid] = dict({"orderId": str(self.next_id), "orderLinkId": cid, "symbol": b["symbol"],
                                     "side": b["side"], "orderType": b["orderType"], "qty": b["qty"],
                                     "price": b.get("price", "0"), "orderStatus": st, "cumExecQty": done,
                                     "avgPrice": px if D(done) else "", "reduceOnly": b.get("reduceOnly", False),
                                     "triggerPrice": b.get("triggerPrice", "0"),
                                     "stopOrderType": "Stop" if cond else "", "cumExecFee": "0.01" if D(done) else "0",
                                     "updatedTime": str(now_ms())}, **over)
            self.category[cid] = b["category"]
            if b["category"] != "spot" and D(done):
                moved = self._move(b["symbol"], b["side"], done, b.get("reduceOnly", False))
                if moved:
                    self._exec(b["symbol"], b["side"], moved, cid, self.next_id, px)
            return bybit_ok({"orderId": str(self.next_id), "orderLinkId": cid})
        return answer

    def bybit_query(self, call):
        q = call["query"]
        cid = q.get("orderLinkId")
        if cid is not None or q.get("openOnly") == "1":
            o = self.orders.get(cid)
            return bybit_ok({"category": q.get("category"), "list": [o] if o else [], "nextPageCursor": ""})
        sym, cat = q.get("symbol"), q.get("category")
        mine = [o for cid, o in self.orders.items() if o.get("symbol") == sym and o.get("orderStatus") in _BYBIT_OPEN
                and self.category.get(cid, "linear") == cat]
        theirs = [o for o in self.foreign if o.get("symbol") == sym]
        rows = mine + theirs
        if q.get("orderFilter") == "StopOrder":   # только условные
            rows = [o for o in rows if o.get("stopOrderType") or D(o.get("triggerPrice") or "0") > 0]
        return bybit_ok({"category": q.get("category"), "list": rows, "nextPageCursor": ""})

    def bybit_cancel(self, call):
        cid = body(call)["orderLinkId"]
        o = self.orders.get(cid)
        if not o or o.get("orderStatus") not in _BYBIT_OPEN:
            return bybit_err(110001, "Order does not exist")
        o["orderStatus"] = "Deactivated" if o.get("orderStatus") == "Untriggered" else "Cancelled"
        return bybit_ok({"orderId": o["orderId"], "orderLinkId": cid})

    def bybit_store(self, cid, **fields):
        self.orders.setdefault(cid, {"orderLinkId": cid}).update(fields)

    def _bybit_row(self, sym, size):
        return {"symbol": sym, "positionIdx": 0, "side": "Buy" if size > 0 else "Sell" if size < 0 else "",
                "size": str(abs(size)), "avgPrice": self.marks.get(sym, "1"), "markPrice": self.marks.get(sym, "1"),
                "liqPrice": self.liq.get(sym, ""), "leverage": self.leverage}

    def bybit_positions(self, call):
        q = call["query"]
        sym = q.get("symbol")
        if q.get("category") != "linear" or q.get("settleCoin") == "USDC":
            return bybit_ok({"list": self.lists.get((q.get("category"), q.get("settleCoin")), []),
                             "nextPageCursor": ""})
        if sym:
            return bybit_ok({"list": [self._bybit_row(sym, self.position.get(sym, D(0)))], "nextPageCursor": ""})
        rows = [self._bybit_row(s, v) for s, v in list(self.position.items()) + self.other if v]
        return bybit_ok({"list": rows, "nextPageCursor": ""})

    def bybit_executions(self, call):
        q = call["query"]
        a, b = int(q["startTime"]), int(q["endTime"])
        rows = [e for e in self.execs + [e for e in self.foreign_execs if "execType" in e]
                if e["symbol"] == q["symbol"] and a <= int(e["execTime"]) <= b]
        return bybit_ok({"list": rows, "nextPageCursor": ""})

    def bybit_wallet(self, call):
        return bybit_ok({"list": [{"accountType": "UNIFIED", "totalEquity": self.equity, "coin": self.borrows}]})

    def bybit_ticker(self, call):
        sym = call["query"]["symbol"]
        return bybit_ok({"list": [{"symbol": sym, "markPrice": self.marks[sym], "lastPrice": self.marks[sym]}]})

    def bybit_instrument(self, call):
        cat, sym = call["query"]["category"], call["query"]["symbol"]
        step, mn, tick, notional = BYBIT_INST[(cat, sym)]
        lot = ({"basePrecision": step, "minOrderQty": mn, "maxOrderQty": "1000", "minOrderAmt": notional}
               if cat == "spot" else {"qtyStep": step, "minOrderQty": mn, "maxOrderQty": "1000",
                                      "minNotionalValue": notional})
        return bybit_ok({"list": [{"symbol": sym, "status": "Trading", "lotSizeFilter": lot,
                                   "priceFilter": {"tickSize": tick}}]})

    def trigger(self, sym, price):
        """Цена дошла до price: условные стопы бота на этом символе срабатывают (исполняются по price, reduceOnly)."""
        for cid, o in self.orders.items():
            if o.get("symbol") != sym:
                continue
            trig = D(str(o.get("triggerPrice") or o.get("stopPrice") or "0"))
            live = o.get("orderStatus") == "Untriggered" or (o.get("type") == "STOP_MARKET"
                                                              and o.get("status") == "NEW")
            if not live or not trig:
                continue
            side = str(o.get("side")).lower()
            if (side == "sell" and D(price) <= trig) or (side == "buy" and D(price) >= trig):
                qty = o.get("qty") or o.get("origQty")
                moved = self._move(sym, side, qty, True)
                if "orderStatus" in o:
                    o.update(orderStatus="Filled", cumExecQty=str(moved), avgPrice=str(price), cumExecFee="0.01")
                    self._exec(sym, o["side"], moved, cid, o["orderId"], price)
                else:
                    o.update(status="FILLED", executedQty=str(moved), avgPrice=str(price), commission="-0.01",
                             updateTime=now_ms())

    # --- BingX ---
    def bingx_create(self, status="NEW", **over):
        def answer(call):
            q = call["query"]
            cid = q["clientOrderId"]
            if cid in self.orders:
                return bingx_err(101481, "clientOrderID has already been used")
            self.next_id += 1
            cond = q["type"] == "STOP_MARKET"
            done = q["quantity"] if status == "FILLED" and not cond else "0"
            px = q.get("price") or self.marks.get(q["symbol"], "1")
            order = dict({"symbol": q["symbol"], "orderId": self.next_id, "side": q["side"],
                          "positionSide": q["positionSide"], "type": q["type"], "origQty": q["quantity"],
                          "price": q.get("price", "0"), "executedQty": done, "status": "NEW" if cond else status,
                          "clientOrderId": cid, "avgPrice": px if D(done) else "0",
                          "stopPrice": q.get("stopPrice", ""), "reduceOnly": q.get("reduceOnly", "false"),
                          "commission": "-0.01" if D(done) else "0", "updateTime": now_ms()}, **over)
            self.orders[cid] = order
            if D(done):
                self._move(q["symbol"], q["side"], done, q.get("reduceOnly") == "true")
            return bingx_ok({"order": order})
        return answer

    def bingx_query(self, call):
        cid = call["query"].get("clientOrderId")
        o = self.orders.get(cid)
        return bingx_ok({"order": o}) if o else bingx_err(109421, "order not exist")

    def bingx_cancel(self, call):
        cid = call["query"].get("clientOrderId")
        o = self.orders.get(cid)
        if not o or o.get("status") not in _BINGX_OPEN:
            return bingx_err(109421, "order not exist")
        o["status"] = "CANCELLED"
        return bingx_ok({"order": o})

    def bingx_open(self, call):
        sym = call["query"].get("symbol")
        mine = [o for o in self.orders.values() if o.get("symbol") == sym and o.get("status") in _BINGX_OPEN]
        return bingx_ok({"orders": mine + [o for o in self.foreign if o.get("symbol") == sym]})

    def bingx_all_orders(self, call):
        q = call["query"]
        a, b = int(q["startTime"]), int(q["endTime"])
        rows = [o for o in list(self.orders.values()) + [e for e in self.foreign_execs if "execType" not in e]
                if o.get("symbol") == q["symbol"] and D(str(o.get("executedQty") or "0")) > 0
                and a <= int(o.get("updateTime") or 0) <= b]
        return bingx_ok({"orders": rows})

    def _bingx_row(self, sym, size):
        return {"symbol": sym, "positionSide": "BOTH", "positionAmt": str(size), "avgPrice": self.marks.get(sym, "1"),
                "markPrice": self.marks.get(sym, "1"), "liquidationPrice": self.liq.get(sym, "0"),
                "leverage": int(self.leverage), "isolated": self.margin["bingx"] == "ISOLATED"}

    def bingx_positions(self, call):
        sym = call["query"].get("symbol")
        items = [(sym, self.position.get(sym, D(0)))] if sym else list(self.position.items()) + self.other
        return bingx_ok([self._bingx_row(s, v) for s, v in items if v])

    def bingx_ticker(self, call):
        sym = call["query"]["symbol"]
        return bingx_ok({"symbol": sym, "lastPrice": self.marks[sym]})

    def bingx_contracts(self, call):
        sym = call["query"]["symbol"]
        qp, pp, mn, notional = BINGX_INST[sym]
        return bingx_ok([{"symbol": sym, "quantityPrecision": qp, "pricePrecision": pp, "tradeMinQuantity": mn,
                          "tradeMinUSDT": notional, "status": 1, "apiStateOpen": "true", "currency": "USDT"}])

    def routes(self, venue, **over):
        """Все пути «биржи» для заглушки Session; over — подмена отдельных (ключ — «POST /v5/order/create» и т. п.)."""
        if venue == "bybit":
            r = {("POST", "/v5/order/create"): self.bybit_create(), ("GET", "/v5/order/realtime"): self.bybit_query,
                 ("POST", "/v5/order/cancel"): self.bybit_cancel,
                 ("GET", "/v5/order/history"): bybit_ok({"list": []}),
                 ("GET", "/v5/position/list"): self.bybit_positions,
                 ("GET", "/v5/execution/list"): self.bybit_executions,
                 ("GET", "/v5/position/closed-pnl"): lambda c: bybit_ok({"list": list(self.closed)}),
                 ("GET", "/v5/account/wallet-balance"): self.bybit_wallet,
                 ("GET", "/v5/account/info"): lambda c: bybit_ok({"marginMode": self.margin["bybit"]}),
                 ("GET", "/v5/market/tickers"): self.bybit_ticker,
                 ("GET", "/v5/market/instruments-info"): self.bybit_instrument}
        else:
            r = {("POST", "/openApi/swap/v2/trade/order"): self.bingx_create(),
                 ("GET", "/openApi/swap/v2/trade/order"): self.bingx_query,
                 ("DELETE", "/openApi/swap/v2/trade/order"): self.bingx_cancel,
                 ("GET", "/openApi/swap/v2/trade/openOrders"): self.bingx_open,
                 ("GET", "/openApi/swap/v2/trade/allOrders"): self.bingx_all_orders,
                 ("GET", "/openApi/swap/v2/user/positions"): self.bingx_positions,
                 ("GET", "/openApi/swap/v2/user/income"): lambda c: bingx_ok(list(self.funding)),
                 ("GET", "/openApi/swap/v3/user/balance"): lambda c: bingx_ok([{"asset": "USDT",
                                                                                 "equity": self.equity}]),
                 ("GET", "/openApi/swap/v1/positionSide/dual"): lambda c: bingx_ok({"dualSidePosition": self.dual}),
                 ("GET", "/openApi/swap/v2/trade/leverage"): lambda c: bingx_ok(
                     {"longLeverage": int(self.leverage), "shortLeverage": int(self.leverage)}),
                 ("GET", "/openApi/swap/v2/trade/marginType"): lambda c: bingx_ok({"marginType": self.margin["bingx"]}),
                 ("GET", "/openApi/swap/v2/quote/ticker"): self.bingx_ticker,
                 ("GET", "/openApi/swap/v2/quote/contracts"): self.bingx_contracts}
        for k, v in over.items():
            method, path = k.split(" ", 1)
            r[(method, path)] = v
        return r
