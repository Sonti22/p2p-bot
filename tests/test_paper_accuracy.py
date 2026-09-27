"""Точность сухого прогона (разбор 25.09): межмонетный маршрут, покупка из нескольких объявлений, объём продажи =
выход маршрута, сеть обменника, сбой площадки — ожидание, план ниже порога — не берём, без двойного запаса на курс."""
import asyncio
import dataclasses
import json
import time

import pytest

import bot as B
import p2p
import paper
from test_bot import Stub


def ad(ex, side, price, nick=None, asset="USDT", avail=10000, max_amt=500000, min_amt=1000, net=""):
    return p2p.Ad(ex, side, price, min_amt, max_amt, avail, ["SBP"], nick or f"{ex}-{side}", 1000, 100.0, "", asset,
                  net, "")


def snap(groups, errors=None, refs=None, spot=None):
    return p2p.Snapshot(88.0, "t", refs or {}, {}, [], {}, {}, errors or {}, groups=groups, spot=spot or {})


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
    only_b = snap({("HTX", "buy", "USDT"): [b]})       # остался один на 6 000 ₽ из 10 000 — сумму не покрыть
    action, note = paper.check_buy_stage(c, only_b, pay_minutes=5, now=1400.0)
    assert action == "fail" and "не покрывают" in note
    b_big = ad("HTX", "buy", 87.6, "merchB", max_amt=20000)   # тот же мерчант поднял лимит — остаток покрывает
    assert paper.check_buy_stage(c, snap({("HTX", "buy", "USDT"): [b_big]}), pay_minutes=5, now=1400.0)[0] == "advance"
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
    late = 1000.0 + 5 * 60 + 31 * 60   # 30 минут отсчитываются после окна оплаты
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


def test_empty_book_without_venue_error_is_decided_at_once():
    """Площадка ответила, годных объявлений нет (группы нет, ошибки нет) — не «площадка недоступна», а обычный итог."""
    c = {"ts_stage": 1000.0, "buy_ex": "HTX", "buy_asset": "USDT", "buy_nick": "m", "buy_nicks": '["m"]', "amount": 10000.0,
         "sell_ex": "KuCoin", "sell_asset": "USDT", "sell_price": 90.0, "sell_net": "", "buy_price": 87.0,
         "planned_pct": 2.0, "sell_qty": 113.0}
    assert paper.check_buy_stage(c, snap({}), pay_minutes=5, now=1400.0) == ("fail", "объявление покупки исчезло")
    action, note, _ = paper.check_sell_stage(c, snap({}), now=1400.0)
    assert action == "fail" and "глубины" in note


def test_venue_error_right_after_pay_window_waits():
    """PAPER_PAY_MINUTES = PAPER_STALE_MINUTES: сбой площадки на первой проверке — ждём, а не срыв."""
    c = {"ts_stage": 1000.0, "buy_ex": "MEXC", "buy_asset": "USDT", "buy_nick": "m", "buy_nicks": '["m"]',
         "amount": 10000.0}
    down = snap({}, errors={"mexc/USDT": "TimeoutError: "})
    assert paper.check_buy_stage(c, down, pay_minutes=30, now=1000.0 + 30 * 60 + 20, stale_minutes=30) == ("wait", "")


def test_transfer_stage_checks_only_real_transfers():
    import netstatus
    netstatus._apply("Bybit", "USDT", {n: {"dep": True, "wd": False, "fee": 1.0} for n in ("TRC20", "BEP20", "ERC20", "TON")})
    cfg = p2p.Config()
    same = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "sell_ex": "Bybit"}
    assert paper.check_transfer_stage(same, cfg, transfer_minutes=3, now=1300.0) == ("advance", "")   # внутри биржи
    from_exchanger = {"ts_stage": 1000.0, "buy_ex": "BestChange", "buy_asset": "USDT", "sell_ex": "Bybit"}
    assert paper.check_transfer_stage(from_exchanger, cfg, transfer_minutes=3, now=1300.0) == ("advance", "")
    out = {"ts_stage": 1000.0, "buy_ex": "Bybit", "buy_asset": "USDT", "sell_ex": "MEXC"}
    assert paper.check_transfer_stage(out, cfg, transfer_minutes=3, now=1300.0)[0] == "fail"


def test_simple_route_filter():
    b, s = ad("Bybit", "buy", 87.0), ad("MEXC", "sell", 90.0)
    assert paper.simple_route((3.0, b, s, "перевод −1 USDT (TRC20) на MEXC"))
    assert not paper.simple_route((3.0, ad("Bybit", "buy", 6e6, asset="BTC"), s, "спот BTC→USDT на Bybit (−0.1%)"))
    assert not paper.simple_route((3.0, b, s, "спот USDT→TON на Bybit (−0.1%) → перевод … → спот TON→USDT на MEXC"))


def test_spot_routes_are_not_taken_into_dry_run(monkeypatch):
    """Разбор #110: связка через спот на сумму прогона проходит по глубине и порогу, но круг не заводится —
    площадка конвертации в круге не хранится (условия возврата — ROADMAP «межмонетные связки, часть 2»)."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    b, s = ad("Bybit", "buy", 6_000_000.0, asset="BTC"), ad("Bybit", "sell", 105.0)
    deal = (5.0, b, s, "спот BTC→USDT на Bybit (−0.1%)")
    sn = p2p.Snapshot(88.0, "t", {}, {}, [deal], {}, {}, {},
                      groups={("Bybit", "buy", "BTC"): [b], ("Bybit", "sell", "USDT"): [s]},
                      spot={"Bybit": {"BTC": (60000.0, 60100.0)}})
    cfg = p2p.Config(min_profit=2.0)
    d = p2p.deal_for_amount(deal, cfg, sn, 10000)
    assert d is not None and d[0] > cfg.min_profit     # без фильтра круг бы завёлся
    bot = Stub(cfg)
    bot.live_scans = 1
    asyncio.run(bot.maybe_start_paper_cycle([deal], sn))
    assert not paper.open_cycles()


def test_cross_asset_sell_recomputes_output_from_fresh_spot():
    """Купили BTC, продаём USDT через спот-конвертацию на Bybit — курс BTC/USDT между стартом круга и
    стадией sell вырос: факт должен получить больше монеты на выходе, а не застрявшее на старте число."""
    b = ad("Bybit", "buy", 6_000_000.0, asset="BTC")
    s = ad("Bybit", "sell", 105.0, asset="USDT", avail=1_000_000)
    cfg = p2p.Config(min_profit=2.0, pay_fee=0.0)
    start_spot = {"Bybit": {"BTC": (60000.0, 60100.0)}}
    qty_start = p2p._route_qty(b, s, dataclasses.replace(cfg, amount=10000), start_spot, disable=frozenset({"risk"}))
    cid = paper.start_cycle(10000, b, s, "спот BTC→USDT на Bybit", 1.0, ts=time.time() - 400,
                            sell_qty=qty_start, pay_fee=0.0)
    paper.set_stage(cid, "sell")
    c = paper.get_cycle(cid)
    up_spot = {"Bybit": {"BTC": (66000.0, 66100.0)}}   # курс BTC/USDT вырос на 10% к моменту продажи
    fresh = snap({("Bybit", "sell", "USDT"): [s]}, spot=up_spot)
    action, note, price = paper.check_sell_stage(c, fresh, cfg=cfg)
    assert action == "advance"
    qty_fresh = paper.recompute_sell_qty(c, cfg, up_spot)
    assert qty_fresh > qty_start * 1.05          # выход вырос вместе с курсом, а не остался на уровне старта
    assert paper.realized_pct(c, price, qty_fresh) > paper.realized_pct(c, price, qty_start)


def test_cross_asset_sell_waits_when_saved_venue_ticker_missing():
    """Разбор «межмонетные связки, часть 2, п.2»: тикер BTC на Bybit (площадка конвертации, сохранённая при
    старте) пропал на один скан — круг ждёт, а не срывается и не считает через другую биржу, даже если там
    тикер есть."""
    b = ad("Bybit", "buy", 6_000_000.0, asset="BTC")
    s = ad("Bybit", "sell", 105.0, asset="USDT", avail=1_000_000)
    cfg = p2p.Config(min_profit=2.0, pay_fee=0.0)
    start_spot = {"Bybit": {"BTC": (60000.0, 60100.0)}}
    qty_start = p2p._route_qty(b, s, dataclasses.replace(cfg, amount=10000), start_spot, disable=frozenset({"risk"}))
    cid = paper.start_cycle(10000, b, s, "спот BTC→USDT на Bybit", 1.0, ts=time.time() - 400,
                            sell_qty=qty_start, pay_fee=0.0, hops=p2p.route_hops(b, s, cfg, start_spot))
    paper.set_stage(cid, "sell")
    c = paper.get_cycle(cid)
    assert paper.cycle_hops(c)["venues"] == ["Bybit"]
    # Bybit пропал из спота, MEXC тикер BTC есть — не должны молча уйти на другую биржу
    gone = snap({("Bybit", "sell", "USDT"): [s]}, spot={"MEXC": {"BTC": (66000.0, 66100.0)}})
    action, note, price = paper.check_sell_stage(c, gone, cfg=cfg, now=time.time())
    assert action == "wait" and price is None
    late = snap({("Bybit", "sell", "USDT"): [s]}, spot={"MEXC": {"BTC": (66000.0, 66100.0)}})
    action, note, price = paper.check_sell_stage(c, late, cfg=cfg, now=time.time() + 31 * 60)
    assert action == "fail" and "недоступна" in note and price is None
    # тикер вернулся на Bybit — считаем по нему же, курс не изменился, выход как при старте
    back_spot = {"Bybit": {"BTC": (60000.0, 60100.0)}, "MEXC": {"BTC": (66000.0, 66100.0)}}
    back = snap({("Bybit", "sell", "USDT"): [s]}, spot=back_spot)
    action, note, price = paper.check_sell_stage(c, back, cfg=cfg)
    assert action == "advance"
    assert paper.recompute_sell_qty(c, cfg, back_spot) == pytest.approx(qty_start)


def test_virtually_exhausted_sbp_limit_puts_fee_into_plan_and_volume(monkeypatch):
    """Виртуальный оборот прогона исчерпал бесплатный лимит СБП всех своих банков — комиссия 0,5% в плане и объёме."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    monkeypatch.setenv("OWN_BANKS", "T-Bank")
    monkeypatch.setenv("SBP_FREE_LIMITS", "T-Bank:10000")
    b, s = ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 91.0, "k")
    old = paper.start_cycle(10000, b, s, "r", 3.0)                 # 10 000 ₽ по СБП с Т-Банка — лимит исчерпан
    paper.finish_cycle(old, "done", 1.0)
    deal = (3.9, b, s, "r")
    sn = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [deal], {}, {}, {},
                      groups={("HTX", "buy", "USDT"): [b], ("KuCoin", "sell", "USDT"): [s]})
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.maybe_start_paper_cycle([deal], sn))
    (c,) = paper.open_cycles()
    no_fee = p2p.deal_for_amount(deal, bot.cfg, sn, 10000)
    assert c["pay_kind"] == "sbp" and c["planned_pct"] < no_fee[0] - 0.4
    assert c["sell_qty"] < no_fee[2].avail
