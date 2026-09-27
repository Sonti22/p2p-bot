import functools
import inspect
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
from asyncio import base_events, proactor_events, selector_events

import pytest

# --- Сеть в тестах заблокирована ------------------------------------------------------------------------------------
# Тесты — только офлайн (заглушки Session, фикстуры площадок): настоящий запрос из теста на ПК владельца (смоук launcher)
# ушёл бы на биржу с его ключами, а в CI — наружу. Блок ставится при загрузке conftest — до импорта модулей бота, сборки
# тестов и фикстур любого scope — на всех уровнях, через которые Python выходит в сеть: socket.connect/connect_ex/sendto,
# DNS (getaddrinfo, gethostbyname*), asyncio (create_connection, create_datagram_endpoint, sock_connect — на Windows
# Proactor соединяется мимо socket.connect); aiohttp, requests, urllib, http.client идут через них. Разрешены только
# unix-сокеты и loopback к портам, которые открыл сам процесс тестов (локальные aiohttp-серверы тестов, socketpair цикла
# asyncio на Windows); к чужому порту на 127.0.0.1 — нет: там слушает прокси VPN (у владельца системный прокси на
# 127.0.0.1), и запрос ушёл бы через него наружу. Имена loopback резолвить можно. Попытка — исключение
# сразу (соединения нет) и красный тест в конце, даже если код под тестом исключение проглотил. Метка allow_network
# снимает блок для одного теста — ни у одного теста её быть не должно. Нарочный обход (_socket напрямую, ctypes) этим не
# поймать — это остаточный риск; здесь — всё, что пишется без умысла спрятать.


class NetworkBlocked(ConnectionRefusedError):
    """Сетевая ошибка, как без сети: код, который её ждёт, ведёт себя как офлайн, а тест всё равно краснеет."""


# attempts — куда писать попытки (список текущего теста или outside); ports — порты, которые процесс открыл сам (bind)
_NET = {"allowed": False, "outside": [], "ports": set()}
_NET["attempts"] = _NET["outside"]
_NET_MP = pytest.MonkeyPatch()


def _local_host(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]").split("%", 1)[0]
    if host in ("", "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_addr(address):
    """Куда соединяться можно: unix-сокет или loopback-порт, открытый этим же процессом."""
    if isinstance(address, (str, bytes, os.PathLike)):   # AF_UNIX: путь к файлу, не сеть
        return True
    return (isinstance(address, tuple) and len(address) >= 2 and _local_host(address[0])
            and address[1] in _NET["ports"])


def _net_blocked(what, target):
    _NET["attempts"].append(f"{what}({target!r})")
    raise NetworkBlocked(f"сеть в тестах заблокирована: {what}({target!r}) — подмени запрос заглушкой "
                         f"(Session, p2p._json, фикстура offline)")


def _block(owner, name, target):
    """owner.name(...) — только если target(*args, **kwargs) — локальный адрес (target → (локальный?, что показать))."""
    real = getattr(owner, name)
    what = f"{getattr(owner, '__name__', owner)}.{name}"

    def check(args, kwargs):
        if not _NET["allowed"]:
            ok, shown = target(*args, **kwargs)
            if not ok:
                _net_blocked(what, shown)

    if inspect.iscoroutinefunction(real):
        @functools.wraps(real)
        async def wrapper(*args, **kwargs):
            check(args, kwargs)
            return await real(*args, **kwargs)
    else:
        @functools.wraps(real)
        def wrapper(*args, **kwargs):
            check(args, kwargs)
            return real(*args, **kwargs)
    _NET_MP.setattr(owner, name, wrapper)


def _host_arg(host, *args, **kwargs):
    return _local_host(host), host


def _sock_addr(sock, address, *args, **kwargs):
    return _local_addr(address), address


def _sendto_addr(sock, data, *rest):
    return _local_addr(rest[-1] if rest else None), (rest[-1] if rest else None)


def _loop_host(loop, protocol_factory, host=None, port=None, **kwargs):
    return _local_host(host), (host, port)


def _loop_remote(loop, protocol_factory, local_addr=None, remote_addr=None, **kwargs):
    return remote_addr is None or _local_addr(remote_addr), remote_addr


_real_bind = socket.socket.bind


def _bind_and_remember(self, address):
    """Порт, который процесс открыл сам (сервер теста, socketpair), — к нему по loopback соединяться можно."""
    _real_bind(self, address)
    try:
        name = self.getsockname()
    except OSError:
        return
    if isinstance(name, tuple) and len(name) >= 2:
        _NET["ports"].add(name[1])


_NET_MP.setattr(socket.socket, "bind", _bind_and_remember)
for _name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
    _block(socket, _name, _host_arg)
for _name in ("connect", "connect_ex"):
    _block(socket.socket, _name, _sock_addr)
_block(socket.socket, "sendto", _sendto_addr)
_block(base_events.BaseEventLoop, "create_connection", _loop_host)
_block(base_events.BaseEventLoop, "create_datagram_endpoint", _loop_remote)
for _cls in (selector_events.BaseSelectorEventLoop, proactor_events.BaseProactorEventLoop):
    _block(_cls, "sock_connect", lambda loop, sock, address: _sock_addr(sock, address))


def pytest_configure(config):
    config.addinivalue_line("markers", "allow_network: снять блок сети для одного теста (ни у одного теста её нет)")


@pytest.fixture(autouse=True)
def _no_network(request):
    """Сеть — только свои loopback-порты; попытка выйти наружу — красный тест (см. блок выше). → попытки этого теста."""
    attempts = []
    _NET.update(attempts=attempts, allowed=request.node.get_closest_marker("allow_network") is not None)
    yield attempts
    _NET.update(attempts=_NET["outside"], allowed=False)
    if attempts:
        pytest.fail("тест пытался выйти в сеть (заблокировано): " + "; ".join(attempts[:5]), pytrace=False)


@pytest.fixture
def network_attempts(_no_network):
    """Попытки выйти в сеть в этом тесте — для тестов самого блока (очисти список, если попытка ожидаема)."""
    return _no_network


def pytest_sessionfinish(session, exitstatus):
    """Попытки вне тестов (импорт, сборка, фикстуры module/session) — тоже красный прогон."""
    if _NET["outside"]:
        print("\nсеть вне тестов (заблокировано): " + "; ".join(_NET["outside"][:5]), file=sys.stderr)
        session.exitstatus = 1


import netstatus  # noqa: E402
import p2p  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _htx_currency(url):
    """Справочник валют HTX: фикстура хранит ответы по монетам, отдаём нужную по ?currency=."""
    d = load("htx_currencies.json").get(url.rsplit("currency=", 1)[-1].upper())
    return {"code": 200, "data": [d] if d else []}


def _kucoin_currency(url):
    d = load("kucoin_currencies.json").get(url.rstrip("/").rsplit("/", 1)[-1].upper())
    return {"code": "200000", "data": d or {}}


_HTX_COIN_BY_ID = {v: k for k, v in p2p.HTX_COIN.items()}
_MEXC_COIN_BY_ID = {c["coinId"]: c["coinName"] for c in json.load(
    open(os.path.join(FIX, "mexc_coins.json"), encoding="utf-8"))["data"]}


def _ads_name(prefix, asset, sell):
    """Фикстуры с ценами под конкретную монету заведены только для BTC/ETH (на порядки отличаются от USDT);
    для остальных монет отдаём фикстуру USDT — как и раньше, до разбора монеты из запроса."""
    suffix = "_sell" if sell else ""
    name = f"{prefix}_ads_{asset.lower()}{suffix}.json"
    if asset == "USDT" or not os.path.exists(os.path.join(FIX, name)):
        name = f"{prefix}_ads{suffix}.json"
    return name


def _bybit_ads(body):
    """Объявления Bybit различаются по стороне запроса (`side` в теле POST): "1" — бот покупает
    (площадке отдаём объявления продавцов), "0" — бот продаёт (объявления покупателей), у них разные
    цены и мерчанты, как в реальном стакане. Монета — `tokenId` в том же теле."""
    body = body or {}
    return load(_ads_name("bybit", body.get("tokenId", "USDT"), body.get("side") == "0"))


def _htx_ads(url):
    """HTX кодирует сторону бота в query `tradeType` (значение — противоположная сторона стакана),
    монету — числовым `coinId` (см. `p2p.HTX_COIN`)."""
    coin = _HTX_COIN_BY_ID.get(int(re.search(r"coinId=(\d+)", url).group(1)), "USDT")
    return load(_ads_name("htx", coin, "tradeType=buy" in url))


def _kucoin_ads(url):
    asset = re.search(r"currency=([^&]+)", url).group(1)
    return load(_ads_name("kucoin", asset, "side=BUY" in url))


def _mexc_ads(url):
    """Монета в запросе MEXC — внутренний `coinId` (хэш), а не тикер; обратно сопоставляем через
    справочник `mexc_coins.json` — тот же, что заполняет `p2p._mexc_coins`."""
    coin_id = re.search(r"coinId=([^&]+)", url).group(1)
    asset = _MEXC_COIN_BY_ID.get(coin_id, "USDT")
    return load(_ads_name("mexc", asset, "tradeType=BUY" in url))


def _bitpapa_ads(url):
    asset = re.search(r"crypto_currency_code=([^&]+)", url).group(1)
    return load(_ads_name("bitpapa", asset, "type=buy" in url))


def _lbank_ads(url):
    """LBank кодирует сторону бота в query `tradeType` как есть: buy — бот покупает (объявления продавцов),
    sell — бот продаёт. Монеты — только USDT/USDC (p2p.LBANK_ASSETS), USDC отдаёт фикстуру USDT."""
    asset = re.search(r"assetCode=([^&]+)", url).group(1)
    return load(_ads_name("lbank", asset, "tradeType=sell" in url))


# подстрока URL -> файл фикстуры (урезанные живые ответы площадок) или функция от URL
ROUTES = [
    # спот-тикеры и справочники сетей — раньше общих правил по доменам htx.com / kucoin.com
    ("api.htx.com/market/tickers", "spot_htx.json"), ("api.kucoin.com/api/v1/market/allTickers", "spot_kucoin.json"),
    ("api.htx.com/v2/reference/currencies", _htx_currency), ("api.kucoin.com/api/v3/currencies/", _kucoin_currency),
    ("queryAllPaymentList", "bybit_pay.json"),
    ("htx.com", _htx_ads), ("kucoin.com", _kucoin_ads),
    ("payment/method", "mexc_pay.json"), ("common/coins", "mexc_coins.json"),
    ("p2p.mexc.com/api/market", _mexc_ads), ("bitpapa.com", _bitpapa_ads), ("lbank.com", _lbank_ads),
    ("api.bybit.com/v5/market/tickers", "spot_bybit.json"), ("api.mexc.com/api/v3/ticker", "spot_mexc.json"),
    ("rapira.net", "rapira.json"),
]


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def offline(monkeypatch):
    """Все запросы площадок отвечают фикстурами; сеть в тестах не нужна."""
    async def fake_json(s, method, url, body=None):
        if "otc/item/online" in url:   # тело POST несёт сторону запроса — отдельно от ROUTES (там только url)
            return _bybit_ads(body)
        for part, name in ROUTES:
            if part in url:
                return name(url) if callable(name) else load(name)
        raise AssertionError(f"unexpected URL in test: {url}")

    async def no_bestchange(s, local=None):   # выгрузки BestChange в фикстурах нет — обменник недоступен, как без сети
        raise OSError("BestChange в тестах офлайн")

    monkeypatch.setattr(p2p, "_json", fake_json)
    # BestChange качается мимо _json (свой ClientSession с привязкой к локальному адресу) — без подмены тест со сканом
    # ходил на bestchange.ru с реального адреса ПК
    monkeypatch.setattr(p2p, "_bc_download", no_bestchange)
    monkeypatch.setattr(p2p, "_local_addrs", lambda: [])
    netstatus.reset()
    for cache in (p2p._bybit_pay, p2p._mexc_pay, p2p._mexc_coins):
        cache.clear()
    p2p._alt.update(t=0.0, ads=[], errors={}, key=None)
    p2p.TRAPS_LOG.clear()
    p2p._venue_backoff.clear()
    return fake_json


@pytest.fixture(autouse=True)
def _clean_netstatus():
    """Статусы сетей — модульное состояние; каждый тест начинает с пустой таблицы."""
    netstatus.reset()
    yield
    netstatus.reset()


# Изоляция от состояния живого бота. Смоук-тест launcher гоняет тесты в его папке: там data/ (ключи владельца,
# базы, отчёты), logs/, .env и .dev_status.json. Без изоляции тесты видят это состояние (и падают — обновление
# молча откатывается) и могут его испортить. Две линии защиты:
# 1) подмена: константы модулей и классов бота и значения аргументов по умолчанию (`path=DB_PATH` фиксируется
#    при импорте), ведущие в состояние, направляем во временную папку — на всю сессию (код сборки тестов,
#    фикстуры module/session) и заново пустую на каждый тест;
# 2) сторож: audit hook роняет тест при любом open/sqlite3.connect/rename/remove внутри состояния бота — ловит
#    то, что подмена не нашла (путь, собранный при вызове, словари, partial и т. п.).
# launcher в поиск не входит: у его тестов свои подмены. Корень не сканируем по glob — посторонний скрипт в папке
# бота не должен выполняться при загрузке тестов; берём модули, которые импортирует сам бот.
import bot as _bot  # noqa: E402,F401  (тянет все модули бота)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIRS = ("data", "logs")                                     # каталоги состояния целиком
STATE_ROOT_FILES = (".env", ".dev_status.json", ".last_good")     # файлы состояния в корне бота


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


_STATE_PREFIXES = [_norm(os.path.join(ROOT, d)) for d in STATE_DIRS]
_STATE_ROOT_ABS = {_norm(os.path.join(ROOT, f)) for f in STATE_ROOT_FILES}


def _in_state(full):
    n = _norm(full)
    return n in _STATE_ROOT_ABS or any(n == d or n.startswith(d + os.sep) for d in _STATE_PREFIXES)


def _state_rel(value):
    """Путь состояния бота относительно его папки ("data/keys.json", "data", ".env") или None. Относительный
    путь считаем от папки бота — так его видят бот и смоук-тест. Нужен разделитель: "data" без него — не путь."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or "://" in value or "\n" in value or not ("/" in value or os.sep in value):
        return None
    full = os.path.normpath(value if os.path.isabs(value) else os.path.join(ROOT, value))
    return os.path.relpath(full, ROOT).replace(os.sep, "/") if _in_state(full) else None


def _classes(owner, module_name):
    for obj in list(vars(owner).values()):
        if inspect.isclass(obj) and obj.__module__ == module_name:
            yield obj
            yield from _classes(obj, module_name)


def _functions(owner, module_name):
    """Функции и методы (вместе со слоями декораторов через __wrapped__) — у них пути бывают по умолчанию."""
    for obj in list(vars(owner).values()):
        obj = getattr(obj, "__func__", obj)   # staticmethod/classmethod
        while inspect.isfunction(obj) and obj.__module__ == module_name:
            yield obj
            obj = getattr(obj, "__wrapped__", None)


# Исходные значения собираем при первом просмотре модуля: подмены ниже строятся от них, а не от текущих
STATE_FILES = {}   # {исходный путь: путь относительно временной папки} — для отчёта и проверок
_ATTRS = {}        # {(модуль или класс, имя): исходное значение}
_FUNCS = {}        # {функция: (исходные __defaults__, __kwdefaults__)}
_CHECKED = set()   # имена уже просмотренных модулей sys.modules (любых, не только бота)


def _scan():
    """Досмотреть новые модули из папки бота (кроме launcher)."""
    for name, mod in list(sys.modules.items()):
        if name in _CHECKED:
            continue
        _CHECKED.add(name)
        f = getattr(mod, "__file__", None)
        if not f or name == "launcher" or _norm(os.path.dirname(os.path.abspath(f))) != _norm(ROOT):
            continue
        for owner in (mod, *_classes(mod, name)):
            for attr, value in list(vars(owner).items()):
                if _state_rel(value):
                    STATE_FILES[os.fspath(value)] = _state_rel(value)
                    _ATTRS[(owner, attr)] = value
            for func in _functions(owner, name):
                defaults, kwdefaults = func.__defaults__ or (), func.__kwdefaults__ or {}
                if any(_state_rel(d) for d in (*defaults, *kwdefaults.values())):
                    _FUNCS[func] = (func.__defaults__, func.__kwdefaults__)


def _redirect(mp, root):
    """Всё найденное состояние бота — в папку root (через monkeypatch mp, откат — его undo)."""
    def moved(value):
        rel = _state_rel(value)
        if rel is None:
            return value
        new = os.path.join(str(root), *rel.split("/"))
        return type(value)(new) if isinstance(value, os.PathLike) else new

    for d in STATE_DIRS:
        os.makedirs(os.path.join(str(root), d), exist_ok=True)
    for (owner, attr), value in _ATTRS.items():
        mp.setattr(owner, attr, moved(value))
    for func, (defaults, kwdefaults) in _FUNCS.items():
        if defaults:
            mp.setattr(func, "__defaults__", tuple(moved(d) for d in defaults))
        if kwdefaults:
            mp.setattr(func, "__kwdefaults__", {k: moved(v) for k, v in kwdefaults.items()})


_GUARD = {"on": False}
_GUARD_EVENTS = {"open", "sqlite3.connect", "os.rename", "os.remove", "os.rmdir", "os.mkdir", "os.truncate",
                 "os.listdir", "os.scandir", "shutil.rmtree", "shutil.copyfile", "shutil.move"}


def _guard(event, args):
    """Audit hook: тест не должен касаться состояния живого бота — ни читать, ни писать. Исключение
    возникает до самой операции, так что файл не открывается и не меняется. Путь проверяем от текущей папки."""
    if not _GUARD["on"] or event not in _GUARD_EVENTS:
        return
    for arg in args[:2]:
        if isinstance(arg, bytes):
            arg = os.fsdecode(arg)
        if isinstance(arg, os.PathLike):
            arg = os.fspath(arg)
        if isinstance(arg, str) and arg and arg != ":memory:" and _in_state(os.path.abspath(arg)):
            raise PermissionError(f"тест обращается к состоянию живого бота ({event}: {arg}) — подмени путь "
                                  f"на tmp_path или добавь его в изоляцию в tests/conftest.py")


_scan()
_SESSION_MP = pytest.MonkeyPatch()
_SESSION_ROOT = tempfile.mkdtemp(prefix="p2p-botstate-")
_redirect(_SESSION_MP, _SESSION_ROOT)
sys.addaudithook(_guard)   # снять hook нельзя — выключаем флагом в pytest_unconfigure
_GUARD["on"] = True


def pytest_unconfigure(config):
    _GUARD["on"] = False
    _SESSION_MP.undo()
    _NET_MP.undo()
    shutil.rmtree(_SESSION_ROOT, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path_factory, monkeypatch):
    """Каждый тест — с пустыми data/ и logs/ и без .env во временной папке. Папка своя, а не tmp_path: тесты
    вроде jsonstore проверяют, что в tmp_path пусто. Модули, которые тест импортировал сам, досматриваем тут."""
    _scan()
    root = tmp_path_factory.mktemp("botstate")
    _redirect(monkeypatch, root)
    return root / "data"


@pytest.fixture(autouse=True)
def _clean_logging():
    """Тесты setup_logging открывают файл в tmp_path — закрыть хендлер, чтобы Windows не держал файл."""
    yield
    root = logging.getLogger()
    for h in list(p2p._log_handlers):
        root.removeHandler(h)
        h.close()
    p2p._log_handlers.clear()


@pytest.fixture(autouse=True)
def _restore_environ():
    """save_env меняет os.environ — после каждого теста возвращаем окружение, чтобы настройки не протекали в другие."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
