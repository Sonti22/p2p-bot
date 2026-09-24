"""Telegram-бот сигналов P2P-связок: карточки-картинки, кнопки, меню. Запуск: python bot.py (настройки в .env)."""
import asyncio
import dataclasses
import html
import json
import os
import re
import time

import aiohttp

import accounts
import trades
from cards import deal_card, portfolio_card, top_chart
from p2p import AMOUNT_MAX, AMOUNT_MIN, ENV_PATH, Config, _money, deal_amounts, fmt_deal, fmt_top, load_env, \
    parse_amount, reliability, scan, spot_url, venue_url

MENU = {"keyboard": [[{"text": "🔥 Лучшая сейчас"}, {"text": "📊 Топ связок"}],
                     [{"text": "⚙️ Настройки"}, {"text": "🛠 Разработка"}],
                     [{"text": "❓ Как работать"}]],
        "resize_keyboard": True, "is_persistent": True}
BUTTONS = {"🔥 Лучшая сейчас": "/best", "📊 Топ связок": "/top", "⚙️ Настройки": "/settings",
           "🛠 Разработка": "/dev", "❓ Как работать": "/help"}
COMMANDS = [{"command": "best", "description": "Лучшая связка сейчас"},
            {"command": "top", "description": "Топ связок графиком"},
            {"command": "calc", "description": "Разовый расчёт под сумму, напр. /calc 20000"},
            {"command": "stats", "description": "Журнал сделок: день/неделя/месяц"},
            {"command": "balance", "description": "Баланс по подключённым биржам"},
            {"command": "settings", "description": "Порог, сумма, пауза"},
            {"command": "dev", "description": "Как развивается бот: версия, изменения, план"},
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


def deal_markup(d, deal_id=None):
    _, b, s, route = d
    row = [{"text": f"{label} · {ad.ex}", "url": venue_url(ad)}
           for ad, label in ((b, "🟢 Купить"), (s, "🔴 Продать")) if venue_url(ad)]
    rows = [row] if row else []
    m = re.search(r"спот (\w+)→(\w+) на (\w+)", route)
    if m and spot_url(route):
        rows.append([{"text": f"🔁 Спот {m.group(1)}→{m.group(2)} · {m.group(3)}", "url": spot_url(route)}])
    if deal_id is not None:
        rows.append([{"text": "✅ Сделал", "callback_data": f"did:{deal_id}"}])
    rows.append([{"text": "📊 Все связки", "callback_data": "top"}, {"text": "🔄 Обновить", "callback_data": "best"}])
    return {"inline_keyboard": rows}


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


def dev_view(status_path=DEV_STATUS, roadmap_path=os.path.join(HERE, "ROADMAP.md")):
    """Текст и кнопки раздела «🛠 Разработка»."""
    try:
        with open(status_path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
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
    rows.append([{"text": "🔄 Обновить", "callback_data": "dev"}])
    return "\n".join(lines), {"inline_keyboard": rows}


TOP_MARKUP = {"inline_keyboard": [
    [{"text": "🔄 Обновить", "callback_data": "top"}, {"text": "📄 Подробно", "callback_data": "detail"}],
    [{"text": "🔥 Лучшая", "callback_data": "best"}, {"text": "⚙️ Настройки", "callback_data": "settings"}]]}


class Bot:
    def __init__(self, session, token, chat_id, cfg):
        self.s, self.token, self.chat_id, self.cfg = session, token, chat_id, cfg
        self.cooldown = int(os.getenv("COOLDOWN", 600))      # сек: не повторять ту же пару бирж
        self.repeat_step = float(os.getenv("REPEAT_STEP", 0.3))  # п.п. роста профита для досрочного повтора
        self.max_signals = int(os.getenv("MAX_SIGNALS", 3))      # сигналим только из топ-N
        self.last = None
        self.paused = False
        self.awaiting_amount = False  # ждём сумму текстом после «✏️ Своя сумма»
        self.awaiting_key = None      # {"ex":.., "step": "key"/"secret", "key":..} — ждём ключ биржи
        self.sent = {}
        self.venue = {}   # ex -> {"streak": сканов подряд с ошибкой, "down_since": ts, "alerted_at": ts}
        self.deals_by_id = {}   # id -> (d, сумма круга) для кнопки «✅ Сделал»; не переживает рестарт
        self.next_deal_id = 1

    async def call(self, method, **params):
        async with self.s.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                               timeout=aiohttp.ClientTimeout(total=40)) as r:
            return await r.json()

    async def send(self, text, chat_id=None, markup=None):
        params = dict(chat_id=chat_id or self.chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        if markup:
            params["reply_markup"] = markup
        return await self.call("sendMessage", **params)

    async def send_photo(self, png, caption, markup=None):
        form = aiohttp.FormData()
        form.add_field("chat_id", str(self.chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        if markup:
            form.add_field("reply_markup", json.dumps(markup))
        form.add_field("photo", png, filename="card.png", content_type="image/png")
        async with self.s.post(f"https://api.telegram.org/bot{self.token}/sendPhoto", data=form,
                               timeout=aiohttp.ClientTimeout(total=40)) as r:
            return await r.json()

    async def photo_or_text(self, render, caption, markup):
        """Картинка с подписью; если не вышло — тем же текстом."""
        if len(caption) <= 1024:
            try:
                r = await self.send_photo(await asyncio.to_thread(render), caption, markup)
                if r.get("ok"):
                    return
                print("sendPhoto:", r.get("description"))
            except Exception as e:
                print("card error:", e)
        await self.send(caption, markup=markup)

    def remember_deal(self, d, cfg=None):
        """Запомнить связку под кнопкой «✅ Сделал»; хранится ограниченное число последних."""
        cfg = cfg or self.cfg
        deal_id, self.next_deal_id = self.next_deal_id, self.next_deal_id + 1
        self.deals_by_id[deal_id] = (d, cfg.amount)
        if len(self.deals_by_id) > 200:
            del self.deals_by_id[min(self.deals_by_id)]
        return deal_id

    async def send_deal(self, d, prefix="", cfg=None, snap=None):
        cfg = cfg or self.cfg
        snap = snap if snap is not None else self.last
        deal_id = self.remember_deal(d, cfg)
        amounts = deal_amounts(d, cfg, snap) if snap else None
        rel = reliability(d, cfg, snap) if snap else None
        await self.photo_or_text(lambda: deal_card(d, cfg, amounts, rel), prefix + fmt_deal(d, cfg, snap),
                                 deal_markup(d, deal_id))

    async def mark_done(self, cq, deal_id):
        """Кнопка «✅ Сделал»: записать сделку в журнал (data/trades.db) и убрать кнопку."""
        entry = self.deals_by_id.pop(deal_id, None)
        if not entry:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Сигнал устарел, не записан")
            return
        d, amount = entry
        bank, total, crossed = trades.log_trade(d, amount)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Записано в журнал ✅")
        await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                        reply_markup=deal_markup(d))
        if crossed:
            await self.send(f"⚠️ Через {bank} по СБП в этом месяце отправлено {_money(total)} ₽ — выше "
                            f"бесплатного лимита 100 000 ₽, дальше банк может взять комиссию до 0.5%. "
                            f"Для следующих сделок с этим мерчантом лучше выбрать другой банк.")

    def stats_view(self):
        st = trades.stats()
        labels = (("day", "За сегодня"), ("week", "За неделю"), ("month", "За месяц"))
        lines = ["📒 <b>Журнал сделок</b>", ""]
        for key, label in labels:
            s = st[key]
            if s["count"]:
                lines.append(f"{label}: {s['count']} сделок, оборот {_money(s['amount'])} ₽, "
                             f"средний профит {s['avg_profit']:+.2f}%")
            else:
                lines.append(f"{label}: сделок нет")
        lines.append("\nОтмечай связку кнопкой «✅ Сделал» под сигналом — так она попадёт в журнал.")
        return "\n".join(lines)

    async def show_best(self, snap=None, cfg=None):
        snap = self.last if snap is None else snap
        cfg = cfg or self.cfg
        if not snap:
            await self.send(WAIT)
        elif not snap.deals:
            await self.send("Связок сейчас нет: все объявления отсеяны фильтрами.")
        else:
            await self.send_deal(snap.deals[0], "🔥 ", cfg, snap)

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
        status = "⏸ пауза сигналов" if self.paused else f"▶️ сканирую каждые {c.interval} с"
        text = (f"⚙️ <b>Настройки</b>\n\nПорог сигнала: <b>{c.min_profit:g}%</b> (1-я строка кнопок)\n"
                f"Сумма круга: <b>{_money(c.amount)} ₽</b> (2-я строка)\nСтатус: {status}\n\n"
                f"Монеты: {', '.join(c.assets)}\nПлощадки: {', '.join(c.exchanges)}")
        mark = lambda on, t: ("✅ " if on else "") + t
        kb = [[{"text": mark(c.min_profit == v, f"{v}%"), "callback_data": f"min:{v}"} for v in MIN_PRESETS],
              [{"text": mark(c.amount == v, f"{v // 1000}к"), "callback_data": f"amt:{v}"} for v in AMOUNT_PRESETS],
              [{"text": "✏️ Своя сумма", "callback_data": "amt_custom"}],
              [{"text": "🔑 Мои биржи", "callback_data": "accounts"}],
              [{"text": "▶️ Возобновить" if self.paused else "⏸ Пауза", "callback_data": "resume" if self.paused else "pause"}]]
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
            return "Сигналы на паузе" if self.paused else "Сигналы включены"
        return ""

    async def scan_loop(self):
        while True:
            try:
                self.last = await scan(self.s, self.cfg)
                if self.chat_id:
                    await self.check_venues(self.last)
                    if not self.paused:
                        await self.notify(self.last)
            except Exception as e:
                print("scan error:", e)
            await asyncio.sleep(self.cfg.interval)

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
                    await self.send(f"⚠️ {ex}: недоступна ({failed[ex]})")
            else:
                if st["alerted_at"]:
                    await self.send(f"✅ {ex}: снова доступна")
                st.update(streak=0, down_since=None, alerted_at=None)

    async def notify(self, snap):
        now = time.time()
        for d in snap.deals[:self.max_signals]:   # только топ-N: новые сигналы, когда меняется верх списка
            profit, b, s, _ = d
            if profit < self.cfg.min_profit:
                break
            key = (b.ex, b.asset, s.ex, s.asset)
            prev = self.sent.get(key)
            if prev and now - prev[0] < self.cooldown and profit < prev[1] + self.repeat_step:
                continue
            self.sent[key] = (now, profit)
            await self.send_deal(d, "🔔 ", snap=snap)

    async def command_loop(self):
        offset = 0
        while True:
            try:
                r = await self.call("getUpdates", offset=offset, timeout=30)
            except Exception as e:
                print("getUpdates error:", e)
                await asyncio.sleep(5)
                continue
            if not r.get("ok"):
                print("Telegram:", r.get("description"))
                await asyncio.sleep(10)
                continue
            for u in r["result"]:
                offset = u["update_id"] + 1
                try:
                    await self.on_update(u)
                except Exception as e:
                    print("update error:", e)

    async def on_update(self, u):
        cq = u.get("callback_query")
        if cq:
            if str(cq.get("message", {}).get("chat", {}).get("id", "")) == self.chat_id:
                await self.on_callback(cq)
            return
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if not chat:
            return
        if not self.chat_id:
            # первый написавший чат становится получателем сигналов
            self.chat_id = chat
            save_env("TG_CHAT_ID", chat)
            print("chat_id сохранён в .env:", chat)
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
        toast = self.apply(data)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=toast)
        if toast:
            text, kb = self.settings_view()
            await self.call("editMessageText", chat_id=self.chat_id, message_id=cq["message"]["message_id"],
                            text=text, parse_mode="HTML", reply_markup=kb)
        elif data == "best":
            await self.show_best()
        elif data == "top":
            await self.show_top()
        elif data == "detail":
            await self.send(fmt_top(self.last, self.cfg) if self.last else WAIT)
        elif data == "settings":
            text, kb = self.settings_view()
            await self.send(text, markup=kb)
        elif data == "dev":
            text, kb = dev_view()
            await self.send(text, markup=kb)
        elif data == "balance":
            await self.balance()
        elif data.startswith("did:"):
            await self.mark_done(cq, int(data[4:]))
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
        if self.awaiting_amount:
            self.awaiting_amount = False       # любая другая команда/кнопка тоже сбрасывает ожидание
            if text not in BUTTONS and not text.startswith("/"):
                await self.set_custom_amount(text)
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
        elif cmd == "/calc":
            if arg:
                await self.calc(arg)
            else:
                await self.send("Нужна сумма: /calc 20000")
        elif cmd == "/stats":
            await self.send(self.stats_view())
        elif cmd == "/balance":
            await self.balance()
        elif cmd == "/settings":
            text, kb = self.settings_view()
            await self.send(text, markup=kb)
        elif cmd == "/dev":
            text, kb = dev_view()
            await self.send(text, markup=kb)
        elif cmd in ("/min", "/amount") and arg:
            try:
                v = float(arg.replace(",", ".").replace(" ", ""))
            except ValueError:
                await self.send("Нужно число.")
                return
            await self.send(self.apply(f"{'min' if cmd == '/min' else 'amt'}:{v}"))
        elif cmd in ("/pause", "/resume"):
            await self.send(self.apply(cmd[1:]))
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
                    print(method, r.get("description"))
            except Exception as e:
                print(method, e)


async def main():
    load_env()
    token = os.getenv("TG_TOKEN", "").strip()
    if not token:
        raise SystemExit("TG_TOKEN не задан: создай бота у @BotFather и пропиши токен в .env")
    cfg = Config.from_env()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        bot = Bot(s, token, os.getenv("TG_CHAT_ID", "").strip(), cfg)
        await bot.setup()
        if bot.chat_id:
            await bot.check_key_safety()
        print(f"Бот запущен: каждые {cfg.interval}s, порог {cfg.min_profit:g}%, биржи {', '.join(cfg.exchanges)}")
        await asyncio.gather(bot.scan_loop(), bot.command_loop())


if __name__ == "__main__":
    asyncio.run(main())
