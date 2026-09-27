"""Переводы между площадками (ревью 2026-09-27): сеть, которой нет в известном справочнике получателя, — не
поддерживается (а не «статус неизвестен»); минимум вывода биржи — на каждый перевод стакана обменников, а не на сумму."""
import pytest

import netstatus
import p2p
from helpers import make_ad

SPOT = {"Bybit": {"USDT": (1.0, 1.0)}, "MEXC": {"USDT": (1.0, 1.0)}}
OPEN = {"dep": True, "wd": True}


def _cfg(amount=50000):
    c = p2p.Config(amount=amount)
    c.risk_buffer, c.pay_fee = {}, 0.0
    return c


def _dir(venue, asset="USDT", **nets):
    """Живой справочник площадки: сеть -> (комиссия, минимум вывода)."""
    netstatus._apply(venue, asset, {n: dict(OPEN, fee=fee, min=lo) for n, (fee, lo) in nets.items()})


# --- сеть, которой нет в известном справочнике получателя ---

def test_receiver_directory_without_network_rejects_it():
    """HTX выводит в TRC20 (1 USDT) и BEP20 (0.01); справочник KuCoin знает только TRC20 — BEP20 у получателя не
    поддерживается: выбирается TRC20, а не дешёвая BEP20 «со статусом неизвестно»."""
    _dir("HTX", TRC20=(1.0, None), BEP20=(0.01, None))
    _dir("KuCoin", TRC20=(1.0, None))
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "KuCoin") == (1.0, "TRC20")
    _, route = p2p._route(make_ad("HTX", "buy", 88.0), make_ad("KuCoin", "sell", 90.0), _cfg(), SPOT)
    assert "TRC20" in route and "BEP20" not in route


def test_receiver_directory_without_any_sender_network_blocks_route():
    _dir("HTX", BEP20=(0.01, None), TON=(0.1, None))
    _dir("KuCoin", TRC20=(1.0, None))
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "KuCoin") is None
    assert p2p._route(make_ad("HTX", "buy", 88.0), make_ad("KuCoin", "sell", 90.0), _cfg(), SPOT) is None


def test_unknown_receiver_directory_stays_unknown_status():
    """Справочника получателя нет (биржа без него или запрос не удался) — это прежний «статус неизвестен»: не мешаем."""
    _dir("HTX", TRC20=(1.0, None), BEP20=(0.01, None))
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "KuCoin") == (0.01, "BEP20")
    netstatus.STATUS[("KuCoin", "USDT")] = {}                   # пустой справочник — тоже «сведений нет»
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "KuCoin") == (0.01, "BEP20")


def test_partial_receiver_directory_stays_unknown_status():
    """Справочник HTX по ETH/BTC урезан при разборе (только родная сеть, без обёрнутых токенов), а ETH в ARBITRUM HTX
    принимает: для ввода на HTX «нет в справочнике» у этих монет — «неизвестно», дешёвая сеть остаётся."""
    from conftest import _htx_currency
    j = _htx_currency("https://api.htx.com/v2/reference/currencies?currency=eth")
    netstatus._apply("HTX", "ETH", netstatus._parse_htx(j, "ETH"))
    assert netstatus.known_nets("HTX", "ETH") == ["ERC20"] and netstatus.deposit_nets("HTX", "ETH") == []
    _dir("Bybit", "ETH", ERC20=(0.0015, None), ARBITRUM=(0.0001, None))
    assert p2p._withdraw(_cfg(), "Bybit", "ETH", "", "HTX") == (0.0001, "ARBITRUM")
    _dir("KuCoin", "ETH", ERC20=(0.002, None))                  # полный справочник — ARBITRUM не поддерживается
    assert p2p._withdraw(_cfg(), "Bybit", "ETH", "", "KuCoin") == (0.0015, "ERC20")


@pytest.mark.parametrize("sender", ["LBank", "BitPapa", "Ghost"])
def test_partial_receiver_directory_unknown_when_sender_has_no_directory(sender):
    """У отправителя нет ни живого справочника, ни строки fees.json (LBank, BitPapa, Bybit без ключей) — работает
    запасная проверка «у получателя ввод закрыт во всех сетях». В урезанном справочнике HTX по ETH есть только родная
    ERC20, и ввод в ней закрыт — но HTX может принять ETH в ARBITRUM: это «неизвестно», запасная комиссия, а не отказ.
    Полный справочник (KuCoin) с закрытым вводом во всех сетях по-прежнему отсекает перевод."""
    cfg = _cfg()
    netstatus._apply("HTX", "ETH", {"ERC20": {"dep": False, "wd": True, "fee": 0.002, "min": None}})
    assert netstatus.known_nets("HTX", "ETH") == ["ERC20"] and netstatus.deposit_nets("HTX", "ETH") == []
    assert p2p._withdraw(cfg, sender, "ETH", "", "HTX") == (cfg.transfer_fees["ETH"], "")
    netstatus._apply("KuCoin", "ETH", {"ERC20": {"dep": False, "wd": True, "fee": 0.002, "min": None}})
    assert p2p._withdraw(cfg, sender, "ETH", "", "KuCoin") is None


@pytest.mark.parametrize("raw,want", [("Toncoin(TON)", "TON"), ("Bitcoin(BTC)", "BTC"), ("GRAM", "TON"),
                                      ("TON", "TON"), ("Asset Hub(Polkadot)", "ASSET HUB(POLKADOT)"),
                                      ("Tron(TRC20)", "TRC20"), ("AVAX C-Chain", "AVAX C-CHAIN")])
def test_normalize_names_with_ticker_in_brackets(raw, want):
    """Названия сетей вида «Имя(ТИКЕР)» — по тикеру: иначе сеть TON справочника «Toncoin(TON)» не совпала бы с TON
    отправителя и маршрут отсеялся бы как «сеть не поддерживается»."""
    assert netstatus.normalize(raw) == want


def test_exchanger_network_absent_from_receiver_directory_rejected():
    """Обменник шлёт в своей сети (BEP20) — у биржи-получателя в известном справочнике её нет: связки нет. Та же
    проверка для кошелька на Bybit в связке обменник → обменник."""
    _dir("KuCoin", TRC20=(1.0, None))
    assert p2p._hop(_cfg(), "BestChange", "BEP20", "KuCoin", "", "USDT") == (None, "")
    assert p2p._hop(_cfg(), "BestChange", "TRC20", "KuCoin", "", "USDT")[0] == 0.0
    _dir("Bybit", TRC20=(1.0, None))
    assert p2p._hop(_cfg(), "BestChange", "BEP20", "BestChange", "TRC20", "USDT") == (None, "")
    assert p2p._hop(_cfg(), "BestChange", "TRC20", "BestChange", "TRC20", "USDT")[0] == 1.0


# --- минимум вывода — на каждый перевод ---

def _exchangers(net="TRC20", caps=(5, 5), price=90.0):
    """Объявления обменников одной сети: каждое примет не больше caps[i] USDT."""
    return [make_ad("BestChange", "sell", price, net=net, min_amt=0, max_amt=cap * price, avail=cap) for cap in caps]


def test_min_withdraw_checked_per_transfer_not_on_total():
    """Круг 900 ₽ → 10 USDT уходят двумя переводами по 5 USDT двум обменникам. Минимум вывода 6: сумма (10) его
    проходит, каждый перевод (5) — нет, связки нет. С минимумом 4 — есть."""
    b = make_ad("HTX", "buy", 90.0, min_amt=0)
    s = p2p._stack_qty(_exchangers(), 10.0)
    assert s.parts == 2
    _dir("HTX", TRC20=(0.0, 6.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is None
    assert p2p._route_qty(b, s, _cfg(900), SPOT) is None
    assert p2p.route_hops(b, s, _cfg(900), SPOT) is None
    _dir("HTX", TRC20=(0.0, 4.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is not None


def test_min_withdraw_uses_smallest_transfer_of_uneven_stack():
    """Переводы 8 и 2 USDT: минимум 3 не проходит меньший — связки нет, хотя средний (5) выше минимума."""
    b = make_ad("HTX", "buy", 90.0, min_amt=0)
    s = p2p._stack_qty(_exchangers(caps=(8, 2)), 10.0)
    assert s.parts == 2
    _dir("HTX", TRC20=(0.0, 3.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is None
    _dir("HTX", TRC20=(0.0, 1.5))
    assert p2p._route(b, s, _cfg(900), SPOT) is not None


def test_single_transfer_min_withdraw_unchanged():
    b = make_ad("HTX", "buy", 90.0, min_amt=0)
    s = p2p._stack_qty(_exchangers(caps=(20,)), 10.0)
    assert s.parts == 1
    _dir("HTX", TRC20=(0.0, 6.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is not None
    _dir("HTX", TRC20=(0.0, 11.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is None


def test_min_withdraw_per_transfer_without_known_split_is_even():
    """Стакан собран не здесь (paper восстанавливает Ad только с parts) — переводы считаем поровну."""
    b = make_ad("HTX", "buy", 90.0, min_amt=0)
    s = make_ad("BestChange", "sell", 90.0, net="TRC20", min_amt=0)
    s.parts = 2
    _dir("HTX", TRC20=(0.0, 6.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is None
    _dir("HTX", TRC20=(0.0, 4.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is not None


def test_exchanger_to_exchanger_via_bybit_checks_each_transfer():
    b = make_ad("BestChange", "buy", 90.0, net="TRC20", min_amt=0)
    s = p2p._stack_qty(_exchangers(caps=(5, 5)), 10.0)
    _dir("Bybit", TRC20=(0.0, 6.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is None
    _dir("Bybit", TRC20=(0.0, 4.0))
    assert p2p._route(b, s, _cfg(900), SPOT) is not None


@pytest.mark.parametrize("caps", [(5, 5), (8, 2), (3, 3, 4)])
def test_stack_records_each_transfer(caps):
    s = p2p._stack_qty(_exchangers(caps=caps), 10.0)
    assert s.parts == len(caps) and s.legs == pytest.approx(caps)
