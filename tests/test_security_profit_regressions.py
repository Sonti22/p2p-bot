"""Офлайн-регрессии октябрьского аудита и денежного результата сухого прогона."""
import asyncio
import time

import aiohttp
from aiohttp import web
import pytest

import accounts
import backup
import bot as B
import p2p
import paper
import payouts
import trades
from helpers import make_ad
from test_bot import Stub


def test_first_private_sender_cannot_take_owner(monkeypatch):
    b = Stub(p2p.Config())
    b.chat_id = ""
    b.guests.add("777")
    writes = []
    monkeypatch.setattr(B, "save_env", lambda k, v: writes.append((k, v)))
    msg = {"chat": {"id": 777, "type": "private"}, "from": {"id": 777}, "text": "/start"}
    asyncio.run(b.on_update({"message": msg}))
    assert b.chat_id == "" and writes == [] and b._owner_gate(msg) is None


def test_missing_owner_prevents_startup_before_any_network(monkeypatch):
    monkeypatch.setattr(B, "setup_logging", lambda: None)
    monkeypatch.setattr(B, "load_env", lambda: None)
    monkeypatch.setenv("TG_TOKEN", "DUMMY_TOKEN")
    monkeypatch.setenv("TG_CHAT_ID", "")
    with pytest.raises(SystemExit, match="TG_CHAT_ID"):
        asyncio.run(B.main())


@pytest.mark.parametrize("status", [302, 307, 308])
def test_signed_get_does_not_follow_redirects(monkeypatch, status):
    async def run():
        captured = []
        async def handler(request):
            if request.path == "/capture":
                captured.append(dict(request.headers))
                return web.json_response({})
            return web.Response(status=status, headers={"Location": "/capture"})
        app = web.Application()
        app.router.add_get("/{path:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        for name in ("BYBIT_BASE", "MEXC_BASE", "HTX_BASE", "KUCOIN_BASE"):
            monkeypatch.setattr(accounts, name, base)
        try:
            async with aiohttp.ClientSession() as s:
                calls = [accounts.bybit_get(s, "DUMMY", "DUMMY", "/v5/account/wallet-balance"),
                         accounts.mexc_get(s, "DUMMY", "DUMMY", "/api/v3/account"),
                         accounts.htx_get(s, "DUMMY", "DUMMY", "/v1/account/accounts"),
                         accounts.kucoin_get(s, "DUMMY", "DUMMY", "DUMMY", "/api/v1/accounts")]
                for call in calls:
                    with pytest.raises(aiohttp.ClientResponseError) as exc:
                        await call
                    assert exc.value.status == status
            assert captured == []
        finally:
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["{broken", "[]", '{"bybit": null}', '{"bybit": {"key": "dpapi:bad", "secret": "dpapi:bad"}}'])
def test_invalid_key_storage_never_falls_back_to_env(monkeypatch, bad):
    monkeypatch.setenv("BYBIT_API_KEY", "DUMMY_ENV")
    monkeypatch.setenv("BYBIT_API_SECRET", "DUMMY_SECRET")
    with open(accounts.KEYS_PATH, "w", encoding="utf-8") as f:
        f.write('{"bybit": {"disabled": true}}')
    assert accounts.keys("bybit") is None
    with open(accounts.KEYS_PATH, "w", encoding="utf-8") as f:
        f.write(bad)
    assert accounts.keys("bybit") is None


def test_partial_sale_is_not_confirmed_and_orders_cannot_be_reused():
    now = time.time()
    d = (10.0, make_ad("Bybit", "buy", 100), make_ad("Bybit", "sell", 110), "внутри биржи")
    ids = [trades.log_trade(d, 10000, ts=now)[0] for _ in range(2)]
    rows = trades.unmatched()
    hist = {"bybit": [{"id": "buy1", "fiat": "RUB", "asset": "USDT", "side": "buy", "ts": now,
                       "amount": 100, "price": 100},
                      {"id": "sell1", "fiat": "RUB", "asset": "USDT", "side": "sell", "ts": now + 1,
                       "amount": 95, "price": 110}]}
    assert trades.set_auto_fact(rows[0], hist) is None
    hist["bybit"][1]["amount"] = 100
    assert trades.set_auto_fact(rows[0], hist) == pytest.approx(10)
    assert trades.set_auto_fact(rows[1], hist) is None
    assert len(trades.unmatched()) == 1


def test_payout_journal_backup_preserves_unknown_and_daily_total(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "7")
    now = time.time()
    src = str(tmp_path / "payouts.db")
    con = payouts._connect(src)
    with con:
        con.execute("INSERT INTO payouts (order_id, created_ts, state, usdt_value) VALUES (?, ?, ?, ?)",
                    ("DUMMY_ORDER", now, "unknown", "40"))
    con.close()
    dest, done = backup.run(now, str(tmp_path))
    assert "payouts.db" in done
    restored = dest + "/payouts.db"
    assert payouts.used_today(now, restored) == 40
    assert payouts.pending(now, restored)[0]["order_id"] == "DUMMY_ORDER"


def test_paper_profit_is_weighted_and_includes_losses():
    now = time.time()
    buy, sell = make_ad("Bybit", "buy", 100), make_ad("MEXC", "sell", 110)
    for amount, pct in ((10000, 2), (20000, -1)):
        cid = paper.start_cycle(amount, buy, sell, "route", pct, ts=now)
        paper.finish_cycle(cid, "done", pct)
    cid = paper.start_cycle(30000, buy, sell, "route", 5, ts=now)
    paper.finish_cycle(cid, "failed_sell", 0)
    st = paper.stats(now=now)["day"]
    assert st["profit_rub"] == 0 and st["turnover_rub"] == 30000 and st["return_pct"] == 0
    b = Stub(p2p.Config())
    view = b.paper_view()
    assert "теоретическая прибыль +0.00 ₽" in view
    assert "стоимость оставшейся монеты" in view


# These regressions explicitly exercise the preserved historical engine.
import pytest as _compat_pytest
pytestmark = _compat_pytest.mark.usefixtures("legacy_paper_engine")
