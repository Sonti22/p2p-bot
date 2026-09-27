import datetime
import sqlite3

import history
import p2p
from helpers import make_ad

# Понедельник, 07:00 UTC = 10:00 МСК — не задевает границу суток при +3ч.
BASE = datetime.datetime(2026, 1, 5, 7, 0, 0, tzinfo=datetime.timezone.utc).timestamp()


def snap(deals, ref=88.0):
    return p2p.Snapshot(ref, "test", {}, {}, deals, {}, {}, {})


def reset(monkeypatch, t):
    history._last["t"] = 0.0
    clock = {"t": t}
    monkeypatch.setattr(history.time, "time", lambda: clock["t"])
    return clock


def test_record_writes_best_profit_per_deal_key_with_coins(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    reset(monkeypatch, BASE)
    deals = [
        (3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "route1"),
        (5.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 92.0, asset="USDC"), "route2"),
        (2.0, make_ad("HTX", "buy", 80.0), make_ad("BestChange", "sell", 95.0), "route3"),
        (1.0, make_ad("Bybit", "buy", 86.0), make_ad("MEXC", "sell", 89.0), "route4"),   # та же связка хуже
    ]
    assert history.record(snap(deals), path=db) is True
    con = sqlite3.connect(db)
    rows = con.execute("SELECT buy_ex, asset_buy, sell_ex, asset_sell, profit, ref FROM history "
                       "ORDER BY sell_ex, asset_sell").fetchall()
    con.close()
    assert rows == [("HTX", "USDT", "BestChange", "USDT", 2.0, 88.0), ("Bybit", "USDT", "MEXC", "USDC", 5.0, 88.0),
                    ("Bybit", "USDT", "MEXC", "USDT", 3.0, 88.0)]                    # лучший на каждую связку


def test_aggregations_take_best_coin_per_exchange_pair(tmp_path, monkeypatch):
    """Строк на пару площадок в одну запись теперь несколько (по монетам) — агрегации считают, как раньше, по лучшей."""
    db = str(tmp_path / "history.db")
    reset(monkeypatch, BASE)
    deals = [(3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "r"),
             (5.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 92.0, asset="USDC"), "r")]
    assert history.record(snap(deals), 50000, path=db) is True
    history._insert([(BASE - 3600, "Bybit", "MEXC", "USDT", "USDT", 1.0, 88.0)], db)   # старая запись: пара одной строкой
    assert history.hourly_avg(path=db, now=BASE + 60)[10] == 5.0
    assert history.hourly_avg(path=db, now=BASE + 60)[9] == 1.0
    bt = history.backtest(2.0, 50000, path=db, now=BASE + 60)[7]
    assert [(r["buy_ex"], r["sell_ex"], r["hits"], r["total"], r["avg"]) for r in bt] == [("Bybit", "MEXC", 1, 2, 5.0)]
    _labels, p2p_med, _bc = history.median_vs_bestchange(path=db, now=BASE + 60)
    assert p2p_med == [3.0]                                                          # медиана из 1.0 и 5.0


# --- signals: эпизоды связок выше порога ---

K1 = ("Bybit", "USDT", "MEXC", "USDT")
K2 = ("HTX", "USDT", "BestChange", "USDT")


def _signals(db):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT buy_ex, buy_asset, sell_ex, sell_asset, first_seen, last_seen, scans, max_profit, "
                       "signalled, signal_ts, reason_not_signalled, amount, min_profit FROM signals ORDER BY id").fetchall()
    con.close()
    return rows


def test_track_signals_episode_lifecycle(tmp_path):
    db = str(tmp_path / "history.db")
    open_ids = history.track_signals([(K1, 1.5, False, "unconfirmed"), (K2, 2.0, False, "max_signals")], 100.0, {},
                                     amount=50000, min_profit=1.0, path=db)
    assert set(open_ids) == {K1, K2}
    open_ids = history.track_signals([(K1, 2.5, True, None), (K2, 1.8, False, "cooldown")], 120.0, open_ids,
                                     amount=50000, min_profit=1.0, path=db)
    open_ids = history.track_signals([(K1, 2.0, False, "cooldown")], 140.0, open_ids, amount=50000, min_profit=1.0,
                                     path=db)
    assert set(open_ids) == {K1}                                                     # K2 ушла под порог — эпизод закрыт
    rows = _signals(db)
    assert rows == [(*K1, 100.0, 140.0, 3, 2.5, 1, 120.0, None, 50000.0, 1.0),     # сигнал был — причины нет
                    (*K2, 100.0, 120.0, 2, 2.0, 0, None, "cooldown", 50000.0, 1.0)]   # последняя преграда
    history.track_signals([(K2, 1.1, False, "quiet")], 160.0, open_ids, path=db)     # вернулась — новый эпизод
    rows = _signals(db)
    assert len(rows) == 3 and rows[2][:7] == (*K2, 160.0, 160.0, 1) and rows[2][10] == "quiet"


def test_track_signals_row_gone_starts_new_episode(tmp_path):
    db = str(tmp_path / "history.db")
    open_ids = history.track_signals([(K1, 1.5, False, "unconfirmed")], 100.0, {}, path=db)
    history.cleanup(path=db, now=100.0 + history.RETENTION + 1)                     # строку удалила очистка
    open_ids = history.track_signals([(K1, 1.7, True, None)], 120.0, open_ids, path=db)
    rows = _signals(db)
    assert len(rows) == 1 and rows[0][4:11] == (120.0, 120.0, 1, 1.7, 1, 120.0, None)


def test_cleanup_drops_old_signal_episodes(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    history.track_signals([(K1, 1.5, False, "paused")], now - 31 * 86400, {}, path=db)
    history.track_signals([(K2, 1.5, False, "paused")], now - 1 * 86400, {}, path=db)
    history.cleanup(path=db, now=now)
    assert [r[:4] for r in _signals(db)] == [K2]


def test_signal_stats_missed_share(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE
    ids = {}
    # K1: 5 минут выше порога, сигнал был; K2: 5 минут без сигнала (кулдаун) — пропуск; k3: минута — не в счёт
    k3 = ("KuCoin", "USDT", "MEXC", "USDT")
    ids = history.track_signals([(K1, 1.5, False, "unconfirmed"), (K2, 2.0, False, "unconfirmed"),
                                 (k3, 1.2, False, "unconfirmed")], now - 300, ids, path=db)
    ids = history.track_signals([(K1, 1.6, True, None), (K2, 2.0, False, "cooldown"), (k3, 1.2, False, "unconfirmed")],
                                now - 240, ids, path=db)
    ids = history.track_signals([(K1, 1.6, False, "cooldown"), (K2, 2.1, False, "cooldown")], now, ids, path=db)
    st = history.signal_stats(path=db, now=now)
    assert st == {"episodes": 3, "signalled": 1, "long": 2, "missed": 1, "missed_share": 0.5,
                  "reasons": {"cooldown": 1}}
    empty = history.signal_stats(path=str(tmp_path / "none.db"), now=now)
    assert empty["episodes"] == 0 and empty["missed_share"] is None


def test_record_stores_amount_next_to_profit(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    reset(monkeypatch, BASE)
    d = make_ad("Bybit", "buy", 85.0)
    s = make_ad("MEXC", "sell", 90.0)
    assert history.record(snap([(3.0, d, s, "route")]), 75000, path=db) is True
    con = sqlite3.connect(db)
    amount, = con.execute("SELECT amount FROM history").fetchone()
    con.close()
    assert amount == 75000


def test_record_false_when_no_deals_and_does_not_spend_throttle(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    clock = reset(monkeypatch, BASE)
    assert history.record(snap([]), path=db) is False
    d = make_ad("Bybit", "buy", 85.0)
    s = make_ad("MEXC", "sell", 90.0)
    clock["t"] += 1   # почти сразу — пустой скан не должен был занять кулдаун
    assert history.record(snap([(3.0, d, s, "route")]), path=db) is True


def test_record_throttles_five_minutes(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    clock = reset(monkeypatch, BASE)
    d = make_ad("Bybit", "buy", 85.0)
    s = make_ad("MEXC", "sell", 90.0)
    one = snap([(3.0, d, s, "route")])
    assert history.record(one, path=db) is True
    clock["t"] += 60           # через минуту — рано
    assert history.record(one, path=db) is False
    clock["t"] = BASE + 300    # ровно порог в 5 минут — можно снова
    assert history.record(one, path=db) is True
    con = sqlite3.connect(db)
    n, = con.execute("SELECT COUNT(*) FROM history").fetchone()
    con.close()
    assert n == 2


def test_cleanup_deletes_older_than_30_days(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    history._insert([(now - 31 * 86400, "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0),
                     (now - 1 * 86400, "Bybit", "MEXC", "USDT", "USDT", 4.0, 88.0)], db)
    removed = history.cleanup(db, now=now)
    assert removed == 1
    con = sqlite3.connect(db)
    left = con.execute("SELECT profit FROM history").fetchall()
    con.close()
    assert left == [(4.0,)]


def test_cleanup_missing_file_is_noop():
    assert history.cleanup("Z:/does/not/exist.db") == 0


def test_is_empty(tmp_path):
    db = str(tmp_path / "history.db")
    assert history.is_empty(db) is True
    history._insert([(BASE, "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0)], db)
    assert history.is_empty(db) is False


def test_hourly_avg_groups_by_msk_hour(tmp_path):
    db = str(tmp_path / "history.db")
    rows = [
        (BASE, "Bybit", "MEXC", "USDT", "USDT", 1.0, 88.0),           # 10:00 МСК, день 0
        (BASE + 86400, "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0),   # 10:00 МСК, день 1 -> avg 2.0
        (BASE + 5 * 3600, "HTX", "BestChange", "USDT", "USDT", 5.0, 88.0),  # 15:00 МСК
    ]
    history._insert(rows, db)
    avg = history.hourly_avg(db, days=7, now=BASE + 2 * 86400)
    assert avg[10] == 2.0
    assert avg[15] == 5.0
    assert avg[11] is None


def test_hourly_avg_ignores_data_outside_window(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    history._insert([(now - 20 * 86400, "Bybit", "MEXC", "USDT", "USDT", 9.0, 88.0)], db)
    avg = history.hourly_avg(db, days=7, now=now)
    assert all(v is None for v in avg.values())   # старше 7 дней — не попадает в окно


def test_heatmap_takes_max_per_cell(tmp_path):
    db = str(tmp_path / "history.db")
    rows = [
        (BASE, "Bybit", "MEXC", "USDT", "USDT", 2.0, 88.0),           # понедельник 10:00
        (BASE + 86400 * 7, "Bybit", "MEXC", "USDT", "USDT", 6.0, 88.0),  # тоже понедельник 10:00, через неделю
    ]
    history._insert(rows, db)
    grid = history.heatmap(db, days=8, now=BASE + 8 * 86400)
    assert grid[(0, 10)] == 6.0   # максимум из двух записей
    assert (0, 11) not in grid


def test_median_vs_bestchange_separates_groups_by_day(tmp_path):
    db = str(tmp_path / "history.db")
    day0 = BASE
    day1 = BASE + 86400
    rows = [
        (day0, "Bybit", "MEXC", "USDT", "USDT", 2.0, 88.0),
        (day0 + 3600, "HTX", "KuCoin", "USDT", "USDT", 4.0, 88.0),          # день0, p2p: медиана 3.0
        (day0 + 7200, "Bybit", "BestChange", "USDT", "USDT", 1.0, 88.0),    # день0, bc: медиана 1.0
        (day1, "Bybit", "MEXC", "USDT", "USDT", 5.0, 88.0),                 # день1, p2p: медиана 5.0, bc нет
    ]
    history._insert(rows, db)
    labels, p2p_med, bc_med = history.median_vs_bestchange(db, days=7, now=day1 + 86400)
    assert len(labels) == 2
    assert p2p_med == [3.0, 5.0]
    assert bc_med == [1.0, None]


def test_median_vs_bestchange_empty_db():
    labels, p2p_med, bc_med = history.median_vs_bestchange("Z:/does/not/exist.db")
    assert labels == [] and p2p_med == [] and bc_med == []


def test_backtest_counts_hits_and_computes_avg_median(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    rows = [
        (now - 1 * 86400, "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0),   # >= порога 2%
        (now - 2 * 86400, "Bybit", "MEXC", "USDT", "USDT", 5.0, 88.0),   # >= порога
        (now - 3 * 86400, "Bybit", "MEXC", "USDT", "USDT", 1.0, 88.0),   # < порога — не попадает в hits
        (now - 1 * 86400, "HTX", "KuCoin", "USDT", "USDT", 10.0, 88.0),  # другая пара, 1 попадание за 7д
        (now - 20 * 86400, "HTX", "KuCoin", "USDT", "USDT", 10.0, 88.0),  # видно только за 30д
    ]
    history._insert(rows, db)
    data = history.backtest(2.0, 50000, path=db, now=now)
    by_pair_7 = {(r["buy_ex"], r["sell_ex"]): r for r in data[7]}
    bybit_mexc = by_pair_7[("Bybit", "MEXC")]
    assert bybit_mexc["hits"] == 2 and bybit_mexc["total"] == 3
    assert bybit_mexc["avg"] == 4.0 and bybit_mexc["median"] == 4.0
    assert bybit_mexc["est_rub"] == 4.0 / 100 * 50000
    assert by_pair_7[("HTX", "KuCoin")]["hits"] == 1   # только запись за последние 7 дней
    by_pair_30 = {(r["buy_ex"], r["sell_ex"]): r for r in data[30]}
    assert by_pair_30[("HTX", "KuCoin")]["hits"] == 2   # обе записи попадают в окно 30 дней
    # топ отсортирован по числу попаданий, лучшая пара первой
    assert data[7][0]["hits"] >= data[7][-1]["hits"]


def test_backtest_uses_stored_amount_not_fallback(tmp_path):
    """У записи есть своя сумма круга (amount) — она определяла % на момент записи, поэтому оценку
    в рублях считаем по ней, а не по сумме, переданной в backtest() (та — только запасной вариант
    для старых записей без сохранённой суммы)."""
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    history._insert([(now - 1 * 86400, "Bybit", "MEXC", "USDT", "USDT", 4.0, 88.0, 100000)], db)
    data = history.backtest(2.0, 50000, path=db, now=now)
    row = data[7][0]
    assert row["est_rub"] == 4.0 / 100 * 100000


def test_backtest_falls_back_to_passed_amount_for_old_rows(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    history._insert([(now - 1 * 86400, "Bybit", "MEXC", "USDT", "USDT", 4.0, 88.0)], db)   # без amount — NULL
    data = history.backtest(2.0, 50000, path=db, now=now)
    row = data[7][0]
    assert row["est_rub"] == 4.0 / 100 * 50000


def test_backtest_pair_without_hits_is_excluded(tmp_path):
    db = str(tmp_path / "history.db")
    now = BASE + 40 * 86400
    history._insert([(now - 1 * 86400, "Bybit", "MEXC", "USDT", "USDT", 0.5, 88.0)], db)   # ниже порога
    data = history.backtest(2.0, 50000, path=db, now=now)
    assert data[7] == [] and data[30] == []


def test_backtest_empty_history_is_empty_lists(tmp_path):
    db = str(tmp_path / "history.db")
    data = history.backtest(1.0, 50000, path=db)
    assert data == {7: [], 30: []}
