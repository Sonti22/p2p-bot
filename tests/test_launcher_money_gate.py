"""Локальный барьер launcher (защищённый файл, правится только вручную): после обновления кода выплаты и торговля
выключены (PAYOUTS/TRADING=0 в .env), а обновление с защищёнными файлами ставится только после
python launcher.py --approve <sha> на ПК. CI на GitHub запускает ci.yml из самой ветки — верить ему нельзя, это
последний барьер перед запуском нового кода рядом с ключами.

Настоящие git, pytest и Telegram не трогаем: подменяются git, smoke, notify; все пути — во временной папке."""
import importlib.util
import os
from types import SimpleNamespace

import pytest

import launcher
import payouts

HEAD, R1, R2 = "a" * 40, "b" * 40, "c" * 40


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
                         diff_error=None, env=tmp_path / ".env", approved=paths["APPROVED_PATH"])

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
    st.merged = lambda: any(c.startswith("merge") for c in st.git)
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
    assert gate.notes[0].startswith("🔄") and gate.money_notes() == [money_note(R1)]
    gate.github = R2                                                # следующее обновление: деньги уже выключены
    assert lau.try_update() is True and gate.head == R2
    assert gate.money_notes() == [money_note(R1)]                   # сообщение — одно, только когда выключили


def test_code_update_without_money_lines_no_money_note(gate):
    gate.env.write_bytes(b"TG_CHAT_ID=42\n")
    assert launcher.Launcher().try_update() is True
    assert gate.money_notes() == [] and gate.env.read_bytes() == b"TG_CHAT_ID=42\n"


@pytest.mark.parametrize("blocker", ["tmp", "env"])
def test_unwritable_env_update_not_applied(gate, blocker):
    """.env не записать (или не прочитать) — новый код не стартует: откат на прежний коммит, громкое сообщение
    один раз; коммит не бракуется — починят .env, и следующая проверка его поставит."""
    if blocker == "tmp":
        gate.env.write_bytes(b"PAYOUTS=1\n")
        (gate.env.parent / ".env.money-off.tmp").mkdir()
    else:
        gate.env.mkdir()                                            # .env — папка: open() падает с OSError
    lau = launcher.Launcher()
    assert lau.try_update() is False
    assert gate.head == HEAD and f"reset --hard {HEAD}" in gate.git and R1 not in lau.bad
    assert len(gate.notes) == 1 and gate.notes[0].startswith("🚨") and "НЕ ставлю" in gate.notes[0]
    assert R1[:7] in gate.notes[0] and HEAD[:7] in gate.notes[0] and not gate.money_notes()
    assert lau.try_update() is False and gate.smoked == 2 and gate.head == HEAD
    assert len(gate.notes) == 1                                     # без спама на каждой проверке
    if blocker == "tmp":
        (gate.env.parent / ".env.money-off.tmp").rmdir()
        assert lau.try_update() is True and gate.head == R1
        assert gate.env.read_bytes() == b"PAYOUTS=0\n" and gate.money_notes() == [money_note(R1)]


def test_docs_only_update_unchanged(gate):
    """Только .md — как раньше: без смоука, без перезапуска и без выключения денег."""
    gate.files = ["README.md", "docs/заметки.md"]
    gate.env.write_bytes(b"PAYOUTS=1\nTRADING=1\n")
    assert launcher.Launcher().try_update() is False
    assert gate.head == R1 and gate.smoked == 0 and gate.merged()
    assert gate.env.read_bytes() == b"PAYOUTS=1\nTRADING=1\n"
    assert gate.notes == [f"📝 Обновлена документация ({R1[:7]}), бот не перезапускал"]


# --- защищённые пути: только после --approve ---

PROTECTED = [".github/workflows/ci.yml", "launcher.py", "LAUNCHER.PY", "run.bat", "scripts/guard.py", "CLAUDE.md",
             ".gitignore", ".gitattributes", "payouts.py", "payouts/__init__.py", "scripts/payout_whitelist.py",
             "tests/test_payouts.py", "tests/test_payout_pins.py", "tests/payout_stubs.py", "docs/PayoutNotes.md",
             "trading/venues.py", "Trading.py", "tests/trading/test_risk.py", "tests/test_launcher_money_gate.py",
             "conftest.py", "tests/conftest.py", "tests/sub/Conftest.py", "pytest.ini", "pkg/pyproject.toml",
             "setup.cfg", "tools/tox.ini", "sitecustomize.py", "lib/usercustomize.py", "evil.pth", "x/Evil.PTH",
             "requirements.txt", "tools/requirements.txt", ".env", "data/keys.json", "logs/approved_shas",
             ".last_good", ".dev_status.json"]
NOT_PROTECTED = ["bot.py", "p2p.py", "accounts.py", "jsonstore.py", "tests/test_bot.py", "tests/helpers.py",
                 "README.md", "ROADMAP.md", ".env.example", "scripts/tool.py", "requirements-dev.md", "docs/data/x.md",
                 "tests/fixtures/bybit_ads.json", "trades.py", "cards.py"]


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

@pytest.fixture
def cli(monkeypatch, tmp_path):
    st = SimpleNamespace(git=[], rev="", approved=tmp_path / "logs" / "approved_shas", ran=[])
    monkeypatch.setattr(launcher, "APPROVED_PATH", str(st.approved))
    monkeypatch.setattr(launcher, "LOG_PATH", str(tmp_path / "launcher.log"))

    def git(*args, check=True):
        st.git.append(" ".join(args))
        return st.rev

    monkeypatch.setattr(launcher, "git", git)
    monkeypatch.setattr(launcher, "acquire_lock", lambda: st.ran.append("lock") or True)
    monkeypatch.setattr(launcher.Launcher, "run", lambda self: st.ran.append("run"))
    return st


def test_approve_full_sha(cli):
    assert launcher.main(["--approve", " " + R1.upper() + " "]) == 0
    assert cli.approved.read_text(encoding="utf-8") == R1 + "\n"
    assert cli.ran == [] and cli.git == []                          # цикл не запускается, git не нужен
    assert launcher.main(["--approve", R2]) == 0
    assert cli.approved.read_text(encoding="utf-8") == f"{R1}\n{R2}\n"
    assert launcher.approved(R1) and launcher.approved(R2.upper()) and not launcher.approved(HEAD)


def test_approve_resolves_unique_prefix(cli):
    cli.rev = "1a2b" + "0" * 36
    assert launcher.main(["--approve", "1A2B"]) == 0
    assert cli.git == ["rev-parse --verify --quiet 1a2b^{commit}"]
    assert cli.approved.read_text(encoding="utf-8") == cli.rev + "\n" and cli.ran == []


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


# --- список launcher покрывает guard ---

def test_launcher_list_covers_guard():
    """Всё, что guard (CI) считает защищённым, launcher тоже не поставит без подтверждения."""
    spec = importlib.util.spec_from_file_location(
        "guard_script", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert "tests/test_launcher_money_gate.py" in guard.PROTECTED
    names = list(guard.PROTECTED) + [f"deep/dir/{b}" for b in guard.PROTECTED_BASENAMES] + ["a/b/x.pth"]
    assert launcher.protected_paths(names) == names
    assert launcher.protected_paths([f"x/{guard.PROTECTED_NAME}_notes.txt"])
