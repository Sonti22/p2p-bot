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


def _meta(db):
    con = SF._connect(db)
    try:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def test_idle_tick_writes_meta_and_days_count_from_first_tick(tmp_path):
    """Позиций нет (ставка ниже планки), но симулятор опрашивает: «данных N дн.» считается от первого тика."""
    db = str(tmp_path / "f.db")
    _market(0.0, 0.0001)
    assert SF.tick(now=NOW, path=db) == {"opened": [], "closed": []}
    assert SF.tick(now=NOW + 3 * 86400 + 600, path=db) == {"opened": [], "closed": []}   # котировки уже старые
    assert _meta(db) == {"first_tick_ts": NOW, "last_tick_ts": NOW + 3 * 86400 + 600}   # first не перезаписывается
    s = SF.stats(db, now=NOW + 3.5 * 86400)
    assert not s["open"] and s["closed"] == 0
    assert s["days"] == pytest.approx(3.5) and s["last_tick"] == NOW + 3 * 86400 + 600
    assert "данных 3.5 дн." in SF.view(db, now=NOW + 3.5 * 86400)


def test_switched_off_tick_leaves_no_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_FUNDING", "0")
    db = str(tmp_path / "f.db")
    SF.tick(now=NOW, path=db)
    assert not (tmp_path / "f.db").exists()
    assert SF.stats(db, now=NOW) == dict(SF.stats(db, now=NOW), days=0.0, last_tick=None)


def test_legacy_db_without_meta_keeps_old_days_rule(tmp_path):
    """База, созданная до meta: таблица досоздаётся при открытии, дни — от первой позиции, тика ещё не было."""
    db = str(tmp_path / "f.db")
    con = SF._connect(db)
    con.execute("DROP TABLE meta")
    con.execute("INSERT INTO positions (scheme, symbol, long_venue, short_venue, qty, ts_open, long_open, short_open, "
                "apr_open) VALUES ('perp_perp', 'BTC', 'Bybit', 'BingX', 0.011, ?, 84000, 84000, 30.0)", (NOW,))
    con.commit()
    con.close()
    s = SF.stats(db, now=NOW + 2 * 86400)
    assert s["days"] == pytest.approx(2.0) and s["last_tick"] is None and len(s["open"]) == 1
    _market(0.0, 0.0001)
    text = SF.view(db, now=NOW + 2 * 86400)
    assert "данных 2.0 дн." in text and "последний тик" not in text
    old_empty = str(tmp_path / "e.db")
    con = SF._connect(old_empty)
    con.execute("DROP TABLE meta")
    con.close()
    SF.tick(now=NOW, path=old_empty)   # первый тик после обновления досоздаёт meta
    assert _meta(old_empty) == {"first_tick_ts": NOW, "last_tick_ts": NOW}
    # файла нет
    s = SF.stats(str(tmp_path / "none.db"), now=NOW)
    assert s["days"] == 0.0 and s["last_tick"] is None and s["open"] == []


def test_days_stay_from_first_tick_once_a_position_opens(tmp_path):
    """Обычное состояние вживую: meta есть и позиция тоже — дни от первого тика, а не от первой позиции."""
    db = str(tmp_path / "f.db")
    _market(0.0, 0.0001)
    SF.tick(now=NOW, path=db)   # холостой тик, позиций нет
    perp.reset()
    _market(0.0, 0.0006, ts=NOW + 2 * 86400, next_funding=NOW + 2 * 86400 + 600)
    assert SF.tick(now=NOW + 2 * 86400, path=db)["opened"]
    s = SF.stats(db, now=NOW + 3 * 86400)
    assert len(s["open"]) == 1 and s["days"] == pytest.approx(3.0)
    assert SF.stats(db, now=NOW - 3600)["days"] == 0.0   # часы сбились назад — не отрицательное число


def test_first_tick_after_upgrade_keeps_days_of_existing_position(tmp_path):
    """База с позицией, созданная до meta: первый тик после обновления не обнуляет «данных N дн.»."""
    db = str(tmp_path / "f.db")
    _market(0.0, 0.0006)
    assert SF.tick(now=NOW, path=db)["opened"]
    con = SF._connect(db)
    con.execute("DROP TABLE meta")
    con.commit()
    con.close()
    SF.tick(now=NOW + 86400, path=db)   # первый тик после обновления: first_tick_ts = NOW + 1 сутки
    assert _meta(db)["first_tick_ts"] == NOW + 86400
    assert SF.stats(db, now=NOW + 5 * 86400)["days"] == pytest.approx(5.0)


def test_view_shows_real_payback_bar_for_candidates(tmp_path, monkeypatch):
    """Ставка 32.85% годовых выше FUND_ENTRY_APR (20), но ниже планки окупаемости — /funding говорит, какая нужна."""
    _market(0.0, 0.0003)
    market = {c["scheme"]: c for c in SF.candidates(NOW)}
    pp, sp = market["perp_perp"], market["spot_perp"]
    assert pp["apr"] >= 20 and not pp["ok"] and pp["why"] == "окупаемость 56 ч > 48 ч"
    text = SF.view(str(tmp_path / "f.db"), now=NOW + 60)
    for c in (pp, sp):
        assert f"(нужно ≥ {c['cost_pct'] * 8760 / 48:.1f}% для окупаемости ≤ 48 ч)" in text
    assert f"нужно ≥ {pp['cost_pct'] * 8760 / 48:.1f}%" in text and pp["cost_pct"] * 8760 / 48 > pp["apr"]
    assert text.count("нужно ≥") == 2
    monkeypatch.setenv("FUND_PAYBACK_HOURS", "24")
    assert f"(нужно ≥ {pp['cost_pct'] * 8760 / 24:.1f}% для окупаемости ≤ 24 ч)" in SF.view(str(tmp_path / "f.db"), now=NOW)
    monkeypatch.setenv("FUND_PAYBACK_HOURS", "0")   # деления на ноль во /funding нет
    assert "нужно ≥" not in SF.view(str(tmp_path / "f.db"), now=NOW)


def test_view_without_cost_has_no_bar(tmp_path, monkeypatch):
    monkeypatch.setenv("FUND_NOTIONAL", "1")   # меньше лота — стоимости входа нет, подсказки тоже
    _market(0.0, 0.0003)
    text = SF.view(str(tmp_path / "f.db"), now=NOW)
    assert "лот больше позиции" in text and "нужно ≥" not in text


def test_view_shows_last_tick_age(tmp_path):
    db = str(tmp_path / "f.db")
    _market(0.0, 0.0001)
    assert "последний тик" not in SF.view(db, now=NOW)   # тиков ещё не было
    SF.tick(now=NOW, path=db)
    assert "последний тик 0 мин назад" in SF.view(db, now=NOW + 20)
    text = SF.view(db, now=NOW + 7 * 60 + 30)
    assert "данных 0.0 дн. · последний тик 7 мин назад" in text
    assert "последний тик 0 мин назад" in SF.view(db, now=NOW - 5)   # часы сбились назад — не отрицательное число


def test_candidate_ok_flags_unchanged_by_view_changes(tmp_path):
    """Правила входа те же: APR ≥ 20% не значит вход — отсекает окупаемость; ok = все три фильтра сразу."""
    expected = {0.0001: (False, "доходность 11.0% < 20%"), 0.0003: (False, "окупаемость 56 ч > 48 ч"), 0.0005: (True, "")}
    for rate, (ok, why) in expected.items():
        perp.reset()
        _market(0.0, rate)
        c = next(c for c in SF.candidates(NOW) if c["scheme"] == "perp_perp")
        assert (c["ok"], c["why"]) == (ok, why)
        assert c["ok"] == (c["apr"] >= 20 and c["payback_h"] <= 48 and abs(c["basis"]) <= 0.3)
        if ok:   # планка показана и у подходящих строк
            rows = SF.view(str(tmp_path / "v.db"), now=NOW + 1).splitlines()
            assert any(r.startswith("✅") and "нужно ≥" in r for r in rows)
    perp.reset()
    _market(0.0, 0.0003)
    assert [(c["scheme"], c["ok"]) for c in SF.candidates(NOW)] == [("perp_perp", False), ("spot_perp", False)]
