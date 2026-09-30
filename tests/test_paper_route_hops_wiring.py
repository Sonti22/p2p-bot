"""Проводка хопов в круг сухого прогона: Bot.maybe_start_paper_cycle — единственное место, где route_hops попадает в
живой круг (hops=hops в paper.start_cycle). Остальные тесты хопов собирают их руками (test_paper.py, test_paper_accuracy.py)
и проводку не видят: потеряй бот hops=hops — стадии transfer/sell молча считали бы маршрут без сетей и комиссий."""
import dataclasses

import p2p
import paper
from test_bot import Stub
from helpers import arun, make_ad

AMOUNT = 10000


def start(monkeypatch, deals, groups):
    """Бот заводит круг сухого прогона (PAPER_AMOUNT 10 000, связка держится 1 скан) по снимку с этим стаканом."""
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", str(AMOUNT))
    sn = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, deals, {}, {}, {}, groups=groups)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    arun(bot.maybe_start_paper_cycle(deals, sn))
    return bot, sn


def groups_of(*deals):
    g = {}
    for _, b, s, _ in deals:
        g[(b.ex, "buy", b.asset)] = [b]
        g[(s.ex, "sell", s.asset)] = [s]
    return g


def test_started_cycle_stores_the_same_hops_the_bot_computes(monkeypatch):
    b, s = make_ad("HTX", "buy", 87.5, pays=("SBP",)), make_ad("KuCoin", "sell", 91.0, pays=("SBP",))
    deal = (3.9, b, s, "перевод USDT на KuCoin")
    assert paper.simple_route(deal) and not deal[1].stale and not deal[2].stale
    bot, sn = start(monkeypatch, [deal], groups_of(deal))
    (c,) = paper.open_cycles()                                   # круг заведён, слот один
    saved = paper.cycle_hops(c)
    assert set(saved) == {"venues", "hops"} and saved["hops"]    # без hops=hops здесь пустой маршрут
    # те же аргументы, что у бота: стек на сумму прогона, cfg с этой суммой, спот и банки за лимитом СБП снимка
    _, b_d, s_d, _ = p2p.deal_for_amount(deal, bot.cfg, sn, AMOUNT)
    route_cfg = dataclasses.replace(bot.cfg, amount=AMOUNT)
    assert saved == p2p.route_hops(b_d, s_d, route_cfg, sn.spot, frozenset(sn.over_banks))


def test_hops_belong_to_the_picked_deal_not_the_first_one(monkeypatch):
    """Две подтверждённые простые связки: в круг идёт лучшая по оценке, и хопы — её (площадки покупки и продажи)."""
    weak = (2.5, make_ad("Bybit", "buy", 89.0, pays=("SBP",)), make_ad("MEXC", "sell", 91.5, pays=("SBP",)), "r")
    strong = (5.0, make_ad("HTX", "buy", 86.0, pays=("SBP",)), make_ad("KuCoin", "sell", 91.0, pays=("SBP",)), "r")
    start(monkeypatch, [weak, strong], groups_of(weak, strong))
    (c,) = paper.open_cycles()
    assert (c["buy_ex"], c["sell_ex"]) == ("HTX", "KuCoin")      # выбрана вторая, не первая по списку
    hops = paper.cycle_hops(c)["hops"]
    assert [(h["frm"], h["to"], h["asset"]) for h in hops] == [("HTX", "KuCoin", "USDT")]
