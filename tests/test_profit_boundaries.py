"""Границы расчёта связки, которые пропускали мутации p2p.py (проверка 2026-09-28): минимум вывода биржи и минимум
объявления включительны (ровно минимум — можно), стакан по сумме круга допускает недобор не больше 0.01 ₽."""
import pytest

import netstatus
import p2p
from helpers import make_ad

SPOT = {"Bybit": {"USDT": (1.0, 1.0)}, "MEXC": {"USDT": (1.0, 1.0)}}


def _cfg(amount=50000):
    c = p2p.Config(amount=amount)
    c.risk_buffer, c.pay_fee = {}, 0.0
    return c


# --- минимум вывода: сумма ровно на минимуме проходит, на копейку меньше — нет ---

def test_withdraw_exactly_at_exchange_minimum_is_allowed():
    netstatus._apply("HTX", "USDT", {"TRC20": {"dep": True, "wd": True, "fee": 1.0, "min": 500.0}})
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "Bybit", qty=500.0) == (1.0, "TRC20")
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "Bybit", qty=499.99) is None


def test_route_at_exact_minimum_withdraw_is_possible():
    # 44 000 ₽ / 88 = 500 USDT — ровно минимум вывода HTX в TRC20
    netstatus._apply("HTX", "USDT", {"TRC20": {"dep": True, "wd": True, "fee": 1.0, "min": 500.0}})
    b, s = make_ad("HTX", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    profit, route = p2p._route(b, s, _cfg(44000), SPOT)
    assert "TRC20" in route
    assert profit == pytest.approx(((500 - 1.0) * 90 / 44000 - 1) * 100)


# --- минимум объявления в стакане по сумме (_stack): остаток ровно на минимуме берётся ---

def test_stack_takes_ad_when_remainder_equals_its_minimum():
    first = make_ad("Bybit", "buy", 100.0, min_amt=1000, max_amt=30000, avail=1000)
    second = make_ad("Bybit", "buy", 101.0, min_amt=20000, max_amt=100000, avail=1000)
    st = p2p._stack([first, second], 50000)
    assert st is not None and st.parts == 2
    assert st.price == pytest.approx(50000 / (30000 / 100.0 + 20000 / 101.0))


def test_stack_skips_ad_when_remainder_just_below_its_minimum():
    first = make_ad("Bybit", "buy", 100.0, min_amt=1000, max_amt=30000, avail=1000)
    second = make_ad("Bybit", "buy", 101.0, min_amt=20000.01, max_amt=100000, avail=1000)
    assert p2p._stack([first, second], 50000) is None


# --- минимум объявления в стакане по количеству (_stack_qty) ---

def test_stack_qty_takes_ad_when_remainder_equals_its_minimum():
    first = make_ad("Bybit", "sell", 90.0, min_amt=0, max_amt=100000, avail=5.0)
    second = make_ad("Bybit", "sell", 89.0, min_amt=5 * 89.0, max_amt=100000, avail=100.0)
    st = p2p._stack_qty([first, second], 10.0)
    assert st is not None and st.parts == 2
    assert st.price == pytest.approx((5 * 90.0 + 5 * 89.0) / 10)


def test_stack_qty_skips_ad_when_remainder_just_below_its_minimum():
    first = make_ad("Bybit", "sell", 90.0, min_amt=0, max_amt=100000, avail=5.0)
    second = make_ad("Bybit", "sell", 89.0, min_amt=5 * 89.0 + 0.01, max_amt=100000, avail=100.0)
    assert p2p._stack_qty([first, second], 10.0) is None


# --- недобор глубины по сумме: до 0.01 ₽ — округление, больше — глубины не хватает ---

def test_stack_rejects_shortfall_above_one_kopeck():
    ad = make_ad("Bybit", "buy", 100.0, min_amt=0, max_amt=49999.98, avail=1000)   # недобор 0.02 ₽
    assert p2p._stack([ad], 50000) is None


def test_stack_accepts_rounding_shortfall_within_one_kopeck():
    ad = make_ad("Bybit", "buy", 100.0, min_amt=0, max_amt=49999.991, avail=1000)  # недобор 0.009 ₽
    st = p2p._stack([ad], 50000)
    assert st is not None
    assert st.price == pytest.approx(50000 / (49999.991 / 100.0))


# --- обменник → Bybit → стакан обменников: вывод с Bybit — на каждый из переводов ---

def test_exchanger_to_exchanger_stack_via_bybit_fee_times_parts():
    ads = [make_ad("BestChange", "sell", 89.9 - i * 0.1, net="ERC20", min_amt=500, max_amt=25_000,
                   avail=25_000 / (89.9 - i * 0.1)) for i in range(3)]
    b = make_ad("BestChange", "buy", 85.0, net="TRC20")
    profit, _, s, route = p2p._match(b, ads, _cfg(), SPOT)
    assert s.parts == 3
    assert "через Bybit: перевод −2.4 USDT (ERC20) ×3" in route   # 0.8 USDT × 3
    assert profit == pytest.approx(((50000 / 85.0 - 2.4) * s.price / 50000 - 1) * 100)
