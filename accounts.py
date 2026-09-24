"""Подписанные запросы к приватным API бирж, ТОЛЬКО ЧТЕНИЕ (балансы, история, уведомления).

Ключи хранятся локально: сначала `data/keys.json` (папка `data/` в git не попадает), иначе `.env`
(`BYBIT_API_KEY`/`BYBIT_API_SECRET`, `MEXC_API_KEY`/`MEXC_API_SECRET`). Нет ключей для биржи —
`keys()` вернёт None, функции аккаунтов для неё выключены. Никаких торговых/выводных запросов —
только подписанные GET к read-only эндпоинтам.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

KEYS_PATH = os.path.join("data", "keys.json")
BYBIT_BASE = "https://api.bybit.com"
MEXC_BASE = "https://api.mexc.com"
HTX_BASE = "https://api.htx.com"
KUCOIN_BASE = "https://api.kucoin.com"
CONNECTABLE = ("bybit", "mexc", "htx", "kucoin")   # биржи, для которых уже есть подпись запросов
PASSPHRASE_REQUIRED = ("kucoin",)      # ключ биржи — 3 шага (key/secret/passphrase)
ONBOARDABLE = tuple(dict.fromkeys(CONNECTABLE + PASSPHRASE_REQUIRED))   # биржи, для которых бот предлагает подключить ключ кнопками


def _keys_file():
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def keys(exchange):
    """(api_key, api_secret) для биржи или None, если ключей нет (файл имеет приоритет над .env)."""
    ex = exchange.lower()
    saved = _keys_file().get(ex, {})
    key = saved.get("key") or os.getenv(f"{exchange.upper()}_API_KEY")
    secret = saved.get("secret") or os.getenv(f"{exchange.upper()}_API_SECRET")
    return (key, secret) if key and secret else None


def save_key(exchange, key, secret, passphrase=None):
    """Сохранить ключ биржи в data/keys.json (создаёт папку/файл при необходимости).

    `passphrase` — только для бирж из PASSPHRASE_REQUIRED (сейчас KuCoin)."""
    data = _keys_file()
    entry = {"key": key, "secret": secret}
    if passphrase:
        entry["passphrase"] = passphrase
    data[exchange.lower()] = entry
    os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def passphrase(exchange):
    """Passphrase ключа биржи (сейчас только KuCoin): data/keys.json, иначе {EXCHANGE}_API_PASSPHRASE в .env."""
    ex = exchange.lower()
    saved = _keys_file().get(ex, {})
    return saved.get("passphrase") or os.getenv(f"{exchange.upper()}_API_PASSPHRASE")


def delete_key(exchange):
    """Удалить сохранённый ключ биржи из data/keys.json, если он там есть."""
    data = _keys_file()
    if data.pop(exchange.lower(), None) is None:
        return False
    os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


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
    async with s.get(url, headers=headers) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


async def bybit_get(s, api_key, api_secret, path, params=None):
    """Подписанный GET к приватному Bybit v5 (например path='/v5/account/wallet-balance')."""
    params = params or {}
    url = f"{BYBIT_BASE}{path}" + (f"?{urlencode(params)}" if params else "")
    return await _get_json(s, url, bybit_headers(api_key, api_secret, params=params))


async def mexc_get(s, api_key, api_secret, path, params=None):
    """Подписанный GET к приватному MEXC v3 (например path='/api/v3/account')."""
    signed = mexc_signed_params(api_secret, params)
    url = f"{MEXC_BASE}{path}?{urlencode(signed)}"
    return await _get_json(s, url, {"X-MEXC-APIKEY": api_key})


async def bybit_post(s, api_key, api_secret, path, body=None, recv_window="5000", timestamp=None):
    """Подписанный POST к приватному Bybit v5 (например P2P-эндпоинты) — та же формула подписи,
    что и у GET (ts+key+recv_window+данные), только query заменяется на JSON-тело запроса."""
    ts = timestamp or str(int(time.time() * 1000))
    payload = json.dumps(body or {}, separators=(",", ":"))
    sign = hmac.new(api_secret.encode(), (ts + api_key + recv_window + payload).encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json",
    }
    async with s.post(f"{BYBIT_BASE}{path}", headers=headers, data=payload) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


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
    """Подписанный GET к приватному HTX v1 (например path='/v1/account/accounts')."""
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
    """Подписанный GET к приватному KuCoin v2 (например path='/api/v1/accounts')."""
    full_path = path + (f"?{urlencode(params)}" if params else "")
    headers = kucoin_headers(api_key, api_secret, passphrase_value, "GET", full_path)
    return await _get_json(s, f"{KUCOIN_BASE}{full_path}", headers)


KUCOIN_READONLY_PERMS = {"General"}    # остальные значения permission у KuCoin дают торговлю/вывод/переводы


async def api_permissions(s, exchange):
    """Права сохранённого ключа биржи по данным самого API: (safe, detail).

    safe=False — ключ даёт торговать или выводить (не read-only), detail — что именно нашли.
    safe=True — либо ключ read-only, либо права проверить не удалось (не блокируем по недоступности API)."""
    ex = exchange.lower()
    pair = keys(ex)
    if not pair:
        return True, ""
    api_key, api_secret = pair
    try:
        if ex == "bybit":
            j = await bybit_get(s, api_key, api_secret, "/v5/user/query-api")
            if j.get("retCode") != 0:
                return True, ""
            result = j.get("result", {})
            if result.get("readOnly") == 1:
                return True, ""
            extra = [name for name, perms in (result.get("permissions") or {}).items() if perms]
            return False, "торговля/переводы (" + ", ".join(extra) + ")" if extra else "ключ не read-only"
        elif ex == "mexc":
            j = await mexc_get(s, api_key, api_secret, "/api/v3/account")
            bad = [name for name, granted in (("торговля", j.get("canTrade")), ("вывод", j.get("canWithdraw"))) if granted]
            return not bad, ", ".join(bad)
        elif ex == "htx":
            j = await htx_get(s, api_key, api_secret, "/v2/user/api-key")
            if j.get("code") != 200:
                return True, ""
            entry = next((e for e in j.get("data") or [] if e.get("accessKey") == api_key), None)
            if not entry:
                return True, ""
            perms = {p.strip().lower() for p in (entry.get("permission") or "").split(",")}
            bad = sorted(perms & {"trade", "withdraw"})
            return not bad, ", ".join(bad)
        elif ex == "kucoin":
            pp = passphrase(ex)
            if not pp:
                return True, ""
            j = await kucoin_get(s, api_key, api_secret, pp, "/api/v1/user/api-key")
            if j.get("code") != "200000":
                return True, ""
            perms = {p.strip() for p in (j.get("data", {}).get("permission") or "").split(",") if p.strip()}
            bad = sorted(perms - KUCOIN_READONLY_PERMS)
            return not bad, ", ".join(bad)
    except Exception:
        return True, ""
    return True, ""


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


BALANCE_FETCHERS = {"bybit": bybit_balances, "mexc": mexc_balances}   # биржи, для которых уже есть /balance


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
        else:
            return False, f"{exchange}: подпись запросов пока не реализована"
    except Exception as e:
        return False, str(e)
    return True, "ключ рабочий, доступ только для чтения"


P2P_STATUS_COMPLETED = 50  # Bybit P2P: код статуса завершённого ордера


async def bybit_p2p_orders(s, api_key, api_secret, size=20):
    """История последних завершённых P2P-ордеров Bybit пользователя (только чтение, для автожурнала).

    Возвращает список {id, side, asset, fiat, amount, price, ts} или None, если P2P API недоступно
    этому ключу (нет прав/бизнес-аккаунта, ошибка сети) — тогда автожурнал берёт факт из другого места."""
    try:
        j = await bybit_post(s, api_key, api_secret, "/v5/p2p/order/simplifyList",
                              {"page": 1, "size": size, "status": P2P_STATUS_COMPLETED})
    except Exception:
        return None
    if j.get("retCode") != 0:
        return None
    out = []
    for it in (j.get("result") or {}).get("items") or []:
        try:
            out.append({
                "id": it.get("id"),
                "side": "buy" if str(it.get("side")) in ("1", "buy", "Buy") else "sell",
                "asset": it.get("tokenId"),
                "fiat": it.get("currencyId"),
                "amount": float(it.get("amount") or 0),
                "price": float(it.get("price") or 0),
                "ts": int(it.get("createDate") or 0) / 1000,
            })
        except (TypeError, ValueError):
            continue
    return out
