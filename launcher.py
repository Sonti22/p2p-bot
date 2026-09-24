"""Запускает бота, сам подтягивает обновления из GitHub и откатывается, если новая версия падает.

Запуск: python launcher.py (через run.bat). Защищённый файл: правится только вручную, не облачным Claude.
- каждые UPDATE_EVERY сек: git fetch; есть новое в origin/main и папка чистая → pull → смоук-тест →
  перезапуск бота и сообщение в Telegram; смоук не прошёл → возврат на прежний коммит;
- бот падает быстрее CRASH_WINDOW сек CRASH_LIMIT раза подряд → откат на последнюю рабочую версию.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_GOOD = os.path.join(HERE, ".last_good")
DEV_STATUS = os.path.join(HERE, ".dev_status.json")   # для кнопки «🛠 Разработка» в боте
STABLE_AFTER = 600            # сек работы, после которых версия считается рабочей
CRASH_WINDOW, CRASH_LIMIT = 60, 3


def log(msg):
    print(time.strftime("%d.%m %H:%M:%S"), "[launcher]", msg, flush=True)


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def env_value(key, default=""):
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].split(" #")[0].strip()
    except OSError:
        pass
    return default


def notify(text):
    token, chat = env_value("TG_TOKEN"), env_value("TG_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # мимо системного прокси
    try:
        opener.open(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20)
    except Exception as e:
        log(f"telegram: {e}")


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


def smoke():
    """Компиляция и тесты новой версии до перезапуска бота."""
    py = [sys.executable, "-m"]
    files = [f for f in os.listdir(HERE) if f.endswith(".py")]
    r = subprocess.run(py + ["py_compile", *files], cwd=HERE, capture_output=True, text=True, errors="replace")
    if r.returncode:
        return False, r.stderr[-500:]
    r = subprocess.run(py + ["pytest", "-q", "-x"], cwd=HERE, capture_output=True, text=True, errors="replace")
    if "No module named pytest" in r.stderr:
        return True, ""
    return r.returncode in (0, 5), (r.stdout + r.stderr)[-500:]   # 5 = тестов нет


class Launcher:
    def __init__(self):
        self.bad = set()          # коммиты, которые не прошли смоук или падали — не ставим повторно
        self.warned_dirty = False
        self.seen_prs = set()     # PR, о которых уже сообщили

    def check_prs(self):
        """Открытые PR = автомерж не прошёл (тесты/guard) — сообщить один раз со ссылкой."""
        if not shutil.which("gh"):
            return
        r = subprocess.run(["gh", "pr", "list", "--state", "open", "--json", "number,title,url"], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            return
        for pr in json.loads(r.stdout or "[]"):
            if pr["number"] not in self.seen_prs:
                self.seen_prs.add(pr["number"])
                notify(f"👀 PR #{pr['number']} ждёт ручной проверки (тесты или guard не пропустили автомерж):\n"
                       f"{pr['title']}\n{pr['url']}")

    def try_update(self):
        """True — код обновлён, бота нужно перезапустить."""
        try:
            git("fetch", "--quiet", "origin", "main")
            head, remote = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
        except RuntimeError as e:
            log(e)
            return False
        if remote in self.bad or git("rev-list", "--count", "HEAD..origin/main") == "0":
            return False
        if not clean_tree():
            if not self.warned_dirty:
                notify("⚠️ Есть обновление бота, но в папке локальные изменения — пропускаю, пока их не закоммитят.")
                self.warned_dirty = True
            return False
        self.warned_dirty = False
        changes = git("log", "--pretty=format:• %s", "HEAD..origin/main", check=False).splitlines()[:10]
        try:
            git("pull", "--ff-only", "--quiet", "origin", "main")
        except RuntimeError as e:
            notify(f"⚠️ Не смог обновиться (ветки разошлись): {e}")
            self.bad.add(remote)
            return False
        ok, err = smoke()
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
        self.try_update()
        crashes, last_check = 0, time.time()
        while True:
            started = time.time()
            write_dev_status()
            proc = subprocess.Popen([sys.executable, "bot.py"], cwd=HERE)
            log(f"бот запущен (pid {proc.pid}, версия {git('rev-parse', '--short', 'HEAD')})")
            good_marked, updated = False, False
            while proc.poll() is None:
                time.sleep(5)
                if not good_marked and time.time() - started > STABLE_AFTER:
                    with open(LAST_GOOD, "w") as f:
                        f.write(git("rev-parse", "HEAD"))
                    good_marked, crashes = True, 0
                if time.time() - last_check > every:
                    last_check = time.time()
                    self.check_prs()
                    if self.try_update():
                        proc.terminate()
                        try:
                            proc.wait(30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        updated = True
            if updated:
                continue
            uptime = time.time() - started
            log(f"бот завершился (код {proc.returncode}) через {uptime:.0f} с")
            crashes = crashes + 1 if uptime < CRASH_WINDOW else 0
            if crashes >= CRASH_LIMIT:
                self.rollback()
                crashes = 0
            time.sleep(15)


if __name__ == "__main__":
    Launcher().run()
