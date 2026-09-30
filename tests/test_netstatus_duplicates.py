"""Дубли записей одной сети в справочниках площадок (после normalize() несколько строк схлопываются в одну сеть):
у всех четырёх разборщиков должна побеждать запись с открытым выводом — независимо от порядка строк в ответе биржи
(правило уже было у KuCoin, задача распространяет его на HTX/Bybit/MEXC через netstatus._merge_net)."""
import pytest

import netstatus


def _htx(rows):
    return {"code": 200, "data": [{"chains": [
        {"chain": name.lower(), "displayName": name, "depositStatus": "allowed",
         "withdrawStatus": "allowed" if wd else "prohibited",
         "transactFeeWithdraw": str(fee), "minWithdrawAmt": "1"} for name, wd, fee in rows]}]}


def _kucoin(rows):
    return {"code": "200000", "data": {"chains": [
        {"chainName": name, "isDepositEnabled": True, "isWithdrawEnabled": wd,
         "withdrawalMinFee": fee, "withdrawalMinSize": 1} for name, wd, fee in rows]}}


def _bybit(rows):
    return {"retCode": 0, "result": {"rows": [{"chains": [
        {"chainType": name, "chainDeposit": "1", "chainWithdraw": "1" if wd else "0",
         "withdrawFee": str(fee), "withdrawMin": "1"} for name, wd, fee in rows]}]}}


def _mexc(rows):
    return [{"coin": "USDT", "networkList": [
        {"network": name, "depositEnable": True, "withdrawEnable": wd,
         "withdrawFee": str(fee), "withdrawMin": "1"} for name, wd, fee in rows]}]


VENUES = [
    ("HTX", _htx, lambda j: netstatus._parse_htx(j), ("TRC20", "Tron(TRC20)")),
    ("KuCoin", _kucoin, netstatus._parse_kucoin, ("TRC20", "Tron(TRC20)")),
    ("Bybit", _bybit, netstatus._parse_bybit, ("Tron (TRC20)", "TRX")),
    ("MEXC", _mexc, lambda j: netstatus._parse_mexc(j, "USDT"), ("TRC20", "TRX")),
]
IDS = [v[0] for v in VENUES]


@pytest.mark.parametrize("venue, build, parse, names", VENUES, ids=IDS)
def test_duplicate_net_open_wins_any_order(venue, build, parse, names):
    n1, n2 = names
    forward = parse(build([(n1, False, 1.0), (n2, True, 2.0)]))
    backward = parse(build([(n2, True, 2.0), (n1, False, 1.0)]))
    assert forward["TRC20"]["wd"] is True and forward["TRC20"]["fee"] == 2.0
    assert backward["TRC20"]["wd"] is True and backward["TRC20"]["fee"] == 2.0
    assert forward == backward


@pytest.mark.parametrize("venue, build, parse, names", VENUES, ids=IDS)
def test_duplicate_net_both_closed_stays_closed(venue, build, parse, names):
    n1, n2 = names
    for rows in ([(n1, False, 1.0), (n2, False, 2.0)], [(n2, False, 2.0), (n1, False, 1.0)]):
        r = parse(build(rows))
        assert "TRC20" in r and r["TRC20"]["wd"] is False


@pytest.mark.parametrize("venue, build, parse, names", VENUES, ids=IDS)
def test_duplicate_net_both_open_keeps_first(venue, build, parse, names):
    n1, n2 = names
    forward = parse(build([(n1, True, 1.0), (n2, True, 2.0)]))
    backward = parse(build([(n2, True, 2.0), (n1, True, 1.0)]))
    assert forward["TRC20"]["fee"] == 1.0
    assert backward["TRC20"]["fee"] == 2.0


@pytest.mark.parametrize("venue, build, parse, names", VENUES, ids=IDS)
def test_withdraw_ok_after_apply_is_order_independent(venue, build, parse, names):
    n1, n2 = names
    for rows in ([(n1, False, 1.0), (n2, True, 2.0)], [(n2, True, 2.0), (n1, False, 1.0)]):
        netstatus.reset()
        netstatus._apply(venue, "USDT", parse(build(rows)))
        assert netstatus.withdraw_ok(venue, "USDT", "TRC20") is True
        assert "TRC20" in netstatus.open_nets(venue, "USDT")


@pytest.mark.parametrize("venue, build, parse, names", VENUES, ids=IDS)
def test_reordered_duplicates_do_not_raise_change_alert(venue, build, parse, names):
    n1, n2 = names
    netstatus._apply(venue, "USDT", parse(build([(n1, False, 1.0), (n2, True, 2.0)])))
    netstatus.pop_changes()
    netstatus._apply(venue, "USDT", parse(build([(n2, True, 2.0), (n1, False, 1.0)])))
    assert netstatus.pop_changes() == []
