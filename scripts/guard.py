"""Защита автомержа: проверяет изменения ветки против базы.

Код выхода 1 = автомерж запрещён, нужна ручная проверка. Защищённый файл: правится только вручную.
Запуск: python scripts/guard.py [база]   (по умолчанию origin/main)
"""
import re
import subprocess
import sys

PROTECTED = (".github/", "scripts/guard.py", "launcher.py", "CLAUDE.md", ".gitignore")
ALLOWED_DOMAINS = ("bybit.com", "mexc.com", "htx.com", "kucoin.com", "bitpapa.com", "bestchange.ru",
                   "rapira.net", "telegram.org", "t.me", "lbank.com", "bingx.com", "cryptomus.com")
FORBIDDEN = (r"\bsubprocess\b", r"\bos\.system\b", r"\bos\.popen\b", r"\beval\(", r"\bexec\(", r"captcha",
             r"selenium", r"playwright", r"pyautogui", r"pywinauto", r"\badb\b", r"uiautomator")
SECRETS = (r"\b\d{8,10}:[A-Za-z0-9_-]{30,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"\bsk-[A-Za-z0-9-]{20,}")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout


def check(base):
    problems = []
    for f in filter(None, git("diff", "--name-only", f"{base}...HEAD").splitlines()):
        if f.startswith(PROTECTED):
            problems.append(f"изменён защищённый файл: {f}")
    cur = ""
    for line in git("diff", "-U0", f"{base}...HEAD", "--", ".", ":(exclude)tests/fixtures").splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else ""
            continue
        if not line.startswith("+"):
            continue
        text = line[1:]
        if not cur.endswith(".md"):   # ссылки в документации не проверяем
            for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text):
                if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
                    problems.append(f"{cur}: новый домен {host}")
        if cur.endswith(".py"):
            for pat in FORBIDDEN:
                if re.search(pat, text, re.I):
                    problems.append(f"{cur}: запрещено /{pat}/: {text.strip()[:80]}")
        for pat in SECRETS:
            if re.search(pat, text):
                problems.append(f"{cur}: похоже на секрет")
    return problems


if __name__ == "__main__":
    found = check(sys.argv[1] if len(sys.argv) > 1 else "origin/main")
    for p in found:
        print("GUARD:", p)
    print("guard: нужна ручная проверка" if found else "guard: ок")
    sys.exit(1 if found else 0)
