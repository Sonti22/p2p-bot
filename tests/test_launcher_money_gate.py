"""Локальный барьер launcher (защищённый файл, правится только вручную): после обновления кода выплаты и торговля
выключены (PAYOUTS/TRADING=0 в .env), а обновление с защищёнными файлами ставится только после
python launcher.py --approve <sha> на ПК. CI на GitHub запускает ci.yml из самой ветки — верить ему нельзя, это
последний барьер перед запуском нового кода рядом с ключами.

Настоящие git, pytest и Telegram не трогаем: подменяются git, smoke, notify; все пути — во временной папке."""
import importlib.util
import os
import time
from types import SimpleNamespace

import pytest

import launcher
import p2p
import payouts

HEAD, R1, R2 = "a" * 40, "b" * 40, "c" * 40
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_guard():
    spec = importlib.util.spec_from_file_location("guard_script", os.path.join(ROOT, "scripts", "guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """try_update на модели репозитория: st.head — HEAD папки бота, st.github — main на GitHub (fetch делает его
    origin/main), merge/reset — ровно названный sha, st.files — вывод git diff --no-renames -z, st.smokes — ответы
    smoke() (по умолчанию — прошёл). .env и logs/approved_shas — в tmp_path."""
    paths = {"HERE": tmp_path, "LOG_PATH": tmp_path / "launcher.log", "LAST_GOOD": tmp_path / ".last_good",
             "APPROVED_PATH": tmp_path / "logs" / "approved_shas"}
    for name, value in paths.items():
        monkeypatch.setattr(launcher, name, str(value))
    st = SimpleNamespace(head=HEAD, github=R1, origin=None, files=["bot.py"], smokes=[], smoked=0, git=[], notes=[],
                         diff_error=None, diverged=False, env=tmp_path / ".env", approved=paths["APPROVED_PATH"])

    def git(*args, check=True):
        st.git.append(" ".join(args))
        if args[0] == "fetch":
            st.origin = st.github
        elif args[0] == "rev-parse":
            return st.head if args[1] == "HEAD" else st.origin
        elif args[0] == "rev-list":
            a, b = args[-1].split("..")
            return "0" if a == b else "1"
        elif args[0] == "diff":
            if st.diff_error:
                raise RuntimeError(st.diff_error)
            return "".join(n + "\0" for n in st.files)
        elif args[0] == "merge-base":
            if st.diverged:   # в папке бота свой коммит: HEAD не предок origin/main
                raise RuntimeError("git merge-base --is-ancestor: exit 1")
        elif args[0] in ("merge", "reset"):
            st.head = args[-1]
        return ""

    def smoke():
        st.smoked += 1
        return st.smokes.pop(0) if st.smokes else (True, "5 passed", False)

    monkeypatch.setattr(launcher, "git", git)
    monkeypatch.setattr(launcher, "clean_tree", lambda: True)
    monkeypatch.setattr(launcher, "smoke", smoke)
    monkeypatch.setattr(launcher, "notify", st.notes.append)
    monkeypatch.setattr(launcher, "write_dev_status", lambda: None)
    monkeypatch.setattr(launcher, "roadmap_progress", lambda: (0, 0, ""))
    monkeypatch.setattr(launcher, "repo_url", lambda: "repo")
    st.merged = lambda: any(c.startswith("merge --ff-only") for c in st.git)   # merge-base — только проверка
    st.money_notes = lambda: [n for n in st.notes if n.startswith("💸")]
    return st


def money_note(sha):
    return (f"💸 Выплаты/торговля выключены после обновления {sha[:7]}: проверь изменения и включи на ПК "
            f"(PAYOUTS=1 / TRADING=1 в .env)")


# --- money_off: только строки PAYOUTS/TRADING, остальное байт в байт ---

def test_money_off_rewrites_only_money_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(("TG_TOKEN=123:fake-token\r\n"
                     "PAYOUTS=1\r\n"
                     "# PAYOUTS=1 — комментарий не трогаем\n"
                     "payouts = 1 # включил сам\n"
                     "PAYOUT_MAX_ONE=100\n"
                     "XPAYOUTS=1\n"
                     "  Trading=1\n"
                     "TRADING=0\n"
                     "TRADING_MODE=auto\n"
                     "PAYOUTS=yes\r"
                     "INCLUDE_PAY=Т-Банк,Сбер\n"
                     "\n"
                     "PAYOUTS=1").encode("utf-8"))                  # последняя строка — без перевода строки
    assert launcher.money_off(str(env)) is True
    assert env.read_bytes() == ("TG_TOKEN=123:fake-token\r\n"
                                "PAYOUTS=0\r\n"
                                "# PAYOUTS=1 — комментарий не трогаем\n"
                                "payouts =0\n"
                                "PAYOUT_MAX_ONE=100\n"
                                "XPAYOUTS=1\n"
                                "  Trading=0\n"
                                "TRADING=0\n"
                                "TRADING_MODE=auto\n"
                                "PAYOUTS=0\r"
                                "INCLUDE_PAY=Т-Банк,Сбер\n"
                                "\n"
                                "PAYOUTS=0").encode("utf-8")
    assert sorted(os.listdir(tmp_path)) == [".env"]                 # временный файл не остался


def test_money_off_matches_bot_switch(tmp_path, monkeypatch):
    """Разбор тот же, что у payouts.switch_from_file: включённые им выплаты после money_off выключены."""
    env = tmp_path / ".env"
    env.write_bytes("\ufeffX=1\npayouts=1\r\nPAYOUTS = 1 # да\n".encode("utf-8"))
    monkeypatch.setenv("PAYOUTS", "1")
    payouts.switch_from_file(str(env))
    assert os.environ["PAYOUTS"] == "1"                             # до: выключатель файла выплаты включает
    assert launcher.money_off(str(env)) is True
    assert env.read_bytes() == "\ufeffX=1\npayouts=0\r\nPAYOUTS =0\n".encode("utf-8")
    payouts.switch_from_file(str(env))
    assert os.environ["PAYOUTS"] == "0"


def test_money_off_bom_line(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("\ufeffPAYOUTS=1\nA=1\n".encode("utf-8"))
    assert launcher.money_off(str(env)) is True
    assert env.read_bytes() == "\ufeffPAYOUTS=0\nA=1\n".encode("utf-8")


@pytest.mark.parametrize("content", ["PAYOUTS=0\nTRADING = 0 # выкл\r\npayouts=0", "TG_TOKEN=1\nAMOUNT=5000\n", "",
                                     "# PAYOUTS=1\n#TRADING=1\n"])
def test_money_off_noop_when_already_off_or_absent(tmp_path, content):
    env = tmp_path / ".env"
    env.write_bytes(content.encode("utf-8"))
    before = env.stat().st_mtime_ns
    assert launcher.money_off(str(env)) is False
    assert env.read_bytes() == content.encode("utf-8") and env.stat().st_mtime_ns == before
    assert sorted(os.listdir(tmp_path)) == [".env"]


def test_money_off_no_env_file(tmp_path):
    assert launcher.money_off(str(tmp_path / ".env")) is False
    assert os.listdir(tmp_path) == []                               # .env не создаётся


def test_money_off_write_failure_raises_and_keeps_env(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"PAYOUTS=1\n")
    (tmp_path / ".env.money-off.tmp").mkdir()                       # временный файл не создать
    with pytest.raises(OSError):
        launcher.money_off(str(env))
    assert env.read_bytes() == b"PAYOUTS=1\n"


# --- try_update: после обновления кода деньги выключены ---

@pytest.mark.parametrize("files", [["bot.py"], ["tests/test_bot.py"], [".env.example"], ["ROADMAP.md", "p2p.py"],
                                   ["scripts/tool.py"], ["helper.py", "HELPER.md"]])
def test_code_update_switches_money_off_once(gate, files):
    gate.files = files
    gate.env.write_bytes(b"TG_CHAT_ID=42\nPAYOUTS=1\r\nTRADING=1\n")
    lau = launcher.Launcher()
    assert lau.try_update() is True and gate.head == R1 and gate.smoked == 1
    assert gate.env.read_bytes() == b"TG_CHAT_ID=42\nPAYOUTS=0\r\nTRADING=0\n"
    assert gate.money_notes() == [money_note(R1)] and gate.notes[-1].startswith("🔄")
    assert gate.git.index(f"merge --ff-only --quiet {R1}") > 0 and gate.notes[0] == money_note(R1)   # выключены до merge
    gate.github = R2                                                # следующее обновление: деньги уже выключены
    assert lau.try_update() is True and gate.head == R2
    assert gate.money_notes() == [money_note(R1)]                   # сообщение — одно, только когда выключили


def test_code_update_without_money_lines_no_money_note(gate):
    gate.env.write_bytes(b"TG_CHAT_ID=42\n")
    assert launcher.Launcher().try_update() is True
    assert gate.money_notes() == [] and gate.env.read_bytes() == b"TG_CHAT_ID=42\n"


@pytest.mark.parametrize("blocker", ["tmp", "env"])
def test_unwritable_env_update_not_applied(gate, blocker):
    """.env не записать (или не прочитать) — обновление не ставится вовсе: деньги выключаются до merge, так что
    новый код не оказывается на диске при включённых деньгах; громкое сообщение один раз; коммит не бракуется —
    починят .env, и следующая проверка его поставит."""
    if blocker == "tmp":
        gate.env.write_bytes(b"PAYOUTS=1\n")
        (gate.env.parent / ".env.money-off.tmp").mkdir()
    else:
        gate.env.mkdir()                                            # .env — папка: open() падает с OSError
    lau = launcher.Launcher()
    assert lau.try_update() is False
    assert not gate.merged() and gate.smoked == 0 and gate.head == HEAD and R1 not in lau.bad
    assert len(gate.notes) == 1 and gate.notes[0].startswith("🚨") and "НЕ ставлю" in gate.notes[0]
    assert R1[:7] in gate.notes[0] and HEAD[:7] in gate.notes[0] and not gate.money_notes()
    assert lau.try_update() is False and not gate.merged() and gate.head == HEAD
    assert len(gate.notes) == 1                                     # без спама на каждой проверке
    if blocker == "tmp":
        (gate.env.parent / ".env.money-off.tmp").rmdir()
        assert lau.try_update() is True and gate.head == R1 and gate.smoked == 1
        assert gate.env.read_bytes() == b"PAYOUTS=0\n" and gate.money_notes() == [money_note(R1)]


def test_money_off_before_merge_even_if_smoke_fails(gate):
    """Деньги выключаются до merge, поэтому и при проваленном смоуке они уже выключены (прежняя версия работает,
    включит снова владелец) — и владелец об этом знает."""
    gate.env.write_bytes(b"PAYOUTS=1\n")
    gate.smokes = [(False, "FAILED tests/test_x.py::test_a", False)]
    lau = launcher.Launcher()
    assert lau.try_update() is False and gate.head == HEAD and R1 in lau.bad
    assert gate.env.read_bytes() == b"PAYOUTS=0\n" and gate.money_notes() == [money_note(R1)]
    assert gate.notes[-1].startswith("⚠️ Обновление")


def test_crash_between_merge_and_smoke_leaves_money_off(gate, monkeypatch):
    """Launcher убит после merge, пока шёл смоук (закрыли окно, перезагрузка): новый launcher видит HEAD == origin/main,
    обновлять нечего — и бот стартует на новом коде. К этому моменту деньги уже должны быть выключены."""
    gate.env.write_bytes(b"PAYOUTS=1\nTRADING=1\n")

    def killed():
        raise KeyboardInterrupt

    monkeypatch.setattr(launcher, "smoke", killed)
    with pytest.raises(KeyboardInterrupt):
        launcher.Launcher().try_update()
    assert gate.head == R1
    assert launcher.Launcher().try_update() is False              # после перезапуска: обновлять нечего
    assert gate.env.read_bytes() == b"PAYOUTS=0\nTRADING=0\n"


def test_docs_only_update_unchanged(gate):
    """Только .md — как раньше: без смоука, без перезапуска и без выключения денег."""
    gate.files = ["README.md", "docs/заметки.md"]
    gate.env.write_bytes(b"PAYOUTS=1\nTRADING=1\n")
    assert launcher.Launcher().try_update() is False
    assert gate.head == R1 and gate.smoked == 0 and gate.merged()
    assert gate.env.read_bytes() == b"PAYOUTS=1\nTRADING=1\n"
    assert gate.notes == [f"📝 Обновлена документация ({R1[:7]}), бот не перезапускал"]


# --- run(): между остановкой старого бота и запуском нового ---

class Stop(Exception):
    """Выход из бесконечного цикла run() в тесте."""


class Proc:
    pid = 4242

    def __init__(self, st):
        self.st, self.returncode = st, None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.st.events.append("stop")
        if self.st.on_stop:                     # то, что успел сделать старый бот, пока его останавливали
            self.st.on_stop()
            self.st.on_stop = None
        self.returncode = 15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = 9


@pytest.fixture
def loop(gate, monkeypatch, tmp_path):
    """run() на модели gate: запуск бота пишет в gate.events версию и .env на этот момент, остановка — "stop".
    Первая проверка при старте launcher ничего не находит, R1 появляется на GitHub на первом sleep; sleep номер
    gate.stop_at бросает Stop."""
    gate.github, gate.events, gate.sleeps, gate.stop_at, gate.on_stop = HEAD, [], 0, 2, None

    def start():
        gate.events.append(("start", gate.head, gate.env.read_bytes() if gate.env.is_file() else None))
        return Proc(gate)

    def sleep(sec):
        gate.sleeps += 1
        if gate.sleeps == 1:
            gate.github = R1
        if gate.sleeps >= gate.stop_at:
            raise Stop

    monkeypatch.setattr(launcher, "start_bot", start)
    monkeypatch.setattr(launcher, "time", SimpleNamespace(time=time.time, strftime=time.strftime, sleep=sleep))
    monkeypatch.setattr(launcher, "kill_orphan", lambda: None)
    monkeypatch.setattr(launcher, "write_pid", lambda pid: None)
    monkeypatch.setattr(launcher, "PID_FILE", str(tmp_path / "bot.pid"))
    monkeypatch.setattr(launcher, "env_value", lambda key, default="": "-1" if key == "UPDATE_EVERY" else default)
    monkeypatch.setattr(launcher.Launcher, "check_prs", lambda self: None)
    monkeypatch.setattr(launcher.Launcher, "check_minutes", lambda self: None)
    gate.runs = lambda: [e if e == "stop" else e[:2] for e in gate.events]
    return gate


def test_money_rechecked_after_old_bot_stopped(loop):
    """Старый бот работает, пока идёт смоук; его save_env (прочитал .env до money_off) может дописаться позже и
    вернуть PAYOUTS=1. Перед запуском нового кода, уже после остановки старого бота, деньги выключаются ещё раз."""
    loop.env.write_bytes(b"PAYOUTS=1\nTRADING=1\n")
    loop.on_stop = lambda: loop.env.write_bytes(b"PAYOUTS=1\nTRADING=1\nTG_CHAT_ID=42\n")
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert loop.runs() == [("start", HEAD), "stop", ("start", R1), "stop"]
    assert loop.events[2][2] == b"PAYOUTS=0\nTRADING=0\nTG_CHAT_ID=42\n"
    # первое — о выключении, второе — что пришлось выключить ещё раз (иначе владелец считал бы их включёнными)
    assert loop.money_notes() == [money_note(R1), launcher.MONEY_OFF_AGAIN_NOTE.format(sha=R1[:7])]


def test_money_not_switched_again_note_when_nothing_changed(loop):
    loop.env.write_bytes(b"PAYOUTS=1\n")
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert loop.money_notes() == [money_note(R1)]                   # повторно выключать было нечего — без 2-й строки


def test_diverged_folder_neither_merges_nor_touches_money(gate):
    """В папке бота свой коммит (ветки разошлись): обновление не ставится и деньги НЕ выключаются — ничего нового
    на ПК не запускается; одно предупреждение, коммит в bad."""
    gate.diverged = True
    gate.env.write_bytes(b"PAYOUTS=1\n")
    lau = launcher.Launcher()
    assert lau.try_update() is False and not gate.merged() and gate.smoked == 0 and R1 in lau.bad
    assert gate.env.read_bytes() == b"PAYOUTS=1\n" and not gate.money_notes()
    assert len(gate.notes) == 1 and "разошлись" in gate.notes[0]
    # понятный текст: не пустой хвост ошибки git, а что случилось и что проверить
    assert R1[:7] in gate.notes[0] and HEAD[:7] in gate.notes[0] and "git log" in gate.notes[0]


def test_rollback_turns_money_off_before_reset(gate):
    """Откат после падений — тоже смена кода под деньгами: сначала PAYOUTS/TRADING=0 и уведомление, потом reset."""
    gate.head = R1
    open(launcher.LAST_GOOD, "w").write(HEAD)
    gate.env.write_bytes(b"PAYOUTS=1\nTRADING=1\n")
    lau = launcher.Launcher()
    lau.rollback()
    assert gate.env.read_bytes() == b"PAYOUTS=0\nTRADING=0\n" and gate.head == HEAD and R1 in lau.bad
    assert gate.money_notes() == [money_note(HEAD)] and any("откатил" in n for n in gate.notes)


def test_rollback_without_writable_env_keeps_code(gate, monkeypatch):
    """.env не записать — не откатываем: падающий бот денег не шлёт, а другой код с включёнными деньгами — мог бы."""
    gate.head = R1
    open(launcher.LAST_GOOD, "w").write(HEAD)

    def locked(path):
        raise PermissionError("locked")
    monkeypatch.setattr(launcher, "money_off", locked)
    launcher.Launcher().rollback()
    assert gate.head == R1 and not any(c.startswith("reset") for c in gate.git)
    assert any(n.startswith("🚨") for n in gate.notes)


def test_launcher_never_runs_exe_from_bot_folder():
    """git.exe/python.exe из папки бота (текущей) Windows не ищет: переменная ставится при импорте launcher."""
    assert os.environ.get("NoDefaultCurrentDirectoryInExePath") == "1"


def test_launcher_and_guard_protect_same_suffixes():
    guard = _load_guard()
    assert guard.PROTECTED_SUFFIXES == launcher.PROTECTED_SUFFIXES


def test_env_unwritable_right_before_new_bot_keeps_old_version(loop):
    """Перед запуском нового кода .env не записать, а в нём снова PAYOUTS=1 — новый код не стартует: откат на
    прежний коммит, работает прежняя версия; коммит не бракуется, сообщение одно."""
    loop.env.write_bytes(b"PAYOUTS=1\n")

    def old_bot_write():
        loop.env.write_bytes(b"PAYOUTS=1\n")
        (loop.env.parent / ".env.money-off.tmp").mkdir()

    loop.on_stop = old_bot_write
    lau = launcher.Launcher()
    with pytest.raises(Stop):
        lau.run()
    assert loop.runs() == [("start", HEAD), "stop", ("start", HEAD), "stop"]
    assert f"reset --hard {HEAD}" in loop.git and R1 not in lau.bad and R1 in lau.money_failed
    alarms = [n for n in loop.notes if n.startswith("🚨")]
    assert len(alarms) == 1 and "НЕ ставлю" in alarms[0] and R1[:7] in alarms[0] and HEAD[:7] in alarms[0]


@pytest.mark.parametrize("name", ["launcher.py", "LAUNCHER.PY"])
def test_launcher_update_restarts_launcher_itself(loop, name):
    """Барьер живёт в самом launcher.py: подтверждённое обновление launcher.py не должно остаться без действия до
    ручного перезапуска. Старый launcher останавливает бота, выключает деньги и выходит — run.bat поднимет новый."""
    loop.files = ["bot.py", name]
    loop.env.write_bytes(b"PAYOUTS=1\n")
    loop.approved.parent.mkdir()
    loop.approved.write_text(R1 + "\n", encoding="utf-8")
    assert launcher.Launcher().run() is None                        # вышел сам, без Stop
    assert loop.runs() == [("start", HEAD), "stop"] and loop.head == R1
    assert loop.env.read_bytes() == b"PAYOUTS=0\n"
    assert any("launcher.py обновлён" in n for n in loop.notes)


def test_run_bat_update_asks_for_manual_restart(loop):
    """run.bat cmd читает с диска по ходу выполнения — перезапускать его из launcher нельзя; владельцу — просьба
    перезапустить окно вручную, бот при этом обновляется как обычно."""
    loop.files = ["run.bat"]
    loop.approved.parent.mkdir()
    loop.approved.write_text(R1 + "\n", encoding="utf-8")
    with pytest.raises(Stop):
        launcher.Launcher().run()
    assert loop.runs() == [("start", HEAD), "stop", ("start", R1), "stop"]
    assert any("run.bat" in n and "вручную" in n for n in loop.notes if n.startswith("🔄"))


def test_bot_gets_money_switches_only_from_env_file(monkeypatch, tmp_path):
    """PAYOUTS/TRADING из окружения Windows или родителя бот не наследует (кроме «0»): включить деньги может только
    .env, а его launcher выключает после каждого обновления. Так выключение держится, даже если обновление убрало
    из bot.main вызов payouts.switch_from_file (он и защищал от PAYOUTS=1 в окружении)."""
    monkeypatch.setenv("PAYOUTS", "1")
    monkeypatch.setenv("TRADING", " 1 ")
    monkeypatch.setenv("P2P_TEST_KEEP", "да")
    env = launcher.bot_env()
    assert not {k.upper() for k in env} & {"PAYOUTS", "TRADING"} and env["P2P_TEST_KEEP"] == "да"
    monkeypatch.setenv("TRADING", "0")
    assert launcher.bot_env()["TRADING"] == "0"                     # окружением выключить можно, включить — нет
    popen = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda args, **kw: popen.append((args, kw)))
    launcher.start_bot()
    assert popen[0][1]["env"] == launcher.bot_env() and popen[0][1]["cwd"] == launcher.HERE
    # бот без switch_from_file: load_env (setdefault) поверх окружения launcher и .env после money_off
    dotenv = tmp_path / ".env"
    dotenv.write_bytes(b"PAYOUTS=1\nTRADING=1\n")
    launcher.money_off(str(dotenv))
    monkeypatch.setenv("PAYOUTS", "1")
    child = launcher.bot_env()
    for key in ("PAYOUTS", "TRADING"):
        monkeypatch.delenv(key, raising=False)
        if key in child:
            monkeypatch.setenv(key, child[key])
    p2p.load_env(str(dotenv))
    assert not payouts.enabled() and os.environ["TRADING"] == "0"


# --- защищённые пути: только после --approve ---

PROTECTED = [".github/workflows/ci.yml", "launcher.py", "LAUNCHER.PY", "run.bat", "scripts/guard.py", "CLAUDE.md",
             ".gitignore", ".gitattributes", "payouts.py", "payouts/__init__.py", "scripts/payout_whitelist.py",
             "tests/test_payouts.py", "tests/test_payout_pins.py", "tests/payout_stubs.py", "docs/PayoutNotes.md",
             "trading/venues.py", "Trading.py", "tests/trading/test_risk.py", "tests/test_launcher_money_gate.py",
             "conftest.py", "tests/conftest.py", "tests/sub/Conftest.py", "pytest.ini", "pkg/pyproject.toml",
             "setup.cfg", "tools/tox.ini", "sitecustomize.py", "lib/usercustomize.py", "evil.pth", "x/Evil.PTH",
             "requirements.txt", "tools/requirements.txt", ".env", "data/keys.json", "logs/approved_shas",
             ".last_good", ".dev_status.json", "git.exe", "tools/Evil.DLL", "x.pyd", "__pycache__/bot.cpython-310.pyc",
             "lib/x.so", "update.bat", "tools/x.CMD", "x.ps1", "json.py", "Hashlib.py", "hashlib/__init__.py",
             "email/x.py"]
NOT_PROTECTED = ["bot.py", "p2p.py", "accounts.py", "jsonstore.py", "tests/test_bot.py", "tests/helpers.py",
                 "README.md", "ROADMAP.md", ".env.example", "scripts/tool.py", "requirements-dev.md", "docs/data/x.md",
                 "tests/fixtures/bybit_ads.json", "trades.py", "cards.py", "scripts/json.py", "tests/test_json.py",
                 "docs/token.md", "calibration.py", "snapshots.py", "replay.py", "perp.py", "simmaker.py"]


def test_protected_paths_list():
    assert launcher.protected_paths(PROTECTED) == PROTECTED
    assert launcher.protected_paths(NOT_PROTECTED) == []


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_change_without_approval_not_applied(gate, path):
    gate.files = ["bot.py", path]
    gate.env.write_bytes(b"PAYOUTS=1\n")
    lau = launcher.Launcher()
    assert lau.try_update() is False
    assert not gate.merged() and gate.smoked == 0 and gate.head == HEAD and R1 not in lau.bad
    assert len(gate.notes) == 1 and gate.notes[0].startswith("🔐")
    assert f"• {path}" in gate.notes[0] and "• bot.py" not in gate.notes[0]
    assert f"python launcher.py --approve {R1}" in gate.notes[0]
    assert gate.env.read_bytes() == b"PAYOUTS=1\n"                   # прежняя версия работает как работала
    assert lau.try_update() is False and len(gate.notes) == 1       # уведомление — один раз на sha
    gate.github = R2
    assert lau.try_update() is False and len(gate.notes) == 2 and f"--approve {R2}" in gate.notes[1]


def test_protected_change_with_approval_applied(gate):
    gate.files = ["payouts.py", "bot.py"]
    gate.env.write_bytes(b"PAYOUTS=1\n")
    gate.approved.parent.mkdir()
    gate.approved.write_text(f"{R2}\n", encoding="utf-8")           # подтверждён другой коммит — этот не ставим
    lau = launcher.Launcher()
    assert lau.try_update() is False and not gate.merged()
    with open(gate.approved, "a", encoding="utf-8") as f:
        f.write(f"  {R1.upper()}  \n")
    assert lau.try_update() is True and gate.head == R1 and gate.smoked == 1
    assert f"merge --ff-only --quiet {R1}" in gate.git
    assert gate.env.read_bytes() == b"PAYOUTS=0\n" and gate.money_notes() == [money_note(R1)]


def test_approval_covers_exactly_that_sha(gate):
    """Подтверждён R1, но на GitHub уже R2 (поверх R1) — diff HEAD..R2 с защищённым файлом, R2 не подтверждён."""
    gate.files, gate.github = ["launcher.py"], R2
    gate.approved.parent.mkdir()
    gate.approved.write_text(R1 + "\n", encoding="utf-8")
    assert launcher.Launcher().try_update() is False and not gate.merged()
    assert f"--approve {R2}" in gate.notes[0]


def test_diff_failure_not_applied(gate):
    """Список изменённых файлов не получен — защищённые пути не проверить, обновление не ставится."""
    gate.diff_error = "git diff: fatal: bad object"
    assert launcher.Launcher().try_update() is False
    assert not gate.merged() and gate.smoked == 0 and gate.head == HEAD
    assert "bad object" in (gate.env.parent / "launcher.log").read_text(encoding="utf-8")


# --- python launcher.py --approve <sha> ---

TIP = "rev-parse --verify --quiet origin/main^{commit}"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """st.rev — ответ git rev-parse на начало sha, st.tip — на origin/main (локальная ссылка, без fetch)."""
    st = SimpleNamespace(git=[], rev="", tip="", approved=tmp_path / "logs" / "approved_shas", ran=[])
    monkeypatch.setattr(launcher, "APPROVED_PATH", str(st.approved))
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))

    def git(*args, check=True):
        st.git.append(" ".join(args))
        return st.tip if " ".join(args) == TIP else st.rev

    monkeypatch.setattr(launcher, "git", git)
    monkeypatch.setattr(launcher, "acquire_lock", lambda: st.ran.append("lock") or True)
    monkeypatch.setattr(launcher.Launcher, "run", lambda self: st.ran.append("run"))
    return st


def test_approve_full_sha(cli):
    assert launcher.main(["--approve", " " + R1.upper() + " "]) == 0
    assert cli.approved.read_text(encoding="utf-8") == R1 + "\n"
    assert cli.ran == [] and cli.git == [TIP]                       # цикл не запускается; git — только вершина main
    assert launcher.main(["--approve", R2]) == 0
    assert cli.approved.read_text(encoding="utf-8") == f"{R1}\n{R2}\n"
    assert launcher.approved(R1) and launcher.approved(R2.upper()) and not launcher.approved(HEAD)


def test_approve_equals_form(cli):
    assert launcher.main(["--approve=" + R1]) == 0
    assert cli.approved.read_text(encoding="utf-8") == R1 + "\n" and cli.ran == []


def test_approve_resolves_unique_prefix(cli):
    cli.rev = "1a2b" + "0" * 36
    assert launcher.main(["--approve", "1A2B"]) == 0
    assert cli.git == ["rev-parse --verify --quiet 1a2b^{commit}", TIP]
    assert cli.approved.read_text(encoding="utf-8") == cli.rev + "\n" and cli.ran == []


def test_approve_tells_when_main_moved_on(cli, capsys):
    """Launcher ставит только вершину main: подтверждение более старого коммита ничего не даст, и владелец должен
    узнать это сразу, а не по второму 🔐."""
    cli.tip = R1
    assert launcher.main(["--approve", R1]) == 0
    out = capsys.readouterr().out
    assert "Подтверждено" in out and R2 not in out
    cli.tip = R2
    assert launcher.main(["--approve", R1]) == 0
    out = capsys.readouterr().out
    assert "Подтверждено" not in out and f"python launcher.py --approve {R2}" in out and R1 in out
    assert launcher.approved(R1)                                    # записано всё равно
    cli.tip = ""                                                    # origin/main не прочитать — обычный текст
    assert launcher.main(["--approve", R2]) == 0 and "Подтверждено" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--aprove", R1], ["-approve", R1], ["approve", R1], ["--approve", R1, R2],
                                  ["--approve=" + R1, R2], ["--help"], ["x"], [R1]])
def test_unknown_arguments_do_not_start_loop(cli, argv, capsys):
    """Опечатка в --approve не должна молча запускать второй цикл обновлений и бота в этом окне."""
    assert launcher.main(argv) == 2
    assert cli.ran == [] and not cli.approved.exists()
    assert "--approve <sha>" in capsys.readouterr().out


@pytest.mark.parametrize("rev", ["", "fatal: ambiguous", "ffff" + "0" * 36, "1a2b"])
def test_approve_prefix_unknown_or_ambiguous(cli, rev):
    cli.rev = rev
    assert launcher.main(["--approve", "1a2b"]) == 2
    assert not cli.approved.exists() and cli.ran == []


@pytest.mark.parametrize("arg", [None, "", "abc", "xyz1", "g" * 40, "a" * 41, "1a2b 3c4d", "HEAD", "--help",
                                 "1a2b;del", "../../x", "b" * 39 + "x", "1a2b^{tree}"])
def test_approve_rejects_garbage(cli, arg):
    argv = ["--approve"] + ([] if arg is None else [arg])
    assert launcher.main(argv) == 2
    assert not cli.approved.exists() and cli.ran == [] and cli.git == []


def test_main_without_approve_runs_loop(cli, monkeypatch):
    assert launcher.main([]) is None and cli.ran == ["lock", "run"]
    monkeypatch.setattr(launcher, "acquire_lock", lambda: False)
    assert launcher.main([]) == 3


# --- списки launcher и guard совпадают ---

def _samples(prefixes, basenames, exact, names):
    """Пути, которые список обязан защищать: сами префиксы, файлы внутри папок, имена на глубине, точные пути,
    слова в пути, *.pth — и всё то же ЗАГЛАВНЫМИ (Windows запишет LAUNCHER.PY поверх launcher.py)."""
    out = (list(prefixes) + [p + "x.py" for p in prefixes if p.endswith("/")] + [f"deep/dir/{b}" for b in basenames]
           + list(exact) + [f"x/my_{n}_notes.txt" for n in names] + ["a/b/x.pth"]
           + [f"a/b/x{s}" for s in launcher.PROTECTED_SUFFIXES] + ["json.py", "hashlib/x.py", "a/__pycache__/x.pyc"])
    return out + [s.upper() for s in out]


def test_launcher_list_covers_guard():
    """Всё, что guard (CI) считает защищённым, launcher тоже не поставит без подтверждения."""
    guard = _load_guard()
    assert "tests/test_launcher_money_gate.py" in guard.PROTECTED
    names = _samples(guard.PROTECTED, guard.PROTECTED_BASENAMES, guard.PROTECTED_EXACT, guard.PROTECTED_NAMES)
    assert [n for n in names if not launcher.protected_paths([n])] == []


def test_guard_covers_launcher_list():
    """И наоборот: что launcher без подтверждения не поставит, guard не пустит в автомерж. Иначе CI вливал бы такой
    коммит, а launcher потом стоял бы на нём (и на всём, что придёт следом) до ручного --approve."""
    guard = _load_guard()
    names = _samples(launcher.PROTECTED_PREFIXES, launcher.PROTECTED_BASENAMES, launcher.PROTECTED_EXACT,
                     launcher.PROTECTED_NAMES)
    assert [n for n in names if not guard.protected(n)] == []
    assert [n for n in NOT_PROTECTED if guard.protected(n)] == []


def test_claude_md_names_every_protected_path():
    """О защищённых путях облачная рутина узнаёт только из CLAUDE.md: там назван каждый путь из списка launcher."""
    with open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8") as f:
        text = f.read().lower()
    listed = (launcher.PROTECTED_PREFIXES + launcher.PROTECTED_BASENAMES + launcher.PROTECTED_EXACT
              + launcher.PROTECTED_NAMES + tuple("*" + s for s in launcher.PROTECTED_SUFFIXES))
    assert [p for p in listed if f"`{p}`" not in text] == []
