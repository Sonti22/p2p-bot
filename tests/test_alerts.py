import time

import alerts
import p2p
from helpers import make_ad


def snap(best):
    return p2p.Snapshot(88.0, "test", {}, best, [], {}, {}, {})


def test_parse_duration():
    assert alerts.parse_duration("7d") == 7 * 86400
    assert alerts.parse_duration("12h") == 12 * 3600
    assert alerts.parse_duration("2w") == 2 * 7 * 86400
    assert alerts.parse_duration("0d") is None
    assert alerts.parse_duration("91d") is None
    assert alerts.parse_duration("bad") is None


def test_add_list_and_remove(tmp_path):
    db = str(tmp_path / "alerts.db")
    alert_id = alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    rows = alerts.list_all("1", path=db)
    assert [(a, s, r) for _, a, s, r, _ in rows] == [("USDT", "sell", 92.0)]
    assert alerts.list_all("2", path=db) == []   # чужой чат не видит
    alerts.remove(alert_id, "1", path=db)
    assert alerts.list_all("1", path=db) == []


def test_remove_wrong_chat_keeps_alert(tmp_path):
    db = str(tmp_path / "alerts.db")
    alert_id = alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    alerts.remove(alert_id, "2", path=db)   # чужой чат не может удалить
    assert len(alerts.list_all("1", path=db)) == 1


def test_list_prunes_expired(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() - 1, path=db)
    assert alerts.list_all("1", path=db) == []


def test_due_fires_on_sell_threshold(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 90.0),
            ("MEXC", "sell", "USDT"): make_ad("MEXC", "sell", 93.0)}
    fired = alerts.due(snap(best), path=db)
    assert len(fired) == 1
    alert_id, chat_id, asset, side, rate, price, ad = fired[0]
    assert (chat_id, asset, side, rate, price, ad.ex) == ("1", "USDT", "sell", 92.0, 93.0, "MEXC")
    assert alerts.list_all("1", path=db) == []   # одноразовый — сработал и удалился


def test_due_not_fired_below_threshold(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.0)}
    assert alerts.due(snap(best), path=db) == []
    assert len(alerts.list_all("1", path=db)) == 1   # не сработал — остаётся


def test_due_buy_side_wants_low_price(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "buy", 85.0, time.time() + 86400, path=db)
    best = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 86.0),
            ("MEXC", "buy", "USDT"): make_ad("MEXC", "buy", 84.0)}
    fired = alerts.due(snap(best), path=db)
    assert len(fired) == 1
    alert_id, chat_id, asset, side, rate, price, ad = fired[0]
    assert (price, ad.ex) == (84.0, "MEXC")


def test_due_ignores_other_asset_and_side(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    best = {("Bybit", "sell", "BTC"): make_ad("Bybit", "sell", 999999.0, asset="BTC"),
            ("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 999.0)}
    assert alerts.due(snap(best), path=db) == []


def test_due_expired_alert_not_fired(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() - 1, path=db)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 99.0)}
    assert alerts.due(snap(best), path=db) == []
