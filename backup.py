"""Резервные копии баз бота: раз в сутки — снимок пользовательских данных в data/backup/<ГГГГММДД-ЧЧММ>/, хранится
последних BACKUP_KEEP (по умолчанию 7; 0 — не делать). Атомарная запись (jsonstore) защищает от порчи при записи, но
не от сбоя диска или случайного удаления — копия рядом восстанавливается без сети.

SQLite копируется через sqlite3 backup API — согласованный снимок, даже если бот в этот момент пишет в базу; JSON —
обычной копией. В копию не идут: ключи бирж (keys.json — секреты не размножаем, их заново вводят в боте), снимки
сканов (snapshots.db — гигабайты, восстанавливаются сами) и всё, чего нет в FILES.
Восстановление — вручную: остановить бота, скопировать файлы из нужной папки data/backup/… в data/.
"""
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
FILES = ("trades.db", "blacklist.db", "alerts.db", "history.db", "paper.db", "favorites.json", "presets.json",
         "hedge_circles.db",   # хеджи кругов (trading/hedge.py): открытые шорты и их итог
         "trading.db")         # журнал ордеров торгового ядра: позиции бота, результат дня (WAL — копия через backup API)
KEEP_DEFAULT = 7
EVERY = 86400                     # сек — не чаще раза в сутки
MSK = timezone(timedelta(hours=3))
_NAME = re.compile(r"^\d{8}-\d{4}$")
RETRY = 3600                      # сек — после сбоя копии следующая попытка не раньше (не долбить диск каждый скан)
_state = {"tried": 0.0}


def keep():
    """BACKUP_KEEP из .env: сколько копий хранить; 0 — копии выключены; мусор или минус — по умолчанию."""
    try:
        n = int(os.getenv("BACKUP_KEEP", KEEP_DEFAULT))
    except ValueError:
        return KEEP_DEFAULT
    return n if n >= 0 else KEEP_DEFAULT


def _dir(data_dir):
    return os.path.join(data_dir, "backup")


def copies(data_dir=DATA_DIR):
    """Папки копий, старые первыми (имя — время по МСК, сортируется как строка)."""
    root = _dir(data_dir)
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if _NAME.match(n) and os.path.isdir(os.path.join(root, n)))


def _ts(name):
    return datetime.strptime(name, "%Y%m%d-%H%M").replace(tzinfo=MSK).timestamp()


def due(now=None, data_dir=DATA_DIR):
    """Пора ли делать копию: включено и последней копии нет или она старше EVERY (по имени папки — переживает
    перезапуск бота)."""
    if keep() <= 0:
        return False
    now = time.time() if now is None else now
    if now - _state["tried"] < RETRY:
        return False
    have = copies(data_dir)
    return not have or now - _ts(have[-1]) >= EVERY


def run(now=None, data_dir=DATA_DIR):
    """Сделать копию FILES (что есть) и удалить старые сверх keep(). Возвращает (папка копии или None, [файлы])."""
    n = keep()
    if n <= 0:
        return None, []
    now = time.time() if now is None else now
    _state["tried"] = now
    name = datetime.fromtimestamp(now, MSK).strftime("%Y%m%d-%H%M")
    dest = os.path.join(_dir(data_dir), name)
    tmp = os.path.join(_dir(data_dir), ".tmp-" + name)   # сбой посередине не оставит «копию», которая засчитается
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    done = []
    try:
        for fname in FILES:
            src = os.path.join(data_dir, fname)
            if not os.path.isfile(src):
                continue
            out = os.path.join(tmp, fname)
            if fname.endswith(".db"):
                s, d = sqlite3.connect(src), sqlite3.connect(out)
                try:
                    s.backup(d)
                finally:
                    d.close()
                    s.close()
            else:
                shutil.copy2(src, out)
            done.append(fname)
        shutil.rmtree(dest, ignore_errors=True)   # та же минута (перезапуск) — заменяем
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    for old in copies(data_dir)[:-n]:   # ротация: только наши папки по шаблону имени внутри data/backup
        shutil.rmtree(os.path.join(_dir(data_dir), old), ignore_errors=True)
    return dest, done
