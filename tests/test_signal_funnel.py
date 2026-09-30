"""Воронка «сигналы → сделки» (signal_funnel.py): только чтение history.signals/trades.db, сопоставление сделки
с сигналом того же направления в пределах окна, /stats не ломается при сбое воронки."""
import os

import bot as B
import history
import p2p
import signal_funnel
import trades
from helpers import make_ad
from test_bot import Stub

NOW = 1_790_000_000.0
KEY = ("Bybit", "USDT", "MEXC", "USDT")


def _signal(db, key, sig_ts, last_seen=None, profit=2.0):
    open_ids = history.track_signals([(key, profit, True, None)], sig_ts, {}, path=db)
    if last_seen is not None and last_seen != sig_ts:
        history.track_signals([(key, profit, False, "cooldown")], last_seen, open_ids, path=db)
    return open_ids


def _trade(db, key, ts, profit=2.0):
    buy_ex, buy_asset, sell_ex, sell_asset = key
    b = make_ad(buy_ex, "buy", 85.0, asset=buy_asset)
    s = make_ad(sell_ex, "sell", 90.0, asset=sell_asset)
    trades.log_trade((profit, b, s, "route"), 10000, path=db, ts=ts)


def test_trade_five_minutes_after_signal_matches(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW)
    _trade(tr, KEY, NOW + 300)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    assert data["traded"] == 1 and data["off_signal"] == 0
    assert data["median_lag_s"] == 300
    assert data["conversion"] == 1.0


def test_trade_two_hours_after_signal_off_signal(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW)
    _trade(tr, KEY, NOW + 7200)
    data = signal_funnel.funnel(days=7, window_s=3600, hist_path=hist, trades_path=tr, now=NOW + 3 * 3600)
    assert data["off_signal"] == 1
    assert data["traded"] == 0
    assert data["conversion"] == 0.0


def test_two_trades_one_signal_conversion_capped(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW)
    _trade(tr, KEY, NOW + 60)
    _trade(tr, KEY, NOW + 120)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    assert data["traded"] == 1 and data["off_signal"] == 1
    assert data["conversion"] <= 1.0


def test_trade_other_direction_not_matched(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW)
    other = ("Bybit", "USDT", "HTX", "USDT")
    _trade(tr, other, NOW + 60)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    assert data["traded"] == 0 and data["off_signal"] == 1


def test_no_signals_conversion_none_and_empty_lines(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _trade(tr, KEY, NOW)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    assert data["signals"] == 0 and data["conversion"] is None
    assert signal_funnel.lines(data) == []


def test_signals_no_trades_conversion_zero_and_no_response_line(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    assert data["traded"] == 0 and data["conversion"] == 0.0
    text = "\n".join(signal_funnel.lines(data))
    assert "без ответа" in text


def test_days_window_clamped_and_filters_old(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW - 10 * 86400)   # старше окна days=7
    _trade(tr, KEY, NOW - 10 * 86400 + 60)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW)
    assert data["signals"] == 0

    data_all = signal_funnel.funnel(days=999, hist_path=hist, trades_path=tr, now=NOW)
    assert data_all["days"] == signal_funnel.DAYS_MAX   # зажато в 1..DAYS_MAX
    data_min = signal_funnel.funnel(days=0, hist_path=hist, trades_path=tr, now=NOW)
    assert data_min["days"] == 1


def test_long_episode_matches_with_lag_from_first_signal(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW, last_seen=NOW + 1800)   # эпизод держался 30 минут
    _trade(tr, KEY, NOW + 1800 + 1800)   # ушла ближе к концу окна last_seen+window_s (1ч по умолчанию)
    data = signal_funnel.funnel(days=7, window_s=3600, hist_path=hist, trades_path=tr, now=NOW + 3 * 3600)
    assert data["traded"] == 1
    assert data["median_lag_s"] == 3600   # лаг — от первого сигнала эпизода (signal_ts), не от last_seen


def test_trade_before_signal_not_matched(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    _signal(hist, KEY, NOW)
    _trade(tr, KEY, NOW - 60)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    assert data["traded"] == 0 and data["off_signal"] == 1


def test_missing_db_files_empty_result_and_no_files_created(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW)
    assert data == {"days": 7, "signals": 0, "traded": 0, "conversion": None, "median_lag_s": None,
                    "off_signal": 0, "by_direction": []}
    assert os.listdir(tmp_path) == []


def test_names_are_html_escaped_in_lines(tmp_path):
    hist, tr = str(tmp_path / "h.db"), str(tmp_path / "t.db")
    key = ("<Ex>", "A&B", "MEXC", "USDT")
    _signal(hist, key, NOW)
    data = signal_funnel.funnel(days=7, hist_path=hist, trades_path=tr, now=NOW + 3600)
    text = "\n".join(signal_funnel.lines(data))
    assert "<Ex>" not in text and "&lt;Ex&gt;" in text
    assert "A&B" not in text and "A&amp;B" in text


def test_stats_view_survives_funnel_exception(monkeypatch, caplog):
    monkeypatch.setattr(B.signal_funnel, "funnel", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    bot = Stub(p2p.Config())
    text = bot.stats_view()
    assert "Журнал сделок" in text
    assert "signal funnel: boom" in caplog.text


def test_stats_view_shows_funnel_header_when_present(monkeypatch):
    fixed = {"days": 7, "signals": 3, "traded": 1, "conversion": 1 / 3, "median_lag_s": 120.0,
             "off_signal": 0, "by_direction": []}
    monkeypatch.setattr(B.signal_funnel, "funnel", lambda: fixed)
    bot = Stub(p2p.Config())
    text = bot.stats_view()
    assert "Сигналы → сделки за 7 дн." in text
