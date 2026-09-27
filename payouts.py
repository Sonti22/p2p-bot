"""Выплаты Cryptomus — этап 2, решение владельца 2026-09-26: только выплаты с БИЗНЕС-кошелька Cryptomus, каждую
запускает и подтверждает владелец в Telegram (/payout). Автовыплат, переводов между кошельками Cryptomus, конвертаций
(to_currency/from_currency/course_source) и торговли в коде нет.

Защита:
- ключ выплат (Merchant ID + Payout key) — только локально: `accounts.keys("cryptomus_payout")`, то есть
  data/keys.json или .env (CRYPTOMUS_PAYOUT_API_KEY / CRYPTOMUS_PAYOUT_API_SECRET); в логи и сообщения не попадает;
- `payout_call` — только пары (метод, путь) из PAYOUT_CALLS и публичный курс из RATE_CALLS; остальное — ValueError до
  подписи и отправки; за редиректом не идём (3xx — не успех). Подпись — ровно по отправляемым байтам (как PHP SDK);
- адреса — только из белого списка data/payout_whitelist.json; бот его не пишет, правит владелец на ПК
  (scripts/payout_whitelist.py); записи с ошибкой в адресе/сети пропускаются;
- лимиты PAYOUT_MAX_ONE и PAYOUT_DAILY_LIMIT в USDT (календарный день МСК, списание = сумма + комиссия) — при
  предпросмотре и ещё раз перед отправкой по свежему курсу; курс сдвинулся больше PAYOUT_RATE_DRIFT — отказ,
  подтвердить заново; нет курса или он вне полосы правдоподобия PAYOUT_RATE_BANDS (в коде, не в .env) — отказ;
- выключатель: выплаты идут только при PAYOUTS=1 (.env на ПК); из Telegram их можно только выключить. «⛔ Стоп»
  проверяется сразу перед каждым POST создания (после каждого await) и будит отправку, спящую перед повтором;
- журнал data/payouts.db: намерение с новым order_id пишется до любого запроса. Неясный исход (таймаут, 5xx) — не
  новый order_id, а /v1/payout/info и повтор с тем же order_id и теми же байтами (не больше MAX_RESEND раз).
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal, DecimalException, InvalidOperation

import accounts
import jsonstore

logger = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "payouts.db")
WHITELIST_PATH = os.path.join(HERE, "data", "payout_whitelist.json")
KEY_NAME = "cryptomus_payout"   # accounts.keys(KEY_NAME) -> (Merchant ID, Payout key); в CONNECTABLE его нет
MSK = timezone(timedelta(hours=3))

COINS = ("USDT", "USDC", "BTC", "ETH", "TON")
CODES = {"TON": "GRAM"}   # код монеты у Cryptomus: Toncoin — GRAM (как accounts.CRYPTOMUS_CODES наоборот)
NETWORKS = {"tron": "tron", "eth": "evm", "bsc": "evm", "polygon": "evm", "arbitrum": "evm", "avalanche": "evm",
            "btc": "btc", "ton": "ton"}   # сеть Cryptomus -> формат адреса
COIN_NETWORKS = {"USDT": ("tron", "eth", "bsc", "polygon", "arbitrum", "avalanche", "ton"),
                 "USDC": ("eth", "bsc", "polygon", "arbitrum", "avalanche"),
                 "BTC": ("btc",), "ETH": ("eth", "bsc", "arbitrum"), "TON": ("ton",)}
STABLE = ("USDT", "USDC")          # к USDT 1:1
RATE_COINS = ("BTC", "ETH", "TON")  # к USDT — по публичному курсу Cryptomus
PAYOUT_CALLS = frozenset({
    ("POST", "/v1/payout/services"),   # сервисы: доступность, мин/макс, комиссия
    ("POST", "/v1/payout"),            # создать выплату
    ("POST", "/v1/payout/info"),       # статус по order_id
    ("POST", "/v1/payout/list"),       # история выплат (сверка)
})
RATE_CALLS = frozenset(("GET", f"/v1/exchange-rate/{CODES.get(c, c)}/list") for c in RATE_COINS)   # без ключа и подписи
# Правдоподобный курс монеты к USDT, [мин, макс] включительно — в предпросмотре и перед самой отправкой. Крошечный, но
# «честный» курс (1e-30) оценил бы выплату в ≈0 USDT и выключил бы лимиты в USDT; вне полосы или монеты нет — отказ.
# Только здесь, не из .env: подменённый .env полосу не расширит. GRAM — код TON у Cryptomus (CODES), полоса та же.
PAYOUT_RATE_BANDS = {
    "USDT": (Decimal("0.95"), Decimal("1.05")),
    "USDC": (Decimal("0.95"), Decimal("1.05")),
    "BTC": (Decimal("1000"), Decimal("10000000")),
    "ETH": (Decimal("10"), Decimal("1000000")),
    "TON": (Decimal("0.01"), Decimal("1000")),
    "GRAM": (Decimal("0.01"), Decimal("1000")),
}

STATES = ("prepared", "sending", "sent", "unknown", "final_paid", "final_failed", "rejected")
COUNTED = ("prepared", "sending", "sent", "unknown", "final_paid")   # идут в дневной лимит
PENDING = ("sent", "unknown")                                         # опрашивает poll
FAIL_STATUSES = ("fail", "cancel", "system_fail")
STATE_NAMES = {"prepared": "готовится", "sending": "отправляется", "sent": "⏳ в обработке",
               "unknown": "⚠️ исход неясен", "final_paid": "✅ выплачено", "final_failed": "❌ не прошла",
               "rejected": "🚫 отклонена"}
MISMATCH = "ответ Cryptomus не совпал с заявкой"
NOT_FOUND = "Cryptomus не находит эту выплату по order_id"
TOKEN_TTL = 120      # сек: столько живёт кнопка «✅ Отправить» предпросмотра
MAX_RESEND = 2       # повторов POST с тем же order_id после неясного исхода и «не найдено» в /info
RETRY_DELAY = 5      # сек × номер попытки: пауза после неясного исхода перед /info и перед повтором
POLL_INTERVAL = 30   # сек между опросами незавершённых выплат
POLL_DAYS = 7        # незавершённые выплаты старше — не опрашиваем (в /payout history они остаются)
DEFAULT_LIMIT = "2000"
DEFAULT_DRIFT = "2"  # %: PAYOUT_RATE_DRIFT — допустимый сдвиг курса монеты к USDT между предпросмотром и отправкой
QUANT = Decimal("0.00000001")
CENT = Decimal("0.01")
OFF = "выплаты выключены (PAYOUTS≠1): включить можно только на ПК — PAYOUTS=1 в .env и перезапуск бота"

_lock = {"loop": None, "lock": None}
_stop = {"n": 0, "wake": set()}   # «⛔ Стоп»: сколько раз нажат и (цикл, событие) отправок, спящих перед повтором


def _send_lock():
    """Один asyncio.Lock на цикл событий: одна отправка/опрос за раз."""
    loop = asyncio.get_running_loop()
    if _lock["loop"] is not loop:
        _lock.update(loop=loop, lock=asyncio.Lock())
    return _lock["lock"]


def enabled():
    """Выключатель: выплаты только при PAYOUTS=1 (читается при каждом обращении)."""
    return os.getenv("PAYOUTS") == "1"


def disable():
    """«⛔ Стоп»: выплаты выключаются в этом процессе сразу, ещё до записи .env (та может и не удаться). Отправка, которая
    уже идёт, после Стопа не начинает ни одного запроса, даже если PAYOUTS снова станет 1, а спящую перед /info или
    повтором Стоп будит сразу."""
    os.environ["PAYOUTS"] = "0"
    _stop["n"] += 1
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    for loop, event in list(_stop["wake"]):
        if loop is running:
            event.set()
        else:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:   # цикл уже закрыт — будить некого
                pass


def _halted(gen):
    """Отправке, начатой при счётчике Стопа gen, больше ничего не слать: выплаты выключены или с тех пор был Стоп."""
    return not enabled() or _stop["n"] != gen


async def _pause(seconds, gen):
    """Пауза отправки перед /info или повтором, которую «⛔ Стоп» прерывает сразу. True — дальше ничего не слать
    (_halted) — проверено и до паузы, и сразу после неё."""
    if _halted(gen) or seconds <= 0:
        return _halted(gen)
    event = asyncio.Event()
    waker = (asyncio.get_running_loop(), event)
    _stop["wake"].add(waker)
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        pass
    finally:
        _stop["wake"].discard(waker)
    return _halted(gen)


def switch_from_file(path):
    """При старте бота: выключатель — только из файла .env. Выплаты остаются включёнными, лишь если в файле есть строка
    PAYOUTS и ВСЕ такие строки (имя без учёта регистра) равны 1; иначе — PAYOUTS=0 в процессе. Так PAYOUTS=1 из
    окружения Windows или родительского процесса и «payouts=1» выше записанного «⛔ Стоп» не включат выплаты снова.
    Только выключает, включить не может."""
    values = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip().upper() == "PAYOUTS":
                        values.append(v.split(" #")[0].strip())
    except (OSError, ValueError):
        values = []
    if not values or any(v != "1" for v in values):
        disable()


def credentials():
    """(Merchant ID, Payout key) или None."""
    return accounts.keys(KEY_NAME)


def _limit(name):
    try:
        v = Decimal(os.getenv(name, DEFAULT_LIMIT).strip())
    except (InvalidOperation, AttributeError):
        return Decimal(0)
    return v if v.is_finite() and v > 0 else Decimal(0)   # мусор в .env — лимит 0, выплаты не пройдут


def limits():
    """(лимит одной выплаты, лимит дня) в USDT из .env."""
    return _limit("PAYOUT_MAX_ONE"), _limit("PAYOUT_DAILY_LIMIT")


def rate_drift():
    """Допустимый сдвиг курса монеты к USDT между предпросмотром и отправкой — доля (PAYOUT_RATE_DRIFT в %, по
    умолчанию 2 %). Мусор или отрицательное в .env — 0: любой сдвиг курса — отказ (стейблкоинов не касается: курс 1)."""
    try:
        v = Decimal(os.getenv("PAYOUT_RATE_DRIFT", DEFAULT_DRIFT).strip())
    except (InvalidOperation, AttributeError):
        return Decimal(0)
    return v / 100 if v.is_finite() and v >= 0 else Decimal(0)


def fmt(d):
    """Decimal -> строка без экспоненты и лишних нулей ("25", "0.015")."""
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _dec(v):
    """Число из ответа Cryptomus (строка/число) -> Decimal или None."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _usdt_value(debit, rate):
    """USDT-оценка списания по курсу, вверх до цента; None — оценка не помещается в Decimal: это отказ с причиной, а не
    исключение посреди предпросмотра или отправки (абсурдный курс вроде "1e30" раньше отсекает _rate_band_error)."""
    try:
        return (debit * rate).quantize(CENT, rounding=ROUND_UP)
    except DecimalException:
        return None


def _rate_text(d):
    """Курс для текста владельцу: абсурдно большой или малый — коротко, в научной записи (не тысячи нулей)."""
    return fmt(d) if -12 <= d.adjusted() <= 15 else f"{d:.3E}"


def _rate_band_error(coin, rate):
    """Причина отказа, если курс coin к USDT вне PAYOUT_RATE_BANDS или полосы для монеты нет, иначе None: по
    неправдоподобному курсу оценка в USDT ничего не значит, и лимиты не проверить."""
    band = PAYOUT_RATE_BANDS.get(coin)
    if band is None:
        return f"нет полосы правдоподобного курса {coin} к USDT — лимит не проверить, выплату не отправляю"
    if not (isinstance(rate, Decimal) and rate.is_finite() and band[0] <= rate <= band[1]):
        shown = _rate_text(rate) if isinstance(rate, Decimal) else "?"
        return f"курс {coin} к USDT неправдоподобен ({shown}) — лимит не проверить, выплату не отправляю"
    return None


def _true(v):
    return v is True or v == 1 or str(v).strip().lower() in ("true", "1")


_AMOUNT_RE = re.compile(r"\d{1,20}(?:[.,]\d{1,20})?")


def parse_amount(text):
    """Сумма выплаты текстом -> (Decimal, None) или (None, причина): > 0, не больше 8 знаков после точки."""
    t = (text or "").strip()
    if not _AMOUNT_RE.fullmatch(t):
        return None, "не понял сумму: нужно число, например 25 или 0.015"
    d = Decimal(t.replace(",", "."))
    if d <= 0:
        return None, "сумма должна быть больше нуля"
    if -d.as_tuple().exponent > 8:
        return None, "не больше 8 знаков после точки"
    return d, None


# --- адреса и белый список ---

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _b58check(s):
    """Тело base58check-адреса без контрольной суммы или None (не base58 / сумма не сошлась)."""
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    raw = b"\0" * (len(s) - len(s.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    body, check = raw[:-4], raw[-4:]
    return body if len(raw) > 4 and hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == check else None


def _bech32_ok(addr):
    """bc1…: контрольная сумма bech32 (segwit v0) / bech32m (v1+, taproot), длины P2WPKH/P2WSH/P2TR."""
    hrp, _, data = addr.rpartition("1")
    vals = [_BECH.find(c) for c in data]
    if hrp != "bc" or len(data) not in (39, 59) or -1 in vals:
        return False
    chk = 1
    for v in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + vals:
        top, chk = chk >> 25, (chk & 0x1ffffff) << 5 ^ v
        for i, g in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            if top >> i & 1:
                chk ^= g
    return chk == (1 if vals[0] == 0 else 0x2bc830a3) and vals[0] <= 16


def _rol64(v, n):
    n %= 64
    return ((v << n) | (v >> (64 - n))) & 0xFFFFFFFFFFFFFFFF


def _keccak_f(lanes):
    """Перестановка Keccak-f[1600] (по CompactFIPS202 авторов Keccak), lanes[x][y] — 64-битные слова."""
    r = 1
    for _ in range(24):
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rol64(c[(x + 1) % 5], 1) for x in range(5)]
        lanes = [[lanes[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        x, y = 1, 0
        cur = lanes[x][y]
        for t in range(24):
            x, y = y, (2 * x + 3 * y) % 5
            cur, lanes[x][y] = lanes[x][y], _rol64(cur, (t + 1) * (t + 2) // 2)
        for y in range(5):
            row = [lanes[x][y] for x in range(5)]
            for x in range(5):
                lanes[x][y] = row[x] ^ (~row[(x + 1) % 5] & row[(x + 2) % 5])
        for j in range(7):
            r = ((r << 1) ^ ((r >> 7) * 0x71)) % 256
            if r & 2:
                lanes[0][0] ^= 1 << ((1 << j) - 1)
    return lanes


def _keccak256(data, suffix=0x01):
    """Keccak-256 как в Ethereum (suffix 0x01). hashlib.sha3_256 — это SHA3 с другим паддингом (suffix 0x06)."""
    rate = 136
    msg = bytearray(data) + bytes([suffix])
    msg += b"\0" * (-len(msg) % rate)
    msg[-1] |= 0x80
    lanes = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            lanes[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i:off + 8 * i + 8], "little")
        lanes = _keccak_f(lanes)
    return b"".join(lanes[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def evm_checksummed(address):
    """EVM-адрес записан со смешанным регистром — значит, несёт контрольную сумму EIP-55."""
    h = address[2:]
    return h != h.lower() and h != h.upper()


def _eip55_ok(address):
    """Смешанный регистр — контрольная сумма EIP-55 должна сойтись; все строчные/заглавные — проверять нечего."""
    if not evm_checksummed(address):
        return True
    h = _keccak256(address[2:].lower().encode("ascii")).hex()
    return all(ch == (ch.upper() if int(h[i], 16) >= 8 else ch.lower()) for i, ch in enumerate(address[2:]))


def _crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = (crc << 1 ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def address_error(network, address):
    """Причина, по которой адрес не подходит сети, или None. Проверяем формат и контрольные суммы, где они есть."""
    kind = NETWORKS.get(network)
    if not isinstance(address, str) or not address or not address.isascii() or re.search(r"\s", address):
        return "адрес пустой, с пробелами или не латиницей"
    if kind == "tron":
        body = _b58check(address) if re.fullmatch(rf"T[{_B58}]{{33}}", address) else None
        return None if body and len(body) == 21 and body[0] == 0x41 else "не адрес TRON (T… 34 символа base58)"
    if kind == "evm":
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            return "не EVM-адрес (0x + 40 hex)"
        return None if _eip55_ok(address) else "EVM-адрес с неверной контрольной суммой (EIP-55): в нём опечатка"
    if kind == "btc":
        if address.startswith("bc1"):
            return None if _bech32_ok(address) else "не адрес BTC bc1… (строчными, с верной контрольной суммой)"
        body = _b58check(address) if re.fullmatch(rf"[13][{_B58}]{{25,34}}", address) else None
        return None if body and len(body) == 21 and body[0] == (0 if address[0] == "1" else 5) else "не адрес BTC"
    if kind == "ton":
        if re.fullmatch(r"0:[0-9a-fA-F]{64}", address):
            return None
        if re.fullmatch(r"[EU]Q[A-Za-z0-9_-]{46}", address):
            raw = base64.urlsafe_b64decode(address)
            if raw[0] in (0x11, 0x51) and raw[1] == 0 and _crc16(raw[:34]) == int.from_bytes(raw[34:], "big"):
                return None
        return "не адрес TON (EQ…/UQ… 48 символов base64url или 0:hex)"
    return f"неизвестная сеть {network}"


def validate_entry(e):
    """Запись белого списка -> (нормализованная запись, None) или (None, причина)."""
    if not isinstance(e, dict):
        return None, "запись — не словарь"
    eid = str(e.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", eid):
        return None, "id: 1–16 символов A-Z a-z 0-9 _ -"
    name = str(e.get("name") or "").strip()
    if not name or len(name) > 40:
        return None, "имя: 1–40 символов"
    cur = str(e.get("currency") or "").upper()
    if cur not in COINS:
        return None, f"монета {cur or '?'} не из {', '.join(COINS)}"
    net = str(e.get("network") or "").lower()
    if net not in COIN_NETWORKS[cur]:
        return None, f"сеть {net or '?'} для {cur} не из {', '.join(COIN_NETWORKS[cur])}"
    address = e.get("address")
    why = address_error(net, address)
    if why:
        return None, why
    memo = e.get("memo") or ""
    if memo and net != "ton":
        return None, "memo — только для сети ton"
    if memo and not (isinstance(memo, str) and re.fullmatch(r"[\x21-\x7e]{1,30}", memo)):
        return None, "memo: 1–30 латинских символов без пробелов"
    return {"id": eid, "name": name, "currency": cur, "network": net, "address": address, "memo": memo}, None


def load_whitelist(path=None):
    """Проверенные записи белого списка ({"entries": [...]}); с ошибкой или повтором id — пропуск с предупреждением.
    Бот файл только читает."""
    raw = jsonstore.read_dict(path or WHITELIST_PATH).get("entries")
    out, seen = [], set()
    for i, e in enumerate(raw if isinstance(raw, list) else []):
        entry, why = validate_entry(e)
        if entry and entry["id"] in seen:
            entry, why = None, "повтор id"
        if why:
            logger.warning("белый список выплат: запись %s пропущена — %s", i + 1, why)
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out


def whitelist_entry(eid, path=None):
    return next((e for e in load_whitelist(path) if e["id"] == eid), None)


# --- запросы ---

def payout_body(payload):
    """Тело запроса как у PHP SDK (json_encode + JSON_UNESCAPED_UNICODE): компактно, "/" -> "\\/", UTF-8. Нет
    параметров — пустое тело. Подписываются и отправляются ровно эти байты."""
    if payload is None:
        return b""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("/", "\\/").encode("utf-8")


def payout_sign(body, api_key):
    """sign = md5_hex(base64(байты тела) + Payout key); пустое тело — md5(key)."""
    return hashlib.md5(base64.b64encode(body) + api_key.encode("utf-8")).hexdigest()


async def _fetch(req):
    """(HTTP-статус, JSON или None). Редирект не выполняется (allow_redirects=False) — вернётся его статус."""
    async with req as r:
        status, raw = r.status, await r.read()
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:   # и UnicodeDecodeError
        return status, None


async def payout_call(s, method, path, payload=None, creds=None, body=None):
    """Запрос выплат Cryptomus: (статус, JSON). Пара (метод, путь) не из PAYOUT_CALLS/RATE_CALLS — ValueError до подписи
    и отправки. POST — заголовки merchant и sign, тело — ровно подписанные байты (body — уже готовые байты, для
    повтора тех же байтов); курс (RATE_CALLS) — GET без ключа и подписи. Заголовки, подпись и тело не логируются."""
    method = str(method).upper()
    url = f"{accounts.CRYPTOMUS_BASE}{path}"
    if (method, path) in RATE_CALLS and payload is None and body is None:
        return await _fetch(s.get(url, allow_redirects=False))
    if (method, path) not in PAYOUT_CALLS:
        raise ValueError(f"Cryptomus: {method} {path} не входит в список выплат")
    if not creds or not all(creds):
        raise ValueError("Cryptomus: нет ключа выплат")
    merchant, key = creds
    data = payout_body(payload) if body is None else body
    headers = {"merchant": merchant, "sign": payout_sign(data, key), "Content-Type": "application/json"}
    return await _fetch(s.post(url, headers=headers, data=data, allow_redirects=False))


def _err_text(status, j, creds=None):
    msg = accounts._cryptomus_err(j) if isinstance(j, dict) else f"HTTP {status}"
    return accounts._scrub(msg, *(creds or ()))


def _classify(status, j, creds=None):
    """("ok", result, "") — только HTTP 200 + state 0 + result с uuid; ("error", None, текст) — отказ: HTTP 200 или 4xx
    (кроме 408/429) и JSON с state≠0 и message/errors (Cryptomus отдаёт их под 422); ("ambiguous", None, текст) —
    остальное: 5xx, 3xx, 408/429, пустое тело, не JSON, неизвестная форма. Логику PHP SDK (пустое тело = успех)
    не повторяем."""
    ok_state = isinstance(j, dict) and j.get("state") in (0, "0")
    result = j.get("result") if isinstance(j, dict) else None
    if status == 200 and ok_state and isinstance(result, dict) and result.get("uuid"):
        return "ok", result, ""
    msg = _err_text(status, j, creds)
    definite = isinstance(j, dict) and not ok_state and bool(j.get("message") or j.get("errors"))
    if definite and (status == 200 or (400 <= status < 500 and status not in (408, 429))):
        return "error", None, msg
    return "ambiguous", None, msg


def _ok_list(status, j):
    return status == 200 and isinstance(j, dict) and j.get("state") in (0, "0") and isinstance(j.get("result"), list)


async def usdt_rate(s, coin):
    """Сколько USDT стоит 1 монета: USDT/USDC — 1, BTC/ETH/TON — публичный курс Cryptomus; нет курса — None."""
    if coin in STABLE:
        return Decimal(1)
    if coin not in RATE_COINS:
        return None
    code = CODES.get(coin, coin)
    try:
        status, j = await payout_call(s, "GET", f"/v1/exchange-rate/{code}/list")
    except Exception as e:
        logger.warning("курс %s: %s", coin, accounts.api_error_text(e))
        return None
    if not _ok_list(status, j):
        return None
    for it in j["result"]:
        if isinstance(it, dict) and str(it.get("from", "")).upper() == code and str(it.get("to", "")).upper() == "USDT":
            v = _dec(it.get("course"))
            return v if v is not None and v > 0 else None
    return None


async def service(s, creds, entry):
    """Сервис выплат Cryptomus для монеты и сети записи: (сервис, None) или (None, причина)."""
    code = CODES.get(entry["currency"], entry["currency"])
    try:
        status, j = await payout_call(s, "POST", "/v1/payout/services", creds=creds)
    except Exception as e:
        return None, f"список сервисов выплат Cryptomus недоступен ({accounts.api_error_text(e)})"
    if not _ok_list(status, j):
        return None, f"список сервисов выплат Cryptomus: {_err_text(status, j, creds)}"
    for it in j["result"]:
        if (isinstance(it, dict) and str(it.get("currency", "")).upper() == code
                and str(it.get("network", "")).lower() == entry["network"]):
            return it, None
    return None, f"Cryptomus не выплачивает {code} в сети {entry['network']}"


def service_fee(svc, entry, amount):
    """Комиссия по сервису: (fee, None) или (None, причина) — сервис выключен, сумма вне мин/макс, нет данных.
    fee = fee_amount + amount × percent / 100 с округлением вверх (итоговую комиссию Cryptomus опросом не узнать)."""
    if not _true(svc.get("is_available")):
        return None, f"Cryptomus сейчас не выплачивает {entry['currency']} в сети {entry['network']}"
    lim, comm = svc.get("limit") or {}, svc.get("commission") or {}
    lo, hi = _dec(lim.get("min_amount")), _dec(lim.get("max_amount"))
    fixed, pct = _dec(comm.get("fee_amount")), _dec(comm.get("percent"))
    if None in (lo, hi, fixed, pct) or fixed < 0 or pct < 0:
        return None, "Cryptomus не отдал лимиты или комиссию — выплата не посчитана"
    if amount < lo:
        return None, f"меньше минимума Cryptomus: {fmt(lo)} {entry['currency']}"
    if amount > hi:
        return None, f"больше максимума Cryptomus: {fmt(hi)} {entry['currency']}"
    return (fixed + amount * pct / 100).quantize(QUANT, rounding=ROUND_UP), None


# --- журнал ---

def _connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS payouts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE NOT NULL, created_ts REAL, "
                "wl_id TEXT, wl_name TEXT, currency TEXT, network TEXT, address TEXT, memo TEXT DEFAULT '', "
                "amount TEXT, fee TEXT, debit TEXT, usdt_value TEXT, state TEXT, uuid TEXT DEFAULT '', "
                "status TEXT DEFAULT '', is_final INTEGER DEFAULT 0, txid TEXT DEFAULT '', note TEXT DEFAULT '', "
                "updated_ts REAL, create_kind TEXT DEFAULT '')")   # create_kind: "error" — создание получило только отказы
    if "create_kind" not in {r[1] for r in con.execute("PRAGMA table_info(payouts)")}:   # база первой версии
        con.execute("ALTER TABLE payouts ADD COLUMN create_kind TEXT DEFAULT ''")
        con.commit()
    return con


def _rows(sql, args=(), path=None):
    if not os.path.exists(path or DB_PATH):   # чтение базу не создаёт: выплат ещё не было
        return []
    con = _connect(path)
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def get(order_id, path=None):
    rows = _rows("SELECT * FROM payouts WHERE order_id=?", (order_id,), path)
    return rows[0] if rows else None


def _update(order_id, path=None, **fields):
    fields["updated_ts"] = time.time()
    con = _connect(path)
    try:
        with con:
            con.execute(f"UPDATE payouts SET {', '.join(f'{k}=?' for k in fields)} WHERE order_id=?",
                        (*fields.values(), order_id))
    finally:
        con.close()
    return get(order_id, path)


def new_order_id():
    """Свежий order_id: ≤ 32 символов [A-Za-z0-9_-] (у /v1/payout/info предел 32)."""
    return f"tg-{datetime.now(MSK):%Y%m%d%H%M%S}-{secrets.token_hex(4)}"


def _insert_intent(entry, amount, q, path=None):
    """Записать намерение (state=prepared) со свежим order_id — до любого запроса. Возвращает строку журнала."""
    con = _connect(path)
    try:
        for _ in range(5):
            order_id = new_order_id()
            try:
                with con:
                    con.execute("INSERT INTO payouts (order_id, created_ts, wl_id, wl_name, currency, network, "
                                "address, memo, amount, fee, debit, usdt_value, state, updated_ts) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)",
                                (order_id, time.time(), entry["id"], entry["name"], entry["currency"], entry["network"],
                                 entry["address"], entry["memo"], fmt(amount), fmt(q["fee"]), fmt(q["debit"]),
                                 str(q["usdt"]), time.time()))
                break
            except sqlite3.IntegrityError:   # совпал order_id — берём другой
                continue
        else:
            raise RuntimeError("не удалось выбрать свободный order_id")
    finally:
        con.close()
    return get(order_id, path)


def day_start(now=None):
    d = datetime.fromtimestamp(time.time() if now is None else now, MSK)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def used_today(now=None, path=None):
    """USDT-оценка списаний за календарный день МСК: все выплаты, кроме точно отклонённых и окончательно не прошедших
    (в том числе готовящиеся, в обработке и с неясным исходом)."""
    marks = ",".join("?" * len(COUNTED))
    rows = _rows(f"SELECT usdt_value FROM payouts WHERE created_ts>=? AND state IN ({marks})",
                 (day_start(now), *COUNTED), path)
    return sum((_dec(r["usdt_value"]) or Decimal(0) for r in rows), Decimal(0))


def check_limits(usdt, now=None, path=None):
    """Причина отказа по лимитам или None."""
    max_one, daily = limits()
    if usdt > max_one:
        return f"больше лимита одной выплаты: ≈{usdt} USDT, PAYOUT_MAX_ONE={fmt(max_one)}"
    used = used_today(now, path)
    if used + usdt > daily:
        return (f"дневной лимит: сегодня (МСК) уже {fmt(used)} из {fmt(daily)} USDT, "
                f"эта выплата ≈{usdt} USDT")
    return None


def pending(now=None, path=None):
    """Незавершённые выплаты (sent/unknown) не старше POLL_DAYS — их опрашивает poll."""
    marks = ",".join("?" * len(PENDING))
    since = (time.time() if now is None else now) - POLL_DAYS * 86400
    return _rows(f"SELECT * FROM payouts WHERE state IN ({marks}) AND created_ts>=? ORDER BY id", (*PENDING, since),
                 path)


def history(limit=10, path=None):
    return _rows("SELECT * FROM payouts ORDER BY id DESC LIMIT ?", (limit,), path)


def resume(path=None):
    """При старте: выплаты, прерванные перезапуском посреди отправки (prepared/sending), — в «исход неясен»: их
    статус выяснит poll по order_id. Возвращает эти строки (для сообщения владельцу)."""
    rows = _rows("SELECT order_id FROM payouts WHERE state IN ('prepared', 'sending')", (), path)
    return [_update(r["order_id"], path, state="unknown", note="бот перезапустился во время отправки") for r in rows]


# --- предпросмотр, отправка, опрос ---

async def quote(s, entry, amount, creds=None, now=None):
    """Предпросмотр: живая комиссия (/v1/payout/services), мин/макс, списание, USDT-оценка и лимиты.
    (данные, None) или (None, причина)."""
    if not enabled():
        return None, OFF
    creds = creds or credentials()
    if not creds:
        return None, "нет ключа выплат"
    svc, why = await service(s, creds, entry)
    fee, why = (None, why) if why else service_fee(svc, entry, amount)
    if why:
        return None, why
    debit = amount + fee
    rate = await usdt_rate(s, entry["currency"])
    if rate is None:
        return None, f"нет курса {entry['currency']}→USDT у Cryptomus — без него лимит не проверить"
    why = _rate_band_error(entry["currency"], rate)
    if why:
        return None, why
    usdt = _usdt_value(debit, rate)
    if usdt is None:
        return None, f"оценка выплаты в USDT по курсу {_rate_text(rate)} не считается — без неё лимит не проверить"
    why = check_limits(usdt, now)
    if why:
        return None, why
    max_one, daily = limits()
    unknown = sum(r["state"] == "unknown" for r in pending(now))
    return {"fee": fee, "debit": debit, "rate": rate, "usdt": usdt, "used": used_today(now), "daily": daily,
            "max_one": max_one, "unknown": unknown}, None


def create_payload(row):
    """Тело POST /v1/payout из строки журнала: только эти поля (без to_currency/from_currency/course_source/priority —
    никаких конвертаций), суммы — строками, комиссия с баланса
    (is_subtract="1": получатель получит ровно amount), memo — только для сети ton."""
    p = {"amount": row["amount"], "currency": CODES.get(row["currency"], row["currency"]), "network": row["network"],
         "order_id": row["order_id"], "address": row["address"], "is_subtract": "1"}
    if row["memo"] and row["network"] == "ton":
        p["memo"] = row["memo"]
    return p


def _ton_account(address):
    """(workchain, hash) TON-адреса в любой форме (EQ…/UQ…, base64 или base64url, 0:hex) или None."""
    if re.fullmatch(r"0:[0-9a-fA-F]{64}", address):
        return 0, bytes.fromhex(address[2:]).hex()
    if re.fullmatch(r"[A-Za-z0-9_+/-]{48}", address):
        raw = base64.urlsafe_b64decode(address.replace("+", "-").replace("/", "_"))
        if _crc16(raw[:34]) == int.from_bytes(raw[34:], "big"):
            return raw[1], raw[2:34].hex()
    return None


def _same_address(network, got, want):
    """EVM — без учёта регистра (hex), TON — тот же аккаунт в любой форме записи, остальное — побайтно."""
    kind = NETWORKS.get(network)
    if kind == "evm":
        return got.lower() == want.lower()
    if kind == "ton":
        return got == want or (_ton_account(got) is not None and _ton_account(got) == _ton_account(want))
    return got == want


def _mismatch(row, res):
    """Поля ответа Cryptomus, не совпавшие с заявкой (пусто — совпало). Сеть и монета — без учёта регистра, сумма —
    как число ("3" = "3.00000000"). memo Cryptomus не возвращает — его держим сами."""
    bad = []
    if not _same_address(row["network"], str(res.get("address") or ""), row["address"]):
        bad.append("адрес")
    if str(res.get("currency") or "").upper() != CODES.get(row["currency"], row["currency"]):
        bad.append("монета")
    if str(res.get("network") or "").lower() != row["network"]:
        bad.append("сеть")
    if _dec(res.get("amount")) != Decimal(row["amount"]):
        bad.append("сумма")
    return bad


def _apply(row, res):
    """Ответ Cryptomus по выплате (create или info) -> строка журнала и событие: "paid", "failed", "found",
    "stuck" (fail/cancel/system_fail без is_final — только через поддержку), "mismatch", "mismatch_final" (итог по
    выплате с несовпадением) или None (для владельца ничего нового). Каждое событие — один раз."""
    status = str(res.get("status") or "").lower()
    final = _true(res.get("is_final"))
    fields = {"uuid": str(res.get("uuid") or row["uuid"] or ""), "status": status, "is_final": int(final),
              "txid": str(res.get("txid") or row["txid"] or "")}
    bad = _mismatch(row, res)
    if bad:   # в лимите остаётся ("unknown"); владелец проверяет выплату в кабинете
        note = f"{MISMATCH}: {', '.join(bad)}"
        if row["state"] != "unknown" or row["note"] != note:
            event = "mismatch"
        elif final and not row["is_final"]:
            event = "mismatch_final"
        else:
            event = None
        return _update(row["order_id"], state="unknown", note=note, **fields), event
    if final and status == "paid":
        return _update(row["order_id"], state="final_paid", **fields), "paid"
    if final and status in FAIL_STATUSES:
        return _update(row["order_id"], state="final_failed", **fields), "failed"
    if status in FAIL_STATUSES and row["status"] != status:
        event = "stuck"   # не итог: в лимите остаётся, опрос продолжается, владелец знает, что нужна поддержка
    else:
        event = "found" if row["state"] == "unknown" else None
    return _update(row["order_id"], state="sent", **fields), event


def _not_found(row):
    """/v1/payout/info ответил «не найдено» по выплате с неясным исходом. Создание получало только точные отказы и
    Cryptomus ни разу не выдал uuid — выплаты нет: "rejected" (как в send), событие "rejected". Иначе остаётся
    "unknown" (в лимите) и одно событие "notfound": владелец проверяет кабинет."""
    if row["create_kind"] == "error" and not row["uuid"]:
        return _update(row["order_id"], state="rejected"), "rejected"
    if NOT_FOUND in (row["note"] or ""):
        return row, None
    return _update(row["order_id"], note=f"{row['note']}; {NOT_FOUND}" if row["note"] else NOT_FOUND), "notfound"


async def _info(s, creds, order_id):
    """Статус выплаты по order_id: ("ok", result, ""), ("notfound", None, текст) или ("error"/"ambiguous", None, текст).
    «Не найдено» — только отказ с «not found» в тексте или HTTP 404 с таким же JSON-отказом."""
    try:
        status, j = await payout_call(s, "POST", "/v1/payout/info", {"order_id": order_id}, creds=creds)
    except Exception as e:
        return "ambiguous", None, accounts.api_error_text(e)
    kind, res, msg = _classify(status, j, creds)
    if kind == "error" and re.search(r"not\s*found", msg, re.I):
        return "notfound", None, msg
    return kind, res, msg


async def _create(s, creds, body):
    try:
        status, j = await payout_call(s, "POST", "/v1/payout", creds=creds, body=body)
    except Exception as e:   # таймаут, обрыв соединения — выплата могла и уйти
        return "ambiguous", None, accounts.api_error_text(e)
    return _classify(status, j, creds)


def _result(state, row=None, reason="", event=None):
    return {"state": state, "row": row, "reason": reason, "event": event}


def _fresh_value(entry, amount, fee, rate, q):
    """USDT-оценка выплаты по свежему курсу перед самой отправкой: (оценка для лимитов и журнала, None) или (None,
    причина отказа). Сначала курс: вне полосы PAYOUT_RATE_BANDS — отказ (крошечный курс, одинаковый в предпросмотре и
    сейчас, иначе прошёл бы проверку сдвига с оценкой ≈0 USDT); сдвинулся больше PAYOUT_RATE_DRIFT против
    предпросмотра — отказ (владелец подтверждал другую сумму в USDT); потом лимиты — по бо́льшей из оценок (предпросмотр
    или свежая), с учётом всех выплат дня."""
    cur, again = entry["currency"], "проверь выплату заново с новой оценкой: /payout"
    if rate is None:
        return None, f"нет курса {cur}→USDT у Cryptomus — без него лимит не проверить, {again}"
    why = _rate_band_error(cur, rate)
    if why:
        return None, why
    # арифметика, которая не помещается в Decimal, — это сдвиг курса (отказ, а не «сбой бота, исход неясен»)
    was, drift = q.get("rate"), rate_drift()
    now = _usdt_value(amount + fee, rate)
    try:
        moved = not isinstance(was, Decimal) or was <= 0 or abs(rate - was) > was * drift
    except DecimalException:
        moved = True
    if moved:
        value = "" if now is None else f", выплата теперь ≈{now} USDT"
        return None, (f"курс {cur}→USDT сдвинулся с предпросмотра: "
                      f"{_rate_text(was) if isinstance(was, Decimal) else '?'} → {_rate_text(rate)} "
                      f"(допуск PAYOUT_RATE_DRIFT={_rate_text(drift * 100)}%){value} — {again}")
    if now is None:
        return None, f"оценка выплаты в USDT по курсу {_rate_text(rate)} не считается — {again}"
    usdt = max(q["usdt"], now)
    why = check_limits(usdt)
    if why:
        return None, f"{why} (по курсу на момент отправки) — {again}"
    return usdt, None


def _stopped(order_id, msg):
    """«⛔ Стоп» во время разбора неясного исхода: больше ничего не шлём; выплата — "unknown" (в лимите), разберёт poll."""
    row = _update(order_id, note=f"{msg}; выплаты выключены — повтор не отправлен, статус выяснит опрос")
    logger.warning("выплата %s: выплаты выключены — повтор не отправлен", order_id)
    return _result("unknown", row, row["note"])


async def send(s, entry, amount, q, creds=None):
    """Отправить выплату, подтверждённую владельцем. Один вызов — не больше одного нового order_id.

    Перед отправкой (под общим замком) заново: сервис Cryptomus доступен, сумма в его мин/макс, комиссия не выросла
    против предпросмотра, свежий курс монеты к USDT; затем без пауз до самого POST — выключатель и Стоп, запись белого
    списка та же, курс в полосе PAYOUT_RATE_BANDS и не ушёл дальше PAYOUT_RATE_DRIFT, лимиты по свежему курсу с учётом
    всех выплат дня. Намерение — в журнал (prepared), потом POST /v1/payout (sending):
    - HTTP 200, state 0, ответ совпал с заявкой — "sent" (или сразу итог); не совпал — "unknown" + тревога;
    - отказ — /v1/payout/info по order_id: «не найдено» — "rejected" (в лимит не идёт), нашлась — принимаем;
    - неясный исход — "unknown", пауза, /v1/payout/info: нашлась — принимаем; «не найдено» — повтор с тем же order_id
      и теми же байтами (не больше MAX_RESEND раз); иначе "unknown" (в лимите), разберёт poll.
    «⛔ Стоп» (disable) с начала вызова — после него не начинается ни один запрос (уже начатый POST не отменить — его
    исход выяснит poll): выключатель и счётчик Стопа проверяются после каждого await и сразу перед каждым POST, а паузу
    перед /info и повтором Стоп прерывает; выплата остаётся "unknown" (в лимите).
    Возвращает {"state": refused|rejected|sent|unknown|final_paid|final_failed, "row", "reason", "event"}."""
    gen = _stop["n"]   # Стоп после этой точки — даже пока ждём замок — останавливает и эту отправку
    async with _send_lock():
        if _halted(gen):
            return _result("refused", reason=OFF)
        creds = creds or credentials()
        if not creds:
            return _result("refused", reason="нет ключа выплат")
        svc, why = await service(s, creds, entry)
        if _halted(gen):   # Стоп, пока шёл запрос сервисов, — и курс уже не запрашиваем
            return _result("refused", reason=OFF)
        fee, why = (None, why) if why else service_fee(svc, entry, amount)
        if why:
            return _result("refused", reason=why)
        if fee > q["fee"]:
            return _result("refused", reason=f"комиссия Cryptomus выросла: {fmt(q['fee'])} → {fmt(fee)} "
                                             f"{entry['currency']}, проверь выплату заново")
        rate = await usdt_rate(s, entry["currency"])   # оценка предпросмотра могла устареть: курс — заново
        # дальше до POST — без await: выключатель, белый список, курс и лимиты проверены прямо перед отправкой
        if _halted(gen):
            return _result("refused", reason=OFF)
        if whitelist_entry(entry["id"]) != entry:
            return _result("refused", reason="запись белого списка изменилась или удалена")
        usdt, why = _fresh_value(entry, amount, fee, rate, q)
        if why:
            return _result("refused", reason=why)
        row = _insert_intent(entry, amount, dict(q, usdt=usdt))
        order_id = row["order_id"]
        body = payout_body(create_payload(row))
        _update(order_id, state="sending")
        logger.info("выплата %s: отправка", order_id)
        kind, res, msg = await _create(s, creds, body)
        posts, ambiguous = 1, False
        while True:
            if kind == "ok":
                row, event = _apply(get(order_id), res)
                logger.info("выплата %s: %s", order_id, row["state"])
                return _result(row["state"], row, row["note"], event)
            ambiguous = ambiguous or kind == "ambiguous"
            _update(order_id, state="unknown", note=msg, create_kind="ambiguous" if ambiguous else "error")
            logger.warning("выплата %s: %s (%s)", order_id, "исход неясен" if kind == "ambiguous" else "отказ", msg)
            # первый запрос точно закончился; после неясного исхода даём Cryptomus время. Стоп — больше ничего не шлём
            if await _pause(RETRY_DELAY * posts if kind == "ambiguous" else 0, gen):
                return _stopped(order_id, msg)
            ikind, ires, imsg = await _info(s, creds, order_id)
            if ikind == "ok":
                kind, res = "ok", ires
                continue
            if ikind == "notfound" and kind == "error" and not ambiguous:
                row = _update(order_id, state="rejected", note=msg)
                logger.info("выплата %s: отклонена", order_id)
                return _result("rejected", row, msg)
            if ikind != "notfound" or kind == "error" or posts > MAX_RESEND:
                row = get(order_id)   # отказ после неясного исхода не доказывает, что первая попытка не прошла
                return _result("unknown", row, row["note"])
            if await _pause(RETRY_DELAY * posts, gen):   # выключатель — до паузы и сразу после неё, перед самым POST
                return _stopped(order_id, msg)
            kind, res, msg = await _create(s, creds, body)   # тот же order_id, те же байты
            posts += 1


async def poll(s, creds=None):
    """Опрос незавершённых выплат (sent/unknown) через /v1/payout/info по order_id: [(событие, строка)].
    Успех — только status paid и is_final; окончательный провал — is_final и fail/cancel/system_fail; fail без
    is_final — ещё в обработке (событие "stuck" один раз). «Не найдено» по выплате с неясным исходом — _not_found;
    ошибки — без изменений. Повторов отправки тут нет."""
    if not pending():
        return []
    creds = creds or credentials()
    if not creds:
        return []
    events = []
    async with _send_lock():
        for row in pending():
            kind, res, _ = await _info(s, creds, row["order_id"])
            if kind == "ok":
                row, event = _apply(row, res)
            elif kind == "notfound" and row["state"] == "unknown":
                row, event = _not_found(row)
            else:
                continue
            if event:
                events.append((event, row))
    return events
