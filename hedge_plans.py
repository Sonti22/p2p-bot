"""План хеджа реальных сделок — только запись «что открыл бы бот», без ордеров и без торгового ядра.

По кнопке «✅ Сделал» (Bot.mark_done) для сделки с монетой из HEDGE_ASSETS считаем тем же simperp.choose, что и хедж
сухого прогона: площадку (Bybit/BingX) с меньшей ожидаемой стоимостью шорта перпа, символ, объём (монета сделки ×
HEDGE_RATIO, округление до лота), ожидаемую стоимость (комиссии + спред − фандинг за HEDGE_HOLD_MINUTES) и
остаточный запас, — и пишем в таблицу hedge_plans журнала сделок (data/trades.db). Хеджа нет (котировки нет, лот не
даёт коэффициент, мало глубины) — строка с причиной. Нужна статистика для решения о хедже реальных сделок
(docs/hedge-design.md, раздел 9): как часто хедж возможен и сколько бы стоил. Показ — в /paper report.
"""
import json
import time

import simperp
import trades

COLUMNS = ("id", "trade_id", "ts", "asset", "coin_qty", "amount", "venue", "symbol", "qty", "ratio", "cost_pct",
           "cost_usdt", "funding_usdt", "residual_pct", "alt", "note")


def _connect(path):
    con = trades._connect(path)   # тот же файл, что журнал сделок: таблица trades уже заведена
    con.execute("CREATE TABLE IF NOT EXISTS hedge_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, "
                "ts REAL, asset TEXT, coin_qty REAL, amount REAL, venue TEXT DEFAULT '', symbol TEXT DEFAULT '', "
                "qty REAL DEFAULT NULL, ratio REAL DEFAULT NULL, cost_pct REAL DEFAULT NULL, "
                "cost_usdt REAL DEFAULT NULL, funding_usdt REAL DEFAULT NULL, residual_pct REAL DEFAULT NULL, "
                "alt TEXT DEFAULT '', note TEXT DEFAULT '')")
    con.commit()
    return con


def coin_qty(deal, amount):
    """Монета круга (hedge_ref_qty): сумма круга ₽ / цена покупки. Одна формула для плана здесь и реального хеджа
    (trading/hedge.py через bot.mark_done)."""
    _, b, _, _ = deal
    return amount / b.price if b.price > 0 else 0.0


def record(trade_id, deal, amount, ref=0.0, risk=0.0, path=trades.DB_PATH, now=None):
    """Посчитать и записать план хеджа сделки trade_id: deal — (прибыль %, покупка Ad, продажа Ad, маршрут), amount —
    сумма круга ₽, ref — ₽ за USDT (snap.ref), risk — запас на курс монеты, %. Монета не хеджируется или хедж
    выключен (PAPER_HEDGE=0, PERPS=0) — ничего не пишем, None; иначе id записи."""
    _, b, _, _ = deal
    qty = coin_qty(deal, amount)
    plan, note = simperp.choose(b.asset, qty, amount, ref, b.price, risk=risk, now=now)
    if plan is None and not note:
        return None
    now = time.time() if now is None else now
    row = {"trade_id": trade_id, "ts": now, "asset": (b.asset or "").upper(), "coin_qty": qty, "amount": amount,
           "note": note if plan is None else ""}
    if plan:
        row.update(venue=plan["venue"], symbol=plan["symbol"], qty=plan["qty"], ratio=plan["ratio"],
                   cost_pct=plan["cost_pct"], cost_usdt=plan["cost"], funding_usdt=plan["funding"],
                   residual_pct=plan["residual_pct"], alt=json.dumps(plan.get("alt") or {}, ensure_ascii=False))
    con = _connect(path)
    with con:
        cur = con.execute(f"INSERT INTO hedge_plans ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          list(row.values()))
    con.close()
    return cur.lastrowid


def rows(path=trades.DB_PATH, limit=None):
    """Записи планов, новые первыми."""
    con = _connect(path)
    sql = f"SELECT {', '.join(COLUMNS)} FROM hedge_plans ORDER BY id DESC" + (f" LIMIT {int(limit)}" if limit else "")
    out = [dict(zip(COLUMNS, r)) for r in con.execute(sql)]
    con.close()
    return out


def report_lines(path=trades.DB_PATH, last=3):
    """Строки для /paper report: сколько сделок с планом хеджа, средняя ожидаемая стоимость, причины «без хеджа» и
    последние планы. Записей нет — пусто."""
    all_rows = rows(path)
    if not all_rows:
        return []
    planned = [r for r in all_rows if r["venue"]]
    lines = ["", f"🛡 <b>Хедж реальных сделок — план</b> (ордеров нет): сделок {len(all_rows)}, хедж возможен "
                 f"в {len(planned)}"]
    if planned:
        avg = sum(r["cost_pct"] for r in planned) / len(planned)
        by_venue = {}
        for r in planned:
            by_venue[r["venue"]] = by_venue.get(r["venue"], 0) + 1
        lines.append(f"ожидаемая стоимость в среднем {avg:.2f}% суммы круга; площадка: "
                     + ", ".join(f"{v} {n}" for v, n in sorted(by_venue.items(), key=lambda kv: -kv[1])))
    reasons = {}
    for r in all_rows:
        if not r["venue"]:
            reasons[r["note"] or "?"] = reasons.get(r["note"] or "?", 0) + 1
    for note, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:3]:
        lines.append(f"без хеджа {n}×: {note}")
    for r in all_rows[:last]:
        lines.append(f"сделка #{r['trade_id']} {r['asset']}: " + (
            f"шорт {r['qty']:g} на {r['venue']} ({r['symbol']}), {r['cost_pct']:.2f}%" if r["venue"]
            else f"без хеджа — {r['note']}"))
    return lines
