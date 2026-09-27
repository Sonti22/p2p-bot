"""⭐ Избранные маршруты: кнопка на карточке, сигнал от FAV_MIN_PROFIT вне порога и топа, /fav, гостям — нет."""
from helpers import arun
import asyncio

import bot as B
import favorites
import p2p
from test_bot import Stub, texts
from test_guests import Stub as GuestStub, msg


def ad(ex, side, price, asset="USDT"):
    return p2p.Ad(ex, side, price, 1000, 500000, 10000, ["SBP"], f"{ex}-{side}", 1000, 100.0, "", asset, "", "")


def deal(profit, b_ex="HTX", s_ex="KuCoin"):
    return profit, ad(b_ex, "buy", 87.5), ad(s_ex, "sell", 89.0), f"перевод на {s_ex}"


def snap(deals):
    return p2p.Snapshot(88.0, "t", {}, {}, list(deals), {}, {}, {})


def _buttons(markup):
    return {b["callback_data"]: b["text"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b}


def _stub(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    return bot


def test_star_button_toggles_route_and_updates_card(monkeypatch):
    bot = _stub(monkeypatch)
    d = deal(2.5)
    deal_id = bot.remember_deal(d)
    assert _buttons(B.deal_markup(d, deal_id))[f"fav:{deal_id}"] == "⭐ В избранное"
    arun(bot.on_callback({"id": "1", "data": f"fav:{deal_id}", "message": {"message_id": 3}}))
    assert favorites.is_fav(("HTX", "USDT", "KuCoin", "USDT"))
    answers = [p for m, p in bot.out if m == "answerCallbackQuery"]
    assert len(answers) == 1 and "В избранном" in answers[0]["text"]          # один ответ на нажатие
    edit = [p for m, p in bot.out if m == "editMessageReplyMarkup"][-1]
    assert _buttons(edit["reply_markup"])[f"fav:{deal_id}"].startswith("★ Убрать")
    arun(bot.on_callback({"id": "2", "data": f"fav:{deal_id}", "message": {"message_id": 3}}))
    assert not favorites.is_fav(("HTX", "USDT", "KuCoin", "USDT"))


def test_favorite_route_signals_below_threshold_and_outside_top(monkeypatch):
    monkeypatch.setenv("FAV_MIN_PROFIT", "1.0")
    bot = _stub(monkeypatch)
    bot.max_signals = 1
    favorites.toggle(("Bybit", "USDT", "MEXC", "USDT"))
    top, fav_low, other_low = deal(3.0), deal(1.4, "Bybit", "MEXC"), deal(1.6, "HTX", "Bybit")
    s = snap([top, fav_low, other_low])
    bot.track_liveness(s)
    arun(bot.notify(s))
    sent = [p["caption"] for m, p in bot.out if m == "sendPhoto"]
    assert len(sent) == 2 and sent[1].startswith("⭐")        # обычный сигнал + избранный ниже порога
    assert "Bybit" in sent[1] and "MEXC" in sent[1]
    arun(bot.notify(s))                                  # антидубль: повторно не шлём
    assert len([m for m, _ in bot.out if m == "sendPhoto"]) == 2
    below = snap([deal(0.8, "Bybit", "MEXC")])                  # ниже FAV_MIN_PROFIT — нет
    bot2 = _stub(monkeypatch)
    bot2.track_liveness(below)
    arun(bot2.notify(below))
    assert not [m for m, _ in bot2.out if m == "sendPhoto"]


def test_favorite_waits_for_liveness_like_normal_signals(monkeypatch):
    bot = _stub(monkeypatch)
    bot.live_scans = 2
    favorites.toggle(("Bybit", "USDT", "MEXC", "USDT"))
    s = snap([deal(1.5, "Bybit", "MEXC")])
    bot.track_liveness(s)
    arun(bot.notify(s))
    assert not [m for m, _ in bot.out if m == "sendPhoto"]      # первый скан — ждём
    bot.track_liveness(s)
    arun(bot.notify(s))
    assert [m for m, _ in bot.out if m == "sendPhoto"]


def test_fav_list_and_remove(monkeypatch):
    bot = _stub(monkeypatch)
    arun(bot.dispatch("/fav", ""))
    assert "Избранных маршрутов нет" in texts(bot)[-1]
    favorites.toggle(("Bybit", "USDT", "MEXC", "USDT"))
    favorites.toggle(("HTX", "USDT", "KuCoin", "USDT"))
    text, kb = bot.favorites_view()
    assert "Bybit USDT → MEXC USDT" in text and "HTX USDT → KuCoin USDT" in text
    first = kb["inline_keyboard"][0][0]
    assert first["text"] == "✖ 1. Bybit USDT → MEXC USDT"
    arun(bot.on_callback({"id": "1", "data": first["callback_data"], "message": {"message_id": 4}}))
    assert favorites.keys() == {"HTX|USDT|KuCoin|USDT"}
    for data in ("favdel:1", "favdel:99"):   # кнопки с номером (до этой правки) и мимо — не удаляют, не падаем
        arun(bot.on_callback({"id": "1", "data": data, "message": {"message_id": 4}}))
    assert favorites.keys() == {"HTX|USDT|KuCoin|USDT"}


def test_guest_has_no_star_and_cannot_use_favorites(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    assert not any(k.startswith("fav:") for k in _buttons(B.deal_markup(deal(2.5))))   # у гостя нет deal_id
    bot = GuestStub(p2p.Config(), guests=["42"])
    for data in ("fav:0", "favdel:1"):
        before = len(bot.out)
        arun(bot.on_update({"callback_query": {"id": "1", "data": data,
                                                      "message": {"chat": {"id": 42}, "message_id": 5}}}))
        assert [m for m, _ in bot.out[before:]] == ["answerCallbackQuery"], data
    arun(bot.on_update(msg(42, "/fav")))
    assert bot.out[-1][1]["text"] == B.GUEST_DENIED
    assert favorites.keys() == set()
