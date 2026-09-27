"""Бумажный мейкер (план, этап 8 — только бумага): симуляция СВОИХ объявлений на P2P-площадке без единого
запроса к бирже. Объявления не создаются и не меняются — всё считается по снимку стакана, который бот и так
собирает (`Snapshot.book`: первая страница выдачи площадки, без обменников и без аномалий за MAX_DEV).

Каждый скан для настроенных площадок/монет/сторон (по умолчанию Bybit, USDT, покупка и продажа):
  1) по виртуальному объявлению прошлого скана оцениваем исполнение за интервал (СЛАБЫЙ прокси, ниже);
  2) исполнения ведём в позицию лотами FIFO: встречные ноги закрывают друг друга — это круги
     «покупка + продажа» с P&L и комиссиями (MAKER_FEE: Bybit 0.3% с объявления на покупку);
  3) заново ставим объявления: цена мейкера (`p2p.maker_quote` — обогнать лучшее на шаг MAKER_TICK), зажатая
     в коридор ±SIM_MAKER_BAND% от ориентира; место в очереди — `p2p.maker_place` по `Snapshot.book`;
  4) позицию между ногами оцениваем по ориентиру (риск запаса), считаем просадку.

Прокси исполнения — СЛАБЫЙ, так и помечен в отчёте. Между двумя сканами смотрим конкурентов нашей очереди
с ценой не хуже нашей ДЛЯ НАС (объявление на покупку: их цена ≤ нашей; на продажу: ≥ нашей): убыль доступного
остатка у оставшихся и остаток пропавших (не больше одного ордера на верх их лимита и на свой лот; хвост полной
страницы не считаем). Раз тейкер сделку по такой цене сделал, наша цена (для него лучше или равна) ему бы тоже
подошла. Наша доля этого потока — SIM_MAKER_CAPTURE, делённая на место в очереди. Слабости:
  - убыль остатка — не обязательно сделка: мерчант правит объём, ордер в работе отменят и объём вернётся;
  - пропажа — не обязательно распродажа: мерчант офлайн, снял объявление, выпал с первой страницы (20 шт.),
    остаток стал меньше суммы запроса (Bybit отдаёт только объявления под сумму круга);
  - тейкер выбирает не только по цене: банк, лимиты, репутация (у нового рекламодателя её нет) — доля потока
    SIM_MAKER_CAPTURE взята наугад, не из данных;
  - нет обратной связи: настоящее объявление забрало бы часть потока и сдвинуло бы конкурентов (ценовая война),
    а симуляция видит стакан без него;
  - сделки у объявлений вне первой страницы и между сканами, не изменившие остаток на снимке, не видны вовсе;
  - монеты кроме USDT берутся из кеша (ALT_INTERVAL): поток приходит рывками.
Поэтому статистика — оценка сверху/снизу неизвестной точности, а не факт; переход «бумага → кнопка» — только
по порогу плана и после проверки на реальных объявлениях владельцем.

SQLite data/sim_maker.db:
  fills  — оценённые исполнения: площадка, монета, сторона, цена, количество, комиссия, ориентир, место в очереди,
           поток (убыль / пропажа), сколько объявление ждало в очереди;
  rounds — закрытые круги: количество, цены покупки/продажи, комиссии, P&L, спред, сколько держали позицию;
  kv     — состояние по (площадка, монета): виртуальные объявления с прошлым стаканом, лоты позиции, счётчики.
"""
import html
import json
import os
import sqlite3
import statistics
import time

import p2p

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "sim_maker.db")

SIDES = {"buy": "buy_ad", "sell": "sell_ad"}          # SIM_MAKER_SIDES -> тип объявления (как в p2p.maker_quote)
QUEUE_SIDE = {"buy_ad": "sell", "sell_ad": "buy"}     # в какой очереди Snapshot.book стоит своё объявление
SIDE_LABELS = {"buy_ad": "покупка", "sell_ad": "продажа"}
NAMES = {"bybit": "Bybit", "mexc": "MEXC", "htx": "HTX", "kucoin": "KuCoin", "bitpapa": "BitPapa", "lbank": "LBank"}
PAUSE_LABELS = {"book": "нет стакана", "ref": "нет ориентира", "band": "вне коридора", "limit": "лимит позиции",
                "spread": "узкий спред"}
EPS = 1e-9
MIN_LOT_SHARE = 0.01   # остаток объявления меньше 1% лота — не выставляем (и такой «хвост» исполнения не пишем)
PAGE_FULL = 20         # выдача площадки — одна страница (Bybit size=20): полная страница может вытеснять хвост
PAGE_TAIL = 3          # пропажу последних объявлений полной страницы потоком не считаем
DAY = 86400
GATE_DAYS = 30         # порог плана «бумага → кнопка»: ≥ 30 дней в плюсе после комиссии


def _num(name, default, cast=float):
    """Число из .env; мусор — значение по умолчанию (отчёт не должен падать из-за опечатки)."""
    try:
        return cast(os.getenv(name, "").strip() or default)
    except ValueError:
        return cast(default)


def _list(name, default):
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def enabled():
    """SIM_MAKER=1 — считать бумажного мейкера на каждом скане. По умолчанию выключен."""
    return os.getenv("SIM_MAKER", "0").strip().lower() in ("1", "true", "yes", "on")


def settings(cfg=None):
    """Настройки из .env — читаются при каждом обращении (после load_env), как paper.settings."""
    return {
        "on": enabled(),
        "venues": [NAMES.get(v.lower(), v) for v in _list("SIM_MAKER_EX", "Bybit")],
        "assets": [a.upper() for a in _list("SIM_MAKER_ASSETS", "USDT")],
        "sides": [SIDES[s.lower()] for s in _list("SIM_MAKER_SIDES", "buy,sell") if s.lower() in SIDES],
        # лот в фиате; по умолчанию — сумма круга: выдача Bybit собрана под неё, так своё объявление видно тем же тейкерам
        "amount": _num("SIM_MAKER_AMOUNT", getattr(cfg, "amount", 50000)),
        "max_lots": max(1, _num("SIM_MAKER_MAX_LOTS", 1, int)),   # предел позиции в лотах (в обе стороны)
        "band": _num("SIM_MAKER_BAND", 2.0),                        # коридор цены, ±% от ориентира
        "min_spread": _num("SIM_MAKER_MIN_SPREAD", 0.0),            # % спреда круга после комиссии, чтобы набирать позицию
        "capture": min(1.0, max(0.0, _num("SIM_MAKER_CAPTURE", 0.5))),   # доля прокси-потока на 1-м месте
        "max_gap": _num("SIM_MAKER_MAX_GAP", 180),                  # сек: дольше между сканами — интервал не оцениваем
    }


# --- чистые функции: цена, место, прокси потока, позиция ---

def band_price(post_side, quote, ref, band):
    """Цена объявления в коридоре ±band% от ориентира `ref`. Цена мейкера за коридором в сторону переплаты
    (покупка выше, продажа ниже) — сдвигаем на границу; если коридор нарушен в другую сторону (весь стакан
    далеко от ориентира и граница сделала бы нас щедрее рынка) — None, объявление не ставим.
    Возвращает (цена, сдвинута ли) или None."""
    lo, hi = ref * (1 - band / 100), ref * (1 + band / 100)
    if post_side == "buy_ad":
        price = min(quote, hi)
        return None if price < lo - EPS else (price, price < quote - EPS)
    if post_side == "sell_ad":
        price = max(quote, lo)
        return None if price > hi + EPS else (price, price > quote + EPS)
    raise ValueError(post_side)


def queue_rows(queue):
    """Компактная копия очереди для сравнения на следующем скане: [ник, цена, остаток монеты, верх лимита ₽]."""
    return [[a.nick, a.price, a.avail, a.max_amt] for a in queue]


def flow(prev_rows, cur_rows, post_side, price, cap_rub=None):
    """СЛАБЫЙ прокси потока тейкеров между двумя сканами, в монете: по конкурентам очереди с ценой не хуже
    нашей для нас (`price`; на покупку — их цена ≤, на продажу — ≥) убыль остатка у оставшихся и остаток
    пропавших — не больше одного ордера на верх их лимита и на `cap_rub` (свой лот). Пропавшие из хвоста полной
    страницы (последние PAGE_TAIL из ≥ PAGE_FULL) не считаем: их могли просто вытеснить со страницы. Ники без
    имени и с несколькими объявлениями в очереди пропускаем — их не сопоставить. Возвращает (убыль, пропажа)."""
    def count(rows):
        c = {}
        for r in rows:
            c[r[0]] = c.get(r[0], 0) + 1
        return c

    before, after = count(prev_rows), count(cur_rows)
    now = {r[0]: r for r in cur_rows}
    tail = len(prev_rows) - PAGE_TAIL if len(prev_rows) >= PAGE_FULL else len(prev_rows)
    drop = gone = 0.0
    for i, (nick, p, avail, max_amt) in enumerate(prev_rows):
        if not nick or before[nick] > 1 or after.get(nick, 0) > 1:
            continue
        if not (p <= price + EPS if post_side == "buy_ad" else p >= price - EPS):
            continue   # впереди нас: их поток до нас не дошёл
        cur = now.get(nick)
        if cur is not None:
            drop += max(0.0, avail - cur[2])
        elif i < tail and p > 0:
            gone += min(avail, max_amt / p, cap_rub / p if cap_rub else avail)
    return drop, gone


def fill_qty(open_qty, place, drop, gone, capture):
    """Оценка исполнения своего объявления: доля `capture` прокси-потока, делённая на место в очереди,
    не больше остатка объявления."""
    return min(open_qty, capture / max(1, place) * (drop + gone))


def open_qty(post_side, pos, lot_qty, max_lots):
    """Остаток объявления по текущей позиции `pos` (монета, + куплено, − продано из своего запаса):
    против позиции — закрыть её (не больше лота), по направлению или с нуля — набрать до max_lots лотов.
    Возвращает (количество, набирает ли позицию)."""
    sign = 1 if post_side == "buy_ad" else -1
    if pos * sign < -EPS:
        return min(lot_qty, abs(pos)), False
    return max(0.0, min(lot_qty, max_lots * lot_qty - abs(pos))), True


def apply_fill(lots, post_side, qty, price, fee_unit, ts):
    """Исполнение в позицию: лоты [количество со знаком, цена, комиссия ₽ на монету, время] по FIFO.
    Встречное исполнение закрывает старые лоты — это круги «покупка + продажа». Возвращает (лоты, [круги])."""
    sign = 1 if post_side == "buy_ad" else -1
    lots = [list(x) for x in lots]
    rounds, left = [], qty
    while left > EPS and lots and lots[0][0] * sign < 0:
        lot = lots[0]
        m = min(left, abs(lot[0]))
        buy, sell = (price, lot[1]) if sign > 0 else (lot[1], price)
        fees = m * (lot[2] + fee_unit)
        rounds.append({"qty": m, "buy_price": buy, "sell_price": sell, "fees": fees,
                       "pnl": m * (sell - buy) - fees, "spread_pct": (sell / buy - 1) * 100,
                       "hold_s": ts - lot[3], "first": "sell" if sign > 0 else "buy"})
        lot[0] += sign * m
        left -= m
        if abs(lot[0]) <= EPS:
            lots.pop(0)
    if left > EPS:
        lots.append([sign * left, price, fee_unit, ts])
    return lots, rounds


def position(lots):
    return sum(x[0] for x in lots)


def mark(lots, ref):
    """Позиция по ориентиру: (нереализованный P&L от движения цены, комиссии открытых лотов), ₽."""
    return sum(x[0] * (ref - x[1]) for x in lots), sum(abs(x[0]) * x[2] for x in lots)


def fee_pct(ex, post_side):
    return p2p.MAKER_FEE.get(ex, {}).get(post_side, 0.0)


# --- шаг по снимку ---

def _new_state(now):
    return {"ads": {}, "lots": [], "realized": 0.0, "fees": 0.0, "peak": 0.0, "max_dd": 0.0, "worst_inv": 0.0,
            "first_ts": now, "last_ts": now, "ref": None, "quotes": {}, "active_s": {}, "paused": {}}


def step(st, snap, ex, asset, s, now):
    """Один скан для (площадка `ex`, монета `asset`): исполнения по объявлениям прошлого скана, позиция и круги,
    новые объявления, оценка позиции. Меняет состояние `st` на месте; возвращает (исполнения, круги) для базы."""
    fills, rounds = [], []
    ads = st["ads"]
    for side in list(ads):
        if side not in s["sides"]:
            ads.pop(side)   # сторону убрали из настроек
    book = snap.book or {}
    # 1) исполнения за интервал: объявление прошлого скана против свежего стакана той же очереди
    for side in s["sides"]:
        ad, queue = ads.get(side), book.get((ex, QUEUE_SIDE[side], asset))
        if not ad or queue is None:
            continue
        gap = now - ad["ts"]
        if gap > s["max_gap"]:
            continue   # долгий разрыв (площадка молчала, бот стоял) — поток за него не приписываем
        st["active_s"][side] = st["active_s"].get(side, 0.0) + gap
        drop, gone = flow(ad["rows"], queue_rows(queue), side, ad["price"], s["amount"])
        q = fill_qty(ad["qty"], ad["place"], drop, gone, s["capture"])
        if q < ad["lot"] * MIN_LOT_SHARE:
            continue
        fee_unit = ad["price"] * fee_pct(ex, side) / 100
        st["lots"], closed = apply_fill(st["lots"], side, q, ad["price"], fee_unit, now)
        st["fees"] += q * fee_unit
        ad["qty"] -= q
        fills.append({"ts": now, "ex": ex, "asset": asset, "side": side, "price": ad["price"], "qty": q,
                      "fee": q * fee_unit, "ref": st["ref"], "place": ad["place"], "total": ad["total"],
                      "flow_drop": drop, "flow_gone": gone, "wait_s": now - ad["since"]})
        ad["since"] = now   # время в очереди считаем заново с этого исполнения
        for r in closed:
            st["realized"] += r["pnl"]
            rounds.append(dict(r, ts=now, ex=ex, asset=asset))
    # 2) новые объявления по свежему снимку
    ref = (snap.refs or {}).get(asset)
    quotes = {side: p2p.maker_quote(snap.groups or {}, ex, asset, side) for side in ("buy_ad", "sell_ad")}
    banded = {side: band_price(side, q[0], ref, s["band"]) if q and ref else None for side, q in quotes.items()}
    prices = {side: (banded[side] or (quotes[side][0],))[0] for side in quotes if quotes[side]}
    net = None   # спред круга после комиссии по своим ценам, %
    if len(prices) == 2:
        net = ((prices["sell_ad"] / prices["buy_ad"] - 1) * 100
               - fee_pct(ex, "buy_ad") - fee_pct(ex, "sell_ad"))
    pos = position(st["lots"])
    for side in s["sides"]:
        queue = book.get((ex, QUEUE_SIDE[side], asset))
        if queue is None:
            continue   # площадка не ответила: настоящее объявление стояло бы дальше — ждём свежий стакан
        if not ref:
            reason = "ref"
        elif not quotes[side]:
            reason = "book"
        elif not banded[side]:
            reason = "band"
        else:
            lot = s["amount"] / ref
            qty, opening = open_qty(side, pos, lot, s["max_lots"])
            if qty < lot * MIN_LOT_SHARE:
                reason = "limit"
            elif opening and (net is None or net < s["min_spread"]):
                reason = "spread"
            else:
                reason = None
        if reason:
            ads.pop(side, None)
            paused = st["paused"].setdefault(side, {})
            paused[reason] = paused.get(reason, 0) + 1
            continue
        price, clamped = banded[side]
        place, total, _ = p2p.maker_place(queue, price, side)
        prev = ads.get(side)
        since = prev["since"] if prev and now - prev["ts"] <= s["max_gap"] else now   # после разрыва — заново
        ads[side] = {"price": price, "qty": qty, "lot": lot, "place": place, "total": total, "ts": now,
                     "since": since, "clamped": clamped, "rows": queue_rows(queue)}
        n, places, n_clamped = st["quotes"].get(side, (0, 0, 0))
        st["quotes"][side] = (n + 1, places + place, n_clamped + int(clamped))
    # 3) позиция по ориентиру и просадка
    if ref:
        st["ref"] = ref
    if st["ref"]:
        unreal, open_fees = mark(st["lots"], st["ref"])
        equity = st["realized"] + unreal - open_fees
        st["peak"] = max(st["peak"], equity)
        st["max_dd"] = max(st["max_dd"], st["peak"] - equity)
        st["worst_inv"] = min(st["worst_inv"], unreal)
    st["last_ts"] = now
    return fills, rounds


# --- хранилище ---

def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ex TEXT, "
                "asset TEXT, side TEXT, price REAL, qty REAL, fee REAL, ref REAL, place INTEGER, total INTEGER, "
                "flow_drop REAL, flow_gone REAL, wait_s REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, ex TEXT, "
                "asset TEXT, qty REAL, buy_price REAL, sell_price REAL, fees REAL, pnl REAL, spread_pct REAL, "
                "hold_s REAL, first TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    return con


def _load(con, key, now):
    row = con.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else _new_state(now)


def on_scan(snap, cfg=None, path=DB_PATH, now=None):
    """Хук скана (bot.scan_loop при SIM_MAKER=1): шаг симуляции по всем настроенным площадкам и монетам,
    запись в data/sim_maker.db. Никаких запросов к бирже — только снимок `snap`."""
    s = settings(cfg)
    now = time.time() if now is None else now
    con = _connect(path)
    try:
        with con:
            for ex in s["venues"]:
                for asset in s["assets"]:
                    key = f"{ex}|{asset}"
                    st = _load(con, key, now)
                    fills, rounds = step(st, snap, ex, asset, s, now)
                    con.executemany("INSERT INTO fills (ts, ex, asset, side, price, qty, fee, ref, place, total, "
                                    "flow_drop, flow_gone, wait_s) VALUES (:ts, :ex, :asset, :side, :price, :qty, "
                                    ":fee, :ref, :place, :total, :flow_drop, :flow_gone, :wait_s)", fills)
                    con.executemany("INSERT INTO rounds (ts, ex, asset, qty, buy_price, sell_price, fees, pnl, "
                                    "spread_pct, hold_s, first) VALUES (:ts, :ex, :asset, :qty, :buy_price, "
                                    ":sell_price, :fees, :pnl, :spread_pct, :hold_s, :first)", rounds)
                    con.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(st)))
    finally:
        con.close()


# --- статистика и отчёт /maker paper ---

def stats(path=DB_PATH):
    """Сводка по каждой (площадка, монета) из базы: исполнения в день, спред кругов, P&L, время в очереди,
    место, просадка. [] — базы ещё нет."""
    if not os.path.exists(path):
        return []
    con = _connect(path)
    try:
        out = []
        for key, value in con.execute("SELECT key, value FROM kv ORDER BY key"):
            st = json.loads(value)
            ex, asset = key.split("|", 1)
            fills = con.execute("SELECT side, qty, flow_drop, flow_gone, wait_s FROM fills WHERE ex = ? AND asset = ?",
                                (ex, asset)).fetchall()
            rounds = con.execute("SELECT qty, buy_price, pnl, spread_pct, hold_s FROM rounds WHERE ex = ? AND asset = ?",
                                 (ex, asset)).fetchall()
            days = (st["last_ts"] - st["first_ts"]) / DAY
            unreal, open_fees = mark(st["lots"], st["ref"]) if st["ref"] else (0.0, 0.0)
            quotes = {side: q for side, q in st["quotes"].items()}
            out.append({
                "ex": ex, "asset": asset, "days": days, "ads": st["ads"], "pos": position(st["lots"]),
                "fills": len(fills), "fills_gone": sum(1 for f in fills if f[2] <= EPS < f[3]),
                "fills_per_day": len(fills) / days if days >= 1 / 24 else None,
                "rounds": len(rounds),
                "spread_gross": statistics.mean(r[3] for r in rounds) if rounds else None,
                "spread_net": (sum(r[2] for r in rounds) / sum(r[0] * r[1] for r in rounds) * 100) if rounds else None,
                "hold_min": statistics.median(r[4] for r in rounds) / 60 if rounds else None,
                "wait_min": statistics.median(f[4] for f in fills) / 60 if fills else None,
                "place": {side: q[1] / q[0] for side, q in quotes.items() if q[0]},
                "clamped": {side: q[2] / q[0] for side, q in quotes.items() if q[0]},
                "active": {side: t / (days * DAY) for side, t in st["active_s"].items() if days > 0},
                "paused": st["paused"],
                "realized": st["realized"], "fees": st["fees"], "unreal": unreal, "open_fees": open_fees,
                "total": st["realized"] + unreal - open_fees, "max_dd": st["max_dd"], "worst_inv": st["worst_inv"],
                "fee_known": ex in p2p.MAKER_FEE,
            })
        return out
    finally:
        con.close()


def _rub(x):
    return "0 ₽" if abs(x) < 0.5 else f"{x:+,.0f}".replace(",", " ") + " ₽"


def _qty(x, asset):
    if abs(x) < 1e-9:
        return f"0 {asset}"
    return f"{x:+,.2f}".replace(",", " ") + f" {asset}" if abs(x) >= 1 else f"{x:+.6g} {asset}"


def report_view(cfg=None, path=DB_PATH):
    """Текст «/maker paper» (только владельцу): настройки, текущие виртуальные объявления и статистика
    с явной пометкой, что исполнения — слабый прокси, а не сделки."""
    s = settings(cfg)
    esc = html.escape
    lines = ["🧪 <b>Бумажный мейкер</b> — симуляция своих объявлений, реальных объявлений и сделок нет", "",
             ("Статус: ✅ считается на каждом скане" if s["on"] else
              "Статус: ⏸ выключен — включить SIM_MAKER=1 в .env на ПК"),
             f"Настройки: {esc(', '.join(s['venues']))} · {esc(', '.join(s['assets']))} · "
             f"{', '.join(SIDE_LABELS[x] for x in s['sides']) or 'стороны не заданы'} · лот {p2p._money(s['amount'])} ₽ · "
             f"до {s['max_lots']} лот. · коридор ±{s['band']:g}% от ориентира · доля потока {s['capture']:.0%}",
             "⚠️ Исполнения — <b>слабый прокси</b>: убыль объёма у конкурентов с ценой не хуже нашей между сканами. "
             "Это не сделки: объём правят, объявления снимают, тейкер выбирает и по банку, и по репутации.", ""]
    rows = stats(path)
    if not rows:
        lines.append("Данных пока нет." + ("" if s["on"] else " Симуляция выключена."))
        return "\n".join(lines)
    for r in rows:
        a = r["asset"]
        lines.append(f"<b>{esc(r['ex'])} {esc(a)}</b> — {r['days']:.1f} дн. наблюдений")
        now = []
        for side in ("buy_ad", "sell_ad"):
            ad = r["ads"].get(side)
            if ad:
                now.append(f"{SIDE_LABELS[side]} {p2p._price(ad['price'])} ₽ (место {ad['place']} из {ad['total']}"
                           f"{', у границы коридора' if ad.get('clamped') else ''})")
        lines.append(f"Сейчас: {'; '.join(now) or 'объявлений нет'} · позиция {_qty(r['pos'], a)}")
        per_day = f"{r['fills_per_day']:.1f}/день" if r["fills_per_day"] is not None else "мало данных"
        lines.append(f"Исполнений (прокси): {r['fills']} ({per_day}), из них по пропавшим объявлениям {r['fills_gone']}")
        if r["rounds"]:
            lines.append(f"Кругов: {r['rounds']} · спред брутто ср. {r['spread_gross']:.2f}% · "
                         f"нетто после комиссии {r['spread_net']:.2f}% · держали позицию медиана {r['hold_min']:.0f} мин")
        else:
            lines.append("Кругов покупка+продажа пока нет")
        lines.append(f"P&L: круги {_rub(r['realized'])} (комиссии всего {_rub(-r['fees'])}) · позиция по ориентиру "
                     f"{_rub(r['unreal'] - r['open_fees'])} · итого <b>{_rub(r['total'])}</b>")
        if not r["fee_known"]:
            lines.append(f"Комиссия мейкера {esc(r['ex'])} неизвестна — считаю 0")
        queue = []
        for side in ("buy_ad", "sell_ad"):
            if side in r["place"]:
                queue.append(f"{SIDE_LABELS[side]}: ср. место {r['place'][side]:.1f}, стояло "
                             f"{r['active'].get(side, 0):.0%} времени, у границы коридора {r['clamped'][side]:.0%}")
        if r["wait_min"] is not None:
            queue.append(f"ожидание до исполнения медиана {r['wait_min']:.0f} мин")
        if queue:
            lines.append("Очередь: " + "; ".join(queue))
        pauses = [f"{SIDE_LABELS.get(side, side)} — " + ", ".join(f"{PAUSE_LABELS.get(k, k)} {n}" for k, n in p.items())
                  for side, p in r["paused"].items() if p]
        if pauses:
            lines.append("Без объявления (сканов): " + "; ".join(pauses))
        lines.append(f"Риск позиции: худшая оценка запаса {_rub(r['worst_inv'])} · макс. просадка итога "
                     f"{_rub(-r['max_dd'])}")
        lines.append(f"Порог «бумага → кнопка»: ≥ {GATE_DAYS} дней в плюсе после комиссии — сейчас "
                     f"{r['days']:.1f} дн., итог {_rub(r['total'])}")
        lines.append("")
    return "\n".join(lines).rstrip()
