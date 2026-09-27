"""Выключатель торговли — как у выплат (payouts.switch_from_file), решение владельца.

- TRADING=1 в .env на ПК — торговля включена; иначе новые позиции не открываются. Сопровождение, стопы и закрытие
  работают и при TRADING=0 (план, этап 0 п. 5): launcher после каждого обновления кода пишет TRADING=0.
- TRADING_MODE=paper|minlot|confirm|auto (по возрастанию риска): paper — реальных ордеров нет; minlot — кнопка и
  минимальный лот (потолки risk.MINLOT); confirm — кнопка, полные потолки; auto — автомат (только если пройдены
  пороги gates.py).
- Включить (TRADING=1) и поднять режим может только .env на ПК: при старте `switch_from_file` берёт значения из файла,
  а переменные окружения Windows/родителя могут их только понизить. Из Telegram — только понизить режим (`lower`) или
  «⛔ Стоп» (`stop`).
- data/trading_state.json (`write_state`) — открытые позиции и ордера для launcher: при TRADING=0 он знает, что
  сопровождать ещё есть что. Ключей и сумм счёта в файле нет.
"""
import os
import time

import jsonstore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "trading_state.json")
MODES = ("paper", "minlot", "confirm", "auto")   # по возрастанию риска
RANK = {m: i for i, m in enumerate(MODES)}
LIVE = ("minlot", "confirm", "auto")             # режимы с реальными ордерами
OFF = "торговля выключена (TRADING≠1): включить можно только на ПК — TRADING=1 в .env и перезапуск бота"
PAPER = "режим paper: реальных ордеров нет (TRADING_MODE в .env на ПК)"


def enabled():
    """TRADING=1 — читается при каждом обращении."""
    return os.getenv("TRADING") == "1"


def mode():
    """Текущий режим; нет или мусор — paper."""
    m = (os.getenv("TRADING_MODE") or "").strip().lower()
    return m if m in RANK else "paper"


def disable():
    """Новые открытия выключаются в этом процессе сразу (ещё до записи .env — та может и не удаться)."""
    os.environ["TRADING"] = "0"


def stop():
    """«⛔ Стоп» из Telegram: TRADING=0 и режим paper в процессе. Закрытие и стопы продолжают работать."""
    disable()
    os.environ["TRADING_MODE"] = "paper"


def lower(new):
    """Понизить режим (Telegram): True — режим теперь new; повысить так нельзя (False, ничего не меняется)."""
    new = str(new or "").strip().lower()
    if new not in RANK or RANK[new] > RANK[mode()]:
        return False
    os.environ["TRADING_MODE"] = new
    return True


def stop_and_persist(save_env):
    """«⛔ Стоп» для бота (как bot.payout_stop): сначала выключить в процессе (stop — сразу, даже если .env не
    запишется), затем TRADING=0 и TRADING_MODE=paper в .env через save_env бота. None — записано; иначе имя ошибки
    записи (владельцу: остановлено только до перезапуска)."""
    stop()
    try:
        save_env("TRADING", "0")
        save_env("TRADING_MODE", "paper")
    except Exception as e:   # .env только для чтения, занят редактором и т. п.
        return type(e).__name__
    finally:
        stop()   # save_env пишет и в окружение — на случай чужой реализации ещё раз «0»
    return None


def lower_and_persist(new, save_env):
    """Понизить режим из Telegram и записать в .env: (True, None) — понижен и записан; (True, ошибка) — понижен
    только в процессе; (False, None) — повысить нельзя, ничего не менялось."""
    if not lower(new):
        return False, None
    try:
        save_env("TRADING_MODE", mode())
    except Exception as e:
        return True, type(e).__name__
    return True, None


def _file_values(path):
    """{имя в верхнем регистре: [значения]} для TRADING и TRADING_MODE из .env — разбор как у payouts.switch_from_file
    (пустые и # строки — мимо, значение — до « #»). Файл не прочитать — пусто."""
    out = {"TRADING": [], "TRADING_MODE": []}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip().lstrip("﻿").strip().upper()
                    if k in out:
                        out[k].append(v.split(" #")[0].strip())
    except (OSError, ValueError):
        return {"TRADING": [], "TRADING_MODE": []}
    return out


def switch_from_file(path):
    """При старте бота. TRADING остаётся «1», только если в файле есть строка TRADING и ВСЕ такие строки равны 1;
    иначе TRADING=0 в процессе. Режим — из файла, только если все строки TRADING_MODE одинаковые и известные (иначе
    paper), и не выше того, что уже в окружении процесса (окружение может понизить, но не повысить). Только
    выключает/понижает."""
    vals = _file_values(path)
    if not vals["TRADING"] or any(v != "1" for v in vals["TRADING"]):
        disable()
    modes = {v.lower() for v in vals["TRADING_MODE"]}
    file_mode = next(iter(modes)) if len(modes) == 1 and next(iter(modes)) in RANK else "paper"
    env = (os.getenv("TRADING_MODE") or "").strip().lower()
    if env and env not in RANK:
        env = "paper"
    final = min(file_mode, env, key=RANK.get) if env else file_mode
    os.environ["TRADING_MODE"] = final
    return enabled(), final


def can_open():
    """(можно ли открывать реальные позиции, причина отказа)."""
    if not enabled():
        return False, OFF
    if mode() not in LIVE:
        return False, PAPER
    return True, ""


def effective_mode(gate_mode):
    """Режим с учётом порогов: не выше .env и не выше того, что разрешают gates. Торговля выключена — paper."""
    if not enabled():
        return "paper"
    gate_mode = gate_mode if gate_mode in RANK else "paper"
    return min(mode(), gate_mode, key=RANK.get)


def _plain(v):
    return v if isinstance(v, (bool, int, str)) or v is None else str(v)


def write_state(positions, open_orders=0, unknown_orders=0, path=None, now=None):
    """data/trading_state.json для launcher: выключатель, режим, открытые позиции (биржа, монета, сторона, размер),
    число открытых и неясных ордеров. Атомарная запись (jsonstore.write_dict)."""
    rows = [{k: _plain(p.get(k)) for k in ("venue", "symbol", "side", "size", "position")} for p in positions or []]
    data = {"version": 1, "ts": time.time() if now is None else now, "trading": enabled(), "mode": mode(),
            "open_positions": rows, "has_positions": bool(rows), "open_orders": int(open_orders),
            "unknown_orders": int(unknown_orders)}
    jsonstore.write_dict(path or STATE_PATH, data)
    return data


def read_state(path=None):
    return jsonstore.read_dict(path or STATE_PATH)
