# Рабочая сессия 2 — репозиторий Sonti22/p2p-bot, Волна 5: тяжёлые выгрузки и финальный контракт команд

Ты — облачный Claude Code, второй исполнитель проекта Telegram-бота @p2psckabot (P2P-арбитраж в рублях, ручная торговля владельца). Первый исполнитель («Межмонетные связки, часть 2») работает параллельно в другой ветке. Ниже — 3 задач этой волны. Делай их строго по одной, в указанном порядке.

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

## Задачи Волна 5: тяжёлые выгрузки и финальный контракт команд

**Почему такой состав волны:** trades-breakdown идёт после plan-fact-spread и signal-trade-funnel (те же trades.py и /stats), paper-cycles-csv работает с paper.py и веткой /paper. command-contract-test намеренно последний: к этому моменту в main уже есть новые /signals (волна 1) и /filters (волна 2), так что тест ловит настоящие расхождения, а не заставляет чужие задачи править справку, README и owner-guide.

**Условие старта:** все задачи волн 1–4 уже влиты в main (проверь `git log origin/main`). Если нет — остановись и напиши владельцу, что ждёшь.

### Задача 1. `trades-breakdown-hour-bank-coin` — /stats hours|dow|banks|coins: разбивка реальных сделок по часу (МСК), дню недели, банку оплаты и монете

**Ценность:** 3/5 · **Размер:** M · **Ветка:** `claude/trades-breakdown-hour-bank-coin`

**Зачем:** /stats показывает периоды и разбивку по направлениям, но не отвечает на вопросы владельца «в какие часы мои сделки лучше», «через какой банк оплаты факт хуже расчёта», «на какой монете я реально зарабатываю». Данные уже есть в trades (ts, bank, buy_asset/sell_asset, profit, fact, fact_source), нужна только агрегация без миграций. Для бумажных кругов разбивка по меткам есть (paper.label_stats), для реальных сделок - нет; /history по часу относится к спредам рынка, а не к сделкам владельца.

**Что сделать:** 1) trades.py (рядом с by_direction/facts_by_pair, без SQL по датам - группировка в Python): константа BREAKDOWN_KINDS = ('hour', 'dow', 'bank', 'coin') и функция breakdown(kind, since=0.0, path=DB_PATH) -> list[dict]. Неизвестный kind -> ValueError. Строки брать через export_rows(since, path=path) (он сам возвращает [] если файла базы нет; строки - словари со всеми колонками). Ключ группы: hour = datetime.datetime.fromtimestamp(row['ts'], MSK).hour (int 0..23); dow = ...weekday() (int, 0=Пн); bank = BANK_NAMES.get(bank, bank) если колонка bank не пуста, иначе строка 'не указан' (колонка bank = банк оплаты: при 'intra' общий банк с мерчантом, при 'sbp' свой банк-отправитель); coin = buy_asset, если buy_asset == sell_asset, иначе f'{buy_asset}→{sell_asset}' (стрелка '→', НЕ '->'; None -> '?'). Строки без ts пропускать. Реальный факт = row['fact'] is not None and not is_estimate(row) (эквивалент _REAL_FACT: оценки fact_source 'plan' и 'plan±' не входят). Для каждой группы словарь {'key', 'count' (все сделки), 'amount' (сумма amount, None считать 0), 'avg_plan' (среднее profit по всем сделкам группы), 'real_n' (сделок с реальным фактом), 'avg_fact' (среднее fact только по реальным, иначе None), 'avg_diff' (среднее fact - profit только по реальным, п.п., иначе None), 'rub' (сумма fact_rub(row) только по реальным, иначе None)}. Сортировка: hour и dow - по key по возрастанию; bank и coin - по count по убыванию, затем по key (детерминированно). trades.stats, by_direction, facts_by_pair, схему и _connect не менять. 2) bot.py, рядом с direction_lines (bot.py ~1424): константы BREAKDOWN_DAYS = 90, BREAKDOWN_MIN_FACTS = 3, BREAKDOWN_LINES = 24, словарь алиасов аргумента {'hours': 'hour', 'hour': 'hour', 'часы': 'hour', 'dow': 'dow', 'days': 'dow', 'дни': 'dow', 'banks': 'bank', 'bank': 'bank', 'банки': 'bank', 'coins': 'coin', 'coin': 'coin', 'монеты': 'coin'}, DOW_NAMES = ('Пн','Вт','Ср','Чт','Пт','Сб','Вс') и функция уровня модуля breakdown_lines(kind, rows, limit=BREAKDOWN_LINES) -> list[str] (чистая, без Bot, как direction_lines). Заголовок жирным: 'Сделки по часам (МСК)' / 'по дням недели (МСК)' / 'по банку оплаты' / 'по монете' плюс ' за 90 дн.'. Строка группы в стиле direction_lines: '• 14:00: 5 сд., 25 000 ₽, расчёт +1.20%' (метка часа f'{h:02d}:00', дня - DOW_NAMES) и далее либо ', факт +0.95% (у 3) ≈ +210 ₽, к расчёту -0.25 п.п.' (rub форматировать как в direction_lines: f'{rub:+,.0f}'.replace(',', ' '), деньги через _money), либо ', факта нет' если real_n == 0; если real_n < BREAKDOWN_MIN_FACTS - добавить ' · мало данных'. Метки банка и монеты пропускать через html.escape (сообщения идут с parse_mode=HTML). Больше limit групп - обрезать и добавить строку '• …ещё N'. Нет сделок - одна строка 'Сделок за 90 дн. нет'. 3) bot.py, Bot.stats_view: сигнатура stats_view(self, arg=''); в самом начале ранний выход: kind = алиас по arg.split()[0].lower() (если arg не пуст); известный -> return '\n'.join(breakdown_lines(kind, trades.breakdown(kind, since=time.time() - BREAKDOWN_DAYS * 86400))); неизвестный непустой -> return строку-подсказку 'Не знаю «{html.escape(arg)}». Доступно: /stats hours|dow|banks|coins' (аргумент экранировать). Остальное тело stats_view НЕ трогать (другие задачи дописывают в него строки: минимизировать конфликты) - при пустом arg поведение и текст побайтно прежние. 4) bot.py диспетчер (~4098-4099): await self.send(self.stats_view(arg)) - arg уже есть в dispatch(cmd, arg). 5) Справка: дописать в HELP_SECTIONS['journal'] (bot.py ~197) отдельной строкой '/stats hours, dow, banks, coins - сделки по часу (МСК), дню недели, банку, монете' и, если влезает, в описание stats в COMMANDS (~bot.py:105, до 256 символов); не переписывать соседние строки и не касаться строки COMMANDS с payout. README.md стр. 40: одно предложение про новые аргументы. 6) ROADMAP.md по протоколу: [x] + дата + итог, строка в Журнал.

**Править:** `trades.py`, `bot.py`, `README.md`, `ROADMAP.md`
**Создать:** `tests/test_trades_breakdown.py`

**Тесты (офлайн):**
- tests/test_trades_breakdown.py: tmp_path база; сделки через trades.log_trade(deal, amount, path=db, ts=ts) c deal = (profit, make_ad(...buy...), make_ad(...sell...), 'route') из tests/helpers.py (как d() в tests/test_stats_direction.py); факт через trades.set_fact(tid, fact, path=db, source=trades.FACT_MANUAL / FACT_PLAN / FACT_PLAN_SHIFT / FACT_AUTO). ts строить через datetime.datetime(2026, 9, 21, 23, 30, tzinfo=datetime.timezone.utc).timestamp(), а не магическим числом
- hour/dow: сделка 2026-09-21 23:30 UTC (Пн) попадает в hour 2 и dow 1 (Вт) по МСК - переход через полночь; сделка 20:59 UTC того же дня - hour 23 и dow 0; результат hour/dow отсортирован по key
- bank: две сделки pays=('Sberbank',) и одна pays=('Tinkoff',) -> ключи 'Сбер' (count 2) и 'Т-Банк' (count 1) в порядке убывания count; сделка с pays=('Cash',) (bank пуст) -> ключ 'не указан'
- coin: USDT/USDT -> 'USDT'; покупка USDT, продажа USDC -> 'USDT→USDC'; порядок по count убыв., затем по key
- реальный факт: в одной группе сделки с fact_source manual, plan, plan± и без факта -> count 4, real_n 1, avg_fact и rub считаются только по manual (rub = amount*fact/100, avg_diff = fact - profit), avg_plan - по всем четырём; fact_source auto и NULL-источник (set_fact без source) считаются реальными
- группа без реального факта -> avg_fact/avg_diff/rub None; breakdown_lines не падает и выводит ', факта нет'; real_n < 3 -> в строке 'мало данных'
- since отсекает старые сделки; неизвестный kind -> ValueError; несуществующий файл базы -> []; breakdown не создаёт файл базы
- breakdown_lines: >24 групп -> ровно 24 строки групп + '…ещё N'; пустой список -> 'Сделок за 90 дн. нет'; метка банка с '<' экранируется; в тексте нет '->'
- bot (Stub по образцу tests/test_stats_direction.py, сделки пишутся в trades.DB_PATH с ts=None - база изолирована conftest): bot.stats_view() и bot.stats_view('') дают одинаковый текст; handle('/stats hours') содержит ':00' и 'МСК'; handle('/stats banks') содержит название банка; handle('/stats xyz') содержит подсказку 'hours|dow|banks|coins'; handle('/stats <b>') экранирует аргумент; существующие tests/test_stats_direction.py и stats_view-тесты в tests/test_bot.py остаются зелёными без правок

**Критерии приёмки:**
- /stats без аргумента даёт тот же текст, что и раньше (tests/test_stats_direction.py и stats_view-тесты в tests/test_bot.py зелёные без изменений); /stats hours, /stats dow, /stats banks, /stats coins (и русские алиасы часы/дни/банки/монеты) возвращают таблицу по реальным сделкам за 90 дней с заголовком, где указан период
- оценки fact_source 'plan' и 'plan±' не попадают в avg_fact, avg_diff и rub, но учитываются в count, amount и avg_plan (проверено тестом)
- сделка в 23:30 UTC попадает в час 2 МСК следующих суток и в следующий день недели (проверено тестом)
- в добавленных строках нет '->', слов payout, TRADING, trd_, 'trading.', ссылок http(s), .post( и каких-либо вызовов сети; пользовательский аргумент и метки банка/монеты экранируются html.escape
- python -m pytest -q зелёный (включая tests/test_trading_surface.py и tests/test_payout_pins.py без правок), python scripts/guard.py -> 'guard: ок'; diff не больше 300 строк

**Не делать:**
- не менять trades.stats, trades.by_direction, trades.facts_by_pair, trades._connect и схему таблицы trades (никаких миграций, никаких новых колонок)
- не менять текст и порядок строк /stats без аргумента и не переписывать тело stats_view: только сигнатура, ранний return в начале и вызов в диспетчере
- не трогать GUEST_CMDS, GUEST_* и остальные константы и функции, закреплённые в tests/test_payout_pins.py; команда остаётся только для владельца (гостю по-прежнему GUEST_DENIED)
- не использовать в добавленных и изменённых строках слова payout, PAYOUT, TRADING, trd_, 'trading.', 'import trading', пути вида /v5/..., а также ссылки http(s); не трогать строку COMMANDS с payout и соседние строки справки
- не писать '->' в текстах сообщения (HTML parse_mode) - только '→'; всё, что подставляется в сообщение (банк, монета, аргумент), пропускать через html.escape
- не добавлять новые зависимости, домены, subprocess/os.system, .post(, сетевые вызовы; никаких SQL-функций дат - только группировка в Python
- не читать .env, data/keys.json, папку ключ; тесты не должны трогать data/ (использовать tmp_path или изолированный trades.DB_PATH из conftest)
- не править tests/conftest.py, tests/test_trading_surface.py, tests/test_payout_pins.py, scripts/guard.py, CLAUDE.md

**Где смотреть в коде (проверено на origin/main ad65d71):**
- trades.py:25 MSK = timezone(timedelta(hours=3)); :50 BANK_NAMES; :78 _REAL_FACT; :406 stats (только периоды, avg_diff); :434 by_direction (разбивка только по направлению); :454 facts_by_pair; :492 export_rows (SELECT * WHERE ts >= ? AND ts < ?, [] если базы нет); :505 is_estimate; :510 fact_rub(row)
- trades.py:259 log_trade пишет ts, buy_asset, sell_asset, amount, profit, bank, kind (bank - банк оплаты из pay_plan, '' если способ неизвестен); :291 set_fact(trade_id, fact_percent, path, source)
- bot.py:1424-1449 STATS_DIRECTIONS и direction_lines (образец форматирования, html.escape, '…ещё N'); bot.py:2058 def stats_view(self); bot.py:4098-4099 elif cmd == '/stats': await self.send(self.stats_view()); bot.py:4076-4078 handle делает partition(' ') -> arg приходит в dispatch(cmd, arg); bot.py:84-85 GUEST_CMDS без /stats; bot.py:1635 parse_mode=HTML
- bot.py:105 COMMANDS stats; bot.py:197 HELP_SECTIONS['journal'] строка '/stats - журнал за день...'; tests/test_help_status_ux.py:60-63 проверяет длину разделов < 4096
- tests/conftest.py:298-394 подмена path=DB_PATH по умолчанию у всех функций модулей бота; tests/test_stats_direction.py - шаблон Stub и arun; tests/test_bot.py:1918,1933,3140 вызывают bot.stats_view() без аргументов
- paper.py:1089 label_stats (разбивка только бумажных кругов); history.py:311/320 hourly_avg/heatmap (спреды рынка, не сделки владельца)
- ROADMAP Журнал: /stats по направлениям сделан 2026-09-28 (bb14f92), разбивок по часу/банку/монете реальных сделок нет; git grep по 50 веткам на breakdown/by_hour/by_bank/by_coin пуст

**Пересечения с другими задачами и ветками:** Не пересекается ни с одной удалённой веткой и записью Журнала (cloud/s2-stats-direction уже в main: by_direction). Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly digest, cross-coin part 2, hedge/gates) не затрагивает trades.py/stats_view; недельный дайджест может читать trades.by_direction - её не менять. Соседние задачи из этого же прогона (plan-fact-spread, signal-trade-funnel) тоже дописывают в /stats: запускать последовательно, эту первой (она вводит сигнатуру stats_view(self, arg='')); благодаря раннему return и отдельной breakdown_lines конфликт возможен только в строке def stats_view и в строке диспетчера.

---
### Задача 2. `paper-cycles-csv` — /paper cycles [дней]: построчная выгрузка кругов бумаги в CSV для Excel

**Ценность:** 3/5 · **Размер:** M · **Ветка:** `claude/paper-cycles-csv`

**Зачем:** /paper report даёт только агрегаты по меткам и связкам; чтобы разобрать конкретные круги (в какой час, на какой связке и при каком плане круг сорвался или недобрал, сколько занял перевод, какое было проскальзывание покупки), владельцу нужна таблица «один круг = одна строка». Данные уже лежат в SQLite-таблице cycles, но наружу отдаётся только сводка. Выгрузка открывается в русском Excel (utf-8-sig, ';', десятичная запятая), не выполняет формулы и по числам совпадает с /paper report (план с запасом COALESCE(planned_raw, planned_pct)). Нужна и как основа для будущих сводок и бэктестов. Только чтение собственной SQLite, без сети и денежного кода.

**Что сделать:** 1) paper.py, сразу после write_report_csv (около строки 1118-1132), добавить: константу CYCLES_CSV_PATH = os.path.join(HERE, "data", "paper_cycles.csv") рядом с REPORT_CSV_PATH (так conftest.py автоматически уведёт путь в tmp в тестах; писать в живой data/ из тестов нельзя). Кортеж CYCLES_COLUMNS с русскими заголовками, ровно в таком порядке: "Старт (МСК)" (формат ДД.ММ.ГГГГ ЧЧ:ММ по MSK), "Час (МСК)" (int 0..23), "Связка" (route, т.е. buy_ex->sell_ex), "Покупка (монета)", "Продажа (монета)", "Сумма", "Банк", "Метка", "План с запасом, %" (COALESCE(planned_raw, planned_pct)), "План без запаса, %" (planned_raw), "Факт, %" (realized_pct только у done), "Факт-план, п.п." (realized_pct минус COALESCE(planned_raw, planned_pct), только у done), "Итог" ("исполнен" / "срыв: покупка|перевод|продажа" через FAIL_LABELS / "открыт"), "Причина срыва" (REASON_LABELS[fail_reason(note)] только для failed_*; если причины нет — пусто), "Покупка, мин", "Перевод факт, мин", "Продажа, мин" (длительности этапов из ts_start/ts_buy_done/ts_transfer_done/ts_sell_done по формуле (b-a)/60, как _measures; пусто если одной из отметок нет), "Перевод план, мин" (столбец transfer_min), "Проскальзывание покупки, %" (buy_slip_pct), "Примечание" (note), "Маршрут" (полный route/хопы, если такой столбец есть в _COLUMNS; иначе не добавлять колонку). Сверить реальные имена столбцов с _COLUMNS/миграциями в _connect (paper.py:161) и STAGES/RESULTS. 2) export_cycles(since: float = 0.0, path: str = DB_PATH) -> list[dict]: если not os.path.exists(path) — вернуть [] и НЕ создавать файл (как остальные читатели); иначе через существующий _connect(path) выполнить SELECT * FROM cycles WHERE ts_start >= ? ORDER BY ts_start, id, превратить строки через _dicts(cur) и читать значения ТОЛЬКО по имени столбца (порядок столбцов в БД — порядок миграций, не _COLUMNS). Открытые круги (result не done и не failed_*) включаются с итогом "открыт" и пустыми недостающими длительностями. Вернуть список dict с ключами = CYCLES_COLUMNS (уже готовые значения: числа как float/int/None, текст как str). 3) write_cycles_csv(rows: list[dict], path: str = CYCLES_CSV_PATH) -> str: os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w", newline="", encoding="utf-8-sig"); csv.writer(delimiter=";"); строка заголовков CYCLES_COLUMNS; числа (проценты, сумма, минуты, слип) через trades._num (2 знака, запятая); None -> ""; все текстовые поля (Связка, Банк, Метка, Причина, Примечание, Маршрут, монеты) через trades._csv_text (экранирование = + - @ \t \r); вернуть path. Код повторяет соглашения trades.write_export_csv (trades.py:536). 4) bot.py, cmd_paper (2237-2275): добавить ветку elif sub == "cycles": days = int(rest) если rest.strip().isdigit() иначе 30; зажать в 1..365; since = time.time() - days * 86400; rows = paper.export_cycles(since); если rows пусто — await self.send("за период кругов нет") и НЕ отправлять файл; иначе path = paper.write_cycles_csv(rows); await self.send_document(path, f"Круги бумаги: {len(rows)} шт. за {days} дн."). Не сравнивать с report по стилю: тот же send_document, что уже используется в ветке report (bot.py:1672, Bot.send_document уже в ALLOWED_SENDERS теста поверхности; новых отправителей не добавлять). Обновить docstring cmd_paper и добавить строку в HELP_SECTIONS["paper"] (~204-210): "/paper cycles [дней] — круги в CSV"; в COMMANDS (107) описание paper дополнить, СОХРАНИВ слово "reset" (tests/test_paper_review110.py:230). Секция help должна остаться < 4096 символов (tests/test_help_status_ux.py:62). GUEST_CMDS (84) не менять. 5) tests/test_paper_cycles_csv.py (см. tests). 6) ROADMAP.md: отметить [x] + дата + одна строка в Журнале (по протоколу CLAUDE.md), пункт добавить в очередь, если его там нет. Не трогать docs/owner-guide.md кроме одной строки в списке подкоманд /paper (по желанию).

**Править:** `paper.py`, `bot.py`, `ROADMAP.md`
**Создать:** `tests/test_paper_cycles_csv.py`

**Тесты (офлайн):**
- tests/test_paper_cycles_csv.py::test_header_matches_columns — первая строка CSV = ';'.join(CYCLES_COLUMNS); файл с BOM (utf-8-sig), разделитель ';'
- test_rows_sorted_by_start_and_counted — три круга с разными ts_start, вставленные не по порядку, выходят по возрастанию ts_start
- test_msk_hour_and_start — старт 22:30 UTC => 'Час (МСК)' = 1 и дата следующего дня в колонке старта
- test_done_cycle_diff_uses_planned_raw — done круг с planned_raw=1.5, planned_pct=1.0, realized=1.2 => 'Факт-план, п.п.' = -0,30 (от planned_raw); при planned_raw=None разница считается от planned_pct (fallback)
- test_failed_cycle_blank_realized_and_reason — failed_transfer: 'Факт, %' и 'Факт-план' пустые (а не 0), 'Итог' = 'срыв: перевод', 'Причина срыва' по-русски из REASON_LABELS, соответствует fail_reason(note)
- test_open_cycle_marked_open — свежий start_cycle без этапов: итог 'открыт', все длительности пусты; после set_stage(..., 'transfer') заполнена длительность покупки
- test_stage_durations_minutes — done круг с известными ts_*: покупка/перевод/продажа в минутах ((b-a)/60), совпадает с _measures
- test_text_fields_escaped — label, начинающийся с '=', и note с '+cmd' выходят с апострофом (trades._csv_text), формулы не выполняются
- test_decimal_comma — числа с запятой и 2 знаками (1,50), не с точкой
- test_since_filters_old_cycles — since отрезает круги старше границы
- test_missing_or_empty_db_returns_empty — path на несуществующий файл: [] и файл НЕ создан; пустая таблица: []
- test_cmd_paper_cycles_sends_document — Stub-бот (tests/test_bot.py Stub/texts или свой), _patch_paper_db на tmp: cmd_paper('cycles') шлёт ровно один sendDocument с подписью 'Круги бумаги: N шт. за 30 дн.'; 'cycles 7' -> 'за 7 дн.'
- test_cmd_paper_cycles_empty_sends_text_only — пустая БД: сообщение 'за период кругов нет', документа нет
- test_cmd_paper_cycles_days_clamp_and_garbage — '0' -> 1 дн., '9999' -> 365 дн., 'abc'/'-5' -> 30 дн. (по подписи)
- test_write_report_csv_unchanged — существующий tests/test_paper.py::write_report_csv (563) остаётся зелёным; отчёт /paper report ведёт себя как раньше

**Критерии приёмки:**
- /paper cycles и /paper cycles 7 присылают один CSV-документ с подписью 'Круги бумаги: N шт. за D дн.'; при отсутствии кругов — текст 'за период кругов нет' без файла
- CSV открывается в русском Excel: BOM (utf-8-sig), разделитель ';', десятичная запятая, экранирование формул (= + - @) во всех текстовых полях
- 'Факт-план, п.п.' считается от COALESCE(planned_raw, planned_pct) и совпадает с /paper report; у срывов и открытых кругов 'Факт' и разница пустые, а не 0
- Открытые круги присутствуют с итогом 'открыт'; срывы — 'срыв: покупка|перевод|продажа' с русской причиной
- Схема БД, start_cycle/finish_cycle/ladder_suggestion/write_report_csv и GUEST_CMDS не изменены; /paper report работает как раньше
- python -m pytest -q зелёный; python scripts/guard.py печатает 'guard: ок'; tests/test_trading_surface.py и tests/test_isolated_data.py зелёные; изменение ≤ ~300 строк

**Не делать:**
- Не менять схему БД и миграции в _connect; не менять start_cycle, finish_cycle, set_stage, ladder_suggestion, write_report_csv, report_rows, stats, _PLAN_CMP
- Не читать столбцы позиционно по _COLUMNS — только по имени через _dicts
- Не считать разницу от planned_pct — только от COALESCE(planned_raw, planned_pct); не писать 0 в факт срывов/открытых кругов
- Не писать в живой data/ из тестов (использовать tmp_path); не открывать .env, data/keys.json, папку ключ, живую папку бота C:\Users\User\Desktop\p2p-bot
- Не использовать в добавленных строках слова payout/PAYOUT/pay_to|ok|no|hist|stop, TRADING, trd_, 'import trading', 'trading.', http(s)-ссылки, subprocess/os.system/eval/exec
- Не создавать новых файлов/функций со словами payout/trading в имени; не добавлять новых отправителей запросов (только уже разрешённый Bot.send_document); не называть переменные s/sess/session/http/client/opener при вызове .get(
- Не использовать asyncio.run в тестах — только helpers.arun (иначе пересечение с задачей tests-arun-audit)
- Не трогать защищённые файлы: tests/conftest.py, pytest.ini, requirements.txt, .github/, CLAUDE.md, scripts/guard.py, launcher.py, payouts.py, data/, logs/; не трогать GUEST_CMDS и запиненные константы (tests/test_payout_pins.py)
- Не запускать bot.py, launcher.py или сетевые скрипты; не добавлять зависимости и новые домены; не убирать слово 'reset' из описания paper в COMMANDS
- Не расширять задачу: без графиков, без cross-coin, без недельных дайджестов, без автоматической рассылки файла

**Где смотреть в коде (проверено на origin/main ad65d71):**
- paper.py:288 _dicts(cur) — строки читаются по имени; paper.py:296 get_cycle, paper.py:306 open_cycles, paper.py:317 set_stage, paper.py:815 finish_cycle (у срывов realized_pct=0.0)
- paper.py:~56 FAIL_LABELS = {failed_buy/failed_transfer/failed_sell: покупка/перевод/продажа}; paper.py:~113 REASON_LABELS (есть 'other': 'другое'); paper.py:1071 fail_reason(note)
- paper.py:887 stats, 915 summary_since, 956 ladder_suggestion, 1014 report_rows — план везде _PLAN_CMP = COALESCE(planned_raw, planned_pct)
- paper.py:991 _measures считает длительности как (b-a)/60
- paper.py:1118 write_report_csv пишет в REPORT_CSV_PATH = data/paper_report.csv (обычный utf-8, запятая) — не во временную папку
- trades.py:524 _csv_text (префикс ' для = + - @ \t \r); trades.py:~530 _num (f"{x:.2f}".replace('.', ',')); trades.py:536 write_export_csv (utf-8-sig, delimiter=';', МСК %Y-%m-%d %H:%M)
- bot.py:84 GUEST_CMDS; bot.py:107 описание paper в COMMANDS; bot.py:~204-210 HELP_SECTIONS['paper']; bot.py:1672 send_document; bot.py:2237-2275 cmd_paper (on/off/amount/report/reset; report шлёт paper.write_report_csv через send_document)
- tests/test_bot.py:25 Stub (sendDocument -> bot.out), :90 _patch_paper_db, :2170-2190 test_cmd_paper_report_*; tests/helpers.py make_ad, arun
- tests/test_paper_review110.py:230 — в описании paper должно быть 'reset'; tests/test_help_status_ux.py:62 — help-секции < 4096 символов
- tests/test_trading_surface.py ALLOWED_SENDERS содержит ('bot','Bot.send_document')
- tests/conftest.py — константы модулей и дефолты path= в data/ и logs/ автоматически уходят в tmp; audit hook кидает PermissionError на живое состояние
- scripts/guard.py: PROTECTED/PROTECTED_NAMES/PROTECTED_BASENAMES, PAYOUT_CODE и TRADING_CODE — предложенные пути и строки чистые
- ROADMAP.md и git ls-remote: нет ветки и пункта про построчный экспорт кругов бумаги

**Пересечения с другими задачами и ветками:** Пересечения нет: по refs/remotes/origin/* поиск export_cycles/write_cycles_csv/CYCLES_COLUMNS/paper_cycles пуст; ветки reports, reports2, audit-paper-replay, audit-sims, s2-digest-v2/v2b, s2-help-ux, s2-stats-direction, paper-route-hops-venues, paper-cross-asset-transfer-fee-frozen не добавляют выгрузку по кругам; в ROADMAP (очередь и Журнал) такого пункта нет. Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports/digest, cross-coin part 2, hedge/gates/trading) семантически не пересекается. Возможен только смежный конфликт строк в bot.py (HELP_SECTIONS['paper'], docstring cmd_paper) и в конце paper.py с задачами недельных дайджестов — решается ребейзом на свежий origin/main перед пушем. tests-arun-audit правит вызовы asyncio.run в существующих тестах; новый файл использует helpers.arun и не конфликтует.

---
### Задача 3. `command-contract-test` — Контрактный тест команд: меню (COMMANDS) = dispatch = /help = README = owner-guide, плюс соответствие пометок (гость) списку GUEST_CMDS

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/command-contract-test`

**Зачем:** Согласованность команд сегодня держится на памяти: /nets есть в меню и в dispatch, но не в /help, README и owner-guide; /export, /fav, /mybanks, /safety, /hedge есть в /help, но не в README. Тест делает правило 'каждая команда описана везде' проверяемым: новая команда без строки в справке, README и руководстве владельца даст красный тест с понятным сообщением. Гостевую поверхность (GUEST_CMDS) уже пинует хэш в tests/test_payout_pins.py, поэтому здесь проверяется другое: документы, которые читает владелец (owner-guide, README), не расходятся с реальным списком гостевых команд. Известный пробел (/backtest не назван в GUEST_DENIED и GUEST_WELCOME) облачный воркер закрыть не может: эти константы запинены хэшем в защищённом файле, поэтому пробел только фиксируется в ROADMAP как 'ждёт владельца'.

**Что сделать:** 1) Новый файл tests/test_command_contract.py (import bot as B; ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) как в tests/test_env_documented.py; файлы читать с encoding='utf-8'). Хелперы: (а) dispatch_commands(): ast.parse(bot.py), найти AsyncFunctionDef с именем 'dispatch' (assert, что он один), вернуть все строковые ast.Constant, для которых re.fullmatch(r'/[a-z_]+', value); сейчас их 38. (б) menu = {'/' + c['command'] for c in B.COMMANDS} (сейчас 32). (в) MONEY = {c for c in menu if c.startswith('/pay')} - ВАЖНО: с ведущим слэшем, иначе множество пустое (элементы menu начинаются с '/'); денежную команду не называть по имени и слово целиком не писать нигде в файле (ни в коде, ни в комментариях, ни в docstring): PAYOUT_CODE в guard считается и в tests/. Она исключается из проверок документации, её описывает отдельный раздел. (г) mentioned(text, cmd) = re.search(r'(?<![\w/])' + re.escape(cmd) + r'(?!\w)', text). (д) DOCUMENTED = menu - {'/help'} - MONEY. 2) Тесты. test_every_menu_command_is_handled: menu - {'/help'} <= dispatch_commands() ('/help' идёт веткой else в dispatch, bot.py:4187) и B.GUEST_CMDS <= dispatch_commands() | {'/help'}. test_hidden_commands_are_pinned: dispatch_commands() - menu == {'/start', '/allow', '/deny', '/amount', '/min', '/calibration', '/trading'} (новая команда без пункта меню требует осознанной правки теста). test_menu_commands_in_help_sections: каждая команда из DOCUMENTED встречается в '\n'.join(text for _, text in B.HELP_SECTIONS.values()). test_menu_commands_in_readme: то же для README.md. test_menu_commands_in_owner_guide: то же для docs/owner-guide.md. Во всех трёх сообщение assert перечисляет недостающие команды и говорит, куда добавить строку (HELP_SECTIONS в bot.py / список команд в README.md / таблица раздела 4 в docs/owner-guide.md). test_guest_commands_match_owner_guide_marks: marked = set(re.findall(r'`(/[a-z_]+)[^`]*`\s*\(гость\)', guide_text)); assert marked == B.GUEST_CMDS - {'/start', '/help'} (сейчас истинно, 9 команд), плюс B.GUEST_CMDS - {'/start'} <= menu. test_readme_guest_block_matches_guest_cmds: блок README между строкой, начинающейся с 'Рыночные (доступны и гостям', и строкой 'Только для владельца:'; set(re.findall(r'^- `(/[a-z_]+)', block, re.M)) == B.GUEST_CMDS - {'/start', '/help'}; если якоря не найдены - явный assert с сообщением. Проверку 'команды владельца не в GUEST_CMDS' для README НЕ делать: там есть строка '- `/maker paper`' в блоке владельца. Литеральный пин B.GUEST_CMDS в тест не копировать: он уже запинен хэшем в tests/test_payout_pins.py. 3) Минимальные текстовые правки, чтобы (c)-(e) и README-блок стали зелёными. bot.py - только HELP_SECTIONS['market']: после строки '/backtest ... /safety — 115-ФЗ, блокировки, правила' добавить строку '/nets — сети площадок, которых бот не распознал (для расширения маппинга)' (формулировка из COMMANDS; раздел market выбран, а не journal, чтобы не соседствовать со строкой /hedge, которую может править хедж-воркер). README.md: в блок 'Рыночные (доступны и гостям…)' добавить '- `/safety` — 115-ФЗ, блокировки карт, правила сделки (справка, не юридическая консультация).'; в блок 'Только для владельца:' по одной строке для /nets (сети площадок, которых бот не распознал: площадка, монета, имя, сколько раз и когда видели; на расчёт не влияет), /export (month / year / prev - журнал в CSV для банка и 3-НДФЛ), /mybanks (свои банки и бесплатные лимиты СБП), /fav (избранные маршруты), /hedge (хедж круга шортом перпа: открытые, закрыть; подробности - docs/owner-guide.md) - формулировки только из HELP_SECTIONS/owner-guide, без новых обещаний. docs/owner-guide.md: в таблицу 'Рынок' рядом с /traps строка '| `/nets` | сети площадок, которых бот не распознал (для расширения маппинга); на расчёт не влияет |' (без пометки (гость)). ROADMAP.md: отметить задачу, строка в Журнал, и отдельной строкой 'ждёт владельца': '/backtest не назван в GUEST_DENIED и GUEST_WELCOME; эти константы запинены хэшем в tests/test_payout_pins.py - править вручную вместе с пином'. 4) Перед пушем убедиться: python -m pytest -q зелёный целиком (в том числе tests/test_payout_pins.py, tests/test_help_status_ux.py, tests/test_guests.py), python scripts/guard.py зелёный, git diff не содержит строк с GUEST_ и с payout в bot.py и tests/.

**Править:** `bot.py`, `README.md`, `docs/owner-guide.md`, `ROADMAP.md`
**Создать:** `tests/test_command_contract.py`

**Тесты (офлайн):**
- tests/test_command_contract.py::test_every_menu_command_is_handled
- tests/test_command_contract.py::test_hidden_commands_are_pinned
- tests/test_command_contract.py::test_menu_commands_in_help_sections
- tests/test_command_contract.py::test_menu_commands_in_readme
- tests/test_command_contract.py::test_menu_commands_in_owner_guide
- tests/test_command_contract.py::test_guest_commands_match_owner_guide_marks
- tests/test_command_contract.py::test_readme_guest_block_matches_guest_cmds
- tests/test_payout_pins.py, tests/test_help_status_ux.py, tests/test_guests.py, tests/test_trading_surface.py (должны остаться зелёными без правок)

**Критерии приёмки:**
- До текстовых правок новые тесты (a), (b), guest-marks (owner-guide) зелёные, а красные ровно по перечисленным пробелам: help - /nets; README - /export /fav /hedge /mybanks /nets /safety (и блок гостей без /safety); owner-guide - /nets. После правок зелёные все 7 новых тестов
- MONEY считается по префиксу '/pay' (со слэшем) и в непустом виде содержит одну денежную команду; в файле теста и в добавленных строках bot.py нет слова payout и pay_to/ok/no/hist/stop
- В /help владельца (раздел market) появилась строка про /nets; len(help_view(key)[0]) < 4096 для каждого раздела (test_help_status_ux.py:59 зелёный)
- git diff bot.py содержит только добавленные/изменённые строки HELP_SECTIONS['market']; ни одной строки с GUEST_, GUEST_DENIED, GUEST_WELCOME, GUEST_CMDS; tests/test_payout_pins.py::test_payout_dependencies_unchanged зелёный
- python -m pytest -q и python scripts/guard.py зелёные; общий diff не больше 300 строк (ожидается около 115)
- В ROADMAP.md есть строка 'ждёт владельца' про /backtest в GUEST_DENIED/GUEST_WELCOME с отсылкой к пину в tests/test_payout_pins.py

**Не делать:**
- не править GUEST_DENIED, GUEST_WELCOME, GUEST_CMDS, GUEST_MENU, GUEST_CALLBACKS и не добавлять в bot.py новые имена с префиксами GUEST_ / PAYOUT_ / payout: они запинены sha256 в защищённом tests/test_payout_pins.py, правка = красный CI, а пин облачная рутина менять не вправе
- не редактировать tests/test_payout_pins.py, tests/test_help_status_ux.py, tests/test_env_documented.py, tests/test_guests.py и любые существующие тесты (новые проверки - только в tests/test_command_contract.py)
- не писать в тесте, комментариях и docstring слово payout целиком и не называть денежную команду по имени; исключать её только через startswith('/pay')
- не менять логику dispatch, COMMANDS, HELP_SECTIONS кроме одной добавленной строки в 'market'; не трогать раздел 'keys' и строку /hedge в 'journal'
- не копировать литеральный набор GUEST_CMDS в тест (он уже запинен хэшем) и не расширять GUEST_CMDS
- не добавлять в README проверку 'владельческие команды не в GUEST_CMDS' (там есть строка /maker paper в блоке владельца)
- не называть в README/owner-guide новых возможностей, которых код не даёт; про /hedge - только формулировка из HELP_SECTIONS
- не редактировать CLAUDE.md, .github/, scripts/guard.py, tests/conftest.py, pytest.ini, requirements.txt, launcher.py

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:4072 async def dispatch (единственный); строки '/cmd' в его теле - 38 (AST); bot.py:4187 'else: # /help и незнакомая команда' - у /help нет отдельной ветки
- bot.py:100-142 COMMANDS (32 команды), bot.py:116 {'command': 'nets'}; bot.py:4140 elif cmd == '/nets'; bot.py:177 HELP_SECTIONS, bot.py:189 раздел 'market' без /nets
- Матрица через import bot + чтение файлов на origin/main: меню - dispatch = {/help}; dispatch - меню = {/allow,/amount,/calibration,/deny,/min,/start,/trading}; нет в HELP_SECTIONS: /nets (и денежная); нет в README.md: /export /fav /hedge /mybanks /nets /safety (и денежная); нет в docs/owner-guide.md: /nets (и денежная)
- tests/test_payout_pins.py:38 BOT_PREFIXES = ('payout', 'PAYOUT_', 'GUEST_'); строки 70-74 PINS['bot'] содержат GUEST_CALLBACKS, GUEST_CMDS, GUEST_DENIED, GUEST_MENU, GUEST_WELCOME; скрипт: все хэши сейчас совпадают, хэш GUEST_DENIED после добавления ', /backtest' не совпадает
- .github/workflows/ci.yml: отдельный шаг гоняет tests/test_payouts.py, test_payouts_hardening.py, test_payout_pins.py и требует зелёного результата - красный пин блокирует автомерж
- docs/owner-guide.md:87 'Гостям доступны только команды с пометкой «гость»'; regex по файлу даёт 9 помеченных команд = GUEST_CMDS - {/start,/help}; README.md блок 'Рыночные (доступны и гостям…)' даёт 8 команд (без /safety)
- bot.py:84-92: GUEST_CMDS включает '/backtest', а GUEST_DENIED и GUEST_WELCOME его не называют (пробел реален, но закрывается только владельцем вместе с пином)
- scripts/guard.py: PROTECTED / PROTECTED_NAMES ('payout','trading') / PAYOUT_CODE / TRADING_CODE; guard.protected() = False для tests/test_command_contract.py, bot.py, README.md, docs/owner-guide.md, ROADMAP.md; синтетические строки нового кода не срабатывают на PAYOUT_CODE, TRADING_CODE, FORBIDDEN
- grep tests/: COMMANDS используется только точечно (test_export_safety.py:256, test_paper_review110.py:230, test_payouts.py:808); общего контракта нет; ни в одной из удалённых веток нет файла command/contract/menu

**Пересечения с другими задачами и ветками:** Темы не заняты: ни в git log origin/main, ни в ROADMAP Журнал/Идеи, ни в удалённых ветках (включая cloud/s2-help-ux, cloud/owner-guide, cloud/docs-env, cloud/s2-unmapped-nets - влиты squash #157/#150/#142) контрактного теста команд нет. С очередью воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, отчёты/дайджест, cross-coin ч.2, хедж/гейты) по файлам не пересекается, кроме возможных правок README/owner-guide и строки /hedge в HELP_SECTIONS['journal'] хедж-воркером: поэтому /nets кладётся в 'market', а README-строки ставятся одним смежным блоком; при конфликте слияния решает ручной разбор (рецепт в памяти проекта). Операционный риск: CI гонит тесты ветки без слияния с main, поэтому ветка воркера 1, которая добавит новый пункт меню (funding-alerts, дайджест, quote-advice) и была создана до этого теста, после автомержа сделает main красным до добавления строки в help/README/owner-guide; сообщения assert в тесте нарочно называют, что и куда дописать, чтобы следующий воркер починил это одной правкой текста.

---
Когда все задачи волны сделаны или помечены «ждёт владельца», напиши итог: список веток, что влито, что ждёт владельца. Следующую волну не начинай — её вставит владелец.
