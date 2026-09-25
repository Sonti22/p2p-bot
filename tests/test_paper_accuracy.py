"""Точность сухого прогона (разбор 25.09): межмонетный маршрут, покупка из нескольких объявлений, объём продажи =
выход маршрута, сеть обменника, сбой площадки — ожидание, план ниже порога — не берём, без двойного запаса на курс."""
import asyncio
import json
import time

import bot as B
import p2p
import paper
from test_bot import Stub


def ad(ex, side, price, nick=None, asset="USDT", avail=10000, max_amt=500000, min_amt=1000, net=""):
    return p2p.Ad(ex, side, price, min_amt, max_amt, avail, ["SBP"], nick or f"{ex}-{side}", 1000, 100.0, "", asset,
                  net, "")


def snap(groups, errors=None, refs=None):
    return p2p.Snapshot(88.0, "t", refs or {}, {}, [], {}, {}, errors or {}, groups=groups)


def test_cross_asset_cycle_sells_the_coin_it_actually_has():
    """Купили BTC за рубли, на споте поменяли в USDT, продаём USDT: объём продажи — в USDT, а не в BTC."""
    buy = ad("Bybit", "buy", 6_000_000.0, asset="BTC")
    sell = ad("Bybit", "sell", 90.0, asset="USDT")
    cid = paper.start_cycle(10000, buy, sell, "спот BTC→USDT", 1.0, ts=time.time() - 400, sell_qty=112.0)
    paper.set_stage(cid, "sell")
    c = paper.get_cycle(cid)
    action, note, price = paper.check_sell_stage(c, snap({("Bybit", "sell", "USDT"): [sell]}))
    assert action == "advance" and price == 90.0
    assert abs(paper.realized_pct(c, price) - (112.0 * 90.0 / 10000 - 1) * 100) < 1e-9


def test_composite_buy_fails_only_when_all_merchants_are_gone():
    a, b = ad("HTX", "buy", 87.5, "merchA", max_amt=6000), ad("HTX", "buy", 87.6, "merchB", max_amt=6000)
    stacked = p2p._stack([a, b], 10000)
    assert stacked.nick == "2 объявл." and set(stacked.nicks) == {"merchA", "merchB"}
    cid = paper.start_cycle(10000, stacked, ad("KuCoin", "sell", 91.0), "r", 3.0, ts=1000.0)
    c = paper.get_cycle(cid)
    assert set(json.loads(c["buy_nicks"])) == {"merchA", "merchB"}
    only_b = snap({("HTX", "buy", "USDT"): [b]})
    assert paper.check_buy_stage(c, only_b, pay_minutes=5, now=1400.0)[0] == "advance"   # один ещё на месте
    nobody = snap({("HTX", "buy", "USDT"): [ad("HTX", "buy", 88.0, "other")]})
    assert paper.check_buy_stage(c, nobody, pay_minutes=5, now=1400.0) == ("fail", "объявление покупки исчезло")


def test_unchanged_book_gives_fact_equal_route_output_not_more_volume():
    """Объём продажи — выход маршрута после комиссии вывода; стакан не менялся — факт = план без запаса на курс,
    ни ложного срыва по глубине впритык, ни «продажа по … вместо …»."""
    sell = ad("KuCoin", "sell", 90.5, avail=113.3)                 # глубина впритык под выход маршрута
    c = {"sell_ex": "KuCoin", "sell_asset": "USDT", "sell_price": 90.5, "sell_net": "", "amount": 10000.0,
         "buy_price": 87.5, "planned_pct": 2.5, "sell_qty": 113.286, "ts_stage": time.time()}
    action, note, price = paper.check_sell_stage(c, snap({("KuCoin", "sell", "USDT"): [sell]}))
    assert action == "advance" and price == 90.5 and note == ""
    assert abs(paper.realized_pct(c, price) - (113.286 * 90.5 / 10000 - 1) * 100) < 1e-9


def test_bestchange_sell_stays_in_the_planned_network():
    trc, erc = ad("BestChange", "sell", 90.0, "ex1 [TRC20]", net="TRC20"), ad("BestChange", "sell", 95.0, "ex2 [ERC20]",
                                                                               net="ERC20")
    c = {"sell_ex": "BestChange", "sell_asset": "USDT", "sell_price": 90.0, "sell_net": "TRC20", "amount": 10000.0,
         "buy_price": 87.0, "planned_pct": 2.5, "sell_qty": 113.0, "ts_stage": time.time()}
    action, note, price = paper.check_sell_stage(c, snap({("BestChange", "sell", "USDT"): [erc, trc]}))
    assert action == "advance" and price == 90.0 and note == ""   # ERC20 по 95 — не наша сеть


def test_venue_error_waits_then_fails_when_stale():
    c = {"ts_stage": 1000.0, "buy_ex": "MEXC", "buy_asset": "USDT", "buy_nick": "m", "buy_nicks": '["m"]',
         "sell_ex": "MEXC", "sell_asset": "USDT", "sell_price": 90.0, "sell_net": "", "amount": 10000.0,
         "buy_price": 87.0, "planned_pct": 2.0, "sell_qty": 113.0}
    down = snap({}, errors={"mexc/USDT": "TimeoutError: "})
    assert paper.check_buy_stage(c, down, pay_minutes=5, now=1400.0) == ("wait", "")
    assert paper.check_sell_stage(c, down, now=1400.0)[0] == "wait"
    paused = snap({("MEXC", "buy", "USDT"): [ad("MEXC", "buy", 87.0, "m")]}, errors={"mexc": "пауза до 23:10"})
    assert paper.check_buy_stage(c, paused, pay_minutes=5, now=1400.0) == ("wait", "")
    late = 1000.0 + 31 * 60
    assert paper.check_buy_stage(c, down, pay_minutes=5, now=late)[0] == "fail"
    action, note, _ = paper.check_sell_stage(c, down, now=late)
    assert action == "fail" and "недоступна" in note


def test_bot_waits_on_venue_error_instead_of_failing(monkeypatch):
    b, s = ad("Bybit", "buy", 85.0, "nick"), ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, b, s, "r", 2.0, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(snap({}, errors={"bybit/USDT": "ClientError"})))
    c = paper.get_cycle(cid)
    assert c["result"] is None and c["stage"] == "buy"
    paper.set_stage(cid, "sell")
    asyncio.run(bot.process_paper_cycles(snap({}, errors={"mexc/USDT": "ClientError"})))
    assert paper.get_cycle(cid)["result"] is None


def test_no_cycle_when_plan_at_paper_amount_is_below_threshold(monkeypatch):
    """Сигнал на +3% при AMOUNT, но на PAPER_AMOUNT стек уходит в худшее объявление — план ниже порога, не берём."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    good_big = ad("HTX", "buy", 87.0, "big", min_amt=40000)        # только от 40 000 ₽
    bad_small = ad("HTX", "buy", 90.0, "small")
    sell = ad("KuCoin", "sell", 90.5)
    deal = (3.0, good_big, sell, "r")
    s = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [deal], {}, {}, {},
                     groups={("HTX", "buy", "USDT"): [good_big, bad_small], ("KuCoin", "sell", "USDT"): [sell]})
    bot = Stub(p2p.Config(min_profit=2.0, amount=50000))
    bot.live_scans = 1
    asyncio.run(bot.maybe_start_paper_cycle([deal], s))
    assert not paper.open_cycles()


def test_bot_stores_route_output_and_network_at_start(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    b, s = ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 91.0, "k")
    deal = (3.9, b, s, "r")
    sn = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [deal], {}, {}, {},
                      groups={("HTX", "buy", "USDT"): [b], ("KuCoin", "sell", "USDT"): [s]})
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.maybe_start_paper_cycle([deal], sn))
    (c,) = paper.open_cycles()
    d = p2p.deal_for_amount(deal, bot.cfg, sn, 10000)
    assert abs(c["sell_qty"] - d[2].avail) < 1e-9 and c["sell_qty"] < 10000 / 87.5   # после комиссий
    assert json.loads(c["buy_nicks"]) == ["m"]
