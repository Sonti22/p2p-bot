"""Подключение торгового ядра к Telegram-боту (bot.py) — без стратегий и без автоматических ордеров.

Решение владельца: торговля на ОСНОВНЫХ аккаунтах Bybit и BingX отдельными торговыми ключами без вывода и переводов;
ручные позиции и ордера владельца бот не трогает (проверки владения — в journal/ownership).

- `startup` — при старте: выключатель и режим уже взяты из .env (bot.main → switch.switch_from_file); journal.resume;
  проверка прав торговых ключей у самих бирж (keys.startup_check). Ключ с лишними правами (вывод, переводы…) — торговля
  выключена (TRADING=0 в процессе); TRADING=1, а годного ключа нет — тоже. Режим маржи (Bybit — на аккаунт) и
  risk.startup_warnings — одним сообщением владельцу.
- `loop` — отдельной задачей бота: раз в INTERVAL с journal.reconcile (если есть проверенные ключи), дневной стоп —
  событием, доставка outbox владельцу понятным текстом (доставленным событие помечается только после ok Telegram),
  trading_state.json для launcher. Сбой ядра бота не роняет: лог, одна тревога, пауза FAIL_PAUSE.
- `command` (/trading) и `callback` (кнопки trd_*) — только владельцу: их вызывает bot.py после _owner_gate (личный чат
  владельца); гостю и группе они недоступны.
- `request_order` — единственный путь реального открытия из бота: карточка владельцу с точным ордером и кнопками
  trd_ok/trd_no. Токен одноразовый, живёт TOKEN_TTL, привязан к намерению (ордер, стратегия, группа) и к сообщению с
  кнопками. По «✅» — journal.submit фоновой задачей (ядро само ещё раз проверяет выключатель, режим, ключ, лимиты и
  чужое). Стратегий здесь нет: request_order никто в боте пока не вызывает.
- «⛔ Стоп торговли» — switch.stop_and_persist (TRADING=0, TRADING_MODE=paper в .env) и отмена всех висящих токенов.
  Режим из Telegram — только понизить (switch.lower_and_persist).
"""
import asyncio
import html
import logging
import os
import secrets
import time
from decimal import Decimal

from trading import journal, keys, risk, switch, venues

logger = logging.getLogger(__name__)

INTERVAL = 30          # сек между сверками (journal.reconcile)
FAIL_PAUSE = 300       # сек паузы после сбоя ядра
TOKEN_TTL = 120        # сек жизни кнопки подтверждения ордера
VENUE_NAMES = {venues.BYBIT: "Bybit", venues.BINGX: "BingX"}
MODE_NAMES = {"paper": "бумага (реальных ордеров нет)", "minlot": "минимальный лот, по кнопке",
              "confirm": "по кнопке", "auto": "автомат"}
# события outbox ядра → (значок, что случилось) для владельца
EVENTS = {
    "unknown": ("❓", "исход ордера неясен — новые открытия запрещены, пока сверка или вы не разберёте ордер"),
    "mismatch": ("⚠️", "ответ биржи не совпал с намерением бота — ордер в разборе, открытия запрещены"),
    "notfound": ("❓", "биржа не находит ордер бота по его id"),
    "rejected": ("↩️", "биржа отклонила ордер"),
    "resolved": ("✅", "исход ордера выяснен"),
    "closed_by_venue": ("⚠️", "позицию бота закрыла биржа (ликвидация, ADL) или вы вручную"),
    "settled": ("🧾", "результат закрытия биржей уточнён"),
    "overfill": ("⚠️", "исполнено больше, чем заказано"),
    "spot_dust": ("🧹", "остаток спота меньше минимума биржи — списан"),
    "foreign": ("👤", "на символе ваша ручная позиция или ордер — бот их не трогает, свои висящие ордера снял"),
    "stop_missing": ("🛑", "позиция бота без стопа"),
    "stop_removed": ("🛑", "стоп бота снят"),
    "stop_breached": ("🛑", "цена за стопом — бот закрывает позицию рынком (программный стоп)"),
    "reconcile_error": ("⚠️", "сверка ордера с биржей не удалась"),
    "watch_error": ("⚠️", "сверка позиций бота с биржей не удалась"),
    "day_stop": ("⛔", "дневной лимит убытка достигнут — новых открытий до конца дня (МСК) не будет"),
}


class Link:
    """Состояние подключения в процессе бота (bot.trading_link)."""

    def __init__(self):
        self.warnings = []        # предупреждения старта — для /trading
        self.checks = {}          # {биржа: keys.KeyCheck} последней проверки при старте
        self.tokens = {}          # токен -> {"order", "strategy", "group", "expires", "message_id"}
        self.alarmed = False      # тревога о сбое ядра уже ушла (до первой удачной сверки)
        self.tasks = set()        # фоновые отправки подтверждённых ордеров


def link_of(bot):
    link = getattr(bot, "trading_link", None)
    if link is None:
        link = bot.trading_link = Link()
    return link


def creds_for(venue):
    """Ключ биржи, только если его последняя проверка прав прошла и свежая (keys.credentials)."""
    return keys.credentials(venue)


def has_keys():
    return any(creds_for(v) for v in venues.VENUES)


def _esc(v):
    return html.escape(str(v))


# --- старт ---

async def _margin_modes(s, usable):
    out = {}
    for v in usable:
        sym = venues.CANDIDATES[(v, "linear" if v == venues.BYBIT else "swap")]["BTCUSDT"][0]
        try:
            mm, why = await venues.margin_mode(s, v, sym, creds_for(v))
        except Exception as e:   # noqa: BLE001 — режим не узнали: открытия и так запрещены (risk)
            mm, why = None, type(e).__name__
        out[v] = mm if mm else f"не прочитан: {why}"
    return out


async def startup(bot):
    """Старт подключения; → текст сообщения владельцу (он же отправляется, если есть чат) или ""."""
    link = link_of(bot)
    try:
        journal.resume()
    except Exception as e:   # noqa: BLE001 — база занята/испорчена: торговля выключена, бот работает
        switch.disable()
        link.warnings.append(f"журнал ордеров не открылся ({type(e).__name__}) — торговля выключена")
    have = [v for v in venues.VENUES if keys.raw_credentials(v)]
    lines = []
    if have:
        try:
            link.checks = {v: kc for v, kc in (await keys.startup_check(bot.s)).items() if v in have}
        except Exception as e:   # noqa: BLE001
            link.checks = {v: keys.KeyCheck(False, "unknown", f"проверка не удалась: {type(e).__name__}", None)
                           for v in have}
    unsafe = [v for v, kc in link.checks.items() if kc.state == "unsafe"]
    usable = [v for v, kc in link.checks.items() if kc.ok]
    if unsafe:
        switch.disable()
        lines.append("⛔ У торгового ключа " + ", ".join(VENUE_NAMES[v] for v in unsafe) + " есть права сверх "
                     "торговли (вывод, переводы…) — торговля выключена. Создайте ключ только с торговлей и "
                     "проверьте его на ПК: python scripts/trading_keys.py check.")
    elif switch.enabled() and not usable:
        switch.disable()
        lines.append("⛔ TRADING=1, но проверенного торгового ключа нет — торговля выключена. Ключ вводится на ПК: "
                     "python scripts/trading_keys.py.")
    link.warnings += risk.startup_warnings(await _margin_modes(bot.s, usable), link.checks)
    if not (have or switch.enabled() or lines or link.warnings):
        return ""
    head = f"🤖 <b>Торговое ядро</b>: {status_line()}"
    text = "\n".join([head] + lines + [f"• {_esc(w)}" for w in link.warnings])
    if bot.chat_id:
        await bot.send(text)
    return text


def status_line():
    ok, why = switch.can_open()
    mode = switch.mode()
    return (f"торговля {'включена' if switch.enabled() else 'выключена'}, режим {mode} ({MODE_NAMES[mode]})"
            + ("" if ok else f" — {_esc(why)}"))


# --- цикл сверки и доставка событий ---

def event_text(ev):
    icon, what = EVENTS.get(ev["event"], ("ℹ️", f"событие ядра «{ev['event']}»"))
    text = f"{icon} <b>Торговля</b>: {what}"
    if ev["note"]:
        text += f"\n{_esc(ev['note'])}"
    if ev["client_id"]:
        text += f"\nордер бота <code>{_esc(ev['client_id'])}</code>"
    return text


async def deliver(bot, limit=20):
    """Недоставленные события outbox → владельцу; доставленным помечается только событие, на которое Telegram ответил
    ok. Первая неудача — стоп до следующего цикла (порядок событий сохраняется). → сколько доставлено."""
    if not bot.chat_id:
        return 0
    done = 0
    for ev in journal.pending_events(limit):
        try:
            r = await bot.send(event_text(ev))
        except Exception as e:   # noqa: BLE001 — сеть: попробуем в следующем цикле
            logger.warning("trading event send: %s", type(e).__name__)
            break
        if not (isinstance(r, dict) and r.get("ok")):
            break
        journal.mark_delivered([ev["id"]])
        done += 1
    return done


def day_limit():
    return risk.limits("hedge", switch.mode())["daily_loss_usdt"]


def check_day_stop(now=None):
    """Убыток дня дошёл до лимита — событие владельцу (один раз за день МСК)."""
    pnl, lim = journal.pnl_today(now), day_limit()
    if pnl <= -lim:
        journal.emit("day_stop", f"результат дня {pnl:.2f} USDT при лимите −{lim} USDT",
                     dedup=f"day_stop|{int(journal.day_start(now))}")
        return True
    return False


def write_state():
    """trading_state.json для launcher: открытые позиции бота (без цен и ключей)."""
    try:
        rows = journal.exposure()
    except ValueError:   # нет цены оценки — позиции всё равно перечисляем
        rows = [{"venue": v, "symbol": s, "side": "", "qty": ""} for v, _, s in journal._exposure_symbols()]
    switch.write_state([{"venue": r["venue"], "symbol": r["symbol"], "side": r.get("side"), "size": r.get("qty")}
                        for r in rows], open_orders=len(journal.active()), unknown_orders=len(
                            [r for r in journal.blocking() if r["state"] == "unknown"]))


def sweep_tokens(link, now=None):
    now = time.time() if now is None else now
    for t in [t for t, v in link.tokens.items() if v["expires"] <= now]:
        link.tokens.pop(t, None)


async def tick(bot):
    """Один шаг цикла: сверка (если есть проверенные ключи), дневной стоп, состояние для launcher, доставка событий."""
    link = link_of(bot)
    sweep_tokens(link)
    if not has_keys() and not os.path.exists(journal.DB_PATH):
        return   # торговлей не пользовались: базу журнала не создаём
    if has_keys():
        await journal.reconcile(bot.s, creds_for)
    check_day_stop()
    write_state()
    await deliver(bot)
    link.alarmed = False


async def loop(bot):
    """Цикл ядра: tick раз в INTERVAL; сбой — лог, одна тревога владельцу, пауза FAIL_PAUSE."""
    link = link_of(bot)
    while True:
        try:
            await tick(bot)
            await asyncio.sleep(INTERVAL)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:   # noqa: BLE001 — ядро не должно ронять бота
            logger.error("trading loop: %s", type(e).__name__)
            if not link.alarmed and bot.chat_id:
                link.alarmed = True
                try:
                    await bot.send(f"⚠️ <b>Торговое ядро</b>: сбой сверки ({_esc(type(e).__name__)}) — повторю через "
                                   f"{FAIL_PAUSE // 60} мин. Новые открытия ядро само запрещает, пока сверка не "
                                   "пройдёт.")
                except Exception:   # noqa: BLE001
                    pass
        await asyncio.sleep(FAIL_PAUSE)


# --- /trading ---

def _fmt(d):
    return venues.fmt(d) if isinstance(d, Decimal) else _esc(d)


def view(bot):
    """(текст, кнопки) /trading: режим, ключи, позиции бота со стопами, результат дня против лимита, блокировки,
    предупреждения, пороги gates."""
    link = link_of(bot)
    lines = ["🤖 <b>Торговля</b>", status_line(), "", "<b>Ключи</b> (основной аккаунт, только торговля):"]
    for v in venues.VENUES:
        why, _ = keys.check_status(v, keys.raw_credentials(v))
        lines.append(f"• {VENUE_NAMES[v]}: " + ("✅ проверен" if not why else _esc(why)))
    lines += ["", "<b>Позиции бота</b>:"]
    try:
        rows = journal.exposure()
    except ValueError as e:
        rows, lines = [], lines + [f"• не оценить: {_esc(e)}"]
    for r in rows:
        stop = f"стоп {_fmt(r['stop_price'])}" if r["stop"] else "⚠️ без стопа"
        lines.append(f"• {VENUE_NAMES.get(r['venue'], r['venue'])} {_esc(r['symbol'])} {r['side']} {_fmt(r['qty'])} "
                     f"по {_fmt(r['entry'])} · {stop} · {_esc(r['strategy'] or '—')}")
    if not rows:
        lines.append("• нет")
    pnl, lim = journal.pnl_today(), day_limit()
    lines += ["", f"<b>День</b> (МСК): {_fmt(pnl)} USDT при лимите убытка −{_fmt(lim)} USDT (и не больше 2% капитала)"]
    block = []
    n = len(journal.blocking())
    if n:
        block.append(f"ордеров с неясным исходом: {n} — открытий нет до разбора")
    n = len(journal.unsettled())
    if n:
        block.append(f"неподтверждённых закрытий биржей: {n}")
    feed = journal._feed_problem()
    if feed:
        block.append(feed)
    if pnl <= -lim:
        block.append("дневной стоп")
    lines += ["", "<b>Блокировки</b>: " + ("нет" if not block else "")] + [f"• {_esc(b)}" for b in block]
    if link.warnings:
        lines += ["", "<b>Предупреждения</b>:"] + [f"• {_esc(w)}" for w in link.warnings]
    lines += ["", "<b>Пороги</b> (самый рискованный режим, который они разрешают):"]
    for st in risk.STRATEGIES:
        try:
            gm = journal._gate_mode(st)
        except Exception as e:   # noqa: BLE001
            gm = f"не прочитать ({type(e).__name__})"
        lines.append(f"• {st}: {_esc(gm)}")
    if link.tokens:
        lines += ["", f"Ордеров ждут вашего подтверждения: {len(link.tokens)}"]
    kb = []
    if switch.enabled() or switch.mode() != "paper" or link.tokens:
        kb.append([{"text": "⛔ Стоп торговли", "callback_data": "trd_stop"}])
    lower = [m for m in switch.MODES if switch.RANK[m] < switch.RANK[switch.mode()]]
    if lower:
        kb.append([{"text": f"⬇️ {m}", "callback_data": f"trd_mode:{m}"} for m in lower])
    kb.append([{"text": "🔄 Обновить", "callback_data": "trd_view"}])
    return "\n".join(lines), {"inline_keyboard": kb}


async def command(bot, arg=""):
    text, kb = view(bot)
    await bot.send(text, markup=kb)


# --- подтверждение ордера ---

def order_text(order, strategy, group=""):
    side = "покупка (long)" if order.side == "buy" else "продажа (short)"
    parts = [f"{VENUE_NAMES.get(order.venue, order.venue)} {_esc(order.category)} {_esc(order.symbol)}", side,
             f"{_esc(order.order_type)} {_fmt(order.qty)}"]
    if order.price is not None:
        parts.append(f"цена {_fmt(order.price)} {_esc(order.tif or '')}".rstrip())
    if order.stop_loss is not None:
        parts.append(f"стоп {_fmt(order.stop_loss)}")
    return " · ".join(parts) + f"\nстратегия {_esc(strategy or '—')}" + (f", группа {_esc(group)}" if group else "")


async def request_order(bot, order, strategy="", group="", why=""):
    """Запросить у владельца подтверждение реального открытия: карточка с точным ордером и кнопками. → токен или None
    (торговля выключена, режим paper, нет ключа, ордер не открывающий, сообщение не ушло)."""
    ok, _ = switch.can_open()
    if not ok or order.reducing or not creds_for(order.venue) or not bot.chat_id:
        return None
    link = link_of(bot)
    token = secrets.token_urlsafe(9)
    text = (f"🟡 <b>Подтвердите ордер</b> (режим {switch.mode()})\n{order_text(order, strategy, group)}"
            + (f"\n{_esc(why)}" if why else "") + f"\nКнопка действует {TOKEN_TTL} с, один раз.")
    kb = {"inline_keyboard": [[{"text": "✅ Отправить", "callback_data": f"trd_ok:{token}"},
                               {"text": "✖️ Отмена", "callback_data": f"trd_no:{token}"}]]}
    r = await bot.send(text, markup=kb)
    if not (isinstance(r, dict) and r.get("ok")):
        return None
    link.tokens[token] = {"order": order, "strategy": strategy, "group": group,
                          "expires": time.time() + TOKEN_TTL, "message_id": (r.get("result") or {}).get("message_id")}
    return token


def take_token(link, token, message_id, now=None):
    """Забрать токен (одноразово): → (запись, "") или (None, причина)."""
    rec = link.tokens.pop(token, None)
    now = time.time() if now is None else now
    if rec is None:
        return None, "кнопка уже использована, отменена или неизвестна"
    if rec["expires"] <= now:
        return None, "кнопка устарела — ордер не отправлен"
    if rec["message_id"] is not None and message_id != rec["message_id"]:
        return None, "кнопка не от этой карточки — ордер не отправлен"
    return rec, ""


async def _submit(bot, rec):
    order = rec["order"]
    ok, why = switch.can_open()
    creds = creds_for(order.venue)
    if not ok or not creds:
        await bot.send(f"✖️ Ордер не отправлен: {_esc(why or 'нет проверенного торгового ключа')}")
        return None
    try:
        res = await journal.submit(bot.s, order, creds, purpose="open", strategy=rec["strategy"], group=rec["group"],
                                   mode=switch.mode())
    except Exception as e:   # noqa: BLE001
        logger.error("trading submit: %s", type(e).__name__)
        await bot.send(f"⚠️ Ордер: сбой отправки ({_esc(type(e).__name__)}) — исход выяснит сверка")
        return None
    state = res.get("state")
    icon = {"open": "✅", "filled": "✅", "closed": "✅", "refused": "✖️", "rejected": "↩️"}.get(state, "❓")
    text = f"{icon} Ордер: {_esc(state)}\n{order_text(order, rec['strategy'], rec['group'])}"
    if res.get("reason"):
        text += f"\n{_esc(res['reason'])}"
    await bot.send(text)
    return res


def stop(bot, save_env):
    """«⛔ Стоп торговли»: TRADING=0 и paper в .env и в процессе, все висящие кнопки подтверждения погашены."""
    link = link_of(bot)
    n = len(link.tokens)
    link.tokens.clear()
    err = switch.stop_and_persist(save_env)
    text = "⛔ Торговля остановлена: TRADING=0, режим paper" + (f", отменено подтверждений: {n}" if n else "") + \
        ". Сопровождение, стопы и закрытие позиций бота продолжают работать. Включить снова — только на ПК."
    if err:
        text += f"\n⚠️ .env не записан ({_esc(err)}) — остановлено до перезапуска бота."
    return text


async def callback(bot, cq, data, save_env):
    """Кнопки trd_* — bot.py вызывает только для владельца в его личном чате (после _owner_gate)."""
    msg = cq.get("message") or {}
    toast = ""
    if data.startswith("trd_ok:"):
        rec, why = take_token(link_of(bot), data[7:], msg.get("message_id"))
        if rec is None:
            toast = why
        else:
            ok, why = switch.can_open()
            if not ok:
                toast = "торговля выключена — ордер не отправлен"
            else:
                toast = "отправляю ордер"
                task = asyncio.ensure_future(_submit(bot, rec))
                link_of(bot).tasks.add(task)
                task.add_done_callback(link_of(bot).tasks.discard)
    elif data.startswith("trd_no:"):
        link_of(bot).tokens.pop(data[7:], None)
        toast = "ордер отменён"
    elif data == "trd_stop":
        await bot.send(stop(bot, save_env))
        toast = "торговля остановлена"
    elif data.startswith("trd_mode:"):
        lowered, err = switch.lower_and_persist(data[9:], save_env)
        toast = (f"режим понижен до {switch.mode()}" + (f" (только до перезапуска: {err})" if err else "")
                 if lowered else "повысить режим из Telegram нельзя — только в .env на ПК")
    if data in ("trd_view", "trd_stop") or data.startswith("trd_mode:"):
        text, kb = view(bot)
        await bot.send(text, markup=kb)
    await bot.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=toast[:190])
    return toast
