"""Telegram-бот сигналов P2P-связок: карточки-картинки, кнопки, меню. Запуск: python bot.py (настройки в .env)."""
import asyncio
import json
import os
import re
import time

import aiohttp

from cards import deal_card, top_chart
from p2p import ENV_PATH, Config, _money, fmt_deal, fmt_top, load_env, scan, spot_url, venue_url

MENU = {"keyboard": [[{"text": "🔥 Лучшая сейчас"}, {"text": "📊 Топ связок"}],
                     [{"text": "⚙️ Настройки"}, {"text": "❓ Как работать"}]],
        "resize_keyboard": True, "is_persistent": True}
BUTTONS = {"🔥 Лучшая сейчас": "/best", "📊 Топ связок": "/top", "⚙️ Настройки": "/settings", "❓ Как работать": "/help"}
COMMANDS = [{"command": "best", "description": "Лучшая связка сейчас"},
            {"command": "top", "description": "Топ связок графиком"},
            {"command": "settings", "description": "Порог, сумма, пауза"},
            {"command": "help", "description": "Как работать с сигналами"}]
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


def deal_markup(d):
    _, b, s, route = d
    row = [{"text": f"{label} · {ad.ex}", "url": venue_url(ad)}
           for ad, label in ((b, "🟢 Купить"), (s, "🔴 Продать")) if venue_url(ad)]
    rows = [row] if row else []
    m = re.search(r"спот (\w+)→(\w+) на (\w+)", route)
    if m and spot_url(route):
        rows.append([{"text": f"🔁 Спот {m.group(1)}→{m.group(2)} · {m.group(3)}", "url": spot_url(route)}])
    rows.append([{"text": "📊 Все связки", "callback_data": "top"}, {"text": "🔄 Обновить", "callback_data": "best"}])
    return {"inline_keyboard": rows}


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
        self.sent = {}

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

    async def send_deal(self, d, prefix=""):
        await self.photo_or_text(lambda: deal_card(d, self.cfg), prefix + fmt_deal(d, self.cfg), deal_markup(d))

    async def show_best(self):
        if not self.last:
            await self.send(WAIT)
        elif not self.last.deals:
            await self.send("Связок сейчас нет: все объявления отсеяны фильтрами.")
        else:
            await self.send_deal(self.last.deals[0], "🔥 ")

    async def show_top(self):
        snap = self.last
        if not snap:
            await self.send(WAIT)
            return
        nets = sorted(((v["sell"].price, net) for net, v in snap.networks.items() if v.get("sell")), reverse=True)[:3]
        caption = (f"📊 <b>Топ связок</b> · USDT {snap.ref:.2f} ₽ · круг {_money(self.cfg.amount)} ₽\n"
                   + (f"Лучше продать USDT обменнику: {', '.join(f'{n} {p:.2f}' for p, n in nets)}\n" if nets else "")
                   + f"Связок всего: {len(snap.deals)} · от {self.cfg.min_profit:g}%: "
                   + f"{sum(1 for d in snap.deals if d[0] >= self.cfg.min_profit)}")
        await self.photo_or_text(lambda: top_chart(snap, self.cfg), caption, TOP_MARKUP)

    def settings_view(self):
        c = self.cfg
        status = "⏸ пауза сигналов" if self.paused else f"▶️ сканирую каждые {c.interval} с"
        text = (f"⚙️ <b>Настройки</b>\n\nПорог сигнала: <b>{c.min_profit:g}%</b> (1-я строка кнопок)\n"
                f"Сумма круга: <b>{_money(c.amount)} ₽</b> (2-я строка)\nСтатус: {status}\n\n"
                f"Монеты: {', '.join(c.assets)}\nПлощадки: {', '.join(c.exchanges)}")
        mark = lambda on, t: ("✅ " if on else "") + t
        kb = [[{"text": mark(c.min_profit == v, f"{v}%"), "callback_data": f"min:{v}"} for v in MIN_PRESETS],
              [{"text": mark(c.amount == v, f"{v // 1000}к"), "callback_data": f"amt:{v}"} for v in AMOUNT_PRESETS],
              [{"text": "▶️ Возобновить" if self.paused else "⏸ Пауза", "callback_data": "resume" if self.paused else "pause"}]]
        return text, {"inline_keyboard": kb}

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
                if self.chat_id and not self.paused:
                    await self.notify(self.last)
            except Exception as e:
                print("scan error:", e)
            await asyncio.sleep(self.cfg.interval)

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
            await self.send_deal(d, "🔔 ")

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
            await self.handle((msg.get("text") or "").strip())

    async def welcome(self):
        await self.send("👋 <b>Бот P2P-связок на связи.</b>\n\n"
                        "Сам пришлю 🔔 карточку, когда появится связка выше порога. "
                        "Кнопки внизу: 🔥 лучшая связка сейчас, 📊 топ графиком, ⚙️ настройки, ❓ как работать.",
                        markup=MENU)

    async def on_callback(self, cq):
        data = cq.get("data", "")
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

    async def handle(self, text):
        cmd, _, arg = BUTTONS.get(text, text).partition(" ")
        cmd = cmd.split("@")[0]
        if cmd == "/start":
            await self.welcome()
        elif cmd == "/best":
            await self.show_best()
        elif cmd == "/top":
            await self.show_top()
        elif cmd == "/settings":
            text, kb = self.settings_view()
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
        print(f"Бот запущен: каждые {cfg.interval}s, порог {cfg.min_profit:g}%, биржи {', '.join(cfg.exchanges)}")
        await asyncio.gather(bot.scan_loop(), bot.command_loop())


if __name__ == "__main__":
    asyncio.run(main())
