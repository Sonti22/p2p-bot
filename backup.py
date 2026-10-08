"""Резервные копии баз бота: раз в сутки — снимок пользовательских данных в data/backup/<ГГГГММДД-ЧЧММ>/, хранится
последних BACKUP_KEEP (по умолчанию 7; 0 — не делать). Атомарная запись (jsonstore) защищает от порчи при записи, но
не от сбоя диска или случайного удаления — копия рядом восстанавливается без сети.

SQLite копируется через sqlite3 backup API — согласованный снимок, даже если бот в этот момент пишет в базу; JSON —
обычной копией. В копию не идут: ключи бирж (keys.json — секреты не размножаем, их заново вводят в боте), снимки
сканов (snapshots.db — гигабайты, восстанавливаются сами) и всё, чего нет в FILES.
Восстановление — вручную: остановить бота, скопировать файлы из нужной папки data/backup/… в data/.

Каждая копия перед попаданием в data/backup проверяется на целостность (PRAGMA quick_check для баз, json.load для
JSON): испорченный файл в новую копию не попадает и не вытесняет ротацией последнюю исправную копию — см. bad()
и last_good().
"""
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
FILES = ("trades.db", "blacklist.db", "alerts.db", "history.db", "paper.db", "paper_portfolio.db", "paper_bank_profiles.json", "favorites.json", "presets.json",
         "payouts.db",         # ID выплат, неизвестные исходы и учёт дневного лимита
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


def _check_db(path):
    """Проверка целостности копии базы (PRAGMA quick_check): None — исправна, иначе текст первой найденной проблемы
    (обрезано до 200 символов). Странично испорченная база бросает DatabaseError вместо возврата строк, поэтому его
    тоже ловим; сбой самой проверки (диск, I/O) — это OperationalError, подкласс DatabaseError, его перехватываем и
    пробрасываем ПЕРВЫМ, иначе сбой копии выглядел бы как испорченная база."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute("PRAGMA quick_check").fetchall()
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as e:
        return str(e)[:200]
    finally:
        con.close()
        for suffix in ("-wal", "-shm", "-journal"):   # копия WAL-базы создаёт их рядом на время проверки
            try:
                os.remove(path + suffix)
            except OSError:
                pass
    if rows == [("ok",)]:
        return None
    return "; ".join(str(r[0]) for r in rows)[:200]


def _check_json(path):
    """None — исправный словарь JSON (favorites/presets — словари, см. jsonstore.read_dict), иначе текст проблемы."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:   # сюда входят json.JSONDecodeError и UnicodeDecodeError
        return "битый JSON"
    if not isinstance(data, dict):
        return "не словарь"
    return None


def bad():
    """Файлы последней копии, не прошедшие проверку целостности и не попавшие в неё: [(имя файла, текст проблемы)]."""
    return list(_state.get("bad", []))


def last_good(fname, data_dir=DATA_DIR):
    """Самая новая папка копии, где есть fname (плохие файлы в копию не попадают, значит присутствие = исправность
    на момент той копии), или None, если ни в одной копии файла нет."""
    for name in reversed(copies(data_dir)):
        if os.path.isfile(os.path.join(_dir(data_dir), name, fname)):
            return name
    return None


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
    """Сделать копию FILES (что есть), пропустив испорченные (см. _check_db/_check_json — bad()), и удалить старые
    сверх keep(), сохраняя последнюю копию каждого испорченного файла. Возвращает (папка копии или None, [файлы])."""
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
    bad_files = []
    try:
        for fname in FILES:
            src = os.path.join(data_dir, fname)
            if not os.path.isfile(src):
                continue
            out = os.path.join(tmp, fname)
            why = None
            if fname.endswith(".db"):
                s, d = sqlite3.connect(src), sqlite3.connect(out)
                try:
                    s.backup(d)
                except sqlite3.OperationalError:
                    raise
                except sqlite3.DatabaseError as e:
                    why = str(e)[:200]
                finally:
                    d.close()
                    s.close()
                if why is None:
                    why = _check_db(out)
            else:
                shutil.copy2(src, out)
                why = _check_json(out)
            if why:
                if os.path.exists(out):
                    os.remove(out)
                bad_files.append((fname, why))
                continue
            done.append(fname)
        shutil.rmtree(dest, ignore_errors=True)   # та же минута (перезапуск) — заменяем
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _state["bad"] = bad_files
    keepset = {g for g in (last_good(f, data_dir) for f, _ in bad_files) if g}
    for old in copies(data_dir)[:-n]:   # ротация: только наши папки по шаблону имени внутри data/backup
        if old in keepset:              # последняя исправная копия испорченного файла переживает ротацию
            continue
        shutil.rmtree(os.path.join(_dir(data_dir), old), ignore_errors=True)
    return dest, done
