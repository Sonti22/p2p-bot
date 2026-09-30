"""Отчёт качества сигналов для /signals (только владелец): доля пропущенных длинных эпизодов против цели
TARGET_MISSED, те же окно/gap/порог длинного эпизода, что у history.signal_stats, причины пропуска, задержка
сигнала и топ-5 направлений по пропускам. Только чтение локальной SQLite (data/history.db), без сети и без
импорта bot: чистые функции build()/render()."""
import html
import math
import os
import statistics
import time

import history

TARGET_MISSED = 0.10
_LONG_SECONDS = 3 * 60   # min_minutes=3 — как в history.signal_stats


def _clamp_days(days):
    """1..30 (RETENTION в history.cleanup = 30 дней); не число или мусор — 7 по умолчанию."""
    try:
        n = int(days)
    except (TypeError, ValueError):
        return 7
    return max(1, min(30, n))


def _percentile90(values):
    """p90 методом nearest-rank: индекс ceil(0.9*n)-1 отсортированного списка (для [5, 10, 60] — 60)."""
    ordered = sorted(values)
    idx = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[idx]


def _sorted_reasons(counts):
    """Порядок: по убыванию счётчика, при равенстве — по SIGNAL_REASONS, неизвестные причины ('?' и т.п.) — вниз."""
    order = {r: i for i, r in enumerate(history.SIGNAL_REASONS)}
    return sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], len(order)), kv[0]))


def build(path=history.DB_PATH, days=7, now=None):
    """Данные отчёта за `days` дней (1..30, по умолчанию 7, мусор — 7). Окно, gap и порог длинного эпизода —
    те же, что у history.signal_stats: last_seen >= now - days*86400, gap = history._cooldown(), длинный эпизод —
    от 180 с. Задержка сигнала считается по сырым строкам (signalled=1, signal_ts не NULL, signal_ts - first_seen),
    а не по слитым эпизодам — для слитых или перезапущенных после рестарта эпизодов это приближение."""
    now = time.time() if now is None else now
    days = _clamp_days(days)
    gap = history._cooldown()
    rows = []
    if os.path.exists(path):
        con = history._connect(path)
        try:
            rows = con.execute(
                "SELECT buy_ex, buy_asset, sell_ex, sell_asset, first_seen, last_seen, signalled, "
                "reason_not_signalled, signal_ts FROM signals WHERE last_seen >= ? "
                "ORDER BY buy_ex, buy_asset, sell_ex, sell_asset, first_seen, id",
                (now - days * 86400,)).fetchall()
        finally:
            con.close()
    data = {"empty": not rows, "days": days, "long": 0, "excluded": 0, "missed": 0, "eligible": 0,
            "missed_share": None, "reasons": [], "delay_median": None, "delay_p90": None, "top_directions": []}
    if not rows:
        return data
    delays = [r[8] - r[4] for r in rows if r[6] and r[8] is not None]
    if delays:
        data["delay_median"] = statistics.median(delays)
        data["delay_p90"] = _percentile90(delays)
    reasons = {}
    by_direction = {}
    for ep in history._merged_episodes([(r[:4], *r[4:8]) for r in rows], gap):
        if ep["last"] - ep["first"] < _LONG_SECONDS:
            continue
        data["long"] += 1
        if ep["signalled"]:
            continue
        reason = ep["reason"] or "?"
        if reason in history.NOT_MISSED:
            data["excluded"] += 1
            continue
        data["missed"] += 1
        reasons[reason] = reasons.get(reason, 0) + 1
        by_direction[ep["key"]] = by_direction.get(ep["key"], 0) + 1
    data["eligible"] = data["long"] - data["excluded"]
    if data["eligible"]:
        data["missed_share"] = data["missed"] / data["eligible"]
    data["reasons"] = _sorted_reasons(reasons)
    data["top_directions"] = sorted(by_direction.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return data


def render(data):
    """Текст /signals (HTML, названия площадок и монет экранированы html.escape). Пустая БД или нет строк в
    окне — одна строка без разметки."""
    if data["empty"]:
        return "за период сигналов нет"
    lines = []
    eligible, missed = data["eligible"], data["missed"]
    if eligible:
        pct = missed / eligible * 100
        marker = "✅ в пределах цели" if pct <= TARGET_MISSED * 100 else "⚠️ выше цели"
        lines.append(f"Пропущено: {missed} из {eligible} длинных = {pct:.1f}% "
                     f"(цель ≤{TARGET_MISSED * 100:.0f}%, {marker})")
    else:
        lines.append("Пропущено: нет подходящих эпизодов")
    if data["reasons"]:
        lines.append("")
        lines.append("<b>Причины пропуска:</b>")
        for reason, count in data["reasons"]:
            lines.append(f"• {html.escape(str(reason))}: {count}")
    if data["delay_median"] is not None:
        lines.append("")
        lines.append(f"<b>Задержка сигнала:</b> медиана {data['delay_median']:.0f} с, "
                     f"p90 {data['delay_p90']:.0f} с")
    if data["top_directions"]:
        lines.append("")
        lines.append("<b>Топ-5 направлений пропусков:</b>")
        for (buy_ex, buy_asset, sell_ex, sell_asset), count in data["top_directions"]:
            lines.append(f"{html.escape(buy_ex)} {html.escape(buy_asset)} -> {html.escape(sell_ex)} "
                         f"{html.escape(sell_asset)}: {count}")
    return "\n".join(lines)
