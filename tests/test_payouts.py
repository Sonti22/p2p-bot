"""Выплаты Cryptomus (этап 2, только выплаты с бизнес-кошелька по подтверждению владельца): подпись по точным байтам,
allowlist вызовов, белый список, лимиты, выключатель, журнал и идемпотентность отправки, опрос статуса, кнопки бота,
гости, скрипт белого списка. Сеть — заглушка (ответ по методу и пути), ключи — фиктивные; настоящих запросов нет."""
import ast
import asyncio
import base64
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import time
from decimal import Decimal

import pytest
from yarl import URL

import accounts
import bot as B
import jsonstore
import p2p
import payouts
from helpers import arun
from payout_stubs import Stub, msg, sent

MERCHANT, KEY = "11111111-2222-3333-4444-555555555555", "FAKEPAYOUTKEY0123456789ABCDEFGH"
TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON2 = "TA4Y62o6YC2Zsck9rZVGTvqW1AQ7X9zTnj"
BTC = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
TON = "EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"
TON_UQ = "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqEBI"
EVM = "0x" + "ab" * 20
EIP55_OK = ("0x52908400098527886E0F7030069857D2E4169EE7", "0x8617E340B3D01FA5F11F306F4090FD50E238070D",   # тесты EIP-55
            "0xde709f2102306220921060314715629080e2fb77", "0x27b1fdb04752bbc536007a920d24acb045561c26",
            "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
            "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb")
ENTRIES = [
    {"id": "w1", "name": "Мой Bybit <TRC20>", "currency": "USDT", "network": "tron", "address": TRON},
    {"id": "w2", "name": "Холодный BTC", "currency": "BTC", "network": "btc", "address": BTC},
    {"id": "w3", "name": "TON на бирже", "currency": "TON", "network": "ton", "address": TON, "memo": "123456"},
    {"id": "w4", "name": "ETH в BSC", "currency": "ETH", "network": "bsc", "address": EVM},
]
PAY, INFO, SERVICES = ("POST", "/v1/payout"), ("POST", "/v1/payout/info"), ("POST", "/v1/payout/services")
_SPEC = importlib.util.spec_from_file_location(
    "payout_whitelist_script", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "payout_whitelist.py"))
payout_whitelist_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(payout_whitelist_script)


class Resp:
    """Ответ заглушки: статус и тело (JSON-объект, сырые байты) или исключение при входе (таймаут, обрыв)."""
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.exc = status, exc
        self.raw = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self.raw


class Session:
    """Заглушка aiohttp: ответ по (метод, путь) — Resp, список Resp (по очереди, последний повторяется) или функция от
    записанного запроса. Каждый запрос записывается; запрос не к api.cryptomus.com или без ответа — ошибка теста."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, method, url, headers=None, data=None, allow_redirects=True):
        assert url.startswith(accounts.CRYPTOMUS_BASE + "/"), url
        call = {"method": method, "path": URL(url).path, "headers": headers, "data": data, "redirects": allow_redirects}
        self.calls.append(call)
        r = self.routes.get((method, call["path"]))
        if r is None:
            raise AssertionError(f"unexpected request: {method} {call['path']}")
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        return r(call) if callable(r) else r

    def get(self, url, headers=None, allow_redirects=True):
        return self._answer("GET", url, headers, allow_redirects=allow_redirects)

    def post(self, url, headers=None, data=None, allow_redirects=True):
        return self._answer("POST", url, headers, data, allow_redirects)

    def posts(self, path="/v1/payout"):
        return [c for c in self.calls if c["method"] == "POST" and c["path"] == path]


def svc(cur, net, fee="1", pct="0", lo="1", hi="100000", avail=True):
    return {"network": net, "currency": cur, "is_available": avail,
            "limit": {"min_amount": lo, "max_amount": hi}, "commission": {"fee_amount": fee, "percent": pct}}


SERVICE_LIST = [svc("USDT", "TRON"), svc("BTC", "BTC", fee="0.0001", lo="0.0001"), svc("GRAM", "TON", fee="0.02", lo="0.1"),
                svc("ETH", "BSC", fee="0.0005", lo="0.001")]


def rate(code, course):
    return Resp(200, {"state": 0, "result": [{"from": code, "to": "USD", "course": "1"},
                                             {"from": code, "to": "USDT", "course": course}]})


def echo(status="process", final=False, **over):
    """Ответ создания/статуса выплаты по телу запроса: те же адрес, монета и сумма; сеть — ЗАГЛАВНЫМИ, сумма — с 8
    знаками, как в примерах Cryptomus."""
    def answer(call):
        body = json.loads(call["data"])
        res = {"uuid": "uuid-1", "amount": format(Decimal(body["amount"]), ".8f"), "currency": body["currency"],
               "network": body["network"].upper(), "address": body["address"], "txid": None, "status": status,
               "is_final": final, "balance": 129, "payer_currency": body["currency"], "payer_amount": 3}
        res.update(over)
        return Resp(200, {"state": 0, "result": res})
    return answer


def info_for(rows_by_order):
    """/v1/payout/info: ответ по order_id из тела; нет в словаре — «не найдено» (422, как отказы Cryptomus)."""
    def answer(call):
        oid = json.loads(call["data"])["order_id"]
        res = rows_by_order.get(oid)
        if res is None:
            return Resp(422, {"state": 1, "message": "Payout not found"})
        return Resp(200, {"state": 0, "result": res})
    return answer


def routes(over=None):
    r = {SERVICES: Resp(200, {"state": 0, "result": SERVICE_LIST}),
         ("GET", "/v1/exchange-rate/BTC/list"): rate("BTC", "60000"),
         ("GET", "/v1/exchange-rate/ETH/list"): rate("ETH", "3000"),
         ("GET", "/v1/exchange-rate/GRAM/list"): rate("GRAM", "5"),
         PAY: echo(), INFO: info_for({})}
    r.update(over or {})
    return r


def run(coro):
    """Корутина на общем цикле событий тестов (helpers.arun). asyncio.run на каждый вызов — новый Proactor-цикл с парой
    сокетов: за сотни вызовов на Windows кончались сокеты (WinError 10055) или socketpair зависал в accept."""
    return arun(coro)


async def until(ready, what, steps=1000):
    """Ждать условия, отдавая ход циклу событий, — без настоящих пауз: сеть — заглушки, журнал — синхронный sqlite,
    так что фоновая отправка продвигается только на этих шагах. Не дождались за steps шагов — тест красный."""
    for _ in range(steps):
        if ready():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"не дождались: {what}")


class MskNoonClock:
    """Часы payouts в тестах: настоящее время, сдвинутое так, что тест начинается ровно в 12:00 МСК. Дневной лимит
    считается по суткам МСК (created_ts, used_today() без now): без сдвига тест, начатый за миг до полуночи МСК,
    записал бы выплату во «вчера», а лимит сверил бы уже за «сегодня». Время идёт как настоящее; остальное из time —
    настоящее."""
    def __init__(self):
        now = time.time()
        self.offset = payouts.day_start(now) + 12 * 3600 - now

    def time(self):
        return time.time() + self.offset

    def __getattr__(self, name):
        return getattr(time, name)


def write_whitelist(entries, path=None):
    jsonstore.write_dict(path or payouts.WHITELIST_PATH, {"entries": entries})


@pytest.fixture(autouse=True)
def _payout_env(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(payouts, "DB_PATH", str(tmp_path / "payouts.db"))
    monkeypatch.setattr(payouts, "WHITELIST_PATH", str(tmp_path / "payout_whitelist.json"))
    monkeypatch.setattr(payouts, "RETRY_DELAY", 0)
    # цикл событий общий на все тесты: замок отправки и счётчик «⛔ Стоп» — свои у каждого теста (замок, оставшийся
    # занятым в упавшем тесте, иначе подвесил бы все следующие отправки)
    monkeypatch.setattr(payouts, "_lock", {"loop": None, "lock": None})
    monkeypatch.setattr(payouts, "_stop", {"n": 0, "wake": set()})
    monkeypatch.setattr(payouts, "time", MskNoonClock())
    monkeypatch.setenv("CRYPTOMUS_PAYOUT_API_KEY", MERCHANT)
    monkeypatch.setenv("CRYPTOMUS_PAYOUT_API_SECRET", KEY)
    monkeypatch.setenv("PAYOUTS", "1")
    for name in ("PAYOUT_MAX_ONE", "PAYOUT_DAILY_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    write_whitelist(ENTRIES)


def entry(eid="w1"):
    return payouts.whitelist_entry(eid)


def quote(s, eid="w1", amount="25"):
    q, why = run(payouts.quote(s, entry(eid), Decimal(amount)))
    assert why is None, why
    return q


# --- подпись по точным байтам и allowlist ---

def test_body_is_php_style_exact_bytes_and_signed_as_sent():
    """Компактный JSON, "/" -> "\\/", UTF-8 без \\u-экранирования (как json_encode + JSON_UNESCAPED_UNICODE)."""
    body = payouts.payout_body({"amount": "0.5", "address": "a/b", "name": "Привет €"})
    assert body == '{"amount":"0.5","address":"a\\/b","name":"Привет €"}'.encode("utf-8")
    assert payouts.payout_sign(body, KEY) == hashlib.md5(base64.b64encode(body) + KEY.encode()).hexdigest()
    assert payouts.payout_body(None) == b"" and payouts.payout_sign(b"", KEY) == hashlib.md5(KEY.encode()).hexdigest()


def test_payout_call_sends_exactly_the_signed_bytes_with_merchant_header():
    s = Session(routes())
    run(payouts.payout_call(s, "POST", "/v1/payout/info", {"order_id": "tg-1/2"}, creds=(MERCHANT, KEY)))
    run(payouts.payout_call(s, "POST", "/v1/payout/services", creds=(MERCHANT, KEY)))
    info, services = s.calls
    assert info["data"] == b'{"order_id":"tg-1\\/2"}'
    assert info["headers"] == {"merchant": MERCHANT, "sign": payouts.payout_sign(info["data"], KEY),
                               "Content-Type": "application/json"}
    assert services["data"] == b"" and services["headers"]["sign"] == hashlib.md5(KEY.encode()).hexdigest()
    assert all(c["redirects"] is False for c in s.calls)


def test_rate_lookup_is_public_get_without_key_or_sign():
    s = Session(routes())
    assert run(payouts.usdt_rate(s, "BTC")) == Decimal("60000")
    assert run(payouts.usdt_rate(s, "TON")) == Decimal("5")          # Toncoin у Cryptomus — GRAM
    assert run(payouts.usdt_rate(s, "USDC")) == Decimal(1) and run(payouts.usdt_rate(s, "USDT")) == Decimal(1)
    assert [(c["method"], c["path"], c["headers"], c["redirects"]) for c in s.calls] == [
        ("GET", "/v1/exchange-rate/BTC/list", None, False), ("GET", "/v1/exchange-rate/GRAM/list", None, False)]


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/transfer/to-personal"), ("POST", "/v1/transfer/to-business"), ("POST", "/v1/payment/refund"),
    ("GET", "/v1/payout"), ("POST", "/v1/balance"), ("POST", "/v1/payment"), ("POST", "/v1/payout/../transfer/to-personal"),
    ("GET", "/v1/exchange-rate/DOGE/list"), ("POST", "/v1/exchange-rate/BTC/list"), ("DELETE", "/v1/payout"),
    ("POST", "/v2/user-api/exchange/orders"), ("POST", "/v1/test-webhook/payout")])
def test_allowlist_refuses_everything_else_before_signing(method, path):
    s = Session({})
    with pytest.raises(ValueError):
        run(payouts.payout_call(s, method, path, {"amount": "1"}, creds=(MERCHANT, KEY)))
    assert s.calls == []


def test_allowlists_are_exactly_the_approved_calls():
    assert payouts.PAYOUT_CALLS == {("POST", "/v1/payout/services"), ("POST", "/v1/payout"),
                                    ("POST", "/v1/payout/info"), ("POST", "/v1/payout/list")}
    assert payouts.RATE_CALLS == {("GET", "/v1/exchange-rate/BTC/list"), ("GET", "/v1/exchange-rate/ETH/list"),
                                  ("GET", "/v1/exchange-rate/GRAM/list")}
    with pytest.raises(ValueError):   # курс — только GET без тела
        run(payouts.payout_call(Session({}), "GET", "/v1/exchange-rate/BTC/list", {"x": "1"}))
    with pytest.raises(ValueError):   # без ключа — ничего не подписываем
        run(payouts.payout_call(Session({}), "POST", "/v1/payout", {"amount": "1"}))


def test_read_only_account_allowlist_unchanged_and_payout_key_not_connectable():
    assert accounts.CRYPTOMUS_READ_CALLS == {("GET", "/v2/user-api/balance"), ("POST", "/v1/balance"),
                                             ("POST", "/v2/user-api/transaction/list")}
    assert payouts.KEY_NAME not in accounts.CONNECTABLE and payouts.KEY_NAME not in accounts.ONBOARDABLE
    assert payouts.credentials() == (MERCHANT, KEY)


def test_create_payload_strings_only_no_conversion_memo_only_for_ton():
    s = Session(routes())
    for eid, amount in (("w1", "25"), ("w3", "1.5"), ("w2", "0.001")):
        assert run(payouts.send(s, entry(eid), Decimal(amount), quote(s, eid, amount)))["state"] == "sent"
    bodies = [json.loads(c["data"]) for c in s.posts()]
    for b in bodies:
        assert set(b) <= {"amount", "currency", "network", "order_id", "address", "is_subtract", "memo"}
        assert all(isinstance(v, str) for v in b.values()) and b["is_subtract"] == "1"
    usdt, ton, btc = bodies
    assert usdt["amount"] == "25" and usdt["currency"] == "USDT" and usdt["network"] == "tron" and "memo" not in usdt
    assert ton["currency"] == "GRAM" and ton["network"] == "ton" and ton["memo"] == "123456" and ton["amount"] == "1.5"
    assert btc["amount"] == "0.001" and "memo" not in btc


# --- белый список и сумма ---

def test_address_formats_and_checksums():
    ok = [("tron", TRON), ("tron", TRON2), ("btc", BTC), ("btc", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"),
          ("btc", "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"),
          ("btc", "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"), ("ton", TON), ("ton", TON_UQ),
          ("ton", "0:" + "0" * 64), ("bsc", EVM), ("polygon", EVM), *(("eth", a) for a in EIP55_OK)]
    for net, addr in ok:
        assert payouts.address_error(net, addr) is None, (net, addr)
    bad = [("tron", TRON[:-1] + "u"), ("tron", TRON + " "), ("tron", " " + TRON), ("tron", "T" + "1" * 33),
           ("eth", "0x" + "AbCdEf0123" * 4), ("bsc", EIP55_OK[4][:-1] + "D"), ("eth", EIP55_OK[5].replace("fB", "Fb")),
           ("tron", EVM), ("btc", BTC[:-1] + "5"), ("btc", BTC.upper()), ("btc", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb"),
           ("ton", TON[:-1] + "M"), ("ton", "kQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"), ("bsc", EVM[:-1]),
           ("bsc", "0x" + "g" * 40), ("eth", TRON), ("sol", TRON), ("tron", "T R7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"),
           ("tron", ""), ("tron", None), ("bsc", "0x" + "ab" * 19 + "аб")]
    for net, addr in bad:
        assert payouts.address_error(net, addr), (net, addr)


def test_whitelist_skips_invalid_entries_with_warning(caplog):
    caplog.set_level(logging.WARNING)
    write_whitelist(ENTRIES + [
        {"id": "b1", "name": "плохая сеть", "currency": "USDT", "network": "btc", "address": BTC},
        {"id": "b2", "name": "плохой адрес", "currency": "USDT", "network": "tron", "address": TRON[:-1] + "u"},
        {"id": "b3", "name": "memo не для ton", "currency": "USDT", "network": "tron", "address": TRON2, "memo": "1"},
        {"id": "b4", "name": "чужая монета", "currency": "DOGE", "network": "tron", "address": TRON2},
        {"id": "w1", "name": "повтор id", "currency": "USDT", "network": "tron", "address": TRON2},
        {"id": "b5", "name": "", "currency": "USDT", "network": "tron", "address": TRON2}, "мусор"])
    assert [e["id"] for e in payouts.load_whitelist()] == ["w1", "w2", "w3", "w4"]
    assert caplog.text.count("пропущена") == 7
    assert payouts.load_whitelist(str(os.path.join(os.path.dirname(payouts.WHITELIST_PATH), "нет.json"))) == []


def test_bot_code_never_writes_the_whitelist():
    for mod in (payouts, B):
        src = inspect.getsource(mod)
        assert "write_dict(WHITELIST_PATH" not in src and "write_dict(payouts.WHITELIST_PATH" not in src
    assert "write_dict" not in inspect.getsource(payouts)


@pytest.mark.parametrize("text,ok", [("25", "25"), ("0,5", "0.5"), ("0.00000001", "0.00000001"), (" 10 ", "10"),
                                     ("0", None), ("-1", None), ("1e3", None), ("abc", None), ("1 000", None),
                                     ("0.000000001", None), ("", None), ("nan", None), ("inf", None)])
def test_parse_amount(text, ok):
    amount, why = payouts.parse_amount(text)
    assert (payouts.fmt(amount) if amount is not None else None) == ok and (why is None) == (ok is not None)


# --- предпросмотр, лимиты, курс, выключатель ---

def test_quote_live_fee_debit_and_usdt_value():
    s = Session(routes({SERVICES: Resp(200, {"state": 0, "result": [svc("USDT", "tron", fee="1", pct="0.5")]})}))
    q = quote(s, "w1", "100")
    assert q["fee"] == Decimal("1.5") and q["debit"] == Decimal("101.5") and q["usdt"] == Decimal("101.50")
    q = quote(Session(routes()), "w2", "0.01")
    assert q["fee"] == Decimal("0.0001") and q["usdt"] == Decimal("606.00")   # (0.01 + 0.0001) × 60000
    assert q["used"] == 0 and q["daily"] == Decimal(2000) and q["max_one"] == Decimal(2000)


@pytest.mark.parametrize("services,reason", [
    ([svc("USDT", "TRON", avail=False)], "не выплачивает"), ([svc("USDT", "BSC")], "не выплачивает USDT в сети tron"),
    ([svc("USDT", "TRON", lo="50")], "меньше минимума"), ([svc("USDT", "TRON", hi="10")], "больше максимума"),
    ([{"network": "TRON", "currency": "USDT", "is_available": True}], "не отдал лимиты")])
def test_quote_refuses_unavailable_service_or_amount_out_of_range(services, reason):
    s = Session(routes({SERVICES: Resp(200, {"state": 0, "result": services})}))
    q, why = run(payouts.quote(s, entry("w1"), Decimal("25")))
    assert q is None and reason in why


@pytest.mark.parametrize("answer", [Resp(500, {"message": "Server error"}), Resp(200, {"state": 0, "result": []}),
                                    Resp(200, {"state": 0, "result": [{"from": "BTC", "to": "USDT", "course": "0"}]}),
                                    Resp(200, b"not json"), Resp(exc=asyncio.TimeoutError())])
def test_rate_unavailable_refuses_fail_closed(answer):
    s = Session(routes({("GET", "/v1/exchange-rate/BTC/list"): answer}))
    q, why = run(payouts.quote(s, entry("w2"), Decimal("0.001")))
    assert q is None and "нет курса" in why and not s.posts()


def _row(eid="w1", usdt="100", state="sent", created=None):
    e = entry(eid)
    row = payouts._insert_intent(e, Decimal("1"), {"fee": Decimal(0), "debit": Decimal(1), "usdt": Decimal(usdt)})
    fields = {"state": state}
    if created is not None:
        fields["created_ts"] = created
    return payouts._update(row["order_id"], **fields)


def test_daily_limit_counts_pending_unknown_paid_but_not_rejected_or_failed():
    now = payouts.day_start(time.time()) + 3600      # 01:00 МСК
    for state, usdt in (("prepared", "1"), ("sending", "2"), ("sent", "10"), ("unknown", "100"),
                        ("final_paid", "1000"), ("rejected", "5000"), ("final_failed", "7000")):
        _row(usdt=usdt, state=state, created=now - 60)
    _row(usdt="900", state="final_paid", created=payouts.day_start(now) - 1)   # вчера по МСК — не в счёт
    assert payouts.used_today(now) == Decimal("1113")
    assert payouts.check_limits(Decimal("887"), now) is None
    assert "дневной лимит" in payouts.check_limits(Decimal("888"), now)


def test_msk_day_boundary_not_utc():
    start = payouts.day_start(time.time())
    assert time.strftime("%H:%M", time.gmtime(start)) == "21:00"   # полночь МСК = 21:00 UTC


def test_per_payout_limit_includes_fee():
    q, why = run(payouts.quote(Session(routes()), entry("w1"), Decimal("2000")))   # 2000 + комиссия 1 > 2000
    assert q is None and "одной выплаты" in why
    assert quote(Session(routes()), "w1", "1999")["usdt"] == Decimal("2000.00")


def test_limits_from_env_and_garbage_fails_closed(monkeypatch):
    monkeypatch.setenv("PAYOUT_DAILY_LIMIT", "150")
    q, why = run(payouts.quote(Session(routes()), entry("w1"), Decimal("200")))
    assert "одной выплаты" not in why and "дневной лимит" in why
    monkeypatch.setenv("PAYOUT_MAX_ONE", "много")
    assert payouts.limits()[0] == 0
    q, why = run(payouts.quote(Session(routes()), entry("w1"), Decimal("1")))
    assert q is None and "одной выплаты" in why


def test_kill_switch_at_preview_and_at_send_without_any_request(monkeypatch):
    s = Session(routes())
    q = quote(s, "w1", "25")
    calls = len(s.calls)
    monkeypatch.setenv("PAYOUTS", "0")
    assert run(payouts.quote(s, entry("w1"), Decimal("25")))[1] == payouts.OFF
    res = run(payouts.send(s, entry("w1"), Decimal("25"), q))
    assert res["state"] == "refused" and res["reason"] == payouts.OFF
    assert len(s.calls) == calls and payouts.history() == []
    monkeypatch.delenv("PAYOUTS")
    assert not payouts.enabled()


def test_send_rechecks_daily_limit_with_payouts_made_after_preview():
    s = Session(routes())
    q = quote(s, "w1", "1000")
    _row(usdt="1500", state="unknown")               # появилась выплата с неясным исходом
    res = run(payouts.send(s, entry("w1"), Decimal("1000"), q))
    assert res["state"] == "refused" and "дневной лимит" in res["reason"] and not s.posts()


def test_send_refuses_when_fee_grew_or_service_closed_since_preview():
    s = Session(routes())
    q = quote(s, "w1", "25")
    s.routes[SERVICES] = Resp(200, {"state": 0, "result": [svc("USDT", "TRON", fee="3")]})
    res = run(payouts.send(s, entry("w1"), Decimal("25"), q))
    assert res["state"] == "refused" and "комиссия Cryptomus выросла" in res["reason"]
    s.routes[SERVICES] = Resp(200, {"state": 0, "result": [svc("USDT", "TRON", avail=False)]})
    assert run(payouts.send(s, entry("w1"), Decimal("25"), q))["state"] == "refused"
    assert not s.posts() and payouts.history() == []


@pytest.mark.parametrize("change", ["address", "removed", "network"])
def test_whitelist_changed_or_removed_between_preview_and_send_refused(change):
    s = Session(routes())
    e = entry("w1")
    q = quote(s, "w1", "25")
    changed = [dict(x) for x in ENTRIES]
    if change == "address":
        changed[0]["address"] = TRON2
    elif change == "network":
        changed[0].update(currency="USDT", network="bsc", address=EVM)
    else:
        changed = changed[1:]
    write_whitelist(changed)
    res = run(payouts.send(s, e, Decimal("25"), q))
    assert res["state"] == "refused" and "белого списка" in res["reason"]
    assert not s.posts() and payouts.history() == []


# --- отправка и идемпотентность ---

def test_success_persists_intent_before_request_and_sends_once():
    seen = []

    def create(call):
        oid = json.loads(call["data"])["order_id"]
        seen.append(payouts.get(oid))                 # намерение уже в журнале до ответа Cryptomus
        return echo()(call)

    s = Session(routes({PAY: create}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "sent" and res["event"] is None
    row = res["row"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", row["order_id"])
    assert seen[0]["state"] == "sending" and seen[0]["address"] == TRON and seen[0]["amount"] == "25"
    assert row["uuid"] == "uuid-1" and row["status"] == "process" and row["fee"] == "1" and row["usdt_value"] == "26.00"
    assert len(s.posts()) == 1 and not s.posts("/v1/payout/info")
    assert payouts.used_today() == Decimal("26")


def test_immediate_final_status_in_create_response():
    s = Session(routes({PAY: echo(status="paid", final=True, txid="0xabc")}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "final_paid" and res["row"]["txid"] == "0xabc"


def test_business_error_confirmed_not_found_is_rejected_and_not_counted():
    s = Session(routes({PAY: Resp(422, {"state": 1, "message": "Not enough funds"})}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "rejected" and res["reason"] == "Not enough funds"
    assert len(s.posts()) == 1 and len(s.posts("/v1/payout/info")) == 1   # отказ сверен с /info по тому же order_id
    assert payouts.used_today() == 0
    assert "Деньги не ушли" in B.payout_result_text(res)


def test_business_error_but_info_finds_payout_adopts_it():
    def info(call):
        return echo()(dict(call, data=s.posts()[0]["data"]))
    s = Session(routes({PAY: Resp(200, {"state": 1, "message": "Something odd"}), INFO: info}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "sent" and res["event"] == "found"


def test_timeout_then_info_found_adopts_without_resend():
    def info(call):
        return echo(status="check")(dict(call, data=s.posts()[0]["data"]))
    s = Session(routes({PAY: Resp(exc=asyncio.TimeoutError()), INFO: info}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "sent" and res["row"]["status"] == "check"
    assert len(s.posts()) == 1


def test_timeout_then_not_found_resends_same_order_id_and_same_bytes():
    s = Session(routes({PAY: [Resp(exc=asyncio.TimeoutError()), Resp(502, b""), echo()]}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "sent"
    posts = s.posts()
    assert len(posts) == 3 and len({p["data"] for p in posts}) == 1
    assert len({p["headers"]["sign"] for p in posts}) == 1
    assert len(payouts.history()) == 1 and len(s.posts("/v1/payout/info")) == 2


def test_still_unknown_after_bounded_resends_stays_counted():
    s = Session(routes({PAY: Resp(exc=asyncio.TimeoutError())}))
    q = quote(s)
    res = run(payouts.send(s, entry("w1"), Decimal("25"), q))
    assert res["state"] == "unknown" and len(s.posts()) == 1 + payouts.MAX_RESEND
    assert len({p["data"] for p in s.posts()}) == 1 and len(payouts.history()) == 1
    assert payouts.used_today() == q["usdt"]
    assert "неясен" in B.payout_result_text(res)


@pytest.mark.parametrize("answer", [Resp(500, {"message": "Server error, #1", "code": 500, "error": None}),
                                    Resp(200, b""), Resp(200, {"state": 0, "result": {}}), Resp(504, b""),
                                    Resp(200, b"<html>"), Resp(307, b""), Resp(429, {"message": "Too many"})])
def test_ambiguous_answers_are_never_success_or_rejection(answer):
    """Пустое тело с 200 у PHP SDK — «успех»; у нас — неясный исход: не "sent" и не "rejected" (не вне лимита)."""
    s = Session(routes({PAY: answer, INFO: Resp(500, b"")}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "unknown" and len(s.posts()) == 1 and payouts.used_today() > 0


def test_error_on_resend_after_ambiguity_stays_unknown():
    s = Session(routes({PAY: [Resp(exc=asyncio.TimeoutError()), Resp(422, {"state": 1, "message": "Not enough funds"})]}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "unknown" and "Not enough funds" in res["reason"] and len(s.posts()) == 2
    assert payouts.used_today() > 0


def test_kill_switch_stops_resend(monkeypatch):
    def info(call):
        monkeypatch.setenv("PAYOUTS", "0")           # «⛔ Стоп» во время разбора неясного исхода
        return Resp(422, {"state": 1, "message": "Payout not found"})
    s = Session(routes({PAY: Resp(exc=asyncio.TimeoutError()), INFO: info}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "unknown" and len(s.posts()) == 1 and "повтор не отправлен" in res["reason"]


@pytest.mark.parametrize("over,field", [({"address": TRON2}, "адрес"), ({"amount": "26.00000000"}, "сумма"),
                                        ({"network": "BSC"}, "сеть"), ({"currency": "USDC"}, "монета")])
def test_response_mismatch_is_unknown_with_alert(over, field):
    s = Session(routes({PAY: echo(**over)}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "unknown" and res["event"] == "mismatch" and field in res["reason"]
    assert B.payout_result_text(res).startswith("🚨") and payouts.used_today() > 0


def test_ton_address_in_other_form_and_evm_case_match():
    s = Session(routes({PAY: echo(address=TON_UQ)}))
    assert run(payouts.send(s, entry("w3"), Decimal("1"), quote(s, "w3", "1")))["state"] == "sent"
    s = Session(routes({PAY: echo(address=EVM.upper().replace("0X", "0x"))}))
    assert run(payouts.send(s, entry("w4"), Decimal("0.01"), quote(s, "w4", "0.01")))["state"] == "sent"
    s = Session(routes({PAY: echo(address=TRON.lower())}))   # base58 — только побайтно
    assert run(payouts.send(s, entry("w1"), Decimal("1"), quote(s, "w1", "1")))["state"] == "unknown"


def test_classify_table():
    c = payouts._classify
    assert c(200, {"state": 0, "result": {"uuid": "x"}})[0] == "ok"
    assert c(201, {"state": 0, "result": {"uuid": "x"}})[0] == "ambiguous"
    assert c(200, {"state": 0, "result": {"status": "paid"}})[0] == "ambiguous"   # без uuid — не успех
    assert c(422, {"state": 1, "errors": {"amount": ["validation.required"]}}) == ("error", None, "validation.required")
    assert c(401, {"message": "Unauthorized"})[0] == "error"
    assert c(401, b"")[0] == c(401, None)[0] == "ambiguous"
    assert c(408, {"state": 1, "message": "timeout"})[0] == "ambiguous"


# --- опрос статуса и перезапуск ---

def _sent_row(eid="w1", state="sent"):
    s = Session(routes())
    res = run(payouts.send(s, entry(eid), Decimal("25"), quote(s, eid, "25")))
    return payouts._update(res["row"]["order_id"], state=state)


def _result_for(row, **over):
    code = payouts.CODES.get(row["currency"], row["currency"])
    res = {"uuid": "uuid-1", "amount": row["amount"], "currency": code, "network": row["network"].upper(),
           "address": row["address"], "txid": None, "status": "process", "is_final": False}
    res.update(over)
    return res


@pytest.mark.parametrize("status,final,state,event", [
    ("paid", True, "final_paid", "paid"), ("paid", False, "sent", None), ("fail", False, "sent", "stuck"),
    ("cancel", False, "sent", "stuck"), ("system_fail", "false", "sent", "stuck"),
    ("fail", True, "final_failed", "failed"), ("cancel", True, "final_failed", "failed"),
    ("system_fail", True, "final_failed", "failed"), ("process", True, "sent", None), ("check", False, "sent", None),
    ("paid", "true", "final_paid", "paid")])
def test_poll_finality_rules(status, final, state, event):
    row = _sent_row()
    s = Session(routes({INFO: info_for({row["order_id"]: _result_for(row, status=status, is_final=final,
                                                                        txid="tx1")})}))
    events = run(payouts.poll(s))
    assert payouts.get(row["order_id"])["state"] == state
    assert [e for e, _ in events] == ([event] if event else [])
    assert payouts.used_today() == (0 if state == "final_failed" else Decimal("26"))


def test_poll_unknown_found_and_mismatch_alert_once_and_not_found_unchanged():
    a, b, c = _sent_row(state="unknown"), _sent_row(state="sent"), _sent_row(state="unknown")
    s = Session(routes({INFO: info_for({a["order_id"]: _result_for(a), b["order_id"]: _result_for(b, address=TRON2)})}))
    events = run(payouts.poll(s))
    assert sorted(e for e, _ in events) == ["found", "mismatch", "notfound"]
    assert payouts.get(a["order_id"])["state"] == "sent" and payouts.get(b["order_id"])["state"] == "unknown"
    assert payouts.get(c["order_id"])["state"] == "unknown"                 # «не найдено» — в лимите, одно сообщение
    assert run(payouts.poll(s)) == []                                    # тревоги — по одному разу
    assert not s.posts()                                                # опрос ничего не отправляет


def test_restart_resumes_interrupted_and_pending_payouts():
    a = _sent_row(state="sending")
    b = _sent_row(state="prepared")
    c = _sent_row(state="sent")
    resumed = payouts.resume()
    assert {r["order_id"] for r in resumed} == {a["order_id"], b["order_id"]}
    assert all(r["state"] == "unknown" and "перезапустился" in r["note"] for r in resumed)
    s = Session(routes({INFO: info_for({a["order_id"]: _result_for(a),
                                          c["order_id"]: _result_for(c, status="paid", is_final=True)})}))
    events = dict((e, r["order_id"]) for e, r in run(payouts.poll(s)))
    assert events == {"found": a["order_id"], "paid": c["order_id"], "notfound": b["order_id"]}
    assert payouts.get(b["order_id"])["state"] == "unknown" and not s.posts()   # не отклонена: исход создания неизвестен


def test_poll_skips_old_pending_and_without_key(monkeypatch):
    row = _sent_row()
    payouts._update(row["order_id"], created_ts=payouts.time.time() - (payouts.POLL_DAYS + 1) * 86400)
    s = Session(routes())
    assert run(payouts.poll(s)) == [] and s.calls == []
    payouts._update(row["order_id"], created_ts=payouts.time.time())
    monkeypatch.delenv("CRYPTOMUS_PAYOUT_API_KEY")
    assert run(payouts.poll(s)) == [] and s.calls == []


# --- бот: /payout, кнопки, гости ---

def owner(session=None):
    bot = Stub(p2p.Config())
    bot.s = session or Session(routes())
    return bot


def cq(data, mid=77, chat=1):
    """Нажатие кнопки в личном чате chat его хозяином (from.id == chat.id)."""
    return {"id": "cb", "data": data, "from": {"id": chat},
            "message": {"message_id": mid, "chat": {"id": chat, "type": "private"}}}


def texts(bot):
    return [p["text"] for p in sent(bot)]


def answers(bot):
    return [p.get("text", "") for m, p in bot.out if m == "answerCallbackQuery"]


def to_preview(bot, amount="25", eid="w1"):
    run(bot.handle("/payout"))
    run(bot.on_callback(cq(f"pay_to:{eid}")))
    run(bot.handle(amount))
    return bot.payout_preview["token"]


def press(bot, data):
    """Кнопка владельца. Подтверждённая выплата уходит фоновой задачей — ждём её в том же цикле событий."""
    async def go():
        await bot.on_callback(cq(data))
        if bot.payout_task is not None and not bot.payout_task.done():
            await bot.payout_task
    run(go())


def test_payout_menu_amount_preview_and_send():
    bot = owner()
    run(bot.handle("/payout"))
    kb = sent(bot)[-1]["reply_markup"]["inline_keyboard"]
    datas = [b["callback_data"] for row in kb for b in row]
    assert datas == ["pay_to:w1", "pay_to:w2", "pay_to:w3", "pay_to:w4", "pay_hist", "pay_stop"]
    run(bot.on_callback(cq("pay_to:w3")))
    assert bot.awaiting_payout == "w3" and TON in texts(bot)[-1] and "123456" in texts(bot)[-1]
    run(bot.handle("0.123456789"))                     # > 8 знаков — ждём сумму дальше
    assert "8 знаков" in texts(bot)[-1] and bot.awaiting_payout == "w3"
    run(bot.handle("1.5"))
    preview = sent(bot)[-1]
    text = preview["text"]
    assert f"<code>{TON}</code>" in text and "<code>123456</code>" in text and "TON · ton" in text
    assert "Получит: <b>1.5 TON</b>" in text and "0.02 TON" in text and "1.52 TON ≈ 7.60 USDT" in text
    assert "использовано 0.00 из 2000.00 USDT" in text and "останется 1992.40" in text
    token = bot.payout_preview["token"]
    assert [[b["callback_data"] for b in row] for row in preview["reply_markup"]["inline_keyboard"]] == [
        [f"pay_ok:{token}", f"pay_no:{token}"], ["pay_stop"]]
    press(bot, f"pay_ok:{token}")
    assert ("editMessageReplyMarkup", {"chat_id": "1", "message_id": 77, "reply_markup": {"inline_keyboard": []}}) \
        in bot.out
    assert "Cryptomus принял выплату 1.5 TON (ton)" in texts(bot)[-1]
    assert len(bot.s.posts()) == 1 and json.loads(bot.s.posts()[0]["data"])["memo"] == "123456"


def test_preview_escapes_whitelist_name():
    bot = owner()
    to_preview(bot)
    assert "Мой Bybit &lt;TRC20&gt;" in texts(bot)[-1] and "<TRC20>" not in texts(bot)[-1]


def test_double_tap_and_stale_token_send_only_once():
    bot = owner()
    token = to_preview(bot)
    press(bot, f"pay_ok:{token}")
    press(bot, f"pay_ok:{token}")       # второй тап / повтор колбэка
    run(bot.on_callback(cq("pay_ok:forged")))
    assert len(bot.s.posts()) == 1 and len(payouts.history()) == 1
    assert answers(bot)[-2:] == ["Кнопка устарела или уже нажата, ничего не отправлено"] * 2


def test_expired_token_refused(monkeypatch):
    bot = owner()
    token = to_preview(bot)
    bot.payout_preview["ts"] -= payouts.TOKEN_TTL + 1
    press(bot, f"pay_ok:{token}")
    assert "истекло" in texts(bot)[-1] and not bot.s.posts() and bot.payout_preview is None
    press(bot, f"pay_ok:{token}")
    assert not bot.s.posts()


def test_cancel_then_confirm_sends_nothing():
    bot = owner()
    token = to_preview(bot)
    run(bot.on_callback(cq(f"pay_no:{token}")))
    press(bot, f"pay_ok:{token}")
    assert not bot.s.posts() and "отменена" in texts(bot)[-1]


def test_stop_button_sets_payouts_0_and_voids_preview(monkeypatch):
    saved = []
    monkeypatch.setattr(B, "save_env", lambda k, v: (saved.append((k, v)), os.environ.__setitem__(k, v)))
    bot = owner()
    token = to_preview(bot)
    run(bot.on_callback(cq("pay_stop")))
    assert saved == [("PAYOUTS", "0")] and not payouts.enabled() and bot.payout_preview is None
    assert "только на ПК" in texts(bot)[-1]
    press(bot, f"pay_ok:{token}")
    assert not bot.s.posts()
    run(bot.handle("/payout"))
    assert texts(bot)[-1] == B.PAYOUT_OFF_HINT
    run(bot.on_callback(cq("pay_to:w1")))
    assert answers(bot)[-1] == "Выплаты выключены" and bot.awaiting_payout is None


def test_kill_switch_flipped_outside_after_preview(monkeypatch):
    bot = owner()
    token = to_preview(bot)
    monkeypatch.setenv("PAYOUTS", "0")
    press(bot, f"pay_ok:{token}")
    assert not bot.s.posts() and "Ничего не отправлено" in texts(bot)[-1]


def test_other_input_cancels_amount_wait():
    bot = owner()
    run(bot.on_callback(cq("pay_to:w1")))
    run(bot.handle("/status"))
    run(bot.handle("25"))
    assert bot.payout_preview is None and not bot.s.calls


def test_payout_hints_without_key_off_and_empty_whitelist(monkeypatch):
    bot = owner()
    monkeypatch.delenv("CRYPTOMUS_PAYOUT_API_KEY")
    run(bot.handle("/payout"))
    hint = texts(bot)[-1]
    assert "Generate Payout key" in hint and "24 часа" in hint and "Merchant ID" in hint and "2FA" in hint
    assert "CRYPTOMUS_PAYOUT_API_SECRET" in hint and "не присылай" in hint
    monkeypatch.setenv("CRYPTOMUS_PAYOUT_API_KEY", MERCHANT)
    monkeypatch.setenv("PAYOUTS", "0")
    run(bot.handle("/payout"))
    assert "PAYOUTS=1" in texts(bot)[-1] and "только на ПК" in texts(bot)[-1]
    monkeypatch.setenv("PAYOUTS", "1")
    write_whitelist([])
    run(bot.handle("/payout"))
    assert "scripts/payout_whitelist.py" in texts(bot)[-1]
    assert not bot.s.calls


def test_payout_history_view():
    bot = owner()
    run(bot.handle("/payout history"))
    assert "не было" in texts(bot)[-1]
    token = to_preview(bot)
    press(bot, f"pay_ok:{token}")
    run(bot.on_callback(cq("pay_hist")))
    assert "25 USDT (tron)" in texts(bot)[-1] and "в обработке" in texts(bot)[-1] and "26.00 из 2000.00" in texts(bot)[-1]


def test_payout_in_command_menu():
    assert any(c["command"] == "payout" for c in B.COMMANDS)
    assert "/payout" not in B.GUEST_CMDS


def test_guests_cannot_use_payouts(monkeypatch):
    bot = Stub(p2p.Config(), guests=["42"])
    bot.s = Session(routes())
    run(bot.on_update(msg(42, "/payout")))
    run(bot.on_update(msg(42, "/payout history")))
    assert [p["text"] for p in sent(bot) if p["chat_id"] == "42"] == [B.GUEST_DENIED] * 2
    for data in ("pay_to:w1", "pay_ok:x", "pay_stop", "pay_hist"):
        run(bot.on_update({"callback_query": {"id": "g", "data": data, "message": {"chat": {"id": 42}, "message_id": 3}}}))
    assert answers(bot)[-4:] == ["Только для владельца бота"] * 4
    assert payouts.enabled() and not bot.s.calls and bot.awaiting_payout is None


def test_no_telegram_path_sets_payouts_1(monkeypatch):
    """В коде бота PAYOUTS пишется только как "0" (в .env — save_env, в процесс — только payouts.disable);
    перебор кнопок (и поддельных колбэков) и команд не включает выплаты."""
    tree = ast.parse(inspect.getsource(B))
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "save_env"
              and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "PAYOUTS"]
    assert writes and all(isinstance(n.args[1], ast.Constant) and n.args[1].value == "0" for n in writes)
    env_writes = 0
    for mod in (B, payouts):
        src = inspect.getsource(mod)
        for n in ast.walk(ast.parse(src)):
            for t in getattr(n, "targets", []):
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == "PAYOUTS":
                    assert isinstance(n.value, ast.Constant) and n.value.value == "0"
                    env_writes += 1
        assert "putenv" not in src and "environ.update" not in src
        assert not re.search(r"setdefault\(\s*[\"']PAYOUTS", src)
    assert env_writes == 1   # payouts.disable
    saved = []
    monkeypatch.setattr(B, "save_env", lambda k, v: saved.append((k, v)))
    monkeypatch.setenv("PAYOUTS", "0")
    bot = owner()
    for data in ("pay_on", "pay_set:1", "pay_to:w1", "pay_ok:x", "pay_hist", "pay_stop", "paper_set:on",
                 "quiet_on", "acc_add:cryptomus_payout", "preset_apply:PAYOUTS", "pay_stop:1",
                 "flt_a:BTC\nPAYOUTS=1", "flt_e:bybit\nPAYOUTS=1", "flt_a:\rPAYOUTS=1", "flt_e:PAYOUTS=1"):
        run(bot.on_callback(cq(data)))
    for text in ("/payout on", "/payout 1", "/payout enable", "/payout PAYOUTS=1", "/payout history", "PAYOUTS=1"):
        run(bot.handle(text))
    assert os.getenv("PAYOUTS") == "0" and all(v == "0" for k, v in saved if k == "PAYOUTS")
    assert all("PAYOUTS" not in v and "\n" not in v and "\r" not in v for k, v in saved)
    assert not bot.s.posts()


def test_payout_key_cannot_be_entered_via_telegram():
    bot = owner()
    run(bot.on_callback(cq("acc_add:cryptomus_payout")))
    assert bot.awaiting_key is None


def test_no_key_or_sign_in_logs_or_messages(caplog):
    """Cryptomus процитировал ключ и Merchant ID в отказе — в сообщения, лог и журнал они не попадают, подпись тоже."""
    caplog.set_level(logging.DEBUG)
    echoed = f"bad sign for {MERCHANT} with {KEY}"
    bot = owner(Session(routes({PAY: Resp(422, {"state": 1, "message": echoed}),
                                  INFO: Resp(422, {"state": 1, "message": echoed})})))
    token = to_preview(bot)
    press(bot, f"pay_ok:{token}")
    run(bot.handle("/payout history"))
    signs = {c["headers"]["sign"] for c in bot.s.calls if c["headers"]}
    dump = json.dumps(bot.out, ensure_ascii=False) + caplog.text + json.dumps(payouts.history(), ensure_ascii=False)
    assert signs and "•••" in dump
    for secret in (KEY, MERCHANT, *signs):
        assert secret not in dump


def test_payouts_loop_reports_resumed_and_final_to_journal(monkeypatch):
    row = _sent_row(state="sending")
    bot = owner(Session(routes({INFO: info_for({row["order_id"]: _result_for(row, status="paid", is_final=True,
                                                                                txid="T" * 64)})})))
    bot.topics = {"journal": 5}
    bot.resumed_payouts = payouts.resume()

    class Stop(Exception):
        pass

    async def stop(_):
        raise Stop

    monkeypatch.setattr(B.asyncio, "sleep", stop)
    with pytest.raises(Stop):
        run(bot.payouts_loop())
    journal = [p["text"] for p in sent(bot) if p.get("message_thread_id") == 5]
    assert "перезапустился во время отправки" in journal[0] and "выполнена" in journal[1] and "T" * 64 in journal[1]
    assert bot.resumed_payouts == [] and payouts.get(row["order_id"])["state"] == "final_paid"


# --- скрипт белого списка ---

def answers_from(*lines):
    it = iter(lines)
    return lambda prompt="": next(it)


def test_script_add_needs_matching_double_address_and_confirmation(tmp_path, capsys):
    main, path = payout_whitelist_script.main, tmp_path / "data" / "payout_whitelist.json"
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("usdt", "TRON", "Мой кошелёк", TRON, TRON2)) == 1
    assert not path.exists() and "не совпали" in capsys.readouterr().out
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("USDT", "tron", "Мой кошелёк", TRON, TRON, "n")) == 1
    assert not path.exists()
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("USDT", "tron", "Мой кошелёк", TRON, TRON, "y")) == 0
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("TON", "ton", "Биржа", TON, TON, "777", "да")) == 0
    got = payouts.load_whitelist(str(path))
    assert got == [{"id": "w1", "name": "Мой кошелёк", "currency": "USDT", "network": "tron", "address": TRON, "memo": ""},
                   {"id": "w2", "name": "Биржа", "currency": "TON", "network": "ton", "address": TON, "memo": "777"}]
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("USDT", "tron", "Ещё раз", TRON, TRON, "y")) == 1
    assert "уже есть" in capsys.readouterr().out
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("USDT", "tron", "Опечатка", TRON[:-1] + "u")) == 1
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("DOGE")) == 1
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("BTC", "tron")) == 1
    assert len(payouts.load_whitelist(str(path))) == 2


def test_script_list_and_remove(tmp_path, capsys):
    main, path = payout_whitelist_script.main, tmp_path / "data" / "payout_whitelist.json"
    write_whitelist(ENTRIES + [{"id": "bad", "name": "x", "currency": "USDT", "network": "tron", "address": "T1"}],
                    str(path))
    assert main(["list", "--bot-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert TRON in out and "бот пропустит" in out
    assert main(["remove", "--bot-dir", str(tmp_path)], answers_from("w9")) == 1
    assert main(["remove", "--bot-dir", str(tmp_path)], answers_from("w2", "n")) == 1
    assert main(["remove", "--bot-dir", str(tmp_path)], answers_from("w2", "y")) == 0
    assert [e["id"] for e in payouts.load_whitelist(str(path))] == ["w1", "w3", "w4"]
    assert main(["list", "--bot-dir", str(tmp_path / "нет")]) == 1


def test_script_never_touches_keys():
    src = inspect.getsource(payout_whitelist_script)
    assert "accounts" not in src and "keys.json" not in src and "credentials" not in src and ".env" not in src


def test_script_evm_without_checksum_needs_explicit_yes(tmp_path, capsys):
    main, path = payout_whitelist_script.main, tmp_path / "data" / "payout_whitelist.json"
    chk = EIP55_OK[4]
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("USDT", "bsc", "Опечатка", chk[:-1] + "D")) == 1
    assert "EIP-55" in capsys.readouterr().out and not path.exists()
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("ETH", "bsc", "Без суммы", EVM, "n")) == 1
    assert "без контрольной суммы" in capsys.readouterr().out and not path.exists()
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("ETH", "bsc", "Без суммы", EVM, "y", EVM, "y")) == 0
    assert main(["add", "--bot-dir", str(tmp_path)], answers_from("USDT", "bsc", "С суммой", chk, chk, "y")) == 0
    assert [e["address"] for e in payouts.load_whitelist(str(path))] == [EVM, chk]


# --- исправления по ревью 2026-09-26 ---

def _load_guard():
    spec = importlib.util.spec_from_file_location(
        "guard_script", os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guard_blocks_automerge_of_any_payout_change(monkeypatch):
    """Код выплат не уходит в автомерж: payouts.py, скрипт белого списка и эти тесты — защищённые файлы, а любая
    добавленная или удалённая строка выплат в других файлах (bot.py…) — ручная проверка."""
    guard = _load_guard()
    for f in ("payouts.py", "scripts/payout_whitelist.py", "tests/test_payouts.py"):
        assert f.startswith(guard.PROTECTED), f
    hunk = "@@ -1 +1 @@\n"
    diffs = {
        "payouts.py": '+++ b/payouts.py\n' + hunk + '+    ("POST", "/v1/transfer/to-personal"),\n',
        "bot.py": "--- a/bot.py\n+++ b/bot.py\n" + hunk + "-        if not payouts.enabled():\n",
        ".env.example": "--- a/.env.example\n+++ b/.env.example\n" + hunk + "+PAYOUT_DAILY_LIMIT=1000000\n",
        "accounts.py": '--- a/accounts.py\n+++ b/accounts.py\n' + hunk + '+KEYS = ("cryptomus_payout",)\n',
        "tests/test_guests.py": ('--- a/tests/test_guests.py\n+++ b/tests/test_guests.py\n' + hunk
                                 + '+    run(bot.on_callback(cq("pay_ok:x")))\n'),
    }

    def fake_git(per_file):
        """Как git в guard.check: список файлов через -z, потом diff по одному файлу (путь — последний аргумент)."""
        return lambda *args: ("\0".join(per_file) + "\0" if "--name-only" in args else per_file.get(args[-1], ""))
    for name, diff in diffs.items():
        monkeypatch.setattr(guard, "git", fake_git({name: diff}))
        assert guard.check("origin/main"), name
    harmless = {"bot.py": "--- a/bot.py\n+++ b/bot.py\n" + hunk + "+    x = trades.pay_label(kind, bank, pays)\n",
                "ROADMAP.md": "--- a/ROADMAP.md\n+++ b/ROADMAP.md\n" + hunk + "+- payouts: заметка\n"}
    monkeypatch.setattr(guard, "git", fake_git(harmless))
    assert guard.check("origin/main") == []
    claude_md = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "CLAUDE.md"), encoding="utf-8").read()
    assert "`payouts.py`, `scripts/payout_whitelist.py`, `tests/test_payouts.py`" in claude_md


def test_stop_is_fail_safe_when_env_cannot_be_written(monkeypatch):
    def locked(k, v):
        raise PermissionError(".env read-only")
    monkeypatch.setattr(B, "save_env", locked)
    bot = owner()
    token = to_preview(bot)
    press(bot, "pay_stop")
    assert not payouts.enabled() and bot.payout_preview is None and bot.awaiting_payout is None
    assert answers(bot)[-1] == "Выплаты выключены"
    assert "до перезапуска" in texts(bot)[-1] and "PermissionError" in texts(bot)[-1] and "вручную" in texts(bot)[-1]
    press(bot, f"pay_ok:{token}")
    assert not bot.s.posts() and payouts.history() == []


def test_forged_filter_callbacks_cannot_inject_env_lines():
    path = B.save_env.__defaults__[0]      # .env во временной папке (tests/conftest.py)
    with open(path, "w", encoding="utf-8") as f:
        f.write("ASSETS=USDT\nPAYOUTS=0\n")
    bot = owner()
    bot.cfg.assets, bot.cfg.exchanges = ["USDT"], ["bybit"]
    for data in ("flt_a:BTC\nPAYOUTS=1", "flt_e:mexc\nPAYOUTS=1", "flt_a:DOGE", "flt_e:evil"):
        run(bot.on_callback(cq(data)))
    assert open(path, encoding="utf-8").read() == "ASSETS=USDT\nPAYOUTS=0\n"
    assert bot.cfg.assets == ["USDT"] and bot.cfg.exchanges == ["bybit"]
    run(bot.on_callback(cq("flt_a:BTC")))                          # настоящая кнопка работает
    assert open(path, encoding="utf-8").read() == "ASSETS=USDT,BTC\nPAYOUTS=0\n"
    for key, value in (("ASSETS", "USDT\nPAYOUTS=1"), ("ASSETS", "USDT\rPAYOUTS=1"), ("PAYOUTS=1\nX", "1"), ("", "1")):
        with pytest.raises(ValueError):
            B.save_env(key, value, path)
    assert "PAYOUTS=1" not in open(path, encoding="utf-8").read()


def test_kill_switch_survives_restart_case_and_inherited_env(monkeypatch, tmp_path):
    path = str(tmp_path / ".env")

    def env(text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    env("payouts=1\nX=1\nPayouts=1\n")
    B.save_env("PAYOUTS", "0", path)                                # «⛔ Стоп»: все строки ключа — одной
    assert open(path, encoding="utf-8").read() == "PAYOUTS=0\nX=1\n"
    for text, inherited, on in (("PAYOUTS=0\n", "1", False),        # PAYOUTS=1 из окружения Windows .env не перебьёт
                                ("payouts=1\nPAYOUTS=0\n", None, False), ("X=1\n", "1", False),
                                ("PAYOUTS=1\n", None, True), ("PAYOUTS=1\nPAYOUTS=1 # да\n", None, True),
                                ("PAYOUTS=1\n", "0", False)):
        env(text)
        monkeypatch.delenv("PAYOUTS", raising=False)
        if inherited is not None:
            monkeypatch.setenv("PAYOUTS", inherited)
        p2p.load_env(path)
        payouts.switch_from_file(path)
        assert payouts.enabled() is on, (text, inherited)
    monkeypatch.setenv("PAYOUTS", "1")
    payouts.switch_from_file(str(tmp_path / "нет.env"))
    assert not payouts.enabled()
    src = inspect.getsource(B.main)
    assert src.index("payouts.switch_from_file(ENV_PATH)") > src.index("load_env()")


class Gated(Resp):
    """Запрос «висит», пока тест не откроет ворота, потом таймаут: выплата уже ушла, а ответа нет."""
    def __init__(self, gate):
        super().__init__()
        self.gate = gate

    async def __aenter__(self):
        await self.gate.wait()
        raise asyncio.TimeoutError()


def test_stop_during_inflight_send_prevents_resends():
    """Отправка идёт фоном: command_loop принимает «⛔ Стоп», пока первый POST висит, — повторов нет."""
    s = Session(routes({INFO: info_for({})}))
    bot = owner(s)
    token = to_preview(bot)

    def update(data):
        return {"update_id": 1, "callback_query": cq(data, mid=5)}

    async def go():
        gate = asyncio.Event()
        s.routes[PAY] = lambda call: Gated(gate)
        # как command_loop: обработка «✅ Отправить» не ждёт ответа Cryptomus (иначе — зависание, тест падает)
        await asyncio.wait_for(bot.on_update(update(f"pay_ok:{token}")), 5)
        await until(lambda: s.posts(), "первый POST висит")
        assert len(s.posts()) == 1 and not bot.payout_task.done()
        await bot.on_update(update("pay_stop"))
        gate.set()
        await bot.payout_task
    run(go())
    assert len(s.posts()) == 1                                     # без Стопа было бы 1 + MAX_RESEND
    row = payouts.history()[0]
    assert row["state"] == "unknown" and "повтор не отправлен" in row["note"]
    assert "неясен" in texts(bot)[-1] and not payouts.enabled()


def test_background_send_failure_is_reported(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db locked")
    bot = owner()
    token = to_preview(bot)
    monkeypatch.setattr(payouts, "send", boom)
    press(bot, f"pay_ok:{token}")
    assert "исход неясен" in texts(bot)[-1] and "/payout history" in texts(bot)[-1]


def test_keccak256_and_eip55():
    assert payouts._keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    for m in (b"", b"abc", b"x" * 135, b"x" * 136, b"x" * 300):   # тот же код с паддингом SHA3 = hashlib.sha3_256
        assert payouts._keccak256(m, 0x06) == hashlib.sha3_256(m).digest()
    assert all(payouts._eip55_ok(a) for a in EIP55_OK)
    assert payouts.evm_checksummed(EIP55_OK[4]) and not payouts.evm_checksummed(EVM)


def test_fail_not_final_alerts_once_and_shows_in_history():
    row = _sent_row()
    s = Session(routes({INFO: info_for({row["order_id"]: _result_for(row, status="fail", is_final=False)})}))
    events = run(payouts.poll(s))
    assert [e for e, _ in events] == ["stuck"] and run(payouts.poll(s)) == []
    text = B.payout_event_text(*events[0])
    assert "поддержку Cryptomus" in text and "fail" in text and "учтена" in text
    view = B.payout_history_view(payouts.history(), payouts.used_today(), Decimal(2000))
    assert "статус fail" in view and "поддержку" in view and payouts.used_today() == Decimal("26")
    s.routes[INFO] = info_for({row["order_id"]: _result_for(row, status="fail", is_final=True)})
    assert [e for e, _ in run(payouts.poll(s))] == ["failed"] and payouts.used_today() == 0
    s2 = Session(routes({PAY: echo(status="fail", final=False)}))  # fail без итога прямо в ответе на создание
    res = run(payouts.send(s2, entry("w1"), Decimal("25"), quote(s2)))
    assert res["state"] == "sent" and res["event"] == "stuck"
    assert B.payout_result_text(res).startswith("⚠️") and "поддержку" in B.payout_result_text(res)


def test_mismatch_reports_final_status_once():
    s = Session(routes({PAY: echo(amount="26.00000000")}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    text = B.payout_result_text(res)
    assert res["state"] == "unknown" and "Проверь её в кабинете" in text and "Итоговый статус" in text
    row = res["row"]
    s.routes[INFO] = info_for({row["order_id"]: _result_for(row, amount="26", status="paid", is_final=True)})
    events = run(payouts.poll(s))
    assert [e for e, _ in events] == ["mismatch_final"] and run(payouts.poll(s)) == []
    assert "итоговый статус Cryptomus — paid" in B.payout_event_text(*events[0])
    assert payouts.get(row["order_id"])["state"] == "unknown" and payouts.used_today() == Decimal("26")
    s2 = Session(routes({PAY: echo(amount="26", status="paid", final=True)}))   # несовпадение сразу с итогом
    res = run(payouts.send(s2, entry("w1"), Decimal("25"), quote(s2)))
    assert res["event"] == "mismatch" and "больше ничего" in B.payout_result_text(res)


def test_rejection_with_failed_info_is_resolved_by_poll():
    s = Session(routes({PAY: Resp(422, {"state": 1, "message": "Not enough funds"}), INFO: Resp(502, b"")}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    assert res["state"] == "unknown" and payouts.used_today() == Decimal("26")
    s.routes[INFO] = info_for({})
    events = run(payouts.poll(s))
    assert [e for e, _ in events] == ["rejected"] and run(payouts.poll(s)) == []
    assert payouts.get(res["row"]["order_id"])["state"] == "rejected" and payouts.used_today() == 0
    assert "Деньги не ушли" in B.payout_event_text(*events[0]) and "Not enough funds" in B.payout_event_text(*events[0])
    assert len(s.posts()) == 1                                      # опрос не отправляет


def test_ambiguous_then_not_found_stays_counted_with_one_alert():
    s = Session(routes({PAY: Resp(exc=asyncio.TimeoutError()), INFO: Resp(502, b"")}))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))
    s.routes[INFO] = info_for({})
    assert [e for e, _ in run(payouts.poll(s))] == ["notfound"] and run(payouts.poll(s)) == []
    row = payouts.get(res["row"]["order_id"])
    assert row["state"] == "unknown" and payouts.used_today() == Decimal("26") and len(s.posts()) == 1
    assert "не находит" in B.payout_event_text("notfound", row) and "кабинет" in B.payout_event_text("notfound", row)


@pytest.mark.parametrize("status,refund", [("fail", True), ("cancel", False), ("system_fail", False)])
def test_failed_text_promises_refund_only_for_fail(status, refund):
    row = dict(_sent_row(), status=status)
    text = B.payout_event_text("failed", row)
    assert ("возвращаются на баланс" in text) is refund and ("не обещает" in text) is not refund
