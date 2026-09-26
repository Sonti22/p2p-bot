"""Защита автомержа: проверяет изменения ветки против базы.

Код выхода 1 = автомерж запрещён, нужна ручная проверка. Защищённый файл: правится только вручную.
Запуск: python scripts/guard.py [база]   (по умолчанию origin/main)
"""
import re
import subprocess
import sys

# Сравнение путей — без учёта регистра: на Windows LAUNCHER.PY из коммита записался бы поверх launcher.py.
# Список совпадает со списком launcher.py (PROTECTED_* там): что launcher без --approve не поставит, то и сюда —
# иначе CI вливал бы коммит, а launcher стоял бы на нём до ручного подтверждения.
PROTECTED = (".github/", "scripts/guard.py", "launcher.py", "CLAUDE.md", ".gitignore", ".gitattributes",
             "payouts.py", "scripts/payout_whitelist.py", "tests/test_payouts.py",   # выплаты — только вручную
             "tests/trading/",                      # торговля (этап 3 плана) — тоже только вручную
             # настройки pytest: через addopts/--ignore тесты выплат молча выпадали бы из CI
             "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py", "tests/conftest.py",
             "tests/test_launcher_money_gate.py",   # тесты барьера launcher: деньги выкл. после обновления, --approve
             "run.bat",                             # его выполняет cmd на ПК владельца, перечитывая файл на ходу
             # локальное состояние: git merge молча перезапишет игнорируемый файл, если коммит добавит его в git
             "data/", "logs/")
PROTECTED_EXACT = (".env", ".last_good", ".dev_status.json")   # ровно эти пути (.env.example — нет)
PROTECTED_NAMES = ("payout", "trading")   # любой путь с этим словом (payouts/__init__.py, paper_trading.py…) — защищён
# по имени файла на любой глубине (tests/sub/conftest.py, pkg/pyproject.toml…): настройки pytest и файлы, которые Python
# выполняет сам при старте (sitecustomize/usercustomize, *.pth), зависимости — тоже только вручную
PROTECTED_BASENAMES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "sitecustomize.py",
                       "usercustomize.py", "requirements.txt")
PROTECTED_SUFFIXES = (".pth",)
# код выплат в остальных файлах (bot.py: /payout, кнопки pay_*, PAYOUTS; accounts.py, .env.example…): любая добавленная
# или удалённая строка — ручная проверка владельца. Документацию (.md) не проверяем.
PAYOUT_CODE = r"payout|\bpay_(?:to|ok|no|hist|stop)\b"
ALLOWED_DOMAINS = ("bybit.com", "mexc.com", "htx.com", "kucoin.com", "bitpapa.com", "bestchange.ru",
                   "rapira.net", "telegram.org", "t.me", "lbank.com", "bingx.com", "cryptomus.com")
FORBIDDEN = (r"\bsubprocess\b", r"\bos\.system\b", r"\bos\.popen\b", r"\beval\(", r"\bexec\(", r"captcha",
             r"selenium", r"playwright", r"pyautogui", r"pywinauto", r"\badb\b", r"uiautomator")
SECRETS = (r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"\bsk-[A-Za-z0-9-]{20,}")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout


def protected(path):
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    return (low.startswith(tuple(p.lower() for p in PROTECTED)) or low in PROTECTED_EXACT
            or any(n in low for n in PROTECTED_NAMES) or base in PROTECTED_BASENAMES
            or base.endswith(PROTECTED_SUFFIXES))


GIT_RAW = ("-c", "core.quotepath=false", "--literal-pathspecs")   # пути как есть: не-ASCII без кавычек, «:(…)» не магия


def changed_files(base):
    """Изменённые пути ветки против базы: -z (пробелы, кавычки, не-ASCII — как есть), --no-renames — переименование
    = удаление старого пути + новый файл целиком, иначе «payouts.py → payouts/__init__.py» проходил бы как «ок»."""
    return [f for f in git(*GIT_RAW, "diff", "--name-only", "-z", "--no-renames", f"{base}...HEAD").split("\0") if f]


def check(base):
    problems = []
    files = changed_files(base)
    for f in files:
        if protected(f):
            problems.append(f"изменён защищённый файл: {f}")
    payout_lines = {}
    for name in files:
        if name.startswith("tests/fixtures/"):
            continue
        # по одному файлу: имя берём из -z списка, а не из заголовков «+++ b/…» (там кавычки и \t у путей с пробелом)
        diff = git(*GIT_RAW, "diff", "-U0", "--no-renames", f"{base}...HEAD", "--", name).splitlines()
        hunk = False
        for line in diff:
            if line.startswith("@@"):
                hunk = True
                continue
            if not hunk or not line.startswith(("+", "-")):
                continue
            if not name.endswith(".md") and not protected(name) and re.search(PAYOUT_CODE, line[1:], re.I):
                payout_lines[name] = payout_lines.get(name, 0) + 1
            if not line.startswith("+"):
                continue
            text = line[1:]
            if not name.endswith(".md"):   # ссылки в документации не проверяем
                for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text):
                    if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
                        problems.append(f"{name}: новый домен {host}")
            if name.lower().endswith(".py"):
                for pat in FORBIDDEN:
                    if re.search(pat, text, re.I):
                        problems.append(f"{name}: запрещено /{pat}/: {text.strip()[:80]}")
            for pat in SECRETS:
                if re.search(pat, text):
                    problems.append(f"{name}: похоже на секрет")
    for name, n in payout_lines.items():
        problems.append(f"{name}: изменён код выплат ({n} стр.) — только ручная проверка владельца")
    return problems


if __name__ == "__main__":
    found = check(sys.argv[1] if len(sys.argv) > 1 else "origin/main")
    for p in found:
        print("GUARD:", p)
    print("guard: нужна ручная проверка" if found else "guard: ок")
    sys.exit(1 if found else 0)
