"""Запускает бота, сам подтягивает обновления из GitHub и откатывается, если новая версия падает.

Запуск: python launcher.py (через run.bat). Защищённый файл: правится только вручную, не облачным Claude.
- каждые UPDATE_EVERY сек: git fetch; есть новое в origin/main и папка чистая → merge --ff-only ровно на
  полученный sha → смоук-тест
  (компиляция + pytest; нет pytest — ставит его, не вышло или тестов нет — провал) →
  перезапуск бота и сообщение в Telegram; смоук не прошёл → возврат на прежний коммит (сбой ОС под нагрузкой —
  после одного повтора, при нехватке сокетов Windows — через SOCKET_COOLDOWN сек); изменились только .md — переход без
  смоука и без перезапуска бота;
- раз в сутки: минуты GitHub Actions за месяц (приватные репозитории), предупреждение на 80% и 95% квоты;
- все уведомления дублируются в logs/launcher.log (без токена бота и паролей из ссылок);
- бот падает быстрее CRASH_WINDOW сек CRASH_LIMIT раза подряд → откат на последнюю рабочую версию;
- один launcher на папку (замок logs/launcher.lock, второй выходит с кодом 3); бот не переживает launcher:
  при любом выходе из run() он останавливается, а бот-сирота убитого извне launcher (pid в logs/bot.pid)
  завершается при следующем старте — иначе два экземпляра делят один токен.

Локальный барьер (решение владельца 2026-09-26; CI на GitHub запускает ci.yml из самой ветки, ему верить нельзя):
- обновление, меняющее защищённые пути (список PROTECTED_* зашит здесь, из репозитория не читается) в итоге или хотя
  бы в одном своём коммите, не ставится,
  пока владелец не подтвердит ровно этот коммит на ПК: python launcher.py --approve <sha> (sha → logs/approved_shas);
  до того работает прежняя версия, уведомление — один раз на sha;
- при каждом обновлении кода (не только .md) PAYOUTS и TRADING в .env переписываются в 0 — деньги включает снова
  только владелец на ПК: до merge (launcher, убитый посреди обновления, не оставит новый код с включёнными деньгами)
  и ещё раз после остановки старого бота (его save_env мог дописать .env поверх); .env не записать — обновление не
  ставится (прежняя версия работает дальше). Из окружения launcher бот получает PAYOUTS/TRADING только «0»:
  включить деньги может только .env;
- подтверждённое обновление launcher.py: launcher останавливает бота и выходит, run.bat через 15 с поднимает новый —
  иначе новые правила барьера работали бы только после ручного перезапуска.
"""
import importlib.metadata
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# Windows ищет git/python сначала в текущей папке (папка бота): git.exe из коммита запустился бы вместо настоящего.
# Переменная окружения это выключает — для launcher и всех процессов, которые он запускает
os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
LAST_GOOD = os.path.join(HERE, ".last_good")
DEV_STATUS = os.path.join(HERE, ".dev_status.json")   # для кнопки «🛠 Разработка» в боте
STABLE_AFTER = 600            # сек работы, после которых версия считается рабочей
CRASH_WINDOW, CRASH_LIMIT = 60, 3
MINUTES_EVERY = 24 * 3600     # как часто смотреть минуты GitHub Actions
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
          "декабрь")


LOG_PATH = os.path.join(HERE, "logs", "launcher.log")
# служебные файлы — в logs/ (уже в .gitignore)
PID_FILE = os.path.join(HERE, "logs", "bot.pid")
LOCK_PATH = os.path.join(HERE, "logs", "launcher.lock")
NO_PYTEST = "pytest не установлен и не ставится сам — выполните вручную: pip install -r requirements.txt"
TRANSIENT = ("WinError", "PermissionError", "OSError")   # в выводе смоука: сбой ОС под нагрузкой, а не ошибка кода
# кончились сокеты/буферы Windows (много процессов с сетью на ПК): сразу повторять бесполезно — пауза перед повтором
SOCKET_EXHAUSTED = ("WinError 10055", "WinError 10048", "No buffer space available", "ENOBUFS")
SOCKET_COOLDOWN = 90   # сек
_lock_fd = None   # дескриптор замка держим открытым до конца процесса

# Защищённые пути: без --approve владельца обновление с ними не ставится. Сравнение без учёта регистра (на Windows
# LAUNCHER.PY из коммита записался бы поверх launcher.py). Список — только здесь, а не в репозитории: иначе обновление
# могло бы само себя «разрешить». Кроме CI, guard, выплат и торгового кода — run.bat (cmd выполняет его, перечитывая
# на ходу) и локальное состояние: git merge молча перезаписывает игнорируемые файлы, если коммит добавит их в git
# (.env, data/ с ключами и белым списком, logs/ с approved_shas, .last_good). scripts/guard.py держит тот же список
# (tests/test_launcher_money_gate.py сверяет оба направления), чтобы CI не вливал то, что launcher потом не поставит.
PROTECTED_PREFIXES = (".github/", "launcher.py", "run.bat", "scripts/guard.py", "claude.md", ".gitignore",
                      ".gitattributes", "payouts.py", "scripts/payout_whitelist.py", "tests/trading/",
                      "tests/test_launcher_money_gate.py", "data/", "logs/")
PROTECTED_NAMES = ("payout", "trading", "__pycache__")   # подстрока в любом месте пути
PROTECTED_BASENAMES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "sitecustomize.py",
                       "usercustomize.py", "requirements.txt")   # на любой глубине
# исполняемое и то, что Windows/Python запустят мимо .py-исходника (git.exe в папке бота вызвался бы вместо git)
PROTECTED_SUFFIXES = (".pth", ".exe", ".dll", ".pyd", ".pyc", ".so", ".bat", ".cmd", ".ps1")
STDLIB_NAMES = frozenset(n.lower() for n in sys.stdlib_module_names)   # json.py/hashlib/ в корне — подмена модуля
# и установленные пакеты (pytest.py в корне заменил бы pytest в смоуке): importlib.metadata + запасной список; свои
# папки проекта — не подмена (на ПК есть пакет с мусорным модулем «tests»). Как в scripts/guard.py
INSTALLED_FALLBACK = ("pytest", "_pytest", "pluggy", "aiohttp", "pil", "iniconfig", "packaging")
OWN_ROOT = ("tests", "scripts", "research")
INSTALLED_TTL = 3600          # сек: список установленных пакетов перечитывается раз в час (pip install без перезапуска)
_installed = {"at": None, "names": frozenset()}
PROTECTED_EXACT = (".env", ".last_good", ".dev_status.json")
APPROVED_PATH = os.path.join(HERE, "logs", "approved_shas")   # sha, подтверждённые владельцем: по одному в строке
MONEY_KEYS = ("PAYOUTS", "TRADING")
MONEY_OFF_NOTE = ("💸 Выплаты/торговля выключены после обновления {sha}: проверь изменения и включи на ПК "
                  "(PAYOUTS=1 / TRADING=1 в .env)")
MONEY_OFF_AGAIN_NOTE = ("💸 Выплаты/торговля снова выключены перед запуском {sha}: их включили, пока шла проверка "
                        "обновления. Включи ещё раз на ПК, когда бот запустится.")
USAGE = ("Запуск: python launcher.py                 — бот с автообновлением (обычно через run.bat)\n"
         "        python launcher.py --approve <sha>  — подтвердить обновление с защищёнными файлами")


def _mask(text):
    """Токен бота из URL Telegram (/bot<токен>/…) и логин:пароль из ссылок — не в консоль и не в launcher.log."""
    return re.sub(r"/bot[^/\s'\"]*", "/bot***", re.sub(r"://[^/\s@]+@", "://***@", str(text)))


def log(msg):
    line = f"{time.strftime('%d.%m %H:%M:%S')} [launcher] {_mask(msg)}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:   # вывод не в UTF-8 (запуск не через run.bat): emoji из уведомлений — «?», а не падение
        enc = sys.stdout.encoding or "ascii"
        print(line.encode(enc, "replace").decode(enc), flush=True)
    try:   # журнал на диске: чтобы после зависания/падения было видно, где остановились
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def git(*args, check=True):
    try:   # без таймаута одна зависшая команда останавливает весь цикл обновлений
        r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)}: timeout")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def gh(*args):
    """gh <args> → stdout; ошибка или таймаут — RuntimeError. В тестах подменяется."""
    try:
        r = subprocess.run(["gh", *args], cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh {' '.join(args)}: timeout")
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def env_value(key, default=""):
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):   # комментарий после любого пробела, в том числе табуляции
                    return re.split(r"\s#", line.split("=", 1)[1], 1)[0].strip()
    except OSError:
        pass
    return default


def notify(text):
    # копия в launcher.log: откаты видно и без Telegram. Одной строкой и коротко; маска — до обрезки
    log("уведомление: " + _mask(" ⏎ ".join(text.strip().splitlines()))[:300])
    token, chat = env_value("TG_TOKEN"), env_value("TG_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # мимо системного прокси
    try:
        opener.open(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20)
    except Exception as e:   # текст ошибки не пишем: он может процитировать URL с токеном
        log(f"telegram: {type(e).__name__}" + (f" {e.code}" if isinstance(e, urllib.error.HTTPError) else ""))


def clean_tree():
    return git("status", "--porcelain", "--untracked-files=no") == ""


def installed_names():
    """Имена верхнего уровня установленных пакетов (нижний регистр) + INSTALLED_FALLBACK; раз в INSTALLED_TTL сек
    перечитываются (packages_distributions() читает метаданные всех пакетов — не на каждый путь)."""
    now = time.time()
    if _installed["at"] is None or now - _installed["at"] > INSTALLED_TTL:
        try:
            found = importlib.metadata.packages_distributions()
        except Exception:   # битые метаданные пакета — не повод пропустить проверку: остаётся запасной список
            found = {}
        names = frozenset(n.lower() for n in found if n.isidentifier()) | frozenset(INSTALLED_FALLBACK)
        _installed.update(at=now, names=names)
    return _installed["names"]


def shadows_module(low):
    """Файл или папка в корне с именем модуля stdlib или установленного пакета (json.py, hashlib/, pytest.py, PIL/;
    .pyw Windows тоже импортирует): Python возьмёт его вместо настоящего модуля. Как shadows_module в guard."""
    first = low.split("/", 1)[0]
    stem = next((first[:-len(ext)] for ext in (".py", ".pyw") if first.endswith(ext)), None)
    if stem is None:
        if "/" not in low:
            return False
        stem = first
    return stem in STDLIB_NAMES or stem not in OWN_ROOT and stem in installed_names()


def protected_paths(files):
    """Какие из путей (как их пишет git diff) защищены — см. PROTECTED_*."""
    out = []
    for f in files:
        low = f.lower()
        base = low.rsplit("/", 1)[-1]
        shadows = shadows_module(low)   # Python возьмёт его вместо модуля
        if (low.startswith(PROTECTED_PREFIXES) or any(n in low for n in PROTECTED_NAMES) or low in PROTECTED_EXACT
                or base in PROTECTED_BASENAMES or base.endswith(PROTECTED_SUFFIXES) or shadows):
            out.append(f)
    return out


def approved(sha):
    """Подтвердил ли владелец ровно этот коммит (python launcher.py --approve <sha>). Файла нет, не читается — нет."""
    try:
        with open(APPROVED_PATH, encoding="utf-8") as f:
            return sha.lower() in {line.strip().lower() for line in f}
    except (OSError, ValueError):
        return False


def money_off(env_path):
    """PAYOUTS и TRADING в .env → 0. Строки разбираются как в payouts.switch_from_file: каждая строка (кроме пустых и
    комментариев), где имя до «=» без учёта регистра — PAYOUTS или TRADING; значение — до « #». Строки со значением
    не «0» переписываются в «<имя как было>=0» (перевод строки тот же), остальные байты файла не меняются; запись —
    через временный файл и os.replace, чтобы обрыв не оставил полфайла. → True — что-то выключили; False — всё уже 0,
    таких строк или самого .env нет. Не прочитать или не записать — OSError (обновление тогда не ставится)."""
    try:
        with open(env_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return False
    lines, changed = raw.splitlines(keepends=True), False   # \n, \r\n и \r — как текстовый режим switch_from_file
    for i, line in enumerate(lines):
        body = line.rstrip(b"\r\n")
        text = body.decode("utf-8", "replace").strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        k, v = text.split("=", 1)
        if k.strip().lstrip("\ufeff").strip().upper() in MONEY_KEYS and v.split(" #")[0].strip() != "0":
            lines[i] = body[:body.index(b"=") + 1] + b"0" + line[len(body):]
            changed = True
    if not changed:
        return False
    tmp = env_path + ".money-off.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(b"".join(lines))
        os.replace(tmp, env_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


def approve(arg):
    """python launcher.py --approve <sha>: владелец проверил обновление с защищёнными файлами и разрешает ровно этот
    коммит. Полный sha (40 hex) или однозначное начало (от 4 символов, через git rev-parse) → строка в
    logs/approved_shas. Код выхода: 0 — записано, 2 — sha не принят."""
    sha = (arg or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        print("Нужен sha коммита: 40 шестнадцатеричных символов (или однозначное начало, от 4 символов). "
              "Пример: python launcher.py --approve 1a2b3c4d")
        return 2
    if len(sha) < 40:
        try:
            full = git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False).lower()
        except RuntimeError as e:
            full = ""
            log(e)
        if not re.fullmatch(r"[0-9a-f]{40}", full) or not full.startswith(sha):
            print(f"{sha}: такого коммита здесь нет или начало подходит к нескольким — нужен полный sha из уведомления.")
            return 2
        sha = full
    try:
        os.makedirs(os.path.dirname(APPROVED_PATH), exist_ok=True)
        with open(APPROVED_PATH, "a", encoding="utf-8") as f:
            f.write(sha + "\n")
    except OSError as e:
        print(f"Не смог записать {APPROVED_PATH}: {e}")
        return 2
    log(f"владелец подтвердил обновление {sha}")
    try:   # вершина main по последнему fetch работающего launcher — без сети
        tip = git("rev-parse", "--verify", "--quiet", "origin/main^{commit}", check=False).lower()
    except RuntimeError:
        tip = ""
    if re.fullmatch(r"[0-9a-f]{40}", tip) and tip != sha:
        # launcher ставит только вершину main, а подтверждение действует ровно на свой коммит
        print(f"Записал подтверждение {sha}, но main на GitHub уже на {tip} (по последней проверке launcher), а "
              f"launcher ставит только вершину main — этот коммит отдельно он не поставит. Проверь изменения до "
              f"вершины и подтверди её: python launcher.py --approve {tip}")
    else:
        print(f"Подтверждено: {sha}. Launcher поставит это обновление при следующей проверке (по умолчанию — "
              f"в течение 5 минут).")
    return 0


def repo_url():
    url = git("remote", "get-url", "origin", check=False)
    return url[:-4] if url.endswith(".git") else url


def roadmap_progress():
    """(сделано, всего, следующая задача) из раздела «Очередь» ROADMAP.md."""
    try:
        with open(os.path.join(HERE, "ROADMAP.md"), encoding="utf-8") as f:
            queue = f.read().split("## Очередь", 1)[-1].split("\n## ", 1)[0]
    except OSError:
        return 0, 0, ""
    items = re.findall(r"^- \[([ x~])\] (.+)$", queue, re.M)
    nxt = next((t for s, t in items if s == " "), "")
    return sum(1 for s, _ in items if s != " "), len(items), nxt.replace("`", "")[:120]


def write_dev_status():
    entries = []
    for line in git("log", "-8", "--pretty=format:%h%x09%cs%x09%s", check=False).splitlines():
        sha, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        entries.append({"sha": sha, "date": date, "subject": subject})
    status = {"version": git("rev-parse", "--short", "HEAD", check=False), "repo": repo_url(),
              "started_at": time.strftime("%d.%m %H:%M"), "log": entries}
    try:
        with open(DEV_STATUS, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False)
    except OSError as e:
        log(f"dev status: {e}")


def _run(args, timeout):
    """python -m <args> в папке бота; в тестах подменяется."""
    return subprocess.run([sys.executable, "-m", *args], cwd=HERE, capture_output=True, text=True, errors="replace",
                          timeout=timeout)


def smoke():
    """Компиляция и тесты новой версии до перезапуска бота. Без pytest или без единого теста — провал:
    иначе обновление считалось бы проверенным, хотя тесты не запускались.
    → (прошёл, хвост вывода, стоит ли повторить: сбой ОС под нагрузкой, а не ошибка в коде или таймаут)."""
    tmp = ""
    try:
        files = [f for f in os.listdir(HERE) if f.endswith(".py")]
        # своя --basetemp: общий pytest-of-<user> делят все pytest на ПК, чужой запуск чистит его под нами (WinError)
        tmp = tempfile.mkdtemp(prefix="p2p-smoke-")
        tests = ["pytest", "-q", "-x", "-p", "no:cacheprovider", f"--basetemp={tmp}"]
        r = _run(["py_compile", *files], 120)
        if r.returncode:   # SyntaxError — ошибка кода; PermissionError при записи .pyc под нагрузкой — сбой ОС
            return False, _tail(r.stderr), any(m in r.stderr for m in TRANSIENT)
        r = _run(tests, 600)
        if "No module named pytest" in r.stderr:   # ставим только сам pytest и повторяем один раз
            log("pytest не установлен — ставлю")
            p = _run(["pip", "install", "-q", "pytest"], 300)
            if p.returncode:
                return False, NO_PYTEST + "\n" + p.stderr[-300:], False
            r = _run(tests, 600)
    except subprocess.TimeoutExpired:
        return False, "смоук-тест не завершился за отведённое время", False
    except OSError as e:   # не запустился (WinError под нагрузкой) — провал попытки, а не исключение посреди обновления
        return False, f"смоук-тест не запустился: {e}", True
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    if "No module named pytest" in r.stderr:
        return False, NO_PYTEST, False
    if r.returncode == 5:
        return False, "pytest не нашёл ни одного теста (tests/ удалён или пуст?)", False
    out = r.stdout + r.stderr
    return r.returncode == 0, _tail(out), r.returncode != 0 and any(m in out for m in TRANSIENT)


def _tail(out):
    """Хвост вывода смоука (500 символов). Нехватка сокетов Windows где-то выше хвоста — её метка в начале: по ней
    try_update ждёт SOCKET_COOLDOWN перед повтором."""
    tail = out[-500:]
    lost = [m for m in SOCKET_EXHAUSTED if m in out and m not in tail]
    return (f"[{lost[0]}] " + tail) if lost and not any(m in tail for m in SOCKET_EXHAUSTED) else tail


def bot_env():
    """Окружение бота: всё окружение launcher, но PAYOUTS/TRADING (имя без учёта регистра) — только со значением «0».
    Включить деньги может только .env (его launcher выключает при каждом обновлении кода), а не переменная Windows
    или родительского процесса: иначе PAYOUTS=1 из окружения пережил бы PAYOUTS=0 в .env (load_env — setdefault),
    стоит обновлению убрать из bot.main вызов payouts.switch_from_file."""
    return {k: v for k, v in os.environ.items() if k.upper() not in MONEY_KEYS or v.strip() == "0"}


def start_bot():
    """Процесс бота; в тестах подменяется."""
    return subprocess.Popen([sys.executable, "bot.py"], cwd=HERE, env=bot_env())


def stop(proc):
    """Остановить бота: terminate, не вышел за 30 с — kill."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            log(f"бот pid {proc.pid} не завершился после kill")


def _proc_start(pid):
    """Время старта живого процесса pid (FILETIME, Windows API); None — процесса нет, он завершился или
    узнать нельзя. Пара «pid + время старта» однозначна: pid после смерти процесса достаётся другим."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # 259 = STILL_ACTIVE
            return None
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return (t[0].dwHighDateTime << 32) | t[0].dwLowDateTime
    finally:
        k.CloseHandle(h)


def write_pid(pid):
    """pid бота и время его старта — чтобы потом не убить чужой процесс, получивший тот же pid."""
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(f"{pid} {_proc_start(pid) or ''}".strip())
    except OSError as e:
        log(f"pid-файл: {e}")


def remove_pid():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def bot_alive(pid, start):
    """Жив ли ИМЕННО тот процесс: pid совпал и время старта то же. Чужой процесс с тем же pid — не наш."""
    return start is not None and _proc_start(pid) == start


def kill_orphan():
    """Бот, переживший прошлый launcher (убит из Диспетчера задач): завершить до запуска нового.
    Вызывать только под замком acquire_lock — иначе второй launcher убьёт бота первого.
    Убиваем, только если совпали pid и время старта из pid-файла; иначе файл устарел — просто удаляем."""
    try:
        with open(PID_FILE) as f:
            parts = f.read().split()
        pid = int(parts[0])
        start = int(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError, IndexError):
        remove_pid()
        return
    if pid != os.getpid() and bot_alive(pid, start):
        log(f"завершаю осиротевший бот pid {pid}")
        try:
            os.kill(pid, signal.SIGTERM)   # на Windows = TerminateProcess
        except OSError as e:
            log(f"не смог завершить pid {pid}: {e}")
        for _ in range(10):
            time.sleep(1)
            if not bot_alive(pid, start):
                break
        else:   # pid-файл оставляем: run.bat перезапустит launcher, и он попробует снова
            msg = f"осиротевший бот pid {pid} не завершился — второй не запускаю"
            notify(f"⚠️ Launcher: {msg}. Заверши его в Диспетчере задач.")
            raise RuntimeError(msg)
    remove_pid()


def acquire_lock():
    """Один launcher на папку (Windows): False — замок держит другой живой launcher.
    Замок снимает ОС, когда процесс умирает (даже убитый извне), поэтому устаревшего замка не бывает."""
    global _lock_fd
    if os.name != "nt":
        return True
    import msvcrt
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT)
    except OSError as e:   # файл не открыть (права, синхронизация) — работаем без замка, но пишем в лог
        log(f"замок launcher недоступен: {e}")
        return True
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd
    return True


class Launcher:
    def __init__(self):
        self.bad = set()          # коммиты, которые не прошли смоук или падали — не ставим повторно
        self.warned_dirty = False
        self.seen_prs = set()     # PR, о которых уже сообщили
        self.minutes_at = 0.0     # когда последний раз смотрели минуты GitHub Actions
        self.minutes_warned = ("", 0)   # (год-месяц, старший порог %, о котором уже предупредили)
        self.asked_approval = set()     # sha с защищёнными файлами, о которых владельцу уже написали
        self.money_failed = set()       # sha, не поставленные из-за незаписанного .env, — о них уже написали
        self.money_noted = set()        # sha, о выключении денег при которых уже написали
        self.pending = None             # (прежний HEAD, новый sha, файлы): поставлено, новый бот ещё не запущен

    def check_prs(self):
        """Открытые PR = автомерж не прошёл (тесты/guard) — сообщить один раз со ссылкой."""
        if not shutil.which("gh"):
            return
        try:
            r = subprocess.run(["gh", "pr", "list", "--state", "open", "--json", "number,title,url"], cwd=HERE,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        except subprocess.TimeoutExpired:
            log("gh pr list: timeout")
            return
        if r.returncode:
            return
        for pr in json.loads(r.stdout or "[]"):
            if pr["number"] not in self.seen_prs:
                self.seen_prs.add(pr["number"])
                notify(f"👀 PR #{pr['number']} ждёт ручной проверки (тесты или guard не пропустили автомерж):\n"
                       f"{pr['title']}\n{pr['url']}")

    def check_minutes(self):
        """Минуты GitHub Actions за месяц: кончатся — встанут CI и автомерж. Предупредить на 80% и на 95% квоты,
        каждый порог — раз в месяц. Любая ошибка (у gh нет прав на billing, сеть) — только в лог."""
        if not shutil.which("gh"):
            return
        try:
            year, month = map(int, time.strftime("%Y %m").split())
            login = gh("api", "user", "--jq", ".login")
            # квоту тратят только приватные репозитории владельца: в публичных Actions бесплатны
            private = set(gh("api", "--paginate", "user/repos?visibility=private&affiliation=owner&per_page=100",
                             "--jq", ".[].name").split())
            usage = json.loads(gh("api", f"users/{login}/settings/billing/usage?year={year}&month={month}"))
            used = 0
            for i in usage.get("usageItems") or []:
                sku = str(i.get("sku")).lower()
                if (str(i.get("product")).lower() == "actions" and str(i.get("unitType")).lower() == "minutes"
                        and str(i.get("repositoryName")).split("/")[-1] in private):   # "owner/repo" или "repo"
                    # минуты Windows списываются из квоты ×2, macOS ×10
                    used += (i.get("quantity") or 0) * (10 if "macos" in sku else 2 if "windows" in sku else 1)
            free = int(env_value("GH_ACTIONS_FREE_MIN", "2000") or 2000)
            pct = used * 100 / free
        except Exception as e:
            log(f"минуты GitHub Actions: {type(e).__name__}: {e}")
            return
        log(f"минуты GitHub Actions (приватные репозитории): {used:.0f} из {free} ({int(pct)}%)")
        level = 95 if pct >= 95 else 80 if pct >= 80 else 0
        key = f"{year}-{month:02d}"
        if level > (self.minutes_warned[1] if self.minutes_warned[0] == key else 0):
            self.minutes_warned = (key, level)
            notify(f"⚠️ GitHub Actions: израсходовано {used:.0f} из {free} мин за {MONTHS[month - 1]} ({int(pct)}%). "
                   "Когда минуты кончатся, CI и автомерж остановятся до 1-го числа.")

    def try_update(self):
        """True — код обновлён, бота нужно перезапустить."""
        try:
            git("fetch", "--quiet", "origin", "main")
            head, remote = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
        except RuntimeError as e:
            log(e)
            return False
        if remote in self.bad or git("rev-list", "--count", f"{head}..{remote}") == "0":
            return False
        if not clean_tree():
            if not self.warned_dirty:
                notify("⚠️ Есть обновление бота, но в папке локальные изменения — пропускаю, пока их не закоммитят.")
                self.warned_dirty = True
            return False
        self.warned_dirty = False
        changes = git("log", "--pretty=format:• %s", f"{head}..{remote}", check=False).splitlines()[:10]
        # --no-renames: иначе helper.py → HELPER.md выглядит как «изменён только .md»; -z — пути без кавычек.
        # Защищённые пути — по КАЖДОМУ коммиту диапазона, а не только по итогу: коммит, который ослабил ci.yml или
        # guard и следующим коммитом вернул их как было, в итоговом diff не виден, а автомерж между ними уже прошёл по
        # ослабленным правилам (-m — и слияния: их правка против каждого родителя).
        # Без списка файлов не узнать, тронуты ли защищённые пути, — тогда не ставим (до следующей проверки)
        try:
            files = [f for f in git("diff", "--no-renames", "--name-only", "-z", head, remote).split("\0") if f]
            touched = [f for f in git("log", "--no-renames", "-m", "--name-only", "-z", "--format=",
                                      f"{head}..{remote}").split("\0") if f.strip()]
        except RuntimeError as e:
            log(e)
            return False
        blocked = protected_paths(list(dict.fromkeys(files + [f.strip("\n") for f in touched])))
        if blocked and not approved(remote):   # защищённые пути — только после --approve ровно этого sha на ПК
            if remote not in self.asked_approval:
                self.asked_approval.add(remote)
                shown = "\n".join(f"• {f}" for f in blocked[:15]) + (
                    f"\n… и ещё {len(blocked) - 15}" if len(blocked) > 15 else "")
                notify(f"🔐 Обновление {remote[:7]} меняет защищённые файлы — без твоего подтверждения не ставлю, "
                       f"работает прежняя версия {head[:7]}:\n{shown}\n\nПроверь изменения (git diff {head[:7]} "
                       f"{remote[:7]}) и подтверди на ПК:\npython launcher.py --approve {remote}")
            return False
        docs_only = bool(files) and all(f.endswith(".md") for f in files)
        try:   # не fast-forward (в папке свой коммит) — merge не пройдёт: не ставим и деньги не трогаем
            git("merge-base", "--is-ancestor", head, remote)
        except RuntimeError:
            notify(f"⚠️ Не смог обновиться до {remote[:7]} (ветки разошлись): в папке бота есть коммиты, которых нет "
                   f"в main, — обновление не ставлю, работает {head[:7]}. Проверь на ПК: git log {remote[:7]}..{head[:7]}")
            self.bad.add(remote)
            return False
        # деньги — до merge: launcher, убитый между merge и концом смоука, при следующем старте запустил бы новый код
        # (HEAD уже равен origin/main — обновлять «нечего») с включёнными деньгами. Не прошёл смоук — деньги всё равно
        # выключены, это безопасная сторона. .env не записать — не ставим вовсе
        if not docs_only and not self.money_off_for(head, remote):
            return False
        try:   # ровно на проверенный sha: pull сходил бы в origin заново и мог принести ещё не проверенный коммит
            git("merge", "--ff-only", "--quiet", remote)
        except RuntimeError as e:
            notify(f"⚠️ Не смог обновиться до {remote[:7]} (ветки разошлись или файлы заняты), работает {head[:7]}: "
                   f"{str(e).split(': ', 1)[-1] or 'git merge без подробностей'}")
            self.bad.add(remote)
            return False
        if docs_only:   # код не менялся: ни смоука, ни перезапуска бота
            log(f"только документация ({remote[:7]}): {', '.join(files)[:200]}")
            try:   # бот работает дальше: рабочая версия — новая, иначе откат при падениях «вернул» бы тот же код
                   # со старыми .md и забраковал бы этот коммит
                if os.path.exists(LAST_GOOD) and open(LAST_GOOD).read().strip() == head:
                    with open(LAST_GOOD, "w") as f:
                        f.write(remote)
            except OSError as e:
                log(f"last_good: {e}")
            notify(f"📝 Обновлена документация ({remote[:7]}), бот не перезапускал")
            write_dev_status()   # кнопка «🛠 Разработка» — с новой версией и списком коммитов
            return False
        ok, err, transient = smoke()
        if not ok and transient:   # сбой ОС под нагрузкой (WinError) — повтор, а не брак хорошего коммита навсегда
            cooldown = any(m in err for m in SOCKET_EXHAUSTED)   # кончились сокеты — сразу повторять бесполезно
            log(f"смоук {remote[:7]}: сбой ОС, повторяю" + (f" через {SOCKET_COOLDOWN} с (нет свободных сокетов)"
                                                            if cooldown else "")
                + ": " + " ⏎ ".join(err.strip().splitlines())[-300:])
            if cooldown:
                time.sleep(SOCKET_COOLDOWN)
            ok, err, _ = smoke()
        if not ok:
            git("reset", "--hard", head)
            self.bad.add(remote)
            notify(f"⚠️ Обновление {remote[:7]} не прошло проверку — оставил {head[:7]}.\n{err}")
            return False
        done, total, nxt = roadmap_progress()
        notify(f"🔄 Бот обновился до {remote[:7]}\n\nЧто нового:\n" + "\n".join(changes)
               + (f"\n\n📋 ROADMAP: сделано {done} из {total}" if total else "")
               + (f"\n➡️ Дальше: {nxt}" if nxt else "")
               + f"\n\n{repo_url()}/commits/main"
               + ("\n\n⚠️ Изменён run.bat — закрой окно launcher и запусти run.bat заново вручную: cmd читает его с "
                  "диска по ходу выполнения, сам launcher его безопасно не перезапустит."
                  if any(f.lower() == "run.bat" for f in files) else ""))
        self.pending = (head, remote, files)   # остальное — в after_update, когда старый бот уже остановлен
        return True

    def money_off_for(self, head, remote, again=False):
        """PAYOUTS/TRADING → 0 перед новым кодом remote. True — выключены (или уже были выключены); False — .env не
        записать: новый код не ставить. Сообщение владельцу — одно на sha и то и другое; again — повторное выключение
        перед самым запуском: если что-то снова пришлось выключить (включили, пока шла проверка), — отдельная строка,
        иначе владелец считал бы выплаты включёнными."""
        try:
            changed = money_off(os.path.join(HERE, ".env"))
            if changed and remote not in self.money_noted:
                self.money_noted.add(remote)
                notify(MONEY_OFF_NOTE.format(sha=remote[:7]))
            elif changed and again:
                notify(MONEY_OFF_AGAIN_NOTE.format(sha=remote[:7]))
            return True
        except OSError as e:
            if remote not in self.money_failed:
                self.money_failed.add(remote)
                notify(f"🚨 Не смог выключить выплаты/торговлю в .env ({type(e).__name__}: {e}) — обновление "
                       f"{remote[:7]} НЕ ставлю, работает прежняя версия {head[:7]}. Проверь, что .env не занят "
                       f"и доступен для записи; попробую снова при следующей проверке.")
            else:
                log(f"{remote[:7]}: .env по-прежнему не записать ({type(e).__name__}) — обновление не ставлю")
            return False

    def after_update(self):
        """Обновление поставлено, старый бот уже остановлен, новый ещё не запущен. Деньги — ещё раз в 0: пока шёл
        смоук, старый бот мог дописать .env своим save_env (прочитал файл до money_off) и вернуть PAYOUTS=1. .env не
        записать — откат на прежний коммит: новый код с включёнными деньгами не стартует (в bad не кладём — починят
        .env, поставим при следующей проверке). → True — обновился launcher.py: выйти из run(), run.bat поднимет
        новый launcher, иначе новые правила барьера работали бы только после ручного перезапуска."""
        if not self.pending:
            return False
        head, remote, files = self.pending
        self.pending = None
        if not self.money_off_for(head, remote, again=True):
            git("reset", "--hard", head)
            return False
        if any(f.lower() == "launcher.py" for f in files):
            notify(f"🔁 launcher.py обновлён ({remote[:7]}) — перезапускаю launcher: run.bat поднимет новый через 15 с "
                   "(если launcher запущен не через run.bat — запусти python launcher.py вручную).")
            return True
        return False

    def rollback(self):
        head = git("rev-parse", "HEAD")
        good = open(LAST_GOOD).read().strip() if os.path.exists(LAST_GOOD) else ""
        if good and good != head and clean_tree():
            # откат — тоже смена кода под деньгами: как и обновление, только с выключенными выплатами/торговлей;
            # .env не записать — не откатываем (падающий бот денег не шлёт, а старый код с включёнными — мог бы)
            if not self.money_off_for(head, good):
                return
            git("reset", "--hard", good)
            self.bad.add(head)
            notify(f"⚠️ Бот падал {CRASH_LIMIT} раза подряд на {head[:7]} — откатил на рабочую {good[:7]}.")
        else:
            notify("⚠️ Бот падает при запуске, откатиться некуда — нужна ручная проверка.")

    def run(self):
        every = int(env_value("UPDATE_EVERY", "300"))
        kill_orphan()
        try:
            updated = self.try_update()
        except Exception as e:
            log(f"ошибка первой проверки: {type(e).__name__}: {e}")
            updated = False
        crashes, last_check = 0, time.time()
        while True:
            if updated:   # старый бот остановлен (или ещё не запускался): деньги ещё раз в 0, обновился ли launcher
                updated = False
                try:
                    if self.after_update():
                        log("launcher.py обновлён — выхожу, run.bat запустит новый launcher")
                        return
                except Exception as e:   # откат не удался — не запускать бота на непроверенном состоянии .env
                    log(f"ошибка после обновления: {type(e).__name__}: {e}")
                    raise
            started = time.time()
            write_dev_status()
            try:   # версию — до запуска: таймаут git после Popen оставил бы бота без присмотра
                ver = git("rev-parse", "--short", "HEAD", check=False)
            except RuntimeError:
                ver = "?"
            proc = start_bot()
            try:   # любой выход из run() (исключение, Ctrl+C) останавливает бота — иначе run.bat запустит второго
                write_pid(proc.pid)
                log(f"бот запущен (pid {proc.pid}, версия {ver})")
                good_marked, updated = False, False
                while proc.poll() is None:
                    time.sleep(5)
                    if not good_marked and time.time() - started > STABLE_AFTER:
                        try:   # одна попытка на запуск: ошибка записи не повод бросать бота
                            head = git("rev-parse", "HEAD")
                            with open(LAST_GOOD, "w") as f:
                                f.write(head)
                        except (OSError, RuntimeError) as e:
                            log(f"last_good: {e}")
                        good_marked, crashes = True, 0
                    if time.time() - last_check > every:
                        last_check = time.time()
                        log("проверка обновлений")
                        try:
                            self.check_prs()
                            if time.time() - self.minutes_at > MINUTES_EVERY:
                                self.minutes_at = time.time()
                                self.check_minutes()
                            updated = self.try_update()
                        except Exception as e:   # любая неожиданная ошибка не должна убивать цикл
                            log(f"ошибка проверки: {type(e).__name__}: {e}")
                            updated = False
                        if updated:
                            break   # бота остановит finally
            finally:
                stop(proc)
                remove_pid()
            if updated:
                continue
            uptime = time.time() - started
            log(f"бот завершился (код {proc.returncode}) через {uptime:.0f} с")
            crashes = crashes + 1 if uptime < CRASH_WINDOW else 0
            if crashes >= CRASH_LIMIT:
                try:
                    self.rollback()
                except Exception as e:
                    log(f"ошибка отката: {type(e).__name__}: {e}")
                crashes = 0
            time.sleep(15)


def main(argv):
    if argv:   # только --approve <sha> или --approve=<sha>; опечатка не должна молча запускать второй цикл и бота
        flag, eq, value = argv[0].partition("=")
        if flag == "--approve" and len(argv) <= (1 if eq else 2):   # записать sha и выйти, цикл не запускается
            return approve(value if eq else (argv[1] if len(argv) > 1 else ""))
        print(USAGE)
        return 2
    if not acquire_lock():   # замок — до kill_orphan в run(): второй launcher не должен трогать бота первого
        log("launcher уже запущен в другом окне — выхожу")
        return 3
    Launcher().run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
