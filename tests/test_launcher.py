"""launcher.py: смоук не считает «тесты не запускались» успехом; бот не переживает launcher, второй launcher не стартует.

Настоящие процессы, git, pip и Telegram не трогаем: подменяются _run, start_bot, _tasklist, git, notify.
"""
import os
import re
import sys
import time
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
    assert launcher.smoke() == (False, "SyntaxError: bad")
    assert calls == [["py_compile", "ok.py"]]


def test_smoke_passes_and_fails_by_pytest(monkeypatch, tmp_path):
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(0, "5 passed")))
    assert launcher.smoke() == (True, "5 passed")
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, "FAILED tests/test_x.py::test_y")))
    ok, text = launcher.smoke()
    assert not ok and "FAILED tests/test_x.py" in text


def test_smoke_no_tests_is_failure(monkeypatch, tmp_path):
    """Код 5 (коммит стёр tests/) раньше считался успехом."""
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(5, "no tests ran in 0.00s")))
    ok, text = launcher.smoke()
    assert not ok and "ни одного теста" in text


def test_smoke_installs_missing_pytest_and_reruns(monkeypatch, tmp_path):
    calls = fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, err=NO_MOD)), (PIP, R()),
                     (PYTEST, R(0, "5 passed")))
    assert launcher.smoke() == (True, "5 passed")
    assert calls[2] == PIP and calls.count(PYTEST) == 2


def test_smoke_pip_failure_is_failure(monkeypatch, tmp_path):
    """Без pytest раньше возвращалось (True, "") — обновление «проверено» без единого теста."""
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, err=NO_MOD)),
             (PIP, R(1, err="ERROR: Could not install packages")))
    ok, text = launcher.smoke()
    assert not ok and "pip install -r requirements.txt" in text and "Could not install" in text


def test_smoke_pytest_still_missing_after_pip(monkeypatch, tmp_path):
    fake_run(monkeypatch, tmp_path, (["py_compile"], R()), (PYTEST, R(1, err=NO_MOD)), (PIP, R()),
             (PYTEST, R(1, err=NO_MOD)))
    ok, text = launcher.smoke()
    assert not ok and "pip install -r requirements.txt" in text


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
    monkeypatch.setattr(launcher.Launcher, "try_update", lambda self: False)
    monkeypatch.setattr(launcher.Launcher, "check_prs", lambda self: None)
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
    assert stand.pid_seen == ["4242"]          # пока бот жив, pid лежит на диске для kill_orphan
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


# --- бот-сирота от убитого извне launcher ---

@pytest.fixture
def orphan(monkeypatch, tmp_path):
    """tasklist видит процессы из st.alive (pid -> образ); os.kill «убивает», если st.killable."""
    monkeypatch.setattr(launcher, "PID_FILE", str(tmp_path / "bot.pid"))
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))
    st = SimpleNamespace(alive={}, killed=[], killable=True, pid_file=tmp_path / "bot.pid")

    def tasklist(pid):
        if pid in st.alive:
            return f'"{st.alive[pid]}","{pid}","Console","1","10 000 K"\n'
        return "INFO: No tasks are running which match the specified criteria.\n"

    def kill(pid, sig):
        st.killed.append(pid)
        if st.killable:
            st.alive.pop(pid, None)

    monkeypatch.setattr(launcher, "_tasklist", tasklist)
    monkeypatch.setattr(launcher.os, "kill", kill)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(time=time.time, strftime=time.strftime, sleep=lambda s: None))
    return st


def test_kill_orphan_kills_live_bot(orphan):
    orphan.alive[777] = os.path.basename(sys.executable)
    orphan.pid_file.write_text("777")
    launcher.kill_orphan()
    assert orphan.killed == [777] and not orphan.pid_file.exists()


@pytest.mark.parametrize("content, image", [(None, None), ("мусор", None), ("777", None), ("777", "notepad.exe")])
def test_kill_orphan_leaves_others(orphan, content, image):
    """Нет файла, мусор, процесс уже мёртв, pid достался чужой программе — никого не трогаем."""
    if image:
        orphan.alive[777] = image
    if content is not None:
        orphan.pid_file.write_text(content)
    launcher.kill_orphan()
    assert orphan.killed == []


def test_kill_orphan_skips_own_pid(orphan):
    """pid из старого файла мог достаться самому новому launcher — себя не убиваем."""
    orphan.alive[os.getpid()] = os.path.basename(sys.executable)
    orphan.pid_file.write_text(str(os.getpid()))
    launcher.kill_orphan()
    assert orphan.killed == [] and not orphan.pid_file.exists()


def test_kill_orphan_refuses_second_bot_if_orphan_survives(orphan):
    orphan.alive[777] = os.path.basename(sys.executable)
    orphan.killable = False
    orphan.pid_file.write_text("777")
    with pytest.raises(RuntimeError, match="777"):
        launcher.kill_orphan()
    assert orphan.pid_file.exists()              # следующий запуск launcher попробует снова


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
