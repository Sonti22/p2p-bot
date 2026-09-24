import time

import alerts
import p2p
from helpers import make_ad


def snap(best, groups=None, deals=None):
    return p2p.Snapshot(88.0, "test", {}, best, deals or [], {}, {}, {}, groups=groups or {})


def cfg():
    return p2p.Config()


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
    assert [(a, s, r, c, v, rl) for _, a, s, r, _, c, v, rl in rows] == \
        [("USDT", "sell", 92.0, None, None, 0)]
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
    fired = alerts.due(snap(best), cfg(), path=db)
    assert len(fired) == 1
    alert_id, chat_id, asset, side, rate, price, ad = fired[0]
    assert (chat_id, asset, side, rate, price, ad.ex) == ("1", "USDT", "sell", 92.0, 93.0, "MEXC")
    assert alerts.list_all("1", path=db) == []   # одноразовый — сработал и удалился


def test_due_not_fired_below_threshold(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.0)}
    assert alerts.due(snap(best), cfg(), path=db) == []
    assert len(alerts.list_all("1", path=db)) == 1   # не сработал — остаётся


def test_due_buy_side_wants_low_price(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "buy", 85.0, time.time() + 86400, path=db)
    best = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 86.0),
            ("MEXC", "buy", "USDT"): make_ad("MEXC", "buy", 84.0)}
    fired = alerts.due(snap(best), cfg(), path=db)
    assert len(fired) == 1
    alert_id, chat_id, asset, side, rate, price, ad = fired[0]
    assert (price, ad.ex) == (84.0, "MEXC")


def test_due_ignores_other_asset_and_side(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    best = {("Bybit", "sell", "BTC"): make_ad("Bybit", "sell", 999999.0, asset="BTC"),
            ("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 999.0)}
    assert alerts.due(snap(best), cfg(), path=db) == []


def test_due_expired_alert_not_fired(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() - 1, path=db)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 99.0)}
    assert alerts.due(snap(best), cfg(), path=db) == []


def test_due_repeat_survives_firing_and_shows_in_list(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, repeat_cooldown=3600)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 93.0)}
    fired = alerts.due(snap(best), cfg(), path=db)
    assert len(fired) == 1
    rows = alerts.list_all("1", path=db)
    assert [(a, s, r, c) for _, a, s, r, _, c, _, _ in rows] == [("USDT", "sell", 92.0, 3600)]   # не удалился


def test_due_repeat_waits_out_cooldown(tmp_path):
    db = str(tmp_path / "alerts.db")
    now = time.time()
    alerts.add("1", "USDT", "sell", 92.0, now + 86400, path=db, repeat_cooldown=3600)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 93.0)}
    assert len(alerts.due(snap(best), cfg(), path=db, now=now)) == 1
    assert alerts.due(snap(best), cfg(), path=db, now=now + 100) == []   # кулдаун ещё не прошёл
    assert len(alerts.due(snap(best), cfg(), path=db, now=now + 3601)) == 1   # кулдаун прошёл — сработал снова


# условия через «И»: объём стакана и надёжность встречной связки

def test_due_volume_condition_not_met(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, min_volume=100_000)
    ad = make_ad("Bybit", "sell", 93.0, max_amt=40_000)
    best = {("Bybit", "sell", "USDT"): ad}
    groups = {("Bybit", "sell", "USDT"): [ad]}
    assert alerts.due(snap(best, groups=groups), cfg(), path=db) == []   # в стакане только 40 000 < 100 000


def test_due_volume_condition_met_by_stacking_several_ads(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, min_volume=100_000)
    top = make_ad("Bybit", "sell", 95.0, max_amt=60_000)
    second = make_ad("Bybit", "sell", 93.0, max_amt=60_000)
    best = {("Bybit", "sell", "USDT"): top}
    groups = {("Bybit", "sell", "USDT"): [top, second]}   # вместе 120 000 ₽ — хватает
    assert len(alerts.due(snap(best, groups=groups), cfg(), path=db)) == 1


def test_due_volume_condition_ignores_ads_below_rate(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, min_volume=100_000)
    top = make_ad("Bybit", "sell", 93.0, max_amt=60_000)
    cheap = make_ad("Bybit", "sell", 80.0, max_amt=60_000)   # цена хуже порога — в объём не считается
    best = {("Bybit", "sell", "USDT"): top}
    groups = {("Bybit", "sell", "USDT"): [top, cheap]}
    assert alerts.due(snap(best, groups=groups), cfg(), path=db) == []


def test_due_volume_condition_counts_exchanger_network_only(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, min_volume=100_000)
    trc = make_ad("BestChange", "sell", 95.0, net="TRC20", max_amt=60_000)
    erc = make_ad("BestChange", "sell", 94.0, net="ERC20", max_amt=60_000)
    best = {("BestChange", "sell", "USDT"): trc}
    groups = {("BestChange", "sell", "USDT"): [trc, erc]}   # 120 000 ₽ только в сумме разных сетей
    assert alerts.due(snap(best, groups=groups), cfg(), path=db) == []
    groups[("BestChange", "sell", "USDT")].append(make_ad("BestChange", "sell", 93.0, net="TRC20", max_amt=60_000))
    assert len(alerts.due(snap(best, groups=groups), cfg(), path=db)) == 1   # в TRC20 набирается 120 000


def test_due_reliable_condition_blocks_trap_deal(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, require_reliable=True)
    buy_ad = make_ad("MEXC", "buy", 85.0, orders=1)     # мерчант у порога фильтра — причина риска
    sell_ad = make_ad("Bybit", "sell", 93.0, orders=1)  # аналогично
    best = {("Bybit", "sell", "USDT"): sell_ad}
    deal = (10.0, buy_ad, sell_ad, "внутри биржи")   # profit>=5 даёт третью причину — «ловушка»
    assert alerts.due(snap(best, deals=[deal]), cfg(), path=db) == []


def test_due_reliable_condition_allows_risky_deal(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, require_reliable=True)
    buy_ad = make_ad("MEXC", "buy", 85.0)
    sell_ad = make_ad("Bybit", "sell", 93.0)
    best = {("Bybit", "sell", "USDT"): sell_ad}
    deal = (10.0, buy_ad, sell_ad, "внутри биржи")   # только одна причина (profit>=5) — «риск», не «ловушка»
    assert len(alerts.due(snap(best, deals=[deal]), cfg(), path=db)) == 1


def test_due_reliable_condition_no_matching_deal_blocks(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db, require_reliable=True)
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 93.0)}
    assert alerts.due(snap(best), cfg(), path=db) == []   # нечем подтвердить надёжность — не срабатывает
