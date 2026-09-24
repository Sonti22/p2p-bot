"""Telegram-бот сигналов P2P-связок: карточки-картинки, кнопки, меню. Запуск: python bot.py (настройки в .env)."""
import asyncio
import dataclasses
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp

import accounts
import alerts
import blacklist
import fees
import history
import netstatus
import presets
import trades
from cards import deal_card, history_card, history_compare_card, portfolio_card, top_chart
from p2p import ALL_EXCHANGES, AMOUNT_MAX, AMOUNT_MIN, DEFAULT_ASSETS, ENV_PATH, LOG_PATH, Config, _money, _price, \
    _route_qty, bank_liquidity, deal_amounts, fmt_ad, fmt_deal, fmt_top, load_env, maker_quote, parse_amount, \
    profit_breakdown, reliability, scan, setup_logging, spot_url, traps_log, venue_url

logger = logging.getLogger(__name__)

# Топики в личке с ботом (Bot API 9.5): ключ -> название; id созданных топиков — в data/topics.json.
TOPICS = (("signals", "🔔 Сигналы"), ("journal", "📒 Журнал"), ("settings", "⚙️ Настройки"), ("dev", "🛠 Разработка"))
TOPIC_HINTS = {"signals": "Сюда приходят 🔔 сигналы, алерты по курсу и утренний дайджест.",
               "journal": "Здесь журнал: подтверждения «✅ Сделал», факт по сделкам, движения по подключённым биржам.",
               "settings": "Пиши здесь ⚙️ команды настроек: /settings, /amount, /filters, /pause — ответы останутся тут.",
               "dev": "Здесь 🛠 разработка и здоровье бота: /dev, /status, /logs, недоступность площадок."}
TOPICS_PATH = os.path.join("data", "topics.json")


def load_topics(path=None):
    try:
        with open(path or TOPICS_PATH, encoding="utf-8") as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_topics(topics, path=None):
    path = path or TOPICS_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(topics, f, ensure_ascii=False)


MENU = {"keyboard": [[{"text": "🔥 Лучшая сейчас"}, {"text": "📊 Топ связок"}],
                     [{"text": "⚙️ Настройки"}, {"text": "🛠 Разработка"}],
                     [{"text": "❓ Как работать"}]],
        "resize_keyboard": True, "is_persistent": True}
BUTTONS = {"🔥 Лучшая сейчас": "/best", "📊 Топ связок": "/top", "⚙️ Настройки": "/settings",
           "🛠 Разработка": "/dev", "❓ Как работать": "/help"}
COMMANDS = [{"command": "best", "description": "Лучшая связка сейчас"},
            {"command": "top", "description": "Топ связок графиком"},
            {"command": "history", "description": "История спредов: время суток, дни недели, BestChange"},
            {"command": "backtest", "description": "Бэктест маршрута по истории спредов (7/30 дней)"},
            {"command": "calc", "description": "Разовый расчёт под сумму, напр. /calc 20000"},
            {"command": "stats", "description": "Журнал сделок: день/неделя/месяц, расчёт vs факт"},
            {"command": "alert", "description": "Алерт на курс, напр. /alert USDT sell 92 7d"},
            {"command": "alerts", "description": "Список алертов на курс"},
            {"command": "blacklist", "description": "Скрытые мерчанты и обменники"},
            {"command": "traps", "description": "Последние отсеянные ловушки (обучение без риска)"},
            {"command": "maker", "description": "Цена мейкера на площадках, напр. /maker USDT"},
            {"command": "banks", "description": "Объём по банкам на площадках, напр. /banks USDT"},
            {"command": "balance", "description": "Баланс по подключённым биржам"},
            {"command": "fees", "description": "Комиссии вывода по сетям и возраст данных"},
            {"command": "settings", "description": "Порог, сумма, пауза"},
            {"command": "pause", "description": "Пауза сигналов: /pause 30m|1h|3h|до утра"},
            {"command": "resume", "description": "Снять паузу сигналов"},
            {"command": "dev", "description": "Как развивается бот: версия, изменения, план"},
            {"command": "status", "description": "Версия, аптайм, последний скан, ошибки площадок"},
            {"command": "logs", "description": "Последние строки лога (logs/bot.log)"},
            {"command": "help", "description": "Как работать с сигналами"}]
HERE = os.path.dirname(os.path.abspath(__file__))
DEV_STATUS = os.path.join(HERE, ".dev_status.json")   # пишет launcher.py при каждом запуске
DESCRIPTION = ("Сканирую P2P Bybit, MEXC, HTX, KuCoin, BitPapa и обменники BestChange. "
               "Присылаю связки USDT, USDC, BTC, ETH, TON за рубли: чистая прибыль, карточка, ссылки на площадки.")
SHORT_DESCRIPTION = "Сигналы P2P-связок за рубли"
GUIDE = ("<b>Как работать с сигналом</b>\n\n"
         "1. «🟢 Купить» — откроется площадка. Найди мерчанта из карточки.\n"
         "2. Если в маршруте есть спот — поменяй монету (кнопка «🔁 Спот»).\n"
         "3. Переведи монету на площадку продажи: сеть и комиссия — в карточке.\n"
         "4. «🔴 Продать» — продай мерчанту или обменнику из карточки.\n\n"
         "<b>Безопасность</b>\n"
         "• Оплата только от человека с ФИО как на бирже. Третьи лица — отказ.\n"
         "• Крипту отпускай, только когда деньги видны в банке. Чек и скриншот — не подтверждение.\n"
         "• Первая сделка с новым мерчантом или обменником — малой суммой.\n"
         "• Спред от 5% часто плата за риск: читай условия мерчанта.\n\n"
         "<b>Что учтено в %</b>\n"
         "• Комиссия вывода по бирже и сети: бот берёт самую дешёвую сеть, у обменника — его сеть.\n"
         "• Спот 0,1% на бирже, где уже лежит монета.\n"
         "• Запас на курс ETH 0,5%, TON 0,7%, BTC 0,3% — пока идут сделки и переводы.\n"
         "• Комиссия банка — если задана PAY_FEE.\n\n"
         "<b>Что НЕ учтено</b>\n"
         "• СБП другим людям сверх 100 тыс. ₽/мес в банке — до 0,5%. Перевод по номеру карты в чужой банк — 1,5–2%.\n"
         "• НДФЛ с дохода от продажи крипты.\n"
         "• Проверки обменников (AML) и время: сделка может зависнуть.\n\n"
         "Площадки:")
LINKS = {"inline_keyboard": [
    [{"text": "Bybit P2P", "url": "https://www.bybit.com/fiat/trade/otc/?actionType=1&token=USDT&fiat=RUB"},
     {"text": "MEXC P2P", "url": "https://www.mexc.com/ru-RU/buy-crypto/p2p?fiat=RUB"}],
    [{"text": "BestChange", "url": "https://www.bestchange.ru/"}, {"text": "BitPapa", "url": "https://bitpapa.com/ru"}]]}
WAIT = "Первый скан ещё идёт, подожди пару секунд."
MIN_PRESETS = (1, 2, 3, 5)
AMOUNT_PRESETS = (25000, 50000, 100000, 200000)
VENUE_DOWN_AFTER = 900      # сек: площадка отдаёт ошибку дольше — алерт, даже если сканы не подряд
VENUE_FAIL_STREAK = 3       # или столько сканов подряд с ошибкой
VENUE_ALERT_COOLDOWN = 3600  # не чаще раза в час на площадку
EXCHANGE_NAMES = {"bybit": "Bybit", "mexc": "MEXC", "htx": "HTX", "kucoin": "KuCoin", "bitpapa": "BitPapa"}
ASSET_LIST = tuple(DEFAULT_ASSETS.split(","))       # монеты для кнопок «🎛 Фильтры»
EXCHANGE_LIST = tuple(ALL_EXCHANGES.split(","))      # площадки для кнопок «🎛 Фильтры»
VENUE_NAMES = dict(EXCHANGE_NAMES, bestchange="BestChange")  # + обменник, которого нет в EXCHANGE_NAMES
ACCOUNT_POLL_INTERVAL = int(os.getenv("ACCOUNT_POLL_INTERVAL", 60))  # опрос истории аккаунтов, сек
MSK = timezone(timedelta(hours=3))                    # тихие часы и /pause считаем по МСК, не по времени ПК
PAUSE_PRESETS = {"30m": 1800, "1h": 3600, "3h": 3 * 3600}  # аргументы /pause -> секунды


def parse_quiet_hours(spec):
    """«01:00-08:00» -> (начало, конец) в минутах с полуночи по МСК; None — не разобрано."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", (spec or "").strip())
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or m1 > 59 or h2 > 23 or m2 > 59:
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def in_quiet_hours(spec, ts=None):
    """Текущее время (МСК) внутри окна тихих часов? Учитывает переход через полночь (23:00-07:00)."""
    hours = parse_quiet_hours(spec)
    if not hours:
        return False
    start, end = hours
    now = datetime.fromtimestamp(ts if ts is not None else time.time(), MSK)
    minute = now.hour * 60 + now.minute
    return start <= minute < end if start < end else (minute >= start or minute < end)


def quiet_hours_end_ts(spec, ts=None):
    """Ближайший unix-timestamp окончания окна тихих часов (МСК), не раньше текущего момента. None — не задано."""
    hours = parse_quiet_hours(spec)
    if not hours:
        return None
    _, end = hours
    now = datetime.fromtimestamp(ts if ts is not None else time.time(), MSK)
    end_dt = now.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if end_dt <= now:
        end_dt += timedelta(days=1)
    return end_dt.timestamp()


def parse_pause_arg(arg):
    """/pause 30m|1h|3h|до утра -> секунды паузы, "morning" (до конца тихих часов) либо None — не разобрано."""
    a = (arg or "").strip().lower()
    if a in PAUSE_PRESETS:
        return PAUSE_PRESETS[a]
    if a in ("до утра", "утра", "morning"):
        return "morning"
    return None


def _hhmm_msk(ts):
    return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")


KEY_HINT = {
    "bybit": ("Создай ключ на Bybit: Профиль → API → Create New Key → System-generated API Keys. "
              "Права — только «Read-Only» (сними «Trade» и «Withdrawal»), в IP access whitelist впиши IP своего ПК."),
    "mexc": ("Создай ключ на MEXC: Профиль → API Management → Create API. "
             "Права — только «Read Info» (сними «Spot & Contract Trading» и «Withdrawals»), "
             "в Bind IP Address впиши IP своего ПК."),
    "kucoin": ("Создай ключ на KuCoin: Профиль → API Management → Create API. "
               "Права — только «General» (сними «Trade» и «Transfer»), в IP restriction впиши IP своего ПК. "
               "KuCoin попросит придумать <b>passphrase</b> — запомни её, бот спросит третьим шагом."),
}


def key_hint(ex, name):
    """Подсказка, как создать ключ «только чтение» с IP-whitelist — своя для каждой биржи, иначе общая фраза."""
    return KEY_HINT.get(ex, f"Создай в личном кабинете {name} API-ключ <b>только для чтения</b> "
                             f"(без торговли и выводов), по возможности ограничь его по IP.")


def save_env(key, value, path=ENV_PATH):
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _qty(x):
    """Объём монеты для вставки в ордер: USDT — сотые, мелкие монеты — до 6 значащих."""
    return f"{x:.2f}" if x >= 100 else f"{x:.6g}"


def copy_buttons(d, cfg, snap):
    """Кнопки «📋» (copy_text, Bot API 7.11): сумма круга в ₽ — вставить в ордер на покупку, объём монеты
    на выходе маршрута — в ордер на продажу. Без snap объём не посчитать — только сумма."""
    _, b, s, _ = d
    row = [{"text": f"📋 {_money(cfg.amount)} {cfg.fiat}", "copy_text": {"text": f"{cfg.amount:g}"}}]
    qty = _route_qty(b, s, cfg, snap.spot, snap.over_banks) if snap else None
    if qty:
        row.append({"text": f"📋 {_qty(qty)} {s.asset}", "copy_text": {"text": _qty(qty)}})
    return row


def plain_markup(markup):
    """Обычные кнопки для клиента/сервера без Bot API 9.4: цвета (`style`) убираем, «📋» (copy_text) выкидываем."""
    rows = []
    for row in markup.get("inline_keyboard", []):
        row = [{k: v for k, v in b.items() if k != "style"} for b in row if "copy_text" not in b]
        if row:
            rows.append(row)
    return {"inline_keyboard": rows}


def is_fancy(markup):
    return any("style" in b or "copy_text" in b for row in (markup or {}).get("inline_keyboard", []) for b in row)


def deal_markup(d, deal_id=None, cfg=None, snap=None):
    """Кнопки под карточкой: купить (зелёная) / продать (красная) — `style` из Bot API 9.4, спот, «📋» копировать
    сумму и объём (если передан cfg), шаги/сделал/скрыть (если сигнал запомнен), топ/обновить."""
    _, b, s, route = d
    row = [{"text": f"{label} · {ad.ex}", "url": venue_url(ad), "style": style}
           for ad, label, style in ((b, "🟢 Купить", "success"), (s, "🔴 Продать", "danger")) if venue_url(ad)]
    rows = [row] if row else []
    m = re.search(r"спот (\w+)→(\w+) на (\w+)", route)
    if m and spot_url(route):
        rows.append([{"text": f"🔁 Спот {m.group(1)}→{m.group(2)} · {m.group(3)}", "url": spot_url(route)}])
    if cfg is not None:
        rows.append(copy_buttons(d, cfg, snap))
    if deal_id is not None:
        rows.append([{"text": "📋 Шаги", "callback_data": f"steps:{deal_id}"},
                     {"text": "✅ Сделал", "callback_data": f"did:{deal_id}"}])
        rows.append([{"text": "🚫 Не показывать", "callback_data": f"bl:{deal_id}"}])
    rows.append([{"text": "📊 Все связки", "callback_data": "top"}, {"text": "🔄 Обновить", "callback_data": "best"}])
    return {"inline_keyboard": rows}


def steps_view(d, cfg, snap):
    """Текст «📋 Шаги»: пошаговый чек-лист маршрута с ценами объявлений (на момент сигнала) и
    разложением прибыли по стадиям; напоминание перепроверить цены перед сделкой."""
    profit, b, s, route = d
    lines = [f"📋 <b>Шаги связки</b> — {_money(cfg.amount)} {cfg.fiat}", "",
             f"1) Купить {b.asset} на {b.ex} по {_price(b.price)} — {html.escape(b.nick)}"]
    n = 1
    for st in route.split(" → ") if route else []:
        n += 1
        m = re.search(r"спот (\w+)→(\w+) на (\w+)", st)
        rate = snap.spot.get(m.group(3), {}).get(m.group(2) if m.group(1) == "USDT" else m.group(1)) \
            if m and snap else None
        lines.append(f"{n}) {st}" + (f" (курс {rate[0]:.4g}/{rate[1]:.4g})" if rate else ""))
    n += 1
    lines.append(f"{n}) Продать {s.asset} на {s.ex} по {_price(s.price)} — {html.escape(s.nick)}")
    breakdown = profit_breakdown(b, s, cfg, snap.spot, snap.over_banks) if snap else None
    if breakdown:
        lines += ["", "Прибыль по стадиям:"] + [f"  {label}: {p:+.2f}%" for label, p in breakdown]
    lines += ["", "⚠️ Цены объявлений и курс спота — на момент сигнала, перепроверь перед сделкой."]
    return "\n".join(lines)


def hist_key(it):
    """Уникальный ключ записи истории аккаунта для отсева повторов (id у Bybit P2P, иначе состав+время)."""
    return it.get("id") or f"{it.get('kind')}:{it.get('asset')}:{it.get('amount')}:{it.get('ts')}"


def hist_text(ex, it):
    """Текст уведомления о новом движении по счёту: депозит/вывод, спот-сделка или P2P-ордер Bybit."""
    name = EXCHANGE_NAMES.get(ex, ex)
    if "fiat" in it:   # P2P-ордер Bybit: {id, side, asset, fiat, amount, price, ts}
        arrow = "купил" if it["side"] == "buy" else "продал"
        return f"💱 {name} P2P: {arrow} {it['amount']:g} {it['asset']} за {it['fiat']}"
    if it.get("kind") == "trade":
        arrow = "купил" if it["side"] == "buy" else "продал"
        return f"💱 {name}: {arrow} {it['amount']:g} {it['asset']} по {it['price']:g}"
    label = {"deposit": "пришёл депозит", "withdraw": "исполнен вывод"}.get(it.get("kind"), it.get("kind"))
    return f"💰 {name}: {label} — {it['amount']:g} {it['asset']}"


def accounts_view(cfg):
    """Текст и кнопки раздела «🔑 Мои биржи»: список бирж со статусом подключения."""
    lines = ["🔑 <b>Мои биржи</b>", "",
             "Только чтение: балансы, история. Торговых ордеров, выводов и P2P-действий бот не делает.", ""]
    rows = []
    for ex in cfg.exchanges:
        name = EXCHANGE_NAMES.get(ex)
        if not name:
            continue
        connected = accounts.keys(ex) is not None
        lines.append(f"{'✅' if connected else '➖'} {name}")
        rows.append([{"text": f"{'✅' if connected else '➖'} {name}", "callback_data": f"acc:{ex}"}])
    rows.append([{"text": "⚙️ Настройки", "callback_data": "settings"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def account_view(ex):
    """Текст и кнопки карточки одной биржи: статус, «Проверить»/«Удалить» или «Подключить»."""
    name = EXCHANGE_NAMES.get(ex, ex)
    pair = accounts.keys(ex)
    back = {"text": "⬅️ Мои биржи", "callback_data": "accounts"}
    if pair:
        text = (f"🔑 <b>{name}</b>\n\nКлюч подключён: <code>{accounts.mask(pair[0])}</code>\n"
                f"Доступ: только чтение.")
        kb = [[{"text": "🔄 Проверить", "callback_data": f"acc_check:{ex}"}],
              [{"text": "🗑 Удалить ключ", "callback_data": f"acc_del:{ex}"}], [back]]
    elif ex in accounts.ONBOARDABLE:
        text = f"🔑 <b>{name}</b>\n\nКлюч не подключён.\n\n{key_hint(ex, name)}"
        kb = [[{"text": "➕ Подключить", "callback_data": f"acc_add:{ex}"}], [back]]
    else:
        text = f"🔑 <b>{name}</b>\n\nПодключение ключа пока не реализовано."
        kb = [[back]]
    return text, {"inline_keyboard": kb}


def filters_view(cfg):
    """Текст и кнопки «🎛 Фильтры»: переключатели монет/площадок (✅/⬜) + пресеты. Нельзя выключить
    последнюю монету или площадку. Изменения пишутся в .env и применяются со следующего скана."""
    mark = lambda on, t: ("✅ " if on else "⬜ ") + t
    asset_row = [{"text": mark(a in cfg.assets, a), "callback_data": f"flt_a:{a}"} for a in ASSET_LIST]
    ex_rows = [[{"text": mark(ex in cfg.exchanges, VENUE_NAMES.get(ex, ex)), "callback_data": f"flt_e:{ex}"}]
               for ex in EXCHANGE_LIST]
    text = (f"🎛 <b>Фильтры</b>\n\nМонеты: {', '.join(cfg.assets)}\nПлощадки: {', '.join(cfg.exchanges)}\n\n"
            f"Изменения применятся со следующего скана. Нельзя выключить все монеты или все площадки.")
    kb = [asset_row] + ex_rows + [
        [{"text": "💾 Сохранить как пресет", "callback_data": "preset_save"}],
        [{"text": "📋 Пресеты", "callback_data": "presets"}],
        [{"text": "⬅️ Настройки", "callback_data": "settings"}]]
    return text, {"inline_keyboard": kb}


def presets_view(cfg):
    """Текст и кнопки «📋 Пресеты»: встроенные (не удаляются) и сохранённые пользователем (можно удалить)."""
    builtin, custom = presets.builtin_presets(cfg), presets.list_custom()
    lines = ["📋 <b>Пресеты фильтров</b>", "", "Пресет меняет сразу все свои поля (условие «И»).", ""]
    kb = []
    for name in builtin:
        lines.append(f"⚙️ {name}")
        kb.append([{"text": f"▶️ {name}"[:64], "callback_data": f"preset_apply:{name}"}])
    for name in custom:
        lines.append(f"💾 {name}")
        kb.append([{"text": f"▶️ {name}"[:64], "callback_data": f"preset_apply:{name}"},
                   {"text": "🗑", "callback_data": f"preset_del:{name}"}])
    if not custom:
        lines += ["", "Своих пресетов пока нет — «💾 Сохранить как пресет» в «🎛 Фильтры»."]
    kb.append([{"text": "⬅️ Фильтры", "callback_data": "filters"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def blacklist_view():
    """Текст и кнопки «/blacklist»: список скрытых мерчантов/обменников с удалением."""
    rows = blacklist.list_all()
    if not rows:
        return ("🚫 <b>Блэклист пуст</b>\n\nКнопка «🚫 Не показывать» под сигналом добавляет сюда мерчанта "
                "или обменника — скан больше не покажет связки с ним.", {"inline_keyboard": []})
    lines = ["🚫 <b>Блэклист</b>", "", "Скан больше не показывает связки с этими мерчантами и обменниками.", ""]
    kb = []
    for entry_id, ex, nick in rows:
        name = EXCHANGE_NAMES.get(ex, ex)
        lines.append(f"{name}: {html.escape(nick)}")
        kb.append([{"text": f"🗑 {name}: {nick}"[:64], "callback_data": f"unbl:{entry_id}"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def traps_view():
    """Текст «/traps»: последние отсеянные аномальные объявления — обучение видеть ловушки без риска."""
    rows = traps_log()
    if not rows:
        return ("🪤 <b>Ловушки</b>\n\nПока ни одной: объявление с ценой намного выгоднее рынка (отсев по "
                "MAX_DEV) автоматически отсеивается и в сигналы не попадает — здесь появятся примеры.")
    lines = ["🪤 <b>Отсеянные ловушки</b>", "",
              "Цена выглядит заманчиво, но слишком далека от рынка — скан такие объявления отсеивает "
              "и в сигнал не пускает. Ниже — последние примеры, без риска.", ""]
    for t in rows:
        when = datetime.fromtimestamp(t["ts"]).strftime("%d.%m %H:%M")
        lines.append(f"{when} — {html.escape(t['reason'])}")
    return "\n".join(lines)


MAKER_HELP = ("Формат: /maker USDT — цена, чтобы встать первым объявлением в очереди на покупку и на продажу "
              "на каждой подключённой площадке, и во сколько это обходится против цены сделки прямо сейчас.")


def maker_view(snap, cfg, asset):
    """Текст «/maker <монета>»: на каждой подключённой площадке (`cfg.exchanges`) — цена мейкера
    (`p2p.maker_quote`) на покупку и на продажу и спред против цены немедленной сделки. Площадка без
    обеих сторон стакана по этой монете пропускается."""
    lines = [f"📝 <b>Мейкер {asset}</b>", "", MAKER_HELP, ""]
    found = False
    for ex in cfg.exchanges:
        name = EXCHANGE_NAMES.get(ex)
        if not name:
            continue
        rows = []
        buy = maker_quote(snap.groups, name, asset, "buy_ad")
        if buy:
            price, now_price, spread = buy
            rows.append(f"купить: выставить {_price(price)} ₽ (сейчас купить сразу можно по {_price(now_price)} ₽, "
                        f"переплата {spread:.2f}%)")
        sell = maker_quote(snap.groups, name, asset, "sell_ad")
        if sell:
            price, now_price, spread = sell
            rows.append(f"продать: выставить {_price(price)} ₽ (сейчас продать сразу можно по {_price(now_price)} ₽, "
                        f"недополучим {spread:.2f}%)")
        if rows:
            found = True
            lines.append(f"<b>{name}</b>")
            lines += rows
            lines.append("")
    if not found:
        lines.append("Нет обеих сторон стакана ни на одной подключённой площадке — попробуй другую монету.")
    return "\n".join(lines).rstrip()


BANKS_HELP = ("Формат: /banks USDT — сколько объявлений и какой объём (₽) по каждому банку/способу оплаты "
              "на каждой подключённой площадке, отдельно на покупку и на продажу.")


def banks_view(snap, cfg, asset):
    """Текст «/banks <монета>»: на каждой подключённой площадке (`cfg.exchanges`, включая BestChange) —
    топ банков по объёму (`p2p.bank_liquidity`), отдельно на покупку и на продажу."""
    lines = [f"🏦 <b>Банки {asset}</b>", "", BANKS_HELP, ""]
    found = False
    for ex in cfg.exchanges:
        name = VENUE_NAMES.get(ex)
        if not name:
            continue
        liq = bank_liquidity(snap.groups, name, asset)
        if not liq:
            continue
        found = True
        lines.append(f"<b>{name}</b>")
        for side, label in (("buy", "купить"), ("sell", "продать")):
            banks = liq.get(side)
            if not banks:
                continue
            top = sorted(banks.items(), key=lambda kv: kv[1][1], reverse=True)[:8]
            row = "; ".join(f"{html.escape(bank)} {cnt} объявл. / {_money(vol)} ₽" for bank, (cnt, vol) in top)
            lines.append(f"{label}: {row}")
        lines.append("")
    if not found:
        lines.append("Нет объявлений ни на одной подключённой площадке — попробуй другую монету.")
    return "\n".join(lines).rstrip()


ALERT_HELP = ("Формат: /alert USDT sell 92 7d — сообщу, когда надёжный покупатель или обменник даст "
              "≥92 ₽ за USDT (для buy — ≤ порога) в течение 7 дней. Монета: одна из настроенных. "
              "Срок: число + h/d/w (часы/дни/недели), не больше 90d.\n"
              "Можно добавить условия через «И» в любом порядке:\n"
              "«vol 50000» (или «50к») — в стакане по такой цене должно набираться не меньше этой суммы;\n"
              "«reliable» — встречная связка с этим объявлением не хуже «⚠️ риск» (не «🪤 ловушка»);\n"
              "«repeat 1h» — алерт не удалится после срабатывания, а будет проверяться дальше и может "
              "сработать снова не раньше, чем через кулдаун (здесь 1h) после прошлого раза.\n"
              "Пример: /alert USDT sell 92 7d vol 50000 reliable repeat 1h")


def alerts_view(chat_id):
    """Текст и кнопки «/alerts»: активные алерты чата с удалением."""
    rows = alerts.list_all(chat_id)
    if not rows:
        return (f"🔔 <b>Алертов нет</b>\n\n{ALERT_HELP}", {"inline_keyboard": []})
    lines = ["🔔 <b>Алерты на курс</b>", ""]
    kb = []
    for alert_id, asset, side, rate, expires_ts, cooldown, min_volume, require_reliable in rows:
        label, cmp = ("продать", "≥") if side == "sell" else ("купить", "≤")
        left_h = max(0, round((expires_ts - time.time()) / 3600))
        mark = f" 🔁 каждые ≥{cooldown / 3600:g} ч" if cooldown else ""
        vol_mark = f" 📦 ≥{_money(min_volume)} ₽" if min_volume else ""
        rel_mark = " 🛡 не хуже риска" if require_reliable else ""
        lines.append(f"{asset} {label} {cmp}{rate:g} ₽ (осталось ~{left_h} ч){mark}{vol_mark}{rel_mark}")
        kb.append([{"text": f"🗑 {asset} {label} {cmp}{rate:g}"[:64], "callback_data": f"delalert:{alert_id}"}])
    return "\n".join(lines), {"inline_keyboard": kb}


def portfolio_rows(port, snap):
    """[(биржа, [(монета, кол-во, ₽ или None)])] и итог в ₽ — общие данные для текста и карточки баланса."""
    rows, total = [], 0.0
    for ex, bal in port.items():
        coins = []
        for coin, amt in sorted(bal.items()):
            ref = snap.refs.get(coin) if snap else (1.0 if coin in ("USDT", "USDC") else None)
            rub = amt * ref if ref else None
            if rub:
                total += rub
            coins.append((coin, amt, rub))
        rows.append((EXCHANGE_NAMES.get(ex, ex), coins))
    return rows, total


def portfolio_view(port, snap):
    """Текст «💰 Баланс»: монеты по подключённым биржам и итог в ₽ по ориентиру текущего снимка."""
    if not port:
        connectable = ", ".join(EXCHANGE_NAMES.get(ex, ex) for ex in accounts.BALANCE_FETCHERS)
        return (f"💰 <b>Баланс</b>\n\nНи одна биржа не подключена ({connectable}) или баланс пуст. "
                f"Подключи ключ: ⚙️ Настройки → 🔑 Мои биржи.")
    rows, total = portfolio_rows(port, snap)
    lines = ["💰 <b>Баланс по биржам</b>", ""]
    for name, coins in rows:
        lines.append(f"<b>{name}</b>")
        for coin, amt, rub in coins:
            lines.append(f"  {amt:g} {coin}" + (f" ≈ {_money(rub)} ₽" if rub else " (нет ориентира в ₽)"))
        lines.append("")
    lines.append(f"<b>Итого:</b> ≈ {_money(total)} ₽")
    return "\n".join(lines)


BALANCE_MARKUP = {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "balance"}]]}


def roadmap_progress(path=os.path.join(HERE, "ROADMAP.md")):
    """(сделано, всего, следующая задача) из раздела «Очередь» ROADMAP.md."""
    try:
        with open(path, encoding="utf-8") as f:
            queue = f.read().split("## Очередь", 1)[-1].split("\n## ", 1)[0]
    except OSError:
        return 0, 0, ""
    items = re.findall(r"^- \[([ x~])\] (.+)$", queue, re.M)
    nxt = next((t for s, t in items if s == " "), "")
    return sum(1 for s, _ in items if s != " "), len(items), nxt.replace("`", "")


def _dev_status(path=DEV_STATUS):
    """Содержимое .dev_status.json (пишет launcher.py): версия, репозиторий, время запуска, лог коммитов."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def dev_view(status_path=DEV_STATUS, roadmap_path=os.path.join(HERE, "ROADMAP.md")):
    """Текст и кнопки раздела «🛠 Разработка»."""
    st = _dev_status(status_path)
    done, total, nxt = roadmap_progress(roadmap_path)
    lines = ["🛠 <b>Разработка бота</b>", ""]
    if st:
        lines.append(f"Версия: <code>{html.escape(st.get('version', '?'))}</code> · запущена {html.escape(st.get('started_at', '?'))}")
    if total:
        bar = "▰" * round(10 * done / total) + "▱" * (10 - round(10 * done / total))
        lines.append(f"📋 План: {bar} {done} из {total}")
    if nxt:
        lines.append(f"➡️ Дальше: {html.escape(nxt[:160])}")
    if st.get("log"):
        lines += ["", "<b>Последние изменения:</b>"]
        lines += [f"• {html.escape(c['date'])} — {html.escape(c['subject'][:90])}" for c in st["log"][:6]]
    lines += ["", "Облачный Claude улучшает бота в 06:00, 14:00 и 22:00 МСК; обновление ставится само."]
    repo, routine = st.get("repo", ""), os.getenv("ROUTINE_URL", "")
    rows = []
    if repo:
        rows += [[{"text": "📜 Изменения", "url": f"{repo}/commits/main"}, {"text": "🔀 Pull requests", "url": f"{repo}/pulls?q=is%3Apr"}],
                 [{"text": "✅ Проверки (CI)", "url": f"{repo}/actions"}, {"text": "📋 План", "url": f"{repo}/blob/main/ROADMAP.md"}]]
    if routine:
        rows.append([{"text": "☁️ Облачные запуски", "url": routine}])
    rows.append([{"text": "📟 Статус", "callback_data": "status"}, {"text": "🔄 Обновить", "callback_data": "dev"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def _uptime_str(seconds):
    """86461 -> «1д 00:01:01», 330 -> «00:05:30» — человекочитаемый аптайм бота."""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    hms = f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{d}д {hms}" if d else hms


def logs_view(path=LOG_PATH, n=30):
    """Текст «/logs»: последние n строк logs/bot.log, экранированные под HTML."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return "📄 <b>Логи</b>\n\nФайл логов пока пуст — бот ещё не писал (logs/bot.log)."
    tail = "".join(lines[-n:]).strip()
    if not tail:
        return "📄 <b>Логи</b>\n\nФайл логов пуст."
    if len(tail) > 3500:   # запас под лимит сообщения Telegram (4096) и заголовок
        tail = tail[-3500:]
    return f"📄 <b>Последние {min(n, len(lines))} строк лога</b>\n\n<pre>{html.escape(tail)}</pre>"


TOP_MARKUP = {"inline_keyboard": [
    [{"text": "🔄 Обновить", "callback_data": "top"}, {"text": "📄 Подробно", "callback_data": "detail"}],
    [{"text": "🔥 Лучшая", "callback_data": "best"}, {"text": "⚙️ Настройки", "callback_data": "settings"}],
    [{"text": "📈 История", "callback_data": "history"}]]}
HISTORY_MARKUP = {"inline_keyboard": [
    [{"text": "🔄 Обновить", "callback_data": "history"}, {"text": "📊 Топ", "callback_data": "top"}],
    [{"text": "📉 Бэктест", "callback_data": "backtest"}]]}


def fact_markup(trade_id):
    """Кнопки быстрого фактического результата под подтверждением «✅ Сделал»."""
    return {"inline_keyboard": [
        [{"text": "как расчёт", "callback_data": f"fact:{trade_id}:calc"}],
        [{"text": "−0.5 п.п.", "callback_data": f"fact:{trade_id}:minus"},
         {"text": "+0.5 п.п.", "callback_data": f"fact:{trade_id}:plus"}],
        [{"text": "✏️ ввести число", "callback_data": f"fact:{trade_id}:manual"}]]}


def backtest_view(cfg):
    """Текст «/backtest»: по history.db — для каждой пары площадок за 7/30 дней сколько раз лучший %
    был >= порога, средний/медианный % в такие моменты и оценка результата в ₽ (профит из истории уже
    чистый, с текущими комиссиями). Пустая история — понятное сообщение вместо таблицы."""
    if history.is_empty():
        return ("📉 История спредов пока пуста — бэктест не на чем считать. Бот пишет лучший % по площадкам "
                "раз в 5 минут, зайди позже, когда наберётся хотя бы несколько дней данных.")
    data = history.backtest(cfg.min_profit, cfg.amount)
    lines = [f"📉 <b>Бэктест маршрута</b> — порог {cfg.min_profit:g}%, круг {_money(cfg.amount)} {cfg.fiat}", ""]
    for days, label in ((7, "7 дней"), (30, "30 дней")):
        rows = data[days][:5]
        lines.append(f"<b>Топ-5 пар за {label}:</b>")
        if not rows:
            lines.append(f"  за этот срок порог {cfg.min_profit:g}% не достигался ни разу.")
        else:
            for r in rows:
                lines.append(f"  {r['buy_ex']} → {r['sell_ex']}: {r['hits']}/{r['total']} раз ≥ порога, "
                             f"средний {r['avg']:+.2f}%, медиана {r['median']:+.2f}%, "
                             f"≈{_money(r['est_rub'])} {cfg.fiat} за круг")
        lines.append("")
    lines.append("⚠️ Прошлое — не прогноз: реальные объявления, курс и комиссии к моменту сделки могут отличаться.")
    return "\n".join(lines)


class Bot:
    def __init__(self, session, token, chat_id, cfg):
        self.s, self.token, self.chat_id, self.cfg = session, token, chat_id, cfg
        self.cooldown = int(os.getenv("COOLDOWN", 600))      # сек: не повторять ту же пару бирж
        self.fancy = os.getenv("FANCY_BUTTONS", "1") != "0"   # цветные кнопки и «📋»; сам выключится при ошибке API
        self.topics = {}          # ключ топика -> message_thread_id, если у бота включены топики в личке
        self.cur_thread = None    # топик, из которого пришла последняя команда/кнопка — туда и отвечаем
        self.repeat_step = float(os.getenv("REPEAT_STEP", 0.3))  # п.п. роста профита для досрочного повтора
        self.max_signals = int(os.getenv("MAX_SIGNALS", 3))      # сигналим только из топ-N
        self.last = None
        self.paused = False
        self.pause_until = 0.0   # unix-время окончания /pause с аргументом; 0 или прошлое = не активна
        self.quiet_hours = os.getenv("QUIET_HOURS", "01:00-08:00")   # окно тихих часов, МСК "HH:MM-HH:MM"
        self.quiet_on = os.getenv("QUIET_HOURS_ON", "0") == "1"      # тихие часы включены (кнопка в настройках)
        self.night_deals = {}    # (ex,asset,ex,asset) -> лучшая связка за тихие часы, для утреннего дайджеста
        self._was_quiet = False  # тихие часы были на прошлом скане — для разового дайджеста при выходе из них
        self.awaiting_amount = False  # ждём сумму текстом после «✏️ Своя сумма»
        self.awaiting_preset_name = False  # ждём имя пресета текстом после «💾 Сохранить как пресет»
        self.awaiting_key = None      # {"ex":.., "step": "key"/"secret", "key":..} — ждём ключ биржи
        self.awaiting_fact = None     # id сделки — ждём фактический результат текстом после «✏️ ввести число»
        self.sent = {}
        self.live = {}                                            # (ex,asset,ex,asset) -> {"first": ts, "streak": n}
        self.live_scans = int(os.getenv("LIVE_SCANS", 2))        # сигнал, только если связка держится ≥ N сканов
        self.venue = {}   # ex -> {"streak": сканов подряд с ошибкой, "down_since": ts, "alerted_at": ts}
        self.deals_by_id = {}   # id -> (d, cfg, snap на момент сигнала) для кнопок «✅ Сделал»/«📋 Шаги»; не переживает рестарт
        self.next_deal_id = 1
        self.acc_seen = {}   # ex -> set известных ключей истории; None пока не было первого опроса
        self.start_ts = time.time()     # для аптайма в /status
        self.last_scan_ts = 0.0         # unix-время окончания последнего скана
        self.last_scan_duration = 0.0   # сколько секунд занял последний скан

    async def call(self, method, **params):
        async with self.s.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                               timeout=aiohttp.ClientTimeout(total=40)) as r:
            return await r.json()

    async def setup_topics(self):
        """Топики в личке (Bot API 9.5): если владелец включил режим топиков у бота в @BotFather
        (`getMe.has_topics_enabled`), завести 4 топика — id хранятся в data/topics.json — и слать сигналы,
        журнал и т.д. каждый в свой. Не включено или не вышло создать — всё в один чат, как раньше."""
        self.topics = {}
        if not self.chat_id:
            return
        me = await self.call("getMe")
        if not me.get("ok") or not me["result"].get("has_topics_enabled"):
            return
        saved = load_topics()
        for key, name in TOPICS:
            if key in saved:
                continue
            r = await self.call("createForumTopic", chat_id=self.chat_id, name=name)
            if not r.get("ok"):
                logger.warning("createForumTopic: %s", r.get("description"))
                return
            saved[key] = r["result"]["message_thread_id"]
            save_topics(saved)
            self.topics = dict(saved)
            await self.send(TOPIC_HINTS[key], topic=key)
        self.topics = saved

    def thread_for(self, topic):
        """id топика для сообщения: свой у сигналов/журнала/…, иначе тот, где написал пользователь."""
        if not self.topics:
            return None
        return self.topics.get(topic) if topic else self.cur_thread

    def markup(self, markup):
        """Разметка под возможности сервера/клиента: без цветов и «📋», если они не поддерживаются."""
        return markup if self.fancy or not markup else plain_markup(markup)

    def _fancy_failed(self, r, markup):
        """Отправка с цветными кнопками/«📋» не удалась: дальше шлём обычные кнопки и повторяем."""
        if r.get("ok") or not self.fancy or not is_fancy(markup):
            return False
        self.fancy = False
        logger.warning("Telegram: цветные кнопки/copy_text не поддерживаются, дальше обычные: %s", r.get("description"))
        return True

    async def send(self, text, chat_id=None, markup=None, topic=None):
        params = dict(chat_id=chat_id or self.chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        if markup:
            params["reply_markup"] = self.markup(markup)
        if self.thread_for(topic):
            params["message_thread_id"] = self.thread_for(topic)
        r = await self.call("sendMessage", **params)
        if self._fancy_failed(r, markup):
            params["reply_markup"] = plain_markup(markup)
            r = await self.call("sendMessage", **params)
        return r

    async def send_photo(self, png, caption, markup=None, topic=None):
        thread = self.thread_for(topic)
        r = await self._post_photo(png, caption, self.markup(markup), thread)
        if self._fancy_failed(r, markup):
            r = await self._post_photo(png, caption, plain_markup(markup), thread)
        return r

    async def _post_photo(self, png, caption, markup, thread=None):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        if markup:
            form.add_field("reply_markup", json.dumps(markup))
        if thread:
            form.add_field("message_thread_id", str(thread))
        form.add_field("photo", png, filename="card.png", content_type="image/png")
        async with self.s.post(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=form,
                               timeout=aiohttp.ClientTimeout(total=40)) as r:
            return await r.json()

    async def photo_or_text(self, render, caption, markup, topic=None):
        """Картинка с подписью; если не вышло — тем же текстом."""
        if len(caption) <= 1024:
            try:
                kw = {"topic": topic} if topic and self.topics else {}
                r = await self.send_photo(await asyncio.to_thread(render), caption, markup, **kw)
                if r.get("ok"):
                    return
                logger.warning("sendPhoto: %s", r.get("description"))
            except Exception as e:
                logger.warning("card error: %s", e)
        await self.send(caption, markup=markup, topic=topic)

    def remember_deal(self, d, cfg=None, snap=None):
        """Запомнить связку под кнопками «✅ Сделал»/«📋 Шаги»; хранится ограниченное число последних."""
        cfg = cfg or self.cfg
        snap = snap if snap is not None else self.last
        deal_id, self.next_deal_id = self.next_deal_id, self.next_deal_id + 1
        self.deals_by_id[deal_id] = (d, cfg, snap)
        if len(self.deals_by_id) > 200:
            del self.deals_by_id[min(self.deals_by_id)]
        return deal_id

    async def send_deal(self, d, prefix="", cfg=None, snap=None, topic=None):
        cfg = cfg or self.cfg
        snap = snap if snap is not None else self.last
        deal_id = self.remember_deal(d, cfg, snap)
        amounts = deal_amounts(d, cfg, snap) if snap else None
        rel = reliability(d, cfg, snap) if snap else None
        breakdown = profit_breakdown(d[1], d[2], cfg, snap.spot, snap.over_banks) if snap else None
        await self.photo_or_text(lambda: deal_card(d, cfg, amounts, rel, breakdown), prefix + fmt_deal(d, cfg, snap),
                                 deal_markup(d, deal_id, cfg, snap), topic)

    async def show_steps(self, cq, deal_id):
        """Кнопка «📋 Шаги»: отдельным сообщением пошаговый чек-лист маршрута."""
        entry = self.deals_by_id.get(deal_id)
        if not entry:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сигнал устарел")
            return
        d, cfg, snap = entry
        await self.call("answerCallbackQuery", callback_query_id=cq["id"])
        await self.send(steps_view(d, cfg, snap))

    async def mark_done(self, cq, deal_id):
        """Кнопка «✅ Сделал»: записать сделку в журнал (data/trades.db), убрать кнопку и предложить
        указать фактический результат сделки (расчёт vs факт для /stats)."""
        entry = self.deals_by_id.pop(deal_id, None)
        if not entry:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сигнал устарел, не записан")
            return
        d, cfg, snap = entry
        trade_id, bank, total, crossed = trades.log_trade(d, cfg.amount)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Записано в журнал ✅")
        await self.send(f"Расчёт был {d[0]:+.2f}%. Какой вышел факт?", markup=fact_markup(trade_id), topic="journal")
        await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                        reply_markup=self.markup(deal_markup(d, cfg=cfg, snap=snap)))
        if crossed:
            await self.send(f"⚠️ Через {bank} по СБП в этом месяце отправлено {_money(total)} ₽ — выше "
                            f"бесплатного лимита 100 000 ₽, дальше банк может взять комиссию до 0.5%. "
                            f"Для следующих сделок с этим мерчантом лучше выбрать другой банк.", topic="journal")

    async def handle_fact_button(self, cq, data):
        """Кнопка быстрого факта («как расчёт»/«±0.5 п.п.»/«✏️ ввести число») под подтверждением сделки."""
        _, trade_id_s, mode = data.split(":", 2)
        trade_id = int(trade_id_s)
        if mode == "manual":
            self.awaiting_fact = trade_id
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
            await self.send("Введи фактический результат числом: проценты (например +1.2% или 1.2) "
                            "или сумма в ₽ (например 650 ₽ или -300).")
            return
        row = trades.get_trade(trade_id)
        if not row:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сделка не найдена")
            return
        fact = {"calc": row["profit"], "minus": row["profit"] - 0.5, "plus": row["profit"] + 0.5}[mode]
        await self.save_fact(cq, trade_id, row, fact)

    async def save_fact(self, cq, trade_id, row, fact):
        trades.set_fact(trade_id, fact)
        if cq is not None:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Факт записан")
            await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            reply_markup={"inline_keyboard": []})
        await self.send(f"✅ Факт: {fact:+.2f}% (расчёт был {row['profit']:+.2f}%)", topic="journal")

    async def set_fact_from_text(self, trade_id, text):
        """Ввод факта текстом после «✏️ ввести число»: проценты или сумма в ₽ — `trades.parse_fact`."""
        row = trades.get_trade(trade_id)
        if not row:
            await self.send("Сделка уже не найдена, факт не записан.")
            return
        fact = trades.parse_fact(text, row["amount"])
        if fact is None:
            await self.send("Не понял результат. Пример: +1.2%, 1.2, 650 ₽, -300.")
            return
        await self.save_fact(None, trade_id, row, fact)

    async def hide_deal(self, cq, deal_id):
        """Кнопка «🚫 Не показывать»: занести обе стороны связки в блэклист, скан их больше не покажет."""
        entry = self.deals_by_id.pop(deal_id, None)
        if not entry:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сигнал устарел")
            return
        d, cfg, snap = entry
        _, b, s, _ = d
        blacklist.add(b.ex, b.nick)
        blacklist.add(s.ex, s.nick)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Скрыто, больше не покажу")
        await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                        reply_markup=self.markup(deal_markup(d, cfg=cfg, snap=snap)))

    async def add_alert(self, arg):
        """Команда «/alert USDT sell 92 7d [vol 50000] [reliable] [repeat 1h]»: разобрать и создать
        алерт на курс — одноразовый либо «повторно» с кулдауном, с необязательными условиями через
        «И» (объём стакана / надёжность встречной связки) в любом порядке хвоста."""
        m = re.match(r"(\w+)\s+(buy|sell)\s+([\d.,]+)\s+(\d+[hdw])(\s.*)?$", (arg or "").strip(), re.I)
        if not m:
            await self.send(ALERT_HELP)
            return
        asset, side, rate_s, dur_s = m.group(1).upper(), m.group(2).lower(), m.group(3), m.group(4).lower()
        if asset not in self.cfg.assets:
            await self.send(f"Монета {asset} не отслеживается ботом ({', '.join(self.cfg.assets)}).")
            return
        try:
            rate = float(rate_s.replace(",", "."))
        except ValueError:
            await self.send("Курс должен быть числом.")
            return
        dur = alerts.parse_duration(dur_s)
        if dur is None:
            await self.send("Срок — число + h/d/w (часы/дни/недели), не больше 90d.")
            return
        min_volume, require_reliable, cooldown_s = None, False, None
        tokens = (m.group(5) or "").split()
        i = 0
        while i < len(tokens):
            tok = tokens[i].lower()
            if tok == "vol" and i + 1 < len(tokens):
                min_volume = parse_amount(tokens[i + 1])
                if min_volume is None:
                    await self.send(f"Объём — сумма в ₽ от {int(AMOUNT_MIN)} до {int(AMOUNT_MAX)}, "
                                    f"например «vol 50000» или «vol 50к».")
                    return
                i += 2
            elif tok == "reliable":
                require_reliable = True
                i += 1
            elif tok == "repeat" and i + 1 < len(tokens):
                cooldown_s = tokens[i + 1].lower()
                i += 2
            else:
                await self.send(ALERT_HELP)
                return
        cooldown = None
        if cooldown_s:
            cooldown = alerts.parse_duration(cooldown_s)
            if cooldown is None:
                await self.send("Кулдаун repeat — число + h/d/w (часы/дни/недели), не больше 90d.")
                return
        alerts.add(self.chat_id, asset, side, rate, time.time() + dur, repeat_cooldown=cooldown,
                   min_volume=min_volume, require_reliable=require_reliable)
        label, cmp = ("продать", "≥") if side == "sell" else ("купить", "≤")
        notes = "".join([f", повтор не чаще раза в {cooldown_s}" if cooldown_s else "",
                         f", объём ≥{_money(min_volume)} ₽" if min_volume else "",
                         ", надёжность не хуже риска" if require_reliable else ""])
        await self.send(f"🔔 Алерт создан: {asset} {label} {cmp}{rate:g} ₽, срок {dur_s}{notes}. "
                        f"Список — /alerts.")

    async def check_alerts(self, snap, cfg=None):
        """Сработавшие алерты по текущему снимку → сообщение в тот чат, где алерт создан."""
        for alert_id, chat_id, asset, side, rate, price, ad in alerts.due(snap, cfg or self.cfg):
            label, cmp = ("продать", "≥") if side == "sell" else ("купить", "≤")
            await self.send(f"🔔 <b>Алерт сработал:</b> {asset} можно {label} по {_price(price)} ₽ "
                            f"({cmp}{rate:g}) — {fmt_ad(ad)}", chat_id=chat_id, topic="signals")

    def stats_view(self):
        st = trades.stats()
        labels = (("day", "За сегодня"), ("week", "За неделю"), ("month", "За месяц"))
        lines = ["📒 <b>Журнал сделок</b>", ""]
        for key, label in labels:
            s = st[key]
            if s["count"]:
                line = (f"{label}: {s['count']} сделок, оборот {_money(s['amount'])} ₽, "
                       f"средний профит {s['avg_profit']:+.2f}%")
                if s["fact_count"]:
                    line += (f"; факт указан у {s['fact_count']} из {s['count']}, средний факт "
                            f"{s['avg_fact']:+.2f}%, расхождение расчёт→факт {s['avg_diff']:+.2f} п.п.")
                lines.append(line)
            else:
                lines.append(f"{label}: сделок нет")
        lines.append("\nОтмечай связку кнопкой «✅ Сделал» под сигналом — так она попадёт в журнал, "
                     "затем укажи факт кнопкой или числом, чтобы сравнить расчёт с реальным результатом.")
        return "\n".join(lines)

    async def show_best(self, snap=None, cfg=None):
        snap = self.last if snap is None else snap
        cfg = cfg or self.cfg
        if not snap:
            await self.send(WAIT)
        elif not snap.deals:
            await self.send("Связок сейчас нет: все объявления отсеяны фильтрами.")
        else:
            await self.send_deal(snap.deals[0], "🔥 " + self.held_label(snap.deals[0]), cfg, snap)

    async def show_top(self, snap=None, cfg=None):
        snap = self.last if snap is None else snap
        cfg = cfg or self.cfg
        if not snap:
            await self.send(WAIT)
            return
        nets = sorted(((v["sell"].price, net) for net, v in snap.networks.items() if v.get("sell")), reverse=True)[:3]
        caption = (f"📊 <b>Топ связок</b> · USDT {snap.ref:.2f} ₽ · круг {_money(cfg.amount)} ₽\n"
                   + (f"Лучше продать USDT обменнику: {', '.join(f'{n} {p:.2f}' for p, n in nets)}\n" if nets else "")
                   + f"Связок всего: {len(snap.deals)} · от {cfg.min_profit:g}%: "
                   + f"{sum(1 for d in snap.deals if d[0] >= cfg.min_profit)}")
        await self.photo_or_text(lambda: top_chart(snap, cfg), caption, TOP_MARKUP)

    async def show_history(self):
        """«/history»: лучшее время суток + хитмап час×день недели (7 дней), медиана P2P vs BestChange
        (окно до 30 дней). Пустая история — понятное сообщение вместо картинок."""
        if history.is_empty():
            await self.send("📈 История спредов пока пуста. Бот пишет лучший % по площадкам раз в 5 минут — "
                            "зайди позже, когда наберётся хотя бы несколько часов данных.")
            return
        hourly = history.hourly_avg()
        grid = history.heatmap()
        await self.photo_or_text(lambda: history_card(hourly, grid),
                                 "📈 <b>История спредов</b> — лучшее время суток и дни недели за 7 дней (МСК)",
                                 HISTORY_MARKUP)
        labels, p2p_med, bc_med = history.median_vs_bestchange()
        await self.photo_or_text(lambda: history_compare_card(labels, p2p_med, bc_med),
                                 "📉 Медиана лучшего % по дням: P2P-связки против связок через BestChange",
                                 HISTORY_MARKUP)

    async def calc(self, arg):
        """Разовый расчёт под сумму (`/calc 20000`): скан с копией Config, без смены настроек."""
        amount = parse_amount(arg)
        if amount is None:
            await self.send(f"Не понял сумму. Пример: /calc 20000, /calc 1,5 млн "
                            f"(от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
            return
        calc_cfg = dataclasses.replace(self.cfg, amount=amount)
        snap = await scan(self.s, calc_cfg, force_alt=True)
        await self.show_top(snap, calc_cfg)
        await self.show_best(snap, calc_cfg)

    async def maker(self, arg):
        """/maker <монета>: режим мейкера на всех подключённых площадках по текущему снимку стакана."""
        asset = (arg or "").strip().upper()
        if not asset:
            await self.send(MAKER_HELP)
            return
        if asset not in self.cfg.assets:
            await self.send(f"Монета {asset} не отслеживается ботом ({', '.join(self.cfg.assets)}).")
            return
        if not self.last:
            await self.send(WAIT)
            return
        await self.send(maker_view(self.last, self.cfg, asset))

    async def banks(self, arg):
        """/banks <монета>: объявления и объём по каждому банку на всех подключённых площадках."""
        asset = (arg or "").strip().upper()
        if not asset:
            await self.send(BANKS_HELP)
            return
        if asset not in self.cfg.assets:
            await self.send(f"Монета {asset} не отслеживается ботом ({', '.join(self.cfg.assets)}).")
            return
        if not self.last:
            await self.send(WAIT)
            return
        await self.send(banks_view(self.last, self.cfg, asset))

    async def balance(self):
        """`/balance`: балансы по подключённым биржам (Bybit — Unified + Funding, MEXC — спот) и итог в ₽.

        Картинка-карточка портфеля; не вышло отрисовать — тот же текст, как у остальных карточек."""
        port = await accounts.portfolio(self.s)
        caption = portfolio_view(port, self.last)
        if not port:
            await self.send(caption, markup=BALANCE_MARKUP)
            return
        rows, total = portfolio_rows(port, self.last)
        await self.photo_or_text(lambda: portfolio_card(rows, total), caption, BALANCE_MARKUP)

    def settings_view(self):
        c = self.cfg
        now = time.time()
        pause_active = self.paused or (self.pause_until and now < self.pause_until)
        if self.paused:
            status = "⏸ пауза сигналов (бессрочно)"
        elif self.pause_until and now < self.pause_until:
            status = f"⏸ пауза до {_hhmm_msk(self.pause_until)}"
        elif self.is_quiet_now():
            status = f"🌙 тихие часы до {_hhmm_msk(quiet_hours_end_ts(self.quiet_hours))}"
        else:
            status = f"▶️ сканирую каждые {c.interval} с"
        text = (f"⚙️ <b>Настройки</b>\n\nПорог сигнала: <b>{c.min_profit:g}%</b> (1-я строка кнопок)\n"
                f"Сумма круга: <b>{_money(c.amount)} ₽</b> (2-я строка)\nСтатус: {status}\n"
                f"Тихие часы: {'✅ вкл' if self.quiet_on else '➖ выкл'} ({self.quiet_hours} МСК)\n\n"
                f"Монеты: {', '.join(c.assets)}\nПлощадки: {', '.join(c.exchanges)}")
        mark = lambda on, t: ("✅ " if on else "") + t
        kb = [[{"text": mark(c.min_profit == v, f"{v}%"), "callback_data": f"min:{v}"} for v in MIN_PRESETS],
              [{"text": mark(c.amount == v, f"{v // 1000}к"), "callback_data": f"amt:{v}"} for v in AMOUNT_PRESETS],
              [{"text": "✏️ Своя сумма", "callback_data": "amt_custom"}],
              [{"text": "🎛 Фильтры", "callback_data": "filters"}],
              [{"text": "🔑 Мои биржи", "callback_data": "accounts"}],
              [{"text": "🌙 Тихие часы: выкл" if self.quiet_on else "🌙 Тихие часы: вкл",
                "callback_data": "quiet_off" if self.quiet_on else "quiet_on"},
               {"text": "▶️ Возобновить" if pause_active else "⏸ Пауза",
                "callback_data": "resume" if pause_active else "pause"}]]
        return text, {"inline_keyboard": kb}

    async def set_custom_amount(self, text):
        """Ввод суммы текстом после «✏️ Своя сумма»: сохранить AMOUNT и сразу пересканировать (как /calc)."""
        amount = parse_amount(text)
        if amount is None:
            await self.send(f"Не понял сумму. Пример: 20000, 1,5 млн (от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
            return
        self.cfg.amount = amount
        save_env("AMOUNT", f"{amount:.0f}")
        snap = await scan(self.s, self.cfg, force_alt=True)
        self.last = snap
        await self.show_top(snap)
        await self.show_best(snap)

    def _toggle(self, values, item, env_key, label):
        """Вкл/выкл монету или площадку в списке фильтра; нельзя выключить последнюю. Пишет в .env."""
        if item in values:
            if len(values) == 1:
                return f"Нельзя выключить последнюю {label}"
            values.remove(item)
            save_env(env_key, ",".join(values))
            return f"Выключено: {item}"
        values.append(item)
        save_env(env_key, ",".join(values))
        return f"Включено: {item}"

    def apply_preset(self, name):
        """Применить пресет фильтров (встроенный или сохранённый «💾 Сохранить как пресет») — сразу все поля."""
        fields = presets.get_preset(name, self.cfg)
        if fields is None:
            return "Пресет не найден"
        env_map = {"assets": ("ASSETS", lambda v: ",".join(v)), "exchanges": ("EXCHANGES", lambda v: ",".join(v)),
                   "include_pay": ("INCLUDE_PAY", lambda v: ",".join(v)),
                   "min_profit": ("MIN_PROFIT", lambda v: f"{v:g}"), "amount": ("AMOUNT", lambda v: f"{v:.0f}"),
                   "same_venue_only": ("SAME_VENUE_ONLY", lambda v: "1" if v else "0")}
        for key, value in fields.items():
            setattr(self.cfg, key, list(value) if isinstance(value, list) else value)
            env_key, fmt = env_map[key]
            save_env(env_key, fmt(getattr(self.cfg, key)))
        return f"Применён пресет «{name}»"

    async def save_preset_named(self, name):
        """Ввод имени текстом после «💾 Сохранить как пресет»: снимок текущих фильтров в data/presets.json."""
        name = name.strip()[:40]
        if not name:
            await self.send("Пустое имя, пресет не сохранён.")
            return
        presets.save_preset(name, self.cfg)
        await self.send(f"💾 Пресет «{name}» сохранён.")
        text, kb = presets_view(self.cfg)
        await self.send(text, markup=kb)

    async def handle_key_input(self, text, message_id):
        """Ввод API key/secret[/passphrase] после «➕ Подключить»: сообщения с ключом удаляются из чата сразу же.

        Для бирж из accounts.PASSPHRASE_REQUIRED (KuCoin) — третий шаг: passphrase."""
        state = self.awaiting_key
        if message_id is not None:
            await self.call("deleteMessage", chat_id=self.chat_id, message_id=message_id)
        name = EXCHANGE_NAMES.get(state["ex"], state["ex"])
        text = text.strip()
        if state["step"] == "key":
            state["key"] = text
            state["step"] = "secret"
            await self.send(f"Ключ получен, сообщение удалено. Теперь пришли <b>secret</b> для {name}.")
            return
        if state["step"] == "secret":
            state["secret"] = text
            if state["ex"] in accounts.PASSPHRASE_REQUIRED:
                state["step"] = "passphrase"
                await self.send(f"Secret получен, сообщение удалено. Теперь пришли <b>passphrase</b> для {name}.")
                return
        else:   # step == "passphrase"
            state["passphrase"] = text
        self.awaiting_key = None
        accounts.save_key(state["ex"], state["key"], state["secret"], state.get("passphrase"))
        ok, msg = await accounts.verify(self.s, state["ex"])
        await self.send("✅ Подключено (только чтение)" if ok else f"⚠️ Ключ сохранён, но проверка не прошла: {msg}")
        t, kb = account_view(state["ex"])
        await self.send(t, markup=kb)

    def apply(self, data):
        if data.startswith("min:"):
            self.cfg.min_profit = float(data[4:])
            save_env("MIN_PROFIT", f"{self.cfg.min_profit:g}")
            return f"Порог {self.cfg.min_profit:g}%"
        if data.startswith("amt:"):
            self.cfg.amount = float(data[4:])
            save_env("AMOUNT", f"{self.cfg.amount:.0f}")
            return f"Сумма {_money(self.cfg.amount)} ₽ — применится со следующего скана"
        if data in ("pause", "resume"):
            self.paused = data == "pause"
            if data == "resume":
                self.pause_until = 0.0
            return "Сигналы на паузе" if self.paused else "Сигналы включены"
        if data in ("quiet_on", "quiet_off"):
            self.quiet_on = data == "quiet_on"
            save_env("QUIET_HOURS_ON", "1" if self.quiet_on else "0")
            return f"Тихие часы ({self.quiet_hours} МСК) " + ("включены" if self.quiet_on else "выключены")
        if data.startswith("flt_a:"):
            return self._toggle(self.cfg.assets, data[6:], "ASSETS", "монету")
        if data.startswith("flt_e:"):
            return self._toggle(self.cfg.exchanges, data[6:], "EXCHANGES", "площадку")
        if data.startswith("preset_apply:"):
            return self.apply_preset(data[len("preset_apply:"):])
        return ""

    async def scan_loop(self):
        while True:
            try:
                t0 = time.time()
                self.last = await scan(self.s, self.cfg)
                self.last_scan_ts, self.last_scan_duration = time.time(), time.time() - t0
                self.track_liveness(self.last)
                if history.record(self.last):   # не чаще раза в 5 минут, независимо от чата
                    history.cleanup()
                if self.chat_id:
                    await self.check_venues(self.last)
                    await self.check_alerts(self.last)
                    await self.check_networks()
                    await self.quiet_and_pause_tick(self.last)
            except Exception as e:
                logger.error("scan error: %s", e)
            await asyncio.sleep(self.cfg.interval)

    def status_view(self, status_path=DEV_STATUS):
        """Текст «/status»: версия, аптайм, время/длительность последнего скана, ошибки площадок,
        сколько связок сейчас выше порога сигнала."""
        st = _dev_status(status_path)
        lines = ["📟 <b>Статус бота</b>", "",
                 f"Версия: <code>{html.escape(st.get('version', '?'))}</code>",
                 f"Аптайм: {_uptime_str(time.time() - self.start_ts)}"]
        if self.last_scan_ts:
            when = datetime.fromtimestamp(self.last_scan_ts).strftime("%d.%m %H:%M:%S")
            lines.append(f"Последний скан: {when} ({self.last_scan_duration:.1f} с)")
        else:
            lines.append("Последний скан: ещё не было")
        snap = self.last
        if snap is None:
            lines.append("Скан ещё не выполнялся.")
            return "\n".join(lines)
        above = sum(1 for d in snap.deals if d[0] >= self.cfg.min_profit)
        lines.append(f"Связок выше порога {self.cfg.min_profit:g}%: {above}")
        if snap.errors:
            lines += ["", "<b>Ошибки площадок:</b>"]
            lines += [f"• {html.escape(k)}: {html.escape(e)}" for k, e in snap.errors.items()]
        else:
            lines.append("Ошибок нет — все площадки отвечают.")
        return "\n".join(lines)

    async def check_networks(self):
        """Сеть вывода/ввода переключилась (открыт ↔ закрыт) — одно сообщение на переключение."""
        for venue, asset, net, kind, is_open in netstatus.pop_changes():
            await self.send(f"{'✅' if is_open else '⚠️'} {venue}: {kind} {asset} ({net}) "
                            + ("снова открыт" if is_open else "приостановлен — связки через эту сеть не показываю"),
                            topic="signals")

    def is_quiet_now(self):
        """Сейчас внутри окна тихих часов (МСК) и они включены в настройках?"""
        return self.quiet_on and in_quiet_hours(self.quiet_hours)

    def collect_night_deals(self, snap):
        """Запомнить связки выше порога за тихие часы — по одной, лучшей по прибыли, на пару площадок."""
        for d in snap.deals[:self.max_signals]:
            if d[0] < self.cfg.min_profit:
                break
            _, b, s, _ = d
            key = (b.ex, b.asset, s.ex, s.asset)
            if key not in self.night_deals or d[0] > self.night_deals[key][0]:
                self.night_deals[key] = d

    async def send_night_digest(self):
        """Дайджест по окончании тихих часов: топ-3 связки за ночь по прибыли, одним сообщением."""
        deals, self.night_deals = list(self.night_deals.values()), {}
        if not deals:
            await self.send("🌅 Тихие часы закончились — связок выше порога не было.", topic="signals")
            return
        top = sorted(deals, key=lambda d: d[0], reverse=True)[:3]
        parts = [f"{i}) {fmt_deal(d, self.cfg)}" for i, d in enumerate(top, 1)]
        await self.send("🌅 <b>Доброе утро! Топ-3 связки за ночь</b>\n\n" + "\n\n".join(parts), topic="signals")

    async def quiet_and_pause_tick(self, snap):
        """Тихие часы копят связки для утреннего дайджеста вместо отправки; обычная пауза (ручная или
        по /pause) просто не шлёт сигналы. Дайджест уходит один раз — в момент выхода из тихих часов."""
        quiet = self.is_quiet_now()
        if quiet:
            self.collect_night_deals(snap)
        elif self._was_quiet:
            await self.send_night_digest()
        self._was_quiet = quiet
        paused = self.paused or (self.pause_until and time.time() < self.pause_until)
        if not quiet and not paused:
            await self.notify(snap)

    async def cmd_pause(self, arg):
        """/pause [30m|1h|3h|до утра] — пауза сигналов; без аргумента — бессрочно, как кнопка «⏸ Пауза»."""
        if not arg:
            self.paused, self.pause_until = True, 0.0
            await self.send("⏸ Сигналы на паузе. /resume — снять.")
            return
        sec = parse_pause_arg(arg)
        if sec is None:
            await self.send("Не понял срок паузы. Пример: /pause 30m, /pause 1h, /pause 3h, /pause до утра.")
            return
        if sec == "morning":
            end = quiet_hours_end_ts(self.quiet_hours)
            if end is None:
                await self.send("Не задано окно тихих часов (QUIET_HOURS) — не могу поставить паузу «до утра».")
                return
        else:
            end = time.time() + sec
        self.paused, self.pause_until = False, end
        await self.send(f"⏸ Сигналы на паузе до {_hhmm_msk(end)} (МСК). /resume — снять раньше.")

    async def cmd_resume(self):
        self.paused, self.pause_until = False, 0.0
        await self.send("▶️ Сигналы включены.")

    async def check_venues(self, snap):
        """Алерт, если площадка недоступна >15 мин или падает 3 скана подряд; и сообщение о восстановлении."""
        now = time.time()
        failed = {k.split("/", 1)[0]: e for k, e in snap.errors.items() if k != "spot"}
        for ex in self.cfg.exchanges:
            st = self.venue.setdefault(ex, {"streak": 0, "down_since": None, "alerted_at": None})
            if ex in failed:
                st["streak"] += 1
                st["down_since"] = st["down_since"] or now
                trouble = st["streak"] >= VENUE_FAIL_STREAK or now - st["down_since"] > VENUE_DOWN_AFTER
                if trouble and (not st["alerted_at"] or now - st["alerted_at"] > VENUE_ALERT_COOLDOWN):
                    st["alerted_at"] = now
                    await self.send(f"⚠️ {ex}: недоступна ({failed[ex]})", topic="dev")
            else:
                if st["alerted_at"]:
                    await self.send(f"✅ {ex}: снова доступна", topic="dev")
                st.update(streak=0, down_since=None, alerted_at=None)

    @staticmethod
    def _deal_key(d):
        _, b, s, _ = d
        return (b.ex, b.asset, s.ex, s.asset)

    def track_liveness(self, snap, now=None):
        """Сколько сканов подряд связка держится выше порога: минутный выброс не сигналим, устойчивую — да."""
        now = now or time.time()
        alive = {self._deal_key(d) for d in snap.deals if d[0] >= self.cfg.min_profit}
        for key in alive:
            rec = self.live.get(key)
            if rec:
                rec["streak"] += 1
            else:
                self.live[key] = {"first": now, "streak": 1}
        for key in list(self.live):
            if key not in alive:
                del self.live[key]

    def held_label(self, d, now=None):
        """«⏱ держится N мин», если связка видна не первый скан; иначе пусто."""
        rec = self.live.get(self._deal_key(d))
        if not rec or rec["streak"] < 2:
            return ""
        minutes = max(1, int(((now or time.time()) - rec["first"]) / 60))
        return f"⏱ держится {minutes} мин · "

    async def notify(self, snap):
        now = time.time()
        for d in snap.deals[:self.max_signals]:   # только топ-N: новые сигналы, когда меняется верх списка
            profit, b, s, _ = d
            if profit < self.cfg.min_profit:
                break
            key = (b.ex, b.asset, s.ex, s.asset)
            if self.live_scans > 1 and self.live.get(key, {}).get("streak", 0) < self.live_scans:
                continue                                          # появилась только что — ждём подтверждения
            prev = self.sent.get(key)
            if prev and now - prev[0] < self.cooldown and profit < prev[1] + self.repeat_step:
                continue
            self.sent[key] = (now, profit)
            await self.send_deal(d, "🔔 " + self.held_label(d, now), snap=snap, topic="signals")

    async def check_accounts(self):
        """Уведомление о новых движениях по подключённым биржам: депозит, вывод, спот-сделка, P2P-ордер.

        Первый опрос после старта только запоминает текущую историю (без сообщений, чтобы не спамить
        старыми записями) — дальше в Telegram уходят только записи, которых не было в прошлый раз."""
        for ex in accounts.ONBOARDABLE:
            if accounts.keys(ex) is None:
                continue
            try:
                hist = await accounts.account_history(self.s, ex)
            except Exception as e:
                logger.warning("account history error: %s %s", ex, e)
                continue
            if not hist:
                continue
            seen = self.acc_seen.get(ex)
            if seen is not None:
                for it in hist:
                    if hist_key(it) not in seen:
                        await self.send(hist_text(ex, it), topic="journal")
            self.acc_seen[ex] = {hist_key(it) for it in hist}

    async def accounts_loop(self):
        while True:
            if self.chat_id:
                try:
                    await self.check_accounts()
                except Exception as e:
                    logger.error("accounts_loop error: %s", e)
            await asyncio.sleep(ACCOUNT_POLL_INTERVAL)

    async def command_loop(self):
        offset = 0
        while True:
            try:
                r = await self.call("getUpdates", offset=offset, timeout=30)
            except Exception as e:
                logger.warning("getUpdates error: %s", e)
                await asyncio.sleep(5)
                continue
            if not r.get("ok"):
                logger.warning("Telegram: %s", r.get("description"))
                await asyncio.sleep(10)
                continue
            for u in r["result"]:
                offset = u["update_id"] + 1
                try:
                    await self.on_update(u)
                except Exception as e:
                    logger.error("update error: %s", e)

    async def on_update(self, u):
        cq = u.get("callback_query")
        if cq:
            if str(cq.get("message", {}).get("chat", {}).get("id", "")) == self.chat_id:
                self.cur_thread = cq["message"].get("message_thread_id")   # ответ — в тот же топик
                await self.on_callback(cq)
            return
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if not chat:
            return
        self.cur_thread = msg.get("message_thread_id")
        if not self.chat_id:
            # первый написавший чат становится получателем сигналов
            self.chat_id = chat
            save_env("TG_CHAT_ID", chat)
            logger.info("chat_id сохранён в .env: %s", chat)
            await self.setup_topics()
            await self.welcome()
        elif chat == self.chat_id:
            text = (msg.get("text") or "").strip()
            if self.awaiting_key and text and text not in BUTTONS and not text.startswith("/"):
                await self.handle_key_input(text, msg.get("message_id"))
            else:
                await self.handle(text)

    async def welcome(self):
        await self.send("👋 <b>Бот P2P-связок на связи.</b>\n\n"
                        "Сам пришлю 🔔 карточку, когда появится связка выше порога. "
                        "Кнопки внизу: 🔥 лучшая связка сейчас, 📊 топ графиком, ⚙️ настройки, "
                        "🛠 как развивается бот, ❓ как работать.",
                        markup=MENU)

    async def on_callback(self, cq):
        data = cq.get("data", "")
        if data != "amt_custom":
            self.awaiting_amount = False   # любая другая кнопка сбрасывает ожидание суммы
        if not data.startswith("acc_add:"):
            self.awaiting_key = None       # любая другая кнопка прерывает ввод ключа
        if data != "preset_save":
            self.awaiting_preset_name = False   # любая другая кнопка прерывает ввод имени пресета
        if not data.endswith(":manual") or not data.startswith("fact:"):
            self.awaiting_fact = None      # любая другая кнопка прерывает ввод факта числом
        toast = self.apply(data)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
        if data.startswith(("flt_a:", "flt_e:", "preset_apply:")):
            text, kb = filters_view(self.cfg)
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif toast:
            text, kb = self.settings_view()
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "best":
            await self.show_best()
        elif data == "top":
            await self.show_top()
        elif data == "history":
            await self.show_history()
        elif data == "backtest":
            await self.send(backtest_view(self.cfg))
        elif data == "detail":
            await self.send(fmt_top(self.last, self.cfg) if self.last else WAIT)
        elif data == "settings":
            text, kb = self.settings_view()
            await self.send(text, markup=kb)
        elif data == "dev":
            text, kb = dev_view()
            await self.send(text, markup=kb)
        elif data == "status":
            await self.send(self.status_view())
        elif data == "balance":
            await self.balance()
        elif data.startswith("did:"):
            await self.mark_done(cq, int(data[4:]))
        elif data.startswith("fact:"):
            await self.handle_fact_button(cq, data)
        elif data.startswith("steps:"):
            await self.show_steps(cq, int(data[6:]))
        elif data.startswith("bl:"):
            await self.hide_deal(cq, int(data[3:]))
        elif data.startswith("unbl:"):
            blacklist.remove(int(data[5:]))
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Удалено из блэклиста")
            text, kb = blacklist_view()
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data.startswith("delalert:"):
            alerts.remove(int(data[9:]), self.chat_id)
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Алерт удалён")
            text, kb = alerts_view(self.chat_id)
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "filters":
            text, kb = filters_view(self.cfg)
            await self.send(text, markup=kb)
        elif data == "presets":
            text, kb = presets_view(self.cfg)
            await self.send(text, markup=kb)
        elif data == "preset_save":
            self.awaiting_preset_name = True
            await self.send("Введи имя пресета текстом (например «Мои банки»).")
        elif data.startswith("preset_del:"):
            presets.delete_preset(data[len("preset_del:"):])
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Пресет удалён")
            text, kb = presets_view(self.cfg)
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "amt_custom":
            self.awaiting_amount = True
            await self.send(f"Введи сумму круга текстом, например 20000 или 1,5 млн "
                            f"(от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
        elif data == "accounts":
            t, kb = accounts_view(self.cfg)
            await self.send(t, markup=kb)
        elif data.startswith("acc_add:"):
            ex = data[8:]
            name = EXCHANGE_NAMES.get(ex, ex)
            self.awaiting_key = {"ex": ex, "step": "key"}
            await self.send(f"{key_hint(ex, name)}\n"
                            f"Пришли <b>API key</b> — сообщение с ним сразу удалю из чата.")
        elif data.startswith("acc_check:"):
            ex = data[10:]
            ok, msg = await accounts.verify(self.s, ex)
            await self.send("✅ Ключ рабочий (только чтение)" if ok else f"⚠️ {msg}")
        elif data.startswith("acc_del:"):
            ex = data[8:]
            accounts.delete_key(ex)
            await self.send("🗑 Ключ удалён")
            t, kb = account_view(ex)
            await self.send(t, markup=kb)
        elif data.startswith("acc:"):
            t, kb = account_view(data[4:])
            await self.send(t, markup=kb)

    async def handle(self, text):
        if self.awaiting_preset_name:
            self.awaiting_preset_name = False  # любая другая команда/кнопка тоже сбрасывает ожидание
            if text not in BUTTONS and not text.startswith("/"):
                await self.save_preset_named(text)
                return
        if self.awaiting_amount:
            self.awaiting_amount = False       # любая другая команда/кнопка тоже сбрасывает ожидание
            if text not in BUTTONS and not text.startswith("/"):
                await self.set_custom_amount(text)
                return
        if self.awaiting_fact is not None:
            trade_id, self.awaiting_fact = self.awaiting_fact, None   # любая другая команда/кнопка сбрасывает
            if text not in BUTTONS and not text.startswith("/"):
                await self.set_fact_from_text(trade_id, text)
                return
        self.awaiting_key = None               # команда/кнопка прерывает ввод ключа биржи
        cmd, _, arg = BUTTONS.get(text, text).partition(" ")
        cmd = cmd.split("@")[0]
        if cmd == "/start":
            await self.welcome()
        elif cmd == "/best":
            await self.show_best()
        elif cmd == "/top":
            await self.show_top()
        elif cmd == "/history":
            await self.show_history()
        elif cmd == "/backtest":
            await self.send(backtest_view(self.cfg))
        elif cmd == "/calc":
            if arg:
                await self.calc(arg)
            else:
                await self.send("Нужна сумма: /calc 20000")
        elif cmd == "/stats":
            await self.send(self.stats_view())
        elif cmd == "/alert":
            if arg:
                await self.add_alert(arg)
            else:
                await self.send(ALERT_HELP)
        elif cmd == "/alerts":
            text, kb = alerts_view(self.chat_id)
            await self.send(text, markup=kb)
        elif cmd == "/blacklist":
            text, kb = blacklist_view()
            await self.send(text, markup=kb)
        elif cmd == "/traps":
            await self.send(traps_view())
        elif cmd == "/maker":
            await self.maker(arg)
        elif cmd == "/banks":
            await self.banks(arg)
        elif cmd == "/balance":
            await self.balance()
        elif cmd == "/fees":
            await self.send(fees.view(live=netstatus.live_fee))
        elif cmd == "/settings":
            text, kb = self.settings_view()
            await self.send(text, markup=kb)
        elif cmd == "/dev":
            text, kb = dev_view()
            await self.send(text, markup=kb)
        elif cmd == "/status":
            await self.send(self.status_view())
        elif cmd == "/logs":
            await self.send(logs_view(LOG_PATH))
        elif cmd in ("/min", "/amount") and arg:
            try:
                v = float(arg.replace(",", ".").replace(" ", ""))
            except ValueError:
                await self.send("Нужно число.")
                return
            await self.send(self.apply(f"{'min' if cmd == '/min' else 'amt'}:{v}"))
        elif cmd == "/pause":
            await self.cmd_pause(arg)
        elif cmd == "/resume":
            await self.cmd_resume()
        else:
            await self.send(GUIDE, markup=LINKS)

    async def check_key_safety(self):
        """При старте: если сохранённый ключ биржи даёт торговать/выводить — удалить его и попросить read-only."""
        for ex in accounts.ONBOARDABLE:
            if accounts.keys(ex) is None:
                continue
            safe, detail = await accounts.api_permissions(self.s, ex)
            if not safe:
                accounts.delete_key(ex)
                name = EXCHANGE_NAMES.get(ex, ex)
                await self.send(f"⚠️ {name}: ключ даёт больше, чем чтение ({detail}) — удалил его из бота.\n"
                                f"Создай новый ключ ТОЛЬКО для чтения и подключи заново: «⚙️ Настройки → 🔑 Мои биржи».")

    async def setup(self):
        for method, params in (("setMyCommands", {"commands": COMMANDS}),
                               ("setMyDescription", {"description": DESCRIPTION}),
                               ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})):
            try:
                r = await self.call(method, **params)
                if not r.get("ok"):
                    logger.warning("%s: %s", method, r.get("description"))
            except Exception as e:
                logger.warning("%s: %s", method, e)


async def main():
    setup_logging()
    load_env()
    token = os.getenv("TG_TOKEN", "").strip()
    if not token:
        raise SystemExit("TG_TOKEN не задан: создай бота у @BotFather и пропиши токен в .env")
    cfg = Config.from_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        bot = Bot(s, token, os.getenv("TG_CHAT_ID", "").strip(), cfg)
        await bot.setup()
        if bot.chat_id:
            await bot.setup_topics()
            await bot.check_key_safety()
        logger.info("Бот запущен: каждые %ss, порог %g%%, биржи %s", cfg.interval, cfg.min_profit,
                    ', '.join(cfg.exchanges))
        await asyncio.gather(bot.scan_loop(), bot.command_loop(), bot.accounts_loop())


if __name__ == "__main__":
    asyncio.run(main())
