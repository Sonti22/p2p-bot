"""Запускает бота, сам подтягивает обновления из GitHub и откатывается, если новая версия падает.

Запуск: python launcher.py (через run.bat). Защищённый файл: правится только вручную, не облачным Claude.
- каждые UPDATE_EVERY сек: git fetch; есть новое в origin/main и папка чистая → merge --ff-only ровно на
  полученный sha → смоук-тест
  (компиляция + pytest; нет pytest — ставит его, не вышло или тестов нет — провал) →
  перезапуск бота и сообщение в Telegram; смоук не прошёл → возврат на прежний коммит (сбой ОС под нагрузкой —
  после одного повтора); изменились только .md — переход без смоука и без перезапуска бота;
- раз в сутки: минуты GitHub Actions за месяц (приватные репозитории), предупреждение на 80% и 95% квоты;
- все уведомления дублируются в logs/launcher.log (без токена бота и паролей из ссылок);
- бот падает быстрее CRASH_WINDOW сек CRASH_LIMIT раза подряд → откат на последнюю рабочую версию;
- один launcher на папку (замок logs/launcher.lock, второй выходит с кодом 3); бот не переживает launcher:
  при любом выходе из run() он останавливается, а бот-сирота убитого извне launcher (pid в logs/bot.pid)
  завершается при следующем старте — иначе два экземпляра делят один токен.
"""
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
_lock_fd = None   # дескриптор замка держим открытым до конца процесса


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
            return False, r.stderr[-500:], any(m in r.stderr for m in TRANSIENT)
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
    return r.returncode == 0, out[-500:], r.returncode != 0 and any(m in out for m in TRANSIENT)


def start_bot():
    """Процесс бота; в тестах подменяется."""
    return subprocess.Popen([sys.executable, "bot.py"], cwd=HERE)


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
        # --no-renames: иначе helper.py → HELPER.md выглядит как «изменён только .md»; -z — пути без кавычек
        files = git("diff", "--no-renames", "--name-only", "-z", head, remote, check=False).split("\0")
        files = [f for f in files if f]
        try:   # ровно на проверенный sha: pull сходил бы в origin заново и мог принести ещё не проверенный коммит
            git("merge", "--ff-only", "--quiet", remote)
        except RuntimeError as e:
            notify(f"⚠️ Не смог обновиться (ветки разошлись): {e}")
            self.bad.add(remote)
            return False
        if files and all(f.endswith(".md") for f in files):   # код не менялся: ни смоука, ни перезапуска бота
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
            log(f"смоук {remote[:7]}: сбой ОС, повторяю: " + " ⏎ ".join(err.strip().splitlines())[-300:])
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
               + f"\n\n{repo_url()}/commits/main")
        return True

    def rollback(self):
        head = git("rev-parse", "HEAD")
        good = open(LAST_GOOD).read().strip() if os.path.exists(LAST_GOOD) else ""
        if good and good != head and clean_tree():
            git("reset", "--hard", good)
            self.bad.add(head)
            notify(f"⚠️ Бот падал {CRASH_LIMIT} раза подряд на {head[:7]} — откатил на рабочую {good[:7]}.")
        else:
            notify("⚠️ Бот падает при запуске, откатиться некуда — нужна ручная проверка.")

    def run(self):
        every = int(env_value("UPDATE_EVERY", "300"))
        kill_orphan()
        try:
            self.try_update()
        except Exception as e:
            log(f"ошибка первой проверки: {type(e).__name__}: {e}")
        crashes, last_check = 0, time.time()
        while True:
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


if __name__ == "__main__":
    if not acquire_lock():   # замок — до kill_orphan в run(): второй launcher не должен трогать бота первого
        log("launcher уже запущен в другом окне — выхожу")
        sys.exit(3)
    Launcher().run()
