"""simfunding.py: бумажный арбитраж фандинга — вход по фильтрам, точный фандинг обеих ног в их расчёты, MTM,
выход, итоги /funding; команда только владельцу."""
import asyncio

import pytest

import bot as B
import p2p
import perp
import simfunding as SF
import test_bot as TB
from perpfx import install, quote
from helpers import arun

NOW = 1790494000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    perp.reset()
    for k in ("SIM_FUNDING", "FUND_ASSETS", "FUND_NOTIONAL", "FUND_ENTRY_APR", "FUND_EXIT_APR", "FUND_PAYBACK_HOURS",
              "FUND_MAX_BASIS", "FUND_STOP", "FUND_MAX_DAYS", "FUND_MAX_OPEN", "FUND_SPOT_FEE", "PERP_MAX_AGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FUND_ASSETS", "BTC")
    yield
    perp.reset()


def _market(bybit_rate, bingx_rate, ts=NOW, next_funding=NOW + 600, mid=84000.0, bingx_mid=None, spot=True):
    install(quote("Bybit", "BTCUSDT", mid=mid, spread=0.2, size=5.0, rate=bybit_rate, lot=0.001, fee=0.055, ts=ts,
                  next_funding=next_funding, now=ts),
            quote("BingX", "BTCUSDT", mid=bingx_mid or mid, spread=0.2, size=5.0, rate=bingx_rate, lot=0.0001, fee=0.05,
                  ts=ts, next_funding=next_funding, now=ts),
            spot=[quote("Bybit", "BTCUSDT", mid=mid, spread=0.2, size=5.0, rate=0.0, lot=0.0, min_qty=0.0, ts=ts,
                        kind="spot", now=ts)] if spot else [])


def test_apr_and_evaluate_filters():
    assert SF.apr(0.0001 / 8) == pytest.approx(10.95)
    _market(0.0, 0.0005)   # BingX 0.05% за 8 ч ≈ 54.75% годовых, Bybit 0
    cands = {c["scheme"]: c for c in SF.candidates(NOW)}
    pp = cands["perp_perp"]
    assert pp["long"].venue == "Bybit" and pp["short"].venue == "BingX" and pp["qty"] == pytest.approx(0.011)
    assert pp["apr"] == pytest.approx(54.75) and pp["ok"] and pp["payback_h"] < 48
    sp = cands["spot_perp"]   # спот Bybit + шорт Bybit: ставка Bybit 0 — не входим
    assert not sp["ok"] and "доходность" in sp["why"]
    perp.reset()
    _market(0.0, 0.0005, bingx_mid=84500)   # разрыв цен 0.6% — не входим
    pp = next(c for c in SF.candidates(NOW) if c["scheme"] == "perp_perp")
    assert not pp["ok"] and "разрыв" in pp["why"]


def test_payback_filter(monkeypatch):
    monkeypatch.setenv("FUND_PAYBACK_HOURS", "2")
    _market(0.0, 0.0005)
    pp = next(c for c in SF.candidates(NOW) if c["scheme"] == "perp_perp")
    assert not pp["ok"] and "окупаемость" in pp["why"]


def test_open_settle_both_legs_and_close(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    t = NOW + 600
    _market(0.0001, 0.0006, next_funding=t)
    out = SF.tick(now=NOW, path=db)
    assert out["opened"] == [{"scheme": "perp_perp", "symbol": "BTC", "apr": pytest.approx(SF.apr(0.0005 / 8))}]
    assert SF.tick(now=NOW + 1, path=db)["opened"] == []   # одна позиция на (схему, символ)
    # до расчёта ставки сменились — спишутся последние перед расчётом
    perp.reset()
    _market(0.0002, 0.0007, ts=NOW + 500, next_funding=t)
    SF.tick(now=NOW + 500, path=db)
    perp.reset()
    _market(0.0002, 0.0007, ts=t + 10, next_funding=t + 8 * 3600)   # после расчёта
    SF.tick(now=t + 10, path=db)
    con = SF._connect(db)
    rows = con.execute("SELECT leg, venue, rate, amount FROM fundings ORDER BY leg").fetchall()
    con.close()
    qty = 0.011
    assert rows == [("long", "Bybit", 0.0002, pytest.approx(-0.0002 * 84000 * qty)),
                    ("short", "BingX", 0.0007, pytest.approx(0.0007 * 84000 * qty))]
    s = SF.stats(db, now=t + 10)
    assert s["settlements"] == 2 and len(s["open"]) == 1
    assert s["open"][0]["funding"] == pytest.approx(0.0005 * 84000 * qty)
    # ставки сошлись — после расчёта выходим
    perp.reset()
    _market(0.0002, 0.0002, ts=t + 60, next_funding=t + 8 * 3600)
    out = SF.tick(now=t + 60, path=db)
    assert len(out["closed"]) == 1 and "доходность упала" in out["closed"][0]["reason"]
    s = SF.stats(db, now=t + 60)
    assert s["closed"] == 1 and not s["open"]
    p = SF._dicts(SF._connect(db).execute("SELECT * FROM positions"))[0]
    legs = (p["long_close"] - p["long_open"]) * qty + (p["short_open"] - p["short_close"]) * qty
    assert p["pnl"] == pytest.approx(legs + p["funding"] - p["fees"])
    assert p["fees"] == pytest.approx(0.00055 * (p["long_open"] + p["long_close"]) * qty
                                      + 0.0005 * (p["short_open"] + p["short_close"]) * qty)
    # как в research/funding_bt.py (комиссии ×2) — отдельной цифрой рядом с фактом
    assert f"при комиссиях ×2, как в бэктесте, — {p['pnl'] - p['fees']:+.2f} USDT" in SF.view(db, now=t + 60)


def test_spot_perp_and_mtm_stop(tmp_path, monkeypatch):
    db = str(tmp_path / "f.db")
    monkeypatch.setenv("FUND_STOP", "0.5")
    _market(0.002, 0.002)   # разницы нет, но ставка Bybit высокая — спот + шорт перпа Bybit
    out = SF.tick(now=NOW, path=db)
    assert out["opened"][0]["scheme"] == "spot_perp"
    perp.reset()   # перп улетел вверх, спот нет — базис против шорта, MTM хуже −0.5%
    install(quote("Bybit", "BTCUSDT", mid=85000, spread=0.2, size=5.0, rate=0.002, lot=0.001, ts=NOW + 30,
                  next_funding=NOW + 600, now=NOW + 30),
            spot=[quote("Bybit", "BTCUSDT", mid=84000, spread=0.2, size=5.0, lot=0.0, min_qty=0.0, ts=NOW + 30,
                        kind="spot", now=NOW + 30)])
    out = SF.tick(now=NOW + 30, path=db)
    assert out["closed"] and out["closed"][0]["reason"].startswith("стоп")


def test_no_quotes_no_entry_and_stale_quotes_wait(tmp_path):
    db = str(tmp_path / "f.db")
    assert SF.tick(now=NOW, path=db) == {"opened": [], "closed": []}
    _market(0.0, 0.0006)
    SF.tick(now=NOW, path=db)
    out = SF.tick(now=NOW + 15 * 86400, path=db)   # срок вышел, но котировки старые — ждём свежих
    assert out["closed"] == []


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_FUNDING", "0")
    _market(0.0, 0.0006)
    assert SF.tick(now=NOW, path=str(tmp_path / "f.db")) == {"opened": [], "closed": []}


def test_view_text(tmp_path):
    db = str(tmp_path / "f.db")
    _market(0.0, 0.0005)
    SF.tick(now=NOW, path=db)
    text = SF.view(db, now=NOW + 60)
    assert "Арбитраж фандинга" in text and "перп–перп BTC:" in text and "&lt;" in text   # «<» экранирован
    assert "<b>Сейчас</b>" in text


def test_funding_command_owner_only(monkeypatch, tmp_path):
    db = str(tmp_path / "f.db")
    orig = SF.view
    monkeypatch.setattr(B.simfunding, "view", lambda: orig(db))
    bot = TB.Stub(p2p.Config())
    arun(bot.dispatch("/funding", ""))
    assert "Арбитраж фандинга" in TB.texts(bot)[-1]
    bot.out.clear()
    token = B.REPLY_CHAT.set("999")   # гость
    try:
        arun(bot.dispatch("/funding", ""))
    finally:
        B.REPLY_CHAT.reset(token)
    assert "Арбитраж" not in TB.texts(bot)[-1] and "только для владельца" in TB.texts(bot)[-1]


def test_sim_tick_isolates_failures(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(B.simfunding, "tick", boom)
    TB.Stub(p2p.Config()).sim_tick()   # не падает


def test_perp_loop_refreshes_then_ticks_sims(monkeypatch):
    calls = []

    async def fake_refresh(s):
        calls.append("refresh")
        return {}

    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(B.perp, "refresh_if_due", fake_refresh)
    bot = TB.Stub(p2p.Config())
    monkeypatch.setattr(bot, "sim_tick", lambda: calls.append("tick"))
    monkeypatch.setattr(B.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        arun(bot.perp_loop())
    assert calls == ["refresh", "tick"]


def test_leg_below_min_notional_after_lot_rounding_is_rejected(tmp_path):
    """FUND_NOTIONAL 1000, но после округления до лота нога ~924 USDT, а минимум ордера BingX 950 — кандидат не
    проходит, причина понятна; позиция не открывается."""
    import dataclasses
    _market(0.0, 0.0005)
    q = perp._quotes[("BingX", "BTCUSDT")]
    perp._quotes[("BingX", "BTCUSDT")] = dataclasses.replace(q, min_notional=950.0)
    pp = next(c for c in SF.candidates(NOW) if c["scheme"] == "perp_perp")
    assert pp["qty"] == pytest.approx(0.011)
    assert not pp["ok"] and "шорт BingX" in pp["why"] and "минимума ордера 950" in pp["why"]
    assert SF.tick(now=NOW, path=str(tmp_path / "f.db"))["opened"] == []   # спот–перп тоже нет: ставка Bybit 0
