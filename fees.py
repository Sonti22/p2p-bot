"""Комиссии вывода по биржам и сетям — из fees.json (биржа → монета → сеть → комиссия в монете, дата проверки).

Таблица нужна для площадок без живого справочника (Bybit/MEXC без ключа). Живые данные из netstatus
(HTX/KuCoin публично, Bybit/MEXC по ключу «только чтение») имеют приоритет в расчёте маршрута, а в /fees
показываются рядом со статическими для сверки. Старше STALE_DAYS — предупреждение.
"""
import html
import json
import os
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
FEES_PATH = os.path.join(HERE, "fees.json")
STALE_DAYS = 30


def load(path=FEES_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def table(path=FEES_PATH):
    """{(биржа, монета): {сеть: комиссия}} — формат, который ждёт p2p._withdraw."""
    try:
        data = load(path)
    except (OSError, ValueError):
        return {}
    return {(venue, asset): {net: float(fee) for net, fee in nets.items()}
            for venue, assets in (data.get("fees") or {}).items() for asset, nets in assets.items()}


def age_days(path=FEES_PATH, today=None):
    try:
        checked = date.fromisoformat(load(path).get("checked", ""))
    except (OSError, ValueError):
        return None
    return ((today or date.today()) - checked).days


def view(path=FEES_PATH, today=None, live=None):
    """Текст /fees: таблица из fees.json, рядом живые комиссии (если известны), возраст данных.
    live(venue, asset, net) -> float|None — обычно netstatus.live_fee."""
    try:
        data = load(path)
    except (OSError, ValueError):
        return "⚠️ fees.json не найден или битый — используются комиссии из TRANSFER_FEES."
    age = age_days(path, today)
    lines = ["💸 <b>Комиссии вывода</b> (в монете)", ""]
    for venue, assets in (data.get("fees") or {}).items():
        lines.append(f"<b>{html.escape(venue)}</b>")
        for asset, nets in assets.items():
            parts = []
            for net, fee in nets.items():
                cur = live(venue, asset, net) if live else None
                mark = ""
                if cur is not None and abs(cur - float(fee)) > max(0.1 * float(fee), 1e-9):
                    mark = f" → сейчас {cur:g} ⚠️"
                elif cur is not None:
                    mark = " ✓"
                parts.append(f"{net} {float(fee):g}{mark}")
            lines.append(f"  {asset}: " + ", ".join(parts))
    if age is None:
        lines += ["", "⚠️ Дата проверки не указана."]
    else:
        flag = " ⚠️ данные старые — сверь на биржах" if age > STALE_DAYS else ""
        lines += ["", f"Проверено {data.get('checked')} ({age} дн. назад){flag}"]
    if data.get("source"):
        lines.append(f"Источник: {html.escape(data['source'])}")
    lines += ["", "✓ — совпадает с живым справочником биржи; ⚠️ — расходится, в расчёте берётся живое значение.",
              "HTX и KuCoin берут комиссии из своих справочников напрямую."]
    return "\n".join(lines)
