"""Автоматическая репутация мерчанта — подсказка в карточке сигнала, без автоблокировки (блэклист — только кнопкой).

Два источника, только чтение баз бота:
- paper.db (сухой прогон): сколько кругов шло через мерчанта и сколько из них сорвалось на его стадии (покупка — у
  мерчантов покупки, продажа — у мерчанта продажи); «ушёл до оплаты» — срыв, где объявление мерчанта исчезло
  (стадия покупки: «объявление покупки исчезло», «часть мерчантов покупки ушла…»).
- snapshots.db (снимки сканов): как часто мерчант меняет цену своих объявлений — смен в час присутствия.

Бот пересчитывает LABELS раз в REFRESH секунд в отдельном потоке (refresh); p2p.fmt_signal показывает метку у ника
(label). Порогов достаточно, чтобы метка появлялась только при накопленной истории.
"""
import json
import os
import sqlite3
import time

REFRESH = 1800            # сек — пересчёт меток
WINDOW = 14 * 86400       # окно истории: снимки хранятся 14 дней
MAX_SCANS = 400           # снимков на пересчёт, равномерно по окну — чтение не дольше пары секунд
MIN_CYCLES = 3            # доля срывов — только после стольких кругов через мерчанта
FAIL_SHARE = 0.34         # доля срывов, с которой показываем
CHANGES_PER_HOUR = 6.0    # смен цены в час, с которой показываем
MIN_HOURS = 1.0           # частота смены цены — только после стольких часов присутствия в снимках
GONE_MARKS = ("исчезл", "мерчантов покупки ушл")   # срыв «мерчант пропал» (пояснения paper.check_buy_stage), не
# «цена ушла» — это смена цены, а не уход мерчанта

LABELS = {}               # (площадка, ник) -> текст метки; пишет refresh, читает p2p.fmt_signal
_state = {"ts": 0.0}


def _empty():
    return {"cycles": 0, "failed": 0, "gone": 0, "changes": 0, "hours": 0.0}


def from_paper(since, path=None):
    """{(площадка, ник): статистика} по кругам сухого прогона, начатым с since и уже завершённым."""
    if path is None:
        import paper
        path = paper.DB_PATH
    out = {}
    if not os.path.exists(path):
        return out
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT buy_ex, buy_nick, buy_nicks, sell_ex, sell_nick, result, note FROM cycles "
                           "WHERE result IS NOT NULL AND ts_start >= ?", (since,)).fetchall()
    except sqlite3.OperationalError:   # старая или пустая база без таблицы/колонок
        rows = []
    finally:
        con.close()
    for buy_ex, buy_nick, buy_nicks, sell_ex, sell_nick, result, note in rows:
        try:
            nicks = json.loads(buy_nicks or "[]") or [buy_nick]
        except ValueError:
            nicks = [buy_nick]
        gone = any(m in (note or "").lower() for m in GONE_MARKS)
        sides = [(buy_ex, n, "failed_buy") for n in dict.fromkeys(nicks) if n] + \
                ([(sell_ex, sell_nick, "failed_sell")] if sell_nick else [])
        for ex, nick, stage in sides:
            rec = out.setdefault((ex, nick), _empty())
            rec["cycles"] += 1
            if result == stage:
                rec["failed"] += 1
                rec["gone"] += 1 if gone else 0
    return out


def from_snapshots(since, until=None, path=None, max_scans=MAX_SCANS):
    """{(площадка, ник): статистика} по снимкам сканов: changes — сколько раз цена объявления мерчанта
    (площадка, сторона, монета, id объявления) сменилась между соседними прочитанными снимками, hours — сколько
    часов объявления мерчанта были в снимках (от первого до последнего, по каждому объявлению). Снимков больше
    max_scans — берём равномерно (смены между пропущенными не видны — оценка снизу)."""
    import snapshots
    path = snapshots.DB_PATH if path is None else path
    ids = snapshots.ids(path, since, until)
    if len(ids) > max_scans:
        step = len(ids) / max_scans
        ids = [ids[int(i * step)] for i in range(max_scans)]
    seen = {}   # (ex, side, asset, nick, ad_id) -> [цена, первый ts, последний ts, смен]
    for sid in ids:
        scan = snapshots.load(sid, path)
        if not scan:
            continue
        ts = scan.get("ts") or sid / 1000
        for a in snapshots.ads_of(scan):
            if not a.nick or a.ex == "BestChange":
                continue
            key = (a.ex, a.side, a.asset, a.nick, a.ad_id or "")
            rec = seen.get(key)
            if rec is None:
                seen[key] = [a.price, ts, ts, 0]
                continue
            if a.price != rec[0]:
                rec[0], rec[3] = a.price, rec[3] + 1
            rec[2] = ts
    out = {}
    for (ex, _side, _asset, nick, _ad), (_p, first, last, changes) in seen.items():
        rec = out.setdefault((ex, nick), _empty())
        rec["changes"] += changes
        rec["hours"] += (last - first) / 3600
    return out


def merge(*parts):
    out = {}
    for part in parts:
        for key, rec in part.items():
            cur = out.setdefault(key, _empty())
            for k, v in rec.items():
                cur[k] += v
    return out


def label_text(rec):
    """Метка по статистике мерчанта; None — показывать нечего (мало истории или всё в порядке)."""
    bits = []
    if rec["cycles"] >= MIN_CYCLES and rec["failed"] / rec["cycles"] >= FAIL_SHARE:
        bits.append(f"срывы {rec['failed']} из {rec['cycles']} кругов")
    if rec["gone"]:
        bits.append(f"ушёл до оплаты ×{rec['gone']}")
    if rec["hours"] >= MIN_HOURS:
        rate = rec["changes"] / rec["hours"]
        if rate >= CHANGES_PER_HOUR:
            bits.append(f"цена меняется ~{rate:.0f}/ч")
    return "🧾 " + ", ".join(bits) if bits else None


def compute(now=None, paper_path=None, snap_path=None):
    """{(площадка, ник): метка} по обоим источникам за WINDOW."""
    now = time.time() if now is None else now
    stats = merge(from_paper(now - WINDOW, paper_path), from_snapshots(now - WINDOW, now, snap_path))
    return {k: t for k, t in ((k, label_text(r)) for k, r in stats.items()) if t}


def refresh(now=None, paper_path=None, snap_path=None):
    """Пересчитать LABELS (вызывать в отдельном потоке); возвращает число мерчантов с меткой. Время отмечаем до
    чтения баз: сбой не повторяется каждый скан, а ждёт следующего REFRESH."""
    _state["ts"] = time.time() if now is None else now
    labels = compute(now, paper_path, snap_path)
    LABELS.clear()
    LABELS.update(labels)
    return len(labels)


def due(now=None):
    now = time.time() if now is None else now
    return now - _state["ts"] >= REFRESH


def label(ad):
    """Метка мерчанта объявления (у стакана — первого мерчанта с меткой) или None."""
    if not LABELS:
        return None
    for nick in (ad.nicks or (ad.nick,)):
        text = LABELS.get((ad.ex, nick))
        if text:
            return text if len(ad.nicks or ()) <= 1 else f"{text} ({nick})"
    return None
