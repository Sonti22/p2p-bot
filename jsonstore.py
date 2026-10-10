"""Общая атомарная запись и защищённое чтение JSON-словарей: ключи, пресеты, топики.

Запись — во временный файл рядом с целевым и `os.replace`: при обрыве процесса на диске
остаётся либо старый файл целиком, либо новый, но никогда не половина JSON.
"""
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


def read_dict(path, strict=False):
    """JSON-словарь из файла; нет файла, битый JSON или не словарь на верхнем уровне (например,
    список) — пустой словарь, во втором и третьем случае ещё и предупреждение в лог.
    strict=True — ошибки чтения и структуры пробрасываются: повреждённое хранилище секретов не пустое.
    Отсутствующий файл и в строгом режиме — пустой словарь."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        if strict:
            raise
        logger.warning("%s: битый JSON (%s), считаем пустым", path, e)
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError("JSON должен содержать словарь")
        logger.warning("%s: верхний уровень %s вместо словаря, считаем пустым", path, type(data).__name__)
        return {}
    return data


def write_dict(path, data):
    """Атомарная запись словаря: временный файл в той же папке + `os.replace`."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
