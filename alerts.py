"""Алерты на целевой курс: /alert USDT sell 92 7d — сообщить, когда надёжный покупатель/обменник
даёт нужную цену, до истечения срока. SQLite data/alerts.db. По умолчанию одноразовые: срабатывают
один раз и удаляются. С «repeat <кулдаун>» алерт не удаляется по сроку — ждёт следующего скана и
может сработать снова не раньше, чем пройдёт кулдаун с прошлого срабатывания.
Условия можно объединять через «И»: «vol <сумма>» — в стакане по цене не хуже порога должно набираться
не меньше этой суммы (p2p._stack на отфильтрованном по цене срезе snap.groups); «reliable» — метка
p2p.reliability() встречной связки (лучшая по snap.deals с этим же объявлением) не хуже «⚠️ риск»,
т.е. не «🪤 ловушка». Нет объявления встречной связки для reliable — алерт не срабатывает: подтвердить
надёжность нечем.

Алерт на связку (kind="route"): /alert route Bybit MEXC USDT 3% 7d — сообщить, когда связка покупка на buy_ex →
продажа на sell_ex по монете asset (одна монета с обеих сторон) даст не меньше rate % чистыми, как в сигнале
(snap.deals), со своим порогом и сроком. Условия «reliable» и «repeat» — те же; «vol» не нужен: связка уже
посчитана на сумму круга. Строка в той же таблице: side="route", rate — порог %, buy_ex/sell_ex — площадки."""
import os
import re
import sqlite3
import time

import p2p

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "alerts.db")
DURATIONS = {"h": 3600, "d": 86400, "w": 7 * 86400}
MAX_DURATION = 90 * 86400  # дольше 90 дней смысла не имеет — курс успеет уйти куда угодно


def _connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS alerts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, asset TEXT, side TEXT, "
                "rate REAL, created_ts REAL, expires_ts REAL, "
                "repeat_cooldown REAL, last_fired_ts REAL, min_volume REAL, require_reliable INTEGER)")
    cols = {row[1] for row in con.execute("PRAGMA table_info(alerts)")}
    if "repeat_cooldown" not in cols:   # апгрейд базы, созданной до режима «повторно»
        con.execute("ALTER TABLE alerts ADD COLUMN repeat_cooldown REAL")
    if "last_fired_ts" not in cols:
        con.execute("ALTER TABLE alerts ADD COLUMN last_fired_ts REAL")
    if "min_volume" not in cols:   # апгрейд базы, созданной до условий «И» (объём/надёжность)
        con.execute("ALTER TABLE alerts ADD COLUMN min_volume REAL")
    if "require_reliable" not in cols:
        con.execute("ALTER TABLE alerts ADD COLUMN require_reliable INTEGER")
    for col in ("kind", "buy_ex", "sell_ex"):   # апгрейд базы до алертов на связку; '' / NULL — алерт на курс
        if col not in cols:
            con.execute(f"ALTER TABLE alerts ADD COLUMN {col} TEXT DEFAULT ''")
    con.commit()
    return con


def parse_duration(text):
    """«7d» / «12h» / «2w» → секунды; None — не разобрано или вне диапазона (0; 90 дней]."""
    m = re.fullmatch(r"(\d+)([hdw])", (text or "").strip().lower())
    if not m:
        return None
    sec = int(m.group(1)) * DURATIONS[m.group(2)]
    return sec if 0 < sec <= MAX_DURATION else None


def add(chat_id, asset, side, rate, expires_ts, path=DB_PATH, repeat_cooldown=None,
        min_volume=None, require_reliable=False):
    """Создать алерт, вернуть его id. repeat_cooldown (сек) — режим «повторно»: алерт не удаляется
    по срабатыванию, а может сработать снова не раньше, чем пройдёт кулдаун; None — одноразовый.
    min_volume (₽) и require_reliable — дополнительные условия через «И» (см. модульный docstring)."""
    con = _connect(path)
    with con:
        cur = con.execute("INSERT INTO alerts (chat_id, asset, side, rate, created_ts, expires_ts, "
                          "repeat_cooldown, min_volume, require_reliable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (chat_id, asset, side, rate, time.time(), expires_ts, repeat_cooldown,
                           min_volume, int(require_reliable)))
        alert_id = cur.lastrowid
    con.close()
    return alert_id


def add_route(chat_id, buy_ex, sell_ex, asset, pct, expires_ts, path=DB_PATH, repeat_cooldown=None,
              require_reliable=False):
    """Создать алерт на связку buy_ex → sell_ex по asset с порогом pct % чистыми, вернуть id."""
    con = _connect(path)
    with con:
        cur = con.execute("INSERT INTO alerts (chat_id, asset, side, rate, created_ts, expires_ts, repeat_cooldown, "
                          "min_volume, require_reliable, kind, buy_ex, sell_ex) "
                          "VALUES (?, ?, 'route', ?, ?, ?, ?, NULL, ?, 'route', ?, ?)",
                          (chat_id, asset, pct, time.time(), expires_ts, repeat_cooldown, int(require_reliable),
                           buy_ex, sell_ex))
        alert_id = cur.lastrowid
    con.close()
    return alert_id


_RATE_ONLY = "COALESCE(kind, '') != 'route'"


def _prune_expired(con, now):
    con.execute("DELETE FROM alerts WHERE expires_ts <= ?", (now,))


def list_all(chat_id, path=DB_PATH, now=None):
    """Активные алерты чата [(id, asset, side, rate, expires_ts, repeat_cooldown, min_volume,
    require_reliable), ...] — для /alerts; попутно чистит истёкшие."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return []
    con = _connect(path)
    with con:
        _prune_expired(con, now)
    rows = con.execute("SELECT id, asset, side, rate, expires_ts, repeat_cooldown, min_volume, "
                       f"require_reliable FROM alerts WHERE chat_id = ? AND {_RATE_ONLY} ORDER BY id",
                       (chat_id,)).fetchall()
    con.close()
    return rows


def list_routes(chat_id, path=DB_PATH, now=None):
    """Активные алерты на связку чата [(id, buy_ex, sell_ex, asset, pct, expires_ts, repeat_cooldown,
    require_reliable), ...] — для /alerts."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return []
    con = _connect(path)
    with con:
        _prune_expired(con, now)
    rows = con.execute("SELECT id, buy_ex, sell_ex, asset, rate, expires_ts, repeat_cooldown, require_reliable "
                       "FROM alerts WHERE chat_id = ? AND kind = 'route' ORDER BY id", (chat_id,)).fetchall()
    con.close()
    return rows


def remove(alert_id, chat_id, path=DB_PATH):
    """Удалить алерт (только своего чата — id снаружи виден лишь владельцу через /alerts)."""
    con = _connect(path)
    with con:
        con.execute("DELETE FROM alerts WHERE id = ? AND chat_id = ?", (alert_id, chat_id))
    con.close()


def _volume_ok(snap, ad, side, asset, rate, min_volume):
    """В стакане площадки ad.ex по цене не хуже rate должно набираться не меньше min_volume ₽ —
    переиспользуем p2p._stack на срезе snap.groups, отфильтрованном по цене (у обменника — и по его сети)."""
    grp = p2p._same_net(snap.groups.get((ad.ex, side, asset), []), ad)
    qualifying = [a for a in grp if (a.price >= rate if side == "sell" else a.price <= rate)]
    return p2p._stack(qualifying, min_volume) is not None


def _matching_deal(snap, ad, side, asset):
    """Лучшая связка из snap.deals (отсортирован по прибыли × надёжности), использующая объявление с той же
    площадки и стороны, что сработавшее ad — для оценки метки надёжности алерта на одну сторону."""
    for deal in snap.deals:
        cand = deal[1] if side == "buy" else deal[2]
        if cand.ex == ad.ex and cand.asset == asset:
            return deal
    return None


def _candidate_ok(snap, cfg, ad, side, asset, rate, min_volume, require_reliable):
    """Все условия «И» для одной площадки: цена не хуже порога, объём стакана, надёжность встречной связки."""
    if not (ad.price >= rate if side == "sell" else ad.price <= rate):
        return False
    if min_volume and not _volume_ok(snap, ad, side, asset, rate, min_volume):
        return False
    if require_reliable:
        deal = _matching_deal(snap, ad, side, asset)
        if deal is None or p2p.reliability(deal, cfg, snap)[0] == p2p.TRAP:
            return False
    return True


def due(snap, cfg, path=DB_PATH, now=None):
    """Сработавшие алерты по текущему снимку: [(id, chat_id, asset, side, rate, price, Ad), ...].
    Цена берётся из snap.best (объявления уже прошли фильтры usable() — мин. сделок/отзывов, отсев
    аномалий, блэклист). Сами алерты здесь не меняются: после доставки сообщения бот вызывает
    mark_fired — одноразовый удаляется, у «повторно» начинается кулдаун; не доставили — алерт сработает
    снова на следующем скане. Истёкшие (expires_ts) удаляются в любом режиме. min_volume/require_reliable —
    дополнительные условия через «И» (см. модульный docstring): все условия проверяются на каждой площадке,
    срабатывает лучшая по цене из прошедших."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return []
    con = _connect(path)
    with con:
        _prune_expired(con, now)
    rows = con.execute("SELECT id, chat_id, asset, side, rate, repeat_cooldown, last_fired_ts, "
                       f"min_volume, require_reliable FROM alerts WHERE {_RATE_ONLY}").fetchall()
    fired = []
    for alert_id, chat_id, asset, side, rate, cooldown, last_fired, min_volume, require_reliable in rows:
        if cooldown is not None and last_fired is not None and now - last_fired < cooldown:
            continue   # алерт «повторно» ещё «отдыхает» после прошлого срабатывания
        # сначала отбираем площадки, прошедшие все условия, потом лучшую по цене среди них —
        # иначе лучшая по цене, но без объёма/надёжности, заслоняет подходящую вторую
        best_ad = None
        for (ex, ad_side, ad_asset), ad in snap.best.items():
            if ad_side != side or ad_asset != asset or ad.stale:   # stale — площадка не ответила за VENUE_TIMEOUT
                continue
            if not _candidate_ok(snap, cfg, ad, side, asset, rate, min_volume, require_reliable):
                continue
            if best_ad is None or (ad.price > best_ad.price if side == "sell" else ad.price < best_ad.price):
                best_ad = ad
        if best_ad is not None:
            fired.append((alert_id, chat_id, asset, side, rate, best_ad.price, best_ad))
    con.close()
    return fired


def route_due(snap, cfg, path=DB_PATH, now=None):
    """Сработавшие алерты на связку: [(id, chat_id, buy_ex, sell_ex, asset, pct, связка)]. Связка — лучшая по
    прибыли из snap.deals с этими площадками и монетой (с обеих сторон), не по устаревшим данным площадки
    (p2p.deal_stale), с прибылью ≥ порога и, при reliable, не «🪤 ловушка». Как в due(): алерт помечается
    сработавшим только после доставки (mark_fired), истёкшие удаляются."""
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return []
    con = _connect(path)
    with con:
        _prune_expired(con, now)
    rows = con.execute("SELECT id, chat_id, buy_ex, sell_ex, asset, rate, repeat_cooldown, last_fired_ts, "
                       "require_reliable FROM alerts WHERE kind = 'route'").fetchall()
    con.close()
    fired = []
    for alert_id, chat_id, buy_ex, sell_ex, asset, pct, cooldown, last_fired, require_reliable in rows:
        if cooldown is not None and last_fired is not None and now - last_fired < cooldown:
            continue
        best = None
        for d in snap.deals:
            profit, b, s, _ = d
            if (b.ex, s.ex, b.asset, s.asset) != (buy_ex, sell_ex, asset, asset) or profit < pct or p2p.deal_stale(d):
                continue
            if require_reliable and p2p.reliability(d, cfg, snap)[0] == p2p.TRAP:
                continue
            if best is None or profit > best[0]:
                best = d
        if best is not None:
            fired.append((alert_id, chat_id, buy_ex, sell_ex, asset, pct, best))
    return fired


def mark_fired(alert_id, path=DB_PATH, now=None):
    """Сообщение об алерте доставлено: одноразовый удалить, «повторно» — запомнить время для кулдауна."""
    now = time.time() if now is None else now
    con = _connect(path)
    with con:
        con.execute("DELETE FROM alerts WHERE id = ? AND repeat_cooldown IS NULL", (alert_id,))
        con.execute("UPDATE alerts SET last_fired_ts = ? WHERE id = ?", (now, alert_id))
    con.close()
