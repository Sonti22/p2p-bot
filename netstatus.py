"""Статус сетей ввода/вывода монет по биржам — чтобы не сигналить связку, в которой вывод закрыт.

HTX и KuCoin — публичные справочники валют; Bybit и MEXC — только при подключённом ключе «только чтение»
(accounts.keys). Неизвестный статус = не мешаем. Обновление раз в TTL секунд; если биржа не ответила
или ответила HTTP 200 с ошибкой в теле (retCode/code не «успех»), остаётся прежняя таблица — распознанная
ошибка API не должна выглядеть как «у площадки нет сетей». Переключения открыт ↔ закрыт копятся в CHANGES —
бот забирает их и шлёт алерт.
"""
import asyncio
import re
import time

import accounts

TTL = 600
KNOWN_NETS = ("TRC20", "BEP20", "ERC20", "TON", "SOL", "POLYGON", "ARBITRUM", "APT", "BTC")
HTX_NATIVE_ONLY = ("BTC", "ETH")   # _parse_htx оставляет у этих монет только родную сеть (без обёрнутых токенов)
# справочники, урезанные при разборе: сети, которой в них нет, у площадки может быть открыта (HTX ETH в ARBITRUM) —
# для ввода «нет в справочнике» у них значит «неизвестно», а не «не поддерживается» (deposit_nets)
PARTIAL = {("HTX", a) for a in HTX_NATIVE_ONLY}
STATUS = {}      # (площадка, монета) -> {сеть: {"dep": bool|None, "wd": bool|None, "fee": float|None, "min": float|None}}
CHANGES = []     # (площадка, монета, сеть, "вывод"/"ввод", открыт: bool)
# сети из справочников площадок, которых normalize() не знает (вне KNOWN_NETS): (площадка, монета, имя) ->
# {"first", "last", "seen"} — подсказка владельцу, что маппинг пора расширить (/nets); на расчёт не влияет
UNMAPPED = {}
UNMAPPED_MAX = 60
_meta = {"t": 0.0, "errors": {}}


def normalize(name):
    """Название сети у биржи -> наше: TRC20/BEP20/ERC20/TON/SOL/POLYGON/ARBITRUM/APT/BTC, иначе как есть.
    «Имя(ТИКЕР)» (Toncoin(TON), Bitcoin(BTC)) — по тикеру в скобках, если он из наших; TON с 15.06.2026 — ещё и GRAM."""
    n = (name or "").upper().strip()
    if "TRC20" in n or n in ("TRX", "TRON") or n.startswith("TRON"):
        return "TRC20"
    if "BEP20" in n or n in ("BSC", "BNB", "BNB SMART CHAIN") or n.startswith("BSC"):
        return "BEP20"
    if "ERC20" in n or n in ("ETH", "ETHEREUM"):
        return "ERC20"
    if n in ("TON", "TONCOIN", "GRAM") or n.startswith("TON("):
        return "TON"
    if n in ("SOL", "SOLANA") or "SOLANA" in n:
        return "SOL"
    if "POLYGON" in n or n == "MATIC":
        return "POLYGON"
    if "ARBITRUM" in n or n in ("ARB", "ARBI"):
        return "ARBITRUM"
    if n in ("APT", "APTOS") or "APTOS" in n:
        return "APT"
    if n in ("BTC", "BITCOIN"):
        return "BTC"
    m = re.fullmatch(r".+\(([^()]+)\)", n)
    if m and normalize(m.group(1)) in KNOWN_NETS:
        return normalize(m.group(1))
    return n


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_htx(j, asset=None):
    """Справочник валют HTX по одной монете. У HTX для BTC/ETH вместе с настоящей сетью в списке
    приходят обёрнутые токены на чужих блокчейнах (chain trc20btc/trc20wbtc/wbtc и т.п. с displayName
    TRC20/ERC20/BEP20) — это не настоящий вывод BTC/ETH в этой сети, а другой актив под тем же именем
    сети. Для BTC/ETH (asset задан) берём только запись с настоящим chain (== код монеты), иначе
    маршрут посчитает копеечную комиссию обёрнутого токена как обычный вывод BTC/ETH."""
    if j.get("code") != 200:      # HTTP 200, но ошибка в теле — не путать с «монеты нет в ответе»
        raise ValueError(j.get("message") or j.get("code"))
    out = {}
    for c in j.get("data") or []:
        for ch in c.get("chains") or []:
            if asset in HTX_NATIVE_ONLY and (ch.get("chain") or "").lower() != asset.lower():
                continue
            out[normalize(ch.get("displayName") or ch.get("chain"))] = {
                "dep": ch.get("depositStatus") == "allowed", "wd": ch.get("withdrawStatus") == "allowed",
                "fee": _f(ch.get("transactFeeWithdraw")), "min": _f(ch.get("minWithdrawAmt"))}
    return out


def _parse_kucoin(j):
    if str(j.get("code")) != "200000":
        raise ValueError(j.get("msg") or j.get("code"))
    out = {}
    for ch in (j.get("data") or {}).get("chains") or []:
        net = normalize(ch.get("chainName"))
        rec = {"dep": bool(ch.get("isDepositEnabled")), "wd": bool(ch.get("isWithdrawEnabled")),
               "fee": _f(ch.get("withdrawalMinFee")), "min": _f(ch.get("withdrawalMinSize"))}
        cur = out.get(net)
        if cur is None or (rec["wd"] and not cur["wd"]):   # у KuCoin бывает две записи TON — берём открытую
            out[net] = rec
    return out


def _parse_bybit(j):
    if j.get("retCode"):
        raise ValueError(j.get("retMsg") or j.get("retCode"))
    out = {}
    for row in (j.get("result") or {}).get("rows") or []:
        for ch in row.get("chains") or []:
            out[normalize(ch.get("chainType") or ch.get("chain"))] = {
                "dep": str(ch.get("chainDeposit")) == "1", "wd": str(ch.get("chainWithdraw")) == "1",
                "fee": _f(ch.get("withdrawFee")), "min": _f(ch.get("withdrawMin"))}
    return out


def _parse_mexc(j, asset):
    if not isinstance(j, list):       # успешный ответ — список монет; словарь — ошибка API (code/msg)
        raise ValueError((j or {}).get("msg") or "MEXC error")
    out = {}
    for c in j:
        if c.get("coin") != asset:
            continue
        for ch in c.get("networkList") or []:
            out[normalize(ch.get("network") or ch.get("netWork"))] = {
                "dep": bool(ch.get("depositEnable")), "wd": bool(ch.get("withdrawEnable")),
                "fee": _f(ch.get("withdrawFee")), "min": _f(ch.get("withdrawMin"))}
    return out


def _note_unmapped(venue, asset, nets, now=None):
    """Запомнить сети справочника вне KNOWN_NETS (normalize вернула имя как есть). Журнал ограничен UNMAPPED_MAX —
    сверх него выпадают давно не виденные."""
    now = time.time() if now is None else now
    for net in nets:
        if not net or net in KNOWN_NETS:
            continue
        rec = UNMAPPED.setdefault((venue, asset, net), {"first": now, "last": now, "seen": 0})
        rec["last"], rec["seen"] = now, rec["seen"] + 1
    while len(UNMAPPED) > UNMAPPED_MAX:
        del UNMAPPED[min(UNMAPPED, key=lambda k: UNMAPPED[k]["last"])]


def unmapped():
    """Журнал нераспознанных сетей: [(площадка, монета, имя, запись)], недавние первыми."""
    return sorted(((v, a, n, r) for (v, a, n), r in UNMAPPED.items()), key=lambda x: (-x[3]["last"], x[:3]))


def _apply(venue, asset, nets):
    _note_unmapped(venue, asset, nets)
    old = STATUS.get((venue, asset)) or {}
    for net, rec in nets.items():
        prev = old.get(net)
        if prev and net in KNOWN_NETS:
            for kind, label in (("wd", "вывод"), ("dep", "ввод")):
                if prev.get(kind) is not None and rec.get(kind) is not None and prev[kind] != rec[kind]:
                    CHANGES.append((venue, asset, net, label, rec[kind]))
    STATUS[(venue, asset)] = nets


async def refresh(s, assets, exchanges, get_json):
    """Обновить STATUS по всем монетам. get_json(s, method, url) — публичный запрос (в тестах подменяется)."""
    ex = {e.lower() for e in exchanges}
    tasks, keys = [], []
    for a in assets:
        if "htx" in ex:
            tasks.append(get_json(s, "GET", f"https://api.htx.com/v2/reference/currencies?currency={a.lower()}"))
            keys.append(("HTX", a, lambda r, a=a: _parse_htx(r, a)))
        if "kucoin" in ex:
            tasks.append(get_json(s, "GET", f"https://api.kucoin.com/api/v3/currencies/{a}"))
            keys.append(("KuCoin", a, _parse_kucoin))
        if "bybit" in ex and accounts.keys("bybit"):
            k, sec = accounts.keys("bybit")
            tasks.append(accounts.bybit_get(s, k, sec, "/v5/asset/coin/query-info", {"coin": a}))
            keys.append(("Bybit", a, _parse_bybit))
    if "mexc" in ex and accounts.keys("mexc"):
        k, sec = accounts.keys("mexc")
        tasks.append(accounts.mexc_get(s, k, sec, "/api/v3/capital/config/getall"))
        keys.append(("MEXC", None, None))
    res = await asyncio.gather(*tasks, return_exceptions=True)
    errors = {}
    for (venue, asset, parse), r in zip(keys, res):
        if isinstance(r, Exception):
            errors[venue] = f"{type(r).__name__}: {r}"[:80]
            continue
        try:
            if venue == "MEXC":
                for a in assets:
                    _apply(venue, a, _parse_mexc(r, a))
            else:
                _apply(venue, asset, parse(r))
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            # формат ответа изменился, или HTTP 200 с ошибкой в теле (retCode/code != OK) — прежние данные не трогаем
            errors[venue] = f"parse: {e}"[:80]
    _meta["errors"] = errors
    _meta["t"] = time.time()
    return errors


async def refresh_if_due(s, assets, exchanges, get_json):
    if time.time() - _meta["t"] < TTL:
        return None
    _meta["t"] = time.time()   # сначала отметка времени: при сбое не долбим биржи каждый скан
    return await refresh(s, assets, exchanges, get_json)


def _rec(venue, asset, net):
    return (STATUS.get((venue, asset)) or {}).get(net)


def withdraw_ok(venue, asset, net):
    """True/False, None — статус неизвестен (площадка без справочника или ключа)."""
    r = _rec(venue, asset, net)
    return None if r is None else r.get("wd")


def deposit_ok(venue, asset, net):
    r = _rec(venue, asset, net)
    return None if r is None else r.get("dep")


def live_fee(venue, asset, net):
    r = _rec(venue, asset, net)
    return None if r is None else r.get("fee")


def min_withdraw(venue, asset, net):
    """Минимальная сумма вывода монеты в этой сети по живому справочнику; None — сведений нет."""
    r = _rec(venue, asset, net)
    return None if r is None else r.get("min")


def open_nets(venue, asset):
    """Сети, где вывод точно открыт (по живому справочнику)."""
    return [n for n, r in (STATUS.get((venue, asset)) or {}).items() if r.get("wd")]


def known_nets(venue, asset):
    """Все сети площадки из живого справочника; пусто — сведений нет."""
    return list(STATUS.get((venue, asset)) or {})


def deposit_nets(venue, asset):
    """Сети полного живого справочника площадки: сети, которой в нём нет, площадка монету не примет. Пусто — сведений
    нет или справочник урезан при разборе (PARTIAL): тогда «нет в справочнике» — это «неизвестно»."""
    return [] if (venue, asset) in PARTIAL else known_nets(venue, asset)


def pop_changes():
    c = CHANGES[:]
    CHANGES.clear()
    return c


def reset():
    STATUS.clear()
    CHANGES.clear()
    UNMAPPED.clear()
    _meta.update(t=0.0, errors={})
