"""Подписанные запросы к приватным API бирж, ТОЛЬКО ЧТЕНИЕ (балансы, история, уведомления).

Ключи хранятся локально: сначала `data/keys.json` (папка `data/` в git не попадает), иначе `.env`
(`BYBIT_API_KEY`/`BYBIT_API_SECRET`, `MEXC_API_KEY`/`MEXC_API_SECRET`). Нет ключей для биржи —
`keys()` вернёт None, функции аккаунтов для неё выключены. Удалённый в боте ключ помечается в
`data/keys.json` как `disabled` — пометка выключает его, даже если он остался в `.env`. Никаких
торговых/выводных запросов — только подписанные запросы к read-only эндпоинтам. key/secret/passphrase в
`data/keys.json` хранятся зашифрованными Windows DPAPI (`protect`/`unprotect`) — файл бесполезен вне этой
учётной записи Windows; открытые значения от прошлой версии шифруются при старте бота (`encrypt_saved_keys`).
POST к Bybit (`bybit_post`) — только пути из BYBIT_POST_PATHS (история P2P-ордеров), без редиректов.
Подписанные GET (`bybit_get`, `mexc_get`, `htx_get`, `kucoin_get`) — только пути из BYBIT/MEXC/HTX/KUCOIN_READ_PATHS
(балансы, права ключа, история, сети монет); другой путь — ValueError до подписи и отправки.

BingX и Cryptomus — только аккаунты (P2P-площадками бота они не являются). Чтение у них ограничено в самом коде,
а не правами ключа: `bingx_get` — только GET и только пути из BINGX_READ_PATHS (спот- и Fund-баланс, права ключа,
история депозитов и выводов), POST-запросов к BingX в коде нет; `cryptomus_call` — только пары (метод, путь) из
CRYPTOMUS_READ_CALLS (баланс личного кабинета GET /v2/user-api/balance, баланс бизнес-кабинета POST /v1/balance,
история личного кабинета POST /v2/user-api/transaction/list). Любой другой путь — ValueError до подписи и отправки;
за редиректами оба не идут (3xx — ошибка), так что путь и хост не может подменить и сервер.
Ключей «только чтение» у Cryptomus нет вовсе: любой его ключ даёт двигать деньги (см. CRYPTOMUS_KEY_RIGHTS).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp

import jsonstore

KEYS_PATH = os.path.join("data", "keys.json")
BYBIT_BASE = "https://api.bybit.com"
MEXC_BASE = "https://api.mexc.com"
HTX_BASE = "https://api.htx.com"
KUCOIN_BASE = "https://api.kucoin.com"
BINGX_BASE = "https://open-api.bingx.com"
CRYPTOMUS_BASE = "https://api.cryptomus.com"
CONNECTABLE = ("bybit", "mexc", "htx", "kucoin", "bingx", "cryptomus")   # биржи, для которых уже есть подпись запросов
PASSPHRASE_REQUIRED = ("kucoin",)      # ключ биржи — 3 шага (key/secret/passphrase)
NO_READONLY_KEYS = ("cryptomus",)      # биржи без ключей «только чтение»: любой ключ даёт двигать деньги
ONBOARDABLE = tuple(dict.fromkeys(CONNECTABLE + PASSPHRASE_REQUIRED))   # биржи, для которых бот предлагает подключить ключ кнопками


SECRET_FIELDS = ("key", "secret", "passphrase")
DPAPI_PREFIX = "dpapi:"


def _dpapi(data, encrypt):
    """Windows DPAPI (CryptProtectData/CryptUnprotectData через ctypes): шифр привязан к учётной записи Windows
    владельца — файл ключей бесполезен на другом ПК или под другим пользователем. Вне Windows — None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1 — без окон и запросов
        raise OSError(ctypes.GetLastError(), "DPAPI")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(value):
    """Секрет → "dpapi:<base64>" для data/keys.json (Windows); уже зашифрованный или вне Windows — как есть."""
    if not value or value.startswith(DPAPI_PREFIX):
        return value
    enc = _dpapi(value.encode("utf-8"), True)
    return value if enc is None else DPAPI_PREFIX + base64.b64encode(enc).decode("ascii")


def unprotect(value):
    """"dpapi:<base64>" → секрет; открытое значение (файл старой версии) — как есть; не расшифровывается
    (другой ПК/учётка Windows, файл испорчен) — None: ключ считается неподключённым."""
    if not value or not value.startswith(DPAPI_PREFIX):
        return value
    try:
        dec = _dpapi(base64.b64decode(value[len(DPAPI_PREFIX):], validate=True), False)
        return None if dec is None else dec.decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def encrypt_saved_keys():
    """Зашифровать открытые key/secret/passphrase в data/keys.json (файл от прошлой версии) — при старте бота.
    Возвращает, сколько полей зашифровано; вне Windows — 0."""
    data, changed = _keys_file(), 0
    for entry in data.values():
        for field in SECRET_FIELDS:
            value = entry.get(field) if isinstance(entry, dict) else None
            if value and not value.startswith(DPAPI_PREFIX):
                enc = protect(value)
                if enc != value:
                    entry[field], changed = enc, changed + 1
    if changed:
        _write_keys_file(data)
    return changed


def _keys_file():
    return jsonstore.read_dict(KEYS_PATH, strict=True)


def _write_keys_file(data):
    jsonstore.write_dict(KEYS_PATH, data)


def in_env(exchange):
    """Есть ли ключ биржи в окружении (.env): {EXCHANGE}_API_KEY или {EXCHANGE}_API_SECRET."""
    ex = exchange.upper()
    return bool(os.getenv(f"{ex}_API_KEY") or os.getenv(f"{ex}_API_SECRET"))


def keys(exchange):
    """(api_key, api_secret) для биржи или None, если ключей нет или ключ выключен пометкой disabled
    (файл имеет приоритет над .env)."""
    ex = exchange.lower()
    try:
        saved = _keys_file().get(ex, {})
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(saved, dict):
        return None
    if saved.get("disabled"):   # ключ удалён в боте — не подхватываем его и из .env
        return None
    if "key" in saved or "secret" in saved:
        if not all(isinstance(saved.get(k), str) for k in ("key", "secret")):
            return None
        key, secret = unprotect(saved["key"]), unprotect(saved["secret"])
    else:
        key = os.getenv(f"{exchange.upper()}_API_KEY")
        secret = os.getenv(f"{exchange.upper()}_API_SECRET")
    return (key, secret) if key and secret else None


def save_key(exchange, key, secret, passphrase=None):
    """Сохранить ключ биржи в data/keys.json (создаёт папку/файл при необходимости).

    `passphrase` — только для бирж из PASSPHRASE_REQUIRED (сейчас KuCoin). Секреты пишутся зашифрованными
    (protect: Windows DPAPI)."""
    data = _keys_file()
    entry = {"key": protect(key), "secret": protect(secret)}
    if passphrase:
        entry["passphrase"] = protect(passphrase)
    data[exchange.lower()] = entry   # новая запись целиком — снимает и пометку disabled
    _write_keys_file(data)


def passphrase(exchange):
    """Passphrase ключа биржи (сейчас только KuCoin): data/keys.json, иначе {EXCHANGE}_API_PASSPHRASE в .env."""
    ex = exchange.lower()
    try:
        saved = _keys_file().get(ex, {})
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(saved, dict):
        return None
    if saved.get("disabled"):
        return None
    if "key" in saved or "secret" in saved or "passphrase" in saved:
        value = saved.get("passphrase")
        return unprotect(value) if isinstance(value, str) else None
    return os.getenv(f"{exchange.upper()}_API_PASSPHRASE")


def delete_key(exchange):
    """Отключить ключ биржи из любого источника: убрать запись из data/keys.json, а если ключ есть
    и в .env — записать пометку disabled, которая перекрывает .env. False — ключ не был подключён."""
    ex = exchange.lower()
    if keys(ex) is None:
        return False
    data = _keys_file()
    data.pop(ex, None)
    if in_env(ex):
        data[ex] = {"disabled": True}
    _write_keys_file(data)
    return True


def set_verified(exchange, state, msg=""):
    """Запомнить результат последней проверки ключа для статуса в «🔑 Мои биржи»: `state` —
    "ok" (запрос прошёл и права подтверждены как только чтение), "error" (запрос не прошёл),
    "unknown" (запрос прошёл, но права подтвердить не удалось — как раньше, не утверждаем «только чтение») или
    "unsafe" (ключ даёт больше, чем чтение, и оставлен по решению владельца, ALLOW_UNSAFE_KEYS=1; msg — какие права).
    Ключ не подключён (запись уже удалена/помечена disabled) — писать некуда, тихо выходим."""
    ex = exchange.lower()
    data = _keys_file()
    if ex not in data or data[ex].get("disabled"):
        return
    data[ex]["verified"] = state
    data[ex]["verified_msg"] = msg if state in ("error", "unsafe") else ""
    _write_keys_file(data)


def verify_status(exchange):
    """Статус последней проверки ключа биржи для «🔑 Мои биржи»: ("none", "") — ключ не подключён,
    ("unknown", "") — подключён, но права не подтверждены (ещё не проверялся или сама биржа не
    вернула права ключа), ("ok", "") — подтверждён только чтение, ("error", msg) — последняя проверка
    не прошла (неверный ключ, сеть и т.п.), ("unsafe", права) — ключ даёт больше, чем чтение, оставлен владельцем."""
    ex = exchange.lower()
    if keys(ex) is None:
        return "none", ""
    saved = _keys_file().get(ex, {})
    state = saved.get("verified", "unknown")
    return state, saved.get("verified_msg", "") if state in ("error", "unsafe") else ""


def mask(key):
    """Ключ обратно не показываем — только последние 4 символа."""
    return "•••" + key[-4:] if key and len(key) > 4 else "••••"


def bybit_headers(api_key, api_secret, params=None, recv_window="5000", timestamp=None):
    """Заголовки X-BAPI-* для приватного GET Bybit v5: подпись = HMAC_SHA256(secret, ts+key+recv_window+query)."""
    ts = timestamp or str(int(time.time() * 1000))
    query = urlencode(params or {})
    sign_str = ts + api_key + recv_window + query
    sign = hmac.new(api_secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
    }


def mexc_signed_params(api_secret, params=None, recv_window="5000", timestamp=None):
    """Параметры запроса + timestamp/signature для приватного MEXC v3: подпись = HMAC_SHA256(secret, query)."""
    p = dict(params or {})
    p.setdefault("recvWindow", recv_window)
    p["timestamp"] = timestamp or str(int(time.time() * 1000))
    p["signature"] = hmac.new(api_secret.encode(), urlencode(p).encode(), hashlib.sha256).hexdigest()
    return p


async def _get_json(s, url, headers):
    async with s.get(url, headers=headers, allow_redirects=False) as r:
        if 300 <= getattr(r, "status", 200) < 400:
            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status,
                                             message="редирект запрещён")
        r.raise_for_status()
        return await r.json(content_type=None)


async def _json_no_redirect(req):
    """JSON ответа на запрос, отправленный с allow_redirects=False (BingX, Cryptomus). 3xx — ошибка: за редиректом
    не идём, иначе путь и хост выбрал бы сервер, а не список чтения, и туда же ушли бы заголовки с ключом и подписью."""
    async with req as r:
        if 300 <= r.status < 400:
            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message="редирект запрещён")
        r.raise_for_status()
        return await r.json(content_type=None)


def api_error_text(e):
    """Текст ошибки запроса к бирже для пользователя и лога — без URL и параметров запроса: str() ошибки
    aiohttp содержит полный URL, а в query HTX лежат AccessKeyId и Signature, у MEXC — signature."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "таймаут запроса"
    if isinstance(e, aiohttp.ClientResponseError):   # и ContentTypeError: только код и причина
        return f"HTTP {e.status}: {e.message}"[:120] if e.message else f"HTTP {e.status}"
    if isinstance(e, aiohttp.ClientConnectorError):   # только хост, без пути и query
        return f"нет соединения с {e.host}"
    return type(e).__name__   # прочие ошибки: str(e) может содержать URL


# Подписанные GET Bybit/MEXC/HTX/KuCoin — только эти пути чтения (как BINGX_READ_PATHS): балансы, права ключа, история
# депозитов/выводов и спот-сделок, сети монет (netstatus). Другой путь (ордера, позиции, вывод, переводы) — ValueError
# до подписи и отправки: ключ владельца может давать больше, чем чтение.
BYBIT_READ_PATHS = frozenset({
    "/v5/user/query-api",                               # права ключа
    "/v5/account/wallet-balance",                       # баланс Unified Trading Account
    "/v5/asset/transfer/query-account-coins-balance",   # баланс Funding wallet (чтение, не перевод)
    "/v5/asset/coin/query-info",                        # сети монеты: вывод/депозит открыт (netstatus)
})
MEXC_READ_PATHS = frozenset({
    "/api/v3/account",                    # баланс и права ключа
    "/api/v3/capital/deposit/hisrec",     # история депозитов
    "/api/v3/capital/withdraw/history",   # история выводов
    "/api/v3/myTrades",                   # спот-сделки (фолбэк истории)
    "/api/v3/capital/config/getall",      # сети монет (netstatus)
})
HTX_READ_PATHS = frozenset({
    "/v2/user/uid",                  # uid владельца ключа (нужен для прав ключа)
    "/v2/user/api-key",              # права ключа
    "/v1/account/accounts",          # проверка ключа
    "/v1/query/deposit-withdraw",    # история депозитов и выводов
})
KUCOIN_READ_PATHS = frozenset({
    "/api/v1/user/api-key",   # права ключа
    "/api/v1/accounts",       # проверка ключа
    "/api/v1/deposits",       # история депозитов
    "/api/v1/withdrawals",    # история выводов (GET — чтение)
    "/api/v1/fills",          # спот-сделки (фолбэк истории)
})


def _read_path(venue, allowed, path):
    """Путь подписанного GET — только из списка чтения этой биржи, иначе ValueError (до подписи и отправки)."""
    if path not in allowed:
        raise ValueError(f"{venue}: путь {path} не входит в список чтения")


async def bybit_get(s, api_key, api_secret, path, params=None):
    """Подписанный GET к приватному Bybit v5 — только пути из BYBIT_READ_PATHS (например '/v5/account/wallet-balance')."""
    _read_path("Bybit", BYBIT_READ_PATHS, path)
    params = params or {}
    url = f"{BYBIT_BASE}{path}" + (f"?{urlencode(params)}" if params else "")
    return await _get_json(s, url, bybit_headers(api_key, api_secret, params=params))


async def mexc_get(s, api_key, api_secret, path, params=None):
    """Подписанный GET к приватному MEXC v3 — только пути из MEXC_READ_PATHS (например '/api/v3/account')."""
    _read_path("MEXC", MEXC_READ_PATHS, path)
    signed = mexc_signed_params(api_secret, params)
    url = f"{MEXC_BASE}{path}?{urlencode(signed)}"
    return await _get_json(s, url, {"X-MEXC-APIKEY": api_key})


BYBIT_POST_PATHS = frozenset({"/v5/p2p/order/simplifyList"})   # POST к Bybit — только чтение истории P2P-ордеров


def bybit_post_headers(api_key, api_secret, payload_str, recv_window, timestamp):
    """Заголовки подписанного POST Bybit v5 — чистая функция: подпись = HMAC_SHA256(secret, ts+key+recv_window+тело),
    тело — ровно та строка, что уйдёт в запрос."""
    sign = hmac.new(api_secret.encode(), (timestamp + api_key + recv_window + payload_str).encode(),
                    hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json",
    }


async def bybit_post(s, api_key, api_secret, path, body=None, recv_window="5000", timestamp=None):
    """Подписанный POST к приватному Bybit v5 — только пути из BYBIT_POST_PATHS (иначе ValueError до подписи и
    отправки). Формула подписи та же, что у GET, только query заменяется на JSON-тело. За редиректом не идём
    (3xx — ошибка): иначе путь и хост выбрал бы сервер, а заголовки с ключом и подписью ушли бы туда же."""
    if path not in BYBIT_POST_PATHS:
        raise ValueError(f"Bybit: POST {path} не входит в список чтения")
    ts = timestamp or str(int(time.time() * 1000))
    payload = json.dumps(body or {}, separators=(",", ":"))
    headers = bybit_post_headers(api_key, api_secret, payload, recv_window, ts)
    return await _json_no_redirect(s.post(f"{BYBIT_BASE}{path}", headers=headers, data=payload, allow_redirects=False))


def htx_signed_params(api_key, api_secret, method, path, params=None, timestamp=None, host="api.htx.com"):
    """Query-параметры для приватного HTX v1 (Signature Version 2): подпись = Base64(HMAC_SHA256(secret,
    METHOD+"\\n"+host+"\\n"+path+"\\n"+отсортированная_query))."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    base = {"AccessKeyId": api_key, "SignatureMethod": "HmacSHA256", "SignatureVersion": "2", "Timestamp": ts}
    base.update(params or {})
    query = urlencode(sorted(base.items()))
    payload = "\n".join([method, host, path, query])
    digest = hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).digest()
    base["Signature"] = base64.b64encode(digest).decode()
    return base


async def htx_get(s, api_key, api_secret, path, params=None):
    """Подписанный GET к приватному HTX — только пути из HTX_READ_PATHS (например '/v1/account/accounts')."""
    _read_path("HTX", HTX_READ_PATHS, path)
    signed = htx_signed_params(api_key, api_secret, "GET", path, params)
    url = f"{HTX_BASE}{path}?{urlencode(sorted(signed.items()))}"
    return await _get_json(s, url, {})


def kucoin_headers(api_key, api_secret, passphrase_value, method, path_with_query, body="", timestamp=None):
    """Заголовки KC-API-* для приватного KuCoin v2: подпись и passphrase — Base64(HMAC_SHA256(secret, ...))."""
    ts = timestamp or str(int(time.time() * 1000))
    sign = base64.b64encode(
        hmac.new(api_secret.encode(), (ts + method + path_with_query + body).encode(), hashlib.sha256).digest()
    ).decode()
    signed_passphrase = base64.b64encode(
        hmac.new(api_secret.encode(), passphrase_value.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY": api_key,
        "KC-API-SIGN": sign,
        "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": signed_passphrase,
        "KC-API-KEY-VERSION": "2",
    }


async def kucoin_get(s, api_key, api_secret, passphrase_value, path, params=None):
    """Подписанный GET к приватному KuCoin — только пути из KUCOIN_READ_PATHS (например '/api/v1/accounts')."""
    _read_path("KuCoin", KUCOIN_READ_PATHS, path)
    full_path = path + (f"?{urlencode(params)}" if params else "")
    headers = kucoin_headers(api_key, api_secret, passphrase_value, "GET", full_path)
    return await _get_json(s, f"{KUCOIN_BASE}{full_path}", headers)


# BingX: ключ владельца даёт торговлю — поэтому только GET и только эти пути; POST-запросов к BingX в коде нет
BINGX_READ_PATHS = frozenset({
    "/openApi/spot/v1/account/balance",           # спот: data.balances[{asset, free, locked}]
    "/openApi/fund/v1/account/balance",           # Fund-счёт (с 2025-10 отдельно от спота), та же форма
    "/openApi/v1/account/apiPermissions",         # права ключа (две документированные формы, см. bingx_key_safety)
    "/openApi/api/v3/capital/deposit/hisrec",     # история депозитов — голый массив
    "/openApi/api/v3/capital/withdraw/history",   # история выводов — голый массив
})
BINGX_ERRORS = {   # код ошибки BingX -> подсказка владельцу (без ключа и URL)
    100001: "неверная подпись — проверь secret",
    100004: "у ключа нет нужного права — включи «Read»",
    100410: "превышен лимит запросов BingX, повтори позже",
    100412: "запрос без подписи",
    100413: "BingX не нашёл такой API key — проверь ключ",
    100419: "IP этого ПК не в белом списке ключа",
    100421: "часы ПК расходятся с BingX — синхронизируй время Windows",
    100500: "BingX занят, повтори позже",
}


def bingx_signed_query(api_secret, params=None, timestamp=None, recv_window="5000"):
    """Query приватного GET BingX вместе с подписью: параметры (с timestamp в мс и recvWindow ≤ 5000) по ключу
    в порядке ASCII, сырые значения key=value&...; signature = hex(HMAC_SHA256(secret, эта строка)) — последним
    параметром. recv_window=None — без recvWindow (как в официальном примере подписи)."""
    p = {k: str(v) for k, v in (params or {}).items()}
    if recv_window is not None:
        p.setdefault("recvWindow", str(recv_window))
    p["timestamp"] = str(timestamp or int(time.time() * 1000))
    query = "&".join(f"{k}={p[k]}" for k in sorted(p))
    sign = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={sign}"


async def bingx_get(s, api_key, api_secret, path, params=None):
    """Подписанный GET к приватному BingX. Путь не из BINGX_READ_PATHS — ValueError до подписи и отправки;
    за редиректом не идём (3xx — ошибка), чтобы сервер не увёл запрос на другой путь или хост."""
    if path not in BINGX_READ_PATHS:
        raise ValueError(f"BingX: путь {path} не входит в список чтения")
    url = f"{BINGX_BASE}{path}?{bingx_signed_query(api_secret, params)}"
    return await _json_no_redirect(s.get(url, headers={"X-BX-APIKEY": api_key}, allow_redirects=False))


def _scrub(text, *secrets, limit=160):
    """Текст ошибки биржи для пользователя: ключ/секрет/ID, если биржа их процитировала, — «•••»; не длиннее limit."""
    for sec in secrets:
        if sec and len(sec) >= 4:
            text = text.replace(sec, "•••")
    return text[:limit]


def bingx_error_text(j, *secrets):
    """Короткая подсказка по коду ошибки BingX; незнакомый код — код и текст биржи (без ключа и секрета)."""
    j = j if isinstance(j, dict) else {}
    code = j.get("code")
    try:
        code = int(code)
    except (TypeError, ValueError):
        pass
    if code in BINGX_ERRORS:
        return f"{BINGX_ERRORS[code]} (код {code})"
    msg = str(j.get("msg") or "")
    return _scrub(f"ошибка BingX (код {code})" + (f": {msg}" if msg else ""), *secrets)


BINGX_READ_CODE = 2   # apiPermissions (docs-v3): 2 — чтение, остальные коды — права сверх чтения
BINGX_PERM_CODES = {1: "торговля спот", 3: "фьючерсы", 4: "переводы между своими счетами",
                    5: "вывод и переводы другим пользователям BingX", 7: "переводы между субаккаунтами"}
BINGX_PERM_FLAGS = {   # apiPermissions (api-ai-skills): флаг enable*/permits* -> что даёт ключ сверх чтения
    "enableSpotAndMarginTrading": "торговля спот", "enableFutures": "фьючерсы", "enableVanillaOptions": "опционы",
    "permitsUniversalTransfer": "переводы между своими счетами",
    "enableWithdrawals": "вывод и переводы другим пользователям BingX",
    "enableInternalTransfer": "переводы между субаккаунтами"}
# без любого из этих флагов «только чтение» не подтвердить: у api-ai-skills есть и урезанная форма ответа (без
# enableWithdrawals); enableVanillaOptions есть не во всех формах — его отсутствие не мешает, «истина» — право сверх чтения
BINGX_FLAGS_REQUIRED = ("enableReading", "enableSpotAndMarginTrading", "enableFutures", "permitsUniversalTransfer",
                        "enableWithdrawals", "enableInternalTransfer")


def _bingx_flag(value):
    """Флаг из ответа BingX: True/False (bool, 1/0, "true"/"false"/"1"/"0" в любом регистре), иначе None — не распознан."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "false", "0"):
        return value.strip().lower() in ("true", "1")
    return None


def bingx_key_safety(data):
    """Права ключа BingX по ответу /openApi/v1/account/apiPermissions (уже без обёртки {code, data}): (safe, detail),
    как key_permissions.

    Официальные источники расходятся — принимаем обе формы: docs-v3 {permissions: [коды], ipAddresses: [...]}
    (2 — чтение, 1 спот, 3 фьючерсы, 4 переводы между своими счетами, 5 вывод и переводы другим пользователям,
    7 переводы между субаккаунтами; незнакомый код — тоже сверх чтения) и api-ai-skills {enableReading,
    enableSpotAndMarginTrading, enableWithdrawals, ..., ipRestrict}. safe=True — только когда чтение подтверждено
    (код 2 и других нет; enableReading и все флаги из BINGX_FLAGS_REQUIRED распознаны, сверх чтения — ни одного).
    Любое право сверх чтения — False, даже если остальное не распознано. Иначе (непонятная форма, неполный набор
    флагов, пустой список прав) — (None, "")."""
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        return None, ""
    missing = []
    if "permissions" in data:
        raw = data["permissions"]
        raw = raw.split(",") if isinstance(raw, str) else raw
        if not isinstance(raw, list):
            return None, ""
        try:
            codes = {int(str(c).strip()) for c in raw if str(c).strip()}
        except ValueError:
            return None, ""
        bad = [BINGX_PERM_CODES.get(c, f"право {c}") for c in sorted(codes - {BINGX_READ_CODE})]
        # вдруг в ответе рядом с кодами ещё и флаги enable*/permits* — их тоже учитываем, а не только коды
        flags = {k: _bingx_flag(v) for k, v in data.items()
                 if str(k).lower().startswith(("enable", "permit")) and k != "enableReading"}
        bad += [BINGX_PERM_FLAGS.get(k, f"право {k}") for k, v in flags.items() if v]
        confirmed = codes == {BINGX_READ_CODE} and None not in flags.values()
        no_ip = "ipAddresses" in data and not data["ipAddresses"]
    elif any(str(k).lower().startswith(("enable", "permit")) for k in data):
        rights = {k: _bingx_flag(v) for k, v in data.items() if str(k).lower().startswith(("enable", "permit"))}
        # известные флаги в порядке BINGX_PERM_FLAGS, затем незнакомые enable*/permits* — тоже права сверх чтения
        order = [*BINGX_PERM_FLAGS, *sorted(k for k in rights if k not in BINGX_PERM_FLAGS and k != "enableReading")]
        bad = [BINGX_PERM_FLAGS.get(k, f"право {k}") for k in order if rights.get(k)]
        missing = [BINGX_PERM_FLAGS[k] for k in BINGX_FLAGS_REQUIRED
                   if k in BINGX_PERM_FLAGS and rights.get(k) is None]
        confirmed = (rights.get("enableReading") is True and None not in rights.values()
                     and all(k in rights for k in BINGX_FLAGS_REQUIRED))
        no_ip = _bingx_flag(data.get("ipRestrict")) is False
    else:
        return None, ""
    if bad:
        return False, (", ".join(dict.fromkeys(bad)) + ("; без привязки к IP" if no_ip else "")
                       + (f"; не проверено: {', '.join(missing)}" if missing else ""))
    return (True, "") if confirmed else (None, "")


# Cryptomus: у любого ключа есть права двигать деньги — поэтому только эти пары (метод, путь)
CRYPTOMUS_READ_CALLS = frozenset({
    ("GET", "/v2/user-api/balance"),              # личный кабинет (userId): result.balances[]
    ("POST", "/v1/balance"),                      # бизнес-кабинет (merchant): result[0].balance.merchant[]/.user[]
    ("POST", "/v2/user-api/transaction/list"),    # история личного кабинета, первая страница
})
CRYPTOMUS_MODES = ("user", "merchant")            # личный кабинет (User API) и бизнес (Merchant API)
CRYPTOMUS_BALANCE = {"user": ("GET", "/v2/user-api/balance", None), "merchant": ("POST", "/v1/balance", {})}
CRYPTOMUS_KEY_RIGHTS = {   # что даёт ключ Cryptomus сверх чтения — ключей «только чтение» у Cryptomus нет
    "user": "у Cryptomus нет ключей только для чтения: ключ личного кабинета даёт конвертации и ордера "
            "на бирже Cryptomus, отмену ордеров, покупку AML-пакетов",
    "merchant": "у Cryptomus нет ключей только для чтения: ключ бизнес-кабинета даёт возвраты платежей "
                "на любой адрес, выплаты и переводы между кошельками",
    None: "у Cryptomus нет ключей только для чтения: ключ личного кабинета даёт конвертации и ордера на бирже "
          "Cryptomus, отмену ордеров, покупку AML-пакетов; ключ бизнес-кабинета — возвраты платежей на любой адрес, "
          "выплаты и переводы между кошельками",   # кабинет ещё не определён
}
CRYPTOMUS_CODES = {"GRAM": "TON"}   # Toncoin у Cryptomus — код GRAM
_CRYPTOMUS_MODE = {}   # {ID: "user"/"merchant"} — какой кабинет принял этот ID (в памяти процесса)


def cryptomus_sign(body, api_key):
    """Подпись Cryptomus (заголовок sign): md5_hex(base64(тело запроса ровно как отправлено) + API key).
    Без тела — base64("") = "", то есть md5(API key)."""
    return hashlib.md5(base64.b64encode(body.encode("utf-8")) + api_key.encode("utf-8")).hexdigest()


async def cryptomus_call(s, uid, api_key, method, path, body=None, mode="user"):
    """Подписанный запрос к Cryptomus. Пара (метод, путь) не из CRYPTOMUS_READ_CALLS — ValueError до подписи
    и отправки. mode="user" — заголовок userId (личный кабинет), "merchant" — merchant (бизнес). Тело — компактный
    JSON, подписывается ровно отправляемая строка. За редиректом не идём (3xx — ошибка): подпись Cryptomus не привязана
    к пути, и переадресованный запрос ушёл бы с валидной подписью туда, куда укажет сервер. Заголовки, подпись и тело
    не логируются."""
    method = str(method).upper()
    if (method, path) not in CRYPTOMUS_READ_CALLS:
        raise ValueError(f"Cryptomus: {method} {path} не входит в список чтения")
    if mode not in CRYPTOMUS_MODES:
        raise ValueError(f"Cryptomus: неизвестный режим {mode}")
    payload = "" if body is None else json.dumps(body, separators=(",", ":"))
    headers = {"userId" if mode == "user" else "merchant": uid, "sign": cryptomus_sign(payload, api_key),
               "Content-Type": "application/json"}
    url = f"{CRYPTOMUS_BASE}{path}"
    req = (s.get(url, headers=headers, allow_redirects=False) if method == "GET"
           else s.post(url, headers=headers, data=payload, allow_redirects=False))
    return await _json_no_redirect(req)


def _cryptomus_ok(j):
    """Успешный ответ Cryptomus: есть result, а state (у v1 и не-биржевых v2) — 0 или его нет вовсе."""
    return isinstance(j, dict) and "result" in j and j.get("state", 0) in (0, "0")


def _cryptomus_err(j):
    """Текст ошибки из ответа Cryptomus: message, иначе первая ошибка из errors, иначе state."""
    if not isinstance(j, dict):
        return "неожиданный ответ"
    if j.get("message"):
        return str(j["message"])
    errors = j.get("errors")
    if isinstance(errors, dict) and errors:
        first = next(iter(errors.values()))
        return str(first[0] if isinstance(first, list) and first else first)
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message"))
    return f"state {j.get('state')}"


async def cryptomus_detect(s, uid, api_key):
    """Какой кабинет Cryptomus принимает ID и ключ: (режим, ответ баланса). Сначала личный (userId, GET
    /v2/user-api/balance), не вышло — бизнес (merchant, POST /v1/balance); подошедший режим запоминается по ID
    и дальше пробуется первым. Не подошёл ни один — (None, текст последней ошибки)."""
    cached = _CRYPTOMUS_MODE.get(uid)
    err = ""
    for mode in sorted(CRYPTOMUS_MODES, key=lambda m: m != cached):
        method, path, body = CRYPTOMUS_BALANCE[mode]
        try:
            j = await cryptomus_call(s, uid, api_key, method, path, body, mode=mode)
        except Exception as e:
            err = api_error_text(e)
            continue
        if _cryptomus_ok(j):
            _CRYPTOMUS_MODE[uid] = mode
            return mode, j
        err = _cryptomus_err(j)
    return None, err


KUCOIN_READONLY_PERMS = {"General"}    # остальные значения permission у KuCoin дают торговлю/вывод/переводы


async def key_permissions(s, exchange):
    """Права сохранённого ключа биржи по данным самого API: (safe, detail).

    safe=True — биржа подтвердила: только чтение; safe=False — ключ даёт торговать или выводить,
    detail — что именно нашли; safe=None — права проверить не удалось (нет ключа, ошибка сети/API/формата).
    Биржи из NO_READONLY_KEYS (Cryptomus) — всегда False без запроса: read-only ключей у них нет вовсе, и сбой сети
    не должен превращаться в «проверить не удалось», которое при старте считается безопасным."""
    ex = exchange.lower()
    pair = keys(ex)
    if not pair:
        return None, ""
    api_key, api_secret = pair
    try:
        if ex == "bybit":
            j = await bybit_get(s, api_key, api_secret, "/v5/user/query-api")
            if j.get("retCode") != 0:
                return None, ""
            result = j.get("result", {})
            if result.get("readOnly") == 1:
                return True, ""
            extra = [name for name, perms in (result.get("permissions") or {}).items() if perms]
            return False, "торговля/переводы (" + ", ".join(extra) + ")" if extra else "ключ не read-only"
        elif ex == "mexc":
            j = await mexc_get(s, api_key, api_secret, "/api/v3/account")
            if "canTrade" not in j and "canWithdraw" not in j:   # ответ с ошибкой — прав в нём нет
                return None, ""
            bad = [name for name, granted in (("торговля", j.get("canTrade")), ("вывод", j.get("canWithdraw"))) if granted]
            return not bad, ", ".join(bad)
        elif ex == "htx":
            # /v2/user/api-key требует обязательный uid владельца ключа — сначала узнаём его
            u = await htx_get(s, api_key, api_secret, "/v2/user/uid")
            if u.get("code") != 200 or not u.get("data"):
                return None, ""
            j = await htx_get(s, api_key, api_secret, "/v2/user/api-key", {"uid": u["data"]})
            if j.get("code") != 200:
                return None, ""
            entry = next((e for e in j.get("data") or [] if e.get("accessKey") == api_key), None)
            if not entry:
                return None, ""
            perms = {p.strip().lower() for p in (entry.get("permission") or "").split(",")}
            bad = sorted(perms & {"trade", "withdraw"})
            return not bad, ", ".join(bad)
        elif ex == "kucoin":
            pp = passphrase(ex)
            if not pp:
                return None, ""
            j = await kucoin_get(s, api_key, api_secret, pp, "/api/v1/user/api-key")
            if j.get("code") != "200000":
                return None, ""
            perms = {p.strip() for p in (j.get("data", {}).get("permission") or "").split(",") if p.strip()}
            bad = sorted(perms - KUCOIN_READONLY_PERMS)
            return not bad, ", ".join(bad)
        elif ex == "bingx":
            j = await bingx_get(s, api_key, api_secret, "/openApi/v1/account/apiPermissions")
            if isinstance(j, dict) and "code" in j:   # обёртка {code, msg, data} (api-ai-skills); ошибка — code != 0
                if j["code"] not in (0, "0"):
                    return None, ""
                j = j.get("data")
            return bingx_key_safety(j)   # без обёртки — {apiKey, permissions, ipAddresses, note}, как в docs-v3
        elif ex == "cryptomus":
            # прав ключа Cryptomus не отдаёт, read-only ключей нет вовсе — ответ известен без запроса; кабинет (что
            # именно даёт ключ) знаем, если verify/баланс его уже определили. key = ID кабинета.
            return False, CRYPTOMUS_KEY_RIGHTS[_CRYPTOMUS_MODE.get(api_key)]
    except Exception:
        return None, ""
    return None, ""


async def api_permissions(s, exchange):
    """Проверка прав при старте бота: (safe, detail), как key_permissions, но «не удалось проверить» = safe.

    safe=False — ключ даёт торговать или выводить (не read-only), detail — что именно нашли; у Cryptomus — всегда.
    safe=True — либо ключ read-only, либо права проверить не удалось (не блокируем по недоступности API)."""
    safe, detail = await key_permissions(s, exchange)
    return safe is not False, detail


BALANCE_COINS = ("USDT", "USDC", "BTC", "ETH", "TON")   # монеты, которые показывает /balance


async def bybit_balances(s, api_key, api_secret):
    """Балансы монет на Bybit: Unified Trading Account + Funding wallet (складываются по монете)."""
    totals = {}
    uni = await bybit_get(s, api_key, api_secret, "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if uni.get("retCode") == 0:
        for acc in uni.get("result", {}).get("list", []):
            for c in acc.get("coin", []):
                amt = float(c.get("walletBalance") or 0)
                if amt:
                    totals[c["coin"]] = totals.get(c["coin"], 0) + amt
    fund = await bybit_get(s, api_key, api_secret, "/v5/asset/transfer/query-account-coins-balance",
                            {"accountType": "FUND"})
    if fund.get("retCode") == 0:
        for c in fund.get("result", {}).get("balance", []):
            amt = float(c.get("walletBalance") or 0)
            if amt:
                totals[c["coin"]] = totals.get(c["coin"], 0) + amt
    return totals


async def mexc_balances(s, api_key, api_secret):
    """Балансы монет на споте MEXC."""
    j = await mexc_get(s, api_key, api_secret, "/api/v3/account")
    totals = {}
    for b in j.get("balances", []):
        amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
        if amt:
            totals[b["asset"]] = totals.get(b["asset"], 0) + amt
    return totals


async def bingx_balances(s, api_key, api_secret):
    """Балансы монет на BingX: спот + Fund-счёт (с 2025-10 BingX отдаёт их раздельно), free+locked по монете.
    Один из счетов не ответил — считаем по второму."""
    totals = {}
    for path in ("/openApi/spot/v1/account/balance", "/openApi/fund/v1/account/balance"):
        try:
            j = await bingx_get(s, api_key, api_secret, path)
        except Exception:
            continue
        if not isinstance(j, dict) or j.get("code") != 0:
            continue
        data = j.get("data")
        rows = data.get("balances") if isinstance(data, dict) else data   # форма Fund-ответа подтверждена не до конца
        for b in rows if isinstance(rows, list) else []:
            try:
                amt = float(b.get("free") or 0) + float(b.get("locked") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if amt and b.get("asset"):
                totals[b["asset"]] = totals.get(b["asset"], 0) + amt
    return totals


def _cryptomus_rows(result):
    """Строки кошельков из result баланса Cryptomus: личный кабинет — {balances: [...]}, бизнес — [{balance:
    {merchant: [...], user: [...]}}] (бизнес- и личный кошелёк)."""
    if isinstance(result, list):
        return [row for part in result for row in _cryptomus_rows(part)]
    if not isinstance(result, dict):
        return []
    if "balances" in result:
        return result["balances"] if isinstance(result["balances"], list) else []
    bal = result.get("balance")
    if isinstance(bal, dict):
        return [row for part in ("merchant", "user") for row in bal.get(part) or [] if isinstance(row, dict)]
    return []


async def cryptomus_balances(s, uid, api_key):
    """Балансы Cryptomus (key = ID кабинета, secret = API key): ключ личного кабинета — личный кошелёк, ключ
    бизнес-кабинета — бизнес- и личный кошельки; GRAM (так Cryptomus зовёт Toncoin) -> TON. Ключ не принят — {}."""
    mode, j = await cryptomus_detect(s, uid, api_key)
    totals = {}
    for b in _cryptomus_rows(j.get("result")) if mode else []:
        code = str(b.get("currency_code") or "").upper()
        code = CRYPTOMUS_CODES.get(code, code)
        try:
            amt = float(b.get("balance") or 0)
        except (TypeError, ValueError):
            continue
        if amt and code:
            totals[code] = totals.get(code, 0) + amt
    return totals


# биржи, для которых уже есть /balance; у всех (s, key, secret) — у Cryptomus key = ID кабинета, secret = API key
BALANCE_FETCHERS = {"bybit": bybit_balances, "mexc": mexc_balances, "bingx": bingx_balances,
                    "cryptomus": cryptomus_balances}


async def portfolio(s):
    """{биржа: {монета: количество}} по всем подключённым биржам с реализованным чтением баланса.

    Оставляет только BALANCE_COINS; ошибка запроса или отсутствие ключа — биржа просто пропускается."""
    out = {}
    for ex, fetch in BALANCE_FETCHERS.items():
        pair = keys(ex)
        if not pair:
            continue
        try:
            bal = await fetch(s, *pair)
        except Exception:
            continue
        bal = {c: amt for c, amt in bal.items() if c in BALANCE_COINS and amt}
        if bal:
            out[ex] = bal
    return out


async def verify(s, exchange):
    """Проверить сохранённый ключ биржи запросом баланса: (ok, сообщение для пользователя)."""
    ex = exchange.lower()
    pair = keys(ex)
    if not pair:
        return False, "ключ не сохранён"
    api_key, api_secret = pair
    try:
        if ex == "bybit":
            j = await bybit_get(s, api_key, api_secret, "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            if j.get("retCode") != 0:
                return False, j.get("retMsg", "ошибка Bybit")
        elif ex == "mexc":
            j = await mexc_get(s, api_key, api_secret, "/api/v3/account")
            if "balances" not in j:
                return False, j.get("msg", "ошибка MEXC")
        elif ex == "htx":
            j = await htx_get(s, api_key, api_secret, "/v1/account/accounts")
            if j.get("status") != "ok":
                return False, j.get("err-msg", "ошибка HTX")
        elif ex == "kucoin":
            pp = passphrase(ex)
            if not pp:
                return False, "не сохранён passphrase"
            j = await kucoin_get(s, api_key, api_secret, pp, "/api/v1/accounts")
            if j.get("code") != "200000":
                return False, j.get("msg", "ошибка KuCoin")
        elif ex == "bingx":
            j = await bingx_get(s, api_key, api_secret, "/openApi/spot/v1/account/balance")
            if not isinstance(j, dict) or j.get("code") != 0:
                return False, bingx_error_text(j, api_key, api_secret)
        elif ex == "cryptomus":
            mode, err = await cryptomus_detect(s, api_key, api_secret)   # key = ID кабинета, secret = API key
            if not mode:
                return False, _scrub(f"ID и ключ не подошли ни к личному, ни к бизнес-кабинету Cryptomus ({err})",
                                     api_key, api_secret)
            place = "личный кабинет" if mode == "user" else "бизнес-кабинет"
            return True, f"ключ рабочий ({place}), бот делает только запросы на чтение"
        else:
            return False, f"{exchange}: подпись запросов пока не реализована"
    except Exception as e:
        return False, api_error_text(e)
    return True, "ключ рабочий, доступ только для чтения"


def _hist_ts(v, tz=timezone.utc):
    """Время записи истории аккаунта: epoch-мс (большинство бирж), "YYYY-MM-DD HH:MM:SS" (MEXC-выводы, Cryptomus;
    зона не указана — `tz`, по умолчанию UTC) или ISO-8601 со смещением ("2023-12-14T04:05:02.000+08:00", выводы
    BingX). Не разобрали — 0.0."""
    try:
        return int(v) / 1000
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).timestamp()


def _hist_item(kind, asset, amount, ts, tz=timezone.utc):
    try:
        return {"kind": kind, "asset": asset, "amount": float(amount or 0), "ts": _hist_ts(ts, tz)}
    except (TypeError, ValueError):
        return None


def _merge_hist(*sources, limit=20):
    """Объединяет несколько списков истории в одну ленту по времени (новые сверху), обрезая до limit.
    Источник может быть None (эндпоинт недоступен/ошибка) — он просто не участвует. Записей нет ни в
    одном: [] — ответили все источники (история действительно пуста), None — хотя бы один не ответил:
    сбой не должен выглядеть как «история пуста» (см. Bot.check_accounts)."""
    items = [it for src in sources if src for it in src]
    if not items:
        return None if any(src is None for src in sources) else []
    items.sort(key=lambda it: it["ts"], reverse=True)
    return items[:limit]


# Статусы исполненных депозитов/выводов (бот пишет «пришёл депозит»/«исполнен вывод» — операция в пути, отклонённая
# или отменённая в ленту не попадает). Запись истории — без статуса, а ключ отсева повторов (bot.hist_key) — состав и
# время создания: при переходе «в пути» → «исполнена» запись впервые появится в ленте — ровно одно уведомление.
# MEXC v3: депозит 5 SUCCESS, 12 COMPLETED (4 PENDING, 6 AUDITING, 7 REJECTED, 8 REFUND, 9 PRE_SUCCESS, 10 INVALID…);
# вывод 7 SUCCESS (1–6 и 10 — в пути/ручная проверка, 8 FAILED, 9 CANCEL).
MEXC_DONE = {"deposit": ("5", "12"), "withdraw": ("7",)}
# HTX /v1/query/deposit-withdraw, поле state: депозит confirmed/safe (confirming — в пути, orphan/unknown — не зачтён);
# вывод confirmed (submitted, reexamine, pass, pre-transfer, wallet-transfer — в пути; canceled, reject, wallet-reject,
# confirm-error, repealed — не прошёл).
HTX_DONE = {"deposit": ("confirmed", "safe"), "withdraw": ("confirmed",)}
# KuCoin /api/v1/deposits и /api/v1/withdrawals, поле status: SUCCESS (PROCESSING, WALLET_PROCESSING, REVIEW — в пути;
# FAILURE — не прошёл).
KUCOIN_DONE = "SUCCESS"


async def mexc_history(s, api_key, api_secret, limit=20):
    """История MEXC для автожурнала (нет отдельного P2P API, как у Bybit): депозиты и выводы
    объединяются в одну ленту по времени, а не берётся первый непустой источник — иначе старый
    депозит скрывает более свежий вывод. Только исполненные (MEXC_DONE), как у BingX: запись «в обработке» не выдаём
    за исполненную, а при переходе 4 → 7 она впервые появится в ленте — одно уведомление."""
    sources = []
    for kind, path, ts_field in (("deposit", "/api/v3/capital/deposit/hisrec", "insertTime"),
                                  ("withdraw", "/api/v3/capital/withdraw/history", "applyTime")):
        try:
            j = await mexc_get(s, api_key, api_secret, path, {"limit": limit})
        except Exception:
            j = None
        if not isinstance(j, list):
            sources.append(None)   # не ответил — пустоту истории им не подтвердить
            continue
        sources.append([it for it in (_hist_item(kind, it.get("coin"), it.get("amount"), it.get(ts_field))
                                       for it in j if isinstance(it, dict)
                                       and str(it.get("status")) in MEXC_DONE[kind]) if it])
    return _merge_hist(*sources, limit=limit)


async def htx_history(s, api_key, api_secret, limit=20):
    """История HTX: депозиты и выводы (единый эндпоинт, два запроса по `type`) объединяются в одну
    ленту по времени. Только исполненные (HTX_DONE)."""
    sources = []
    for kind in ("deposit", "withdraw"):
        try:
            j = await htx_get(s, api_key, api_secret, "/v1/query/deposit-withdraw", {"type": kind, "size": limit})
        except Exception:
            j = {}
        if j.get("status") != "ok":
            sources.append(None)
            continue
        sources.append([it for it in (_hist_item(kind, it.get("currency"), it.get("amount"), it.get("created-at"))
                                       for it in j.get("data") or [] if isinstance(it, dict)
                                       and str(it.get("state")).lower() in HTX_DONE[kind]) if it])
    return _merge_hist(*sources, limit=limit)


async def kucoin_history(s, api_key, api_secret, passphrase_value, limit=20):
    """История KuCoin: депозиты и выводы объединяются в одну ленту по времени. Только исполненные (KUCOIN_DONE)."""
    sources = []
    for kind, path in (("deposit", "/api/v1/deposits"), ("withdraw", "/api/v1/withdrawals")):
        try:
            j = await kucoin_get(s, api_key, api_secret, passphrase_value, path, {"pageSize": limit})
        except Exception:
            j = {}
        if j.get("code") != "200000":
            sources.append(None)
            continue
        items = (j.get("data") or {}).get("items") or []
        sources.append([it for it in (_hist_item(kind, it.get("currency"), it.get("amount"), it.get("createdAt"))
                                       for it in items if isinstance(it, dict)
                                       and str(it.get("status")).upper() == KUCOIN_DONE) if it])
    return _merge_hist(*sources, limit=limit)


BINGX_NETWORKS = ("TRC20", "ERC20", "BEP20", "BEP2", "TON", "SOL", "SPL", "POLYGON", "ARBITRUM", "OPTIMISM",
                  "AVAXC", "BASE")   # хвосты сети в поле coin истории BingX ("USDTTRC20")


def _bingx_coin(coin, network=""):
    """Монета из записи истории BingX без хвоста сети: "USDTTRC20" -> "USDT". Хвост срезаем, если он равен полю
    network записи или известной сети и остаток — монета бота (иначе, например, "ETHW" стал бы "ETH")."""
    c, net = str(coin or "").upper(), str(network or "").upper()
    if net and c.endswith(net) and len(c) > len(net):
        return c[:-len(net)]
    for suffix in BINGX_NETWORKS:
        if c.endswith(suffix) and c[:-len(suffix)] in BALANCE_COINS:
            return c[:-len(suffix)]
    return c


async def bingx_history(s, api_key, api_secret, limit=20):
    """История BingX одной лентой по времени: завершённые депозиты (status 1 или 6; 0 — ещё в пути) и выводы
    (status 6). Вывод с transferType 2 — перевод другому пользователю BingX (innerTransfer, право Withdraw), а не на
    внешний адрес и не между своими счетами: kind "transfer_out" — деньги ушли третьему лицу.
    Ответы — голые массивы; время депозита — insertTime (мс), вывода — applyTime (ISO-8601 со смещением)."""
    sources = []
    for kind, path in (("deposit", "/openApi/api/v3/capital/deposit/hisrec"),
                       ("withdraw", "/openApi/api/v3/capital/withdraw/history")):
        try:
            j = await bingx_get(s, api_key, api_secret, path, {"limit": limit})
        except Exception:
            j = None
        if isinstance(j, dict) and j.get("code") == 0 and isinstance(j.get("data"), list):
            j = j["data"]   # на случай, если BingX завернёт ответ в {code, data}
        if not isinstance(j, list):
            sources.append(None)   # ошибка ({code, msg}) или сбой — пустоту истории не подтвердить
            continue
        items = []
        for it in j:
            if not isinstance(it, dict):
                continue
            status = str(it.get("status"))
            if kind == "deposit" and status in ("1", "6"):
                items.append(_hist_item("deposit", _bingx_coin(it.get("coin"), it.get("network")), it.get("amount"),
                                        it.get("insertTime")))
            elif kind == "withdraw" and status == "6":
                k = "transfer_out" if str(it.get("transferType")) == "2" else "withdraw"
                items.append(_hist_item(k, _bingx_coin(it.get("coin"), it.get("network")), it.get("amount"),
                                        it.get("applyTime")))
        sources.append([it for it in items if it])
    return _merge_hist(*sources, limit=limit)


MSK_TZ = timezone(timedelta(hours=3))
CRYPTOMUS_HIST_KINDS = {"payment": "deposit", "payout": "withdraw", "transfer": "transfer"}


async def cryptomus_history(s, uid, api_key, limit=20):
    """История личного кабинета Cryptomus (POST /v2/user-api/transaction/list, первая страница): только
    завершённые (status paid); payment — пришёл платёж, payout — вывод, transfer — перевод между бизнес- и
    личным кошельком. created_at без зоны — считаем МСК (UTC+3, как у выплат Cryptomus). Ключ бизнес-кабинета
    или ошибка — None: история необязательна, баланс работает и без неё."""
    mode = _CRYPTOMUS_MODE.get(uid) or (await cryptomus_detect(s, uid, api_key))[0]
    if mode != "user":
        return None
    try:
        j = await cryptomus_call(s, uid, api_key, "POST", "/v2/user-api/transaction/list", {}, mode="user")
    except Exception:
        return None
    if not _cryptomus_ok(j):
        return None
    result = j.get("result")
    rows = result.get("items") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return None
    items = []
    for it in rows:
        if not isinstance(it, dict) or str(it.get("status")).lower() != "paid":
            continue
        kind = CRYPTOMUS_HIST_KINDS.get(str(it.get("type")).lower())
        code = str(it.get("currency") or "").upper()
        if kind:
            items.append(_hist_item(kind, CRYPTOMUS_CODES.get(code, code), it.get("amount"), it.get("created_at"),
                                    MSK_TZ))
    return _merge_hist([it for it in items if it], limit=limit)


# mexc/kucoin — своя цепочка фолбэков ниже, bybit — P2P-эндпоинт; у всех (s, key, secret, limit=...)
HISTORY_FETCHERS = {"htx": htx_history, "bingx": bingx_history, "cryptomus": cryptomus_history}


SPOT_TRADE_SYMBOLS = ("USDCUSDT", "BTCUSDT", "ETHUSDT", "TONUSDT")   # монеты бота (DEFAULT_ASSETS в p2p.py) к USDT


def _trade_item(asset, side, amount, price, ts):
    try:
        return {"kind": "trade", "asset": asset, "side": "buy" if side else "sell",
                "amount": float(amount or 0), "price": float(price or 0), "ts": _hist_ts(ts)}
    except (TypeError, ValueError):
        return None


async def mexc_spot_trades(s, api_key, api_secret, limit=20):
    """Фолбэк-история MEXC, третий источник (после депозитов/выводов): спот-сделки. У MEXC нет эндпоинта
    истории без символа — перебираем монеты бота (SPOT_TRADE_SYMBOLS) против USDT и объединяем результат.
    Сделок нет: [] — ответили все символы, None — хотя бы один не ответил."""
    out, failed = [], False
    for sym in SPOT_TRADE_SYMBOLS:
        try:
            j = await mexc_get(s, api_key, api_secret, "/api/v3/myTrades", {"symbol": sym, "limit": limit})
        except Exception:
            j = None
        if not isinstance(j, list):
            failed = True
            continue
        asset = sym[:-len("USDT")]
        out.extend(it for it in (_trade_item(asset, tr.get("isBuyer"), tr.get("qty"), tr.get("price"),
                                              tr.get("time")) for tr in j) if it)
    if not out:
        return None if failed else []
    out.sort(key=lambda it: it["ts"], reverse=True)
    return out[:limit]


async def kucoin_spot_trades(s, api_key, api_secret, passphrase_value, limit=20):
    """Фолбэк-история KuCoin, третий источник: спот-сделки (`/api/v1/fills`). В отличие от MEXC, тут
    обязателен не symbol, а tradeType — одним запросом получаем сделки по всем парам. [] — ответ без
    сделок, None — ошибка."""
    try:
        j = await kucoin_get(s, api_key, api_secret, passphrase_value, "/api/v1/fills",
                              {"tradeType": "TRADE", "pageSize": limit})
    except Exception:
        return None
    if j.get("code") != "200000":
        return None
    items = (j.get("data") or {}).get("items") or []
    out = [it for it in (_trade_item(str(tr.get("symbol") or "").split("-")[0], tr.get("side") == "buy",
                                      tr.get("size"), tr.get("price"), tr.get("createdAt")) for tr in items) if it]
    return out


async def account_history(s, exchange, limit=20):
    """История последних движений по счёту для автожурнала: (id, side, asset, ..., ts) для Bybit
    (P2P-эндпоинт, см. bybit_p2p_orders); для MEXC/KuCoin депозиты, выводы и спот-сделки объединяются
    в одну ленту по времени (а не берётся первый непустой источник — иначе старый депозит скрывает
    более свежий вывод или спот-сделку); депозиты+выводы для остальных площадок. [] — все источники
    ответили, записей нет; None — нет ключа или записей нет, а какой-то источник не ответил."""
    ex = exchange.lower()
    pair = keys(ex)
    if not pair:
        return None
    if ex == "bybit":
        return await bybit_p2p_orders(s, *pair, size=limit)
    if ex == "kucoin":
        pp = passphrase(ex)
        if not pp:
            return None
        return _merge_hist(await kucoin_history(s, *pair, pp, limit=limit),
                            await kucoin_spot_trades(s, *pair, pp, limit=limit), limit=limit)
    if ex == "mexc":
        return _merge_hist(await mexc_history(s, *pair, limit=limit),
                            await mexc_spot_trades(s, *pair, limit=limit), limit=limit)
    fetch = HISTORY_FETCHERS.get(ex)
    return await fetch(s, *pair, limit=limit) if fetch else None


P2P_STATUS_COMPLETED = 50  # Bybit P2P: код статуса завершённого ордера
P2P_SIDE_BUY = 0           # Bybit P2P: side 0 = покупка, 1 = продажа


async def bybit_p2p_orders(s, api_key, api_secret, size=20):
    """История последних завершённых P2P-ордеров Bybit пользователя (только чтение, для автожурнала).

    Возвращает список {id, side, asset, fiat, amount, price, ts} (amount — количество монеты, не фиат)
    или None, если P2P API недоступно этому ключу (нет прав/бизнес-аккаунта, ошибка сети) — тогда
    автожурнал берёт факт из другого места."""
    try:
        j = await bybit_post(s, api_key, api_secret, "/v5/p2p/order/simplifyList",
                              {"page": 1, "size": size, "status": P2P_STATUS_COMPLETED})
    except Exception:
        return None
    # P2P-эндпоинты Bybit отвечают в snake_case (ret_code/ret_msg), а не retCode, как остальной v5
    if j.get("ret_code", j.get("retCode")) != 0:
        return None
    out = []
    for it in (j.get("result") or {}).get("items") or []:
        try:
            price = float(it.get("price") or 0)
            # amount — сумма сделки в фиате; монеты — notifyTokenQuantity (в order/info — quantity)
            qty = it.get("notifyTokenQuantity") or it.get("quantity")
            fiat_sum = float(it.get("amount") or 0)
            out.append({
                "id": it.get("id"),
                "side": "buy" if str(it.get("side")) in (str(P2P_SIDE_BUY), "buy", "Buy") else "sell",
                "asset": it.get("tokenId"),
                "fiat": it.get("currencyId"),
                "amount": float(qty) if qty else (fiat_sum / price if price else 0.0),
                "price": price,
                "ts": int(it.get("createDate") or 0) / 1000,
            })
        except (TypeError, ValueError):
            continue
    return out
