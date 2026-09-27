"""Биржевые вызовы торгового ядра: Bybit v5 (linear USDT-перпы и спот) и BingX (perpetual swap).

Единственное место в trading/, откуда уходят запросы к биржам (`_send`). Каждый вызов сначала проходит `prepare()`:
- пара (метод, путь) — только из ALLOWED[биржа]; всё остальное (выводы, переводы, субаккаунты, P2P, займы, earn,
  конвертации, пакетные ордера, «закрыть всё», разворот, смена режима позиции или аккаунта) — ValueError до подписи;
- поля — только из списка этого вызова, каждое значение — по своему правилу: символ — только из CANDIDATES, сторона,
  тип, количество и цена — положительные десятичные строки, клиентский id — только наш формат (чужие ордера владельца
  бот не трогает), плечо ≤ HARD_MAX_LEVERAGE, режим позиции — только односторонний (Bybit positionIdx=0, BingX
  positionSide=BOTH — всегда явно), маржа BingX — только изолированная; лишнее поле, неверное значение или
  несовместимая пара полей — ValueError;
- подпись по точным байтам: Bybit GET — accounts.bybit_headers (query — urlencode тех же параметров), Bybit POST —
  accounts.bybit_post_headers (подписывается ровно отправляемая строка тела), BingX — accounts.bingx_signed_query
  (параметры в query у всех методов, тело пустое; значения с «[»/«{» в URL кодируются, подпись — по сырой строке);
- URL не перекодируется (yarl encoded=True), за редиректом не идём (allow_redirects=False), у каждого запроса свой
  таймаут aiohttp.ClientTimeout(total=REQUEST_TIMEOUT);
- ответ — (HTTP-статус, JSON или None); `outcome()` раскладывает его на ok / rejected / duplicate / notfound /
  ambiguous; тексты ошибок — через accounts._scrub и accounts.api_error_text (без ключа, подписи и URL).
Публичные справочники (PUBLIC, `public_get`) — без ключа: проверка, какой символ биржи сейчас торгуется (TON → GRAM),
шаги количества и цены (`instrument`), цена для оценки номинала (`mark_price`).
Чтение по одному символу для проверки «чьё это» (trading/ownership.py): `symbol_positions` (строго: чужой символ,
режим хеджа позиций или битое поле — ошибка, а не «пусто»), `open_orders`, `symbol_leverage`, `margin_mode`;
по всему аккаунту — `account_positions` (кросс-маржа).

Источники: документация Bybit v5 (bybit-exchange.github.io/docs/v5: guide, order/create-order, order/cancel-order,
order/open-order, position, position/trading-stop, position/leverage, account/account-info, user/apikey-info, error,
rate-limit) и BingX (docs-v3 и api-ai-skills: authentication, swap-trade, swap-account, error-codes); сводка —
futures_api_spec.json исследования 2026-09-27. Неуверенное помечено TODO(api).
"""
import json
import re
import time
from collections import namedtuple
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

import accounts

BYBIT, BINGX = "bybit", "bingx"
VENUES = (BYBIT, BINGX)
BASES = {BYBIT: accounts.BYBIT_BASE, BINGX: accounts.BINGX_BASE}
COINS = ("BTC", "ETH", "TON")
SYMBOLS = tuple(f"{c}USDT" for c in COINS)            # канонический вид ядра: монета P2P + USDT
# Символы бирж. TON с 15.06.2026 называется GRAM (1:1): перп TONUSDT на Bybit закрыт, спот TONUSDT — «Not supported
# symbols»; торгуется GRAMUSDT (Bybit, спот и перп) и GRAMTON-USDT (BingX, displayName GRAM-USDT). ЛОВУШКА: у BingX
# «GRAM-USDT» — другой, делистнутый токен (GRM) — его здесь нет и не будет. Монеты из VERIFY торгуются только после
# свежей проверки resolve_symbols по справочнику контрактов и обороту; не прошла — монета не торгуется (хедж TON выкл).
CANDIDATES = {
    (BYBIT, "linear"): {"BTCUSDT": ("BTCUSDT",), "ETHUSDT": ("ETHUSDT",), "TONUSDT": ("GRAMUSDT",)},
    (BYBIT, "spot"): {"BTCUSDT": ("BTCUSDT",), "ETHUSDT": ("ETHUSDT",), "TONUSDT": ("GRAMUSDT",)},
    (BINGX, "swap"): {"BTCUSDT": ("BTC-USDT",), "ETHUSDT": ("ETH-USDT",), "TONUSDT": ("GRAMTON-USDT",)},
}
VERIFY = frozenset({"TONUSDT"})                        # переименованная монета: символ — только после проверки
VENUE_SYMBOLS = {v: tuple(sorted({x for (vv, _), m in CANDIDATES.items() if vv == v for c in m.values() for x in c}))
                 for v in VENUES}
RESOLVE_TTL = 6 * 3600                                 # сек: столько живёт проверка символов
MIN_TURNOVER_24H = Decimal(1_000_000)                  # USDT оборота за сутки — «ликвидный» контракт
HARD_MAX_LEVERAGE = 3                                  # потолок плана (хедж/фандинг 3×); risk.py режет ниже
RECV_WINDOW = "5000"                                   # мс; у BingX это и максимум
REQUEST_TIMEOUT = 10                                   # сек на весь запрос (ClientTimeout total) — у каждого запроса
LIST_LIMIT = 50                                        # столько открытых ордеров Bybit за запрос; ровно столько — «не все»
BYBIT_LEVERAGE_UNCHANGED = 110043                      # set-leverage: «плечо не изменилось» — для этого вызова успех
CLIENT_ID_RE = re.compile(r"t\d{12}[0-9a-f]{8}")       # наш id, 21 символ: Bybit ≤ 36 [A-Za-z0-9_-], BingX 1–40
                                                       # (BingX переводит id в нижний регистр — у нас он уже такой)
_DEC_RE = re.compile(r"\d{1,12}(?:\.\d{1,12})?")      # без знака, экспоненты и пробелов
_INT_RE = re.compile(r"\d{1,13}")
_BINGX_BAD = re.compile(r"[&=?#\r\n%+ ]")               # как validateParams у BingX: значение не должно «дописать» query

Spec = namedtuple("Spec", "kind fields required check")   # kind: "read" | "write"
Request = namedtuple("Request", "venue method path url headers body")


# --- правила значений ---

def _enum(*allowed):
    def check(v):
        if not isinstance(v, str) or v not in allowed:
            raise ValueError(f"значение {v!r} не из {allowed}")
    return check


def _positive_dec(v):
    """Положительное десятичное число строкой: "0.01", "65000.5"."""
    if not isinstance(v, str) or not _DEC_RE.fullmatch(v) or Decimal(v) <= 0:
        raise ValueError(f"не положительное число строкой: {v!r}")


def _client_id(v):
    if not isinstance(v, str) or not CLIENT_ID_RE.fullmatch(v):
        raise ValueError(f"клиентский id не нашего формата: {v!r}")


def _flag(v):
    if type(v) is not bool:
        raise ValueError(f"не bool: {v!r}")


def _oneway_idx(v):
    """positionIdx — только 0: односторонний режим (хедж-режим 1/2 ядро не использует)."""
    if type(v) is not int or v != 0:
        raise ValueError(f"positionIdx не 0: {v!r}")


def _leverage(v):
    """Плечо строкой: целое 1..HARD_MAX_LEVERAGE (дробное не нужно и проверяется хуже)."""
    if not isinstance(v, str) or not _INT_RE.fullmatch(v) or not 1 <= int(v) <= HARD_MAX_LEVERAGE:
        raise ValueError(f"плечо вне 1..{HARD_MAX_LEVERAGE}: {v!r}")


def _small_int(lo, hi):
    def check(v):
        if not isinstance(v, str) or not _INT_RE.fullmatch(v) or not lo <= int(v) <= hi:
            raise ValueError(f"число вне {lo}..{hi}: {v!r}")
    return check


def _ms(v):
    if not isinstance(v, str) or not re.fullmatch(r"\d{13}", v):
        raise ValueError(f"не время в мс: {v!r}")


def bingx_stop_loss(stop_price):
    """JSON-строка стопа BingX для ордера на открытие: {"type":"STOP_MARKET","stopPrice":<число>,
    "workingType":"MARK_PRICE"} — stopPrice JSON-числом (не строкой), как требует документация."""
    s = fmt(Decimal(stop_price))
    _positive_dec(s)
    return '{"type":"STOP_MARKET","stopPrice":' + s + ',"workingType":"MARK_PRICE"}'


def _bingx_stop(v):
    """stopLoss BingX — ровно то, что строит bingx_stop_loss (других ключей и типов нет)."""
    if not isinstance(v, str):
        raise ValueError("stopLoss не строка")
    try:
        obj = json.loads(v, parse_float=Decimal, parse_int=Decimal)
    except ValueError:
        raise ValueError("stopLoss не JSON") from None
    if not isinstance(obj, dict) or set(obj) != {"type", "stopPrice", "workingType"}:
        raise ValueError("stopLoss: не те поля")
    if not isinstance(obj["stopPrice"], Decimal) or v != bingx_stop_loss(obj["stopPrice"]):
        raise ValueError("stopLoss: не наш формат")


# --- allowlist: (метод, путь) -> Spec ---

_BYBIT_CAT = _enum("linear", "spot")
_LINEAR = _enum("linear")
_BY_SYM = _enum(*VENUE_SYMBOLS[BYBIT])
_BX_SYM = _enum(*VENUE_SYMBOLS[BINGX])


def _bybit_create_check(p):
    cat, typ = p["category"], p["orderType"]
    if p["symbol"] not in {x for c in CANDIDATES[(BYBIT, cat)].values() for x in c}:
        raise ValueError(f"{p['symbol']} не символ категории {cat}")
    if typ == "Limit" and "price" not in p:
        raise ValueError("Limit без price")
    if typ == "Market" and ("price" in p or "timeInForce" in p):
        raise ValueError("Market с price/timeInForce")
    if cat == "spot":
        extra = {"reduceOnly", "positionIdx", "stopLoss", "tpslMode", "slTriggerBy"} & set(p)
        if extra:
            raise ValueError(f"спот: поля {sorted(extra)} не для спота")
        if typ == "Market" and p.get("marketUnit") != "baseCoin":
            # у Bybit спот-маркет на покупку по умолчанию считает qty в USDT — только явное количество монеты
            raise ValueError("спот-маркет только с marketUnit=baseCoin")
        if typ == "Limit" and "marketUnit" in p:
            raise ValueError("marketUnit только для спот-маркета")
    else:
        if "marketUnit" in p:
            raise ValueError("marketUnit не для linear")
        if "positionIdx" not in p:
            raise ValueError("linear: positionIdx=0 обязателен (односторонний режим — явно)")
        if p.get("reduceOnly") is True and "stopLoss" in p:
            raise ValueError("reduceOnly вместе со стопом Bybit не принимает")
        if ("tpslMode" in p or "slTriggerBy" in p) and "stopLoss" not in p:
            raise ValueError("tpslMode/slTriggerBy без stopLoss")


def _same_leverage(p):
    if p["buyLeverage"] != p["sellLeverage"]:
        raise ValueError("buyLeverage ≠ sellLeverage (односторонний режим)")


def _bingx_order_check(p):
    typ = p["type"]
    if typ == "LIMIT" and "price" not in p:
        raise ValueError("LIMIT без price")
    if typ == "MARKET" and ("price" in p or "timeInForce" in p):
        raise ValueError("MARKET с price/timeInForce")
    if p.get("reduceOnly") == "true" and "stopLoss" in p:
        raise ValueError("стоп — только у ордера на открытие")


def _none(p):
    return None


ALLOWED = {
    BYBIT: {
        # --- запись: ордера, стоп позиции, плечо ---
        ("POST", "/v5/order/create"): Spec("write", {
            "category": _BYBIT_CAT, "symbol": _BY_SYM, "side": _enum("Buy", "Sell"),
            "orderType": _enum("Market", "Limit"), "qty": _positive_dec, "price": _positive_dec,
            "timeInForce": _enum("GTC", "IOC", "FOK", "PostOnly"), "orderLinkId": _client_id,
            "reduceOnly": _flag, "positionIdx": _oneway_idx, "stopLoss": _positive_dec,
            "tpslMode": _enum("Full"), "slTriggerBy": _enum("MarkPrice", "LastPrice"),
            "marketUnit": _enum("baseCoin"),
        }, ("category", "symbol", "side", "orderType", "qty", "orderLinkId"), _bybit_create_check),
        ("POST", "/v5/order/cancel"): Spec("write", {
            "category": _BYBIT_CAT, "symbol": _BY_SYM, "orderLinkId": _client_id,
        }, ("category", "symbol", "orderLinkId"), _none),
        # стоп на всю позицию; stopLoss > 0 — снять стоп (0) этим кодом нельзя
        ("POST", "/v5/position/trading-stop"): Spec("write", {
            "category": _LINEAR, "symbol": _BY_SYM, "tpslMode": _enum("Full"), "positionIdx": _oneway_idx,
            "stopLoss": _positive_dec, "slTriggerBy": _enum("MarkPrice", "LastPrice"),
        }, ("category", "symbol", "tpslMode", "positionIdx", "stopLoss"), _none),
        # 110043 «плечо не изменилось» — для этого вызова успех (leverage_outcome)
        ("POST", "/v5/position/set-leverage"): Spec("write", {
            "category": _LINEAR, "symbol": _BY_SYM, "buyLeverage": _leverage, "sellLeverage": _leverage,
        }, ("category", "symbol", "buyLeverage", "sellLeverage"), _same_leverage),
        # --- чтение ---
        ("GET", "/v5/order/realtime"): Spec("read", {
            "category": _BYBIT_CAT, "symbol": _BY_SYM, "orderLinkId": _client_id,
            "openOnly": _enum("0", "1"), "limit": _small_int(1, 50),
        }, ("category", "symbol"), _none),
        ("GET", "/v5/order/history"): Spec("read", {
            "category": _BYBIT_CAT, "symbol": _BY_SYM, "orderLinkId": _client_id, "limit": _small_int(1, 50),
        }, ("category", "symbol"), _none),
        ("GET", "/v5/execution/list"): Spec("read", {
            "category": _BYBIT_CAT, "symbol": _BY_SYM, "orderLinkId": _client_id, "limit": _small_int(1, 100),
            "startTime": _ms, "endTime": _ms,
        }, ("category", "symbol"), _none),
        ("GET", "/v5/position/list"): Spec("read", {
            "category": _LINEAR, "symbol": _BY_SYM, "settleCoin": _enum("USDT"), "limit": _small_int(1, 200),
        }, ("category",), _none),
        ("GET", "/v5/position/closed-pnl"): Spec("read", {
            "category": _LINEAR, "symbol": _BY_SYM, "startTime": _ms, "endTime": _ms, "limit": _small_int(1, 100),
        }, ("category",), _none),
        ("GET", "/v5/account/wallet-balance"): Spec("read", {
            "accountType": _enum("UNIFIED"), "coin": _enum("USDT"),
        }, ("accountType",), _none),
        ("GET", "/v5/account/info"): Spec("read", {}, (), _none),        # marginMode — на весь аккаунт UTA
        ("GET", "/v5/user/query-api"): Spec("read", {}, (), _none),      # права ключа (keys.py)
    },
    BINGX: {
        # --- запись ---
        ("POST", "/openApi/swap/v2/trade/order"): Spec("write", {
            "symbol": _BX_SYM, "side": _enum("BUY", "SELL"),
            "positionSide": _enum("BOTH"),   # без него BingX подставит LONG — поэтому всегда явно и только BOTH
            "type": _enum("MARKET", "LIMIT"), "quantity": _positive_dec, "price": _positive_dec,
            "timeInForce": _enum("GTC", "IOC", "FOK", "PostOnly"), "clientOrderId": _client_id,
            "reduceOnly": _enum("true", "false"), "stopLoss": _bingx_stop,
        }, ("symbol", "side", "positionSide", "type", "quantity", "clientOrderId"), _bingx_order_check),
        ("DELETE", "/openApi/swap/v2/trade/order"): Spec("write", {
            "symbol": _BX_SYM, "clientOrderId": _client_id,
        }, ("symbol", "clientOrderId"), _none),
        ("POST", "/openApi/swap/v2/trade/leverage"): Spec("write", {
            "symbol": _BX_SYM, "side": _enum("BOTH"), "leverage": _leverage,
        }, ("symbol", "side", "leverage"), _none),
        ("POST", "/openApi/swap/v2/trade/marginType"): Spec("write", {
            "symbol": _BX_SYM, "marginType": _enum("ISOLATED"),   # только изолированная (основной аккаунт)
        }, ("symbol", "marginType"), _none),
        # --- чтение ---
        ("GET", "/openApi/swap/v2/trade/order"): Spec("read", {
            "symbol": _BX_SYM, "clientOrderId": _client_id,
        }, ("symbol", "clientOrderId"), _none),
        ("GET", "/openApi/swap/v2/trade/openOrders"): Spec("read", {"symbol": _BX_SYM}, (), _none),
        ("GET", "/openApi/swap/v2/user/positions"): Spec("read", {"symbol": _BX_SYM}, (), _none),
        ("GET", "/openApi/swap/v3/user/balance"): Spec("read", {}, (), _none),
        ("GET", "/openApi/swap/v2/user/income"): Spec("read", {
            "symbol": _BX_SYM, "incomeType": _enum("REALIZED_PNL", "FUNDING_FEE", "TRADING_FEE"),
            "startTime": _ms, "endTime": _ms, "limit": _small_int(1, 1000),
        }, (), _none),
        ("GET", "/openApi/swap/v2/trade/leverage"): Spec("read", {"symbol": _BX_SYM}, ("symbol",), _none),
        ("GET", "/openApi/swap/v2/trade/marginType"): Spec("read", {"symbol": _BX_SYM}, ("symbol",), _none),
        ("GET", "/openApi/swap/v1/positionSide/dual"): Spec("read", {}, (), _none),
        ("GET", "/openApi/v1/account/apiPermissions"): Spec("read", {}, (), _none),   # права ключа (keys.py)
    },
}
# публичные справочники (без ключа и подписи): какой символ биржи сейчас торгуется
PUBLIC = {
    BYBIT: {"/v5/market/instruments-info": ({"category": _BYBIT_CAT, "symbol": _BY_SYM}, ("category", "symbol")),
            "/v5/market/tickers": ({"category": _BYBIT_CAT, "symbol": _BY_SYM}, ("category", "symbol"))},
    BINGX: {"/openApi/swap/v2/quote/contracts": ({"symbol": _BX_SYM}, ("symbol",)),
            "/openApi/swap/v2/quote/ticker": ({"symbol": _BX_SYM}, ("symbol",))},
}


def spec(venue, method, path):
    """Spec вызова или ValueError: биржа и пара (метод, путь) — только из ALLOWED."""
    calls = ALLOWED.get(venue)
    if calls is None:
        raise ValueError(f"биржа {venue!r} не из {VENUES}")
    sp = calls.get((str(method), str(path)))
    if sp is None:
        raise ValueError(f"{venue}: {method} {path} не входит в список торгового ядра")
    return sp


def _check_fields(venue, path, fields, required, params):
    unknown = set(params) - set(fields)
    if unknown:
        raise ValueError(f"{venue} {path}: поля {sorted(unknown)} не разрешены")
    missing = [k for k in required if k not in params]
    if missing:
        raise ValueError(f"{venue} {path}: нет обязательных полей {missing}")
    for k, v in params.items():
        fields[k](v)
        if venue == BINGX and (not isinstance(v, str) or _BINGX_BAD.search(v)):
            raise ValueError(f"{venue} {path}: недопустимый символ в {k}")


def validate(venue, method, path, params):
    """Проверить вызов целиком (без подписи и сети): пара (метод, путь), поля, значения, сочетания полей.
    ValueError — с причиной; возвращает Spec."""
    sp = spec(venue, method, path)
    if not isinstance(params, dict):
        raise ValueError("параметры — не словарь")
    _check_fields(venue, path, sp.fields, sp.required, params)
    sp.check(params)
    return sp


# --- подпись и отправка ---

def _bingx_url_query(signed):
    """Query для URL из подписанной строки bingx_signed_query: значения с «[» или «{» кодируются (ключи — нет), как в
    документации BingX; подпись остаётся от сырой строки. Разбор по «&» безопасен: значения его не содержат (validate)."""
    out = []
    for part in signed.split("&"):
        k, v = part.split("=", 1)
        out.append(f"{k}={quote(v, safe='')}" if "[" in v or "{" in v else part)
    return "&".join(out)


def prepare(venue, method, path, params, creds, timestamp=None):
    """Проверить и подписать вызов — всё до сети. Request(url — строка ровно тех байтов, что уйдут; body — строка
    тела или None). Ошибка правил или нет ключа — ValueError, ничего не подписано."""
    params = dict(params or {})
    validate(venue, method, path, params)
    if (not isinstance(creds, (tuple, list)) or len(creds) != 2
            or not all(isinstance(c, str) and c for c in creds)):
        raise ValueError(f"{venue}: нет торгового ключа")
    key, secret = creds
    ts = str(timestamp if timestamp is not None else int(time.time() * 1000))
    base = BASES[venue]
    if venue == BYBIT:
        if method == "GET":
            query = urlencode(params)   # те же параметры и тот же urlencode, что внутри accounts.bybit_headers
            headers = accounts.bybit_headers(key, secret, params=params, recv_window=RECV_WINDOW, timestamp=ts)
            return Request(venue, method, path, base + path + (f"?{query}" if query else ""), headers, None)
        body = json.dumps(params, separators=(",", ":"))
        headers = accounts.bybit_post_headers(key, secret, body, RECV_WINDOW, ts)
        return Request(venue, method, path, base + path, headers, body)
    signed = accounts.bingx_signed_query(secret, params, timestamp=ts, recv_window=RECV_WINDOW)
    return Request(venue, method, path, f"{base}{path}?{_bingx_url_query(signed)}", {"X-BX-APIKEY": key}, None)


async def _send(s, req):
    """Отправить подготовленный запрос: (HTTP-статус, JSON или None). Без редиректов, URL — как есть, свой таймаут
    REQUEST_TIMEOUT на весь запрос (зависший ответ не держит ни отправку, ни сверку). Числа с точкой — Decimal (id
    ордеров BingX больше 2^53 — целые Python и так точные)."""
    url = URL(req.url, encoded=True)
    kw = {"headers": req.headers, "allow_redirects": False, "timeout": aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)}
    if req.method == "GET":
        ctx = s.get(url, **kw)
    elif req.method == "POST":
        ctx = s.post(url, data=None if req.body is None else req.body.encode("utf-8"), **kw)
    elif req.method == "DELETE":
        ctx = s.delete(url, **kw)
    else:
        raise ValueError(f"метод {req.method} не поддерживается")
    async with ctx as r:
        status, raw = r.status, await r.read()
    try:
        return status, (json.loads(raw.decode("utf-8"), parse_float=Decimal) if raw else None)
    except ValueError:   # и UnicodeDecodeError
        return status, None


async def call(s, venue, method, path, params, creds, timestamp=None):
    """Проверить, подписать и отправить: (HTTP-статус, JSON или None). ValueError — до подписи; сетевые ошибки
    (таймаут, обрыв) — как есть, их разбирает вызывающий (исход неясен)."""
    return await _send(s, prepare(venue, method, path, params, creds, timestamp))


async def public_get(s, venue, path, params=None):
    """Публичный GET справочника (PUBLIC): без ключа и подписи, без редиректов. (HTTP-статус, JSON или None)."""
    rules = PUBLIC.get(venue, {}).get(path)
    if rules is None:
        raise ValueError(f"{venue}: GET {path} не входит в публичный список ядра")
    params = dict(params or {})
    _check_fields(venue, path, *rules, params)
    query = urlencode(params)
    return await _send(s, Request(venue, "GET", path, BASES[venue] + path + (f"?{query}" if query else ""), {}, None))


# --- разбор ответа ---

BYBIT_DUPLICATE = frozenset({110072, 170141})           # orderLinkId уже был
BYBIT_NOT_FOUND = frozenset({110001, 170213})           # ордера нет
BYBIT_REJECT = frozenset({10001, 10002, 10003, 10004, 10005, 10010, 110003, 110004, 110007, 110017, 110043, 110094,
                          170130, 170131, 170136, 170137, 170140, 170148, 170193, 170194, 30208})   # запрос не исполнен
BINGX_DUPLICATE = frozenset({101481, 109201})           # clientOrderId уже был / повтор за короткое время
BINGX_NOT_FOUND = frozenset({109421, 100404, 80016})    # TODO(api): 80016 — «ордера нет» у старых версий v2
BINGX_REJECT = frozenset({100001, 100004, 100412, 100413, 100419, 100421, 101204, 101206, 101209, 101211, 101212,
                          101222, 101400, 101414, 101415, 101419, 101460, 101485, 109400, 109418, 109425, 110206,
                          110400})
# 101400 у BingX — и «повтор clientOrderID» среди прочих причин: после отказа журнал всё равно спрашивает ордер по id


def _code(venue, j):
    raw = j.get("retCode") if venue == BYBIT else j.get("code")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def outcome(venue, status, j, creds=()):
    """(вид, данные, код, текст): вид — "ok" (HTTP 200 и код 0; данные — result/data), "rejected" (точный отказ:
    этот запрос не исполнен), "duplicate" (клиентский id уже занят), "notfound" (ордера нет), "ambiguous" (всё прочее:
    5xx, 3xx, 403/429, не JSON, незнакомый код). Неизвестное — всегда "ambiguous": за ним идёт запрос статуса по id."""
    if not isinstance(j, dict):
        return "ambiguous", None, None, f"HTTP {status}, ответ не JSON"
    code = _code(venue, j)
    msg = accounts._scrub(str(j.get("retMsg" if venue == BYBIT else "msg") or ""), *(creds or ()))
    text = f"код {code}" + (f": {msg}" if msg else "")
    if status != 200 or code is None:
        return "ambiguous", None, code, f"HTTP {status}, {text}"
    if code == 0:
        data = j.get("result") if venue == BYBIT else j.get("data")
        return "ok", data, 0, ""
    dup, nf, rej = ((BYBIT_DUPLICATE, BYBIT_NOT_FOUND, BYBIT_REJECT) if venue == BYBIT
                    else (BINGX_DUPLICATE, BINGX_NOT_FOUND, BINGX_REJECT))
    kind = "duplicate" if code in dup else "notfound" if code in nf else "rejected" if code in rej else "ambiguous"
    return kind, None, code, text


def leverage_outcome(venue, status, j, creds=()):
    """outcome() для смены плеча: у Bybit код 110043 («плечо не изменилось», HTTP 200) — это успех: плечо уже такое."""
    kind, data, code, text = outcome(venue, status, j, creds)
    if venue == BYBIT and status == 200 and code == BYBIT_LEVERAGE_UNCHANGED:
        return "ok", data, code, text
    return kind, data, code, text


# --- символы ---

def fmt(d):
    """Decimal -> строка без экспоненты и лишних нулей ("0.01", "65000")."""
    s = format(Decimal(d), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def dec(v):
    """Число из ответа биржи (строка/Decimal/int) -> Decimal или None ("" и мусор — None)."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


_RESOLVED = {}   # (биржа, категория) -> {"ts", "map": {канонический: биржевой | None}, "why": {канонический: причина}}


def venue_symbol(venue, category, symbol, now=None):
    """Канонический символ ядра → символ биржи. Монеты из VERIFY (TON) — только по свежей проверке resolve_symbols;
    не проверен, устарел, не торгуется — ValueError (ордер не собирается)."""
    cands = CANDIDATES.get((venue, category), {}).get(symbol)
    if not cands:
        raise ValueError(f"символ {symbol!r} не из {SYMBOLS} для {venue}/{category}")
    if symbol not in VERIFY and len(cands) == 1:
        return cands[0]
    r = _RESOLVED.get((venue, category))
    now = time.time() if now is None else now
    if not r or not 0 <= now - r["ts"] <= RESOLVE_TTL:
        raise ValueError(f"{symbol} на {venue}/{category}: символ не проверен по справочнику (resolve_symbols)")
    vs = r["map"].get(symbol)
    if vs is None:
        raise ValueError(f"{symbol} на {venue}/{category} не торгуется: {r['why'].get(symbol, 'нет контракта')}")
    return vs


def canonical_symbol(venue, v):
    """Символ биржи → канонический (GRAMUSDT → TONUSDT) или None — не наша монета (в т. ч. GRAM-USDT у BingX)."""
    v = str(v or "").upper()
    for (vv, _), m in CANDIDATES.items():
        if vv == venue:
            for canon, cands in m.items():
                if v in cands:
                    return canon
    return None


def _turnover_ok(v):
    d = dec(v)
    return d is not None and d >= MIN_TURNOVER_24H


def _bybit_list(data):
    rows = data.get("list") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else None


async def _check_candidate(s, venue, category, vs):
    """("yes"|"no"|"error", пояснение): символ есть в справочнике, торгуется и ликвиден."""
    try:
        if venue == BYBIT:
            status, j = await public_get(s, venue, "/v5/market/instruments-info", {"category": category, "symbol": vs})
            kind, data, _, msg = outcome(venue, status, j)
            rows = _bybit_list(data) if kind == "ok" else None
            if rows is None:
                return "error", msg or "справочник не прочитан"
            row = next((r for r in rows if isinstance(r, dict) and r.get("symbol") == vs), None)
            if row is None:
                return "no", "нет в справочнике"
            live = row.get("status") == "Trading" and row.get("quoteCoin") == "USDT" and (
                category == "spot" or (row.get("contractType") == "LinearPerpetual" and row.get("settleCoin") == "USDT"))
            if not live:
                return "no", f"статус {row.get('status')}"
            status, j = await public_get(s, venue, "/v5/market/tickers", {"category": category, "symbol": vs})
            kind, data, _, msg = outcome(venue, status, j)
            rows = _bybit_list(data) if kind == "ok" else None
            if rows is None:
                return "error", msg or "тикер не прочитан"
            t = next((r for r in rows if isinstance(r, dict) and r.get("symbol") == vs), None)
            return ("yes", "") if t and _turnover_ok(t.get("turnover24h")) else ("no", "мал оборот за сутки")
        status, j = await public_get(s, venue, "/openApi/swap/v2/quote/contracts", {"symbol": vs})
        kind, data, _, msg = outcome(venue, status, j)
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else None
        if kind != "ok" or rows is None:
            return "error", msg or "справочник не прочитан"
        row = next((r for r in rows if isinstance(r, dict) and r.get("symbol") == vs), None)
        if row is None:
            return "no", "нет в справочнике"
        if str(row.get("status")) != "1" or str(row.get("apiStateOpen", "")).lower() != "true" \
                or str(row.get("currency") or "USDT") != "USDT":
            return "no", f"статус {row.get('status')}, apiStateOpen {row.get('apiStateOpen')}"
        status, j = await public_get(s, venue, "/openApi/swap/v2/quote/ticker", {"symbol": vs})
        kind, data, _, msg = outcome(venue, status, j)
        if kind != "ok":
            return "error", msg or "тикер не прочитан"
        t = data[0] if isinstance(data, list) and data else data
        return ("yes", "") if isinstance(t, dict) and _turnover_ok(t.get("quoteVolume")) else ("no", "мал оборот")
    except Exception as e:   # сеть, таймаут
        return "error", accounts.api_error_text(e)


async def resolve_symbols(s, venue, category, now=None):
    """Проверить символы биржи по публичным справочникам: ({канонический: биржевой | None}, {канонический: причина}).
    Монета торгуется, только если ровно один кандидат «yes» и ни одной ошибки проверки. Результат — в кэш на
    RESOLVE_TTL (его читает venue_symbol)."""
    out, why = {}, {}
    for canon, cands in CANDIDATES[(venue, category)].items():
        res = {vs: await _check_candidate(s, venue, category, vs) for vs in cands}
        good = [vs for vs, (k, _) in res.items() if k == "yes"]
        errors = [f"{vs}: {n}" for vs, (k, n) in res.items() if k == "error"]
        if len(good) == 1 and not errors:
            out[canon] = good[0]
            continue
        out[canon] = None
        if errors:
            why[canon] = "ошибка проверки — " + "; ".join(errors)
        elif good:
            why[canon] = "несколько торгуемых символов: " + ", ".join(good)
        else:
            why[canon] = "нет торгуемого ликвидного контракта (" + "; ".join(f"{vs}: {n}" for vs, (_, n) in
                                                                             res.items()) + ")"
    _RESOLVED[(venue, category)] = {"ts": time.time() if now is None else now, "map": out, "why": why}
    return out, why


# --- ордера: нормализованный вид ---

def _exact(v):
    """Decimal из строки/Decimal/int; float и bool — ValueError (двоичная дробь дала бы не то количество)."""
    if isinstance(v, (float, bool)):
        raise ValueError(f"число {v!r}: нужен Decimal или строка")
    d = Decimal(v)
    if not d.is_finite():
        raise ValueError(f"число {v!r}")
    return d


class Order(namedtuple("Order", "venue category symbol side order_type qty price tif reduce_only stop_loss")):
    """Ордер в виде ядра, без биржевых имён: venue bybit|bingx; category linear|spot (Bybit), swap (BingX);
    symbol канонический (TONUSDT); side buy|sell; order_type market|limit; qty/price — Decimal; tif GTC|IOC|FOK|
    PostOnly (лимитки); reduce_only — перпы; stop_loss — цена стопа (только открытие перпа). Режим позиции —
    всегда односторонний."""
    __slots__ = ()

    def __new__(cls, venue, category, symbol, side, order_type, qty, price=None, tif=None, reduce_only=False,
                stop_loss=None):
        return super().__new__(cls, venue, category, symbol, side, order_type, _exact(qty),
                               None if price is None else _exact(price), tif, bool(reduce_only),
                               None if stop_loss is None else _exact(stop_loss))

    @property
    def reducing(self):
        """Ордер только уменьшает позицию: перп — reduceOnly; спот — продажа (продать можно только купленное самим
        ботом — это проверяет журнал)."""
        return self.side == "sell" if self.category == "spot" else self.reduce_only


def create_call(order, client_id, now=None):
    """(метод, путь, параметры) ордера. Все поля проходят validate; ValueError — ордер не собран."""
    if order.side not in ("buy", "sell") or order.order_type not in ("market", "limit"):
        raise ValueError("сторона/тип ордера")
    if order.venue == BYBIT:
        p = {"category": order.category, "symbol": venue_symbol(BYBIT, order.category, order.symbol, now),
             "side": "Buy" if order.side == "buy" else "Sell",
             "orderType": "Market" if order.order_type == "market" else "Limit", "qty": fmt(order.qty)}
        if order.order_type == "limit":
            p["price"] = fmt(order.price) if order.price is not None else None
            p["timeInForce"] = order.tif or "GTC"
        p["orderLinkId"] = client_id
        if order.category == "linear":
            p["positionIdx"] = 0
            if order.reduce_only:
                p["reduceOnly"] = True
            if order.stop_loss is not None:
                p.update(stopLoss=fmt(order.stop_loss), tpslMode="Full", slTriggerBy="MarkPrice")
        else:
            if order.reduce_only or order.stop_loss is not None:
                raise ValueError("спот: без reduceOnly и стопа")
            if order.order_type == "market":
                p["marketUnit"] = "baseCoin"
        call_ = ("POST", "/v5/order/create", p)
    elif order.venue == BINGX:
        if order.category != "swap":
            raise ValueError("BingX: только swap")
        p = {"symbol": venue_symbol(BINGX, "swap", order.symbol, now), "side": order.side.upper(),
             "positionSide": "BOTH", "type": order.order_type.upper(), "quantity": fmt(order.qty)}
        if order.order_type == "limit":
            p["price"] = fmt(order.price) if order.price is not None else None
            p["timeInForce"] = order.tif or "GTC"
        p["clientOrderId"] = client_id
        if order.reduce_only:
            p["reduceOnly"] = "true"
        if order.stop_loss is not None:
            p["stopLoss"] = bingx_stop_loss(order.stop_loss)
        call_ = ("POST", "/openApi/swap/v2/trade/order", p)
    else:
        raise ValueError(f"биржа {order.venue!r}")
    if None in call_[2].values():
        raise ValueError("лимитка без цены")
    validate(order.venue, *call_)
    return call_


def cancel_call(venue, category, venue_sym, client_id):
    """Снятие нашего ордера по клиентскому id; символ — биржевой, как в параметрах создания."""
    if venue == BYBIT:
        c = ("POST", "/v5/order/cancel", {"category": category, "symbol": venue_sym, "orderLinkId": client_id})
    else:
        c = ("DELETE", "/openApi/swap/v2/trade/order", {"symbol": venue_sym, "clientOrderId": client_id})
    validate(venue, *c)
    return c


_BYBIT_STATES = {"New": "open", "PartiallyFilled": "open", "Untriggered": "open", "Active": "open",
                 "Filled": "filled", "Cancelled": "closed", "PartiallyFilledCanceled": "closed",
                 "Deactivated": "closed", "Rejected": "closed"}   # Triggered — условный ордер, у нас таких нет → None
_BINGX_STATES = {"NEW": "open", "PARTIALLY_FILLED": "open", "PARTIALLYFILLED": "open", "PENDING": "open",
                 "FILLED": "filled", "CANCELED": "closed", "CANCELLED": "closed", "EXPIRED": "closed",
                 "FAILED": "closed"}


def _first(d, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return None


def _bybit_fee(item, symbol):
    """Комиссия ордера в базовой монете (для спот-покупки — сколько монеты удержано): cumFeeDetail {монета: сумма}
    (cumExecFee у linear/spot устарел), иначе cumExecFee. Нет — None."""
    detail = item.get("cumFeeDetail")
    base = symbol[:-4] if isinstance(symbol, str) and symbol.endswith("USDT") else None
    if isinstance(detail, dict) and base:
        vals = [dec(v) for k, v in detail.items() if str(k).upper() == base]
        if vals and None not in vals:
            return sum(vals, Decimal(0))
    return dec(item.get("cumExecFee"))


def order_view(venue, item):
    """Ордер из ответа биржи (создание или запрос) в виде ядра: {client_id, order_id, symbol, raw_symbol, side, type,
    qty, price, filled, avg_price, fee, reduce_only, status, state} или None, если это не ордер. symbol — канонический
    (None — не наша монета, в т. ч. «GRAM-USDT» BingX), raw_symbol — как прислала биржа (журнал сверяет его с символом
    из параметров создания: чужой или пустой — несовпадение). state: open / filled / closed / rejected (закрыт без
    исполнения по отказу биржи). Незнакомый статус — state None (журнал оставит unknown)."""
    if venue == BINGX and isinstance(item, dict) and isinstance(item.get("order"), dict):
        item = item["order"]   # BingX заворачивает ордер в data.order
    if not isinstance(item, dict):
        return None
    if venue == BYBIT:
        status = str(item.get("orderStatus") or "")
        filled, reduce_only = dec(item.get("cumExecQty")), item.get("reduceOnly")
        cid, oid = _first(item, "orderLinkId"), _first(item, "orderId")
        fee = _bybit_fee(item, item.get("symbol"))
        typ = str(item.get("orderType") or "").lower()
    else:
        status = str(item.get("status") or "").upper()
        filled, reduce_only = dec(item.get("executedQty")), item.get("reduceOnly")
        cid, oid = _first(item, "clientOrderId", "clientOrderID"), _first(item, "orderId", "orderID")
        fee = None
        typ = str(item.get("type") or "").lower()
    state = (_BYBIT_STATES if venue == BYBIT else _BINGX_STATES).get(status)
    if state == "closed" and status in ("Rejected", "FAILED") and not filled:
        state = "rejected"
    if isinstance(reduce_only, str) and reduce_only.lower() in ("true", "false"):
        reduce_only = reduce_only.lower() == "true"
    return {"client_id": str(cid or "").lower() if venue == BINGX else str(cid or ""), "order_id": str(oid or ""),
            "symbol": canonical_symbol(venue, item.get("symbol")), "raw_symbol": str(item.get("symbol") or ""),
            "side": str(item.get("side") or "").lower(),
            "type": typ, "qty": dec(_first(item, "qty", "origQty", "quantity")), "price": dec(item.get("price")),
            "filled": filled, "avg_price": dec(item.get("avgPrice")), "fee": fee,
            "reduce_only": reduce_only if isinstance(reduce_only, bool) else None, "status": status, "state": state}


async def find_order(s, venue, category, venue_sym, client_id, creds):
    """Ордер по нашему клиентскому id: ("found", вид, ""), ("notfound", None, текст) или ("ambiguous", None, текст) —
    запрос не удался (исход создания по-прежнему неизвестен). Bybit: /v5/order/realtime (открытые, затем openOnly=1 —
    последние 500 закрытых; этот кэш Bybit сбрасывает при своём перезапуске), затем /v5/order/history (отменённые —
    только за 24 ч); «пусто везде» — не найден. BingX: /openApi/swap/v2/trade/order (любой статус)."""
    if venue == BYBIT:
        base = {"category": category, "symbol": venue_sym, "orderLinkId": client_id}
        queries = [("/v5/order/realtime", base), ("/v5/order/realtime", {**base, "openOnly": "1"}),
                   ("/v5/order/history", base)]
    else:
        queries = [("/openApi/swap/v2/trade/order", {"symbol": venue_sym, "clientOrderId": client_id})]
    for path, params in queries:
        try:
            status, j = await call(s, venue, "GET", path, params, creds)
        except Exception as e:   # таймаут, обрыв и т. п.; ошибка правил тоже — не «не найден», а «неизвестно»
            return "ambiguous", None, accounts.api_error_text(e)
        kind, data, code, msg = outcome(venue, status, j, creds)
        if kind == "notfound":
            if venue == BINGX:
                return "notfound", None, msg
            continue
        if kind != "ok":
            return "ambiguous", None, msg
        items = _bybit_list(data) if venue == BYBIT else [data]
        if items is None:
            return "ambiguous", None, "неожиданная форма ответа"
        for it in items:
            view = order_view(venue, it)
            if view and view["client_id"] == client_id:
                return "found", view, ""
        if venue == BINGX:
            return "ambiguous", None, "ответ без нашего ордера"
    return "notfound", None, "ордер с этим id не найден"


def position_view(venue, item):
    """Позиция в виде ядра или None (пустая/не наша монета): {venue, symbol, side long|short, size, entry, mark, liq,
    leverage, isolated}. Bybit: side "" — пустая позиция; liqPrice "" — нет цены ликвидации (liq None)."""
    if not isinstance(item, dict):
        return None
    sym = canonical_symbol(venue, item.get("symbol"))
    if sym is None:
        return None
    if venue == BYBIT:
        side = {"Buy": "long", "Sell": "short"}.get(str(item.get("side") or ""))
        size = dec(item.get("size"))
        return None if side is None or not size else {
            "venue": venue, "symbol": sym, "side": side, "size": size, "entry": dec(item.get("avgPrice")),
            "mark": dec(item.get("markPrice")), "liq": dec(item.get("liqPrice")) or None,
            "leverage": dec(item.get("leverage")), "isolated": None}   # у UTA маржа — на весь аккаунт (/v5/account/info)
    amt = dec(item.get("positionAmt"))
    ps = str(item.get("positionSide") or "").upper()
    if not amt:
        return None
    side = "long" if ps == "LONG" else "short" if ps == "SHORT" else ("long" if amt > 0 else "short")
    iso = item.get("isolated")
    return {"venue": venue, "symbol": sym, "side": side, "size": abs(amt), "entry": dec(item.get("avgPrice")),
            "mark": dec(item.get("markPrice")), "liq": dec(item.get("liquidationPrice")) or None,
            "leverage": dec(item.get("leverage")), "isolated": iso if isinstance(iso, bool) else None}


async def positions(s, venue, creds):
    """Открытые позиции по нашим монетам: (список видов, None) или (None, текст ошибки). Это ВСЕ позиции аккаунта по
    этим монетам — и ручные владельца: что из них бота, решает journal.own_positions (ownership.annotate)."""
    if venue == BYBIT:
        path, params = "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": "200"}
    else:
        path, params = "/openApi/swap/v2/user/positions", {}
    try:
        status, j = await call(s, venue, "GET", path, params, creds)
    except Exception as e:
        return None, accounts.api_error_text(e)
    kind, data, _, msg = outcome(venue, status, j, creds)
    if kind != "ok":
        return None, msg
    rows = _bybit_list(data) if venue == BYBIT else data
    if not isinstance(rows, list):
        return None, "неожиданная форма ответа"
    return [v for v in (position_view(venue, it) for it in rows) if v], None


# --- чтение по одному символу: чьё это (trading/ownership.py) ---

async def _read(s, venue, path, params, creds):
    """GET с ключом → (данные, "") или (None, причина). Любая ошибка — причина, не исключение."""
    try:
        status, j = await call(s, venue, "GET", path, params, creds)
    except Exception as e:   # сеть, таймаут, ошибка правил
        return None, accounts.api_error_text(e)
    kind, data, _, msg = outcome(venue, status, j, creds)
    if kind != "ok":
        return None, msg or f"ответ {kind}"
    return data, ""


def _same_symbol(raw, venue_sym):
    return isinstance(raw, str) and raw.upper() == venue_sym.upper()


def _signed_size(venue, item):
    """Знаковый размер строки позиции (long > 0, short < 0) или ValueError — битая строка."""
    if venue == BYBIT:
        size, side = dec(item.get("size")), str(item.get("side") or "")
        if size is None or size < 0:
            raise ValueError(f"битое количество позиции {item.get('size')!r}")
        if size == 0:
            return Decimal(0)
        if side not in ("Buy", "Sell"):
            raise ValueError(f"сторона позиции {side!r}")
        return size if side == "Buy" else -size
    amt = dec(item.get("positionAmt"))
    if amt is None:
        raise ValueError(f"битое количество позиции {item.get('positionAmt')!r}")
    ps = str(item.get("positionSide") or "").upper()
    if ps == "LONG":
        return abs(amt)
    if ps == "SHORT":
        return -abs(amt)
    if ps in ("BOTH", ""):
        return amt   # TODO(api): в одностороннем режиме BingX знак positionAmt — направление (как в position_view)
    raise ValueError(f"positionSide {ps!r}")


async def symbol_positions(s, venue, venue_sym, creds):
    """Позиция одного перп-символа, строго: ({"net": знаковый размер, "rows": [...], "leverage": плечо или None}, "")
    или (None, причина). Строка чужого символа, битое количество, незнакомая сторона, больше одной ненулевой строки
    (режим хеджа позиций) или ненулевая строка Bybit с positionIdx ≠ 0 — ошибка, а не «пусто»: бот не угадывает.
    Плечо — Bybit: из строки positionIdx 0 (биржа отдаёт её и при нулевой позиции); BingX — symbol_leverage."""
    if venue == BYBIT:
        path, params = "/v5/position/list", {"category": "linear", "symbol": venue_sym}
    else:
        path, params = "/openApi/swap/v2/user/positions", {"symbol": venue_sym}
    data, why = await _read(s, venue, path, params, creds)
    if why:
        return None, why
    rows = _bybit_list(data) if venue == BYBIT else data
    if not isinstance(rows, list):
        return None, "неожиданная форма ответа (позиции)"
    out, lev, net = [], None, Decimal(0)
    for it in rows:
        if not isinstance(it, dict) or not _same_symbol(it.get("symbol"), venue_sym):
            got = it.get("symbol") if isinstance(it, dict) else it
            return None, f"в ответе позиция не по символу {venue_sym}: {str(got)[:40]!r}"
        try:
            signed = _signed_size(venue, it)
        except ValueError as e:
            return None, str(e)
        idx = it.get("positionIdx", 0) if venue == BYBIT else 0
        if signed and idx != 0:
            return None, "позиция в режиме хеджа (positionIdx ≠ 0) — ядро работает только в одностороннем режиме"
        if venue == BYBIT and idx == 0:
            lev = dec(it.get("leverage"))
        if signed:
            out.append({"signed": signed, "entry": dec(it.get("avgPrice")), "mark": dec(it.get("markPrice")),
                        "liq": dec(it.get("liqPrice" if venue == BYBIT else "liquidationPrice")) or None,
                        "leverage": dec(it.get("leverage"))})
            net += signed
    if len(out) > 1:
        return None, "по символу больше одной позиции (режим хеджа позиций?) — бот не угадывает"
    return {"net": net, "rows": out, "leverage": lev if lev and lev > 0 else None}, ""


async def symbol_leverage(s, venue_sym, creds):
    """Плечо символа BingX (GET /openApi/swap/v2/trade/leverage): большее из long/short — (Decimal, "") или
    (None, причина). У Bybit плечо — в symbol_positions."""
    data, why = await _read(s, BINGX, "/openApi/swap/v2/trade/leverage", {"symbol": venue_sym}, creds)
    if why:
        return None, why
    vals = [dec(data.get(k)) for k in ("longLeverage", "shortLeverage")] if isinstance(data, dict) else [None]
    if None in vals or any(v <= 0 for v in vals):
        return None, "плечо символа не прочитано"
    return max(vals), ""


_BYBIT_STOP_TYPES = frozenset({"StopLoss", "TakeProfit", "TrailingStop", "PartialStopLoss", "PartialTakeProfit",
                               "Stop", "tpslOrder"})
_BINGX_STOP_TYPES = frozenset({"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT", "TRAILING_STOP_MARKET",
                               "TRIGGER_MARKET", "TRIGGER_LIMIT", "TRAILING_TP_SL"})


def open_order_view(venue, item):
    """Открытый ордер из списка биржи → {client_id, order_id, raw_symbol, side, type, qty, price, trigger, stop_type,
    reduce_only} или None (не словарь). stop_type — вид условного ордера (Bybit stopOrderType, BingX type STOP_MARKET…),
    "" — обычный. Стоп позиции (tpslMode=Full у Bybit, stopLoss у BingX) биржа показывает условным ордером без нашего
    клиентского id — его ownership сверяет с ценой стопа из журнала."""
    if not isinstance(item, dict):
        return None
    if venue == BYBIT:
        stop_type, cid = str(item.get("stopOrderType") or ""), str(item.get("orderLinkId") or "")
        trigger, typ = dec(item.get("triggerPrice")), str(item.get("orderType") or "").lower()
        reduce = item.get("reduceOnly") is True or item.get("closeOnTrigger") is True
    else:
        t = str(item.get("type") or "").upper()
        stop_type, typ = (t if t in _BINGX_STOP_TYPES else ""), t.lower()
        cid = str(item.get("clientOrderId") or item.get("clientOrderID") or "").lower()
        trigger = dec(item.get("stopPrice"))
        reduce = any(item.get(k) is True or str(item.get(k)).lower() == "true" for k in ("reduceOnly", "closePosition"))
    return {"client_id": cid, "order_id": str(_first(item, "orderId", "orderID") or ""),
            "raw_symbol": str(item.get("symbol") or ""), "side": str(item.get("side") or "").lower(), "type": typ,
            "qty": dec(_first(item, "qty", "origQty", "quantity")), "price": dec(item.get("price")),
            "trigger": trigger or None, "stop_type": stop_type, "reduce_only": reduce}


async def open_orders(s, venue, category, venue_sym, creds):
    """Все открытые ордера символа — наши, владельца, условные: (список open_order_view, "") или (None, причина).
    Строго: не та форма ответа, ордер чужого символа, у Bybit LIST_LIMIT строк и больше (могли прочитать не все) —
    ошибка. TODO(api): у Bybit UTA для linear без orderFilter в ответе и условные ордера (стопы позиции) — сверить на
    живом ключе владельца до первого реального ордера."""
    if venue == BYBIT:
        path, params = "/v5/order/realtime", {"category": category, "symbol": venue_sym, "limit": str(LIST_LIMIT)}
    else:
        path, params = "/openApi/swap/v2/trade/openOrders", {"symbol": venue_sym}
    data, why = await _read(s, venue, path, params, creds)
    if why:
        return None, why
    rows = _bybit_list(data) if venue == BYBIT else data.get("orders") if isinstance(data, dict) else None
    if not isinstance(rows, list):   # TODO(api): BingX без ордеров — "orders": [] (null считаем ошибкой)
        return None, "неожиданная форма ответа (ордера)"
    if venue == BYBIT and len(rows) >= LIST_LIMIT:
        return None, f"открытых ордеров {LIST_LIMIT} и больше — прочитаны не все"
    out = []
    for it in rows:
        view = open_order_view(venue, it)
        if view is None or not _same_symbol(view["raw_symbol"], venue_sym):
            return None, f"в ответе ордер не по символу {venue_sym}"
        out.append(view)
    return out, ""


async def account_positions(s, venue, creds):
    """Все ненулевые USDT-перп позиции аккаунта, любые монеты (для кросс-маржи — у неё общий залог):
    ([{raw_symbol, symbol (канонический или None), signed}], "") или (None, причина). Битая строка — ошибка."""
    if venue == BYBIT:
        path, params = "/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": "200"}
    else:
        path, params = "/openApi/swap/v2/user/positions", {}
    data, why = await _read(s, venue, path, params, creds)
    if why:
        return None, why
    rows = _bybit_list(data) if venue == BYBIT else data
    if not isinstance(rows, list):
        return None, "неожиданная форма ответа (позиции аккаунта)"
    if venue == BYBIT and len(rows) >= 200:
        return None, "позиций 200 и больше — прочитаны не все"
    out = []
    for it in rows:
        if not isinstance(it, dict) or not isinstance(it.get("symbol"), str):
            return None, "битая строка позиции"
        try:
            signed = _signed_size(venue, it)
        except ValueError as e:
            return None, str(e)
        if signed:
            out.append({"raw_symbol": it["symbol"], "symbol": canonical_symbol(venue, it["symbol"]), "signed": signed})
    return out, ""


# --- публичные справочники: шаги инструмента и цена ---

Instrument = namedtuple("Instrument", "qty_step min_qty max_qty tick min_notional")


def _positive(*vals):
    return all(v is not None and v > 0 for v in vals)


def instrument_view(venue, category, item):
    """Строка справочника → Instrument (шаг количества, мин./макс. количество, шаг цены, мин. номинал USDT) или None —
    чего-то нет или не положительное (тогда ордер не собирается)."""
    if not isinstance(item, dict):
        return None
    if venue == BYBIT:
        lot, pf = item.get("lotSizeFilter"), item.get("priceFilter")
        if not isinstance(lot, dict) or not isinstance(pf, dict):
            return None
        step = dec(lot.get("basePrecision" if category == "spot" else "qtyStep"))
        min_qty, max_qty = dec(lot.get("minOrderQty")), dec(lot.get("maxOrderQty")) or None
        tick = dec(pf.get("tickSize"))
        min_notional = dec(lot.get("minOrderAmt" if category == "spot" else "minNotionalValue"))
        if min_notional is None:
            min_notional = Decimal(0) if category != "spot" else None   # старые контракты без minNotionalValue
    else:
        qp, pp = item.get("quantityPrecision"), item.get("pricePrecision")
        if type(qp) is not int or type(pp) is not int or not (0 <= qp <= 12 and 0 <= pp <= 12):
            return None
        step, tick = Decimal(1).scaleb(-qp), Decimal(1).scaleb(-pp)
        min_qty, max_qty, min_notional = dec(item.get("tradeMinQuantity")), None, dec(item.get("tradeMinUSDT"))
    if not _positive(step, min_qty, tick) or min_notional is None or min_notional < 0 \
            or (max_qty is not None and max_qty < min_qty):
        return None
    return Instrument(step, min_qty, max_qty, tick, min_notional)


async def instrument(s, venue, category, venue_sym):
    """Шаги инструмента из публичного справочника: (Instrument, "") или (None, причина)."""
    try:
        if venue == BYBIT:
            status, j = await public_get(s, venue, "/v5/market/instruments-info",
                                         {"category": category, "symbol": venue_sym})
        else:
            status, j = await public_get(s, venue, "/openApi/swap/v2/quote/contracts", {"symbol": venue_sym})
    except Exception as e:
        return None, accounts.api_error_text(e)
    kind, data, _, msg = outcome(venue, status, j)
    if kind != "ok":
        return None, msg or "справочник не прочитан"
    rows = _bybit_list(data) if venue == BYBIT else data if isinstance(data, list) else [data]
    row = next((r for r in rows or () if isinstance(r, dict) and r.get("symbol") == venue_sym), None)
    inst = instrument_view(venue, category, row)
    return (inst, "") if inst else (None, f"{venue_sym}: нет шагов инструмента в справочнике")


async def mark_price(s, venue, category, venue_sym):
    """Цена для оценки номинала: Bybit linear — markPrice, спот — lastPrice; BingX — lastPrice. (Decimal, "") или
    (None, причина)."""
    try:
        if venue == BYBIT:
            status, j = await public_get(s, venue, "/v5/market/tickers", {"category": category, "symbol": venue_sym})
        else:
            status, j = await public_get(s, venue, "/openApi/swap/v2/quote/ticker", {"symbol": venue_sym})
    except Exception as e:
        return None, accounts.api_error_text(e)
    kind, data, _, msg = outcome(venue, status, j)
    if kind != "ok":
        return None, msg or "тикер не прочитан"
    if venue == BYBIT:
        row = next((r for r in _bybit_list(data) or () if isinstance(r, dict) and r.get("symbol") == venue_sym), None)
        px = dec((row or {}).get("markPrice" if category == "linear" else "lastPrice"))
    else:
        row = data[0] if isinstance(data, list) and data else data
        px = dec(row.get("lastPrice")) if isinstance(row, dict) and row.get("symbol") == venue_sym else None
    return (px, "") if px and px > 0 else (None, f"{venue_sym}: нет цены в тикере")


# --- вызовы настроек символа (их отправляет journal после проверки «чьё это») ---

def leverage_call(venue, venue_sym, leverage):
    """(метод, путь, параметры) смены плеча: целое 1..HARD_MAX_LEVERAGE, односторонний режим."""
    lev = str(leverage)
    if venue == BYBIT:
        c = ("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": venue_sym, "buyLeverage": lev,
                                                   "sellLeverage": lev})
    else:
        c = ("POST", "/openApi/swap/v2/trade/leverage", {"symbol": venue_sym, "side": "BOTH", "leverage": lev})
    validate(venue, *c)
    return c


def margin_isolated_call(venue_sym):
    """BingX: изолированная маржа символа (у Bybit UTA режим маржи — на весь аккаунт, ядро его не меняет)."""
    c = ("POST", "/openApi/swap/v2/trade/marginType", {"symbol": venue_sym, "marginType": "ISOLATED"})
    validate(BINGX, *c)
    return c


def stop_call(venue_sym, stop):
    """Bybit: стоп всей позиции символа (tpslMode=Full, по mark) — трогать можно, только если вся позиция — бота."""
    c = ("POST", "/v5/position/trading-stop", {"category": "linear", "symbol": venue_sym, "tpslMode": "Full",
                                               "positionIdx": 0, "stopLoss": fmt(stop), "slTriggerBy": "MarkPrice"})
    validate(BYBIT, *c)
    return c


MARGIN_MODES = {"ISOLATED_MARGIN": "isolated", "REGULAR_MARGIN": "cross", "PORTFOLIO_MARGIN": "portfolio",
                "ISOLATED": "isolated", "CROSSED": "cross"}


async def margin_mode(s, venue, venue_sym, creds):
    """Режим маржи, в котором откроется позиция: ("isolated" | "cross" | "portfolio", "") или (None, причина).
    Bybit UTA — на весь аккаунт (/v5/account/info marginMode; ядро его не меняет), BingX — по символу
    (/openApi/swap/v2/trade/marginType). Не узнали — None: открытие запрещено (risk.py)."""
    if venue == BYBIT:
        path, params, key = "/v5/account/info", {}, "marginMode"
    else:
        path, params, key = "/openApi/swap/v2/trade/marginType", {"symbol": venue_sym}, "marginType"
    try:
        status, j = await call(s, venue, "GET", path, params, creds)
    except Exception as e:
        return None, accounts.api_error_text(e)
    kind, data, _, msg = outcome(venue, status, j, creds)
    if kind != "ok" or not isinstance(data, dict):
        return None, msg or "неожиданная форма ответа"
    mode = MARGIN_MODES.get(str(data.get(key) or ""))
    return (mode, "") if mode else (None, f"незнакомый режим маржи {data.get(key)!r}")
