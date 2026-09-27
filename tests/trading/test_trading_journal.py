"""Торговое ядро, journal: намерение до запроса, идемпотентность по клиентскому id, неясный исход и ограниченный повтор,
несовпадение → unknown, выключатель посреди отправки, resume/reconcile, хаос на каждом await; замки (сеть — вне общего
замка), версии строк (CAS), outbox, сверка любого возраста."""
import asyncio
import json
import time
from decimal import Decimal

import aiohttp
import pytest

from trading import journal, switch, venues
from trading_stubs import (CREDS, Book, Gated, Resp, Session, bingx_err, bingx_ok, body, business, bybit_err,
                           bybit_ok, fresh_journal, run)

CREATE_BY, CREATE_BX = ("POST", "/v5/order/create"), ("POST", "/openApi/swap/v2/trade/order")
RT, HIST, QBX = ("GET", "/v5/order/realtime"), ("GET", "/v5/order/history"), ("GET", "/openApi/swap/v2/trade/order")


def BY(**kw):
    return venues.Order("bybit", "linear", "BTCUSDT", kw.pop("side", "sell"), "market", kw.pop("qty", "0.001"), **kw)


def BX(**kw):
    return venues.Order("bingx", "swap", "ETHUSDT", kw.pop("side", "sell"), "market", kw.pop("qty", "0.01"), **kw)


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(venues, "_RESOLVED", {})
    fresh_journal(monkeypatch, tmp_path)
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")


def bybit_routes(book):
    return book.routes("bybit")


def bingx_routes(book):
    return book.routes("bingx")


def submit(s, order, **kw):
    if kw.get("purpose", "open") == "open":
        kw.setdefault("strategy", "hedge")
    return run(journal.submit(s, order, CREDS, **kw))


def creates(s):
    return s.sent(*CREATE_BY) + s.sent(*CREATE_BX)


def lookups(s):
    """Запросы ордера по нашему id (не список открытых ордеров символа)."""
    return [c for c in s.sent(*RT) if "orderLinkId" in c["query"]] + s.sent(*HIST) + s.sent(*QBX)


def lookup_route(book, answer):
    """GET /v5/order/realtime: список открытых ордеров символа — как у «биржи», запрос по id — answer."""
    def route(call):
        if "orderLinkId" in call["query"]:
            return answer(call) if callable(answer) else answer
        return book.bybit_query(call)
    return route


def own_position(venue="bybit", side="sell", qty=None, strategy="hedge", group="", book=None):
    """Позиция бота в журнале (исполненный ордер на открытие) и, если дана «биржа», — на ней."""
    qty = qty or ("0.001" if venue == "bybit" else "0.01")
    order = BY(side=side, qty=qty) if venue == "bybit" else BX(side=side, qty=qty)
    row = journal._insert_intent(order, "open", strategy, "confirm", None, group=group)
    journal._update(row["client_id"], state="sending")
    journal._update(row["client_id"], state="filled", filled=qty, avg_price="65000" if venue == "bybit" else "3000")
    if book is not None:
        sym = "BTCUSDT" if venue == "bybit" else "ETH-USDT"
        book.position[sym] = book.position.get(sym, Decimal(0)) + (Decimal(qty) if side == "buy" else -Decimal(qty))
    return journal.get(row["client_id"])


# --- намерение и успех ---

def test_intent_persisted_before_request_with_exact_params():
    book, seen = Book(), []

    def create(call):
        cid = body(call)["orderLinkId"]
        seen.append(journal.get(cid))                     # намерение уже в журнале до ответа биржи
        return book.bybit_create()(call)

    s = Session(bybit_routes(book) | {CREATE_BY: create})
    res = submit(s, BY(stop_loss="70000"), strategy="directional", mode="confirm", notional=Decimal("1"))
    row = res["row"]
    assert res["state"] == "open" and res["event"] is None and row["venue_order_id"] == book.orders[row["client_id"]][
        "orderId"]
    assert seen[0]["state"] == "sending" and json.loads(seen[0]["params"]) == body(creates(s)[0])
    assert row["strategy"] == "directional" and row["mode"] == "confirm"
    assert Decimal(row["notional"]) == Decimal("65")                    # номинал считает ядро по цене биржи
    assert len(creates(s)) == 1 and not lookups(s)
    assert journal.blocking() == []
    assert all(c["timeout"].total == venues.REQUEST_TIMEOUT for c in s.calls)   # таймаут у каждого запроса


def test_mode_never_above_switch(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "minlot")
    s = Session(bybit_routes(Book()))
    res = submit(s, BY(), mode="auto")                                  # 65 USDT > 50 minlot — режим не выше .env
    assert res["state"] == "refused" and "итоговая позиция" in res["reason"] and not creates(s)


@pytest.mark.parametrize("status,state,event", [("NEW", "open", None), ("FILLED", "filled", "filled"),
                                                ("PARTIALLY_FILLED", "open", None)])
def test_bingx_success_state_from_response(status, state, event):
    book = Book()
    s = Session(bingx_routes(book) | {CREATE_BX: book.bingx_create(status=status)})
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

    s = Session(bybit_routes(book) | {CREATE_BY: create})
    res = submit(s, BY())
    assert res["state"] == "open" and res["event"] == "found" and len(creates(s)) == 1


def test_timeout_then_not_found_resends_same_id_and_same_body():
    book = Book()
    s = Session(bybit_routes(book) | {CREATE_BY: [Resp(exc=asyncio.TimeoutError()), Resp(502, b""),
                                                   book.bybit_create()]})
    res = submit(s, BY())
    assert res["state"] == "open"
    posts = creates(s)
    assert len(posts) == 3 and len({c["data"] for c in posts}) == 1          # те же байты тела
    assert len({c["headers"]["X-BAPI-API-KEY"] for c in posts}) == 1
    assert len(journal.history()) == 1 and journal.get(res["row"]["client_id"])["posts"] == 3


def test_bingx_resend_same_business_params_fresh_timestamp():
    book = Book()
    s = Session(bingx_routes(book) | {CREATE_BX: [Resp(exc=aiohttp.ServerDisconnectedError()), book.bingx_create()]})
    res = submit(s, BX(stop_loss="3500"))
    posts = creates(s)
    assert res["state"] == "open" and len(posts) == 2
    assert business(posts[0]) == business(posts[1]) and business(posts[0])["stopLoss"].startswith('{"type"')


def test_still_unknown_after_bounded_resends_blocks_opens():
    s = Session(bybit_routes(Book()) | {CREATE_BY: Resp(exc=asyncio.TimeoutError())})
    res = submit(s, BY())
    assert res["state"] == "unknown" and res["event"] == "unknown"
    assert len(creates(s)) == 1 + journal.MAX_RESEND and len({c["data"] for c in creates(s)}) == 1
    assert [r["client_id"] for r in journal.blocking()] == [res["row"]["client_id"]]


def test_definite_rejection_confirmed_by_query_is_rejected_not_blocking():
    s = Session(bybit_routes(Book()) | {CREATE_BY: bybit_err(110007, "Available balance is insufficient")})
    res = submit(s, BY())
    assert res["state"] == "rejected" and "110007" in res["reason"]
    assert len(creates(s)) == 1 and len(lookups(s)) == 3                        # сверено по id: 2 realtime + history
    assert journal.blocking() == []


def test_rejection_but_order_exists_is_adopted():
    book = Book()

    def create(call):
        book.bybit_create()(call)
        return bybit_err(10001, "params error")

    s = Session(bybit_routes(book) | {CREATE_BY: create})
    res = submit(s, BY())
    assert res["state"] == "open" and res["event"] == "found"


def test_duplicate_id_found_adopted_not_found_unknown():
    book = Book()
    s = Session(bybit_routes(book) | {CREATE_BY: lambda c: (book.bybit_create()(c), bybit_err(110072))[1]})
    assert submit(s, BY())["state"] == "open"
    s = Session(bybit_routes(book) | {CREATE_BY: bybit_err(110072)})
    res = submit(s, BY())
    assert res["state"] == "unknown" and len(creates(s)) == 1


@pytest.mark.parametrize("answer", [Resp(500, {"retCode": 10016}), Resp(200, b""), Resp(200, b"<html>"),
                                    Resp(307, b""), Resp(403, b"forbidden"), Resp(429, b""), bybit_err(10016),
                                    bybit_err(999999), Resp(exc=aiohttp.ClientConnectionError()),
                                    Resp(200, {"retCode": 0, "result": {"orderId": "1", "orderLinkId": "t0"}})])
def test_ambiguous_or_mismatched_never_success_or_rejection(answer):
    """Неясный ответ, а статус по id не узнать (сверка сама падает) — unknown, в блоке открытий; не open и не
    rejected. Ответ 0 с чужим orderLinkId — тоже unknown (несовпадение)."""
    book = Book()
    s = Session(bybit_routes(book) | {CREATE_BY: answer, RT: lookup_route(book, Resp(500, b"")), HIST: Resp(500, b"")})
    res = submit(s, BY())
    assert res["state"] == "unknown" and journal.blocking()
    assert len(creates(s)) == 1


def test_error_on_resend_after_ambiguity_stays_unknown():
    s = Session(bybit_routes(Book()) | {CREATE_BY: [Resp(exc=asyncio.TimeoutError()), bybit_err(110007)]})
    res = submit(s, BY())
    assert res["state"] == "unknown" and len(creates(s)) == 2 and journal.blocking()


@pytest.mark.parametrize("venue,over", [("bybit", {"qty": "0.002"}), ("bybit", {"symbol": "ETHUSDT"}),
                                        ("bybit", {"side": "Buy"}), ("bybit", {"symbol": ""}),
                                        ("bingx", {"origQty": "0.02"}), ("bingx", {"side": "BUY"}),
                                        ("bingx", {"clientOrderId": "tother"}), ("bingx", {"status": "WEIRD"}),
                                        ("bingx", {"symbol": "GRAM-USDT"}), ("bingx", {"symbol": "BTC-USDT"})])
def test_mismatch_is_unknown_with_alert(venue, over):
    """Ответ не совпал с намерением (в т. ч. чужой или пустой символ; «GRAM-USDT» BingX — другой токен) — unknown,
    тревога в outbox, блок открытий."""
    book = Book()
    if venue == "bybit":
        def create(call):
            book.bybit_create(**over)(call)
            return Resp(exc=asyncio.TimeoutError())       # статус узнаём запросом — там и несовпадение
        s = Session(bybit_routes(book) | {CREATE_BY: create})
        res = submit(s, BY())
    else:
        s = Session(bingx_routes(book) | {CREATE_BX: book.bingx_create(**over)})
        res = submit(s, BX())
    assert res["state"] == "unknown" and res["event"] == "mismatch" and journal.blocking()
    assert [e["event"] for e in journal.pending_events()] == ["mismatch"]


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
    own_position(book=book)
    s = Session(bybit_routes(book))
    res = submit(s, BY(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "open" and body(creates(s)[0])["reduceOnly"] is True


def test_stop_during_ambiguity_blocks_resend_of_open(monkeypatch):
    book = Book()

    def query(call):
        switch.stop()                                     # «⛔ Стоп» во время разбора неясного исхода
        return bybit_ok({"list": []})
    s = Session(bybit_routes(book) | {CREATE_BY: Resp(exc=asyncio.TimeoutError()), RT: lookup_route(book, query)})
    res = submit(s, BY())
    assert res["state"] == "unknown" and len(creates(s)) == 1 and "повтор не отправлен" in res["reason"]


def test_stop_does_not_block_resend_of_close(monkeypatch):
    book = Book()
    own_position(book=book)

    def query(call):
        switch.stop()
        return book.bybit_query(call)
    s = Session(bybit_routes(book) | {CREATE_BY: [Resp(exc=asyncio.TimeoutError()), book.bybit_create()],
                                      RT: lookup_route(book, query)})
    res = submit(s, BY(side="buy", reduce_only=True), purpose="close")
    assert res["state"] == "open" and len(creates(s)) == 2


def test_stop_while_first_request_in_flight_sends_nothing_more():
    """Стоп, пока первый запрос летит: он уже ушёл (его не отозвать), но повторов нет."""
    book = Book()
    s = Session(bybit_routes(book))

    async def go():
        gate = asyncio.Event()
        s.routes[CREATE_BY] = lambda call: Gated(gate, Resp(exc=asyncio.TimeoutError()))
        task = asyncio.ensure_future(journal.submit(s, BY(), CREDS, strategy="hedge"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(creates(s)) == 1 and not task.done()
        switch.stop()
        gate.set()
        return await task
    res = run(go())
    assert res["state"] == "unknown" and len(creates(s)) == 1


def test_precheck_runs_under_lock_after_snapshot_and_refuses_before_insert():
    s = Session(bybit_routes(Book()))
    seen = []

    def precheck():
        seen.append(journal.state_lock().locked())
        return "дневной стоп"
    res = submit(s, BY(), precheck=precheck)
    assert res == {"state": "refused", "row": None, "reason": "дневной стоп", "event": None}
    assert seen == [True] and not creates(s) and journal.history() == []


def test_no_key_or_invalid_order_refused_before_insert():
    s = Session(bybit_routes(Book()))
    assert run(journal.submit(s, BY(), None, strategy="hedge"))["state"] == "refused"
    bad = venues.Order("bybit", "linear", "DOGEUSDT", "sell", "market", "1")
    res = run(journal.submit(s, bad, CREDS, strategy="hedge"))
    assert res["state"] == "refused" and "DOGEUSDT" in res["reason"]
    assert run(journal.submit(s, BY(), CREDS))["state"] == "refused"          # без стратегии — не открываем
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


# --- замки: сеть вне общего замка, открытия одного символа — по очереди ---

def test_opens_on_one_symbol_serialize():
    """Два открытия одного символа одной биржи: второе не начинает (даже снимок символа), пока первое не закончило."""
    book = Book()
    s = Session(bybit_routes(book))

    async def go():
        gate = asyncio.Event()
        first = book.bybit_create()
        s.routes[CREATE_BY] = [lambda call: Gated(gate, first(call)), book.bybit_create()]
        t1 = asyncio.ensure_future(journal.submit(s, BY(), CREDS, strategy="hedge"))
        t2 = asyncio.ensure_future(journal.submit(s, BY(qty="0.002"), CREDS, strategy="hedge"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(creates(s)) == 1 and not journal.state_lock().locked()   # общий замок сеть не держит
        gate.set()
        return await t1, await t2
    r1, r2 = run(go())
    assert r1["state"] == r2["state"] == "open" and len(creates(s)) == 2


def test_close_on_other_venue_not_waiting_behind_bybit_retries():
    """Открытие на Bybit висит в запросе (и потом в повторах) — закрытие на BingX проходит сразу, сверка тоже."""
    by, bx = Book(), Book()
    own_position("bingx", book=bx)
    routes = bybit_routes(by) | bingx_routes(bx)
    s = Session(routes)

    async def go():
        gate = asyncio.Event()
        s.routes[CREATE_BY] = lambda call: Gated(gate, Resp(exc=asyncio.TimeoutError()))
        t_open = asyncio.ensure_future(journal.submit(s, BY(), CREDS, strategy="hedge"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not t_open.done()
        close = await asyncio.wait_for(journal.submit(s, BX(side="buy", reduce_only=True), CREDS, purpose="close"), 5)
        events = await asyncio.wait_for(journal.reconcile(s, lambda v: CREDS), 5)
        gate.set()
        return close, events, await t_open
    close, events, opened = run(go())
    assert close["state"] == "open" and business(s.sent(*CREATE_BX)[0])["reduceOnly"] == "true"
    assert opened["state"] == "unknown"                                  # Bybit: таймаут, не найден, повторы
    assert all(r["client_id"] != opened["row"]["client_id"] for _, r in events)   # идущую отправку сверка не трогала


def test_resolve_or_cancel_during_ambiguity_prevents_resend():
    """Владелец разобрал строку (другой процесс) или запросил отмену, пока submit ждал повтора: повтора нет — версия
    строки сменилась."""
    for action in ("resolve", "cancel"):
        book = Book()
        s = Session(bybit_routes(book) | {("POST", "/v5/order/cancel"): bybit_err(110001, "Order does not exist")})
        state = {}

        def query(call):
            cid = call["query"]["orderLinkId"]
            if not state:
                state["done"] = True
                if action == "resolve":
                    row = journal.get(cid)
                    journal._transition(cid, expect=row["version"], state="closed", note="владелец: проверил")
                else:
                    asyncio.ensure_future(journal.cancel(s, cid, CREDS))
            return bybit_ok({"list": []})
        s.routes[RT] = lookup_route(book, query)
        s.routes[CREATE_BY] = [Resp(exc=asyncio.TimeoutError()), book.bybit_create()]
        res = submit(s, BY())
        assert len(creates(s)) == 1, action                                    # повтор не ушёл
        assert res["reason"] == journal.CHANGED and res["state"] in ("closed", "unknown")
        journal._locks.update(loop=None)


def test_resolve_refuses_inflight_row():
    row = _row("unknown")
    journal._inflight.add(row["client_id"])
    with pytest.raises(ValueError, match="отправляется"):
        journal.resolve(row["client_id"], "closed", "x")
    journal._inflight.discard(row["client_id"])
    assert journal.resolve(row["client_id"], "closed", "x")["state"] == "closed"
    assert [e["event"] for e in journal.pending_events()] == ["resolved"]


# --- журнал: переходы, resume, счётчики, версии ---

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
    before = journal.get(row["client_id"])
    with pytest.raises(ValueError):
        journal._update(row["client_id"], state=dst)
    assert journal.get(row["client_id"]) == before                         # ни версии, ни полей


def test_version_compare_and_set():
    row = _row("unknown")
    new, _ = journal._transition(row["client_id"], expect=row["version"], note="a")
    assert new["version"] == row["version"] + 1
    assert journal._transition(row["client_id"], expect=row["version"], note="b") == (None, None)   # устаревшая
    assert journal.get(row["client_id"])["note"] == "a"


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
    assert journal.pending_events() == [] and journal.exposure() == []
    assert not (tmp_path / "trading.db").exists()


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT UNIQUE NOT NULL, "
                "state TEXT)")
    con.commit()
    con.close()
    con = journal._connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(orders)")}
    con.close()
    assert {"grp", "version", "cancel_requested"} <= cols


# --- outbox ---

def test_outbox_written_with_state_change_and_deduplicated():
    row = _row("unknown")
    journal._transition(row["client_id"], state="unknown", note="x", event="mismatch")
    journal._transition(row["client_id"], state="unknown", note="x", event="mismatch")      # то же — не дублируется
    journal._transition(row["client_id"], state="unknown", note="y", event="mismatch")
    ev = journal.pending_events()
    assert [(e["event"], e["note"]) for e in ev] == [("mismatch", "x"), ("mismatch", "y")]
    assert journal.mark_delivered([ev[0]["id"]]) == 1 and [e["note"] for e in journal.pending_events()] == ["y"]
    with pytest.raises(ValueError):                                     # запрещённый переход — и события нет
        journal._transition(row["client_id"], state="prepared", event="boom")
    assert [e["event"] for e in journal.pending_events()] == ["mismatch"]


def test_reconcile_row_failure_keeps_other_events(monkeypatch):
    """Сбой на одной строке сверки не теряет события остальных: они уже в outbox той же транзакцией."""
    book = Book()
    a, b = _row("open"), _row("open")
    book.bybit_store(a["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="0.001",
                     orderStatus="Filled", cumExecQty="0.001")
    book.bybit_store(b["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="0.001",
                     orderStatus="Filled", cumExecQty="0.001")
    real = journal._apply_view

    def flaky(row, view, expect=None):
        if row["client_id"] == b["client_id"]:
            raise RuntimeError("диск")
        return real(row, view, expect)
    monkeypatch.setattr(journal, "_apply_view", flaky)
    s = Session({RT: book.bybit_query, HIST: bybit_ok({"list": []})})
    events = run(journal.reconcile(s, lambda v: CREDS))
    assert [(e, r["client_id"]) for e, r in events] == [("filled", a["client_id"])]
    got = {(e["event"], e["client_id"]) for e in journal.pending_events()}
    assert got == {("filled", a["client_id"]), ("reconcile_error", b["client_id"])}


def test_reconcile_cancelled_midway_keeps_committed_events():
    book = Book()
    a, b = _row("open"), _row("open")
    book.bybit_store(a["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="0.001",
                     orderStatus="Filled", cumExecQty="0.001")

    def query(call):
        if call["query"].get("orderLinkId") == b["client_id"]:
            return Resp(exc=asyncio.CancelledError())
        return book.bybit_query(call)
    s = Session({RT: query, HIST: bybit_ok({"list": []})})
    with pytest.raises(asyncio.CancelledError):
        run(journal.reconcile(s, lambda v: CREDS))
    assert [(e["event"], e["client_id"]) for e in journal.pending_events()] == [("filled", a["client_id"])]


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
    assert {(e["client_id"], e["event"]) for e in journal.pending_events()} == {(r, ev) for r, ev in got.items()}
    assert all(c["method"] == "GET" for c in s.calls)                        # сверка ничего не отправляет


def test_reconcile_query_failure_changes_nothing_and_orphans_become_unknown():
    a, b = _row("open"), _row("sending")
    s = Session({RT: Resp(exc=asyncio.TimeoutError()), HIST: Resp(500, b"")})
    events = run(journal.reconcile(s, _creds_for))
    assert events == [("unknown", journal.get(b["client_id"]))]
    assert journal.get(a["client_id"])["state"] == "open" and journal.get(b["client_id"])["state"] == "unknown"


def test_reconcile_any_age_and_keyless_still_block():
    """Старые незавершённые ордера не выпадают из сверки молча: запрос идёт, «не найден» — unknown и блок открытий."""
    old = _row("open", created=time.time() - 400 * 86400)
    bx = _row("unknown", venue="bingx")
    s = Session({RT: bybit_ok({"list": []}), HIST: bybit_ok({"list": []})})
    events = run(journal.reconcile(s, lambda v: CREDS if v == "bybit" else None))
    assert [(e, r["client_id"]) for e, r in events] == [("notfound", old["client_id"])]
    assert {c["query"].get("orderLinkId") for c in s.calls} == {old["client_id"]}
    assert {r["client_id"] for r in journal.blocking()} == {old["client_id"], bx["client_id"]}


def test_reconcile_skips_row_changed_during_query():
    """Строку изменили, пока шёл запрос (CAS): сверка её не перезаписывает — разберёт следующая."""
    row = _row("unknown")
    book = Book()
    book.bybit_store(row["client_id"], symbol="BTCUSDT", side="Sell", orderType="Market", qty="0.001",
                     orderStatus="Filled", cumExecQty="0.001")

    def query(call):
        journal._update(row["client_id"], note="владелец смотрит")
        return book.bybit_query(call)
    s = Session({RT: query, HIST: bybit_ok({"list": []})})
    assert run(journal.reconcile(s, _creds_for)) == []
    assert journal.get(row["client_id"])["state"] == "unknown"


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
    assert journal.get(by["client_id"])["cancel_requested"] == 1


# --- хаос: сбой на каждом await ---

FAULTS = ("lost_request", "lost_response", "5xx_before", "5xx_after", "redirect", "broken_json", "reject",
          "partial", "stop")


class Chaos:
    """Биржа, у которой i-й запрос создания или статуса по id даёт сбой. «Сбой после» — биржа ордер приняла, ответ
    потерялся; «до» — не дошёл. stop — «⛔ Стоп» в этот момент, запрос проходит нормально. Снимок символа (позиция,
    список ордеров, плечо, цена) — без сбоев."""
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
        if self.venue == "bybit" and "orderLinkId" not in call["query"]:
            return self.book.bybit_query(call)       # список открытых ордеров символа — снимок, без сбоя
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
        r = self.book.routes(self.venue)
        if self.venue == "bybit":
            r.update({CREATE_BY: self.create, RT: self.query, HIST: self.query})
        else:
            r.update({CREATE_BX: self.create, QBX: self.query})
        return r


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
        ticks = [i for i, c in enumerate(s.calls) if (c["method"], c["path"]) in (CREATE_BY, CREATE_BX, QBX, HIST)
                 or (c["method"], c["path"]) == RT and "orderLinkId" in c["query"]]
        assert all(i <= ticks[chaos.stop_at] for i in idx)
    # сверка после хаоса (сеть уже здорова) доводит до правды и ничего не отправляет
    chaos.at = -1
    run(journal.reconcile(s, _creds_for))
    final = journal.get(cid)["state"]
    assert (final in ("open", "filled", "closed")) == exists or final == "unknown"
    assert final != "rejected" or not exists
    assert len(creates(s)) == len(posts)


@pytest.mark.parametrize("step", range(5))
def test_cancellation_at_any_await_leaves_order_blocking_until_reconciled(step):
    """Задачу отменили на любом await (перезапуск, выключение) — строка не пропадает и блокирует открытия, пока
    reconcile/resume не выяснят исход; отмена до намерения — ни строки, ни ордера."""
    book = Book()
    n = {"i": 0}

    def maybe_cancel(inner):
        def answer(call):
            i, n["i"] = n["i"], n["i"] + 1
            if i == step:
                return Resp(exc=asyncio.CancelledError())
            return inner(call) if callable(inner) else inner
        return answer

    s = Session(bybit_routes(book) | {
        CREATE_BY: maybe_cancel(lambda c: (book.bybit_create()(c), Resp(exc=asyncio.TimeoutError()))[1]),
        RT: lookup_route(book, maybe_cancel(bybit_ok({"list": []}))), HIST: maybe_cancel(bybit_ok({"list": []})),
        ("GET", "/v5/position/list"): maybe_cancel(book.bybit_positions)})
    try:
        res = submit(s, BY())
    except asyncio.CancelledError:
        res = None
    rows = journal.history()
    if res is None and not rows:
        assert not creates(s) and not journal._inflight                  # отменили до намерения
        return
    row = rows[0]
    if res is None:
        assert row["state"] in ("sending", "unknown") and journal.blocking() and not journal._inflight
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
    s = Session(bybit_routes(book))
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
    s = Session(bybit_routes(Book()))
    ton = venues.Order("bybit", "linear", "TONUSDT", "sell", "market", "10")
    res = submit(s, ton)
    assert res["state"] == "refused" and "resolve_symbols" in res["reason"] and s.calls == []
    venues._RESOLVED[("bybit", "linear")] = {"ts": time.time(), "map": {"TONUSDT": "GRAMUSDT"}, "why": {}}
    res = submit(s, ton)
    assert res["state"] == "open" and body(creates(s)[0])["symbol"] == "GRAMUSDT"
    assert res["row"]["symbol"] == "TONUSDT"
    assert {c["query"].get("symbol") for c in s.calls if c["method"] == "GET" and "symbol" in c["query"]} == \
        {"GRAMUSDT"}                                                        # снимок — по символу биржи
    s.routes[RT] = lambda c: bybit_ok({"list": [{"orderLinkId": res["row"]["client_id"], "symbol": "GRAMUSDT",
                                                 "side": "Sell", "orderType": "Market", "qty": "10",
                                                 "orderStatus": "Filled", "cumExecQty": "10"}]})
    venues._RESOLVED.clear()                                                # сверка идёт по символу из параметров
    events = run(journal.reconcile(s, _creds_for))
    assert [e for e, _ in events] == ["filled"] and s.calls[-1]["query"]["symbol"] == "GRAMUSDT"
