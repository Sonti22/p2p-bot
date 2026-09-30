"""_bc_parse (разбор выгрузки BestChange info.zip): единичная битая строка справочника или bm_rates.dat не должна
ронять или отравлять весь разбор — своя ветка для каждого случая порчи; системная поломка формата (битых строк
bm_rates.dat больше, чем разобранных объявлений) по-прежнему поднимает исключение."""
import io
import logging
import math
import zipfile

import pytest

import p2p

CY = ("10;20;Tether TRC20 (USDT);USDT TRC20;840;0;x\n"
      "42;231;Сбербанк RUB;RUB Сбербанк;643;3;x").encode("cp1251")
EXCH = "1396;CinusExc;;0;1".encode("cp1251")
SELL_ROW = "10;42;1396;1;89.8;10000000;0.822;1;100;1000;0"
BUY_ROW = "42;10;1396;90.5;1;50000;1.40;1;2000;300000;0"


def _zip(cy, exch, rates):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bm_cy.dat", cy)
        z.writestr("bm_exch.dat", exch)
        z.writestr("bm_rates.dat", rates)
    return buf.getvalue()


def test_undecodable_byte_in_dictionaries(monkeypatch):
    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    cy = CY + b"\n99;300;Bad\x98Currency;???;643;3;x"                 # 0x98 не определён в cp1251
    exch = EXCH + b"\n2000;Bad\x98Exch;;0;1"
    ads = p2p._bc_parse(_zip(cy, exch, SELL_ROW.encode("cp1251")))
    assert len(ads) == 1
    assert ads[0].price == pytest.approx(89.8) and ads[0].pays == ["Sberbank"]


def test_bad_rates_rows_skipped(monkeypatch):
    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    valid = [SELL_ROW] * 5 + [BUY_ROW] * 5
    bad = [
        "10;42;1396;0;89.8;10000000;0.822;1;100;1000;0",       # sell give=0
        "10;42;1396;1;0;10000000;0.822;1;100;1000;0",          # sell recv=0
        "42;10;1396;0;1;50000;1.40;1;2000;300000;0",           # buy give=0 (раньше давал price=0.0)
        "42;10;1396;90.5;0;50000;1.40;1;2000;300000;0",        # buy recv=0
        "10;42;1396;1;89.8;10000000;0.822;1;;1000;0",          # пустой лимит min
        "10;42;1396;1;89.8",                                    # слишком короткая строка
        "10;42;1396;1;89.8;10000000;0.822;1;abc;1000;0",       # нечисловое поле
        "10;42;1396;1;nan;10000000;0.822;1;100;1000;0",        # nan
        "10;42;1396;1;inf;10000000;0.822;1;100;1000;0",        # inf
    ]
    rates = "\n".join(valid + bad).encode("cp1251")
    ads = p2p._bc_parse(_zip(CY, EXCH, rates))
    assert len(ads) == 10
    assert all(math.isfinite(a.price) and a.price > 0 for a in ads)
    assert p2p._bc_stats["skipped"] == len(bad)


def test_short_dictionary_rows_skipped(monkeypatch):
    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    cy = CY + b"\nx;y;z"                          # 3 поля — короче 6
    exch = EXCH + b"\n2000"                        # 1 поле — короче 2
    ads = p2p._bc_parse(_zip(cy, exch, SELL_ROW.encode("cp1251")))
    assert len(ads) == 1
    assert p2p._bc_stats["skipped"] == 2


def test_warning_once_per_change(monkeypatch, caplog):
    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    valid = "\n".join([SELL_ROW] * 5)
    one_bad = (valid + "\n10;42;1396;1;89.8").encode("cp1251")   # +1 короткая строка
    with caplog.at_level(logging.WARNING, logger="p2p"):
        p2p._bc_parse(_zip(CY, EXCH, one_bad))
        assert sum("BestChange" in r.message for r in caplog.records) == 1
        caplog.clear()
        p2p._bc_parse(_zip(CY, EXCH, one_bad))         # то же число битых строк — тишина
        assert not any("BestChange" in r.message for r in caplog.records)
        caplog.clear()
        two_bad = (valid + "\n10;42;1396;1;89.8\n10;42;1396;1;nan").encode("cp1251")
        p2p._bc_parse(_zip(CY, EXCH, two_bad))          # другое число — новое предупреждение
        assert sum("BestChange" in r.message for r in caplog.records) == 1
        caplog.clear()
        clean = SELL_ROW.encode("cp1251")
        p2p._bc_stats["skipped"] = 0
        p2p._bc_parse(_zip(CY, EXCH, clean))
        assert not any("BestChange" in r.message for r in caplog.records)


def test_systematic_break_raises(monkeypatch, caplog):
    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    comma_rates = "\n".join([
        "10;42;1396;1;89,8;10000000;0.822;1;100;1000;0",
        "42;10;1396;90,5;1;50000;1.40;1;2000;300000;0",
    ]).encode("cp1251")
    with caplog.at_level(logging.WARNING, logger="p2p"):
        with pytest.raises(ValueError):
            p2p._bc_parse(_zip(CY, EXCH, comma_rates))
    assert p2p._bc_stats["skipped"] == 2                       # счётчик и лог выставлены до raise
    assert any("BestChange" in r.message for r in caplog.records)

    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    no_match_rates = "999;998;1396;1;89.8;10000000;0.822;1;100;1000;0".encode("cp1251")
    ads = p2p._bc_parse(_zip(CY, EXCH, no_match_rates))         # пары не совпали ни разу — пусто, без исключения
    assert ads == []


def test_clean_dump_unchanged(monkeypatch):
    monkeypatch.setattr(p2p, "_bc_stats", {"skipped": 0})
    cy = "\n".join([
        "10;20;Tether TRC20 (USDT);USDT TRC20;840;0;x",
        "42;231;Сбербанк RUB;RUB Сбербанк;643;3;x",
        "46;236;Т-Банк cash-in RUB;RUB Т-Банк cash-in;643;3;x",
        "21;1;СБП RUB;RUB СБП;643;2;x"]).encode("cp1251")
    exch = "1396;CinusExc;;0;1\n933;TytCash;;0;1".encode("cp1251")
    rates = "\n".join([
        "10;42;1396;1;89.8;10000000;0.822;1;100;1000;0",
        "42;10;933;90.5;1;50000;1.40;1;2000;300000;0",
        "10;46;1396;1;95;100000;0.5;1;1;1000;0",
    ]).encode("cp1251")
    ads = p2p._bc_parse(_zip(cy, exch, rates))
    sell = [a for a in ads if a.side == "sell"]
    buy = [a for a in ads if a.side == "buy"]
    assert len(sell) == 1 and len(buy) == 1
    assert sell[0].price == pytest.approx(89.8) and sell[0].pays == ["Sberbank"] and sell[0].net == "TRC20"
    assert sell[0].min_amt == pytest.approx(100 * 89.8)
    assert buy[0].price == pytest.approx(90.5)
    assert buy[0].orders == 40 and buy[0].rate == pytest.approx(40 / 41 * 100)
    assert "bestchange.ru/click.php" in sell[0].url
    assert p2p._bc_stats["skipped"] == 0
