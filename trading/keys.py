"""Торговые ключи: bybit_trade и bingx_trade — основной аккаунт владельца (его решение), только «торговля».

- Хранятся как остальные ключи бота: accounts.keys(имя) — data/keys.json (DPAPI) или .env
  (BYBIT_TRADE_API_KEY/…_SECRET, BINGX_TRADE_API_KEY/…_SECRET). Вводит их только владелец на ПК:
  scripts/trading_keys.py (getpass), не Telegram. В accounts.CONNECTABLE их нет — кнопки бота их не видят.
- При старте (`check`) права ключа берутся у самой биржи; торговля по ключу разрешена, только если права — ровно из
  списка нужных (ALLOWED_*), а вывода, переводов, субаккаунтов, P2P, earn и прочего нет. Не удалось проверить,
  незнакомая форма ответа, незнакомое право — отказ (fail closed, в отличие от accounts.api_permissions).
Bybit: GET /v5/user/query-api (readOnly 0/1, permissions {группа: [права]}, ips). BingX: GET
/openApi/v1/account/apiPermissions — две документированные формы (коды permissions или флаги enable*/permits*), как в
accounts.bingx_key_safety.
"""
from collections import namedtuple

import accounts
from trading import venues

KEY_NAMES = {venues.BYBIT: "bybit_trade", venues.BINGX: "bingx_trade"}
# Bybit: разрешённые права по группам. Всё остальное непустое — отказ.
BYBIT_ALLOWED = {"ContractTrade": {"Order", "Position"}, "Derivatives": {"DerivativesTrade"}, "Spot": {"SpotTrade"},
                 "Exchange": {"ExchangeHistory"}}   # ExchangeHistory — только история конвертаций
BYBIT_TRADE_NEEDED = {"ContractTrade": {"Order", "Position"}}   # торговля перпами (UTA): оба права
BINGX_ALLOWED_CODES = {1, 2, 3}      # 1 спот, 2 чтение, 3 перпы; 4/5/7 (переводы, вывод) и незнакомые — отказ
BINGX_FUTURES_CODE = 3
BINGX_ALLOWED_FLAGS = {"enableReading", "enableFutures", "enableSpotAndMarginTrading"}

KeyCheck = namedtuple("KeyCheck", "ok state detail ip_bound")   # state: ok / unsafe / unusable / unknown / none


def credentials(venue):
    """(api_key, api_secret) торгового ключа биржи или None."""
    return accounts.keys(KEY_NAMES[venue])


def bybit_rights(result):
    """Разбор result ответа /v5/user/query-api → KeyCheck (без запроса)."""
    if not isinstance(result, dict) or result.get("readOnly") not in (0, 1) \
            or not isinstance(result.get("permissions"), dict):
        return KeyCheck(False, "unknown", "незнакомая форма ответа Bybit о правах ключа", None)
    extra = []
    for group, rights in result["permissions"].items():
        if not isinstance(rights, list) or not all(isinstance(r, str) for r in rights):
            return KeyCheck(False, "unknown", f"незнакомая форма прав: {group}", None)
        extra += [f"{group}:{r}" for r in rights if r not in BYBIT_ALLOWED.get(group, set())]
    ips = result.get("ips")
    ip_bound = bool(ips) and ips != ["*"] if isinstance(ips, list) else None
    if extra:
        return KeyCheck(False, "unsafe", "у ключа лишние права: " + ", ".join(extra), ip_bound)
    if result["readOnly"] == 1:
        return KeyCheck(False, "unusable", "ключ только для чтения — торговать им нельзя", ip_bound)
    perms = result["permissions"]
    if not all(need <= set(perms.get(g, [])) for g, need in BYBIT_TRADE_NEEDED.items()):
        return KeyCheck(False, "unusable", "у ключа нет прав Contract: Orders и Positions", ip_bound)
    return KeyCheck(True, "ok", "" if ip_bound else "ключ без привязки к IP", ip_bound)


def bingx_rights(data):
    """Разбор ответа apiPermissions BingX (без обёртки {code, data}) → KeyCheck."""
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        return KeyCheck(False, "unknown", "незнакомая форма ответа BingX о правах ключа", None)
    _, human = accounts.bingx_key_safety(data)   # готовый текст «что даёт ключ сверх чтения»
    if "permissions" in data:
        raw = data["permissions"]
        raw = raw.split(",") if isinstance(raw, str) else raw
        try:
            codes = {int(str(c).strip()) for c in raw if str(c).strip()} if isinstance(raw, list) else None
        except ValueError:
            codes = None
        flags = {k: accounts._bingx_flag(v) for k, v in data.items() if str(k).lower().startswith(("enable", "permit"))}
        if codes is None or None in flags.values():
            return KeyCheck(False, "unknown", "незнакомые права BingX", None)
        bad = sorted(codes - BINGX_ALLOWED_CODES) + sorted(k for k, v in flags.items() if v and
                                                           k not in BINGX_ALLOWED_FLAGS)
        futures = BINGX_FUTURES_CODE in codes or flags.get("enableFutures") is True
        ips = data.get("ipAddresses")
        ip_bound = bool(ips) if isinstance(ips, list) else None
    else:
        flags = {k: accounts._bingx_flag(v) for k, v in data.items() if str(k).lower().startswith(("enable", "permit"))}
        if not flags or None in flags.values() or any(k not in flags for k in accounts.BINGX_FLAGS_REQUIRED):
            return KeyCheck(False, "unknown", "неполный набор прав BingX — не проверить", None)
        bad = sorted(k for k, v in flags.items() if v and k not in BINGX_ALLOWED_FLAGS)
        futures = flags.get("enableFutures") is True
        ip = accounts._bingx_flag(data.get("ipRestrict"))
        ip_bound = ip
    if bad:
        return KeyCheck(False, "unsafe", "у ключа лишние права: " + (human or ", ".join(map(str, bad))), ip_bound)
    if not futures:
        return KeyCheck(False, "unusable", "у ключа нет права торговли перпетуалами", ip_bound)
    return KeyCheck(True, "ok", "" if ip_bound else "ключ без привязки к IP", ip_bound)


async def check(s, venue, creds=None):
    """Права торгового ключа по данным биржи → KeyCheck. Любой сбой — отказ."""
    creds = creds or credentials(venue)
    if not creds:
        return KeyCheck(False, "none", "торговый ключ не сохранён", None)
    path = "/v5/user/query-api" if venue == venues.BYBIT else "/openApi/v1/account/apiPermissions"
    try:
        status, j = await venues.call(s, venue, "GET", path, {}, creds)
    except Exception as e:   # сеть, таймаут
        return KeyCheck(False, "unknown", f"права не проверить: {accounts.api_error_text(e)}", None)
    if venue == venues.BINGX and isinstance(j, dict) and "code" not in j and status == 200:
        return bingx_rights(j)   # docs-v3: ответ без обёртки {code, data}
    kind, data, _, msg = venues.outcome(venue, status, j, creds)
    if kind != "ok":
        return KeyCheck(False, "unknown", f"права не проверить: {msg}", None)
    return bybit_rights(data) if venue == venues.BYBIT else bingx_rights(data)


async def startup_check(s):
    """{биржа: KeyCheck} для всех бирж ядра. Торговать можно только там, где ok."""
    return {v: await check(s, v) for v in venues.VENUES}
