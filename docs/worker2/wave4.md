# Рабочая сессия 2 — репозиторий Sonti22/p2p-bot, Волна 4: UX, /stats-расхождение, здоровье данных, контракт причин

Ты — облачный Claude Code, второй исполнитель проекта Telegram-бота @p2psckabot (P2P-арбитраж в рублях, ручная торговля владельца). Первый исполнитель («Межмонетные связки, часть 2») работает параллельно в другой ветке. Ниже — 5 задач этой волны. Делай их строго по одной, в указанном порядке.

> **Поправка 2026-09-30.** Ветки `claude/paper-route-hops-venues`, `claude/paper-cross-asset-transfer-fee-frozen` и `cloud/cross-coin-part2` — мёртвые дубли: всё их содержимое уже в main (PR #117 и #132), PR закрыты владельцем. Любые оговорки ниже про то, что они «могут влиться первыми», игнорируй: ничего ребейзить заранее не нужно. Точки расширения маршрутов (`route_hops`, `qty_from_hops`, `transfer_check`) уже есть в main — не переписывай их.

## Как работать (обязательный протокол)

1. Перед КАЖДОЙ задачей: `git fetch origin && git checkout -B claude/<id-задачи> origin/main`. Одна задача = одна ветка `claude/<id-задачи>` = один пуш. Не смешивай задачи в одной ветке.
2. Реализуй по разделам «Что сделать» и «Файлы». Все тесты офлайн: сеть в тестах заблокирована (tests/conftest.py, файл защищён). Асинхронные тесты — только через `from helpers import arun`, не `asyncio.run`.
3. Перед пушем обязательно, обе команды зелёные:
   - `pytest -q`
   - `PYTHONIOENCODING=utf-8 python scripts/guard.py origin/main` (должно напечатать «guard: ок»)
4. Отметь задачу в ROADMAP.md: пункт `[x]` + дата + одна строка результата, плюс строка в «Журнал» сверху. При конфликте ребейза в «Журнале» оставь ОБЕ строки (свою и чужую).
5. Запушь новую ветку `claude/<id-задачи>` (без force-push). CI и автомерж сами создадут PR и вольют его, если всё зелёное.
6. Если guard или `tests/test_trading_surface.py` красный от ТВОЕЙ правки — НЕ обходи. Откати правку, в ROADMAP пометь задачу «ждёт владельца: <что именно>» и переходи к следующей.
7. Дождись, пока ветка вольётся в main (проверь `git fetch origin` и `git log origin/main --oneline -5`), прежде чем брать следующую задачу этой же волны, если она правит те же файлы. Волны нужно вливать по порядку: волна N+1 требует, чтобы волна N уже была в main.
   **Как определять слияние:** CI вливает PR **squash-ом** (новый коммит с темой твоего коммита), поэтому `git merge-base --is-ancestor <твоя-ветка> origin/main` НИКОГДА не станет истинным — не используй его. Проверяй по теме: `git fetch origin main && git log origin/main --format=%s | grep -F '<тема твоего коммита>'` (после слияния ветка на origin может быть удалена). Ждать — фоновой командой `until <проверка>; do sleep 30; done` с тайм-аутом ~15 минут; не вышло — посмотри `git log origin/main --oneline -10` глазами, а не повторяй ту же проверку. Если за 20 минут не влилось — проверь, что CI не красный (ветка без PR или с конфликтом в ROADMAP «Журнал» — перебазируй на свежий origin/main, оставь обе строки журнала, запушь без force).

## Жёсткие запреты (нарушение = откат)

- НЕ читай и не выводи `.env`, `data/keys.json`, папку «ключ» и папку живого бота на ПК. Ключей и токенов в коде, тестах и логах быть не должно.
- НЕ трогай защищённые пути: любые файлы с «payout» или «trading» в пути, `.github/`, `launcher.py`, `scripts/guard.py`, `CLAUDE.md`, `tests/conftest.py`, `pytest.ini`, `requirements.txt`, `data/`, `logs/`, `*.bat`.
- НЕ меняй закреплённые символы (их sha256 проверяет защищённый `tests/test_payout_pins.py`): всё в `accounts.py` (cryptomus_sign, _json_no_redirect, keys, unprotect, _dpapi, _scrub, api_error_text и т.д.); `jsonstore.read_dict`; в `bot.py` — `Bot._owner_gate`, `Bot.is_guest`, `Bot.on_guest_callback`, `Bot.cmd_payout`, `Bot.payout_*`, `Bot.payouts_loop`, `GUEST_CALLBACKS/CMDS/DENIED/MENU/WELCOME`, `PAYOUT_*`, `_payout_what`, `payout_event_text`, `payout_history_view`, `payout_menu_view`, `save_env`. Правь соседний код, но эти символы не задевай ни строкой, ни форматированием.
- `tests/test_trading_surface.py` (AST) запрещает торговые эндпоинты (ордера, позиции, маржа) и `.post(` вне `trading/`. Не добавляй `.post(`, `subprocess`, новые зависимости, новые домены и новые переменные окружения, если задача этого прямо не требует.
- Не добавляй фичи сверх задачи, не рефактори чужой код, не расширяй дифф. Комментарии — только там, где причина неочевидна.
- Никаких реальных запросов к биржам и реальных денег. Ты ничего не торгуешь и не выводишь.

## Формат ответа после каждой задачи

Одна короткая сводка: ветка, что сделано, результат `pytest -q` и `guard`, помечена ли задача в ROADMAP. Если что-то «ждёт владельца» — что именно.

---

## Задачи Волна 4: UX, /stats-расхождение, здоровье данных, контракт причин

**Почему такой состав волны:** Оставшиеся UX- и аналитические задачи ценности 3, идущие после того, как в main уже есть правки status_view (волны 1-2), /stats (волна 3) и бэкапа (волна 3). data-health-status читает возраст копии из backup.py уже после backup-integrity-check. signal-reason-codes-contract трогает history.py и p2p.py (удаление sell_depth_ok) в местах, которые не задевают другие задачи волны; идёт после signal-quality-report, чтобы контракт причин покрыл и его код.

**Условие старта:** все задачи волн 1–3 уже влиты в main (проверь `git log origin/main`). Если нет — остановись и напиши владельцу, что ждёшь.

### Задача 1. `blacklist-undo-buttons` — Блэклист: кнопка «↩️ Вернуть» после снятия записи и «↩️ Отменить» после «🚫 Скрыть мерчанта»

**Ценность:** 3/5 · **Размер:** M · **Ветка:** `claude/blacklist-undo-buttons`

**Зачем:** Обе операции необратимы одним нажатием. «🚫 Скрыть мерчанта» под сигналом заносит в блэклист обе стороны связки (и всех мерчантов стакана), нажатие рядом с «✅ Сделал» легко случайное; чтобы отменить, надо идти в /blacklist и искать записи по id. Кнопка «🗑 …» в /blacklist делает жёсткий DELETE без подтверждения: теряются запись, причина (note) и дата добавления. Скрытый мерчант перестаёт попадаться в сканах, поэтому случайное нажатие незаметно ухудшает выдачу. Подтверждение перед каждым действием раздражало бы, дешёвый откат в памяти лучше.

**Что сделать:** 0) ПОЧЕМУ ТОСТЫ ДЕЛАЕМ ДО ОБЩЕГО ОТВЕТА: в Bot.on_callback (bot.py ~3891-3892) на каждое нажатие сначала идёт `toast = self.apply(data)` и `answerCallbackQuery(text=toast)`, и только потом цепочка elif; второй answerCallbackQuery в ветке (как сейчас в unbl:/bl:/did:) Telegram не покажет (см. комментарий у paper_ladder, bot.py ~3952). Поэтому новые колбэки blundo:/hideundo: обрабатываются РАНЬШЕ, с единственным ответом на нажатие.

1) Состояние. Константа модуля BL_UNDO_MAX = 20. В Bot.__init__ рядом с self.deals_by_id (bot.py:1541) две строки: `self.bl_undo = {}` (токен -> (вид, данные); не переживает рестарт, как deals_by_id) и `self._bl_undo_seq = 0`. Метод `bl_undo_add(self, kind, payload) -> int`: seq += 1, кладёт (kind, payload) под токеном seq; пока len(self.bl_undo) > BL_UNDO_MAX — удаляет самый старый (`self.bl_undo.pop(next(iter(self.bl_undo)))`). Токен — возрастающий int. Не использовать имена методов post/put/send/get на своих объектах, кроме self.send/self.call (tests/test_trading_surface.py ловит отправителей по имени атрибута).

2) Ветка `unbl:` в on_callback (bot.py:3964-3969). До blacklist.remove взять строку: `row = next((r for r in blacklist.list_all() if r[0] == entry_id), None)`. Остальное как сейчас (remove, answerCallbackQuery «Удалено из блэклиста», `text, kb = blacklist_view()`). Если row не None: `_, ex, nick, added_ts, note, *_ = row` (с `*_`: список last_seen_ts из ROADMAP «Идеи» расширит кортеж), `token = self.bl_undo_add("unbl", (ex, nick, added_ts, note))`, и ПЕРЕД editMessageText `kb["inline_keyboard"].append([{"text": f"↩️ Вернуть: {EXCHANGE_NAMES.get(ex, ex)}: {nick}"[:64], "callback_data": f"blundo:{token}"}])`. Строку добавлять в on_callback, а не в blacklist_view (его тесты остаются зелёными; при пустом списке клавиатура состоит из одной строки «Вернуть»). Если row None (запись уже снята, старая кнопка) — как сейчас, без кнопки, без исключения. Не менять blacklist.py и blacklist_view.

3) Ранняя диспетчеризация. В on_callback сразу после блока `if data.startswith("pay_"): ... return` и ДО `toast = self.apply(data)` вставить `if data.startswith(("blundo:", "hideundo:")):` -> `await self.blacklist_undo(cq, data)` -> `return`. Строки с pay_/payout/trd_ не трогать и не переформатировать (guard считает и удалённые строки; пины TRADING_LINES_APPROVED).

4) Метод `async def blacklist_undo(self, cq, data)` (ровно ОДИН answerCallbackQuery на нажатие). kind = "unbl" для blundo, "hide" для hideundo; токен `int(...)` в try (ValueError -> устарел); `entry = self.bl_undo.pop(token, None)`; если entry есть, но entry[0] != kind — считать устаревшим и вернуть токен на место (не менять ничего другого).
  a) blundo, токен жив: `ex, nick, added_ts, note = payload`; если `(ex, nick) in blacklist.blocked()` — тост «Уже в блэклисте», запись и её причину не трогать; иначе `new_id = blacklist.add(ex, nick, ts=added_ts)` и, если note, `blacklist.set_note(new_id, note)`; тост «↩️ Возвращён в блэклист»; затем editMessageText с `blacklist_view()` (id записи новый, AUTOINCREMENT — нормально; added_ts None вернётся с текущей датой — так ведёт себя add(ts=None), blacklist.py не менять).
  b) blundo, токена нет/устарел (повтор, рестарт, вытеснен): тост «Уже поздно: после перезапуска бота отмена недоступна — добавь заново кнопкой 🚫 под сигналом», список не менять, но перерисовать `blacklist_view()` (мёртвая кнопка исчезает).
  c) hideundo, токен жив: payload = список (ex, nick, id) из hide_deal; для каждого `blacklist.remove(id)` (удаление по id безопасно: AUTOINCREMENT не переиспользует id, снятая вручную запись — no-op); тост «Скрытие отменено»; editMessageText сообщения `cq["message"]["message_id"]` с текстом «↩️ Отменено: {Площадка: ник через запятую, EXCHANGE_NAMES и html.escape} — снова могут попасть в сигналы.» (parse_mode HTML, без reply_markup — кнопка исчезает). Кнопки исходной карточки сигнала НЕ восстанавливаем (deals_by_id уже потрачен в hide_deal).
  d) hideundo, токена нет/повтор: тост «Уже отменено или устарело» и editMessageReplyMarkup с `{"inline_keyboard": []}`.

5) hide_deal (bot.py:1886-1901). До цикла добавления: `before = blacklist.blocked()`. После него: `new = [(ex, nick, i) for ex, nick, i in added if (ex, nick) not in before]`, затем дедуп по (ex, nick) (`dict.fromkeys`-приёмом: обе стороны могут быть с одной площадки и одним ником). Текст «🚫 В блэклисте: …» не менять; если new не пуст — `token = self.bl_undo_add("hide", new)` и `self.send(text, markup={"inline_keyboard": [[{"text": "↩️ Отменить", "callback_data": f"hideundo:{token}"}]]})`, иначе send без markup, как сейчас. Записи, которые уже были в списке, отмена не трогает.

6) Гостям ничего не добавлять: blundo/hideundo не в GUEST_CALLBACKS (bot.py:86), on_guest_callback отвечает «Только для владельца бота». callback_data короткие (до 64 байт).

7) ROADMAP.md по протоколу CLAUDE.md: пункт [x] с датой и строка в «Журнал».

**Править:** `bot.py`, `ROADMAP.md`
**Создать:** `tests/test_blacklist_undo.py`

**Тесты (офлайн):**
- Свой Stub в новом файле (по образцу tests/test_bot.py:17: наследник B.Bot, call() пишет вызовы в self.out) и `from helpers import arun, make_ad`. НЕ импортировать tests/payout_stubs (слово payout в добавленной строке завалит guard). Пути БД не патчить: conftest перенаправляет DB_PATH на временную папку теста.
- unbl: blacklist.add("Bybit","Плохой", ts=фикс.) + set_note; on_callback unbl:<id> -> list_all пуст, последний вызов editMessageText, в клавиатуре есть кнопка с callback_data == f"blundo:{n}" (<= 64 байт).
- blundo:<n>: (ex, nick) вернулся с прежним note и тем же added_ts (list_all), в out ровно один answerCallbackQuery с «Возвращён», editMessageText со списком без кнопки blundo.
- Повторный blundo:<n> и неизвестный токен: ровно один answerCallbackQuery с «поздно», запись не дублируется (UNIQUE), список перерисован без кнопки.
- blundo, когда запись за это время добавлена заново с другой причиной: тост «Уже в блэклисте», свежая причина не затёрта.
- unbl по несуществующему id: без исключения и без кнопки «Вернуть».
- hide_deal (bot.remember_deal + bot.hide_deal): у sendMessage «🚫 В блэклисте» в reply_markup кнопка hideundo:<n>; при заранее занесённом ("Bybit","nick") с причиной hideundo удаляет только новую запись, старая и её причина остаются; один answerCallbackQuery «Скрытие отменено»; editMessageText «↩️ Отменено».
- hide_deal, когда обе стороны уже в блэклисте: у sendMessage нет reply_markup.
- Повтор hideundo:<n> и неизвестный токен: тост «Уже отменено», editMessageReplyMarkup с пустой клавиатурой, список без изменений.
- Переполнение: 25 снятий подряд -> len(bot.bl_undo) == 20, blundo с токеном первого — «поздно», запись не вернулась.
- Вид токена: blundo с токеном от hide_deal (и наоборот) — «устарел», ничего не изменилось, токен на месте.
- Гость: bot.guests = {"42"}; on_update({"callback_query": {"id": "7", "data": "blundo:1", "from": {"id": 42}, "message": {"chat": {"id": 42, "type": "private"}, "message_id": 5}}}) и то же для hideundo:1 при живом токене от владельца: список не изменён, токен остался, ответ «Только для владельца бота».
- Без правок остаются зелёными tests/test_bot.py:799 test_unbl_callback_removes_entry, :812, :824-870 и tests/test_review_export_blacklist.py:50.

**Критерии приёмки:**
- После снятия записи кнопкой «🗑» в /blacklist владелец одним нажатием возвращает её с причиной и исходной датой; повторное нажатие или устаревший токен даёт понятный тост «Уже поздно…» и убирает мёртвую кнопку.
- После «🚫 Скрыть мерчанта» одним нажатием отменяется именно это скрытие; записи, что были в блэклисте раньше, и их причины не задеваются; если новых записей нет, кнопки «↩️ Отменить» нет.
- На каждое нажатие blundo:/hideundo: уходит ровно один answerCallbackQuery (ветки обрабатываются до общего ответа в on_callback), тест это проверяет.
- Данные отмены только в памяти (bl_undo, не более BL_UNDO_MAX = 20 записей, самый старый вытесняется); на диск ничего не пишется; blacklist.py и blacklist_view не изменены.
- python -m pytest -q и python scripts/guard.py зелёные; всего не более ~250 изменённых строк с тестами.

**Не делать:**
- Не менять blacklist.py (схему, add/remove/list_all/blocked), blacklist_view, deal_markup и живые карточки (fancy-403 / fancy-edit-errors / audit-bot), launcher.py, tests/conftest.py.
- Не вводить запрос подтверждения «Точно удалить?» перед снятием: нужна отмена после, а не ещё один клик.
- Не хранить токены отмены на диске и не заводить новых таблиц/файлов.
- Не отвечать на blundo:/hideundo: вторым answerCallbackQuery внутри elif-цепочки после общего ответа: тост не покажется; ветки — только до `toast = self.apply(data)`.
- Не трогать и не переформатировать существующие строки со словами payout/pay_/trd_/TRADING/trading (guard и пины считают и удалённые строки); новые строки тоже не должны их содержать. Не импортировать tests/payout_stubs в новом тесте.
- Не пытаться восстановить кнопки исходной карточки сигнала после hideundo (deals_by_id уже потрачен), не вызывать здесь deal_markup.
- Не добавлять вызовов .post/.put/.delete/.request/.get на сетевых объектах, subprocess, новых зависимостей и доменов; не редактировать tests/test_trading_surface.py и scripts/guard.py.
- Не добавлять blundo/hideundo в GUEST_CALLBACKS.

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:3964-3969 ветка unbl: `blacklist.remove(int(data[5:]))` без подтверждения и без возможности вернуть.
- bot.py:3891-3892 `toast = self.apply(data)` и общий answerCallbackQuery до цепочки elif; bot.py:3952 комментарий «на нажатие уже ответили выше — второй answerCallbackQuery не нужен» -> тосты внутри веток не показываются.
- blacklist.py:51 remove — жёсткий DELETE; blacklist.py:27 add(ex, nick, path, ts) — INSERT OR IGNORE, возвращает id, не сообщает новизну; blacklist.py:39 set_note; blacklist.py:58 list_all -> (id, ex, nick, added_ts, note); blacklist.py:69 blocked().
- bot.py:1886-1901 hide_deal: добавляет обе стороны и мерчантов стакана, шлёт «🚫 В блэклисте: …» через self.send без кнопок, затем editMessageReplyMarkup карточки.
- bot.py:729 blacklist_view (кнопки unbl:<id>, «🗑 …», лимит 90 кнопок из 100); bot.py:1541 deals_by_id — образец памяти без переживания рестарта; bot.py:1632 send(text, markup=...).
- bot.py:86 GUEST_CALLBACKS={best, top}; bot.py:3726-3728 on_guest_callback отвечает «Только для владельца бота»; bot.py:_owner_gate -> 'guest' для chat 42 при bot.guests={'42'} (tests/test_guests.py:146-153).
- scripts/guard.py PAYOUT_CODE (payout, re.I) считается и в tests/; tests/test_guests.py:6 импортирует payout_stubs — образец, которого нужно избегать; CLAUDE.md: tests/payout_stubs.py защищён.
- tests/conftest.py _redirect/_FUNCS: default-аргументы path=DB_PATH перенаправлены во временную папку каждого теста, monkeypatch путей не нужен (tests/test_bot.py:812 так и работает).
- tests/test_trading_surface.py:359-361 STATE_METHODS/READ_METHODS — точные имена атрибутов, ALLOWED_SENDERS :393; tests/test_bot.py:799, :810, :826 существующие тесты.

**Пересечения с другими задачами и ветками:** Проверены все remote-ветки (cloud/*, claude/*): diff bot.py/blacklist.py по unbl|hide_deal|blacklist|undo затрагивает только cloud/s2-help-ux (строка справки /blacklist). Влитое по теме: #27 (блэклист), #107, #108 (стакан, ревью), #169 (идея last_seen_ts — не взята, не пересекается; поэтому распаковка строки list_all с `*_`). Идеи про отмену/вернуть в ROADMAP нет. Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports, cross-coin part 2, hedge/gates/trading) не пересекается. Возможный git-конфликт с другими ветками — только в Bot.__init__ рядом с deals_by_id (одна добавленная строка) и в on_callback; при конфликте действует обычный рецепт слияния.

---
### Задача 2. `alert-create-market-hint` — /alert: в ответе на создание показывать текущий рынок и предупреждать об опечатке в курсе

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/alert-create-market-hint`

**Зачем:** Ответ «🔔 Алерт создан: USDT продать ≥920 ₽» не сверяет курс с рынком. Опечатка в порядке (920 вместо 92) создаёт алерт, который не сработает, и владелец узнаёт об этом только когда пропустит нужный курс. Обратная ошибка (9.2 вместо 92 для sell, 930 вместо 93 для buy) даёт алерт, который сработает на первом же скане с ложным «достигнуто». Бот уже держит последний снимок (self.last.best), поэтому подсказка бесплатная: ни новых запросов, ни новых зависимостей.

**Что сделать:** 1) bot.py, рядом с ALERT_HELP (стр. ~976) добавить константу модуля ALERT_FAR_PCT = 10 (порог «опечатка в курсе», %; не .env) и чистую функцию alert_market_hint(snap, asset, side, rate, extra=False) -> str. Из snap.best (dict (ex, side, asset) -> Ad) взять свежие объявления (not ad.stale) этой монеты и стороны (тот же отбор, что alerts.due, alerts.py:168-201); best_ad = max по ad.price для side == "sell", min для "buy". snap is None, нет таких объявлений или best_ad.price <= 0 -> вернуть "" (молча). Иначе первая строка: f"Сейчас лучшая цена {продать|купить}: {_price(best_ad.price)} ₽ ({best_ad.ex})" — _price уже импортирован в bot.py (стр. 42) и сам форматирует цены ≥1000; название площадки — best_ad.ex как есть (в проде это уже «Bybit»/«BestChange», EXCHANGE_NAMES.get не нужен). Дальше, по отклонению dev = (rate / price - 1) * 100 (price = best_ad.price), добавить строки через "\n": (а) условие уже выполнено (sell: price >= rate; buy: price <= rate) -> при extra == False «⚡ Условие уже выполнено — алерт сработает на ближайшем скане.», при extra == True (заданы vol/reliable; alerts.due проверяет их через _candidate_ok) «⚡ По цене условие уже выполнено — сработает на ближайшем скане, когда пройдут и остальные условия (vol/reliable).»; (б) курс дальше рынка более чем на ALERT_FAR_PCT % (sell: dev > ALERT_FAR_PCT; buy: dev < -ALERT_FAR_PCT) -> «⚠️ Курс на N % дальше рынка — проверь, нет ли опечатки (сработает вряд ли).» (N = f"{abs(dev):.0f}"); (в) курс с «лёгкой» стороны рынка больше чем на ALERT_FAR_PCT % (sell: dev < -ALERT_FAR_PCT; buy: dev > ALERT_FAR_PCT) -> дополнительно «⚠️ Курс на N % {ниже|выше} рынка — не опечатка ли? Сработает сразу.» Иначе (курс в пределах 10 % и условие не выполнено) — только строка с ценой рынка. 2) Bot.add_alert (bot.py:1913+): после alerts.add(...) собрать ответ как сейчас (текст «🔔 Алерт создан: …», notes про повтор/объём/надёжность, «Список — /alerts.» — без изменений), затем если self.last не None: hint = alert_market_hint(self.last, asset, side, rate, extra=bool(min_volume or require_reliable)); если hint непустой — дописать в ответ через "\n\n" и отправить одним self.send. self.last None или hint == "" -> текст ровно как сейчас. 3) Алерт создаётся всегда (только подсказка, не отказ). /alert route (add_route_alert) и ALERT_HELP не менять. 4) Тесты в новом tests/test_alert_market_hint.py: `from test_bot import Stub, texts`, `from helpers import arun, make_ad`; снимок собирать как p2p.Snapshot(88.0, "test", {}, best, [], {}, {}, {}) (best = {("Bybit","sell","USDT"): make_ad("Bybit","sell",91.4), ...}); для add_alert подменять БД как в tests/test_bot.py:899-906 (monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))). 5) ROADMAP.md: пункт `[x]` с датой и строка в «Журнал» (протокол CLAUDE.md п.4).

**Править:** `bot.py`, `ROADMAP.md`
**Создать:** `tests/test_alert_market_hint.py`

**Тесты (офлайн):**
- alert_market_hint: sell 92 при лучшей цене 91.4 (Bybit) -> в тексте «91.40» и «Bybit», нет ⚡ и ⚠️; при нескольких площадках sell берётся максимум, buy — минимум.
- sell 90 при рынке 91.4 -> «⚡ Условие уже выполнено»; buy 93 при рынке 91.4 -> ⚡; те же вызовы с extra=True -> текст про «остальные условия (vol/reliable)», без «сработает на ближайшем скане» без оговорки.
- sell 920 при рынке 91.4 -> «⚠️ … дальше рынка» и число N %; buy 9.2 при рынке 91.4 -> ⚠️ «дальше рынка».
- Лёгкая сторона: sell 9.2 при рынке 91.4 -> и ⚡, и «⚠️ … ниже рынка … Сработает сразу»; buy 930 -> ⚠️ «выше рынка».
- stale-объявления (make_ad(...) с dataclasses.replace(ad, stale=True)) не учитываются; если после фильтра пусто, а также при другой монете/стороне -> hint == ""; snap None -> ""; price <= 0 -> "".
- Bot.add_alert без снимка (Stub, last=None): ответ прежний, «🔔 Алерт создан: USDT продать ≥92 ₽…», без строки «Сейчас лучшая цена».
- add_alert('USDT sell 92 7d vol 50000 reliable repeat 1h') с bot.last = снимок: ответ содержит «Алерт создан», «повтор», «объём», «надёжность» и строку рынка; алерт записан (alerts.list_all("1", path=db)); один вызов sendMessage.
- Существующие tests/test_bot.py:899-965 (add_alert) зелёные без правок (они проверяют вхождения, а не точное равенство; у Stub last=None).

**Критерии приёмки:**
- Ответ на /alert <монета> sell|buy <курс> <срок> для монеты с данными снимка содержит текущую лучшую цену и площадку.
- Курс, уже достигнутый рынком, помечен «⚡» (с оговоркой при vol/reliable); курс дальше рынка более чем на 10 % — «⚠️» с намёком на опечатку; курс более чем на 10 % с лёгкой стороны — «⚠️ … Сработает сразу».
- Алерт создаётся в любом случае; без снимка или без свежих объявлений текст ответа такой же, как сейчас.
- python -m pytest -q (включая tests/test_trading_surface.py и существующие тесты add_alert) и python scripts/guard.py зелёные; не более ~150 изменённых строк вместе с тестами и ROADMAP.

**Не делать:**
- Не менять alerts.py, логику due/route_due/mark_fired и check_alerts; не запрещать и не откладывать создание алерта.
- Не делать сетевых запросов за ценой: только self.last.best; не вызывать fresh_scan().
- Не трогать /alert route (add_route_alert), ALERT_HELP, launcher.py, payouts.py, любые строки со словами payout/trading.
- Не выводить в текст ключи, аккаунты и балансы; не добавлять настройку в .env (порог 10 % — константа ALERT_FAR_PCT).
- Не использовать в новом коде слова order, position, margin, leverage, withdraw, transfer, trade в именах и строках (guard/TRADING_CODE и сканер test_trading_surface): имена — alert_market_hint, ALERT_FAR_PCT, best_ad, price, dev.
- В новой функции не называть параметры/переменные s, sess, session, http, client, opener и не вызывать у них .get/.send/.post (сканер отправителей в tests/test_trading_surface.py); никаких .post(, subprocess, eval/exec, новых доменов и зависимостей.
- Не править tests/conftest.py, pytest.ini, tests/test_trading_surface.py, CLAUDE.md, scripts/guard.py и существующие тесты; новый тест-файл назвать без слов payout/trading.

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:1913-1971 Bot.add_alert: alerts.add(...) на стр. 1964, ответ «🔔 Алерт создан: … Список — /alerts.» на стр. 1970-1971, сверки с рынком нет; проверки — только asset in self.cfg.assets (стр. ~1922) и float(rate_s) (стр. ~1926).
- alerts.py:155-201 _candidate_ok и due(): цена из snap.best, пропуск ad.stale (стр. ~190), sell — максимум, buy — минимум, условия vol/reliable проверяются в _candidate_ok — поэтому ⚡ без оговорки при extra неверен.
- p2p.py:1557-1561 Snapshot.best: (ex, side, asset) -> Ad; p2p.py:2250-2263 сборка best (buy — min, sell — max после usable/MAX_DEV); p2p.py:358 поле Ad.stale; p2p.py:425-598 Ad.ex — отображаемое имя («Bybit», «BestChange»); p2p.py:2605 _price(p); bot.py:42 импорт _price.
- bot.py:1509 self.last = None; bot.py:2631 self.last присваивается в scan_loop; bot.py:2648 check_alerts(self.last) на каждом скане.
- tests/test_bot.py:25-41 Stub(B.Bot), :67 texts(), :899-931 существующие тесты add_alert (проверяют только вхождения «Алерт создан», «повтор», «объём», «надёжность»); tests/test_alerts.py:8 snap(); tests/helpers.py make_ad; другие тесты уже делают `from test_bot import Stub`.
- scripts/guard.py PROTECTED*/TRADING_CODE/PAYOUT_CODE/FORBIDDEN проверены на синтетических строках реализации: bot.py и tests/test_alert_market_hint.py не защищены, совпадений regex нет.
- ROADMAP.md: подсказки рынка при создании алерта нет ни в Очереди, ни в Идеях, ни в Журнале (строки про /alert: 472-488, 673-679, 751, 840-842 — другое).

**Пересечения с другими задачами и ветками:** cloud/s2-route-alert уже в main и касается только /alert route; cloud/tests-sockets (замена asyncio.run на arun) уже в main. Ни одна из ветвей origin/cloud/*, origin/claude/* не содержит подсказку рынка в add_alert (проверен diff по bot.py и tests). Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, reports/digest, cross-coin part 2, hedge/gates/trading) add_alert и alert-подсказки не затрагивает; при параллельной правке bot.py возможен лишь текстовый конфликт рядом с ALERT_HELP/add_alert — разрешается ребейзом, логика независима.

---
### Задача 3. `plan-fact-spread` — Расхождение план → факт в /stats: медиана, худший дециль, доля хуже допуска, худшие сделки за месяц

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/plan-fact-spread`

**Зачем:** trades.stats даёт только среднее расхождение расчёта и факта (avg_diff), а среднее прячет хвост: две-три сделки с проскальзыванием -1.5 п.п. тонут среди нормальных. Владельцу нужны медиана (типичное расхождение), худший дециль (что бывает в плохой день), доля сделок хуже допуска и конкретные худшие сделки для ручного разбора. Считается только по сделкам с настоящим фактом; оценки «как расчёт»/±0.5 п.п. не участвуют.

**Что сделать:** 1) trades.py. Добавить `import statistics` (между sqlite3 и time) и модульную константу `BAD_DIFF_PP = -0.5` (п.п.; сделка «плохая», если факт хуже расчёта строго сильнее этого). Новую функцию `plan_fact_spread(since=0.0, path=DB_PATH, worst=3)` положить сразу после `facts_by_pair` (trades.py:454-465), до `period_start`. Логика: если `not os.path.exists(path)` -> None; `con = _connect(path)`; `SELECT id, ts, buy_ex, sell_ex, fact - profit FROM trades WHERE {_REAL_FACT} AND profit IS NOT NULL AND ts >= ?` (f-строка с `_REAL_FACT`, как в facts_by_pair; оценки fact_source 'plan'/'plan±' отсеиваются самим `_REAL_FACT`); закрыть соединение; строки отсортировать по `(diff, id)` по возрастанию (детерминированный порядок при равных diff); если строк < 3 -> None. Вернуть словарь: `n` (число сделок), `median` (`statistics.median` по diff), `p10` = `diffs[(n - 1) // 10]` по отсортированному списку (то есть при n <= 10 это минимум; при n = 11..20 второй по худшести и т.д.), `bad_n` = число diff < BAD_DIFF_PP (строго), `share_bad` = bad_n / n (доля 0..1, не проценты), `worst` = первые `worst` записей `{"id", "ts", "buy_ex", "sell_ex", "diff"}` (route НЕ брать: в БД это длинное описание маршрута, а не «A → B»). Без numpy, без новых зависимостей. `trades.stats`, `match_fact`, `set_fact`, схему таблицы и `_connect` не менять. 2) bot.py. Рядом с `direction_lines` (bot.py:1427; после неё, до `OUTAGE_DAYS`) добавить МОДУЛЬНУЮ функцию `spread_lines(data)` -> list[str] (не метод Bot: self не нужен, прецедент - `direction_lines`/`B.direction_lines` в tests/test_stats_direction.py). Как и direction_lines, результат начинается с пустой строки-разделителя. data None -> `["", "План → факт: мало сделок с фактом (меньше 3)"]`. Иначе: `["", f"<b>План → факт за месяц</b> ({n} сд. с фактом): медиана {median:+.2f} п.п., худшие 10% — {p10:+.2f} и ниже, хуже {trades.BAD_DIFF_PP:+g} п.п.: {share_bad:.0%} ({bad_n} из {n})"]`, плюс вторая строка `Худшие: #12 Bybit → Rapira -1.40; #9 ...` (`html.escape` для бирж, `{diff:+.2f}`), в неё попадают только записи worst с diff < 0; если таких нет - второй строки нет. Стрелка ТОЛЬКО «→» (Юникод): /stats уходит в HTML-режиме, голое «>» из «->» не экранировано. Все числа - только через форматы {:+.2f}/{:.0%}/{:+g}, никаких nan. В `Bot.stats_view` (bot.py:2058) вставить одну строку `lines += spread_lines(trades.plan_fact_spread(trades.period_start("month")))` СРАЗУ ПОСЛЕ цикла `for key, label in labels` (после ветки `else: lines.append(f"{label}: сделок нет")`) и ДО существующей `lines += direction_lines(...)`. Окно - календарный месяц МСК (`trades.period_start("month")`), ровно как строка «За месяц» и блок «По направлениям за месяц»; НЕ «30 дней» и без новых аргументов команды. Существующие строки /stats не менять. 3) tests/test_plan_fact_spread.py (новый): хелперы как в tests/test_stats_direction.py (T = 1_790_000_000.0, `trades.log_trade((profit, make_ad(...), make_ad(...), "route"), amount, path=db, ts=...)`, затем `trades.set_fact(id, fact, path=db, source=trades.FACT_MANUAL)`). Использовать точно представимые в двоичном виде числа (например profit/fact кратные 0.25/0.5), чтобы граница -0.5 и медиана сравнивались точно, иначе pytest.approx. 4) ROADMAP.md: одна запись в начало «Журнала» (формат соседних строк, дата 2026-09-29) без правки «Очереди» и «Идей».

**Править:** `trades.py`, `bot.py`, `ROADMAP.md`
**Создать:** `tests/test_plan_fact_spread.py`

**Тесты (офлайн):**
- plan_fact_spread: 5 сделок с известными profit и fact (например diff = -1.5, -0.5, 0.0, +0.5, +1.0) через trades.log_trade + trades.set_fact -> n=5, median=0.0, p10=-1.5 (минимум при n<10), bad_n=1 (diff ровно -0.5 НЕ плохая: строгое <), share_bad=0.2; 25 сделок -> p10 равен diffs[2] (формула (n-1)//10)
- сделки с fact_source FACT_PLAN и FACT_PLAN_SHIFT ('plan', 'plan±'), сделки без факта (fact NULL) и сделки за пределами since не попадают в n/median/worst; fact_source=None (старые записи) и FACT_AUTO/FACT_MANUAL - попадают
- n=2 -> None; n=3 -> словарь; несуществующий файл базы (tmp_path/'none.db') -> None; сделка с profit IS NULL (через sqlite3 UPDATE) не учитывается
- worst: по возрастанию diff, не длиннее аргумента worst (worst=2 -> 2 записи), в записях есть id, ts, buy_ex, sell_ex, diff; при равных diff порядок по id
- since отсекает старые сделки (сделка за 40 дней до T не входит при since=T-30*86400)
- spread_lines(None) -> ['', 'План → факт: мало сделок с фактом (меньше 3)']; словарь -> текст содержит медиану со знаком, «20% (1 из 5)», «-0.5», ровно до трёх «#id» и стрелку «→», не содержит подстроки '->' и 'nan'; при worst без отрицательных diff строки «Худшие:» нет; html.escape применён к названию биржи (например 'A&B')
- интеграция: сделки в trades.DB_PATH (путь по умолчанию подменён conftest) с ts=None и Stub(B.Bot) как в tests/test_stats_direction.py -> bot.stats_view() содержит «План → факт за месяц» и «#id»; без сделок - «мало сделок с фактом (меньше 3)» и не падает
- регрессия: существующие tests/test_trades.py, tests/test_trade_facts.py, tests/test_stats_direction.py и stats_view-тесты в tests/test_bot.py (1918, 1933, 3133) проходят без правок; trades.stats не изменилась

**Критерии приёмки:**
- /stats показывает после строк периодов блок «План → факт за месяц» (медиана, худшие 10%, доля хуже -0.5 п.п., до 3 худших сделок с #id и направлением) или строку «мало сделок с фактом (меньше 3)»
- оценочные сделки (fact_source 'plan'/'plan±') и сделки без факта никогда не влияют на числа блока
- окно блока = trades.period_start("month"), как у «За месяц» и «По направлениям за месяц»; в тексте нет «->» и «nan»
- `python -m pytest -q` зелёный, `python scripts/guard.py` -> «guard: ок»; diff PR не больше 220 строк вместе с тестами и записью в Журнал

**Не делать:**
- не менять trades.stats, trades.match_fact, trades.set_fact, trades.by_direction, bot.direction_lines и схему таблицы trades; не менять существующие строки /stats, только вставить блок
- не вводить новых аргументов команды /stats, новых env-настроек и новых команд; не трогать GUEST_CMDS, GUEST_CALLBACKS и остальные константы/функции, закреплённые tests/test_payout_pins.py (имена в bot.py, начинающиеся с payout/PAYOUT_/GUEST_, не создавать)
- не использовать в добавленных строках (в том числе в тестах) слова payout, pay_ok/pay_to/pay_no/pay_hist/pay_stop, TRADING, trd_, `trading.`, `import trading`, ссылки http(s), subprocess, eval(, exec(; не добавлять сетевых вызовов и .post(/.get( на клиентах; не называть файлы и функции словами payout/trading
- не писать «->» в тексте для Telegram (только «→») и не выводить в новом тексте фразы «факт указан у», «сделок нет» (на них есть негативные проверки в tests/test_bot.py:1918 и :3140)
- не брать окно «30 дней» (PERIODS['month']) - только календарный месяц trades.period_start("month"); не выводить route сделки в блок
- не читать .env, data/keys.json, папку ключ; в тестах data/ не трогать, базы только в tmp_path или через изолированный trades.DB_PATH
- не править CLAUDE.md, scripts/guard.py, tests/conftest.py, .github/, requirements.txt; в ROADMAP.md добавить только запись в «Журнал»

**Где смотреть в коде (проверено на origin/main ad65d71):**
- trades.py:406 def stats: считает только AVG(fact - profit) как avg_diff по _REAL_FACT, медианы/хвоста нет (проверено)
- trades.py:78 _REAL_FACT = "fact IS NOT NULL AND (fact_source IS NULL OR fact_source NOT IN ('plan', 'plan±'))"; trades.py:505 is_estimate по PLAN_SOURCES
- trades.py:454 facts_by_pair - образец запроса по _REAL_FACT с since и проверкой os.path.exists; trades.py:259 log_trade (profit = плановый %), trades.py:291 set_fact(source=...)
- trades.py:413 stats: month = _month_start(now) (календарный месяц), tests/test_trades.py test_stats_month_is_calendar_month_not_rolling_30_days; bot.py:2078 direction_lines(trades.by_direction(trades.period_start("month")))
- bot.py:2058 stats_view собирает строки периодов, затем direction_lines, контрагентов; bot.py:1427 direction_lines - модульный прецедент с html.escape и «→»; bot.py:4099 /stats отправляет stats_view() через self.send
- tests/test_stats_direction.py - готовый шаблон тестов (log/set_fact с ts, Stub(B.Bot), trades.DB_PATH подменён conftest)
- scripts/guard.py: PROTECTED/PROTECTED_NAMES не задевают trades.py, bot.py, tests/test_plan_fact_spread.py; PAYOUT_CODE/TRADING_CODE/FORBIDDEN на наброске добавляемых строк не срабатывают
- grep на origin/main: spread_lines, plan_fact_spread, BAD_DIFF_PP не найдены; ROADMAP «Идеи»/«Журнал»: медианы/худших сделок план→факт в /stats нет; ни одна удалённая ветка не добавляет median/worst в trades.py/bot.py

**Пересечения с другими задачами и ветками:** Тема не занята: нет ветки claude/plan-fact-*, в Журнале и в очереди worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports, cross-coin part 2, hedge) её нет; cloud/s2-stats-direction (by_direction) уже влита и не дублируется; reputation.from_trades (по нику мерчанта) и paper._median (по кругам сухого прогона) - другие данные. Правит те же файлы, что соседние задачи (trades-breakdown-hour-bank-coin меняет by_direction/stats_view, signal-trade-funnel дописывает конец) - запускать последовательно; чтобы уменьшить конфликты: trades.plan_fact_spread - сразу после facts_by_pair, bot.spread_lines - сразу после direction_lines, вставка в stats_view - одной строкой между циклом периодов и direction_lines; при конфликте перенести вставку, не меняя чужих строк.

---
### Задача 4. `data-health-status` — Здоровье данных в /status: свободный диск, размеры баз и логов, возраст копии; алерт при нехватке места

**Ценность:** 3/5 · **Размер:** M · **Ветка:** `claude/data-health-status`

**Зачем:** Сейчас бот не знает, сколько осталось места на диске. Когда диск заканчивается, тихо перестают писаться history.db, trades.db и снимки, и владелец узнаёт об этом задним числом. Блок здоровья данных в подробном статусе и разовый алерт в dev-топик закрывают эту дыру без сети и без побочных действий (только чтение).

**Что сделать:** 1) Новый модуль health.py (импортирует только os, glob, shutil, time, backup, p2p; без сети, без subprocess, без env-переменных). Константы LOW_FREE = 1_000_000_000, DISK_CHECK_EVERY = 3600. Функция snapshot(data_dir=None, log_path=None): пути разрешаются в момент вызова (None -> backup.DATA_DIR / p2p.LOG_PATH), чтобы conftest мог их перенаправить и тест-хук не падал на реальных data/logs. Возвращает dict: free_bytes и total_bytes (через shutil.disk_usage(data_dir); при OSError оба None), dbs (только файлы data_dir/*.db, пары (имя, размер) по убыванию размера, топ-5), logs_bytes (сумма размеров bot.log и bot.log.* рядом с log_path через os.stat/glob), backup_count и backup_last_ts (через backup.copies(data_dir) и backup._ts последней копии; при отсутствии копий 0 и None). Любые OSError на отдельных файлах гасятся. Содержимое файлов не читается, список ограничен именами *.db. fmt_size(n): байты в 'X.Y МБ' или 'X.Y ГБ' с одним знаком (None -> '?'). lines(snap, now): список строк '💾 Диск: свободно X ГБ из Y; базы: name1 A МБ, name2 B МБ; логи Z МБ' (при free_bytes None: 'Диск: нет данных') и 'Копия баз: N ч назад, копий K' либо 'Копий баз ещё нет'. low(free_bytes): True, если free_bytes не None и меньше LOW_FREE. Внутри модуля значения вызываются через атрибуты модуля health (health.free_bytes и т.п. как отдельные функции-обёртки), чтобы тесты могли их monkeypatch-ить. 2) bot.py, status_view (~2840): после строки 'Аптайм' и до раннего return при snap is None добавить строки health.lines(health.snapshot(), time.time()) внутри try/except Exception as e -> logger.warning('data health: %s', e). Существующие блоки и текст не менять, на ветке scan-step-isolation при конфликте оставить оба блока. 3) bot.py, Bot.__init__: self.disk_alerted = False, self.disk_checked_ts = time.time() (не 0, чтобы монкипатченное time.time в существующих тестах watchdog не запускало проверку). Новый async disk_check(self, now=None): now = now or time.time(); если now < self.disk_checked_ts или now - self.disk_checked_ts < DISK_CHECK_EVERY, вернуть None; иначе self.disk_checked_ts = now и free = health.snapshot()['free_bytes']. Если free is None, ничего не делать. Если health.low(free) и не disk_alerted: r = await self.send('⚠️ На диске осталось X ГБ (порог 1 ГБ): базы и снимки могут перестать писаться. Проверьте data/ и logs/', topic='dev'); self.disk_alerted = True только если (r or {}).get('ok') (иначе повтор в следующий час). Если disk_alerted и free > 1.5 * LOW_FREE: отправить '✅ Место на диске восстановилось' в dev-топик и сбросить disk_alerted при ok. 4) watchdog_loop: вызвать await self.disk_check() внутри существующего try после watchdog_check, но в собственном вложенном try/except Exception as e: logger.warning('disk_check: %s', e), чтобы сбой проверки диска не влиял на watchdog_check. scan_loop, watchdog_check, watchdog_message не трогать. 5) ROADMAP.md: отметить пункт [x] и добавить строку в Журнал (2026-09-29 или дата запуска) по формату CLAUDE.md. Порядок работы: прочитать CLAUDE.md, реализовать, прогнать python -m pytest -q и python scripts/guard.py, пушить ветку claude/data-health-status.

**Править:** `bot.py`, `ROADMAP.md`
**Создать:** `health.py`, `tests/test_health.py`

**Тесты (офлайн):**
- tests/test_health.py: tmp data dir с trades.db и history.db (несколько байт), фейковый keys.json со строкой 'FAKE' (только в tmp_path, чтобы проверить, что snapshot его не учитывает) и папкой backup/20260928-0930; snapshot перечисляет только *.db, не keys.json и не backup
- tests/test_health.py: fmt_size (МБ и ГБ, один знак, None), lines (обычный случай, 'Копий баз ещё нет', free_bytes None), low на границе LOW_FREE
- tests/test_health.py: shutil.disk_usage бросает OSError -> snapshot отдаёт free_bytes None без исключения
- tests/test_health.py на Stub(B.Bot): disk_check алерт при low и disk_alerted=True; повтор в пределах интервала не шлёт; не дублирует при disk_alerted; при r без ok (call вернул {'ok': False}) disk_alerted остаётся False и алерт повторится; восстановление при free > 1.5 * LOW_FREE шлёт '✅' и сбрасывает флаг; now раньше disk_checked_ts -> no-op
- tests/test_health.py: status_view со сломанным health.snapshot (raise) не падает, остальной текст статуса на месте, в тексте нет строки диска
- существующие tests/test_help_status_ux.py, tests/test_scan_watchdog.py, tests/test_bot.py, tests/test_isolated_data.py, tests/test_trading_surface.py, tests/test_env_documented.py остаются зелёными

**Критерии приёмки:**
- python -m pytest -q зелёный, python scripts/guard.py зелёный
- «📋 Подробно» (status_view) показывает строку диска с базами и логами и строку возраста копии либо «Копий баз ещё нет»; при сбое health бот отвечает статусом без этих строк и пишет logger.warning
- При свободном месте меньше 1 ГБ в dev-топик уходит ровно одно предупреждение, при повторных проверках без изменений дубля нет; после успешной доставки флаг взведён, при неуспешной повторяется через час
- После роста свободного места выше 1.5 ГБ приходит одно сообщение о восстановлении и флаг сбрасывается
- Модуль health только читает: os.stat, glob, shutil.disk_usage, backup.copies, backup._ts; нет сети, subprocess, env-переменных, записей и удалений файлов
- Изменены только bot.py, ROADMAP.md, health.py, tests/test_health.py; в ROADMAP.md пункт отмечен [x] и есть строка Журнала

**Не делать:**
- не трогать launcher.py, .github/, CLAUDE.md, scripts/guard.py, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, .env*, *.bat, trading/ и любые пути со словами payout/trading
- не добавлять env-переменных, зависимостей и новых доменов; не использовать subprocess, os.system, .post( вне trading/
- не читать содержимое keys.json и не упоминать это имя в коде health.py и bot.py (в тесте фейковый keys.json допустим только в tmp_path); из data/ смотреть только имена и размеры *.db
- не удалять и не чистить файлы автоматически, не делать VACUUM, ничего не писать на диск из health.py
- не править scan_loop, watchdog_check, watchdog_message, backup.py и run_backup; не менять текст существующих строк status_view
- не использовать в добавленных строках слова position, order, margin, withdraw, payout, TRADING (срабатывают guard и test_trading_surface)
- не разрешать пути в значениях по умолчанию при определении функции (только None и разрешение при вызове); не обращаться к реальным data/logs из тестов, только tmp_path

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:2840-2879 status_view(self, status_path=DEV_STATUS): строка 'Аптайм' и ранний return при snap is None, блока про диск нет
- bot.py:2727-2738 watchdog_loop: while True, sleep(WATCHDOG_TICK), try watchdog_check except logger.error('watchdog: %s') - точка вызова disk_check; WATCHDOG_TICK = 60 (bot.py:270)
- bot.py:2740-2758 schedule_backup/run_backup: сбой копии только в logger.warning('backup: %s')
- bot.py:1537-1560 Bot.__init__ (backup_task, start_ts, stall_alerted, watchdog_wake_ts): сюда добавляются disk_alerted и disk_checked_ts; bot.py:21 import backup; bot.py:1632 send(..., topic=None)
- backup.py: DATA_DIR, copies(data_dir), _ts(name), папки YYYYMMDD-HHMM; snapshots.db и keys.json намеренно не копируются
- p2p.py:74 LOG_PATH = logs/bot.log; p2p.py:80 setup_logging (RotatingFileHandler 5x1 МБ)
- snapshots.py:72-74 max_bytes() и .env.example:187-190 SNAPSHOT_MAX_MB=1500: снимки сами себя ограничивают, но не следят за свободным местом диска
- grep disk_usage|getsize по репозиторию: только launcher.py:114 и тесты, проверки свободного места в боте нет
- tests/test_scan_watchdog.py:168-230: тесты watchdog_loop подменяют B.time.time и B.asyncio.sleep - disk_check должен оставаться no-op при now < disk_checked_ts
- tests/conftest.py: блок сети, перенаправление state-путей в tmp data/logs и хук PermissionError на реальные data/logs - тесты и snapshot() обязаны работать через tmp_path и пути, разрешаемые при вызове

**Пересечения с другими задачами и ветками:** Прямых пересечений нет: ни одна из 50 удалённых веток и ни одна запись Журнала ROADMAP не касаются места на диске, размеров баз/логов и возраста копии в статусе. Сосед backup-integrity-check правит backup.py и run_backup, а эта задача только читает backup.copies/_ts и правит status_view, __init__ и watchdog_loop. Сосед scan-step-isolation добавляет блок «Сбои шагов скана» в status_view: при конфликте слияния в status_view оставить оба блока (порядок не важен). Очередь worker 1 эти функции не затрагивает.

---
### Задача 5. `signal-reason-codes-contract` — Коды причин 'сигнал не ушёл': тест-контракт Bot.signal_reasons против history.SIGNAL_REASONS и NOT_MISSED, убрать мёртвую p2p.sell_depth_ok

**Ценность:** 2/5 · **Размер:** S · **Ветка:** `claude/signal-reason-codes-contract`

**Зачем:** history.SIGNAL_REASONS нигде не используется (мёртвая константа), а реальный набор причин живёт строковыми литералами в Bot.signal_reasons. Доля пропущенных связок в дайджесте считается через NOT_MISSED (history.py:259): новая причина, добавленная в бот без обновления history, молча попадёт в 'пропущенные' и исказит долю. Тест закрепляет единый источник правды. Заодно p2p.sell_depth_ok (p2p.py:918-921) не вызывается ни кодом, ни тестами, ни другими ветками, а её докстринг врёт, что она нужна paper.py (paper.py:792 давно использует p2p.sell_fill_price).

**Что сделать:** 1) Новый tests/test_signal_reason_codes.py (имя без слов payout/trading; импорты: ast, os, pytest, history). Путь к bot.py: os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot.py'); читать open(..., encoding='utf-8') (в файле кириллица, на Windows cp1251 по умолчанию). Чистая функция _reason_literals(source) -> set[str]: ast.parse(source), найти ровно один класс Bot, в нём ровно один метод signal_reasons (иначе pytest.fail с понятным текстом, а не пустое множество), в теле через ast.walk взять каждый ast.Assign, у которого цель Name('reason'), и собрать все ast.Constant со значением str из его value (это покрывает IfExp 'quiet' if quiet else 'paused'; reason = None пропускается). Тесты: (a) test_bot_reason_literals_equal_history_constant: _reason_literals(bot.py) == set(history.SIGNAL_REASONS), сообщение об ошибке называет расхождение в обе стороны и говорит 'обновить history.SIGNAL_REASONS и решить, входит ли причина в NOT_MISSED'; (b) test_not_missed_is_subset_of_reasons: set(history.NOT_MISSED) <= set(history.SIGNAL_REASONS) и len(set(SIGNAL_REASONS)) == len(SIGNAL_REASONS); (c) test_extractor_sees_a_new_reason: на синтетическом исходнике 'class Bot:\n def signal_reasons(self):\n  reason = "quiet" if q else "paused"\n  reason = "brand_new"\n  reason = None' экстрактор возвращает {'quiet','paused','brand_new'} (доказывает, что девятая причина сделает (a) красным, без правки bot.py); (d) parametrize по history.SIGNAL_REASONS, test_reason_round_trips_and_is_classified(tmp_path, reason): в БД tmp_path/'history.db' записать один ключ K=('Bybit','USDT','MEXC','USDT') 16 сканами с шагом 20 с от t0 (t0 = 1_700_000_000.0) через history.track_signals([(K, 1.5, False, reason)], ts, ids, path=db), затем history.signal_stats(path=db, now=t0 + 700, cooldown=600) (образец tests/test_history.py:195-215): для reason из NOT_MISSED ожидать missed == 0 и excluded_reasons == {reason: 1}, для остальных missed == 1 и reasons == {reason: 1}; заодно проверить SELECT reason_not_signalled FROM signals через sqlite3, что строка сохранилась без изменений. 2) p2p.py: удалить целиком def sell_depth_ok (p2p.py:918-921: def, докстринг из двух строк, return) вместе с одной из двух пустых строк-разделителей так, чтобы между концом _stack_qty и def sell_fill_price остались ровно две пустые строки; ничего больше в p2p.py не менять. Перед удалением выполнить grep -rn sell_depth_ok по всему репозиторию: в .py и tests/ других ссылок быть не должно (ROADMAP.md:43 и :1463 - исторические, не править). 3) history.py: над строкой SIGNAL_REASONS = ... (history.py:135) добавить один комментарий на русском: набор кодов сверяется тестом tests/test_signal_reason_codes.py с Bot.signal_reasons; при добавлении причины обновить и NOT_MISSED, если она не считается пропуском. Значение константы и логику не менять. 4) ROADMAP.md: по протоколу CLAUDE.md п.4 дописать в конец Журнала одну строку с датой и итогом (контракт кодов причин + удалена sell_depth_ok); больше ничего в ROADMAP.md не править.

**Править:** `p2p.py`, `history.py`, `ROADMAP.md`
**Создать:** `tests/test_signal_reason_codes.py`

**Тесты (офлайн):**
- tests/test_signal_reason_codes.py::test_bot_reason_literals_equal_history_constant
- tests/test_signal_reason_codes.py::test_not_missed_is_subset_of_reasons
- tests/test_signal_reason_codes.py::test_extractor_sees_a_new_reason
- tests/test_signal_reason_codes.py::test_reason_round_trips_and_is_classified
- регресс: tests/test_history.py, tests/test_signal_reasons.py, tests/test_paper*.py, tests/test_route.py, tests/test_trading_surface.py (после удаления sell_depth_ok)

**Критерии приёмки:**
- python -m pytest -q tests/test_signal_reason_codes.py зелёный на origin/main + правка; test_extractor_sees_a_new_reason доказывает, что новая девятая причина в Bot.signal_reasons без правки history.SIGNAL_REASONS делает test_bot_reason_literals_equal_history_constant красным
- grep -rn sell_depth_ok --include=*.py . пуст; grep -n 'def sell_depth_ok' p2p.py пуст; sell_fill_price и withdraw_open на месте
- python -m pytest -q и python scripts/guard.py зелёные (guard: ок, без строк 'изменён код выплат/торговый код'); git diff --stat origin/main: не больше 100 изменённых строк, файлы только p2p.py, history.py, ROADMAP.md, tests/test_signal_reason_codes.py
- значения history.SIGNAL_REASONS и history.NOT_MISSED и коды/порядок веток Bot.signal_reasons побайтно не изменены

**Не делать:**
- не менять NOT_MISSED и семантику 'пропущено', не менять коды причин и порядок веток signal_reasons, не трогать bot.py
- не удалять paper.RESULTS и paper.RISK_LABELS (тоже мёртвые), paper.py правят соседние ветки
- не трогать p2p.withdraw_open (используется в tests/test_paper_stage2.py:256) и p2p.sell_fill_price/_stack_qty
- ROADMAP.md: не править исторические упоминания sell_depth_ok на строках :43 и :1463, только добавить строку в конец Журнала; не править CLAUDE.md, scripts/guard.py, tests/conftest.py, pytest.ini
- не импортировать bot и не создавать Bot в новом тесте: читать bot.py как текст и разбирать ast.parse; не использовать eval( и exec( (guard FORBIDDEN), open() без encoding='utf-8'
- в новых строках не писать слова payout, TRADING, trd_, .post(, order/position/margin/withdraw-эндпоинты; имена тестов и функций без payout/trading
- не использовать asyncio.run в тесте (задача tests-arun-audit у воркера 1), не добавлять зависимости, сетевые вызовы, новые домены

**Где смотреть в коде (проверено на origin/main ad65d71):**
- history.py:135: SIGNAL_REASONS = ('unconfirmed', 'max_signals', 'cooldown', 'quiet', 'trap', 'paused', 'unsent', 'stale'); grep по .py даёт единственное вхождение имени
- bot.py:3061 def signal_reasons в class Bot (bot.py:1490); присваивания reason на bot.py:3076-3091: None, 'quiet' if quiet else 'paused', 'trap', 'stale', 'max_signals', 'unconfirmed', 'cooldown', 'unsent'; python-прогон ast-разбора: множество из 8 литералов == set(SIGNAL_REASONS)
- history.py:178 NOT_MISSED = ('quiet','paused','cooldown','trap'); history.py:259 'if reason in NOT_MISSED' иначе причина считается пропуском
- p2p.py:918-921 def sell_depth_ok(ads, qty); paper.py:792 использует p2p.sell_fill_price; git log -S sell_depth_ok: 86c08dc (#87) добавила, 3b776e9 (#98) убрала вызов из paper.py; git grep по всем 50 origin/* веткам: только определение
- tests/test_history.py:195-215 образец _scans/signal_stats(now=t0+700, cooldown=600) для эпизода из 16 сканов по 20 с; tests/test_history.py:187 подтверждает, что причина из NOT_MISSED попадает в excluded_reasons, остальные в reasons; pytest.ini: pythonpath = . tests
- scripts/guard.py: PROTECTED/PROTECTED_NAMES/PROTECTED_BASENAMES не задевают p2p.py, history.py, tests/test_signal_reason_codes.py; PAYOUT_CODE/TRADING_CODE/FORBIDDEN проверены python-ом на удаляемых и синтетических новых строках

**Пересечения с другими задачами и ветками:** В ROADMAP.md (Очередь, Идеи, Журнал) темы контракта кодов причин нет; 'эпизоды сигналов и доля пропущенных' сделаны раньше, но без сверки кодов. Ни одна из 50 удалённых веток не использует p2p.sell_depth_ok и не трогает SIGNAL_REASONS; правки p2p.py в cloud/audit-paper-replay, cloud/bc-reliability-v2, claude/paper-route-hops-venues, cloud/cross-coin-part2 находятся в других местах файла, а cloud/s2-venue-outages уже влита (0b47f7e). Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, reports/digest, cross-coin part 2, hedge/gates/trading) не пересекается; существующий tests/test_signal_reasons.py не трогаем, новый файл отдельный, чтобы не создавать конфликтов слияния.

---
Когда все задачи волны сделаны или помечены «ждёт владельца», напиши итог: список веток, что влито, что ждёт владельца. Следующую волну не начинай — её вставит владелец.
