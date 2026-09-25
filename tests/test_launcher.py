"""launcher.py: смоук не считает «тесты не запускались» успехом; бот не переживает launcher, второй launcher не стартует.

Настоящие процессы, git, gh, pip и Telegram не трогаем: подменяются _run, start_bot, _proc_start, git, gh, notify.
"""
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
from types import SimpleNamespace

import pytest

import launcher

PYTEST = ["pytest", "-q", "-x", "-p", "no:cacheprovider"]
PIP = ["pip", "install", "-q", "pytest"]
NO_MOD = "python.exe: No module named pytest"


class R:
    """Ответ _run: код выхода и вывод."""

    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def fake_run(monkeypatch, tmp_path, *answers):
    """_run отвечает по очереди (ожидаемые аргументы, ответ); лишний или чужой вызов — падение теста."""
    (tmp_path / "ok.py").write_text("x = 1\n")
    monkeypatch.setattr(launcher, "HERE", str(tmp_path))
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))
    calls, queue = [], list(answers)

    def run(args, timeout):
        calls.append(args)
        expected, answer = queue.pop(0)
        assert args[:len(expected)] == expected, args
        return answer

    monkeypatch.setattr(launcher, "_run", run)
    return calls


def test_smoke_compile_error_stops_before_tests(monkeypatch, tmp_path):
    calls = fake_run(monkeypatch, tmp_path, (["py_compile"], R(1, err="SyntaxError: bad")))
    assert launcher.smoke() == (False, "SyntaxError: bad", False)   # ошибка кода — без повтора
    assert calls == [["py_compile", "ok.py"]]


def test_smoke_passes_and_fails_by_pytest(monkeypatch, tmp_path):
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(0, "5 passed")))
    assert launcher.smoke() == (True, "5 passed", False)
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, "FAILED tests/test_x.py::test_y")))
    ok, text, retry = launcher.smoke()
    assert not ok and "FAILED tests/test_x.py" in text and not retry


def test_smoke_no_tests_is_failure(monkeypatch, tmp_path):
    """Код 5 (коммит стёр tests/) раньше считался успехом."""
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(5, "no tests ran in 0.00s")))
    ok, text, retry = launcher.smoke()
    assert not ok and "ни одного теста" in text and not retry


def test_smoke_installs_missing_pytest_and_reruns(monkeypatch, tmp_path):
    calls = fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, err=NO_MOD)), (PIP, R()),
                     (PYTEST, R(0, "5 passed")))
    assert launcher.smoke() == (True, "5 passed", False)
    assert calls[2] == PIP and sum(c[:len(PYTEST)] == PYTEST for c in calls) == 2


def test_smoke_pip_failure_is_failure(monkeypatch, tmp_path):
    """Без pytest раньше возвращалось (True, "") — обновление «проверено» без единого теста."""
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, err=NO_MOD)),
             (PIP, R(1, err="ERROR: Could not install packages")))
    ok, text, retry = launcher.smoke()
    assert not ok and "pip install -r requirements.txt" in text and "Could not install" in text and not retry


def test_smoke_pytest_still_missing_after_pip(monkeypatch, tmp_path):
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, err=NO_MOD)), (PIP, R()),
             (PYTEST, R(1, err=NO_MOD)))
    ok, text, retry = launcher.smoke()
    assert not ok and "pip install -r requirements.txt" in text and not retry


def test_smoke_own_basetemp_removed_after(monkeypatch, tmp_path):
    """Общий pytest-of-<user> делят все pytest на ПК: чужой запуск чистил его под смоуком (WinError) — у каждого
    смоука своя --basetemp, после смоука её нет."""
    fake_run(monkeypatch, tmp_path)
    seen = []

    def run(args, timeout):
        if args[0] == "pytest":
            base = args[-1].split("=", 1)[1]
            seen.append((args[-1], base, os.path.isdir(base)))
        return R(0, "5 passed")

    monkeypatch.setattr(launcher, "_run", run)
    assert launcher.smoke() == (True, "5 passed", False)
    assert launcher.smoke() == (True, "5 passed", False)
    (arg1, base1, existed1), (_, base2, _) = seen
    assert arg1.startswith("--basetemp=") and os.path.basename(base1).startswith("p2p-smoke-") and existed1
    assert base1 != base2 and not os.path.exists(base1) and not os.path.exists(base2)


def test_smoke_basetemp_removed_on_error(monkeypatch, tmp_path):
    """OSError при запуске — провал попытки (не исключение посреди обновления); basetemp убирается при любом выходе."""
    fake_run(monkeypatch, tmp_path)
    bases, fail = [], [OSError("[WinError 1455] Файл подкачки слишком мал")]

    def run(args, timeout):
        if args[0] == "pytest":
            bases.append(args[-1].split("=", 1)[1])
            raise fail[0]
        return R()

    monkeypatch.setattr(launcher, "_run", run)
    ok, text, retry = launcher.smoke()
    assert not ok and "WinError 1455" in text and retry     # сбой ОС — повторить стоит
    fail[0] = RuntimeError("неожиданное")
    with pytest.raises(RuntimeError):
        launcher.smoke()
    assert len(bases) == 2 and not any(os.path.exists(b) for b in bases)


@pytest.mark.parametrize("stage, out, retry", [
    ("pytest", "FAILED tests/test_x.py::test_y - PermissionError: [WinError 32] файл занят", True),
    ("pytest", "FAILED tests/test_x.py::test_y - OSError: [Errno 24] Too many open files", True),
    ("pytest", "FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2", False),
    ("py_compile", "[WinError 32] Процесс не может получить доступ к файлу: '__pycache__\\bot.cpython-310.pyc'", True),
    ("py_compile", "SyntaxError: invalid syntax", False)])
def test_smoke_retry_only_os_failures(monkeypatch, tmp_path, stage, out, retry):
    """Повторять стоит только сбой ОС под нагрузкой; падение теста или SyntaxError — сразу откат."""
    if stage == "pytest":
        fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, out)))
    else:
        fake_run(monkeypatch, tmp_path, (["py_compile"], R(1, err=out)))
    assert launcher.smoke() == (False, out, retry)


def test_requirements_include_pytest():
    """Смоук на ПК бота запускает pytest — он ставится вместе с зависимостями бота."""
    with open(os.path.join(os.path.dirname(launcher.__file__), "requirements.txt"), encoding="utf-8") as f:
        names = [re.split(r"[<>=!~;\s\[]", s.strip(), maxsplit=1)[0].lower() for s in f if s.strip()]
    assert "pytest" in names


# --- бот не переживает launcher ---

class Stop(Exception):
    """Выход из бесконечного цикла run() в тесте."""


class FakeProc:
    pid = 4242

    def __init__(self, alive=True):
        self.returncode = None if alive else 1
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated, self.returncode = True, 15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = 9


@pytest.fixture
def stand(monkeypatch, tmp_path):
    """Launcher без git, Telegram и процессов. sleep на stop_at-м вызове бросает exc — так выходим из run()."""
    paths = {"HERE": tmp_path, "LAST_GOOD": tmp_path / ".last_good", "LOG_PATH": tmp_path / "launcher.log",
             "PID_FILE": tmp_path / "bot.pid"}
    for name, value in paths.items():
        monkeypatch.setattr(launcher, name, str(value))
    monkeypatch.setattr(launcher, "git", lambda *a, check=True: "abc1234")
    monkeypatch.setattr(launcher, "write_dev_status", lambda: None)
    monkeypatch.setattr(launcher, "env_value", lambda key, default="": default)
    monkeypatch.setattr(launcher, "notify", lambda text: None)
    monkeypatch.setattr(launcher, "kill_orphan", lambda: None)
    monkeypatch.setattr(launcher, "_proc_start", lambda pid: 111)
    monkeypatch.setattr(launcher.Launcher, "try_update", lambda self: False)
    monkeypatch.setattr(launcher.Launcher, "check_prs", lambda self: None)
    monkeypatch.setattr(launcher.Launcher, "check_minutes", lambda self: None)
    st = SimpleNamespace(procs=[], alive=True, sleeps=0, stop_at=2, exc=Stop, pid_seen=[])

    def start():
        st.procs.append(FakeProc(st.alive))
        return st.procs[-1]

    def sleep(sec):
        st.sleeps += 1
        pid = paths["PID_FILE"]
        st.pid_seen.append(pid.read_text() if pid.exists() else None)
        if st.sleeps >= st.stop_at:
            raise st.exc

    monkeypatch.setattr(launcher, "start_bot", start)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(time=time.time, strftime=time.strftime, sleep=sleep))
    st.log = lambda: (tmp_path / "launcher.log").read_text(encoding="utf-8")
    st.pid_file = paths["PID_FILE"]
    return st


def test_run_stops_bot_on_any_exit(stand):
    """Ctrl+C / исключение в launcher: раньше бот оставался жить, и run.bat запускал второго."""
    stand.exc, stand.stop_at = KeyboardInterrupt, 1
    with pytest.raises(KeyboardInterrupt):
        launcher.Launcher().run()
    assert len(stand.procs) == 1 and stand.procs[0].terminated
    assert stand.pid_seen == ["4242 111"]      # пока бот жив, pid и время старта лежат на диске для kill_orphan
    assert not stand.pid_file.exists()         # штатная остановка убирает pid-файл


def test_last_good_write_error_keeps_loop(stand, monkeypatch, tmp_path):
    """.last_good недоступен для записи — раньше PermissionError выбрасывал launcher, бот оставался сиротой."""
    monkeypatch.setattr(launcher, "STABLE_AFTER", -1)
    monkeypatch.setattr(launcher, "LAST_GOOD", str(tmp_path))   # каталог: open(..., "w") → OSError
    stand.stop_at = 3
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert len(stand.procs) == 1 and stand.procs[0].terminated
    assert stand.log().count("last_good:") == 1  # одна попытка на запуск бота, без спама


def test_version_git_timeout_keeps_loop(stand, monkeypatch):
    """Таймаут git сразу после запуска бота раньше ронял launcher, оставляя бота жить."""
    def git(*args, check=True):
        raise RuntimeError(f"git {' '.join(args)}: timeout")

    monkeypatch.setattr(launcher, "git", git)
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert len(stand.procs) == 1 and stand.procs[0].terminated
    assert "версия ?" in stand.log()


def test_rollback_error_keeps_loop(stand, monkeypatch):
    def rollback(self):
        raise RuntimeError("git rev-parse HEAD: timeout")

    monkeypatch.setattr(launcher, "CRASH_LIMIT", 1)
    monkeypatch.setattr(launcher.Launcher, "rollback", rollback)
    stand.alive, stand.stop_at = False, 1        # бот падает сразу; первый sleep — пауза перед перезапуском
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert "ошибка отката: RuntimeError" in stand.log()


def test_update_restarts_bot_once(stand, monkeypatch):
    answers = iter([False, True])                # первая проверка при старте, затем обновление в цикле
    monkeypatch.setattr(launcher.Launcher, "try_update", lambda self: next(answers, False))
    monkeypatch.setattr(launcher, "env_value", lambda key, default="": "-1" if key == "UPDATE_EVERY" else default)
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert len(stand.procs) == 2 and all(p.terminated for p in stand.procs)


def test_minutes_checked_once_a_day(stand, monkeypatch):
    """Минуты GitHub Actions — не на каждой проверке обновлений, а раз в сутки."""
    calls = []
    monkeypatch.setattr(launcher.Launcher, "check_minutes", lambda self: calls.append(1))
    monkeypatch.setattr(launcher, "env_value", lambda key, default="": "-1" if key == "UPDATE_EVERY" else default)
    stand.stop_at = 4                            # три проверки обновлений подряд
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert calls == [1]


# --- try_update: переход ровно на проверенный коммит, повтор смоука, обновления только документации ---

HEAD, R1, R2 = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def upd(monkeypatch, tmp_path):
    """try_update без настоящего git и Telegram — модель репозитория: st.head — HEAD папки бота, st.github — main
    на GitHub; fetch запоминает его как origin/main, pull берёт с GitHub то, что там сейчас, merge и reset — ровно
    названный sha. st.after_fetch — «облако запушило ещё коммит» сразу после fetch. st.files — вывод
    diff --no-renames, st.renamed — что показал бы diff с поиском переименований. st.smokes — ответы smoke()."""
    paths = {"LOG_PATH": tmp_path / "launcher.log", "LAST_GOOD": tmp_path / ".last_good"}
    for name, value in paths.items():
        monkeypatch.setattr(launcher, name, str(value))
    st = SimpleNamespace(head=HEAD, github=R1, origin=None, files=["bot.py"], renamed=None, after_fetch=None,
                         diverged=False, smokes=[], smoked=0, git=[], notes=[], dev=[], last_good=paths["LAST_GOOD"])

    def git(*args, check=True):
        st.git.append(" ".join(args))
        if args[0] == "fetch":
            st.origin = st.github
            if st.after_fetch:
                st.after_fetch()
        elif args[0] == "rev-parse":
            return st.head if args[1] == "HEAD" else st.origin
        elif args[0] == "rev-list":
            a, b = args[-1].split("..")
            return "0" if a == b else "1"
        elif args[0] == "diff":
            names = st.files if "--no-renames" in args else (st.renamed or st.files)
            return "".join(n + "\0" for n in names) if "-z" in args else "\n".join(names)
        elif args[0] == "merge" and st.diverged:
            raise RuntimeError(f"git {' '.join(args)}: fatal: Not possible to fast-forward, aborting.")
        elif args[0] == "pull":
            st.head = st.github
        elif args[0] in ("merge", "reset"):
            st.head = args[-1]
        return ""

    def smoke():
        st.smoked += 1
        return st.smokes.pop(0)

    monkeypatch.setattr(launcher, "git", git)
    monkeypatch.setattr(launcher, "clean_tree", lambda: True)
    monkeypatch.setattr(launcher, "smoke", smoke)
    monkeypatch.setattr(launcher, "notify", st.notes.append)
    monkeypatch.setattr(launcher, "write_dev_status", lambda: st.dev.append(st.head))
    monkeypatch.setattr(launcher, "roadmap_progress", lambda: (0, 0, ""))
    monkeypatch.setattr(launcher, "repo_url", lambda: "repo")
    st.log = lambda: (tmp_path / "launcher.log").read_text(encoding="utf-8")
    return st


def test_update_lands_exactly_on_checked_commit(upd):
    """Между fetch и переходом облако запушило R2 с кодом: pull сходил бы в origin заново и поставил R2 без смоука
    (diff считался для R1 — только .md). Теперь переход ровно на R1, а R2 — следующей проверкой, со смоуком."""
    upd.files = ["ROADMAP.md"]
    upd.after_fetch = lambda: setattr(upd, "github", R2)
    lau = launcher.Launcher()
    assert lau.try_update() is False and upd.head == R1 and upd.smoked == 0
    assert f"diff --no-renames --name-only -z {HEAD} {R1}" in upd.git and f"merge --ff-only --quiet {R1}" in upd.git
    assert not any(c.startswith("pull") for c in upd.git)
    upd.after_fetch, upd.files, upd.smokes = None, ["bot.py"], [(True, "5 passed", False)]
    assert lau.try_update() is True and upd.head == R2 and upd.smoked == 1
    assert f"diff --no-renames --name-only -z {R1} {R2}" in upd.git


def test_update_diverged_branches_notify(upd):
    upd.diverged = True
    lau = launcher.Launcher()
    assert lau.try_update() is False and upd.head == HEAD and upd.smoked == 0 and R1 in lau.bad
    assert len(upd.notes) == 1 and "ветки разошлись" in upd.notes[0]


def test_update_rename_to_md_is_not_docs_only(upd):
    """helper.py → HELPER.md: с поиском переименований diff показал бы только HELPER.md — «одна документация»,
    хотя код удалён. Без переименований виден и удалённый helper.py — обычное обновление со смоуком."""
    upd.files, upd.renamed = ["HELPER.md", "helper.py"], ["HELPER.md"]
    upd.smokes = [(True, "5 passed", False)]
    assert launcher.Launcher().try_update() is True and upd.smoked == 1


def test_update_smoke_retry_keeps_good_commit(upd):
    """Смоук упал на WinError под нагрузкой, повтор прошёл — раньше коммит откатывался и больше не ставился."""
    upd.smokes = [(False, "E   PermissionError: [WinError 32] файл занят\n1 failed in 3.1s", True),
                  (True, "5 passed", False)]
    lau = launcher.Launcher()
    assert lau.try_update() is True               # бот перезапустится на новой версии
    assert upd.smoked == 2 and not any(c.startswith("reset") for c in upd.git) and not lau.bad
    assert len(upd.notes) == 1 and upd.notes[0].startswith("🔄")
    assert "сбой ОС, повторяю" in upd.log() and "WinError 32" in upd.log()


def test_update_smoke_fails_twice_rolls_back(upd):
    upd.smokes = [(False, "PermissionError: [WinError 5] test_a", True), (False, "PermissionError: test_b", True)]
    lau = launcher.Launcher()
    assert lau.try_update() is False
    assert upd.smoked == 2 and f"reset --hard {HEAD}" in upd.git and upd.head == HEAD and R1 in lau.bad
    assert len(upd.notes) == 1 and "не прошло проверку" in upd.notes[0] and "test_b" in upd.notes[0]
    assert lau.try_update() is False and upd.smoked == 2   # забракованный коммит больше не проверяем


@pytest.mark.parametrize("err", ["FAILED tests/test_x.py::test_a - AssertionError",
                                 "смоук-тест не завершился за отведённое время",
                                 "pytest не нашёл ни одного теста (tests/ удалён или пуст?)"])
def test_update_code_failure_not_retried(upd, err):
    """Падение теста, таймаут, нет тестов — повтор ничего не даст, а присмотр за ботом стоял бы вдвое дольше."""
    upd.smokes = [(False, err, False)]
    lau = launcher.Launcher()
    assert lau.try_update() is False
    assert upd.smoked == 1 and f"reset --hard {HEAD}" in upd.git and R1 in lau.bad and err in upd.notes[0]


def test_update_docs_only_no_smoke_no_restart(upd):
    upd.files = ["README.md", "docs/заметки.md"]  # -z: путь как есть, без кавычек и \ooo
    upd.last_good.write_text(HEAD)
    lau = launcher.Launcher()
    assert lau.try_update() is False              # бот не перезапускаем
    assert upd.head == R1 and upd.smoked == 0 and not any(c.startswith("reset") for c in upd.git)
    assert upd.notes == [f"📝 Обновлена документация ({R1[:7]}), бот не перезапускал"]
    assert "только документация" in upd.log() and "заметки.md" in upd.log()
    assert upd.dev == [R1]                        # «🛠 Разработка» — с новой версией
    assert upd.last_good.read_text() == R1        # работающий бот и есть новая версия
    lau.rollback()                                # падения потом: раньше «откат» на тот же код со старыми .md
    assert upd.head == R1 and R1 not in lau.bad and not any(c.startswith("reset") for c in upd.git)


@pytest.mark.parametrize("good", [None, "d" * 40])
def test_update_docs_only_keeps_other_last_good(upd, good):
    """.last_good нет или там более старая версия (текущая ещё не проработала STABLE_AFTER) — не трогаем."""
    upd.files = ["README.md"]
    if good:
        upd.last_good.write_text(good)
    assert launcher.Launcher().try_update() is False
    assert (upd.last_good.read_text() if upd.last_good.exists() else None) == good


def test_update_code_and_docs_goes_through_smoke(upd):
    upd.files = ["ROADMAP.md", "bot.py"]
    upd.smokes = [(True, "5 passed", False)]
    assert launcher.Launcher().try_update() is True
    assert upd.smoked == 1 and upd.head == R1 and upd.notes[0].startswith("🔄")


# --- уведомления дублируются в launcher.log, секреты в лог не попадают ---

def test_notify_copied_to_log_one_line(monkeypatch, tmp_path):
    """Откаты раньше были видны только в Telegram; без TG_TOKEN — вообще нигде."""
    log = tmp_path / "launcher.log"
    monkeypatch.setattr(launcher, "LOG_PATH", str(log))
    monkeypatch.setattr(launcher, "env_value", lambda key, default="": default)
    launcher.notify("⚠️ Обновление bbbbbbb не прошло проверку — оставил aaaaaaa.\nFAILED tests/test_x.py\n"
                    + "x" * 1000)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "уведомление: ⚠️ Обновление bbbbbbb не прошло проверку — оставил aaaaaaa. ⏎ FAILED tests/test_x.py ⏎ x" \
        in lines[0]
    assert len(lines[0].split("уведомление: ", 1)[1]) == 300


@pytest.mark.parametrize("error, logged", [
    (lambda url: http.client.InvalidURL(f"URL can't contain control characters. {url!r} (found at least '\\t')"),
     "telegram: InvalidURL"),
    (lambda url: urllib.error.HTTPError(url, 401, f"Unauthorized {url}", None, None), "telegram: HTTPError 401")])
def test_notify_log_has_no_secrets(monkeypatch, tmp_path, error, logged):
    """Токен с управляющим символом (TG_TOKEN=<токен><TAB># …): InvalidURL цитирует repr URL с \\t, и прежняя
    замена str(e).replace(token) не срабатывала — токен уходил в launcher.log. Текст ошибки больше не пишем:
    только тип и код HTTP. Логин:пароль в ссылке из текста уведомления тоже вырезается."""
    log = tmp_path / "launcher.log"
    monkeypatch.setattr(launcher, "LOG_PATH", str(log))
    env = {"TG_TOKEN": "123:fake-token-for-test\t# бот", "TG_CHAT_ID": "42"}
    monkeypatch.setattr(launcher, "env_value", lambda key, default="": env.get(key, default))
    sent = []

    class Opener:
        def open(self, url, data, timeout):
            sent.append(url)
            raise error(url)

    monkeypatch.setattr(launcher.urllib.request, "build_opener", lambda *handlers: Opener())
    launcher.notify("🔄 Бот обновился\nssh://deploy:hunter2@host/repo.git")
    text = log.read_text(encoding="utf-8")
    assert sent and "fake-token-for-test" in sent[0]      # токен ушёл только в запрос
    assert "fake-token" not in text and "hunter2" not in text
    assert text.rstrip().endswith(logged) and "ssh://***@host" in text


def test_log_masks_bot_token_and_url_passwords(monkeypatch, tmp_path):
    """Страховка для любых строк лога (ошибки git, gh, текст исключений): /bot<токен> и логин:пароль в ссылках."""
    log = tmp_path / "launcher.log"
    monkeypatch.setattr(launcher, "LOG_PATH", str(log))
    launcher.log("URL can't contain control characters. '/bot123:fake-token\\t#x/sendMessage'")
    launcher.log(RuntimeError("git fetch --quiet origin main: fatal: unable to access 'ssh://deploy:hunter2@host/r/'"))
    launcher.log("FAILED tests/test_bot.py::test_x")   # обычные строки с «bot» не портим
    text = log.read_text(encoding="utf-8")
    assert "fake-token" not in text and "hunter2" not in text
    assert "'/bot***/sendMessage'" in text and "ssh://***@host/r/" in text and "tests/test_bot.py::test_x" in text


def test_log_survives_non_utf8_stdout(monkeypatch, tmp_path):
    """Уведомления с emoji теперь идут и в лог: вывод в cp1251 (не через run.bat) не должен ронять notify."""
    log = tmp_path / "launcher.log"
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1251")
    monkeypatch.setattr(launcher, "LOG_PATH", str(log))
    monkeypatch.setattr(launcher.sys, "stdout", out)
    launcher.log("уведомление: 📝 Обновлена документация")
    out.seek(0)
    assert "уведомление: ? Обновлена документация" in out.read()
    assert "📝 Обновлена документация" in log.read_text(encoding="utf-8")   # в файле — как есть


def test_env_value_strips_comment_after_any_space(monkeypatch, tmp_path):
    """TG_TOKEN=<токен><TAB># комментарий: раньше комментарий после табуляции попадал в значение, а с ним в URL."""
    monkeypatch.setattr(launcher, "HERE", str(tmp_path))
    (tmp_path / ".env").write_text("TG_TOKEN=123:fake-token\t# бот\nTG_CHAT_ID=42 # чат\nUPDATE_EVERY=60\n",
                                   encoding="utf-8")
    assert launcher.env_value("TG_TOKEN") == "123:fake-token"
    assert launcher.env_value("TG_CHAT_ID") == "42" and launcher.env_value("UPDATE_EVERY") == "60"
    assert launcher.env_value("NOPE", "d") == "d"


# --- минуты GitHub Actions ---

@pytest.fixture
def minutes(monkeypatch, tmp_path):
    """check_minutes без сети: gh отвечает st.used минут Actions за месяц в приватных репозиториях владельца
    (плюс публичный репозиторий и строки, которые не минуты Actions); квота из .env — st.free.
    st.fail = (кусок аргументов gh, исключение) — этот вызов gh падает."""
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "gh")
    st = SimpleNamespace(used=0, free="", private="p2p-bot\nprivate-tool", fail=None, calls=[], notes=[])

    def gh(*args):
        st.calls.append(args)
        if st.fail and st.fail[0] in " ".join(args):
            raise st.fail[1]
        if args[1] == "user":
            return "owner"
        if "--paginate" in args:
            return st.private
        return json.dumps({"usageItems": [
            {"product": "actions", "sku": "actions_linux", "unitType": "Minutes", "quantity": st.used - 30,
             "repositoryName": "owner/p2p-bot"},
            {"product": "actions", "sku": "actions_windows", "unitType": "Minutes", "quantity": 5,   # ×2
             "repositoryName": "owner/private-tool"},
            {"product": "actions", "sku": "actions_macos", "unitType": "Minutes", "quantity": 2,     # ×10
             "repositoryName": "p2p-bot"},
            {"product": "actions", "sku": "actions_linux", "unitType": "Minutes", "quantity": 5000,  # публичный
             "repositoryName": "owner/public-site"},
            {"product": "actions", "sku": "actions_storage", "unitType": "GigabyteHours", "quantity": 5000,
             "repositoryName": "owner/p2p-bot"},
            {"product": "copilot", "sku": "copilot_premium_request", "unitType": "Requests", "quantity": 5000}]})

    monkeypatch.setattr(launcher, "gh", gh)
    monkeypatch.setattr(launcher, "notify", st.notes.append)
    monkeypatch.setattr(launcher, "env_value",
                        lambda key, default="": (st.free or default) if key == "GH_ACTIONS_FREE_MIN" else default)
    st.log = lambda: (tmp_path / "launcher.log").read_text(encoding="utf-8")
    return st


def test_minutes_below_80_silent(minutes):
    """Публичные репозитории (5000 мин) квоту не тратят — считаются только приватные, Windows ×2, macOS ×10."""
    minutes.used = 1500
    launcher.Launcher().check_minutes()
    year, month = map(int, time.strftime("%Y %m").split())
    assert minutes.notes == []
    assert minutes.calls == [
        ("api", "user", "--jq", ".login"),
        ("api", "--paginate", "user/repos?visibility=private&affiliation=owner&per_page=100", "--jq", ".[].name"),
        ("api", f"users/owner/settings/billing/usage?year={year}&month={month}")]
    assert "1500 из 2000 (75%)" in minutes.log()


def test_minutes_no_private_repos_nothing_counts(minutes):
    minutes.used, minutes.private = 1990, ""
    launcher.Launcher().check_minutes()
    assert minutes.notes == [] and "0 из 2000 (0%)" in minutes.log()


def test_minutes_80_then_95_once_each(minutes):
    lau = launcher.Launcher()
    minutes.used = 1650
    lau.check_minutes()
    month = launcher.MONTHS[int(time.strftime("%m")) - 1]
    assert minutes.notes == [f"⚠️ GitHub Actions: израсходовано 1650 из 2000 мин за {month} (82%). "
                             "Когда минуты кончатся, CI и автомерж остановятся до 1-го числа."]
    minutes.used = 1800
    lau.check_minutes()
    assert len(minutes.notes) == 1                 # 80% — один раз за месяц
    minutes.used = 1900
    lau.check_minutes()
    minutes.used = 1990
    lau.check_minutes()
    assert len(minutes.notes) == 2 and "(95%)" in minutes.notes[1]   # 95% — тоже один раз


def test_minutes_jump_past_95_single_notice(minutes):
    """Сразу за 95% (квота из GH_ACTIONS_FREE_MIN) — одно сообщение, а не два подряд."""
    minutes.used, minutes.free = 2900, "3000"
    lau = launcher.Launcher()
    lau.check_minutes()
    lau.check_minutes()
    assert len(minutes.notes) == 1 and "2900 из 3000" in minutes.notes[0] and "(96%)" in minutes.notes[0]


def test_minutes_new_month_warns_again(minutes, monkeypatch):
    ym = ["2026 09"]
    monkeypatch.setattr(launcher, "time", SimpleNamespace(
        time=time.time, strftime=lambda fmt: ym[0] if fmt == "%Y %m" else time.strftime(fmt)))
    minutes.used = 1700
    lau = launcher.Launcher()
    lau.check_minutes()
    ym[0] = "2026 10"
    lau.check_minutes()
    assert len(minutes.notes) == 2 and "за сентябрь" in minutes.notes[0] and "за октябрь" in minutes.notes[1]
    assert minutes.calls[-1][1].endswith("year=2026&month=10")


def test_minutes_without_gh_does_nothing(minutes, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    minutes.used = 1990
    launcher.Launcher().check_minutes()
    assert minutes.calls == [] and minutes.notes == []


@pytest.mark.parametrize("fail", [("user --jq", RuntimeError("gh api user: timeout")),
                                  ("user/repos", RuntimeError("gh api user/repos: HTTP 403")),
                                  ("billing", ValueError("Expecting value"))])
def test_minutes_errors_only_logged(minutes, fail):
    """Нет прав у gh на billing или список репозиториев, таймаут, мусор вместо JSON — проверка пропускается,
    только строка в логе; launcher работает дальше."""
    minutes.used, minutes.fail = 1990, fail
    launcher.Launcher().check_minutes()
    assert minutes.notes == [] and f"минуты GitHub Actions: {type(fail[1]).__name__}" in minutes.log()


# --- бот-сирота от убитого извне launcher ---

@pytest.fixture
def orphan(monkeypatch, tmp_path):
    """st.alive: pid -> время старта живого процесса; os.kill «убивает», если st.killable."""
    monkeypatch.setattr(launcher, "PID_FILE", str(tmp_path / "bot.pid"))
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))
    st = SimpleNamespace(alive={}, killed=[], killable=True, notes=[], pid_file=tmp_path / "bot.pid")

    def kill(pid, sig):
        st.killed.append(pid)
        if st.killable:
            st.alive.pop(pid, None)

    monkeypatch.setattr(launcher, "_proc_start", lambda pid: st.alive.get(pid))
    monkeypatch.setattr(launcher.os, "kill", kill)
    monkeypatch.setattr(launcher, "notify", st.notes.append)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(time=time.time, strftime=time.strftime, sleep=lambda s: None))
    return st


def test_kill_orphan_kills_live_bot(orphan):
    orphan.alive[777] = 5
    orphan.pid_file.write_text("777 5")
    launcher.kill_orphan()
    assert orphan.killed == [777] and not orphan.pid_file.exists()


@pytest.mark.parametrize("content, start", [(None, None), ("мусор", None), ("777 5", None), ("777 5", 6), ("777", 5)])
def test_kill_orphan_leaves_others(orphan, content, start):
    """Нет файла, мусор, процесс уже мёртв, pid достался чужой программе (другое время старта),
    старый pid-файл без времени старта — никого не трогаем, устаревший файл убираем."""
    if start is not None:
        orphan.alive[777] = start
    if content is not None:
        orphan.pid_file.write_text(content)
    launcher.kill_orphan()
    assert orphan.killed == [] and not orphan.pid_file.exists()


def test_kill_orphan_skips_own_pid(orphan):
    """pid из старого файла мог достаться самому новому launcher — себя не убиваем."""
    orphan.alive[os.getpid()] = 5
    orphan.pid_file.write_text(f"{os.getpid()} 5")
    launcher.kill_orphan()
    assert orphan.killed == [] and not orphan.pid_file.exists()


def test_kill_orphan_refuses_second_bot_if_orphan_survives(orphan):
    orphan.alive[777] = 5
    orphan.killable = False
    orphan.pid_file.write_text("777 5")
    with pytest.raises(RuntimeError, match="777"):
        launcher.kill_orphan()
    assert orphan.pid_file.exists()              # следующий запуск launcher попробует снова
    assert orphan.notes and "777" in orphan.notes[0]   # и владелец узнает в Telegram, а не только из лога


@pytest.mark.skipif(os.name != "nt", reason="WinAPI — только Windows")
def test_proc_start_real_process():
    """Собственный процесс жив: время старта есть и стабильно; несуществующий pid — None."""
    me = launcher._proc_start(os.getpid())
    assert me and me == launcher._proc_start(os.getpid())
    assert launcher._proc_start(0x7FFFFFF0) is None


@pytest.mark.skipif(os.name != "nt", reason="замок через msvcrt — только Windows")
def test_single_instance_lock(monkeypatch, tmp_path):
    import msvcrt
    lock = str(tmp_path / "launcher.lock")
    monkeypatch.setattr(launcher, "LOCK_PATH", lock)
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))
    monkeypatch.setattr(launcher, "_lock_fd", None)
    other = os.open(lock, os.O_RDWR | os.O_CREAT)   # «первый launcher» держит замок
    msvcrt.locking(other, msvcrt.LK_NBLCK, 1)
    try:
        assert launcher.acquire_lock() is False
    finally:
        os.close(other)                                # первый умер — ОС сняла замок
    assert launcher.acquire_lock() is True
    os.close(launcher._lock_fd)
