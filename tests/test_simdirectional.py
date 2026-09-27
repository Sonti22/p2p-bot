"""simdirectional.py: EMA(20/100) 1ч на бумаге — индикаторы, вход только по живой свече, стоп ATR (по котировке и по
свече), разворот, одна позиция, случайная база, фандинг, итоги /futures; команда только владельцу."""

import pytest

import bot as B
import p2p
import perp
import simdirectional as SD
import test_bot as TB
from perpfx import install, quote
from helpers import arun

H = 3600
L0 = 1790488800.0   # начало последней «ровной» свечи (кратно часу)
REAL_KLINE_TIME = perp.kline_time


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    perp.reset()
    for k in ("SIM_DIRECTIONAL", "DIR_ASSETS", "DIR_RISK_USDT", "DIR_MAX_NOTIONAL", "DIR_ATR_MULT", "DIR_ENTRY_LAG",
              "DIR_RANDOM_P", "DIR_RANDOM_HOLD_H", "DIR_STOP_SLIP", "DIR_FEE_MULT", "PERP_MAX_AGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DIR_ASSETS", "BTC")
    monkeypatch.setenv("DIR_RANDOM_P", "0")
    # свечи тесты кладут в perp._klines сами (без опроса) — считаем их загруженными «сейчас»; правило «свеча закрыта,
    # только если загружена после закрытия» проверяет test_candle_fetched_before_close_is_not_closed
    monkeypatch.setattr(perp, "kline_time", lambda venue, symbol: float("inf"))
    yield
    perp.reset()


def flat(n=150, price=100.0, end=L0):
    return [(end - (n - 1 - i) * H, price, price + 0.5, price - 0.5, price) for i in range(n)]


def candle(start, close, prev=None, low=None, high=None):
    o = prev if prev is not None else close
    return (start, o, high if high is not None else max(o, close) + 0.5, low if low is not None else min(o, close) - 0.5,
            close)


def _q(mid, ts, symbol="BTCUSDT", **kw):
    return quote("Bybit", symbol, mid=mid, spread=0.1, size=100.0, lot=0.001, min_qty=0.001, ts=ts, now=ts, **kw)


def _enter_long(db, monkeypatch=None):
    """Ровный рынок, первая встреча (без сделок), затем свеча-скачок вверх — пересечение и вход в лонг."""
    perp._klines[("Bybit", "BTCUSDT")] = flat()
    assert SD.tick(now=L0 + H + 60, path=db) == {"opened": [], "closed": []}   # первая встреча — прошлое не торгуем
    perp._klines[("Bybit", "BTCUSDT")].append(candle(L0 + H, 110.0, prev=100.0))
    t = L0 + 2 * H + 60
    install(_q(110.0, t))
    return SD.tick(now=t, path=db), t


def test_ema_atr_crosses():
    assert SD.ema([1, 2, 3], 1) == [1, 2, 3]
    e = SD.ema([10, 20], 3)
    assert e == [10, 15.0]
    a = SD.atr([(0, 1, 2, 1, 1.5), (1, 1.5, 4, 1.5, 3)], n=2)
    assert a[0] == 1 and a[1] == pytest.approx(1 + (2.5 - 1) / 2)
    closes = [100.0] * 120 + [110.0] + [110.0] * 5
    c = SD.crosses(closes)
    assert c[120] == 1 and sum(abs(x) for x in c) == 1


def test_entry_on_live_cross_with_atr_stop_and_fixed_risk(tmp_path):
    db = str(tmp_path / "d.db")
    out, t = _enter_long(db)
    assert out["opened"] == [{"book": "ema", "asset": "BTC", "side": 1}]
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    atr_v = SD.atr(perp.klines("Bybit", "BTCUSDT"))[-1]
    assert p["px_open"] == pytest.approx(110.05)                      # по аскам
    assert p["stop"] == pytest.approx(110.05 - 2 * atr_v)
    assert p["qty"] == pytest.approx(int(2 / (2 * atr_v) / 0.001) * 0.001)
    assert p["risk"] == pytest.approx(p["qty"] * 2 * atr_v) and p["risk"] <= 2.0
    assert p["fees"] == pytest.approx(0.00055 * 110.05 * p["qty"] * 2)   # тейкер ×2, как в бэктесте


def test_late_candle_is_not_entered(tmp_path):
    db = str(tmp_path / "d.db")
    perp._klines[("Bybit", "BTCUSDT")] = flat()
    SD.tick(now=L0 + H + 60, path=db)
    perp._klines[("Bybit", "BTCUSDT")].append(candle(L0 + H, 110.0, prev=100.0))
    t = L0 + 2 * H + 2000   # свеча закрылась 33 мин назад — дольше DIR_ENTRY_LAG
    install(_q(110.0, t))
    assert SD.tick(now=t, path=db)["opened"] == []


def test_stop_by_quote(tmp_path):
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)
    install(_q(100.0, t + 60))
    out = SD.tick(now=t + 60, path=db)
    assert out["closed"][0]["reason"] == "стоп" and out["closed"][0]["pnl"] < 0


def test_stop_by_candle_when_quotes_missed(tmp_path):
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    kl = perp._klines[("Bybit", "BTCUSDT")]
    kl.append(candle(L0 + 2 * H, 110.0, prev=110.0))   # свеча входа: её low до входа не в счёт
    kl.append(candle(L0 + 3 * H, 110.0, prev=110.0, low=p["stop"] - 1))
    out = SD.tick(now=L0 + 4 * H + 5000, path=db)   # котировка старая — стоп по low закрытой свечи
    assert out["closed"][0]["reason"] == "стоп (по свече)"
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    assert p["px_close"] == pytest.approx(p["stop"] * (1 - 0.0005))


def test_reversal_exit_and_single_position(tmp_path, monkeypatch):
    db = str(tmp_path / "d.db")
    monkeypatch.setenv("DIR_ATR_MULT", "25")   # стоп далеко — проверяем выход по развороту
    monkeypatch.setenv("DIR_RISK_USDT", "20")
    _enter_long(db)
    kl = perp._klines[("Bybit", "BTCUSDT")]
    prev = 110.0
    for i in range(30):
        kl.append(candle(L0 + (2 + i) * H, 90.0, prev=prev))
        prev = 90.0
    t = L0 + 32 * H + 60
    install(_q(90.0, t))
    out = SD.tick(now=t, path=db)
    assert [c["reason"] for c in out["closed"]] == ["разворот тренда"]
    assert out["opened"] == []   # пересечение было не на последней свече — входа нет


def test_reversal_without_quote_closes_on_next_quote(tmp_path, monkeypatch):
    db = str(tmp_path / "d.db")
    monkeypatch.setenv("DIR_ATR_MULT", "25")
    monkeypatch.setenv("DIR_RISK_USDT", "20")
    _, t = _enter_long(db)
    kl = perp._klines[("Bybit", "BTCUSDT")]
    prev = 110.0
    for i in range(30):
        kl.append(candle(L0 + (2 + i) * H, 90.0, prev=prev))
        prev = 90.0
    t2 = L0 + 32 * H + 60
    assert SD.tick(now=t2, path=db)["closed"] == []   # котировка устарела — выход отложен
    install(_q(90.0, t2 + 30))
    assert SD.tick(now=t2 + 30, path=db)["closed"][0]["reason"] == "разворот тренда"


def test_max_one_strategy_position(tmp_path, monkeypatch):
    db = str(tmp_path / "d.db")
    monkeypatch.setenv("DIR_ASSETS", "BTC,ETH")
    for sym in ("BTCUSDT", "ETHUSDT"):
        perp._klines[("Bybit", sym)] = flat()
    SD.tick(now=L0 + H + 60, path=db)
    t = L0 + 2 * H + 60
    for sym in ("BTCUSDT", "ETHUSDT"):
        perp._klines[("Bybit", sym)].append(candle(L0 + H, 110.0, prev=100.0))
    install(_q(110.0, t), _q(110.0, t, symbol="ETHUSDT"))
    out = SD.tick(now=t, path=db)
    assert len([o for o in out["opened"] if o["book"] == "ema"]) == 1


def test_random_baseline_and_time_exit(tmp_path, monkeypatch):
    db = str(tmp_path / "d.db")
    monkeypatch.setenv("DIR_RANDOM_P", "1")
    monkeypatch.setenv("DIR_RANDOM_HOLD_H", "1")
    perp._klines[("Bybit", "BTCUSDT")] = flat()
    SD.tick(now=L0 + H + 60, path=db)
    perp._klines[("Bybit", "BTCUSDT")].append(candle(L0 + H, 100.0))   # без пересечения
    t = L0 + 2 * H + 60
    install(_q(100.0, t))
    out = SD.tick(now=t, path=db)
    assert [o["book"] for o in out["opened"]] == ["random"]
    install(_q(100.0, t + H + 1))
    out = SD.tick(now=t + H + 1, path=db)
    assert out["closed"][0]["book"] == "random" and out["closed"][0]["reason"] == "срок 1 ч"


def test_funding_long_pays(tmp_path):
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)   # котировка входа: ставка 0.0001, расчёт через час
    install(_q(110.0, t + H + 10, rate=0.0003, next_funding=t + 9 * H))
    SD.tick(now=t + H + 10, path=db)
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    assert p["funding"] == pytest.approx(-0.0001 * 110.0 * p["qty"])


def test_book_stats():
    rows = [{"pnl": 3.0, "risk": 1.0}, {"pnl": -1.0, "risk": 1.0}, {"pnl": -1.0, "risk": 1.0}, {"pnl": 2.0, "risk": 1.0}]
    s = SD.book_stats(rows)
    assert s["trades"] == 4 and s["win_rate"] == 0.5 and s["pf"] == 2.5 and s["pnl"] == 3.0
    assert s["max_dd"] == 2.0 and s["avg_r"] == 0.75
    assert SD.book_stats([])["trades"] == 0


def test_view_and_owner_only_command(tmp_path, monkeypatch):
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)
    text = SD.view(db, now=t)
    assert "Направленная стратегия" in text and "лонг" in text and "Держать BTC" in text
    orig = SD.view
    monkeypatch.setattr(B.simdirectional, "view", lambda: orig(db, now=t))
    bot = TB.Stub(p2p.Config())
    arun(bot.dispatch("/futures", "paper"))
    assert "Направленная стратегия" in TB.texts(bot)[-1]
    token = B.REPLY_CHAT.set("999")
    try:
        arun(bot.dispatch("/futures", ""))
    finally:
        B.REPLY_CHAT.reset(token)
    assert "только для владельца" in TB.texts(bot)[-1]


def test_fees_x2_like_backtest_and_x1_reported(tmp_path, monkeypatch):
    """Итог сделки — с комиссиями ×DIR_FEE_MULT (2, как research/directional_bt.py); /futures показывает и ×1.
    Множитель запоминается на входе: смена настройки не пересчитывает открытую позицию."""
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)
    monkeypatch.setenv("DIR_FEE_MULT", "1")   # после входа — на эту позицию не влияет
    install(_q(100.0, t + 60))
    c = SD.tick(now=t + 60, path=db)["closed"][0]
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    taker = 0.00055 * (p["px_open"] + p["px_close"]) * p["qty"]
    assert p["fees"] == pytest.approx(2 * taker)
    assert c["pnl"] == pytest.approx((p["px_close"] - p["px_open"]) * p["qty"] - 2 * taker + p["funding"])
    assert SD.at_fees_x1(p)["pnl"] == pytest.approx(c["pnl"] + taker)
    books, _, _, books_x1 = SD.stats(db)
    assert books_x1["ema"]["pnl"] == pytest.approx(books["ema"]["pnl"] + taker)
    assert "При комиссиях ×1: стратегия" in SD.view(db, now=t + 60)


def test_candle_fetched_before_close_is_not_closed(tmp_path, monkeypatch):
    """Свечи в кеше загружены до закрытия часа — последняя в них неполная: по ней не входим и её не «съедаем»
    (last:BTC не сдвигается), вход — когда свечи загружены после закрытия."""
    monkeypatch.setattr(perp, "kline_time", REAL_KLINE_TIME)
    db = str(tmp_path / "d.db")
    key = ("Bybit", "BTCUSDT")
    perp._klines[key] = flat()
    perp._kline_srv[key] = L0 + H + 30
    SD.tick(now=L0 + H + 60, path=db)
    perp._klines[key].append(candle(L0 + H, 110.0, prev=100.0))
    t = L0 + 2 * H + 20
    install(_q(110.0, t))
    perp._kline_srv[key] = L0 + 2 * H - 30   # загрузка за 30 с до закрытия свечи
    assert SD.tick(now=t, path=db)["opened"] == []
    assert SD._get_state(SD._connect(db), "last:BTC") == L0
    perp._kline_srv[key] = L0 + 2 * H + 10   # докачали после закрытия
    assert SD.tick(now=t, path=db)["opened"] == [{"book": "ema", "asset": "BTC", "side": 1}]


def test_position_of_removed_asset_is_still_managed(tmp_path, monkeypatch):
    """Монету убрали из DIR_ASSETS при открытой позиции — стоп по ней работает, новых входов нет."""
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)
    monkeypatch.setenv("DIR_ASSETS", "ETH")
    install(_q(100.0, t + 60))
    out = SD.tick(now=t + 60, path=db)
    assert out["closed"] and out["closed"][0]["asset"] == "BTC" and out["closed"][0]["reason"] == "стоп"


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_DIRECTIONAL", "0")
    perp._klines[("Bybit", "BTCUSDT")] = flat()
    assert SD.tick(now=L0 + H + 60, path=str(tmp_path / "d.db")) == {"opened": [], "closed": []}


def test_restore_after_downtime_charges_funding_only_until_candle_stop(tmp_path):
    """Бот лежал: стоп по свече случился раньше двух следующих расчётов фандинга. События — по времени: фандинг только
    до стопа, время закрытия — время свечи стопа, а не «сейчас»."""
    db = str(tmp_path / "d.db")
    _, t = _enter_long(db)   # котировка входа: ставка 0.0001, расчёт через час (t + H), интервал 8 ч
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    kl = perp._klines[("Bybit", "BTCUSDT")]
    stop_start = L0 + 5 * H
    for start in range(int(L0 + 2 * H), int(L0 + 20 * H), H):
        kl.append(candle(start, 110.0, prev=110.0, low=p["stop"] - 1 if start == stop_start else None))
    now = L0 + 21 * H + 60   # расчёты в t+H, t+9H, t+17H уже «наступили», но стоп был в свече L0+5H
    out = SD.tick(now=now, path=db)
    assert [c["reason"] for c in out["closed"]] == ["стоп (по свече)"]
    p = SD._dicts(SD._connect(db).execute("SELECT * FROM positions"))[0]
    assert p["ts_close"] == pytest.approx(stop_start)
    assert p["funding"] == pytest.approx(-0.0001 * 110.0 * p["qty"])   # один расчёт (t + H), а не три


def test_unrecoverable_candle_gap_pauses_strategy_explicitly(tmp_path):
    """После простоя свечи с last_ts не догрузить (perp.kline_gap) — пропущенные часы не торгуем, пауза видна в /futures;
    со следующей свечи стратегия идёт дальше."""
    db = str(tmp_path / "d.db")
    _enter_long(db)
    tail_end = L0 + 400 * H
    perp._klines[("Bybit", "BTCUSDT")] = flat(150, price=110.0, end=tail_end)   # непрерывный хвост после разрыва
    perp._kline_gap[("Bybit", "BTCUSDT")] = (L0 + 2 * H, tail_end - 149 * H)
    out = SD.tick(now=tail_end + H + 60, path=db)
    assert out["paused"] == [{"asset": "BTC", "reason": "разрыв свечей после простоя — пропущенные часы не торгуем"}]
    assert "⏸ BTC: пауза стратегии" in SD.view(path=db, now=tail_end + H + 60)
    perp._klines[("Bybit", "BTCUSDT")].append(candle(tail_end + H, 110.0, prev=110.0))
    out = SD.tick(now=tail_end + 2 * H + 60, path=db)
    assert "paused" not in out and "пауза" not in SD.view(path=db, now=tail_end + 2 * H + 60)
