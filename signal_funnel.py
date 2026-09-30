"""Воронка «сигналы → сделки» для /stats: сколько отправленных сигналов (history.signals) закончились реальной
сделкой (trades.db) и как быстро. Только чтение обеих баз, ничего не пишет и не меняет торговую логику.

Сопоставление жадное, один сигнал — одна сделка: для каждой сделки (по возрастанию времени) среди ещё не занятых
сигналов того же направления (площадка/монета покупки и продажи) берётся тот, чей эпизод покрывает время сделки
(от первого сигнала эпизода до last_seen + WINDOW_S), с самым поздним signal_ts; сделка без пары — off_signal.
"""
import html
import os
import sqlite3
import statistics
import time

import history
import trades

DAYS_MAX = 30       # сколько дней сигналов держит history.signals
WINDOW_S = 3600
TOP_DIRECTIONS = 5


def _select(path, sql, args):
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def funnel(days=7, window_s=WINDOW_S, hist_path=None, trades_path=None, now=None):
    """{days, signals, traded, conversion, median_lag_s, off_signal, by_direction}."""
    hist_path = hist_path or history.DB_PATH
    trades_path = trades_path or trades.DB_PATH
    now = time.time() if now is None else now
    days = min(max(int(days), 1), DAYS_MAX)
    since = now - days * 86400

    sig_rows = _select(
        hist_path,
        "SELECT buy_ex, buy_asset, sell_ex, sell_asset, signal_ts, last_seen FROM signals "
        "WHERE signalled = 1 AND signal_ts IS NOT NULL AND signal_ts >= ? AND signal_ts <= ? "
        "ORDER BY signal_ts, id", (since, now))
    trade_rows = _select(
        trades_path,
        "SELECT buy_ex, buy_asset, sell_ex, sell_asset, ts FROM trades WHERE ts >= ? AND ts <= ? ORDER BY ts, id",
        (since, now))

    signals_by_key = {}
    for buy_ex, buy_asset, sell_ex, sell_asset, sig_ts, last_seen in sig_rows:
        key = (buy_ex, buy_asset, sell_ex, sell_asset)
        signals_by_key.setdefault(key, []).append({"sig_ts": sig_ts, "last_seen": last_seen, "used": False})

    traded_by_key = {}
    traded = off_signal = 0
    lags = []
    for buy_ex, buy_asset, sell_ex, sell_asset, ts in trade_rows:
        key = (buy_ex, buy_asset, sell_ex, sell_asset)
        best = None
        for sig in signals_by_key.get(key, ()):
            if sig["used"]:
                continue
            if sig["sig_ts"] <= ts <= max(sig["last_seen"], sig["sig_ts"]) + window_s:
                if best is None or sig["sig_ts"] > best["sig_ts"]:
                    best = sig
        if best is None:
            off_signal += 1
            continue
        best["used"] = True
        traded += 1
        lags.append(ts - best["sig_ts"])
        traded_by_key[key] = traded_by_key.get(key, 0) + 1

    signals_total = len(sig_rows)
    by_direction = sorted(
        ({"key": key, "signals": len(lst), "traded": traded_by_key.get(key, 0),
          "conversion": traded_by_key.get(key, 0) / len(lst)} for key, lst in signals_by_key.items()),
        key=lambda d: (-d["signals"], d["key"]))[:TOP_DIRECTIONS]

    return {"days": days, "signals": signals_total, "traded": traded,
            "conversion": (traded / signals_total) if signals_total else None,
            "median_lag_s": statistics.median(lags) if lags else None,
            "off_signal": off_signal, "by_direction": by_direction}


def lines(data):
    """Строки для /stats: [] — за окно не было отправленных сигналов (блок не показываем)."""
    if not data["signals"]:
        return []
    out = ["", f"<b>Сигналы → сделки за {data['days']} дн.</b>"]
    pct = (data["conversion"] or 0.0) * 100
    summary = f"{data['signals']} сигналов, {data['traded']} сделок ({pct:.0f}%)"
    if data["median_lag_s"] is not None:
        minutes = max(1, round(data["median_lag_s"] / 60))
        summary += f", медиана до сделки {minutes} мин"
    summary += f"; вне сигнала: {data['off_signal']}"
    out.append(summary)
    worst = next((d for d in data["by_direction"] if d["traded"] == 0), None)
    if worst is not None:
        buy_ex, buy_asset, sell_ex, sell_asset = worst["key"]
        out.append(f"чаще всего без ответа: {html.escape(buy_ex)} {html.escape(buy_asset)} → "
                   f"{html.escape(sell_ex)} {html.escape(sell_asset)} ({worst['signals']} сигналов, 0 сделок)")
    return out
