import asyncio
import io
import zipfile

import pytest

import p2p


@pytest.mark.parametrize("name", ["bybit", "htx", "kucoin", "mexc", "bitpapa"])
def test_adapter_parses_fixture(offline, name):
    ads = asyncio.run(p2p.FETCHERS[name](None, p2p.Config(), "buy", "USDT"))
    assert ads, name
    for a in ads:
        assert a.price > 0 and a.side == "buy" and a.asset == "USDT"
        assert a.max_amt >= a.min_amt >= 0
        assert isinstance(a.pays, list) and a.pays


def test_htx_unknown_coin_is_empty(offline):
    assert asyncio.run(p2p.htx(None, p2p.Config(), "buy", "TON")) == []


def test_bybit_buy_and_sell_use_different_fixture_ads(offline):
    """Тело POST несёт сторону запроса (side) — фикстура должна отвечать разными объявлениями/мерчантами
    для buy и sell, как настоящий стакан Bybit, а не одним и тем же списком под обе стороны."""
    buy = asyncio.run(p2p.bybit(None, p2p.Config(), "buy", "USDT"))
    sell = asyncio.run(p2p.bybit(None, p2p.Config(), "sell", "USDT"))
    assert {a.nick for a in buy}.isdisjoint({a.nick for a in sell})
    assert all(a.side == "sell" for a in sell)


@pytest.mark.parametrize("name", ["htx", "kucoin", "mexc", "bitpapa"])
def test_buy_and_sell_use_different_fixture_ads(offline, name):
    """Как и у Bybit — HTX/KuCoin/MEXC/BitPapa кодируют сторону бота в URL (tradeType/side/type), и
    фикстура должна отвечать разными объявлениями/мерчантами для buy и sell."""
    buy = asyncio.run(p2p.FETCHERS[name](None, p2p.Config(), "buy", "USDT"))
    sell = asyncio.run(p2p.FETCHERS[name](None, p2p.Config(), "sell", "USDT"))
    assert buy and sell, name
    assert {a.nick for a in buy}.isdisjoint({a.nick for a in sell}), name
    assert all(a.side == "sell" for a in sell), name


def test_spot_prices(offline):
    spot = asyncio.run(p2p.spot_prices(None, ["USDT", "BTC", "ETH", "USDC"]))
    for venue in ("Bybit", "MEXC", "HTX", "KuCoin"):
        bid, ask = spot[venue]["ETH"]
        assert 0 < bid <= ask, venue
    assert spot["KuCoin"]["USDC"][0] > 0        # символ KuCoin с дефисом (USDC-USDT) распознан
    assert spot["HTX"]["BTC"][0] > 0            # символ HTX в нижнем регистре (btcusdt) распознан


def test_spot_prices_survive_one_venue_down(offline, monkeypatch):
    real = p2p._json

    async def flaky(s, method, url, body=None):
        if "api.htx.com" in url:
            raise RuntimeError("htx down")
        return await real(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", flaky)
    spot = asyncio.run(p2p.spot_prices(None, ["USDT", "ETH"]))
    assert "ETH" not in spot["HTX"] and spot["KuCoin"]["ETH"][0] > 0


def test_spot_prices_survive_garbage_ticker(offline, monkeypatch):
    """Один тикер с нечисловой ценой не должен ронять сбор спота всей площадки — только эту монету."""
    real = p2p._json

    async def garbage_bid(s, method, url, body=None):
        j = await real(s, method, url, body)
        if "api.bybit.com/v5/market/tickers" in url:
            for x in j["result"]["list"]:
                if x["symbol"] == "ETHUSDT":
                    x["bid1Price"] = "n/a"
        return j

    monkeypatch.setattr(p2p, "_json", garbage_bid)
    spot = asyncio.run(p2p.spot_prices(None, ["USDT", "BTC", "ETH"]))
    assert "ETH" not in spot["Bybit"]           # битый тикер пропущен
    assert spot["Bybit"]["BTC"][0] > 0          # остальные монеты той же площадки не задеты
    assert spot["KuCoin"]["ETH"][0] > 0          # другие площадки не задеты вовсе


def _bc_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bm_cy.dat", "\n".join([
            "10;20;Tether TRC20 (USDT);USDT TRC20;840;0;x",
            "42;231;Сбербанк RUB;RUB Сбербанк;643;3;x",
            "46;236;Т-Банк cash-in RUB;RUB Т-Банк cash-in;643;3;x",
            "21;1;СБП RUB;RUB СБП;643;2;x"]).encode("cp1251"))
        z.writestr("bm_exch.dat", "1396;CinusExc;;0;1\n933;TytCash;;0;1".encode("cp1251"))
        z.writestr("bm_rates.dat", "\n".join([
            "10;42;1396;1;89.8;10000000;0.822;1;100;1000;0",   # продать USDT обменнику, выплата на Сбер
            "42;10;933;90.5;1;50000;1.40;1;2000;300000;0",     # купить USDT за Сбер
            "10;46;1396;1;95;100000;0.5;1;1;1000;0",           # cash-in — должен быть пропущен
        ]).encode("cp1251"))
    return buf.getvalue()


def test_bestchange_parse():
    ads = p2p._bc_parse(_bc_zip())
    sell = [a for a in ads if a.side == "sell"]
    buy = [a for a in ads if a.side == "buy"]
    assert len(sell) == 1 and len(buy) == 1
    assert sell[0].price == pytest.approx(89.8) and sell[0].pays == ["Sberbank"] and sell[0].net == "TRC20"
    assert sell[0].min_amt == pytest.approx(100 * 89.8)            # лимиты USDT переведены в рубли
    assert buy[0].price == pytest.approx(90.5)
    assert buy[0].orders == 40 and buy[0].rate == pytest.approx(40 / 41 * 100)
    assert "bestchange.ru/click.php" in sell[0].url
