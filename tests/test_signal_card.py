"""Понятная карточка сигнала: одни и те же номера шагов на картинке, в подписи и на кнопках (1 купить → 2… перевод,
спот → N продать), без эмодзи на картинке (в шрифте их нет — были квадраты) и без общих кнопок «Топ/Лучшая» под
сигналами (из-за них соседние карточки сливались в одну ленту)."""

import bot as B
import cards
import p2p
from helpers import arun, make_ad
from test_bot import Stub

PNG = b"\x89PNG\r\n\x1a\n"
SPOT_ROUTE = "спот BTC→USDT на Bybit (−0.1%) → перевод −1 USDT (TRC20) на HTX → запас на курс −0.3%"


def snap(deals=()):
    return p2p.Snapshot(88.0, "Rapira", {"USDT": 88.0}, {}, list(deals), {}, {}, {})


def deal(route="перевод −1 USDT (TRC20) на BestChange", terms=""):
    return (3.89, make_ad("MEXC", "buy", 87.69, pays=("T-Bank", "SBP"), orders=2412, rate=99.6, terms=terms),
            make_ad("BestChange", "sell", 91.10, pays=("Сбербанк",), orders=845), route)


def buttons(kb):
    return [b for row in kb["inline_keyboard"] for b in row]


def test_route_actions_split_actions_and_costs():
    acts, costs = p2p.route_actions(SPOT_ROUTE)
    assert acts == ["спот BTC→USDT на Bybit (−0.1%)", "перевод −1 USDT (TRC20) на HTX"]
    assert costs == ["запас на курс −0.3%"]
    # обменник сам шлёт монету — это тоже шаг владельца (указать адрес и дождаться); «внутри биржи» — не шаг
    assert p2p.route_actions("обменник шлёт USDT (TRC20) на Bybit")[0] == ["обменник шлёт USDT (TRC20) на Bybit"]
    assert p2p.route_actions("внутри биржи → комиссия банка −0.5%") == ([], ["комиссия банка −0.5%"])
    assert p2p.sell_step_number(SPOT_ROUTE) == 4 and p2p.sell_step_number("внутри биржи") == 2


def test_signal_caption_is_numbered_steps():
    d = deal(terms="Оплата только с карты на ваше ФИО, третьи лица запрещены. Чек обязателен.")
    text = p2p.fmt_signal(d, p2p.Config(amount=50000), snap())
    order = [text.index(x) for x in ("1️⃣ Купить USDT на MEXC", "2️⃣</b> Перевод −1 USDT", "3️⃣ Продать USDT на BestChange")]
    assert order == sorted(order)
    assert "на 50 000 ₽" in text and "надёжность" in text
    assert "Продавец: nick" in text and "Обменник: nick" in text and "Оплатить через: T-Bank, SBP" in text
    assert "Деньги придут на: Сбербанк" in text and "⚠️ Условия:" in text
    assert "<a href" not in text                       # ссылка на объявление — на кнопке, не в подписи
    spot = p2p.fmt_signal((2.4, make_ad("Bybit", "buy", 8_950_000, asset="BTC"), make_ad("HTX", "sell", 90.2),
                           SPOT_ROUTE), p2p.Config(), snap())
    assert "4️⃣ Продать USDT на HTX" in spot and "Уже учтено в прибыли: запас на курс −0.3%" in spot


def test_signal_caption_fits_photo_limit():
    long_terms = "Только с карты на ваше ФИО. Третьи лица запрещены. Чек обязателен. Селфи с паспортом. " * 3
    b = make_ad("MEXC", "buy", 87.69, pays=tuple(f"Bank{i}" for i in range(9)), terms=long_terms)
    s = make_ad("BestChange", "sell", 91.1, pays=tuple(f"Банк{i}" for i in range(9)), terms=long_terms)
    b.nick = s.nick = "N" * 60
    route = "спот USDT→BTC на Bybit (−0.1%) → перевод −0.0002 BTC (BTC) на MEXC → спот BTC→USDT на MEXC (−0.1%) → " \
            "перевод −1 USDT (TRC20) на BestChange → комиссия банка −0.5% → запас на курс −0.3%"
    text = p2p.fmt_signal((3.0, b, s, route), p2p.Config(), snap(), limit=600)
    assert len(text) <= 600 and "1️⃣ Купить" in text and "Продать USDT на BestChange" in text
    assert len(p2p.fmt_signal((3.0, b, s, route), p2p.Config(), snap())) <= p2p.SIGNAL_MAX


def test_buttons_numbered_like_caption_and_copy_labels():
    d = (2.4, make_ad("MEXC", "buy", 8_950_000, asset="BTC"), make_ad("HTX", "sell", 90.2), SPOT_ROUTE)
    kb = B.deal_markup(d, deal_id=1, cfg=p2p.Config(amount=50000), snap=None)
    texts = [b["text"] for b in buttons(kb)]
    assert "🟢 1. Купить на MEXC" in texts and "🔴 4. Продать на HTX" in texts
    assert "📋 Сумма: 50 000 ₽" in texts
    assert "📝 Инструкция" in texts and "🚫 Скрыть мерчанта" in texts


def test_signals_have_no_shared_nav_buttons():
    d = deal()
    assert "top" in [b.get("callback_data") for b in buttons(B.deal_markup(d, deal_id=1))]
    assert not {"top", "best"} & {b.get("callback_data") for b in buttons(B.deal_markup(d, deal_id=1, nav=False))}


def test_notify_signal_card_without_nav_best_with_nav(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=1.0))
    bot.live_scans = 1
    d = deal()
    arun(bot.notify(snap([d])))
    arun(bot.send_deal(d, snap=snap([d])))            # /best — тут общие кнопки к месту
    marks = [p["markup"] for m, p in bot.out if m == "sendPhoto"]
    cbs = [{b.get("callback_data") for b in buttons(kb)} for kb in marks]
    assert len(cbs) == 2 and "top" not in cbs[0] and "top" in cbs[1]


def test_card_strips_emoji_and_fits_long_price():
    assert cards._plain("🔥CryptoKing🔥 ✅") == "CryptoKing"
    assert cards._plain("⚠️ риск") == "риск" and cards._plain("Т-Банк → СБП ₽") == "Т-Банк → СБП ₽"
    b = make_ad("Bybit", "buy", 8_950_000.0, asset="BTC", terms="чек обязателен")
    b.nick = "BTC_trader 🚀"
    d = (2.4, b, make_ad("HTX", "sell", 90.2), SPOT_ROUTE)
    png = cards.deal_card(d, p2p.Config(), {10000: 2.5, 100000: None}, ("⚠️ риск", ["мерчант у порога"], 8))
    assert png[:8] == PNG
    assert cards.deal_card(deal(route="внутри биржи"), p2p.Config())[:8] == PNG
