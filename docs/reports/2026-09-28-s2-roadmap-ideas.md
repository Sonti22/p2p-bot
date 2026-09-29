# cloud/s2-roadmap-ideas + синхронизация веток s2 с main

## Идеи (очередь пуста — по протоколу CLAUDE.md)
- Ветка `cloud/s2-roadmap-ideas` (коммит 6c47d1a), только ROADMAP.md: 3 идеи в «Идеи» —
  1) репутация мерчанта по реальным сделкам (`trades.db`: факт − расчёт по нику) поверх `reputation.py`;
  2) суточная `PRAGMA quick_check` баз перед копией `backup.py` — не копировать испорченную базу поверх целых;
  3) недельный дайджест из готовых сводок (сигналы по направлениям, банки, журнал по направлениям, прогон).
- Тесты: 3237 passed, 5 skipped; guard: ок.

## Синхронизация с main
main ушёл вперёд (торговое ядро `trading/`, 5b16bd2). У всех веток s2 конфликт был только в журнале ROADMAP.md —
влил origin/main merge-коммитом (без force-push), записи обеих сторон сохранены. После слияния на каждой ветке
полный прогон и guard:

| Ветка | Новый head | Тесты | guard |
|---|---|---|---|
| s2-profit-tests | c432b52 | 3246 passed | ок |
| s2-digest-v2 | 706fe9b | 3244 passed | ок |
| s2-bank-spread-history | 58ce46a | 3243 passed | ок |
| s2-merchant-reputation | 899bcfe | 3246 passed | ок |
| s2-help-ux | 70fb843 | 3244 passed | ок |
| s2-stats-direction | 429d1d5 | 3240 passed | ок |
| s2-db-backup | 52fbcb1 | 3242 passed | ок |
| s2-unmapped-nets | 58f63a7 | 3243 passed | ок |
| s2-route-alert | 01c9c17 | 3244 passed | ок |

Между собой ветки s2 пересекались не только в журнале: digest-v2 × merchant-reputation × db-backup — вставки в
`Bot.__init__` и `scan_loop` в одном месте, bank-spread-history × digest-v2 — новые функции перед `signal_stats` в
history.py. Развёл места вставки (коммиты без изменения поведения): db-backup 7781cb9 (инициализация рядом с
`chip_tasks`, запуск копии после записи снимка; нечитаемая data/backup больше не роняет скан), merchant-reputation
55c65df (`rep_task` рядом с `snapshot_keep`), bank-spread-history abefdba (`bank_spread_stats` в конец модуля).
Полный прогон на каждой: 3242 / 3246 / 3243 passed, guard ок.

Проверка: все 10 веток s2 по очереди влиты во временную ветку от origin/main (не пушилась) — конфликты только в
журнале ROADMAP.md («обе записи»), итог 3296 passed, 5 skipped, guard ок.
