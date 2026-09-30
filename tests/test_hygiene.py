"""AST-сторож: каждая тестовая функция верхнего уровня tests/test_*.py должна на самом деле что-то проверять
(assert, pytest.raises/warns, pytest.fail или вызов вроде assert_called/assert_ok) — иначе тест «зелёный», даже
если ничего не делает (так было в tests/test_simfunding.py:154 до задачи test-hygiene-assert-guard, см. соседний
tests/test_sim_tick.py). tests/trading/ вне охвата (защищённый путь): там проверка через свои хелперы bad()/ok()."""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _test_functions(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]


def _call_name(node):
    if not isinstance(node, ast.Call):
        return None
    try:
        return ast.unparse(node.func)
    except Exception:
        return None


def _has_check(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                if _call_name(item.context_expr) in ("pytest.raises", "pytest.warns"):
                    return True
        if isinstance(sub, ast.Call):
            name = _call_name(sub)
            if name is None:
                continue
            if name == "pytest.fail":
                return True
            if "assert" in name.rsplit(".", 1)[-1].lower():
                return True
    return False


def untested(source, filename="<src>"):
    """[(lineno, имя), ...] тестовых функций (def test*, методы классов и async def тоже) без проверки внутри:
    ast.Assert; with pytest.raises(...)/pytest.warns(...); вызов pytest.fail(...); вызов, у последнего сегмента
    имени которого встречается 'assert' в нижнем регистре (assert_called, assert_ok...)."""
    tree = ast.parse(source, filename=filename)
    return [(n.lineno, n.name) for n in _test_functions(tree) if not _has_check(n)]


def test_detector_flags_only_tests_without_checks():
    source = '''
import pytest


def test_no_check():
    x = 1 + 1


def test_with_assert():
    assert 1 == 1


def test_with_raises():
    with pytest.raises(ValueError):
        raise ValueError("x")


def test_with_fail_call():
    pytest.fail("x")


async def test_async_with_assert():
    assert True


class TestGroup:
    def test_method_with_helper(self):
        self.assert_ok(1)


def helper_not_a_test():
    pass
'''
    assert [name for _lineno, name in untested(source)] == ["test_no_check"]


def test_every_test_function_has_an_assertion():
    files = sorted(TESTS.glob("test_*.py"))
    total = 0
    offenders = []
    for f in files:
        source = f.read_text(encoding="utf-8")
        total += len(_test_functions(ast.parse(source, filename=str(f))))
        offenders += [f"{f.name}:{lineno}:{name}" for lineno, name in untested(source, filename=str(f))]
    assert len(files) >= 50 and total >= 1000   # защита от пустого прохождения при неверном пути/glob
    assert not offenders, "Тесты без проверки (добавь assert, pytest.raises или хелпер assert_*): " + ", ".join(offenders)
