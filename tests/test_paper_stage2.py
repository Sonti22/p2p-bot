"""Реализм сухого прогона (план, этап 2.4): покупка по свежему стакану (цена, объём, лимиты, проскальзывание),
неизвестный статус сети — риск, покупка у обменника — только по свежей котировке BestChange, время перевода по сетям,
межмонетные «часть 2» п. 3–4 (выход по сохранённым хопам, перевод — по реальным хопам), перезапуск посреди круга,
новые причины в отчёте. Без сети: снимки собираются руками, справочник сетей — netstatus._apply."""
import asyncio
import csv
import dataclasses
import json
import sqlite3
import time

import pytest

import netstatus
import p2p
import paper
from test_bot import Stub, texts


def ad(ex, side, price, nick=None, asset="USDT", avail=10000, max_amt=500000, min_amt=1000, net="", fetched=0.0):
    a = p2p.Ad(ex, side, price, min_amt, max_amt, avail, ["SBP"], nick or f"{ex}-{side}", 1000, 100.0, "", asset,
               net, "")
    a.fetched_ts = fetched
    return a


def snap(groups, errors=None, spot=None, jobs=None):
    return p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, errors or {}, groups=groups, spot=spot or {}, jobs=jobs or [])


def _cfg(**kw):
    kw.setdefault("pay_fee", 0.0)
    return p2p.Config(**kw)


def _hop(frm, to, asset="USDT", net="TRC20", fee=1.0, frm_net=""):
    return {"frm": frm, "frm_net": frm_net, "to": to, "to_net": net, "asset": asset, "fee": fee, "parts": 1}


def _open(venue, asset, nets, wd=True, dep=True, fee=0.0001):
    netstatus._apply(venue, asset, {n: {"dep": dep, "wd": wd, "fee": fee, "min": None} for n in nets})


# --- а) покупка по свежему стакану ---

def test_buy_min_raised_walks_book_and_fact_uses_fill_price():
    t0 = time.time() - 400
    cid = paper.start_cycle(10000, ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 91.0, "k"), "r", 3.0, ts=t0,
                            sell_qty=10000 / 87.5 - 1.0)
    c = paper.get_cycle(cid)
    fresh = snap({("HTX", "buy", "USDT"): [ad("HTX", "buy", 87.5, "m", min_amt=20000), ad("HTX", "buy", 87.9, "o")]})
    assert paper.check_buy_stage(c, fresh, pay_minutes=5) == ("advance", "")
    fill = paper.buy_fill(c, fresh)
    assert fill["price"] == 87.9 and fill["book"] and fill["own"] == 1   # минимум мерчанта вырос выше суммы круга
    paper.set_buy_fill(cid, fill)
    c = paper.get_cycle(cid)
    assert c["buy_fill_price"] == 87.9 and abs(c["buy_slip_pct"] - (87.9 / 87.5 - 1) * 100) < 1e-9
    assert [code for code, _ in paper.cycle_risks(c)] == ["buy_book"]
    qty = paper.recompute_sell_qty(c, _cfg(), {})
    assert qty == pytest.approx(10000 / 87.9 - 1.0)                   # та же комиссия перевода, монеты меньше
    assert paper.realized_pct(c, 91.0, qty) < paper.realized_pct(c, 91.0, paper.sell_qty(c)) - 0.4


def test_buy_small_volume_takes_rest_from_book_and_short_book_fails():
    c = {"ts_stage": 1000.0, "buy_ex": "HTX", "buy_asset": "USDT", "buy_nick": "m", "buy_nicks": '["m"]',
         "buy_price": 87.5, "amount": 10000.0}
    fresh = snap({("HTX", "buy", "USDT"): [ad("HTX", "buy", 87.5, "m", avail=50), ad("HTX", "buy", 88.0, "o")]})
    fill = paper.buy_fill(c, fresh)
    assert fill["price"] == pytest.approx(10000 / (50 + (10000 - 50 * 87.5) / 88.0)) and fill["book"]
    assert paper.check_buy_stage(c, fresh, pay_minutes=5, now=1400.0) == ("advance", "")
    short = snap({("HTX", "buy", "USDT"): [ad("HTX", "buy", 87.5, "m", max_amt=4000)]})   # лимит мерчанта упал
    action, note = paper.check_buy_stage(c, short, pay_minutes=5, now=1400.0)
    assert action == "fail" and note == "стакана покупки не хватает на сумму круга"
    assert paper.fail_reason(note) == "buy_depth"


# --- б) неизвестный статус сети — риск, а не «открыта» ---

def test_unknown_network_status_is_recorded_risk_not_open():
    hops = {"venues": [], "hops": [_hop("MEXC", "Bybit")]}
    cid = paper.start_cycle(10000, ad("MEXC", "buy", 87.5), ad("Bybit", "sell", 90.0), "r", 2.0,
                            ts=time.time() - 400, hops=hops)
    paper.set_stage(cid, "transfer", ts=time.time() - 400)
    c = paper.get_cycle(cid)
    assert paper.check_transfer_stage(c, _cfg(), transfer_minutes=3) == ("advance", "")   # не срыв
    risks = paper.transfer_risks(c, _cfg())
    assert [code for code, _ in risks] == ["net_unknown", "net_unknown"]
    assert "вывод USDT (TRC20) с MEXC" in risks[0][1] and "ввод USDT (TRC20) на Bybit" in risks[1][1]
    _open("MEXC", "USDT", ["TRC20"])
    _open("Bybit", "USDT", ["TRC20"])
    assert paper.transfer_risks(c, _cfg()) == []                      # справочник знает оба конца — риска нет
    _open("MEXC", "USDT", ["TRC20"], wd=False)
    action, note = paper.check_transfer_stage(c, _cfg(), transfer_minutes=3)
    assert action == "fail" and note == "вывод USDT (TRC20) с MEXC закрыт"
    _open("MEXC", "USDT", ["BEP20"])                                   # справочник есть, сети круга в нём нет — закрыта
    assert paper.check_transfer_stage(c, _cfg(), transfer_minutes=3)[0] == "fail"


def test_bot_records_network_risk_and_report_counts_it():
    hops = {"venues": [], "hops": [_hop("MEXC", "Bybit", net="")]}   # сеть не выбрана (нет ни таблицы, ни справочника)
    b, s = ad("MEXC", "buy", 87.5, "m"), ad("Bybit", "sell", 90.0, "k")
    cid = paper.start_cycle(10000, b, s, "r", 2.0, ts=time.time() - 2000, hops=hops, sell_qty=113.0)
    paper.set_stage(cid, "transfer", ts=time.time() - 2000)
    bot = Stub(_cfg(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(snap({("Bybit", "sell", "USDT"): [s]})))
    c = paper.get_cycle(cid)
    assert c["stage"] == "sell" and "сеть не выбрана" in paper.cycle_risks(c)[0][1]
    asyncio.run(bot.process_paper_cycles(snap({("Bybit", "sell", "USDT"): [s]})))
    assert paper.get_cycle(cid)["result"] == "done"
    (row,) = paper.report_rows()
    assert row["net_unknown"] == 1 and row["buy_from_book"] == 0


# --- в) покупка у обменника — только по свежей котировке BestChange ---

def _bc_cycle(t0):
    return {"ts_stage": t0, "buy_ex": "BestChange", "buy_asset": "USDT", "buy_net": "TRC20",
            "buy_nick": "ex1 [TRC20]", "buy_nicks": '["ex1 [TRC20]"]', "buy_price": 87.0, "amount": 10000.0}


def test_bestchange_buy_waits_for_fresh_quote_then_fails():
    t0 = 100_000.0
    c = _bc_cycle(t0)
    old = snap({("BestChange", "buy", "USDT"): [ad("BestChange", "buy", 87.0, "ex1 [TRC20]", net="TRC20",
                                                   fetched=t0 - 100)]})   # выгрузка до старта круга (кэш)
    assert paper.check_buy_stage(c, old, pay_minutes=5, now=t0 + 400) == ("wait", "")
    late = t0 + 5 * 60 + 31 * 60
    action, note = paper.check_buy_stage(c, old, pay_minutes=5, now=late)
    assert action == "fail" and note == "нет свежей котировки BestChange по USDT (TRC20) (не старше 5 мин)"
    assert paper.fail_reason(note) == "bc_stale"
    fresh = snap({("BestChange", "buy", "USDT"): [
        ad("BestChange", "buy", 87.0, "ex1 [TRC20]", net="TRC20", fetched=t0 + 300),
        ad("BestChange", "buy", 80.0, "ex2 [ERC20]", net="ERC20", fetched=t0 + 300)]})   # чужая сеть — не наша
    assert paper.check_buy_stage(c, fresh, pay_minutes=5, now=t0 + 400) == ("advance", "")
    assert paper.buy_fill(c, fresh)["price"] == 87.0
    # свежая после старта, но старше окна PAPER_BC_FRESH_MINUTES — ждём следующую выгрузку
    assert paper.check_buy_stage(c, fresh, pay_minutes=5, now=t0 + 300 + 6 * 60) == ("wait", "")
    assert paper.check_buy_stage(c, fresh, pay_minutes=5, now=t0 + 300 + 6 * 60, bc_fresh_minutes=10)[0] == "advance"


def test_bestchange_fresh_dump_without_exchanger_fails_as_gone():
    """Свежая выгрузка пришла (время — по замеру запроса), обменника в ней нет — это уже решение, а не ожидание."""
    t0 = 100_000.0
    jobs = [{"ex": "bestchange", "side": "buy", "asset": "USDT", "t1": t0 + 350, "age": 10.0}]
    empty = snap({("BestChange", "buy", "USDT"): []}, jobs=jobs)
    assert paper.check_buy_stage(_bc_cycle(t0), empty, pay_minutes=5, now=t0 + 400) == \
        ("fail", "объявление покупки исчезло")


def test_bot_bestchange_buy_does_not_pass_on_cached_dump():
    b, s = ad("BestChange", "buy", 87.0, "ex1 [TRC20]", net="TRC20"), ad("Bybit", "sell", 90.0)
    t0 = time.time() - 400
    cid = paper.start_cycle(10000, b, s, "r", 2.0, ts=t0)
    cached = snap({("BestChange", "buy", "USDT"): [ad("BestChange", "buy", 87.0, "ex1 [TRC20]", net="TRC20",
                                                      fetched=t0 - 60)]})
    bot = Stub(_cfg(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(cached))
    assert paper.get_cycle(cid)["stage"] == "buy"                     # раньше прошла бы автоматически
    fresh = snap({("BestChange", "buy", "USDT"): [ad("BestChange", "buy", 87.0, "ex1 [TRC20]", net="TRC20",
                                                     fetched=time.time() - 30)]})
    asyncio.run(bot.process_paper_cycles(fresh))
    assert paper.get_cycle(cid)["stage"] == "transfer"


# --- г) время перевода по сетям ---

def test_transfer_minutes_from_network_table():
    table, d = paper.net_minutes_table(), 3.0
    assert paper.hops_transfer_minutes([_hop("MEXC", "Bybit")], table, d) == 3.0
    assert paper.hops_transfer_minutes([_hop("HTX", "KuCoin", "BTC", "BTC")], table, d) == 40.0
    assert paper.hops_transfer_minutes([_hop("HTX", "KuCoin", net="XYZ")], table, 7.0) == 7.0   # нет в таблице
    assert paper.hops_transfer_minutes([_hop("HTX", "KuCoin", net="")], table, 7.0) == 7.0      # сеть не выбрана
    assert paper.hops_transfer_minutes([_hop("Bybit", "Bybit", net="")], table, 7.0) == 0.0     # внутри биржи
    relay = _hop("BestChange", "BestChange", net="BEP20", frm_net="TRC20")                      # обменник→Bybit→обменник
    assert paper.hops_transfer_minutes([relay], table, d) == 3.0 + 2.0
    two = [_hop("Bybit", "Bybit", "BTC", "", 0.0), _hop("Bybit", "MEXC", "USDT", "BEP20", 0.2)]
    assert paper.hops_transfer_minutes(two, table, d) == 2.0


def test_transfer_minutes_saved_at_start_survive_env_change(monkeypatch):
    hops = {"venues": [], "hops": [_hop("HTX", "KuCoin", "BTC", "BTC", 0.0001)]}
    b, s = ad("HTX", "buy", 6e6, "m", asset="BTC"), ad("KuCoin", "sell", 6.1e6, "k", asset="BTC")
    cid = paper.start_cycle(10000, b, s, "r", 2.0, hops=hops)
    t = time.time()
    paper.set_stage(cid, "transfer", ts=t)
    monkeypatch.setenv("PAPER_NET_MINUTES", "BTC:10,кривое,TRC20:x")   # правка после старта (или перезапуск)
    c = paper.get_cycle(cid)
    assert c["transfer_min"] == 40.0 and paper.transfer_minutes_for(c, 3.0) == 40.0
    assert paper.check_transfer_stage(c, _cfg(), 3.0, now=t + 30 * 60) == ("wait", "")
    assert paper.check_transfer_stage(c, _cfg(), 3.0, now=t + 41 * 60) == ("advance", "")
    new = paper.get_cycle(paper.start_cycle(10000, b, s, "r", 2.0, hops=hops))
    assert new["transfer_min"] == 10.0 and paper.net_minutes_table()["TRC20"] == 3.0
    old = {"ts_stage": t, "buy_ex": "HTX", "buy_asset": "USDT", "sell_ex": "KuCoin", "route_hops": ""}
    assert paper.transfer_minutes_for(old, 3.0) == 3.0                # старый круг без хопов — как раньше
    pre = dict(old, route_hops=json.dumps(hops), transfer_min=None)    # хопы есть, времени нет (до миграции)
    assert paper.transfer_minutes_for(pre, 3.0) == 10.0


def test_same_venue_cycle_has_no_transfer_wait():
    b, s = ad("Bybit", "buy", 87.0), ad("Bybit", "sell", 90.0)
    hops = p2p.route_hops(b, s, _cfg(amount=10000), {})
    cid = paper.start_cycle(10000, b, s, "r", 2.0, hops=hops)
    t = time.time()
    paper.set_stage(cid, "transfer", ts=t)
    c = paper.get_cycle(cid)
    assert c["transfer_min"] == 0.0 and paper.check_transfer_stage(c, _cfg(), 3.0, now=t + 1) == ("advance", "")


# --- д) межмонетные связки, «часть 2» п. 3–4 ---

def test_cross_asset_sell_ignores_network_closed_after_transfer():
    """П. 3: на продаже сети уже прошедших переводов заново не проверяются — комиссии из route_hops, пересчитывается
    только курс. Раньше _route_qty видел закрытый вывод USDT с Bybit и срывал продажу («конвертация недоступна»)."""
    b = ad("Bybit", "buy", 6_000_000.0, "m", asset="BTC")
    s = ad("MEXC", "sell", 90.0, "k", asset="USDT", avail=1_000_000)
    cfg = _cfg(min_profit=2.0)
    spot = {"Bybit": {"BTC": (60000.0, 60100.0)}}
    route_cfg = dataclasses.replace(cfg, amount=10000)
    qty0 = p2p._route_qty(b, s, route_cfg, spot, disable=frozenset({"risk"}))
    hops = p2p.route_hops(b, s, route_cfg, spot)
    assert hops["venues"] == ["Bybit"] and hops["hops"][1]["to_net"] and hops["hops"][1]["fee"] > 0
    cid = paper.start_cycle(10000, b, s, "спот BTC→USDT на Bybit", 2.0, ts=time.time() - 400, sell_qty=qty0,
                            hops=hops)
    paper.set_stage(cid, "sell")
    c = paper.get_cycle(cid)
    _open("Bybit", "USDT", ["TRC20", "BEP20", "ERC20", "TON"], wd=False)   # вывод закрылся уже после перевода
    assert p2p._route_qty(b, s, route_cfg, spot, disable=frozenset({"risk"})) is None
    fresh = snap({("MEXC", "sell", "USDT"): [s]}, spot=spot)
    action, note, price = paper.check_sell_stage(c, fresh, cfg=cfg)
    assert action == "advance" and price == 90.0
    assert paper.recompute_sell_qty(c, cfg, spot) == pytest.approx(qty0)
    up = {"Bybit": {"BTC": (66000.0, 66100.0)}}                        # курсовая часть пересчитывается
    assert paper.recompute_sell_qty(c, cfg, up) > qty0 * 1.09


def _htx_to_kucoin_btc(cfg):
    """Покупка USDT на HTX, продажа BTC на KuCoin: конвертация на споте HTX, перевод — BTC, а не USDT."""
    _open("HTX", "USDT", ["TRC20"])
    _open("HTX", "BTC", ["BTC"])
    _open("KuCoin", "BTC", ["BTC"])
    b, s = ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 6.1e6, "k", asset="BTC")
    spot = {"HTX": {"BTC": (60000.0, 60100.0)}}
    hops = p2p.route_hops(b, s, dataclasses.replace(cfg, amount=10000), spot)
    assert hops["venues"] == ["HTX"] and [h["asset"] for h in paper._real_hops(hops["hops"])] == ["BTC"]
    cid = paper.start_cycle(10000, b, s, "спот USDT→BTC на HTX → перевод BTC на KuCoin", 2.0, ts=time.time() - 400,
                            hops=hops)
    return paper.get_cycle(cid)


def test_transfer_checks_saved_hops_not_direct_withdraw_of_buy_asset():
    """П. 4: закрыт вывод USDT, открыт BTC — перевод маршрута (BTC) проходит; наоборот — срыв. Раньше проверялся
    вывод монеты покупки (USDT) напрямую HTX → KuCoin — ровно наоборот."""
    cfg = _cfg()
    c = _htx_to_kucoin_btc(cfg)
    _open("HTX", "USDT", ["TRC20"], wd=False)
    assert not p2p.withdraw_open(cfg, "HTX", "USDT", receiver="KuCoin")   # старая проверка сорвала бы круг
    assert paper.check_transfer_stage(c, cfg, 3.0, now=time.time() + 3600) == ("advance", "")
    _open("HTX", "USDT", ["TRC20"])
    _open("HTX", "BTC", ["BTC"], wd=False)
    action, note = paper.check_transfer_stage(c, cfg, 3.0, now=time.time() + 3600)
    assert action == "fail" and note == "вывод BTC (BTC) с HTX закрыт"


def test_transfer_same_venue_with_conversion_elsewhere_checks_both_transfers():
    """buy_ex == sell_ex (BitPapa), конвертация на Bybit: два перевода — BTC на Bybit и USDT обратно; раньше «внутри
    одной площадки» переводов не проверяли вовсе."""
    cfg = _cfg()
    b, s = ad("BitPapa", "buy", 6e6, "m", asset="BTC"), ad("BitPapa", "sell", 90.0, "k")
    spot = {"Bybit": {"BTC": (60000.0, 60100.0)}}
    hops = p2p.route_hops(b, s, dataclasses.replace(cfg, amount=10000), spot)
    assert [(h["frm"], h["to"], h["asset"]) for h in hops["hops"]] == [("BitPapa", "Bybit", "BTC"),
                                                                       ("Bybit", "BitPapa", "USDT")]
    cid = paper.start_cycle(10000, b, s, "r", 2.0, ts=time.time() - 400, hops=hops)
    c = paper.get_cycle(cid)
    assert c["transfer_min"] > 0
    closed, risks = paper.transfer_check(c, cfg)
    assert closed is None and len(risks) >= 2                          # BitPapa без справочника, Bybit без ключа
    _open("Bybit", "USDT", ["TRC20"], wd=False)
    action, note = paper.check_transfer_stage(c, cfg, 3.0, now=time.time() + 3 * 3600)
    assert action == "fail" and note == "вывод USDT (TRC20) с Bybit закрыт"


# --- перезапуск посреди круга ---

def _old_db(path, stage, ts_stage):
    """База версии до этапа 2.4 (без колонок реализма, как у круга, начатого старым кодом) с кругом на стадии stage."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL, amount REAL, "
                "buy_ex TEXT, buy_asset TEXT, buy_price REAL, buy_nick TEXT, sell_ex TEXT, sell_asset TEXT, "
                "sell_price REAL, sell_nick TEXT, route TEXT, planned_pct REAL, stage TEXT, ts_stage REAL, "
                "realized_pct REAL DEFAULT NULL, result TEXT DEFAULT NULL, note TEXT DEFAULT '', "
                "sell_qty REAL DEFAULT NULL, buy_nicks TEXT DEFAULT '', route_hops TEXT DEFAULT '')")
    con.execute("INSERT INTO cycles (ts_start, amount, buy_ex, buy_asset, buy_price, buy_nick, sell_ex, sell_asset, "
                "sell_price, sell_nick, route, planned_pct, stage, ts_stage, sell_qty, buy_nicks) VALUES "
                "(?, 10000, 'HTX', 'USDT', 87.5, 'm', 'KuCoin', 'USDT', 90.0, 'k', 'r', 2.0, ?, ?, 113.0, '[\"m\"]')",
                (ts_stage - 60, stage, ts_stage))
    con.commit()
    con.close()


@pytest.mark.parametrize("stage", ["buy", "transfer", "sell"])
def test_old_cycle_in_progress_finishes_after_upgrade(monkeypatch, tmp_path, stage):
    """Круг, начатый старым кодом (нет колонок реализма, нет хопов), после обновления идёт дальше: миграция добавляет
    колонки (NULL/пусто), покупка — по свежему стакану, перевод — PAPER_TRANSFER_MINUTES, итог — по сохранённому
    sell_qty; ничего из памяти процесса не нужно."""
    from test_bot import _patch_paper_db
    db = str(tmp_path / "paper.db")
    _old_db(db, stage, time.time() - 400)
    _patch_paper_db(monkeypatch, db)
    book = snap({("HTX", "buy", "USDT"): [ad("HTX", "buy", 87.5, "m")],
                 ("KuCoin", "sell", "USDT"): [ad("KuCoin", "sell", 90.0, "k")]})
    for _ in range(3):
        asyncio.run(Stub(_cfg(min_profit=2.0)).process_paper_cycles(book))   # каждый раз «новый процесс»
        c = paper.get_cycle(1, path=db)
        if c["result"]:
            break
        paper.set_stage(1, c["stage"], path=db, ts=time.time() - 400)        # время стадии прошло
    c = paper.get_cycle(1, path=db)
    assert c["result"] == "done" and c["realized_pct"] == pytest.approx((113.0 * 90.0 / 10000 - 1) * 100)
    assert c["transfer_min"] is None                                    # старый круг: время перевода — по умолчанию
    if stage != "sell":                                                  # сеть перевода не выбрана — риск записан
        assert {code for code, _ in paper.cycle_risks(c)} == {"net_unknown"}
    if stage == "buy":
        assert c["buy_fill_price"] == 87.5 and c["buy_slip_pct"] == 0.0



def test_cycle_state_lives_in_db_between_bot_restarts():
    """Покупка с проскальзыванием на «первом процессе», перевод и продажа — на «втором»: цена покупки и риски — из
    базы, факт считает по ним."""
    b, s = ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 91.0, "k")
    hops = {"venues": [], "hops": [_hop("HTX", "KuCoin", net="TRC20", fee=1.0)]}
    cid = paper.start_cycle(10000, b, s, "r", 3.0, ts=time.time() - 400, sell_qty=10000 / 87.5 - 1.0, hops=hops)
    first = Stub(_cfg(min_profit=2.0))
    asyncio.run(first.process_paper_cycles(snap({("HTX", "buy", "USDT"): [ad("HTX", "buy", 87.8, "o")]})))
    c = paper.get_cycle(cid)
    assert c["stage"] == "transfer" and c["buy_fill_price"] == 87.8 and c["transfer_min"] == 3.0
    paper.set_stage(cid, "transfer", ts=time.time() - 200)
    second = Stub(_cfg(min_profit=2.0))                                   # новый процесс — памяти нет
    fresh = snap({("KuCoin", "sell", "USDT"): [ad("KuCoin", "sell", 91.0, "k")]})
    asyncio.run(second.process_paper_cycles(fresh))
    asyncio.run(second.process_paper_cycles(fresh))
    c = paper.get_cycle(cid)
    assert c["result"] == "done"
    assert c["realized_pct"] == pytest.approx(((10000 / 87.8 - 1.0) * 91.0 / 10000 - 1) * 100)
    assert {code for code, _ in paper.cycle_risks(c)} == {"buy_book", "net_unknown"}
    assert [t for t in texts(second) if "завершён" in t]


# --- отчёты ---

def test_report_and_csv_show_new_reasons(tmp_path):
    b, s = ad("HTX", "buy", 87.5, "m"), ad("KuCoin", "sell", 91.0, "k")
    slip = paper.start_cycle(10000, b, s, "r", 3.0, ts=0.0)
    paper.finish_cycle(slip, "failed_buy", note="цена ушла: покупка по 89 ₽ вместо 87.5 ₽ (+1.71%, допуск 1%)", ts=600.0)
    stale = paper.start_cycle(10000, b, s, "r", 3.0, ts=0.0)
    paper.finish_cycle(stale, "failed_buy", note="нет свежей котировки BestChange по USDT (TRC20) (не старше 5 мин)",
                       ts=600.0)
    ok = paper.start_cycle(10000, b, s, "r", 3.0, ts=0.0)
    paper.set_buy_fill(ok, {"price": 87.8, "slip_pct": 0.343, "book": True, "own": 0})
    paper.add_risks(ok, [["net_unknown", "HTX→KuCoin: статус неизвестен — вывод USDT (TRC20) с HTX"]])
    paper.add_risks(ok, [["net_unknown", "HTX→KuCoin: статус неизвестен — вывод USDT (TRC20) с HTX"]])   # без повтора
    paper.finish_cycle(ok, "done", 1.9, ts=900.0)
    assert len(paper.cycle_risks(paper.get_cycle(ok))) == 2
    (row,) = paper.report_rows()
    assert row["fail_reasons"] == {"buy_slip": 1, "bc_stale": 1}
    assert row["net_unknown"] == 1 and row["buy_from_book"] == 1 and row["avg_buy_slip_pct"] == pytest.approx(0.343)
    path = paper.write_report_csv(paper.report_rows(), path=str(tmp_path / "r.csv"))
    with open(path, encoding="utf-8") as f:
        header, rec = list(csv.reader(f))
    rec = dict(zip(header, rec))
    assert rec["fail_reasons"] == "цена покупки ушла дальше допуска:1;нет свежей котировки BestChange:1"
    assert rec["net_unknown"] == "1" and rec["buy_from_book"] == "1" and header[-1] == "failed_by_reason"
    text = Stub(_cfg()).paper_report_view(paper.report_rows())
    assert "причины срывов: цена покупки ушла дальше допуска 1, нет свежей котировки BestChange 1" in text
    assert "статус сети неизвестен 1×" in text and "у других мерчантов 1×" in text and "покупка к плану +0.34%" in text
