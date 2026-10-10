"""Торговая поверхность вне trading/ (защищённый файл: «trading» в пути — правится только вручную, мерж ручной).

Торговое ядро живёт только в trading/ (этап 4 плана) — защищённом пакете с ручным мержем. Здесь проверяется, что
остальной код его не обходит:
1) guard (scripts/guard.py): любая добавленная/удалённая строка торгового кода вне trading/ — ручная проверка; такие
   строки в нынешнем коде запинены (TRADING_LINES_APPROVED — хуки ядра в bot.py) — новая без пина краснит и launcher;
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
    git не ответил — красный тест, а не обход папки: иначе проверка молча смотрела бы не на то (или ни на что)."""
    try:
        out = guard.git("-C", ROOT, "-c", "core.quotepath=false", "ls-files", "-z", "--", pattern)
    except Exception as e:
        pytest.fail(f"git ls-files не сработал ({type(e).__name__}: {str(e)[:200]}) — без списка файлов из git "
                    f"поверхность вне trading/ не проверить. Тесты запускаются в checkout репозитория (CI, папка бота "
                    f"на ПК) с git в PATH.", pytrace=False)
    return [f for f in out.split("\0") if f]


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
    # режимы маржи и позиций, поля позиций (фьючерсы — только trading/)
    "    '/v5/account/set-margin-mode',",
    "    '/v5/spot-margin-trade/switch-mode',",
    "    body['positionIdx'] = 0",
    "    params = {'positionSide': 'LONG'}",
    # вывод и переводы — путь с ведущей «/» (нигде, и в trading/ тоже; см. MONEY_OUT ниже)
    "    '/v5/asset/withdraw/create',",
    "    url = f'{BINGX_BASE}/openApi/wallets/v1/capital/withdraw/apply'",
    "    '/v5/asset/transfer/inter-transfer',",
    "    '/v5/asset/transfer/universal-transfer',",
    "    '/api/v3/capital/sub-account/universalTransfer',",
    "    '/openApi/wallets/v1/capital/innerTransfer/apply',",
    "    '/openApi/api/v3/post/asset/transfer',",
    "    PATH = '/v1/transfer/to-personal'",
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
    # чтение истории и слова без пути — не вывод и не перевод
    "        trade_id = row['trade_id']",
    "    '/api/v3/capital/withdraw/history',",
    "    '/openApi/api/v3/capital/withdraw/history',",
    "    '/v5/asset/transfer/query-inter-transfer-list',",
    "    '/api/v1/withdrawals',",
    "    '/v1/query/deposit-withdraw',",
    "    'permitsUniversalTransfer': 'переводы между своими счетами',",
    "    (status 6). Вывод с transferType 2 — перевод другому пользователю BingX (innerTransfer, право Withdraw), а",
    "    positions = [], position_side = None",
]


@pytest.mark.parametrize("line", TRADING_LINES)
def test_guard_trading_code_catches(line):
    assert re.search(guard.TRADING_CODE, line), line


@pytest.mark.parametrize("line", NOT_TRADING_LINES)
def test_guard_trading_code_ignores(line):
    assert not re.search(guard.TRADING_CODE, line), line


# Строки торгового кода вне trading/ (как их видит guard), которые владелец проверил и принял: {путь: [строка без
# отступов, …]} — хуки подключения ядра в bot.py (импорт, выключатель из .env при старте, старт, цикл сверки, /trading и
# кнопки trd_*) и настройки торговли в .env.example. Guard не пускает такие строки в автомерж, а этот пин держит их и на ПК: новая строка
# TRADING/trd_*/import trading… в bot.py или accounts.py без записи здесь — красный тест, значит и смоук launcher, и
# обновление не встанет, пока владелец не впишет её сюда (защищённый файл — на ПК только с --approve), даже если CI обойдён.
TRADING_LINES_APPROVED = {   # подключение ядра к боту (ветка cloud/s2-trading-wiring, решение владельца 2026-09-28)
    '.env.example': [
        '# владельца бот не трогает. Launcher после каждого обновления кода пишет TRADING=0 — включает снова только владелец.',
        '# TRADING=1 — торговля включена (только строкой в этом файле: окружение Windows её не включит); иначе новых открытий нет',
        'TRADING=0',
        'TRADING_MODE=paper',
        'TRADING_SHORT_PAPER=0',
        'TRADING_MAX_LEVERAGE=2',          # этап владельца (хедж кругов, confirm, 29.09): плечо 2, позиция и всего
        'TRADING_MAX_POSITION_USDT=250',   # до 250 USDT (ETH-круг от 20 000 ₽ ≈ 220 USDT), день 5 USDT
        'TRADING_MAX_TOTAL_USDT=250',
        'TRADING_DAILY_LOSS_USDT=5',
        'TRADING_MAX_ORDERS_PER_MIN=',
        'TRADING_MAX_ORDERS_PER_DAY=',
        'TRADING_MINLOT_POSITION_USDT=',
        'TRADING_MINLOT_DAILY_LOSS_USDT=',
    ],
    'backup.py': [   # суточная копия журнала ордеров ядра (sqlite3 backup API, база в WAL), ревью этапа 5
        '"trading.db")         # журнал ордеров торгового ядра: позиции бота, результат дня (WAL — копия через backup '
        'API)',
    ],
    'bot.py': [
        'import trading.wiring',
        # хедж кругов (этап 5, решение владельца 29.09): карточка после «✅ Сделал» — вызов целиком одной строкой (сумма,
        # монета круга, курс, запас — всё в пине), и /hedge — условие «только владелец» и вызов
        'trading.hedge.offer_soon(self, "trade", trade_id, d[1].asset, hedge_plans.coin_qty(d, cfg.amount), '
        'cfg.amount, getattr(snap, "ref", 0.0) or 0.0, cfg.risk_buffer.get(d[1].asset, 0.0))  # noqa: E501 — одной '
        'строкой: вся строка в пине TRADING_LINES_APPROVED',
        'await trading.wiring.callback(self, cq, data, save_env)',
        'await trading.wiring.command(self, arg)',
        'elif cmd == "/hedge" and REPLY_CHAT.get() is None:   # только владелец → trading.wiring.hedge_command (пин)',
        'await trading.wiring.hedge_command(self, arg)',
        'trading.switch.switch_from_file(ENV_PATH)   # торговля: TRADING и TRADING_MODE — только из .env, извне не поднять',
        'trading.gates.flags_from_file(ENV_PATH)     # флаг владельца TRADING_SHORT_PAPER — тоже только из файла .env',
        'bot.trading_task = asyncio.ensure_future(trading.wiring.run(bot))',   # старт и сверка — своей задачей
    ],
}


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
    TRADING_LINES_APPROVED: хуки подключения ядра в bot.py и настройки в .env.example; ложных срабатываний на обычном
    коде нет."""
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


def test_guard_skips_trading_lines_in_tests_but_not_payout_lines(tmp_path, monkeypatch):
    """В tests/ строки торгового кода guard не считает (списки запретных путей там нарочно, как в этом файле;
    tests/trading/ и tests/conftest.py защищены путём), а строки выплат — считает, как раньше."""
    g = _repo(tmp_path, monkeypatch)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("P = '/v5/order/create'\nT = 'TRADING=1'\nimport trading\n",
                                                  encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "tests")
    assert guard.check("main") == []
    (tmp_path / "tests" / "test_y.py").write_text("X = 'pay_ok'\n", encoding="utf-8")
    (tmp_path / "tests" / "trading").mkdir()
    (tmp_path / "tests" / "trading" / "test_z.py").write_text("X = 1\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "more")
    found = guard.check("main")
    assert len(found) == 2 and set(found) == {
        "изменён защищённый файл: tests/trading/test_z.py",
        "tests/test_y.py: изменён код выплат (1 стр.) — только ручная проверка владельца"}, found


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

# Отправитель — обращение к методу запроса на объекте, который может быть сетевым клиентом:
# - STATE_METHODS (.post/.put/.delete/.patch/.request/.send/.urlopen/.ws_connect…) — отправитель, если объект не
#   заведомо свой: не очередь/словарь/хранилище, созданные тут же (asyncio.Queue(), {}, deque()…), не экземпляр своего
#   класса бота, не свой метод (self.send в классе со своим send; bot.send / self.bot.send, когда у класса Bot свой send),
#   не функция своего модуля (payouts.send), не объект с именем очереди/хранилища (q, queue, store, cache, db…);
# - READ_METHODS (.get/.head/.options/.open) — отправитель, только если объект — сетевой клиент: создан как клиент
#   (aiohttp.ClientSession(), urllib.request.build_opener()…), параметр или self-атрибут с именем сессии (s, session…)
#   или с аннотацией клиента, — или вызов похож на HTTP (URL первым аргументом, headers=/params=/allow_redirects=…);
#   dict.get('ключ') — не отправитель;
# - модуль: requests.get, aiohttp.request, urllib.request.urlopen, socket.create_connection, asyncio.open_connection…
STATE_METHODS = ("post", "put", "delete", "patch", "request", "_request", "send", "urlopen", "ws_connect",
                 "open_connection", "create_connection", "sendall", "sendto", "sock_sendall", "sock_connect")
READ_METHODS = ("get", "head", "options", "open")
SEND_METHODS = STATE_METHODS + READ_METHODS
DYNAMIC_LOOKUP = ("getattr", "__getattribute__", "attrgetter", "methodcaller")
DYNAMIC_IMPORT = ("__import__", "import_module")
NET_MODULES = ("requests", "httpx", "urllib3", "urllib.request", "http.client", "http", "socket", "ssl",
               "asyncio.streams", "websockets", "websocket", "ftplib", "smtplib", "telnetlib", "xmlrpc", "pycurl",
               "curl_cffi", "grpc")
AIOHTTP_SENDERS = ("request", "ClientSession", "ClientRequest", "TCPConnector", "Session", "client", "connector")
NET_ROOTS = frozenset({m.split(".")[0] for m in NET_MODULES} | {"aiohttp"})   # модуль-получатель: requests.get…
ASYNC_NET = ("open_connection", "create_connection", "sock_connect", "sock_sendall")   # asyncio.<это> — соединение
# вызовы (и аннотации), которые дают сетевой клиент: его .get/.post/.open — запрос
NET_CTORS = frozenset({
    "aiohttp.ClientSession", "aiohttp.ClientRequest", "urllib.request.build_opener", "urllib.request.OpenerDirector",
    "http.client.HTTPConnection", "http.client.HTTPSConnection", "requests.Session", "requests.session",
    "httpx.Client", "httpx.AsyncClient", "websockets.connect", "socket.socket", "socket.create_connection",
})
# вызовы (и аннотации), которые дают заведомо не сетевой объект: его .put/.delete/.send — не запрос
LOCAL_CTORS = frozenset({
    "dict", "list", "set", "frozenset", "tuple", "bytearray", "str", "int", "float", "bool", "bytes",
    "asyncio.Queue", "asyncio.LifoQueue", "asyncio.PriorityQueue", "asyncio.Event", "asyncio.Lock", "asyncio.Semaphore",
    "asyncio.BoundedSemaphore", "asyncio.Condition", "queue.Queue", "queue.SimpleQueue", "queue.LifoQueue",
    "queue.PriorityQueue", "collections.deque", "collections.OrderedDict", "collections.defaultdict",
    "collections.Counter", "contextvars.ContextVar", "threading.Event", "threading.Lock", "sqlite3.connect",
})
SESSION_NAMES = ("s", "sess", "session", "http", "client", "opener")   # параметр/self-атрибут с таким именем — сессия
LOCAL_NAME = re.compile(r"(?i)(?:^|_)(?:q|queue|store|cache|db|registry|pending|jobs|tasks|events|lock)$")
URL_NAME = re.compile(r"(?i)url|uri$|endpoint|(?:^|_)base$")
HTTP_KWARGS = frozenset({"headers", "params", "allow_redirects", "json", "data", "proxy", "ssl", "cookies", "auth",
                         "verify"})
NET, LOCAL, UNKNOWN = "net", "local", "?"

# (модуль, функция) — кто вообще может отправить запрос. У каждого — свой узкий allowlist путей или один хост.
ALLOWED_SENDERS = {
    ("accounts", "_get_json"): "подписанные GET Bybit/MEXC/HTX/KuCoin — пути только из *_READ_PATHS (чтение)",
    ("accounts", "bingx_get"): "GET BingX — только BINGX_READ_PATHS, без редиректов",
    ("accounts", "bybit_post"): "POST к Bybit — только BYBIT_POST_PATHS (история P2P-ордеров), запинено",
    ("accounts", "cryptomus_call"): "Cryptomus — только CRYPTOMUS_CALLS (балансы, история), чтение",
    ("payouts", "payout_call"): "выплаты Cryptomus — только PAYOUT_CALLS, защищённый payouts.py",
    ("p2p", "_json"): "публичные запросы без ключей — только p2p.JSON_ALLOWED (пин JSON_ALLOWED_PIN ниже)",
    ("p2p", "_bc_download"): "выгрузка BestChange: GET BC_URL (info.zip), без ключей",
    ("perp", "_get"): "публичные котировки перпов: GET только https на perp.HOSTS (пин PERP_HOSTS_PIN ниже), без ключей "
                      "и редиректов",
    ("bot", "Bot.call"): "Telegram Bot API",
    ("bot", "Bot._post_photo"): "Telegram sendPhoto",
    ("bot", "Bot.send_document"): "Telegram sendDocument",
    ("launcher", "notify"): "уведомление владельцу в Telegram (urllib, защищённый launcher.py)",
}
# p2p._json шлёт только это (метод, хост, путь; путь с «/» на конце — префикс + монета). Расширить — только владелец:
# сначала сюда (защищённый файл), потом в p2p.JSON_ALLOWED.
JSON_ALLOWED_PIN = frozenset({
    ("GET", "api.bybit.com", "/v5/market/orderbook"),
    ("GET", "api.bybit.com", "/v5/market/instruments-info"),
    ("GET", "api.mexc.com", "/api/v3/depth"),
    ("GET", "api.mexc.com", "/api/v3/exchangeInfo"),
    ("GET", "api.htx.com", "/market/depth"),
    ("GET", "api.htx.com", "/v1/common/symbols"),
    ("GET", "api.kucoin.com", "/api/v1/market/orderbook/level2_100"),
    ("GET", "api.kucoin.com", "/api/v2/symbols/"),
    ("POST", "api2.bybit.com", "/fiat/otc/configuration/queryAllPaymentList"),
    ("POST", "api2.bybit.com", "/fiat/otc/item/online"),
    ("GET", "www.htx.com", "/-/x/otc/v1/data/trade-market"),
    ("GET", "www.kucoin.com", "/_api/otc/ad/list"),
    ("GET", "www.mexc.com", "/api/platform/p2p/api/payment/method"),
    ("GET", "www.mexc.com", "/api/platform/p2p/api/common/coins"),
    ("GET", "p2p.mexc.com", "/api/market"),
    ("GET", "bitpapa.com", "/api/v1/pro/search"),
    ("GET", "www.lbank.com", "/lbk-api/otc-trade-center/fiat/p2p/adv/advertisementList"),
    ("GET", "api.rapira.net", "/open/market/rates"),
    ("GET", "api.bybit.com", "/v5/market/tickers"),
    ("GET", "api.mexc.com", "/api/v3/ticker/bookTicker"),
    ("GET", "api.htx.com", "/market/tickers"),
    ("GET", "api.kucoin.com", "/api/v1/market/allTickers"),
    ("GET", "api.htx.com", "/v2/reference/currencies"),
    ("GET", "api.kucoin.com", "/api/v3/currencies/"),
})
# perp._get (публичные котировки перпов, без ключей) ходит только на эти хосты. Расширить — только владелец, как выше
PERP_HOSTS_PIN = ("api.bybit.com", "open-api.bingx.com")
# сетевой модуль, импортированный целиком: (модуль бота или "*" — любой, сетевой модуль) → какие его атрибуты можно
# трогать (только через точку: socket.gethostname(), не s = socket)
ALLOWED_NET_IMPORTS = {
    ("*", "socket"): ("gethostbyname", "gethostbyname_ex", "gethostname", "timeout", "gaierror", "herror", "error"),
    ("*", "ssl"): ("SSLError", "SSLCertVerificationError", "SSLEOFError", "SSLZeroReturnError", "CertificateError"),
    ("*", "http"): ("HTTPStatus",),
    ("launcher", "urllib.request"): ("build_opener", "ProxyHandler"),
}
# getattr с вычисляемым именем — только эти (чтение настроек), в точности как написано. Не считается вычисляемым
# getattr(obj, k), где k — переменная цикла (for или генератор) в той же функции по константе модуля: кортеж/список/
# множество строк или словарь со строковыми ключами, связанной в модуле ровно один раз (presets.save_preset: for k in
# FIELDS) — имена видны в исходнике; есть среди них имя метода запроса — это отправитель.
ALLOWED_DYNAMIC = {
    ("bot", "Bot.apply_preset", "getattr(self.cfg, key)"),
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


def _own_classes(tree):
    """{класс: его методы} — классы верхнего уровня без чужих базовых классов (только свои из этого модуля или object):
    экземпляр такого класса — код бота, а не подкласс aiohttp.ClientSession."""
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    out = {}
    for name, node in classes.items():
        bases = [_dotted(b) for b in node.bases]
        if not node.keywords and all(b == "object" or b in classes for b in bases):
            out[name] = frozenset(n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return out


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


def _import_aliases(tree):
    """{имя в модуле: полное имя}: import urllib.request → urllib; import x as y → y: x; from a import b as c → c: a.b.
    И множество путей модулей, импортированных целиком (urllib.request, http.client), — это модуль, а не метод."""
    aliases, modules = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    aliases[a.name.split(".")[0]] = a.name.split(".")[0]
                parts = a.name.split(".")
                modules |= {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases, modules


def _scope_nodes(scope):
    """Узлы области видимости scope (функция, lambda или модуль) без тел вложенных функций и классов."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _params(fn):
    """{имя параметра: аннотация или None} функции или lambda."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return {}
    a = fn.args
    args = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]
    return {x.arg: x.annotation for x in args}


def _bindings(name, scope):
    """Что связывается с именем в области scope: значения присваиваний и with … as; None — неизвестно что (цикл,
    распаковка, +=, global)."""
    out = []
    for node in _scope_nodes(scope):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(node.value)
                elif not isinstance(t, ast.Name) and any(isinstance(x, ast.Name) and x.id == name
                                                         and isinstance(x.ctx, ast.Store) for x in ast.walk(t)):
                    out.append(None)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            out.append(node.value if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) else None)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None \
                and any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(node.optional_vars)):
            out.append(node.context_expr if isinstance(node.optional_vars, ast.Name) else None)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) \
                and any(isinstance(x, ast.Name) and x.id == name for x in ast.walk(node.target)):
            out.append(None)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            out.append(ast.Constant(None))   # объект исключения
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            out.append(None)
    return out


def _store_count(name, scope):
    """Сколько раз имя связывается в области scope (присваивания, for, with, except, import, global), не считая
    переменных генераторов — у них своя область видимости."""
    nodes = list(_scope_nodes(scope))
    in_comp = {id(x) for n in nodes if isinstance(n, ast.comprehension) for x in ast.walk(n.target)}
    count = 0
    for n in nodes:
        if isinstance(n, ast.Name) and n.id == name and not isinstance(n.ctx, ast.Load) and id(n) not in in_comp:
            count += 1
        elif isinstance(n, (ast.Global, ast.Nonlocal)) and name in n.names or \
                isinstance(n, ast.ExceptHandler) and n.name == name or \
                isinstance(n, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name).split(".")[0] == name
                                                                      for a in n.names):
            count += 1
    return count


def _module_literals(tree):
    """{имя: [строки]} — константы модуля: кортеж/список/множество строк или словарь со строковыми ключами; имя
    связано во всём модуле ровно один раз (верхний уровень, простое присваивание) и нигде не global."""
    stores = {}
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names = [node.id]
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names = list(node.names) * 2
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [(a.asname or a.name).split(".")[0] for a in node.names]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        for n in names:
            stores[n] = stores.get(n, 0) + 1
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and stores.get(node.targets[0].id) == 1:
            v = node.value
            elts = v.keys if isinstance(v, ast.Dict) else v.elts if isinstance(v, (ast.Tuple, ast.List, ast.Set)) \
                else None
            if elts is not None and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
                out[node.targets[0].id] = [e.value for e in elts]
    return out


def _url_like(node):
    """Похоже на URL: строка/f-строка с «://» или «http…», склейка с таким началом, имя вроде url/BC_URL/BYBIT_BASE."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "://" in node.value or node.value.startswith("http")
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        return _url_like(first.value if isinstance(first, ast.FormattedValue) else first)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _url_like(node.left)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return bool(URL_NAME.search(node.id if isinstance(node, ast.Name) else node.attr))
    return False


def _http_like(call):
    """Вызов .get/.open похож на HTTP-запрос: URL первым аргументом (или url=) или HTTP-параметры (headers=, …)."""
    if call is None:
        return False
    if {k.arg for k in call.keywords if k.arg} & HTTP_KWARGS:
        return True
    first = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "url"), None)
    return first is not None and _url_like(first)


def _surface(mod_name, src, local_defs=None, local_classes=None):
    """→ (отправители {(модуль, функция): [что]}, динамика {(модуль, функция, код)}, нарушения сетевых импортов [..]).
    Функция — полное имя: «Класс.метод», «внешняя.внутренняя», «<module>», «….<lambda>». local_defs — {модуль бота:
    имена его функций}: вызов своей функции другого модуля (payouts.send) — не отправитель, её тело проверяется само;
    local_classes — {модуль бота: {класс: методы}} (без чужих базовых классов): экземпляр своего класса — не клиент."""
    tree = ast.parse(src)
    senders, dynamic, net_bad = {}, set(), []
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    allowed_imports = {}
    for (m, net), attrs in ALLOWED_NET_IMPORTS.items():
        if m in ("*", mod_name):
            allowed_imports[net] = tuple(allowed_imports.get(net, ())) + tuple(attrs)
    local_defs = local_defs or {}
    local_mods = _local_imports(tree, local_defs)
    aliases, module_paths = _import_aliases(tree)
    own = _own_classes(tree)
    local_classes = dict(local_classes or {})
    local_classes[mod_name] = own
    safe_classes = set(own) | {f"{m}.{c}" for m, classes in local_classes.items() for c in classes}
    methods_by_class = {}
    for classes in local_classes.values():
        for c, methods in classes.items():
            methods_by_class[c.lower()] = methods_by_class.get(c.lower(), frozenset()) | methods
    literals = _module_literals(tree)

    def is_net(name):
        return name in NET_MODULES or name.split(".")[0] in NET_MODULES

    def add(where, what):
        senders.setdefault((mod_name, where), []).append(what)

    def resolve(node):
        d = _dotted(node)
        if d is None:
            return None
        root, _, rest = d.partition(".")
        full = aliases.get(root, root)
        return f"{full}.{rest}" if rest else full

    def chain(fn):
        """Функция, внешние функции (замыкание) и модуль — где искать связывание имени."""
        out = []
        while fn is not None:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                out.append(fn)
            fn = parents.get(fn)
        return out + [tree]

    def bound_locally(name, fn):
        return any(name in _params(sc) or _bindings(name, sc) for sc in chain(fn)[:-1])

    def combine(kinds):
        kinds = list(kinds)
        if NET in kinds:
            return NET
        return LOCAL if kinds and all(k == LOCAL for k in kinds) else UNKNOWN

    def type_kind(ann):
        name = resolve(ann) if ann is not None else None
        if name in NET_CTORS:
            return NET
        if name in LOCAL_CTORS or name in safe_classes:
            return LOCAL
        return UNKNOWN

    def kind(expr, fn, cls, depth=0):
        if expr is None or depth > 8:
            return UNKNOWN
        if isinstance(expr, ast.Await):
            return kind(expr.value, fn, cls, depth + 1)
        if isinstance(expr, (ast.Dict, ast.List, ast.Set, ast.Tuple, ast.DictComp, ast.ListComp, ast.SetComp,
                             ast.GeneratorExp, ast.Constant, ast.JoinedStr, ast.BinOp, ast.Compare)):
            return LOCAL
        if isinstance(expr, ast.IfExp):
            return combine(kind(e, fn, cls, depth + 1) for e in (expr.body, expr.orelse))
        if isinstance(expr, ast.BoolOp):
            return combine(kind(e, fn, cls, depth + 1) for e in expr.values)
        if isinstance(expr, ast.Call):
            return type_kind(expr.func)
        if isinstance(expr, ast.Name):
            for sc in chain(fn):
                params = _params(sc)
                values = _bindings(expr.id, sc)
                if expr.id in params:
                    ann = type_kind(params[expr.id])
                    return combine([ann if ann != UNKNOWN else NET if expr.id in SESSION_NAMES else UNKNOWN]
                                   + [kind(v, sc, cls, depth + 1) for v in values])
                if values:
                    return combine(kind(v, sc, cls, depth + 1) for v in values)
            return UNKNOWN
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self" \
                and cls is not None:
            want, kinds = f"self.{expr.attr}", []
            for item in cls.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) \
                        and item.target.id == expr.attr:
                    kinds.append(type_kind(item.annotation))   # attr: asyncio.Queue на уровне класса
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(item):   # self.attr = … в любом методе класса
                    if isinstance(node, ast.Assign):
                        pairs = []
                        for t in node.targets:
                            if isinstance(t, (ast.Tuple, ast.List)):
                                same = isinstance(node.value, (ast.Tuple, ast.List)) \
                                    and len(node.value.elts) == len(t.elts)
                                pairs += [(x, node.value.elts[i] if same else None) for i, x in enumerate(t.elts)]
                            else:
                                pairs.append((t, node.value))
                    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                        pairs = [(node.target, getattr(node, "value", None))]
                    else:
                        continue
                    for t, value in pairs:
                        if _dotted(t) != want:
                            continue
                        if value is not None and not isinstance(node, ast.AugAssign):
                            kinds.append(kind(value, item, cls, depth + 1))
                        else:
                            kinds.append(type_kind(node.annotation) if isinstance(node, ast.AnnAssign) else UNKNOWN)
            if not kinds:
                return NET if expr.attr in SESSION_NAMES else UNKNOWN
            return combine(kinds)
        return UNKNOWN

    def module_of(recv, fn):
        """Полное имя модуля, если получатель — импортированный модуль (и имя не связано в функции заново)."""
        d = _dotted(recv)
        if d is None or d.split(".")[0] not in aliases or bound_locally(d.split(".")[0], fn):
            return None
        return resolve(recv)

    def sends(recv, method, call, fn, cls, own_methods):
        mod = module_of(recv, fn)
        if mod is not None:
            local = local_mods.get(_dotted(recv))
            if local is not None:                     # функция модуля бота: её тело проверяется само
                return method not in local_defs.get(local, ())
            root = mod.split(".")[0]
            if root == "asyncio":
                return method in ASYNC_NET
            if root in NET_ROOTS:
                return True
            return method in STATE_METHODS            # неизвестный модуль: .send/.post — как раньше, отправитель
        if isinstance(recv, ast.Name) and recv.id == "self" and method in own_methods:
            return False                              # self.send в классе со своим send
        k = kind(recv, fn, cls)
        if k in (NET, LOCAL):
            return k == NET
        ident = recv.id if isinstance(recv, ast.Name) else recv.attr if isinstance(recv, ast.Attribute) else ""
        if ident and LOCAL_NAME.search(ident):
            return False                              # очередь, хранилище, кэш
        if method in READ_METHODS:
            return _http_like(call)
        return method not in methods_by_class.get(ident.lower(), ())   # bot.send / self.bot.send → свой Bot.send

    def loop_strings(arg, call, fn):
        """Строки константы модуля, по которой идёт ближайший цикл (for или генератор) с переменной arg вокруг call в
        этой же функции (см. ALLOWED_DYNAMIC); None — не такой случай."""
        if not isinstance(arg, ast.Name) or fn is None or arg.id in _params(fn):
            return None
        child, node = call, parents.get(call)
        while node is not None and node is not fn:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                return None
            loops = []
            if isinstance(node, (ast.For, ast.AsyncFor)) and child in node.body:
                loops = [(node.target, node.iter, True)]
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)) \
                    and not isinstance(child, ast.comprehension):
                loops = [(g.target, g.iter, False) for g in node.generators]
            for target, it, is_for in loops:
                names = [x.id for x in ast.walk(target) if isinstance(x, ast.Name)]
                if arg.id not in names:
                    continue
                first = target.elts[0] if isinstance(target, ast.Tuple) and target.elts else target
                if names.count(arg.id) != 1 or not (isinstance(first, ast.Name) and first.id == arg.id):
                    return None
                # for — переменная функции: больше нигде в ней не связывается (генераторы — свои области видимости)
                if is_for and _store_count(arg.id, fn) != 1:
                    return None
                unpack = isinstance(target, ast.Tuple)
                if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and not it.args \
                        and not it.keywords and it.func.attr in ("items", "keys") \
                        and unpack == (it.func.attr == "items"):
                    it = it.func.value
                elif unpack:
                    return None
                if not isinstance(it, ast.Name) or it.id not in literals or bound_locally(it.id, fn):
                    return None
                return literals[it.id]
            child, node = node, parents.get(node)
        return None

    def visit(node, where, own_methods, fn, cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where = node.name if where == "<module>" else f"{where}.{node.name}"
            if isinstance(node, ast.ClassDef):
                own_methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                cls = node
            else:
                fn = node
        elif isinstance(node, ast.Lambda):
            where, fn = f"{where}.<lambda>", node
        if isinstance(node, ast.Attribute) and node.attr in SEND_METHODS and _dotted(node) not in module_paths:
            parent = parents.get(node)
            call = parent if isinstance(parent, ast.Call) and parent.func is node else None
            if sends(node.value, node.attr, call, fn, cls, own_methods):
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
                    names = loop_strings(args[0], node, fn) if name == "getattr" else None
                    if names is None:
                        dynamic.add((mod_name, where, ast.unparse(node)))
                    elif any(n.lower() in SEND_METHODS for n in names):
                        add(where, f"{ast.unparse(node)} (стр. {node.lineno})")
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
            visit(child, where, own_methods, fn, cls)

    visit(tree, "<module>", set(), None, None)
    # разрешённый сетевой модуль (если модуль его импортирует): только его разрешённые атрибуты и только через точку
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names if a.asname is None}
    for net, attrs in allowed_imports.items():
        if net not in imported:
            continue
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
    local_classes = {m: _own_classes(ast.parse(src)) for m, (_, src) in sources.items()}
    senders, dynamic, net_bad = {}, set(), []
    for mod_name in runtime:
        s, d, n = _surface(mod_name, sources[mod_name][1], local_defs, local_classes)
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


def _list_problems(found, allowed, show):
    """Расхождение кода и списка: новые записи, пропавшие записи и пары «похоже на переименование» (тот же модуль:
    запись пропала, а рядом появилась новая) — с именами, чтобы владелец перенёс запись, а не гадал."""
    new, gone = sorted(set(found) - set(allowed)), sorted(set(allowed) - set(found))
    lines = [f"новое, в списке нет: {show(k)}" for k in new]
    lines += [f"есть в списке, в коде больше нет: {show(k)}" for k in gone]
    lines += [f"похоже на переименование или перенос: {show(o)} → {show(n)} — это новый код: владелец проверяет его "
              f"и переносит запись (защищённый файл); просто удалить старую запись — не то же самое"
              for o in gone for n in new if o[0] == n[0]]
    return lines


def test_only_known_senders(scan):
    """Запросы (и вообще любой сетевой клиент) шлют только функции из ALLOWED_SENDERS; запись, которой в коде уже
    нет, — тоже красный тест (её место не должен занять другой код)."""
    senders, _, _, _ = scan
    senders = {k: v for k, v in senders.items() if not _is_trading(k[0])}
    problems = _list_problems(senders, ALLOWED_SENDERS, lambda k: f"{k[0]}.{k[1]}"
                              + (f" ({', '.join(senders[k])})" if k in senders else ""))
    assert not problems, HOW_TO_UPDATE + "\nALLOWED_SENDERS:\n" + "\n".join(problems)


def test_only_known_dynamic_lookups(scan):
    _, dynamic, _, _ = scan
    dynamic = {d for d in dynamic if not _is_trading(d[0])}
    problems = _list_problems(dynamic, ALLOWED_DYNAMIC, lambda k: f"{k[0]}.{k[1]}: {k[2]}")
    assert not problems, HOW_TO_UPDATE + "\nALLOWED_DYNAMIC:\n" + "\n".join(problems)


def test_net_modules_used_only_as_allowed(scan):
    _, _, net_bad, _ = scan
    assert not net_bad, HOW_TO_UPDATE + "\n" + "\n".join(net_bad)


def test_public_json_allowlist_pinned():
    """p2p._json (публичные запросы без ключей) шлёт только p2p.JSON_ALLOWED — и он ровно JSON_ALLOWED_PIN: новая
    площадка или адрес — только после проверки владельцем (этот файл защищён)."""
    import p2p
    assert p2p.JSON_ALLOWED == JSON_ALLOWED_PIN, HOW_TO_UPDATE + (
        f"\nв p2p.JSON_ALLOWED лишнее: {sorted(p2p.JSON_ALLOWED - JSON_ALLOWED_PIN)}"
        f"\nнет в p2p.JSON_ALLOWED: {sorted(JSON_ALLOWED_PIN - p2p.JSON_ALLOWED)}")
    for method, host, path in JSON_ALLOWED_PIN:
        url = "https" + "://" + host + path + ("USDT" if path.endswith("/") else "")
        assert p2p.json_allowed(method, url), url
        assert not p2p.json_allowed(method, url.replace(host, host + ":443", 1))
        assert not p2p.json_allowed("PUT" if method == "GET" else "GET", url)


def test_perp_public_get_hosts_pinned():
    """perp._get (публичные котировки перпов Bybit/BingX, без ключей) — только https на perp.HOSTS, и он ровно
    PERP_HOSTS_PIN; другой хост, http, порт, логин в адресе — ValueError до отправки."""
    import perp
    assert tuple(perp.HOSTS) == PERP_HOSTS_PIN, HOW_TO_UPDATE + f"\nperp.HOSTS: {perp.HOSTS}"

    class Session:
        sent = []

        def get(self, url, **kw):
            self.sent.append(url)
            raise AssertionError(f"запрос ушёл: {url}")

    s, scheme = Session(), "https" + "://"
    for url in ("http" + "://api.bybit.com/v5/market/tickers", scheme + "api.bybit.com:8443/v5/market/tickers",
                scheme + "x@open-api.bingx.com/openApi/swap/v2/quote/premiumIndex", scheme + "api2.bybit.com/x",
                scheme + "open-api.bingx.com.example.org/x", scheme + "example.org/x"):
        with pytest.raises(ValueError):
            asyncio.run(perp._get(s, url))
    assert s.sent == []


def _hits(patterns, text):
    return [p for p in patterns if re.search(p, text, re.I)]


# списки запретных путей самого guard (TRADING_CODE — регулярные выражения по этим же путям): он ничего не отправляет,
# сам защищён путём, а его строки — описание запрета, а не вызов
PATTERN_FILES = ("scripts/guard.py",)


def test_no_trading_endpoints_or_trade_keys_outside_trading(scan):
    """Эндпоинты ордеров/позиций/своих P2P-объявлений и ключи *_trade — только в trading/."""
    _, _, _, strings = scan
    bad = []
    for mod_name, (path, src) in strings.items():
        if _is_trading(path) or path in PATTERN_FILES:
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
        if path in PATTERN_FILES:
            continue
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
    # фьючерсы — и чтение (позиции, исполнения, баланс фьючерсного счёта): так задумано, всё это — только trading/
    "/v5/position/list", "/v5/position/closed-pnl", "/openApi/swap/v2/user/balance", "/openApi/swap/v2/user/income",
])
def test_trading_endpoint_patterns_catch(path):
    """Ордера, позиции, маржа, свои P2P-объявления — вне trading/ нельзя; фьючерсные эндпоинты чтения тоже (решение
    владельца: торговое ядро целиком в защищённом trading/, бот вне его фьючерсы не читает)."""
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
            "async def f(d, cfg, key, row):\n"
            "    x = getattr(d, 'name', None)\n"
            "    y = row.get('plan_facts'), d.get(key, 0), cfg.get('url')\n"
            "    return d.get('post'), x, y, urllib.parse.urlencode({}), aiohttp.ClientTimeout(total=5)\n"
            "def g(r):\n    return r.request_info, r.url\n")
    senders, dynamic, net_bad = _fake_surface(body)
    assert senders == set() and dynamic == set() and net_bad == []
    _, dynamic, _ = _fake_surface("def f(cfg, k):\n    return getattr(cfg, k)\n")
    assert dynamic == {("m", "f", "getattr(cfg, k)")}


@pytest.mark.parametrize("body", [
    "async def f(s):\n    async with s.get('u') as r:\n        return await r.json()\n",   # параметр-сессия
    "async def f(session, u):\n    return await session.head(u)\n",
    "import aiohttp\nasync def f(u):\n    async with aiohttp.ClientSession() as c:\n        return await c.get(u)\n",
    "import aiohttp\nasync def f(u, own, s):\n    x = aiohttp.ClientSession() if own else s\n    return x.get(u)\n",
    "import aiohttp\nasync def f(x: aiohttp.ClientSession, u):\n    return x.get(u)\n",   # аннотация
    "async def f(obj, url):\n    return obj.get(url)\n",                                   # URL первым аргументом
    "async def f(obj, key):\n    return obj.get(key, headers={'X': '1'})\n",               # HTTP-параметры
    "async def f(obj, BYBIT_BASE):\n    return obj.get(f'{BYBIT_BASE}/v5/x')\n",
    "def f(u):\n    import urllib.request\n    o = urllib.request.build_opener()\n    return o.open(u)\n",
    "def f(u):\n    import requests\n    return requests.get(u)\n",
    "class C:\n    def __init__(self, s):\n        self.s = s\n    def f(self, u):\n        return self.s.get(u)\n",
])
def test_surface_sees_get_requests_on_network_clients(body):
    """GET тоже запрос: .get/.head/.open у сетевого клиента — отправитель (иначе список публичных адресов p2p._json
    обходился бы простым s.get(...) в новой функции)."""
    senders, _, _ = _fake_surface(body)
    assert senders and all(s.split(".")[-1] == "f" for s in senders), senders


def test_surface_ignores_queues_stores_and_own_bot_methods():
    """Ложные срабатывания, которых быть не должно: asyncio.Queue.put, store/cache/dict .delete, bot.send и
    self.bot.send (свой Bot.send — его тело проверяется само), .put своего класса, .send своей очереди."""
    body = ("import asyncio\nimport collections\n"
            "class Bot:\n"
            "    def __init__(self, s):\n        self.s = s\n        self.jobs = {}\n"
            "    async def send(self, text):\n        return await self.call('sendMessage', text)\n"
            "    async def call(self, m, t):\n        return await self.s.post(m, json=t)\n"
            "class Box:\n    def put(self, x):\n        return x\n"
            "class Paper:\n"
            "    def __init__(self, bot):\n        self.bot = bot\n        self.q = asyncio.Queue()\n"
            "        self.box, self.d = Box(), {}\n"
            "    async def note(self, store, cache, bot, queue):\n"
            "        await self.bot.send('x')\n        await bot.send('y')\n        await self.q.put(1)\n"
            "        store.delete('k')\n        cache.delete('k')\n        await queue.put(3)\n"
            "        self.box.put(1)\n        self.d.pop('a', None)\n        d = {}\n        d.get('url')\n"
            "        q = asyncio.Queue()\n        await q.put(2)\n        dq = collections.deque()\n"
            "        dq.append(1)\n        return self.jobs.get('a'), Box().put(2)\n")
    senders, dynamic, net_bad = _fake_surface(body)
    assert senders == {"Bot.call"} and dynamic == set() and net_bad == []
    # но то же имя с сетевым клиентом — отправитель: bot = aiohttp.ClientSession(); неизвестный obj.put — тоже
    for bad in ("import aiohttp\nasync def f(u):\n    bot = aiohttp.ClientSession()\n    return await bot.send(u)\n",
                "def f(obj, u):\n    return obj.put(u)\n", "def f(conn):\n    return conn.request('GET', '/')\n",
                "import aiohttp\nclass S(aiohttp.ClientSession):\n    pass\ndef f(u):\n    return S().post(u)\n"):
        assert _fake_surface(bad)[0] == {"f"}, bad


def test_getattr_over_module_literal_is_not_dynamic():
    """getattr(obj, k), где k — переменная цикла в той же функции по константе модуля (кортеж/список/множество строк,
    словарь со строковыми ключами; связана в модуле один раз): имена видны в исходнике — это не вычисляемый getattr.
    Имя метода запроса среди них — отправитель; всё остальное — по-прежнему в ALLOWED_DYNAMIC."""
    ok = ("FIELDS = ('assets', 'amount')\nMAP = {'a': 1, 'b': 2}\nNAMES = ['x']\n"
          "def f(cfg):\n    return {k: getattr(cfg, k) for k in FIELDS}\n"
          "def g(cfg):\n    for k, v in MAP.items():\n        getattr(cfg, k)\n    for n in NAMES:\n        getattr(cfg, n)\n"
          "def h(cfg):\n    return [getattr(cfg, k) for k in MAP.keys()] + [getattr(cfg, k) for k in MAP]\n")
    assert _fake_surface(ok) == (set(), set(), [])
    senders, dynamic, _ = _fake_surface("FIELDS = ('assets', 'post')\ndef f(s):\n    return [getattr(s, k) for k in FIELDS]\n")
    assert senders == {"f"} and dynamic == set()
    for body in (
            "FIELDS = ('a',)\nFIELDS = ('b',)\ndef f(c):\n    return [getattr(c, k) for k in FIELDS]\n",   # связано дважды
            "FIELDS = ('a',)\ndef g():\n    global FIELDS\n    FIELDS = ('post',)\n"
            "def f(c):\n    return [getattr(c, k) for k in FIELDS]\n",
            "def f(c, FIELDS):\n    return [getattr(c, k) for k in FIELDS]\n",                            # параметр
            "FIELDS = ('a',)\ndef f(c, FIELDS):\n    return [getattr(c, k) for k in FIELDS]\n",
            "X = 'post'\nFIELDS = ('a', X)\ndef f(c):\n    return [getattr(c, k) for k in FIELDS]\n",       # не строки
            "FIELDS = ('a',)\ndef f(c):\n    k = 'x'\n    for k in FIELDS:\n        getattr(c, k)\n",   # k связано дважды
            "FIELDS = ('a',)\ndef f(c):\n    for k in FIELDS:\n        pass\n    return lambda: getattr(c, k)\n",
            "FIELDS = ('a',)\ndef f(c):\n    return [getattr(c, k) for k in FIELDS.values()]\n",
            "FIELDS = {'a': 'post'}\ndef f(c):\n    return [getattr(c, v) for k, v in FIELDS.items()]\n"):
        senders, dynamic, _ = _fake_surface(body)
        assert dynamic and not senders, body


def test_allowed_net_import_limits_attributes():
    ok = "import socket\ndef f():\n    return socket.gethostbyname_ex(socket.gethostname())\n"
    assert _fake_surface(ok, "p2p") == (set(), set(), [])
    # в любом модуле: исключения ssl/socket, gethostbyname, HTTPStatus
    anywhere = ("import http\nimport socket\nimport ssl\nfrom http import HTTPStatus\nfrom socket import timeout\n"
                "def f(e):\n    return isinstance(e, (ssl.SSLError, socket.gaierror, socket.timeout)), "
                "socket.gethostbyname('localhost'), http.HTTPStatus.OK, HTTPStatus.NOT_FOUND\n")
    assert _fake_surface(anywhere, "bot") == (set(), set(), [])
    for body in ("import socket\ndef f(h):\n    return socket.create_connection((h, 80))\n",
                 "import socket\nS = socket\n",
                 "from socket import create_connection\n",
                 "import socket as s\n",
                 "import ssl\ndef f():\n    return ssl.create_default_context()\n",
                 "import http\ndef f(h):\n    return http.client.HTTPSConnection(h)\n",
                 "from http import client\n", "import http.client\n"):
        senders, _, net_bad = _fake_surface(body, "p2p")
        assert senders or net_bad, body
    launcher_ok = ("import urllib.error\nimport urllib.parse\nimport urllib.request\n"
                   "def notify(t):\n    o = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
                   "    o.open(t, b'x', timeout=5)\n"
                   "    return urllib.parse.urlencode({}), urllib.error.HTTPError\n")
    senders, _, net_bad = _fake_surface(launcher_ok, "launcher")
    assert senders == {"notify"} and net_bad == []
    assert _fake_surface(launcher_ok.replace("    o.open(t, b'x', timeout=5)\n", ""), "launcher") == (set(), set(), [])
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


def test_foreign_loopback_port_is_blocked(network_attempts, monkeypatch):
    """На 127.0.0.1 чужой порт — это, например, прокси VPN владельца: через него запрос ушёл бы наружу. Соединяться по
    loopback можно только с портами, которые открыл сам процесс тестов."""
    foreign = 10808   # не эфемерный порт: процесс тестов его сам не открывает
    # Проверяем транспорт через заданный прокси независимо от proxy bypass Windows и его DNS-вызовов.
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
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
