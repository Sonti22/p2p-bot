"""Каждая настройка, которую код читает из окружения (os.getenv / os.environ и обёртки над ними — env_float, _on,
_env_parsed, _list…), описана в .env.example. Разбор — по AST всего кода бота (без tests/): новый os.getenv без строки в
.env.example — красный тест."""
import ast
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# переменные окружения системы, а не настройки бота
SYSTEM = {"WINDIR"}


def _files():
    out = []
    for pattern in ("*.py", "scripts/*.py", "research/*.py"):
        out += glob.glob(os.path.join(ROOT, pattern))
    return sorted(out)


def _is_environ(node):
    """os.environ / environ."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (isinstance(node, ast.Name)
                                                                            and node.id == "environ")


def _direct_getenv(call):
    """Вызов читает окружение по первому аргументу: os.getenv(x), getenv(x), os.environ.get(x)."""
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "getenv":
        return True
    if isinstance(f, ast.Name) and f.id == "getenv":
        return True
    return isinstance(f, ast.Attribute) and f.attr in ("get", "setdefault") and _is_environ(f.value)


def _callee(call, module, aliases):
    """(модуль, имя) вызываемой функции: имя в этом модуле, модуль.имя или локальный псевдоним (f = perp.env_float)."""
    f = call.func
    if isinstance(f, ast.Name):
        return aliases.get((module, f.id), (module, f.id))
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return (f.value.id, f.attr)
    return None


def _first_arg(call):
    return call.args[0] if call.args else None


def env_names():
    """{имя настройки: {файлы}} — все строки-имена, которые код читает из окружения."""
    trees = {}
    for path in _files():
        with open(path, encoding="utf-8") as fh:
            trees[os.path.splitext(os.path.basename(path))[0], path] = ast.parse(fh.read())
    wrappers = set()   # (модуль, функция): её первый параметр уходит в чтение окружения
    aliases = {}
    changed = True
    while changed:     # обёртки над обёртками — до неподвижной точки
        changed = False
        for (module, _), tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    v = node.value
                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and (v.value.id, v.attr) in wrappers:
                        if aliases.get((module, node.targets[0].id)) != (v.value.id, v.attr):
                            aliases[(module, node.targets[0].id)] = (v.value.id, v.attr)
                            changed = True
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.args.args:
                    continue
                param = node.args.args[0].arg
                for call in ast.walk(node):
                    if not isinstance(call, ast.Call):
                        continue
                    arg = _first_arg(call)
                    if not (isinstance(arg, ast.Name) and arg.id == param):
                        continue
                    if _direct_getenv(call) or _callee(call, module, aliases) in wrappers:
                        if (module, node.name) not in wrappers:
                            wrappers.add((module, node.name))
                            changed = True
    names = {}
    for (module, path), tree in trees.items():
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Call) and (_direct_getenv(node) or _callee(node, module, aliases) in wrappers):
                key = _first_arg(node)
            elif isinstance(node, ast.Subscript) and _is_environ(node.value):
                key = node.slice
            elif isinstance(node, ast.Compare) and any(_is_environ(c) for c in node.comparators):
                key = node.left
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+",
                                                                                              key.value):
                names.setdefault(key.value, set()).add(rel)
    return names


def documented():
    with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as fh:
        return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", fh.read(), re.M))


def test_scanner_sees_direct_reads_and_wrappers():
    names = env_names()
    for key in ("TG_TOKEN", "MIN_ORDERS", "AMOUNT", "EXCHANGES", "PERP_INTERVAL", "DIR_RISK_USDT", "DEPTH_PAGE2",
                "CAL_MIN_N", "PAPER_AMOUNT"):
        assert key in names, key


def test_every_env_setting_is_documented_in_env_example():
    missing = {k: sorted(v) for k, v in env_names().items() if k not in documented() and k not in SYSTEM}
    assert not missing, f"нет в .env.example: {missing}"
