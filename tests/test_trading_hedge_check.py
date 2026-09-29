"""scripts/trading_hedge_check.py: проверка Bybit перед хеджем — только чтение, вывод по-русски, без сети (FakeSession)."""
import asyncio
import json
import os
import sys
from decimal import Decimal

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import trading_hedge_check as thc  # noqa: E402
from trading import keys as trade_keys  # noqa: E402
from trading import venues  # noqa: E402

KEY, SECRET = "TESTKEY-abcdef123456", "TESTSECRET-zyxwvu987654"
CREDS = (KEY, SECRET)
D = Decimal


# --- фальшивая сессия: только GET, ответы по пути запроса --------------------------------------------------------------

def bybit(result=None, code=0, msg="OK"):
    return {"retCode": code, "retMsg": msg, "result": {} if result is None else result}


class FakeResp:
    def __init__(self, status, body):
        self.status, self._raw = status, json.dumps(body).encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._raw


class FakeSession:
    """s.get(url, ...) → ответ по пути; любой не-GET и любой неизвестный путь — в журнал нарушений."""

    def __init__(self, routes):
        self.routes, self.calls, self.violations = routes, [], []

    def get(self, url, **kw):
        path, query = url.path, dict(url.query)
        self.calls.append((path, query))
        route = self.routes.get(path)
        if route is None:
            self.violations.append(f"неожиданный GET {path}")
            return FakeResp(200, bybit(code=10001, msg="unexpected path"))
        body = route(query) if callable(route) else route
        return FakeResp(*body) if isinstance(body, tuple) else FakeResp(200, body)

    def _write(self, *a, **kw):
        self.violations.append("запись: POST/PUT/DELETE")
        raise AssertionError("хедж-проверка обязана только читать")

    post = put = delete = request = _write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


MARK = {"BTCUSDT": "40000", "ETHUSDT": "2000", "GRAMUSDT": "2.5"}
LOT = {"BTCUSDT": ("0.001", "0.001", "5"), "ETHUSDT": ("0.01", "0.01", "5"), "GRAMUSDT": ("1", "1", "5")}
GOOD_KEY = {"readOnly": 0, "permissions": {"ContractTrade": ["Order", "Position"]}, "ips": ["203.0.113.5"]}


def flat_row(sym):
    return {"symbol": sym, "size": "0", "side": "", "positionIdx": 0, "leverage": "2", "avgPrice": "0", "markPrice": "0",
            "liqPrice": ""}


def routes(key=None, margin="ISOLATED_MARGIN", equity="200.5", rows=None, orders=None, mark=None, lot=None,
           listed=None):
    """Маршруты Bybit «всё хорошо»; ключевые аргументы подменяют куски. rows/orders — {символ: [строки]}."""
    mark, lot = {**MARK, **(mark or {})}, {**LOT, **(lot or {})}
    listed = listed if listed is not None else set(MARK)

    def instruments(q):
        sym = q["symbol"]
        if sym not in listed:
            return bybit({"category": "linear", "list": []})
        step, mn, notional = lot[sym]
        return bybit({"category": "linear", "list": [{
            "symbol": sym, "status": "Trading", "quoteCoin": "USDT", "settleCoin": "USDT",
            "contractType": "LinearPerpetual", "priceFilter": {"tickSize": "0.01"},
            "lotSizeFilter": {"qtyStep": step, "minOrderQty": mn, "maxOrderQty": "1000", "minNotionalValue": notional}}]})

    def tickers(q):
        sym = q["symbol"]
        return bybit({"category": "linear", "list": [{"symbol": sym, "markPrice": mark[sym],
                                                      "turnover24h": "90000000"}]})

    def positions(q):
        sym = q["symbol"]
        return bybit({"category": "linear", "list": (rows or {}).get(sym, [flat_row(sym)])})

    def order_list(q):
        return bybit({"category": "linear", "list": (orders or {}).get(q["symbol"], [])})

    return {
        "/v5/user/query-api": bybit(GOOD_KEY if key is None else key),
        "/v5/account/info": bybit({"marginMode": margin}),
        "/v5/account/wallet-balance": bybit({"list": [{"totalEquity": equity, "coin": []}]}),
        "/v5/position/list": positions,
        "/v5/order/realtime": order_list,
        "/v5/market/instruments-info": instruments,
        "/v5/market/tickers": tickers,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("HEDGE_ASSETS", "BYBIT_TRADE_API_KEY", "BYBIT_TRADE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    venues._RESOLVED.clear()
    yield
    venues._RESOLVED.clear()


def run(session, creds=CREDS, tmp=None, **kw):
    path = os.path.join(str(tmp), "keycheck.json") if tmp else None
    checks, errors = asyncio.run(thc.run_checks(session, creds=creds, check_path=path, **kw))
    return checks, errors


def by_title(checks, start):
    got = [c for c in checks if c.title.startswith(start)]
    assert got, [c.title for c in checks]
    return got[0] if len(got) == 1 else got


def levels(checks):
    return {c.title: c.level for c in checks}


# --- всё хорошо -------------------------------------------------------------------------------------------------------

def test_happy_path_all_ok_and_read_only(tmp_path):
    s = FakeSession(routes())
    checks, errors = run(s, tmp=tmp_path)
    # всё ✅, кроме постоянного ⚠️: свободный USDT ядро отдельно не читает (см. test_free_usdt_is_always_a_warning)
    rest = [(c.title, c.level) for c in checks if c.level != thc.OK]
    assert rest == [("Свободный USDT под маржу: не проверен", thc.WARN)], rest
    assert not s.violations and not errors
    titles = [c.title for c in checks]
    for expect in ("Права ключа", "Привязка ключа к IP", "Режим маржи аккаунта: Isolated", "Режим позиций: One-Way",
                   "Деньги на фьючерсном аккаунте", "Свободный USDT", "Инструмент BTCUSDT", "Инструмент ETHUSDT",
                   "Инструмент TONUSDT", "Ваши ручные позиции", "Часы ПК"):
        assert any(t.startswith(expect) for t in titles), expect
    # ничего кроме чтения: пути только из списка проверки
    allowed = {"/v5/user/query-api", "/v5/account/info", "/v5/account/wallet-balance", "/v5/position/list",
               "/v5/order/realtime", "/v5/market/instruments-info", "/v5/market/tickers"}
    assert {p for p, _ in s.calls} <= allowed


def test_happy_path_render_and_exit_code(tmp_path, capsys):
    s = FakeSession(routes())
    # ключ — через окружение, папка бота пустая
    os.environ["BYBIT_TRADE_API_KEY"], os.environ["BYBIT_TRADE_API_SECRET"] = CREDS
    rc = thc.main(["--bot-dir", str(tmp_path)], session_factory=lambda: s, env_loader=lambda p: None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "✅" in out and "❌ нет" in out and "Итог" in out
    # ⚠️ есть (свободный USDT не проверен) — «готов к хеджу» без оговорки печатать нельзя
    assert "⚠️ 1" in out and "готов, но есть предупреждения" in out and "готов к хеджу" not in out
    assert KEY not in out and SECRET not in out
    assert not s.violations
    # итог проверки ключа писался во временный файл, а не в data/ бота
    assert not os.path.exists(trade_keys.CHECK_PATH)


# --- ключ -------------------------------------------------------------------------------------------------------------

def test_no_key_saved_fails_and_asks_for_it(tmp_path):
    s = FakeSession(routes())
    checks, _ = run(s, creds=(), tmp=tmp_path)
    key = by_title(checks, "Торговый ключ")
    assert key.level == thc.FAIL and "не сохранён" in key.detail and "trading_keys.py set bybit" in key.fix
    # приватных запросов нет, а зависимые пункты не выдают ложный ✅
    assert not any(p in ("/v5/user/query-api", "/v5/account/info", "/v5/position/list", "/v5/account/wallet-balance")
                   for p, _ in s.calls)
    for start in ("Режим маржи", "Режим позиций", "Деньги на фьючерсном", "Ваши ручные", "Часы"):
        c = by_title(checks, start)
        assert c.level == thc.FAIL and "не проверено" in c.detail, start
    # публичные справочники читаются и без ключа
    assert by_title(checks, "Инструмент BTCUSDT").level == thc.OK


def test_unsafe_key_withdraw_or_transfer_is_fail(tmp_path):
    bad = {"readOnly": 0, "ips": ["203.0.113.5"],
           "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer", "SubMemberTransfer"]}}
    s = FakeSession(routes(key=bad))
    checks, _ = run(s, tmp=tmp_path)
    c = by_title(checks, "Права ключа")
    assert c.level == thc.FAIL and "Wallet:AccountTransfer" in c.detail and "НОВЫЙ ключ" in c.fix
    # ключ с лишними правами — дальше живые данные им не читаем
    assert not any(p in ("/v5/account/info", "/v5/position/list") for p, _ in s.calls)
    assert by_title(checks, "Режим маржи").level == thc.FAIL
    # запрос принят биржей — значит часы в порядке
    assert by_title(checks, "Часы ПК").level == thc.OK


def test_read_only_key_is_fail(tmp_path):
    s = FakeSession(routes(key={"readOnly": 1, "permissions": {"ContractTrade": ["Order", "Position"]},
                                "ips": ["203.0.113.5"]}))
    checks, _ = run(s, tmp=tmp_path)
    c = by_title(checks, "Права ключа")
    assert c.level == thc.FAIL and "только для чтения" in c.detail and "Contract" in c.fix


@pytest.mark.parametrize("ips", [[], ["*"], None])
def test_key_without_ip_binding_is_warning_not_fail(tmp_path, ips):
    key = dict(GOOD_KEY, ips=ips)
    if ips is None:
        del key["ips"]
    checks, _ = run(FakeSession(routes(key=key)), tmp=tmp_path)
    c = by_title(checks, "Привязка ключа к IP")
    assert c.level == thc.WARN and c.fix
    assert by_title(checks, "Права ключа").level == thc.OK
    assert not any(x.level == thc.FAIL for x in checks)


def test_ip_not_whitelisted_error_points_to_ip_fix(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/user/query-api"] = bybit(code=10010, msg="Unmatched IP, please check your API key's bound IP addresses.")
    checks, _ = run(s, tmp=tmp_path)
    c = by_title(checks, "Права ключа")
    assert c.level == thc.FAIL and "IP" in c.detail and "привязк" in c.fix.lower()


def test_wrong_key_error_points_to_recreate(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/user/query-api"] = bybit(code=10003, msg="API key is invalid.")
    checks, _ = run(s, tmp=tmp_path)
    c = by_title(checks, "Права ключа")
    assert c.level == thc.FAIL and "10003" in c.detail and "trading_keys.py set bybit" in c.fix


# --- режим маржи ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode, word", [("REGULAR_MARGIN", "Cross"), ("PORTFOLIO_MARGIN", "Portfolio")])
def test_margin_not_isolated_is_fail_and_owner_switches_it(tmp_path, mode, word):
    checks, _ = run(FakeSession(routes(margin=mode)), tmp=tmp_path)
    c = by_title(checks, "Режим маржи")
    assert c.level == thc.FAIL and word in c.detail
    assert "САМИ" in c.fix and "бот режим маржи Bybit не меняет" in c.fix and "Isolated Margin" in c.fix


def test_margin_unreadable_is_fail(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/account/info"] = bybit(code=10001, msg="params error")
    c = by_title(run(s, tmp=tmp_path)[0], "Режим маржи")
    assert c.level == thc.FAIL and "не прочитан" in c.detail


# --- режим позиций ----------------------------------------------------------------------------------------------------

def test_hedge_mode_with_open_position_is_fail(tmp_path):
    rows = {"ETHUSDT": [dict(flat_row("ETHUSDT"), size="0.5", side="Buy", positionIdx=1)]}
    c = by_title(run(FakeSession(routes(rows=rows)), tmp=tmp_path)[0], "Режим позиций")
    assert c.level == thc.FAIL and "хедж" in c.detail and "One-Way" in c.fix


def test_flat_hedge_mode_is_not_confirmed_one_way_warning(tmp_path):
    # Both Sides без позиции: строки positionIdx 1 и 2, строки 0 нет
    rows = {sym: [dict(flat_row(sym), positionIdx=1), dict(flat_row(sym), positionIdx=2)]
            for sym in ("BTCUSDT", "ETHUSDT", "GRAMUSDT")}
    for r in rows.values():
        for x in r:
            x["leverage"] = ""
    c = by_title(run(FakeSession(routes(rows=rows)), tmp=tmp_path)[0], "Режим позиций")
    assert c.level == thc.WARN and "не подтверждён" in c.detail and "One-Way" in c.fix


def test_position_read_error_is_fail(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/position/list"] = bybit(code=10001, msg="boom")
    c = by_title(run(s, tmp=tmp_path)[0], "Режим позиций")
    assert c.level == thc.FAIL and "не прочитаны" in c.detail


# --- деньги -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("equity, level", [("200.5", thc.OK), ("150", thc.OK), ("149.99", thc.WARN),
                                           ("125", thc.WARN), ("124.99", thc.FAIL), ("10", thc.FAIL)])
def test_funds_against_margin_250_at_2x(tmp_path, equity, level):
    c = by_title(run(FakeSession(routes(equity=equity)), tmp=tmp_path)[0], "Деньги на фьючерсном")
    assert c.level == level
    assert "125.00 USDT" in c.title and "150–200 USDT" in c.detail   # 250 / 2 = 125; с запасом — 150–200
    if level != thc.OK:
        assert "вручную" in c.fix and "бот переводов между счетами не делает" in c.fix


def test_funds_scale_with_position_and_leverage(tmp_path):
    c = by_title(run(FakeSession(routes(equity="300")), tmp=tmp_path, position=D(1000), leverage=D(2))[0],
                 "Деньги на фьючерсном")
    assert c.level == thc.FAIL and "500.00 USDT" in c.title
    c = by_title(run(FakeSession(routes(equity="290")), tmp=tmp_path, position=D(250), leverage=D(1))[0],
                 "Деньги на фьючерсном")
    assert c.level == thc.WARN and "250.00 USDT" in c.title   # нужно 250, есть 290 (< 300 с запасом)


def test_funds_unreadable_is_fail(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/account/wallet-balance"] = bybit(code=10001, msg="no")
    c = by_title(run(s, tmp=tmp_path)[0], "Деньги на фьючерсном")
    assert c.level == thc.FAIL and "не прочитан" in c.detail


def test_funds_check_says_it_compares_total_equity(tmp_path):
    c = by_title(run(FakeSession(routes(equity="1000")), tmp=tmp_path)[0], "Деньги на фьючерсном")
    assert c.level == thc.OK
    assert "ОБЩИЙ капитал" in c.detail and "totalEquity" in c.detail and "свободный USDT отдельно не проверялся" in c.detail


def test_free_usdt_is_always_a_warning(tmp_path):
    # даже при огромном капитале: доступный баланс ядро не отдаёт (в ответе кошелька есть только totalEquity)
    checks, _ = run(FakeSession(routes(equity="100000")), tmp=tmp_path)
    c = by_title(checks, "Свободный USDT")
    assert c.level == thc.WARN and "не проверен" in c.title
    assert "125.00" in c.fix and "150–200" in c.fix and "глазами" in c.fix
    assert by_title(checks, "Деньги на фьючерсном").level == thc.OK
    # без ключа приватных данных нет: там уже ❌ «не проверено», отдельного ⚠️ не добавляем
    assert not any(c.title.startswith("Свободный USDT") for c in run(FakeSession(routes()), creds=(), tmp=tmp_path)[0])


def test_free_usdt_follows_position_and_leverage(tmp_path):
    c = by_title(run(FakeSession(routes()), tmp=tmp_path, position=D(1000), leverage=D(2))[0], "Свободный USDT")
    assert "500.00" in c.fix


# --- инструменты ------------------------------------------------------------------------------------------------------

def test_instrument_facts_are_reported(tmp_path):
    c = by_title(run(FakeSession(routes()), tmp=tmp_path)[0], "Инструмент ETHUSDT")
    assert c.level == thc.OK
    assert "шаг лота 0.01 ETH" in c.detail and "мин. количество 0.01 ETH" in c.detail and "мин. номинал 5 USDT" in c.detail
    assert "цена 2000 USDT" in c.detail and "≈ 20.00 USDT" in c.detail


def test_min_lot_above_minlot_but_within_cap_is_warning(tmp_path):
    # BTC 0.001 × 65000 = 65 USDT: больше 50 (minlot), но меньше лимита позиции 250
    checks, _ = run(FakeSession(routes(mark={"BTCUSDT": "65000"})), tmp=tmp_path)
    c = by_title(checks, "Инструмент BTCUSDT")
    assert c.level == thc.WARN and "minlot" in c.detail and "confirm" in c.fix


def test_min_lot_above_position_cap_is_fail(tmp_path):
    checks, _ = run(FakeSession(routes(lot={"ETHUSDT": ("1", "1", "5")}, mark={"ETHUSDT": "3000"})), tmp=tmp_path)
    c = by_title(checks, "Инструмент ETHUSDT")
    assert c.level == thc.FAIL and "больше лимита позиции 250.00 USDT" in c.detail and "HEDGE_ASSETS" in c.fix


def test_min_notional_counts_too(tmp_path):
    checks, _ = run(FakeSession(routes(lot={"BTCUSDT": ("0.001", "0.001", "300")})), tmp=tmp_path)
    assert by_title(checks, "Инструмент BTCUSDT").level == thc.FAIL


def test_ton_symbol_not_listed_is_fail_when_hedged(tmp_path):
    checks, _ = run(FakeSession(routes(listed={"BTCUSDT", "ETHUSDT"})), tmp=tmp_path)
    c = by_title(checks, "Инструмент TONUSDT")
    assert c.level == thc.FAIL and "не подтверждён" in c.detail and "HEDGE_ASSETS=BTC,ETH" in c.fix


def test_ton_problem_is_only_warning_when_not_in_hedge_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("HEDGE_ASSETS", "BTC,ETH")
    checks, _ = run(FakeSession(routes(listed={"BTCUSDT", "ETHUSDT"})), tmp=tmp_path)
    c = by_title(checks, "Инструмент TONUSDT")
    assert c.level == thc.WARN and "не в HEDGE_ASSETS" in c.detail
    assert not any(x.level == thc.FAIL for x in checks)


def test_ton_uses_gram_symbol_after_resolution(tmp_path):
    s = FakeSession(routes())
    run(s, tmp=tmp_path)
    syms = {q.get("symbol") for p, q in s.calls if p == "/v5/market/instruments-info"}
    assert "GRAMUSDT" in syms and "TONUSDT" not in syms


def test_instrument_read_error_is_fail(tmp_path):
    s = FakeSession(routes())
    good = s.routes["/v5/market/instruments-info"]
    s.routes["/v5/market/instruments-info"] = lambda q: (500, {}) if q["symbol"] == "ETHUSDT" else good(q)
    c = by_title(run(s, tmp=tmp_path)[0], "Инструмент ETHUSDT")
    assert c.level == thc.FAIL and "не прочитан" in c.detail


# --- чужие позиции и ордера -------------------------------------------------------------------------------------------

def test_owner_manual_position_is_warning(tmp_path):
    rows = {"ETHUSDT": [dict(flat_row("ETHUSDT"), size="0.4", side="Buy", avgPrice="2000", markPrice="2000")]}
    c = by_title(run(FakeSession(routes(rows=rows)), tmp=tmp_path)[0], "Ваши ручные")
    assert c.level == thc.WARN and "ETHUSDT" in c.detail and "long" in c.detail and "не откроет" in c.fix


def test_owner_manual_order_is_warning(tmp_path):
    orders = {"BTCUSDT": [{"symbol": "BTCUSDT", "orderId": "111", "orderLinkId": "", "side": "Buy", "orderType": "Limit",
                           "qty": "0.01", "price": "30000", "stopOrderType": ""}]}
    c = by_title(run(FakeSession(routes(orders=orders)), tmp=tmp_path)[0], "Ваши ручные")
    assert c.level == thc.WARN and "BTCUSDT" in c.detail and "ордер" in c.detail


def test_no_manual_positions_is_ok(tmp_path):
    assert by_title(run(FakeSession(routes()), tmp=tmp_path)[0], "Ваши ручные").level == thc.OK


def test_foreign_unknown_ownership_is_never_ok_orders_unreadable(tmp_path):
    # мутант M16: «чьё это» не решить (ордера не прочитаны) → пункт не должен быть ✅
    s = FakeSession(routes())
    s.routes["/v5/order/realtime"] = bybit(code=10001, msg="no orders for you")
    c = by_title(run(s, tmp=tmp_path)[0], "Ваши ручные")
    assert c.level == thc.FAIL and "не прочитано" in c.detail and "BTCUSDT" in c.detail and c.fix


def test_foreign_unknown_ownership_is_never_ok_position_unreadable(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/position/list"] = bybit(code=10001, msg="no positions for you")
    c = by_title(run(s, tmp=tmp_path)[0], "Ваши ручные")
    assert c.level == thc.FAIL and "не прочитано" in c.detail


def test_foreign_unknown_beats_found_and_ok(tmp_path):
    # у ETH чужая позиция (⚠️), у BTC чтение не удалось: побеждает «не знаем» (❌), а не ⚠️ и не ✅
    rows = {"ETHUSDT": [dict(flat_row("ETHUSDT"), size="0.4", side="Buy", avgPrice="2000", markPrice="2000")]}
    s = FakeSession(routes(rows=rows))
    ok_orders = s.routes["/v5/order/realtime"]
    s.routes["/v5/order/realtime"] = lambda q: (bybit(code=10001, msg="no") if q["symbol"] == "BTCUSDT" else ok_orders(q))
    c = by_title(run(s, tmp=tmp_path)[0], "Ваши ручные")
    assert c.level == thc.FAIL and "BTCUSDT" in c.detail


def test_foreign_check_pure_missing_snapshot_is_fail():
    c = thc.foreign_check(["BTCUSDT"], {})
    assert c.level == thc.FAIL and "не читалось" in c.detail and c.level != thc.OK


# --- часы -------------------------------------------------------------------------------------------------------------

def test_clock_skew_error_is_fail_with_sync_fix(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/user/query-api"] = bybit(code=10002, msg="invalid request, please check your server timestamp or "
                                                          "recv_window param")
    checks, _ = run(s, tmp=tmp_path)
    c = by_title(checks, "Часы ПК")
    assert c.level == thc.FAIL and "10002" in c.detail and "Синхронизировать" in c.fix
    assert by_title(checks, "Права ключа").level == thc.FAIL


def test_clock_skew_in_later_read_is_fail(tmp_path):
    s = FakeSession(routes())
    s.routes["/v5/account/wallet-balance"] = bybit(code=10002, msg="recv_window")
    checks, _ = run(s, tmp=tmp_path)
    assert by_title(checks, "Часы ПК").level == thc.FAIL
    assert by_title(checks, "Деньги на фьючерсном").level == thc.FAIL


# --- вывод: ключи не печатаются ---------------------------------------------------------------------------------------

def test_render_never_prints_key_or_secret(tmp_path):
    s = FakeSession(routes())
    # биржа процитировала секрет в тексте ошибки
    s.routes["/v5/account/info"] = bybit(code=10001, msg=f"bad {SECRET} and {KEY}")
    checks, _ = run(s, tmp=tmp_path)
    text = "\n".join(thc.render(checks, CREDS))
    assert KEY not in text and SECRET not in text


def test_clean_masks_secrets():
    assert thc._clean(f"x {SECRET} y {KEY}", CREDS) == "x ••• y •••"
    assert thc._clean("abc", None) == "abc" and thc._clean("abc", ("ab", None)) == "abc"


def test_render_shows_fix_for_fail_and_warn_only(tmp_path):
    checks, _ = run(FakeSession(routes(margin="REGULAR_MARGIN", key=dict(GOOD_KEY, ips=[]))), tmp=tmp_path)
    lines = thc.render(checks, CREDS)
    text = "\n".join(lines)
    assert text.count("Что сделать:") == sum(1 for c in checks if c.level != thc.OK and c.fix)
    assert "❌" in lines[-3] or any(line.startswith("Итог: ❌") for line in lines)
    assert "Скрипт только читает" in lines[-1]


OK_CHECK = thc.Check(thc.OK, "Проверка А", "всё хорошо", "")
WARN_CHECK = thc.Check(thc.WARN, "Проверка Б", "не блокирует", "Прочтите.")
FAIL_CHECK = thc.Check(thc.FAIL, "Проверка В", "плохо", "Исправьте.")


def test_render_ready_only_without_warnings():
    text = "\n".join(thc.render([OK_CHECK, OK_CHECK]))
    assert "Аккаунт Bybit готов к хеджу." in text and "предупрежд" not in text
    warned = "\n".join(thc.render([OK_CHECK, WARN_CHECK]))
    assert "готов, но есть предупреждения" in warned and "готов к хеджу" not in warned and "⚠️ 1" in warned
    failed = "\n".join(thc.render([OK_CHECK, WARN_CHECK, FAIL_CHECK]))
    assert "готов" not in failed and "TRADING=1 включать рано" in failed
    assert "Как включать хедж" not in failed   # при ❌ инструкции по включению не показываем


def test_render_lists_warnings_next_to_the_verdict():
    other = thc.Check(thc.WARN, "Проверка Г", "", "Тоже прочтите.")
    lines = thc.render([OK_CHECK, WARN_CHECK, other])
    i = next(n for n, x in enumerate(lines) if x.startswith("Итог:"))
    assert "готов, но есть предупреждения:" in lines[i] and "⚠️ 2" in lines[i]
    assert lines[i + 1] == "   ⚠️ Проверка Б: не блокирует" and lines[i + 2] == "   ⚠️ Проверка Г"   # сразу под итогом, по порядку
    assert "Проверка А" not in "\n".join(lines[i:])                                                   # ✅ в список не попадают
    assert not any(x.startswith("   ⚠️") for x in thc.render([OK_CHECK]))
    assert not any(x.startswith("   ⚠️") for x in thc.render([WARN_CHECK, FAIL_CHECK])[-6:])       # при ❌ списка нет: итог и так про ❌


def test_unconfirmed_one_way_is_repeated_next_to_the_verdict(tmp_path):
    rows = {sym: [dict(flat_row(sym), positionIdx=1), dict(flat_row(sym), positionIdx=2)]
            for sym in ("BTCUSDT", "ETHUSDT", "GRAMUSDT")}
    for r in rows.values():
        for x in r:
            x["leverage"] = ""
    checks, _ = run(FakeSession(routes(rows=rows)), tmp=tmp_path)
    lines = thc.render(checks, CREDS)
    i = next(n for n, x in enumerate(lines) if x.startswith("Итог:"))
    tail = "\n".join(lines[i:])
    assert "готов, но есть предупреждения:" in lines[i]
    assert "   ⚠️ Режим позиций: One-Way: односторонний режим не подтверждён" in tail                # главное предупреждение не потеряно
    assert sum(x.startswith("   ⚠️") for x in lines[i + 1:]) == 2                                   # плюс «свободный USDT»: всего два ⚠️


def test_render_ladder_matches_core_gates():
    # gates.max_mode: пороги бумаги → сразу confirm; один сильный бэктест → minlot с РЕАЛЬНЫМИ ордерами
    text = "\n".join(thc.render([OK_CHECK]))
    assert "САМ разрешает РЕАЛЬНЫЕ ордера minlot (до 50 USDT)" in text
    assert "СРАЗУ, минуя minlot" in text and "confirm не ставьте" in text
    assert "10–20 реальных кругов" in text and "TRADING_MODE=paper" in text and "--backtest" in text
    # старая ошибочная версия («пока по бумаге меньше 14 дней … остаётся на бумаге», «сначала minlot») не вернулась
    assert "остаётся на бумаге" not in text and "TRADING_MODE=confirm (после" not in text


# --- main -------------------------------------------------------------------------------------------------------------

def write_keys(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "keys.json").write_text(json.dumps({"bybit_trade": {"key": KEY, "secret": SECRET}}), encoding="utf-8")


def test_main_reads_keys_from_bot_dir_and_returns_1_on_fail(tmp_path, capsys):
    write_keys(tmp_path)
    s = FakeSession(routes(margin="REGULAR_MARGIN"))
    rc = thc.main(["--bot-dir", str(tmp_path)], session_factory=lambda: s, env_loader=lambda p: None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "❌ Режим маржи аккаунта: Isolated" in out and "Cross" in out
    assert KEY not in out and SECRET not in out
    assert not s.violations
    assert any(p == "/v5/user/query-api" for p, _ in s.calls)   # ключ из папки бота нашли


def test_main_without_key_returns_1(tmp_path, capsys):
    s = FakeSession(routes())
    rc = thc.main(["--bot-dir", str(tmp_path)], session_factory=lambda: s, env_loader=lambda p: None)
    out = capsys.readouterr().out
    assert rc == 1 and "❌ Торговый ключ Bybit: не сохранён" in out


def test_main_loads_env_from_bot_dir(tmp_path):
    seen = []
    thc.main(["--bot-dir", str(tmp_path)], session_factory=lambda: FakeSession(routes()), env_loader=seen.append)
    assert seen == [os.path.join(str(tmp_path), ".env")]


def test_main_passes_position_and_leverage(tmp_path, capsys):
    write_keys(tmp_path)
    s = FakeSession(routes(equity="200"))
    rc = thc.main(["--bot-dir", str(tmp_path), "--position-usdt", "1000", "--leverage", "2"],
                  session_factory=lambda: s, env_loader=lambda p: None)
    out = capsys.readouterr().out
    assert rc == 1 and "маржа 500.00 USDT" in out and "позиция 1000.00 USDT" in out


@pytest.mark.parametrize("args", [["--leverage", "4"], ["--leverage", "0.5"], ["--position-usdt", "abc"],
                                  ["--position-usdt", "0"], ["--position-usdt", "-5"], ["--position-usdt", "nan"]])
def test_main_rejects_bad_numbers(tmp_path, capsys, args):
    called = []
    rc = thc.main(["--bot-dir", str(tmp_path), *args], session_factory=lambda: called.append(1),
                  env_loader=lambda p: None)
    assert rc == 2 and "⛔" in capsys.readouterr().out and not called


def test_main_network_failure_is_exit_2_without_leaking_url(tmp_path, capsys):
    write_keys(tmp_path)

    class Down:
        async def __aenter__(self):
            raise asyncio.TimeoutError("https://api.bybit.com/v5/x?api_key=" + KEY)

        async def __aexit__(self, *exc):
            return False

    rc = thc.main(["--bot-dir", str(tmp_path)], session_factory=Down, env_loader=lambda p: None)
    out = capsys.readouterr().out
    assert rc == 2 and "проверка не выполнена" in out and "таймаут" in out
    assert KEY not in out and "api.bybit.com" not in out


def test_main_key_check_record_not_written_to_bot_data(tmp_path):
    write_keys(tmp_path)
    thc.main(["--bot-dir", str(tmp_path)], session_factory=lambda: FakeSession(routes()), env_loader=lambda p: None)
    assert sorted(os.listdir(tmp_path / "data")) == ["keys.json"]   # trading_keycheck.json рядом не появился


# --- мелкие функции ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("HTTP 200, код 10002: invalid request, please check your server timestamp or recv_window param", "clock"),
    ("код 10010: Unmatched IP", "ip"), ("код 10003: API key is invalid.", "key"), ("код 10004: Error sign", "key"),
    ("таймаут запроса", "net"), ("нет соединения с api.bybit.com", "net"), ("код 110017: qty", "other"),
])
def test_kind(text, kind):
    assert thc._kind(text) == kind


def test_position_mode_check_pure():
    ok = {"BTCUSDT": ({"net": D(0), "rows": [], "leverage": D(2)}, "")}
    assert thc.position_mode_check(ok).level == thc.OK
    assert thc.position_mode_check({}).level == thc.FAIL
    unk = {"BTCUSDT": ({"net": D(0), "rows": [], "leverage": None}, "")}
    assert thc.position_mode_check(unk).level == thc.WARN
    # ненулевая позиция при неизвестном плече — не «нет строки 0»
    live = {"BTCUSDT": ({"net": D("-0.1"), "rows": [{}], "leverage": None}, "")}
    assert thc.position_mode_check(live).level == thc.OK


def test_source_has_no_write_calls_or_forbidden_words():
    src = open(os.path.join(ROOT, "scripts", "trading_hedge_check.py"), encoding="utf-8").read()
    for bad in (".post(", ".put(", ".delete(", ".request(", "requests", "urllib", "socket", "import research"):
        assert bad not in src, bad
