# Рабочая сессия 2 — репозиторий Sonti22/p2p-bot, Волна 1: главный отчёт, быстрые защитные правки

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

## Задачи Волна 1: главный отчёт, быстрые защитные правки

**Почему такой состав волны:** Самая ценная задача (signal-quality-report, ценность 5) плюс быстрые S-победы с низким риском: утечка токена в лог и /logs, невидимая пауза в /status, фантомная прибыль от кривой спот-котировки, сторож тестов без assert (ранний сторож заставит следующие волны писать тесты с проверками). Файлы почти не пересекаются: новые модули sigreport.py и logsafe.py; в p2p.py правки в разных местах (setup_logging и спот-разбор), в bot.py разные участки (новая ветка /signals, status_brief/status_view, три строки логирования).

### Задача 1. `signal-quality-report` — Отчёт качества сигналов: команда /signals (только владелец)

**Ценность:** 5/5 · **Размер:** M · **Ветка:** `claude/signal-quality-report`

**Зачем:** Главная метрика бота, доля пропущенных сигналов против цели 10%, сейчас видна только сырым словарём history.signal_stats и нигде не показана владельцу. Владелец не видит, почему сигналы пропускаются (unconfirmed, max_signals, unsent, stale), как быстро сигнал уходит после появления связки и на каких направлениях пропусков больше всего. Отчёт по /signals даёт это одним сообщением и позволяет настраивать пороги и кулдаун по фактам. Работа только на чтении из локальной SQLite, без сети и без торговых путей.

**Что сделать:** 1) Создать sigreport.py (чистые функции, без сети, без импорта bot; можно import history). Константа TARGET_MISSED = 0.10. Функция build(path=history.DB_PATH, days=7, now=None) -> dict и функция render(data) -> str (HTML, экранировать через html.escape). Окно, gap и порог длинного эпизода должны совпадать с history.signal_stats: окно last_seen >= now - days*86400; gap = history._cooldown(); длинный = last_seen - first_seen >= 180 c (min_minutes=3); исключаемые причины = history.NOT_MISSED; слияние эпизодов только через history._merged_episodes(rows, gap), где rows это кортежи (key, first, last, signalled, reason), key = (buy_ex, buy_asset, sell_ex, sell_asset). SQL для выборки: SELECT buy_ex, buy_asset, sell_ex, sell_asset, first_seen, last_seen, signalled, reason_not_signalled, signal_ts FROM signals WHERE last_seen >= ? ORDER BY buy_ex, buy_asset, sell_ex, sell_asset, first_seen, id. days ограничить диапазоном 1..30 (0 и 999 зажимаются к 1 и 30; RETENTION в history.cleanup = 30 дней). 2) Содержимое отчёта: (а) строка 'Пропущено: X из Y длинных = Z%' с целью 10% и маркером выше или ниже цели; здесь Y = long - excluded (тот же знаменатель, что у missed_share в signal_stats), при Y == 0 показывать 'нет подходящих эпизодов' вместо процента; (б) причины пропуска: счётчики по reason среди пропущенных эпизодов, порядок по убыванию, потом по SIGNAL_REASONS, неизвестные причины внизу; (в) задержка сигнала: по сырым строкам с signalled=1 и signal_ts не NULL считать signal_ts - first_seen, показать медиану и p90; p90 методом nearest-rank: отсортировать, взять элемент с индексом ceil(0.9*n)-1 (для [5, 10, 60] p90 = 60, медиана = 10); задержки считать по сырым строкам, а не по слитым эпизодам, и в docstring отметить, что для слитых или перезапущенных эпизодов это приближение; (г) топ-5 направлений по числу пропущенных эпизодов, формат 'buy_ex buy_asset -> sell_ex sell_asset: N'. Пустая БД или нет строк в окне: одна строка 'за период сигналов нет'. Время показывать по МСК, если нужно (bot.MSK = UTC+3), но не импортировать bot: принимать готовые значения. 3) bot.py (не больше 25 строк): import sigreport в блоке импортов (после import simdirectional/рядом с import history, строки 17-39), одна запись в COMMANDS отдельной строкой ({'cmd': '/signals', ...} по образцу соседей), одна строка в HELP_SECTIONS раздела signal (отдельной строкой), метод Bot.signals_view(self, arg) (разобрать arg как целое число дней, по умолчанию 7, мусор -> 7, зажатие 1..30 внутри sigreport), и ветка elif cmd == '/signals': await self.send(self.signals_view(arg)) сразу после ветки /stats (~4098). Команда только для владельца: НЕ добавлять её в GUEST_CMDS (гостям запрещает существующий механизм GUEST_CMDS/dispatch). 4) tests/test_sigreport.py: БД в tmp_path через history.track_signals, время фиксировано параметром now, без сети. Тесты: (а) пустая БД -> 'за период сигналов нет'; (б) доля пропущенных и знаменатель long - excluded на построенном наборе (эпизод с quiet/paused/cooldown/trap не входит в знаменатель); (в) согласованность с signal_stats: сумма пропущенных по направлениям равна history.signal_stats(...)['missed'] и доля совпадает с missed_share на тех же данных; (г) причины: порядок и счётчики; (д) задержки: значения [5, 10, 60] дают медиану 10 и p90 60; чётная выборка и один элемент; (е) топ-5 направлений: 6 направлений, в отчёте ровно 5, порядок по убыванию; (ж) clamp days: 0 -> 1, 999 -> 30, мусорный аргумент -> 7; (з) render экранирует HTML в названиях; (и) в /help и COMMANDS есть /signals, а в GUEST_CMDS нет. 5) Отметить в ROADMAP.md строку в Журнале о новом отчёте (по протоколу CLAUDE.md), запустить pytest -q и scripts/guard.py, запушить ветку claude/signal-quality-report.

**Править:** `bot.py`, `ROADMAP.md`
**Создать:** `sigreport.py`, `tests/test_sigreport.py`

**Тесты (офлайн):**
- tests/test_sigreport.py: пустая БД даёт 'за период сигналов нет'
- tests/test_sigreport.py: знаменатель missed_share равен long - excluded, эпизоды с quiet/paused/cooldown/trap не входят
- tests/test_sigreport.py: сумма пропущенных по направлениям равна history.signal_stats(...)['missed'], доли совпадают
- tests/test_sigreport.py: p90 nearest-rank для [5, 10, 60] равен 60, медиана равна 10; чётная выборка и один элемент
- tests/test_sigreport.py: топ-5 направлений при 6 направлениях, порядок по убыванию
- tests/test_sigreport.py: clamp days (0 -> 1, 999 -> 30, мусор -> 7)
- tests/test_sigreport.py: /signals есть в COMMANDS и справке, отсутствует в GUEST_CMDS
- pytest -q целиком (в том числе tests/test_payout_pins.py, tests/test_trading_surface.py, tests/test_help_status_ux.py) и scripts/guard.py

**Критерии приёмки:**
- /signals показывает долю пропущенных X из Y длинных = Z% против цели 10%, причины, задержку (медиана и p90) и топ-5 направлений по пропускам
- Y в строке отчёта равен long - excluded из history.signal_stats на тех же данных; окно, gap и порог длинного эпизода совпадают с signal_stats
- p90 считается nearest-rank (для [5, 10, 60] это 60, медиана 10)
- days зажат в 1..30, по умолчанию 7, мусорный аргумент даёт 7
- Пустая БД или нет строк в окне: одна строка 'за период сигналов нет'
- Команда доступна только владельцу: /signals отсутствует в GUEST_CMDS, гость получает стандартный отказ
- pytest -q зелёный, scripts/guard.py проходит без нарушений, tests/test_payout_pins.py и tests/test_trading_surface.py не тронуты и зелёные
- Изменения bot.py не больше 25 строк; форма словаря history.signal_stats, record_signals и track_signals не изменены

**Не делать:**
- Не читать и не трогать .env, data/, logs/, ключи и живую папку бота; тесты только на tmp_path
- Не использовать в добавленных строках (включая комментарии и docstring) подстроки payout, TRADING, trd_, trading., pay_to/pay_ok/pay_no/pay_hist/pay_stop и слова order/position/margin/withdraw/transfer в виде эндпоинтов
- Не добавлять http(s)-ссылки и новые домены; не давать аргументам имена url, uri, endpoint, base; не вызывать .get/.send/.open/.request на клиентоподобных объектах (s/session/client/http/opener) в sigreport.py; не использовать getattr с вычисляемым именем
- Не называть новые имена в bot.py с префиксами payout, PAYOUT_, GUEST_ и не трогать GUEST_CMDS, GUEST_CALLBACKS, GUEST_DENIED, GUEST_MENU, GUEST_WELCOME, Bot._owner_gate, Bot.is_guest, Bot.on_guest_callback, Bot.cmd_payout
- Не менять форму словаря history.signal_stats и функции record_signals/track_signals; не править функции дайджеста (venue_signals и т.п.)
- Не переписывать существующие строки bot.py с payout (COMMANDS, dispatch, справка): новые записи COMMANDS, HELP_SECTIONS и dispatch ставить отдельными новыми строками
- Не запускать bot.py, launcher.py и скрипты с сетью; не править защищённые пути (.github/, scripts/guard.py, launcher.py, CLAUDE.md, .gitignore, payouts.py, tests/trading/, pytest.ini, conftest.py, tests/conftest.py, data/, logs/)
- Не называть файл или модуль так, чтобы он затенял stdlib или установленный пакет

**Где смотреть в коде (проверено на origin/main ad65d71):**
- history.py: таблица signals, SIGNAL_REASONS, NOT_MISSED, _cooldown(), _merged_episodes(rows, gap), signal_stats(path, days=7, now, min_minutes=3, cooldown), track_signals(rows, ts, open_ids, amount, min_profit, path)
- bot.py: COMMANDS (~100), GUEST_CMDS (~84), HELP_SECTIONS (~176-215), ветка /stats в dispatch (~4098), Bot.signal_reasons (~3061), record_signals (~3095), Bot.signal_rows (~1529, в памяти, сбрасывается при рестарте)
- tests/test_history.py:170-186: test_signal_stats_missed_share и помощники _scans/BASE/K1/K2 как образец для сборки данных
- tests/test_help_status_ux.py:36,60: проверяют только ключи HELP_SECTIONS и callbacks, а не точный текст
- scripts/guard.py: PROTECTED/PROTECTED_NAMES/PAYOUT_CODE/TRADING_CODE, shadows_module; tests/test_trading_surface.py: AST-проверка сетевых отправителей; tests/test_payout_pins.py: sha256-пины payout*/PAYOUT_*/GUEST_*
- grep по 50 удалённым веткам и main: sigreport, /signals, signals_view отсутствуют
- ROADMAP.md ~855: недельный дайджест только как идея, отчёта качества сигналов в Журнале нет

**Пересечения с другими задачами и ветками:** Ни одна из 50 ремотных веток и main не содержит sigreport, /signals, signals_view. Очередь исполнителя 1 (недельные отчёты и дайджест, funding-alerts, maker-paper, hedge/gates/trading, bc-stack-dedup, tests-arun-audit) не пересекается: дайджест в будущем может вызвать history.signal_stats, а эта задача экспортирует собственные функции и не правит функции дайджеста. Небольшой риск конфликта в bot.py рядом с блоком COMMANDS, HELP_SECTIONS и веткой /stats в dispatch: новые строки ставить отдельно, при конфликте применить стандартный рецепт разрешения конфликтов bot.py (сохранить обе стороны, независимые соседние строки).

---
### Задача 2. `status-shows-mute-state` — /status: показывать, что сигналы на паузе или в тихих часах

**Ценность:** 4/5 · **Размер:** S · **Ветка:** `claude/status-shows-mute-state`

**Зачем:** Владелец жмёт /status, когда сигналов давно нет. Сейчас status_brief и status_view пишут «🔔 Связок выше порога N» и «Ошибок нет», но ни слова о том, что сигналы заглушены /pause или тихими часами: это видно только в «⚙️ Настройки» (settings_view) и в разовом ответе на /pause. Пауза после /pause без аргумента бессрочная и живёт только в памяти (после перезапуска бота её нет, пока владелец не поставит снова). Итог: «связок 5, а в чат ничего не пришло», причину владелец не видит. Правка только текстовая, риск минимальный, деньги и сеть не затрагиваются.

**Что сделать:** 1) В bot.py, рядом с settings_view (класс Bot), добавить метод Bot.mute_line(self, now=None) -> str | None с той же логикой, что переменная status в settings_view (bot.py:2426-2434), в таком порядке: now = time.time() если now is None; если self.paused -> «⏸ Сигналы на паузе (бессрочно) — /resume»; elif self.pause_until and now < self.pause_until -> f«⏸ Сигналы на паузе до {_hhmm_msk(self.pause_until)} МСК — /resume»; elif self.is_quiet_now() -> end = quiet_hours_end_ts(self.quiet_hours); при end is None (защита, в settings_view её нет) строка «🌙 Тихие часы — сигналы копятся для дайджеста», иначе f«🌙 Тихие часы до {_hhmm_msk(end)} МСК — сигналы копятся для дайджеста» (не «до утра»: окно QUIET_HOURS настраиваемое); иначе None. Строки статичны, пользовательский ввод в них не попадает, html.escape не нужен. 2) status_brief (bot.py:2806): mute = self.mute_line() посчитать один раз; если mute — вставить его в lines сразу после блока «Скан…» (после if/else про last_scan_ts, до `snap = self.last`), причём выводить и когда self.last is None и когда скана ещё не было; строку «🔔 Связок выше порога {N}%: {above}» при mute дополнить суффиксом « (не отправляются)». 3) status_view (bot.py:2840): та же строка сразу после «Последний скан: …» и ДО раннего return при snap is None (чтобы она была и до первого скана); к строке «Связок выше порога …» при mute добавить « (не отправляются)». 4) Без паузы и тихих часов оба текста не меняются ни на байт (mute_line() is None -> ни вставки, ни суффикса). 5) settings_view не рефакторить и mute_line в него не подставлять. 6) Функции _hhmm_msk (bot.py:400), quiet_hours_end_ts (bot.py:377), is_quiet_now (bot.py:2911) уже есть, новых импортов и зависимостей не нужно; не менять логику паузы/тихих часов, кнопки «📋 Подробно» / «🔄 Обновить» и callback-и status/status_full.

**Править:** `bot.py`
**Создать:** `tests/test_status_mute_state.py`

**Тесты (офлайн):**
- tests/test_status_mute_state.py: свой класс Stub(B.Bot) как в tests/test_help_status_ux.py:10-21 (super().__init__(None, 'x', '1', cfg), call пишет в self.out, без сети); корутины запускать только через helpers.arun (не asyncio.run). В каждом тесте явно выставлять bot.paused, bot.pause_until, bot.quiet_on: Stub берёт quiet_on из окружения, тест не должен зависеть от .env и от часов.
- bot.paused=True, quiet_on=False: status_brief(str(tmp_path/'none.json'))[0] содержит «на паузе (бессрочно)» и «/resume»; bot.status_view(...) тоже; при last is None (скана не было) строка есть в обоих текстах (в status_view — она выше «Скан ещё не выполнялся.»).
- bot.pause_until=time.time()+1800: в тексте «пауза» не нужна дословно, проверять f"на паузе до {B._hhmm_msk(bot.pause_until)} МСК"; после bot.pause_until=time.time()-5 строки паузы нет.
- quiet_on=True, bot.quiet_hours='01:00-08:00', monkeypatch.setattr(B, 'in_quiet_hours', lambda spec, ts=None: True): строка «Тихие часы до 08:00 МСК» (окно 01:00-08:00 даёт 08:00 при любой дате); quiet_on=False (тот же monkeypatch) — строки нет. Отдельно: bot.quiet_hours='мусор' + monkeypatch in_quiet_hours=True — mute_line() не падает и возвращает строку «Тихие часы …» без времени.
- Регресс вывода без паузы и тихих часов: bot.last_scan_ts=time.time()-1, bot.last_scan_duration=1.5, bot.last = p2p.Snapshot(88.0,'t',{},{},[d],{},{},{}) с одной связкой выше порога (d как в tests/test_help_status_ux.py:80); paused=False, pause_until=0.0, quiet_on=False: mute_line() is None, в text нет «⏸», «🌙» и «(не отправляются)», а строки status_brief совпадают с уже проверяемыми (lines[3] == '🔔 Связок выше порога 2%: 1' при last is not None).
- При активной паузе строка «🔔 Связок выше порога …» оканчивается на « (не отправляются)», а строка паузы стоит сразу после строки скана (lines[2] при 🟢-скане).
- Кнопки не меняются: labels(kb) == ['status_full', 'status'] и при паузе.
- После bot.paused=True затем arun(bot.handle('/resume')): mute_line() is None и в status_brief нет строки паузы. Дополнительно arun(bot.handle('/pause')) даёт status_brief со строкой «бессрочно».

**Критерии приёмки:**
- При активной паузе или включённых и наступивших тихих часах и /status (status_brief), и «📋 Подробно» (status_view) явно говорят, что сигналы не отправляются, и до какого времени (МСК) или как снять паузу (/resume).
- Без паузы и без тихих часов оба текста не изменились ни на байт; существующие тесты (tests/test_help_status_ux.py, tests/test_bot.py:3252-3300, test_speed.py, test_terms_log.py, test_venue_outages.py, test_depth_pages.py) проходят без правок.
- Нет новых обращений к сети, БД, .env и диску; pytest и scripts/guard.py зелёные; tests/test_trading_surface.py зелёный (новые строки bot.py не содержат TRADING, trd_, trading., payout, pay_*).
- Изменено не больше ~150 строк (код около 25, тесты не больше ~120); затронуты только bot.py и tests/test_status_mute_state.py, плюс отметка задачи в ROADMAP.md по протоколу CLAUDE.md.

**Не делать:**
- Не менять саму логику паузы, тихих часов и quiet_and_pause_tick/record_signals/signal_reasons (bot.py:3044-3100), cmd_pause/cmd_resume (bot.py:3106-3127), apply('pause'/'resume') (bot.py:2569-2572).
- Не сохранять паузу на диск в этой задаче: новый файл состояния попадёт под изоляцию conftest и protected-пути data/; это отдельная идея.
- Не трогать launcher.py, trading/, payouts.py, .github/, scripts/guard.py, CLAUDE.md, tests/conftest.py, pytest.ini, requirements.txt; не писать в новых и изменённых строках bot.py слова TRADING, trd_, «trading.», payout, pay_to/pay_ok/pay_no/pay_hist/pay_stop: tests/test_trading_surface.py:238 пинует строки bot.py по TRADING_CODE, guard блокирует такие строки.
- Не переписывать status_brief и status_view целиком: только вставка строки и суффикс к строке «Связок выше порога»; settings_view не рефакторить.
- Не добавлять в новых тестах asyncio.run (только helpers.arun; у воркера 1 идёт аудит asyncio.run в тестах), не импортировать модули из tests/ друг из друга (свой Stub в новом файле), не использовать .post( и любые сетевые вызовы.
- Не вводить новых настроек и переменных окружения (.env.example не трогать), не менять закреплённое «Статус рынка» (market_status_view).

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:2806-2838 status_brief: версия, скан, ошибки площадок, «Связок выше порога» (bot.py:2834-2835); упоминаний paused/pause_until/quiet нет.
- bot.py:2840-2879 status_view: тот же пробел; ранний return при snap is None на bot.py:2853-2855; «Связок выше порога» на bot.py:2857.
- bot.py:2426-2434 settings_view: единственное место с «⏸ пауза сигналов (бессрочно)» / «⏸ пауза до HH:MM» / «🌙 тихие часы до HH:MM»; оттуда переносится логика.
- bot.py:1510-1511 self.paused / self.pause_until (только в памяти, не сохраняются); bot.py:2911-2913 is_quiet_now; bot.py:3044-3059 quiet_and_pause_tick: при quiet или paused notify пропускается; bot.py:3106-3127 cmd_pause / cmd_resume; bot.py:377 quiet_hours_end_ts, bot.py:400 _hhmm_msk; bot.py:366-371 in_quiet_hours возвращает False при неразобранном окне.
- bot.py:3918-3921 и 4158-4160: /status и кнопка «status» шлют status_brief, «status_full» шлёт status_view.
- tests/test_help_status_ux.py:10-21 (Stub без сети), 77-99 (тесты status_brief); tests/test_bot.py:3252-3280 (тесты status_view); scripts/guard.py:55-67 (PAYOUT_CODE, TRADING_CODE, PROTECTED*); tests/test_trading_surface.py:238 (пин торговых строк bot.py).

**Пересечения с другими задачами и ветками:** Проверено: ROADMAP.md (Очередь, Идеи, Журнал) не содержит «пауза/тихие часы в /status»; git log origin/main не имеет такой правки. Среди 47 remote-веток status_brief меняла только cloud/s2-help-ux (влита #157), паузу в него не добавляла. claude/quiet-own-service-msg (+21 строка в bot.py/tests/test_bot.py) касается отказа владельцу на служебные сообщения, не статуса. Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly digest, cross-coin part 2, hedge/gates) status_brief не трогает. Возможен лишь текстовый конфликт мержа в bot.py, если параллельная задача тоже добавит строку в status_brief; конфликт разрешается штатным рецептом пайплайна.

---
### Задача 3. `log-redact-secrets` — Маскировка токена бота и подписей запросов в bot.log, консоли и /logs

**Ценность:** 4/5 · **Размер:** S · **Ветка:** `claude/log-redact-secrets`

**Зачем:** Токен Telegram-бота лежит в URL каждого вызова Bot API (Bot.call), а str() исключений aiohttp содержит полный URL. Проверено на aiohttp 3.13.3: ContentTypeError при ответе 502 с HTML печатается как "502, message='...', url='https://api.telegram.org/bot<токен>/getUpdates'". Три места пишут исключение сырым через %s: command_loop (getUpdates раз в 5 с при сбое сети), update error и setup. Так токен попадает в logs/bot.log (5 файлов), консоль и в команду /logs (последние 30 строк уходят в чат). В других местах бот уже пользуется accounts.api_error_text, но общей страховки нет: сырое e логируется ещё в bot.py:1700, 3414, 3489, и любой новый logger.warning('%s', e) снова утечёт. Нужна централизованная маскировка на уровне форматтера плюс точечные правки.

**Что сделать:** 1) Новый модуль logsafe.py в корне (импортирует только logging и re: не p2p, не bot, не accounts, не aiohttp, чтобы не было циклов и сетевых импортов). Регэкспы компилировать на уровне модуля. Правила redact(text) применять по порядку: (а) токен Bot API в пути: r'/bot\d+:[A-Za-z0-9_-]+' -> '/bot<токен скрыт>'; (а2) голый токен где угодно: r'\b\d{8,10}:[A-Za-z0-9_-]{30,}' -> '<токен скрыт>'; (б) секретные параметры запроса без учёта регистра, r'(?i)\b(signature|sign|accesskeyid|api_key|apikey|access_token|token|secret)=[^&\s\'"]+' -> имя как в тексте + '=***' (функция-замена через m.group(1)); (в) r'(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s\'",;]+' -> группа 1 + '***' и r'(?i)\b(bearer\s+)[\w.~+/=-]+' -> группа 1 + '***'. redact: text = str(text) (не падает на не-строках, None, исключениях), идемпотентна (повторный вызов не меняет результат). class RedactingFormatter(logging.Formatter) с переопределённым format(self, record): return redact(super().format(record)) — это покрывает и трейсбеки (exc_text), и логи asyncio.
2) p2p.py: добавить import logsafe рядом с локальными импортами (fees, netstatus…); в setup_logging (p2p.py:80-98) заменить ровно одну строку fmt = logging.Formatter(...) на logsafe.RedactingFormatter(...) с тем же форматом '%(asctime)s %(levelname)s %(name)s: %(message)s' и датой '%d.%m %H:%M:%S' (формат для файлового и консольного хендлеров тот же, остальные строки функции не трогать: рядом может править setup_logging задача log-repeat-throttle, дифф должен быть минимальным).
3) bot.py: добавить import logsafe к локальным импортам; в logs_view (bot.py:1258) сразу после tail = ''.join(lines[-n:]).strip() применить logsafe.redact, до проверки пустоты, обрезки tail[-3500:] и html.escape (старые строки уже лежащих на диске логов тоже скрываются).
4) bot.py:3620 (getUpdates error) и bot.py:4383 (setup): заменить e на accounts.api_error_text(e), добавив в конец строки комментарий '# без URL с токеном бота' как в bot.py:1708; bot.py:3632 (update error) оставить с e (там прикладные ошибки), его закроет форматтер. accounts.py не менять.
5) ROADMAP.md: новая строка '- [x] Маскировка токена бота и подписей запросов в логах: ...' с датой запуска и итогом в одну строку в разделе '### Надёжность 24/7' (сразу после пункта про logs/bot.log и /logs, ROADMAP.md ~545) и запись в начале '## Журнал' (новые сверху). Очередь пуста, поэтому пункт добавляется сразу выполненным.
6) tests/test_logsafe.py (без сети, без реальных токенов): TOKEN = '123456789' + ':' + 'A' * 35 (цифры и двоеточие в разных литералах). Цепочку aiohttp собирать локально: url = yarl.URL(f'https://api.telegram.org/bot{TOKEN}/getUpdates'); exc = aiohttp.ContentTypeError(aiohttp.client_reqrep.RequestInfo(url, 'POST', multidict.CIMultiDict(), url), (), status=502, message='x') (yarl и multidict ставятся вместе с aiohttp; работает на aiohttp>=3.9). Сначала assert TOKEN in str(exc) (тест не проходит вхолостую), потом проверки маскировки.

**Править:** `p2p.py`, `bot.py`, `ROADMAP.md`
**Создать:** `logsafe.py`, `tests/test_logsafe.py`

**Тесты (офлайн):**
- tests/test_logsafe.py, redact: скрывает токен в URL и в str(ContentTypeError) (502), голый токен вне URL, signature=/Signature=/AccessKeyId=/api_key=/token=/secret= в query (URL только на разрешённых доменах: api.telegram.org, api.mexc.com, api.bybit.com, api.htx.com), 'Authorization: Bearer ...' и 'Bearer ...'; обычный текст, числа, время 12:34:56, проценты, кириллица, слова вроде 'design=1' и строки без секретов не меняются; redact(redact(x)) == redact(x); redact(None), redact(123), redact(exc) не падают.
- tests/test_logsafe.py, setup_logging: p2p.setup_logging(str(tmp_path / 'bot.log')) в самом тесте, затем logging.getLogger('x').warning('getUpdates error: %s', str(exc)): в файле (utf-8) нет TOKEN и есть '<токен скрыт>'; строка лога начинается с даты, уровня и имени логгера по regex r'^\d\d\.\d\d \d\d:\d\d:\d\d WARNING x: ' (формат не изменился); то же для logger.exception с исключением, в тексте которого токен (трейсбек); консоль: capsys.readouterr().out тоже без токена (StreamHandler привязан к sys.stdout уже внутри теста). Хендлеры закрывает autouse-фикстура conftest _clean_logging.
- tests/test_logsafe.py, logs_view: B.logs_view(str(tmp_path / 'bot.log')) для файла, в котором токен уже записан сырым, — в ответе токена нет, '<токен скрыт>' есть, HTML по-прежнему экранируется; tests/test_bot.py:3299-3323 остаются зелёными без правок.
- tests/test_logsafe.py, bot.py:3620 и 4383: Stub (from test_bot import Stub) с monkeypatch.setattr(bot, 'call', raiser), где async raiser бросает exc из шага 6; для command_loop подменить B.asyncio.sleep на функцию с raise asyncio.CancelledError (паттерн tests/test_bot.py:3337) и вызвать через pytest.raises(asyncio.CancelledError) + arun; для setup() — arun(bot.setup()). caplog.set_level(logging.WARNING): в [r.getMessage() for r in caplog.records] нет TOKEN и есть 'HTTP 502'. Проверить и update error (bot.py:3632): вызывающая обработка on_update с исключением, содержащим токен, после setup_logging пишет в файл без токена (форматтер закрывает то, что не заменено на api_error_text).

**Критерии приёмки:**
- Ни в logs/bot.log, ни в консоли, ни в ответе /logs не появляется токен бота и значения signature/AccessKeyId/api_key даже при сыром logger.warning('%s', e) с исключением aiohttp (проверено тестами на файл, capsys и logs_view).
- Формат строк лога (дата, уровень, имя логгера) не изменился; tests/test_logging_setup.py зелёный (RotatingFileHandler 5 x 1 МБ остаётся); существующие тесты logs_view зелёные без правок.
- redact идемпотентна, не падает на не-строках и не меняет обычный текст (набор проверок на кириллице, числах, времени, процентах).
- python -m pytest -q и python scripts/guard.py зелёные (guard: ок, в том числе tests/test_trading_surface.py: logsafe.py без отправителей запросов и динамики); ROADMAP отмечен [x] в разделе 'Надёжность 24/7' с записью в Журнале. Изменено не больше ~300 строк вместе с тестами.

**Не делать:**
- Не менять launcher.py (там своя _mask для launcher.log, файл защищён), .github/, CLAUDE.md, tests/conftest.py, tests/test_trading_surface.py, pytest.ini, requirements.txt, data/, logs/, .env*, *.bat, payouts.py, trading/, accounts.py.
- Не писать в код и тесты токен или похожую на него строку одним литералом (guard SECRETS: \b\d{8,10}:[A-Za-z0-9_-]{30,}); в тесте только конкатенация, как в спеке. Значения 'sk-...', 'ghp_...' в тестах не использовать.
- В logsafe.py: без getattr/attrgetter/methodcaller, без вызовов .get(url...), .send, .post, .request, .open, без импортов aiohttp, socket, ssl, http, urllib, requests (их ловит tests/test_trading_surface.py); без новых доменов, зависимостей и subprocess. В .py-файлах, включая комментарии и docstring, не писать хосты вне ALLOWED_DOMAINS (guard ищет https?://хост в любой добавленной строке .py).
- Не заменять e на api_error_text в bot.py:3632 (потеряются тексты прикладных ошибок), не менять уровни логирования и остальные logger-вызовы, не трогать строки, содержащие payout или TRADING; в добавляемых строках bot.py/p2p.py слов payout, TRADING, trd_, position, order и т.п. не использовать.
- В p2p.py менять только import и одну строку fmt = ... в setup_logging (минимальный дифф под возможный конфликт с log-repeat-throttle); при конфликте оставить оба изменения.
- Не менять существующие тесты; новые проверки только в tests/test_logsafe.py. Тесты не должны ходить в сеть и писать в реальные data/ и logs/ (только tmp_path).
- Не читать и не выводить реальные ключи, .env, data/keys.json, содержимое живой папки бота; не запускать бота.

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:1559-1560 — Bot.call: токен внутри URL f"https://api.telegram.org/bot{self.token}/{method}", r.json() без raise_for_status
- bot.py:3620 — logger.warning('getUpdates error: %s', e) в цикле с sleep(5); bot.py:3632 — logger.error('update error: %s', e); bot.py:4383 — logger.warning('%s: %s', method, e)
- bot.py:1700, 3414, 3489 — сырое e в logger.warning рядом с вызовами Bot API: общая страховка нужна
- accounts.py:252-261 — api_error_text: приём уже принят, применён точечно (bot.py:1575, 1708, 2037, 3401, 3539 и др.)
- p2p.py:80-98 — setup_logging: голый logging.Formatter, редакции нет; launcher.py:100-103 _mask защищает только launcher.log
- bot.py:1258-1272 — logs_view отправляет хвост bot.log в чат, только html.escape
- воспроизведено (aiohttp 3.13.3): str(ContentTypeError) содержит url='https://api.telegram.org/bot<токен>/getUpdates'; прототип redact скрывает токен, signature= и AccessKeyId= и в трейсбеке logger.exception
- tests/test_bot.py:25-41 Stub с подменой call; tests/test_startup_offline.py — setup() без сети; tests/test_bot.py:3337, tests/test_calibration_ev.py:246 — CancelledError в подменённом asyncio.sleep; tests/conftest.py:445 _clean_logging
- scripts/guard.py: protected() False для logsafe.py и tests/test_logsafe.py; tests/test_trading_surface.py::_surface на черновике logsafe.py: отправителей и сетевых импортов нет

**Пересечения с другими задачами и ветками:** Не пересекается с задачами worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2, funding-alerts, digest, cross-coin part 2, hedge/gates). git grep по всем 46 удалённым веткам: logsafe, RedactingFormatter, redact пусто; ветки, добавляющие api_error_text (audit-bot, s2-digest-v2b, s2-route-alert), правят другие строки; cloud/trading-core применяет accounts._scrub к ключам биржи, не к логам; fancy-403 и fancy-edit-errors правят send/edit, не command_loop, setup и logs_view. Возможен текстовый конфликт со схожей задачей log-repeat-throttle в setup_logging (p2p.py) и ROADMAP.md — при конфликте оставить оба изменения, ROADMAP.md по стандартному рецепту.

---
### Задача 4. `spot-sanity-filter` — Санити-фильтр спот-котировок: перевёрнутый стакан, широкий спред, выброс площадки

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/spot-sanity-filter`

**Зачем:** Маршруты через спот-ноги считают покупку по ask и продажу по bid напрямую (p2p.py:1122-1160), а единственная проверка котировки сейчас bid > 0 и ask > 0 (p2p.py:756). Одна кривая котировка (bid > ask, спред в десятки процентов, выброс площадки на 10-30%, устаревший тикер) даёт фантомную прибыль в сигнале, потому что маршрут выбирает спот-площадку по порядку (b.ex, s.ex) + SPOT_VENUES без сверки с остальными. Правка чисто защитная и отказобезопасная: подозрительная котировка отбрасывается, маршрут строится по другим площадкам, а если не осталось ни одной - связка штатно пропускается (_mid вернёт None, _route_qty вернёт None), как для монеты без спота.

**Что сделать:** Всё в p2p.py, сразу после `_mid` (p2p.py ~768), плюс тест и запись в Журнал. 1) Модульные константы (НОВЫХ env-переменных не заводить): SPOT_MAX_SPREAD_PCT = 2.0, SPOT_MAX_DEV_PCT = 5.0, SPOT_REJECTED = {}  # {(площадка, монета): причина} последнего скана. 2) Чистая функция `sane_spot(spot, max_spread_pct=SPOT_MAX_SPREAD_PCT, max_dev_pct=SPOT_MAX_DEV_PCT)` -> `(clean, dropped)`; spot вида {venue: {asset: (bid, ask)}}. Не мутирует вход, без побочных эффектов, без сети. clean содержит КАЖДУЮ площадку из входа (даже если остался пустой словарь) и проходит USDT (1.0, 1.0) без изменений - tests/test_speed.py:239 утверждает set(snap.spot['HTX']) == {'USDT'}. dropped - список кортежей (venue, asset, reason). Порядок проверок для пары (venue, asset), первая сработавшая даёт причину: (a) не пара из двух конечных чисел (не число, None, длина != 2, nan, inf; math.isfinite) -> 'nonfinite'; (b) bid <= 0 или ask <= 0 -> 'nonpositive'; (c) bid > ask -> 'crossed' (bid == ask допустим, USDT именно такой); (d) ask > bid * (1 + max_spread_pct / 100) -> 'spread' (умножением, НЕ через (ask/bid - 1) * 100: float даёт (102/100-1)*100 = 2.0000000000000018). Затем межплощадочная проверка: для каждой монеты, у которой ПОСЛЕ шагов a-d осталось >= 3 площадок, mid = (bid + ask) / 2, median = statistics.median(mid всех оставшихся площадок этой монеты); площадка с abs(mid / median - 1) * 100 > max_dev_pct отбрасывается с причиной 'outlier' (все решения по одной и той же медиане за один проход, без итераций). При 2 и менее площадках межплощадочную проверку не делать: большинства нет. 3) Обёртка `filter_spot(spot)` -> clean: вызывает sane_spot, заменяет содержимое SPOT_REJECTED на {(venue, asset): reason} на месте (SPOT_REJECTED.clear(); .update(...)), пишет `logger.warning` (список площадка/монета/причина) ТОЛЬКО когда набор изменился относительно прошлого скана (не раз в 20 с); пустой набор после непустого - без предупреждения. 4) В collect (p2p.py:2182-2187) внутри `try:` сразу после `spot = await spot_task` и до `spot_err = None` добавить ровно одну строку `spot = filter_spot(spot)`. Тогда ошибка самого фильтра уходит в существующий except (fallback {'Bybit': {'USDT': (1.0, 1.0)}} + errors['spot']), а не роняет scan. Ветку except с fallback не менять и фильтр к ней не применять. 5) spot_prices, _mid, _route_qty, assemble, bot.py не менять: очищенный spot попадает и в _refs (p2p.py:2196), и в assemble, и в Snapshot/snapshots/paper. 6) ROADMAP.md: одна строка в начало «Журнал» (новые записи сверху) - что сделано, пороги 2%/5%, где вызов, тесты; очередь пуста, отдельный пункт [x] не нужен.

**Править:** `p2p.py`, `ROADMAP.md`
**Создать:** `tests/test_spot_sanity.py`

**Тесты (офлайн):**
- tests/test_spot_sanity.py, autouse-фикстура: очистить p2p.SPOT_REJECTED до и после теста (tests/conftest.py защищён и не сбрасывает это состояние); асинхронное - только через `from helpers import arun`, не asyncio.run
- crossed: (2640.2, 2640.1) -> reason 'crossed'; nonpositive: (0, 1), (-1, 1), (1, 0) -> 'nonpositive'; nonfinite: nan, inf, None, строка, кортеж длины 1 -> 'nonfinite'; без исключений, остальные монеты и площадки в clean не задеты
- спред: bid 100, ask 101.9 (1.9%) остаётся, ask 102.1 (2.1%) отбрасывается с 'spread'; свой max_spread_pct передаётся параметром. Значения на самом пороге в тестах НЕ проверять (float)
- выброс: 4 площадки, у одной mid +20% -> отброшена только она с 'outlier', остальные три целы; то же для 3 площадок; при 2 площадках с mid, различающимися на 50%, dropped пуст; после шагов a-d осталось 2 площадки (одна crossed из трёх) -> межплощадочная проверка не выполняется
- форма результата: ключи всех площадок сохранены (площадка с одним USDT остаётся {'USDT': (1.0, 1.0)}), вход не изменён (deepcopy до/после), sane_spot(clean) идемпотентна: (clean, [])
- фикстуры: через `offline` получить spot_prices(None, ['USDT','BTC','ETH','USDC']) (образец test_adapters.py:65) -> sane_spot возвращает результат, равный входу, и пустой dropped; сами tests/fixtures/spot_*.json не править
- filter_spot и лог (caplog, уровень WARNING, логгер p2p): первый вызов с одной кривой котировкой - ровно одно предупреждение и SPOT_REJECTED == {('KuCoin','ETH'): 'outlier'}; повторный вызов с тем же набором - новых предупреждений нет; набор изменился - снова одно; чистый вход - SPOT_REJECTED пуст
- фантомная прибыль: SPOT из трёх здоровых площадок (ETH около 2500) + KuCoin ETH (1990.0, 2000.0); маршрут `p2p._route(make_ad('KuCoin','buy',88.0), make_ad('Bybit','sell',245000.0, asset='ETH'), cfg(), spot)` (cfg/make_ad по образцу tests/test_route.py:659-666) на сыром spot даёт заметно более высокую прибыль, чем на filter_spot(spot); на очищенном она равна прибыли на spot без KuCoin (pytest.approx)
- интеграция через collect/scan: фикстура `offline`, обёртка над p2p._json (образец test_adapters.py:87), которая умножает KuCoin ETH buy/sell на 1.25; `arun(p2p.scan(None, cfg))` (конфиг по образцу tests/test_speed.py) -> 'ETH' not in snap.spot['KuCoin'], у Bybit/MEXC/HTX ETH на месте, USDT везде на месте, p2p.SPOT_REJECTED == {('KuCoin','ETH'): 'outlier'}
- полный `python -m pytest -q` и `python scripts/guard.py` зелёные; существующие tests/test_speed.py, tests/test_assemble.py, tests/test_review_audit_bot.py (прибыль -0.543529 по spot из фикстур) проходят без правок

**Критерии приёмки:**
- Кривая котировка (nonfinite, nonpositive, bid > ask, спред > 2%, выброс > 5% от медианы при >= 3 площадках) не попадает в snap.spot и в расчёт маршрутов; причины из фиксированного набора: nonfinite, nonpositive, crossed, spread, outlier
- Все существующие тесты и фикстуры проходят без правок; фикстуры spot_*.json проходят фильтр без потерь; ключи площадок и USDT (1.0, 1.0) в очищенном spot сохраняются
- В p2p.py ровно одно место вызова в collect (`spot = filter_spot(spot)` внутри try после `await spot_task`); spot_prices, _mid, _route_qty, assemble, fallback-ветка не изменены (git diff их не затрагивает)
- Предупреждение в лог пишется только при смене набора отброшенных котировок, не на каждом скане (тест с caplog)
- Новых env-переменных, доменов, зависимостей, сетевых вызовов, .post(, subprocess нет; scripts/guard.py и tests/test_trading_surface.py зелёные
- В ROADMAP.md добавлена строка в начало «Журнал»
- Изменено не больше около 300 строк вместе с тестами

**Не делать:**
- Не менять сигнатуру и тело spot_prices, _mid, _route_qty, assemble, bot.py, snapshots.py, replay.py, paper.py (код stage2-speed с call=None уже в main; правка чужих функций раздувает дифф и конфликтует с открытыми ветками)
- Не добавлять новые переменные окружения, не трогать .env.example и fees.json; пороги только модульными константами
- Не защищённые пути: trading/, payouts.py, launcher.py, scripts/guard.py, CLAUDE.md, .github/, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, *.bat; любой новый путь без слов payout и trading
- Не писать в коде, тестах, комментариях и docstring слова payout и trading (в любом регистре), TRADING, trd_, orderLinkId, clientOrderId, reduceOnly, positionIdx, positionSide - регулярки guard.py их считают
- Не добавлять .post(, subprocess, os.system, eval(, exec(, новые домены, новые зависимости, getattr с вычисляемым именем
- В новых функциях p2p.py не называть параметры и переменные s, sess, session, http, client, opener и не вызывать у них .get(: tests/test_trading_surface.py считает `s.get(...)` сетевым отправителем (проверено на синтетическом исходнике). Использовать имена spot, coins, by_asset, mids и т.п.; .get не вызывать с аргументом-URL
- Не проверять спред через (ask / bid - 1) * 100 > порог: float (102/100-1)*100 = 2.0000000000000018; использовать умножение ask > bid * (1 + pct / 100), а в тестах брать значения с запасом (1.9% / 2.1%), не ровно на пороге
- Не подменять отброшенную котировку ценой другой площадки и не 'чинить' её: только отбрасывать; не применять фильтр к fallback-ветке и не менять состав SPOT_VENUES
- Не запускать сеть в тестах: только фикстуры offline и monkeypatch; не использовать asyncio.run в тестах (только helpers.arun; отдельная задача аудита asyncio.run уже в очереди)
- Не править существующие тесты и фикстуры tests/fixtures/spot_*.json, не ослаблять пороги, чтобы 'позеленить' тест
- Не заводить отдельный пункт очереди и не трогать остальные разделы ROADMAP.md, кроме одной строки в начале «Журнал»

**Где смотреть в коде (проверено на origin/main ad65d71):**
- p2p.py:756-757 - единственная проверка `if bid > 0 and ask > 0:` перед `out[venue][a] = (bid, ask)` в spot_prices (p2p.py:727-761)
- p2p.py:763-768 - _mid берёт первую площадку из SPOT_VENUES (p2p.py:52) без сверки с остальными
- p2p.py:1099-1186 - _route_qty: спот-нога берёт `spot[venue][alt]` (p2p.py:1122-1126: qty / ask или qty * bid), площадка выбирается по порядку (b.ex, s.ex) + SPOT_VENUES
- p2p.py:2182-2187 - `try: spot = await spot_task; spot_err = None` / `except Exception: spot = {'Bybit': {'USDT': (1.0, 1.0)}}`; p2p.py:2196 - тот же spot идёт в _refs, далее в assemble/Snapshot (p2p.py:2232, 2308)
- tests/test_adapters.py:65-71 - test_spot_prices проверяет только 0 < bid <= ask; :87 - test_spot_prices_survive_garbage_ticker (образец интеграционного теста)
- tests/fixtures/spot_bybit/htx/kucoin/mexc.json: ETH mid 2640.20/2686.70/2685.85/2640.68, отклонение от медианы 2663.26 не более 0.88%; BTC не более 0.72%; спреды 0.0004-0.01% - пороги 2%/5% проходят без потерь
- tests/test_speed.py:239 - `set(snap.spot['HTX']) == {'USDT'}` и `'BTC' in snap.spot['Bybit']`: очищенный spot обязан сохранять ключи площадок и USDT
- p2p.py:727 (`async def spot_prices(s, assets, call=None)`), p2p.py:2111 и ROADMAP.md:877 - код cloud/stage2-speed уже в main, старое доказательство про конфликт сигнатуры неактуально
- scripts/guard.py: PAYOUT_CODE и TRADING_CODE (строки 62-72), FORBIDDEN; guard.protected() для p2p.py, ROADMAP.md, tests/test_spot_sanity.py = False; tests/test_trading_surface.py: SESSION_NAMES = ('s','sess','session','http','client','opener') - `.get` на такой переменной считается запросом (проверено через _surface)
- Float-проверка: (102/100-1)*100 = 2.0000000000000018 > 2.0, (105/100-1)*100 = 5.000000000000004 > 5.0; 102 > 100*(1+2.0/100) = False (умножение дало 102.0)

**Пересечения с другими задачами и ветками:** Ни одна из 48 удалённых веток не содержит проверки котировок: grep sane_spot|spot_sanity|crossed|SPOT_MAX|bid > ask по diff origin/main...origin/<ветка> -- p2p.py даёт 0 совпадений; в ROADMAP.md (Очередь, Идеи, Журнал) темы нет. cloud/stage2-speed (spot_prices(call=None), spot_task с recent) уже влит в main по содержимому (p2p.py:727, 2111; Журнал 2026-09-27), поэтому конфликта по сигнатуре нет; spot_prices всё равно не трогаем. Ветки claude/paper-route-hops-venues и cloud/cross-coin-part2 правят _route_qty/_hop/paper.py, а новый код идёт сразу после _mid и одной строкой в collect; при мерже их раньше - rebase на origin/main, подтвердить, что вызов filter_spot стоит внутри try после `await spot_task`. С очередью worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, отчёты/дайджест, межмонетные связки часть 2, хедж/gates/trading) пересечений нет: их код в BestChange-стеке, trading/, paper/maker-симуляции и bot.py, а не в фильтрации спот-котировок.

---
### Задача 5. `test-hygiene-assert-guard` — Заменить единственный тест без проверки (test_sim_tick_isolates_failures) настоящими тестами sim_tick и добавить AST-сторожа «у каждого теста есть проверка»

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/test-hygiene-assert-guard`

**Зачем:** Bot.sim_tick обещает «сбой одной симуляции не мешает другой и опросу», но единственный тест на это (tests/test_simfunding.py:154-158) только вызывает функцию и ничего не проверяет: он пройдёт, даже если обернуть обе симуляции в один try или вообще не вызывать simdirectional. Среди 106 файлов tests/test_*.py (1883 теста) это единственный тест без assert/raises/fail; дешёвый AST-сторож не даст безоценочным облачным воркерам добавлять такие тесты в будущем. Польза для владельца: сбой в бумажной симуляции фандинга не должен молча глушить симуляцию направленных сделок, и это теперь под настоящей защитой.

**Что сделать:** 0) Перед началом: убедиться, что tests/test_sim_tick.py и tests/test_hygiene.py на origin/main отсутствуют (ls). Если кто-то уже создал файл с таким именем — не перезаписывать, выбрать другое имя (tests/test_sim_tick_isolation.py / tests/test_assert_guard.py) и записать это в отчёт.

1) tests/test_simfunding.py: удалить ровно строки 154-158 (def test_sim_tick_isolates_failures ... TB.Stub(p2p.Config()).sim_tick()   # не падает) и одну пустую строку-разделитель, чтобы между соседними тестами осталось две пустые строки. Больше ничего в файле не менять (импорты B, p2p, TB, monkeypatch остаются нужны другим тестам).

2) Новый tests/test_sim_tick.py. Импорты как в tests/test_simfunding.py: import logging; import bot as B; import p2p; import test_bot as TB. Общий помощник _patch(monkeypatch, fail=None) -> calls: список; создаёт две подмены tick(*a, **k) с именами 'simfunding' и 'simdirectional'; каждая делает calls.append(имя) и, если имя == fail, raise RuntimeError('x'); ставит monkeypatch.setattr(B.simfunding, 'tick', ...) и monkeypatch.setattr(B.simdirectional, 'tick', ...). Три теста, в каждом bot = TB.Stub(p2p.Config()); bot.sim_tick() (синхронный, asyncio/arun не нужен):
 (a) test_first_sim_failure_does_not_block_second: fail='simfunding'; после вызова calls == ['simfunding', 'simdirectional'] (вторая вызвана ровно один раз, исключение наружу не вылетело); с caplog.at_level(logging.ERROR) список [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == ['simfunding: x'].
 (b) test_second_sim_failure_does_not_block_first: fail='simdirectional'; calls == ['simfunding', 'simdirectional']; ERROR-сообщения == ['simdirectional: x'].
 (c) test_both_sims_run_in_order_without_errors: fail=None; calls == ['simfunding', 'simdirectional']; ERROR-записей в caplog нет.
Сеть и реальные симуляции не запускаются: оба tick всегда подменены. Проверено на origin/main: логгер bot (bot.py:48) распространяется в root, caplog его видит, текст записи именно '<имя>: x'.

3) Новый tests/test_hygiene.py (без параметров и без списка исключений). Импорты: ast, pathlib.Path. TESTS = Path(__file__).resolve().parent (путь от файла, не от cwd: смоук launcher запускает тесты из папки бота). Функция untested(source, filename='<src>') -> список (lineno, name): парсит ast.parse(source), обходит ast.walk и для каждого ast.FunctionDef/ast.AsyncFunctionDef с name.startswith('test') (методы классов тоже) проверяет наличие внутри хотя бы одного из: ast.Assert; ast.With/ast.AsyncWith, у которого context_expr — вызов pytest.raises или pytest.warns (сравнивать ast.unparse(call.func)); вызов pytest.fail; вызов, у последнего сегмента имени которого встречается 'assert' в нижнем регистре (assert_called, assert_ok...). Тесты:
 (i) test_detector_flags_only_tests_without_checks: синтетические исходники в строках — test без проверки помечен; с assert, с with pytest.raises, с pytest.fail, async def test с assert, метод класса с assert_ok(...) — не помечены; обычная функция не test* без assert не помечена; максимум ~25 строк.
 (ii) test_every_test_function_has_an_assertion: files = sorted(TESTS.glob('test_*.py')) — СТРОГО верхний уровень tests/, без rglob (tests/trading/ защищена путём и проверяет через свои хелперы bad()/ok(), в охват не входит); читать через f.read_text(encoding='utf-8'); собрать нарушителей в виде 'файл:строка:имя' и общее число test-функций; сначала assert len(files) >= 50 and total >= 1000 (защита от пустого прохождения при неверном пути/glob; сейчас 106 файлов и 1883 теста), затем assert not offenders, с сообщением 'Тесты без проверки (добавь assert, pytest.raises или хелпер assert_*): ' + перечень.
После шага 1 нарушителей 0.

4) Мутационная проверка (вручную, результат записать в отчёт/Журнал): временно поправить bot.py sim_tick (а) убрать try/except у одной симуляции — тест (a) и/или (b) должны стать красными; (б) заменить два try на один общий — красным должен стать (a). Затем ОБЯЗАТЕЛЬНО вернуть bot.py: git checkout -- bot.py; перед коммитом git diff --name-only origin/main должен показывать ровно три файла: tests/test_simfunding.py, tests/test_sim_tick.py, tests/test_hygiene.py (+ROADMAP.md по протоколу). Дополнительно проверить, что при возврате пропавшего assert в любом тесте (например, временно убрать assert в маленьком тесте на копии в памяти через untested(...)) детектор красный — это покрывает самотест из шага 3(i).

**Править:** `tests/test_simfunding.py`
**Создать:** `tests/test_sim_tick.py`, `tests/test_hygiene.py`

**Тесты (офлайн):**
- tests/test_sim_tick.py::test_first_sim_failure_does_not_block_second
- tests/test_sim_tick.py::test_second_sim_failure_does_not_block_first
- tests/test_sim_tick.py::test_both_sims_run_in_order_without_errors
- tests/test_hygiene.py::test_detector_flags_only_tests_without_checks
- tests/test_hygiene.py::test_every_test_function_has_an_assertion
- tests/test_simfunding.py (остальные тесты без изменений, в том числе test_perp_loop_refreshes_then_ticks_sims)

**Критерии приёмки:**
- AST-скан tests/test_*.py (только верхний уровень) даёт 0 тестов без проверки; сторож проверяет ещё и, что найдено >= 50 файлов и >= 1000 тестов
- Мутация «убрать try/except вокруг одной симуляции в Bot.sim_tick» делает красным тест (a) или (b), мутация «один общий try на обе симуляции» делает красным (a); результат записан в отчёт, после проверки bot.py возвращён (git diff --name-only origin/main не содержит bot.py)
- Самотест детектора (test_detector_flags_only_tests_without_checks) зелёный и падает, если из детектора убрать хотя бы одно из правил assert / pytest.raises / pytest.fail / assert_* в имени вызова
- python -m pytest -q и python scripts/guard.py зелёные; общий diff не больше 150 строк, затронуты только три перечисленных файла плюс отметка в ROADMAP.md
- В tests/test_simfunding.py удалены только строки test_sim_tick_isolates_failures, остальной файл побайтно тот же

**Не делать:**
- не менять bot.py, simfunding.py, simdirectional.py, perp.py в итоговом коммите — только тесты (временная мутация bot.py для проверки допустима, но обязана быть откачена: git checkout -- bot.py)
- не трогать другие тесты в tests/test_simfunding.py, кроме удаления test_sim_tick_isolates_failures
- не заносить нарушителей в исключения сторожа и не ослаблять правила детектора: если появится нарушитель — исправить сам тест
- не расширять сторож на tests/trading/ и любые подпапки (защищённый путь, у тестов там проверка через хелперы bad()/ok()); не использовать rglob/'**'
- не редактировать tests/conftest.py, pytest.ini, requirements.txt, .github/, scripts/guard.py, CLAUDE.md, любые пути со словом payout или trading
- не использовать в добавленных строках (включая докстринги, комментарии и тексты сообщений) слова payout / pay_ok / pay_no / subprocess / os.system / eval( / exec( / captcha и http(s):// ссылки: guard считает PAYOUT_CODE и в tests/, FORBIDDEN и домены — во всех .py
- не запускать реальные симуляции, сеть, asyncio.run/arun: sim_tick синхронный, оба tick всегда подменены monkeypatch'ем, сторож читает только текст и AST файлов
- не открывать data/, logs/, .env: пути только от Path(__file__).parent (tests/), audit hook conftest другого не пропустит
- не создавать файлы, если tests/test_sim_tick.py или tests/test_hygiene.py уже появились на main к моменту старта (см. шаг 0 spec): выбрать другое имя и упомянуть в отчёте
- не писать проверки на точное число тестов или файлов (только нижние границы), чтобы сторож не краснил при обычном добавлении тестов

**Где смотреть в коде (проверено на origin/main ad65d71):**
- tests/test_simfunding.py:154-158 на origin/main ad65d71: def test_sim_tick_isolates_failures(monkeypatch): boom -> RuntimeError('x'); monkeypatch.setattr(B.simfunding, 'tick', boom); TB.Stub(p2p.Config()).sim_tick()   # не падает — ни одного assert
- bot.py:2789-2795: Bot.sim_tick перебирает (('simfunding', simfunding.tick), ('simdirectional', simdirectional.tick)) с try/except Exception и logger.error('%s: %s', name, e); logger = logging.getLogger(__name__) (bot.py:48), propagate=True
- grep sim_tick по tests/ и bot.py: tests/test_simfunding.py:154, :158, :172 (test_perp_loop_refreshes_then_ticks_sims подменяет sim_tick целиком: monkeypatch.setattr(bot, 'sim_tick', ...)), bot.py:2784 (вызов из perp_loop), bot.py:2789 (определение)
- AST-скан tests/test_*.py на origin/main: 106 файлов, 1883 теста, ровно один без Assert/pytest.raises/warns/fail/assert_*: tests/test_simfunding.py:154. В tests/trading/ (вне охвата, защищённый путь) тот же скан показывает 25 тестов без прямого assert (test_trading_risk.py:66, :109, :115, ... test_trading_venues.py:330) — они проверяют через хелперы bad()/ok()/prepare(), поэтому сторож ограничен верхним уровнем
- Прогон в python -c без сети и без записи файлов: при simfunding.tick -> RuntimeError('x') simdirectional.tick всё равно вызван, порядок ['simfunding','simdirectional'], лог ERROR 'simfunding: x'; зеркально для simdirectional
- scripts/guard.py: protected('tests/test_sim_tick.py') = False, protected('tests/test_hygiene.py') = False, protected('tests/test_simfunding.py') = False; PAYOUT_CODE/TRADING_CODE/FORBIDDEN на предполагаемом коде и удаляемых строках не срабатывают
- tests/test_sim_tick.py и tests/test_hygiene.py отсутствуют на origin/main; ROADMAP.md не содержит темы (grep assert|sim_tick|гигиен|сторож — только несвязанная строка 1225 про audit hook conftest)
- git diff origin/main...origin/<ветка> по всем удалённым веткам: cloud/audit-sims добавляет +13 строк в КОНЕЦ tests/test_simfunding.py (после test_perp_loop_refreshes_then_ticks_sims) и меняет simfunding.py/simdirectional.py; cloud/tests-sockets уже влита (#139)

**Пересечения с другими задачами и ветками:** cloud/audit-sims правит хвост tests/test_simfunding.py (добавляет test_leg_below_min_notional_after_lot_rounding_is_rejected) и simfunding.py/simdirectional.py; наша правка — удаление 5 строк в середине того же файла, хунки не пересекаются, всё новое живёт в новых файлах. cloud/tests-sockets (#139) уже влита squash-коммитом (helpers.arun). Ни одна из 49 удалённых веток не добавляет тестов без проверок (проверено AST-сканом изменённых tests/test_*.py каждой ветки), поэтому сторож не сломает автомерж чужих веток. Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, отчёты/дайджест, межмонетные связки часть 2, хедж/гейты/торговля) тему «тест без assert» и sim_tick не затрагивает; новые тесты не используют asyncio.run/arun, сторож читает только AST. Если tests-arun-audit создаст файл с тем же именем — сработает правило шага 0 и do_not.

---
Когда все задачи волны сделаны или помечены «ждёт владельца», напиши итог: список веток, что влито, что ждёт владельца. Следующую волну не начинай — её вставит владелец.
