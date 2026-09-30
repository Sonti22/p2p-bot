"""sigreport: /signals — доля пропущенных длинных эпизодов, причины, задержка сигнала, топ направлений.
Все БД — в tmp_path через history.track_signals, время фиксировано параметром now, без сети."""
import bot as B
import history
import sigreport

BASE = 1_700_000_000.0
K1 = ("Bybit", "USDT", "MEXC", "USDT")
K2 = ("HTX", "USDT", "BestChange", "USDT")


def test_empty_db_has_no_signals_line(tmp_path):
    db = str(tmp_path / "none.db")
    data = sigreport.build(path=db, now=BASE)
    assert data["empty"] is True
    assert sigreport.render(data) == "за период сигналов нет"


def test_no_rows_in_window_has_no_signals_line(tmp_path):
    db = str(tmp_path / "history.db")
    history.track_signals([(K1, 1.5, True, None)], BASE - 200_000, {}, path=db)   # больше 1 дня назад
    data = sigreport.build(path=db, days=1, now=BASE)
    assert data["empty"] is True
    assert sigreport.render(data) == "за период сигналов нет"


def test_missed_share_denominator_excludes_not_missed_reasons(tmp_path):
    db = str(tmp_path / "history.db")
    k3 = ("KuCoin", "USDT", "MEXC", "USDT")     # quiet — не пропуск, не входит в знаменатель
    k4 = ("Bybit", "USDT", "HTX", "USDT")       # paused — не пропуск
    k5 = ("MEXC", "USDT", "Bybit", "USDT")      # cooldown — не пропуск
    k6 = ("HTX", "USDT", "KuCoin", "USDT")      # trap — не пропуск
    ids = {}
    ids = history.track_signals(
        [(K1, 1.5, False, "unconfirmed"), (K2, 2.0, False, "unconfirmed"), (k3, 1.2, False, "unconfirmed"),
         (k4, 1.1, False, "unconfirmed"), (k5, 1.1, False, "unconfirmed"), (k6, 1.1, False, "unconfirmed")],
        BASE - 300, ids, path=db)
    ids = history.track_signals(
        [(K1, 1.6, True, None), (K2, 2.0, False, "max_signals"), (k3, 1.2, False, "quiet"),
         (k4, 1.1, False, "paused"), (k5, 1.1, False, "cooldown"), (k6, 1.1, False, "trap")],
        BASE - 240, ids, path=db)
    history.track_signals(
        [(K1, 1.6, False, "cooldown"), (K2, 2.1, False, "max_signals"), (k3, 1.2, False, "quiet"),
         (k4, 1.1, False, "paused"), (k5, 1.1, False, "cooldown"), (k6, 1.1, False, "trap")],
        BASE, ids, path=db)
    data = sigreport.build(path=db, now=BASE)
    st = history.signal_stats(path=db, now=BASE)
    assert data["long"] == st["long"] and data["excluded"] == st["excluded"]
    assert data["eligible"] == st["long"] - st["excluded"] == 2   # K1 (сигнал) и K2 (пропуск), k3..k6 исключены
    assert data["missed"] == 1   # только K2 (max_signals) — реальный пропуск


def test_missed_matches_signal_stats_by_direction_and_share(tmp_path):
    db = str(tmp_path / "history.db")
    ids = {}
    ids = history.track_signals([(K1, 1.5, False, "unconfirmed"), (K2, 2.0, False, "unconfirmed")],
                                BASE - 300, ids, path=db)
    ids = history.track_signals([(K1, 1.6, False, "max_signals"), (K2, 2.0, False, "max_signals")],
                                BASE - 240, ids, path=db)
    history.track_signals([(K1, 1.6, False, "max_signals"), (K2, 2.1, False, "max_signals")], BASE, ids, path=db)
    data = sigreport.build(path=db, now=BASE)
    st = history.signal_stats(path=db, now=BASE)
    assert sum(n for _key, n in data["top_directions"]) == st["missed"] == data["missed"]
    assert data["missed_share"] == st["missed_share"]


def test_reasons_ordered_by_count_then_signal_reasons_order(tmp_path):
    db = str(tmp_path / "history.db")
    keys = [K1, K2, ("A", "USDT", "B", "USDT"), ("C", "USDT", "D", "USDT")]
    reasons = ["stale", "unconfirmed", "unconfirmed", "max_signals"]
    ids = {}
    for key, reason in zip(keys, reasons):
        ids.update(history.track_signals([(key, 1.5, False, reason)], BASE - 300, ids, path=db))
        history.track_signals([(key, 1.5, False, reason)], BASE, ids, path=db)
    data = sigreport.build(path=db, now=BASE)
    # unconfirmed (2) первым, дальше по порядку SIGNAL_REASONS среди счётчика 1: max_signals раньше stale
    assert data["reasons"] == [("unconfirmed", 2), ("max_signals", 1), ("stale", 1)]


def test_delay_median_and_p90_nearest_rank(tmp_path):
    db = str(tmp_path / "history.db")
    keys = [("E1", "USDT", "F1", "USDT"), ("E2", "USDT", "F2", "USDT"), ("E3", "USDT", "F3", "USDT")]
    delays = [5.0, 10.0, 60.0]
    ids = {}
    for key, delay in zip(keys, delays):
        ids = history.track_signals([(key, 1.5, False, "unconfirmed")], BASE - 400, ids, path=db)
        history.track_signals([(key, 1.6, True, None)], BASE - 400 + delay, ids, path=db)
    data = sigreport.build(path=db, now=BASE)
    assert data["delay_median"] == 10.0
    assert data["delay_p90"] == 60.0


def test_delay_even_sample_and_single_value(tmp_path):
    db = str(tmp_path / "history.db")
    keys = [("G1", "USDT", "H1", "USDT"), ("G2", "USDT", "H2", "USDT")]
    ids = {}
    for key, delay in zip(keys, (4.0, 8.0)):
        ids = history.track_signals([(key, 1.5, False, "unconfirmed")], BASE - 500, ids, path=db)
        history.track_signals([(key, 1.6, True, None)], BASE - 500 + delay, ids, path=db)
    data = sigreport.build(path=db, now=BASE)
    assert data["delay_median"] == 6.0
    assert data["delay_p90"] == 8.0

    db2 = str(tmp_path / "history2.db")
    ids2 = history.track_signals([(K1, 1.5, False, "unconfirmed")], BASE - 500, {}, path=db2)
    history.track_signals([(K1, 1.6, True, None)], BASE - 493, ids2, path=db2)
    data2 = sigreport.build(path=db2, now=BASE)
    assert data2["delay_median"] == 7.0 and data2["delay_p90"] == 7.0


def test_top5_directions_picks_five_of_six_ordered_desc(tmp_path, monkeypatch):
    monkeypatch.setenv("COOLDOWN", "60")   # эпизоды одного направления разносим на 300 с (> gap) — не сливаются
    db = str(tmp_path / "history.db")
    keys = [(f"EX{i}", "USDT", f"SX{i}", "USDT") for i in range(6)]
    counts = [6, 5, 4, 3, 2, 1]
    for key, n in zip(keys, counts):
        for i in range(n):
            t = BASE - 5000 + i * 300
            ids = history.track_signals([(key, 1.5, False, "unconfirmed")], t, {}, path=db)
            history.track_signals([(key, 1.5, False, "max_signals")], t + 200, ids, path=db)
    data = sigreport.build(path=db, days=30, now=BASE)
    assert len(data["top_directions"]) == 5
    top_counts = [n for _key, n in data["top_directions"]]
    assert top_counts == sorted(top_counts, reverse=True)
    by_key = dict(data["top_directions"])
    assert by_key[keys[0]] == 6
    assert keys[5] not in by_key   # у шестого направления меньше всего пропусков — не попало в топ-5


def test_clamp_days(tmp_path):
    db = str(tmp_path / "history.db")
    history.track_signals([(K1, 1.5, True, None)], BASE, {}, path=db)
    assert sigreport.build(path=db, days=0, now=BASE)["days"] == 1
    assert sigreport.build(path=db, days=999, now=BASE)["days"] == 30
    assert sigreport.build(path=db, days="мусор", now=BASE)["days"] == 7
    assert sigreport.build(path=db, days=None, now=BASE)["days"] == 7


def test_render_escapes_html_in_names(tmp_path):
    db = str(tmp_path / "history.db")
    key = ("<b>Ex</b>", "USDT", "Sell&Co", "USDT")
    ids = history.track_signals([(key, 1.5, False, "unconfirmed")], BASE - 300, {}, path=db)
    history.track_signals([(key, 1.5, False, "max_signals")], BASE, ids, path=db)
    text = sigreport.render(sigreport.build(path=db, now=BASE))
    assert "<b>Ex</b>" not in text.replace("<b>Причины", "").replace("<b>Топ", "").replace("<b>Задержка", "")
    assert "&lt;b&gt;Ex&lt;/b&gt;" in text and "Sell&amp;Co" in text


def test_signals_in_commands_help_and_not_in_guest_cmds():
    assert any(c["command"] == "signals" for c in B.COMMANDS)
    assert "/signals" not in B.GUEST_CMDS
    assert "/signals" in B.HELP_SECTIONS["signal"][1]
