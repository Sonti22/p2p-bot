"""Сводный отчёт бэктестов против порогов плана (этап 3) и правила владельца об ускорении (27.09.2026).

Правило владельца: при сильном бэктесте живая бумага сокращается до 7–14 дней (направленная — 30), а режим
«кнопка» с минимальным лотом (20–50 USDT на позицию, дневной убыток до 5 USDT) разрешён сразу после хорошего
бэктеста. Полные лимиты — только после порогов плана. Вердикт GO/NO-GO — только про «кнопку с минимальным лотом».

Запуск: python -m research.report --out <папка> [--offline] [--paper-db путь ...] [--history-db путь]
Пишет backtest_report.json и backtest_report.md. Сеть — только публичные GET к api.bybit.com и open-api.bingx.com.
"""
import argparse
import datetime
import json
import os
import sys
import time

from research import data, directional_bt, funding_bt, hedge_bt

EARN_MARGIN_PP = 3.0
FUND_DD_MAX = 2.0
FUND_MIN_YEARS = 1.0
FUND_MIN_SETTLEMENTS = 90
DIR_GATES = {"oos_months": 12, "trades": 200, "sharpe": 1.0, "pf": 1.2, "p": 0.05}
MIN_LOT_RULE = "кнопка с минимальным лотом (20–50 USDT на позицию, дневной убыток ≤ 5 USDT)"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _d(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d") if ms else "—"


def funding_verdicts(fund):
    out = {}
    for kind in ("spot_perp", "perp_perp"):
        for coin, r in fund.get(kind, {}).items():
            key = f"{kind}:{coin}"
            if not r.get("ok"):
                out[key] = {"go": False, "checks": {}, "reason": r.get("reason")}
                continue
            checks = {
                "история ≥ 12 мес.": r["period"]["years"] >= FUND_MIN_YEARS,
                "выплат в позиции ≥ 90": r["settlements_in_position"] >= FUND_MIN_SETTLEMENTS,
                f"чистая APR ≥ earn + {EARN_MARGIN_PP:g} п.п.": r["net_apr_pct"] >= r["earn_apr_pct"] + EARN_MARGIN_PP,
                f"просадка ≤ {FUND_DD_MAX:g}%": r["max_dd_pct"] <= FUND_DD_MAX,
            }
            out[key] = {"go": all(checks.values()), "checks": checks}
    return out


def directional_verdict(res):
    pf, rnd = res.get("portfolio") or {}, res.get("random") or {}
    if not pf:
        return {"go": False, "checks": {}, "reason": "нет оценки"}
    checks = {
        "вне выборки ≥ 12 мес.": _num(pf.get("months")) >= DIR_GATES["oos_months"],
        "сделок ≥ 200": _num(pf.get("trades")) >= DIR_GATES["trades"],
        "Sharpe ≥ 1": _num(pf.get("sharpe")) >= DIR_GATES["sharpe"],
        "PF ≥ 1.2": _num(pf.get("profit_factor")) >= DIR_GATES["pf"],
        "лучше случайных входов, p < 0.05": _num(rnd.get("p_value")) < DIR_GATES["p"],
    }
    by_coin = {}
    for coin, c in res.get("coins", {}).items():
        if c.get("ok"):
            w = c["walk_forward"]
            by_coin[coin] = {"sharpe_ge_1": _num(w["sharpe"]) >= 1, "pf_ge_1.2": _num(w["profit_factor"]) >= 1.2,
                             "p_lt_0.05": _num((rnd.get("p_value_by_coin") or {}).get(coin)) < 0.05}
    return {"go": all(checks.values()), "checks": checks, "by_coin": by_coin,
            "edge": _num(rnd.get("p_value")) < DIR_GATES["p"] and _num(pf.get("mean_ret_pct")) > 0}


def hedge_verdicts(hres):
    out = {}
    for coin, c in hres.get("coins", {}).items():
        if not c.get("ok"):
            out[coin] = {"go": False, "checks": {}, "reason": c.get("reason")}
            continue
        g = c["gates"]
        checks = {"стоимость ≤ 0.6 × запаса": g["cost_le_0.6_buffer"],
                  "σ с хеджем ≤ 0.5 × без": g["sigma_ratio_le_0.5"],
                  "коэффициент лота 0.9–1.1 в ≥ 95%": g["lot_coef_in_band_95"]}
        out[coin] = {"go": all(checks.values()), "checks": checks,
                     "cycles_to_hedge": c["paper_cycles"], "history_signals": c["history_signals"],
                     "applicable": bool(c["paper_cycles"] or c["history_signals"])}
    return out


def hedge_notes(hres):
    """Пояснения к вердикту хеджа: что дают комиссии ×1 и какие суммы круга проходят по лоту."""
    lines = []
    for coin, c in hres.get("coins", {}).items():
        if not c.get("ok"):
            continue
        buf = c["buffer_pct"]
        x1 = c["main_fees_x1"]["cost_mean_pct"]
        parts = [f"стоимость ×2 {c['main']['cost_mean_pct']}% = {c['main']['cost_to_buffer']} запаса, ×1 {x1}% = "
                 f"{x1 / buf:.2f} запаса (порог 0.6)"]
        for venue, lots in c.get("lots", {}).items():
            parts.append(f"лот {venue}: " + ", ".join(f"{int(a):,} ₽ — {v['in_band_share']:.0%} дней в полосе "
                                                      f"(последний коэф. {v['last_coef']})".replace(",", " ")
                                                      for a, v in lots.items()))
        if not (c["paper_cycles"] or c["history_signals"]):
            parts.append("кругов и сигналов с монетой нет — хеджировать нечего")
        lines.append(f"{coin}: " + "; ".join(parts))
    return lines


def doubts(ds, fund, direc, hres):
    notes = [
        "Комиссии taker взяты ×2 (запас). С реальными ×1 результаты лучше — они в таблицах справочно, "
        "вердикт — по ×2.",
        "Ставка earn — параметр отчёта (по умолчанию 5% годовых), а не загруженная ставка Bybit Earn.",
        "Часовые свечи: стопы, базис и хедж считаются по часовым open/high/low/close; внутри часа путь цены "
        "неизвестен. Стоп исполняется по цене стопа с проскальзыванием 0.02–0.05% — в резком рынке хуже.",
        "Перп–перп: переводы маржи между биржами — мгновенно за 1 USDT; в жизни это часы и риск ликвидации "
        "одной ноги при резком движении.",
        "Спот+перп: залог спотом в едином счёте не моделируется; вместо этого ребалансировка при ±25% "
        "(полное закрытие и открытие) — консервативно.",
        "Хедж: реальная длительность P2P-круга неизвестна — в бумаге 9 мин (это настройки симуляции). Окна < 60 мин "
        "оценены масштабом √t. Модель покрывает только курс монеты; риск P2P-цены (стакан, мерчант) хедж не снимает.",
        "Toncoin переименован в GRAM: перп TONUSDT закрыт 15.06.2026 (поставка), GRAMUSDT с 22.06.2026 — "
        "неделя без перпа; у BingX история TON-перпа только с 03.07.2026.",
    ]
    cov = data.coverage(ds)
    bx = cov.get("BTC", {}).get("bingx") or []
    if bx and bx[0]["klines"].get("first"):
        notes.append(f"BingX: свечи есть только с {bx[0]['klines']['first'][:10]}; раньше цена BingX для базиса "
                     f"перп–перп — markPrice из истории фандинга (раз в 8 ч).")
    ton_spot = cov.get("TON", {}).get("spot", {})
    if ton_spot.get("missing_steps"):
        notes.append(f"Спот GRAMUSDT: пропущено {ton_spot['missing_steps']} часовых свечей.")
    for coin, c in hres.get("coins", {}).items():
        if c.get("paper_cycles") == 0:
            notes.append(f"Хедж {coin}: в бумаге нет ни одного круга с этой монетой (выборка кругов — 0); "
                         f"сигналов в history.db — {c.get('history_signals')} за {c.get('history_span_days')} дн.")
    notes += [f"Данные: {n}" for n in ds["meta"].get("notes", [])]
    notes.append("Walk-forward выбирает k стопа из сетки {1.5, 2, 3}; сама стратегия (EMA 20/100) распространённая — "
                 "её выбор мог быть навеян общим знанием рынка (неявная подгонка), поэтому p-value важнее Sharpe.")
    return notes


def build(ds, fund, direc, hres, args=None):
    v_f, v_d, v_h = funding_verdicts(fund), directional_verdict(direc), hedge_verdicts(hres)
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "args": args or {},
        "data": {"start": ds["meta"]["start"], "end": ds["meta"]["end"], "coverage": data.coverage(ds),
                 "notes": ds["meta"].get("notes", [])},
        "funding": fund, "directional": direc, "hedge": hres,
        "verdicts": {"funding": v_f, "directional": v_d, "hedge": v_h},
        "doubts": doubts(ds, fund, direc, hres),
    }


def _go(x):
    return "**GO**" if x else "**NO-GO**"


def _checks(ch):
    return "; ".join(f"{k} {'✓' if ok else '✗'}" for k, ok in ch.items()) or "—"


def to_markdown(rep):
    f, d, h, v = rep["funding"], rep["directional"], rep["hedge"], rep["verdicts"]
    L = [f"# Бэктесты этапа 3 — отчёт ({rep['generated_at']})", "",
         "Только история и публичные данные. Ордеров, ключей и подписанных запросов нет. "
         f"Вердикт — про «{MIN_LOT_RULE}» по правилу владельца от 27.09.2026.", "", "## Итог", "",
         "| Стратегия | Вердикт | Что решило |", "|---|---|---|"]
    fund_go = [k for k, x in v["funding"].items() if x["go"]]
    for key, x in v["funding"].items():
        kind, coin = key.split(":")
        title = "Фандинг спот+перп Bybit" if kind == "spot_perp" else "Фандинг перп–перп Bybit/BingX"
        L.append(f"| {title}, {coin} | {_go(x['go'])} | {_checks(x['checks']) if x['checks'] else x.get('reason')} |")
    L.append(f"| Направленная EMA 20/100, 1 ч (портфель BTC/ETH/TON) | {_go(v['directional']['go'])} | "
             f"{_checks(v['directional']['checks'])} |")
    for coin, x in v["hedge"].items():
        extra = f"; кругов с {coin} в бумаге: {x.get('cycles_to_hedge', 0)}" if x.get("checks") else ""
        if x.get("go") and not x.get("applicable"):
            extra += " — пороги пройдены, но хеджировать нечего (нет ни кругов, ни сигналов с монетой)"
        L.append(f"| Хедж кругов, {coin} | {_go(x['go'])} | "
                 f"{_checks(x['checks']) if x['checks'] else x.get('reason')}{extra} |")
    L += ["", "**Что делать по правилу владельца:**", ""]
    L.append("- GO → можно сразу «кнопку» с минимальным лотом; параллельно живая бумага 7–14 дней (направленная — 30); "
             "полные лимиты — только после порогов плана. Код режима «кнопка» (торговое ядро, этап 4) ещё не написан.")
    L.append("- NO-GO → кнопку не включать и бумагу не сокращать. Менять правила под этот отчёт нельзя: новая версия "
             "стратегии — только с новой предрегистрацией и новым бэктестом.")
    if not fund_go and not v["directional"]["go"]:
        L.append("- По деньгам сейчас ни фандинг, ни направленная не проходят: честно — прибыли сверх USDT-earn "
                 "в истории не видно.")
    # данные
    L += ["", "## Данные", "", f"Период запроса: {_d(rep['data']['start'])} … {_d(rep['data']['end'])}.", "",
          "| Монета | Ряд | Символ | С | По | Точек | Пропусков |", "|---|---|---|---|---|---|---|"]
    for coin, c in rep["data"]["coverage"].items():
        s = c["spot"]
        L.append(f"| {coin} | спот Bybit 1ч | {s['symbol']} | {s.get('first', '—')[:10]} | {s.get('last', '—')[:10]} | "
                 f"{s['n']} | {s.get('missing_steps', 0)} |")
        for venue, segs in (("перп Bybit", c["perp"]), ("перп BingX", c["bingx"])):
            for sg in segs:
                k, fu = sg["klines"], sg["funding"]
                L.append(f"| {coin} | {venue} 1ч | {sg['symbol']} | {k.get('first', '—')[:10]} | "
                         f"{k.get('last', '—')[:10]} | {k['n']} | {k.get('missing_steps', 0)} |")
                L.append(f"| {coin} | {venue} фандинг | {sg['symbol']} | {fu.get('first', '—')[:10]} | "
                         f"{fu.get('last', '—')[:10]} | {fu['n']} | — |")
    # фандинг
    p = f["params"]
    L += ["", "## Арбитраж фандинга", "",
          f"Правила (заданы заранее): вход при фандинге за {p['lookback_h']} ч ≥ {p['entry_apr']:g}% годовых на номинал "
          f"(для спот+перп — ещё и последняя ставка > 0), выход < {p['exit_apr']:g}%; решения только после выплаты; "
          f"номинал {p['notional']:g} USDT на ногу, плечо {p['leverage']:g}×; комиссии taker ×{p['fee_mult']:g} "
          f"(спот {p['spot_taker']}%, Bybit перп {p['bybit_taker']}%, BingX перп {p['bingx_taker']}%), "
          f"проскальзывание {p['slippage']} % на ногу; ребалансировка при ±{p['rebalance_move']:g}%. "
          f"База: USDT earn {p['earn_apr']:g}% годовых.", "",
          "| Вариант | Монета | Период | Чистая APR | − earn | Просадка | Худший день | Входов | Выплат | "
          "APR ×1 | Всегда в позиции | Посл. 12 мес.: APR / просадка / входов |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for kind, title in (("spot_perp", "спот+перп"), ("perp_perp", "перп–перп")):
        for coin, r in f.get(kind, {}).items():
            if not r.get("ok"):
                L.append(f"| {title} | {coin} | — | нет оценки: {r.get('reason')} | | | | | | | | |")
                continue
            l12 = r.get("last_12m") or {}
            L.append(f"| {title} | {coin} | {_d(r['period']['start'])}…{_d(r['period']['end'])} ({r['period']['years']:.2f} г.) | "
                     f"{r['net_apr_pct']:+.2f}% | {r['excess_vs_earn_pp']:+.2f} п.п. | {r['max_dd_pct']:.2f}% | "
                     f"{r['worst_day_pct']:+.2f}% | {r['entries']} | {r['settlements_in_position']} | "
                     f"{r.get('fees_x1_apr_pct')}% | {r.get('always_on_apr_pct')}% | "
                     f"{l12.get('net_apr_pct')}% / {l12.get('max_dd_pct')}% / {l12.get('entries')} |")
    L += ["", "Средний фандинг за всю историю (годовых на номинал, доля положительных выплат): " + "; ".join(
        f"{coin}: Bybit {x['bybit'].get('apr_pct')}% ({x['bybit'].get('positive_share')}), "
        f"BingX {x['bingx'].get('apr_pct')}% ({x['bingx'].get('positive_share')})" for coin, x in f.get("profile", {}).items()),
        "", "Статьи результата, USDT (фандинг / комиссии / проскальзывание / базис / переводы):"]
    for kind in ("spot_perp", "perp_perp"):
        for coin, r in f.get(kind, {}).items():
            if r.get("ok"):
                it = r["items_usdt"]
                L.append(f"- {kind} {coin}: {it['funding']:+.2f} / {it['fees']:+.2f} / {it['slippage']:+.2f} / "
                         f"{it['basis']:+.2f} / {it['transfer']:+.2f}; ребалансировок {r['rebalances']}, "
                         f"принудительных закрытий {r['forced_closes']}, в позиции {r['exposure_pct']}% времени")
    # направленная
    dp = d.get("params", {})
    L += ["", "## Направленная стратегия (EMA 20/100, 1 ч, стоп k×ATR14, риск 1%)", "",
          f"Walk-forward: {dp.get('in_months')} мес. in-sample выбирает k из {list(dp.get('k_grid', []))}, "
          f"{dp.get('out_months')} мес. вне выборки; комиссии taker {dp.get('taker')}% ×{dp.get('fee_mult')}, "
          f"проскальзывание {dp.get('slippage')} %, фандинг по истории.", "",
          "| Монета | Вне выборки | Сделок | Win | PF | Sharpe | Просадка | Итог | Фикс. k=2 итог | Купить и держать |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for coin, c in d.get("coins", {}).items():
        if not c.get("ok"):
            L.append(f"| {coin} | нет оценки: {c.get('reason')} | | | | | | | | |")
            continue
        w, fx, bh = c["walk_forward"], c["fixed_k"], c["buy_and_hold"]
        L.append(f"| {coin} | {_d(c['window']['start'])}…{_d(c['window']['end'])} ({w['months']} мес.) | {w['trades']} | "
                 f"{w['win_rate']} | {w['profit_factor']} | {w['sharpe']} | {w['max_dd_pct']}% | {w['total_return_pct']}% | "
                 f"{fx['total_return_pct']}% | {bh.get('total_return_pct')}% (просадка {bh.get('max_dd_pct')}%) |")
    pf = d.get("portfolio") or {}
    if pf:
        bh = d.get("buy_and_hold_portfolio") or {}
        L.append(f"| **Портфель** | {pf['months']} мес. | {pf['trades']} | {pf['win_rate']} | {pf['profit_factor']} | "
                 f"{pf['sharpe']} | {pf['max_dd_pct']}% | {pf['total_return_pct']}% | "
                 f"{(d.get('portfolio_fixed_k') or {}).get('total_return_pct')}% | {bh.get('total_return_pct')}% |")
        rnd = d.get("random") or {}
        x1 = d.get("fees_x1_portfolio") or {}
        L += ["", f"Случайные входы ({rnd.get('sims')} прогонов, seed {rnd.get('seed')}, та же доля лонгов и то же "
                  f"распределение удержания): стратегия {rnd.get('strategy_mean_pct')}% на сделку, случайные в среднем "
                  f"{rnd.get('random_mean_pct')}% (95-й перцентиль {rnd.get('random_p95_pct')}%), "
                  f"**p = {rnd.get('p_value')}** (по монетам {rnd.get('p_value_by_coin')}).",
              f"С комиссиями ×1: итог {x1.get('total_return_pct')}%, Sharpe {x1.get('sharpe')}, PF {x1.get('profit_factor')}.",
              f"Средний удерживаемый срок {pf['avg_hold_h']} ч, доля стопов {pf['stops_share']}, "
              f"комиссии за всё время {pf['fees_pct_sum']}% номинала сделок, фандинг {pf['funding_pct_sum']}%."]
        if not v["directional"].get("edge"):
            L.append("")
            L.append("**Преимущества нет (no edge):** вне выборки стратегия не лучше случайных входов с теми же "
                     "издержками и хуже «купить и держать».")
    # хедж
    L += ["", "## Хедж P2P-кругов шортом перпа", "",
          f"Бумага: кругов {h['paper']['cycles']}, завершённых {h['paper']['done']}, медиана длительности "
          f"{h['paper']['duration_min']['median']} мин ({h['paper']['note']}). Курс ₽/USDT для лотов: {h['rub_per_usdt']}. "
          "Окна — каждый час последних 365 дней; главное окно 60 мин (измерено), короче — оценка √t. "
          "Стоимость — по более дешёвой бирже (BingX taker 0.05%) ×2 + проскальзывание − фандинг шорта.", "",
          "| Монета | Запас | Кругов в бумаге | Сигналов history | σ без хеджа 60 мин | Убыток > запаса | σ с хеджем | "
          "Стоимость (×1) | Стоимость / запас | Лот 10k/20k ₽ в полосе (BingX / Bybit) |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for coin, c in h.get("coins", {}).items():
        if not c.get("ok"):
            L.append(f"| {coin} | — | {c.get('paper_cycles')} | {c.get('history_signals')} | нет оценки: {c.get('reason')} | | | | | |")
            continue
        m = c["main"]
        lots = c.get("lots", {})

        def lot(venue):
            x = lots.get(venue)
            return "/".join(f"{x[a]['in_band_share']:.0%}" for a in x) if x else "—"

        L.append(f"| {coin} | {c['buffer_pct']}% | {c['paper_cycles']} | {c['history_signals']} ({c['history_episodes']} эп.) | "
                 f"{m['unhedged']['sigma_pct']}% (p95 модуля {m['unhedged']['abs_p95_pct']}%) | {m['loss_over_buffer_share']:.1%} | "
                 f"{m['hedged']['sigma_pct']}% | {m['cost_mean_pct']}% ({c['main_fees_x1']['cost_mean_pct']}%) | "
                 f"{m['cost_to_buffer']} | {lot('bingx')} / {lot('bybit')} |")
    L += ["", "Почему такой вердикт по хеджу:"] + [f"- {x}" for x in hedge_notes(h)]
    L += ["", "Доля окон, где убыток без хеджа больше запаса, по длине окна (BingX):"]
    for coin, c in h.get("coins", {}).items():
        if c.get("ok"):
            L.append(f"- {coin}: " + ", ".join(f"{w['minutes']} мин — {w['loss_over_buffer_share']:.1%} "
                                               f"(p95 |Δ| {w['unhedged']['abs_p95_pct']}%)" for w in c["windows"]["bingx"]))
    # пороги и сомнения
    L += ["", "## Пороги плана и правило ускорения", "",
          "- Фандинг: ≥ 60 дней / 90 выплат бумаги, чистая APR ≥ earn + 3 п.п., просадка ≤ 2%. Бэктест проверяет "
          "то же на ≥ 12 мес. истории.",
          "- Направленная: бэктест ≥ 12 мес. вне выборки, ≥ 200 сделок, Sharpe ≥ 1, PF ≥ 1.2, лучше случайных (p < 0.05); "
          "бумага 90 дней / 50 сделок (по ускорению — 30 дней).",
          "- Хедж: стоимость ≤ 0.6 × запаса, σ с хеджем ≤ 0.5 × без, коэффициент лота 0.9–1.1 в ≥ 95%; бумага "
          "≥ 50 хеджей / 14 дней (по ускорению — 7–14 дней).",
          "- Ускорение (владелец, 27.09.2026): сильный бэктест → бумага 7–14 дней (направленная — 30) и сразу "
          f"{MIN_LOT_RULE}; полные лимиты — после порогов плана.", "", "## Сомнения и ограничения", ""]
    L += [f"- {x}" for x in rep["doubts"]]
    L += ["", "## Как воспроизвести", "",
          "```", "python -m research.report --out <папка> --paper-db <paper.db> --paper-db <архив> --history-db <history.db>",
          "```", "Кеш публичных данных — %TEMP%\\p2p_research_cache; `--offline` — только кеш."]
    return "\n".join(L) + "\n"


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return str(x)
    return x


def write(rep, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    jp, mp = os.path.join(out_dir, "backtest_report.json"), os.path.join(out_dir, "backtest_report.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(_jsonable(rep), fh, ensure_ascii=False, indent=1)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(rep))
    return jp, mp


def main(argv=None):
    ap = argparse.ArgumentParser(description="Бэктесты этапа 3 (только история, без ордеров)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--cache", default=data.CACHE_DIR)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--paper-db", action="append", default=[])
    ap.add_argument("--history-db")
    ap.add_argument("--earn", type=float, default=5.0)
    ap.add_argument("--sims", type=int, default=1000)
    a = ap.parse_args(argv)
    t0 = time.time()
    start = int(datetime.datetime.strptime(a.start, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    http = data.Http(cache_dir=a.cache, offline=a.offline)
    ds = data.load_dataset(http, start, 10 ** 14)
    print(f"данные: запросов {http.requests}, из кеша {http.cache_hits}")
    fund = funding_bt.run(ds, funding_bt.Params(earn_apr=a.earn))
    direc = directional_bt.run(ds, directional_bt.Params(sims=a.sims))
    hres = hedge_bt.run(ds, a.paper_db, a.history_db)
    args = {"start": a.start, "earn": a.earn, "sims": a.sims, "offline": a.offline,
            "paper_db": [os.path.basename(x) for x in a.paper_db],
            "history_db": os.path.basename(a.history_db) if a.history_db else None}
    rep = build(ds, fund, direc, hres, args)
    jp, mp = write(rep, a.out)
    for line in funding_bt.summary_ru(fund) + directional_bt.summary_ru(direc) + hedge_bt.summary_ru(hres):
        print(line)
    print(f"готово за {time.time() - t0:.0f} с: {jp}, {mp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
