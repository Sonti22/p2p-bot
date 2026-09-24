import asyncio

import pytest

import netstatus
import p2p
from conftest import _htx_currency, _kucoin_currency
from helpers import make_ad


@pytest.mark.parametrize("raw,want", [
    ("TRC20", "TRC20"), ("Tron(TRC20)", "TRC20"), ("TRX", "TRC20"),
    ("BEP20", "BEP20"), ("BEP20(BSC)", "BEP20"), ("BSC", "BEP20"),
    ("ERC20", "ERC20"), ("ETH", "ERC20"), ("Ethereum(ERC20)", "ERC20"),
    ("TON", "TON"), ("SOL", "SOL"), ("SOLANA", "SOL"), ("POLYGON", "POLYGON"), ("MATIC", "POLYGON"),
    ("ARBITRUM", "ARBITRUM"), ("ARBI", "ARBITRUM"), ("APT", "APT"), ("APTOS", "APT"), ("BTC", "BTC"),
    ("ETHT", "ETHT"), ("OPTIMISM", "OPTIMISM"), ("Asset Hub(Polkadot)", "ASSET HUB(POLKADOT)"),
])
def test_normalize(raw, want):
    assert netstatus.normalize(raw) == want


def test_parse_public_fixtures():
    htx = netstatus._parse_htx(_htx_currency("https://api.htx.com/v2/reference/currencies?currency=usdt"))
    assert htx["BEP20"]["wd"] is True and htx["BEP20"]["dep"] is True and htx["BEP20"]["fee"] == pytest.approx(0.01)
    assert htx["POLYGON"]["wd"] is False           # в фикстуре вывод POLYGON у HTX закрыт
    ku = netstatus._parse_kucoin(_kucoin_currency("https://api.kucoin.com/api/v3/currencies/USDT"))
    assert ku["BEP20"]["wd"] is True and ku["TON"]["wd"] is True   # из двух записей TON взята открытая


def test_parse_bybit_and_mexc_shapes():
    by = netstatus._parse_bybit({"result": {"rows": [{"coin": "USDT", "chains": [
        {"chain": "TRX", "chainType": "Tron (TRC20)", "chainDeposit": "1", "chainWithdraw": "0", "withdrawFee": "1"},
        {"chain": "BSC", "chainType": "BSC (BEP20)", "chainDeposit": "1", "chainWithdraw": "1", "withdrawFee": "0.2"}]}]}})
    assert by["TRC20"]["wd"] is False and by["BEP20"]["fee"] == pytest.approx(0.2)
    mx = netstatus._parse_mexc([{"coin": "USDT", "networkList": [
        {"network": "TRC20", "depositEnable": True, "withdrawEnable": True, "withdrawFee": "1"},
        {"network": "BEP20(BSC)", "depositEnable": False, "withdrawEnable": True, "withdrawFee": "0.01"}]},
        {"coin": "ETH", "networkList": [{"network": "ERC20", "depositEnable": True, "withdrawEnable": True}]}], "USDT")
    assert mx["BEP20"]["dep"] is False and "ERC20" not in mx


def test_refresh_offline_fills_status_and_detects_changes(offline):
    asyncio.run(netstatus.refresh(None, ["USDT", "ETH"], ["htx", "kucoin", "bybit"], p2p._json))
    assert netstatus.withdraw_ok("HTX", "USDT", "BEP20") is True
    assert netstatus.withdraw_ok("KuCoin", "USDT", "BEP20") is True
    assert netstatus.withdraw_ok("Bybit", "USDT", "BEP20") is None    # ключа нет — статус неизвестен
    assert not netstatus.pop_changes()                                 # первая загрузка — без алертов
    netstatus._apply("HTX", "USDT", {"BEP20": {"dep": True, "wd": False, "fee": 0.01},
                                     "XYZ": {"dep": False, "wd": False, "fee": 1}})
    assert netstatus.pop_changes() == [("HTX", "USDT", "BEP20", "вывод", False)]   # XYZ не из KNOWN_NETS — молчим
    assert not netstatus.pop_changes()


def test_refresh_if_due_throttles(offline):
    calls = []

    async def counting(s, method, url, body=None):
        calls.append(url)
        return await p2p._json(s, method, url, body)

    assert asyncio.run(netstatus.refresh_if_due(None, ["USDT"], ["htx"], counting)) == {}
    assert asyncio.run(netstatus.refresh_if_due(None, ["USDT"], ["htx"], counting)) is None
    assert len(calls) == 1


def test_refresh_keeps_old_data_on_error():
    async def boom(s, method, url):
        raise RuntimeError("down")

    netstatus._apply("HTX", "USDT", {"TRC20": {"dep": True, "wd": True, "fee": 1.0}})
    errors = asyncio.run(netstatus.refresh(None, ["USDT"], ["htx"], boom))
    assert "HTX" in errors and netstatus.withdraw_ok("HTX", "USDT", "TRC20") is True


def _cfg():
    c = p2p.Config()
    c.risk_buffer, c.pay_fee = {}, 0.0
    return c


SPOT = {"Bybit": {"USDT": (1.0, 1.0)}, "MEXC": {"USDT": (1.0, 1.0)}}


def test_route_skips_closed_network_and_blocks_when_all_closed():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "BEP20" in route                                     # по таблице дешевле всего BEP20
    closed = {"dep": True, "wd": False, "fee": 0.01}
    netstatus._apply("MEXC", "USDT", {"BEP20": closed, "TON": closed, "ERC20": closed,
                                      "TRC20": {"dep": True, "wd": True, "fee": 1.0}})
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "TRC20" in route and "BEP20" not in route            # дешёвые сети закрыты — берём открытую TRC20
    netstatus._apply("MEXC", "USDT", {n: {"dep": True, "wd": False, "fee": 1.0} for n in ("BEP20", "TRC20", "ERC20", "TON")})
    assert p2p._route(b, s, _cfg(), SPOT) is None               # все известные сети закрыты — связки нет
    p2p.profit_breakdown(b, s, _cfg(), SPOT)                    # разложение прибыли не падает на закрытом маршруте


def test_route_respects_receiver_deposit_and_exchanger_net():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    no_dep = {"dep": False, "wd": True, "fee": 0.2}
    netstatus._apply("Bybit", "USDT", {"BEP20": no_dep, "TON": no_dep, "ERC20": no_dep,
                                       "TRC20": {"dep": True, "wd": True, "fee": 1.0}})
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "TRC20" in route                                     # у получателя закрыт ввод в дешёвых сетях
    ex = make_ad("BestChange", "sell", 89.9, net="TRC20")
    netstatus._apply("MEXC", "USDT", {"TRC20": {"dep": True, "wd": False, "fee": 1.0}})
    assert p2p._route(b, ex, _cfg(), SPOT) is None              # обменнику нужен TRC20, а вывод в нём закрыт


def test_live_fee_extends_table_for_venues_without_one():
    b, s = make_ad("HTX", "buy", 88.0), make_ad("MEXC", "sell", 90.0)
    netstatus._apply("HTX", "USDT", {"BEP20": {"dep": True, "wd": True, "fee": 0.01},
                                     "TRC20": {"dep": True, "wd": True, "fee": 1.0}})
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "BEP20" in route and "−0.01 USDT" in route           # у HTX нет таблицы — комиссия из справочника


def test_scan_offline_refreshes_networks(offline):
    c = p2p.Config(exchanges=["htx", "kucoin"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert netstatus.withdraw_ok("KuCoin", "USDT", "TRC20") is not None
    assert not [k for k in snap.errors if k.startswith("сети")]
