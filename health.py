"""Здоровье данных бота: свободное место на диске, размеры баз и логов, возраст последней резервной копии.
Только чтение (os.stat, glob, shutil.disk_usage, backup.copies/_ts) — без сети, без subprocess, без
env-переменных, ничего не пишет и не удаляет. Пути по умолчанию разрешаются при вызове snapshot(), а не при
определении функций, — так conftest может подменить backup.DATA_DIR/p2p.LOG_PATH на временную папку теста."""
import glob
import os
import shutil

import backup
import p2p

LOW_FREE = 1_000_000_000   # байт — порог «места мало» (1 ГБ)
DISK_CHECK_EVERY = 3600    # сек — не чаще раза в час проверять диск и слать алерт


def _disk_usage(data_dir):
    """(свободно, всего) байт для раздела data_dir; сбой (диска нет, нет прав) — (None, None), без исключения."""
    try:
        usage = shutil.disk_usage(data_dir)
        return usage.free, usage.total
    except OSError:
        return None, None


def free_bytes(data_dir):
    return _disk_usage(data_dir)[0]


def total_bytes(data_dir):
    return _disk_usage(data_dir)[1]


def dbs(data_dir, top=5):
    """[(имя, размер)] — только *.db из data_dir, крупнейшие первыми (не больше top); содержимое не читается,
    сбой на отдельном файле (гонка с записью/правами) гасится."""
    out = []
    for path in glob.glob(os.path.join(data_dir, "*.db")):
        try:
            out.append((os.path.basename(path), os.stat(path).st_size))
        except OSError:
            continue
    out.sort(key=lambda item: -item[1])
    return out[:top]


def logs_bytes(log_path):
    """Сумма размеров bot.log и его ротаций bot.log.1/.2/… рядом с log_path."""
    total = 0
    for path in glob.glob(log_path + "*"):
        try:
            total += os.stat(path).st_size
        except OSError:
            continue
    return total


def backup_count(data_dir):
    try:
        return len(backup.copies(data_dir))
    except OSError:
        return 0


def backup_last_ts(data_dir):
    """Unix-время последней копии (backup.copies — старые первыми) или None — копий ещё нет/сбой чтения."""
    try:
        names = backup.copies(data_dir)
    except OSError:
        return None
    if not names:
        return None
    try:
        return backup._ts(names[-1])
    except ValueError:
        return None


def snapshot(data_dir=None, log_path=None):
    """Срез здоровья данных для /status и disk_check: free_bytes/total_bytes (диск), dbs (топ-5 *.db по размеру),
    logs_bytes, backup_count и backup_last_ts. data_dir/log_path разрешаются здесь (None -> backup.DATA_DIR /
    p2p.LOG_PATH), чтобы тест мог передать tmp_path напрямую, не трогая настоящие data/logs."""
    data_dir = backup.DATA_DIR if data_dir is None else data_dir
    log_path = p2p.LOG_PATH if log_path is None else log_path
    return {
        "free_bytes": free_bytes(data_dir),
        "total_bytes": total_bytes(data_dir),
        "dbs": dbs(data_dir),
        "logs_bytes": logs_bytes(log_path),
        "backup_count": backup_count(data_dir),
        "backup_last_ts": backup_last_ts(data_dir),
    }


def fmt_size(n):
    """Байты в 'X.Y МБ'/'X.Y ГБ' (один знак после запятой); None -> '?'."""
    if n is None:
        return "?"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} ГБ"
    return f"{n / 1_000_000:.1f} МБ"


def low(free):
    """Свободного места мало (меньше LOW_FREE)? free=None (диск не прочитать) — не мало, а неизвестно."""
    return free is not None and free < LOW_FREE


def lines(snap, now):
    """Строки блока здоровья данных для /status «Подробно»: диск (свободно/всего, крупнейшие базы, логи) и
    возраст последней резервной копии."""
    if snap["free_bytes"] is None:
        disk = "💾 Диск: нет данных"
    else:
        dbs_part = ", ".join(f"{name} {fmt_size(size)}" for name, size in snap["dbs"]) or "нет"
        disk = (f"💾 Диск: свободно {fmt_size(snap['free_bytes'])} из {fmt_size(snap['total_bytes'])}; "
                f"базы: {dbs_part}; логи {fmt_size(snap['logs_bytes'])}")
    if snap["backup_last_ts"] is None:
        backup_line = "Копий баз ещё нет"
    else:
        age_hours = max(0, int((now - snap["backup_last_ts"]) // 3600))
        backup_line = f"Копия баз: {age_hours} ч назад, копий {snap['backup_count']}"
    return [disk, backup_line]
