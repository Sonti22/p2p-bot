"""Контрактный тест команд: меню (COMMANDS) = dispatch = /help = README = owner-guide, плюс гостевая поверхность
(GUEST_CMDS) владельческих документов (README, docs/owner-guide.md). Каждая команда описана везде — новая команда
без строки в справке/README/owner-guide даёт красный тест с понятным сообщением, куда дописать строку.
Денежная команда (allowlist по '/pay*') не документируется здесь отдельным разделом, см. «Выплаты» в CLAUDE.md."""
import ast
import os
import re

import bot as B

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dispatch_commands():
    """Все строки-команды ('/[a-z_]+') внутри тела единственного async def dispatch в bot.py — то, что реально
    обрабатывается (AST, а не текстовый grep: не путает строки внутри docstring/комментариев с веткой elif)."""
    src = open(os.path.join(ROOT, "bot.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "dispatch"]
    assert len(funcs) == 1, f"dispatch должен быть один, найдено {len(funcs)}"
    cmds = set()
    for n in ast.walk(funcs[0]):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.fullmatch(r"/[a-z_]+", n.value):
            cmds.add(n.value)
    return cmds


MENU = {"/" + c["command"] for c in B.COMMANDS}
# денежная команда (allowlist по префиксу, без имени целиком: имя закреплено отдельным хэш-пином, см. CLAUDE.md,
# раздел «Выплаты» — здесь оно не копируется и не пишется целиком) — её описывает отдельный раздел, в общий
# контракт не идёт
MONEY = {c for c in MENU if c.startswith("/pay")}
DOCUMENTED = MENU - {"/help"} - MONEY


def mentioned(text, cmd):
    """Команда `cmd` встречается в `text` как отдельное слово (не часть более длинной команды/пути)."""
    return re.search(r"(?<![\w/])" + re.escape(cmd) + r"(?!\w)", text)


def test_every_menu_command_is_handled():
    dispatch = dispatch_commands()
    missing = (MENU - {"/help"}) - dispatch
    assert not missing, f"в меню (COMMANDS), но не обрабатывается в dispatch: {sorted(missing)}"
    missing_guest = B.GUEST_CMDS - (dispatch | {"/help"})
    assert not missing_guest, f"в GUEST_CMDS, но не обрабатывается в dispatch: {sorted(missing_guest)}"


def test_hidden_commands_are_pinned():
    """dispatch обрабатывает и команды без пункта меню (/help-ветка не считается, т.к. /help сама в меню есть) —
    список известен и осознанно закреплён здесь: новая скрытая команда требует явной правки этого теста, а не
    тихого расширения контракта."""
    hidden = dispatch_commands() - MENU
    assert hidden == {"/start", "/allow", "/deny", "/amount", "/min", "/calibration", "/trading"}, sorted(hidden)


def test_menu_commands_in_help_sections():
    text = "\n".join(t for _, t in B.HELP_SECTIONS.values())
    missing = [c for c in sorted(DOCUMENTED) if not mentioned(text, c)]
    assert not missing, f"нет строки в HELP_SECTIONS (bot.py) для: {missing}"


def test_menu_commands_in_readme():
    text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    missing = [c for c in sorted(DOCUMENTED) if not mentioned(text, c)]
    assert not missing, f"нет строки в README.md для: {missing}"


def test_menu_commands_in_owner_guide():
    text = open(os.path.join(ROOT, "docs", "owner-guide.md"), encoding="utf-8").read()
    missing = [c for c in sorted(DOCUMENTED) if not mentioned(text, c)]
    assert not missing, f"нет строки в таблице раздела 4 docs/owner-guide.md для: {missing}"


def test_guest_commands_match_owner_guide_marks():
    text = open(os.path.join(ROOT, "docs", "owner-guide.md"), encoding="utf-8").read()
    marked = set(re.findall(r"`(/[a-z_]+)[^`]*`\s*\(гость\)", text))
    assert marked == B.GUEST_CMDS - {"/start", "/help"}
    assert B.GUEST_CMDS - {"/start"} <= MENU


def test_readme_guest_block_matches_guest_cmds():
    text = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    m = re.search(r"^Рыночные \(доступны и гост.*?\n(.*?)\nТолько для владельца:", text, re.S | re.M)
    assert m, "не нашёл в README.md блок 'Рыночные (доступны и гостям…)' / 'Только для владельца:'"
    block = m.group(1)
    found = set(re.findall(r"^- `(/[a-z_]+)", block, re.M))
    assert found == B.GUEST_CMDS - {"/start", "/help"}
