"""P2P LBank: разбор ответа advertisementList (фикстуры — урезанные живые ответы RUB), ссылки, подключение площадки."""
import json
import os

import pytest

import bot
import cards
import p2p
import trades
from helpers import arun, make_ad

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _run(side, asset="USDT"):
    return arun(p2p.lbank(None, p2p.Config(), side, asset))


def test_lbank_buy_skips_mirrored_mexc_ads(offline):
    """Стакан RUB на покупку у LBank сейчас — объявления MEXC, которые LBank показывает у себя (source=MEXC). Они уже
    есть в стакане MEXC (дубли связок), а блэклист MEXC их под площадкой LBank не узнал бы — не берём. В фикстуре
    одно своё объявление LBank (own_lbank_seller, добавлено для тестов) — только оно и остаётся."""
    assert [a.nick for a in _run("buy")] == ["own_lbank_seller"]


def test_lbank_own_ad_fields_and_broken_item(monkeypatch):
    with open(os.path.join(FIX, "lbank_ads.json"), encoding="utf-8") as f:
        item = next(i for i in json.load(f)["data"]["resultList"] if i["nickName"] == "1-Переводом")
    item = dict(item, source="LBANK")
    broken = dict(item, price=None, nickName="broken")   # одно кривое объявление не роняет всю сторону

    async def fake(s, method, url, body=None):
        return {"code": 200, "data": {"resultList": [broken, item]}}

    monkeypatch.setattr(p2p, "_json", fake)
    ads = _run("buy")
    assert len(ads) == 1
    first = ads[0]
    assert (first.ex, first.side, first.asset, first.nick) == ("LBank", "buy", "USDT", "1-Переводом")
    assert (first.price, first.min_amt, first.max_amt, first.avail) == (86.8, 92400.0, 92401.0, 3346.3502)
    assert first.pays == ["SBP - Fast Bank Transfer"] and trades.is_sbp(first.pays[0])
    assert (first.orders, first.rate) == (0, 0.0)
    assert first.terms.startswith("ВАЖНО!")                     # adRemark без ведущих пробелов


def test_transfer_from_venue_without_directory_respects_closed_deposit(monkeypatch):
    """У LBank/BitPapa нет справочника сетей — но если у получателя ввод закрыт во всех сетях, маршрута нет
    (раньше бралась запасная комиссия, и связка «LBank → HTX» проходила при закрытом вводе HTX)."""
    cfg = p2p.Config()
    closed = {"TRC20": {"wd": True, "dep": False}, "BEP20": {"wd": True, "dep": False}}
    monkeypatch.setitem(p2p.netstatus.STATUS, ("HTX", "USDT"), closed)
    assert p2p._withdraw(cfg, "LBank", "USDT", receiver="HTX") is None
    assert p2p._withdraw(cfg, "BitPapa", "USDT", receiver="HTX") is None
    monkeypatch.setitem(p2p.netstatus.STATUS, ("HTX", "USDT"), dict(closed, BEP20={"wd": True, "dep": True}))
    assert p2p._withdraw(cfg, "LBank", "USDT", receiver="HTX") == (cfg.transfer_fees["USDT"], "")


def test_lbank_skips_new_user_flash_sale(offline):
    """«New User Flash Sale»: 1 USDT по 10 ₽ только для первой сделки новичка — не рыночная цена, в стакан не берём."""
    with open(os.path.join(FIX, "lbank_ads.json"), encoding="utf-8") as f:
        raw = [i["nickName"] for i in json.load(f)["data"]["resultList"]]
    assert "ANTRADER" in raw
    assert "ANTRADER" not in {a.nick for a in _run("buy")}
    assert all(a.price > 80 for a in _run("buy"))


def test_lbank_sell_reads_merchant_stats_and_fixes_alfa(offline):
    """Бот продаёт: объявления покупателей LBank. Сделки — dealOrderTotal, % завершения — turnoverRateTotal
    (его LBank показывает как «Completion»); «AIfa-bank» (заглавная I) приводится к Alfa-bank."""
    ads = {a.nick: a for a in _run("sell")}
    assert all(a.side == "sell" for a in ads.values())
    ko = ads["ko****"]
    assert (ko.price, ko.orders, ko.rate, ko.pays) == (70.0, 2, 66.0, ["OZON Bank"])
    dimasik = ads["$~DIMASIK~$"]
    assert "Alfa-bank" in dimasik.pays and "AIfa-bank" not in dimasik.pays
    assert {trades.bank_of(p) for p in dimasik.pays} >= {"Alfa-bank", "Sberbank", "T-Bank", "VTB", "Ozon Bank"}


def test_lbank_merchants_without_stats_do_not_pass_default_filters(offline):
    """Мерчанты без истории (как почти весь стакан RUB на LBank сейчас) отсеиваются обычными MIN_ORDERS/MIN_RATE."""
    cfg = p2p.Config()
    assert not [a for a in _run("buy") + _run("sell") if p2p.usable(a, cfg)]


def test_lbank_unsupported_coin_makes_no_request(monkeypatch):
    async def no_network(*a, **kw):
        raise AssertionError("LBank не должен запрашивать монету, которой нет в его P2P")

    monkeypatch.setattr(p2p, "_json", no_network)
    for asset in ("BTC", "ETH", "TON"):
        assert _run("buy", asset) == []


def test_lbank_usdc_is_requested(offline):
    assert all(a.asset == "USDC" for a in _run("sell", "USDC"))


def test_lbank_error_code_raises(monkeypatch):
    """Ответ с ошибкой (не code=200) — исключение, а не пустой стакан: scan() запишет ошибку площадки и включит паузу."""
    async def error(s, method, url, body=None):
        return {"code": 10009, "message": "Too many requests", "data": None}

    monkeypatch.setattr(p2p, "_json", error)
    with pytest.raises(ValueError, match="LBank"):
        _run("buy")


def test_lbank_request_side_and_fiat(monkeypatch):
    urls = []

    async def capture(s, method, url, body=None):
        urls.append(url)
        return {"code": 200, "data": {"resultList": []}}

    monkeypatch.setattr(p2p, "_json", capture)
    _run("buy")
    _run("sell", "USDC")
    assert "tradeType=buy&assetCode=USDT&currencyCode=RUB" in urls[0]
    assert "tradeType=sell&assetCode=USDC&currencyCode=RUB" in urls[1]


def test_lbank_venue_url():
    buy, sell = make_ad(ex="LBank", side="buy"), make_ad(ex="LBank", side="sell", asset="USDC")
    assert p2p.venue_url(buy) == "https://www.lbank.com/crypto/buy?assetCode=USDT&currencyCode=RUB"
    assert p2p.venue_url(sell) == "https://www.lbank.com/crypto/sell?assetCode=USDC&currencyCode=RUB"


def test_every_venue_is_wired():
    """Новая площадка: адаптер в FETCHERS, имя в bot.EXCHANGE_NAMES (кнопки фильтров, /maker, /banks), цвет на карточке."""
    for ex in p2p.ALL_EXCHANGES.split(","):
        assert ex in p2p.FETCHERS, ex
        name = bot.VENUE_NAMES[ex]
        assert name in cards.VENUE_COLORS, name
    assert bot.EXCHANGE_NAMES["lbank"] == "LBank"
