# Рабочая сессия 2 — репозиторий Sonti22/p2p-bot, Волна 3: бэкапы, воронка сигналов, разбор BestChange, троттлинг лога

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

## Задачи Волна 3: бэкапы, воронка сигналов, разбор BestChange, троттлинг лога

**Почему такой состав волны:** backup-integrity-check и signal-trade-funnel (ценность 4) идут вместе с тремя быстрыми S-задачами в изолированных файлах или участках: _bc_parse в p2p.py, netstatus.py (нет пересечений с другими), logthrottle.py + setup_logging (после log-redact-secrets из волны 1). bc-dump после adapters-per-ad-tolerance: может переиспользовать счётчик ADS_SKIPPED. Участки bot.py разные: run_backup и блок /stats.

**Условие старта:** все задачи волн 1–2 уже влиты в main (проверь `git log origin/main`). Если нет — остановись и напиши владельцу, что ждёшь.

### Задача 1. `backup-integrity-check` — Суточная копия баз: проверка целостности, испорченное не затирает исправные копии

**Ценность:** 4/5 · **Размер:** M · **Ветка:** `claude/backup-integrity-check`

**Зачем:** backup.run копирует базы через sqlite3 backup API без проверки: испорченная база (диск, аварийное выключение ПК) молча попадает в свежую копию, а ротация удаляет самые старые копии сверх BACKUP_KEEP (по умолчанию 7) — через неделю целых копий не остаётся. Сбой копии виден только строкой в логе (bot.py:2758), владелец не узнаёт. Идея «Проверка целостности баз раз в сутки» записана в ROADMAP (раздел Идеи, строки 698-702) и нигде не реализована (проверено по коду main и всем веткам origin/*). Проверка именно КОПИИ нужна: база с испорченной страницей проходит s.backup без ошибки, а quick_check копии бросает DatabaseError 'database disk image is malformed'.

**Что сделать:** Общее: все новые строки backup.py и bot.py (код, комментарии, docstring) НЕ содержат подстрок 'trading.', 'TRADING', 'trd_' и слова 'payout' (пин tests/test_trading_surface.py:182-201 сравнивает строки backup.py по содержимому; строки FILES backup.py:19-21 оставить байт-в-байт). Базы называть в комментариях обобщённо ('базы из FILES').

1) backup.py (добавить `import json`):
- `_check_db(path) -> None | str`: `con = sqlite3.connect(path)`; в try: `rows = con.execute("PRAGMA quick_check").fetchall()`; `except sqlite3.OperationalError: raise` (диск полон / I/O — это сбой копии, а не порча базы; OperationalError — подкласс DatabaseError, поэтому его надо перехватить ПЕРВЫМ); `except sqlite3.DatabaseError as e: return str(e)[:200]` (на странично испорченной базе quick_check бросает, а не возвращает строки); `finally: con.close()`. Ответ `[('ok',)]` -> None, иначе '; '.join(первые строки)[:200]. После закрытия соединения убрать остатки `path+'-wal'`, `path+'-shm'`, `path+'-journal'` (os.remove в try/except OSError): копия WAL-базы сохраняет режим WAL и проверка создаёт их рядом.
- `_check_json(path) -> None | str`: json.load(open(path, encoding='utf-8')); `except ValueError` (сюда входят JSONDecodeError и UnicodeDecodeError) -> 'битый JSON'; не dict (favorites/presets — словари, см. jsonstore.read_dict) -> 'не словарь'. OSError не ловить.
- В `run()` цикл по FILES: для .db — `s, d = connect(src), connect(out)`; try: `s.backup(d)`; `except sqlite3.OperationalError: raise`; `except sqlite3.DatabaseError as e: why = str(e)[:200]`; finally: d.close(); s.close() (оба закрыть ДО любой проверки/удаления — Windows). Если why нет — `why = _check_db(out)`. Для .json — `shutil.copy2`, затем `_check_json(out)`. Если why: файл `out` удалить из tmp (если он есть), `bad.append((fname, why))`, `continue` (в `done` он не попадает; остальные файлы копируются как раньше). Иначе `done.append(fname)`.
- Сразу после успешного `os.replace(tmp, dest)`: `_state['bad'] = bad` (новый список, при отсутствии плохих — пустой; при исключении посередине прошлое значение не трогать). Читать везде через `_state.get('bad', [])`: tests/test_db_backup.py:_fresh подменяет `_state` на {'tried': 0.0}.
- Ротация: `keepset = {g for g in (last_good(f, data_dir) for f, _ in bad) if g}`; `for old in copies(data_dir)[:-n]: if old in keepset: continue; shutil.rmtree(...)`. Защита снимается сама, когда bad пуст. Число лишних копий не больше числа плохих файлов.
- Публичные хелперы: `bad() -> list[(имя, текст)]` (копия списка) и `last_good(fname, data_dir=DATA_DIR) -> имя папки | None` — самая новая папка из copies(data_dir), где есть os.path.isfile(<data_dir>/backup/<папка>/<fname>). Сигнатуру `run(now=None, data_dir=DATA_DIR) -> (dest, done)`, due()/keep()/RETRY/_NAME не менять. В docstring модуля добавить 1-2 предложения про проверку.

2) bot.py: в `__init__` рядом с `self.backup_task = None` (bot.py:1537) добавить `self.backup_bad_alerted = set()  # файлы, о порче которых владельцу уже написали`. В `run_backup` (bot.py:2752) ПОСЛЕ существующего try/except (тот остаётся как есть) и только если `dest` — вызвать новый `async def backup_alert(self)` в собственном `try/except Exception as e: logger.warning('backup alert: %s', e)` (сбой отправки run_backup не роняет). `backup_alert`: `bad = dict(backup.bad())`; для КАЖДОГО плохого файла на каждом прогоне `logger.warning('backup: %s не прошёл проверку целостности: %s', имя, текст)`; `if not self.chat_id: return` (состояние не менять); `new = sorted(set(bad) - self.backup_bad_alerted)`, `fixed = sorted(self.backup_bad_alerted - set(bad))`. Если new — ОДНО сообщение в `topic='dev'` со строкой на файл: «⚠️ Резервная копия: <имя> не прошёл проверку целостности (<текст>) и в новую копию не попал. Последняя копия этого файла: <backup.last_good(имя) или «нет»>. Прошлые копии сохранены.»; имя и текст пропускать через `html.escape` (send шлёт parse_mode=HTML); только если `(r or {}).get('ok')` — `self.backup_bad_alerted |= set(new)` (как watchdog_check, bot.py:2718-2726). Если fixed — одно сообщение «✅ <имя> снова проходит проверку целостности и попал в копию.»; при ok — `self.backup_bad_alerted -= set(fixed)`. Повторный прогон с тем же bad ничего не шлёт (в лог — шлёт).

3) ROADMAP.md: в разделе «Идеи» к пункту «Проверка целостности баз раз в сутки» дописать «(сделано 2026-09-29: backup._check_db, backup.bad/last_good, сообщение в «Разработку»)» по образцу строки 600 (чекбоксов у идей нет); строку в НАЧАЛО «Журнала» (новые записи там сверху): что проверяется (quick_check копии, JSON — словарь), что плохой файл не попадает в копию, что ротация сохраняет последнюю копию плохого файла, одно сообщение о порче и одно о восстановлении. docs/ и .env.example не трогать.

**Править:** `backup.py`, `bot.py`, `ROADMAP.md`
**Создать:** `tests/test_backup_integrity.py`

**Тесты (офлайн):**
- tests/test_backup_integrity.py по образцу tests/test_db_backup.py (`from helpers import arun`, `_data`-помощник, `_fresh` = monkeypatch.setattr(backup, '_state', {'tried': 0.0}) в КАЖДОМ тесте, чтобы не загрязнять глобальный _state; для бота — `from test_bot import Stub, texts` как в tests/test_scan_watchdog.py).
- Битая шапка: trades.db = b'not a database' * 200 -> run пропускает файл, presets.json копируется, done == ['presets.json'], в папке копии нет trades.db, backup.bad() == [('trades.db', <текст с 'not a database'>)]; исправная база -> bad() == [].
- Испорченная страница при целой шапке: таблица из ~2000 строк по 100 символов, затем `open(p,'r+b')`: seek(4096*3), write(b'\xff'*4096) -> run не падает, файл не попал в копию, bad() содержит его (s.backup здесь проходит, ловит именно quick_check копии). Битый JSON (presets.json = '{oops') и JSON-список '[1]' -> не копируются, попадают в bad().
- Не-Database сбой не маскируется под порчу: monkeypatch backup.sqlite3.connect -> OSError (как в tests/test_db_backup.py:119) и подмена _check_db/backup на исключение sqlite3.OperationalError('database or disk is full') -> run пробрасывает исключение, bad() не меняется, tmp-папка убрана.
- Ротация: BACKUP_KEEP=2, 4 суточных прогона (NOW + day*86400), на 3-м trades.db портится, на 4-м — портится ещё: папка последней копии с целой trades.db (прогон 2) переживает ротацию, папки без неё удаляются по обычным правилам (итог: copies() = [прогон 2, прогон 3, прогон 4] минус лишнее ровно по правилу — проверить точный список); после починки на следующем прогоне защита снимается и старая копия удаляется. last_good() возвращает имя папки и None, если файла нигде нет.
- WAL-база (paper.db в режиме WAL с живым писателем по образцу tests/test_db_backup.py:61, но с именем файла из FILES без слов trading/payout): копируется, проходит проверку, bad() пуст, в папке копии ровно файлы из FILES без -wal/-shm.
- run_backup в боте (Stub, monkeypatch backup.run -> ('/x/20260928-0930', ['presets.json']), backup.bad -> [('trades.db', 'file is not a database <x>')], backup.last_good -> '20260927-0930'): ровно одно sendMessage, в топик dev (thread), с 'trades.db', '20260927-0930' и экранированным '&lt;x&gt;'; повторный прогон с тем же bad не дублирует; bad -> [] даёт одно сообщение '✅ trades.db снова проходит'; следующий прогон — тишина; send с {'ok': False} не фиксирует флаг (повтор при следующем прогоне) и не роняет run_backup; исключение из self.send не роняет run_backup (в caplog 'backup alert:').
- Существующие tests/test_db_backup.py зелёные без правок; python -m pytest -q и python scripts/guard.py зелёные, tests/test_trading_surface.py (пин TRADING_LINES_APPROVED) зелёный.

**Критерии приёмки:**
- Испорченная база (битая шапка или испорченная страница) и битый JSON никогда не попадают в новую копию; остальные файлы копируются; backup.bad() возвращает [(имя, текст)], а при исправных файлах и после починки — [].
- Последняя копия, где лежит плохой файл, переживает ротацию (число лишних копий <= числа плохих файлов); после починки лишняя копия удаляется на следующем прогоне.
- OperationalError (диск полон, I/O) и OSError не превращаются в 'база повреждена': run по-прежнему падает целиком, RETRY/due() работают как раньше.
- Владелец получает ровно одно сообщение о порче (топик dev, html.escape, с последней копией файла или 'нет') и ровно одно о восстановлении; при неудачной отправке флаг не меняется; в логе на каждый прогон — warning с именем файла.
- backup.run(now, data_dir) -> (dest, done), due(), keep(), RETRY, _NAME и строки FILES backup.py:19-21 не изменены (diff по backup.py не трогает строки со словом trading); в папке копии нет -wal/-shm.
- python -m pytest -q и python scripts/guard.py зелёные; в ROADMAP: пометка '(сделано 2026-09-29: ...)' у идеи и новая строка в начале Журнала.

**Не делать:**
- Не трогать launcher.py, .github/, CLAUDE.md, scripts/guard.py, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, .env*, *.bat, trading/, tests/test_trading_surface.py и любые пути со словами payout/trading; не менять tests/test_db_backup.py.
- Не добавлять в backup.py, bot.py (в т.ч. в комментарии и docstring) подстроки 'trading.', 'TRADING', 'trd_' и слово payout, и не править строки FILES backup.py:19-21 (их содержимое запинено в tests/test_trading_surface.py:198-201 и ловится TRADING_CODE guard); слово payout не использовать и в tests/test_backup_integrity.py (PAYOUT_CODE считает строки тестов). В тестах называть файлы из FILES, кроме журнала ядра, например paper.db.
- Не ловить sqlite3.DatabaseError без предшествующего `except sqlite3.OperationalError: raise` (OperationalError — подкласс, иначе диск полон/I/O станут 'порчей базы' и файл выпадет из копии).
- Не проверять и не открывать живые базы ради quick_check — только копию во временной папке; не запускать VACUUM/REINDEX/.recover и не восстанавливать базы автоматически — только сообщение владельцу; не менять поведение s.backup (таймауты, pages, progress).
- Не добавлять keys.json и снимки в FILES; не читать содержимое ключей; не добавлять новые переменные окружения и не править .env.example (tests/test_env_documented.py); не менять docs/.
- Не менять формат имён папок копий (_NAME), сигнатуру run(), правила due()/keep()/RETRY.
- Не использовать asyncio.run в тестах — только arun из helpers (сейчас идёт аудит tests-arun-audit); не писать тесты, читающие/пишущие в data/ бота — только tmp_path; каждый тест подменяет backup._state через monkeypatch.
- Не добавлять .post(, сетевые вызовы, subprocess/os.system, новые домены и зависимости.

**Где смотреть в коде (проверено на origin/main ad65d71):**
- backup.py:19-21 — FILES (trades/blacklist/alerts/history/paper/hedge_circles/trading .db + favorites/presets .json); строка 21 с trading.db запинена в tests/test_trading_surface.py:198-201
- backup.py:81-95 — цикл копирования: sqlite3.connect(src)/connect(out), s.backup(d), shutil.copy2 для JSON, done.append без проверки результата
- backup.py:101-102 — ротация: for old in copies(data_dir)[:-n]: rmtree, без учёта здоровья копий
- bot.py:2740-2758 — schedule_backup/run_backup: исход только logger.info/logger.warning (2756/2758), владельцу ничего
- bot.py:1632-1647 — send(): parse_mode=HTML, возвращает {'ok': False} без исключения; bot.py:2718-2726 (watchdog_check) — образец 'флаг меняем только при r["ok"]'; bot.py:3143 — образец self.send(..., topic="dev")
- tests/test_db_backup.py:_fresh (строки 27-29) подменяет backup._state на {'tried': 0.0}; :61 WAL-тест, :86 integrity_check копии; :119 подмена sqlite3.connect на OSError; :140 тест бота с монкипатчем backup.run
- jsonstore.read_dict (jsonstore.py:14-32) и favorites.py/presets.py: favorites.json и presets.json — словари, проверка isinstance(dict) не даёт ложных срабатываний
- grep quick_check|integrity_check: код — только tests/test_db_backup.py:86; ROADMAP.md:698-702 (идея открыта), :855 (в Журнале названа следующим шагом); git grep по всем origin/*: реализации нет
- Эксперимент sqlite (Python 3.10.11): 'not a database' -> DatabaseError при s.backup; испорченная страница -> s.backup OK, quick_check копии бросает DatabaseError 'database disk image is malformed'; копия WAL-базы открывается в WAL и на время проверки создаёт -wal/-shm; пустой файл -> quick_check 'ok'; s.backup на заблокированной источнике ждёт бесконечно (уже существующее поведение, не трогать)
- tests/conftest.py:_scan/_redirect (строки 340-395) — функции с data_dir=DATA_DIR по умолчанию перенаправляются автоматически; scripts/guard.py: protected(backup.py|bot.py|ROADMAP.md|tests/test_backup_integrity.py) -> False

**Пересечения с другими задачами и ветками:** cloud/s2-db-backup (#161) — влитая база backup.py; в поздних hedge-коммитах (c32c305, 376d020) менялась лишь строка FILES с журналом ядра, её не трогаем. cloud/reports2 и cloud/s2-roadmap-ideas только вносят текст идеи в ROADMAP/docs, кода нет; git grep quick_check|backup.bad по всем 50 веткам origin/* — пусто. Очередь worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports/digest, cross-coin part 2, hedge/gates/trading) backup.py и run_backup не касается; с tests-arun-audit совместимо, если новый тест использует arun. Задача data-health-status (если возьмут) читает только backup.copies() и не мешает. Возможен только текстовый конфликт в ROADMAP.md (Журнал сверху) и в bot.py рядом с run_backup/__init__ — правки маленькие.

---
### Задача 2. `signal-trade-funnel` — Воронка сигнал -> сделка в /stats

**Ценность:** 4/5 · **Размер:** M · **Ветка:** `claude/signal-trade-funnel`

**Зачем:** Владелец не видит, сколько отправленных сигналов заканчиваются реальной сделкой и как быстро. Воронка (сигналов, сделок, конверсия, медианный лаг, сделки вне сигнала, направление без ответа) показывает, какие сигналы шум и стоит ли ужесточать пороги. Только чтение двух БД, ничего не пишется и не меняет торговую логику.

**Что сделать:** 1) Новый модуль signal_funnel.py в корне (рядом с history.py и trades.py). Импорты: html, os, sqlite3, statistics, time, history, trades. Константы: DAYS_MAX=30 (хранение signals 30 дн.), WINDOW_S=3600, TOP_DIRECTIONS=5.
2) _select(path, sql, args): если not os.path.exists(path) -> []; иначе обычный sqlite3.connect(path), execute(...).fetchall(), close в finally; sqlite3.Error -> []. НЕ вызывать history._connect и trades._connect (они создают таблицы и файлы). Только SELECT.
3) funnel(days=7, window_s=WINDOW_S, hist_path=None, trades_path=None, now=None) -> dict. Пути раскрывать при вызове: hist_path = hist_path or history.DB_PATH, trades_path = trades_path or trades.DB_PATH. now = time.time() если None; days = min(max(int(days),1), DAYS_MAX); since = now - days*86400.
   Сигналы: SELECT buy_ex, buy_asset, sell_ex, sell_asset, signal_ts, last_seen FROM signals WHERE signalled = 1 AND signal_ts IS NOT NULL AND signal_ts >= ? AND signal_ts <= ? ORDER BY signal_ts, id.
   Сделки: SELECT buy_ex, buy_asset, sell_ex, sell_asset, ts FROM trades WHERE ts >= ? AND ts <= ? ORDER BY ts, id. Условия по kind НЕТ: считаются все строки trades за окно (kind это вид оплаты, не признак сделки).
   Сопоставление жадное один-к-одному по ключу направления: для каждой сделки (по возрастанию ts) среди ещё не занятых сигналов того же ключа с sig_ts <= ts <= max(last_seen, sig_ts) + window_s берётся сигнал с самым поздним sig_ts; он помечается занятым; лаг = ts - sig_ts (от первого сигнала эпизода, нижняя граница 0, а не -60). Сделка без пары это off_signal.
   Возврат: {days, signals, traded, conversion (traded/signals или None если сигналов 0), median_lag_s (statistics.median или None), off_signal, by_direction: top TOP_DIRECTIONS по числу сигналов [{key, signals, traded, conversion}] сортировка (-signals, key)}.
4) lines(data) -> list[str]: если signals == 0 вернуть []. Иначе строки: пустая, заголовок '<b>Сигналы → сделки за N дн.</b>', итог 'X сигналов, Y сделок (Z%), медиана до сделки M мин; вне сигнала: K' (медиану добавлять только если она не None; минуты = max(1, round(lag/60))), и при наличии направления с traded == 0 строка 'чаще всего без ответа: A → B (n сигналов, 0 сделок)'. Все названия биржи/актива пропускать через html.escape (stats_view отвечает в HTML parse mode).
5) bot.py: добавить import signal_funnel в блок импортов по алфавиту (рядом с simperp/snapshots). В stats_view, перед строкой-подсказкой 'Отмечай связку...', добавить блок не более 6 строк: try: lines += signal_funnel.lines(signal_funnel.funnel()); except Exception as e: logger.warning('signal funnel: %s', e). Переменная называется lines (не out). Сигнатуру stats_view и остальной код не менять.
6) ROADMAP.md: отметить пункт по протоколу CLAUDE.md и добавить строку в Журнал (2 строки).
7) Тесты tests/test_signal_funnel.py: только tmp_path, сеть и живые data/ не нужны. Сигналы создавать через history.track_signals(..., path=tmp), сделки через trades.log_trade(..., path=tmp, ts=...). Время now брать реалистичным (около 1_790_000_000), НЕ 1_000_000: trades._month_start падает на Windows на датах 1970 г. Для образца вызова stats_view смотреть tests/test_bot.py около 1918-1943 и tests/helpers.py (make_ad).
Прогнать pytest -q и python scripts/guard.py (ожидается 'guard: ок').

**Править:** `bot.py`, `ROADMAP.md`
**Создать:** `signal_funnel.py`, `tests/test_signal_funnel.py`

**Тесты (офлайн):**
- сделка через 5 минут после сигнала того же направления: traded=1, median_lag_s=300, conversion=1.0, off_signal=0
- сделка через 2 часа после сигнала (окно 1 ч): off_signal=1, traded=0, сигнал без ответа
- 2 сделки и 1 сигнал того же направления: одна сопоставлена, одна off_signal, conversion не больше 1.0
- сделка другого направления не сопоставляется с сигналом
- нет сигналов: conversion=None, lines(...) == []
- есть сигналы, нет сделок: traded=0, conversion=0.0, в lines есть строка 'без ответа'
- окно days режет старые сигналы и сделки; days зажимается в 1..DAYS_MAX
- длинный эпизод: сделка после signal_ts, но до last_seen+window_s, сопоставляется (лаг от первого сигнала)
- сделка раньше сигнала не сопоставляется (нижняя граница 0)
- нет файлов БД: пустой результат и файлы НЕ созданы (os.listdir tmp пуст)
- названия с символами < > & экранируются в lines()
- регрессия stats_view: monkeypatch signal_funnel.funnel бросает Exception, /stats всё равно отвечает без блока воронки
- stats_view при наличии сигналов (monkeypatch signal_funnel.funnel возвращает фиксированный dict) содержит заголовок воронки
- python -m pytest tests/test_trading_surface.py зелёный на новом модуле

**Критерии приёмки:**
- /stats показывает блок воронки только когда за окно есть отправленные сигналы; иначе вывод /stats не меняется
- сбой воронки (исключение) не ломает /stats, пишется logger.warning
- pytest -q зелёный целиком, python scripts/guard.py печатает 'guard: ок' (код выхода 0)
- python -m pytest tests/test_trading_surface.py зелёный
- diff PR не более 300 строк, изменения bot.py не более 10 строк
- БД только читаются (SELECT), файлы БД не создаются и не изменяются
- ROADMAP.md: пункт отмечен, строка в Журнале добавлена

**Не делать:**
- не использовать в добавленных строках слова payout, TRADING, trd_, 'trading.', ссылки http(s)://
- не вызывать history._connect и trades._connect, не писать INSERT/UPDATE/DELETE/CREATE в БД
- не называть переменные s/sess/session/http/client/opener и не вызывать у них .get/.open
- не использовать в именах url/uri/endpoint/base; не вызывать .send/.put/.delete/.request/.post
- не менять mark_done, log_trade, track_signals, схемы таблиц, GUEST_*, _owner_gate, is_guest
- не менять сигнатуру stats_view и остальное тело функции кроме вставки блока и импорта
- не трогать защищённые файлы: CLAUDE.md, scripts/guard.py, tests/conftest.py, tests/test_trading_surface.py, payouts.py, launcher.py, pytest.ini, .github/
- не читать .env, data/keys.json, папку ключ и любые файлы с ключами; в тестах использовать только tmp_path
- не запускать bot.py, launcher.py и сетевые скрипты
- не ссылаться на несуществующий sigreport.py

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:2058-2088 stats_view копит lines и возвращает '\n'.join(lines) (переменной out нет)
- bot.py:4098-4099 диспетчер команды /stats
- bot.py:3156 Bot._deal_key: ключ (buy_ex, buy_asset, sell_ex, sell_asset), тот же порядок полей в signals и trades
- history.py: таблица signals (signalled, signal_ts, first_seen, last_seen), signal_ts ставится один раз за эпизод, эпизоды перезапускаются после рестарта, хранение 30 дн. (cleanup)
- trades.py:259 log_trade(..., path=, ts=) пригоден для тестов на tmp БД; trades.py:406 stats без фильтра kind
- trades.py:181 _month_start падает на Windows на дате 1970 г. (OSError) поэтому тестовое now должно быть реалистичным
- scripts/guard.py:55 PAYOUT_CODE, :62 TRADING_CODE, :71 FORBIDDEN, :14-32 PROTECTED*, :93 protected(); черновик модуля прошёл эти проверки
- tests/test_trading_surface.py: test_trading_lines_outside_trading_are_pinned и AST-проверки отправителей (s/sess/session/http/client/opener + .get/.open, .post/.put/.delete/.request/.send); _surface() на черновике пуст
- tests/conftest.py: автоизоляция путей по умолчанию, аудит-хук падает на обращении теста к живому data/, сеть заблокирована
- черновик signal_funnel.py прогнан в scratchpad на tmp БД: 5 мин -> median_lag_s=300.0, 2 ч -> off_signal=1, 2 сделки/1 сигнал -> traded=1, off_signal=1, нет БД -> пусто и файлы не созданы
- git ls-remote: 47 remote heads, ветки с воронкой нет; в ROADMAP.md Журнале воронки нет, в Идеях (строка ~751) есть только alerts-vs-trades

**Пересечения с другими задачами и ветками:** Задачи plan-fact-spread и trades-breakdown-hour-bank-coin тоже правят stats_view в bot.py: запускать последовательно, не параллельно. При конфликте в bot.py применить рецепт разрешения конфликтов из pipeline (сохранить обе вставки, блок воронки перед строкой-подсказкой). cloud/s2-stats-direction (by_direction) уже в main и не пересекается. Идея alerts-vs-trades из ROADMAP про другое (алерты, а не воронка по сигналам).

---
### Задача 3. `bc-dump-row-robustness` — BestChange: одна битая строка дампа не должна ронять или отравлять весь разбор info.zip

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/bc-dump-row-robustness`

**Зачем:** Разбор выгрузки BestChange строгий: .decode("cp1251") в трёх местах без errors=, а тело цикла по bm_rates.dat не защищено. Один непечатный байт в названии обменника (в cp1251 байт 0x98 не определён), пустой лимит, короткая строка, give=0/recv=0 у стороны sell (ZeroDivisionError) роняют всю выгрузку: _bc_refresh считает это падением площадки, включает бэкофф, и все BestChange-связки пропадают на минуты. Обратная сторона: nan/inf в курсе сейчас НЕ падают, а тихо попадают в выдачу как Ad с price=nan/inf, а buy с give=0 даёт Ad с price=0.0 (воспроизведено на p2p._bc_parse). Нужно и пропускать единичные битые строки, и не допустить, чтобы системная поломка формата (например десятичная запятая во всех строках) превратилась из видимой ошибки с бэкоффом в тихий пустой кэш. У LBank (p2p.py:521) защита per-item уже есть, у BestChange нет.

**Что сделать:** Всё в p2p.py, функция _bc_parse (сейчас стр. 566-597). Сигнатуру `_bc_parse(data) -> list[Ad]`, порядок объявлений, BC_URL, _bc_fetch, _bc_refresh, бэкофф, формулы price/avail/лимитов и порядок полей Ad НЕ менять (на cloud/stage2-speed правятся соседние участки и вызов asyncio.to_thread(_bc_parse, data), тело функции остаётся). `import math` в p2p.py уже есть.
1) Модульный словарь рядом с _bc: `_bc_stats = {"skipped": 0}` (комментарий по-русски: сколько строк ПОСЛЕДНЕЙ выгрузки пропущено как битые; значение присваивается при каждом разборе, а не накапливается).
2) Три `.decode("cp1251")` (bm_cy.dat, bm_exch.dat, строка bm_rates.dat) заменить на `.decode("cp1251", errors="replace")`.
3) bm_cy.dat: в цикле по строкам `if len(f) < 6: skipped += 1; continue` (f[2], f[4], f[5] читаются ниже для каждой строки - сегодня любая короткая строка роняет разбор). bm_exch.dat: `if len(f) < 2: skipped += 1; continue`. Для рабочего дампа поведение то же (короткие строки сегодня давали IndexError).
4) bm_rates.dat: всё тело цикла ПОСЛЕ отбора по `pairs` (начиная с `f = line.decode(...)`) обернуть в `try/except (ValueError, IndexError, KeyError, ZeroDivisionError)`, при ошибке `bad_rows += 1; skipped += 1; continue`. До расчёта Ad явно отбрасывать (тот же счётчик): `give <= 0`, `recv <= 0`, любое из give, recv, reserve, mn, mx не `math.isfinite`, а после расчёта `price <= 0` или не finite (это убирает Ad с price=nan/inf/0.0, которые сегодня проходят). Остальные формулы, включая `float(f[9]) or 1e12`, не трогать.
5) Предохранитель системной поломки (главное отличие от исходной версии задачи): после цикла, если `bad_rows > len(ads)` (битых строк bm_rates больше, чем разобранных объявлений - это смена формата, а не случайная строка), поднять `ValueError(f"BestChange: разобрано {len(ads)}, пропущено {bad_rows} строк - формат выгрузки изменился?")`. Тогда _bc_refresh, как и раньше, вызовет _venue_backoff_fail и оставит прежний кэш, а не запишет пустой список и не сбросит бэкофф. Счётчик и предупреждение (п.6) выполнить ДО этого raise.
6) Счётчик и лог: `prev = _bc_stats["skipped"]; _bc_stats["skipped"] = skipped; if skipped and skipped != prev: logger.warning("BestChange: пропущено %d строк выгрузки, разобрано %d", skipped, len(ads))` - без спама: одинаковое число на следующей выгрузке не логируется, ноль не логируется.
7) ROADMAP.md: одна запись в начале раздела «Журнал» (там новые сверху) с датой и итогом одной строкой; отметку [x] в «Очереди» ставить только если там окажется соответствующий пункт (добавлять новый пункт не нужно).

**Править:** `p2p.py`, `ROADMAP.md`
**Создать:** `tests/test_bc_parse_robust.py`

**Тесты (офлайн):**
- tests/test_bc_parse_robust.py, чисто синхронные тесты без asyncio.run/arun; свой сборщик zip в файле (zipfile+io, cp1251, как _bc_zip в tests/test_adapters.py, но без импорта из другого теста; bm_cy: USDT TRC20 + "Сбербанк RUB" по образцу test_adapters.py:109). Каждый тест сам делает `monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})` (conftest этот словарь не сбрасывает, а править conftest нельзя).
- test_undecodable_byte_in_dictionaries: байт 0x98 в названии обменника bm_exch.dat и в названии постороннего (не из BC_COINS/BC_BANKS) курса в bm_cy.dat: исключения нет, валидные объявления на месте, метка обменника в pays/nick не роняет разбор.
- test_bad_rates_rows_skipped: не меньше 8 валидных строк bm_rates (обе стороны) плюс битые: sell с give=0, sell с recv=0, buy с give=0 (раньше давал Ad с price 0.0), buy с recv=0, пустой лимит min, слишком короткая строка, нечисловое поле, nan, inf. Результат - ровно валидные Ad, у всех price finite и > 0; p2p._bc_stats["skipped"] равен числу битых строк.
- test_short_dictionary_rows_skipped: строка с 3 полями в bm_cy.dat и строка с 1 полем в bm_exch.dat пропущены, разбор не падает, счётчик учитывает обе.
- test_warning_once_per_change (caplog, уровень WARNING): повторный разбор того же дампа не даёт второго предупреждения; другое число битых строк даёт новое; чистый дамп предупреждения не даёт.
- test_systematic_break_raises: все строки bm_rates с десятичной запятой ("89,8") -> ValueError; при этом счётчик выставлен и предупреждение записано; дамп из нуля подходящих строк по-прежнему возвращает [] без исключения.
- test_clean_dump_unchanged: на дампе из tests/test_adapters.py::_bc_zip (скопировать данные строкой) результат тот же, что проверяет существующий test_bestchange_parse (он проходит без правок); счётчик 0.
- Полный `python -m pytest -q` и `python scripts/guard.py` зелёные.

**Критерии приёмки:**
- Битая строка bm_rates.dat (give=0, recv=0, пустое/нечисловое поле, короткая, nan, inf) пропускается, остальные объявления BestChange разбираются, порядок и значения полей Ad у них прежние
- Ad с price nan, inf или <= 0 больше не попадают в результат _bc_parse
- Недекодируемый байт в bm_cy.dat и bm_exch.dat не роняет разбор; короткая строка справочника пропускается
- Деление на ноль при give/recv = 0 исключено явной проверкой, а не поимкой исключения
- Если битых строк bm_rates больше, чем разобранных объявлений, _bc_parse поднимает ValueError (кэш _bc["ads"] и бэкофф остаются в режиме «сбой площадки», как до правки), а не возвращает пустой список
- _bc_stats["skipped"] отражает последнюю выгрузку; предупреждение в логе только при skipped > 0 и изменении числа
- Существующие тесты BestChange (test_adapters.py::test_bestchange_parse, test_bestchange_route.py, test_bc_reliability.py, test_speed.py, test_snapshots.py) проходят без правок; сигнатура и место _bc_parse не менялись
- Нет новых доменов, зависимостей, env-переменных, сетевых вызовов; в ROADMAP.md есть запись в Журнале

**Не делать:**
- Не менять BC_URL, _bc_download, _bc_fetch, _bc_refresh, _venue_backoff_* и логику бэкоффа (это территория bc-reliability*/stage2-speed)
- Не менять формулы price/avail/лимитов, `float(f[9]) or 1e12` и порядок полей в Ad; не превращать пропуск строки в подстановку значения по умолчанию
- Не оборачивать в try весь _bc_parse и не проглатывать BadZipFile/KeyError на отсутствующем файле архива: битый архив должен оставаться ошибкой выгрузки
- Не убирать raise при системной поломке (п.5): пустой кэш вместо ошибки недопустим
- Не хранить в репозитории реальные дампы info.zip; тесты - только in-memory zip (в tests/fixtures/ допустимы только .json)
- Не писать в .py-строки (в том числе в тестах и комментариях) URL с хостами вне ALLOWED_DOMAINS из scripts/guard.py; допустим только https://www.bestchange.ru/...
- Не трогать trading/, payouts.py, launcher.py, scripts/guard.py, CLAUDE.md, .github/, tests/conftest.py, pytest.ini, requirements.txt, data/, logs/, *.bat, .env*, tests/test_trading_surface.py
- Не писать слова payout, trading, order, position, withdraw, margin в коде, тестах и комментариях; не добавлять .post(, subprocess, eval(, exec(, getattr с динамическим именем
- Не использовать asyncio.run/arun в новых тестах (задача tests-arun-audit у другого воркера) и не добавлять тест на _bc_refresh
- Не добавлять env-переменные, новые зависимости и импорты (math уже импортирован)

**Где смотреть в коде (проверено на origin/main ad65d71):**
- p2p.py:566 def _bc_parse(data); :569 z.read("bm_cy.dat").decode("cp1251"); :573 z.read("bm_exch.dat").decode("cp1251"); :587 line.decode("cp1251").strip().split(";") - все без errors=
- p2p.py:588-596: float(f[3]), float(f[9]) or 1e12, int(bad or 0), price = recv / give, reserve / price, give / recv - без try, f[N] может выйти за границы
- p2p.py:571-577: coins/banks строятся по f[2], f[4], f[5] каждой строки bm_cy.dat - короткая строка сегодня роняет разбор
- p2p.py:651-660 _bc_refresh: `except Exception: _venue_backoff_fail("bestchange"); raise`, а при успехе `_bc["ads"] = ads` без проверки на пустоту - поэтому нужен предохранитель п.5
- Воспроизведено через p2p._bc_parse на синтетических zip: 0x98 в bm_exch -> UnicodeDecodeError; give=0/recv=0 sell -> ZeroDivisionError; пустой min -> ValueError; короткая строка и короткая строка bm_cy -> IndexError; '89,8' -> ValueError; nan и inf -> Ad с price nan/inf; buy give=0 -> Ad с price 0.0
- p2p.py:521 async def lbank + `try: ... except (KeyError, TypeError, ValueError): continue` и tests/test_lbank.py:27 - прецедент защиты per-item
- tests/test_adapters.py:106-131 _bc_zip и test_bestchange_parse; tests/conftest.py:277 сбрасывает _bc, но не _bc_stats (conftest защищён, тест сам подменяет)
- scripts/guard.py: protected() False для p2p.py, ROADMAP.md, tests/test_bc_parse_robust.py; PAYOUT_CODE/TRADING_CODE/FORBIDDEN на строках будущей правки не срабатывают; домены проверяются и в tests/*.py по ALLOWED_DOMAINS
- git diff -U0 всех remote-веток: в зоне p2p.py 520-660 ханки только у cloud/stage2-speed (после стр. 632 _bc_fetch, строка to_thread в _bc_refresh, хвост lbank ~540), cloud/stage2-freshness (653+) и cloud/audit-paper-replay (635); внутри 566-600 нет ни одного; bc-reliability/-v2 уже в main (Журнал 2026-09-28, #167)

**Пересечения с другими задачами и ветками:** Ветки bc-reliability и bc-reliability-v2 влиты в main и тело _bc_parse не меняют. cloud/stage2-speed правит только вызов asyncio.to_thread(_bc_parse, data), вставку в _bc_fetch и хвост lbank: ханки не пересекаются с строками 566-597, сигнатура _bc_parse сохраняется. bc-stack-dedup (worker 1) работает с _stack, а не с разбором дампа. tests-arun-audit не затрагивается, если новые тесты синхронные. Открытый пункт [~] ROADMAP про флаг «белого треугольника» когда-нибудь добавит поле в _bc_parse: это будущая отдельная правка после разбора реального info.zip владельцем, она не блокирует эту задачу. Перед стартом сверить: git diff origin/main...origin/cloud/stage2-speed -U0 -- p2p.py | grep '^@@' - ханков в диапазоне _bc_parse быть не должно.

---
### Задача 4. `log-repeat-throttle` — Троттлинг одинаковых WARNING/ERROR в логе: сбой сети не заливает bot.log и /logs одинаковыми строками

**Ценность:** 3/5 · **Размер:** S · **Ветка:** `claude/log-repeat-throttle`

**Зачем:** При обрыве интернета command_loop пишет logger.warning('getUpdates error: ...') каждые 5 секунд (до ~17 000 одинаковых строк в сутки при мгновенном отказе соединения; при зависшей сети с таймаутом 40 с — около 1 900), scan_loop — 'scan error' каждый интервал, sim_tick/perp_loop/accounts_loop — свои error. Это порядка 2-3 МБ в сутки: ротация 5 x 1 МБ (p2p.py:90) вытесняет из файлов всё полезное за 2-3 суток простоя, а /logs (последние 30 строк) через 2.5 минуты показывает 30 одинаковых строк вместо картины «что было до сбоя». Троттлинг оставляет первую строку сразу и одну в 5 минут с счётчиком пропущенных, так что диагностика после простоя сохраняется.

**Что сделать:** 1) Новый модуль logthrottle.py (только stdlib: copy, logging, time, logging.handlers.RotatingFileHandler; константы WINDOW = 300, MAX_KEYS = 500, KEY_LEN = 200 — без переменных окружения). Класс RepeatThrottle — миксин к logging.Handler: __init__(self, *args, window=WINDOW, min_level=logging.WARNING, clock=time.monotonic, max_keys=MAX_KEYS, **kwargs) вызывает super().__init__(*args, **kwargs) и заводит self._seen = {} (ключ -> [время последнего пропущенного в вывод, число подавленных]). Часы по умолчанию time.monotonic, НЕ time.time (скачок системных часов после сна ПК). Ключ повтора: (record.name, record.levelno, record.getMessage()[:KEY_LEN]).
2) emit(self, record): если record.levelno < self.min_level — сразу super().emit(record), состояние не трогать. Иначе в try/except Exception считаем решение; любая ошибка при вычислении ключа (например logger.warning('x %d', 's') — getMessage бросает TypeError) означает «пропустить запись как есть» (super().emit(record), где стандартный handleError сам обработает кривой формат). Из emit НЕЛЬЗЯ выбрасывать исключения: иначе кривой вызов логгера уронит command_loop/scan_loop. Решение: ключа нет в _seen — запись проходит, ключ запоминается; ключ есть и 0 <= now - last < window — запись подавляется (счётчик +1, super().emit не вызывается); окно истекло (или now < last — часы пошли назад, считать истёкшим) — запись проходит, время last обновляется, счётчик обнуляется; если счётчик был > 0, вместо оригинала выпускается copy.copy(record) с msg = f"{record.getMessage()} (ещё {n} таких же за {m} мин)", где m = max(1, round((now - last) / 60)), и args = (). Исходный объект записи НЕ мутировать (один и тот же record обрабатывают файловый и консольный хендлеры, у каждого свой независимый _seen). При каждом проходе ключ перезаписывать через pop и повторную вставку, чтобы порядок словаря = порядок последнего вывода. При добавлении нового ключа сверх max_keys: сначала удалить просроченные, затем самые старые (первые по порядку словаря), пока len(_seen) <= max_keys. В docstring зафиксировать ограничение: счётчик пропущенного показывается только когда тот же ключ повторился после окна; если сбой прекратился, последняя порция счётчика не печатается (сознательно, без вывода при close).
3) В том же модуле два класса без своего тела: ThrottledFileHandler(RepeatThrottle, RotatingFileHandler) и ThrottledStreamHandler(RepeatThrottle, logging.StreamHandler); позиционные и именованные аргументы уходят родителям (maxBytes, backupCount, encoding, stream).
4) p2p.py: добавить `from logthrottle import ThrottledFileHandler, ThrottledStreamHandler` к остальным локальным импортам; в setup_logging заменить RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding='utf-8') на ThrottledFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding='utf-8') и logging.StreamHandler(sys.stdout) на ThrottledStreamHandler(sys.stdout); удалить ставший ненужным `from logging.handlers import RotatingFileHandler` (p2p.py:24, других использований нет: grep); форматтер, уровень root INFO, _log_handlers и идемпотентность не менять; в docstring setup_logging добавить фразу про троттлинг (одинаковые WARNING+ не чаще раза в 5 минут). Тесты с caplog не затрагиваются: подавляют только два хендлера, которые ставит setup_logging.
5) ROADMAP.md: очередь пуста, поэтому добавить в конец раздела «### Надёжность 24/7» строку `- [x] Троттлинг одинаковых WARNING/ERROR в логе ...` с датой и итогом одной строкой и вставить запись в начало «## Журнал» (сначала новые), формат как у соседних записей.
6) Перед пушем прогнать python -m pytest -q и python scripts/guard.py. Если tests/test_trading_surface.py красный из-за нового модуля — не править тест и не обходить: переписать код проще (словарь только через pop/присваивание/del/items, без обращения к сессиям и сети) либо отметить в ROADMAP «ждёт владельца». Прототип этой спеки (в памяти, без файла) уже прогнан через _surface из этого теста и через регулярки guard — чисто.

**Править:** `p2p.py`, `ROADMAP.md`
**Создать:** `logthrottle.py`, `tests/test_logthrottle.py`

**Тесты (офлайн):**
- tests/test_logthrottle.py: хендлер ThrottledStreamHandler(io.StringIO()) с подменённым clock (изменяемая переменная времени) и собственным логгером (propagate=False, хендлер снимать в finally): 1000 одинаковых WARNING за 60 с -> в выводе одна строка; после window следующая одинаковая строка выходит с суффиксом 'ещё 999 таких же'; разные тексты не подавляют друг друга; INFO и ниже (min_level) не подавляются и не заводят ключи; ERROR подавляется по тем же правилам, а WARNING и ERROR с одним текстом — разные ключи.
- Исходный LogRecord не изменён после emit (второй хендлер на том же логгере видит оригинальный текст и args; у копии msg с суффиксом, args == ()).
- Переполнение max_keys (600 разных сообщений при max_keys=500) не растит словарь выше max_keys и не падает; просроченные ключи вытесняются раньше живых, при равенстве — самые старые.
- emit не бросает: logger.warning('bad %d', 's') не поднимает исключение из вызова логгера (logging.raiseExceptions на время теста False или monkeypatch handleError), запись не пропадает молча; часы, пошедшие назад (now < last), считаются истёкшим окном.
- Сообщение с %-аргументами (logger.warning('x %s', 1)) считается по итоговому тексту getMessage(): 'x 1' и 'x 2' — разные ключи, два раза 'x 1' — один.
- Обрезка ключа: два сообщения длиннее KEY_LEN с одинаковым началом считаются одним ключом (документированное поведение).
- p2p.setup_logging(tmp_path/'bot.log'): два одинаковых warning подряд -> в файле одна строка; hasattr(h, 'maxBytes') верно для файлового хендлера, maxBytes == 1_000_000 и backupCount == 5; повторный вызов setup_logging снимает старые хендлеры (существующий tests/test_logging_setup.py остаётся зелёным без правок; хендлеры закрывает автофикстура conftest, а в тестах с прямым созданием ThrottledFileHandler на tmp_path вызвать close() в finally — на Windows иначе файл занят).

**Критерии приёмки:**
- Идентичные WARNING/ERROR (одинаковые логгер, уровень и первые 200 символов текста) не пишутся в bot.log и в консоль чаще одного раза за 5 минут; при следующем показе в строке указано «(ещё N таких же за M мин)».
- INFO-строки, формат строки лога, уровень root INFO и ротация 5 x 1 МБ не изменились; исходный LogRecord не мутируется; из emit не выходят исключения.
- python -m pytest -q (включая tests/test_logging_setup.py, tests/test_trading_surface.py, tests/test_env_documented.py и все caplog-тесты) и python scripts/guard.py зелёные; guard не находит защищённых путей и торговых/платёжных строк.
- В ROADMAP.md есть строка [x] в разделе «Надёжность 24/7» и новая запись в начале Журнала; изменено не более ~250 строк в сумме.

**Не делать:**
- Не менять launcher.py, .github/, CLAUDE.md, scripts/guard.py, tests/conftest.py, tests/test_trading_surface.py, pytest.ini, requirements.txt, data/, logs/, .env*, *.bat, trading/, payouts.py и любые пути со словами payout/trading; не называть переменные и функции payout*/trading* и не писать в новых строках слова из запретного списка guard (TRADING, trd_, ордера, позиции, вывод/переводы, эндпоинты).
- Не добавлять переменные окружения и не читать os.environ (.env.example защищён, tests/test_env_documented.py): window, лимиты и ключ — только константы модуля и параметры конструктора.
- Не править bot.py и отдельные logger.warning/error в нём (это отдельная задача про секреты в логах), не менять уровни логирования и текст существующих сообщений.
- Не подавлять записи через logging.Filter на корневом логгере и не ставить хендлеры на root вне setup_logging; не мутировать переданный LogRecord (только copy.copy).
- Не использовать time.time как часы по умолчанию (только time.monotonic); не бросать исключения из emit.
- Не использовать getattr с вычисляемым именем, import socket/ssl/http/requests/subprocess/eval/exec, объекты с именами s/session/client и методы .send/.open/.post/.get на них — это флаги tests/test_trading_surface.py и guard; если тест красный, не ослаблять его.
- Не трогать файлы logs/ и не читать содержимое реальных логов; не менять тесты в tests/test_logging_setup.py (новый функционал — только в новом файле).

**Где смотреть в коде (проверено на origin/main ad65d71):**
- bot.py:3614-3621 — Bot.command_loop: при исключении getUpdates logger.warning("getUpdates error: %s", e) и await asyncio.sleep(5) в бесконечном цикле (строки 3618-3621)
- bot.py:1559-1562 — Bot.call: aiohttp.ClientTimeout(total=40); мгновенный отказ соединения даёт ~17 280 строк в сутки, зависшая сеть около 1 900
- bot.py:2656 — logger.error("scan error: %s", e) в каждом скане при сбое; bot.py:2794-2795 — logger.error("%s: %s", name, e) в sim_tick
- p2p.py:80-97 — setup_logging: RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8") на строке 90 и logging.StreamHandler(sys.stdout) без дедупликации; p2p.py:24 — единственный импорт RotatingFileHandler
- bot.py:1258-1272 — logs_view показывает только последние 30 строк файла
- tests/test_logging_setup.py:16-19 фиксирует hasattr(h, 'maxBytes'), maxBytes == 1_000_000, backupCount == 5; tests/conftest.py:445-452 (_clean_logging) снимает и закрывает p2p._log_handlers
- p2p.py:1612 — ошибки площадок уже сглажены _venue_backoff, поэтому основной шум идёт из циклов bot.py, которые эта задача покрывает на уровне хендлера без правок bot.py
- scripts/guard.py: protected() для logthrottle.py, tests/test_logthrottle.py, p2p.py, ROADMAP.md — False; регулярки TRADING_CODE/PAYOUT_CODE/FORBIDDEN на прототипе — 0 совпадений; tests/test_trading_surface._surface на прототипе и изменённом p2p.py — отправителей и динамики нет
- ROADMAP.md: в Очереди нет незакрытых [ ], последний коммит 81086ff «queue empty»; ни в Журнале, ни в 50 удалённых ветках нет темы троттлинга логов

**Пересечения с другими задачами и ветками:** Ни одна из перечисленных веток и записей Журнала не занимается троттлингом логов (cloud/terms-traps-log* — журнал ловушек условий мерчантов, другая область). Очередь worker 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, отчёты/дайджест, межмонетные связки часть 2, хедж/гейты/торговля) не пересекается: нет общих файлов, кроме ROADMAP.md (строки добавляются в разные места, при конфликте оставить обе). Ветки bc-reliability*, scan-watchdog* правят другие участки p2p.py/bot.py, не setup_logging. Возможная соседняя задача log-redact-secrets в списке веток не значится; если появится, она правит форматтер в p2p.py:89, а эта — строки 90-91 и импорты: конфликт только по соседним строкам, при слиянии сохранить обе правки.

---
### Задача 5. `netstatus-duplicate-net-merge` — netstatus: одно правило слияния дублей сети во всех четырёх разборщиках справочников (открытая на вывод выигрывает)

**Ценность:** 2/5 · **Размер:** S · **Ветка:** `claude/netstatus-duplicate-net-merge`

**Зачем:** Если справочник биржи отдаёт две записи одной сети (после normalize() обе становятся, например, TRC20), _parse_kucoin выбирает открытую на вывод (комментарий 'у KuCoin бывает две записи TON'), а _parse_htx, _parse_bybit и _parse_mexc безусловно перезаписывают запись последней, так что вывод (wd) зависит от порядка строк в ответе биржи. Проверено: HTX [закрыт TRC20, открыт Tron(TRC20)] даёт wd False, а обратный порядок даёт wd True. Хуже того, netstatus._apply сравнивает таблицу с прежней и кладёт переключения в CHANGES, поэтому смена порядка строк между двумя опросами превращается в ложный алерт владельцу 'вывод приостановлен / снова открыт' (проверено: pop_changes() = [('HTX','USDT','TRC20','вывод',False)]). Правило 'открытая выигрывает' уже принято у KuCoin (и закреплено tests/test_netstatus.py::test_parse_public_fixtures: ku['TON']['wd'] is True) - задача распространяет его на остальные три разборщика. Тестов на дубли для HTX/Bybit/MEXC нет.

**Что сделать:** 1) В netstatus.py прямо перед _parse_htx добавить приватную функцию _merge_net(out, net, rec) (именно это имя; не _put) с коротким комментарием на русском: 'if net not in out or (rec["wd"] and not out[net]["wd"]): out[net] = rec' - то есть при повторе сети побеждает запись с открытым выводом, при равном статусе вывода остаётся первая; dep/fee/min берутся из победившей записи целиком. Это ровно правило из текущего _parse_kucoin (netstatus.py:93-94).
2) Использовать _merge_net во всех четырёх разборщиках (номера строк на ad65d71): _parse_htx (присваивание out[normalize(displayName or chain)] = {...} на 78-81) -> _merge_net(out, normalize(...), {...}); фильтр HTX_NATIVE_ONLY (76-77) остаётся ДО слияния без изменений; _parse_kucoin (93-94: cur = out.get(net) / if cur is None or ...) -> одна строка _merge_net(out, net, rec), поведение KuCoin не меняется; _parse_bybit (104-107) и _parse_mexc (118-121) - так же. Словарь записи {'dep','wd','fee','min'} и вызовы _f() не менять; normalize/KNOWN_NETS/PARTIAL/HTX_NATIVE_ONLY/_apply/CHANGES/UNMAPPED не трогать.
3) Новый tests/test_netstatus_duplicates.py (только pytest + netstatus + helpers.arun при необходимости; синтетический JSON, никаких фикстур tests/fixtures/*.json и URL). Локальные построители ответа по списку строк (имя, wd: bool, fee: float), dep всегда открыт, min='1': HTX {'code':200,'data':[{'chains':[{'chain': имя.lower(), 'displayName': имя, 'depositStatus':'allowed', 'withdrawStatus':'allowed'|'prohibited', 'transactFeeWithdraw': str(fee), 'minWithdrawAmt':'1'}]}]} разбирается _parse_htx(j) без asset; KuCoin {'code':'200000','data':{'chains':[{'chainName': имя, 'isDepositEnabled':True, 'isWithdrawEnabled': wd, 'withdrawalMinFee': fee, 'withdrawalMinSize':1}]}} -> _parse_kucoin; Bybit {'retCode':0,'result':{'rows':[{'chains':[{'chainType': имя, 'chainDeposit':'1', 'chainWithdraw':'1'|'0', 'withdrawFee': str(fee), 'withdrawMin':'1'}]}]}} -> _parse_bybit; MEXC [{'coin':'USDT','networkList':[{'network': имя, 'depositEnable':True, 'withdrawEnable': wd, 'withdrawFee': str(fee), 'withdrawMin':'1'}]}] -> _parse_mexc(j,'USDT'). Пары имён, обе нормализуются в TRC20: HTX ('TRC20','Tron(TRC20)'), KuCoin ('TRC20','Tron(TRC20)'), Bybit ('Tron (TRC20)','TRX'), MEXC ('TRC20','TRX').
Тесты (каждый параметризован по 4 площадкам и, где нужно, по порядку строк):
  a) test_duplicate_net_open_wins_any_order: [закрыт fee 1, открыт fee 2] и обратный порядок -> r['TRC20']['wd'] is True и fee == 2.0 в обоих порядках, таблицы равны.
  b) test_duplicate_net_both_closed_stays_closed: обе закрыты, оба порядка -> 'TRC20' присутствует и wd is False (блокировка маршрута не ослаблена).
  c) test_duplicate_net_both_open_keeps_first: обе открыты с разной fee -> fee равна fee первой строки в обоих порядках (закрепляет tie-break как у KuCoin).
  d) test_withdraw_ok_after_apply_is_order_independent: netstatus._apply(площадка, 'USDT', разбор) для обоих порядков (netstatus.reset() между ними; autouse-фикстура conftest уже сбрасывает состояние между тестами) -> netstatus.withdraw_ok(...,'TRC20') is True и 'TRC20' in netstatus.open_nets(...) в обоих порядках.
  e) test_reordered_duplicates_do_not_raise_change_alert: _apply(порядок A), затем _apply(порядок B) для [закрыт, открыт] -> netstatus.pop_changes() == [] (регресс против ложного алерта 'вывод приостановлен').
4) В ROADMAP.md по протоколу CLAUDE.md добавить строку в раздел 'Журнал' (что сделано: правило 'открытая на вывод выигрывает' теперь в четырёх разборщиках; KuCoin без изменений; при равном wd остаётся первая запись, dep/fee/min из неё). Больше ничего в ROADMAP.md не править.

**Править:** `netstatus.py`, `ROADMAP.md`
**Создать:** `tests/test_netstatus_duplicates.py`

**Тесты (офлайн):**
- tests/test_netstatus_duplicates.py::test_duplicate_net_open_wins_any_order[HTX|KuCoin|Bybit|MEXC]
- tests/test_netstatus_duplicates.py::test_duplicate_net_both_closed_stays_closed
- tests/test_netstatus_duplicates.py::test_duplicate_net_both_open_keeps_first
- tests/test_netstatus_duplicates.py::test_withdraw_ok_after_apply_is_order_independent
- tests/test_netstatus_duplicates.py::test_reordered_duplicates_do_not_raise_change_alert
- tests/test_netstatus.py без изменений и зелёный (в т.ч. test_parse_public_fixtures с ku['TON']['wd'] is True и test_parse_htx_ignores_wrapped_tokens_for_btc_eth: там проверяется только fee)
- tests/test_trading_surface.py и tests/test_guard*.py зелёные без изменений

**Критерии приёмки:**
- Для _parse_htx, _parse_kucoin, _parse_bybit, _parse_mexc значение wd у сети с дублями не зависит от порядка строк: если хотя бы одна запись открыта на вывод - wd True; если все закрыты - wd False (сеть остаётся в таблице)
- При равном статусе вывода остаётся первая запись (dep/fee/min из неё) - как уже делал _parse_kucoin; поведение KuCoin на всех входах не изменилось
- Таблицы, которые дают боевые вызовы на существующих фикстурах (HTX USDT/USDC/BTC/ETH с asset, KuCoin USDT/USDC/BTC/ETH, MEXC USDT/USDC/BTC/ETH), идентичны таблицам до правки; tests/test_netstatus.py проходит без правок (разбор HTX BTC без asset может отличаться только полем min у TRC20 - тест это не проверяет)
- Два подряд _apply с одним ответом биржи в разном порядке дублей не оставляют записей в netstatus.CHANGES
- python -m pytest -q и python scripts/guard.py зелёные; diff не больше 150 строк; guard не показывает ни защищённых путей, ни торговых/выплатных строк
- Отчёт воркера явно говорит: выбрано правило 'открытая выигрывает' по аналогии с KuCoin; для HTX/Bybit/MEXC меняется результат только при дублях сети (раньше выигрывала последняя запись); dep/fee/min берутся из победившей записи

**Не делать:**
- не менять normalize(), KNOWN_NETS, PARTIAL, HTX_NATIVE_ONLY (фильтр остаётся до слияния), _apply, CHANGES-логику, UNMAPPED, TTL, refresh()
- не менять правило KuCoin и не переходить на 'закрытая выигрывает' (это сломает test_parse_public_fixtures: ku['TON']['wd'] is True) и не ослаблять блокировку: случай 'все записи сети закрыты' остаётся закрытым
- не объединять поля dep/fee/min из разных записей (без частичных слияний): побеждает целая запись
- не менять tests/fixtures/*.json, tests/test_netstatus.py, tests/conftest.py, p2p.py (потребители netstatus в p2p.py:978-1075 остаются как есть)
- не называть помощника put/_put и не использовать в новых строках netstatus.py слова payout, TRADING, trd_, .post(, order/position/margin/withdraw-эндпоинтов; помощник называется _merge_net
- не добавлять сетевые запросы, домены, зависимости, subprocess; в тесте не писать URL и не использовать asyncio.run (нужен цикл - helpers.arun)
- не читать .env, data/keys.json, папки с ключами; разборщики только преобразуют готовый JSON

**Где смотреть в коде (проверено на origin/main ad65d71):**
- netstatus.py:78-81 (_parse_htx): 'out[normalize(displayName or chain)] = {...}' - последняя запись выигрывает; фильтр HTX_NATIVE_ONLY на строках 76-77
- netstatus.py:104-107 (_parse_bybit) и netstatus.py:118-121 (_parse_mexc): тот же безусловный out[...] = {...}
- netstatus.py:91-94 (_parse_kucoin): 'cur = out.get(net); if cur is None or (rec["wd"] and not cur["wd"]):  # у KuCoin бывает две записи TON - берём открытую'
- netstatus.py:142-151 (_apply): CHANGES.append при prev[kind] != rec[kind] для сетей из KNOWN_NETS - смена порядка дублей даёт ложный алерт (проверено запуском: [('HTX','USDT','TRC20','вывод',False)])
- tests/test_netstatus.py:22-34: дубль покрыт только для KuCoin (TON, из фикстуры); ни одного теста на две записи одной сети у HTX/Bybit/MEXC; tests/test_netstatus.py:37-53 проверяет только fee при разборе HTX BTC без asset
- tests/fixtures/htx_currencies.json BTC: chain trc20btc (displayName TRC20) и trc20wbtc (TRC20WBTC) дают два TRC20 без asset-фильтра (fee у обоих 1e-05, min 4.1e-06 против 0.001); с asset='BTC' (боевой путь, netstatus.py:161) фильтр их отсекает; в USDT/USDC/ETH у HTX, во всём KuCoin (кроме TON x2 у USDT) и в 4 монетах mexc_coins.json дублей нет
- p2p.py:978-981 (deposit_nets/deposit_ok), 1003-1016 (open_nets, live_fee, min_withdraw, withdraw_ok в _withdraw): порядок дублей влияет на решение 'сеть закрыта' для маршрута
- tests/conftest.py:287-292: autouse _clean_netstatus вызывает netstatus.reset(); tests/helpers.py:39 arun; scripts/guard.py: PROTECTED/PROTECTED_NAMES/TRADING_CODE не совпадают с добавляемыми строками (проверено импортом guard и test_trading_surface._fake_surface на предложенном коде)

**Пересечения с другими задачами и ветками:** Ветки cloud/s2-unmapped-nets (+24 строки в netstatus.py, влита как #159), cloud/owner-stop-fixes (normalize/PARTIAL, влито как d3286fb) и cloud/tests-sockets (arun, влито #139) правили netstatus.py/test_netstatus.py, но слияние дублей сетей не трогали; ROADMAP Журнал слияние дублей не упоминает. Очередь воркера 1 (bc-stack-dedup, tests-arun-audit, maker-paper-v2/quote-advice, funding-alerts, weekly reports/digest, cross-coin part 2, hedge/gates/trading) не пересекается. Единственная точка касания - tests-arun-audit: новый тест должен использовать helpers.arun (или вовсе быть синхронным, как в спецификации), чтобы аудит asyncio.run в тестах ничего в нём не находил.

---
Когда все задачи волны сделаны или помечены «ждёт владельца», напиши итог: список веток, что влито, что ждёт владельца. Следующую волну не начинай — её вставит владелец.
