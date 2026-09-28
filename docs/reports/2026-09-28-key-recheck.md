# cloud/key-recheck — периодическая перепроверка прав ключей бирж

Идея из ROADMAP «Идеи» (очередь координатора пуста).

- Ветка: `cloud/key-recheck` от origin/main cf16810, коммит 685395f.
- Тесты: 2362 passed, 5 skipped (`python -m pytest -q -p no:cacheprovider`); guard: ок.

## Что сделано
- `bot.key_recheck_hours()` — `KEY_RECHECK_HOURS` из .env (по умолчанию 1; 0 — только при старте; мусор, минус,
  nan, inf → по умолчанию). Добавлено в `.env.example`.
- `Bot.key_checked_ts` + `Bot.key_recheck_due(now)`; `check_key_safety` отмечает время проверки.
- `accounts_loop` после `check_accounts` вызывает `check_key_safety(periodic=True)`, когда пора. Правила те же, что при
  старте: ключ с торговлей/выводом удаляется с сообщением; при `ALLOW_UNSAFE_KEYS=1` остаётся, неизменное состояние
  не пишется в лог каждый час. Исключение перепроверки ловится отдельно и не рвёт цикл (в лог — только тип ошибки).
- `tests/test_key_recheck.py` — 6 тестов (через `helpers.arun`).
- ROADMAP: идея отмечена «сделано 2026-09-28», строка в «Журнал».

## Сомнения
- Cryptomus: `api_permissions` для него всегда «не только чтение», так что без `ALLOW_UNSAFE_KEYS=1` ключ удаляется и
  при старте — поведение не изменилось, просто проверка теперь повторяется.
- Перепроверка идёт только при заданном `TG_CHAT_ID` (как и сам `accounts_loop`).
