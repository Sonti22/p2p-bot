"""Торговая поверхность вне trading/ (защищённый файл: «trading» в пути — правится только вручную, мерж ручной).

Торговое ядро живёт только в trading/ (этап 4 плана) — защищённом пакете с ручным мержем. Здесь проверяется, что
остальной код его не обходит:
1) guard (scripts/guard.py): любая добавленная/удалённая строка торгового кода вне trading/ — ручная проверка; такие
   строки в нынешнем коде запинены (TRADING_LINES_APPROVED, сейчас пусто) — новая без пина краснит и смоук launcher;
2) AST всех модулей вне trading/ (тесты не в счёт — у них списки запретных путей нарочно): нет эндпоинтов ордеров и
   позиций и ключей *_trade; во всём коде, включая trading/ и research/, нет вывода, переводов, P2P
   «отпустить»/«оплачено»; выплаты Cryptomus — только в payouts.py; отправлять запросы (.post/.put/.delete/.patch/
   .request/.send/.urlopen…, getattr с таким именем, сетевые клиенты) могут только функции из ALLOWED_SENDERS;
   research/ (офлайн-бэктесты владельца) бот не импортирует, а сам он только читает (GET);
3) сеть в тестах заблокирована (tests/conftest.py): наружу — красный тест; loopback — только к своим портам.
Нарочно запутанный код (exec, vars(...)["po" + "st"], _socket напрямую) этим не поймать — это остаточный риск из
ROADMAP; здесь — всё, что пишется без умысла спрятать.
"""
import ast
import asyncio
import importlib.util
import os
import re
import socket
import urllib.error
import urllib.request

import aiohttp
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOW_TO_UPDATE = (
    "Это торговая поверхность вне trading/. Торговый код, вывод и переводы вне защищённого пакета trading/ не "
    "добавлять; новый отправитель запросов — только если владелец проверил его сам и вписал в ALLOWED_SENDERS в "
    "tests/test_trading_surface.py (защищённый файл: правка вручную, мерж — после проверки владельцем, на ПК — "
    "python launcher.py --approve <sha>). Облачной рутине этот список не менять.")


def _load_guard():
    spec = importlib.util.spec_from_file_location("guard_script", os.path.join(ROOT, "scripts", "guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()


def _tracked(pattern="*.py"):
    """Файлы, которые отслеживает git (в CI и в папке бота на ПК это checkout; посторонние файлы ПК не в счёт).
    Без git — обход папки (без .git, виртуальных окружений, data/ и logs/)."""
    try:
        out = guard.git("-C", ROOT, "-c", "core.quotepath=false", "ls-files", "-z", "--", pattern)
        files = [f for f in out.split("\0") if f]
    except Exception:
        files = []
    if files:
        return files
    skip = {".git", "__pycache__", ".venv", "venv", "env", "data", "logs", ".pytest_cache", "node_modules"}
    for top, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for n in names:
            if n.endswith(pattern.lstrip("*")):
                files.append(os.path.relpath(os.path.join(top, n), ROOT).replace(os.sep, "/"))
    return files


def _is_trading(path):
    """Путь торгового кода: как у guard/launcher — любой путь со словом trading (trading/, scripts/trading_keys.py…)."""
    return "trading" in path.lower()


RESEARCH = "research"   # офлайн-бэктесты: CLI, который запускает владелец; бот его не импортирует (проверено ниже)


def _is_research(mod_name):
    return mod_name == RESEARCH or mod_name.startswith(RESEARCH + ".")


def _modules(research=False):
    """{имя модуля: путь} — весь код бота, кроме тестов; research/ — отдельно (research=True — только он)."""
    out = {}
    for f in _tracked():
        if f.split("/")[0] == "tests" or not f.endswith(".py"):
            continue
        name = f[:-3].replace("/", ".").removesuffix(".__init__")
        if _is_research(name) == research:
            out[name] = f
    return out


def _source(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# --- 1. guard: строки торгового кода вне trading/ --------------------------------------------------------------------

TRADING_LINES = [
    "TRADING = os.getenv('TRADING', '0')",
    "    if os.getenv('TRADING_MODE', 'paper') == 'auto':",
    "PAPER_TRADING=1",
    'kb = [[{"text": "✅ Отправить", "callback_data": f"trd_ok:{token}"}]]',
    "    elif data == 'trd_stop':",
    "import trading",
    "from trading import venues",
    "    from trading.venues import place_order",
    "        await trading.venues.place(s, order)",
    "risk = importlib.import_module('trading.risk')",
    "    r = await bybit_post(s, k, sec, '/v5/order/create', body)",
    "    '/v5/position/set-leverage',",
    "    '/v5/execution/list',",
    "    '/V5/ORDER/cancel',",
    "    '/openApi/swap/v2/trade/order',",
    "    '/openApi/swap/v3/user/balance',",
    "    '/openApi/cswap/v1/trade/order',",
    "    '/openApi/spot/v1/trade/order',",
    "    '/v5/p2p/order/finish',",
    "    '/v5/p2p/order/pay',",
    "    '/v5/p2p/item/create',",
    "    body['orderLinkId'] = cid",
    "    params = {'clientOrderID': cid}",
    "    params = {'newClientOrderId': cid}",
    "    body = {'reduceOnly': True}",
    "    key = accounts.keys().get('bybit_trade')",
    "BINGX_TRADE_KEY=",
    "def bingx_trade_key():",
]

NOT_TRADING_LINES = [
    "    'enableSpotAndMarginTrading': 'торговля спот', 'enableFutures': 'фьючерсы',",
    "BINGX_FLAGS_REQUIRED = ('enableReading', 'enableSpotAndMarginTrading', 'enableFutures',",
    '    """Балансы монет на Bybit: Unified Trading Account + Funding wallet."""',
    "'Права — только «Read Info» (сними «Spot & Contract Trading» и «Withdrawals»), '",
    "'Права — только «Read» (сними «Spot Trading», «Perpetual Futures Trading», «Universal Transfer» и '",
    '"""Сухой прогон (paper trading) — этап 1 полуавтомата: виртуальные круги без реальных денег,',
    "# Сухой прогон (paper trading): виртуальные круги без реальных денег",
    "import trades",
    "from trades import match_fact",
    "        trade_id, bank, total, crossed = trades.log_trade(d, cfg.amount)",
    "        row = trades.get_trade(trade_id)",
    "bybit_trades = []",
    "    j = await mexc_get(s, api_key, api_secret, '/api/v3/myTrades', {'symbol': sym})",
    "BYBIT_POST_PATHS = frozenset({'/v5/p2p/order/simplifyList'})",
    "    fund = await bybit_get(s, k, sec, '/v5/asset/transfer/query-account-coins-balance')",
    "    '/v5/p2p/order/payments',",
    "# paper_trading.py и trading-заметки в комментарии",
    "    'trade': 'сделка',",
]


@pytest.mark.parametrize("line", TRADING_LINES)
def test_guard_trading_code_catches(line):
    assert re.search(guard.TRADING_CODE, line), line


@pytest.mark.parametrize("line", NOT_TRADING_LINES)
def test_guard_trading_code_ignores(line):
    assert not re.search(guard.TRADING_CODE, line), line


# Строки торгового кода вне trading/ (как их видит guard), которые владелец проверил и принял: {путь: [строка без
# отступов, …]} — сейчас ни одной. Guard не пускает такие строки в автомерж, а этот пин держит их и на ПК: новая строка
# TRADING/trd_*/import trading… в bot.py или accounts.py без записи здесь — красный тест, значит и смоук launcher, и
# обновление не встанет, пока владелец не впишет её сюда (защищённый файл — на ПК только с --approve), даже если CI обойдён.
TRADING_LINES_APPROVED = {}


def current_trading_lines():
    """{путь: [строки]} — весь код вне тестов, .md и защищённых путей, как если бы каждая его строка была добавлена."""
    found, seen = {}, 0
    for f in _tracked("*"):
        if f.split("/")[0] == "tests" or f.endswith(".md") or guard.protected(f):
            continue
        try:
            text = _source(f)
        except UnicodeDecodeError:   # картинки
            continue
        seen += 1
        for line in text.splitlines():
            if re.search(guard.TRADING_CODE, line):
                found.setdefault(f, []).append(line.strip())
    assert seen >= 10, "код бота не найден"
    return found


def test_trading_lines_outside_trading_are_pinned():
    """Срабатывания guard на нынешнем коде вне trading/ (accounts.py, bot.py, paper.py, .env.example…) — ровно
    TRADING_LINES_APPROVED. Сейчас их нет: ложных срабатываний на обычном коде тоже нет."""
    now = current_trading_lines()
    assert now == TRADING_LINES_APPROVED, (
        "Строки торгового кода вне trading/ изменились. Проверить и вписать их в TRADING_LINES_APPROVED в "
        "tests/test_trading_surface.py может только владелец (защищённый файл). Сейчас:\n"
        + "\n".join(f"{f}: {line[:120]}" for f, lines in now.items() for line in lines))


def _repo(tmp_path, monkeypatch):
    """Настоящий git во временной папке: база main с bot.py и paper.py, ветка claude/x."""
    monkeypatch.chdir(tmp_path)
    g = lambda *a: guard.git("-c", "user.name=t", "-c", "user.email=t@t", *a)   # noqa: E731
    g("init", "-q", "-b", "main")
    (tmp_path / "bot.py").write_text("import trades\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "paper.py").write_text('"""Сухой прогон (paper trading)."""\nX = 1\n', encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-b", "claude/x")
    return g


def test_guard_blocks_trading_lines_and_trading_paths(tmp_path, monkeypatch):
    """Строка TRADING в bot.py, удалённая строка с trd_, пакет trading/ и tests/trading/ — ручная проверка; обычная
    правка paper.py с «paper trading» и .md с TRADING — нет."""
    g = _repo(tmp_path, monkeypatch)
    (tmp_path / "bot.py").write_text("import trades\nimport trading\n\n\ndef f():\n    return os.getenv('TRADING')\n",
                                     encoding="utf-8")
    (tmp_path / "paper.py").write_text('"""Сухой прогон (paper trading), круг 20 000."""\nX = 2\n', encoding="utf-8")
    (tmp_path / "notes.md").write_text("TRADING=1 включает владелец\n", encoding="utf-8")
    (tmp_path / "trading").mkdir()
    (tmp_path / "trading" / "venues.py").write_text("PATHS = ()\n", encoding="utf-8")
    (tmp_path / "tests" / "trading").mkdir(parents=True)
    (tmp_path / "tests" / "trading" / "test_risk.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "trading")
    found = guard.check("main")
    assert "bot.py: изменён торговый код (2 стр.) — только ручная проверка" in found, found
    assert "изменён защищённый файл: trading/venues.py" in found and \
        "изменён защищённый файл: tests/trading/test_risk.py" in found, found
    assert not [p for p in found if p.startswith(("paper.py", "notes.md"))], found
    g("checkout", "-q", "main")
    (tmp_path / "bot.py").write_text("import trades\n\n\ndef f():\n    return 'trd_no'\n", encoding="utf-8")
    g("commit", "-q", "-am", "add")
    g("checkout", "-q", "-b", "claude/remove")
    (tmp_path / "bot.py").write_text("import trades\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    g("commit", "-q", "-am", "remove")
    assert guard.check("main") == ["bot.py: изменён торговый код (1 стр.) — только ручная проверка"]


def test_guard_protects_trading_paths():
    for path in ("trading/venues.py", "trading/__init__.py", "tests/trading/test_risk.py", "scripts/trading_keys.py",
                 "tests/test_trading_surface.py", "Trading/Risk.py", "tests/conftest.py"):
        assert guard.protected(path), path
    for path in ("trades.py", "paper.py", "tests/test_trades.py"):
        assert not guard.protected(path), path


# --- 2. AST: торговые эндпоинты, вывод и переводы, отправители запросов -----------------------------------------------

# ордера, позиции, маржа, свои P2P-объявления — только в trading/
TRADING_ENDPOINTS = (
    r"/v5/(?:order|position|execution|spot-margin-trade|spot-cross-margin-trade)/",   # Bybit
    r"/v5/account/set-",                                                                # Bybit: режимы маржи/хеджа
    r"/v5/p2p/(?:item/(?:create|update|cancel)|order/message/send)",                    # Bybit P2P: свои объявления
    r"/openApi/c?swap/v\d/(?:trade|user)/", r"/openApi/spot/v\d/trade/",                # BingX
    r"/api/v3/(?:order|batchOrders|openOrders)(?![\w-])",                               # MEXC
    r"/v1/order/", r"/(?:linear-)?swap-api/",                                           # HTX
    r"/api/v[123]/(?:hf/)?(?:orders|stop-order|oco/order|margin/order)(?![\w-])",       # KuCoin
)
# вывод, переводы, P2P «отпустить»/«оплачено» — нигде, и в trading/ тоже
MONEY_OUT = (
    r"/withdraw(?:als?)?/(?:apply|create|submit|cancel)\b",   # Bybit /v5/asset/withdraw/create, BingX …/withdraw/apply
    r"/capital/withdraw(?:/apply)?(?![\w/-])",                 # MEXC (…/withdraw/history — чтение, не ловится)
    r"/v3/withdrawals\b", r"/dw/withdraw",                     # KuCoin v3, HTX
    r"/[\w-]*(?:inner|inter|universal|sub)-?transfer",         # между счетами и субаккаунтами (Bybit, BingX, KuCoin, MEXC)
    r"/(?:account|asset|capital|futures|point|subuser|subAccount)(?:/v\d)?/transfer(?:/(?:internal|apply))?(?![\w/-])",
    r"/post/asset/transfer", r"deposit-to-account",
    r"/v\d/transfer/",                                         # Cryptomus /v1/transfer/to-personal|to-business
    r"/v5/p2p/order/(?:finish|pay)\b",                         # Bybit P2P: отпустить крипту, «оплачено»
    r"\b(?:p2p|otc|c2c|fiat)\b[\w/.-]*/(?:finish|release\w*|pay|paid|mark-?paid|confirm-?(?:pay|paid|release)\w*)"
    r"(?![\w-])",
)
PAYOUT_ENDPOINT = r"/v1/payout\b"   # выплаты Cryptomus — только payouts.py (решение владельца 2026-09-26)
PAYOUT_MODULES = ("payouts",)
TRADE_KEY_STR = r"^[\w{}]*_trade(?:_[\w{}]*)?$"   # строка-ключ: bybit_trade, BYBIT_TRADE_KEY, f"{ex}_trade"…
TRADE_KEY_NAME = r"(?:bybit|bingx|mexc|htx|kucoin|lbank|bitpapa|cryptomus)_trade(?![a-z0-9])"

SEND_METHODS = ("post", "put", "delete", "patch", "request", "_request", "send", "urlopen", "ws_connect",
                "open_connection", "create_connection", "sendall", "sendto", "sock_sendall", "sock_connect")
DYNAMIC_LOOKUP = ("getattr", "__getattribute__", "attrgetter", "methodcaller")
DYNAMIC_IMPORT = ("__import__", "import_module")
NET_MODULES = ("requests", "httpx", "urllib3", "urllib.request", "http.client", "http", "socket", "ssl",
               "asyncio.streams", "websockets", "websocket", "ftplib", "smtplib", "telnetlib", "xmlrpc", "pycurl",
               "curl_cffi", "grpc")
AIOHTTP_SENDERS = ("request", "ClientSession", "ClientRequest", "TCPConnector", "Session", "client", "connector")

# (модуль, функция) — кто вообще может отправить запрос. У каждого — свой узкий allowlist путей или один хост.
ALLOWED_SENDERS = {
    ("accounts", "bybit_post"): "POST к Bybit — только BYBIT_POST_PATHS (история P2P-ордеров), запинено",
    ("accounts", "cryptomus_call"): "Cryptomus — только CRYPTOMUS_CALLS (балансы, история), чтение",
    ("payouts", "payout_call"): "выплаты Cryptomus — только PAYOUT_CALLS, защищённый payouts.py",
    ("p2p", "_json"): "публичные объявления площадок без ключей (POST Bybit otc/item/online, queryAllPaymentList)",
    ("bot", "Bot.call"): "Telegram Bot API",
    ("bot", "Bot._post_photo"): "Telegram sendPhoto",
    ("bot", "Bot.send_document"): "Telegram sendDocument",
    ("launcher", "notify"): "уведомление владельцу в Telegram (urllib, защищённый launcher.py)",
}
# вызов функции своего модуля (payouts.send, self.send в классе со своим send) — не отправитель: тело этой функции
# проверяется само, и отправитель внутри неё уже в списке выше (payouts.send → payout_call)
# сетевой модуль, импортированный целиком: (модуль, сетевой модуль) → какие его атрибуты можно трогать
ALLOWED_NET_IMPORTS = {
    ("p2p", "socket"): ("gethostbyname_ex", "gethostname"),       # локальные адреса ПК: BestChange в обход VPN
    ("launcher", "urllib.request"): ("build_opener", "ProxyHandler"),
}
# getattr с вычисляемым именем — только эти (чтение настроек), в точности как написано
ALLOWED_DYNAMIC = {
    ("bot", "Bot.apply_preset", "getattr(self.cfg, key)"),
    ("presets", "save_preset", "getattr(cfg, k)"),
}


def _strings(tree):
    """(строка, строка кода): константы, склейки констант через +, f-строки со {} на месте подстановок."""
    def text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            v = node.value
            return v.decode("latin-1") if isinstance(v, bytes) else v
        if isinstance(node, ast.JoinedStr):
            return "".join(text(v) if isinstance(v, ast.Constant) else "{}" for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = text(node.left), text(node.right)
            return None if left is None or right is None else left + right
        return None

    for node in ast.walk(tree):
        s = text(node)
        if s is not None:
            yield s, getattr(node, "lineno", 0)


def _identifiers(tree):
    for node in ast.walk(tree):
        for attr in ("id", "attr", "name", "arg", "asname"):
            v = getattr(node, attr, None)
            if isinstance(v, str):
                yield v, getattr(node, "lineno", 0)


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


def _top_defs(src):
    """Имена функций и классов верхнего уровня модуля."""
    return {n.name for n in ast.parse(src).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _local_imports(tree, local_defs):
    """{имя в модуле: модуль бота}: import payouts, import payouts as p, from research import metrics."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in local_defs and (a.asname or "." not in a.name):
                    out[a.asname or a.name] = a.name
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for a in node.names:
                if f"{node.module}.{a.name}" in local_defs:
                    out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _surface(mod_name, src, local_defs=None):
    """→ (отправители {(модуль, функция): [что]}, динамика {(модуль, функция, код)}, нарушения сетевых импортов [..]).
    Функция — полное имя: «Класс.метод», «внешняя.внутренняя», «<module>», «….<lambda>». local_defs — {модуль бота:
    имена его функций}: вызов своей функции другого модуля (payouts.send) — не отправитель, её тело проверяется само."""
    tree = ast.parse(src)
    senders, dynamic, net_bad = {}, set(), []
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    allowed_imports = {net: attrs for (m, net), attrs in ALLOWED_NET_IMPORTS.items() if m == mod_name}
    local_defs = local_defs or {}
    local_mods = _local_imports(tree, local_defs)

    def is_net(name):
        return name in NET_MODULES or name.split(".")[0] in NET_MODULES

    def add(where, what):
        senders.setdefault((mod_name, where), []).append(what)

    def visit(node, where, own_methods):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where = node.name if where == "<module>" else f"{where}.{node.name}"
            if isinstance(node, ast.ClassDef):
                own_methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        elif isinstance(node, ast.Lambda):
            where = f"{where}.<lambda>"
        if isinstance(node, ast.Attribute) and node.attr in SEND_METHODS:
            base = node.value.id if isinstance(node.value, ast.Name) else None
            own = (base == "self" and node.attr in own_methods          # self.send в классе со своим send
                   or node.attr in local_defs.get(local_mods.get(base), ()))   # payouts.send — функция модуля бота
            if not own:
                add(where, f"{ast.unparse(node)} (стр. {node.lineno})")
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in DYNAMIC_LOOKUP:
                args = node.args[1:2] if name == "getattr" else node.args[:1]
                if not args or any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                                   and a.value.lower() in SEND_METHODS for a in args):
                    add(where, f"{ast.unparse(node)} (стр. {node.lineno})")
                elif any(not (isinstance(a, ast.Constant) and isinstance(a.value, str)) for a in args):
                    dynamic.add((mod_name, where, ast.unparse(node)))
            if name in DYNAMIC_IMPORT:
                a = node.args[0] if node.args else None
                if not (isinstance(a, ast.Constant) and isinstance(a.value, str)) or is_net(a.value) \
                        or a.value.split(".")[0] == "aiohttp":
                    add(where, f"{ast.unparse(node)} (стр. {node.lineno})")
        if isinstance(node, ast.Import):
            for a in node.names:
                if is_net(a.name) and not (a.name in allowed_imports and a.asname is None):
                    add(where, f"import {a.name} (стр. {node.lineno})")
        if isinstance(node, ast.ImportFrom):
            m = node.module or ""
            names = {a.name for a in node.names}
            if is_net(m) and not (m in allowed_imports and names <= set(allowed_imports[m])) \
                    or m.split(".")[0] == "aiohttp" and names & set(AIOHTTP_SENDERS):
                add(where, f"from {m} import {', '.join(sorted(names))} (стр. {node.lineno})")
        for child in ast.iter_child_nodes(node):
            visit(child, where, own_methods)

    visit(tree, "<module>", set())
    # разрешённый сетевой модуль: только его разрешённые атрибуты и только через точку (не x = socket)
    for net, attrs in allowed_imports.items():
        root = net.split(".")[0]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Name) and node.id == root) or isinstance(parents.get(node), ast.Import):
                continue
            top = node
            while isinstance(parents.get(top), ast.Attribute):
                top = parents[top]
            dotted = _dotted(top) or root
            if dotted.startswith(net + ".") and dotted[len(net) + 1:].split(".")[0] in attrs:
                continue
            if root != net and not (dotted + ".").startswith(net + "."):
                continue   # urllib.parse/urllib.error при разрешённом urllib.request — не сеть
            net_bad.append(f"{mod_name}: {dotted} (стр. {node.lineno}) — из {net} можно только {', '.join(attrs)}")
    return senders, dynamic, net_bad


def _scan():
    """Код бота (без research/): отправители, динамика, сетевые импорты; исходники — и research/ (для строк)."""
    runtime, research = _modules(), _modules(research=True)
    sources = {m: (p, _source(p)) for m, p in {**runtime, **research}.items()}
    local_defs = {m: _top_defs(src) for m, (_, src) in sources.items()}
    senders, dynamic, net_bad = {}, set(), []
    for mod_name in runtime:
        s, d, n = _surface(mod_name, sources[mod_name][1], local_defs)
        senders.update(s)
        dynamic |= d
        net_bad += n
    return senders, dynamic, net_bad, sources


@pytest.fixture(scope="module")
def scan():
    return _scan()


def test_modules_found():
    """Сканируется весь код бота: без главных модулей проверка молча ничего бы не проверяла."""
    mods = _modules()
    assert {"accounts", "bot", "p2p", "payouts", "paper", "launcher", "scripts.guard"} <= set(mods), sorted(mods)
    assert not [m for m in mods if m.startswith("tests") or _is_research(m)]


def test_only_known_senders(scan):
    """Запросы, меняющие состояние (и вообще любой сетевой клиент), шлют только функции из ALLOWED_SENDERS."""
    senders, _, _, _ = scan
    unknown = {k: v for k, v in senders.items() if k not in ALLOWED_SENDERS and not _is_trading(k[0])}
    assert not unknown, HOW_TO_UPDATE + "\n" + "\n".join(f"{m}.{f}: {', '.join(v)}" for (m, f), v in unknown.items())
    stale = set(ALLOWED_SENDERS) - set(senders)
    assert not stale, f"в ALLOWED_SENDERS есть то, чего в коде нет (убери, чтобы место не занял другой код): {stale}"


def test_only_known_dynamic_lookups(scan):
    _, dynamic, _, _ = scan
    dynamic = {d for d in dynamic if not _is_trading(d[0])}
    assert dynamic == ALLOWED_DYNAMIC, HOW_TO_UPDATE + f"\nнеизвестные: {dynamic - ALLOWED_DYNAMIC}, " \
                                                       f"пропавшие: {ALLOWED_DYNAMIC - dynamic}"


def test_net_modules_used_only_as_allowed(scan):
    _, _, net_bad, _ = scan
    assert not net_bad, HOW_TO_UPDATE + "\n" + "\n".join(net_bad)


def _hits(patterns, text):
    return [p for p in patterns if re.search(p, text, re.I)]


def test_no_trading_endpoints_or_trade_keys_outside_trading(scan):
    """Эндпоинты ордеров/позиций/своих P2P-объявлений и ключи *_trade — только в trading/."""
    _, _, _, strings = scan
    bad = []
    for mod_name, (path, src) in strings.items():
        if _is_trading(path):
            continue
        tree = ast.parse(src)
        for s, line in _strings(tree):
            for p in _hits(TRADING_ENDPOINTS, s) + ([TRADE_KEY_STR] if re.search(TRADE_KEY_STR, s, re.I) else []):
                bad.append(f"{path}:{line}: /{p}/ в {s[:80]!r}")
        for name, line in _identifiers(tree):
            if re.search(TRADE_KEY_NAME, name, re.I):
                bad.append(f"{path}:{line}: имя {name}")
    assert not bad, HOW_TO_UPDATE + "\n" + "\n".join(bad)


def test_no_withdrawals_transfers_or_p2p_release_anywhere(scan):
    """Вывод, переводы, P2P «отпустить»/«оплачено» — ни в одном модуле, и в trading/ тоже; выплаты Cryptomus (/v1/payout)
    — только в payouts.py."""
    _, _, _, strings = scan
    bad = []
    for mod_name, (path, src) in strings.items():
        for s, line in _strings(ast.parse(src)):
            for p in _hits(MONEY_OUT, s):
                bad.append(f"{path}:{line}: /{p}/ в {s[:80]!r}")
            if mod_name not in PAYOUT_MODULES and re.search(PAYOUT_ENDPOINT, s, re.I):
                bad.append(f"{path}:{line}: выплата Cryptomus вне payouts.py: {s[:80]!r}")
    assert not bad, HOW_TO_UPDATE + "\n" + "\n".join(bad)


# research/ — офлайн-бэктесты, которые запускает владелец (публичные свечи и фандинг Bybit/BingX через urllib GET). Он
# вне списка отправителей, поэтому: бот его не импортирует, а сам он только читает и не трогает ключи, деньги и торговлю.
RESEARCH_NO_IMPORT = ("accounts", "payouts", "bot", "launcher", "trading", "scripts")
RESEARCH_WRITE = ("post", "put", "delete", "patch", "_request", "send", "sendall", "sendto", "ws_connect",
                  "open_connection", "create_connection")


def _imported(node):
    """Имена модулей, которые импортирует узел (import, from … import, import_module/__import__ со строкой)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level:
        return [node.module or ""]
    if isinstance(node, ast.Call) and (_dotted(node.func) or "").split(".")[-1] in DYNAMIC_IMPORT and node.args \
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return [node.args[0].value]
    return []


def _research_problems(path, src):
    bad = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute) and node.attr in RESEARCH_WRITE:
            bad.append(f"{path}:{line}: {ast.unparse(node)} — research только читает")
        if isinstance(node, ast.Call) and (_dotted(node.func) or "").split(".")[-1] == "Request" \
                and (len(node.args) > 1 or any(k.arg in ("data", "method") for k in node.keywords)):
            bad.append(f"{path}:{line}: {ast.unparse(node)[:80]} — запрос с телом/методом, research только GET")
        for name in _imported(node):
            if name.split(".")[0] in RESEARCH_NO_IMPORT:
                bad.append(f"{path}:{line}: import {name} — ключи, деньги и торговля research недоступны")
    for s, line in _strings(tree):
        for p in _hits(TRADING_ENDPOINTS, s) + ([TRADE_KEY_STR] if re.search(TRADE_KEY_STR, s, re.I) else []):
            bad.append(f"{path}:{line}: /{p}/ в {s[:80]!r}")
    return bad


def test_bot_never_imports_research():
    """Ни один модуль бота (bot.py, p2p.py, paper.py, accounts.py, launcher.py, scripts/…) не импортирует research."""
    bad = [f"{path}:{node.lineno}: {name}" for path in _modules().values()
           for node in ast.walk(ast.parse(_source(path))) for name in _imported(node) if _is_research(name)]
    assert not bad, HOW_TO_UPDATE + "\n" + "\n".join(bad)


def test_research_only_reads_public_market_data():
    bad = [p for path in _modules(research=True).values() for p in _research_problems(path, _source(path))]
    assert not bad, HOW_TO_UPDATE + "\n" + "\n".join(bad)


def test_research_checks_catch():
    ok = ("import urllib.request\nfrom research import metrics\n"
          "def get(u):\n    req = urllib.request.Request(u, headers={'User-Agent': 'x'})\n"
          "    return urllib.request.build_opener().open(req, timeout=5).read()\n"
          "URL = 'https://api.bybit.com/v5/market/kline?category=linear'\n")
    assert _research_problems("research/data.py", ok) == []
    for body in ("def f(s):\n    return s.post('u')\n",
                 "import urllib.request\ndef f(u):\n    return urllib.request.Request(u, data=b'x')\n",
                 "import urllib.request\ndef f(u):\n    return urllib.request.Request(u, b'x')\n",
                 "import urllib.request\ndef f(u):\n    return urllib.request.Request(u, method='POST')\n",
                 "import accounts\n", "from trading import venues\n", "import importlib\nimportlib.import_module('bot')\n",
                 "P = '/v5/order/create'\n", "K = 'bybit_trade'\n"):
        assert _research_problems("research/x.py", body), body
    tree = ast.parse("import research.data\nfrom research import metrics\nimport importlib\n"
                     "m = importlib.import_module('research.x')\n")
    assert [n for node in ast.walk(tree) for n in _imported(node) if _is_research(n)] == \
        ["research.data", "research", "research.x"]


@pytest.mark.parametrize("path", [
    "/v5/order/create", "/v5/order/cancel-all", "/v5/position/set-leverage", "/v5/position/trading-stop",
    "/v5/execution/list", "/v5/account/set-margin-mode", "/v5/spot-margin-trade/switch-mode",
    "/v5/p2p/item/create", "/v5/p2p/item/update", "/v5/p2p/item/cancel", "/v5/p2p/order/message/send",
    "/openApi/swap/v2/trade/order", "/openApi/swap/v2/trade/leverage", "/openApi/swap/v2/user/positions",
    "/openApi/swap/v3/user/balance", "/openApi/cswap/v1/trade/order", "/openApi/spot/v1/trade/order",
    "/api/v3/order", "/api/v3/order/test", "/api/v3/batchOrders", "/v1/order/orders/place",
    "/linear-swap-api/v1/swap_cross_order", "/api/v1/orders", "/api/v1/hf/orders", "/api/v3/margin/order",
    "https://api.bybit.com/v5/order/create", "https://open-api.bingx.com/openApi/swap/v2/trade/order?x=1",
])
def test_trading_endpoint_patterns_catch(path):
    assert _hits(TRADING_ENDPOINTS, path), path


@pytest.mark.parametrize("path", [
    "/v5/asset/withdraw/create", "/v5/asset/withdraw/cancel", "/v5/asset/transfer/inter-transfer",
    "/v5/asset/transfer/universal-transfer", "/v5/asset/deposit/deposit-to-account",
    "/openApi/wallets/v1/capital/withdraw/apply", "/openApi/wallets/v1/capital/innerTransfer/apply",
    "/openApi/wallets/v1/capital/subAccountInnerTransfer/apply", "/openApi/api/v3/post/asset/transfer",
    "/openApi/api/asset/v1/transfer", "/api/v3/capital/withdraw", "/api/v3/capital/withdraw/apply",
    "/api/v3/capital/transfer", "/api/v3/capital/transfer/internal", "/api/v3/capital/sub-account/universalTransfer",
    "/v1/dw/withdraw/api/create", "/v1/account/transfer", "/v2/account/transfer", "/v1/futures/transfer",
    "/api/v3/withdrawals", "/api/v2/accounts/inner-transfer", "/api/v3/accounts/universal-transfer",
    "/api/v1/accounts/sub-transfer", "/v1/transfer/to-personal", "/v1/transfer/to-business",
    "/v5/p2p/order/finish", "/v5/p2p/order/pay", "https://api2.bybit.com/fiat/otc/order/finish",
    "/fiat/otc/order/pay", "/api/p2p/order/release", "/c2c/order/confirm-release", "/p2p/v1/order/markPaid",
])
def test_money_out_patterns_catch(path):
    assert _hits(MONEY_OUT, path), path


@pytest.mark.parametrize("path", [
    # то, что бот читает сейчас (accounts.py, p2p.py, netstatus.py, payouts.py)
    "/v5/p2p/order/simplifyList", "/v5/asset/transfer/query-account-coins-balance", "/v5/account/wallet-balance",
    "/v5/user/query-api", "/v5/asset/coin/query-info", "/v5/market/tickers", "/api/v3/myTrades", "/api/v3/account",
    "/api/v3/capital/withdraw/history", "/api/v3/capital/deposit/hisrec", "/api/v3/capital/config/getall",
    "/openApi/api/v3/capital/withdraw/history", "/openApi/spot/v1/account/balance", "/openApi/v1/account/apiPermissions",
    "/v1/query/deposit-withdraw", "/v1/account/accounts", "/v2/user/api-key", "/api/v1/withdrawals",
    "/api/v1/deposits", "/api/v1/fills", "/api/v1/accounts", "/v2/user-api/transaction/list", "/v1/balance",
    "https://api2.bybit.com/fiat/otc/item/online", "https://api2.bybit.com/fiat/otc/configuration/queryAllPaymentList",
    "https://p2p.mexc.com/api/market", "/v1/exchange-rate/USDT/list", "/v5/p2p/order/payments",
])
def test_read_endpoints_not_flagged(path):
    assert not _hits(TRADING_ENDPOINTS + MONEY_OUT, path), path


def _fake_surface(body, mod_name="m"):
    senders, dynamic, net_bad = _surface(mod_name, body)
    return {f for (_, f) in senders}, dynamic, net_bad


@pytest.mark.parametrize("body", [
    "def f(s):\n    return s.post('u')\n",
    "def f(s):\n    op = s.put\n    return op('u')\n",                                  # псевдоним метода
    "import functools\ndef f(s):\n    return functools.partial(s.request, 'POST')('u')\n",
    "def f(s):\n    return getattr(s, 'post')('u')\n",
    "def f(s):\n    return s.__getattribute__('delete')('u')\n",
    "import operator\ndef f(s):\n    return operator.methodcaller('patch', 'u')(s)\n",
    "def f(u):\n    from aiohttp import request\n    return request('POST', u)\n",
    "def f(u):\n    from requests import post\n    return post(u)\n",
    "def f(u):\n    import urllib.request\n    return urllib.request.urlopen(u)\n",
    "def f(u):\n    import http.client\n    return http.client.HTTPSConnection(u)\n",
    "def f(u):\n    import socket\n    return socket.create_connection((u, 443))\n",
    "def f(u):\n    return __import__('requests').get(u)\n",
    "def f(n):\n    import importlib\n    return importlib.import_module(n)\n",
    "def f(s, r):\n    return s.send(r)\n",
    "def f(s):\n    return s.ws_connect('u')\n",
    "async def f():\n    import asyncio\n    return await asyncio.open_connection('h', 443)\n",
    "class B:\n    def f(self):\n        return self.send('x')\n",                    # send не свой — чужой метод
])
def test_surface_sees_senders(body):
    senders, _, _ = _fake_surface(body)
    assert senders and all(s.split(".")[-1] == "f" or s.startswith("B.f") for s in senders), senders


def test_surface_qualified_names_and_own_methods():
    body = ("class Bot:\n"
            "    async def call(self, m):\n        return await self.s.post(m)\n"
            "    async def send(self, t):\n        return await self.call('sendMessage')\n"
            "    async def hello(self):\n        await self.send('hi')\n"
            "        g = lambda: self.s.delete('u')\n"
            "def outer():\n    def inner(s):\n        return s.patch('u')\n    return inner\n")
    senders, _, _ = _fake_surface(body)
    assert senders == {"Bot.call", "Bot.hello.<lambda>", "outer.inner"}


def test_surface_skips_calls_into_bot_modules():
    """payouts.send(...) — вызов функции модуля бота (её тело проверяется само); s.send(...) рядом — отправитель."""
    local = {"payouts": {"send"}, "research.metrics": {"post"}}
    body = ("import payouts\nimport payouts as pay\nfrom research import metrics\n"
            "async def f(s):\n    await payouts.send(s)\n    await pay.send(s)\n    metrics.post(1)\n"
            "    return s.send(1)\n")
    senders, _, _ = _surface("bot", body, local)
    assert senders == {("bot", "f"): ["s.send (стр. 8)"]}
    assert set(_surface("bot", body)[0][("bot", "f")]) > {"s.send (стр. 8)"}   # без знания модулей — всё подряд


def test_surface_ignores_plain_reads():
    body = ("import aiohttp\nimport urllib.parse\n"
            "async def f(s, d, cfg, key):\n"
            "    x = getattr(d, 'name', None)\n"
            "    async with s.get('u') as r:\n        j = await r.json()\n"
            "    return d.get('post'), x, r.request_info, urllib.parse.urlencode({}), aiohttp.ClientTimeout(total=5)\n")
    senders, dynamic, net_bad = _fake_surface(body)
    assert senders == set() and dynamic == set() and net_bad == []
    _, dynamic, _ = _fake_surface("def f(cfg, k):\n    return getattr(cfg, k)\n")
    assert dynamic == {("m", "f", "getattr(cfg, k)")}


def test_allowed_net_import_limits_attributes():
    ok = "import socket\ndef f():\n    return socket.gethostbyname_ex(socket.gethostname())\n"
    assert _fake_surface(ok, "p2p") == (set(), set(), [])
    for body in ("import socket\ndef f(h):\n    return socket.create_connection((h, 80))\n",
                 "import socket\nS = socket\n",
                 "from socket import create_connection\n",
                 "import socket as s\n"):
        senders, _, net_bad = _fake_surface(body, "p2p")
        assert senders or net_bad, body
    launcher_ok = ("import urllib.error\nimport urllib.parse\nimport urllib.request\n"
                   "def notify(t):\n    o = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
                   "    return urllib.parse.urlencode({}), urllib.error.HTTPError\n")
    senders, _, net_bad = _fake_surface(launcher_ok, "launcher")
    assert senders == {"notify"} and net_bad == []
    senders, _, net_bad = _fake_surface("import urllib.request\ndef g(u):\n    return urllib.request.urlopen(u)\n",
                                        "launcher")
    assert "g" in senders and net_bad


def test_strings_see_concatenation_and_fstrings():
    tree = ast.parse("A = '/v5/' + 'order/create'\nB = f'/v5/{kind}/create'\nC = f'/openApi/swap/v2/{x}/order'\n"
                     "D = b'/v5/asset/withdraw/create'\ndef f():\n    '''docstring /v1/transfer/to-personal'''\n")
    got = [s for s, _ in _strings(tree)]
    assert "/v5/order/create" in got and "/v5/{}/create" in got and "/v5/asset/withdraw/create" in got
    assert _hits(MONEY_OUT, "docstring /v1/transfer/to-personal")
    assert [n for n, _ in _identifiers(ast.parse("def bybit_trade_key():\n    x = cfg.bingx_trade\n"))
            if re.search(TRADE_KEY_NAME, n, re.I)] == ["bybit_trade_key", "bingx_trade"]
    assert not re.search(TRADE_KEY_NAME, "bybit_trades")
    for key in ("bybit_trade", "BYBIT_TRADE_KEY", "{}_trade"):
        assert re.search(TRADE_KEY_STR, key, re.I), key
    for text in ("trade_id и trades", "перебираем монеты бота (SPOT_TRADE_SYMBOLS) против USDT", "trades.py"):
        assert not re.search(TRADE_KEY_STR, text, re.I), text


# --- 3. Сеть в тестах заблокирована (tests/conftest.py) ---------------------------------------------------------------

NOWHERE = "192.0.2.1"   # TEST-NET-1 (RFC 5737): не маршрутизируется, даже если блок бы не сработал
LOOPBACK = "127.0.0.1"


def test_socket_connect_outside_is_blocked(network_attempts):
    with pytest.raises(ConnectionRefusedError, match="сеть в тестах заблокирована"):
        socket.create_connection((NOWHERE, 443), timeout=1)
    with pytest.raises(OSError):
        socket.getaddrinfo("example.com", 443)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError):
            s.sendto(b"x", (NOWHERE, 53))
        with pytest.raises(OSError):
            s.connect_ex((NOWHERE, 53))
    finally:
        s.close()
    assert len(network_attempts) == 4, network_attempts
    network_attempts.clear()


def test_swallowed_attempt_still_recorded(network_attempts):
    """Код под тестом ловит сетевую ошибку и живёт дальше — попытка всё равно записана, и тест покраснеет в конце."""
    try:
        socket.gethostbyname("api.bybit.com")
    except OSError:
        pass
    assert network_attempts == ["socket.gethostbyname('api.bybit.com')"]
    network_attempts.clear()


def test_aiohttp_and_urllib_are_blocked(network_attempts):
    async def go():
        async with aiohttp.ClientSession() as s:
            for url in (f"http://{NOWHERE}/", "https://api.bybit.com/v5/order/create"):
                with pytest.raises(aiohttp.ClientError):
                    async with s.post(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        await r.read()
    asyncio.run(go())
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # без системного прокси ПК
    with pytest.raises(urllib.error.URLError):
        direct.open(f"http://{NOWHERE}/", timeout=1)
    assert len(network_attempts) == 3, network_attempts
    assert any("api.bybit.com" in a for a in network_attempts), network_attempts
    network_attempts.clear()


def test_foreign_loopback_port_is_blocked(network_attempts):
    """На 127.0.0.1 чужой порт — это, например, прокси VPN владельца: через него запрос ушёл бы наружу. Соединяться по
    loopback можно только с портами, которые открыл сам процесс тестов."""
    foreign = 10808   # не эфемерный порт: процесс тестов его сам не открывает
    with pytest.raises(ConnectionRefusedError, match="сеть в тестах заблокирована"):
        socket.create_connection((LOOPBACK, foreign), timeout=1)
    via_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://{LOOPBACK}:{foreign}"}))
    with pytest.raises(urllib.error.URLError):
        via_proxy.open("http://api.bybit.com/v5/order/create", timeout=1)

    async def go():
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.ClientError):
                async with s.get("http://api.bybit.com/", proxy=f"http://{LOOPBACK}:{foreign}") as r:
                    await r.read()
    asyncio.run(go())
    assert len(network_attempts) == 3 and all(str(foreign) in a for a in network_attempts), network_attempts
    network_attempts.clear()


def test_own_loopback_server_is_allowed(network_attempts):
    async def go():
        async def echo(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(echo, LOOPBACK, 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection(LOOPBACK, port)
        writer.write(b"ping\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        server.close()
        await server.wait_closed()
        return line
    assert asyncio.run(go()) == b"ping\n"
    assert network_attempts == []


def test_allow_network_marker_registered_and_unused(request):
    """Метка allow_network есть (для отладки вручную), но ни один тест её не ставит."""
    assert any(m.startswith("allow_network") for m in request.config.getini("markers"))
    used = [f for f in _tracked("*.py") if f.startswith("tests/") and "mark.allow_network" in _source(f)
            and f != "tests/test_trading_surface.py"]
    assert not used, used
