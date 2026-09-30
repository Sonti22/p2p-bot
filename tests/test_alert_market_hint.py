"""Подсказка о текущем рынке и опечатке в курсе в ответе на /alert (bot.alert_market_hint)."""
import dataclasses
import functools

import bot as B
import p2p
from test_bot import Stub, texts
from helpers import arun, make_ad


def snap(best):
    return p2p.Snapshot(88.0, "test", {}, best, [], {}, {}, {})


def test_hint_shows_best_price_no_flags():
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4)}
    hint = B.alert_market_hint(snap(best), "USDT", "sell", 92, extra=False)
    assert "91.40" in hint and "Bybit" in hint
    assert "⚡" not in hint and "⚠️" not in hint


def test_hint_sell_picks_max_price_buy_picks_min_price():
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4),
            ("MEXC", "sell", "USDT"): make_ad("MEXC", "sell", 91.8)}
    hint = B.alert_market_hint(snap(best), "USDT", "sell", 92, extra=False)
    assert "91.80" in hint and "MEXC" in hint
    best_buy = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 91.4),
                ("MEXC", "buy", "USDT"): make_ad("MEXC", "buy", 90.9)}
    hint2 = B.alert_market_hint(snap(best_buy), "USDT", "buy", 92, extra=False)
    assert "90.90" in hint2 and "MEXC" in hint2


def test_hint_condition_already_met():
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4)}
    hint = B.alert_market_hint(snap(best), "USDT", "sell", 90, extra=False)
    assert "⚡ Условие уже выполнено" in hint
    best_buy = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 91.4)}
    hint2 = B.alert_market_hint(snap(best_buy), "USDT", "buy", 93, extra=False)
    assert "⚡ Условие уже выполнено" in hint2


def test_hint_condition_met_with_extra_conditions():
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4)}
    hint = B.alert_market_hint(snap(best), "USDT", "sell", 90, extra=True)
    assert "остальные условия (vol/reliable)" in hint
    assert "сработает на ближайшем скане." not in hint   # без оговорки — другая фраза


def test_hint_far_from_market():
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4)}
    hint = B.alert_market_hint(snap(best), "USDT", "sell", 920, extra=False)
    assert "⚠️" in hint and "дальше рынка" in hint
    best_buy = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 91.4)}
    hint2 = B.alert_market_hint(snap(best_buy), "USDT", "buy", 9.2, extra=False)
    assert "⚠️" in hint2 and "дальше рынка" in hint2


def test_hint_light_side_of_market():
    best = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4)}
    hint = B.alert_market_hint(snap(best), "USDT", "sell", 9.2, extra=False)
    assert "⚡" in hint
    assert "⚠️" in hint and "ниже рынка" in hint and "Сработает сразу" in hint
    best_buy = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 91.4)}
    hint2 = B.alert_market_hint(snap(best_buy), "USDT", "buy", 930, extra=False)
    assert "⚠️" in hint2 and "выше рынка" in hint2


def test_hint_ignores_stale_ads_and_other_asset_or_side():
    stale = dataclasses.replace(make_ad("Bybit", "sell", 91.4), stale=True)
    assert B.alert_market_hint(snap({("Bybit", "sell", "USDT"): stale}), "USDT", "sell", 92) == ""
    best = {("Bybit", "sell", "BTC"): make_ad("Bybit", "sell", 91.4, asset="BTC")}
    assert B.alert_market_hint(snap(best), "USDT", "sell", 92) == ""
    best2 = {("Bybit", "buy", "USDT"): make_ad("Bybit", "buy", 91.4)}
    assert B.alert_market_hint(snap(best2), "USDT", "sell", 92) == ""
    assert B.alert_market_hint(None, "USDT", "sell", 92) == ""
    best3 = {("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 0.0)}
    assert B.alert_market_hint(snap(best3), "USDT", "sell", 92) == ""


def test_add_alert_without_snapshot_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    assert bot.last is None
    arun(bot.add_alert("USDT sell 92 7d"))
    text = texts(bot)[-1]
    assert text == "🔔 Алерт создан: USDT продать ≥92 ₽, срок 7d. Список — /alerts."
    assert "Сейчас лучшая цена" not in text


def test_add_alert_with_snapshot_includes_market_line(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    bot.last = snap({("Bybit", "sell", "USDT"): make_ad("Bybit", "sell", 91.4)})
    arun(bot.add_alert("USDT sell 92 7d vol 50000 reliable repeat 1h"))
    rows = B.alerts.list_all("1", path=db)
    assert len(rows) == 1
    text = texts(bot)[-1]
    assert "Алерт создан" in text and "повтор" in text and "объём" in text and "надёжность" in text
    assert "Сейчас лучшая цена" in text
    assert len([m for m, p in bot.out if m == "sendMessage"]) == 1
