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


def test_record_writes_best_profit_per_exchange_pair(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    reset(monkeypatch, BASE)
    deals = [
        (3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "route1"),
        (5.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 92.0, asset="USDC"), "route2"),
        (2.0, make_ad("HTX", "buy", 80.0), make_ad("BestChange", "sell", 95.0), "route3"),
    ]
    assert history.record(snap(deals), path=db) is True
    con = sqlite3.connect(db)
    rows = con.execute("SELECT buy_ex, sell_ex, profit, ref FROM history ORDER BY sell_ex").fetchall()
    con.close()
    assert rows == [("HTX", "BestChange", 2.0, 88.0), ("Bybit", "MEXC", 5.0, 88.0)]   # выбран лучший из пары


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
