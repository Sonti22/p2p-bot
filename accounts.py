"""Подписанные запросы к приватным API бирж, ТОЛЬКО ЧТЕНИЕ (балансы, история, уведомления).

Ключи хранятся локально: сначала `data/keys.json` (папка `data/` в git не попадает), иначе `.env`
(`BYBIT_API_KEY`/`BYBIT_API_SECRET`, `MEXC_API_KEY`/`MEXC_API_SECRET`). Нет ключей для биржи —
`keys()` вернёт None, функции аккаунтов для неё выключены. Никаких торговых/выводных запросов —
только подписанные GET к read-only эндпоинтам.
"""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

KEYS_PATH = os.path.join("data", "keys.json")
BYBIT_BASE = "https://api.bybit.com"
MEXC_BASE = "https://api.mexc.com"
CONNECTABLE = ("bybit", "mexc")   # биржи, для которых уже есть подпись запросов (HTX/KuCoin — позже)


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


def save_key(exchange, key, secret):
    """Сохранить ключ биржи в data/keys.json (создаёт папку/файл при необходимости)."""
    data = _keys_file()
    data[exchange.lower()] = {"key": key, "secret": secret}
    os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
        else:
            return False, f"{exchange}: подпись запросов пока не реализована"
    except Exception as e:
        return False, str(e)
    return True, "ключ рабочий, доступ только для чтения"
