"""Журнал сделок: SQLite data/trades.db — время, связка, сумма, расчётный %. Пишется по кнопке «✅ Сделал».
Факт (реальный результат сделки) — необязательное поле `fact`, вводится кнопками или числом после
«✅ Сделал»; `/stats` показывает расчёт vs факт."""
import datetime
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "trades.db")
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}
MSK = datetime.timezone(datetime.timedelta(hours=3))
# Банк по названию способа оплаты на площадках: каноническое имя → куски названий (латиница/кириллица,
# нижний регистр). Площадки пишут по-разному: «Tinkoff»/«T-Bank»/«Т-Банк», «VTB Bank», «OZON Bank»…
BANK_ALIASES = {
    "T-Bank": ("t-bank", "tbank", "tinkoff", "т-банк", "тинькоф"),
    "Sberbank": ("sber", "сбер"),
    "Alfa-bank": ("alfa", "альфа"),
    "VTB": ("vtb", "втб"),
    "Rosselkhozbank": ("rosselkhoz", "rshb", "россельхоз", "рсхб"),
    "MTS Bank": ("mts", "мтс"),
    "Raiffeisen": ("raiffeisen", "райффайзен"),
    "Gazprombank": ("gazprom", "газпром"),
    "Ozon Bank": ("ozon", "озон"),
    "Yandex Bank": ("yandex", "яндекс"),
    "PSB": ("promsvyaz", "psb", "промсвязь", "псб"),
    "Sovcombank": ("sovcom", "sovkom", "совком", "halva", "халва"),
    "Pochta Bank": ("pochta", "почта"),
    "Rosbank": ("rosbank", "росбанк"),
    "OTP Bank": ("otp", "отп"),
    "Uralsib": ("uralsib", "уралсиб"),
    "Ak Bars": ("ak bars", "акбарс", "ак барс"),
    "Russian Standard": ("russian standard", "русский стандарт"),
    "Otkritie": ("otkritie", "открытие"),
    "Home Credit": ("home credit", "хоум"),
}
BANK_NAMES = {"T-Bank": "Т-Банк", "Sberbank": "Сбер", "Alfa-bank": "Альфа", "VTB": "ВТБ", "Rosselkhozbank": "Россельхоз",
              "MTS Bank": "МТС Банк", "Raiffeisen": "Райффайзен", "Gazprombank": "Газпромбанк", "Ozon Bank": "Озон Банк",
              "Yandex Bank": "Яндекс Банк", "PSB": "ПСБ", "Sovcombank": "Совкомбанк", "SBP": "СБП"}
SBP_WORDS = ("sbp", "сбп", "fast bank transfer", "fast payment")
# Свои банки владельца (OWN_BANKS в .env): с них платим по СБП — в этом порядке; «*» — у владельца есть карта
# и в любом другом банке, так что перевод мерчанту в любой распознанный банк идёт внутри банка.
DEFAULT_OWN_BANKS = "T-Bank,Sberbank,Alfa-bank,VTB,Rosselkhozbank,MTS Bank,*"
# Бесплатный лимит СБП другим людям — 100 тыс. ₽ в календарный месяц НА КАЖДЫЙ банк-отправитель (правило ЦБ),
# дальше комиссия до 0.5%. Банки дают больше: ВТБ с 01.07.2026 — 300 тыс. на базовом тарифе, Т-Банк с Pro —
# 300 тыс., с Premium — без лимита. Свой тариф — SBP_FREE_LIMITS в .env («T-Bank:300000,VTB:inf»).
BANK_LIMIT = 100_000.0
DEFAULT_FREE_LIMITS = {"VTB": 300_000.0}
SBP_OVER_FEE = 0.5  # % — комиссия банка сверх бесплатного лимита СБП (до 0.5%)
# Без единицы измерения короткое число похоже на проценты (типичный профит связки — единицы процентов),
# длинное — на рубли (типичная сумма выигрыша за круг — сотни-тысячи ₽).
FACT_PLAIN_AS_PERCENT_MAX = 50.0
_FACT_PCT = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*%$")
_FACT_RUB = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)\s*(?:₽|руб\.?|р\.?)$", re.I)
_FACT_PLAIN = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)$")


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS trades ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, route TEXT, "
                "buy_ex TEXT, buy_asset TEXT, sell_ex TEXT, sell_asset TEXT, "
                "amount REAL, profit REAL, bank TEXT DEFAULT '', fact REAL DEFAULT NULL, kind TEXT DEFAULT '')")
    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
    if "bank" not in cols:
        con.execute("ALTER TABLE trades ADD COLUMN bank TEXT DEFAULT ''")
    if "fact" not in cols:
        con.execute("ALTER TABLE trades ADD COLUMN fact REAL DEFAULT NULL")
    if "kind" not in cols:   # '' — старые записи: банк брался из объявления и всегда считался в лимит СБП
        con.execute("ALTER TABLE trades ADD COLUMN kind TEXT DEFAULT ''")
    return con


def parse_fact(text, amount):
    """Фактический результат сделки из текста: «+1.2%»/«−0.5%» — проценты как есть; «650 ₽»/«-300 руб» —
    сумма в фиате, переводится в % от `amount`; голое число без единицы — проценты, если |x| не больше
    `FACT_PLAIN_AS_PERCENT_MAX`, иначе тоже сумма в ₽. None — не разобрано (мусор) или `amount` пустой
    для рублёвого варианта."""
    t = re.sub(r"\s+", "", (text or "").strip()).replace(",", ".")
    m = _FACT_PCT.match(t)
    if m:
        return float(m.group(1))
    m = _FACT_RUB.match(t)
    if m:
        return float(m.group(1)) / amount * 100 if amount else None
    m = _FACT_PLAIN.match(t)
    if m:
        val = float(m.group(1))
        return val if abs(val) <= FACT_PLAIN_AS_PERCENT_MAX else (val / amount * 100 if amount else None)
    return None


def bank_of(pay):
    """Каноническое имя банка по названию способа оплаты («Tinkoff», «Т-Банк» → T-Bank); '' — не банк."""
    pl = (pay or "").lower()
    for bank, keys in BANK_ALIASES.items():
        if any(k in pl for k in keys):
            return bank
    return ""


def is_sbp(pay):
    return any(w in (pay or "").lower() for w in SBP_WORDS)


def own_banks():
    """(свои банки по порядку, есть ли «*» — карта и в любом другом банке) из OWN_BANKS."""
    items = [x.strip() for x in os.getenv("OWN_BANKS", DEFAULT_OWN_BANKS).split(",") if x.strip()]
    return [bank_of(x) or x for x in items if x != "*"], "*" in items


def free_limit(bank):
    """Бесплатный лимит СБП банка в месяц, ₽ (inf — без лимита): SBP_FREE_LIMITS, иначе DEFAULT_FREE_LIMITS/BANK_LIMIT."""
    limits = dict(DEFAULT_FREE_LIMITS)
    for part in os.getenv("SBP_FREE_LIMITS", "").split(","):
        name, _, val = part.partition(":")
        try:
            limits[bank_of(name) or name.strip()] = float(val)
        except ValueError:
            continue   # пустой или кривой кусок — пропускаем
    return limits.get(bank, BANK_LIMIT)


def pay_plan(pays, over=frozenset(), own=None):
    """Как владелец заплатит мерчанту (kind, bank): «intra» — у мерчанта есть банк владельца, перевод внутри банка,
    лимит СБП не тратится; «sbp» — мерчант принимает СБП или чужой банк: по СБП со своего банка — первого по
    OWN_BANKS, у которого лимит ещё не исчерпан (over), иначе с первого; ("", "") — способ неизвестен."""
    listed, star = own if own is not None else own_banks()
    banks = [b for b in map(bank_of, pays) if b]
    mine = next((b for b in banks if star or b in listed), "")
    if mine:
        return "intra", mine
    if banks or any(map(is_sbp, pays)):
        pool = listed or ["SBP"]
        return "sbp", next((b for b in pool if b not in over), pool[0])
    return "", ""


def pay_label(kind, bank, pays=()):
    """Как платим — для карточек: «внутри банка (Т-Банк)», «СБП с Т-Банк», иначе способы оплаты мерчанта."""
    name = BANK_NAMES.get(bank, bank)
    if kind == "intra":
        return f"внутри банка ({name})"
    if kind == "sbp":
        return f"СБП с {name}"
    return ", ".join(list(pays)[:3]) or "—"


def _month_start(ts):
    dt = datetime.datetime.fromtimestamp(ts)
    return datetime.datetime(dt.year, dt.month, 1).timestamp()


def _day_start(ts):
    """Начало календарных суток по МСК, в которые попадает `ts`."""
    dt = datetime.datetime.fromtimestamp(ts, MSK)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def bank_month_total(bank, path=DB_PATH, now=None):
    """Сумма отправленного со своего банка по СБП с начала текущего календарного месяца (переводы внутри банка
    лимит не тратят и не считаются; старые записи без kind — считаются, как и раньше)."""
    if not bank or not os.path.exists(path):
        return 0.0
    now = time.time() if now is None else now
    con = _connect(path)
    total, = con.execute("SELECT COALESCE(SUM(amount), 0) FROM trades WHERE bank = ? AND ts >= ? "
                         "AND kind IN ('sbp', '')", (bank, _month_start(now))).fetchone()
    con.close()
    return total


def banks_over_limit(banks, path=DB_PATH, now=None):
    """Из списка банков — те, что уже набрали свой бесплатный лимит СБП за календарный месяц (free_limit)."""
    return {b for b in banks if b and bank_month_total(b, path, now) >= free_limit(b)}


def log_trade(d, amount, path=DB_PATH, ts=None):
    """Записать сделку: d — (profit %, buy Ad, sell Ad, маршрут), amount — сумма круга в фиате. Как платили —
    pay_plan (внутри банка или СБП со своего банка, у которого лимит ещё есть). Возвращает (id сделки — для
    ввода факта, банк, сумма по СБП за месяц с этой сделкой, пересёк ли этой сделкой бесплатный лимит банка)."""
    profit, b, s, route = d
    ts = ts if ts is not None else time.time()
    kind, bank = pay_plan(b.pays, over=banks_over_limit(own_banks()[0], path, ts))
    prev = bank_month_total(bank, path, ts) if kind == "sbp" else 0.0
    con = _connect(path)
    with con:
        cur = con.execute("INSERT INTO trades (ts, route, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit, bank, "
                          "kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (ts, route, b.ex, b.asset, s.ex, s.asset, amount, profit, bank, kind))
    trade_id = cur.lastrowid
    con.close()
    total = prev + amount if kind == "sbp" else 0.0
    crossed = kind == "sbp" and prev < free_limit(bank) <= total
    return trade_id, bank, total, crossed


def get_trade(trade_id, path=DB_PATH):
    """{"amount", "profit"} сделки по id (нужна сумма круга, чтобы перевести факт в ₽ в проценты) —
    None, если сделки нет."""
    if not os.path.exists(path):
        return None
    con = _connect(path)
    row = con.execute("SELECT amount, profit FROM trades WHERE id = ?", (trade_id,)).fetchone()
    con.close()
    return {"amount": row[0], "profit": row[1]} if row else None


def set_fact(trade_id, fact_percent, path=DB_PATH):
    """Записать фактический результат (%) для сделки; True — сделка найдена и обновлена."""
    con = _connect(path)
    with con:
        cur = con.execute("UPDATE trades SET fact = ? WHERE id = ?", (fact_percent, trade_id))
    con.close()
    return cur.rowcount > 0


# Автосопоставление истории биржи (accounts.account_history) со сделками журнала для автозаполнения факта.
AUTO_MATCH_WINDOW = 1800          # сек — запись истории считается той самой сделкой, если она не дальше по времени
AUTO_MATCH_AMOUNT_TOLERANCE = 0.25  # 25% — насколько сумма записи истории (в ₽) может отличаться от суммы круга


def unmatched(path=DB_PATH, since=None):
    """Сделки без введённого факта (для автосопоставления) не старше `since` (epoch, по умолчанию — все).
    [{"id", "ts", "buy_ex", "buy_asset", "sell_ex", "sell_asset", "amount", "profit"}, ...]."""
    if not os.path.exists(path):
        return []
    since = 0.0 if since is None else since
    con = _connect(path)
    rows = con.execute("SELECT id, ts, buy_ex, buy_asset, sell_ex, sell_asset, amount, profit FROM trades "
                       "WHERE fact IS NULL AND ts >= ? ORDER BY ts", (since,)).fetchall()
    con.close()
    return [{"id": r[0], "ts": r[1], "buy_ex": r[2], "buy_asset": r[3], "sell_ex": r[4],
             "sell_asset": r[5], "amount": r[6], "profit": r[7]} for r in rows]


def _match_leg(hist, asset, side, ts, want_fiat, window, amount_tolerance):
    """Ближайшая по времени запись истории биржи (`accounts.account_history`) для одной ноги сделки:
    та же монета, та же сторона (buy/sell), цена есть (депозиты/выводы её не несут — не подтверждают
    цену исполнения), сумма в ₽ (amount*price) не дальше `amount_tolerance` от суммы круга сделки,
    само время — не дальше `window` секунд от времени сделки. Кандидатов несколько — берём ближайший
    по времени. Ничего не подошло — None."""
    asset = (asset or "").upper()
    best, best_dt = None, None
    for it in hist or []:
        if (it.get("asset") or "").upper() != asset or it.get("side") != side:
            continue
        price = it.get("price") or 0
        if price <= 0 or abs(it.get("ts", 0) - ts) > window:
            continue
        fiat = it.get("amount", 0) * price
        if want_fiat and abs(fiat - want_fiat) > want_fiat * amount_tolerance:
            continue
        dt = abs(it["ts"] - ts)
        if best is None or dt < best_dt:
            best, best_dt = it, dt
    return best


def match_fact(trade, hist_by_ex, window=AUTO_MATCH_WINDOW, amount_tolerance=AUTO_MATCH_AMOUNT_TOLERANCE):
    """Реализованный % прибыли по истории подключённых бирж для сделки журнала (`unmatched`), если в
    истории нашлась и покупка, и продажа той же монеты рядом по времени и сумме — иначе None (нет ключа
    у нужной биржи в этом опросе, движения ещё не видно, или сумма/время слишком не совпадают).
    `hist_by_ex` — {биржа (в нижнем регистре): список записей `accounts.account_history` за этот опрос}."""
    buy_hist = hist_by_ex.get((trade["buy_ex"] or "").lower())
    sell_hist = hist_by_ex.get((trade["sell_ex"] or "").lower())
    if buy_hist is None or sell_hist is None:
        return None
    buy = _match_leg(buy_hist, trade["buy_asset"], "buy", trade["ts"], trade["amount"], window, amount_tolerance)
    sell = _match_leg(sell_hist, trade["sell_asset"], "sell", trade["ts"], trade["amount"], window, amount_tolerance)
    if not buy or not sell:
        return None
    return (sell["price"] / buy["price"] - 1) * 100


def stats(path=DB_PATH, now=None):
    """{"day"/"week"/"month": {"count", "amount", "avg_profit", "fact_count", "avg_fact", "avg_diff"}} —
    для /stats. «day» — календарные сутки по МСК, «month» — календарный месяц (как счётчик лимита СБП),
    «week» — последние 7 суток. `fact_count`/`avg_fact`/`avg_diff` — только по сделкам, где введён факт
    (avg_diff = среднее факт-расчёт, п.п.); при отсутствии таких сделок avg_fact/avg_diff — None."""
    now = time.time() if now is None else now
    starts = {"day": _day_start(now), "week": now - PERIODS["week"], "month": _month_start(now)}
    out = {p: {"count": 0, "amount": 0.0, "avg_profit": 0.0, "fact_count": 0, "avg_fact": None, "avg_diff": None}
           for p in PERIODS}
    if not os.path.exists(path):
        return out
    con = _connect(path)
    for period, start in starts.items():
        count, amount, avg_profit = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0), COALESCE(AVG(profit), 0) FROM trades WHERE ts >= ?",
            (start,)).fetchone()
        fact_count, avg_fact, avg_diff = con.execute(
            "SELECT COUNT(*), AVG(fact), AVG(fact - profit) FROM trades WHERE ts >= ? AND fact IS NOT NULL",
            (start,)).fetchone()
        out[period] = {"count": count, "amount": amount, "avg_profit": avg_profit,
                       "fact_count": fact_count, "avg_fact": avg_fact, "avg_diff": avg_diff}
    con.close()
    return out
