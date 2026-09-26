"""Пины зависимостей выплат (защищённый файл, правится только вручную владельцем).

payouts.py и тесты выплат защищены guard и launcher, но выплаты опираются и на код в незащищённых файлах: подпись и
ключи в accounts.py, чтение JSON в jsonstore.py, чтение .env при старте в p2p.py, запись .env, проверку «владелец или
гость» и экраны выплат в bot.py.
Здесь — sha256 исходника каждой такой функции и константы (ast.get_source_segment вместе с декораторами, переводы
строк нормализованы). Любая правка любой из них — красный тест, а значит и смоук launcher: обновление не встанет, пока
владелец не проверит изменение и не обновит пин. Плюс — кто в accounts.py и payouts.py вообще может слать POST.
"""
import ast
import hashlib
import inspect
import os

import pytest

import accounts
import bot as B
import jsonstore
import p2p
import payouts

HOW_TO_UPDATE = (
    "Изменился код, от которого зависят выплаты. Обновить пин может только владелец, проверив само изменение: "
    "посмотри diff этих функций (git diff), убедись, что выплаты не ослаблены, и впиши новый хэш из этого сообщения в "
    "PINS в tests/test_payout_pins.py. Это защищённый файл: правка — только вручную, мерж — после проверки владельцем "
    "(на ПК — python launcher.py --approve <sha>). Облачной рутине пины не менять.")

# имена в bot.py, которые пинуются все, сколько их есть (новая функция/константа с таким именем без пина — тоже красный)
BOT_PREFIXES = ("payout", "PAYOUT_", "GUEST_")
# и отдельно: запись .env, ворота «владелец/гость» и остальное, через что гость мог бы дойти до кнопок выплат
BOT_EXTRA = ("save_env", "Bot._owner_gate", "Bot.is_guest", "Bot.on_guest_callback", "Bot.cmd_payout")

PINS = {
    "accounts": {
        "CRYPTOMUS_BASE": "d50e7f53cd6012099468fe912b7efdc27eac511ca5ca4c05bd79bc8dd5237636",
        "DPAPI_PREFIX": "558fa5806742bd9dece7f93fc15499948c74c69c3724d6a97fafe265411376c4",
        "KEYS_PATH": "bcdeb6ae8b057cdb6fbfe88423177cb553cc0421de5b60d76ec912b2555ca73a",
        "_dpapi": "e05c4989f06bb3225abce35ae8fca035f0149e4b285b0f0ea2472183c148ca7d",
        "_json_no_redirect": "cce7ba856bee23dd5e1fa262ddcc67f386bc736f5bcd2952a9c612dd28de1b13",
        "_keys_file": "f2562b36bc299ad07fbb421381d0cb00911e58066a7dca2b6ae75a0861d46f93",
        "_scrub": "3146a0186a5c4e8da7816f3b707295e02ff572db3321f2177eaeec11c9d0108f",
        "api_error_text": "81d9084f8708a800e2d4dd8520502c27c2aaff9050e15fa0c68768a4823dd2ac",
        "cryptomus_sign": "2d1e3c3bab112ce82a952ac496c0d90375a8134f5411a4e0ec792183ddeb8d15",
        "keys": "023f663c307679982718475f03ec298304566c0961452453738e034535fb25c9",
        "unprotect": "80e72cb3c549e601b1f4386028baceb430f2161fb007b6d06fad99e3f95e163e",
    },
    "jsonstore": {
        "read_dict": "5a8483b471a9d1a2a28086bfb5be90a1f2ffabd41f069b256d8712fa6c2db609",
    },
    "p2p": {   # чтение .env при старте бота: от него зависит, что PAYOUTS=0 от launcher дойдёт до процесса
        "ENV_PATH": "b798b14e84cc4dc7fb9a2a071f5738cb7ad4e90e60e2c599be38012c46f27391",
        "HERE": "23b6448daffea6947fa94a8eeb3549908f2d11dfd93427a81564c4f1c6f23726",
        "load_env": "fd47832c89bca7a4b7076c3c7aa28e5b08ed6a92e4820ecd4230b3a3afbcf600",
    },
    "bot": {
        "Bot._owner_gate": "791b619f4d5e5c423537f02c7d737b3b1015cf7a593c2918c59698bc68b8d307",
        "Bot.cmd_payout": "2ae4aa84ef8550ce13d865bfbca73263f1e3367186ea874cb029687389490403",
        "Bot.is_guest": "6ec5bae4210f98d830738dd39b2a4af415903dc282cec6b7491d6afc5236ddff",
        "Bot.on_guest_callback": "b07a402c731d1cd1caece87305842e5dd9a77760512a5ace172923d5bf47330d",
        "Bot.payout_amount": "21359a591d61369ae8c9c1705eb2e31165418d816ec296360da0d06d59efca0d",
        "Bot.payout_callback": "44e229587f144ff556f5ff6bf0107d36531a3060e94e4925fc513b2fc54ddbe9",
        "Bot.payout_confirm": "26995ae3ff79a4633d1133e2302c4141734f31c4e4b6c17bd2cfc1fa2bbedd2a",
        "Bot.payout_pick": "3951617dc74926cd7e9f6140e8612f408ebce2e347c88b14ba46b4e6735749ce",
        "Bot.payout_send": "373d2f464362e2f005ce26e7719fdbe1705599ed8d43a203089edd6d22524fb9",
        "Bot.payout_stop": "e255028eaf105ef2ba69356461441efac11dfe8475c94be6b38d8d4196f68929",
        "Bot.payouts_loop": "64444f069607db42adaea966047b5b03c1b54c96e2207419b9a0a055c5a36c50",
        "GUEST_CALLBACKS": "9f3b2225ed8fddade9c108bff262cad324ad7c76a862af77f764e65d7066da0a",
        "GUEST_CMDS": "0ead04bcc22a7abedf6cb785df1d7bbb24ebd594359d07ebba83a1461de5f9ff",
        "GUEST_DENIED": "e18c4c51af2df5fcce2f554bc1f29bad93fe38f1b732a32bf4f35ebdf41ce158",
        "GUEST_MENU": "6f3ed0f724a01f2ebcd227b17e5db6c776db8a67721df4a9ceb54453a91a84df",
        "GUEST_WELCOME": "2dbf8c1fb14980ad2e21a41f95ae64fa9470d9892d4250810534a108a4ece4cc",
        "PAYOUT_KEY_HINT": "fbeff759d7214640106979b7504e4c446ff3cba20b74464b7e05e9810fd5bc0d",
        "PAYOUT_OFF_HINT": "a2cb4f5402dd737587d00a5386307e1d048100563a22169c35eec29acc83b561",
        "PAYOUT_STOPPED": "5cf90f6474157bedd939384660a600c8311a78fa435736beeec8e3adb15545be",
        "PAYOUT_STOPPED_NO_ENV": "ded3033d3093efbe51c87c25bb328b5fa6b085b4abb1e28f78101356fd8ece78",
        "PAYOUT_STOP_BTN": "91a2c0fafb645c0689d5884689e4a9ec5f1f2d29f0e5d180f674372f7d7c97b0",
        "PAYOUT_WL_HINT": "43522d24f8bbe2845782a973b9946fade0ba919076eecde24238eb8b350570cd",
        "_payout_what": "c22eae57ebe59a0e948934f0833a0608d1717205af7cec28d12b49fc24f971ad",
        "payout_event_text": "8ff8fd58dc0aed4f5a54b5f270b531bb2566ee9dfd3cb8f4394fc095802b7c3c",
        "payout_history_view": "404c2ba20dc0abc95f6e65405df3079bfb4c338df9ae51a6e80806e2285b89c9",
        "payout_menu_view": "f626daa263d612278379cf7e61d0f76bbf0b873d419b1feb4e57d4a577c151dc",
        "payout_preview_view": "fc1b74160ac07ed24b10f9671405699aa67be93e896f13aef5f9852ec3c2f549",
        "payout_result_text": "549686c284bcb0e2ed38bedcee97d8fea58e98c143d48924f3dd95b21de9a9bb",
        "save_env": "6cc5a56e2078e62bb5489c04208d803690bee7209b0d156059911b3f217f9527",
    },
}
MODULES = {"accounts": accounts, "jsonstore": jsonstore, "p2p": p2p, "bot": B}


def _source(mod):
    with open(mod.__file__, encoding="utf-8") as f:   # текстовый режим: \r\n и \r → \n
        return f.read().replace("\r\n", "\n").replace("\r", "\n")


def _bound_names(target):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for t in target.elts:
            yield from _bound_names(t)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _definitions(body, prefix=""):
    """{имя: [узлы]} — всё, что связывает имя на этом уровне (def, class, присваивание, import, for, with…),
    включая вложенные if/try/for/while/with того же уровня."""
    found = {}

    def add(name, node):
        found.setdefault(prefix + name, []).append(node)

    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node.name, node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in getattr(node, "targets", None) or [node.target]:
                for name in _bound_names(t):
                    add(name, node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                add((a.asname or a.name).split(".")[0], node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for name in _bound_names(node.target):
                add(name, node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for name in _bound_names(item.optional_vars):
                        add(name, node)
        for field in ("body", "orelse", "finalbody", "handlers"):
            inner = getattr(node, field, None)
            if isinstance(inner, list) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for k, v in _definitions(inner, prefix).items():
                    found.setdefault(k, []).extend(v)
        if isinstance(node, ast.ExceptHandler) and node.name:
            add(node.name, node)
    return found


def _names_of(mod):
    """{имя или "Класс.метод": [узлы]} модуля и его классов верхнего уровня."""
    tree = ast.parse(_source(mod))
    names = _definitions(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.update(_definitions(node.body, node.name + "."))
    return tree, names


def _digest(src, node):
    parts = [ast.get_source_segment(src, d) for d in getattr(node, "decorator_list", [])]
    parts.append(ast.get_source_segment(src, node))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _pinned_names(mod_name, names):
    if mod_name != "bot":
        return set(PINS[mod_name])
    own = {n for n in names if n.split(".")[-1].lstrip("_").startswith(BOT_PREFIXES)
           and n.split(".")[-1] not in ("payouts",)}   # import payouts — модуль, он защищён сам
    return own | set(BOT_EXTRA)


def current_pins():
    """{модуль: {имя: sha256}} — по нынешнему коду (для сравнения и для обновления пина владельцем)."""
    out = {}
    for mod_name, mod in MODULES.items():
        src = _source(mod)
        _, names = _names_of(mod)
        out[mod_name] = {n: _digest(src, names[n][0]) for n in sorted(_pinned_names(mod_name, names)) if n in names}
    return out


def test_payout_dependencies_unchanged():
    now = current_pins()
    problems = []
    for mod_name, pins in PINS.items():
        for name in sorted(set(pins) | set(now[mod_name])):
            if name not in now[mod_name]:
                problems.append(f"{mod_name}.{name}: пропало из кода (пин {pins[name][:12]}…)")
            elif name not in pins:
                problems.append(f'{mod_name}.{name}: нового имени нет в PINS — "{name}": "{now[mod_name][name]}"')
            elif pins[name] != now[mod_name][name]:
                problems.append(f'{mod_name}.{name}: изменилось — новый пин "{name}": "{now[mod_name][name]}"')
    assert not problems, HOW_TO_UPDATE + "\n" + "\n".join(problems)


@pytest.mark.parametrize("mod_name", sorted(MODULES))
def test_pinned_names_bound_once_and_live(mod_name):
    """Пин исходника не обойти второй привязкой имени (save_env = … ниже по файлу, import поверх, global внутри
    функции), обёрткой-декоратором или подменой из другого модуля при импорте: имя связано ровно один раз, и в памяти
    лежит ровно тот объект, чей исходник запинен."""
    mod = MODULES[mod_name]
    tree, names = _names_of(mod)
    pinned = [n for n in PINS[mod_name] if n in names]
    for name in pinned:
        assert len(names[name]) == 1, f"{mod_name}.{name}: имя связывается {len(names[name])} раз — {HOW_TO_UPDATE}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            assert not set(node.names) & {n.split(".")[-1] for n in pinned}, f"{mod_name}: global {node.names}"
    for name in pinned:
        node = names[name][0]
        owner = mod
        for part in name.split(".")[:-1]:
            owner = getattr(owner, part)
        live = inspect.getattr_static(owner, name.split(".")[-1])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            code = getattr(live, "__code__", None)
            assert code is not None and os.path.normcase(code.co_filename) == os.path.normcase(mod.__file__) \
                and code.co_firstlineno == first, f"{mod_name}.{name}: в памяти не та функция, что в исходнике"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            try:
                expected = ast.literal_eval(node.value)
            except ValueError:
                continue
            assert live == expected, f"{mod_name}.{name}: значение в памяти не совпадает с исходником"


def _fake_module(tmp_path, text):
    path = tmp_path / "fake.py"
    path.write_bytes(text.encode("utf-8"))
    return type("M", (), {"__file__": str(path)})


def test_pin_checks_see_rebinding_decorators_and_new_names(tmp_path):
    """Сама проверка пинов: вторая привязка имени, новый декоратор, CRLF и новая функция payout_* в bot.py."""
    def fake(text):
        return _fake_module(tmp_path, text)

    base = "def save_env(k, v):\n    return 1\n"
    src = _source(fake(base))
    _, names = _names_of(fake(base))
    pin = _digest(src, names["save_env"][0])
    assert _digest(_source(fake(base.replace("\n", "\r\n"))), names["save_env"][0]) == pin   # CRLF не меняет пин
    deco = "import functools\n@functools.lru_cache\n" + base
    _, names2 = _names_of(fake(deco))
    assert _digest(_source(fake(deco)), names2["save_env"][0]) != pin
    for tail in ("save_env = print\n", "from os import getenv as save_env\n", "if True:\n    save_env = print\n",
                 "try:\n    pass\nexcept Exception as save_env:\n    pass\n", "for save_env in ():\n    pass\n"):
        _, names3 = _names_of(fake(base + tail))
        assert len(names3["save_env"]) == 2, tail
    cls = "class Bot:\n    def payout_new(self):\n        pass\n    def _payout_x(self):\n        pass\n"
    _, names4 = _names_of(fake(base + cls + "PAYOUT_Y = 1\nGUEST_Z = 2\nimport payouts\n"))
    assert _pinned_names("bot", names4) >= {"Bot.payout_new", "Bot._payout_x", "PAYOUT_Y", "GUEST_Z", "save_env"}
    assert "payouts" not in _pinned_names("bot", names4)


SEND_METHODS = ("post", "put", "delete", "patch", "request", "_request", "send", "urlopen")
# поиск метода по имени-строке: getattr(s, "post"), s.__getattribute__("post"), operator.methodcaller("post")…
DYNAMIC_LOOKUP = ("getattr", "__getattribute__", "attrgetter", "methodcaller")
# другие сетевые клиенты — любой их импорт в accounts.py/payouts.py уже отправитель (запрос без атрибута .post:
# requests.post как имя, urlopen(Request(..., data=...)), http.client…); из aiohttp — функции/классы, шлющие запрос
NET_MODULES = ("requests", "httpx", "urllib3", "urllib.request", "http.client", "http", "socket", "ssl", "asyncio.streams")
AIOHTTP_SENDERS = ("request", "ClientSession", "ClientRequest", "TCPConnector", "Session")


def _senders(mod):
    """Имена функций модуля (или <module>/<lambda>), которые могут отправить POST/PUT/DELETE/PATCH: любое обращение
    к атрибуту .post/.put/.delete/.patch/.request/._request (вызов, псевдоним op = s.post, functools.partial(s.post))
    и поиск метода по имени — getattr/__getattribute__/attrgetter/methodcaller с таким именем строкой или с именем,
    которое вычисляется (не строка). Нарочно запутанный код (exec, vars(...)["po" + "st"]) этим не поймать — это
    остаточный риск из ROADMAP; здесь — всё, что пишется без умысла спрятать."""
    found = set()

    def dynamic(call):
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name not in DYNAMIC_LOOKUP:
            return False
        args = call.args[1:2] if name in ("getattr",) else call.args[:1]
        return any(not (isinstance(a, ast.Constant) and isinstance(a.value, str))
                   or a.value.lower() in SEND_METHODS for a in args) or not args

    def net_import(node):
        if isinstance(node, ast.Import):
            return any(a.name in NET_MODULES or a.name.split(".")[0] in NET_MODULES for a in node.names)
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            return (mod in NET_MODULES or mod.split(".")[0] in NET_MODULES
                    or mod.split(".")[0] == "aiohttp" and any(a.name in AIOHTTP_SENDERS for a in node.names))
        return False

    def visit(node, where):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            where = node.name
        elif isinstance(node, ast.Lambda):
            where = "<lambda>"
        if (isinstance(node, ast.Attribute) and node.attr in SEND_METHODS
                or isinstance(node, ast.Call) and dynamic(node) or net_import(node)):
            found.add(where)
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(ast.parse(_source(mod)), "<module>")
    return found


def test_only_known_senders_of_state_changing_requests():
    """POST/PUT/DELETE/PATCH: в accounts.py — только bybit_post (свой allowlist путей, история P2P) и cryptomus_call
    (allowlist чтения), в payouts.py — только payout_call (allowlist выплат)."""
    assert _senders(accounts) == {"bybit_post", "cryptomus_call"}
    assert _senders(payouts) == {"payout_call"}
    assert accounts.BYBIT_POST_PATHS == frozenset({"/v5/p2p/order/simplifyList"})


@pytest.mark.parametrize("body", [
    "def f(s):\n    return s.post('u')\n",
    "def f(s):\n    op = s.post\n    return op('u')\n",                       # псевдоним метода
    "import functools\ndef f(s):\n    return functools.partial(s.request, 'POST')('u')\n",
    "def f(s):\n    return getattr(s, 'post')('u')\n",                       # имя метода строкой
    "def f(s):\n    return getattr(s, 'POST'.lower())('u')\n",               # имя метода вычисляется
    "def f(s, m):\n    return getattr(s, m)('u')\n",
    "def f(s):\n    return s.__getattribute__('post')('u')\n",
    "import operator\ndef f(s):\n    return operator.methodcaller('post', 'u')(s)\n",
    "import operator\ndef f(s):\n    return operator.attrgetter('put')(s)('u')\n",
    # без атрибута .post: другие клиенты и функции-отправители, импортированные по имени
    "def f(u):\n    from aiohttp import request\n    return request('POST', u)\n",
    "def f(u):\n    from requests import post\n    return post(u)\n",
    "def f(u):\n    from urllib.request import urlopen, Request\n    return urlopen(Request(u, data=b'x'))\n",
    "def f(s, r):\n    return s.send(r)\n",
    "def f(u):\n    import http.client\n    return http.client.HTTPSConnection(u)\n",
])
def test_senders_see_aliases_and_dynamic_lookup(tmp_path, body):
    """Отправитель виден и без прямого вызова s.post(...): псевдоним, partial, getattr/__getattribute__,
    operator.methodcaller/attrgetter — иначе новая незапиненная функция в accounts.py слала бы POST незаметно."""
    assert _senders(_fake_module(tmp_path, body)) == {"f"}


def test_senders_ignore_plain_reads(tmp_path):
    body = ("def f(s, d):\n    x = getattr(d, 'name', None)\n    return s.get('u'), d.get('post'), x\n"
            "def g(s):\n    return s.request_info\n")
    assert _senders(_fake_module(tmp_path, body)) == set()


def test_bot_main_takes_payouts_switch_from_env_file():
    """bot.main: сначала load_env(), затем payouts.switch_from_file(ENV_PATH) — оба верхним уровнем тела main (не под
    if/try), и именно main запускается из __main__. Без этого PAYOUTS=1 из окружения Windows пережил бы PAYOUTS=0,
    записанный launcher в .env после обновления (launcher такое окружение боту и не передаёт — это второй рубеж)."""
    tree = ast.parse(_source(B))
    mains = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"]
    assert len(mains) == 1 and len(_names_of(B)[1]["main"]) == 1
    calls = [ast.unparse(s.value) for s in mains[0].body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)]
    assert "load_env()" in calls and "payouts.switch_from_file(ENV_PATH)" in calls
    assert calls.index("load_env()") < calls.index("payouts.switch_from_file(ENV_PATH)")
    entry = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    assert len(entry) == 1 and [ast.unparse(s) for s in entry[0].body] == ["asyncio.run(main())"]
    assert B.load_env is p2p.load_env and B.ENV_PATH == p2p.ENV_PATH
