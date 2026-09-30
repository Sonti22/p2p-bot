"""scan_step: сбой одного шага scan_loop не глушит остальные шаги и не тонет молча — считается по имени шага,
алертит владельца в topic dev после нескольких подряд сбоев и сообщает о восстановлении."""
import asyncio
import sqlite3

import aiohttp
from aiohttp.client_reqrep import ConnectionKey
import pytest

import bot as B
import p2p
from helpers import arun
from test_bot import Stub, snap, texts


def _bot(chat_id="1"):
    bot = Stub(p2p.Config())
    bot.chat_id = chat_id
    return bot


def test_sync_and_async_steps_both_work():
    bot = _bot()
    calls = []

    def sync_step(a, b):
        calls.append(("sync", a, b))

    async def async_step(a, b):
        calls.append(("async", a, b))

    arun(bot.scan_step("s1", sync_step, 1, 2))
    arun(bot.scan_step("s2", async_step, 3, 4))
    assert calls == [("sync", 1, 2), ("async", 3, 4)]
    assert bot.step_fail == {}


def test_failing_step_does_not_block_later_steps():
    bot = _bot()
    order = []

    def boom(snap_):
        raise RuntimeError("boom")

    async def ok_step(snap_):
        order.append("quiet_pause")

    arun(bot.scan_step("alerts", boom, snap([])))
    arun(bot.scan_step("quiet_pause", ok_step, snap([])))
    assert order == ["quiet_pause"]
    assert bot.scan_errors == 0
    assert bot.step_fail["alerts"]["n"] == 1


def test_alert_after_three_consecutive_failures_once():
    bot = _bot()

    def boom():
        raise RuntimeError("boom")

    for _ in range(2):
        arun(bot.scan_step("networks", boom))
    assert texts(bot) == []
    arun(bot.scan_step("networks", boom))
    sent = texts(bot)
    assert len(sent) == 1
    assert "networks" in sent[0] and "3" in sent[0]
    assert bot.step_fail["networks"]["alerted"] is True
    arun(bot.scan_step("networks", boom))
    arun(bot.scan_step("networks", boom))
    assert len(texts(bot)) == 1   # без повторной отправки


def test_alert_retried_when_send_not_ok(monkeypatch):
    bot = _bot()
    now = [1000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])

    replies = iter([{"ok": False}, {"ok": True}])

    async def fake_send(text, chat_id=None, markup=None, topic=None, thread=None):
        bot.out.append(("sendMessage", {"text": text}))
        return next(replies)

    bot.send = fake_send

    def boom():
        raise RuntimeError("boom")

    for _ in range(3):
        arun(bot.scan_step("venues", boom))
    assert len(texts(bot)) == 1
    assert bot.step_fail["venues"]["alerted"] is False

    now[0] += 10   # ещё рано для повтора
    arun(bot.scan_step("venues", boom))
    assert len(texts(bot)) == 1

    now[0] += B.SCAN_STEP_ALERT_RETRY_SEC
    arun(bot.scan_step("venues", boom))
    assert len(texts(bot)) == 2
    assert bot.step_fail["venues"]["alerted"] is True

    arun(bot.scan_step("venues", boom))
    assert len(texts(bot)) == 2   # ok=True — больше не шлём


def test_alert_deferred_until_chat_id_exists():
    bot = _bot(chat_id="")

    def boom():
        raise RuntimeError("boom")

    for _ in range(3):
        arun(bot.scan_step("alerts", boom))
    assert texts(bot) == []
    assert bot.step_fail["alerts"]["n"] == 3

    bot.chat_id = "1"
    arun(bot.scan_step("alerts", boom))
    sent = texts(bot)
    assert len(sent) == 1 and "alerts" in sent[0]


def test_recovery_message_and_cleanup():
    bot = _bot()

    def boom():
        raise RuntimeError("boom")

    async def ok():
        return None

    for _ in range(3):
        arun(bot.scan_step("paper_ladder", boom))
    assert len(texts(bot)) == 1
    arun(bot.scan_step("paper_ladder", ok))
    sent = texts(bot)
    assert len(sent) == 2 and "снова работает" in sent[-1]
    assert "paper_ladder" not in bot.step_fail

    bot2 = _bot()

    async def send_fails(*a, **k):
        raise RuntimeError("send down")

    for _ in range(3):
        arun(bot2.scan_step("paper_ladder", boom))
    bot2.send = send_fails
    arun(bot2.scan_step("paper_ladder", ok))
    assert "paper_ladder" not in bot2.step_fail   # запись всё равно удалена

    bot3 = _bot()
    arun(bot3.scan_step("paper_ladder", ok))
    assert texts(bot3) == []   # успех без алерта молчит


def test_network_errors_counted_but_no_alert():
    bot = _bot()

    key = ConnectionKey("example.com", 443, True, None, None, None, None)

    def net_err():
        raise aiohttp.ClientConnectorError(key, OSError("refused"))

    for _ in range(5):
        arun(bot.scan_step("networks", net_err))
    assert texts(bot) == []
    assert bot.step_fail["networks"]["n"] == 5
    assert bot.step_fail["networks"]["net"] is True

    def timeout_err():
        raise asyncio.TimeoutError()

    bot2 = _bot()
    for _ in range(5):
        arun(bot2.scan_step("networks", timeout_err))
    assert texts(bot2) == []

    def conn_err():
        raise ConnectionError("reset")

    bot3 = _bot()
    for _ in range(5):
        arun(bot3.scan_step("networks", conn_err))
    assert texts(bot3) == []

    def sqlite_err():
        raise sqlite3.OperationalError("database is locked")

    bot4 = _bot()
    for _ in range(3):
        arun(bot4.scan_step("history", sqlite_err))
    assert len(texts(bot4)) == 1

    def disk_err():
        raise OSError("disk full")

    bot5 = _bot()
    for _ in range(3):
        arun(bot5.scan_step("history", disk_err))
    assert len(texts(bot5)) == 1


def test_cancelled_error_propagates():
    bot = _bot()

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        arun(bot.scan_step("networks", cancelled))
    assert bot.step_fail == {}


def test_status_view_shows_step_failures():
    bot = _bot()
    text = bot.status_view()
    assert "⚠️ Сбои шагов скана:" not in text

    bot.step_fail["alerts"] = {"n": 2, "last": "RuntimeError: boom", "alerted": False, "net": False, "try_ts": 0.0}
    text = bot.status_view()
    assert "⚠️ Сбои шагов скана:" in text
    assert "alerts" in text and "2 подряд" in text


def test_scan_loop_history_failure_still_sends_signals(monkeypatch):
    bot = _bot()

    async def fake_scan(s, cfg):
        return snap([])

    monkeypatch.setattr(B, "scan", fake_scan)

    def bad_record(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(B.history, "record", bad_record)

    called = []

    async def fake_quiet(self, snap_):
        called.append("quiet_pause")

    async def fake_market(self, snap_):
        called.append("market_status")

    monkeypatch.setattr(B.Bot, "quiet_and_pause_tick", fake_quiet)
    monkeypatch.setattr(B.Bot, "update_market_status", fake_market)

    async def no_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        arun(bot.scan_loop())

    assert "quiet_pause" in called and "market_status" in called
    assert bot.last_scan_ts > 0
    assert bot.scan_errors == 0
    assert bot.step_fail["history"]["n"] == 1
