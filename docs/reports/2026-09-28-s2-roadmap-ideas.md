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

Между собой ветки s2 по-прежнему пересекаются в журнале ROADMAP.md (каждая добавляет строку в начало) и местами в
bot.py (`Bot.__init__`, `scan_loop` — разные строки, git сводит сам); при вливании по одной — разрешать «обе записи».
