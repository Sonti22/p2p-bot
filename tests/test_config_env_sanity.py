"""Config.from_env: числовые настройки и комиссии читались голым int()/float() — MAX_DEV=nan молча выключал отсев
аномальных цен (сравнение с nan всегда False), MAX_DEV=0 отсекал всё, MAX_DEV=4% или пустое значение роняли бота при
старте, отрицательные PAY_FEE/SPOT_FEES/RISK_BUFFER/TRANSFER_FEES завышали прибыль каждой связки, десятичная запятая
в комиссиях (BTC:0,0002) давала нулевую комиссию монеты. _parse_num/_env_num/_env_fees делают эти поля толерантными
(мусор -> значение по умолчанию + warning), не трогая строгий общий `_fees` (на нём стоит скрипт порогов хеджа)."""
import math

import pytest

import p2p

NUMERIC_FIELDS = {
    "MIN_ORDERS": dict(attr="min_orders", default=100, too_big="2000000", too_small="-5"),
    "MIN_RATE": dict(attr="min_rate", default=95.0, too_big="150", too_small="-5"),
    "MAX_DEV": dict(attr="max_dev", default=4.0, too_big="80", too_small="0.05"),
    "INTERVAL": dict(attr="interval", default=20, too_big="99999", too_small="1"),
    "ALT_INTERVAL": dict(attr="alt_interval", default=60, too_big="99999", too_small="1"),
    "BC_REFRESH": dict(attr="bc_refresh", default=120, too_big="99999", too_small="1"),
    "PAY_FEE": dict(attr="pay_fee", default=0.0, too_big="50", too_small="-5"),
    "RISK_PENALTY": dict(attr="risk_penalty", default=1.5, too_big="100", too_small="-5"),
}
GARBAGE = ["nan", "inf", "-inf", "abc", "-1", "1e3", "1_0"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(NUMERIC_FIELDS) + ["SPOT_FEES", "RISK_BUFFER", "TRANSFER_FEES", "TRANSFER_FEE"]:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_dataclass_without_env(caplog):
    with caplog.at_level("WARNING"):
        cfg = p2p.Config.from_env()
    default = p2p.Config()
    for spec in NUMERIC_FIELDS.values():
        assert getattr(cfg, spec["attr"]) == getattr(default, spec["attr"]), spec["attr"]
    assert cfg.spot_fees == default.spot_fees
    assert cfg.risk_buffer == default.risk_buffer
    assert cfg.transfer_fees == default.transfer_fees
    assert caplog.text == ""


@pytest.mark.parametrize("name", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("bad", GARBAGE)
def test_garbage_falls_back_to_default_with_warning(monkeypatch, caplog, name, bad):
    spec = NUMERIC_FIELDS[name]
    monkeypatch.setenv(name, bad)
    with caplog.at_level("WARNING"):
        cfg = p2p.Config.from_env()
    assert getattr(cfg, spec["attr"]) == spec["default"], (name, bad)
    assert f"{name}=" in caplog.text


@pytest.mark.parametrize("name", list(NUMERIC_FIELDS))
def test_out_of_range_falls_back_to_default(monkeypatch, name):
    spec = NUMERIC_FIELDS[name]
    for bad in (spec["too_big"], spec["too_small"]):
        monkeypatch.setenv(name, bad)
        cfg = p2p.Config.from_env()
        assert getattr(cfg, spec["attr"]) == spec["default"], (name, bad)


@pytest.mark.parametrize("name", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_value_is_default_without_warning(monkeypatch, caplog, name, blank):
    spec = NUMERIC_FIELDS[name]
    monkeypatch.setenv(name, blank)
    with caplog.at_level("WARNING"):
        cfg = p2p.Config.from_env()
    assert getattr(cfg, spec["attr"]) == spec["default"]
    assert caplog.text == ""


def test_max_dev_specific_values(monkeypatch):
    monkeypatch.setenv("MAX_DEV", "nan")
    assert p2p.Config.from_env().max_dev == 4.0   # фильтр не выключен — значение конечное
    monkeypatch.setenv("MAX_DEV", "0")
    assert p2p.Config.from_env().max_dev == 4.0
    monkeypatch.setenv("MAX_DEV", "4%")
    assert p2p.Config.from_env().max_dev == 4.0
    monkeypatch.setenv("MAX_DEV", "4,5")
    assert p2p.Config.from_env().max_dev == 4.5
    monkeypatch.setenv("MAX_DEV", " 3 ")
    assert p2p.Config.from_env().max_dev == 3.0


def test_pay_fee_min_orders_interval_specific_values(monkeypatch):
    monkeypatch.setenv("PAY_FEE", "0,3")
    assert p2p.Config.from_env().pay_fee == 0.3
    monkeypatch.setenv("MIN_ORDERS", "50")
    assert p2p.Config.from_env().min_orders == 50
    monkeypatch.setenv("MIN_ORDERS", "50.5")
    assert p2p.Config.from_env().min_orders == 100
    monkeypatch.setenv("INTERVAL", "10")
    assert p2p.Config.from_env().interval == 10


def test_transfer_fees_decimal_comma_and_glued_fraction(monkeypatch):
    monkeypatch.setenv("TRANSFER_FEES", "BTC:0,0002,ETH:0.001")
    fees = p2p.Config.from_env().transfer_fees
    assert fees["BTC"] == 0.0002 and fees["ETH"] == 0.001


def test_transfer_fees_negative_key_rolls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("TRANSFER_FEES", "USDT:-1,BTC:0.0002")
    with caplog.at_level("WARNING"):
        fees = p2p.Config.from_env().transfer_fees
    assert fees["USDT"] == 1.0   # дефолт монеты — не 0, не -1
    assert fees["BTC"] == 0.0002
    assert "USDT" in caplog.text


@pytest.mark.parametrize("raw", ["USDT:nan", "BTC:abc"])
def test_transfer_fees_bad_value_rolls_back_to_coin_default(monkeypatch, raw):
    monkeypatch.setenv("TRANSFER_FEES", raw)
    fees = p2p.Config.from_env().transfer_fees
    coin = raw.split(":")[0]
    assert fees[coin] == p2p.Config().transfer_fees[coin]


def test_transfer_fees_part_without_colon_is_skipped_not_fatal(monkeypatch, caplog):
    monkeypatch.setenv("TRANSFER_FEES", "мусор,USDT:2")
    with caplog.at_level("WARNING"):
        fees = p2p.Config.from_env().transfer_fees
    assert fees["USDT"] == 2.0
    assert "без «:»" in caplog.text


def test_transfer_fees_unmentioned_coin_is_zero(monkeypatch):
    monkeypatch.setenv("TRANSFER_FEES", "USDT:1")
    fees = p2p.Config.from_env().transfer_fees
    assert fees.get("TON", 0) == 0   # семантика не изменилась


def test_spot_fees_negative_rolls_back_keeping_case(monkeypatch):
    monkeypatch.setenv("SPOT_FEES", "Bybit:-0.1")
    fees = p2p.Config.from_env().spot_fees
    assert fees["Bybit"] == 0.1
    assert "bybit" not in fees and "BYBIT" not in fees


def test_risk_buffer_decimal_comma_and_unknown_coin_has_no_default(monkeypatch):
    monkeypatch.setenv("RISK_BUFFER", "BTC:0,3,ETH:0,5")
    buf = p2p.Config.from_env().risk_buffer
    assert buf["BTC"] == 0.3 and buf["ETH"] == 0.5

    monkeypatch.setenv("RISK_BUFFER", "SOL:-1")
    buf = p2p.Config.from_env().risk_buffer
    assert "SOL" not in buf   # у SOL нет дефолта — ключ не добавляется


def test_risk_buffer_empty_is_still_empty_dict(monkeypatch):
    monkeypatch.setenv("RISK_BUFFER", "")
    assert p2p.Config.from_env().risk_buffer == {}


def test_legacy_transfer_fee_used_only_without_transfer_fees(monkeypatch, caplog):
    monkeypatch.setenv("TRANSFER_FEE", "abc")
    with caplog.at_level("WARNING"):
        fees = p2p.Config.from_env().transfer_fees
    assert fees["USDT"] == 1.0

    monkeypatch.setenv("TRANSFER_FEE", "2,5")
    assert p2p.Config.from_env().transfer_fees["USDT"] == 2.5

    monkeypatch.setenv("TRANSFER_FEE", "-1")
    assert p2p.Config.from_env().transfer_fees["USDT"] == 1.0

    monkeypatch.setenv("TRANSFER_FEES", "USDT:3")
    assert p2p.Config.from_env().transfer_fees["USDT"] == 3.0   # легаси игнорируется, раз TRANSFER_FEES задан


def test_fees_contract_pin_stays_strict():
    """Общий `_fees` (не трогали) остаётся строгим: на нём стоит скрипт порогов хеджа (fail-closed на мусоре)."""
    with pytest.raises(ValueError):
        p2p._fees("BTC:abc")
    assert p2p._fees("BTC:-0.5") == {"BTC": -0.5}
    assert math.isnan(p2p._fees("BTC:nan")["BTC"])
