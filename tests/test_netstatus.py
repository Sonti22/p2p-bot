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
    assert htx["BEP20"]["min"] == pytest.approx(1)
    assert htx["POLYGON"]["wd"] is False           # в фикстуре вывод POLYGON у HTX закрыт
    ku = netstatus._parse_kucoin(_kucoin_currency("https://api.kucoin.com/api/v3/currencies/USDT"))
    assert ku["BEP20"]["wd"] is True and ku["TON"]["wd"] is True   # из двух записей TON взята открытая
    assert ku["BEP20"]["min"] == pytest.approx(10) and ku["TRC20"]["min"] == pytest.approx(4)


@pytest.mark.parametrize("asset,native,fee,wrapped_net", [
    ("BTC", "BTC", 0.00005, "TRC20"),    # chain trc20btc/trc20wbtc — обёрнутый BTC на Tron, не настоящая сеть
    ("ETH", "ERC20", 0.0005, "BEP20"),   # chain bep20eth — обёрнутый ETH на BSC, не настоящая сеть
])
def test_parse_htx_ignores_wrapped_tokens_for_btc_eth(asset, native, fee, wrapped_net):
    # HTX вместе с настоящей сетью BTC/ETH отдаёт обёрнутые токены на чужих блокчейнах (trc20btc,
    # trc20wbtc, wbtc, bep20eth...) с displayName TRC20/BEP20/ERC20 — это не настоящий вывод BTC/ETH
    # в этой сети, и его нельзя путать с настоящими TRC20/BEP20/ERC20 у USDT/USDC.
    j = _htx_currency(f"https://api.htx.com/v2/reference/currencies?currency={asset.lower()}")
    htx = netstatus._parse_htx(j, asset)
    assert set(htx) == {native}
    assert htx[native]["fee"] == pytest.approx(fee)
    # без asset (как для USDT/USDC) обёрнутые сети по-прежнему разбираются как есть — это и была причина бага
    raw = netstatus._parse_htx(j)
    assert wrapped_net in raw and raw[wrapped_net]["fee"] != pytest.approx(fee)


def test_route_htx_btc_does_not_use_wrapped_trc20():
    # раньше маршрут HTX(BTC)→Bybit/BitPapa шёл «через TRC20» с копеечной комиссией обёрнутого токена —
    # теперь у HTX для BTC остаётся только настоящая сеть BTC.
    j = _htx_currency("https://api.htx.com/v2/reference/currencies?currency=btc")
    netstatus._apply("HTX", "BTC", netstatus._parse_htx(j, "BTC"))
    fee, net = p2p._withdraw(_cfg(), "HTX", "BTC", "", "Bybit")
    assert net == "BTC" and fee == pytest.approx(0.00005)
    # BitPapa принимает по RECEIVE_NETS только TRC20, но это ограничение не для BTC — сеть BTC не блокируется
    fee, net = p2p._withdraw(_cfg(), "HTX", "BTC", "", "BitPapa")
    assert net == "BTC" and fee == pytest.approx(0.00005)


def test_parse_bybit_and_mexc_shapes():
    by = netstatus._parse_bybit({"result": {"rows": [{"coin": "USDT", "chains": [
        {"chain": "TRX", "chainType": "Tron (TRC20)", "chainDeposit": "1", "chainWithdraw": "0", "withdrawFee": "1", "withdrawMin": "5"},
        {"chain": "BSC", "chainType": "BSC (BEP20)", "chainDeposit": "1", "chainWithdraw": "1", "withdrawFee": "0.2", "withdrawMin": "1"}]}]}})
    assert by["TRC20"]["wd"] is False and by["BEP20"]["fee"] == pytest.approx(0.2)
    assert by["BEP20"]["min"] == pytest.approx(1)
    mx = netstatus._parse_mexc([{"coin": "USDT", "networkList": [
        {"network": "TRC20", "depositEnable": True, "withdrawEnable": True, "withdrawFee": "1", "withdrawMin": "10"},
        {"network": "BEP20(BSC)", "depositEnable": False, "withdrawEnable": True, "withdrawFee": "0.01"}]},
        {"coin": "ETH", "networkList": [{"network": "ERC20", "depositEnable": True, "withdrawEnable": True}]}], "USDT")
    assert mx["BEP20"]["dep"] is False and "ERC20" not in mx
    assert mx["TRC20"]["min"] == pytest.approx(10) and mx["BEP20"]["min"] is None   # нет поля — сведений нет


def test_refresh_offline_fills_status_and_detects_changes(offline):
    asyncio.run(netstatus.refresh(None, ["USDT", "ETH"], ["htx", "kucoin", "bybit"], p2p._json))
    assert netstatus.withdraw_ok("HTX", "USDT", "BEP20") is True
    assert netstatus.withdraw_ok("KuCoin", "USDT", "BEP20") is True
    assert netstatus.withdraw_ok("Bybit", "USDT", "BEP20") is None    # ключа нет — статус неизвестен
    assert netstatus.min_withdraw("HTX", "USDT", "BEP20") == pytest.approx(1)
    assert netstatus.min_withdraw("KuCoin", "USDT", "TRC20") == pytest.approx(4)
    assert netstatus.min_withdraw("Bybit", "USDT", "BEP20") is None   # ключа нет — сведений нет
    assert netstatus.min_withdraw("HTX", "USDT", "XYZ") is None       # такой сети нет вовсе
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


@pytest.mark.parametrize("parse,bad", [
    (netstatus._parse_htx, {"code": 500, "message": "boom"}),
    (netstatus._parse_kucoin, {"code": "400001", "msg": "bad request"}),
    (netstatus._parse_bybit, {"retCode": 10001, "retMsg": "params error"}),
])
def test_parse_raises_on_api_error_in_body(parse, bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_parse_mexc_raises_on_api_error_in_body():
    with pytest.raises(ValueError):
        netstatus._parse_mexc({"code": 700002, "msg": "signature invalid"}, "USDT")


def test_refresh_keeps_old_data_on_http_200_api_error():
    """HTTP 200, но ошибка в теле (retCode/code не «успех») — не должно стирать прежний статус сети,
    как если бы у площадки внезапно не осталось ни одной сети."""
    async def bad_body(s, method, url):
        return {"code": 500, "message": "boom"}

    netstatus._apply("HTX", "USDT", {"TRC20": {"dep": True, "wd": True, "fee": 1.0}})
    errors = asyncio.run(netstatus.refresh(None, ["USDT"], ["htx"], bad_body))
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


# Живой справочник MEXC: BEP20 подорожал до 10 (в таблице 0.01), TON и ERC20 до 5 — дешевле всех TRC20.
LIVE_MEXC = {"BEP20": {"dep": True, "wd": True, "fee": 10.0}, "TRC20": {"dep": True, "wd": True, "fee": 1.0},
             "TON": {"dep": True, "wd": True, "fee": 5.0}, "ERC20": {"dep": True, "wd": True, "fee": 5.0}}


def test_live_fee_overrides_table_same_net():
    assert p2p.WITHDRAW[("MEXC", "USDT")]["BEP20"] == pytest.approx(0.01)   # табличная из fees.json
    netstatus._apply("MEXC", "USDT", LIVE_MEXC)
    assert p2p._withdraw(_cfg(), "MEXC", "USDT", "BEP20") == (10.0, "BEP20")       # явная сеть — живое значение
    assert p2p._withdraw(_cfg(), "MEXC", "USDT", "", "Bybit") == (1.0, "TRC20")   # автовыбор — по живым комиссиям


def test_route_uses_live_fee_over_table():
    b, s = make_ad("MEXC", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "BEP20" in route and "−0.01 USDT" in route                              # без живых данных — таблица
    netstatus._apply("MEXC", "USDT", LIVE_MEXC)
    profit, route = p2p._route(b, s, _cfg(), SPOT)
    assert "TRC20" in route and "−1 USDT" in route and "BEP20" not in route      # живое значение приоритетнее
    assert profit == pytest.approx(((50000 / 88 - 1.0) * 90 / 50000 - 1) * 100)


def test_route_blocks_when_all_known_nets_closed_without_table():
    b, s = make_ad("HTX", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    assert ("HTX", "USDT") not in p2p.WITHDRAW                  # у HTX нет таблицы комиссий
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "−1 USDT на Bybit" in route                          # сведений нет — запасная комиссия
    netstatus._apply("HTX", "USDT", {n: {"dep": True, "wd": False, "fee": 1.0} for n in ("TRC20", "BEP20", "ERC20", "TON")})
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "Bybit") is None
    assert p2p._route(b, s, _cfg(), SPOT) is None               # справочник есть, и все его сети закрыты
    assert p2p.profit_breakdown(b, s, _cfg(), SPOT) is None
    netstatus.reset()
    assert p2p._route(b, s, _cfg(), SPOT) is not None           # сведений снова нет — не мешаем


def test_closed_net_blocks_only_nets_receiver_accepts():
    b, s = make_ad("HTX", "buy", 88.0), make_ad("BitPapa", "sell", 94.0)
    # у HTX открыт вывод только в BEP20 без известной комиссии, TRC20 закрыт — а BitPapa принимает лишь TRC20
    netstatus._apply("HTX", "USDT", {"TRC20": {"dep": True, "wd": False, "fee": 1.0},
                                     "BEP20": {"dep": True, "wd": True, "fee": None}})
    assert p2p._route(b, s, _cfg(), SPOT) is None
    assert p2p._route(b, make_ad("Bybit", "sell", 90.0), _cfg(), SPOT) is not None   # Bybit примет BEP20
    netstatus._apply("HTX", "USDT", {"BEP20": {"dep": True, "wd": False, "fee": 0.01}})
    assert p2p._route(b, s, _cfg(), SPOT) is None               # справочник есть, TRC20 в нём нет
    netstatus.reset()
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "−1 USDT на BitPapa" in route                        # сведений нет — запасная комиссия


def test_listed_directory_without_receiver_net_blocks_route():
    # справочник KuCoin по USDC известен, но TRC20 в нём нет, а BitPapa принимает только TRC20
    netstatus._apply("KuCoin", "USDC", {"ERC20": {"dep": True, "wd": True, "fee": 5.0},
                                        "BEP20": {"dep": True, "wd": True, "fee": 1.0}})
    b, s = make_ad("KuCoin", "buy", 88.0, asset="USDC"), make_ad("BitPapa", "sell", 94.0, asset="USDC")
    assert p2p._withdraw(_cfg(), "KuCoin", "USDC", "", "BitPapa") is None
    assert p2p._route(b, s, _cfg(), SPOT) is None
    assert p2p._route(b, make_ad("Bybit", "sell", 90.0, asset="USDC"), _cfg(), SPOT) is not None   # Bybit примет BEP20
    netstatus.reset()
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "−1 USDC на BitPapa" in route                        # сведений нет — запасная комиссия
    # ETH в TRC20 не бывает: RECEIVE_NETS BitPapa (TRC20) — ограничение только для монет, у которых
    # TRC20 в принципе существует (USDT/USDC); для ETH оно не действует, и берётся настоящая сеть ERC20.
    netstatus._apply("HTX", "ETH", {"ERC20": {"dep": True, "wd": True, "fee": 0.002}})
    assert p2p._withdraw(_cfg(), "HTX", "ETH", "", "BitPapa") == (0.002, "ERC20")


def test_exchanger_to_exchange_checks_receiver_deposit():
    b, s = make_ad("BestChange", "buy", 88.0, net="TRC20"), make_ad("MEXC", "sell", 90.0)
    netstatus._apply("MEXC", "USDT", {"TRC20": {"dep": False, "wd": True, "fee": 1.0}})
    assert p2p._hop(_cfg(), "BestChange", "TRC20", "MEXC", "", "USDT") == (None, "")
    assert p2p._route(b, s, _cfg(), SPOT) is None               # у биржи закрыт ввод в сети обменника
    netstatus._apply("MEXC", "USDT", {"TRC20": {"dep": True, "wd": True, "fee": 1.0}})
    _, route = p2p._route(b, s, _cfg(), SPOT)
    assert "обменник шлёт USDT (TRC20) на MEXC" in route
    # кросс-монета: обменник шлёт USDT на спот Bybit, а там ввод TRC20 закрыт
    netstatus._apply("Bybit", "USDT", {"TRC20": {"dep": False, "wd": True, "fee": 1.0}})
    spot = {"Bybit": {"USDT": (1.0, 1.0), "BTC": (5_000_000.0, 5_000_000.0)}}
    assert p2p._route(b, make_ad("Bybit", "sell", 5_200_000.0, asset="BTC"), _cfg(), spot) is None
    # обменник → обменник через кошелёк на Bybit: ввод TRC20 на Bybit закрыт
    assert p2p._route(b, make_ad("BestChange", "sell", 90.0, net="BEP20"), _cfg(), SPOT) is None
    netstatus.reset()
    _, route = p2p._route(b, make_ad("BestChange", "sell", 90.0, net="BEP20"), _cfg(), SPOT)
    assert "через Bybit" in route                               # сведений нет — не мешаем


def test_exchanger_to_bitpapa_only_in_trc20():
    s = make_ad("BitPapa", "sell", 92.0)
    assert p2p._hop(_cfg(), "BestChange", "BEP20", "BitPapa", "", "USDT") == (None, "")
    for net in ("BEP20", "TON", "ERC20"):   # BitPapa принимает только TRC20 (RECEIVE_NETS)
        assert p2p._route(make_ad("BestChange", "buy", 88.0, net=net), s, _cfg(), SPOT) is None
    _, route = p2p._route(make_ad("BestChange", "buy", 88.0, net="TRC20"), s, _cfg(), SPOT)
    assert "обменник шлёт USDT (TRC20) на BitPapa" in route
    _, route = p2p._route(make_ad("BestChange", "buy", 88.0, net="BEP20"), make_ad("MEXC", "sell", 90.0), _cfg(), SPOT)
    assert "обменник шлёт USDT (BEP20) на MEXC" in route        # у бирж ограничения сети нет
    # BTC и ETH в TRC20 не ходят — ограничение BitPapa на них не распространяется
    assert p2p._hop(_cfg(), "BestChange", "BTC", "BitPapa", "", "BTC") == (0.0, "обменник шлёт BTC (BTC) на BitPapa")
    assert p2p._hop(_cfg(), "BestChange", "ERC20", "BitPapa", "", "ETH")[0] == 0.0


def test_route_skips_network_below_min_withdraw():
    # сумма вывода (50000/88 ≈ 568 USDT) ниже минимума BEP20 у HTX — сеть недоступна, как закрытая,
    # хотя её комиссия дешевле; выбирается TRC20, для которого минимум неизвестен
    b, s = make_ad("HTX", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    netstatus._apply("HTX", "USDT", {"BEP20": {"dep": True, "wd": True, "fee": 0.01, "min": 10000},
                                     "TRC20": {"dep": True, "wd": True, "fee": 1.0, "min": None}})
    profit, route = p2p._route(b, s, _cfg(), SPOT)
    assert "TRC20" in route and "−1 USDT" in route and "BEP20" not in route
    assert profit == pytest.approx(((50000 / 88 - 1.0) * 90 / 50000 - 1) * 100)


def test_route_blocks_when_amount_below_min_withdraw_everywhere():
    b, s = make_ad("HTX", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    netstatus._apply("HTX", "USDT", {n: {"dep": True, "wd": True, "fee": 1.0, "min": 100000}
                                     for n in ("TRC20", "BEP20", "ERC20", "TON")})
    assert p2p._withdraw(_cfg(), "HTX", "USDT", "", "Bybit", qty=568.0) is None
    assert p2p._route(b, s, _cfg(), SPOT) is None
    assert p2p.profit_breakdown(b, s, _cfg(), SPOT) is None


def test_min_withdraw_ignored_when_withdraw_stage_disabled():
    # валовый спред (disable содержит "withdraw") не проверяет минимум вывода — только реальные
    # стадии с комиссией вывода из profit_breakdown должны блокироваться
    b, s = make_ad("HTX", "buy", 88.0), make_ad("Bybit", "sell", 90.0)
    netstatus._apply("HTX", "USDT", {n: {"dep": True, "wd": True, "fee": 1.0, "min": 100000}
                                     for n in ("TRC20", "BEP20", "ERC20", "TON")})
    gross = p2p._route_qty(b, s, _cfg(), SPOT, disable=frozenset({"bank", "withdraw", "spot", "risk"}))
    assert gross is not None
    assert p2p.profit_breakdown(b, s, _cfg(), SPOT) is None


def test_scan_offline_refreshes_networks(offline):
    c = p2p.Config(exchanges=["htx", "kucoin"], assets=["USDT"], min_orders=0, min_rate=0)
    snap = asyncio.run(p2p.scan(None, c))
    assert netstatus.withdraw_ok("KuCoin", "USDT", "TRC20") is not None
    assert not [k for k in snap.errors if k.startswith("сети")]
