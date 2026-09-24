"""P2P scanner: Bybit, HTX, KuCoin, MEXC, BitPapa + обменники BestChange (public endpoints, no API keys).

Монеты USDT/USDC/BTC/ETH/TON за рубли, связки P2P↔спот через USDT, сравнение сетей у обменников.
One-off snapshot:  python p2p.py
"""
import asyncio
import dataclasses
import html
import io
import json
import logging
import os
import re
import statistics
import sys
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

import aiohttp

import fees
import netstatus

import blacklist
import trades

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
HTX_FIAT = {"RUB": 11}
HTX_COIN = {"USDT": 2, "BTC": 1, "ETH": 3}
DEFAULT_EXCLUDE = "mobile top-up,cash,наличн,чат,chat,api,qr"  # пополнение телефона, наличные, реквизиты «в чат», шлюзы
ALL_EXCHANGES = "bybit,mexc,htx,kucoin,bitpapa,bestchange"
DEFAULT_ASSETS = "USDT,USDC,BTC,ETH,TON"
DEFAULT_FEES = "USDT:1,USDC:1,BTC:0.0002,ETH:0.001,TON:0.05"   # комиссия вывода по умолчанию, в единицах монеты
# Комиссии вывода по биржам и сетям, в монете (агрегаторы Yieldo 31.07.2026 и ChainCost 01.2026 — сверять на бирже).
# Для бирж не из таблицы берётся DEFAULT_FEES/TRANSFER_FEES.
WITHDRAW = fees.table()   # fees.json: биржа → монета → сеть → комиссия; /fees показывает таблицу и возраст данных
# Сети, которые точно принимает площадка-получатель (не подтверждено иное — только TRC20). Нет в списке — любые.
RECEIVE_NETS = {"BitPapa": ("TRC20",)}
SPOT_VENUES = ("Bybit", "MEXC", "HTX", "KuCoin")   # порядок = приоритет ориентира и спота, если монета лежит не на бирже
DEFAULT_SPOT_FEES = "Bybit:0.1,MEXC:0.1,HTX:0.2,KuCoin:0.1"   # % тейкер-комиссии спота
DEFAULT_RISK = "BTC:0.3,ETH:0.5,TON:0.7"          # % запаса на движение курса, пока идут сделки и переводы
MAKER_TICK = 0.01   # шаг цены объявления (₽ за монету), чтобы обогнать текущее первое на 1 позицию
# % мейкера, который биржа берёт именно с объявления (сверх обычных издержек маршрута); площадка -> тип
# объявления ("buy_ad" — я покупаю монету, "sell_ad" — я продаю) -> %. Нет записи — 0.
MAKER_FEE = {"Bybit": {"buy_ad": 0.3}}   # Bybit берёт 0.3% с рублёвых объявлений на покупку
# BestChange: берём все рублёвые банки и карты (тип 2/3 в bm_cy.dat), кроме наличных/QR/юрлиц.
# Основные — латиницей, чтобы совпадали с названиями на P2P-биржах и в INCLUDE_PAY.
BC_BANKS = {"Сбербанк RUB": "Sberbank", "Т-Банк RUB": "T-Bank", "Альфа-Банк RUB": "Alfa-bank", "СБП RUB": "SBP"}
BC_SKIP = ("cash-in", "QR", "компании", "ATM")
BC_COINS = {  # имя в bm_cy.dat -> (монета, сеть)
    "Tether TRC20 (USDT)": ("USDT", "TRC20"), "Tether BEP20 (USDT)": ("USDT", "BEP20"),
    "Tether TON (USDT)": ("USDT", "TON"), "Tether SOL (USDT)": ("USDT", "SOL"),
    "Tether ERC20 (USDT)": ("USDT", "ERC20"), "Tether POLYGON (USDT)": ("USDT", "POLYGON"),
    "Tether ARBITRUM (USDT)": ("USDT", "ARBITRUM"),
    "USDC TRC20 (USDC)": ("USDC", "TRC20"), "USDC BEP20 (USDC)": ("USDC", "BEP20"),
    "USDC ERC20 (USDC)": ("USDC", "ERC20"), "USDC SOL (USDC)": ("USDC", "SOL"),
    "Bitcoin (BTC)": ("BTC", "BTC"), "Ethereum (ETH)": ("ETH", "ERC20"),
}
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
LOG_PATH = os.path.join(HERE, "logs", "bot.log")   # ротация 5 x 1 МБ; хвост читает Bot.logs_view (/logs)

logger = logging.getLogger(__name__)
_log_handlers = []   # хендлеры, поставленные setup_logging — чтобы повторный вызов не плодил дубликаты


def setup_logging(path=LOG_PATH):
    """Логи в файл с ротацией (5 x 1 МБ, 5 бэкапов) + консоль, вместо print. Можно звать повторно
    (например, из тестов с другим path) — старые хендлеры этой функции снимаются и закрываются."""
    root = logging.getLogger()
    for h in _log_handlers:
        root.removeHandler(h)
        h.close()
    _log_handlers.clear()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%d.%m %H:%M:%S")
    file_h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_h.setFormatter(fmt)
    console_h = logging.StreamHandler(sys.stdout)
    console_h.setFormatter(fmt)
    root.setLevel(logging.INFO)
    root.addHandler(file_h)
    root.addHandler(console_h)
    _log_handlers.extend([file_h, console_h])


def load_env(path=ENV_PATH):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip())


def _list(name, default=""):
    return [x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip()]


def _fees(spec, upper=True):
    out = {}
    for part in spec.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().upper() if upper else k.strip()] = float(v)
    return out


AMOUNT_MIN, AMOUNT_MAX = 1000, 5_000_000
_AMOUNT_UNITS = {"млн": 1_000_000, "m": 1_000_000, "тыс": 1_000, "к": 1_000, "k": 1_000}


def parse_amount(text):
    """Сумма круга из текста: «20000», «20 000», «20к», «1,5 млн». None — не разобрано или вне
    диапазона 1 000–5 000 000 ₽."""
    t = re.sub(r"\s+", "", (text or "").strip().lower())
    m = re.fullmatch(r"([\d.,]+)(млн\.?|тыс\.?|к|k|m)?", t)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    num *= _AMOUNT_UNITS.get((m.group(2) or "").rstrip("."), 1)
    return num if AMOUNT_MIN <= num <= AMOUNT_MAX else None


MIN_PROFIT_MIN, MIN_PROFIT_MAX = 0.1, 100   # порог сигнала, % чистыми


def parse_min_profit(text):
    """Порог сигнала из текста: «2», «1,5», «1.5 %». None — не разобрано или вне диапазона 0,1–100%
    (как и в parse_amount, regex не пропускает nan/inf/минус/экспоненту)."""
    m = re.fullmatch(r"([\d.,]+)%?", re.sub(r"\s+", "", (text or "").strip()))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return num if MIN_PROFIT_MIN <= num <= MIN_PROFIT_MAX else None


def _env_parsed(name, parse, default):
    """Сумма/порог из .env тем же парсером, что и команды бота: испорченное значение (0, nan, inf…)
    не должно пережить рестарт — берём значение по умолчанию и пишем предупреждение в лог."""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = parse(raw)
    if value is None:
        logger.warning("%s=%s в .env не подходит, использую %g", name, raw, default)
        return default
    return value


@dataclass
class Config:
    fiat: str = "RUB"
    amount: float = 50000          # сумма одного круга в фиате
    min_profit: float = 1.0        # % чистыми, порог сигнала
    min_orders: int = 100          # мин. сделок у мерчанта
    min_rate: float = 95.0         # мин. % завершённых сделок
    max_dev: float = 4.0           # % от биржевого курса; дальше — аномалия, отсев
    interval: int = 20             # сек между сканами
    alt_interval: int = 60         # сек между опросами монет кроме USDT
    bc_refresh: int = 120          # сек между скачиваниями выгрузки BestChange (~16 МБ)
    pay_fee: float = 0.0           # % комиссии банка за оплату продавцу (СБП сверх 100 тыс./мес — до 0.5%)
    risk_penalty: float = 1.5      # штраф в п.п. профита за каждую причину риска при сортировке связок
    spot_fees: dict = field(default_factory=lambda: _fees(DEFAULT_SPOT_FEES, upper=False))
    risk_buffer: dict = field(default_factory=lambda: _fees(DEFAULT_RISK))
    assets: list = field(default_factory=lambda: DEFAULT_ASSETS.split(","))
    transfer_fees: dict = field(default_factory=lambda: _fees(DEFAULT_FEES))
    exchanges: list = field(default_factory=lambda: ALL_EXCHANGES.split(","))
    include_pay: list = field(default_factory=list)
    exclude_pay: list = field(default_factory=lambda: DEFAULT_EXCLUDE.split(","))
    same_venue_only: bool = False  # True — только связки внутри одной площадки (пресет «USDT без переводов»)

    @classmethod
    def from_env(cls):
        fees = _fees(os.getenv("TRANSFER_FEES", DEFAULT_FEES))
        if "TRANSFER_FEES" not in os.environ and os.getenv("TRANSFER_FEE"):
            fees["USDT"] = float(os.getenv("TRANSFER_FEE"))
        return cls(
            fiat=os.getenv("FIAT", "RUB").upper(),
            amount=_env_parsed("AMOUNT", parse_amount, 50000),
            min_profit=_env_parsed("MIN_PROFIT", parse_min_profit, 1.0),
            min_orders=int(os.getenv("MIN_ORDERS", 100)),
            min_rate=float(os.getenv("MIN_RATE", 95)),
            max_dev=float(os.getenv("MAX_DEV", 4)),
            interval=int(os.getenv("INTERVAL", 20)),
            alt_interval=int(os.getenv("ALT_INTERVAL", 60)),
            bc_refresh=int(os.getenv("BC_REFRESH", 120)),
            pay_fee=float(os.getenv("PAY_FEE", 0)),
            risk_penalty=float(os.getenv("RISK_PENALTY", 1.5)),
            spot_fees=_fees(os.getenv("SPOT_FEES", DEFAULT_SPOT_FEES), upper=False),
            risk_buffer=_fees(os.getenv("RISK_BUFFER", DEFAULT_RISK)),
            assets=[a.upper() for a in _list("ASSETS", DEFAULT_ASSETS)],
            transfer_fees=fees,
            exchanges=_list("EXCHANGES", ALL_EXCHANGES),
            include_pay=_list("INCLUDE_PAY"),
            exclude_pay=_list("EXCLUDE_PAY", DEFAULT_EXCLUDE),
            same_venue_only=os.getenv("SAME_VENUE_ONLY", "0").strip().lower() in ("1", "true", "yes", "on"),
        )


@dataclass
class Ad:
    ex: str
    side: str        # "buy": мы покупаем монету у объявления; "sell": продаём ему
    price: float     # фиат за 1 монету
    min_amt: float   # лимиты в фиате
    max_amt: float
    avail: float     # сколько монеты доступно
    pays: list
    nick: str
    orders: int
    rate: float      # % завершённых сделок
    url: str = ""
    asset: str = "USDT"
    net: str = ""    # сеть (только у обменников)
    terms: str = ""  # условия мерчанта из объявления (remark/remarks/tradeTerms/conditions)
    all_pays: list = None  # исходные способы оплаты до фильтра _pays (объявление живёт в кэше _alt несколько сканов)
    parts: int = 1   # из скольких объявлений стакана собрано (_combined) — обменникам нужен свой перевод на каждого


async def _json(s, method, url, body=None):
    async with s.request(method, url, json=body, headers=HEADERS) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


_bybit_pay = {}


async def bybit(s, cfg, side, asset):
    if not _bybit_pay:
        j = await _json(s, "POST", "https://api2.bybit.com/fiat/otc/configuration/queryAllPaymentList", {})
        _bybit_pay.update({str(p["paymentType"]): p["paymentName"] for p in j["result"]["paymentConfigVo"]})
    body = {"userId": "", "tokenId": asset, "currencyId": cfg.fiat, "payment": [], "side": "1" if side == "buy" else "0",
            "size": "20", "page": "1", "amount": str(int(cfg.amount)), "authMaker": False, "canTrade": False}
    j = await _json(s, "POST", "https://api2.bybit.com/fiat/otc/item/online", body)
    return [Ad("Bybit", side, float(i["price"]), float(i["minAmount"]), float(i["maxAmount"]), float(i["lastQuantity"]),
               [_bybit_pay.get(p, p) for p in i["payments"]], i["nickName"], int(i["recentOrderNum"]),
               float(i["recentExecuteRate"]), asset=asset, terms=i.get("remark") or "")
            for i in j["result"]["items"] or []]


async def htx(s, cfg, side, asset):
    cur, coin = HTX_FIAT.get(cfg.fiat), HTX_COIN.get(asset)
    if cur is None or coin is None:
        return []
    url = ("https://www.htx.com/-/x/otc/v1/data/trade-market?coinId=%d&currency=%d&tradeType=%s&currPage=1&payMethod=0"
           "&acceptOrder=0&blockType=general&online=1&range=0&amount=%d&onlyTradable=false&isFollowed=false"
           % (coin, cur, "sell" if side == "buy" else "buy", cfg.amount))
    j = await _json(s, "GET", url)
    return [Ad("HTX", side, float(i["price"]), float(i["minTradeLimit"]), float(i["maxTradeLimit"]), float(i["tradeCount"]),
               [p["name"] for p in i["payMethods"]], i["userName"], int(i["tradeMonthTimes"]),
               float(i["orderCompleteRate"] or 0), asset=asset)
            for i in j.get("data") or []]


def _kucoin_pay(p):
    if p["payTypeCode"] == "OTHER" and p.get("reservedFields"):
        return json.loads(p["reservedFields"]).get("payTypeName", "Other")
    return p["payTypeNameEn"]


async def kucoin(s, cfg, side, asset):
    url = (f"https://www.kucoin.com/_api/otc/ad/list?currency={asset}&side={'SELL' if side == 'buy' else 'BUY'}"
           f"&legal={cfg.fiat}&page=1&pageSize=20&status=PUTUP&lang=en_US")
    j = await _json(s, "GET", url)
    return [Ad("KuCoin", side, float(i["floatPrice"]), float(i["limitMinQuote"]), float(i["limitMaxQuote"]),
               float(i["currencyBalanceQuantity"]), [_kucoin_pay(p) for p in i["adPayTypes"]], i["nickName"],
               int(i.get("dealOrderNum") or 0), float((i.get("dealOrderRate") or "0").rstrip("%")), asset=asset,
               terms=i.get("remarks") or "")
            for i in j.get("items") or []]


_mexc_pay = {}
_mexc_coins = {}


async def mexc(s, cfg, side, asset):
    if not _mexc_pay:
        j = await _json(s, "GET", f"https://www.mexc.com/api/platform/p2p/api/payment/method?currency={cfg.fiat}")
        _mexc_pay.update({str(p["id"]): p["name"] for p in j["data"]})
    if not _mexc_coins:
        j = await _json(s, "GET", "https://www.mexc.com/api/platform/p2p/api/common/coins")
        _mexc_coins.update({c["coinName"]: c["coinId"] for c in j["data"]})
    coin = _mexc_coins.get(asset)
    if not coin:
        return []
    url = ("https://p2p.mexc.com/api/market?adsType=1&allowTrade=false&blockTrade=false&countryCode=&follow=false"
           f"&haveTrade=false&payMethod=&coinId={coin}&currency={cfg.fiat}&amount={int(cfg.amount)}&page=1&pageSize=50"
           + ("&tradeType=SELL&adOrderSortField=price&adOrderSort=0" if side == "buy" else "&tradeType=BUY"))
    j = await _json(s, "GET", url)
    out = []
    for i in j.get("data") or []:
        st = i.get("merchantStatistics") or {}
        out.append(Ad("MEXC", side, float(i["price"]), float(i["minTradeLimit"]), float(i["maxTradeLimit"]),
                      float(i["availableQuantity"]), [_mexc_pay.get(p, f"pm{p}") for p in str(i["payMethod"]).split(",")],
                      (i.get("merchant") or {}).get("nickName", "?"), int(st.get("doneLastMonthCount") or 0),
                      float(st.get("completeRate") or 0) * 100, asset=asset, terms=i.get("tradeTerms") or ""))
    return out


async def bitpapa(s, cfg, side, asset):
    url = ("https://bitpapa.com/api/v1/pro/search?crypto_currency_code=%s&currency_code=%s&page=1&limit=50&type=%s&sort=%s"
           % (asset, cfg.fiat, "sell" if side == "buy" else "buy", "price" if side == "buy" else "-price"))
    j = await _json(s, "GET", url)
    out = []
    for a in j.get("ads") or []:
        u = a.get("user") or {}
        if u.get("is_suspicious"):
            continue
        trade_count, done = u.get("trades_count") or 0, u.get("completed_trades_count") or 0
        terms = a.get("conditions") or ""
        if a.get("for_identified_people"):
            terms += " [только верифицированные]"
        out.append(Ad("BitPapa", side, float(a["price"]), float(a["limit_min"] or 0), float(a["limit_max"] or 0),
                      float(a["limit_max_crypto"] or 0), [a["payment_method"]["name"]], u.get("user_name", "?"),
                      done, done / trade_count * 100 if trade_count else 0, asset=asset, terms=terms.strip()))
    return out


_bc = {"t": 0.0, "ads": []}
_bc_lock = asyncio.Lock()


def _bc_parse(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    cy = {}
    for line in z.read("bm_cy.dat").decode("cp1251").splitlines():
        f = line.split(";")
        cy[f[0]] = f
    exch = {}
    for line in z.read("bm_exch.dat").decode("cp1251").splitlines():
        f = line.split(";")
        exch[f[0]] = f[1]
    coins = {k: BC_COINS[f[2]] for k, f in cy.items() if f[2] in BC_COINS}
    banks = {k: BC_BANKS.get(f[2], f[2].removesuffix(" RUB")) for k, f in cy.items()
             if f[4] == "643" and f[5] in ("2", "3") and not any(x in f[2] for x in BC_SKIP)}
    pairs = {f"{c};{b};".encode(): "sell" for c in coins for b in banks}   # мы продаём монету обменнику
    pairs.update({f"{b};{c};".encode(): "buy" for c in coins for b in banks})
    ads = []
    with z.open("bm_rates.dat") as rows:
        for line in rows:
            side = pairs.get(line[:line.find(b";", line.find(b";") + 1) + 1])
            if not side:
                continue
            f = line.decode("cp1251").strip().split(";")
            give, recv, reserve, mn, mx = float(f[3]), float(f[4]), float(f[5]), float(f[8]), float(f[9]) or 1e12
            bad, _, good = f[6].partition(".")
            bad, good = int(bad or 0), int(good or 0)
            c, b = (f[0], f[1]) if side == "sell" else (f[1], f[0])
            if side == "sell":   # лимиты в монете, резерв в рублях
                price = recv / give
                mn, mx, avail = mn * price, mx * price, reserve / price
            else:                # лимиты в рублях, резерв в монете
                price, avail = give / recv, reserve
            asset, net = coins[c]
            ads.append(Ad("BestChange", side, price, mn, mx, avail, [banks[b]], f"{exch.get(f[2], f[2])} [{net}]",
                          good, good / (good + bad) * 100 if good + bad else 0,
                          f"https://www.bestchange.ru/click.php?id={f[2]}&from={f[0]}&to={f[1]}&city=0", asset, net))
    return ads


async def bestchange(s, cfg, side, asset):
    async with _bc_lock:   # все стороны и монеты скана делят одну выгрузку
        now = time.time()
        # после сбоя не повторяем минуту: иначе каждая пара монета×сторона ждёт свой таймаут
        if now - _bc["t"] > cfg.bc_refresh and now - _bc.get("tried", 0) > 60:
            _bc["tried"] = now
            async with s.get("http://api.bestchange.ru/info.zip", headers=HEADERS,
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                r.raise_for_status()
                data = await r.read()
            _bc["ads"] = await asyncio.to_thread(_bc_parse, data)
            _bc["t"] = time.time()
    return [a for a in _bc["ads"] if a.side == side and a.asset == asset]


FETCHERS = {"bybit": bybit, "htx": htx, "kucoin": kucoin, "mexc": mexc, "bitpapa": bitpapa, "bestchange": bestchange}


async def rapira_mid(s):
    j = await _json(s, "GET", "https://api.rapira.net/open/market/rates")
    r = next(x for x in j["data"] if x["symbol"] == "USDT/RUB")
    return (r["askPrice"] + r["bidPrice"]) / 2


def _spot_fee(cfg, venue):
    return cfg.spot_fees.get(venue, _fees(DEFAULT_SPOT_FEES, upper=False).get(venue, 0.1))


# публичные тикеры спота: (URL, список тикеров из ответа, ключ символа, bid, ask, как записан символ ETH/USDT)
SPOT_SOURCES = {
    "Bybit": ("https://api.bybit.com/v5/market/tickers?category=spot",
              lambda j: j["result"]["list"], "symbol", "bid1Price", "ask1Price", lambda a: a + "USDT"),
    "MEXC": ("https://api.mexc.com/api/v3/ticker/bookTicker",
             lambda j: j, "symbol", "bidPrice", "askPrice", lambda a: a + "USDT"),
    "HTX": ("https://api.htx.com/market/tickers",
            lambda j: j["data"], "symbol", "bid", "ask", lambda a: (a + "usdt").lower()),
    "KuCoin": ("https://api.kucoin.com/api/v1/market/allTickers",
               lambda j: j["data"]["ticker"], "symbol", "buy", "sell", lambda a: a + "-USDT"),
}


async def spot_prices(s, assets):
    """{площадка: {монета: (bid, ask)}} к USDT на споте Bybit, MEXC, HTX, KuCoin (одним запросом на биржу)."""
    res = await asyncio.gather(*(_json(s, "GET", SPOT_SOURCES[v][0]) for v in SPOT_VENUES), return_exceptions=True)
    out, errors = {}, []
    for venue, j in zip(SPOT_VENUES, res):
        out[venue] = {"USDT": (1.0, 1.0)}
        if isinstance(j, Exception):
            errors.append(j)
            continue
        _, items, k_sym, k_bid, k_ask, sym = SPOT_SOURCES[venue]
        try:
            t = {x[k_sym]: x for x in items(j)}
        except (KeyError, TypeError) as e:   # формат ответа изменился — площадка без спота, остальные работают
            errors.append(e)
            continue
        for a in assets:
            x = t.get(sym(a))
            if not x:
                continue
            try:   # битый тикер (не число в bid/ask) — пропустить монету, площадку не ронять
                bid, ask = float(x.get(k_bid) or 0), float(x.get(k_ask) or 0)
            except (TypeError, ValueError):
                continue
            if bid > 0 and ask > 0:
                out[venue][a] = (bid, ask)
    if len(errors) == len(SPOT_VENUES):
        raise errors[0]
    return out


def _mid(spot, asset):
    for venue in SPOT_VENUES:
        if asset in spot.get(venue, {}):
            bid, ask = spot[venue][asset]
            return (bid + ask) / 2
    return None


def _pays(a, cfg):
    """Отфильтровать способы оплаты объявления по exclude_pay/include_pay. Объявление из кэша `_alt`
    переживает несколько сканов и фильтруется каждый раз заново (настройки могли поменяться) — поэтому
    считаем всегда от исходного списка `a.all_pays`, а не от уже отфильтрованного `a.pays` из прошлого раза."""
    if a.all_pays is None:
        a.all_pays = a.pays
    pays = [p for p in a.all_pays if not any(x in p.lower() for x in cfg.exclude_pay)]
    if cfg.include_pay:
        pays = [p for p in pays if any(x in p.lower() for x in cfg.include_pay)]
    a.pays = pays
    return pays


# Условия мерчанта. Стоп-фразы — объявление отсеивается; заметки — показываем в карточке.
# Отрицания про третьих лиц («от третьих лиц не принимаю») вырезаются до проверки — это норма, а не риск.
_TERMS_NEG_THIRD = re.compile(r"(не\s+принима\w*\s+(оплат\w*\s+)?от\s+третьих\s+лиц|от\s+третьих\s+лиц\s+не\s+принима\w*"
                              r"|треть\w+\s+лиц\w*\s+не\s+принима\w*|без\s+третьих\s+лиц|третьи\s+лица\s+(мимо|нет|запрещ\w*))", re.I)
TERMS_BLOCK = (
    (r"треть\w+\s+лиц|с\s+любых\s+карт|любые\s+карты|чужих\s+карт|чужие\s+карты", "оплата от третьих лиц"),
    (r"telegram|телеграм|whatsapp|ватсап|вацап|(?<![\w.])@[A-Za-z0-9_]{5,}", "зовёт на связь вне площадки"),
    (r"обнал|дроп\b|дропы|обход\s+(банк|лимит)", "обещает «обнал/обход банка» — серая схема"),
)
TERMS_WARN = (
    (r"без\s+блокировок|никаких\s+блокир", "обещает «без блокировок»"),
    (r"в\s+чат", "реквизиты в чате"),
    (r"\bчек", "нужен чек (на почту/в чат)"),
    (r"одним\s+платеж|не\s+дел[иь]|кратн", "одним платежом / кратные суммы"),
    (r"только\s+(с\s+)?(т[\s-]?банк|тиньк|сбер|альф|втб|райф)", "принимает только с одного банка"),
    (r"сч[её]т\s+ип|на\s+ип\b|юр\.?\s?лиц|расч[её]тн\w*\s+сч", "оплата на счёт ИП/юрлица"),
    (r"верифиц|kyc", "требует верификацию"),
    (r"\+7\s?\(?\d{3}|\b8\s?9\d{2}", "в условиях указан телефон"),
)
TERMS_RISKY = ("реквизиты в чате", "оплата на счёт ИП/юрлица", "в условиях указан телефон")


def terms_flags(text):
    """(стоп-причины, заметки) по тексту условий объявления."""
    t = _TERMS_NEG_THIRD.sub(" ", (text or "").lower())
    blocked = [label for pat, label in TERMS_BLOCK if re.search(pat, t, re.I)]
    notes = [label for pat, label in TERMS_WARN if re.search(pat, t, re.I)]
    return blocked, notes


def usable(a, cfg, blocked=frozenset()):
    return bool(_pays(a, cfg)) and a.min_amt <= cfg.amount <= a.max_amt and a.avail * a.price >= cfg.amount \
        and a.orders >= cfg.min_orders and a.rate >= cfg.min_rate and (a.ex, a.nick) not in blocked \
        and not terms_flags(a.terms)[0]


def _signal_ok(a, cfg, blocked=frozenset()):
    """Фильтр объявления для стакана глубины: мерчант/способ оплаты/условия, без требования, что объём
    покрывает всю сумму в одиночку — это делают _stack (покупка) и _stack_qty (продажа, под фактический
    выход монеты маршрута), складывая несколько объявлений."""
    return bool(_pays(a, cfg)) and a.orders >= cfg.min_orders and a.rate >= cfg.min_rate \
        and (a.ex, a.nick) not in blocked and not terms_flags(a.terms)[0]


def _stack(ads, amount):
    """Сложить объявления по цене (ads отсортированы: лучшая цена первой), пока не наберётся amount
    в фиате — не только верхнее объявление. Возвращает синтетическое Ad со средневзвешенной ценой
    и объединёнными условиями использованных объявлений, None — если суммарной глубины меньше amount."""
    remaining, qty, used = amount, 0.0, []
    for a in ads:
        if remaining <= 0:
            break
        take = min(remaining, a.max_amt, a.avail * a.price)
        if take < a.min_amt:
            continue   # меньше минимума этого объявления — пропускаем, берём из следующего
        remaining -= take
        qty += take / a.price
        used.append(a)
    if remaining > 0.01 or not used:
        return None
    return _combined(used, amount / qty, amount, qty)


def _stack_qty(ads, qty):
    """Сложить объявления продажи (лучшая цена первой), пока они не примут qty монеты — фактический
    выход маршрута, а не сумму круга в фиате: при прибыли монеты на выходе больше, чем сумма / цена.
    Синтетическое Ad: цена — средневзвешенная по проданному объёму, min_amt — выручка в фиате;
    None — если глубины не хватает."""
    if qty is None or qty <= 0:
        return None
    remaining, fiat, used = qty, 0.0, []
    for a in ads:
        if remaining <= 0:
            break
        take = min(remaining, a.avail, a.max_amt / a.price)
        if take * a.price < a.min_amt:
            continue   # меньше минимума этого объявления — пропускаем, берём из следующего
        remaining -= take
        fiat += take * a.price
        used.append(a)
    if remaining > qty * 1e-9 or not used:
        return None
    return _combined(used, fiat / qty, fiat, qty)


def _combined(used, price, total, qty):
    """Синтетическое Ad из использованных объявлений стакана: total — объём в фиате, qty — в монете;
    сеть сохраняем, если она у всех объявлений одна, условия мерчантов объединяем."""
    one = len(used) == 1
    nets = {a.net for a in used}
    terms = "; ".join(dict.fromkeys(a.terms.strip() for a in used if a.terms and a.terms.strip()))
    return Ad(used[0].ex, used[0].side, price, total, sum(a.max_amt for a in used), qty,
              sorted(set(p for a in used for p in a.pays)), used[0].nick if one else f"{len(used)} объявл.",
              min(a.orders for a in used), min(a.rate for a in used), used[0].url if one else "",
              used[0].asset, nets.pop() if len(nets) == 1 else "", terms=terms, parts=len(used))


def _net_parts(grp):
    """Части стакана, которые можно складывать вместе: у обменников — по сетям (монету шлют в сеть
    конкретного обменника), у бирж — весь стакан."""
    if not grp or grp[0].ex != "BestChange":
        return [grp]
    return [[a for a in grp if a.net == n] for n in dict.fromkeys(a.net for a in grp)]


def _same_net(grp, ad):
    """Объявления стакана, которые можно складывать с ad: у обменника — только той же сети."""
    return [a for a in grp if a.net == ad.net] if ad.ex == "BestChange" else grp


def _same_venue(b, s):
    return b.ex == s.ex and b.ex != "BestChange"   # обменники всегда внешние: нужен перевод


def _receive_nets(to, asset):
    """Сети, в которых получатель точно примет asset (RECEIVE_NETS); None — ограничения нет. Ограничение
    касается монет, которые вообще ходят в этих сетях (USDT/USDC в TRC20 — по BC_COINS): BTC или ETH
    в TRC20 не бывает, сети получателя для них не подтверждены — не мешаем."""
    nets = RECEIVE_NETS.get(to)
    return nets if nets and any((asset, n) in BC_COINS.values() for n in nets) else None


def _withdraw(cfg, sender, asset, net="", receiver="", qty=None):
    """Комиссия вывода монеты с биржи и сеть. Сеть задана (её требует обменник) — берём её,
    иначе самую дешёвую из тех, что принимает получатель. None — открытой сети нет: у отправителя
    закрыт вывод или у получателя ввод (по живому справочнику netstatus; неизвестно = не мешаем),
    либо справочник отправителя известен и все его сети закрыты (или нет сети, которую принимает
    получатель), либо сумма вывода (qty) ниже минимума биржи в этой сети. Нет сведений — запасная
    комиссия."""
    table = dict(WITHDRAW.get((sender, asset), {}))
    for n in netstatus.open_nets(sender, asset):       # живой справочник дополняет таблицу и переопределяет её
        fee = netstatus.live_fee(sender, asset, n)
        if fee is not None:
            table[n] = fee

    def ok(n):
        if qty is not None:
            m = netstatus.min_withdraw(sender, asset, n)
            if m is not None and qty < m:
                return False   # меньше минимума биржи — сеть недоступна, как закрытая
        return netstatus.withdraw_ok(sender, asset, n) is not False and \
            (not receiver or netstatus.deposit_ok(receiver, asset, n) is not False)

    need = _receive_nets(receiver, asset)   # ограничение получателя (BitPapa — TRC20) только для монет,
    accepts = (lambda n: n in need) if need else (lambda n: True)   # которые в этой сети реально ходят (BC_COINS)

    if net:
        return (table.get(net, cfg.transfer_fees.get(asset, 0)), net) if ok(net) else None
    allowed = {n: f for n, f in table.items() if accepts(n)}
    if allowed:
        cand = {n: f for n, f in allowed.items() if ok(n)}
        if not cand:
            return None
        best = min(cand, key=cand.get)
        return cand[best], best
    listed = netstatus.known_nets(sender, asset)
    known = [n for n in listed if accepts(n)]
    if known and not any(ok(n) for n in known):
        return None   # справочник есть, и во всех его сетях вывод (или ввод у получателя) закрыт
    if listed and not known and need and set(need) <= set(netstatus.KNOWN_NETS):
        return None   # справочник есть, а сети, которую принимает получатель (BitPapa — TRC20), в нём нет
    return cfg.transfer_fees.get(asset, 0), ""


def _hop(cfg, frm, frm_net, to, to_net, asset, qty=None, parts=1):
    """Перевод монеты между площадками: (комиссия в монете, подпись шага); та же биржа — (0, '');
    (None, '') — перевод невозможен: вывод или ввод в нужной сети закрыт (или сумма qty меньше
    минимума вывода биржи в единственной подходящей сети). parts — из скольких объявлений собран
    получатель (стакан обменников одной сети, см. Ad.parts): каждому нужен свой перевод, комиссия
    вывода умножается на их число."""
    if frm == to and frm != "BestChange":
        return 0.0, ""
    if frm == "BestChange" and to == "BestChange":   # обменник → твой кошелёк на бирже → другой обменник
        if netstatus.deposit_ok("Bybit", asset, frm_net) is False:
            return None, ""                          # на Bybit закрыт ввод в сети первого обменника
        w = _withdraw(cfg, "Bybit", asset, to_net, qty=qty)
        if w is None:
            return None, ""
        fee, net = w
        fee *= parts
        mult = f" ×{parts}" if parts > 1 else ""
        return fee, f"через Bybit: перевод −{fee:g} {asset} ({net}){mult}"
    if frm == "BestChange":                          # обменник сам шлёт монету, комиссия в его курсе
        need = _receive_nets(to, asset)
        if need and frm_net not in need:
            return None, ""                          # получатель не принимает сеть обменника (BitPapa: USDT/USDC — только TRC20)
        if netstatus.deposit_ok(to, asset, frm_net) is False:
            return None, ""                          # у биржи закрыт ввод в сети обменника
        return 0.0, f"обменник шлёт {asset} ({frm_net}) на {to}"
    w = _withdraw(cfg, frm, asset, to_net if to == "BestChange" else "", to, qty=qty)
    if w is None:
        return None, ""
    fee, net = w
    fee *= parts
    mult = f" ×{parts}" if parts > 1 else ""
    cost = f"−{fee:g} {asset}" if fee else f"{asset} без комиссии"
    return fee, f"перевод {cost}" + (f" ({net})" if net else "") + mult + f" на {to}"


def _route_qty(b, s, cfg, spot, over_banks=frozenset(), disable=frozenset()):
    """Количество s.asset на выходе маршрута; None, если связка невозможна. disable — категории
    издержек, которые надо считать нулевыми ('bank'/'withdraw'/'spot'/'risk') — для разложения
    прибыли на составляющие в profit_breakdown."""
    pay_fee = 0.0 if "bank" in disable else cfg.pay_fee
    bank = trades.sbp_bank(b.pays)
    if "bank" not in disable and bank in over_banks and pay_fee < trades.SBP_OVER_FEE:
        pay_fee = trades.SBP_OVER_FEE
    qty = cfg.amount * (1 - pay_fee / 100) / b.price
    # вывод меньше минимума биржи в сети — сеть недоступна (как закрытая); при "withdraw" в disable
    # комиссия и минимум вывода игнорируются (для profit_breakdown без стадии вывода)
    wqty = None if "withdraw" in disable else qty
    # стакан обменников из N объявлений одной сети (_stack_qty) — каждому нужен свой перевод с биржи
    s_parts = s.parts if s.ex == "BestChange" else 1
    if b.asset == s.asset:
        fee, _ = _hop(cfg, b.ex, b.net, s.ex, s.net, b.asset, qty=wqty, parts=s_parts)
        if fee is None:
            return None
        qty -= 0.0 if "withdraw" in disable else fee
    elif "USDT" in (b.asset, s.asset):
        alt = s.asset if b.asset == "USDT" else b.asset
        # спот там, где монета уже лежит: меньше переводов
        venue = next((v for v in (b.ex, s.ex) + SPOT_VENUES if v in SPOT_VENUES and alt in spot.get(v, {})), None)
        if not venue:
            return None
        bid, ask = spot[venue][alt]
        fee, _ = _hop(cfg, b.ex, b.net, venue, "", b.asset, qty=wqty)
        if fee is None:
            return None
        qty -= 0.0 if "withdraw" in disable else fee
        sf = 0.0 if "spot" in disable else _spot_fee(cfg, venue)
        qty = (qty / ask if b.asset == "USDT" else qty * bid) * (1 - sf / 100)
        wqty = None if "withdraw" in disable else qty
        fee, _ = _hop(cfg, venue, "", s.ex, s.net, s.asset, qty=wqty, parts=s_parts)
        if fee is None:
            return None
        qty -= 0.0 if "withdraw" in disable else fee
    else:
        # промежуточная монета: обе стороны — не USDT (например BTC→USDT→ETH), нужны две спот-конвертации;
        # берём площадку, где есть обе пары к USDT — без лишнего перевода самого USDT между биржами
        venue = next((v for v in (b.ex, s.ex) + SPOT_VENUES
                      if v in SPOT_VENUES and b.asset in spot.get(v, {}) and s.asset in spot.get(v, {})), None)
        if venue:
            bid1, _ = spot[venue][b.asset]
            _, ask2 = spot[venue][s.asset]
            fee, _ = _hop(cfg, b.ex, b.net, venue, "", b.asset, qty=wqty)
            if fee is None:
                return None
            qty -= 0.0 if "withdraw" in disable else fee
            sf = 0.0 if "spot" in disable else _spot_fee(cfg, venue)
            qty = qty * bid1 * (1 - sf / 100)     # b.asset → USDT
            qty = (qty / ask2) * (1 - sf / 100)   # USDT → s.asset
            wqty = None if "withdraw" in disable else qty
            fee, _ = _hop(cfg, venue, "", s.ex, s.net, s.asset, qty=wqty, parts=s_parts)
            if fee is None:
                return None
            qty -= 0.0 if "withdraw" in disable else fee
        else:
            # ни одна площадка не держит обе пары: конвертация на каждой своей + перевод USDT между ними
            venue1 = next((v for v in (b.ex,) + SPOT_VENUES if v in SPOT_VENUES and b.asset in spot.get(v, {})), None)
            venue2 = next((v for v in (s.ex,) + SPOT_VENUES if v in SPOT_VENUES and s.asset in spot.get(v, {})), None)
            if not venue1 or not venue2 or venue1 == venue2:
                return None
            bid1, _ = spot[venue1][b.asset]
            _, ask2 = spot[venue2][s.asset]
            fee, _ = _hop(cfg, b.ex, b.net, venue1, "", b.asset, qty=wqty)
            if fee is None:
                return None
            qty -= 0.0 if "withdraw" in disable else fee
            sf1 = 0.0 if "spot" in disable else _spot_fee(cfg, venue1)
            qty = qty * bid1 * (1 - sf1 / 100)    # b.asset → USDT на venue1
            wqty = None if "withdraw" in disable else qty
            fee, _ = _hop(cfg, venue1, "", venue2, "", "USDT", qty=wqty)
            if fee is None:
                return None
            qty -= 0.0 if "withdraw" in disable else fee
            sf2 = 0.0 if "spot" in disable else _spot_fee(cfg, venue2)
            qty = (qty / ask2) * (1 - sf2 / 100)  # USDT → s.asset на venue2
            wqty = None if "withdraw" in disable else qty
            fee, _ = _hop(cfg, venue2, "", s.ex, s.net, s.asset, qty=wqty, parts=s_parts)
            if fee is None:
                return None
            qty -= 0.0 if "withdraw" in disable else fee
    vol = 0.0 if "risk" in disable else max(cfg.risk_buffer.get(b.asset, 0), cfg.risk_buffer.get(s.asset, 0))
    if vol:   # курс ETH/BTC/TON может уйти, пока идут сделки и переводы
        qty *= 1 - vol / 100
    return qty


def _route(b, s, cfg, spot, over_banks=frozenset()):
    """Чистая прибыль % и шаги маршрута со всеми издержками; None, если связка невозможна.
    over_banks — банки, уже превысившие месячный лимит СБП: комиссия банка выставляется автоматически,
    даже если PAY_FEE в настройках не задан (или задан меньше)."""
    steps = []
    pay_fee, auto_bank = cfg.pay_fee, ""
    bank = trades.sbp_bank(b.pays)
    if bank in over_banks and pay_fee < trades.SBP_OVER_FEE:
        pay_fee, auto_bank = trades.SBP_OVER_FEE, bank
    if pay_fee:
        note = f" (лимит СБП {auto_bank} исчерпан)" if auto_bank else ""
        steps.append(f"комиссия банка −{pay_fee:g}%{note}")
    # тот же расчёт количества, что и в _route_qty — чтобы _hop здесь выбирал ту же сеть (сумма
    # вывода на каждом шаге влияет на то, проходит ли сеть по минимуму биржи)
    qty = cfg.amount * (1 - pay_fee / 100) / b.price
    # стакан обменников из N объявлений одной сети (_stack_qty) — каждому нужен свой перевод с биржи
    s_parts = s.parts if s.ex == "BestChange" else 1
    if b.asset == s.asset:
        fee, label = _hop(cfg, b.ex, b.net, s.ex, s.net, b.asset, qty=qty, parts=s_parts)
        if fee is None:
            return None
        steps.append(label or "внутри биржи")
    elif "USDT" in (b.asset, s.asset):
        alt = s.asset if b.asset == "USDT" else b.asset
        venue = next((v for v in (b.ex, s.ex) + SPOT_VENUES if v in SPOT_VENUES and alt in spot.get(v, {})), None)
        if not venue:
            return None
        bid, ask = spot[venue][alt]
        fee, label = _hop(cfg, b.ex, b.net, venue, "", b.asset, qty=qty)
        if fee is None:
            return None
        if label:
            steps.append(label)
        qty -= fee
        sf = _spot_fee(cfg, venue)
        steps.append(f"спот {b.asset}→{s.asset} на {venue} (−{sf:g}%)")
        qty = (qty / ask if b.asset == "USDT" else qty * bid) * (1 - sf / 100)
        fee, label = _hop(cfg, venue, "", s.ex, s.net, s.asset, qty=qty, parts=s_parts)
        if fee is None:
            return None
        if label:
            steps.append(label)
    else:
        # промежуточная монета: две спот-конвертации на одной площадке, где есть обе пары к USDT
        venue = next((v for v in (b.ex, s.ex) + SPOT_VENUES
                      if v in SPOT_VENUES and b.asset in spot.get(v, {}) and s.asset in spot.get(v, {})), None)
        if venue:
            bid1, _ = spot[venue][b.asset]
            _, ask2 = spot[venue][s.asset]
            fee, label = _hop(cfg, b.ex, b.net, venue, "", b.asset, qty=qty)
            if fee is None:
                return None
            if label:
                steps.append(label)
            qty -= fee
            sf = _spot_fee(cfg, venue)
            steps.append(f"спот {b.asset}→USDT на {venue} (−{sf:g}%)")
            steps.append(f"спот USDT→{s.asset} на {venue} (−{sf:g}%)")
            qty = qty * bid1 * (1 - sf / 100)     # b.asset → USDT
            qty = (qty / ask2) * (1 - sf / 100)   # USDT → s.asset
            fee, label = _hop(cfg, venue, "", s.ex, s.net, s.asset, qty=qty, parts=s_parts)
            if fee is None:
                return None
            if label:
                steps.append(label)
        else:
            # ни одна площадка не держит обе пары: конвертация на каждой своей + перевод USDT между ними
            venue1 = next((v for v in (b.ex,) + SPOT_VENUES if v in SPOT_VENUES and b.asset in spot.get(v, {})), None)
            venue2 = next((v for v in (s.ex,) + SPOT_VENUES if v in SPOT_VENUES and s.asset in spot.get(v, {})), None)
            if not venue1 or not venue2 or venue1 == venue2:
                return None
            bid1, _ = spot[venue1][b.asset]
            _, ask2 = spot[venue2][s.asset]
            fee, label = _hop(cfg, b.ex, b.net, venue1, "", b.asset, qty=qty)
            if fee is None:
                return None
            if label:
                steps.append(label)
            qty -= fee
            sf1 = _spot_fee(cfg, venue1)
            steps.append(f"спот {b.asset}→USDT на {venue1} (−{sf1:g}%)")
            qty = qty * bid1 * (1 - sf1 / 100)    # b.asset → USDT на venue1
            fee, label = _hop(cfg, venue1, "", venue2, "", "USDT", qty=qty)
            if fee is None:
                return None
            if label:
                steps.append(label)
            qty -= fee
            sf2 = _spot_fee(cfg, venue2)
            steps.append(f"спот USDT→{s.asset} на {venue2} (−{sf2:g}%)")
            qty = (qty / ask2) * (1 - sf2 / 100)  # USDT → s.asset на venue2
            fee, label = _hop(cfg, venue2, "", s.ex, s.net, s.asset, qty=qty, parts=s_parts)
            if fee is None:
                return None
            if label:
                steps.append(label)
    vol = max(cfg.risk_buffer.get(b.asset, 0), cfg.risk_buffer.get(s.asset, 0))
    if vol:
        steps.append(f"запас на курс −{vol:g}%")
    out_qty = _route_qty(b, s, cfg, spot, over_banks)
    if out_qty is None:
        return None
    return (out_qty * s.price / cfg.amount - 1) * 100, " → ".join(steps)


def _match(b, sell_ads, cfg, spot, over_banks=frozenset()):
    """Связка покупки b со стаканом продажи (sell_ads отсортированы: лучшая цена первой). Продажа
    собирается под фактический выход монеты маршрута: при прибыли его больше, чем сумма круга / цена,
    и объявления, покрывающие сумму круга впритык, весь объём не примут.
    (прибыль %, b, s, маршрут) или None — маршрут невозможен или глубины продажи не хватает."""
    if not sell_ads:
        return None
    # от цены продажи выход не зависит; запас на курс только занижает оценку прибыли, а продавать
    # придётся всю монету — глубину сверяем с количеством без него
    qty = _route_qty(b, sell_ads[0], cfg, spot, over_banks, disable=frozenset({"risk"}))
    s = _stack_qty(sell_ads, qty)
    if s is None:
        return None
    r = _route(b, s, cfg, spot, over_banks)
    return (r[0], b, s, r[1]) if r else None


def profit_breakdown(b, s, cfg, spot, over_banks=frozenset()):
    """Разложение чистой прибыли связки на составляющие: валовый спред (без издержек) → минус
    вывод → минус спот (если есть конвертация монеты) → минус запас на курс → чистыми (с учётом
    комиссии банка, если применяется) — каждая стадия в % от суммы круга. None — маршрут невозможен."""
    def profit(disable):
        qty = _route_qty(b, s, cfg, spot, over_banks, disable)
        return None if qty is None else (qty * s.price / cfg.amount - 1) * 100

    stages = [("Валовый спред", {"bank", "withdraw", "spot", "risk"}),
              ("− вывод", {"bank", "spot", "risk"})]
    if b.asset != s.asset:
        stages.append(("− спот", {"bank", "risk"}))
    stages += [("− запас на курс", {"bank"}), ("Чистыми", set())]
    out = []
    for label, disable in stages:
        p = profit(disable)
        if p is None:
            return None
        out.append((label, p))
    return out


def breakeven_rate(b, s, cfg, spot, over_banks=frozenset()):
    """Курс покупки (₽ за b.asset), при котором связка выходит в ноль — цена продажи и остальные
    условия маршрута (комиссии, курсы конвертации) считаются неизменными. Цена покупки входит в
    маршрут только через первое деление суммы круга на неё, поэтому итоговое количество монеты на
    выходе — аффинная функция от 1/цена (M/price − K); берём две точки и решаем систему на M и K.
    None — маршрут невозможен или в нём нет цены, при которой связка выходит в ноль."""
    target = cfg.amount / s.price   # сколько s.asset нужно на выходе, чтобы прибыль была нулевой
    x1 = b.price
    x2 = x1 * 1.01
    q1 = _route_qty(b, s, cfg, spot, over_banks)
    q2 = _route_qty(dataclasses.replace(b, price=x2), s, cfg, spot, over_banks)
    if q1 is None or q2 is None:
        return None
    inv1, inv2 = 1 / x1, 1 / x2
    m = (q1 - q2) / (inv1 - inv2)
    if m <= 0:
        return None
    k = m * inv1 - q1
    denom = target + k
    if denom <= 0:
        return None
    return m / denom


def fmt_breakeven(d, cfg, snap):
    """Текст с ROI на сумму круга (в ₽) и курсом безубыточности покупки — для «/calc»."""
    profit, b, s, _ = d
    profit_rub = cfg.amount * profit / 100
    text = f"📐 ROI на {_money(cfg.amount)} {cfg.fiat}: <b>{profit:+.2f}%</b> ({profit_rub:+.0f} ₽)\n"
    rate = breakeven_rate(b, s, cfg, snap.spot, snap.over_banks)
    if rate is None:
        return text + "Курс безубыточности посчитать не удалось (маршрут не зависит от цены покупки)."
    cushion = (rate / b.price - 1) * 100
    return text + (f"Курс безубыточности покупки: <b>{_price(rate)} ₽</b> за {b.asset} "
                   f"(сейчас {_price(b.price)} ₽, запас {cushion:+.1f}%)")


def maker_quote(groups, ex, asset, post_side):
    """Режим мейкера: цена, чтобы встать первым объявлением на площадке, и спред против цены,
    которую сразу даёт лучшее встречное объявление (то есть чем я жертвую ради первого места).
    post_side: "buy_ad" — я выставляю объявление на покупку монеты (встаю в очередь тех, у кого
    бот сам бы продал — group[ex,"sell",asset]); "sell_ad" — на продажу (встаю в очередь тех,
    у кого бот сам бы купил — group[ex,"buy",asset]). None — нет обеих сторон стакана на площадке."""
    if post_side == "buy_ad":
        queue, counter = groups.get((ex, "sell", asset)), groups.get((ex, "buy", asset))
        if not queue or not counter:
            return None
        price = queue[0].price + MAKER_TICK
        counter_price = counter[0].price
        spread = price - counter_price   # я переплачиваю сверх цены немедленной покупки
    elif post_side == "sell_ad":
        queue, counter = groups.get((ex, "buy", asset)), groups.get((ex, "sell", asset))
        if not queue or not counter:
            return None
        price = queue[0].price - MAKER_TICK
        counter_price = counter[0].price
        spread = counter_price - price   # я недополучаю против цены немедленной продажи
    else:
        raise ValueError(post_side)
    fee = MAKER_FEE.get(ex, {}).get(post_side, 0.0)
    return price, counter_price, spread / counter_price * 100 + fee


def bank_liquidity(groups, ex, asset):
    """{"buy"/"sell": {банк: (число объявлений, объём ₽)}} на площадке `ex` для `asset` — по объявлениям
    стакана (`groups`, как в `snap.groups`: уже прошли фильтры мерчанта и отсев аномалий). Объём
    объявления — доступный остаток в фиате, как при сборке стакана в `_stack` (min(лимит объявления,
    остаток монеты по цене)). Сторона/площадка без объявлений в результат не попадает."""
    out = {}
    for side in ("buy", "sell"):
        banks = {}
        for a in groups.get((ex, side, asset), []):
            vol = min(a.max_amt, a.avail * a.price)
            for p in a.pays:
                cnt, total = banks.get(p, (0, 0.0))
                banks[p] = (cnt + 1, total + vol)
        if banks:
            out[side] = banks
    return out


@dataclass
class Snapshot:
    ref: float
    ref_src: str
    refs: dict       # монета -> ориентир в фиате
    best: dict       # (ex, side, asset) -> лучшее Ad после фильтров
    deals: list      # [(profit %, buy Ad, sell Ad, маршрут)], по убыванию
    networks: dict   # сеть -> {"buy": Ad, "sell": Ad} для USDT у обменников
    dropped: dict    # ex -> сколько аномальных объявлений отсеяно
    errors: dict     # "ex/монета" -> текст ошибки
    groups: dict = field(default_factory=dict)      # (ex, side, asset) -> объявления стакана (отсортированы по цене)
    spot: dict = field(default_factory=dict)        # для deal_amounts: те же спот-цены, что использовал _route
    over_banks: frozenset = field(default_factory=frozenset)


_alt = {"t": 0.0, "ads": [], "errors": {}, "key": None}   # key — (монеты, площадки, сумма круга), под которые собран кэш

VENUE_BACKOFF_BASE = 30    # сек: первая пауза площадки после ошибки
VENUE_BACKOFF_MAX = 600    # сек: потолок паузы (10 мин)
_venue_backoff = {}   # ex -> {"delay": текущая пауза (0 = нет бэкоффа), "until": unix-время окончания паузы}


def _venue_paused_until(ex):
    """Unix-время окончания паузы площадки, если она сейчас в бэкоффе после серии ошибок, иначе None."""
    st = _venue_backoff.get(ex)
    if st and st["until"] > time.time():
        return st["until"]
    return None


def _venue_backoff_fail(ex):
    """Ошибка площадки: следующая пауза растёт 30с → 60 → 120 … до потолка VENUE_BACKOFF_MAX (10 мин)."""
    st = _venue_backoff.setdefault(ex, {"delay": 0, "until": 0.0})
    st["delay"] = VENUE_BACKOFF_BASE if not st["delay"] else min(st["delay"] * 2, VENUE_BACKOFF_MAX)
    st["until"] = time.time() + st["delay"]
    logger.warning("%s: ошибка, пропускаю %d с", ex, st["delay"])


def _venue_backoff_ok(ex):
    """Площадка ответила успешно — сброс паузы (следующая ошибка снова начнёт с VENUE_BACKOFF_BASE)."""
    if _venue_backoff.pop(ex, None):
        logger.info("%s: снова отвечает, бэкофф сброшен", ex)


TRAPS_LOG_SIZE = 30
TRAPS_LOG = deque(maxlen=TRAPS_LOG_SIZE)   # последние отсеянные «ловушки» — для /traps, обучение без риска


def _trap_entry(a, ref, cfg):
    """Объявление отсеяно фильтром аномалий (usable/_signal_ok прошли, но цена далеко от рынка).
    Возвращает запись для /traps, только если отклонение делает объявление привлекательным
    (дешевле рынка на покупку / дороже рынка на продажу) — это и есть типичная ловушка;
    невыгодные для нас аномалии в другую сторону никого не заманивают, их не показываем."""
    dev = (a.price / ref - 1) * 100
    attractive = dev < 0 if a.side == "buy" else dev > 0
    if not attractive:
        return None
    action = "купить" if a.side == "buy" else "продать"
    word = "ниже" if a.side == "buy" else "выше"
    reason = (f"{action} {a.asset} на {a.ex} по {_price(a.price)} ₽ — на {abs(dev):.1f}% {word} рынка "
              f"(ориентир {_price(ref)} ₽, отсев >{cfg.max_dev:g}%)")
    return {"ts": time.time(), "ex": a.ex, "side": a.side, "asset": a.asset, "price": a.price,
            "ref": ref, "dev": dev, "reason": reason}


def traps_log():
    """Последние отсеянные ловушки, новые первыми."""
    return list(reversed(TRAPS_LOG))


async def scan(s, cfg, force_alt=False):
    """force_alt — разовый скан под свою сумму (`/calc`, «своя сумма»): всегда опросить не-USDT монеты
    заново и не трогать общий кэш _alt, потому что лимиты объявлений зависят от cfg.amount."""
    names_all = [n for n in cfg.exchanges if n in FETCHERS]
    key = (tuple(cfg.assets), tuple(names_all), cfg.amount)   # площадки фильтруют объявления по сумме
    if not force_alt and _alt["key"] != key:   # сменили монеты/площадки/сумму — старый кэш не годится, опросить заново
        _alt.update(t=0.0, ads=[], errors={}, key=key)
    paused = {n: u for n in names_all if (u := _venue_paused_until(n))}   # площадки на паузе после ошибок
    names = [n for n in names_all if n not in paused]
    alts = [a for a in cfg.assets if a != "USDT"]
    alt_due = force_alt or (bool(alts) and time.time() - _alt["t"] >= cfg.alt_interval)
    jobs = [(n, side, asset) for n in names
            for asset in (["USDT"] if "USDT" in cfg.assets else []) + (alts if alt_due or n == "bestchange" else [])
            for side in ("buy", "sell")]
    ref_task = asyncio.ensure_future(rapira_mid(s)) if cfg.fiat == "RUB" else None
    spot_task = asyncio.ensure_future(spot_prices(s, cfg.assets))
    net_task = asyncio.ensure_future(netstatus.refresh_if_due(s, cfg.assets, cfg.exchanges, _json))
    res = await asyncio.gather(*(FETCHERS[n](s, cfg, side, asset) for n, side, asset in jobs), return_exceptions=True)
    try:
        net_errors = await net_task or {}
    except Exception as e:
        net_errors = {"сети": f"{type(e).__name__}: {e}"[:80]}

    ads, errors, alt_ads, alt_errors = [], {}, [], {}
    venue_seen, venue_failed = set(), set()
    for (n, _, asset), r in zip(jobs, res):
        venue_seen.add(n)
        cached = asset != "USDT" and n != "bestchange"   # не-USDT монеты кэшируем на alt_interval
        if isinstance(r, Exception):
            venue_failed.add(n)
            (alt_errors if cached else errors)[f"{n}/{asset}"] = f"{type(r).__name__}: {r}"[:120]
        else:
            (alt_ads if cached else ads).extend(r)
    for n in venue_seen:   # бэкофф по площадке: сбрасываем на успехе, растим паузу на ошибке
        (_venue_backoff_fail if n in venue_failed else _venue_backoff_ok)(n)
    for n, until in paused.items():
        errors[n] = f"пауза до {time.strftime('%H:%M', time.localtime(until))}"
    if alt_due and not force_alt:
        _alt.update(t=time.time(), ads=alt_ads, errors=alt_errors)
    ads += alt_ads if force_alt else _alt["ads"]
    errors.update(alt_errors if force_alt else _alt["errors"])

    ref, ref_src = None, "-"
    if ref_task:
        try:
            ref, ref_src = await ref_task, "Rapira USDT/RUB"
        except Exception:
            pass
    if ref is None:
        usdt = [a.price for a in ads if a.asset == "USDT"]
        ref, ref_src = (statistics.median(usdt), "медиана P2P") if usdt else (None, "-")
    try:
        spot = await spot_task
    except Exception as e:
        spot = {"Bybit": {"USDT": (1.0, 1.0)}}
        errors["spot"] = f"{type(e).__name__}: {e}"[:120]
    errors.update({f"сети/{k}": v for k, v in net_errors.items()})
    refs = {}
    for a in cfg.assets:
        mid = _mid(spot, a)
        if ref and mid:
            refs[a] = ref * mid
        else:
            p = [x.price for x in ads if x.asset == a]
            if p:
                refs[a] = statistics.median(p)

    blocked = blacklist.blocked()
    best, dropped, networks = {}, {}, {}
    for a in ads:
        if not usable(a, cfg, blocked):
            continue
        r = refs.get(a.asset)
        if r and abs(a.price / r - 1) * 100 > cfg.max_dev:
            dropped[a.ex] = dropped.get(a.ex, 0) + 1
            trap = _trap_entry(a, r, cfg)
            if trap:
                TRAPS_LOG.append(trap)
            continue
        better = (lambda cur: cur is None or (a.price < cur.price if a.side == "buy" else a.price > cur.price))
        if better(best.get((a.ex, a.side, a.asset))):
            best[(a.ex, a.side, a.asset)] = a
        if a.net and a.asset == "USDT" and better(networks.setdefault(a.net, {}).get(a.side)):
            networks[a.net][a.side] = a

    # для связок — стакан глубины: покупку складываем по цене, пока не наберётся сумма круга, продажу —
    # под фактический выход монеты маршрута (_match); связку, которую объём не покрывает, не сигналим;
    # обменники складываем только в пределах одной сети
    groups = {}
    for a in ads:
        if not _signal_ok(a, cfg, blocked):
            continue
        r = refs.get(a.asset)
        if r and abs(a.price / r - 1) * 100 > cfg.max_dev:
            continue
        groups.setdefault((a.ex, a.side, a.asset), []).append(a)
    buys, sells = [], []
    for key, grp in groups.items():
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
        for part in _net_parts(grp):
            if key[1] == "sell":
                sells.append(part)   # продажа собирается в _match под выход монеты конкретной связки
                continue
            stacked = _stack(part, cfg.amount)
            if stacked:
                buys.append(stacked)

    over_banks = trades.banks_over_limit({trades.sbp_bank(b.pays) for b in buys})
    deals = []
    for b in buys:
        for sl in sells:
            if cfg.same_venue_only and not _same_venue(b, sl[0]):
                continue   # пресет «USDT без переводов»: только связки внутри одной площадки
            d = _match(b, sl, cfg, spot, over_banks)
            if d:
                deals.append(d)
    snap = Snapshot(ref or 0, ref_src, refs, best, [], networks, dropped, errors, groups, spot, over_banks)
    # сортировка «прибыль × надёжность»: каждая причина риска снимает risk_penalty п.п. с профита
    deals.sort(key=lambda d: d[0] - cfg.risk_penalty * len(reliability(d, cfg, snap)[1]), reverse=True)
    # обменники разных сетей дают по связке на одну и ту же пару площадок; бот сигналит по паре
    # (b.ex, b.asset, s.ex, s.asset) — оставляем лучшую, чтобы дубли не вытеснили из топ-N другие площадки
    seen = set()
    for d in deals:
        key = (d[1].ex, d[1].asset, d[2].ex, d[2].asset)
        if key not in seen:
            seen.add(key)
            snap.deals.append(d)
    return snap


DEPTH_AMOUNTS = (50_000, 100_000, 300_000)   # суммы круга для разбивки прибыли в карточке связки


def deal_amounts(deal, cfg, snap, amounts=DEPTH_AMOUNTS):
    """Прибыль % той же связки на другие суммы круга: пересобрать те же объявления стакана
    (snap.groups) под каждую сумму — покупку через _stack, продажу под выход монеты (_match).
    None для суммы, на которую не хватает глубины."""
    _, b, s, _ = deal
    buy_ads = _same_net(snap.groups.get((b.ex, "buy", b.asset), []), b)
    sell_ads = _same_net(snap.groups.get((s.ex, "sell", s.asset), []), s)
    out = {}
    for amount in amounts:
        bb = _stack(buy_ads, amount)
        d = _match(bb, sell_ads, dataclasses.replace(cfg, amount=amount), snap.spot, snap.over_banks) if bb else None
        out[amount] = d[0] if d else None
    return out


def venue_url(a, fiat="RUB"):
    """Страница площадки для объявления: у обменника — его ссылка с BestChange."""
    if a.url:
        return a.url
    buy, t = a.side == "buy", a.asset.upper()
    return {
        "Bybit": f"https://www.bybit.com/fiat/trade/otc/?actionType={1 if buy else 0}&token={t}&fiat={fiat}",
        "MEXC": f"https://www.mexc.com/ru-RU/buy-crypto/p2p?fiat={fiat}",
        "HTX": f"https://www.htx.com/ru-ru/fiat-crypto/trade/{'buy' if buy else 'sell'}-{t.lower()}-{fiat.lower()}/",
        "KuCoin": f"https://www.kucoin.com/ru/otc/{'buy' if buy else 'sell'}/{t}-{fiat}",
        "BitPapa": "https://bitpapa.com/ru",
    }.get(a.ex, "")


def spot_url(route):
    """Ссылка на спот-пару из описания маршрута («спот USDT→ETH на Bybit»), иначе пусто."""
    m = re.search(r"спот (\w+)→(\w+) на (\w+)", route)
    if not m:
        return ""
    coin = m.group(2) if m.group(1) == "USDT" else m.group(1)
    return {"Bybit": f"https://www.bybit.com/trade/spot/{coin}/USDT",
            "MEXC": f"https://www.mexc.com/ru-RU/exchange/{coin}_USDT",
            "HTX": f"https://www.htx.com/trade/{coin.lower()}_usdt",
            "KuCoin": f"https://www.kucoin.com/trade/{coin}-USDT"}.get(m.group(3), "")


RELIABLE, RISKY, TRAP = "✅ надёжно", "⚠️ риск", "🪤 ловушка"


def reliability(deal, cfg, snap):
    """Метка надёжности связки и причины: очки риска за отклонение цены от ориентира, мерчанта
    у порога фильтра по сделкам/отзывам, число переводов/конвертаций в маршруте, волатильную
    монету и спред ≥5%. 0 очков — надёжно, 1-2 — риск, 3+ — похоже на ловушку."""
    profit, b, s, route = deal
    reasons = []
    for ad, side in ((b, "покупка"), (s, "продажа")):
        ref = snap.refs.get(ad.asset)
        if ref:
            dev = abs(ad.price / ref - 1) * 100
            if dev >= cfg.max_dev * 0.6:
                reasons.append(f"{side}: цена {dev:.1f}% от ориентира (отсев >{cfg.max_dev:g}%)")
        if ad.orders < cfg.min_orders * 1.5 or ad.rate < cfg.min_rate + 1:
            reasons.append(f"{side}: мерчант у порога фильтра ({ad.orders} сделок/{ad.rate:.0f}%)")
        risky = [n for n in terms_flags(ad.terms)[1] if n in TERMS_RISKY]
        if risky:
            reasons.append(f"{side}: условия — {', '.join(risky)}")
    steps = route.split(" → ") if route else []
    transfers = sum(1 for st in steps if "перевод" in st or "спот" in st or "через" in st)
    if transfers >= 2:
        reasons.append(f"{transfers} перевода/конвертации в маршруте")
    vol = next((a for a in (b.asset, s.asset) if cfg.risk_buffer.get(a)), None)
    if vol:
        reasons.append(f"{vol} — волатильная монета, курс может уйти за время сделки")
    if profit >= 5:
        reasons.append(f"спред {profit:.1f}% ≥5% — часто плата за риск")
    label = TRAP if len(reasons) >= 3 else RISKY if reasons else RELIABLE
    return label, reasons


def fmt_reliability(label, reasons):
    return label if not reasons else f"{label} ({'; '.join(reasons)})"


def _money(x):
    return f"{x:,.0f}".replace(",", " ")


def _price(p):
    return _money(p) if p >= 1000 else f"{p:.2f}"


def fmt_ad(a):
    stats = f"{a.orders} отз/{a.rate:.0f}% хор." if a.ex == "BestChange" else f"{a.orders} сд/{a.rate:.0f}%"
    link = f' · <a href="{html.escape(a.url)}">открыть</a>' if a.url else ""
    pays = ", ".join(a.pays[:4]) + (f" +{len(a.pays) - 4}" if len(a.pays) > 4 else "")
    notes = terms_flags(a.terms)[1]
    cond = f" · ⚠ {html.escape('; '.join(notes[:2]))}" if notes else ""
    return f"{a.ex} {a.asset} {_price(a.price)} ({html.escape(pays)}) · {html.escape(a.nick)} · {stats}{link}{cond}"


def fmt_deal(d, cfg, snap=None):
    profit, b, s, route = d
    text = (f"<b>{profit:+.2f}%</b> на {_money(cfg.amount)} {cfg.fiat} ({route})\n")
    if snap is not None:
        text += html.escape(fmt_reliability(*reliability(d, cfg, snap))) + "\n"
    return text + f"Купить: {fmt_ad(b)}\nПродать: {fmt_ad(s)}"


def fmt_top(snap, cfg, n=5):
    refs = " · ".join(f"{a} {_price(p)}" for a, p in snap.refs.items() if a != "USDT")
    lines = [f"Ориентир USDT {snap.ref:.2f} ({snap.ref_src})" + (f" · {refs}" if refs else ""),
             f"Сумма круга {_money(cfg.amount)} {cfg.fiat}", "", "<b>USDT по площадкам:</b>"]
    for ex in sorted({ex for ex, _, asset in snap.best if asset == "USDT"}):
        b, s = snap.best.get((ex, "buy", "USDT")), snap.best.get((ex, "sell", "USDT"))
        line = f"{ex}: купить {b.price:.2f}" if b else f"{ex}: купить —"
        line += f" / продать {s.price:.2f}" if s else " / продать —"
        if b and s:
            line += f" → спред {(b.price / s.price - 1) * 100:.2f}%"
        lines.append(line)
    alts = [a for a in cfg.assets if a != "USDT"]
    if alts:
        lines += ["", "<b>Другие монеты (лучшее):</b>"]
        for asset in alts:
            bs = [v for (_, side, a), v in snap.best.items() if a == asset and side == "buy"]
            ss = [v for (_, side, a), v in snap.best.items() if a == asset and side == "sell"]
            b = min(bs, key=lambda x: x.price) if bs else None
            s = max(ss, key=lambda x: x.price) if ss else None
            lines.append(f"{asset}: купить " + (f"{_price(b.price)} ({b.ex})" if b else "—")
                         + " / продать " + (f"{_price(s.price)} ({s.ex})" if s else "—"))
    if snap.networks:
        lines += ["", "<b>Сети USDT у обменников:</b>"]
        for net, v in sorted(snap.networks.items()):
            b, s = v.get("buy"), v.get("sell")
            lines.append(f"{net}: купить " + (f"{b.price:.2f}" if b else "—") + " / продать " + (f"{s.price:.2f}" if s else "—"))
    if snap.dropped:
        lines.append("\nОтсеяно аномальных (>±%g%% от ориентира): %s" % (
            cfg.max_dev, ", ".join(f"{ex} {c}" for ex, c in snap.dropped.items())))
    if snap.errors:
        lines.append("Ошибки: " + "; ".join(f"{ex}: {e}" for ex, e in snap.errors.items()))
    lines += ["", "<b>Лучшие связки:</b>"]
    text = "\n".join(lines)
    if not snap.deals:
        return text + "\nнет: на одной из сторон не осталось объявлений после фильтров"
    for d in snap.deals[:n]:   # целыми блоками, чтобы не резать HTML и влезть в лимит Telegram 4096
        block = "\n" + fmt_deal(d, cfg, snap) + "\n"
        if len(text) + len(block) > 4000:
            break
        text += block
    return text


async def main():
    load_env()
    cfg = Config.from_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        snap = await scan(s, cfg)
    logger.info(html.unescape(re.sub(r"<[^>]+>", "", fmt_top(snap, cfg, n=10))))


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
