"""Метка надёжности, часть 3 (BestChange-факторы): старая выгрузка и резерв обменника впритык — причины риска у
стороны-обменника; биржи не затронуты. И scripts/bc_fields.py — разбор полей info.zip без сети."""
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
