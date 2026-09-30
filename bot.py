"""Telegram-бот сигналов P2P-связок: карточки-картинки, кнопки, меню. Запуск: python bot.py (настройки в .env)."""
import asyncio
import contextvars
import copy
import dataclasses
import html
import inspect
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import aiohttp

import accounts
import calibration
import favorites
import alerts
import backup
import blacklist
import fees
import history
import jsonstore
import logsafe
import netstatus
import paper
import payouts
import perp
import presets
import reputation
import sigreport
import simdirectional
import simfunding
import simmaker
import hedge_plans
import simperp
import snapshots
import trades
import trading.wiring
from cards import deal_card, history_card, history_compare_card, portfolio_card, top_chart
from p2p import ALL_EXCHANGES, AMOUNT_MAX, AMOUNT_MIN, DEFAULT_ASSETS, ENV_PATH, LOG_PATH, MIN_PROFIT_MAX, \
    MIN_PROFIT_MIN, TRAP, Config, fmt_signal, sell_step_number, _money, _price, _route_qty, bank_liquidity, book_spread, deal_amounts, \
    deal_for_amount, deal_fresh, fmt_ad, fmt_breakeven, fmt_deal, fmt_top, load_env, maker_neighbors, maker_place, maker_quote, \
    maker_round_fee, parse_amount, parse_min_profit, profit_breakdown, reliability, reliability_index, route_hops, scan, \
    score, setup_logging, spot_url, traps_log, venue_url, ScanSpeed, deal_stale
from p2p import depth_for_deal, depth_settings, terms_log, terms_summary

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
        return {k: int(v) for k, v in jsonstore.read_dict(path or TOPICS_PATH).items()}
    except (TypeError, ValueError):   # значение не приводится к id топика
        return {}


def save_topics(topics, path=None):
    jsonstore.write_dict(path or TOPICS_PATH, topics)


MENU = {"keyboard": [[{"text": "🔥 Лучшая сейчас"}, {"text": "📊 Топ связок"}],
                     [{"text": "⚙️ Настройки"}, {"text": "🛠 Разработка"}],
                     [{"text": "❓ Как работать"}, {"text": "🛡 Безопасность"}]],
        "resize_keyboard": True, "is_persistent": True}
BUTTONS = {"🔥 Лучшая сейчас": "/best", "📊 Топ связок": "/top", "⚙️ Настройки": "/settings",
           "🛠 Разработка": "/dev", "❓ Как работать": "/help", "🛡 Безопасность": "/safety"}

# Гости (TG_GUESTS в .env): получают сигналы и рыночные команды; настройки, ключи, журнал, алерты — только
# владельцу. REPLY_CHAT живёт в контексте задачи command_loop: фоновые циклы (скан, аккаунты) его не видят
# и шлют владельцу, даже если в этот момент обрабатывается команда гостя.
REPLY_CHAT = contextvars.ContextVar("reply_chat", default=None)
GUEST_MENU = {"keyboard": [[{"text": "🔥 Лучшая сейчас"}, {"text": "📊 Топ связок"}],
                           [{"text": "❓ Как работать"}, {"text": "🛡 Безопасность"}]],
              "resize_keyboard": True, "is_persistent": True}
GUEST_CMDS = {"/start", "/help", "/best", "/top", "/calc", "/banks", "/maker", "/fees", "/history", "/backtest",
              "/safety"}
GUEST_CALLBACKS = {"best", "top"}
GUEST_DENIED = ("🔒 Это только для владельца бота. Тебе доступны: /best, /top, /calc, /banks, /maker, /fees, /history, "
                "/safety.")
GUEST_WELCOME = ("👋 <b>Владелец открыл тебе доступ.</b>\n\nБуду присылать 🔔 карточки связок, как и ему. "
                 "Команды: 🔥 лучшая связка, 📊 топ, /calc &lt;сумма&gt;, /banks, /maker, /fees, /history, 🛡 /safety. "
                 "Настройки, ключи бирж и журнал сделок — только у владельца.")
ACCESS_HINT = ("🔒 Бот приватный. Твой id: <code>{chat}</code> — попроси владельца выполнить "
               "<code>/allow {chat}</code>, и я начну отвечать.")
# владелец — только личный чат: чат TG_CHAT_ID оказался группой/каналом (или пишет не владелец)
OWNER_ONLY_PRIVATE = ("🔒 Команды владельца (настройки, ключи, выплаты) работают только в личном чате с ботом. "
                      "Сейчас TG_CHAT_ID в .env — группа или канал: впиши туда id своего личного чата с ботом (он "
                      "равен твоему Telegram user id) и перезапусти бота.")
OWNER_ONLY_TOAST = "Кнопки владельца — только в личном чате владельца с ботом"
FIRST_CHAT_PRIVATE = "🔒 Владельцем бота становится только личный чат: напиши мне /start в личные сообщения."
COMMANDS = [{"command": "best", "description": "Лучшая связка сейчас"},
            {"command": "top", "description": "Топ связок графиком"},
            {"command": "history", "description": "История спредов: время суток, дни недели, BestChange"},
            {"command": "backtest", "description": "Бэктест маршрута по истории спредов (7/30 дней)"},
            {"command": "calc", "description": "Разовый расчёт под сумму, напр. /calc 20000"},
            {"command": "stats", "description": "Журнал сделок: день/неделя/месяц, расчёт vs факт"},
            {"command": "export", "description": "Журнал сделок в CSV для банка и 3-НДФЛ: /export month|year"},
            {"command": "paper", "description": "Сухой прогон: круги, статистика, /paper on|off|amount|report|reset"},
            {"command": "funding", "description": "Арбитраж фандинга на бумаге: позиции, итог, ставки сейчас"},
            {"command": "futures", "description": "Направленная стратегия на бумаге: сделки, PF, просадка, vs случайные"},
            {"command": "mybanks", "description": "Мои банки и бесплатные лимиты СБП"},
            {"command": "fav", "description": "Избранные маршруты"},
            {"command": "alert", "description": "Алерт на курс, напр. /alert USDT sell 92 7d"},
            {"command": "alerts", "description": "Список алертов на курс"},
            {"command": "blacklist", "description": "Скрытые мерчанты и обменники"},
            {"command": "traps", "description": "Последние отсеянные ловушки (обучение без риска)"},
            {"command": "nets", "description": "Сети площадок, которые бот не распознал"},
            {"command": "maker", "description": "Цена мейкера на площадках, напр. /maker USDT"},
            {"command": "banks", "description": "Банки: спред за 7 дней; /banks USDT — объём сейчас"},
            {"command": "balance", "description": "Баланс по подключённым биржам"},
            {"command": "payout", "description": "Выплата Cryptomus на адрес из белого списка, /payout history"},
            {"command": "hedge", "description": "Хедж кругов шортом перпа: открытые, закрыть, стоп"},
            {"command": "fees", "description": "Комиссии вывода по сетям и возраст данных"},
            {"command": "settings", "description": "Порог, сумма, пауза"},
            {"command": "pause", "description": "Пауза сигналов: /pause 30m|1h|3h|до утра"},
            {"command": "resume", "description": "Снять паузу сигналов"},
            {"command": "dev", "description": "Как развивается бот: версия, изменения, план"},
            {"command": "status", "description": "Версия, аптайм, последний скан, скорость, ошибки площадок"},
            {"command": "signals", "description": "Отчёт качества сигналов: доля пропущенных, причины, топ направлений"},
            {"command": "logs", "description": "Последние строки лога (logs/bot.log)"},
            {"command": "guests", "description": "Гости: кому ещё слать сигналы (/allow id, /deny id)"},
            {"command": "safety", "description": "Безопасность: 115-ФЗ, блокировки карт, правила сделки"},
            {"command": "help", "description": "Справка по разделам: сигнал, метки, команды"}]
HERE = os.path.dirname(os.path.abspath(__file__))
DEV_STATUS = os.path.join(HERE, ".dev_status.json")   # пишет launcher.py при каждом запуске
PERP_LOOP_TICK = 5   # сек: как часто perp_loop проверяет, пора ли опросить перпы (сам интервал — PERP_INTERVAL)
DESCRIPTION = ("Сканирую P2P Bybit, MEXC, HTX, KuCoin, BitPapa, LBank и обменники BestChange. "
               "Присылаю связки USDT, USDC, BTC, ETH, TON за рубли: чистая прибыль, карточка, ссылки на площадки.")
SHORT_DESCRIPTION = "Сигналы P2P-связок за рубли"
GUIDE_BODY = ("<b>Как работать с сигналом</b>\n"
              "Шаги пронумерованы одинаково на картинке, в тексте под ней и на кнопках.\n\n"
              "1. «🟢 1. Купить на …» — откроется площадка. Найди продавца из шага 1, оплати способом из карточки. "
              "Сумму скопирует «📋 Сумма».\n"
              "2. Средние шаги — перевод монеты на площадку продажи (сеть и комиссия в тексте) и спот, если он есть "
              "(кнопка «🔁 Спот»).\n"
              "3. «🔴 N. Продать на …» — продай покупателю или обменнику из последнего шага. Объём скопирует "
              "«📋 Продать».\n"
              "Пошаговый чек-лист с ценами — «📝 Инструкция». Сделал — «✅ Сделал», сделка попадёт в журнал. "
              "Мерчант не понравился — «🚫 Скрыть мерчанта».\n\n"
              "<b>Безопасность</b>\n"
              "• Оплата только от человека с ФИО как на бирже. Третьи лица — отказ.\n"
              "• Крипту отпускай, только когда деньги видны в банке. Чек и скриншот — не подтверждение.\n"
              "• Первая сделка с новым мерчантом или обменником — малой суммой.\n"
              "• Спред от 5% часто плата за риск: читай условия мерчанта.\n"
              "• Законы, документы для банка, блокировки карт — /safety.\n\n"
              "<b>Что учтено в %</b>\n"
              "• Комиссия вывода по бирже и сети: бот берёт самую дешёвую сеть, у обменника — его сеть.\n"
              "• Спот 0,1% на бирже, где уже лежит монета.\n"
              "• Запас на курс ETH 0,5%, TON 0,7%, BTC 0,3% — пока идут сделки и переводы.\n"
              "• Комиссия банка — если задана PAY_FEE.\n"
              "• Комиссия СБП 0,5% — если у мерчанта нет твоего банка и бесплатный лимит СБП за месяц исчерпан "
              "(считается по журналу сделок, банки и лимиты — /mybanks).\n\n"
              "<b>Что НЕ учтено</b>\n"
              "• Перевод по номеру карты в чужой банк — 1,5–2%.\n"
              "• НДФЛ с дохода от продажи крипты.\n"
              "• Проверки обменников (AML) и время: сделка может зависнуть.\n")
GUIDE = GUIDE_BODY + "\nПлощадки:"
# Справка одна для всех: правила ЦБ (ОД-2506 и др.) — общая информация, она в /safety (ссылка в GUIDE_BODY)
OWNER_GUIDE = GUIDE
LINKS = {"inline_keyboard": [
    [{"text": "Bybit P2P", "url": "https://www.bybit.com/fiat/trade/otc/?actionType=1&token=USDT&fiat=RUB"},
     {"text": "MEXC P2P", "url": "https://www.mexc.com/ru-RU/buy-crypto/p2p?fiat=RUB"}],
    [{"text": "BestChange", "url": "https://www.bestchange.ru/"}, {"text": "BitPapa", "url": "https://bitpapa.com/ru"}]]}


# /help владельца — разделы кнопками (как docs/owner-guide.md): ключ → (кнопка, текст). Раздел открывается правкой того
# же сообщения. Гостю — прежняя справка одним сообщением (GUIDE): его кнопки обрабатывает запиненный on_guest_callback.
_GUIDE_PARTS = GUIDE_BODY.split("\n\n<b>")
HELP_SECTIONS = {
    "signal": ("📨 Сигнал", _GUIDE_PARTS[0] + "\n\n/signals — отчёт качества сигналов: доля пропущенных, причины, "
               "задержка, топ направлений (только владелец)."),
    "safety": ("🛡 Безопасность", "<b>" + _GUIDE_PARTS[1]),
    "costs": ("🧮 Что учтено в %", "<b>" + "\n\n<b>".join(_GUIDE_PARTS[2:]).rstrip()),
    "labels": ("🏷 Метки", "<b>Метки надёжности</b>\n"
               "✅ надёжно — причин риска нет; ⚠️ риск — 1–2 причины (в скобках); 🪤 ловушка — 3 и больше, сигналом "
               "не приходит (SIGNAL_TRAPS=0), в /top и /best видна с меткой.\n"
               "Надёжность N/10 — 10 минус веса причин; оценка ±X — прибыль минус штраф за риск, по ней сортировка.\n\n"
               "<b>Причины риска</b>: цена далеко от ориентира, мерчант у порога фильтров, рискованные условия, мерчант "
               "офлайн, 2+ перевода или конвертации, волатильная монета, спред ≥ 5%, «обменник → обменник».\n\n"
               "<b>Отсеивается совсем</b>: цена дальше MAX_DEV (видно в /traps), стоп-фразы в условиях (третьи лица, "
               "мессенджеры), мерчанты ниже MIN_ORDERS / MIN_RATE / MERCHANT_MIN, блэклист."),
    "market": ("📊 Рынок", "<b>Рынок</b>\n"
               "/best — лучшая связка сейчас\n/top — топ связок графиком, «📄 Подробно» — текстом\n"
               "/calc 20000 — расчёт под свою сумму (20к, 1,5 млн), настройки не меняет\n"
               "/maker USDT — цена, чтобы встать первым объявлением, место в стакане\n"
               "/banks USDT — объём объявлений по банкам\n/fees — комиссии вывода по сетям\n"
               "/history — лучшее время суток и хитмап спреда за 7 дней\n"
               "/backtest — сколько раз связки были выше порога по истории\n/safety — 115-ФЗ, блокировки, правила"),
    "journal": ("🧾 Сделки", "<b>Сделки и журнал</b>\n"
                "/stats — журнал за день, неделю, месяц: сделки, сумма, расчёт против факта\n"
                "/export (month / year / prev) — журнал в CSV для банка и 3-НДФЛ\n"
                "/mybanks — свои банки и бесплатные лимиты СБП\n/fav — избранные маршруты\n"
                "/alert USDT sell 92 7d, /alerts — алерты на курс\n"
                "/blacklist, /blacklist note &lt;id&gt; &lt;текст&gt; — скрытые мерчанты\n"
                "/balance — баланс подключённых бирж, итог в ₽\n/traps — последние отсеянные ловушки\n"
                "/hedge — хедж круга шортом перпа: открытые, закрыть"),
    "paper": ("🧪 Сухой прогон", "<b>Сухой прогон и бумажные симуляции (без денег)</b>\n"
              "/paper — открытый круг, итоги, план против факта, виртуальный баланс\n"
              "/paper on / off — включить / выключить\n/paper amount 20000 — сумма круга\n"
              "/paper report — отчёт по площадкам и парам + CSV\n/paper reset — обнулить (с подтверждением)\n"
              "/funding — бумажный арбитраж фандинга\n/futures — бумажная стратегия EMA 20/100\n"
              "/maker paper — бумажный мейкер\n/calibration — поправка факт − план (CALIBRATION=1)"),
    "settings": ("⚙️ Настройки", "<b>Настройки и служебное</b>\n"
                 "/settings — порог, сумма, фильтры, мои биржи и банки, тихие часы, пауза, пресеты (всё в .env)\n"
                 "/amount 100000, /min 1.5 — сумма круга и порог сигнала\n"
                 "/pause 30m / 1h / 3h / до утра, /resume — пауза сигналов\n"
                 "/status — скан, ошибки, скорость; «📋 Подробно» — всё\n/logs — хвост logs/bot.log\n"
                 "/dev — версия, изменения, план, CI\n/allow, /deny, /guests — гости"),
    "keys": ("🔑 Биржи", "<b>Биржи — только чтение</b>\n"
             "«⚙️ Настройки → 🔑 Мои биржи → ➕ Подключить». Ключ — только для чтения: сообщение с ним бот сразу "
             "удалит, проверит права и будет показывать баланс и движения. Ключ с правами торговли или вывода бот "
             "удалит сразу (если не ALLOW_UNSAFE_KEYS=1)."),
}
HELP_INTRO = ("❓ <b>Справка</b>\n\nБот ищет P2P-связки за рубли и присылает карточку: что купить, куда перевести, "
              "где продать и сколько останется чистыми. Деньги он не трогает — сделки делаешь ты.\n\n"
              "Выбери раздел кнопкой ниже. Законы, документы для банка, блокировки карт — /safety. "
              "Площадки — ссылками внизу.")


def help_view(section=None):
    """(текст, кнопки) «/help»: без section — вступление и кнопки разделов; с section — текст раздела и те же кнопки
    (открытый раздел отмечен), неизвестный раздел — вступление. Внизу — ссылки на площадки (LINKS)."""
    keys = list(HELP_SECTIONS)
    if section not in keys:
        section = None
    text = HELP_INTRO if section is None else HELP_SECTIONS[section][1] + "\n\n<i>Разделы — кнопками ниже.</i>"
    buttons = [{"text": ("• " if k == section else "") + HELP_SECTIONS[k][0], "callback_data": f"help:{k}"}
               for k in keys]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if section is not None:
        rows.append([{"text": "⬅️ Оглавление", "callback_data": "help:"}])
    return text, {"inline_keyboard": rows + LINKS["inline_keyboard"]}
# «🛡 Безопасность» (/safety) — общая справка, доступна и гостям; только факты, без советов по обходу контроля банков.
SAFETY = ("🛡 <b>Безопасность P2P</b> — справка, не юридическая консультация.\n\n"
          "<b>115-ФЗ.</b> Банк может запросить документы по операциям и ограничить их. Храни историю ордеров "
          "на биржах, TXID переводов и выписки банка.\n\n"
          "<b>161-ФЗ и база ЦБ (ФинЦЕРТ).</b> Если банк счёл перевод мошенническим, данные могут попасть в базу ЦБ, "
          "и банки ограничат карты и онлайн-банк. Заблокировали — запроси у банка основание и обжалуй блокировку "
          "в официальном порядке.\n\n"
          "<b>Приказ ЦБ ОД-2506.</b> С 01.01.2026 платёж новому получателю в течение 24 ч после перевода самому себе "
          "больше 200 000 ₽ по СБП может быть задержан.\n\n"
          "<b>Ст. 187 УК РФ.</b> С 05.07.2025 передать свою карту или доступ к онлайн-банку другим — преступление. "
          "Никому не давай пользоваться своими картами.\n\n"
          "<b>Правила сделки</b>\n"
          "• Плати только со своих карт.\n"
          "• Не принимай «оплату от третьих лиц».\n"
          "• Крипту отпускай, только когда деньги реально пришли на счёт.\n\n"
          "<b>282-ФЗ.</b> До 30.06.2027 P2P на своём аккаунте работает как раньше. С 01.07.2027 сделки резидентов "
          "идут через посредников из реестра Банка России — проверь правила до этой даты.")
EXPORT_PERIODS = {"month": "month", "месяц": "month", "year": "year", "год": "year", "prev": "prev",
                  "прошлый": "prev", "prevyear": "prevyear", "прошлыйгод": "prevyear"}
EXPORT_HELP = ("Формат: /export — сделки журнала за текущий месяц; /export year — с 1 января; /export prev — "
               "прошлый месяц; /export 2026 — весь 2026 год (для 3-НДФЛ); время — МСК.")
EXPORT_NOTE = ("Это выгрузка данных журнала, не налоговая консультация: состав документов и расчёт налога "
               "сверяй с бухгалтером.")
WAIT = "Первый скан ещё идёт, подожди пару секунд."
MIN_PRESETS = (1, 2, 3, 5)
AMOUNT_PRESETS = (25000, 50000, 100000, 200000)
VENUE_DOWN_AFTER = 900      # сек: площадка отдаёт ошибку дольше — алерт, даже если сканы не подряд
VENUE_FAIL_STREAK = 3       # или столько сканов подряд с ошибкой
VENUE_ALERT_COOLDOWN = 3600  # не чаще раза в час на площадку
SCAN_STALL_MINUTES_DEFAULT = 5   # мин без успешного скана — алерт «скан стоит» (SCAN_STALL_MINUTES)
WATCHDOG_TICK = 60                # сек между проверками watchdog
SCAN_STEP_ALERT_AFTER = 3         # шаг скана падает подряд столько раз — один алерт владельцу (topic dev)
SCAN_STEP_ALERT_RETRY_SEC = 300   # не чаще одного повторного алерта по шагу за этот интервал
SCAN_STEP_NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError)  # сеть подождёт, не алертим
LADDER_ALERT_COOLDOWN = 86400  # предложение лестницы суммы сухого прогона — не чаще раза в сутки
EXCHANGE_NAMES = {"bybit": "Bybit", "mexc": "MEXC", "htx": "HTX", "kucoin": "KuCoin", "bitpapa": "BitPapa",
                  "lbank": "LBank"}
ASSET_LIST = tuple(DEFAULT_ASSETS.split(","))       # монеты для кнопок «🎛 Фильтры»
EXCHANGE_LIST = tuple(ALL_EXCHANGES.split(","))      # площадки для кнопок «🎛 Фильтры»
VENUE_NAMES = dict(EXCHANGE_NAMES, bestchange="BestChange")  # + обменник, которого нет в EXCHANGE_NAMES
# экраны аккаунтов (ключи, /balance, история): + биржи, которые не P2P-площадки бота и в скан/фильтры не попадают
ACCOUNT_NAMES = dict(EXCHANGE_NAMES, bingx="BingX", cryptomus="Cryptomus")
ACCOUNT_ONLY = tuple(ex for ex in ACCOUNT_NAMES if ex not in VENUE_NAMES)   # ("bingx", "cryptomus")
ACCOUNT_POLL_INTERVAL_DEFAULT = 60  # опрос истории аккаунтов, сек — если не задано в .env
KEY_RECHECK_HOURS_DEFAULT = 1.0     # повторная проверка прав ключей бирж, часы — если не задано в .env
LIVE_EDIT_INTERVAL = 30     # сек: не чаще обновляем карточку последнего сигнала вместо повторной отправки
STALE_RETRY_BASE = 30       # сек: пометку «⌛ устарела» после 429/5xx/сбоя сети повторим не раньше (дальше ×2)
STALE_RETRY_MAX = 600       # сек: потолок паузы между повторами пометки
# отказы Telegram на правку сообщения, которые не про кнопки: повтор с обычными кнопками их не исправит, и цветные
# кнопки из-за них выключать нельзя (Bot._fancy_failed)
NOT_BUTTON_ERRORS = ("message is not modified", "message to edit not found", "message can't be edited",
                     "message not found", "chat not found")
# признаки отказа из-за самой разметки кнопок («can't parse inline keyboard button», «can't parse reply keyboard
# markup», BUTTON_TYPE_INVALID, REPLY_MARKUP_INVALID) — только так сервер или клиент не принимает цвета (style) и «📋»
# (copy_text). Отказ без них (403 «bot was blocked by the user», «message is too long», «can't parse entities»)
# повтор с обычными кнопками не исправит, и цветные кнопки из-за него выключать нельзя (Bot._fancy_failed)
BUTTON_ERRORS = ("button", "keyboard", "markup")
MARKET_STATUS_INTERVAL = 60  # сек: не чаще обновляем закреплённое сообщение «Статус рынка»
MSK = timezone(timedelta(hours=3))                    # тихие часы и /pause считаем по МСК, не по времени ПК
PAUSE_PRESETS = {"30m": 1800, "1h": 3600, "3h": 3 * 3600}  # аргументы /pause -> секунды
PAPER_STAGE_LABELS = {"buy": "оплата", "transfer": "перевод", "sell": "продажа"}
PAPER_AMOUNTS = (10000, 20000)   # суммы круга сухого прогона на кнопках /paper
PAPER_RESET_MARKUP = {"inline_keyboard": [[{"text": "🗑 Да, обнулить", "callback_data": "paper_reset:yes"},
                                           {"text": "Отмена", "callback_data": "paper_reset:no"}]]}


DIGEST_MAX = 4000                  # утренний дайджест — одно сообщение, предел Telegram 4096 с запасом
DIGEST_VENUES = 8                  # направлений площадок в дайджесте, остальные — «…ещё N»
DIGEST_FALLBACK_WINDOW = 12 * 3600  # начало ночи неизвестно (бот перезапущен в тихие часы) — окно 12 ч до конца
DIGEST_RETRY = 600                  # сек — повтор недоставленного дайджеста не чаще


def cut_lines(text, limit):
    """Обрезать HTML-текст до limit символов целыми строками (теги в строках закрыты — разметку не рвём); что не
    влезло — «…» последней строкой. Одна строка длиннее limit — без тегов, по символам."""
    if len(text) <= limit:
        return text
    kept, size = [], 2   # 2 — «\n…»
    for line in text.split("\n"):
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    if not kept:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        out = ""
        for ch in plain:   # экранируем посимвольно: срез не разорвёт «&amp;»
            esc = html.escape(ch)
            if len(out) + len(esc) > limit - 1:
                break
            out += esc
        return out + "…"
    return "\n".join(kept) + "\n…"


def account_poll_interval():
    """Читаем ACCOUNT_POLL_INTERVAL при каждом обращении, а не при импорте модуля — иначе значение
    из .env не подхватывается: load_env() вызывается в main() уже после импорта bot.py."""
    return int(os.getenv("ACCOUNT_POLL_INTERVAL", ACCOUNT_POLL_INTERVAL_DEFAULT))


def key_recheck_hours():
    """KEY_RECHECK_HOURS — раз в сколько часов accounts_loop заново проверяет права сохранённых ключей бирж теми же
    правилами, что и при старте (биржа могла перевыпустить ключ с торговлей/выводом, а процесс живёт неделями).
    0 — только при старте; мусор или минус — значение по умолчанию. Читаем при каждом обращении."""
    try:
        v = float(os.getenv("KEY_RECHECK_HOURS", KEY_RECHECK_HOURS_DEFAULT))
    except ValueError:
        return KEY_RECHECK_HOURS_DEFAULT
    return v if v >= 0 and v == v and v != float("inf") else KEY_RECHECK_HOURS_DEFAULT


def signal_traps():
    """SIGNAL_TRAPS=1 — «🪤 ловушки» тоже приходят сигналом (обычные, избранные, ночной дайджест). По умолчанию
    нет: их видно только в /top и /best с пометкой. Читаем при каждом обращении, как paper.settings()."""
    return os.getenv("SIGNAL_TRAPS", "0").strip().lower() in ("1", "true", "yes", "on")


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
    "bingx": ("Создай ключ на BingX: Профиль → API Management → Create API. "
              "Права — только «Read» (сними «Spot Trading», «Perpetual Futures Trading», «Universal Transfer» и "
              "«Withdraw»), в IP whitelist впиши IP своего ПК."),
    "cryptomus": ("⚠️ У Cryptomus <b>нет ключей только для чтения</b>: любой ключ даёт двигать деньги. Бот делает "
                  "только запросы на чтение (баланс, история), но оставит такой ключ, только если в .env стоит "
                  "ALLOW_UNSAFE_KEYS=1 — иначе удалит сразу после проверки.\n"
                  "Нужны два значения. Первое — <b>ID</b> (UUID): User ID личного кабинета (значок профиля) или "
                  "Merchant ID бизнес-кабинета. Второе — <b>API key</b> того же кабинета: лучше User API key личного "
                  "кабинета (Settings → User API key: конвертации и ордера, вывода наружу в его API не описано), для "
                  "бизнес-кабинета — Payment API key. Ключ выплат (<b>Payout key</b>) сюда не присылай: он нужен только "
                  "для /payout и вписывается на ПК в .env (подробности — /payout). Если "
                  "Cryptomus позволяет — ограничь ключ по IP своего ПК."),
}
KEY_STEPS = {"cryptomus": ("ID", "ID (User ID или Merchant ID, UUID)", "API key")}   # ввод ключа: что получили, 1-й и 2-й шаг
KEY_STEPS_DEFAULT = ("Ключ", "API key", "secret")


def key_hint(ex, name):
    """Подсказка, как создать ключ «только чтение» с IP-whitelist — своя для каждой биржи, иначе общая фраза."""
    return KEY_HINT.get(ex, f"Создай в личном кабинете {name} API-ключ <b>только для чтения</b> "
                             f"(без торговли и выводов), по возможности ограничь его по IP.")


def save_env(key, value, path=ENV_PATH):
    """KEY=value в .env и в окружение процесса. Ключ сравнивается без учёта регистра (окружение Windows регистр не
    различает, а load_env берёт первую строку): первая совпавшая строка заменяется, повторы удаляются. Перевод строки
    в значении — ValueError: иначе через значение (например, данные кнопки) в .env дописывается чужая строка."""
    value = str(value)
    # любой разделитель строк, который понимает str.splitlines (\x0b, \x0c, \x1c-\x1e, \x85, U+2028/2029…): файл
    # читается ниже именно splitlines, и такое значение при следующей записи распалось бы на две строки .env
    if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value
            or "".join(value.splitlines()) != value or len(value.splitlines()) > 1):
        raise ValueError(f"save_env: недопустимый ключ или значение ({key!r})")
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip().upper() == key.upper():
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    # и сразу в окружение процесса: PAPER, PAPER_AMOUNT и др. читаются через os.getenv при каждом обращении —
    # без этого «/paper on» отвечал «включён», а работал только после перезапуска бота
    os.environ[key] = str(value)


def _qty(x):
    """Объём монеты для вставки в ордер: USDT — сотые, мелкие монеты — до 6 значащих."""
    return f"{x:.2f}" if x >= 100 else f"{x:.6g}"


def copy_buttons(d, cfg, snap):
    """Кнопки «📋» (copy_text, Bot API 7.11): сумма круга в ₽ — вставить в ордер на покупку, объём монеты
    на выходе маршрута — в ордер на продажу. Без snap объём не посчитать — только сумма.
    Объём — фактический выход маршрута, без запаса на курс (он занижает оценку прибыли, но не сам
    выход монеты — продавать придётся всё, что реально пришло; запас виден отдельной строкой в
    маршруте карточки)."""
    _, b, s, _ = d
    row = [{"text": f"📋 Сумма: {_money(cfg.amount)} ₽", "copy_text": {"text": f"{cfg.amount:g}"}}]
    qty = _route_qty(b, s, cfg, snap.spot, snap.over_banks, disable=frozenset({"risk"})) if snap else None
    if qty:
        row.append({"text": f"📋 Продать: {_qty(qty)} {s.asset}", "copy_text": {"text": _qty(qty)}})
    return row


def plain_markup(markup):
    """Обычные кнопки для клиента/сервера без Bot API 9.4: цвета (`style`) убираем, «📋» (copy_text) выкидываем."""
    rows = []
    for row in markup.get("inline_keyboard", []):
        row = [{k: v for k, v in b.items() if k != "style"} for b in row if "copy_text" not in b]
        if row:
            rows.append(row)
    return {"inline_keyboard": rows}


def delivery_final(r):
    """Ответ Telegram закрывает отправку сигнала/алерта: доставлено или отказ, который повтор не исправит
    (400 — чат/топик не найден, разметка; 403 — бот заблокирован). 429, 5xx и сбой сети — повторим на
    следующем скане."""
    return bool(r.get("ok")) or r.get("error_code") in (400, 403)


def is_fancy(markup):
    return any("style" in b or "copy_text" in b for row in (markup or {}).get("inline_keyboard", []) for b in row)


def deal_markup(d, deal_id=None, cfg=None, snap=None, nav=True):
    """Кнопки под карточкой: купить (зелёная) / продать (красная) — `style` из Bot API 9.4, спот, «📋» копировать
    сумму и объём (если передан cfg), шаги/сделал/скрыть (если сигнал запомнен), топ/обновить."""
    _, b, s, route = d
    sell_n = sell_step_number(route)   # номера шагов — как в подписи: 1 купить … N продать
    row = [{"text": f"{label} на {ad.ex}", "url": venue_url(ad), "style": style}
           for ad, label, style in ((b, "🟢 1. Купить", "success"), (s, f"🔴 {sell_n}. Продать", "danger"))
           if venue_url(ad)]
    rows = [row] if row else []
    m = re.search(r"спот (\w+)→(\w+) на (\w+)", route)
    if m and spot_url(route):
        rows.append([{"text": f"🔁 Спот {m.group(1)}→{m.group(2)} · {m.group(3)}", "url": spot_url(route)}])
    if cfg is not None:
        rows.append(copy_buttons(d, cfg, snap))
    if deal_id is not None:
        rows.append([{"text": "📝 Инструкция", "callback_data": f"steps:{deal_id}"},
                     {"text": "✅ Сделал", "callback_data": f"did:{deal_id}"}])
        fav = favorites.is_fav((b.ex, b.asset, s.ex, s.asset))
        rows.append([{"text": "★ Убрать из избранного" if fav else "⭐ В избранное", "callback_data": f"fav:{deal_id}"},
                     {"text": "🚫 Скрыть мерчанта", "callback_data": f"bl:{deal_id}"}])
    if nav:   # в сигналах не показываем: общие кнопки сливали соседние карточки в одну ленту
        rows.append([{"text": "📊 Топ связок", "callback_data": "top"}, {"text": "🔄 Лучшая сейчас", "callback_data": "best"}])
    return {"inline_keyboard": rows}


def steps_view(d, cfg, snap):
    """Текст «📝 Инструкция»: пошаговый чек-лист маршрута с ценами объявлений (на момент сигнала) и
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
    """Текст уведомления о новом движении по счёту: депозит/вывод, перевод между своими кошельками (Cryptomus) или
    другому пользователю биржи (BingX — деньги ушли третьему лицу, не «внутренний»), спот-сделка или P2P-ордер Bybit."""
    name = ACCOUNT_NAMES.get(ex, ex)
    if "fiat" in it:   # P2P-ордер Bybit: {id, side, asset, fiat, amount, price, ts}
        arrow = "купил" if it["side"] == "buy" else "продал"
        return f"💱 {name} P2P: {arrow} {it['amount']:g} {it['asset']} за {it['fiat']}"
    if it.get("kind") == "trade":
        arrow = "купил" if it["side"] == "buy" else "продал"
        return f"💱 {name}: {arrow} {it['amount']:g} {it['asset']} по {it['price']:g}"
    label = {"deposit": "пришёл депозит", "withdraw": "исполнен вывод", "transfer": "внутренний перевод",
             "transfer_out": "списан перевод другому пользователю"}.get(it.get("kind"), it.get("kind"))
    return f"💰 {name}: {label} — {it['amount']:g} {it['asset']}"


ACCOUNT_STATUS_ICON = {"none": "➖", "unknown": "❓", "ok": "✅", "error": "⚠️", "unsafe": "⚠️"}
ACCOUNT_STATUS_NOTE = {"none": "", "unknown": " (права не подтверждены)", "ok": " (только чтение)",
                       "error": " (ошибка проверки)", "unsafe": " (права сверх чтения, оставлен тобой)"}


def accounts_view(cfg):
    """Текст и кнопки раздела «🔑 Мои биржи»: список бирж со статусом последней проверки ключа —
    подтверждён только чтение / ошибка проверки / ещё не проверялся (не путать с «не подключён»).
    P2P-площадки из фильтров, затем биржи только для аккаунтов (ACCOUNT_ONLY: BingX, Cryptomus)."""
    lines = ["🔑 <b>Мои биржи</b>", "",
             "Только чтение: балансы, история. Торговых ордеров, выводов и P2P-действий бот не делает.", ""]
    rows = []
    for ex in dict.fromkeys([*cfg.exchanges, *ACCOUNT_ONLY]):
        name = ACCOUNT_NAMES.get(ex)
        if not name:
            continue
        state, _ = accounts.verify_status(ex)
        icon = ACCOUNT_STATUS_ICON[state]
        lines.append(f"{icon} {name}{ACCOUNT_STATUS_NOTE[state]}")
        rows.append([{"text": f"{icon} {name}", "callback_data": f"acc:{ex}"}])
    rows.append([{"text": "⚙️ Настройки", "callback_data": "settings"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def env_key_hint(ex):
    """Подсказка после отключения ключа, если он записан ещё и в .env: бот его уже не берёт, но секрет лежит в файле."""
    if not accounts.in_env(ex):
        return ""
    return (f"\nКлюч также задан в .env ({ex.upper()}_API_KEY/{ex.upper()}_API_SECRET) — бот его больше "
            "не использует, но сотри его оттуда.")


def allow_unsafe_keys():
    """ALLOW_UNSAFE_KEYS=1 в .env — владелец сознательно оставляет ключ с правами сверх чтения (торговля/вывод).
    По умолчанию такой ключ удаляется. Бот и в этом режиме делает только запросы на чтение."""
    return os.getenv("ALLOW_UNSAFE_KEYS", "0").strip() == "1"


def readonly_note(safe, detail=""):
    """Хвост к «✅ Подключено»/«✅ Ключ рабочий»: «только чтение» — лишь когда биржа сама подтвердила права ключа."""
    if safe:
        return " (только чтение)"
    if safe is False:
        return (f". ⚠️ Ключ даёт больше, чем чтение ({html.escape(detail)}) — оставлен по твоему решению "
                f"(ALLOW_UNSAFE_KEYS=1). Бот делает только запросы на чтение, но при утечке ключа им можно торговать"
                f" или выводить. Отключить: ALLOW_UNSAFE_KEYS=0 в .env.")
    return ", но права ключа проверить не удалось — убедись, что у него нет прав на торговлю и вывод."


def verify_state(ok, safe):
    """Итог проверки ключа для accounts.set_verified: "error" — verify() не прошёл, "ok" — прошёл и
    биржа подтвердила права только на чтение, "unknown" — прошёл, но права подтвердить не удалось,
    "unsafe" — прошёл, ключ даёт больше, чем чтение (оставлен владельцем через ALLOW_UNSAFE_KEYS=1)."""
    if not ok:
        return "error"
    if safe is False:
        return "unsafe"
    return "ok" if safe is True else "unknown"


def account_view(ex):
    """Текст и кнопки карточки одной биржи: статус, «Проверить»/«Удалить» или «Подключить»."""
    name = ACCOUNT_NAMES.get(ex, ex)
    pair = accounts.keys(ex)
    back = {"text": "⬅️ Мои биржи", "callback_data": "accounts"}
    if pair:
        state, err = accounts.verify_status(ex)
        status_line = {
            "unknown": "❓ Права ключа не подтверждены (не проверялся или биржа не вернула права).",
            "ok": "✅ Подтверждён только чтение.",
            "error": f"⚠️ Ошибка последней проверки: {html.escape(err)}" if err else "⚠️ Ошибка последней проверки.",
            "unsafe": f"⚠️ Ключ даёт больше, чем чтение ({html.escape(err)}) — оставлен по твоему решению (ALLOW_UNSAFE_KEYS=1).",
        }[state]
        need = (f"Ключей только для чтения у {name} нет — бот сам делает только запросы на чтение"
                if ex in accounts.NO_READONLY_KEYS else "Нужен ключ только для чтения")
        text = (f"🔑 <b>{name}</b>\n\nКлюч подключён: <code>{accounts.mask(pair[0])}</code>\n{status_line}\n"
                f"{need}: права проверяю при подключении, по «🔄 Проверить» и при старте бота.")
        kb = [[{"text": "🔄 Проверить", "callback_data": f"acc_check:{ex}"}],
              [{"text": "🗑 Удалить ключ", "callback_data": f"acc_del:{ex}"}], [back]]
    elif ex in accounts.ONBOARDABLE:
        text = f"🔑 <b>{name}</b>\n\nКлюч не подключён.\n\n{key_hint(ex, name)}"
        kb = [[{"text": "➕ Подключить", "callback_data": f"acc_add:{ex}"}], [back]]
    else:
        text = f"🔑 <b>{name}</b>\n\nПодключение ключа пока не реализовано."
        kb = [[back]]
    return text, {"inline_keyboard": kb}


ONBOARD_BANKS = ("Sberbank", "T-Bank", "Alfa-bank", "VTB", "SBP")  # шаг 2/3 онбординга — фильтр способов оплаты


def onboarding_amount_view():
    """Шаг 1/3 онбординга («/start» в первый раз): сумма круга — те же пресеты, что и в «⚙️ Настройки»."""
    text = "👋 <b>Настроим бота за 3 шага.</b>\n\nШаг 1/3 — сумма одного круга сделки в рублях."
    kb = [[{"text": f"{v // 1000}к", "callback_data": f"onb_amt:{v}"} for v in AMOUNT_PRESETS]]
    return text, {"inline_keyboard": kb}


def onboarding_banks_view(selected):
    """Шаг 2/3: банки, которыми пользуется человек (сохранится в INCLUDE_PAY). Можно выбрать несколько
    или не выбрать ни одного — тогда бот покажет связки по всем банкам."""
    mark = lambda on, t: ("✅ " if on else "⬜ ") + t
    kb = [[{"text": mark(name in selected, name), "callback_data": f"onb_bank:{name}"}] for name in ONBOARD_BANKS]
    kb.append([{"text": "Дальше ➡️", "callback_data": "onb_bank_next"}])
    text = ("Шаг 2/3 — какими банками пользуешься (можно несколько). Ничего не выбрал — покажу связки по всем "
            "банкам.\nПоменять можно потом в «⚙️ Настройки → 🎛 Фильтры».")
    return text, {"inline_keyboard": kb}


def onboarding_min_view():
    """Шаг 3/3: порог сигнала — те же пресеты, что и в «⚙️ Настройки»."""
    text = "Шаг 3/3 — с какой чистой прибыли присылать сигнал."
    kb = [[{"text": f"{v}%", "callback_data": f"onb_min:{v}"} for v in MIN_PRESETS]]
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
    """Текст и кнопки «📋 Пресеты»: встроенные (не удаляются) и сохранённые пользователем (можно удалить).
    В callback_data — короткий id пресета, не имя: у Telegram лимит 64 байта, имя кириллицей его превышает."""
    builtin, custom = presets.builtin_presets(cfg), presets.list_custom()
    lines = ["📋 <b>Пресеты фильтров</b>", "", "Пресет меняет сразу все свои поля (условие «И»).", ""]
    kb = []
    for name in builtin:
        lines.append(f"⚙️ {html.escape(name)}")
        kb.append([{"text": f"▶️ {name}"[:64], "callback_data": f"preset_apply:{presets.preset_id(name)}"}])
    for name in custom:
        lines.append(f"💾 {html.escape(name)}")
        kb.append([{"text": f"▶️ {name}"[:64], "callback_data": f"preset_apply:{presets.preset_id(name)}"},
                   {"text": "🗑", "callback_data": f"preset_del:{presets.preset_id(name)}"}])
    if not custom:
        lines += ["", "Своих пресетов пока нет — «💾 Сохранить как пресет» в «🎛 Фильтры»."]
    kb.append([{"text": "⬅️ Фильтры", "callback_data": "filters"}])
    return "\n".join(lines), {"inline_keyboard": kb}


BLACKLIST_NOTE_HELP = ("Причина к записи: <code>/blacklist note &lt;id&gt; &lt;текст&gt;</code> — id из списка "
                       "/blacklist.")


BLACKLIST_NOTE_SHOW, BLACKLIST_TEXT_MAX, BLACKLIST_BUTTONS_MAX = 60, 3800, 90   # лимиты Telegram: 4096 символов, 100 кнопок


def blacklist_view(now=None):
    """Текст и кнопки «/blacklist»: список скрытых мерчантов/обменников — id, сколько дней в списке, причина —
    с удалением. Сами записи не снимаются: решает владелец."""
    rows = blacklist.list_all()
    if not rows:
        return ("🚫 <b>Блэклист пуст</b>\n\nКнопка «🚫 Скрыть мерчанта» под сигналом добавляет сюда мерчанта "
                "или обменника — скан больше не покажет связки с ним.", {"inline_keyboard": []})
    now = time.time() if now is None else now
    lines = ["🚫 <b>Блэклист</b>", "", "Скан больше не показывает связки с этими мерчантами и обменниками. "
             "Сами записи не снимаются — только кнопкой 🗑.", ""]
    kb, size = [], sum(len(x) + 1 for x in lines) + len(BLACKLIST_NOTE_HELP) + 60
    for i, (entry_id, ex, nick, added_ts, note) in enumerate(rows):
        name = EXCHANGE_NAMES.get(ex, ex)
        line = f"{name}: {html.escape(nick)} (id {entry_id})"
        if added_ts is not None:   # у записей из версии без даты возраст неизвестен
            line += f", в списке {max(0, int((now - added_ts) // 86400))} дн."
        if note:   # полная причина — в /blacklist note; в списке коротко, чтобы влезть в 4096 символов Telegram
            line += f" — 📝 {html.escape(note if len(note) <= BLACKLIST_NOTE_SHOW else note[:BLACKLIST_NOTE_SHOW] + '…')}"
        if size + len(line) > BLACKLIST_TEXT_MAX or len(kb) >= BLACKLIST_BUTTONS_MAX:
            lines.append(f"…и ещё {len(rows) - i} — сними часть записей кнопками 🗑, чтобы увидеть остальные.")
            break
        size += len(line) + 1
        lines.append(line)
        kb.append([{"text": f"🗑 {name}: {nick}"[:64], "callback_data": f"unbl:{entry_id}"}])
    lines += ["", BLACKLIST_NOTE_HELP]
    return "\n".join(lines), {"inline_keyboard": kb}


TERMS_LOG_SHOW = 10
TRAPS_TEXT_MAX = 4000   # одно сообщение /traps: лимит Telegram 4096 символов, с запасом


def fit_lines(head, rows, room=TRAPS_TEXT_MAX):
    """head + сколько строк rows влезет в room символов (с переводами строк); не влезшие — одной строкой «… и ещё N».
    Длиннее 4096 символов Telegram сообщение не примет (400 «message is too long») — команда молча не ответит."""
    text = "\n".join(head)
    for i, line in enumerate(rows):
        left = len(rows) - i - 1
        tail = f"\n{_more_note(left)}" if left else ""   # место под пометку, если следующие строки не влезут
        if len(text) + 1 + len(line) + len(tail) > room:
            return f"{text}\n{_more_note(len(rows) - i)}"
        text += "\n" + line
    return text


def _more_note(n):
    return f"… и ещё {n} — не влезли в сообщение Telegram"


def traps_view():
    """Текст «/traps»: последние отсеянные аномальные объявления — обучение видеть ловушки без риска. Журнал стоп-фраз
    условий — отдельным сообщением (terms_view): вместе с полным журналом ловушек он не влезает в лимит Telegram."""
    rows = traps_log()
    if not rows:
        return ("🪤 <b>Ловушки</b>\n\nПока ни одной: объявление с ценой намного выгоднее рынка (отсев по "
                "MAX_DEV) автоматически отсеивается и в сигналы не попадает — здесь появятся примеры.")
    head = ["🪤 <b>Отсеянные ловушки</b>", "",
            "Цена выглядит заманчиво, но слишком далека от рынка — скан такие объявления отсеивает "
            "и в сигнал не пускает. Ниже — последние примеры, без риска.", ""]
    lines = [f"{datetime.fromtimestamp(t['ts']).strftime('%d.%m %H:%M')} — {html.escape(t['reason'])}" for t in rows]
    return fit_lines(head, lines)


def terms_view(limit=TERMS_LOG_SHOW):
    """Второе сообщение «/traps»: последние мерчанты, отсеянные стоп-фразами условий (p2p.terms_log), — сверить, не
    режет ли список нормальных. Журнала нет — None, сообщение не шлём."""
    rows = terms_log()
    if not rows:
        return None
    merchants = len({(r["ex"], r["nick"]) for r in rows})
    head = [f"🚫 <b>Отсеяны стоп-фразами в условиях</b> — {merchants} мерчантов с запуска, последние:", ""]
    lines = []
    for r in rows[:limit]:
        when = datetime.fromtimestamp(r["last"]).strftime("%d.%m %H:%M")
        side = "покупка" if r["side"] == "buy" else "продажа"
        lines.append(f"{when} — {html.escape(r['ex'])} {html.escape(r['nick'])} ({side} {html.escape(r['asset'])}): "
                     f"{html.escape(r['label'])}, фраза «{html.escape(r['phrase'])}», сканов {r['scans']}")
    return fit_lines(head, lines)


NETS_ROWS = 30   # строк в /nets


def unmapped_nets_view(rows=None, now=None):
    """Текст «/nets»: сети из справочников площадок, которые netstatus.normalize не распознал (вне KNOWN_NETS). Такая
    «сеть» не совпадёт с той же сетью под другим именем у другой площадки — маршрут через неё не найдётся. Только
    подсказка: расширить маппинг в netstatus.normalize; на расчёт не влияет."""
    rows = netstatus.unmapped() if rows is None else rows
    now = time.time() if now is None else now
    if not rows:
        return ("🧭 <b>Нераспознанные сети</b>\n\nПока нет: все сети из справочников площадок бот узнаёт "
                f"({', '.join(netstatus.KNOWN_NETS)}). Справочники обновляются раз в {netstatus.TTL // 60} мин.")
    lines = ["🧭 <b>Нераспознанные сети</b>", "",
             "Эти имена сетей бот не сопоставил со своими — перевод через них между площадками не посчитается, "
             "даже если у обеих сеть одна. На расчёт это не влияет; если сеть нужна — добавить имя в "
             "netstatus.normalize.", ""]
    for venue, asset, net, rec in rows[:NETS_ROWS]:
        ago = max(0, int((now - rec["last"]) // 60))
        lines.append(f"• {html.escape(venue)} {html.escape(asset or '')}: <code>{html.escape(net)}</code> — "
                     f"{rec['seen']} раз, последний {ago} мин назад")
    if len(rows) > NETS_ROWS:
        lines.append(f"• …ещё {len(rows) - NETS_ROWS}")
    return "\n".join(lines)


MAKER_HELP = ("Формат: /maker USDT — цена, чтобы встать первым в очереди на покупку и на продажу, и сколько это "
              "стоит против сделки сразу.")


def maker_view(snap, cfg, asset):
    """Текст «/maker <монета>»: на каждой подключённой площадке (`cfg.exchanges`) — цена мейкера
    (`p2p.maker_quote`) на покупку и на продажу и спред против цены немедленной сделки. Площадка без
    обеих сторон стакана по этой монете пропускается. В конце — стакан для самого выгодного варианта
    (наименьший спред из всех): `maker_book_lines`."""
    lines = [f"📝 <b>Мейкер {asset}</b>", "", MAKER_HELP, ""]
    quotes = []   # (спред %, площадка, тип объявления, цена) — из них выбираем вариант для блока стакана
    for ex in cfg.exchanges:
        name = EXCHANGE_NAMES.get(ex)
        if not name:
            continue
        rows = []
        buy = maker_quote(snap.groups, name, asset, "buy_ad")
        if buy:
            price, now_price, spread = buy
            rows.append(f"купить: выставить {_price(price)} ₽ (сразу по {_price(now_price)} ₽, "
                        f"переплата {spread:.2f}%)")
            quotes.append((spread, name, "buy_ad", price))
        sell = maker_quote(snap.groups, name, asset, "sell_ad")
        if sell:
            price, now_price, spread = sell
            rows.append(f"продать: выставить {_price(price)} ₽ (сразу по {_price(now_price)} ₽, "
                        f"недополучим {spread:.2f}%)")
            quotes.append((spread, name, "sell_ad", price))
        if rows:
            lines.append(f"<b>{name}</b>")
            lines += rows
            lines.append("")
    if not quotes:
        lines.append("Нет обеих сторон стакана ни на одной подключённой площадке — попробуй другую монету.")
    else:
        _, name, post_side, price = min(quotes, key=lambda q: q[0])
        lines += maker_book_lines(snap, cfg, name, asset, post_side, price)
    return "\n".join(lines).rstrip()


MAKER_ROWS = 5   # сколько объявлений-конкурентов показать вокруг своей цены


def _rival_line(place, a, cfg):
    """Строка конкурента в /maker: место, цена, лимиты, остаток, сделки/%, способы оплаты (коротко);
    ⚠️ — его лимиты не включают сумму круга, за ту же сделку он не конкурирует."""
    pays = a.all_pays if a.all_pays is not None else a.pays   # как на площадке, до своего фильтра оплаты
    short = ", ".join(pays[:2]) + (f" +{len(pays) - 2}" if len(pays) > 2 else "")
    vol = _money(a.avail) if a.avail >= 100 else f"{a.avail:.4g}"
    warn = "" if a.min_amt <= cfg.amount <= a.max_amt else " ⚠️ лимиты не пересекаются"
    return (f"{place}. {_price(a.price)} ₽ · {_money(a.min_amt)}–{_money(a.max_amt)} ₽ · {vol} {a.asset} · "
            f"{a.orders} сд/{a.rate:.0f}% · {html.escape(short)}{warn}")


def maker_book_lines(snap, cfg, ex, asset, post_side, price):
    """Блок /maker для одного варианта (площадка, тип объявления, цена мейкера): место своего объявления
    в очереди (`p2p.maker_place` по `snap.book` — вся выдача площадки, включая мерчантов, отсеянных своими
    фильтрами; без неё — `snap.groups`), до MAKER_ROWS конкурентов вокруг этой цены и спред стакана с остатком
    после комиссии мейкера. Только публичные данные стакана и сумма круга — можно показывать и гостям."""
    side = "sell" if post_side == "buy_ad" else "buy"   # в какой очереди стоит моё объявление (см. maker_quote)
    queue = snap.book.get((ex, side, asset)) or snap.groups.get((ex, side, asset), [])
    place, total, gap = maker_place(queue, price, post_side)
    action = "купить" if post_side == "buy_ad" else "продать"
    lines = [f"📍 <b>Стакан {ex}: {action} по {_price(price)} ₽</b> (лучший вариант)"]
    if place > 1:
        lines.append(f"Место в стакане: {place}-е из {total}, до 1-го {_price(gap)} ₽ ({gap / price * 100:.2f}%)")
    elif queue:
        lines.append(f"Место в стакане: 1-е из {total}, отрыв от 2-го {_price(abs(queue[0].price - price))} ₽")
    else:
        lines.append("Место в стакане: 1-е, других объявлений нет")
    rows = [(n, _rival_line(n, a, cfg)) for n, a in maker_neighbors(queue, place, MAKER_ROWS)]
    if rows:
        rows.append((place, f"▶ {place}. {_price(price)} ₽ — ты"))
        lines.append(f"Конкуренты рядом (сумма {_money(cfg.amount)} ₽):")
        lines += [text for _, text in sorted(rows)]
    spread = book_spread(snap.groups, ex, asset)
    if spread:
        ask, bid, pct = spread
        fee = maker_round_fee(ex)
        rest = (f"комиссия мейкера {ex} неизвестна — остаток не считаю" if fee is None else
                f"после комиссии мейкера за круг ({fee:.2f}%) остаётся {pct - fee:.2f}%")
        lines.append(f"Спред {ex}: купить {_price(ask)} / продать {_price(bid)} ₽ → {pct:.2f}%; {rest}")
    return lines


BANKS_HELP = ("Формат: /banks USDT — сколько объявлений и какой объём (₽) по каждому банку/способу оплаты "
              "на каждой подключённой площадке, отдельно на покупку и на продажу.")
BANK_HISTORY_DAYS = 7    # /banks без монеты — история спреда связок по банкам за столько дней
BANK_HISTORY_ROWS = 8    # банков на сторону


def bank_history_view(days=BANK_HISTORY_DAYS, stats=None):
    """Текст «/banks» без монеты: через какие банки связки выгоднее за `days` дней (history.bank_spread_stats) —
    отдельно банк, которым платим на покупке, и банк, куда получаем на продаже: средний лучший % связки в срезе
    истории (раз в 5 минут), лучший %, доля срезов с плюсом."""
    stats = history.bank_spread_stats(days) if stats is None else stats
    lines = [f"🏦 <b>Спред связок по банкам за {days} дн.</b>", ""]
    if not stats:
        lines.append("Истории ещё нет — бот пишет её раз в 5 минут, пока есть связки.")
    for side, label in (("buy", "Платим мерчанту (покупка)"), ("sell", "Получаем (продажа)")):
        rows = stats.get(side)
        if not rows:
            continue
        lines.append(f"<b>{label}</b>")
        for bank, n, avg, best, pos in rows[:BANK_HISTORY_ROWS]:
            name = "СБП (банк не указан)" if bank == "SBP" else trades.BANK_NAMES.get(bank, bank)
            lines.append(f"• {html.escape(name)}: в среднем {avg:+.2f}%, лучшая {best:+.2f}%, в плюсе "
                         f"{pos * 100:.0f}% срезов ({n})")
        if len(rows) > BANK_HISTORY_ROWS:
            lines.append(f"• …ещё {len(rows) - BANK_HISTORY_ROWS}")
        lines.append("")
    lines.append("Средний — лучшей связки, где банк есть у объявления, по срезам истории. " + BANKS_HELP)
    return "\n".join(lines).rstrip()


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
              "Пример: /alert USDT sell 92 7d vol 50000 reliable repeat 1h\n\n"
              "Алерт на связку: /alert route Bybit MEXC USDT 3% 7d — сообщу, когда покупка на Bybit → продажа на "
              "MEXC по USDT даст ≥3% чистыми (как сигнал, но со своим порогом и сроком). Можно «reliable» и "
              "«repeat 1h».")


def alerts_view(chat_id):
    """Текст и кнопки «/alerts»: активные алерты чата с удалением."""
    rows, routes = alerts.list_all(chat_id), alerts.list_routes(chat_id)
    if not rows and not routes:
        return (f"🔔 <b>Алертов нет</b>\n\n{ALERT_HELP}", {"inline_keyboard": []})
    lines = ["🔔 <b>Алерты</b>" if routes else "🔔 <b>Алерты на курс</b>", ""]
    kb = []
    for alert_id, buy_ex, sell_ex, asset, pct, expires_ts, cooldown, require_reliable in routes:
        left_h = max(0, round((expires_ts - time.time()) / 3600))
        mark = f" 🔁 каждые ≥{cooldown / 3600:g} ч" if cooldown else ""
        rel_mark = " 🛡 не хуже риска" if require_reliable else ""
        lines.append(f"🔀 {html.escape(buy_ex)} → {html.escape(sell_ex)} {asset} ≥{pct:g}% "
                     f"(осталось ~{left_h} ч){mark}{rel_mark}")
        kb.append([{"text": f"🗑 {buy_ex}→{sell_ex} {asset} ≥{pct:g}%"[:64], "callback_data": f"delalert:{alert_id}"}])
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
            ref = snap.refs.get(coin) if snap else None   # до первого снимка курса нет — ₽ не считаем
            rub = amt * ref if ref else None
            if rub:
                total += rub
            coins.append((coin, amt, rub))
        rows.append((ACCOUNT_NAMES.get(ex, ex), coins))
    return rows, total


def portfolio_view(port, snap):
    """Текст «💰 Баланс»: монеты по подключённым биржам и итог в ₽ по ориентиру текущего снимка.
    Снимка ещё нет (первый скан не прошёл) — только количества и пометка, что курс ещё не получен."""
    if not port:
        connectable = ", ".join(ACCOUNT_NAMES.get(ex, ex) for ex in accounts.BALANCE_FETCHERS)
        return (f"💰 <b>Баланс</b>\n\nНи одна биржа не подключена ({connectable}) или баланс пуст. "
                f"Подключи ключ: ⚙️ Настройки → 🔑 Мои биржи.")
    rows, total = portfolio_rows(port, snap)
    lines = ["💰 <b>Баланс по биржам</b>", ""]
    for name, coins in rows:
        lines.append(f"<b>{name}</b>")
        for coin, amt, rub in coins:
            no_rub = " (нет ориентира в ₽)" if snap else ""
            lines.append(f"  {amt:g} {coin}" + (f" ≈ {_money(rub)} ₽" if rub else no_rub))
        lines.append("")
    if snap:
        lines.append(f"<b>Итого:</b> ≈ {_money(total)} ₽")
    else:
        lines.append("<b>Итого:</b> курс ещё не получен — оценка в ₽ появится после первого скана.")
    return "\n".join(lines)


BALANCE_MARKUP = {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "balance"}]]}

# Выплаты Cryptomus (payouts.py): только владелец, только адреса из белого списка, каждую подтверждает кнопкой.
PAYOUT_KEY_HINT = ("💸 <b>Выплаты Cryptomus</b>: ключа выплат нет.\n\n"
                   "Как создать: в Cryptomus открой настройки, включи 2FA, зайди в Business settings и нажми Generate "
                   "Payout key. После генерации ключа Cryptomus на 24 часа блокирует выводы. Там же, в настройках "
                   "мерчанта, скопируй Merchant ID.\n\n"
                   "В Telegram ключ <b>не присылай</b>. Впиши его на ПК в .env бота: "
                   "CRYPTOMUS_PAYOUT_API_KEY=&lt;Merchant ID&gt; и CRYPTOMUS_PAYOUT_API_SECRET=&lt;Payout key&gt;, затем "
                   "перезапусти бота. Если Cryptomus позволяет, ограничь ключ по IP своего ПК. На бизнес-кошельке держи "
                   "только сумму, которую собираешься выплатить.")
PAYOUT_OFF_HINT = ("⛔ Выплаты выключены. Включить их можно только на ПК: PAYOUTS=1 в .env бота, затем перезапуск. "
                   "Из Telegram выплаты можно только выключить кнопкой «⛔ Стоп выплаты».")
PAYOUT_WL_HINT = ("💸 Белый список адресов пуст. Бот платит только на адреса из data/payout_whitelist.json, а сам этот "
                  "файл не меняет. Добавь адрес на ПК: <code>python scripts/payout_whitelist.py add</code> (адрес "
                  "вводится дважды). Посмотреть список: <code>list</code>, убрать адрес: <code>remove</code>.")
PAYOUT_STOPPED = ("⛔ Выплаты выключены (PAYOUTS=0). Включить снова можно только на ПК: PAYOUTS=1 в .env бота, затем "
                  "перезапуск. Если выплата сейчас отправляется, повторов её запроса не будет; ушедший запрос не "
                  "отозвать. Статус выплат, которые уже в обработке, бот продолжает отслеживать.")
PAYOUT_STOPPED_NO_ENV = ("⛔ Выплаты остановлены, но только до перезапуска бота: записать PAYOUTS=0 в .env не удалось "
                         "({err}). Впиши PAYOUTS=0 в .env бота на ПК вручную, иначе после перезапуска выплаты снова "
                         "будут включены.")
PAYOUT_STOP_BTN = {"text": "⛔ Стоп выплаты", "callback_data": "pay_stop"}


def payout_menu_view(entries, used, daily):
    rows = [[{"text": f"{e['name']} · {e['currency']} {e['network']}"[:60], "callback_data": f"pay_to:{e['id']}"}]
            for e in entries]
    rows.append([{"text": "📜 История", "callback_data": "pay_hist"}, PAYOUT_STOP_BTN])
    return ("💸 <b>Выплата с бизнес-кошелька Cryptomus</b>\n"
            f"Лимит на сегодня (МСК): использовано {used:.2f} из {daily:.2f} USDT.\n"
            "Выбери получателя из белого списка. Потом бот спросит сумму и покажет всё ещё раз перед отправкой."), \
        {"inline_keyboard": rows}


def payout_preview_view(entry, amount, q, token):
    """Экран проверки перед отправкой: полный адрес, сеть, memo, комиссия, списание, лимит дня; кнопки одноразовые."""
    cur = entry["currency"]
    memo = f"Memo: <code>{html.escape(entry['memo'])}</code>\n" if entry["memo"] else ""
    left = q["daily"] - q["used"] - q["usdt"]
    warn = (f"⚠️ Есть выплаты с неясным исходом ({q['unknown']}): проверь /payout history, прежде чем платить тому же "
            f"получателю ещё раз.\n\n" if q.get("unknown") else "")
    text = ("💸 <b>Проверь выплату</b> (Cryptomus, бизнес-кошелёк)\n"
            f"Получатель: {html.escape(entry['name'])}\n"
            f"Монета и сеть: <b>{cur} · {entry['network']}</b>\n"
            f"Адрес: <code>{html.escape(entry['address'])}</code>\n{memo}"
            f"Получит: <b>{payouts.fmt(amount)} {cur}</b>\n"
            f"Комиссия Cryptomus: {payouts.fmt(q['fee'])} {cur}, списывается с баланса сверху\n"
            f"Спишется с баланса: {payouts.fmt(q['debit'])} {cur} ≈ {q['usdt']:.2f} USDT\n"
            f"Лимит дня (МСК): использовано {q['used']:.2f} из {q['daily']:.2f} USDT, после выплаты останется "
            f"{left:.2f}\n\n{warn}"
            f"Кнопка действует {payouts.TOKEN_TTL // 60} мин. Отправленную выплату не отменить, поэтому сверь адрес "
            f"и сеть.")
    return text, {"inline_keyboard": [[{"text": "✅ Отправить", "callback_data": f"pay_ok:{token}"},
                                       {"text": "Отмена", "callback_data": f"pay_no:{token}"}], [PAYOUT_STOP_BTN]]}


def _payout_what(row):
    return f"{row['amount']} {row['currency']} ({row['network']}) → {html.escape(row['wl_name'])}"


def payout_event_text(event, row):
    """Сообщение о выплате: итог опроса статуса (paid/failed/found/stuck/mismatch/mismatch_final/notfound/rejected)
    или перезапуск посреди отправки."""
    what, order = _payout_what(row), f"заявка <code>{row['order_id']}</code>"
    status = html.escape(row["status"] or "?")
    if event == "paid":
        txid = f", TXID <code>{html.escape(row['txid'])}</code>" if row["txid"] else ""
        return f"✅ Выплата {what} выполнена{txid}."
    if event == "failed":
        refund = ("По документации Cryptomus при таком провале средства возвращаются на баланс — проверь баланс в "
                  "кабинете." if row["status"] == "fail" else
                  "Возврат средств при таком статусе Cryptomus не обещает — сначала проверь баланс в кабинете, потом "
                  "запускай новую выплату.")
        return (f"❌ Выплата {what} не прошла (статус {status}), {order}. {refund} Автоповтора нет: новую выплату "
                f"запускай через /payout.")
    if event == "stuck":
        return (f"⚠️ Выплата {what}: статус {status}, но Cryptomus не считает его итоговым ({order}). Переотправить "
                f"такую выплату можно только через поддержку Cryptomus — проверь её в кабинете. В дневном лимите "
                f"она учтена, бот продолжает проверять статус.")
    if event == "found":
        return f"ℹ️ Выплата {what} нашлась в Cryptomus ({order}), статус {status}. Итог пришлю сюда."
    if event == "mismatch":
        tail = (f"Cryptomus уже считает её завершённой (статус {status}), бот больше ничего по ней не пришлёт."
                if row["is_final"] else "Итоговый статус Cryptomus бот пришлёт сюда.")
        return (f"🚨 Выплата {what}: {html.escape(row['note'])} ({order}). Проверь её в кабинете Cryptomus. "
                f"В дневном лимите она учтена. {tail}")
    if event == "mismatch_final":
        return (f"🚨 Выплата {what}: итоговый статус Cryptomus — {status}, но {html.escape(row['note'])} ({order}). "
                f"Проверь её в кабинете Cryptomus. В дневном лимите она учтена.")
    if event == "notfound":
        return (f"⚠️ Выплата {what}: {payouts.NOT_FOUND} ({order}). Проверь кабинет Cryptomus: если выплаты там нет, "
                f"она не ушла. В дневном лимите она пока учтена, бот продолжает проверять статус.")
    if event == "rejected":
        return (f"🚫 Cryptomus отклонил выплату {what} и не находит её ({order}): {html.escape(row['note'])}. Деньги "
                f"не ушли, из дневного лимита она убрана. Новая попытка — через /payout.")
    if event == "resumed":
        return (f"⚠️ Бот перезапустился во время отправки выплаты {what} ({order}). Исход неясен, проверяю статус "
                f"в Cryptomus. В дневном лимите выплата учтена.")
    return f"Выплата {what}: {payouts.STATE_NAMES.get(row['state'], row['state'])}."


def payout_result_text(res):
    """Итог payouts.send для владельца."""
    row, state, reason = res["row"], res["state"], html.escape(res.get("reason") or "")
    if state == "refused":
        return f"⛔ Выплата не отправлена: {reason}."
    what, order = _payout_what(row), f"заявка <code>{row['order_id']}</code>"
    if state == "rejected":
        return (f"🚫 Cryptomus отклонил выплату {what}: {reason}. Деньги не ушли, в лимит не засчитано. "
                f"Новая попытка — снова через /payout.")
    if state == "unknown" and payouts.MISMATCH in (res.get("reason") or ""):
        tail = ("Cryptomus уже считает её завершённой, бот больше ничего по ней не пришлёт." if row["is_final"] else
                "Итоговый статус Cryptomus бот пришлёт в «📒 Журнал».")
        return (f"🚨 Исход выплаты {what} неясен: {reason}. Проверь её в кабинете Cryptomus. Заявка "
                f"<code>{row['order_id']}</code> учтена в дневном лимите. {tail} Не повторяй эту выплату, пока не "
                f"разберёшься.")
    if state == "unknown":
        return (f"⚠️ Исход выплаты {what} неясен: {reason}. Заявка <code>{row['order_id']}</code> учтена в "
                f"дневном лимите. "
                f"Бот проверяет её статус в Cryptomus и напишет в «📒 Журнал». Не повторяй эту выплату, пока "
                f"статус не выяснится.")
    if state == "final_paid":
        return payout_event_text("paid", row)
    if state == "final_failed":
        return payout_event_text("failed", row)
    if row["status"] in payouts.FAIL_STATUSES:
        return payout_event_text("stuck", row)
    return (f"✅ Cryptomus принял выплату {what}, {order}, статус {html.escape(row['status'] or '?')}. "
            f"Итог пришлю в «📒 Журнал».")


def payout_history_view(rows, used, daily):
    if not rows:
        return "📜 Выплат ещё не было."
    lines = [f"📜 <b>Выплаты</b>, последние {len(rows)}. Сегодня (МСК): {used:.2f} из {daily:.2f} USDT."]
    for r in rows:
        ts = datetime.fromtimestamp(r["created_ts"], MSK).strftime("%d.%m %H:%M")
        txid = f", TXID <code>{html.escape(r['txid'][:16])}…</code>" if r["txid"] else ""
        note = f" ({html.escape(r['note'])})" if r["state"] in ("rejected", "unknown") and r["note"] else ""
        if r["state"] == "sent" and r["status"] in payouts.FAIL_STATUSES:
            note = f" (статус {r['status']}, не итоговый: переотправка только через поддержку Cryptomus)"
        lines.append(f"• {ts} {_payout_what(r)}: {payouts.STATE_NAMES.get(r['state'], r['state'])}{note}{txid}")
    return "\n".join(lines)


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
    rows.append([{"text": "🧪 Сухой прогон", "callback_data": "paper"},
                 {"text": "📟 Статус", "callback_data": "status"}])
    rows.append([{"text": "🔄 Обновить", "callback_data": "dev"}])
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
    tail = logsafe.redact("".join(lines[-n:]).strip())
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


MY_BANKS = ("T-Bank", "Sberbank", "Alfa-bank", "VTB", "Rosselkhozbank", "MTS Bank", "Ozon Bank", "Gazprombank",
            "Raiffeisen", "Yandex Bank")
# Тарифы, от которых зависит бесплатный лимит СБП в месяц (исследование 25.09.2026; проверь в приложении банка)
SBP_TARIFFS = {"T-Bank": ((100_000, "база"), (300_000, "Pro"), (float("inf"), "Premium")),
               "VTB": ((300_000, "база"), (float("inf"), "Привилегия")),
               "Gazprombank": ((100_000, "база"), (200_000, "Бонус Плюс")),
               "Ozon Bank": ((100_000, "база"), (5_000_000, "Ultra"))}


def _limit_text(limit):
    return "без лимита" if limit == float("inf") else f"{_money(limit)} ₽"


def _limit_key(limit):
    return "inf" if limit == float("inf") else str(int(limit))


def mybanks_view():
    """«🏦 Мои банки»: с каких банков владелец платит (порядок = очерёдность оплаты по СБП), есть ли карты и в
    остальных банках (перевод мерчанту в его банк — внутри банка) и бесплатный лимит СБП каждого по тарифу."""
    listed, star = trades.own_banks()
    lines = ["🏦 <b>Мои банки и лимиты СБП</b>", "",
             "Есть у мерчанта твой банк — перевод внутри банка, лимит СБП не тратится. Мерчант принимает только СБП "
             "или чужой банк — бот считает оплату по СБП с первого твоего банка, у которого бесплатный лимит за "
             "месяц ещё не исчерпан.", ""]
    for b in listed:
        lines.append(f"• {trades.BANK_NAMES.get(b, b)} — бесплатно по СБП {_limit_text(trades.free_limit(b))} в месяц")
    lines.append(f"• {'✅' if star else '➖'} карты и в любом другом банке")
    kb = [[{"text": ("✅ " if b in listed else "") + trades.BANK_NAMES.get(b, b), "callback_data": f"ownbank:{b}"}
           for b in MY_BANKS[i:i + 3]] for i in range(0, len(MY_BANKS), 3)]
    kb.append([{"text": ("✅ " if star else "") + "Остальные банки тоже мои", "callback_data": "ownbank:*"}])
    for bank, tariffs in SBP_TARIFFS.items():
        if bank in listed:
            cur = trades.free_limit(bank)
            kb.append([{"text": ("✅ " if cur == lim else "") + f"{trades.BANK_NAMES[bank]}: {name}",
                        "callback_data": f"sbplim:{bank}:{_limit_key(lim)}"} for lim, name in tariffs])
    return "\n".join(lines), {"inline_keyboard": kb}


def apply_mybanks(data):
    """Кнопки «🏦 Мои банки»: ownbank:<банк>/ownbank:* — добавить/убрать банк; sbplim:<банк>:<лимит> — тариф.
    Значения не из списков кнопок игнорируются."""
    kind, _, rest = data.partition(":")
    if kind == "ownbank":
        listed, star = trades.own_banks()
        if rest == "*":
            star = not star
        elif rest in MY_BANKS:
            listed = [b for b in listed if b != rest] if rest in listed else listed + [rest]
        else:
            return
        save_env("OWN_BANKS", ",".join(listed + (["*"] if star else [])))
    elif kind == "sbplim":
        bank, _, val = rest.partition(":")
        if bank not in SBP_TARIFFS or val not in {_limit_key(lim) for lim, _ in SBP_TARIFFS[bank]}:
            return
        limits = dict(p.split(":", 1) for p in os.getenv("SBP_FREE_LIMITS", "").split(",") if ":" in p)
        limits[bank] = val
        save_env("SBP_FREE_LIMITS", ",".join(f"{k}:{v}" for k, v in limits.items()))


STALE_MARK = "\n\n⌛ <i>связка устарела</i>"   # пометка последнего сигнала по связке, ушедшей из топа
CHIP_DEPTH_TIMEOUT = 4.0   # сек: фоновое уточнение фишек сумм карточки не дольше (CHIP_DEPTH_TIMEOUT в .env)


def chip_depth_timeout():
    """CHIP_DEPTH_TIMEOUT из .env (сек); пусто, не число или не больше нуля — по умолчанию."""
    try:
        v = float(os.getenv("CHIP_DEPTH_TIMEOUT", CHIP_DEPTH_TIMEOUT))
    except ValueError:
        return CHIP_DEPTH_TIMEOUT
    return v if 0 < v < 600 else CHIP_DEPTH_TIMEOUT


def chips_line(amounts):
    """Фишки сумм строкой для подписи карточки — те же, что на картинке (cards._amounts_chips)."""
    parts = [f"{_money(a)} ₽ {v:+.2f}%" if v is not None else f"{_money(a)} ₽ нет объёма" for a, v in amounts.items()]
    return "📏 На другую сумму: " + " · ".join(parts)


def _lean(snap):
    """Снимок без полного стакана (Snapshot.book), всех объявлений скана (Snapshot.ads), котировок перпов
    (Snapshot.perps) и срабатываний стоп-фраз (Snapshot.terms_hits): они нужны только /maker, записи снимка
    (snapshots.py) и журналу отсева (p2p.TERMS_LOG, его пишет scan) по свежему скану — запомненные сделки (до 200) и
    живые карточки их не держат."""
    if snap is not None and (snap.book or snap.ads or snap.jobs or snap.perps or snap.terms_hits):
        return dataclasses.replace(snap, book={}, ads=[], jobs=[], perps={}, terms_hits=[])
    return snap


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
    lines.append("ℹ️ Это оценка по сохранённым снимкам истории, а не перепрогон маршрутов на текущих объявлениях.")
    return "\n".join(lines)


SPEED_NAMES = {"rapira": "Rapira (ориентир)", "spot": "спот", "networks": "справочник сетей"}


def speed_lines(speed, timeout):
    """Строки «⏱ Скорость» для /status из ScanSpeed.summary(): p50/p90 длительности скана и задержки каждого источника
    (медленные первыми), сколько сканов он не успел за VENUE_TIMEOUT."""
    n = speed["n"]
    p50, p90 = speed["scan"]
    lines = [f"⏱ <b>Скорость</b> — последние {n} скан(ов), таймаут площадки {timeout:g} с:",
             f"скан: p50 {p50:.1f} с · p90 {p90:.1f} с"]
    for ex, v in sorted(speed["venues"].items(), key=lambda kv: (-kv[1]["p90"], kv[0])):
        line = f"• {html.escape(SPEED_NAMES.get(ex) or VENUE_NAMES.get(ex, ex))}: p50 {v['p50']:.1f} с · p90 {v['p90']:.1f} с"
        if v["timeouts"]:
            line += f" · таймаут в {v['timeouts']} из {n}"
        lines.append(line)
    return lines


STATS_DIRECTIONS = 8   # направлений в /stats, остальные — «…ещё N»


def direction_lines(rows, limit=STATS_DIRECTIONS):
    """Строки /stats «По направлениям за месяц» из trades.by_direction: какие пары площадок реально приносят деньги —
    по факту (₽ и средний %), где факта нет — только расчёт. [] — сделок за месяц нет."""
    if not rows:
        return []
    lines = ["", "<b>По направлениям за месяц</b> (покупка → продажа):"]
    for d in rows[:limit]:
        line = (f"• {html.escape(d['buy_ex'])} → {html.escape(d['sell_ex'])}: {d['count']} сд., "
                f"{_money(d['amount'])} ₽, расчёт {d['avg_profit']:+.2f}%")
        if d["fact_count"]:
            rub = f"{d['fact_rub']:+,.0f}".replace(",", " ")
            line += f", факт {d['avg_fact']:+.2f}% (у {d['fact_count']}) ≈ {rub} ₽"
        else:
            line += ", факта нет"
        lines.append(line)
    if len(rows) > limit:
        lines.append(f"• …ещё {len(rows) - limit}")
    return lines


OUTAGE_DAYS = 7            # /status «Подробно»: недоступность площадок за столько дней
OUTAGE_MIN_SECONDS = 60    # короче — разовый сбой скана, в сводку не идёт


def _dur(sec):
    sec = int(sec)
    return f"{sec // 3600} ч {sec % 3600 // 60} мин" if sec >= 3600 else f"{max(1, sec // 60)} мин"


def outage_lines(stats, days=OUTAGE_DAYS):
    """Строки «Недоступность площадок за N дн.» из history.outage_stats: сначала дольше всех недоступные. [] — простоев
    не было."""
    if not stats:
        return []
    lines = ["", f"📉 <b>Недоступность площадок за {days} дн.</b> (простои от {OUTAGE_MIN_SECONDS // 60} мин):"]
    for venue, r in sorted(stats.items(), key=lambda kv: (-kv[1]["total"], kv[0])):
        name = VENUE_NAMES.get(venue, venue)
        lines.append(f"• {html.escape(name)}: {r['count']} раз, всего {_dur(r['total'])}, дольше всего "
                     f"{_dur(r['longest'])}" + (f", с алертом {r['alerted']}" if r["alerted"] else ""))
    return lines


def market_status_view(snap, cfg):
    """Текст закреплённого сообщения «Статус рынка»: ориентир курса, лучшая связка, площадки ок/недоступны."""
    lines = ["📌 <b>Статус рынка</b>", "", f"Ориентир USDT: {snap.ref:.2f} ₽ ({html.escape(snap.ref_src)})"]
    if snap.deals:
        profit, b, s, _ = snap.deals[0]
        lines.append(f"🔥 Лучшая связка: {b.ex} → {s.ex} ({b.asset}→{s.asset}) {profit:+.2f}%")
    else:
        lines.append("🔥 Лучшая связка: сейчас нет связок выше порога")
    failed = {k.split("/", 1)[0] for k in snap.errors} & set(cfg.exchanges)
    ok = [VENUE_NAMES.get(ex, ex) for ex in cfg.exchanges if ex not in failed]
    down = [VENUE_NAMES.get(ex, ex) for ex in cfg.exchanges if ex in failed]
    parts = []
    if ok:
        parts.append("✅ " + ", ".join(ok))
    if down:
        parts.append("⚠️ " + ", ".join(down))
    lines.append("Площадки: " + (" · ".join(parts) if parts else "—"))
    lines.append(f"обновлено {datetime.now(MSK).strftime('%H:%M')} МСК")
    return "\n".join(lines)


class Bot:
    def __init__(self, session, token, chat_id, cfg):
        self.s, self.token, self.chat_id, self.cfg = session, token, chat_id, cfg
        self.cooldown = int(os.getenv("COOLDOWN", 600))      # сек: не повторять ту же пару бирж
        self.fancy = os.getenv("FANCY_BUTTONS", "1") != "0"   # цветные кнопки и «📋»; сам выключится при ошибке API
        self.topics = {}          # ключ топика -> message_thread_id, если у бота включены топики в личке
        self.cur_thread = None    # топик, из которого пришла последняя команда/кнопка — туда и отвечаем
        self.key_checked_ts = time.time()   # последняя проверка прав ключей (check_key_safety), для KEY_RECHECK_HOURS
        entries = {g.strip() for g in os.getenv("TG_GUESTS", "").split(",") if g.strip()}
        self.guests = {g for g in entries if not g.startswith("@")}          # id чатов гостей
        self.pending = {g.lower() for g in entries if g.startswith("@")}   # @ники: доступ откроется с первого сообщения
        self.asked = set()        # чужие чаты, которым уже ответили «бот приватный» (раз за запуск)
        # чаты, которым до привязки владельца ответили «владелец — только личный чат» (раз за запуск). Отдельно от
        # asked: иначе такой чат после привязки не дошёл бы до ask_access, и владелец не узнал бы о нём
        self.unbound_asked = set()
        self.refused_at = {}      # чат TG_CHAT_ID не личный -> когда последний раз ответили «команды — только в личке»
        self.username = ""       # @ник бота из getMe — для подсказок
        self.repeat_step = float(os.getenv("REPEAT_STEP", 0.3))  # п.п. роста профита для досрочного повтора
        self.max_signals = int(os.getenv("MAX_SIGNALS", 3))      # сигналим только из топ-N
        self.last = None
        self.paused = False
        self.pause_until = 0.0   # unix-время окончания /pause с аргументом; 0 или прошлое = не активна
        self.quiet_hours = os.getenv("QUIET_HOURS", "01:00-08:00")   # окно тихих часов, МСК "HH:MM-HH:MM"
        self.quiet_on = os.getenv("QUIET_HOURS_ON", "0") == "1"      # тихие часы включены (кнопка в настройках)
        self.night_deals = {}    # (ex,asset,ex,asset) -> лучшая связка за тихие часы, для утреннего дайджеста
        self.quiet_since = None  # когда начались текущие тихие часы (для окна утреннего дайджеста)
        self.digest_pending = False  # дайджест не ушёл (сеть, 429/5xx) — повторить после DIGEST_RETRY
        self.digest_retry_at = 0.0
        self._was_quiet = False  # тихие часы были на прошлом скане — для разового дайджеста при выходе из них
        self.awaiting_amount = False  # ждём сумму текстом после «✏️ Своя сумма»
        self.awaiting_preset_name = False  # ждём имя пресета текстом после «💾 Сохранить как пресет»
        self.awaiting_key = None      # {"ex":.., "step": "key"/"secret", "key":..} — ждём ключ биржи
        self.awaiting_fact = None     # id сделки — ждём фактический результат текстом после «✏️ ввести число»
        self.awaiting_payout = None   # id записи белого списка — ждём сумму выплаты текстом после выбора получателя
        self.payout_preview = None    # {"token", "entry", "amount", "quote", "ts"} — последний предпросмотр выплаты
        self.resumed_payouts = []     # выплаты, прерванные перезапуском (payouts.resume в main), — сообщить владельцу
        self.payout_task = None       # фоновая отправка подтверждённой выплаты: command_loop тем временем принимает «⛔ Стоп»
        self.onboarding = None   # {"step": "amount"/"banks"/"min", "banks": set()} — мастер первого /start
        self.sent = {}
        self.signal_rows = {}   # (ex,asset,ex,asset) -> id открытого эпизода в history.signals (связка выше порога)
        self.snapshot_scans = 0      # сканов с запуска — снимок пишется каждый SNAPSHOT_EVERY-й
        self.snapshot_keep = set()   # id сканов, на которых стартовал круг сухого прогона: их снимок пишется всегда
        self.rep_task = None         # фоновый пересчёт меток репутации мерчантов (schedule_reputation)
        self.cal = None              # EV_RANK=1: калибровка (calibration.build) и когда собрана — пересборка раз в
        self.cal_ts = 0.0            # calibration.REFRESH сек, в отдельном потоке
        self.live_msg = {}   # (ex,asset,ex,asset) -> последнее сообщение сигнала для «живой карточки» (editMessage)
        self.chip_tasks = set()   # фоновые уточнения фишек сумм у отправленных карточек (ссылки держим до конца)
        self.backup_task = None   # фоновая суточная копия баз (schedule_backup)
        self.live = {}                                            # (ex,asset,ex,asset) -> {"first": ts, "streak": n}
        self.live_scans = int(os.getenv("LIVE_SCANS", 2))        # сигнал, только если связка держится ≥ N сканов
        self.venue = {}   # ex -> {"streak": сканов подряд с ошибкой, "down_since": ts, "alerted_at": ts}
        self.deals_by_id = {}   # id -> (d, снимок cfg, snap на момент сигнала) для кнопок «✅ Сделал»/«📝 Инструкция»; не переживает рестарт
        self.acc_seen = {}   # ex -> set известных ключей истории; None пока не было первого опроса
        self.start_ts = time.time()     # для аптайма в /status
        self.scan_error = ""            # текст последней ошибки скана — для watchdog
        self.scan_errors = 0            # ошибок скана подряд
        self.stall_alerted = False      # watchdog уже сообщил «скан стоит» (Telegram принял) — ждём восстановления
        # имя шага scan_loop -> {'n': подряд сбоев, 'last': текст ошибки, 'alerted': бот уже сообщил, 'net': сетевая
        # ошибка (без алерта), 'try_ts': unix-время последней попытки алерта} — для /status и алерта в topic dev
        self.step_fail = {}
        self.watchdog_prev_tick = 0.0   # время прошлого тика watchdog — заметить сон ПК между тиками
        self.watchdog_wake_ts = 0.0     # когда watchdog заметил пробуждение ПК: простой скана считается от него
        # id сигналов от времени старта в мс: после рестарта старая кнопка did:N не попадёт на новую связку
        self.next_deal_id = int(self.start_ts * 1000)
        self.last_scan_ts = 0.0         # unix-время окончания последнего скана
        self.last_scan_duration = 0.0   # сколько секунд занял последний скан
        self.speed = ScanSpeed()        # длительность сканов и задержка площадок за последние сканы (/status)
        self.market_msg_id = None       # id закреплённого сообщения «Статус рынка»
        self.market_status_ts = 0.0     # unix-время последнего обновления статуса рынка
        self.paper_ladder_alerted_ts = 0.0   # unix-время последнего предложения лестницы суммы сухого прогона
        self.paper_reset_ask = self.paper_reset_done = None   # id сообщения-вопроса /paper reset и уже отвеченного

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
        try:
            me = await self.call("getMe")
        except Exception as e:   # нет сети при старте (ПК проснулся, VPN ещё не поднялся) — не падаем: топики из
            logger.warning("getMe: %s — топики из сохранённых, режим проверю при следующем запуске",   # файла
                           accounts.api_error_text(e))
            self.topics = load_topics()
            return
        if me.get("ok"):
            self.username = me["result"].get("username") or ""
        if not me.get("ok") or not me["result"].get("has_topics_enabled"):
            return
        saved = load_topics()
        for key, name in TOPICS:
            if key in saved:
                continue
            try:
                r = await self.call("createForumTopic", chat_id=self.chat_id, name=name)
            except Exception as e:
                r = {"ok": False, "description": accounts.api_error_text(e)}
            if not r.get("ok"):
                logger.warning("createForumTopic: %s", r.get("description"))
                return
            saved[key] = r["result"]["message_thread_id"]
            save_topics(saved)
            self.topics = dict(saved)
            await self.send(TOPIC_HINTS[key], topic=key)
        self.topics = saved

    def chat_for(self, chat_id=None):
        """Куда слать: явный chat_id → он; команда гостя (REPLY_CHAT) → гость; иначе владелец."""
        return chat_id or REPLY_CHAT.get() or self.chat_id

    def is_guest(self, chat):
        return chat in self.guests

    def thread_for(self, topic, chat_id=None):
        """id топика для сообщения: свой у сигналов/журнала/…, иначе тот, где написал пользователь.
        Топики — только в чате владельца."""
        if not self.topics or self.chat_for(chat_id) != self.chat_id:
            return None
        return self.topics.get(topic) if topic else self.cur_thread

    def markup(self, markup):
        """Разметка под возможности сервера/клиента: без цветов и «📋», если они не поддерживаются."""
        return markup if self.fancy or not markup else plain_markup(markup)

    def _fancy_failed(self, r, markup):
        """Отправка с цветными кнопками/«📋» не удалась из-за самих кнопок (BUTTON_ERRORS): дальше шлём обычные кнопки
        и повторяем. Остальные отказы — не про кнопки, цвета не выключаем: 429 и 5xx (перегрузка/сбой Telegram, повтор —
        забота вызывающего, retry_after), 403 (гость заблокировал бота), «message is too long», «can't parse entities»,
        отказ правки из NOT_BUTTON_ERRORS (подпись не изменилась, сообщения нет или его нельзя править) — иначе один
        такой отказ выключал бы цветные кнопки всему боту до перезапуска."""
        code = r.get("error_code") or 0
        text = str(r.get("description") or "").lower()
        if r.get("ok") or not self.fancy or not is_fancy(markup) or code in (403, 429) or code >= 500 \
                or any(x in text for x in NOT_BUTTON_ERRORS) or not any(x in text for x in BUTTON_ERRORS):
            return False
        self.fancy = False
        logger.warning("Telegram: цветные кнопки/copy_text не поддерживаются, дальше обычные: %s", r.get("description"))
        return True

    async def send(self, text, chat_id=None, markup=None, topic=None, thread=None):
        """thread — явный топик (ответ фоновой задачи: cur_thread к тому времени мог смениться)."""
        chat_id = self.chat_for(chat_id)
        params = dict(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        if markup:
            params["reply_markup"] = self.markup(markup)
        thread = thread or self.thread_for(topic, chat_id)
        if thread:
            params["message_thread_id"] = thread
        r = await self.call("sendMessage", **params)
        if self._fancy_failed(r, markup):
            params["reply_markup"] = plain_markup(markup)
            r = await self.call("sendMessage", **params)
        if not r.get("ok"):
            logger.warning("sendMessage: %s", r.get("description"))
        return r

    async def send_photo(self, png, caption, markup=None, topic=None, chat_id=None):
        chat_id = self.chat_for(chat_id)
        thread = self.thread_for(topic, chat_id)
        kw = {"chat_id": chat_id} if chat_id != self.chat_id else {}
        r = await self._post_photo(png, caption, self.markup(markup), thread, **kw)
        if self._fancy_failed(r, markup):
            r = await self._post_photo(png, caption, plain_markup(markup), thread, **kw)
        return r

    async def _post_photo(self, png, caption, markup, thread=None, chat_id=None):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id or self.chat_id))
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

    async def send_document(self, path, caption="", topic=None, chat_id=None):
        """Отправить файл с диска (например CSV-отчёт) документом; caption — обычный текст, без HTML-разметки."""
        chat_id = self.chat_for(chat_id)
        thread = self.thread_for(topic, chat_id)
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)
        if thread:
            form.add_field("message_thread_id", str(thread))
        with open(path, "rb") as f:
            form.add_field("document", f.read(), filename=os.path.basename(path), content_type="text/csv")
        async with self.s.post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=form,
                               timeout=aiohttp.ClientTimeout(total=40)) as r:
            return await r.json()

    async def photo_or_text(self, render, caption, markup, topic=None, chat_id=None):
        """Картинка с подписью; если не вышло — тем же текстом. Возвращает (ответ Telegram, картинка ли)."""
        if len(caption) <= 1024:
            try:
                kw = {"topic": topic} if topic and self.topics else {}
                if chat_id:
                    kw["chat_id"] = chat_id
                r = await self.send_photo(await asyncio.to_thread(render), caption, markup, **kw)
                if r.get("ok"):
                    return r, True
                logger.warning("sendPhoto: %s", r.get("description"))
            except Exception as e:
                logger.warning("card error: %s", e)
        return await self.send(caption, chat_id=chat_id, markup=markup, topic=topic), False

    async def delete_message(self, message_id):
        """deleteMessage с проверкой ответа: True — только если Telegram подтвердил удаление."""
        try:
            r = await self.call("deleteMessage", chat_id=self.chat_id, message_id=message_id)
        except Exception as e:
            logger.warning("deleteMessage error: %s", accounts.api_error_text(e))   # без URL с токеном бота
            return False
        if not r.get("ok"):
            logger.warning("deleteMessage %s: %s", message_id, r.get("description"))
            return False
        return True

    def snap_cfg(self, snap):
        """Настройки расчёта снимка — копия, с которой его собрал fresh_scan (snap.cfg): %, сумма круга на карточке и
        запись «✅ Сделал» — из одного расчёта, даже если сумму сменили после скана. Порог сигнала в расчёт не входит —
        он текущий. У снимка нет snap.cfg (собран не fresh_scan) — текущие настройки."""
        if snap is None or snap.cfg is None:
            return self.cfg
        return dataclasses.replace(snap.cfg, min_profit=self.cfg.min_profit)

    def remember_deal(self, d, cfg=None, snap=None):
        """Запомнить связку под кнопками «✅ Сделал»/«📝 Инструкция»; хранится ограниченное число последних."""
        snap = snap if snap is not None else self.last
        cfg = copy.deepcopy(cfg or self.snap_cfg(snap))   # копия и списков: старая карточка не увидит новые сумму/порог
        snap = _lean(snap)
        deal_id, self.next_deal_id = self.next_deal_id, self.next_deal_id + 1
        self.deals_by_id[deal_id] = (d, cfg, snap)
        if len(self.deals_by_id) > 200:
            del self.deals_by_id[min(self.deals_by_id)]
        return deal_id

    async def send_deal(self, d, prefix="", cfg=None, snap=None, topic=None, chat_id=None, nav=True):
        """Карточка связки (картинка или текст); возвращает ответ Telegram — ok ли доставка."""
        snap = snap if snap is not None else self.last
        cfg = cfg or self.snap_cfg(snap)
        guest = self.is_guest(self.chat_for(chat_id))
        deal_id = None if guest else self.remember_deal(d, cfg, snap)   # у гостя нет «✅ Сделал»/«📝 Инструкция»/«🚫»
        # фишки сумм — по стакану скана: карточка уходит сразу, уточнение под каждую сумму (chip_refresh) — потом, фоном
        amounts = deal_amounts(d, cfg, snap) if snap else None
        rel = (*reliability(d, cfg, snap), reliability_index(d, cfg, snap)) if snap else None
        caption = prefix + fmt_signal(d, cfg, snap)
        markup = deal_markup(d, deal_id, cfg, snap, nav)
        r, is_photo = await self.photo_or_text(lambda: deal_card(d, cfg, amounts, rel), caption, markup, topic,
                                               chat_id)
        message_id = (r.get("result") or {}).get("message_id") if r.get("ok") else None
        if topic == "signals" and message_id is not None and not guest:
            key = self._deal_key(d)
            self.live_msg[key] = {"message_id": message_id, "photo": is_photo, "deal_id": deal_id,
                                  "last_edit": time.time(), "caption": caption, "stale": False,
                                  "deal": self.deals_by_id.get(deal_id)}   # связка/настройки/снимок под кнопками
        if message_id is not None and snap is not None and not guest and self.s is not None:
            card = {"message_id": message_id, "photo": is_photo, "caption": caption, "chat_id": self.chat_for(chat_id),
                    "markup": markup, "deal_id": deal_id, "nav": nav}
            task = asyncio.ensure_future(self.chip_refresh(d, cfg, snap, amounts, card))
            self.chip_tasks.add(task)
            task.add_done_callback(self.chip_tasks.discard)
        return r

    async def chip_depth(self, d, cfg, snap):
        """Снимок со стаканом связки под фишки сумм карточки (p2p.depth_for_deal), не дольше CHIP_DEPTH_TIMEOUT;
        сбой или таймаут — стакан скана как есть."""
        try:
            return await asyncio.wait_for(depth_for_deal(self.s, cfg, snap, d), chip_depth_timeout())
        except Exception as e:
            logger.warning("глубина под суммы: %s: %s", type(e).__name__, e)
            return snap

    async def chip_refresh(self, d, cfg, snap, amounts, card):
        """Фоном после отправки карточки: фишки 50/100/300 тыс. по запросам под каждую сумму (chip_depth). Изменилась
        хоть одна — строка фишек дописывается в подпись/текст отправленного сообщения (Bot.call editMessageCaption/
        editMessageText: картинку без новой загрузки не поменять) и остаётся при правках «живой карточки». Текст — тот,
        что на карточке к моменту правки (живая правка могла его сменить), «⌛ связка устарела» сохраняется; кнопки —
        те же (без reply_markup Telegram их снял бы; после «✅ Сделал» — уже без кнопок журнала). Не изменилось, сбой
        или таймаут — карточка как была. Исключения только в лог. card — message_id, photo, caption, chat_id, markup,
        deal_id, nav отправленной карточки."""
        try:
            fresh = deal_amounts(d, cfg, await self.chip_depth(d, cfg, snap))
            if not fresh or fresh == amounts:
                return
            line = chips_line(fresh)
            live = self.live_msg.get(self._deal_key(d))
            live = live if live and live["message_id"] == card["message_id"] else None
            base = live["caption"] if live else card["caption"]
            text = base + "\n" + line + (STALE_MARK if live and live["stale"] else "")
            if len(text) > (1024 if card["photo"] else 4096):
                return
            markup = card["markup"]
            if card["deal_id"] is not None and card["deal_id"] not in self.deals_by_id:   # «✅ Сделал» уже нажали
                markup = deal_markup(d, None, cfg, snap, False)
            kw = {"chat_id": card["chat_id"], "message_id": card["message_id"], "parse_mode": "HTML"}
            for mk in (self.markup(markup), plain_markup(markup)):
                if card["photo"]:
                    r = await self.call("editMessageCaption", caption=text, reply_markup=mk, **kw)
                else:
                    r = await self.call("editMessageText", text=text, reply_markup=mk, disable_web_page_preview=True,
                                        **kw)
                if not self._fancy_failed(r, mk):
                    break
            if not r.get("ok"):
                logger.warning("фишки сумм: правка карточки не прошла: %s", r.get("description"))
                return
            if live:   # живые правки и «⌛ устарел» строку фишек не теряют
                live["chips"], live["caption"] = line, base + "\n" + line
        except Exception as e:
            logger.warning("фишки сумм: %s: %s", type(e).__name__, e)

    async def show_steps(self, cq, deal_id):
        """Кнопка «📝 Инструкция»: отдельным сообщением пошаговый чек-лист маршрута."""
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
        try:   # план хеджа «что открыл бы бот» (без ордеров); сбой не мешает журналу
            hedge_plans.record(trade_id, d, cfg.amount, ref=getattr(snap, "ref", 0.0) or 0.0,
                               risk=cfg.risk_buffer.get(d[1].asset, 0.0))
        except Exception as e:
            logger.error("hedge_plans: %s: %s", type(e).__name__, e)
        try:   # реальный хедж (trading/hedge.py) фоновой задачей: карточка, только если торговля, ключ и пороги позволяют
            trading.hedge.offer_soon(self, "trade", trade_id, d[1].asset, hedge_plans.coin_qty(d, cfg.amount), cfg.amount, getattr(snap, "ref", 0.0) or 0.0, cfg.risk_buffer.get(d[1].asset, 0.0))  # noqa: E501 — одной строкой: вся строка в пине TRADING_LINES_APPROVED
        except Exception as e:
            logger.error("hedge offer: %s: %s", type(e).__name__, e)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Записано в журнал ✅")
        await self.send(f"Расчёт был {d[0]:+.2f}%. Какой вышел факт?", markup=fact_markup(trade_id), topic="journal")
        await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                        reply_markup=self.markup(deal_markup(d, cfg=cfg, snap=snap, nav=False)))
        if crossed:
            await self.send(f"⚠️ С {trades.BANK_NAMES.get(bank, bank)} по СБП в этом месяце отправлено "
                            f"{_money(total)} ₽ — выше бесплатного лимита {_limit_text(trades.free_limit(bank))}, "
                            f"дальше банк может взять комиссию до 0.5%. Оплату по СБП бот дальше считает со "
                            f"следующего своего банка (🏦 Мои банки в настройках).", topic="journal")

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
        await self.save_fact(cq, trade_id, row, fact, trades.FACT_PLAN if mode == "calc" else trades.FACT_PLAN_SHIFT)

    async def save_fact(self, cq, trade_id, row, fact, source=trades.FACT_MANUAL):
        trades.set_fact(trade_id, fact, source=source)
        if cq is not None:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Факт записан")
            await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            reply_markup={"inline_keyboard": []})
        note = ("\nЭто оценка, а не факт: в сравнение расчёт→факт не идёт. Найдутся ордера в истории биржи — заменю."
                if source in trades.PLAN_SOURCES else "")
        await self.send(f"✅ Факт: {fact:+.2f}% (расчёт был {row['profit']:+.2f}%){note}", topic="journal")

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
        await self.save_fact(None, trade_id, row, fact, trades.FACT_MANUAL)

    async def hide_deal(self, cq, deal_id):
        """Кнопка «🚫 Скрыть мерчанта»: занести обе стороны связки в блэклист (у стакана — всех его мерчантов),
        скан их больше не покажет."""
        entry = self.deals_by_id.pop(deal_id, None)
        if not entry:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сигнал устарел")
            return
        d, cfg, snap = entry
        _, b, s, _ = d
        # сторона из нескольких объявлений стакана («2 объявл.») — в список каждый настоящий мерчант из Ad.nicks
        added = [(a.ex, nick, blacklist.add(a.ex, nick)) for a in (b, s) for nick in dict.fromkeys(a.nicks or (a.nick,))]
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Скрыто, больше не покажу")
        hidden = ", ".join(f"{EXCHANGE_NAMES.get(ex, ex)}: {html.escape(nick)} (id {i})" for ex, nick, i in added)
        await self.send(f"🚫 В блэклисте: {hidden}.\n{BLACKLIST_NOTE_HELP}")
        await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                        reply_markup=self.markup(deal_markup(d, cfg=cfg, snap=snap, nav=False)))

    async def blacklist_note(self, arg):
        """«/blacklist note <id> <текст>» (только владелец): записать причину, почему мерчант в блэклисте."""
        m = re.fullmatch(r"note\s+(\d+)\s+(.+)", (arg or "").strip(), re.I | re.S)
        if not m:
            await self.send(BLACKLIST_NOTE_HELP)
        elif blacklist.set_note(int(m.group(1)), m.group(2)):
            await self.send(f"📝 Причина записана (id {m.group(1)}). Список — /blacklist.")
        else:
            await self.send(f"В блэклисте нет записи с id {m.group(1)}. Список с id — /blacklist.")

    async def add_alert(self, arg):
        """Команда «/alert USDT sell 92 7d [vol 50000] [reliable] [repeat 1h]»: разобрать и создать
        алерт на курс — одноразовый либо «повторно» с кулдауном, с необязательными условиями через
        «И» (объём стакана / надёжность встречной связки) в любом порядке хвоста."""
        if (arg or "").strip().lower().startswith("route"):
            await self.add_route_alert(arg.strip()[5:])
            return
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

    async def add_route_alert(self, arg):
        """«/alert route <площадка покупки> <площадка продажи> <монета> <порог>% <срок> [reliable] [repeat 1h]»."""
        m = re.match(r"\s*(\w+)\s+(\w+)\s+(\w+)\s+(-?[\d.,]+)%?\s+(\d+[hdw])(\s.*)?$", arg or "", re.I)
        if not m:
            await self.send(ALERT_HELP)
            return
        names = {**{k.lower(): v for k, v in VENUE_NAMES.items()}, **{v.lower(): v for v in VENUE_NAMES.values()}}
        buy_ex, sell_ex = names.get(m.group(1).lower()), names.get(m.group(2).lower())
        if not buy_ex or not sell_ex:
            await self.send(f"Площадки: {', '.join(VENUE_NAMES.values())}.")
            return
        off = [VENUE_NAMES.get(k, k) for k in VENUE_NAMES if VENUE_NAMES[k] in (buy_ex, sell_ex)
               and k not in self.cfg.exchanges]
        if off:   # площадка не сканируется — связки через неё не появятся, алерт молча не сработал бы
            await self.send(f"{', '.join(off)} сейчас не сканируется — включи в «⚙️ Настройки → 🎛 Фильтры».")
            return
        asset = m.group(3).upper()
        if asset not in self.cfg.assets:
            await self.send(f"Монета {asset} не отслеживается ботом ({', '.join(self.cfg.assets)}).")
            return
        try:
            pct = float(m.group(4).replace(",", "."))
        except ValueError:
            await self.send("Порог — число в %, например 3 или 2.5%.")
            return
        if not -10 <= pct <= MIN_PROFIT_MAX:
            await self.send(f"Порог — от −10 до {MIN_PROFIT_MAX:g}%.")
            return
        dur = alerts.parse_duration(m.group(5))
        if dur is None:
            await self.send("Срок — число + h/d/w (часы/дни/недели), не больше 90d.")
            return
        require_reliable, cooldown_s = False, None
        tokens = (m.group(6) or "").split()
        i = 0
        while i < len(tokens):
            tok = tokens[i].lower()
            if tok == "reliable":
                require_reliable, i = True, i + 1
            elif tok == "repeat" and i + 1 < len(tokens):
                cooldown_s, i = tokens[i + 1].lower(), i + 2
            else:
                await self.send(ALERT_HELP)
                return
        cooldown = alerts.parse_duration(cooldown_s) if cooldown_s else None
        if cooldown_s and cooldown is None:
            await self.send("Кулдаун repeat — число + h/d/w (часы/дни/недели), не больше 90d.")
            return
        alerts.add_route(self.chat_id, buy_ex, sell_ex, asset, pct, time.time() + dur, repeat_cooldown=cooldown,
                         require_reliable=require_reliable)
        notes = "".join([f", повтор не чаще раза в {cooldown_s}" if cooldown_s else "",
                         ", надёжность не хуже риска" if require_reliable else ""])
        await self.send(f"🔔 Алерт на связку создан: {buy_ex} → {sell_ex} {asset} ≥{pct:g}% чистыми, "
                        f"срок {m.group(5).lower()}{notes}. Список — /alerts.")

    async def check_alerts(self, snap, cfg=None):
        """Сработавшие алерты по текущему снимку → сообщение в тот чат, где алерт создан. Сработавшим
        (одноразовый — удалён) алерт помечается только после доставки; сбой — повторим в следующем скане."""
        for alert_id, chat_id, asset, side, rate, price, ad in alerts.due(snap, cfg or self.cfg):
            label, cmp = ("продать", "≥") if side == "sell" else ("купить", "≤")
            try:
                r = await self.send(f"🔔 <b>Алерт сработал:</b> {asset} можно {label} по {_price(price)} ₽ "
                                    f"({cmp}{rate:g}) — {fmt_ad(ad)}", chat_id=chat_id, topic="signals")
            except Exception as e:   # сеть/таймаут: остальные алерты этого скана тоже не дойдут
                logger.warning("alert send error: %s", accounts.api_error_text(e))   # без URL с токеном бота
                break
            if not r.get("ok"):
                logger.warning("alert not sent: %s", r.get("description"))
            if delivery_final(r):
                alerts.mark_fired(alert_id)
        for alert_id, chat_id, buy_ex, sell_ex, asset, pct, d in alerts.route_due(snap, cfg or self.cfg):
            profit, b, sl, route = d
            try:
                r = await self.send(f"🔔 <b>Алерт связки:</b> {html.escape(buy_ex)} → {html.escape(sell_ex)} {asset} "
                                    f"<b>{profit:+.2f}%</b> чистыми (порог {pct:g}%) на "
                                    f"{_money(self.snap_cfg(snap).amount)} ₽\nКупить: {fmt_ad(b)}\nПродать: {fmt_ad(sl)}",
                                    chat_id=chat_id, topic="signals")
            except Exception as e:
                logger.warning("alert send error: %s", accounts.api_error_text(e))
                break
            if not r.get("ok"):
                logger.warning("alert not sent: %s", r.get("description"))
            if delivery_final(r):
                alerts.mark_fired(alert_id)

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
                if s.get("plan_facts"):
                    line += f"; «как расчёт»/±0.5 у {s['plan_facts']} — не факт, в сравнение не идут"
                lines.append(line)
            else:
                lines.append(f"{label}: сделок нет")
        lines += direction_lines(trades.by_direction(trades.period_start("month")))
        banks = trades.month_banks()
        if banks:   # только информация: сколько разных мерчантов было по каждой своей карте
            lines += ["", f"Контрагенты по картам (ориентир ЦБ 16-МР: &gt;{trades.COUNTERPARTY_DAY} в день, "
                          f"&gt;{trades.COUNTERPARTY_MONTH} в месяц):"]
            for bank in banks:
                day, month = trades.counterparties(bank)
                warn = [" ⚠️" if n >= trades.COUNTERPARTY_WARN * lim else ""
                        for n, lim in ((day, trades.COUNTERPARTY_DAY), (month, trades.COUNTERPARTY_MONTH))]
                lines.append(f"• {trades.BANK_NAMES.get(bank, bank)}: сегодня {day}{warn[0]}, "
                             f"за месяц {month}{warn[1]}")
        lines.append("\nОтмечай связку кнопкой «✅ Сделал» под сигналом — так она попадёт в журнал, "
                     "затем укажи факт кнопкой или числом, чтобы сравнить расчёт с реальным результатом.")
        return "\n".join(lines)

    def signals_view(self, arg):
        """/signals (только владелец): отчёт sigreport за arg дней (по умолчанию 7, мусор — тоже 7; 1..30 — sigreport.build)."""
        try:
            days = int(arg) if arg else 7
        except ValueError:
            days = 7
        return sigreport.render(sigreport.build(days=days))

    def paper_view(self):
        """Текст «/paper»: настройки, открытые виртуальные круги, статистика за день/неделю/всё время
        (исполнилось/сорвалось и почему, средний факт vs план) и виртуальный баланс с изменением с начала."""
        s = paper.settings()
        lines = ["🧪 <b>Сухой прогон</b>", "",
                 f"Статус: {'🟢 включён' if s['on'] else '⚪ выключен'}, сумма круга {_money(s['amount'])} ₽", ""]
        open_ = paper.open_cycles()
        if not open_:
            lines.append("Открытых кругов нет.")
        else:
            now = time.time()
            for c in open_:
                mins = (now - c["ts_stage"]) / 60
                stage = PAPER_STAGE_LABELS.get(c["stage"], c["stage"])
                lines.append(f"🔄 {c['buy_ex']}→{c['sell_ex']} ({c['buy_asset']}→{c['sell_asset']}): "
                            f"стадия «{stage}» {mins:.0f} мин, план {c['planned_pct']:+.2f}%")
        lines.append("")
        st = paper.stats()
        for key, label in (("day", "За сегодня"), ("week", "За неделю"), ("all", "За всё время")):
            p = st[key]
            if not p["total"]:
                lines.append(f"{label}: кругов не было")
                continue
            line = f"{label}: {p['total']} кругов, исполнилось {p['done']}"
            if p["failed"]:
                reasons = ", ".join(f"{paper.FAIL_LABELS.get(r, r)} {n}" for r, n in p["failed_by_reason"].items())
                line += f", сорвалось {p['failed']} ({reasons})"
            if p["avg_diff"] is not None:
                line += f", факт vs план {p['avg_diff']:+.2f} п.п."
            lines.append(line)
        balance = paper.get_balance()
        if balance is not None:
            change = paper.balance_change()
            change_str = f"{change:+,.0f}".replace(",", " ")
            lines.append("")
            lines.append(f"Виртуальный баланс: {_money(balance)} ₽ (изменение с начала: {change_str} ₽)")
        banks = paper.banks_this_month()
        if banks:
            lines.append("")
            lines.append("Лимит СБП за месяц (виртуальный оборот):")
            for bank, total in sorted(banks.items(), key=lambda kv: -kv[1]):
                limit = trades.free_limit(bank)
                mark = "⚠️ " if total >= limit else ""
                lines.append(f"{mark}{trades.BANK_NAMES.get(bank, bank)}: {_money(total)} ₽ / {_limit_text(limit)}")
        lines.append("")
        lines.append("Кнопки ниже; то же командами: /paper on, /paper off, /paper amount 20000, /paper report. "
                     "/paper reset — начать статистику с нуля (старая база — в архив)")
        return "\n".join(lines)

    def paper_markup(self):
        """Кнопки под сводкой «/paper»: включить/выключить, сумма круга, отчёт. Ссылка «/paper» в тексте
        отправляет команду без аргумента, поэтому включать прогон нужно кнопкой, а не набором «/paper on»."""
        s = paper.settings()
        toggle = ({"text": "⏹ Выключить", "callback_data": "paper_set:off"} if s["on"]
                  else {"text": "▶️ Включить", "callback_data": "paper_set:on"})
        amounts = [{"text": ("✓ " if s["amount"] == a else "") + f"{_money(a)} ₽", "callback_data": f"paper_amt:{a}"}
                   for a in PAPER_AMOUNTS]
        return {"inline_keyboard": [[toggle], amounts,
                                    [{"text": "📊 Отчёт + CSV", "callback_data": "paper_report"},
                                     {"text": "🔄 Обновить", "callback_data": "paper"}]]}

    def paper_report_view(self, rows):
        """Текст «/paper report»: по каждой связке площадка/монета покупки → площадка/монета продажи —
        план/факт, срывы по причинам, нехватка глубины стакана, среднее время круга. План здесь — без запаса
        на курс (paper._PLAN_CMP): в факте запаса нет, иначе факт выглядел бы лучше плана на размер запаса."""
        if not rows:
            text = "🧪 Отчёт сухого прогона: завершённых кругов ещё нет."
            try:   # план хеджа реальных сделок (hedge_plans) от кругов прогона не зависит
                text += "\n".join(hedge_plans.report_lines())
            except Exception as e:
                logger.error("hedge_plans: %s: %s", type(e).__name__, e)
            return text
        lines = ["🧪 <b>Отчёт сухого прогона</b> — по площадкам и парам (план — без запаса на курс, как и факт):", ""]
        for r in rows:
            line = (f"{r['buy_ex']}→{r['sell_ex']} ({r['buy_asset']}→{r['sell_asset']}): "
                    f"{r['total']} кругов, исполнилось {r['done']}")
            if r["avg_planned_pct"] is not None:
                line += f", план {r['avg_planned_pct']:+.2f}%"
            if r["avg_realized_pct"] is not None:
                line += f" / факт {r['avg_realized_pct']:+.2f}%"
            if r["failed"]:
                reasons = ", ".join(f"{paper.FAIL_LABELS.get(k, k)} {v}"
                                    for k, v in r["failed_by_reason"].items())
                line += f", сорвалось {r['failed']} ({reasons})"
            if r["depth_shortfall"]:
                line += f", не хватило глубины {r['depth_shortfall']}×"
            if r["avg_duration_min"] is not None:
                line += f", среднее время круга {r['avg_duration_min']:.0f} мин"
            if r.get("avg_buy_slip_pct") is not None:
                line += f", покупка к плану {r['avg_buy_slip_pct']:+.2f}%"
            if r.get("buy_from_book"):
                line += f", у других мерчантов {r['buy_from_book']}×"
            if r.get("net_unknown"):
                line += f", статус сети неизвестен {r['net_unknown']}×"
            if r.get("fail_reasons"):
                line += " · причины срывов: " + ", ".join(
                    f"{paper.REASON_LABELS.get(k, k)} {v}" for k, v in r["fail_reasons"].items())
            lines.append(line)
        by_label = paper.label_stats()
        if by_label:
            lines += ["", "По метке надёжности на старте:"]
            for label, g in sorted(by_label.items()):
                line = f"{label}: {g['total']} кругов, исполнилось {g['done']}, план {g['avg_planned_pct']:+.2f}%"
                if g["avg_realized_pct"] is not None:
                    line += f" / факт {g['avg_realized_pct']:+.2f}%"
                lines.append(line)
        try:   # сбой отчёта бумажного хеджа не ломает /paper report
            lines += simperp.report_lines()
        except Exception as e:
            logger.error("simperp: %s: %s", type(e).__name__, e)
        try:
            lines += hedge_plans.report_lines()
        except Exception as e:
            logger.error("hedge_plans: %s: %s", type(e).__name__, e)
        lines += ["", *self.paper_vs_real_lines(rows)]
        lines.append("")
        lines.append("📄 разбор по связкам — файлом CSV ниже.")
        return "\n".join(lines)

    def paper_vs_real_lines(self, rows):
        """«Сухой прогон vs реальные сделки»: по тем же связкам, что в отчёте (report_rows), и за то же окно — с
        первого круга до сейчас — средний факт исполнившихся кругов против среднего факта сделок журнала (где факт
        введён). Реальных сделок с фактом нет — одна строка."""
        since = paper.first_start() or 0.0
        real = trades.facts_by_pair(since)
        title = f"<b>Сухой прогон vs реальные сделки</b> (с {datetime.fromtimestamp(since, MSK):%d.%m.%Y})"
        if not real:
            return [f"{title}: реальных сделок с фактом за это время нет."]
        lines = [title + ":"]
        for r in rows:
            g = real.get((r["buy_ex"], r["buy_asset"], r["sell_ex"], r["sell_asset"]))
            if not g:
                continue
            line = f"{r['buy_ex']}→{r['sell_ex']} ({r['buy_asset']}→{r['sell_asset']}): "
            if r["avg_realized_pct"] is None:
                line += f"прогон — не исполнилось ни одного из {r['total']} кругов"
            else:
                line += f"прогон {r['avg_realized_pct']:+.2f}% ({r['done']} кругов)"
            line += f" / реальные {g['avg_fact']:+.2f}% ({g['count']} сделок)"
            if r["avg_realized_pct"] is not None:
                line += f", разница {g['avg_fact'] - r['avg_realized_pct']:+.2f} п.п."
            lines.append(line)
        if len(lines) == 1:
            n = sum(g["count"] for g in real.values())
            return [f"{title}: реальные сделки с фактом ({n}) были по другим связкам."]
        return lines

    async def cmd_paper(self, arg):
        """/paper — сводка сухого прогона; /paper on|off — включить/выключить; /paper amount 20000 —
        сумма виртуального круга (баланс не сбрасывает, действует для новых кругов); /paper report —
        отчёт по площадкам и парам + CSV-файл (data/paper_report.csv); /paper reset — спросить кнопками и
        обнулить (paper_reset:yes → paper.reset, база в архив)."""
        sub, _, rest = arg.strip().partition(" ")
        sub = sub.lower()
        if sub == "on":
            save_env("PAPER", "1")
            await self.send("🧪 Сухой прогон включён.")
        elif sub == "off":
            save_env("PAPER", "0")
            await self.send("🧪 Сухой прогон выключен.")
        elif sub == "amount":
            amount = parse_amount(rest)
            if amount is None:
                await self.send(f"Не понял сумму. Пример: /paper amount 20000 "
                                f"(от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
                return
            save_env("PAPER_AMOUNT", f"{amount:.0f}")
            await self.send(f"🧪 Сумма круга сухого прогона: {_money(amount)} ₽.")
        elif sub == "report":
            rows = paper.report_rows()
            await self.send(self.paper_report_view(rows))
            if rows:
                path = paper.write_report_csv(rows)
                await self.send_document(path, "Отчёт сухого прогона (CSV)")
        elif sub == "reset":
            r = await self.send("🧪 Обнулить сухой прогон? Все круги (и открытые) уйдут в архив "
                            "data/paper-archive-…db — он не удаляется; статистика, баланс и лестница начнутся с нуля. "
                            "Вкл/выкл и сумма круга не меняются.", markup=PAPER_RESET_MARKUP)
            # «Да» принимается только с этого сообщения и один раз: двойное нажатие до того, как кнопки пропали,
            # иначе затирало итог обнуления текстом «обнулять нечего»
            self.paper_reset_ask = (r.get("result") or {}).get("message_id")
        else:
            await self.send(self.paper_view(), markup=self.paper_markup())

    def paper_reset(self):
        """Кнопка «🗑 Да, обнулить»: paper.reset и текст ответа — что ушло в архив."""
        try:
            res = paper.reset()
        except OSError as e:   # файл базы занят/нет прав — база на месте, говорим как есть
            return f"⚠️ Не получилось обнулить сухой прогон: {html.escape(str(e))}"
        if res is None:
            return "🧪 Обнулять нечего — кругов сухого прогона ещё не было."
        change = f"{res['change']:+,.0f}".replace(",", " ")
        return (f"🗑 Сухой прогон обнулён. В архиве data/{html.escape(os.path.basename(res['archive']))}: "
                f"кругов {res['cycles']}, итог завершённых {change} ₽. Статистика, баланс и лестница — с нуля; "
                f"вкл/выкл и сумма круга прежние.")

    async def cmd_export(self, arg):
        """/export [month|year] — журнал сделок за календарный месяц (по умолчанию) или год до сегодня по МСК:
        CSV-файл data/trades_export.csv (документы для банка по 115-ФЗ, данные для 3-НДФЛ) и короткая сводка."""
        key = "".join((arg or "").lower().split()) or "month"
        period = key if key.isdigit() and 2020 <= int(key) <= 2100 else EXPORT_PERIODS.get(key)
        if period is None:
            await self.send(EXPORT_HELP)
            return
        now = time.time()
        since, until = trades.period_range(period, now)
        last = (until - 1) if until is not None else now
        span = f"с {datetime.fromtimestamp(since, MSK):%d.%m.%Y} по {datetime.fromtimestamp(last, MSK):%d.%m.%Y}"
        rows = trades.export_rows(since, until=until)
        if not rows:
            await self.send(f"📤 Выгрузка {span}: сделок в журнале нет.")
            return
        await self.send_document(trades.write_export_csv(rows), f"Журнал сделок {span} (МСК), CSV")
        s = trades.export_summary(rows)
        result = f"{s['result']:+,.0f}".replace(",", " ")
        estimated = (f" Из них {s['estimated']} — с оценкой «как расчёт» (не факт): в CSV она в колонке "
                     f"«Оценка (не факт), %», а не в «Факт, %»." if s["estimated"] else "")
        lines = [f"📤 <b>Выгрузка журнала</b> {span} (МСК)",
                 f"Сделок: {s['count']}, оборот {_money(s['amount'])} ₽",
                 f"Результат по факту: {result} ₽",
                 (f"Без факта: {s['no_fact']} из {s['count']} — их результат в сумму не вошёл.{estimated}"
                  if s["no_fact"] else "Факт указан у всех сделок."),
                 "", EXPORT_NOTE]
        await self.send("\n".join(lines))

    async def show_best(self, snap=None, cfg=None):
        snap = self.last if snap is None else snap
        cfg = cfg or self.snap_cfg(snap)
        if not snap:
            await self.send(WAIT)
        elif not snap.deals:
            await self.send("Связок сейчас нет: все объявления отсеяны фильтрами.")
        else:
            await self.send_deal(snap.deals[0], "🔥 " + self.held_label(snap.deals[0]), cfg, snap)

    async def show_top(self, snap=None, cfg=None):
        snap = self.last if snap is None else snap
        cfg = cfg or self.snap_cfg(snap)
        if not snap:
            await self.send(WAIT)
            return
        nets = sorted(((v["sell"].price, net) for net, v in snap.networks.items() if v.get("sell")), reverse=True)[:3]
        traps = sum(1 for d in snap.deals if reliability(d, cfg, snap)[0] == TRAP)
        caption = (f"📊 <b>Топ связок</b> · USDT {snap.ref:.2f} ₽ · круг {_money(cfg.amount)} ₽\n"
                   + (f"Лучше продать USDT обменнику: {', '.join(f'{n} {p:.2f}' for p, n in nets)}\n" if nets else "")
                   + f"Связок всего: {len(snap.deals)} · от {cfg.min_profit:g}%: "
                   + f"{sum(1 for d in snap.deals if d[0] >= cfg.min_profit)}"
                   + (f"\n🪤 Ловушек: {traps} — помечены, сигналом не приходят" if traps and not signal_traps() else ""))
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
        snap = await self.fresh_scan(calc_cfg, force_alt=True)
        await self.show_top(snap, calc_cfg)
        await self.show_best(snap, calc_cfg)
        if snap.deals:
            await self.send(fmt_breakeven(snap.deals[0], calc_cfg, snap))

    async def maker(self, arg):
        """/maker <монета>: режим мейкера на всех подключённых площадках по текущему снимку стакана."""
        asset = (arg or "").strip().upper()
        if not asset:
            await self.send(MAKER_HELP)
            return

        if asset == "PAPER":   # /maker paper — отчёт бумажного мейкера (simmaker), только владельцу
            if REPLY_CHAT.get() is not None:
                await self.send(GUEST_DENIED)
                return
            await self.send(simmaker.report_view(self.cfg))
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
            try:
                text = bank_history_view(stats=await asyncio.to_thread(history.bank_spread_stats, BANK_HISTORY_DAYS))
            except Exception as e:
                logger.warning("bank history: %s", e)
                text = BANKS_HELP
            await self.send(text)
            return
        if asset not in self.cfg.assets:
            await self.send(f"Монета {asset} не отслеживается ботом ({', '.join(self.cfg.assets)}).")
            return
        if not self.last:
            await self.send(WAIT)
            return
        await self.send(banks_view(self.last, self.cfg, asset))

    async def balance(self):
        """`/balance`: балансы по подключённым биржам (Bybit — Unified + Funding, MEXC — спот, BingX — спот + Fund,
        Cryptomus — кошельки кабинета) и итог в ₽.

        Картинка-карточка портфеля; не вышло отрисовать — тот же текст, как у остальных карточек."""
        port = await accounts.portfolio(self.s)
        caption = portfolio_view(port, self.last)
        if not port or not self.last:   # до первого скана карточку с «≈ 0 ₽» не рисуем — только текст
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
              [{"text": "🔑 Мои биржи", "callback_data": "accounts"},
               {"text": "🏦 Мои банки", "callback_data": "mybanks"}],
              [{"text": "🌙 Тихие часы: выкл" if self.quiet_on else "🌙 Тихие часы: вкл",
                "callback_data": "quiet_off" if self.quiet_on else "quiet_on"},
               {"text": "▶️ Возобновить" if pause_active else "⏸ Пауза",
                "callback_data": "resume" if pause_active else "pause"}]]
        return text, {"inline_keyboard": kb}

    def mute_line(self, now=None):
        """Одна строка для /status: сигналы сейчас не отправляются (пауза или тихие часы) — и до какого времени
        (МСК) или как это снять. Та же логика, что status в settings_view. None — сигналы идут как обычно."""
        now = time.time() if now is None else now
        if self.paused:
            return "⏸ Сигналы на паузе (бессрочно) — /resume"
        if self.pause_until and now < self.pause_until:
            return f"⏸ Сигналы на паузе до {_hhmm_msk(self.pause_until)} МСК — /resume"
        if self.is_quiet_now():
            end = quiet_hours_end_ts(self.quiet_hours)
            if end is None:
                return "🌙 Тихие часы — сигналы копятся для дайджеста"
            return f"🌙 Тихие часы до {_hhmm_msk(end)} МСК — сигналы копятся для дайджеста"
        return None

    async def set_custom_amount(self, text):
        """Ввод суммы текстом после «✏️ Своя сумма»: сохранить AMOUNT и сразу пересканировать (как /calc)."""
        amount = parse_amount(text)
        if amount is None:
            await self.send(f"Не понял сумму. Пример: 20000, 1,5 млн (от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
            return
        self.cfg.amount = amount
        save_env("AMOUNT", f"{amount:.0f}")
        snap = await self.fresh_scan(force_alt=True)
        self.last = snap
        await self.show_top(snap)
        await self.show_best(snap)

    def _toggle(self, values, item, env_key, label, allowed):
        """Вкл/выкл монету или площадку в списке фильтра; нельзя выключить последнюю. Пишет в .env.
        Только значения с кнопок (allowed): данные колбэка может подделать клиент."""
        if item not in allowed:
            return "Нет такой кнопки"
        if item in values:
            if len(values) == 1:
                return f"Нельзя выключить последнюю {label}"
            values.remove(item)
            save_env(env_key, ",".join(values))
            return f"Выключено: {item}"
        values.append(item)
        save_env(env_key, ",".join(values))
        return f"Включено: {item}"

    def apply_preset(self, pid):
        """Применить пресет фильтров (встроенный или сохранённый «💾 Сохранить как пресет») — сразу все поля.
        pid — id из кнопки; кнопки из старых сообщений несут имя — его тоже понимаем."""
        name = presets.name_by_id(pid, self.cfg) or pid
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
        await self.send(f"💾 Пресет «{html.escape(name)}» сохранён.")
        text, kb = presets_view(self.cfg)
        await self.send(text, markup=kb)

    async def handle_key_input(self, text, message_id):
        """Ввод API key/secret[/passphrase] после «➕ Подключить»: сообщения с ключом удаляются из чата сразу же.

        Для бирж из accounts.PASSPHRASE_REQUIRED (KuCoin) — третий шаг: passphrase."""
        state = self.awaiting_key
        # «удалено» пишем, только если Telegram подтвердил; отказ/сбой — просим удалить вручную, ввод не прерываем
        deleted = None if message_id is None else await self.delete_message(message_id)
        if deleted is False:
            await self.send("⚠️ Не смог удалить сообщение с ключом из чата — удали его вручную.")
        done = ", сообщение удалено" if deleted else ""
        name = ACCOUNT_NAMES.get(state["ex"], state["ex"])
        got, _, second = KEY_STEPS.get(state["ex"], KEY_STEPS_DEFAULT)   # у Cryptomus: ID, затем API key
        text = text.strip()
        if state["step"] == "key":
            state["key"] = text
            state["step"] = "secret"
            await self.send(f"{got} получен{done}. Теперь пришли <b>{second}</b> для {name}.")
            return
        if state["step"] == "secret":
            state["secret"] = text
            if state["ex"] in accounts.PASSPHRASE_REQUIRED:
                state["step"] = "passphrase"
                await self.send(f"Secret получен{done}. Теперь пришли <b>passphrase</b> для {name}.")
                return
        else:   # step == "passphrase"
            state["passphrase"] = text
        self.awaiting_key = None
        ex = state["ex"]
        accounts.save_key(ex, state["key"], state["secret"], state.get("passphrase"))
        # сначала права: ключ с торговлей/выводом удаляем сразу, «только чтение» — лишь когда биржа это подтвердила
        safe, detail = await accounts.key_permissions(self.s, ex)
        if safe is False and not allow_unsafe_keys():
            await self.drop_unsafe_key(ex, detail)
        else:
            ok, msg = await accounts.verify(self.s, ex)
            accounts.set_verified(ex, verify_state(ok, safe), detail if ok and safe is False else msg)
            await self.send(f"✅ Подключено{readonly_note(safe, detail)}" if ok
                            else f"⚠️ Ключ сохранён, но проверка не прошла: {html.escape(msg)}")
        t, kb = account_view(ex)
        await self.send(t, markup=kb)

    def apply(self, data):
        # порог и сумма — теми же парсерами, что и команды: 0/nan/inf/минус не попадут ни в cfg, ни в .env
        if data.startswith("min:"):
            v = parse_min_profit(data[4:])
            if v is None:
                return "Некорректное значение"
            self.cfg.min_profit = v
            save_env("MIN_PROFIT", f"{v:g}")
            return f"Порог {v:g}%"
        if data.startswith("amt:"):
            v = parse_amount(data[4:])
            if v is None:
                return "Некорректное значение"
            self.cfg.amount = v
            save_env("AMOUNT", f"{v:.0f}")
            return f"Сумма {_money(v)} ₽ — применится со следующего скана"
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
            return self._toggle(self.cfg.assets, data[6:], "ASSETS", "монету", ASSET_LIST)
        if data.startswith("flt_e:"):
            return self._toggle(self.cfg.exchanges, data[6:], "EXCHANGES", "площадку", EXCHANGE_LIST)
        if data.startswith("preset_apply:"):
            return self.apply_preset(data[len("preset_apply:"):])
        return ""

    async def ev_rank(self, snap, cfg=None):
        """EV_RANK=1 (Config.ev_rank): связки снимка по убыванию ожидаемой прибыли (calibration.rank_snapshot) — от
        этого порядка /top, /best, топ-N сигналов и «EV … (p=…)» в карточке. Калибровку по базам прогона и журнала
        собираем не чаще calibration.REFRESH; чтение баз и ранжирование — в отдельном потоке. EV_RANK=0 — снимок как
        есть, базы не читаем. Сбой — снимок как есть и строка в логе: сигналы не ломаются."""
        cfg = cfg or self.cfg
        if snap is None or not cfg.ev_rank:
            return snap
        try:
            now = time.time()
            if self.cal is None or now - self.cal_ts >= calibration.REFRESH:
                self.cal = await asyncio.to_thread(calibration.build)
                self.cal_ts = now
            await asyncio.to_thread(calibration.rank_snapshot, self.cal, snap, cfg)
        except Exception as e:
            logger.warning("ev rank: %s", e)
        return snap

    async def fresh_scan(self, cfg=None, force_alt=False):
        """Скан (p2p.scan) и, при EV_RANK=1, порядок по EV (ev_rank) — до того, как снимок увидят команды и сигналы.
        Скан идёт по копии настроек, она же остаётся в snap.cfg: сумма, сменённая во время скана или после него, не
        смешается с % этого снимка (snap_cfg)."""
        cfg = copy.deepcopy(cfg or self.cfg)
        snap = await (scan(self.s, cfg, force_alt=True) if force_alt else scan(self.s, cfg))
        snap.cfg = cfg
        return await self.ev_rank(snap, cfg)

    def schedule_reputation(self):
        """Раз в reputation.REFRESH — пересчёт меток мерчантов (paper.db + snapshots.db) фоном в отдельном потоке:
        скан его не ждёт, прошлый пересчёт ещё идёт — новый не запускаем."""
        if not reputation.due() or (self.rep_task is not None and not self.rep_task.done()):
            return
        self.rep_task = asyncio.ensure_future(self.refresh_reputation())

    async def refresh_reputation(self):
        try:
            n = await asyncio.to_thread(reputation.refresh)
            logger.info("репутация мерчантов: меток %d", n)
        except Exception as e:
            logger.warning("reputation: %s", e)

    async def scan_step(self, name, fn, *args):
        """Один шаг scan_loop изолированно: исключение шага не глушит остальные (только Exception — CancelledError и
        KeyboardInterrupt проходят наружу). Подряд идущие сбои считает step_fail; на SCAN_STEP_ALERT_AFTER подряд —
        один алерт владельцу в topic dev (сетевые ошибки не алертим, sqlite/диск и прочее — алертим); успех после
        алерта шлёт «снова работает» и чистит запись."""
        try:
            res = fn(*args)
            if inspect.isawaitable(res):
                await res
        except Exception as e:
            logger.error("scan step %s: %s", name, e)
            rec = self.step_fail.setdefault(name, {"n": 0, "last": "", "alerted": False, "net": False, "try_ts": 0.0})
            rec["n"] += 1
            rec["last"] = f"{type(e).__name__}: {e}"[:200]
            rec["net"] = isinstance(e, SCAN_STEP_NET_ERRORS)
            if (rec["n"] >= SCAN_STEP_ALERT_AFTER and not rec["alerted"] and not rec["net"] and self.chat_id
                    and time.time() - rec["try_ts"] >= SCAN_STEP_ALERT_RETRY_SEC):
                rec["try_ts"] = time.time()
                try:
                    r = await self.send(f"⚠️ Шаг скана «{name}» падает {rec['n']} раз подряд: "
                                         f"{html.escape(rec['last'])}", topic="dev")
                    if (r or {}).get("ok"):
                        rec["alerted"] = True
                except Exception as send_err:
                    logger.error("scan step %s alert: %s", name, send_err)
        else:
            rec = self.step_fail.pop(name, None)
            if rec and rec["alerted"] and self.chat_id:
                try:
                    await self.send(f"✅ Шаг скана «{name}» снова работает", topic="dev")
                except Exception as send_err:
                    logger.error("scan step %s recovery: %s", name, send_err)

    def _history_step(self, snap):
        if history.record(snap, self.snap_cfg(snap).amount):   # не чаще раза в 5 минут, независимо от чата
            history.cleanup()

    async def scan_loop(self):
        while True:
            snap = None
            try:
                t0 = time.time()
                snap = self.last = await self.fresh_scan()
                self.last_scan_ts, self.last_scan_duration = time.time(), time.time() - t0
                self.scan_errors = 0
                self.speed.add(self.last, self.last_scan_duration)
                await self.scan_step("track_liveness", self.track_liveness, self.last)
                await self.scan_step("history", self._history_step, snap)
                await self.scan_step("reputation", self.schedule_reputation)

                if simmaker.enabled():   # бумажный мейкер (SIM_MAKER=1): только расчёт по снимку, объявлений нет
                    try:
                        simmaker.on_scan(self.last, self.cfg)
                    except Exception as e:
                        logger.error("simmaker: %s", e)

                if self.chat_id:
                    await self.scan_step("venues", self.check_venues, self.last)
                    await self.scan_step("alerts", self.check_alerts, self.last)
                    await self.scan_step("networks", self.check_networks)
                    await self.scan_step("paper_cycles", self.process_paper_cycles, self.last)
                    await self.scan_step("paper_hedge", self.paper_hedge_tick, self.last)
                    await self.scan_step("paper_ladder", self.check_paper_ladder)
                    await self.scan_step("quiet_pause", self.quiet_and_pause_tick, self.last)
                    await self.scan_step("market_status", self.update_market_status, self.last)
            except Exception as e:
                logger.error("scan error: %s", e)
                if snap is None:   # сам скан не прошёл (а не шаг после него) — для watchdog
                    self.scan_error, self.scan_errors = f"{type(e).__name__}: {e}"[:200], self.scan_errors + 1
            if snap is not None:   # после сигналов: снимок для разбора не задерживает их
                await self.save_snapshot(snap)
                self.schedule_backup()   # суточная копия баз — фоном, скан не ждёт
            await asyncio.sleep(self.cfg.interval)

    def scan_stall_limit(self):
        """Сколько секунд без успешного скана — уже «скан стоит»: SCAN_STALL_MINUTES (по умолчанию 5 мин), но не
        меньше трёх интервалов скана (INTERVAL) — иначе редкий опрос давал бы ложные алерты."""
        try:
            minutes = float(os.getenv("SCAN_STALL_MINUTES", SCAN_STALL_MINUTES_DEFAULT))
        except ValueError:
            minutes = SCAN_STALL_MINUTES_DEFAULT
        if not minutes > 0:
            minutes = SCAN_STALL_MINUTES_DEFAULT
        return max(minutes * 60, 3 * self.cfg.interval)

    def watchdog_awake(self, now):
        """Запоминает время тика watchdog. False — с прошлого тика прошло больше 3×WATCHDOG_TICK: ПК спал (или цикл
        событий стоял) и скан не мог идти — тик пропускаем, простой скана дальше считаем от пробуждения, а не от
        последнего скана до сна (иначе сразу после сна — ложное «⚠️ Скан стоит… 480 мин», а за ним «✅ снова идёт»)."""
        prev, self.watchdog_prev_tick = self.watchdog_prev_tick, now
        if prev and now - prev > 3 * WATCHDOG_TICK:
            self.watchdog_wake_ts = now
            return False
        return True

    def watchdog_message(self, now=None):
        """Проверка watchdog: текст алерта или сообщения о восстановлении, None — сообщать нечего. Отдельная площадка
        (check_venues) тут ни при чём: смотрим, идёт ли скан целиком — он мог зависнуть или падать каждый раз
        (исключение вне адаптеров), и тогда бот молчит, пока владелец не заметит отсутствие сигналов. Состояние
        алерта (stall_alerted) не меняет — это делает watchdog_check, когда Telegram принял сообщение."""
        now = time.time() if now is None else now
        if not self.watchdog_awake(now):
            return None
        scanned = self.last_scan_ts and self.last_scan_ts >= self.watchdog_wake_ts   # был скан после пробуждения
        since = self.last_scan_ts if scanned else max(self.start_ts, self.watchdog_wake_ts)
        idle = now - since
        if idle > self.scan_stall_limit():
            if self.stall_alerted:
                return None
            ago = f"{int(idle // 60)} мин"
            if scanned:
                what = f"последний успешный скан {ago} назад"
            elif self.watchdog_wake_ts > self.start_ts:
                what = f"после пробуждения ПК ({ago}) ни одного успешного скана"
            else:
                what = f"с запуска ({ago}) ни одного успешного скана"
            text = f"⚠️ Скан стоит: {what} — сигналы не приходят."
            if self.scan_errors:
                text += f"\nОшибок скана подряд: {self.scan_errors}, последняя: {html.escape(self.scan_error)}"
            else:
                text += "\nОшибок нет — похоже, скан завис. Проверьте /logs, при необходимости перезапустите бота."
            return text
        if self.stall_alerted and scanned:
            return "✅ Скан снова идёт."
        return None

    async def watchdog_check(self, now=None):
        """Один тик watchdog: сообщение — в топик «Разработка»; stall_alerted меняется, только если Telegram его принял
        (r["ok"]), — не дошло, повторим на следующем тике, а не замолчим до восстановления."""
        text = self.watchdog_message(now)
        if not text:
            return
        alert = not self.stall_alerted   # без алерта сообщение — только алерт, после него — только восстановление
        r = await self.send(text, topic="dev")
        if (r or {}).get("ok"):
            self.stall_alerted = alert

    async def watchdog_loop(self):
        """Раз в WATCHDOG_TICK: скан целиком стоит дольше SCAN_STALL_MINUTES — один алерт владельцу (топик «Разработка»),
        пошёл снова — сообщение о восстановлении. Своя задача, а не шаг scan_loop: зависший скан её не остановит."""
        while True:
            await asyncio.sleep(WATCHDOG_TICK)
            if not self.chat_id:
                self.watchdog_awake(time.time())   # тики идут и без чата — сон ПК в это время тоже заметим
                continue   # владельца ещё нет — не «тратим» алерт впустую, проверим, когда он появится
            try:
                await self.watchdog_check()
            except Exception as e:
                logger.error("watchdog: %s", e)

    def schedule_backup(self):
        """Раз в сутки — копия баз (backup.py) фоном в отдельном потоке; скан её не ждёт, вторая параллельно не идёт."""
        if self.backup_task is not None and not self.backup_task.done():
            return
        try:
            if not backup.due():
                return
        except (OSError, ValueError) as e:   # не читается data/backup или чужая папка — скан не падает
            logger.warning("backup: %s", e)
            return
        self.backup_task = asyncio.ensure_future(self.run_backup())

    async def run_backup(self):
        try:
            dest, files = await asyncio.to_thread(backup.run)
            if dest:
                logger.info("резервная копия баз: %s (%d файлов)", dest, len(files))
        except Exception as e:
            logger.warning("backup: %s", e)

    async def save_snapshot(self, snap):
        """Снимок скана в data/snapshots.db (snapshots.py): каждый SNAPSHOT_EVERY-й скан (первый после запуска —
        всегда) и скан, на котором стартовал круг сухого прогона (snapshot_id круга должен найтись). Данные собираем
        здесь, пишем в отдельном потоке — цикл событий не ждёт диск. Ошибка записи скан не ломает — строка в логе.
        Возвращает id снимка или None (скан не пишется или ошибка)."""
        keep = snapshots.scan_id(snap) in self.snapshot_keep
        self.snapshot_keep.clear()   # круг стартует только в текущем скане — старые отметки не нужны
        due = self.snapshot_scans % snapshots.every() == 0
        self.snapshot_scans += 1
        if not (due or keep):
            return None
        try:
            data = snapshots.collect(snap, self.snap_cfg(snap), self.live)
            return await asyncio.to_thread(snapshots.write, data)
        except Exception as e:
            logger.warning("snapshot: %s", e)
            return None

    async def perp_loop(self):
        """Публичные данные перпов (perp.py) со своим интервалом PERP_INTERVAL — отдельно от скана P2P, чтобы
        медленная площадка фьючерсов не задерживала сигналы. Только чтение, без ключей и ордеров."""
        while True:
            try:
                if await perp.refresh_if_due(self.s) is not None:
                    self.sim_tick()
            except Exception as e:
                logger.error("perp_loop error: %s", e)
            await asyncio.sleep(PERP_LOOP_TICK)

    def sim_tick(self):
        """Бумажные симуляции на свежих котировках перпов; сбой одной не мешает другой и опросу."""
        for name, run in (("simfunding", simfunding.tick), ("simdirectional", simdirectional.tick)):
            try:
                run()
            except Exception as e:
                logger.error("%s: %s", name, e)

    async def open_help(self, cq, section):
        """Кнопка раздела /help: правим то же сообщение; не вышло — шлём новым."""
        text, kb = help_view(section or None)
        msg = cq.get("message") or {}
        r = await self.call("editMessageText", chat_id=self.chat_id, message_id=msg.get("message_id"), text=text,
                            parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        if not r.get("ok") and "message is not modified" not in r.get("description", ""):
            await self.send(text, markup=kb)

    def status_brief(self, status_path=DEV_STATUS):
        """(текст, кнопки) «/status» коротко: версия и аптайм, последний скан (жив ли, длительность, p50/p90),
        ошибки площадок одной строкой, связок выше порога. «📋 Подробно» — полный status_view."""
        st = _dev_status(status_path)
        lines = [f"📟 <b>Статус</b> · <code>{html.escape(st.get('version', '?'))}</code> · аптайм "
                 f"{_uptime_str(time.time() - self.start_ts)}"]
        if self.last_scan_ts:
            age = time.time() - self.last_scan_ts
            fresh = age <= max(3 * self.cfg.interval, 60)
            when = datetime.fromtimestamp(self.last_scan_ts).strftime("%H:%M:%S")
            line = f"{'🟢' if fresh else '🔴'} Скан {when} ({self.last_scan_duration:.1f} с)"
            speed = self.speed.summary()
            if speed:
                p50, p90 = speed["scan"]
                line += f" · p50 {p50:.1f} / p90 {p90:.1f} с"
            if not fresh:
                line += f" — {int(age // 60)} мин назад"
            lines.append(line)
        else:
            lines.append("⏳ Скана ещё не было")
        mute = self.mute_line()
        if mute:
            lines.append(mute)
        snap = self.last
        if snap is not None:
            if snap.errors:
                names = ", ".join(html.escape(k) for k in list(snap.errors)[:4])
                more = f" +{len(snap.errors) - 4}" if len(snap.errors) > 4 else ""
                lines.append(f"⚠️ Ошибки площадок ({len(snap.errors)}): {names}{more}")
            else:
                lines.append("✅ Ошибок нет — все площадки отвечают")
            above = sum(1 for d in snap.deals if d[0] >= self.cfg.min_profit)
            line = f"🔔 Связок выше порога {self.cfg.min_profit:g}%: {above}"
            if mute:
                line += " (не отправляются)"
            lines.append(line)
        kb = {"inline_keyboard": [[{"text": "📋 Подробно", "callback_data": "status_full"},
                                   {"text": "🔄 Обновить", "callback_data": "status"}]]}
        return "\n".join(lines), kb

    def status_view(self, status_path=DEV_STATUS):
        """Текст «/status»: версия, аптайм, время/длительность последнего скана, ошибки площадок (и таймауты: площадка
        не успела за VENUE_TIMEOUT — её данные в этом скане устарели), сколько связок сейчас выше порога сигнала;
        скорость — p50/p90 длительности скана и задержки площадок за последние сканы (ScanSpeed)."""
        st = _dev_status(status_path)
        lines = ["📟 <b>Статус бота</b>", "",
                 f"Версия: <code>{html.escape(st.get('version', '?'))}</code>",
                 f"Аптайм: {_uptime_str(time.time() - self.start_ts)}"]
        if self.last_scan_ts:
            when = datetime.fromtimestamp(self.last_scan_ts).strftime("%d.%m %H:%M:%S")
            lines.append(f"Последний скан: {when} ({self.last_scan_duration:.1f} с)")
        else:
            lines.append("Последний скан: ещё не было")
        mute = self.mute_line()
        if mute:
            lines.append(mute)
        if self.step_fail:
            lines += ["", "⚠️ Сбои шагов скана:"]
            lines += [f"• {html.escape(name)}: {rec['n']} подряд, последняя ошибка {html.escape(rec['last'])}"
                      for name, rec in self.step_fail.items()]
        snap = self.last
        if snap is None:
            lines.append("Скан ещё не выполнялся.")
            return "\n".join(lines)
        above = sum(1 for d in snap.deals if d[0] >= self.cfg.min_profit)
        line = f"Связок выше порога {self.cfg.min_profit:g}%: {above}"
        if mute:
            line += " (не отправляются)"
        lines.append(line)
        terms = terms_summary()
        if terms:
            top = ", ".join(f"{label} {n}" for label, n in sorted(terms.items(), key=lambda kv: -kv[1]))
            merchants = len({(r["ex"], r["nick"]) for r in terms_log()})
            lines.append(f"Отсеяно стоп-фразами в условиях: {merchants} мерчантов с запуска ({top}) — /traps")
        extra, lim = snap.extra, depth_settings()
        lines.append(f"Доп. запросы глубины в скане: вторые страницы {extra.get('page2', 0)} (лимит "
                     f"{lim['page2_max']}), под суммы {extra.get('amounts', 0)} (лимит {lim['chips_max']})")
        if snap.errors:
            lines += ["", "<b>Ошибки площадок:</b>"]
            lines += [f"• {html.escape(k)}: {html.escape(e)}" for k, e in snap.errors.items()]
        else:
            lines.append("Ошибок нет — все площадки отвечают.")
        speed = self.speed.summary()
        if speed:
            lines += [""] + speed_lines(speed, self.cfg.venue_timeout)
        try:
            lines += outage_lines(history.outage_stats(OUTAGE_DAYS, min_seconds=OUTAGE_MIN_SECONDS))
        except Exception as e:
            logger.warning("outage stats: %s", e)
        return "\n".join(lines)

    async def update_market_status(self, snap):
        """Закреплённое сообщение «📌 Статус рынка»: ориентир курса, лучшая связка, площадки ок/недоступны.
        Первый раз шлёт сообщение и закрепляет его, дальше правит на месте (editMessageText) не чаще раза
        в MARKET_STATUS_INTERVAL секунд, чтобы не спамить и не упереться в лимиты Telegram."""
        if not self.chat_id:
            return
        now = time.time()
        if self.market_msg_id and now - self.market_status_ts < MARKET_STATUS_INTERVAL:
            return
        text = market_status_view(snap, self.cfg)
        if self.market_msg_id:
            r = await self.call("editMessageText", chat_id=self.chat_id, message_id=self.market_msg_id,
                                text=text, parse_mode="HTML", disable_web_page_preview=True)
            if not r.get("ok") and "message is not modified" not in r.get("description", ""):
                self.market_msg_id = None   # сообщение удалили/недоступно — создадим заново ниже
        if not self.market_msg_id:
            r = await self.send(text)
            if r.get("ok"):
                self.market_msg_id = r["result"]["message_id"]
                await self.call("pinChatMessage", chat_id=self.chat_id, message_id=self.market_msg_id,
                                disable_notification=True)
        self.market_status_ts = now

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
        """Запомнить связки выше порога за тихие часы — по одной, лучшей по прибыли, на пару площадок, вместе с
        настройками её снимка (snap_cfg): сумму могли сменить до утра, а % посчитан на сумму снимка. Связку по
        устаревшим данным площадки (не ответила за VENUE_TIMEOUT) _signal_deals не отдаёт — как и сигналом."""
        cfg = None
        for d in self._signal_deals(snap):
            _, b, s, _ = d
            key = (b.ex, b.asset, s.ex, s.asset)
            if key not in self.night_deals or d[0] > self.night_deals[key][0][0]:
                if cfg is None:
                    cfg = copy.deepcopy(self.snap_cfg(snap))
                self.night_deals[key] = (d, cfg)

    def night_signal_lines(self, since, now, limit=DIGEST_VENUES):
        """Строки дайджеста «сколько было связок выше порога по направлениям площадок» за ночь (history.signals):
        сначала направления с большим числом эпизодов, не больше limit строк, остальное — «…ещё N»."""
        try:
            counts = history.venue_signals(since, now)
        except Exception as e:
            logger.warning("digest signals: %s", e)
            return []
        if not counts:
            return []
        total = sum(v["episodes"] for v in counts.values())
        order = sorted(counts.items(), key=lambda kv: (-kv[1]["episodes"], -kv[1]["best"], kv[0]))
        lines = [f"📡 <b>Связки выше порога</b>: {total} по {len(counts)} направл."]
        lines += [f"• {html.escape(b)} → {html.escape(s)}: {v['episodes']} (лучшая {v['best']:+.2f}%)"
                  for (b, s), v in order[:limit]]
        if len(order) > limit:
            lines.append(f"• …ещё {len(order) - limit} направл.")
        return lines

    def night_digest_text(self, deals, since, now):
        """Утренний дайджест одним сообщением (до DIGEST_MAX символов): связки выше порога по площадкам за ночь,
        топ-3 связки ночи и итог сухого прогона за сутки. deals — [(связка, cfg её снимка)] из night_deals."""
        head = f"🌅 <b>Доброе утро! Итоги ночи</b> ({_hhmm_msk(since)}–{_hhmm_msk(now)} МСК)"
        signals = self.night_signal_lines(since, now)
        top = sorted(deals, key=lambda dc: dc[0][0], reverse=True)[:3]
        try:
            paper_lines = self.paper_digest_lines(now)
        except Exception as e:   # paper.db занята/испорчена — дайджест со связками всё равно уходит
            logger.warning("digest paper: %s", e)
            paper_lines = []

        def build(full_deals, venues):
            parts = [head]
            if signals:
                parts.append("\n".join(signals[:venues + 1] if venues < len(signals) - 1 else signals))
            if top:
                items = [f"{i}) {fmt_deal(d, cfg)}" if full_deals else f"{i}) {html.escape(d[1].ex)} → "
                         f"{html.escape(d[2].ex)} {html.escape(d[1].asset)}/{html.escape(d[2].asset)} "
                         f"<b>{d[0]:+.2f}%</b>" for i, (d, cfg) in enumerate(top, 1)]
                parts.append("🏆 <b>Топ-3 связки за ночь</b>\n\n" + "\n\n".join(items))
            else:
                parts.append("Тихие часы закончились — связок выше порога не было.")
            if paper_lines:
                parts.append("\n".join(paper_lines))
            return "\n\n".join(parts)

        text = build(True, len(signals))
        if len(text) > DIGEST_MAX:
            text = build(False, len(signals))    # топ-3 одной строкой вместо полной карточки
        venues = len(signals)
        while len(text) > DIGEST_MAX and venues > 1:
            venues -= 1                          # меньше направлений — первая строка (итог) остаётся
            text = build(False, venues)
        return cut_lines(text, DIGEST_MAX)

    async def send_night_digest(self):
        """Дайджест по окончании тихих часов — одно сообщение (night_digest_text): связки по площадкам за ночь, топ-3
        по прибыли и сухой прогон за сутки; сумма круга у каждой связки — та, на которую её посчитали (из её
        снимка), а не текущая. Ночные связки и начало ночи очищаются только после доставки (или отказа Telegram,
        который повтор не исправит — delivery_final); сбой сборки, сети, 429/5xx — данные остаются, повтор не раньше
        чем через DIGEST_RETRY сек (digest_pending держит quiet_and_pause_tick). True — дайджест закрыт."""
        now = time.time()
        if now < self.digest_retry_at:
            return False
        since = self.quiet_since or now - DIGEST_FALLBACK_WINDOW
        try:
            r = await self.send(self.night_digest_text(list(self.night_deals.values()), since, now), topic="signals")
        except Exception as e:
            logger.warning("night digest: %s", accounts.api_error_text(e))
            r = {}
        if not delivery_final(r):
            self.digest_retry_at = now + DIGEST_RETRY
            logger.warning("night digest not sent, retry in %ss: %s", DIGEST_RETRY, r.get("description"))
            return False
        if not r.get("ok"):
            logger.warning("night digest refused: %s", r.get("description"))
        self.night_deals, self.quiet_since, self.digest_retry_at = {}, None, 0.0
        return True

    def paper_digest_lines(self, now=None):
        """Сводка сухого прогона за последние сутки для утреннего дайджеста: строка итогов (как «За сегодня» в /paper,
        но за 24 ч до now), результат исполнившихся в ₽ и самые частые причины срывов. [] — прогон выключен или за
        сутки не завершилось ни одного круга."""
        if not paper.settings()["on"]:
            return []
        now = time.time() if now is None else now
        try:
            p = paper.summary_since(now - 86400)
        except Exception as e:   # paper.db занята/испорчена — дайджест уходит без сводки прогона
            logger.warning("digest paper: %s", e)
            return []
        if not p["total"]:
            return []
        line = f"🧪 Сухой прогон за сутки: {p['total']} кругов, исполнилось {p['done']}"
        if p["failed"]:
            reasons = ", ".join(f"{paper.FAIL_LABELS.get(r, r)} {n}" for r, n in p["failed_by_reason"].items())
            line += f", сорвалось {p['failed']} ({reasons})"
        if p["avg_diff"] is not None:
            line += f", факт vs план {p['avg_diff']:+.2f} п.п."
        balance = paper.get_balance()
        if balance is not None:
            change = paper.balance_change()
            change_str = f"{change:+,.0f}".replace(",", " ")
            line += f", баланс {_money(balance)} ₽ ({change_str} ₽)"
        lines = [line]
        if p["done"]:
            lines.append(f"Итог исполнившихся за сутки: {p['profit_rub']:+,.0f} ₽".replace(",", " "))
        if p["top_notes"]:
            lines.append("Причины срывов: " + "; ".join(f"{html.escape(n[:80])} ×{c}" for n, c in p["top_notes"]))
        return lines

    def paper_digest_line(self):
        """Первая строка paper_digest_lines() — итоги сухого прогона за сутки; None — показывать нечего."""
        lines = self.paper_digest_lines()
        return lines[0] if lines else None

    async def quiet_and_pause_tick(self, snap):
        """Тихие часы копят связки для утреннего дайджеста вместо отправки; обычная пауза (ручная или
        по /pause) просто не шлёт сигналы. Дайджест уходит один раз — в момент выхода из тихих часов."""
        quiet = self.is_quiet_now()
        if quiet:
            if not self._was_quiet and self.quiet_since is None:
                self.quiet_since = time.time()   # начало ночи — окно для «связок по площадкам» в дайджесте
            self.collect_night_deals(snap)
        elif self._was_quiet or self.digest_pending:
            self.digest_pending = not await self.send_night_digest()
        self._was_quiet = quiet
        paused = self.paused or (self.pause_until and time.time() < self.pause_until)
        since = time.time()   # отправленное notify в этом скане отмечено в self.sent не раньше
        if not quiet and not paused:
            await self.notify(snap)
        await self.record_signals(snap, since, quiet, bool(paused))

    def signal_reasons(self, snap, since, quiet=False, paused=False):
        """Связки выше порога и почему по каждой не ушёл сигнал в этом скане: [(ключ, связка, причина)], причина
        None — сигнал ушёл (антидубль отмечен не раньше since). Иначе — первая преграда в порядке notify: quiet /
        paused → trap (🪤 не шлём, SIGNAL_TRAPS=0) → stale (данные площадки устарели: не ответила за VENUE_TIMEOUT,
        p2p.deal_stale; места в топ-N не занимает) → max_signals (вне топ-N и не ⭐ избранная) → unconfirmed (держится
        меньше LIVE_SCANS сканов) → cooldown (антидубль) → unsent (не доставлено)."""
        top = {self._deal_key(d) for d in self._signal_deals(snap)}
        favs, fav_min = favorites.keys(), favorites.fav_min_profit()
        traps = signal_traps()
        out = []
        for d in snap.deals:
            if d[0] < self.cfg.min_profit:
                continue
            key = self._deal_key(d)
            prev = self.sent.get(key)
            if prev and prev[0] >= since:
                reason = None
            elif quiet or paused:
                reason = "quiet" if quiet else "paused"
            elif not traps and reliability(d, self.cfg, snap)[0] == TRAP:
                reason = "trap"
            elif deal_stale(d):
                reason = "stale"
            elif key not in top and not (favorites.key_str(key) in favs and d[0] >= fav_min):
                reason = "max_signals"
            elif not self.is_confirmed(d):
                reason = "unconfirmed"
            elif prev and since - prev[0] < self.cooldown and d[0] < prev[1] + self.repeat_step:
                reason = "cooldown"
            else:
                reason = "unsent"
            out.append((key, d, reason))
        return out

    async def record_signals(self, snap, since, quiet=False, paused=False):
        """Эпизоды связок выше порога в history.signals (этап 1 «измерения»): был ли сигнал и почему нет — для доли
        пропущенных связок (history.signal_stats). Пишем в отдельном потоке; ошибка — строка в логе, сигналы не ломает."""
        try:
            rows = [(key, d[0], reason is None, reason)
                    for key, d, reason in self.signal_reasons(snap, since, quiet, paused)]
            self.signal_rows = await asyncio.to_thread(history.track_signals, rows, snap.ts or since, self.signal_rows,
                                                       self.snap_cfg(snap).amount, self.cfg.min_profit)
        except Exception as e:
            logger.warning("signals: %s", e)

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
                st["reason"] = failed[ex]
                st["down_since"] = st["down_since"] or now
                trouble = st["streak"] >= VENUE_FAIL_STREAK or now - st["down_since"] > VENUE_DOWN_AFTER
                if trouble and (not st["alerted_at"] or now - st["alerted_at"] > VENUE_ALERT_COOLDOWN):
                    st["alerted_at"] = now
                    await self.send(f"⚠️ {ex}: недоступна ({failed[ex]})", topic="dev")
            else:
                if st["down_since"]:   # эпизод простоя закончился — в историю (после рестарта счётчики обнуляются)
                    try:
                        history.record_outage(ex, st["down_since"], now, st["streak"], bool(st["alerted_at"]),
                                              st.get("reason", ""))
                    except Exception as e:
                        logger.warning("outage history: %s", e)
                if st["alerted_at"]:
                    await self.send(f"✅ {ex}: снова доступна", topic="dev")
                st.update(streak=0, down_since=None, alerted_at=None, reason="")

    @staticmethod
    def _deal_key(d):
        _, b, s, _ = d
        return (b.ex, b.asset, s.ex, s.asset)

    def _signal_deals(self, snap, traps=None):
        """Связки выше порога в порядке сканера (p2p.score; EV_RANK=1 — по EV, Bot.ev_rank), не больше MAX_SIGNALS.
        Сначала порог и отсев «🪤 ловушек» и связок по устаревшим данным площадки (p2p.deal_stale: не ответила за
        VENUE_TIMEOUT — в /top видны с пометкой), потом топ-N: надёжная связка ниже порога, ловушка или устаревшая,
        стоящая выше в списке, не должна закрывать связки за ней. traps — брать и ловушки (None — по SIGNAL_TRAPS)."""
        traps = signal_traps() if traps is None else traps
        return [d for d in snap.deals if d[0] >= self.cfg.min_profit and not deal_stale(d)
                and (traps or reliability(d, self.cfg, snap)[0] != TRAP)][:self.max_signals]

    def track_liveness(self, snap, now=None):
        """Сколько сканов подряд связка держится выше порога: минутный выброс не сигналим, устойчивую — да.
        Серия растёт, только если обе стороны связки получены в этом скане (p2p.deal_fresh): сторона из кэша монет
        (ALT_INTERVAL) или выгрузки BestChange (BC_REFRESH) — те же данные, что скан назад, не новое подтверждение;
        такой скан серию не продлевает и не сбрасывает (новая связка начинает с 0). Упала ниже порога — сброс."""
        now = now or time.time()
        favs, fav_min = favorites.keys(), favorites.fav_min_profit()
        alive = {}
        for d in snap.deals:
            key = self._deal_key(d)
            if key not in alive and (d[0] >= self.cfg.min_profit
                                     or (d[0] >= fav_min and favorites.key_str(key) in favs)):
                alive[key] = d
        for key, d in alive.items():
            fresh = deal_fresh(d, snap)
            rec = self.live.get(key)
            if rec:
                if fresh:
                    rec["streak"] += 1
            else:
                self.live[key] = {"first": now, "streak": 1 if fresh else 0}
        for key in list(self.live):
            if key not in alive:
                del self.live[key]

    def is_confirmed(self, d):
        """Связка держится LIVE_SCANS сканов подряд — только такие уходят владельцу сигналом (и в сухой прогон)."""
        return self.live_scans <= 1 or self.live.get(self._deal_key(d), {}).get("streak", 0) >= self.live_scans

    def held_label(self, d, now=None):
        """«⏱ держится N мин», если связка видна не первый скан; иначе пусто."""
        rec = self.live.get(self._deal_key(d))
        if not rec or rec["streak"] < 2:
            return ""
        minutes = max(1, int(((now or time.time()) - rec["first"]) / 60))
        return f"⏱ держится {minutes} мин · "

    async def maybe_start_paper_cycle(self, deals, snap):
        """Сухой прогон (paper.py): при свободном слоте виртуально «берём» лучшую по p2p.score (на сумме
        PAPER_AMOUNT) связку из тех, о которых владелец получает сигнал (выше порога и держится LIVE_SCANS
        сканов), если стакана хватает на PAPER_AMOUNT (deal_for_amount/_stack) и она не «🪤 ловушка»
        (PAPER_TRAPS=1 — брать и их, notify тогда отдаёт их сюда и при SIGNAL_TRAPS=0). Пишем
        круг с меткой надёжности в data/paper.db со стадией buy. Карточка — только владельцу, гостям про
        сухой прогон ничего не идёт."""
        settings = paper.settings()
        if not settings["on"] or not self.chat_id or not deals:
            return
        if len(paper.open_cycles()) >= settings["max_open"]:
            return
        # лимит СБП исчерпан по-настоящему (trades) или по виртуальному обороту прогона — комиссия 0.5% в плане
        own = trades.own_banks()[0]
        over = frozenset(snap.over_banks) | {b for b in own if paper.bank_month_total(b) >= trades.free_limit(b)}
        psnap = dataclasses.replace(snap, over_banks=over)
        picked = None   # лучшая по p2p.score уже на сумме прогона, а не первая в списке (тот отсортирован на AMOUNT)
        for deal in deals:
            if not self.is_confirmed(deal) or deal_stale(deal):
                continue   # сигнала о ней ещё не было (выброс одного скана) или данные площадки устарели — не берём
            if not paper.simple_route(deal):
                continue   # через спот/межмонетные — пока нет, условия возврата в ROADMAP (межмонетные, часть 2)
            d = deal_for_amount(deal, self.cfg, psnap, settings["amount"])
            if d is None or d[0] < self.cfg.min_profit:
                continue   # на сумму сухого прогона глубины не хватает или прибыль ниже порога
            label, reasons = reliability(d, self.cfg, snap)
            if label == TRAP and not settings["traps"]:
                continue
            rank = score(d, self.cfg, snap)
            if picked is None or rank > picked[0]:
                picked = (rank, d, label, reasons)
        if picked is None:
            return
        rank, d, label, reasons = picked
        profit, b, s, route = d
        paper.init_balance(settings["amount"])
        # выход маршрута в монете продажи по итоговому стеку s (его parts: переводов на каждый обменник) — без
        # запаса на курс и с комиссией СБП, если лимит исчерпан; по нему же план без запаса — с ним сравнивается факт
        route_cfg = dataclasses.replace(self.cfg, amount=settings["amount"])
        qty = _route_qty(b, s, route_cfg, psnap.spot, over, disable=frozenset({"risk"}))
        raw = (qty * s.price / settings["amount"] - 1) * 100 if qty else profit
        # площадки конвертации и сеть/комиссия каждого хопа на момент старта: стадия transfer проверяет именно эти
        # переводы, sell считает выход по их комиссиям, время перевода круга — по их сетям (paper.start_cycle);
        # межмонетные связки фильтр paper.simple_route пока не пускает (снятие — шаг владельца)
        hops = route_hops(b, s, route_cfg, psnap.spot, over)
        # для разбора (этап 1 «измерения»): индекс и причины надёжности, серия «живости», запас глубины и id снимка
        # скана — снимок пишется после сигналов, но id (время начала скана) известен уже сейчас
        measures = {"index": reliability_index(d, self.cfg, snap), "reasons": reasons,
                    "streak": self.live.get(self._deal_key(d), {}).get("streak", 0),
                    "depth": paper.depth_margin(psnap, b, s, settings["amount"], qty or s.avail),
                    "snapshot_id": snapshots.scan_id(snap)}
        if measures["snapshot_id"] is not None:   # снимок этого скана запишется, даже если он не SNAPSHOT_EVERY-й
            self.snapshot_keep.add(measures["snapshot_id"])
        # бумажный хедж (simperp): шорт перпа на монету круга; HEDGE_PLAN=1 — в плане стоимость хеджа вместо запаса.
        # Монета круга — выход маршрута, а без него купленное (не s.avail — это весь объём объявления продажи).
        # for_cycle не бросает исключений: сбой хеджа не мешает ни кругу, ни сигналам после него
        hedge, hedge_note, profit, hedge_line = simperp.for_cycle(
            b.asset, qty or settings["amount"] / b.price, settings["amount"], psnap.ref, b.price, raw, profit,
            risk=self.cfg.risk_buffer.get(b.asset, 0.0))
        # over — тот же, что в плане и qty: банк оплаты и комиссия СБП в круге совпадут с планом
        cycle = paper.get_cycle(paper.start_cycle(settings["amount"], b, s, route, profit, label=label,
                                                  sell_qty=qty or s.avail, pay_fee=self.cfg.pay_fee, over=over,
                                                  planned_raw=raw, hops=hops, **measures)) or {}
        try:
            simperp.open_hedge(cycle.get("id"), hedge, hedge_note)
        except Exception as e:
            logger.error("simperp: %s", e)
        pay = trades.pay_label(cycle.get("pay_kind", ""), cycle.get("bank", ""), b.pays)
        qty = settings["amount"] / b.price
        text = (f"🧪 <b>Сухой прогон</b>: купил бы {_money(qty)} {b.asset} у {html.escape(b.nick)} "
                f"по {_price(b.price)} ₽, оплата: {html.escape(pay)} · план {profit:.2f}% · {label}"
                f" · оценка {rank:+.2f}")
        if reasons:
            text += "\n" + "\n".join(f"• {html.escape(r)}" for r in reasons)
        if hedge_line:
            text += "\n" + html.escape(hedge_line)
        await self.send(text, topic="signals")

    async def process_paper_cycles(self, snap):
        """Сухой прогон: стадии открытых виртуальных кругов по свежему снимку/справочникам, без сети.
        buy — через PAPER_PAY_MINUTES покупка по свежему стакану (мерчанты круга, не хватило — другие по цене;
        цена хуже плана больше PAPER_BUY_SLIP_MAX — срыв; у обменника — только по свежей котировке BestChange),
        цена покупки пишется в круг; transfer — через время перевода по сетям круга переводы маршрута ещё возможны
        (fees/netstatus), неизвестный статус сети — риск в круге; sell — продаём лучшим объявлениям стакана на весь
        объём, прибыль — по фактическим ценам покупки и продажи (может быть ниже плана и в минус). Срыв
        (failed_buy/failed_transfer/failed_sell): стакана покупки не хватило или цена ушла, перевод закрыт,
        покупателей на весь объём нет. Всё состояние круга — в data/paper.db: перезапуск бота круг продолжает."""
        if not self.chat_id:
            return
        settings = paper.settings()
        for cycle in paper.open_cycles():
            if cycle["stage"] == "buy":
                action, note = paper.check_buy_stage(cycle, snap, settings["pay_minutes"],
                                                     stale_minutes=settings["stale_minutes"])
                if action == "wait":
                    continue
                paper.set_buy_check(cycle["id"], *paper.buy_observed(cycle, snap))   # цена и объём на проверке
                if action == "fail":
                    if not paper.finish_cycle(cycle["id"], "failed_buy", 0.0, note):
                        continue   # круга уже нет (/paper reset посреди обработки)
                    await self.send(f"🧪 Сухой прогон: круг #{cycle['id']} сорвался на покупке — {note}",
                                    topic="signals")
                else:
                    # цена покупки по свежему стакану (проскальзывание, другие мерчанты) — для факта на продаже
                    paper.set_buy_fill(cycle["id"], paper.buy_fill(cycle, snap))
                    paper.set_stage(cycle["id"], "transfer")
            elif cycle["stage"] == "transfer":
                action, note = paper.check_transfer_stage(cycle, self.cfg, settings["transfer_minutes"])
                if action == "wait":
                    continue
                if action == "fail":
                    if not paper.finish_cycle(cycle["id"], "failed_transfer", 0.0, note):
                        continue
                    await self.send(f"🧪 Сухой прогон: круг #{cycle['id']} сорвался на переводе — {note}",
                                    topic="signals")
                else:
                    paper.add_risks(cycle["id"], paper.transfer_risks(cycle, self.cfg))   # неизвестный статус сети — риск
                    paper.set_stage(cycle["id"], "sell")
            elif cycle["stage"] == "sell":
                action, note, price = paper.check_sell_stage(cycle, snap, cfg=self.cfg,
                                                              stale_minutes=settings["stale_minutes"])
                if action == "wait":
                    continue
                if action == "fail":
                    if not paper.finish_cycle(cycle["id"], "failed_sell", 0.0, note):
                        continue
                    await self.send(f"🧪 Сухой прогон: круг #{cycle['id']} сорвался на продаже — {note}",
                                    topic="signals")
                else:
                    # тот же пересчёт по свежему snap.spot, что уже решил check_sell_stage — межмонетные/спот
                    # связки видят движение курса между стартом круга и продажей, а не число со старта
                    qty = paper.recompute_sell_qty(cycle, self.cfg, snap.spot)
                    rp = paper.realized_pct(cycle, price, qty)
                    if not paper.finish_cycle(cycle["id"], "done", rp, note, sell_fact=price):
                        continue
                    # план без запаса на курс — с ним сравнивает и /paper (у старых кругов его нет — план с запасом)
                    plan = cycle.get("planned_raw")
                    plan = cycle["planned_pct"] if plan is None else plan
                    await self.send(f"🧪 Сухой прогон: круг #{cycle['id']} завершён — план "
                                    f"{plan:.2f}%, факт {rp:.2f}%"
                                    + (f" ({note})" if note else ""), topic="signals")

    def paper_hedge_tick(self, snap):
        """Бумажный хедж (simperp): фандинг по расчётам и откуп шорта у завершённых кругов — сбой не мешает скану."""
        try:
            simperp.tick(snap.ref)
        except Exception as e:
            logger.error("simperp: %s", e)

    async def check_paper_ladder(self):
        """Лестница суммы сухого прогона (paper.ladder_suggestion): сам PAPER_AMOUNT не меняет —
        шлёт владельцу сообщение с кнопкой подтверждения, не чаще раза в LADDER_ALERT_COOLDOWN."""
        if not self.chat_id or not paper.settings()["on"]:
            return
        suggestion = paper.ladder_suggestion()
        if not suggestion:
            return
        now = time.time()
        if now - self.paper_ladder_alerted_ts < LADDER_ALERT_COOLDOWN:
            return
        self.paper_ladder_alerted_ts = now
        amount = suggestion["amount"]
        if suggestion["action"] == "up":
            text = ("🧪 Сухой прогон стабилен (≥20 кругов, срывов мало, факт не хуже плана) — "
                    f"можно попробовать сумму круга {_money(amount)} ₽.")
        else:
            text = (f"🧪 Сухой прогон: за неделю много срывов — может, вернуться на "
                    f"{_money(amount)} ₽ за круг?")
        kb = {"inline_keyboard": [[{"text": f"Перейти на {_money(amount)} ₽",
                                    "callback_data": f"paper_ladder:{amount:.0f}"}]]}
        await self.send(text, markup=kb, topic="signals")

    async def notify(self, snap):
        now = time.time()
        deals = self._signal_deals(snap)   # сначала порог, потом топ-N по надёжности
        # PAPER_TRAPS=1 — прогону нужны и ловушки, даже когда сигналом они не приходят (SIGNAL_TRAPS=0)
        await self.maybe_start_paper_cycle(self._signal_deals(snap, traps=True) if paper.settings()["traps"]
                                           else deals, snap)
        active = {self._deal_key(d) for d in deals}   # заранее: обрыв отправки не делает связки «устаревшими»
        # связки по устаревшим данным площадки (не ответила за VENUE_TIMEOUT) не шлём и карточку по ним не правим, но и
        # «⌛ связка устарела» не ставим: связка на месте, данных просто нет в этом скане
        active |= {self._deal_key(d) for d in snap.deals if d[0] >= self.cfg.min_profit and deal_stale(d)}
        for d in deals:
            profit = d[0]
            key = self._deal_key(d)
            if not self.is_confirmed(d):
                continue                                          # появилась только что — ждём подтверждения
            prev = self.sent.get(key)
            if prev and now - prev[0] < self.cooldown and profit < prev[1] + self.repeat_step:
                await self.update_live_card(key, d, snap, now)    # без нового сообщения — обновляем на месте
                continue
            # антидубль расходуем только после доставки: сбой Telegram — повторим в следующем скане
            try:
                r = await self.send_deal(d, "🔔 " + self.held_label(d, now), snap=snap, topic="signals", nav=False)
            except Exception as e:   # сеть/таймаут: остальные связки этого скана тоже не дойдут
                logger.warning("signal send error: %s", accounts.api_error_text(e))   # без URL с токеном бота
                break
            if not r.get("ok"):
                logger.warning("signal not sent: %s", r.get("description"))
            if not delivery_final(r):
                continue
            self.sent[key] = (now, profit)
            # гостям — та же карточка, без кнопок журнала и без «живого» обновления; только после доставки
            # владельцу, иначе повтор на следующем скане продублировал бы её гостям
            for g in sorted(self.guests):
                try:
                    await self.send_deal(d, "🔔 " + self.held_label(d, now), snap=snap, chat_id=g, nav=False)
                except Exception as e:
                    logger.warning("сигнал гостю %s: %s", g, e)
        await self.notify_favorites(snap, active, now)
        await self.mark_stale_deals(active)

    async def notify_favorites(self, snap, active, now):
        """⭐ Избранные маршруты: сигнал от FAV_MIN_PROFIT, даже ниже общего порога и вне топа MAX_SIGNALS; тот же
        антидубль и «живость», что у обычных сигналов. Только владельцу. active дополняется — связка не «устареет».
        «🪤 Ловушку» и по избранному маршруту не шлём (SIGNAL_TRAPS=1 — шлём)."""
        favs, fav_min = favorites.keys(), favorites.fav_min_profit()
        if not favs:
            return
        traps = signal_traps()
        for d in snap.deals:
            key = self._deal_key(d)
            if key in active or favorites.key_str(key) not in favs or d[0] < fav_min or not self.is_confirmed(d):
                continue
            if not traps and reliability(d, self.cfg, snap)[0] == TRAP:
                continue
            active.add(key)
            if deal_stale(d):
                continue   # данные площадки устарели (VENUE_TIMEOUT) — не шлём, карточка остаётся как есть
            prev = self.sent.get(key)
            if prev and now - prev[0] < self.cooldown and d[0] < prev[1] + self.repeat_step:
                await self.update_live_card(key, d, snap, now)
                continue
            try:
                r = await self.send_deal(d, "⭐ " + self.held_label(d, now), snap=snap, topic="signals", nav=False)
            except Exception as e:
                logger.warning("signal send error: %s", accounts.api_error_text(e))
                break
            if delivery_final(r):
                self.sent[key] = (now, d[0])

    async def toggle_favorite(self, cq, data):
        """Кнопка ⭐ на карточке: маршрут связки в избранное / из избранного; кнопка на карточке меняется."""
        entry = self.deals_by_id.get(int(data[4:])) if data[4:].isdigit() else None
        if not entry:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сигнал устарел")
            return
        d, cfg, snap = entry
        on = favorites.toggle(self._deal_key(d))
        await self.call("answerCallbackQuery", callback_query_id=cq["id"],
                        text=(f"⭐ В избранном: сигнал по маршруту от {favorites.fav_min_profit():g}%" if on
                              else "Убрано из избранного"))
        await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                        reply_markup=self.markup(deal_markup(d, int(data[4:]), cfg, snap)))

    def favorites_view(self):
        """«/fav»: избранные маршруты и порог сигнала по ним; кнопки — убрать."""
        favs = sorted(favorites.keys())
        if not favs:
            return ("⭐ Избранных маршрутов нет. Нажми «⭐ В избранное» под карточкой связки — по этому маршруту "
                    f"сигнал будет приходить от {favorites.fav_min_profit():g}% (FAV_MIN_PROFIT), даже вне топа."), None
        lines = [f"⭐ <b>Избранные маршруты</b> — сигнал от {favorites.fav_min_profit():g}%:", ""]
        lines += [f"{i}. {html.escape(favorites.label(k))}" for i, k in enumerate(favs, 1)]
        # в кнопке — сам маршрут, а не номер: список мог смениться, пока сообщение висит в чате
        kb = [[{"text": f"✖ {i}. {favorites.label(k)}"[:60], "callback_data": f"favdel:{k}"}] for i, k in enumerate(favs, 1)]
        return "\n".join(lines), {"inline_keyboard": kb}

    async def update_live_card(self, key, d, snap, now):
        """«Живая карточка»: вместо повторной отправки того же сигнала правим последнее сообщение по нему
        (editMessage), но не чаще раза в LIVE_EDIT_INTERVAL секунд."""
        live = self.live_msg.get(key)
        if not live or live["stale"] or now - live["last_edit"] < LIVE_EDIT_INTERVAL:
            return
        cfg = self.snap_cfg(snap)
        caption = "🔔 " + self.held_label(d, now) + fmt_signal(d, cfg, snap)
        chips = live.get("chips")   # строка уточнённых фишек сумм (chip_refresh) при живых правках остаётся
        if chips and len(caption) + len(chips) + 1 <= (1024 if live["photo"] else 4096):
            caption += "\n" + chips
        live["last_edit"], live["caption"] = now, caption
        entry = (d, copy.deepcopy(cfg), _lean(snap))
        try:
            r = await self.edit_live_card(live, caption, self.live_markup(live, entry))
        except Exception as e:
            logger.warning("live card edit error: %s", e)
            return
        # подпись теперь по новой связке и настройкам её снимка — кнопки «✅ Сделал»/«📝 Инструкция»/«🚫» этого
        # сообщения тоже, иначе в журнал уйдёт сумма, которой на карточке уже нет
        if r.get("ok"):
            live["deal"] = entry
            if live.get("deal_id") in self.deals_by_id:
                self.deals_by_id[live["deal_id"]] = entry

    def live_markup(self, live, entry=None):
        """Кнопки сигнальной карточки для её правки (живая карточка, «⌛ устарела»): без reply_markup Telegram снимает
        с сообщения все кнопки. entry — (связка, настройки, снимок) новой подписи, по умолчанию — нынешней
        (live["deal"]). Кнопки журнала («📝»/«✅ Сделал»/«⭐»/«🚫») — пока запись под deal_id жива; нажали
        «✅ Сделал»/«🚫» — как после них: купить/продать, спот, «📋». Неизвестно, что на карточке, — None."""
        entry = entry or live.get("deal")
        if not entry:
            return None
        d, cfg, snap = entry
        deal_id = live.get("deal_id") if live.get("deal_id") in self.deals_by_id else None
        return deal_markup(d, deal_id, cfg, snap, nav=False)

    async def edit_live_card(self, live, text, markup):
        """Правка подписи/текста сигнальной карточки вместе с её кнопками (markup, см. live_markup); цветные кнопки
        не поддерживаются — повтор с обычными, как в chip_refresh. Ответ Telegram; исключения — вызывающему."""
        kw = {"chat_id": self.chat_id, "message_id": live["message_id"], "parse_mode": "HTML"}
        for mk in (self.markup(markup), plain_markup(markup)) if markup else (None,):
            if mk is not None:
                kw["reply_markup"] = mk
            if live["photo"]:
                r = await self.call("editMessageCaption", caption=text, **kw)
            else:
                r = await self.call("editMessageText", text=text, disable_web_page_preview=True, **kw)
            if not self._fancy_failed(r, mk):
                break
        return r

    async def mark_stale_deals(self, active):
        """Связка пропала из топа — один раз пометить последний сигнал по ней «⌛ устарел». Помеченной считаем, только
        когда Telegram принял правку: 429, 5xx и сбой сети — повтор на следующих сканах не раньше retry_after (нет его —
        через STALE_RETRY_BASE, дальше ×2 до STALE_RETRY_MAX); сообщения уже нет (400/403) — повтор не поможет.
        Кнопки карточки остаются (live_markup), и «✅ Сделал» тоже: сделку могли провести, пока связка держалась, —
        записать её можно и после пометки, по тому же расчёту, что в подписи."""
        now = time.time()
        for key, live in self.live_msg.items():
            if key in active or live["stale"] or now < live.get("stale_retry_at", 0.0):
                continue
            text = live["caption"] + STALE_MARK
            try:
                r = await self.edit_live_card(live, text, self.live_markup(live))
            except Exception as e:   # сеть/таймаут — повторим
                logger.warning("live card stale error: %s", accounts.api_error_text(e))   # без URL с токеном бота
                r = {}
            if r and not r.get("ok"):
                logger.warning("live card stale: %s", r.get("description"))
            if delivery_final(r):
                live["stale"], live["deal"] = True, None   # помеченную больше не правим — снимок не держим
                continue
            tries = live["stale_tries"] = live.get("stale_tries", 0) + 1
            retry = (r.get("parameters") or {}).get("retry_after")
            live["stale_retry_at"] = now + (retry or min(STALE_RETRY_BASE * 2 ** (tries - 1), STALE_RETRY_MAX))

    async def check_accounts(self):
        """Уведомление о новых движениях по подключённым биржам: депозит, вывод, спот-сделка, P2P-ордер.

        Первый опрос после старта только запоминает текущую историю (без сообщений, чтобы не спамить
        старыми записями) — дальше в Telegram уходят только записи, которых не было в прошлый раз.
        Пустая история при успешном ответе ([]) — тоже первый опрос: первая же операция после неё придёт.
        Заодно история этого опроса идёт на автосопоставление с журналом сделок (`self.auto_match_facts`)."""
        hist_by_ex = {}
        for ex in accounts.ONBOARDABLE:
            if accounts.keys(ex) is None:
                continue
            try:
                hist = await accounts.account_history(self.s, ex)
            except Exception as e:
                logger.warning("account history error: %s %s", ex, accounts.api_error_text(e))   # без URL с ключом
                continue
            if hist is None:   # ошибка / не ответил источник — базу не трогаем; [] — успешно пусто, это тоже база
                continue
            hist_by_ex[ex] = hist
            seen = self.acc_seen.get(ex)
            if seen is not None:
                for it in hist:
                    if hist_key(it) not in seen:
                        await self.send(hist_text(ex, it), topic="journal")
            # объединяем, а не заменяем: если один источник сейчас не ответил, его записи из прошлых опросов
            # не должны после восстановления прийти как новые
            self.acc_seen[ex] = (seen or set()) | {hist_key(it) for it in hist}
        if hist_by_ex:
            await self.auto_match_facts(hist_by_ex)

    async def auto_match_facts(self, hist_by_ex):
        """Сделки журнала без факта или с фактом «как расчёт»/«±0.5 п.п.» (`trades.unmatched`) сверить с историей
        бирж этого опроса (`trades.match_fact`, чистыми) и записать факт (fact_source=auto), если нашлись P2P-ордера
        и покупки, и продажи."""
        since = time.time() - 86400  # не старше суток — дальше сопоставлять по времени уже нет смысла
        for trade in trades.unmatched(since=since):
            fact = trades.match_fact(trade, hist_by_ex)
            if fact is None:
                continue
            trades.set_fact(trade["id"], fact, source=trades.FACT_AUTO)
            was = " вместо «как расчёт»" if trade.get("fact_source") in trades.PLAN_SOURCES else ""
            await self.send(f"✅ автосопоставление сделки #{trade['id']}: факт {fact:+.2f}% чистыми{was} "
                            f"(расчёт был {trade['profit']:+.2f}%)", topic="journal")

    def key_recheck_due(self, now=None):
        """Пора ли повторно проверить права ключей: KEY_RECHECK_HOURS > 0 и с прошлой проверки прошло столько часов."""
        hours = key_recheck_hours()
        now = time.time() if now is None else now
        return hours > 0 and now - self.key_checked_ts >= hours * 3600

    async def accounts_loop(self):
        while True:
            if self.chat_id:
                try:
                    await self.check_accounts()
                except Exception as e:
                    logger.error("accounts_loop error: %s", e)
                if self.key_recheck_due():
                    try:
                        await self.check_key_safety(periodic=True)
                    except Exception as e:
                        logger.error("key recheck error: %s", type(e).__name__)
            await asyncio.sleep(account_poll_interval())

    async def command_loop(self):
        offset = 0
        while True:
            try:
                r = await self.call("getUpdates", offset=offset, timeout=30)
            except Exception as e:
                logger.warning("getUpdates error: %s", accounts.api_error_text(e))   # без URL с токеном бота
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

    def _owner_gate(self, u):
        """Кто прислал u — сообщение (message) или нажатие кнопки (callback_query) Telegram:
        "owner" — владелец в своём личном чате: chat.type == "private" и from.id == chat.id == TG_CHAT_ID (у личного
        чата id равен id пользователя; у кнопки from — тот, кто нажал, chat — где сообщение с кнопкой): ему всё, в том
        числе настройки, ключи, выплаты и их подтверждение; "refuse" — чат TG_CHAT_ID, но не личный (группа,
        супергруппа, канал) или пишет/нажал не владелец: команды и кнопки владельца — отказ; "bind" — TG_CHAT_ID пуст и
        пишет человек в личном чате с ботом: этот чат станет чатом владельца (группа или канал — никогда);
        "guest" — гость из /allow: только GUEST_CMDS/GUEST_CALLBACKS; None — чужой.
        Защищённая функция (пин в tests/test_payout_pins.py): от неё зависит, кто может нажимать кнопки выплат."""
        u = u if isinstance(u, dict) else {}
        m = u if "chat" in u else u.get("message")   # у кнопки чат — у сообщения, к которому она приложена
        chat = (m.get("chat") if isinstance(m, dict) else None) or {}
        cid = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        sender = u.get("from") if isinstance(u.get("from"), dict) else {}
        private = bool(cid) and chat.get("type") == "private" and str(sender.get("id", "")) == cid
        if self.chat_id and cid == self.chat_id:
            return "owner" if private else "refuse"
        if not self.chat_id and private and m is u:
            return "bind"
        if cid and self.is_guest(cid):
            return "guest"
        return None

    async def on_update(self, u):
        cq = u.get("callback_query")
        if cq:
            chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            who = self._owner_gate(cq)
            if who == "owner":
                self.cur_thread = cq["message"].get("message_thread_id")   # ответ — в тот же топик
                await self.on_callback(cq)
            elif who == "refuse":
                logger.warning("кнопка владельца не из его личного чата (чат %s, нажал %s) — отказ",
                               chat, (cq.get("from") or {}).get("id"))
                await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=OWNER_ONLY_TOAST)
            elif who == "guest":
                await self.on_guest_callback(cq, chat)
            return
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if not chat:
            return
        self.cur_thread = msg.get("message_thread_id")
        who = self._owner_gate(msg)
        if who == "bind":
            # первый, кто написал боту в личку, становится владельцем и получателем сигналов
            self.chat_id = chat
            save_env("TG_CHAT_ID", chat)
            logger.info("chat_id сохранён в .env: %s", chat)
            await self.setup_topics()
            await self.start_onboarding()
            return
        if not self.chat_id:
            if chat not in self.unbound_asked:   # группа, канал или чужой отправитель: владельцем не делаем, раз
                self.unbound_asked.add(chat)
                logger.warning("TG_CHAT_ID пуст: чат %s (%s) не личный — владельцем не назначен, жду /start в личке",
                               chat, msg.get("chat", {}).get("type"))
                await self.send(FIRST_CHAT_PRIVATE, chat_id=chat)
            return
        if who == "owner":
            text = (msg.get("text") or "").strip()
            if self.awaiting_key and text and text not in BUTTONS and not text.startswith("/"):
                await self.handle_key_input(text, msg.get("message_id"))
            else:
                await self.handle(text)
        elif who == "refuse":
            if (msg.get("from") or {}).get("is_bot"):
                return   # служебное сообщение самого бота («закреплено» после pinChatMessage): не команда, шума в логе не надо
            text = (msg.get("text") or "").strip()
            logger.warning("команда владельца не из его личного чата (чат %s, %s, от %s) — отказ",
                           chat, msg.get("chat", {}).get("type"), (msg.get("from") or {}).get("id"))
            # на болтовню в группе не отвечаем, на команды — не чаще раза в 10 минут на чат (любой участник группы
            # иначе заставил бы бота слать отказ за отказом и тратить общий с сигналами лимит Telegram)
            if (text.startswith("/") or text in BUTTONS) and time.time() - self.refused_at.get(chat, 0) > 600:
                self.refused_at[chat] = time.time()
                private = msg.get("chat", {}).get("type") == "private"
                await self.send("🔒 Это только для владельца бота." if private else OWNER_ONLY_PRIVATE, chat_id=chat)
        elif who == "guest":
            await self.handle_guest(chat, (msg.get("text") or "").strip())
        else:
            await self.ask_access(chat, msg.get("from") or {})

    async def handle_guest(self, chat, text):
        """Команда гостя: ответы уходят ему (REPLY_CHAT), состояния ввода владельца не трогаем."""
        token = REPLY_CHAT.set(chat)
        try:
            cmd, _, arg = BUTTONS.get(text, text).partition(" ")
            await self.dispatch(cmd.split("@")[0], arg)
        finally:
            REPLY_CHAT.reset(token)

    async def on_guest_callback(self, cq, chat):
        data = cq.get("data", "")
        if data not in GUEST_CALLBACKS:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Только для владельца бота")
            return
        await self.call("answerCallbackQuery", callback_query_id=cq["id"])
        token = REPLY_CHAT.set(chat)
        try:
            await (self.show_best() if data == "best" else self.show_top())
        finally:
            REPLY_CHAT.reset(token)

    async def ask_access(self, chat, sender):
        """Чужой чат: если его @ник заранее разрешён (/allow @name) — подключить сразу; иначе один раз за запуск
        сказать ему id и один раз сообщить владельцу, как дать доступ."""
        uname = "@" + (sender.get("username") or "").lower()
        if uname in self.pending:
            self.pending.discard(uname)
            self.guests.add(chat)
            self.save_guests()
            await self.send(GUEST_WELCOME, chat_id=chat, markup=GUEST_MENU)
            await self.send(f"✅ {html.escape(uname)} (id <code>{chat}</code>) написал боту и подключён как гость. "
                            f"Убрать: /deny {chat}", topic="settings")
            return
        if chat in self.asked:
            return
        self.asked.add(chat)
        await self.send(ACCESS_HINT.format(chat=chat), chat_id=chat)
        who = html.escape(" ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or "?")
        if sender.get("username"):
            who += f" (@{html.escape(sender['username'])})"
        await self.send(f"👤 {who}, id <code>{chat}</code> написал боту. Дать доступ к сигналам и рыночным "
                        f"командам: <code>/allow {chat}</code>", topic="settings")

    def save_guests(self):
        save_env("TG_GUESTS", ",".join(sorted(self.guests) + sorted(self.pending)))

    async def cmd_allow(self, arg):
        gid = (arg or "").strip()
        if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", gid):          # по нику: подключится с первого сообщения боту
            self.pending.add(gid.lower())
            self.save_guests()
            await self.send(f"✅ {html.escape(gid)} получит доступ, как только напишет боту @{self.username or 'боту'} "
                            f"любое сообщение. Список: /guests")
        elif not re.fullmatch(r"-?\d+", gid):
            await self.send("Нужен id чата или @ник: /allow 123456789 либо /allow @username.")
        elif gid == self.chat_id:
            await self.send("Это твой собственный чат.")
        elif gid in self.guests:
            await self.send(f"{gid} уже в списке гостей.")
        else:
            self.guests.add(gid)
            self.save_guests()
            self.asked.discard(gid)
            await self.send(f"✅ {gid} добавлен: получает сигналы и рыночные команды. Убрать: /deny {gid}")
            await self.send(GUEST_WELCOME, chat_id=gid, markup=GUEST_MENU)

    async def cmd_deny(self, arg):
        gid = (arg or "").strip()
        if gid.lower() in self.pending:
            self.pending.discard(gid.lower())
            self.save_guests()
            await self.send(f"🚫 {html.escape(gid)} убран из ожидающих.")
            return
        if gid not in self.guests:
            await self.send(f"{gid or '?'} не в списке гостей. Список: /guests")
            return
        self.guests.discard(gid)
        self.save_guests()
        await self.send(f"🚫 {gid} убран из гостей.")
        await self.send("🔒 Владелец закрыл доступ к боту.", chat_id=gid, markup={"remove_keyboard": True})

    async def cmd_guests(self):
        if not self.guests and not self.pending:
            await self.send("Гостей нет. /allow @ник — доступ откроется с первого сообщения; "
                            "или, когда друг напишет боту, пришлю его id и команду /allow.")
            return
        lines = ["👥 <b>Гости</b> (сигналы + /best, /top, /calc, /banks, /maker, /fees, /history, /safety):"]
        lines += [f"• <code>{g}</code> — /deny {g}" for g in sorted(self.guests)]
        lines += [f"• {html.escape(u)} — ждёт первого сообщения боту, /deny {html.escape(u)}" for u in sorted(self.pending)]
        await self.send("\n".join(lines))

    async def welcome(self):
        if REPLY_CHAT.get() is not None:
            await self.send(GUEST_WELCOME, markup=GUEST_MENU)
            return
        await self.send("👋 <b>Бот P2P-связок на связи.</b>\n\n"
                        "Сам пришлю 🔔 карточку, когда появится связка выше порога. "
                        "Кнопки внизу: 🔥 лучшая связка сейчас, 📊 топ графиком, ⚙️ настройки, "
                        "🛠 как развивается бот, ❓ как работать.",
                        markup=MENU)

    async def start_onboarding(self):
        """Мастер первого «/start»: 3 шага кнопками (сумма → банки → порог сигнала) вместо сразу
        общего приветствия — дальше как обычно."""
        self.onboarding = {"step": "amount", "banks": set()}
        text, kb = onboarding_amount_view()
        await self.send(text, markup=kb)

    async def handle_onboarding(self, cq, data):
        """Обработка кнопки текущего шага онбординга; клик по кнопке чужого/уже пройденного шага — игнор."""
        ob = self.onboarding
        if ob is None:
            return
        mid = cq["message"]["message_id"]
        if data.startswith("onb_amt:") and ob["step"] == "amount":
            amount = parse_amount(data[len("onb_amt:"):])   # callback_data можно подделать — как в apply
            if amount is None:
                return
            self.cfg.amount = amount
            save_env("AMOUNT", f"{self.cfg.amount:.0f}")
            ob["step"] = "banks"
            text, kb = onboarding_banks_view(ob["banks"])
            await self.call("editMessageText", chat_id=self.chat_id, message_id=mid,
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data.startswith("onb_bank:") and ob["step"] == "banks":
            name = data[len("onb_bank:"):]
            if name not in ONBOARD_BANKS:   # только банки с кнопок: имя уходит в .env (INCLUDE_PAY)
                return
            ob["banks"].symmetric_difference_update({name})
            text, kb = onboarding_banks_view(ob["banks"])
            await self.call("editMessageText", chat_id=self.chat_id, message_id=mid,
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "onb_bank_next" and ob["step"] == "banks":
            self.cfg.include_pay = [b.lower() for b in ob["banks"]]
            save_env("INCLUDE_PAY", ",".join(self.cfg.include_pay))
            ob["step"] = "min"
            text, kb = onboarding_min_view()
            await self.call("editMessageText", chat_id=self.chat_id, message_id=mid,
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data.startswith("onb_min:") and ob["step"] == "min":
            v = parse_min_profit(data[len("onb_min:"):])
            if v is None:
                return
            self.cfg.min_profit = v
            save_env("MIN_PROFIT", f"{self.cfg.min_profit:g}")
            self.onboarding = None
            await self.call("editMessageText", chat_id=self.chat_id, message_id=mid, parse_mode="HTML",
                            text="✅ Готово! Сумму, банки и порог сигнала можно поменять в «⚙️ Настройки».")
            await self.welcome()

    async def on_callback(self, cq):
        data = cq.get("data", "")
        if data.startswith("onb_"):
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
            await self.handle_onboarding(cq, data)
            return
        if data.startswith("fav:"):
            await self.toggle_favorite(cq, data)
            return
        if data.startswith("trd_"):   # торговля: сюда доходит только владелец в личном чате (_owner_gate)
            await trading.wiring.callback(self, cq, data, save_env)
            return
        if data != "amt_custom":
            self.awaiting_amount = False   # любая другая кнопка сбрасывает ожидание суммы
        if not data.startswith("acc_add:"):
            self.awaiting_key = None       # любая другая кнопка прерывает ввод ключа
        if data != "preset_save":
            self.awaiting_preset_name = False   # любая другая кнопка прерывает ввод имени пресета
        if not data.endswith(":manual") or not data.startswith("fact:"):
            self.awaiting_fact = None      # любая другая кнопка прерывает ввод факта числом
        if not data.startswith("pay_to:"):
            self.awaiting_payout = None    # любая другая кнопка прерывает ввод суммы выплаты
        if data.startswith("pay_"):
            await self.payout_callback(cq, data)
            return
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
            await self.send(fmt_top(self.last, self.snap_cfg(self.last)) if self.last else WAIT)
        elif data == "settings":
            text, kb = self.settings_view()
            await self.send(text, markup=kb)
        elif data == "dev":
            text, kb = dev_view()
            await self.send(text, markup=kb)
        elif data == "status":
            text, kb = self.status_brief()
            await self.send(text, markup=kb)
        elif data == "status_full":
            await self.send(self.status_view())
        elif data.startswith("help:"):
            await self.open_help(cq, data[5:])
        elif data == "paper":
            await self.send(self.paper_view(), markup=self.paper_markup())
        elif data.startswith(("paper_set:", "paper_amt:")):
            key, value = data.split(":", 1)
            if key == "paper_set" and value in ("on", "off"):
                save_env("PAPER", "1" if value == "on" else "0")
            elif key == "paper_amt" and value in {str(a) for a in PAPER_AMOUNTS}:
                save_env("PAPER_AMOUNT", value)
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=self.paper_view(), parse_mode="HTML", reply_markup=self.paper_markup())
        elif data == "paper_report":
            await self.cmd_paper("report")
        elif data.startswith("paper_reset:"):
            # сообщение с вопросом заменяем ответом — кнопки пропадают; ответ только на последний вопрос и один раз
            mid = cq["message"]["message_id"]
            if mid == self.paper_reset_done:
                return   # повторное нажатие уже отвеченного вопроса — итог на месте, ничего не делаем
            if mid != self.paper_reset_ask:   # старый вопрос или бот перезапускался — не сбрасываем вслепую
                await self.call("editMessageText", chat_id=self.chat_id, message_id=mid, parse_mode="HTML",
                                text="🧪 Кнопка устарела — повтори /paper reset.")
                return
            self.paper_reset_ask, self.paper_reset_done = None, mid
            text = (self.paper_reset() if data == "paper_reset:yes"
                    else "🧪 Обнуление отменено — сухой прогон не тронут.")
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML")
        elif data.startswith("paper_ladder:"):
            amount = float(data[len("paper_ladder:"):])
            save_env("PAPER_AMOUNT", f"{amount:.0f}")   # на нажатие уже ответили выше — второй answerCallbackQuery не нужен
            await self.send(f"🧪 Сумма круга сухого прогона: {_money(amount)} ₽.")
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
            pid = data[len("preset_del:"):]
            presets.delete_preset(presets.name_by_id(pid, self.cfg) or pid)   # старые кнопки несут имя
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Пресет удалён")
            text, kb = presets_view(self.cfg)
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "amt_custom":
            self.awaiting_amount = True
            await self.send(f"Введи сумму круга текстом, например 20000 или 1,5 млн "
                            f"(от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
        elif data.startswith("favdel:"):
            # маршрут из кнопки; его уже нет (удалён раньше, кнопка из старого списка с номером) — ничего не трогаем
            gone = not favorites.remove(data[7:])
            text, kb = self.favorites_view()
            if gone:
                text = "Этого маршрута уже нет в избранном — список обновлён.\n\n" + text
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "mybanks":
            text, kb = mybanks_view()
            await self.send(text, markup=kb, topic="settings")
        elif data.startswith(("ownbank:", "sbplim:")):
            apply_mybanks(data)
            text, kb = mybanks_view()
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "accounts":
            t, kb = accounts_view(self.cfg)
            await self.send(t, markup=kb)
        elif data.startswith("acc_add:"):
            ex = data[8:]
            if ex not in accounts.ONBOARDABLE:   # ключ выплат (cryptomus_payout) и прочее — только локально на ПК
                return
            name = ACCOUNT_NAMES.get(ex, ex)
            first = KEY_STEPS.get(ex, KEY_STEPS_DEFAULT)[1]
            self.awaiting_key = {"ex": ex, "step": "key"}
            await self.send(f"{key_hint(ex, name)}\n"
                            f"Пришли <b>{first}</b> — сообщение с ним сразу удалю из чата.")
        elif data.startswith("acc_check:"):
            ex = data[10:]
            safe, detail = await accounts.key_permissions(self.s, ex)
            if safe is False and not allow_unsafe_keys():
                await self.drop_unsafe_key(ex, detail)
                t, kb = account_view(ex)
                await self.send(t, markup=kb)
            else:
                ok, msg = await accounts.verify(self.s, ex)
                accounts.set_verified(ex, verify_state(ok, safe), detail if ok and safe is False else msg)
                await self.send(f"✅ Ключ рабочий{readonly_note(safe, detail)}" if ok else f"⚠️ {html.escape(msg)}")
        elif data.startswith("acc_del:"):
            ex = data[8:]
            if accounts.delete_key(ex):
                await self.send("🗑 Ключ удалён" + env_key_hint(ex))
            else:
                await self.send("Ключ не был подключён")
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
        if self.awaiting_payout is not None:
            eid, self.awaiting_payout = self.awaiting_payout, None   # любая другая команда/кнопка — отмена
            if text not in BUTTONS and not text.startswith("/"):
                await self.payout_amount(eid, text)
                return
        self.awaiting_key = None               # команда/кнопка прерывает ввод ключа биржи
        cmd, _, arg = BUTTONS.get(text, text).partition(" ")
        await self.dispatch(cmd.split("@")[0], arg)

    async def dispatch(self, cmd, arg):
        """Команда → обработчик. Гостю (REPLY_CHAT задан) доступны только GUEST_CMDS."""
        if REPLY_CHAT.get() is not None and cmd not in GUEST_CMDS:
            await self.send(GUEST_DENIED)
            return
        if cmd == "/allow":
            await self.cmd_allow(arg)
        elif cmd == "/deny":
            await self.cmd_deny(arg)
        elif cmd == "/guests":
            await self.cmd_guests()
        elif cmd == "/start":
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
        elif cmd == "/signals":
            await self.send(self.signals_view(arg))
        elif cmd == "/paper":
            await self.cmd_paper(arg)
        # план 2.6: отчёт калибровки — только владельцу (не в GUEST_CMDS), за флагом CALIBRATION=1 (по умолчанию выкл.);
        # базы (прогон, журнал, снимки, история) читает в отдельном потоке
        elif cmd == "/calibration" and calibration.enabled():
            snap, ev_rank = self.last, self.cfg.ev_rank
            await self.send(await asyncio.to_thread(lambda: calibration.report_text(ev_rank=ev_rank, snap=snap)))
        elif cmd == "/funding":
            await self.send(simfunding.view())
        elif cmd == "/futures":   # и «/futures paper» — пока есть только бумага
            await self.send(simdirectional.view())
        elif cmd == "/fav":
            text, kb = self.favorites_view()
            await self.send(text, markup=kb)
        elif cmd == "/export":
            await self.cmd_export(arg)
        elif cmd == "/safety":
            await self.send(SAFETY)
        elif cmd == "/mybanks":
            text, kb = mybanks_view()
            await self.send(text, markup=kb, topic="settings")
        elif cmd == "/alert":
            if arg:
                await self.add_alert(arg)
            else:
                await self.send(ALERT_HELP)
        elif cmd == "/alerts":
            text, kb = alerts_view(self.chat_id)
            await self.send(text, markup=kb)
        elif cmd == "/blacklist":
            if arg.strip():
                await self.blacklist_note(arg)
            else:
                text, kb = blacklist_view()
                await self.send(text, markup=kb)
        elif cmd == "/traps":
            await self.send(traps_view())
            terms = terms_view()
            if terms:
                await self.send(terms)
        elif cmd == "/nets":
            await self.send(unmapped_nets_view())
        elif cmd == "/maker":
            await self.maker(arg)
        elif cmd == "/banks":
            await self.banks(arg)
        elif cmd == "/balance":
            await self.balance()
        elif cmd == "/payout":
            await self.cmd_payout(arg)
        elif cmd == "/fees":
            await self.send(fees.view(live=netstatus.live_fee))
        elif cmd == "/settings":
            text, kb = self.settings_view()
            await self.send(text, markup=kb)
        elif cmd == "/dev":
            text, kb = dev_view()
            await self.send(text, markup=kb)
        elif cmd == "/status":
            text, kb = self.status_brief()
            await self.send(text, markup=kb)
        elif cmd == "/logs":
            await self.send(logs_view(LOG_PATH))
        elif cmd == "/amount" and arg:
            amount = parse_amount(arg)
            if amount is None:
                await self.send(f"Не понял сумму. Пример: /amount 20000, /amount 1,5 млн "
                                f"(от {_money(AMOUNT_MIN)} до {_money(AMOUNT_MAX)} ₽).")
                return
            await self.send(self.apply(f"amt:{amount}"))
        elif cmd == "/min" and arg:
            v = parse_min_profit(arg)
            if v is None:
                await self.send(f"Не понял порог. Пример: /min 2, /min 1,5 "
                                f"(от {MIN_PROFIT_MIN:g} до {MIN_PROFIT_MAX:g}% чистыми).")
                return
            await self.send(self.apply(f"min:{v}"))
        elif cmd == "/pause":
            await self.cmd_pause(arg)
        elif cmd == "/resume":
            await self.cmd_resume()
        elif cmd == "/trading" and REPLY_CHAT.get() is None:   # только владелец (гость сюда и не доходит: GUEST_CMDS)
            await trading.wiring.command(self, arg)
        elif cmd == "/hedge" and REPLY_CHAT.get() is None:   # только владелец → trading.wiring.hedge_command (пин)
            await trading.wiring.hedge_command(self, arg)
        elif REPLY_CHAT.get() is not None:   # гостю — справка одним сообщением (кнопки разделов — у владельца)
            await self.send(GUIDE, markup=LINKS)
        else:   # /help и незнакомая команда — оглавление справки с кнопками разделов
            text, kb = help_view()
            await self.send(text, markup=kb)

    async def cmd_payout(self, arg):
        """/payout — выплата Cryptomus на адрес из белого списка; /payout history — последние 10 выплат.
        Только владелец: команда гостя (REPLY_CHAT задан) — отказ здесь же, а не только в dispatch."""
        if REPLY_CHAT.get() is not None:
            await self.send(GUEST_DENIED)
            return
        if arg.strip().lower() in ("history", "история"):
            await self.send(payout_history_view(payouts.history(10), payouts.used_today(), payouts.limits()[1]))
            return
        if payouts.credentials() is None:
            await self.send(PAYOUT_KEY_HINT)
            return
        if not payouts.enabled():
            await self.send(PAYOUT_OFF_HINT)
            return
        entries = payouts.load_whitelist()
        if not entries:
            await self.send(PAYOUT_WL_HINT)
            return
        text, kb = payout_menu_view(entries, payouts.used_today(), payouts.limits()[1])
        await self.send(text, markup=kb)

    def payout_stop(self):
        """«⛔ Стоп выплаты»: сначала PAYOUTS=0 в процессе и сброс предпросмотра — это не может не сработать, — потом
        PAYOUTS=0 в .env. Возвращает None или причину, по которой .env не записан. Включить обратно из Telegram нельзя."""
        payouts.disable()
        self.payout_preview = None
        self.awaiting_payout = None
        try:
            save_env("PAYOUTS", "0")
        except Exception as e:   # .env только для чтения, занят антивирусом/редактором и т. п.
            logger.error("⛔ Стоп выплат: PAYOUTS=0 в .env не записан: %s", type(e).__name__)
            return type(e).__name__
        return None

    async def payout_callback(self, cq, data):
        """Кнопки выплат: pay_to:<id> — получатель из белого списка, pay_ok/pay_no:<токен> — кнопки предпросмотра,
        pay_hist — история, pay_stop — выключить выплаты. Только сам владелец в своём личном чате (_owner_gate: тип чата,
        кто нажал) и не в контексте гостя — проверка здесь же, а не только в маршрутизации on_update."""
        if REPLY_CHAT.get() is not None or self._owner_gate(cq) != "owner":
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Только для владельца бота")
            return
        kind, _, arg = data.partition(":")
        if kind == "pay_stop":
            err = self.payout_stop()
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Выплаты выключены")
            await self.send(PAYOUT_STOPPED_NO_ENV.format(err=html.escape(err)) if err else PAYOUT_STOPPED)
        elif kind == "pay_hist":
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])
            await self.cmd_payout("history")
        elif kind == "pay_to":
            await self.payout_pick(cq, arg)
        elif kind in ("pay_ok", "pay_no"):
            await self.payout_confirm(cq, kind, arg)
        else:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"])

    async def payout_pick(self, cq, eid):
        entry = payouts.whitelist_entry(eid)
        why = ("Нет ключа выплат" if payouts.credentials() is None else "Выплаты выключены" if not payouts.enabled()
               else "Этого адреса нет в белом списке" if entry is None else "")
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=why)
        if why:
            return
        self.awaiting_payout = entry["id"]
        cur = entry["currency"]
        memo = f"Memo: <code>{html.escape(entry['memo'])}</code>\n" if entry["memo"] else ""
        await self.send(f"💸 {html.escape(entry['name'])}: <b>{cur} · {entry['network']}</b>\n"
                        f"Адрес: <code>{html.escape(entry['address'])}</code>\n{memo}"
                        f"Сколько {cur} отправить? Пришли число, до 8 знаков после точки, например 25 или 0.015. "
                        f"Получатель получит ровно эту сумму, комиссия Cryptomus спишется с баланса сверху.")

    async def payout_amount(self, eid, text):
        """Сумма выплаты текстом -> живая комиссия и лимиты (payouts.quote) -> экран проверки с одноразовой кнопкой.
        Только владелец: в контексте гостя (REPLY_CHAT) — ничего."""
        if REPLY_CHAT.get() is not None:
            return
        amount, why = payouts.parse_amount(text)
        if why:
            self.awaiting_payout = eid   # ждём сумму дальше; любая команда или кнопка — отмена
            await self.send(f"⚠️ {why}. Пришли сумму ещё раз или любую команду для отмены.")
            return
        entry = payouts.whitelist_entry(eid)
        if entry is None:
            await self.send("Этого адреса уже нет в белом списке, выплата не готовится.")
            return
        q, why = await payouts.quote(self.s, entry, amount)
        if why:
            await self.send(f"⛔ Выплата не готова: {html.escape(why)}.")
            return
        token = secrets.token_urlsafe(12)
        self.payout_preview = {"token": token, "entry": entry, "amount": amount, "quote": q, "ts": time.time()}
        text, kb = payout_preview_view(entry, amount, q, token)
        await self.send(text, markup=kb)

    async def payout_confirm(self, cq, kind, token):
        """«✅ Отправить»/«Отмена» под предпросмотром. Кнопка одноразовая: токен снимается до любого запроса, так что
        второе нажатие, повтор колбэка или старая кнопка ничего не отправят; через TOKEN_TTL и после «⛔ Стоп» — тоже."""
        p = self.payout_preview
        if not p or not token or p["token"] != token:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"],
                            text="Кнопка устарела или уже нажата, ничего не отправлено")
            await self.drop_buttons(cq)
            return
        self.payout_preview = None
        await self.drop_buttons(cq)
        if kind == "pay_no":
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Отменено")
            await self.send("Выплата отменена, ничего не отправлено.")
            return
        if time.time() - p["ts"] > payouts.TOKEN_TTL:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Подтверждение истекло")
            await self.send(f"⌛ Подтверждение истекло ({payouts.TOKEN_TTL} с), ничего не отправлено. "
                            f"Начни заново: /payout")
            return
        if not payouts.enabled():
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Выплаты выключены")
            await self.send(f"⛔ Ничего не отправлено: {payouts.OFF}.")
            return
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Отправляю…")
        # в фоне: отправка с разбором неясного исхода может идти минуты (таймауты, /info, повторы), а command_loop
        # обрабатывает обновления по одному — «⛔ Стоп» иначе дошёл бы только после всех повторов
        self.payout_task = asyncio.create_task(self.payout_send(p, self.thread_for(None)))

    async def payout_send(self, p, thread):
        """Фоновая отправка подтверждённой выплаты (одна за раз — замок в payouts.send) и итог владельцу."""
        try:
            res = await payouts.send(self.s, p["entry"], p["amount"], p["quote"])
            text = payout_result_text(res)
        except Exception as e:
            logger.error("выплата: сбой отправки: %s", accounts.api_error_text(e))
            text = ("⚠️ Сбой бота при отправке выплаты — исход неясен. Не повторяй её вслепую: проверь /payout history "
                    "и кабинет Cryptomus. В дневном лимите она учтена.")
        await self.send(text, thread=thread)

    async def drop_buttons(self, cq):
        mid = (cq.get("message") or {}).get("message_id")
        if mid is not None:
            await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=mid,
                            reply_markup={"inline_keyboard": []})

    async def payouts_loop(self):
        """Выплаты: сообщить о прерванных перезапуском, дальше раз в ~30 с опрос незавершённых — итог в «📒 Журнал»."""
        while True:
            if self.chat_id:
                try:
                    while self.resumed_payouts:
                        await self.send(payout_event_text("resumed", self.resumed_payouts[0]), topic="journal")
                        self.resumed_payouts.pop(0)
                    for event, row in await payouts.poll(self.s):
                        await self.send(payout_event_text(event, row), topic="journal")
                except Exception as e:
                    logger.error("payouts_loop error: %s", accounts.api_error_text(e))
            await asyncio.sleep(payouts.POLL_INTERVAL)

    async def drop_unsafe_key(self, ex, detail):
        """Ключ даёт больше, чем чтение: удалить его и попросить новый read-only (у бирж без read-only ключей —
        объяснить, что подключить их можно только с ALLOW_UNSAFE_KEYS=1)."""
        accounts.delete_key(ex)
        name = ACCOUNT_NAMES.get(ex, ex)
        advice = (f"Ключей только для чтения у {name} нет: подключить его можно, только если осознанно оставить ключ "
                  "с правами сверх чтения — ALLOW_UNSAFE_KEYS=1 в .env (бот всё равно делает только запросы на чтение)."
                  if ex in accounts.NO_READONLY_KEYS else
                  "Создай новый ключ ТОЛЬКО для чтения и подключи заново: «⚙️ Настройки → 🔑 Мои биржи».")
        await self.send(f"⚠️ {name}: ключ даёт больше, чем чтение ({html.escape(detail)}) — удалил его из бота.\n"
                        + advice + env_key_hint(ex))

    async def check_key_safety(self, periodic=False):
        """При старте и раз в KEY_RECHECK_HOURS (periodic=True, из accounts_loop): если сохранённый ключ биржи даёт
        торговать/выводить — удалить его и попросить read-only. Не удалось проверить — ключ не трогаем, как при старте."""
        self.key_checked_ts = time.time()
        for ex in accounts.ONBOARDABLE:
            if accounts.keys(ex) is None:
                continue
            safe, detail = await accounts.api_permissions(self.s, ex)
            if not safe and allow_unsafe_keys():   # владелец оставил ключ сознательно — без удаления и без спама
                if periodic and accounts.verify_status(ex) == ("unsafe", detail):
                    continue                        # то же, что уже знаем: не писать в лог каждый час
                accounts.set_verified(ex, "unsafe", detail)
                logger.warning("%s: ключ даёт больше, чем чтение (%s) — оставлен, ALLOW_UNSAFE_KEYS=1", ex, detail)
            elif not safe:
                await self.drop_unsafe_key(ex, detail)

    async def setup(self):
        for method, params in (("setMyCommands", {"commands": COMMANDS}),
                               ("setMyDescription", {"description": DESCRIPTION}),
                               ("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})):
            try:
                r = await self.call(method, **params)
                if not r.get("ok"):
                    logger.warning("%s: %s", method, r.get("description"))
            except Exception as e:
                logger.warning("%s: %s", method, accounts.api_error_text(e))   # без URL с токеном бота


async def main():
    setup_logging()
    load_env()
    payouts.switch_from_file(ENV_PATH)   # выключатель выплат — только из .env: PAYOUTS=1 извне его не перебьёт
    trading.switch.switch_from_file(ENV_PATH)   # торговля: TRADING и TRADING_MODE — только из .env, извне не поднять
    trading.gates.flags_from_file(ENV_PATH)     # флаг владельца TRADING_SHORT_PAPER — тоже только из файла .env
    token = os.getenv("TG_TOKEN", "").strip()
    if not token:
        raise SystemExit("TG_TOKEN не задан: создай бота у @BotFather и пропиши токен в .env")
    cfg = Config.from_env()
    try:   # ключи от прошлой версии лежат открыто — шифруем (DPAPI); сбой не мешает запуску
        if accounts.encrypt_saved_keys():
            logger.info("ключи бирж в data/keys.json зашифрованы (Windows DPAPI)")
    except Exception as e:
        logger.warning("шифрование ключей: %s", type(e).__name__)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        bot = Bot(s, token, os.getenv("TG_CHAT_ID", "").strip(), cfg)
        bot.resumed_payouts = payouts.resume()   # до первого опроса: прерванные отправки — в «исход неясен»
        await bot.setup()
        if bot.chat_id:
            await bot.setup_topics()
            await bot.check_key_safety()
        bot.perp_task = asyncio.ensure_future(bot.perp_loop())   # публичные данные перпов — своим циклом (perp.py)
        # торговое ядро: старт (ключи у бирж, предупреждения — одно сообщение) и сверка — своей задачей, скан не ждёт
        bot.trading_task = asyncio.ensure_future(trading.wiring.run(bot))
        bot.watchdog_task = asyncio.ensure_future(bot.watchdog_loop())   # «скан стоит» — своей задачей
        logger.info("Бот запущен: каждые %ss, порог %g%%, биржи %s", cfg.interval, cfg.min_profit,
                    ', '.join(cfg.exchanges))
        await asyncio.gather(bot.scan_loop(), bot.command_loop(), bot.accounts_loop(), bot.payouts_loop())


if __name__ == "__main__":
    asyncio.run(main())
