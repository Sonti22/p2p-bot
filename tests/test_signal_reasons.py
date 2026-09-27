"""Таблица signals (history.db): эпизоды связок выше порога, был ли сигнал и почему нет — первая преграда в порядке
notify (quiet / paused / trap / max_signals / unconfirmed / cooldown / unsent). Пишет Bot.record_signals после
отправки сигналов скана (quiet_and_pause_tick)."""
import logging
import sqlite3
import time

import favorites
import history
import p2p
from test_signal_traps import _bot, ad, good_deal, trap_deal
from helpers import arun

KEY_GOOD = ("HTX", "USDT", "KuCoin", "USDT")
KEY_TRAP = ("Bybit", "USDT", "MEXC", "USDT")


def other_deal():
    return 3.0, ad("MEXC", "buy", 87.6), ad("HTX", "sell", 90.5), "перевод на HTX"


KEY_OTHER = ("MEXC", "USDT", "HTX", "USDT")


def snap_of(deals, ts):
    return p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, list(deals), {}, {}, {}, ts=ts)


def reasons(bot, s, since=None, **kw):
    return {key: r for key, _d, r in bot.signal_reasons(s, time.time() if since is None else since, **kw)}


def rows():
    con = sqlite3.connect(history.DB_PATH)
    got = con.execute("SELECT buy_ex, buy_asset, sell_ex, sell_asset, first_seen, last_seen, scans, max_profit, "
                      "signalled, signal_ts, reason_not_signalled FROM signals ORDER BY id").fetchall()
    con.close()
    return {r[:4]: r[4:] for r in got}


def test_reason_order_quiet_paused_trap_topn(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    bot = _bot(monkeypatch)
    bot.max_signals = 1
    s = snap_of([trap_deal(), good_deal(), other_deal()], 1000.0)
    assert reasons(bot, s, quiet=True) == dict.fromkeys((KEY_TRAP, KEY_GOOD, KEY_OTHER), "quiet")
    assert reasons(bot, s, paused=True) == dict.fromkeys((KEY_TRAP, KEY_GOOD, KEY_OTHER), "paused")
    # слот топа — у чистой связки; ловушка отсеяна до топа; третья — вне топ-1
    assert reasons(bot, s) == {KEY_TRAP: "trap", KEY_GOOD: "unsent", KEY_OTHER: "max_signals"}
    monkeypatch.setenv("SIGNAL_TRAPS", "1")                 # ловушки шлём — она и занимает слот
    assert reasons(bot, s) == {KEY_TRAP: "unsent", KEY_GOOD: "max_signals", KEY_OTHER: "max_signals"}


def test_below_threshold_not_tracked(monkeypatch):
    bot = _bot(monkeypatch, min_profit=3.5)
    s = snap_of([good_deal(), other_deal()], 1000.0)          # 3.9% и 3.0%
    assert set(reasons(bot, s)) == {KEY_GOOD}


def test_unconfirmed_and_cooldown(monkeypatch):
    bot = _bot(monkeypatch)
    bot.live_scans = 2
    s = snap_of([good_deal()], 1000.0)
    assert reasons(bot, s) == {KEY_GOOD: "unconfirmed"}
    bot.live[KEY_GOOD] = {"first": 900.0, "streak": 2}
    now = time.time()
    bot.sent[KEY_GOOD] = (now - 10, 3.9)                       # слали 10 с назад, прибыль не выросла
    assert reasons(bot, s, since=now) == {KEY_GOOD: "cooldown"}
    bot.sent[KEY_GOOD] = (now - 10, 3.0)                       # выросла на ≥ REPEAT_STEP — повтор разрешён
    assert reasons(bot, s, since=now) == {KEY_GOOD: "unsent"}
    bot.sent[KEY_GOOD] = (now + 1, 3.9)                        # отметка этого скана — сигнал ушёл
    assert reasons(bot, s, since=now) == {KEY_GOOD: None}


def test_favorite_outside_top_is_not_max_signals(monkeypatch):
    bot = _bot(monkeypatch)
    bot.max_signals = 1
    favorites.toggle(KEY_OTHER)
    s = snap_of([good_deal(), other_deal()], 1000.0)
    assert reasons(bot, s) == {KEY_GOOD: "unsent", KEY_OTHER: "unsent"}


def test_tick_writes_episodes(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    bot = _bot(monkeypatch)
    bot.live_scans = 2
    first = snap_of([trap_deal(), good_deal()], 1000.0)
    bot.track_liveness(first)
    arun(bot.quiet_and_pause_tick(first))
    got = rows()
    assert got[KEY_GOOD] == (1000.0, 1000.0, 1, 3.9, 0, None, "unconfirmed")
    assert got[KEY_TRAP] == (1000.0, 1000.0, 1, 8.0, 0, None, "trap")
    second = snap_of([good_deal()], 1020.0)                   # ловушка ушла под порог, чистая держится 2 скана
    bot.track_liveness(second)
    arun(bot.quiet_and_pause_tick(second))
    got = rows()
    assert got[KEY_GOOD] == (1000.0, 1020.0, 2, 3.9, 1, 1020.0, None)   # сигнал ушёл на втором скане
    assert got[KEY_TRAP][1] == 1000.0 and set(bot.signal_rows) == {KEY_GOOD}
    assert history.signal_stats(now=1020.0)["episodes"] == 2


def test_send_failure_is_unsent(monkeypatch):
    bot = _bot(monkeypatch)

    async def boom(*a, **k):
        raise OSError("net down")
    monkeypatch.setattr(bot, "send_deal", boom)
    arun(bot.quiet_and_pause_tick(snap_of([good_deal()], 1000.0)))
    assert rows()[KEY_GOOD][4:] == (0, None, "unsent")


def test_paused_tick_records_paused(monkeypatch):
    bot = _bot(monkeypatch)
    bot.paused = True
    arun(bot.quiet_and_pause_tick(snap_of([good_deal()], 1000.0)))
    assert rows()[KEY_GOOD][6] == "paused" and not bot.out


def test_record_error_logged_not_raised(monkeypatch, caplog):
    bot = _bot(monkeypatch)

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(history, "track_signals", broken)
    with caplog.at_level(logging.WARNING, logger="bot"):
        arun(bot.quiet_and_pause_tick(snap_of([good_deal()], 1000.0)))
    assert "signals: disk I/O error" in caplog.text
    assert [m for m, _p in bot.out if m == "sendPhoto"]           # сигнал при этом ушёл
