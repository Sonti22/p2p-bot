# Рабочая сессия 2 — репозиторий Sonti22/p2p-bot, Волна 2: надёжность скана, фильтры, устойчивость входных данных

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

## Задачи Волна 2: надёжность скана, фильтры, устойчивость входных данных

**Почему такой состав волны:** Две задачи ценности 4 (изоляция шагов скана, фильтры банков и SAME_VENUE_ONLY) и три защиты входа в p2p.py, которые не пересекаются по функциям: адаптеры площадок (bybit/htx/kucoin/mexc/bitpapa), Config.from_env, rapira_mid плюс _refs/assemble. rapira-ref-sanity идёт после spot-sanity-filter (волна 1): обе работают рядом с rapira_mid и spot_prices. В bot.py scan_loop/status_view и filters_view/settings_view/dispatch разнесены.

**Условие старта:** все задачи волн 1–1 уже влиты в main (проверь `git log origin/main`). Если нет — остановись и напиши владельцу, что ждёшь.

### Задача 1. `scan-step-isolation` — Изолировать шаги после скана в scan_loop: сбой одного шага не глушит остальные и не молчит

**Ценность:** 4/5 · **Размер:** M · **Ветка:** `claude/scan-step-isolation`

**Зачем:** Сейчас все шаги после скана (история, репутация, check_venues, check_alerts, check_networks, paper-циклы, quiet_and_pause_tick -> notify, update_market_status) стоят в одном try. Любое исключение (sqlite 'database is locked', диск, баг в шаге) обрывает оставшиеся шаги, и сигналы перестают уходить, а last_scan_ts обновлён и scan_errors=0, поэтому watchdog и /status показывают, что всё в порядке. Для круглосуточной работы это тихий отказ главной функции бота. Нужно изолировать шаги, считать подряд идущие сбои по имени шага, один раз предупредить владельца в topic dev и показать сбои в /status.

**Что сделать:** 1) bot.py, импорты: добавить 'import inspect' после 'import html'. Рядом с SCAN_STALL_MINUTES_DEFAULT/WATCHDOG_TICK (~269-270) добавить константы SCAN_STEP_ALERT_AFTER = 3 и SCAN_STEP_ALERT_RETRY_SEC = 300. Кортеж SCAN_STEP_NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) (голый OSError НЕ включать).
2) Bot.__init__ (~1546, рядом со stall_alerted): self.step_fail = {} ; значение: {'n': int, 'last': 'ТипИсключения: текст'[:200], 'alerted': bool, 'net': bool, 'try_ts': float}.
3) Новый async-метод Bot.scan_step(self, name, fn, *args): try: res = fn(*args); если inspect.isawaitable(res): await res; except Exception as e (только Exception, CancelledError и KeyboardInterrupt проходят наружу): logger.error('scan step %s: %s', name, e); rec = self.step_fail.setdefault(name, {'n': 0, 'last': '', 'alerted': False, 'net': False, 'try_ts': 0.0}); rec['n'] += 1; rec['last'] = f'{type(e).__name__}: {e}'[:200]; rec['net'] = isinstance(e, SCAN_STEP_NET_ERRORS); если rec['n'] >= SCAN_STEP_ALERT_AFTER and not rec['alerted'] and not rec['net'] and self.chat_id and time.time() - rec['try_ts'] >= SCAN_STEP_ALERT_RETRY_SEC: rec['try_ts'] = time.time(); try: r = await self.send('⚠️ Шаг скана «%s» падает %d раз подряд: %s' ..., topic='dev'); if (r or {}).get('ok'): rec['alerted'] = True; except Exception: logger.error(...). Ветка успеха (else): rec = self.step_fail.pop(name, None); если rec and rec['alerted'] and self.chat_id: try: await self.send('✅ Шаг скана «%s» снова работает', topic='dev'); except Exception: logger.error(...). Запись в step_fail удаляется всегда при успехе, даже если отправка не удалась. Использовать именно self.send (у Bot свой send), никаких новых session.post/.post(.
4) Новый метод Bot._history_step(self, snap): 'if history.record(snap, self.snap_cfg(snap).amount): history.cleanup()' (тот же смысл, что и текущие строки 2636-2637).
5) scan_loop: заменить прямые вызовы на await self.scan_step(...) в прежнем порядке: 'track_liveness' (брать атрибутом в момент вызова: self.track_liveness, позиционно self.last, без именованных аргументов), 'history' (self._history_step, snap), 'reputation' (schedule_reputation), 'venues' (check_venues), 'alerts' (check_alerts), 'networks' (check_networks), 'paper_cycles' (process_paper_cycles), 'paper_hedge' (paper_hedge_tick), 'paper_ladder' (check_paper_ladder), 'quiet_pause' (quiet_and_pause_tick), 'market_status' (update_market_status). Аргументы и условия if self.chat_id сохранить как сейчас. Не менять: блок simmaker со своим try, внешний except (scan_error/scan_errors), speed.add, fresh_scan, save_snapshot, schedule_backup, watchdog.
6) status_view: после строк 'Последний скан' и до 'if snap is None: ... return' добавить, если self.step_fail непуст, блок '⚠️ Сбои шагов скана:' с одной строкой на шаг ('• имя: N подряд, последняя ошибка ...'). Сигнатуру status_view и её __defaults__ не менять (tests/test_isolated_data.py:32).
7) tests/test_bot.py:3329: лямбду 'lambda snap: False' заменить на 'lambda *a, **k: False' (файл не защищён), чтобы тест переживал вызов через scan_step. Проверить, что test_scan_watchdog.py:140 (track(s, now=None)) и test_simmaker.py/test_calibration_ev.py остаются зелёными: если нужен, править там только подмены.
8) ROADMAP.md: в разделе '### Надёжность 24/7' (после последних [x], ~546) добавить строку '- [x] Изоляция шагов скана: ...' и в Журнал (в конец) строку '- 2026-09-29 — scan-step-isolation: ...' в формате соседних записей. Если случится конфликт при слиянии, действовать по рецепту из CLAUDE.md.
9) Перед коммитом: python -m pytest -q и python scripts/guard.py зелёные; ветка claude/scan-step-isolation.

**Править:** `bot.py`, `ROADMAP.md`, `tests/test_bot.py`
**Создать:** `tests/test_scan_step_isolation.py`

**Тесты (офлайн):**
- tests/test_scan_step_isolation.py::test_failing_step_does_not_block_later_steps: подмена check_alerts на функцию с исключением, quiet_and_pause_tick всё равно вызван, порядок шагов сохранён, scan_errors остаётся 0
- ::test_sync_and_async_steps_both_work: обычная и async-функция как fn, позиционные аргументы передаются
- ::test_alert_after_three_consecutive_failures_once: сбои 1-2 без отправки, на 3-м ровно одна отправка в topic='dev', 4-й и 5-й сбои без повторной отправки
- ::test_alert_retried_when_send_not_ok: send вернул {'ok': False} или бросил исключение -> alerted=False, повтор только после SCAN_STEP_ALERT_RETRY_SEC (патчить time.time), после ok=True больше не шлёт
- ::test_alert_deferred_until_chat_id_exists: chat_id пустой на 3-м сбое, алерт уходит на следующем сбое после появления chat_id
- ::test_recovery_message_and_cleanup: успех после алерта шлёт '✅ ... снова работает' и удаляет запись; если отправка упала, запись всё равно удалена; успех без алерта молчит
- ::test_network_errors_counted_but_no_alert: aiohttp.ClientConnectorError/asyncio.TimeoutError/ConnectionError -> n растёт, rec['net']=True, send не вызывается; sqlite3.OperationalError и OSError('disk full') алерт дают
- ::test_cancelled_error_propagates: fn бросает asyncio.CancelledError, scan_step его не глотает
- ::test_status_view_shows_step_failures: при непустом step_fail в тексте есть '⚠️ Сбои шагов скана:' и имя шага, при пустом блока нет
- ::test_scan_loop_history_failure_still_sends_signals: history.record падает (sqlite3.OperationalError), но quiet_and_pause_tick и update_market_status вызваны; last scan и scan_errors не ломаются
- существующие: tests/test_bot.py::test_scan_loop_records_last_scan_timestamp_and_duration, tests/test_scan_watchdog.py, tests/test_simmaker.py, tests/test_calibration_ev.py, tests/test_snapshots.py, tests/test_isolated_data.py, tests/test_trading_surface.py остаются зелёными

**Критерии приёмки:**
- python -m pytest -q зелёный целиком (включая tests/test_trading_surface.py и tests/test_isolated_data.py)
- python scripts/guard.py зелёный
- grep 'self.step_fail' bot.py и 'async def scan_step' bot.py находят реализацию; прямых вызовов check_venues/check_alerts/check_networks/process_paper_cycles/check_paper_ladder/quiet_and_pause_tick/update_market_status/schedule_reputation из scan_loop вне scan_step больше нет (grep по телу scan_loop)
- Порядок шагов в scan_loop идентичен прежнему (тест фиксирует порядок вызовов)
- Тест с падающим history.record показывает, что quiet_and_pause_tick вызван; тест с падающим check_alerts показывает то же
- Ровно один алерт в topic='dev' на серию из >=3 подряд сбоев; повтор возможен только при неудачной отправке и не чаще раза в SCAN_STEP_ALERT_RETRY_SEC
- Сетевые исключения не вызывают send; sqlite3.OperationalError и OSError вызывают
- asyncio.CancelledError из шага не подавляется
- /status при непустом step_fail содержит блок '⚠️ Сбои шагов скана:'; при пустом блока нет; сигнатура status_view не изменилась
- ROADMAP.md: новый пункт '- [x]' в 'Надёжность 24/7' и строка в Журнале от 2026-09-29
- git diff origin/main не затрагивает PROTECTED-пути и не содержит слов payout/TRADING/trading.x, exec(/eval(/captcha/adb, .post(
- Суммарный размер изменения не больше ~300 строк

**Не делать:**
- Не трогать PROTECTED-пути: .github/, scripts/guard.py, launcher.py, CLAUDE.md, payouts.py, tests/trading/, pytest.ini, conftest.py, run.bat, data/, logs/, .env, tests/test_trading_surface.py
- Не писать в добавленных строках слово payout (регистр не важен, считается и в tests/), заглавное TRADING, trd_, trading.<x>; не использовать exec(, eval(, subprocess, os.system, captcha, adb; не добавлять новых доменов
- Не добавлять .post( и новые сетевые вызовы: алерты только через существующий self.send(..., topic='dev'); не использовать getattr с вычисляемым именем шага
- Не ловить BaseException и голый except: только except Exception; CancelledError обязан проходить наружу
- Не оборачивать fresh_scan и блок simmaker, не менять внешний except (scan_error/scan_errors), watchdog, speed.add, save_snapshot, schedule_backup
- Не менять сигнатуру status_view и Bot.send/Bot.call; не вводить новые переменные окружения и не читать .env, data/keys.json, папку ключ и живую папку C:\Users\User\Desktop\p2p-bot
- Не добавлять OSError в кортеж сетевых исключений (иначе 'диск полон' не даст алерта)
- Не слать алерт по каждому сбою и не спамить: строго по правилу 3 подряд + пауза повтора + флаг alerted
- Не выполнять живых запросов к сети/бирже в тестах; только Stub и заблокированную сеть из conftest.py
- Не расширять задачу: никакой перестройки scan_loop, дайджестов, новых команд бота

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:2626-2662 scan_loop; try с 2629, last_scan_ts 2632, scan_errors = 0 2633
- bot.py:2636-2637 history.record + cleanup внутри общего try
- bot.py:2647-2654 цепочка if self.chat_id: check_venues ... quiet_and_pause_tick (2653) ... update_market_status
- bot.py:2655 except Exception: logger.error('scan error'), при snap is not None ошибка нигде не учитывается
- bot.py:1491-1560 Bot.__init__, stall_alerted на 1546 (место для step_fail)
- bot.py:2840 status_view(self, status_path=DEV_STATUS), строки 'Последний скан' идут до 'if snap is None'
- bot.py:3044 quiet_and_pause_tick вызывает notify
- bot.py:269-270 SCAN_STALL_MINUTES_DEFAULT/WATCHDOG_TICK (место для констант)
- bot.py:2-12 импорты, import inspect отсутствует
- history.py:35-37 sqlite3.connect(path) с тайм-аутом по умолчанию; history.py:64-80 record без внутреннего try; backup.py:87 sqlite3.connect(src)
- tests/test_bot.py:3329 test_scan_loop_records_last_scan_timestamp_and_duration, лямбда 'lambda snap: False'
- tests/test_scan_watchdog.py:140 подмена track_liveness как track(s, now=None)
- tests/test_isolated_data.py:32 читает B.Bot.status_view.__defaults__[0]
- tests/test_trading_surface.py ALLOWED_SENDERS: Bot.call, Bot._post_photo, Bot.send_document; self.send внутри Bot не считается отправителем
- scripts/guard.py PROTECTED, PAYOUT_CODE, TRADING_CODE, FORBIDDEN: 0 совпадений на планируемых строках
- ROADMAP.md:542 раздел 'Надёжность 24/7', Журнал с 773 до конца (формат '- 2026-09-DD — ...'), пункта пока нет
- git ls-remote --heads и git grep 'scan_step|step_fail' по origin/main и веткам: нигде не найдено

**Пересечения с другими задачами и ветками:** Ни в одной удалённой ветке нет scan_step/step_fail. Старые несмерженные cloud/scan-watchdog, cloud/scan-watchdog-v2 и cloud/s2-merchant-reputation (28.09) содержат правки рядом с Bot.__init__ и scan_loop, но их функции (watchdog, schedule_reputation) уже есть в main; возможен только конфликт по контексту при слиянии, решается рецептом из CLAUDE.md. В ROADMAP пункта нет, работа воркера 1 этот пробел не покрывает. Тесты, подменяющие шаги scan_loop (test_bot.py:3329, test_scan_watchdog.py:140, test_simmaker.py, test_calibration_ev.py), нужно прогнать и при необходимости поправить лямбды подмен (не защищены).

---
### Задача 2. `filters-show-bank-and-venue-filter` — Фильтры: банки и «только внутри одной площадки» видны и переключаются в «🎛 Фильтры» и /filters

**Ценность:** 4/5 · **Размер:** M · **Ветка:** `claude/filters-show-bank-and-venue-filter`

**Зачем:** INCLUDE_PAY (фильтр по банкам, его пишет онбординг) и SAME_VENUE_ONLY реально режут сигналы, но в меню «Фильтры» и в настройках их не видно и поменять из Telegram нельзя; TOPIC_HINTS['settings'] обещает команду /filters, которой нет. Владелец тратит время на «почему нет сигнала» и правку .env вручную. Задача закрывает пробел без новых env-имён, сети и торговли.

**Что сделать:** Все правки в bot.py (по имени функции, номера строк на ad65d71 приблизительны). 1) filters_view (~687-700): добавить строку «Банки: <список include_pay через запятую, либо «любые»>» и строку «Только внутри одной площадки: вкл/выкл» (по cfg.same_venue_only), плюс короткую заметку, что «🏦 Мои банки» - другой список (для аккаунта, не для фильтра сигналов). Кнопки: банки из ONBOARD_BANKS по 3 в ряд, callback 'flt_b:<Name>' (Name - ровно строка из ONBOARD_BANKS), метка ✅ если name.lower() in cfg.include_pay, иначе ⬜; кнопка сброса 'flt_b:*' только если include_pay не пуст; кнопка-переключатель 'flt_sv'. Существующие кнопки flt_a:/flt_e:/preset и ряд «назад» сохранить как есть. Длина callback_data <= 64 байт (проверить в тесте для всех банков). 2) Bot.apply (~2576-2582, рядом с ветками flt_a:/flt_e:): добавить ветку flt_b: - если суффикс == '*': save_env('INCLUDE_PAY', '') и тост «Банки: любые»; иначе суффикс ДОЛЖЕН точно совпасть с элементом ONBOARD_BANKS, иначе вернуть тост «Нет такой кнопки» и НИЧЕГО не писать (защита от подделанных callback и инъекции строк в .env). Текущий список брать из self.cfg.include_pay (уже в нижнем регистре), переключить name.lower(), значения не из ONBOARD_BANKS (ручные из .env) сохранить, порядок стабильный, без дублей; писать save_env('INCLUDE_PAY', ','.join(...)). Ветка flt_sv: save_env('SAME_VENUE_ONLY', '0' если сейчас включено, иначе '1'), тост с новым состоянием. Использовать штатный save_env и то, как соседние ветки перечитывают cfg (смотреть flt_a:), ничего не менять в самом save_env. 3) on_callback (~3893): в кортеж префиксов перерисовки добавить 'flt_b:' и 'flt_sv' (после apply тост, затем перерисовка filters_view, как для flt_a:). 4) settings_view (~2426-2455): строка «Банки: …» только если include_pay не пуст; строка про одну площадку только если фильтр включён; при пустых значениях вывод байт-в-байт прежний (существующие тесты settings_view не должны меняться). 5) dispatch (~4152, рядом с /settings): ветка '/filters' - показывает filters_view, как кнопка 'filters' (~3977); владелец-only обеспечивается существующим шлюзом (GUEST_CMDS не менять, гость по-прежнему получает отказ). В COMMANDS (~100) добавить ('filters', ...) с русским описанием, копируя формат соседних записей. 6) Онбординг (~3863): в финальном тексте после выбора банков одной фразой пояснить, что банки меняются в «🎛 Фильтры»; слово «Готово» оставить (его ждут tests/test_onboarding.py:140). Текст onboarding_banks_view (~676) не менять. 7) tests/test_filters_bank_venue.py (новый): локальный Stub(B.Bot) с call(), пишущим в self.out, и локальный env_file (tmp .env, monkeypatch B.save_env через functools.partial(B.save_env, path=str(env))); НЕ импортировать tests/payout_stubs.py. Тесты: (а) filters_view показывает «Банки» и строку про площадку, ✅/⬜ по include_pay; (б) flt_b:<Name> добавляет банк в INCLUDE_PAY, повтор снимает, ручное значение из .env сохраняется; (в) flt_b:* очищает; (г) подделанные flt_b:Evil\nPAYOUTS=1 и flt_b:НетТакого не пишут ничего в .env и дают «Нет такой кнопки»; (д) flt_sv переключает SAME_VENUE_ONLY 1/0; (е) settings_view: строки условные; (ж) /filters у владельца отвечает filters_view, у гостя - отказ, без записи; (з) callback_data всех кнопок <= 64 байт; (и) существующий кортеж перерисовки: on_callback('flt_b:...') перерисовывает фильтры. По протоколу CLAUDE.md добавить одну строку в «Журнал» ROADMAP.md (не в защищённые пути).

**Править:** `bot.py`, `ROADMAP.md`
**Создать:** `tests/test_filters_bank_venue.py`

**Тесты (офлайн):**
- python -m pytest -q tests/test_filters_bank_venue.py
- python -m pytest -q tests/test_bot.py tests/test_onboarding.py tests/test_guests.py tests/test_payout_pins.py tests/test_env_documented.py tests/test_trading_surface.py
- python -m pytest -q
- python scripts/guard.py

**Критерии приёмки:**
- /filters у владельца открывает экран «Фильтры» с строками «Банки» и «Только внутри одной площадки», кнопки банков по 3 в ряд с ✅/⬜
- Нажатие банка добавляет/снимает его в INCLUDE_PAY, ручные значения из .env сохраняются, flt_b:* очищает список, flt_sv переключает SAME_VENUE_ONLY; после каждого нажатия экран перерисовывается
- Подделанный flt_b: с именем не из ONBOARD_BANKS ничего не пишет в .env и возвращает «Нет такой кнопки»
- settings_view показывает «Банки:» и строку про площадку только когда они заданы/включены; в остальных случаях вывод не изменился
- Гость на /filters получает отказ (GUEST_CMDS не расширен); все существующие тесты, tests/test_payout_pins.py и tests/test_trading_surface.py зелёные без правки пинов
- python scripts/guard.py зелёный; нет новых env-имён, доменов, зависимостей, subprocess/os.system и .post( вне trading/

**Не делать:**
- Не использовать в добавляемых строках идентификаторы pay_ok, pay_no, pay_to, pay_hist, pay_stop и слова payout/PAYOUT (guard блокирует автомерж, в том числе в tests/)
- Не импортировать tests/payout_stubs.py и не копировать из него код; писать локальные Stub и env_file
- Не менять запиненные имена: GUEST_CMDS/GUEST_CALLBACKS/GUEST_DENIED/GUEST_MENU/GUEST_WELCOME, save_env, Bot._owner_gate, Bot.is_guest, Bot.on_guest_callback, Bot.cmd_payout; не добавлять новые имена с префиксами payout/PAYOUT_/GUEST_
- Не расширять GUEST_CMDS и GUEST_CALLBACKS: /filters и flt_* только для владельца
- Не менять p2p.py, presets.py, accounts.py, .env.example, onboarding_banks_view (bot.py:~676), конфиг Config и семантику _pays/same_venue_only
- Не вводить новые env-имена; писать только INCLUDE_PAY и SAME_VENUE_ONLY через штатный save_env; не писать в .env значения, не прошедшие проверку по ONBOARD_BANKS
- Не добавлять .post(, сеть, subprocess/os.system, новые зависимости и домены; не трогать защищённые пути (CLAUDE.md, scripts/guard.py, launcher.py, conftest.py, .github/, data/, .env)
- Не читать и не открывать .env, data/keys.json и любые файлы ключей; тесты только на фиктивных tmp .env

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:54 TOPIC_HINTS['settings'] упоминает /filters, а в dispatch (bot.py:4072-4195) ветки /filters нет
- bot.py:687-700 filters_view: нет строк про INCLUDE_PAY и SAME_VENUE_ONLY
- bot.py:659 ONBOARD_BANKS; bot.py:3842-3863 онбординг кладёт ob['banks'] в include_pay
- bot.py:2576-2582 Bot.apply с ветками flt_a:/flt_e: (образец для flt_b:/flt_sv); bot.py:3893 кортеж ('flt_a:', 'flt_e:', 'preset_apply:') в on_callback; bot.py:3977 elif data == 'filters'
- bot.py:2426-2455 settings_view; bot.py:100 COMMANDS; bot.py:436 save_env
- p2p.py:111 _list (нижний регистр), p2p.py:772-779 _pays (подстрока по cfg.include_pay), p2p.py:2303 same_venue_only
- .env.example уже содержит INCLUDE_PAY и SAME_VENUE_ONLY (tests/test_env_documented.py пройдёт без правок)
- scripts/guard.py: PAYOUT_CODE = payout|\bpay_(?:to|ok|no|hist|stop)\b по добавленным строкам любых не-.md файлов; tests/test_payout_pins.py пинит sha256 GUEST_*, save_env, _owner_gate, is_guest, on_guest_callback, cmd_payout

**Пересечения с другими задачами и ветками:** Ни одна origin/* ветка не трогает flt_b/flt_sv//filters/include_pay/same_venue_only/filters_view в bot.py; в ROADMAP.md и git log такой задачи нет. Остаточный риск: параллельная очередь owner-commands-consistency правит регион dispatch и кортеж on_callback (bot.py:~3893) - конфликт разрешать rebase на свежий main, вставки держать компактными и локальными; строка в «Журнал» ROADMAP.md может конфликтовать с другими ветками, при конфликте оставить обе строки.

---
### Задача 3. `adapters-per-ad-tolerance` — Адаптеры площадок: пропуск одного битого объявления вместо падения всей площадки

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/adapters-per-ad-tolerance`

**Зачем:** Сейчас одно объявление с price=None, пропущенным ключом или inf в числовом поле роняет весь адаптер Bybit/HTX/KuCoin/MEXC/BitPapa: collect уводит площадку в venue_failed и backoff 30..600 с, то есть бот теряет весь стакан площадки из-за одной строки. Отдельная дыра: price 'nan' и '0' проходят молча, nan-цена проходит фильтр max_dev и попадает в snap.deals. Фикс: разбор по объявлениям с пропуском битого, проверка price конечная и >0, счётчик ADS_SKIPPED для наблюдаемости, и громкий сбой, если не разобрано НИ ОДНО объявление (смена схемы API).

**Что сделать:** 1) В p2p.py сразу после _json (строки ~404-413) и до _bybit_pay добавить модульные объекты: ADS_SKIPPED: dict[tuple[str, str, str], int] = {} ; _BAD_AD_ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError) ; функцию _parse_ads(venue, side, asset, items, make) -> list[Ad]. Логика: bad = 0, first_error = None; for it in items: try: ad = make(it); если ad не None и не (math.isfinite(ad.price) and ad.price > 0) - raise ValueError(f'bad price {ad.price!r}'); except _BAD_AD_ERRORS as ex: bad += 1; запомнить первое 'ТипИсключения: текст'; continue; если ad не None - добавить в результат. make возвращающий None - это намеренный фильтр (например is_suspicious у bitpapa), он НЕ считается битым. После цикла: ADS_SKIPPED[(venue, side, asset)] = bad (присваивание на каждый ответ, 0 при чистом ответе, без накопления); если bad - один logger.debug со счётчиком и первой ошибкой (logger и math уже импортированы в p2p.py); если items непустой и bad == len(items) - raise ValueError(f'{venue}: не разобрано ни одно из {len(items)} объявлений: {first_error}') - так смена схемы по-прежнему валит площадку с backoff и текстом в errors['{n}/{asset}']. 2) В bybit, htx, kucoin, mexc, bitpapa заменить цикл разбора на вызов _parse_ads с вложенной функцией make, в которой логика полей ОДИН В ОДИН как сейчас (ни URL, ни params, ни JSON_ALLOWED, ни формулы не менять). _note_page остаётся ДО разбора и считает сырое число элементов. Ошибки запроса в _json не глотать. Итерация по не-списку (items is None) остаётся как было. 3) LBank (свой try/except) и bestchange не трогать. 4) Новый файл tests/test_adapter_bad_items.py: фикстура offline из conftest, поверх неё обёртка над fake_json (сначала прочитать сигнатуру fake_json в tests/conftest.py), которая мутирует загруженный JSON: не подменять p2p._json напрямую, иначе сломаются прелоады _bybit_pay/_mexc_pay/_mexc_coins. Для каждого из 5 адаптеров (buy и sell, где есть фикстура): (a) одно объявление битое (price=None / удалён обязательный ключ / нечисловая строка / inf в целочисленном поле) - остальные разобраны, исключения нет, ADS_SKIPPED == 1; (b) price 0, отрицательная, 'nan', 'inf' - объявление отброшено; (c) чистый ответ даёт те же Ad, что и раньше (сверка с существующими ожиданиями tests/test_adapters.py, сам файл не менять) и ADS_SKIPPED == 0; (d) все объявления битые - pytest.raises(ValueError); (e) через p2p.collect: на всех-битых площадка попадает в errors/venue_failed и получает backoff, на одном-битом - нет; (f) bitpapa is_suspicious по-прежнему отфильтровывается и не считается битым. Использовать arun и make_ad из tests/helpers.py. 5) ROADMAP.md: короткая запись в разделе 'Надёжность 24/7' (по стилю соседних пунктов, рядом со строкой ~246 про мусорные тикеры spot_prices) с описанием поведения и счётчика ADS_SKIPPED.

**Править:** `p2p.py`, `ROADMAP.md`
**Создать:** `tests/test_adapter_bad_items.py`

**Тесты (офлайн):**
- tests/test_adapter_bad_items.py: одно битое объявление у каждого из 5 адаптеров не роняет разбор, остальные Ad целы, ADS_SKIPPED == 1
- tests/test_adapter_bad_items.py: price 0 / отрицательная / nan / inf отбрасываются и считаются в ADS_SKIPPED
- tests/test_adapter_bad_items.py: чистый ответ даёт прежние Ad, ADS_SKIPPED == 0
- tests/test_adapter_bad_items.py: все объявления битые -> ValueError с числом объявлений и первой ошибкой
- tests/test_adapter_bad_items.py: через collect - all-bad уходит в venue_failed и backoff, one-bad нет
- tests/test_adapter_bad_items.py: bitpapa is_suspicious отфильтрован и не входит в счётчик битых
- существующие tests/test_adapters.py, tests/test_lbank.py и весь pytest проходят без изменений

**Критерии приёмки:**
- pytest целиком зелёный, tests/test_adapters.py и tests/test_lbank.py не изменены
- python scripts/guard.py на диффе проходит (protected, PAYOUT_CODE, TRADING_CODE, FORBIDDEN, новые домены)
- tests/test_trading_surface.py зелёный: JSON_ALLOWED_PIN и список сетевых отправителей не изменились, новых сетевых вызовов нет
- Для каждого из 5 адаптеров ответ из >=2 объявлений с одним битым даёт список из остальных без исключения, ADS_SKIPPED[(venue, side, asset)] == 1
- Ответ, где не разобрано ни одно объявление (items непустой), поднимает ValueError и уходит в venue_failed/backoff как раньше
- Объявления с price nan, inf, 0 или отрицательной не попадают в результат адаптера
- Разбор чистых ответов побайтно эквивалентен прежнему (те же поля Ad)
- Нет новых зависимостей, доменов, env-переменных; запись в ROADMAP.md есть; дифф около 150-250 строк, не больше 300

**Не делать:**
- Не менять URL, params, JSON_ALLOWED и формулы разбора полей; не оборачивать целиком адаптеры или _json в try/except (ошибки запроса должны падать как раньше)
- Не глотать ситуацию 'все объявления битые' - она обязана поднимать ValueError
- Не трогать bestchange, lbank, _note_page (остаётся до разбора), trading/, payouts.py, launcher.py, scripts/guard.py, CLAUDE.md, .github/, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, *.bat, .env*
- Не писать слова payout и trading/TRADING в коде, тестах и комментариях; не использовать идентификаторы pay_ok, pay_no, pay_to, pay_hist, pay_stop (PAYOUT_CODE проверяет и тесты, без учёта регистра)
- Не писать positionSide, orderLinkId и подобные слова в p2p.py (TRADING_CODE)
- Не использовать .post(, subprocess, os.system, eval(, exec(, getattr для поиска сетевых методов; не называть параметры хелпера s/sess/session/http/client и не вызывать на них .get/.open/.send/.request (AST-сканер test_trading_surface)
- Не подменять p2p._json напрямую в тестах без обёртки над offline fake_json: сломаются прелоады _bybit_pay/_mexc_pay/_mexc_coins
- Не делать ADS_SKIPPED накопительным между ответами и не считать намеренный фильтр (bitpapa is_suspicious, make вернул None) за битое объявление
- Не читать и не открывать ключи, .env, data/keys.json, папку ключ и живую папку бота; не запускать bot.py, launcher.py и ничего, что ходит в сеть

**Где смотреть в коде (проверено на origin/main ad65d71):**
- p2p.py:404-413 (_json), 416-429 (_bybit_pay, bybit), 431-445 (htx), 447-467 (_kucoin_pay, kucoin), 470-494 (_mexc_pay, _mexc_coins, mexc), 496-519 (bitpapa) - разбор float(i['price'])/int(...)/i['nickName'] без защиты
- p2p.py:521-563 (lbank) - образец с локальным try/except, тест tests/test_lbank.py::test_lbank_own_ad_fields_and_broken_item, коммит 8d2d555
- p2p.py:1596-1620 (_venue_backoff*, VENUE_BACKOFF_BASE/MAX 30..600 с) и collect ~2061: исключение адаптера -> venue_failed -> backoff + errors['{n}/{asset}']
- p2p.py:1923 (_note_page считает сырые элементы до разбора), 1850 (PAGE_SIZE), 697 (FETCHERS), 2236 (assemble)
- Прототип на фикстуре bybit_ads.json с подменённым _json: price None -> TypeError, нет ключа -> KeyError, recentOrderNum inf -> OverflowError, price 'nan' и '0' проходят молча, не-список -> TypeError
- Эксперимент assemble: nan-цена buy-объявления даёт сделку с nan в snap.deals (сравнение abs(a.price/r-1)*100 > max_dev False для nan), нулевая цена отбрасывается только при наличии ref, при ref=None и нулевых ценах ZeroDivisionError в _stack
- scripts/guard.py: PAYOUT_CODE = payout|\bpay_(?:to|ok|no|hist|stop)\b (регистронезависимо, к добавленным и удалённым строкам всех не-.md непротектных файлов включая тесты), TRADING_CODE только не-тесты; guard.protected() False для p2p.py, ROADMAP.md, tests/test_adapter_bad_items.py
- tests/conftest.py: фикстура offline, ROUTES, load() читает JSON заново на каждый вызов, сбрасывает _bybit_pay/_mexc_pay/_mexc_coins и p2p._venue_backoff; tests/helpers.py: arun, make_ad; tests/test_adapters.py как образец
- Фикстуры: bybit buy 4/sell 4, htx 4/3, kucoin 4/2, mexc 4/2, bitpapa 4/2 объявлений - везде >=2, сценарий 'одно битое из нескольких' применим
- ROADMAP.md:246 - ближайший пункт про мусорные тикеры spot_prices, записи про адаптеры нет

**Пересечения с другими задачами и ветками:** Ни одна из ~50 удалённых веток (трёхточечные диффы) не меняет парсинг адаптеров в p2p.py. cloud/stage2-depth-realism устарела, её содержимое влито в main через #132. LBank и bestchange намеренно не трогаются, чтобы не пересекаться с правками _bc_parse в bc-dump-row-robustness; хелпер _parse_ads добавляется между _json и _bybit_pay, вызовы - внутри тел пяти адаптеров. Новый тестовый файл не конфликтует с tests/test_adapters.py. Очередь worker-1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports/digest, cross-coin part 2, hedge/gates) адаптеры не затрагивает.

---
### Задача 4. `config-env-numeric-sanity` — Config.from_env: проверка диапазонов и конечности числовых настроек и разбор комиссий (без изменения _fees)

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/config-env-numeric-sanity`

**Зачем:** Числовые настройки Config.from_env читаются сырым int()/float(): MAX_DEV=nan молча выключает отсев аномальных цен (сравнение с nan всегда False), MAX_DEV=0 отсекает всё, MAX_DEV=4% или пустое значение роняет бота при старте, отрицательные PAY_FEE/SPOT_FEES/RISK_BUFFER/TRANSFER_FEES завышают прибыль каждой связки. Десятичная запятая в комиссиях (BTC:0,0002) даёт нулевую комиссию монеты, часть без двоеточия молча теряется. Толерантные разборщики уже есть для AMOUNT/MIN_PROFIT/MERCHANT_OFFLINE_MIN/VENUE_TIMEOUT/FRESH_WINDOW (_env_parsed), но не для остальных полей. Тестов на это нет. ВАЖНО: общий `_fees` менять нельзя - от его строгости зависит защищённый скрипт порогов хеджа и его тесты; толерантность делается только внутри Config.from_env.

**Что сделать:** Всё в p2p.py, `_fees` (p2p.py:115-121) НЕ ТРОГАТЬ: его строгое поведение (ValueError на мусоре, отрицательные/nan/inf возвращаются как есть) нужно simperp.risk_buffers() -> scripts/trading_gates_stats.py:752-760 и tests/test_trading_gates_stats.py:745, 985-1010, а также calibration.py и replay.py:48.

1) Рядом с `_env_parsed` (после p2p.py ~стр. 170) добавить приватную `_parse_num(text)`: убирает все пробелы и один завершающий '%'; принимает только `re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", t)` (так отсекаются nan/inf/infinity/экспонента/«1_0», как в parse_amount и parse_min_profit), возвращает float(t.replace(',', '.')) или None.

2) Добавить `_env_num(name, default, lo=None, hi=None, cast=float)`: raw = os.getenv(name); None или пустая/пробельная строка -> default без warning (как незаданная); value = _parse_num(raw); для cast=int значение обязано быть целым (value == int(value)), иначе отказ; границы lo/hi включительно; при отказе `logger.warning("%s=%s в .env не подходит, использую %s", name, raw[:40], default)` и return default (формат как у _env_parsed; именно %s, потому что default может быть None); успех -> cast(value). Первый параметр функции должен называться `name` и идти прямо в os.getenv(name): так AST-тест tests/test_env_documented.py распознаёт обёртку и требует имена в .env.example (все нужные там уже есть).

3) Добавить `_env_fees(name, default_spec, upper=True, hi=None)`: raw = os.getenv(name); None -> `_fees(default_spec, upper)`; пустая/пробельная строка -> `_fees(raw, upper)` (то есть {} как сейчас: семантику пустого значения НЕ менять). Иначе разбор `raw.split(',')`: часть с ':' -> ключ (strip, upper при upper=True) и текст значения; следующая часть без ':' из одних цифр (`\d+`) при целом значении без точки у предыдущей части приклеивается как дробная (десятичная запятая: «BTC:0,0002» -> 0.0002, «USDT:1,5» -> 1.5); любая другая часть без ':' -> warning «часть «x» без «:» пропущена (формат МОНЕТА:число)» и пропуск; значение проверяется _parse_num, конечно, >= 0 и <= hi (если задан). Отказ по значению -> warning с именем настройки и частью (raw части обрезать до 40 символов) и ОТКАТ К ЗНАЧЕНИЮ ПО УМОЛЧАНИЮ ДЛЯ ЭТОГО КЛЮЧА, если ключ есть в `_fees(default_spec, upper)`, иначе ключ не добавляется. Откат нужен, чтобы «BTC:-1» в TRANSFER_FEES не превратился в отсутствующую монету = комиссия 0 (p2p.py:1025/1043 берут `.get(asset, 0)`). Семантика «монета не указана в TRANSFER_FEES -> 0» не меняется.

4) Config.from_env (p2p.py:293-323): min_orders=_env_num("MIN_ORDERS", 100, 0, MERCHANT_ORDERS_MAX, int); min_rate=_env_num("MIN_RATE", 95.0, 0.0, 100.0); max_dev=_env_num("MAX_DEV", 4.0, 0.1, 50.0); interval=_env_num("INTERVAL", 20, 5, 3600, int); alt_interval=_env_num("ALT_INTERVAL", 60, 5, 3600, int); bc_refresh=_env_num("BC_REFRESH", 120, 10, 3600, int); pay_fee=_env_num("PAY_FEE", 0.0, 0.0, 20.0); risk_penalty=_env_num("RISK_PENALTY", 1.5, 0.0, 50.0); spot_fees=_env_fees("SPOT_FEES", DEFAULT_SPOT_FEES, upper=False, hi=100.0); risk_buffer=_env_fees("RISK_BUFFER", DEFAULT_RISK, hi=100.0); transfer_fees через `fees = _env_fees("TRANSFER_FEES", DEFAULT_FEES)`. Легаси-ветку (стр. 295-296, только если TRANSFER_FEES не в os.environ) заменить на `v = _env_num("TRANSFER_FEE", None, 0.0, 1000.0)` и `if v is not None: fees["USDT"] = v`. Значения по умолчанию не менять (они равны дефолтам dataclass Config). Границы взяты широкими: .env.example и README дают INTERVAL=10, ALT_INTERVAL=60, BC_REFRESH=120, MIN_ORDERS=50, MIN_RATE=95, MAX_DEV=4, PAY_FEE=0, RISK_PENALTY=1.5, все внутри диапазонов (CLAUDE.md рекомендует интервал ~10 с и выше, нижняя граница 5 оставлена ради уже работающих .env). AMOUNT/MIN_PROFIT/MERCHANT_*/FRESH_WINDOW/VENUE_TIMEOUT/EV_RANK/остальные ветки from_env не трогать.

5) tests/test_config_env_sanity.py (см. tests). 6) ROADMAP.md: одна строка в верхней части «Журнал» (дата, что сделано, что `_fees` намеренно не менялся и почему); в «Очереди» задачи нет, так что отмечать нечего.

**Править:** `p2p.py`, `ROADMAP.md`
**Создать:** `tests/test_config_env_sanity.py`

**Тесты (офлайн):**
- autouse-фикстура: monkeypatch.delenv(raising=False) для MIN_ORDERS, MIN_RATE, MAX_DEV, INTERVAL, ALT_INTERVAL, BC_REFRESH, PAY_FEE, RISK_PENALTY, SPOT_FEES, RISK_BUFFER, TRANSFER_FEES, TRANSFER_FEE (живое окружение ПК не должно влиять)
- без env: скалярные поля Config.from_env() равны полям Config() (защита от расхождения дефолтов), spot_fees/risk_buffer/transfer_fees равны дефолтным словарям, warning в caplog нет
- параметризованно для каждого из 8 числовых полей: 'nan', 'inf', '-inf', 'abc', '-1', '1e3', '1_0', слишком большое, слишком малое -> Config собирается без исключения, поле == default, в caplog есть 'ИМЯ=значение'; '' и '  ' -> default без warning
- MAX_DEV: 'nan' -> 4.0 (фильтр не выключен, значение конечное), '0' -> 4.0, '4%' -> 4.0, '4,5' -> 4.5, ' 3 ' -> 3.0; PAY_FEE '0,3' -> 0.3; MIN_ORDERS '50' -> 50, '50.5' -> 100 (default) с warning; INTERVAL '10' -> 10
- TRANSFER_FEES='BTC:0,0002,ETH:0.001' -> BTC 0.0002 и ETH 0.001 (не 0.0); 'USDT:-1,BTC:0.0002' -> USDT равен дефолту 1.0 (не 0 и не -1) + warning; 'USDT:nan', 'BTC:abc' -> откат к дефолту монеты; 'мусор,USDT:2' -> не падает, warning про часть без ':', USDT 2.0; монета вне списка ('TON' при 'USDT:1') -> cfg.transfer_fees.get('TON', 0) == 0 (семантика не изменилась)
- SPOT_FEES='Bybit:-0.1' -> Bybit 0.1 (дефолт) + warning, регистр ключей площадок сохраняется; RISK_BUFFER='BTC:0,3,ETH:0,5' -> 0.3/0.5; 'SOL:-1' -> ключа SOL нет (у него нет дефолта); RISK_BUFFER='' -> {} как раньше
- легаси: TRANSFER_FEE='abc' (без TRANSFER_FEES) -> USDT остаётся дефолтным 1.0 + warning; TRANSFER_FEE='2,5' -> USDT 2.5; TRANSFER_FEE='-1' -> 1.0; при заданном TRANSFER_FEES легаси игнорируется как раньше
- пин контракта общего `_fees`: p2p._fees('BTC:abc') поднимает ValueError, p2p._fees('BTC:-0.5') == {'BTC': -0.5}, p2p._fees('BTC:nan') содержит nan (на этом стоит скрипт порогов хеджа; в комментарии теста называть его «скрипт порогов хеджа», без слов из do_not)
- полный python -m pytest -q (в том числе tests/test_env_documented.py, tests/test_amount.py, tests/test_replay.py, tests/test_trading_gates_stats.py без правок) и python scripts/guard.py остаются зелёными

**Критерии приёмки:**
- Для каждой из 8 числовых настроек и легаси TRANSFER_FEE любое значение из набора nan / inf / -inf / abc / -1 / 1e3 / 1_0 / вне диапазона не роняет Config.from_env(), поле получает значение по умолчанию, в логе ровно один warning с 'ИМЯ=значение'; пустое значение -> default без warning
- MAX_DEV=nan и MAX_DEV=0 дают cfg.max_dev == 4.0; MAX_DEV=4% -> 4.0; MAX_DEV=4,5 -> 4.5
- Комиссии: отрицательные/nan/inf/нечисловые значения в SPOT_FEES, RISK_BUFFER, TRANSFER_FEES не принимаются - ключ откатывается к значению по умолчанию (если оно есть) с warning; десятичная запятая BTC:0,0002 даёт 0.0002; часть без ':' логируется и не роняет разбор; монета, не указанная в TRANSFER_FEES, по-прежнему = 0; пустой RISK_BUFFER/TRANSFER_FEES по-прежнему = {}
- Функция p2p._fees, simperp.py, calibration.py, replay.py, scripts/, trading/ не изменены (git diff по ним пуст, кроме p2p.py вне тела _fees); все существующие тесты проходят без правок
- Разумные значения (все из .env.example и README) дают те же поля Config, что и раньше; значения по умолчанию не изменились; новых имён env-переменных нет, .env.example не тронут; изменённых строк не больше ~300 с тестами
- python scripts/guard.py и tests/test_trading_surface.py, tests/test_env_documented.py зелёные

**Не делать:**
- НЕ менять функцию `_fees` (p2p.py:115-121) и её вызовы в calibration.py, simperp.py, replay.py, scripts/: ей нужна строгость (ValueError на мусоре, отрицательные/nan/inf возвращаются как есть), иначе покраснеют tests/test_trading_gates_stats.py:745 и :985-1010 и сломается fail-closed скрипта порогов хеджа; править те тесты нельзя
- Не добавлять новые имена env-переменных и не править .env.example и README
- Не менять значения по умолчанию, семантику «монета не указана в TRANSFER_FEES -> 0» и семантику пустого значения словарных настроек ('' -> {})
- Не менять AMOUNT/MIN_PROFIT/MERCHANT_*/FRESH_WINDOW/VENUE_TIMEOUT/EV_RANK/EXCLUDE_PAY и прочие ветки from_env
- Не читать и не логировать .env и ключи: в warning только имя настройки и отвергнутое значение (обрезать до 40 символов)
- Не трогать trading/, payouts.py, launcher.py, scripts/guard.py, CLAUDE.md, .github/, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, *.bat, .env*, tests/test_trading_gates_stats.py и любые файлы с payout/trading в пути
- Не писать слова payout и trading (в любом регистре) в новом коде, тестах и комментариях; не добавлять .post(, subprocess, eval, exec, новые домены и import новых модулей; не делать `import calibration` в p2p.py (циклический импорт) - свой _env_num
- Не менять первый параметр `_env_num` (`name`, идёт прямо в os.getenv(name)) - на нём держится AST-проверка tests/test_env_documented.py

**Где смотреть в коде (проверено на origin/main ad65d71):**
- p2p.py:301-308: голые int(os.getenv("MIN_ORDERS", 100)), float(os.getenv("MIN_RATE", 95)), float(os.getenv("MAX_DEV", 4)), int(...INTERVAL/ALT_INTERVAL/BC_REFRESH), float(os.getenv("PAY_FEE", 0)); стр. 309-310: _fees(...) для SPOT_FEES/RISK_BUFFER; стр. 294-296: TRANSFER_FEES и легаси float(os.getenv("TRANSFER_FEE")); Config.from_env занимает стр. 293-323
- p2p.py:115-121: `_fees` берёт только части с ':' (остальные молча теряет), float(v) без проверки знака и конечности; python -c: _fees('BTC:0,0002,ETH:0.001') -> {'BTC': 0.0, 'ETH': 0.001}, _fees('USDT:-1,BTC:nan,ETH:inf') -> все три без ошибки, _fees('BTC:abc') -> ValueError
- p2p.py:1888, 2255, 2275, 2294: `abs(a.price / r - 1) * 100 > cfg.max_dev`; python -c: сравнение с nan -> False (фильтр выключен), с 0 -> True (режет всё)
- p2p.py:1025, 1043: `cfg.transfer_fees.get(asset, 0)` - отсутствующая монета = комиссия 0, поэтому отброшенный ключ надо откатывать к дефолту, а не удалять
- p2p.py:159-171 `_env_parsed`, 150-152 parse_min_profit, 213-227 parse_offline_min/parse_seconds - подход толерантных парсеров уже принят; regex `[\d.,]+` в parse_amount/parse_min_profit отсекает nan/inf/минус
- Общий `_fees` держит защищённый код: simperp.py:300-302 (risk_buffers), scripts/trading_gates_stats.py:752-755 (`except ValueError` -> «RISK_BUFFER не разобрать») и :469-495 (buffer_nonsense отказывает на -0.5/nan/inf, которые `_fees` возвращает как есть), tests/test_trading_gates_stats.py:745 и :985-1010, trading/hedge.py:384, calibration.py:224/485/544, replay.py:48-52
- tests/test_env_documented.py:27-118: AST-проверка обёрток над os.getenv - все 9 имён (MIN_ORDERS, MIN_RATE, MAX_DEV, INTERVAL, ALT_INTERVAL, BC_REFRESH, PAY_FEE, RISK_PENALTY, TRANSFER_FEE) уже есть в .env.example (стр. 24-28, 58-59, 70, 73, 80, 88)
- tests/test_amount.py:55-77 и tests/test_merchant_offline.py:106 - образец теста Config.from_env + caplog; тестов на мусор в перечисленных полях нет (grep tests/ по MAX_DEV/from_env)
- scripts/guard.py: PROTECTED/PROTECTED_NAMES/PROTECTED_BASENAMES не задевают p2p.py, ROADMAP.md, tests/test_config_env_sanity.py; PAYOUT_CODE и TRADING_CODE на синтетических строках нового кода не срабатывают (проверено python -c)

**Пересечения с другими задачами и ветками:** Ни одна из 50 удалённых веток не правит Config.from_env/_fees/PAY_FEE (git diff origin/main...origin/<ветка> -- p2p.py); docs-env - только .env.example/README/AST-тест, уже в main; stage2-speed/stage2-freshness/stage2-depth-realism добавили venue_timeout/fresh_window/depth_settings, они уже влиты и остаются нетронутыми. Очередь worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports/digest, cross-coin part 2, hedge/gates/trading) не пересекается. Единственный вероятный конфликт - верхняя строка Журнала в ROADMAP.md (её дописывают все воркеры): при конфликте сохранить обе записи. Возможный будущий отдельный шаг (не в этой задаче): такая же защита в calibration.py:224/485/544 и bot.py (COOLDOWN, MAX_SIGNALS, LIVE_SCANS читаются голым int/float), но это другие файлы и другая задача.

---
### Задача 5. `rapira-ref-sanity` — Санити ориентира Rapira USDT/RUB: битая котировка не должна слепить фильтр MAX_DEV

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/rapira-ref-sanity`

**Зачем:** Ориентир для отсева аномалий и ловушек (MAX_DEV) — Rapira USDT/RUB × спот. rapira_mid возвращает (ask + bid) / 2 без единой проверки. Если Rapira отдаст bid=0 при ask>0 (пустая сторона стакана), ориентир упадёт вдвое, и MAX_DEV отсеет ВСЕ объявления: сканер слепнет молча. Если в ответе NaN, `if ref and mid` в _refs истинно, а сравнение с NaN всегда ложно, поэтому отсев не работает вообще и ловушки проходят. Завышенный или устаревший курс делает то же самое. Запасной ориентир (медиана P2P USDT, REF_MEDIAN) уже есть: assemble включает его при ref=None. Его надо включать не только при исключении, но и при недоверии к котировке.

**Что сделать:** 1) p2p.py, сразу после REF_MEDIAN (строка ~701): три константы БЕЗ env: REF_MAX_SPREAD_PCT = 5.0 (спред Rapira ask/bid шире — котировка битая), REF_SANITY_PCT = 10.0 (ориентир дальше от медианы P2P USDT — не верим), REF_MIN_ADS = 5 (меньше объявлений первой страницы медиана для сверки не годится). Две чистые функции. (a) rapira_quote_mid(bid, ask) -> float: приводит к float (TypeError/ValueError -> ValueError), требует math.isfinite у обоих, bid > 0, ask > 0, bid <= ask и (ask / bid - 1) * 100 <= REF_MAX_SPREAD_PCT; иначе raise ValueError("Rapira: bad quote"); возвращает (bid + ask) / 2. (b) ref_vs_median(ref, prices, limit=REF_SANITY_PCT) -> (ok, reason): ref не конечное число или <= 0 -> (False, причина) независимо от числа объявлений; len(prices) < REF_MIN_ADS -> (True, "") (доказательств нет, доверие к Rapira сохраняется); иначе m = statistics.median(prices), dev = abs(ref / m - 1) * 100, dev > limit -> (False, короткая русская причина вида «ориентир Rapira 176.30 ₽ расходится с медианой P2P 87.40 ₽ на 102%», не длиннее ~120 символов), иначе (True, ""). 2) rapira_mid (p2p.py:704-707): после выбора r = next(...) вернуть rapira_quote_mid(r["bidPrice"], r["askPrice"]). Сигнатура, URL и тип возврата (float) не меняются: test_public_json.py и подмены p2p.rapira_mid в тестах остаются рабочими. Исключение по-прежнему ловит `except Exception: pass` в collect, дальше ветка ref is None. 3) collect (p2p.py ~2176-2196), только эти hunks: после получения spot и `first = [...]` вынести `usdt = [a.price for a in first if a.asset == "USDT"]` ДО блока `if ref is None:`; перед ним, если ref is not None: `ok, why = ref_vs_median(ref, usdt, max(REF_SANITY_PCT, cfg.max_dev))`; при not ok: `ref, ref_src = None, "-"` (ровно как при недоступной Rapira: assemble сам поставит ref_src=REF_MEDIAN и медиану по всем объявлениям) и `errors["rapira"] = why`. Существующий блок `if ref is None: ref_est = median(usdt) if usdt else None / else: ref_est = ref` оставить, он использует тот же usdt. 4) Проверено при разборе: Bot.check_venues (bot.py:3133) строит failed по всем ключам snap.errors, но цикл идёт по cfg.exchanges (ALL_EXCHANGES без rapira), поэтому ключ 'rapira' площадкой не считается; /status (bot.py:2828-2831, 2867-2869) просто покажет запись. bot.py, replay.py, snapshots.py, assemble, _refs не менять: replay.rebuild уже читает сохранённые ref/ref_src и errors из снимка. Поведение при исключениях и таймаутах Rapira не менять (errors не пополняются, ошибка остаётся в snap.jobs через _timed). 5) tests/test_ref_sanity.py (тесты офлайн; from helpers import arun, make_ad; фикстура offline из conftest; asyncio.run НЕ использовать; подмену недоступной Rapira из test_replay.py не импортировать, а повторить локально в 3 строки). 6) ROADMAP.md: строка в «Журнал» (2026-09-29 или дата запуска) одной-двумя фразами: санитария ориентира Rapira, порог, запасной ориентир, тесты. Очередь пуста — отмечать `[x]` нечего.

**Править:** `p2p.py`, `ROADMAP.md`
**Создать:** `tests/test_ref_sanity.py`

**Тесты (офлайн):**
- rapira_quote_mid: bid=0, ask=0, bid<0, bid>ask, nan, inf, строки-мусор ('x'), None, спред 6% -> ValueError с текстом 'Rapira: bad quote'; 88.1/88.2 -> 88.15; спред ровно на границе 5% проходит
- ref_vs_median: ref=nan/0/-1 -> ok=False даже без объявлений; меньше REF_MIN_ADS объявлений (в т.ч. пустой список) и ref=176 -> ok=True; 5+ объявлений около 87 и ref 88.15 -> ok=True; те же объявления и ref=176.3 (x2) -> ok=False с причиной, содержащей оба числа; ref=44 (вдвое ниже) -> ok=False; отклонение чуть ниже порога -> ok=True
- rapira_mid при bidPrice=0 в ответе (подмена p2p._json: обёртка над offline, для url с 'rapira.net' отдаёт {'data': [{'symbol': 'USDT/RUB', 'askPrice': 88.2, 'bidPrice': 0}]}) поднимает ValueError; с фикстурой rapira.json возвращает 88.15
- интеграция scan (offline, exchanges bybit,htx,kucoin,mexc,bitpapa, assets USDT): Rapira bid=0 -> snap.ref_src == p2p.REF_MEDIAN, snap.best не пуст и множество ключей snap.best совпадает со сканом при недоступной Rapira (локальный аналог _rapira_down); без правки все объявления были бы отсеяны
- интеграция scan: подмена p2p.rapira_mid на async-функцию, возвращающую 176.3 (x2 к фикстуре) при нормальной P2P-медиане -> snap.ref_src == p2p.REF_MEDIAN, 'rapira' in snap.errors, snap.best не пуст; ориентир снимка близок к медиане (не 176)
- интеграция scan: Rapira подменена на 176.3, но у fake-фетчера (monkeypatch.setitem(p2p.FETCHERS, 'fake', ...), exchanges=['fake']) 2 объявления (< REF_MIN_ADS) -> доверие к Rapira: snap.ref_src == 'Rapira USDT/RUB', 'rapira' not in snap.errors
- интеграция scan: фетчер отдаёт пустой список USDT (медианы нет) -> ориентир Rapira сохранён, ошибок 'rapira' нет
- интеграция scan на чистой фикстуре: snap.ref == pytest.approx(88.15), snap.ref_src == 'Rapira USDT/RUB', 'rapira' not in snap.errors
- ветка cfg.max_dev: при max_dev=15 порог сверки max(10, 15)=15: ориентир, расходящийся с медианой на 12%, доверия не теряет (ref_vs_median вызван с limit=15 -> ok)
- полный python -m pytest -q и python scripts/guard.py зелёные, существующие тесты без правок

**Критерии приёмки:**
- Rapira bid=0, crossed (bid>ask), NaN или спред >5% не приводит к отсеву всех объявлений: scan включает REF_MEDIAN (snap.ref_src == p2p.REF_MEDIAN, snap.best не пуст)
- Ориентир Rapira, расходящийся с медианой первой страницы P2P USDT больше max(REF_SANITY_PCT, cfg.max_dev) при >= REF_MIN_ADS объявлений, заменяется на REF_MEDIAN, а в snap.errors['rapira'] лежит причина с обоими числами
- При < REF_MIN_ADS объявлений USDT или без них доверие к Rapira сохраняется (поведение как до правки)
- Нормальная котировка Rapira (фикстура 88.1/88.2) работает как раньше: ref 88.15, ref_src 'Rapira USDT/RUB', snap.errors без 'rapira'; существующие тесты проходят без правок
- python -m pytest -q и python scripts/guard.py зелёные; в diff нет новых env-переменных, доменов, зависимостей, .post(, subprocess, eval/exec; изменены только p2p.py (rapira_mid, две функции и константы, hunk в collect), ROADMAP.md и новый tests/test_ref_sanity.py

**Не делать:**
- Не менять список доменов, URL и JSON_ALLOWED для Rapira; rapira_mid остаётся async-функцией (s) -> float
- Не менять _refs, assemble, порог MAX_DEV, replay.py, snapshots.py, bot.py
- Не добавлять env-переменные и не трогать .env.example; пороги — константы модуля
- Не менять поведение collect при исключении или таймауте Rapira (errors не пополнять), только новый случай расхождения с медианой
- Не трогать trading/, payouts.py, launcher.py, scripts/guard.py, CLAUDE.md, .github/, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, *.bat, tests/test_trading_surface.py
- Не писать слова payout, trading, TRADING, trd_ (в любом регистре для payout) в коде, тестах, комментариях и именах; не использовать pay_ok/pay_to/pay_no/pay_hist/pay_stop; не добавлять .post(, subprocess, os.system, eval(, exec(, captcha
- Не использовать asyncio.run в тестах: только arun из tests/helpers.py; не импортировать функции из других тестовых модулей
- Не запускать реальную сеть; в тестах подменять p2p._json (обёртка над offline) или p2p.rapira_mid
- Не переименовывать ref_src и не ставить REF_MEDIAN в collect: при недоверии ref, ref_src = None, "-" — REF_MEDIAN ставит assemble

**Где смотреть в коде (проверено на origin/main ad65d71):**
- p2p.py:701 REF_MEDIAN = "медиана P2P"; p2p.py:704-707 rapira_mid: `return (r["askPrice"] + r["bidPrice"]) / 2` без проверок
- p2p.py:2176-2181: `ref, ref_src = None, "-"`; `ref, ref_src = await ref_task, "Rapira USDT/RUB"` в try/except Exception: pass, без сверки с P2P
- p2p.py:2190-2196: usdt-медиана первой страницы (ref_est) считается только при ref is None; p2p.py:2245-2247 (assemble) при ref is None сам ставит медиану и REF_MEDIAN
- p2p.py:1871-1882 (_refs): `if ref and mid` — NaN истинно, 0.0 ложно (fallback к медиане монеты); p2p.py:1888, 2255, 2275, 2294: `abs(a.price / r - 1) * 100 > cfg.max_dev` — при r вдвое ниже отсекается всё, при NaN не отсекается ничего
- p2p.py:1758-1769 (_timed): ошибка rapira_mid попадает в rec['err'] (snap.jobs), в snap.errors — нет
- bot.py:3133 (check_venues: failed по всем ключам, цикл по cfg.exchanges), bot.py:1477, 2828-2831, 2867-2869 — ключ 'rapira' безопасен; p2p.py:44 ALL_EXCHANGES без rapira
- replay.py:125-131: rebuild читает scan['ref'], scan['ref_src'], scan['errors'] из снимка — путь совместим
- tests/test_replay.py:93-96 _rapira_down (образец подмены p2p.rapira_mid); tests/conftest.py:248 ROUTES rapira.net -> rapira.json; tests/fixtures/rapira.json: USDT/RUB ask 88.2, bid 88.1; tests/test_public_json.py:64 вызывает p2p.rapira_mid(s); tests/helpers.py:39 arun
- scripts/guard.py: PROTECTED/PROTECTED_NAMES/PAYOUT_CODE/TRADING_CODE/FORBIDDEN прочитаны; guard.protected() == False для p2p.py, ROADMAP.md, tests/test_ref_sanity.py; синтетические строки нового кода флагов не дают; tests/test_trading_surface.py:238 пин строк TRADING вне trading/ не затрагивается
- git ls-remote/branch -r: 50 веток, ни одной по теме санитарии Rapira; cloud/stage2-speed и stage2-depth-realism (влиты squash) уже в main; ROADMAP Журнал и origin/main log без упоминаний санитарии ориентира

**Пересечения с другими задачами и ветками:** Открытых пересечений нет. Ветки cloud/stage2-speed (recent(...) вокруг ref_task, p2p.py:2109) и cloud/stage2-depth-realism (ref_est/_refs) уже влиты в main, это видно по коду на ad65d71; cloud/stage2-freshness блок ref/rapira не трогает. Пересечения с очередью worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, digest, cross-coin part 2, hedge/gates) нет. Идея «курс ЦБ как второй ориентир» (ROADMAP:657) отдельная: новый домен и только справочная строка. Перед стартом сверить на свежем origin/main, что блок collect 2176-2196 не изменился: `git log -3 --format=%h -- p2p.py` и `git grep -n "ref_est" p2p.py`.

---
Когда все задачи волны сделаны или помечены «ждёт владельца», напиши итог: список веток, что влито, что ждёт владельца. Следующую волну не начинай — её вставит владелец.
