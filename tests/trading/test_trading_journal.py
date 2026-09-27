"""Торговое ядро, journal: намерение до запроса, идемпотентность по клиентскому id, неясный исход и ограниченный повтор,
несовпадение → unknown, выключатель посреди отправки, resume/reconcile под одним замком, хаос на каждом await."""
import asyncio
import json
import time
from decimal import Decimal

import aiohttp
import pytest

from trading import journal, switch, venues
from trading_stubs import (CREDS, Book, Gated, Resp, Session, bingx_err, bingx_ok, body, business, bybit_err,
                           bybit_ok, run)

CREATE_BY, CREATE_BX = ("POST", "/v5/order/create"), ("POST", "/openApi/swap/v2/trade/order")
RT, HIST, QBX = ("GET", "/v5/order/realtime"), ("GET", "/v5/order/history"), ("GET", "/openApi/swap/v2/trade/order")


def BY(**kw):
    return venues.Order("bybit", "linear", "BTCUSDT", kw.pop("side", "sell"), "market", kw.pop("qty", "0.001"), **kw)


def BX(**kw):
    return venues.Order("bingx", "swap", "ETHUSDT", kw.pop("side", "sell"), "market", kw.pop("qty", "0.01"), **kw)


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(venues, "_RESOLVED", {})
    monkeypatch.setattr(journal, "DB_PATH", str(tmp_path / "trading.db"))
    monkeypatch.setattr(journal, "RETRY_DELAY", 0)
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "minlot")


def bybit_routes(book, **over):
    r = {CREATE_BY: book.bybit_create(), RT: book.bybit_query, HIST: bybit_ok({"list": []})}
    r.update(over)
    return r


def bingx_routes(book, **over):
    r = {CREATE_BX: book.bingx_create(), QBX: book.bingx_query}
    r.update(over)
    return r


def submit(s, order, **kw):
    return run(journal.submit(s, order, CREDS, **kw))


def creates(s):
    return s.sent(*CREATE_BY) + s.sent(*CREATE_BX)


# --- намерение и успех ---

def test_intent_persisted_before_request_with_exact_params():
    book, seen = Book(), []

    def create(call):
        cid = body(call)["orderLinkId"]
        seen.append(journal.get(cid))                     # намерение уже в журнале до ответа биржи
        return book.bybit_create()(call)

    s = Session(bybit_routes(book, **{"_": None}) | {CREATE_BY: create})
    res = submit(s, BY(stop_loss="70000"), strategy="directional", mode="minlot", notional=Decimal("65"))
    row = res["row"]
    assert res["state"] == "open" and res["event"] is None and row["venue_order_id"] == book.orders[row["client_id"]][
        "orderId"]
    assert seen[0]["state"] == "sending" and json.loads(seen[0]["params"]) == body(creates(s)[0])
    assert row["strategy"] == "directional" and row["mode"] == "minlot" and row["notional"] == "65"
    assert len(creates(s)) == 1 and not s.sent(*RT)
    assert journal.blocking() == []


@pytest.mark.parametrize("status,state,event", [("NEW", "open", None), ("FILLED", "filled", "filled"),
                                                ("PARTIALLY_FILLED", "open", None)])
def test_bingx_success_state_from_response(status, state, event):
    book = Book()
    s = Session(bingx_routes(book, **{"_": None}) | {CREATE_BX: book.bingx_create(status=status)})
    res = submit(s, BX())
    assert res["state"] == state and res["event"] == event and res["row"]["venue_order_id"]
    q = business(creates(s)[0])
    assert q["clientOrderId"] == res["row"]["client_id"] and q["quantity"] == "0.01"


def test_client_id_format_fits_both_venues():
    cid = journal.new_client_id()
    assert venues.CLIENT_ID_RE.fullmatch(cid) and len(cid) <= 36 and cid == cid.lower()
    assert len({journal.new_client_id() for _ in range(200)}) == 200


# --- неясный исход, повтор, отказ ---

def test_timeout_after_venue_accepted_then_found_no_resend():
    book = Book()

    def create(call):
        book.bybit_create()(call)                           # биржа ордер приняла, ответ потерялся
        return Resp(exc=asyncio.TimeoutError())

    s = Session(bybit_routes(book, **{"_": None}) | {CREATE_BY: create})
    res = submit(s, BY())
    assert res["state"] == "open" and res["event"] == "found" and len(creates(s)) == 1


def test_timeout_then_not_found_resends_same_id_and_same_body():
    book = Book()
    s = Session(bybit_routes(book, **{"_": None}) | {CREATE_BY: [Resp(exc=asyncio.TimeoutError()), Resp(502, b""),
                                                                  book.bybit_create()]})
    res = submit(s, BY())
    assert res["state"] == "open"
    posts = creates(s)
    assert len(posts) == 3 and len({c["data"] for c in posts}) == 1          # те же байты тела
    assert len({c["headers"]["X-BAPI-API-KEY"] for c in posts}) == 1
    assert len(journal.history()) == 1 and journal.get(res["row"]["client_id"])["posts"] == 3


def test_bingx_resend_same_business_params_fresh_timestamp():
    book = Book()
    s = Session(bingx_routes(book, **{"_": None}) | {CREATE_BX: [Resp(exc=aiohttp.ServerDisconnectedError()),
                                                                  book.bingx_create()]})
    res = submit(s, BX(stop_loss="3500"))
    posts = creates(s)
    assert res["state"] == "open" and len(posts) == 2
    assert business(posts[0]) == business(posts[1]) and business(posts[0])["stopLoss"].startswith('{"type"')


def test_still_unknown_after_bounded_resends_blocks_opens():
    s = Session(bybit_routes(Book(), **{"_": None}) | {CREATE_BY: Resp(exc=asyncio.TimeoutError())})
    res = submit(s, BY())
    assert res["state"] == "unknown" and res["event"] == "unknown"
    assert len(creates(s)) == 1 + journal.MAX_RESEND and len({c["data"] for c in creates(s)}) == 1
    assert [r["client_id"] for r in journal.blocking()] == [res["row"]["client_id"]]


def test_definite_rejection_confirmed_by_query_is_rejected_not_blocking():
    s = Session(bybit_routes(Book(), **{"_": None}) | {CREATE_BY: bybit_err(110007, "Available balance is insufficient")})
    res = submit(s, BY())
    assert res["state"] == "rejected" and "110007" in res["reason"]
    assert len(creates(s)) == 1 and len(s.sent(*RT)) == 2 and len(s.sent(*HIST)) == 1   # сверено по id
    assert journal.blocking() == []


def test_rejection_but_order_exists_is_adopted():
    book = Book()

    def create(call):
        book.bybit_create()(call)
        return bybit_err(10001, "params error")

    s = Session(bybit_routes(book, **{"_": None}) | {CREATE_BY: create})
    res = submit(s, BY())
    assert res["state"] == "open" and res["event"] == "found"


def test_duplicate_id_found_adopted_not_found_unknown():
    book = Book()
    s = Session(bybit_routes(book, **{"_": None}) | {CREATE_BY: lambda c: (book.bybit_create()(c), bybit_err(110072))[1]})
    assert submit(s, BY())["state"] == "open"
    s = Session(bybit_routes(Book(), **{"_": None}) | {CREATE_BY: bybit_err(110072)})
    res = submit(s, BY())
    assert res["state"] == "unknown" and len(creates(s)) == 1


@pytest.mark.parametrize("answer", [Resp(500, {"retCode": 10016}), Resp(200, b""), Resp(200, b"<html>"),
                                    Resp(307, b""), Resp(403, b"forbidden"), Resp(429, b""), bybit_err(10016),
                                    bybit_err(999999), Resp(exc=aiohttp.ClientConnectionError()),
                                    Resp(200, {"retCode": 0, "result": {"orderId": "1", "orderLinkId": "t0"}})])
def test_ambiguous_or_mismatched_never_success_or_rejection(answer):
    """Неясный ответ, а статус по id не узнать (сверка сама падает) — unknown, в блоке открытий; не open и не
    rejected. Ответ 0 с чужим orderLinkId — тоже unknown (несовпадение)."""
    s = Session({CREATE_BY: answer, RT: Resp(500, b""), HIST: Resp(500, b"")})
    res = submit(s, BY())
    assert res["state"] == "unknown" and journal.blocking()
    assert len(creates(s)) == 1


def test_error_on_resend_after_ambiguity_stays_unknown():
    s = Session(bybit_routes(Book(), **{"_": None}) | {CREATE_BY: [Resp(exc=asyncio.TimeoutError()),
                                                                  bybit_err(110007)]})
    res = submit(s, BY())
    assert res["state"] == "unknown" and len(creates(s)) == 2 and journal.blocking()


@pytest.mark.parametrize("venue,over", [("bybit", {"qty": "0.002"}), ("bybit", {"symbol": "ETHUSDT"}),
                                        ("bybit", {"side": "Buy"}), ("bingx", {"origQty": "0.02"}),
                                        ("bingx", {"side": "BUY"}), ("bingx", {"clientOrderId": "tother"}),
                                        ("bingx", {"status": "WEIRD"})])
def test_mismatch_is_unknown_with_alert(venue, over):
    book = Book()
    if venue == "bybit":
        def create(call):
            book.bybit_create(**over)(call)
            return Resp(exc=asyncio.TimeoutError())       # статус узнаём запросом — там и несовпадение
        s = Session(bybit_routes(book, **{"_": None}) | {CREATE_BY: create})
        res = submit(s, BY())
    else:
        s = Session(bingx_routes(book, **{"_": None}) | {CREATE_BX: book.bingx_create(**over)})
        res = submit(s, BX())
    assert res["state"] == "unknown" and res["event"] == "mismatch" and journal.blocking()


# --- выключатель, precheck, ключ, назначение ---

def test_trading_off_or_paper_refuses_open_without_any_request(monkeypatch):
    s = Session(bybit_routes(Book()))
    monkeypatch.setenv("TRADING", "0")
    assert submit(s, BY())["reason"] == switch.OFF
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert submit(s, BY())["state"] == "refused" and s.calls == [] and journal.history() == []


def test_close_works_when_trading_off(monkeypatch):
    monkeypatch.setenv("TRADING", "0")
    book = Book()
    s = Session(bybit_routes(book, **{"_": None}))
    res = submit(s, BY(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "open" and body(creates(s)[0])["reduceOnly"] is True


def test_stop_during_ambiguity_blocks_resend_of_open(monkeypatch):
    def query(call):
        switch.stop()                                     # «⛔ Стоп» во время разбора неясного исхода
        return bybit_ok({"list": []})
    s = Session({CREATE_BY: Resp(exc=asyncio.TimeoutError()), RT: query, HIST: bybit_ok({"list": []})})
    res = submit(s, BY())
    assert res["state"] == "unknown" and len(creates(s)) == 1 and "повтор не отправлен" in res["reason"]


def test_stop_does_not_block_resend_of_close(monkeypatch):
    book = Book()

    def query(call):
        switch.stop()
        return book.bybit_query(call)
    s = Session({CREATE_BY: [Resp(exc=asyncio.TimeoutError()), book.bybit_create()], RT: query,
                 HIST: bybit_ok({"list": []})})
    res = submit(s, BY(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "open" and len(creates(s)) == 2


def test_stop_while_first_request_in_flight_sends_nothing_more():
    """Стоп, пока первый запрос летит: он уже ушёл (его не отозвать), но повторов нет."""
    s = Session({RT: bybit_ok({"list": []}), HIST: bybit_ok({"list": []})})

    async def go():
        gate = asyncio.Event()
        s.routes[CREATE_BY] = lambda call: Gated(gate, Resp(exc=asyncio.TimeoutError()))
        task = asyncio.ensure_future(journal.submit(s, BY(), CREDS))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(creates(s)) == 1 and not task.done()
        switch.stop()
        gate.set()
        return await task
    res = run(go())
    assert res["state"] == "unknown" and len(creates(s)) == 1


def test_precheck_runs_under_lock_and_refuses_before_insert():
    s = Session(bybit_routes(Book()))
    res = submit(s, BY(), precheck=lambda: "дневной стоп")
    assert res == {"state": "refused", "row": None, "reason": "дневной стоп", "event": None}
    assert s.calls == [] and journal.history() == []


def test_no_key_or_invalid_order_refused_before_insert():
    s = Session(bybit_routes(Book()))
    assert run(journal.submit(s, BY(), None))["state"] == "refused"
    bad = venues.Order("bybit", "linear", "DOGEUSDT", "sell", "market", "1")
    res = run(journal.submit(s, bad, CREDS))
    assert res["state"] == "refused" and "DOGEUSDT" in res["reason"]
    assert s.calls == [] and journal.history() == []


def test_purpose_must_match_direction():
    s = Session()
    with pytest.raises(ValueError):
        submit(s, BY(reduce_only=True))                                   # открытие уменьшающим ордером
    with pytest.raises(ValueError):
        submit(s, BY(), purpose="close")                                  # закрытие не уменьшающим
    with pytest.raises(ValueError):
        submit(s, BY(), purpose="withdraw")
    assert s.calls == []


def test_one_operation_at_a_time():
    """Два submit одновременно: второй не отправляет, пока первый не закончил (общий замок)."""
    book = Book()
    s = Session(bybit_routes(book, **{"_": None}))

    async def go():
        gate = asyncio.Event()
        first = book.bybit_create()
        s.routes[CREATE_BY] = [lambda call: Gated(gate, first(call)), book.bybit_create()]
        t1 = asyncio.ensure_future(journal.submit(s, BY(), CREDS))
        t2 = asyncio.ensure_future(journal.submit(s, BY(qty="0.002"), CREDS))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(creates(s)) == 1
        gate.set()
        return await t1, await t2
    r1, r2 = run(go())
    assert r1["state"] == r2["state"] == "open" and len(creates(s)) == 2


# --- журнал: переходы, resume, счётчики ---

def _row(state="open", venue="bybit", created=None):
    row = journal._insert_intent(BY() if venue == "bybit" else BX(), "open", "hedge", "minlot", None)
    if state != "prepared":
        journal._update(row["client_id"], state="sending")
    if state not in ("prepared", "sending"):
        journal._update(row["client_id"], state=state if state != "filled" else "open")
        if state == "filled":
            journal._update(row["client_id"], state="filled")
    if created is not None:
        journal._update(row["client_id"], created_ts=created)
    return journal.get(row["client_id"])


@pytest.mark.parametrize("src,dst", [("prepared", "open"), ("closed", "open"), ("rejected", "open"),
                                     ("filled", "open"), ("closed", "unknown"), ("rejected", "unknown")])
def test_forbidden_transitions(src, dst):
    row = _row("open")
    if src in ("closed", "rejected"):
        journal._update(row["client_id"], state=src)
    elif src == "filled":
        journal._update(row["client_id"], state="filled")
    else:
        row = _row("prepared")
    with pytest.raises(ValueError):
        journal._update(row["client_id"], state=dst)


def test_resume_marks_interrupted_unknown_and_resolve_by_owner():
    a, b, c = _row("sending"), _row("prepared"), _row("open")
    resumed = journal.resume()
    assert {r["client_id"] for r in resumed} == {a["client_id"], b["client_id"]}
    assert all(r["state"] == "unknown" and "перезапустился" in r["note"] for r in resumed)
    assert {r["client_id"] for r in journal.blocking()} == {a["client_id"], b["client_id"]}
    journal.resolve(a["client_id"], "closed", "проверил в кабинете")
    with pytest.raises(ValueError):
        journal.resolve(c["client_id"], "closed", "не unknown")
    assert journal.get(a["client_id"])["note"].startswith("владелец:")
    f = _row("filled")
    assert journal.close_out(f["client_id"])["state"] == "closed"


def test_order_counts_and_daily_pnl_msk():
    now = journal.day_start(time.time()) + 5 * 3600            # 05:00 МСК
    for created in (now - 10, now - 30, now - 120, journal.day_start(now) - 1):
        _row("open", created=created)
    assert journal.order_counts(now) == (2, 3)
    assert journal.add_pnl("bybit", "BTCUSDT", "-3.5", "trade", "exec-1", ts=now - 60)
    assert not journal.add_pnl("bybit", "BTCUSDT", "-3.5", "trade", "exec-1", ts=now - 60)   # повтор не дублируется
    journal.add_pnl("bingx", "ETHUSDT", Decimal("1.25"), "funding", "f-1", ts=now - 60)
    journal.add_pnl("bingx", "ETHUSDT", "-100", "trade", "old", ts=journal.day_start(now) - 1)   # вчера по МСК
    assert journal.pnl_today(now) == Decimal("-2.25")
    with pytest.raises(ValueError):
        journal.add_pnl("bybit", "BTCUSDT", "NaN", "trade", "x")


def test_reading_does_not_create_database(tmp_path):
    assert journal.history() == [] and journal.blocking() == [] and journal.order_counts() == (0, 0)
    assert not (tmp_path / "trading.db").exists()


# --- reconcile ---

def _creds_for(venue):
    return CREDS


def test_reconcile_updates_states_and_alerts_once():
    book = Book()
    filled, found, gone_err, gone_amb, vanished, mism = (_row("open"), _row("unknown"), _row("unknown"),
                                                         _row("unknown"), _row("open"), _row("open"))
    journal._update(gone_err["client_id"], create_kind="error")
    journal._update(gone_amb["client_id"], create_kind="ambiguous")
    book.bybit_store(filled["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="0.001",
                     orderStatus="Filled", cumExecQty="0.001", avgPrice="65000")
    book.bybit_store(found["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="0.001",
                     orderStatus="New", cumExecQty="0")
    book.bybit_store(mism["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="5",
                     orderStatus="New", cumExecQty="0")
    s = Session({RT: book.bybit_query, HIST: bybit_ok({"list": []})})
    events = run(journal.reconcile(s, _creds_for))
    got = {row["client_id"]: ev for ev, row in events}
    assert got == {filled["client_id"]: "filled", found["client_id"]: "found", gone_err["client_id"]: "rejected",
                   gone_amb["client_id"]: "notfound", vanished["client_id"]: "notfound", mism["client_id"]: "mismatch"}
    st = {r: journal.get(r)["state"] for r in got}
    assert st == {filled["client_id"]: "filled", found["client_id"]: "open", gone_err["client_id"]: "rejected",
                  gone_amb["client_id"]: "unknown", vanished["client_id"]: "unknown", mism["client_id"]: "unknown"}
    assert journal.get(filled["client_id"])["avg_price"] == "65000"
    assert run(journal.reconcile(s, _creds_for)) == []                       # тревоги — по одному разу
    assert all(c["method"] == "GET" for c in s.calls)                        # сверка ничего не отправляет


def test_reconcile_query_failure_changes_nothing_and_orphans_become_unknown():
    a, b = _row("open"), _row("sending")
    s = Session({RT: Resp(exc=asyncio.TimeoutError()), HIST: Resp(500, b"")})
    events = run(journal.reconcile(s, _creds_for))
    assert events == [("unknown", journal.get(b["client_id"]))]
    assert journal.get(a["client_id"])["state"] == "open" and journal.get(b["client_id"])["state"] == "unknown"


def test_reconcile_skips_old_and_keyless_but_they_still_block():
    old = _row("unknown", created=time.time() - (journal.RECONCILE_DAYS + 1) * 86400)
    bx = _row("unknown", venue="bingx")
    s = Session({})
    assert run(journal.reconcile(s, lambda v: CREDS if v == "bybit" else None)) == [] and s.calls == []
    assert {r["client_id"] for r in journal.blocking()} == {old["client_id"], bx["client_id"]}


def test_cancel_only_orders_from_our_journal():
    s = Session({("POST", "/v5/order/cancel"): bybit_ok({"orderId": "1", "orderLinkId": "x"}),
                 ("DELETE", "/openApi/swap/v2/trade/order"): bingx_err(109421)})
    by, bx = _row("open"), _row("open", venue="bingx")
    assert run(journal.cancel(s, by["client_id"], CREDS))[0] == "ok"
    assert run(journal.cancel(s, bx["client_id"], CREDS))[0] == "notfound"
    for foreign in ("manual-123", journal.new_client_id()):     # чужой / не из журнала — ничего не отправляем
        with pytest.raises(ValueError):
            run(journal.cancel(s, foreign, CREDS))
    assert body(s.calls[0]) == {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": by["client_id"]}
    assert s.calls[1]["method"] == "DELETE" and s.calls[1]["query"]["symbol"] == "ETH-USDT" and len(s.calls) == 2


# --- хаос: сбой на каждом await ---

FAULTS = ("lost_request", "lost_response", "5xx_before", "5xx_after", "redirect", "broken_json", "reject",
          "partial", "stop")


class Chaos:
    """Биржа, у которой i-й запрос (любой: создание или запрос статуса) даёт сбой. «Сбой после» — биржа ордер приняла,
    ответ потерялся; «до» — не дошёл. stop — «⛔ Стоп» в этот момент, запрос проходит нормально."""
    def __init__(self, venue, at, fault):
        self.venue, self.at, self.fault, self.n, self.book = venue, at, fault, 0, Book()
        self.stop_at = None

    def _tick(self):
        i, self.n = self.n, self.n + 1
        if i != self.at:
            return None
        if self.fault == "stop":
            switch.stop()
            self.stop_at = i
            return None
        return self.fault

    def create(self, call):
        f = self._tick()
        err = bybit_err(110007) if self.venue == "bybit" else bingx_err(101204)
        if f == "lost_request":
            return Resp(exc=asyncio.TimeoutError())
        if f == "5xx_before":
            return Resp(502, b"")
        if f == "redirect":
            return Resp(307, b"")
        if f == "reject":
            return err
        make = (self.book.bybit_create(status="PartiallyFilled" if f == "partial" else "New")
                if self.venue == "bybit" else self.book.bingx_create(status="PARTIALLY_FILLED" if f == "partial"
                                                                      else "NEW"))
        ok = make(call)
        if f == "lost_response":
            return Resp(exc=aiohttp.ClientConnectionError())
        if f == "5xx_after":
            return Resp(502, b"")
        if f == "broken_json":
            return Resp(200, b'{"retCode":0,"result":')
        return ok

    def query(self, call):
        f = self._tick()
        if f in ("lost_request", "lost_response"):
            return Resp(exc=asyncio.TimeoutError())
        if f in ("5xx_before", "5xx_after", "reject"):
            return Resp(503, b"")
        if f == "redirect":
            return Resp(302, b"")
        if f == "broken_json":
            return Resp(200, b"<html>")
        return self.book.bybit_query(call) if self.venue == "bybit" else self.book.bingx_query(call)

    def routes(self):
        if self.venue == "bybit":
            return {CREATE_BY: self.create, RT: self.query, HIST: self.query}
        return {CREATE_BX: self.create, QBX: self.query}


@pytest.mark.parametrize("venue", ["bybit", "bingx"])
@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("at", range(8))
def test_chaos_every_await(venue, fault, at):
    chaos = Chaos(venue, at, fault)
    s = Session(chaos.routes())
    order = BY() if venue == "bybit" else BX()
    res = submit(s, order)
    rows = journal.history()
    assert len(rows) == 1 and res["state"] == rows[0]["state"] in ("open", "filled", "closed", "rejected", "unknown")
    cid = rows[0]["client_id"]
    posts = creates(s)
    assert 1 <= len(posts) <= 1 + journal.MAX_RESEND
    if venue == "bybit":
        assert {body(c)["orderLinkId"] for c in posts} == {cid} and len({c["data"] for c in posts}) == 1
    else:
        assert {business(c)["clientOrderId"] for c in posts} == {cid} and len({json.dumps(business(c), sort_keys=True)
                                                                               for c in posts}) == 1
    exists = cid in chaos.book.orders
    if res["state"] == "rejected":
        assert not exists                          # «отклонён» — только если ордера на бирже правда нет
    if res["state"] in ("open", "filled", "closed"):
        assert exists
    if res["state"] == "unknown":
        assert journal.blocking()
    if chaos.stop_at is not None:                  # после «⛔ Стоп» новых ордеров на открытие не уходит
        idx = [i for i, c in enumerate(s.calls) if (c["method"], c["path"]) in (CREATE_BY, CREATE_BX)]
        assert all(i <= chaos.stop_at for i in idx)
    # сверка после хаоса (сеть уже здорова) доводит до правды и ничего не отправляет
    chaos.at = -1
    run(journal.reconcile(s, _creds_for))
    final = journal.get(cid)["state"]
    assert (final in ("open", "filled", "closed")) == exists or final == "unknown"
    assert final != "rejected" or not exists
    assert len(creates(s)) == len(posts)


@pytest.mark.parametrize("step", range(4))
def test_cancellation_at_any_await_leaves_order_blocking_until_reconciled(step):
    """Задачу отменили на любом await (перезапуск, выключение) — строка не пропадает и блокирует открытия, пока
    reconcile/resume не выяснят исход."""
    book = Book()
    n = {"i": 0}

    def maybe_cancel(inner):
        def answer(call):
            i, n["i"] = n["i"], n["i"] + 1
            if i == step:
                return Resp(exc=asyncio.CancelledError())
            return inner(call) if callable(inner) else inner
        return answer

    s = Session({CREATE_BY: maybe_cancel(lambda c: (book.bybit_create()(c), Resp(exc=asyncio.TimeoutError()))[1]),
                 RT: maybe_cancel(bybit_ok({"list": []})), HIST: maybe_cancel(bybit_ok({"list": []}))})
    try:
        res = submit(s, BY())
    except asyncio.CancelledError:
        res = None
    row = journal.history()[0]
    if res is None:
        assert row["state"] in ("sending", "unknown") and journal.blocking()
        s.routes = {RT: book.bybit_query, HIST: bybit_ok({"list": []})}
        run(journal.reconcile(s, _creds_for))
        # ордер дошёл до биржи — сверка его находит; не дошёл — «не найден», но исход создания неизвестен: unknown
        assert journal.get(row["client_id"])["state"] == ("open" if row["client_id"] in book.orders else "unknown")


# --- спот: продать можно только купленное ботом ---

def _spot(side, qty, state, filled=None, fee=None, symbol="ETHUSDT"):
    order = venues.Order("bybit", "spot", symbol, side, "market", qty)
    row = journal._insert_intent(order, "open" if side == "buy" else "close", "funding", "minlot", None)
    journal._update(row["client_id"], state="sending")
    fields = {k: v for k, v in (("filled", filled), ("fee", fee)) if v is not None}
    if state in ("open", "filled", "closed", "rejected", "unknown"):
        journal._update(row["client_id"], state="open" if state in ("filled", "closed") else state, **fields)
    if state in ("filled", "closed"):
        journal._update(row["client_id"], state=state)
    return journal.get(row["client_id"])


def test_spot_inventory_counts_only_bot_fills_minus_fee():
    assert journal.spot_inventory("bybit", "ETHUSDT") == 0
    _spot("buy", "1", "filled", filled="1", fee="0.001")                   # комиссия меньше запаса 0.2% → 0.998
    _spot("buy", "2", "unknown")                                            # исход неясен — не считаем
    _spot("buy", "3", "rejected")
    _spot("buy", "0.5", "open", filled="0.5", fee="0.005")                 # частично, комиссия больше запаса
    assert journal.spot_inventory("bybit", "ETHUSDT") == Decimal("0.998") + Decimal("0.495")
    _spot("sell", "0.3", "unknown")                                         # продажа с неясным исходом — целиком
    _spot("sell", "0.2", "rejected")                                        # отклонённая — 0
    _spot("sell", "0.4", "closed", filled="0.1")                            # закрыта частично — исполненное
    assert journal.spot_inventory("bybit", "ETHUSDT") == Decimal("1.493") - Decimal("0.3") - Decimal("0.1")
    assert journal.spot_inventory("bybit", "BTCUSDT") == 0 and journal.spot_inventory("bingx", "ETHUSDT") == 0


def test_spot_sell_limited_to_inventory_even_when_trading_off(monkeypatch):
    _spot("buy", "1", "filled", filled="1", fee="0")
    book = Book()
    s = Session(bybit_routes(book, **{"_": None}))
    monkeypatch.setenv("TRADING", "0")                                     # закрытие работает и при TRADING=0
    too_much = venues.Order("bybit", "spot", "ETHUSDT", "sell", "market", "0.999")
    res = submit(s, too_much, purpose="close")
    assert res["state"] == "refused" and "купленное ботом" in res["reason"] and s.calls == []
    ok = venues.Order("bybit", "spot", "ETHUSDT", "sell", "market", "0.998")
    res = submit(s, ok, purpose="close")
    assert res["state"] == "open" and body(creates(s)[0])["marketUnit"] == "baseCoin"
    again = submit(s, venues.Order("bybit", "spot", "ETHUSDT", "sell", "market", "0.001"), purpose="close")
    assert again["state"] == "refused" and len(creates(s)) == 1            # первая продажа уже списана
    with pytest.raises(ValueError):                                        # продажа на споте — не «открытие»
        submit(s, ok, purpose="open")


def test_ton_order_refused_until_symbol_verified_then_uses_gram():
    s = Session(bybit_routes(Book(), **{"_": None}))
    ton = venues.Order("bybit", "linear", "TONUSDT", "sell", "market", "10")
    res = submit(s, ton)
    assert res["state"] == "refused" and "resolve_symbols" in res["reason"] and s.calls == []
    venues._RESOLVED[("bybit", "linear")] = {"ts": time.time(), "map": {"TONUSDT": "GRAMUSDT"}, "why": {}}
    res = submit(s, ton)
    assert res["state"] == "open" and body(creates(s)[0])["symbol"] == "GRAMUSDT"
    assert res["row"]["symbol"] == "TONUSDT"
    s.routes[RT] = lambda c: bybit_ok({"list": [{"orderLinkId": res["row"]["client_id"], "symbol": "GRAMUSDT",
                                                 "side": "Sell", "orderType": "Market", "qty": "10",
                                                 "orderStatus": "Filled", "cumExecQty": "10"}]})
    venues._RESOLVED.clear()                                                # сверка идёт по символу из параметров
    events = run(journal.reconcile(s, _creds_for))
    assert [e for e, _ in events] == ["filled"] and s.calls[-1]["query"]["symbol"] == "GRAMUSDT"
