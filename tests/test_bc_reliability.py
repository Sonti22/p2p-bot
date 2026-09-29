"""Метка надёжности, часть 3 (BestChange-факторы): старая выгрузка и резерв обменника впритык — причины риска у
стороны-обменника; биржи не затронуты. И scripts/bc_fields.py — разбор полей info.zip без сети."""
import dataclasses
import io
import os
import sys
import zipfile

import p2p
from helpers import make_ad

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import bc_fields  # noqa: E402

TS = 1_790_000_000.0


def _bc(side, price, reserve_coin, nick="ex1 [TRC20]", net="TRC20", fetched=TS):
    a = make_ad("BestChange", side, price, net=net, avail=reserve_coin, orders=5000, rate=100.0)
    a.nick, a.fetched_ts = nick, fetched
    return a


def _snap(groups, ts=TS):
    return p2p.Snapshot(90.0, "t", {"USDT": 90.0}, {}, [], {}, {}, {}, groups=groups, ts=ts)


def _cfg():
    return p2p.Config(amount=50_000)


def _reasons(deal, snap):
    return [r for _, r in p2p._risks(deal, _cfg(), snap)]


def test_reserve_close_to_round_volume_is_a_risk():
    s = _bc("sell", 92.0, reserve_coin=600.0)              # нужно 50 000 / 92 ≈ 543 USDT, резерв 600 — 1.1×
    b = make_ad("Bybit", "buy", 89.0, orders=5000)
    deal = (2.0, b, s, "перевод −1 USDT (TRC20) на BestChange")
    reasons = _reasons(deal, _snap({("BestChange", "sell", "USDT"): [s]}))
    assert "продажа: резерв обменника впритык (1.1× объёма круга) — может кончиться до перевода" in reasons
    s_big = _bc("sell", 92.0, reserve_coin=5000.0)
    assert not [r for r in _reasons((2.0, b, s_big, ""), _snap({("BestChange", "sell", "USDT"): [s_big]}))
                if "резерв" in r]


def test_reserve_of_a_stack_counts_all_its_exchangers_of_that_network():
    one, two = _bc("sell", 92.0, 400.0, "ex1 [TRC20]"), _bc("sell", 91.9, 300.0, "ex2 [TRC20]")
    other_net = _bc("sell", 92.1, 10_000.0, "ex1 [ERC20]", net="ERC20")   # тот же обменник, другая сеть — не в счёт
    stack = p2p._stack_qty([one, two], 543.0)
    assert stack.parts == 2 and set(stack.nicks) == {"ex1 [TRC20]", "ex2 [TRC20]"}
    snap = _snap({("BestChange", "sell", "USDT"): [other_net, one, two]})
    reasons = _reasons((2.0, make_ad("Bybit", "buy", 89.0, orders=5000), stack, ""), snap)
    assert any("резерв обменника впритык (1.3×" in r for r in reasons)            # 700 / 544


def test_stale_dump_is_a_risk_only_for_exchanger_sides():
    old = _bc("buy", 88.0, 100_000.0, fetched=TS - 25 * 60)
    b_ex = make_ad("HTX", "buy", 88.0, orders=5000)
    b_ex.fetched_ts = TS - 25 * 60                          # у биржи время старое, но это не выгрузка BestChange
    s = make_ad("Bybit", "sell", 90.0, orders=5000)
    snap = _snap({("BestChange", "buy", "USDT"): [old]})
    assert "покупка: выгрузка BestChange 25 мин назад — курс и резерв могли уйти" in _reasons((2.0, old, s, ""), snap)
    assert not [r for r in _reasons((2.0, b_ex, s, ""), snap) if "выгрузка" in r]
    fresh = _bc("buy", 88.0, 100_000.0, fetched=TS - 60)
    assert not [r for r in _reasons((2.0, fresh, s, ""), _snap({("BestChange", "buy", "USDT"): [fresh]}))
                if "выгрузка" in r or "резерв" in r]
    unknown = _bc("buy", 88.0, 100_000.0, fetched=0.0)      # время неизвестно — не считаем старой
    assert not [r for r in _reasons((2.0, unknown, s, ""), _snap({}, ts=TS)) if "выгрузка" in r]


def test_factors_lower_index_and_score():
    s = _bc("sell", 92.0, reserve_coin=600.0, fetched=TS - 30 * 60)
    b = make_ad("Bybit", "buy", 89.0, orders=5000)
    deal = (2.0, b, s, "")
    snap = _snap({("BestChange", "sell", "USDT"): [s]})
    assert p2p.risk_weight(deal, _cfg(), snap) == 2
    assert p2p.reliability_index(deal, _cfg(), snap) == 8


def _banks(side, price, reserve_coin, banks, nick="ex1 [TRC20]"):
    """Строки выгрузки одного обменника — по строке на банк, в каждой его резерв целиком."""
    out = []
    for bank in banks:
        a = _bc(side, price, reserve_coin, nick)
        a.pays = [bank]
        out.append(a)
    return out


def test_reserve_of_an_exchanger_in_several_banks_is_not_multiplied():
    banks = ["Сбербанк", "T-Bank", "Альфа-Банк", "ВТБ"]
    rows = _banks("sell", 92.0, 600.0, banks)                          # нужно 50 000 / 92 ≈ 543, резерв 600 — 1.1×
    snap = _snap({("BestChange", "sell", "USDT"): rows})
    s = p2p._stack_qty(rows, 543.0)
    assert s.parts == 1 and s.pays == ["Сбербанк"]
    b = make_ad("Bybit", "buy", 89.0, orders=5000)
    reasons = _reasons((2.0, b, s, ""), snap)
    assert "продажа: резерв обменника впритык (1.1× объёма круга) — может кончиться до перевода" in reasons   # не 4.4×
    assert p2p._bc_reserve(s, snap) == 600.0
    # в банке связки резерв меньше, чем в других строках обменника, — берётся банк связки
    rows[0].avail = 300.0
    snap = _snap({("BestChange", "sell", "USDT"): rows})
    assert p2p._bc_reserve(rows[0], snap) == 300.0
    other_bank = _bc("sell", 92.0, 1.0)
    other_bank.pays = ["Райффайзен"]                                   # банка связки нет в строках — наибольший из них
    assert p2p._bc_reserve(other_bank, snap) == 600.0
    # стек двух обменников: по каждому — один резерв, по обменникам — сумма
    two = rows + _banks("sell", 91.9, 400.0, banks, "ex2 [TRC20]")
    snap = _snap({("BestChange", "sell", "USDT"): two})
    stack = p2p._combined([rows[1], two[4]], 92.0, 50_000, 543.0)
    assert p2p._bc_reserve(stack, snap) == 1000.0                      # 600 + 400, а не сумма всех 8 строк


def test_stale_dump_is_one_reason_per_route_not_per_side():
    old_b = _bc("buy", 88.0, 100_000.0, "ex1 [TRC20]", fetched=TS - 25 * 60)
    old_s = _bc("sell", 92.0, 100_000.0, "ex2 [TRC20]", fetched=TS - 25 * 60)
    snap = _snap({("BestChange", "buy", "USDT"): [old_b], ("BestChange", "sell", "USDT"): [old_s]})
    deal = (2.0, old_b, old_s, "")
    reasons = _reasons(deal, snap)
    assert [r for r in reasons if "выгрузка" in r] == [
        "покупка и продажа: выгрузка BestChange 25 мин назад — курс и резерв могли уйти"]
    # задержка выгрузки + «обменник → обменник» — риск, а не ловушка
    label, why = p2p.reliability(deal, _cfg(), snap)
    assert label == p2p.RISKY and len(why) == 2
    fresh_b = _bc("buy", 88.0, 100_000.0, "ex1 [TRC20]", fetched=TS - 60)
    assert p2p._bc_stale(fresh_b, old_s, snap) == [
        (1, "продажа: выгрузка BestChange 25 мин назад — курс и резерв могли уйти")]
    assert p2p._bc_stale(fresh_b, make_ad("Bybit", "sell", 90.0), snap) == []


def test_reserves_are_computed_once_per_snapshot():
    rows = _banks("sell", 92.0, 600.0, ["Сбербанк", "T-Bank"])
    snap = _snap({("BestChange", "sell", "USDT"): rows, ("Bybit", "buy", "USDT"): [make_ad(orders=5000)]})
    first = p2p._bc_reserves(snap)
    assert first == {("sell", "USDT", "TRC20"): {"ex1 [TRC20]": {"Сбербанк": 600.0, "T-Bank": 600.0}}}
    b = make_ad("Bybit", "buy", 89.0, orders=5000)
    for s in rows:
        p2p._risks((2.0, b, s, ""), _cfg(), snap)
    assert p2p._bc_reserves(snap) is first                              # не пересчитывается на каждой связке
    # новый снимок с другими стаканами (replace, как в depth_for_deal) — своя карта
    more = dataclasses.replace(snap, groups={("BestChange", "sell", "USDT"): _banks("sell", 92.0, 900.0, ["ВТБ"])})
    assert p2p._bc_reserves(more) == {("sell", "USDT", "TRC20"): {"ex1 [TRC20]": {"ВТБ": 900.0}}}
    assert p2p._bc_reserves(snap) is first
    snap.groups = {}                                                    # подставили другие стаканы — пересчёт
    assert p2p._bc_reserves(snap) == {}


def _zip(exch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bm_exch.dat", exch.encode("cp1251"))
    return buf.getvalue()


def test_bc_fields_lists_columns_and_rare_flag_values(tmp_path, capsys):
    rows = "\n".join(f"{i};Обменник{i};{'1' if i == 7 else '0'};{i * 10}" for i in range(1, 21))
    path = tmp_path / "info.zip"
    path.write_bytes(_zip(rows))
    assert bc_fields.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "строк: 20, колонок: 4" in out
    assert "[2] заполнено 20/20, разных 2" in out and "редкое '1' у 1: Обменник7" in out
    assert bc_fields.main([str(tmp_path / "nope.zip")]) == 2
