"""scripts/merchant_suggest.py: предложение MERCHANT_MIN по snapshots.db — перцентили по уникальным мерчантам площадки,
не мягче 100/95, BestChange и площадки с малым числом мерчантов — мимо. База — синтетическая, пишется самим
snapshots.save (тот же формат, что у бота)."""
import os
import sys

import pytest

import p2p
import snapshots

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import merchant_suggest as MS  # noqa: E402


def ad(ex, nick, orders, rate, side="buy", asset="USDT", price=85.0):
    return p2p.Ad(ex, side, price, 1000, 100000, 1000, ["SBP"], nick, orders, rate, "", asset, "", "")


HTX = [("h1", 250, 96.5), ("h2", 300, 97.0), ("h3", 320, 97.5), ("h4", 400, 98.0), ("h5", 600, 99.0), ("h6", 800, 25.0)]
MEXC = [("m1", 5, 91.0), ("m2", 10, 92.0), ("m3", 20, 93.0), ("m4", 22, 94.0), ("m5", 30, 99.0), ("m6", 40, 99.5)]


def _write(db, ads, ts):
    snapshots.save(p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=ts, ads=ads), p2p.Config(), path=db)


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "snapshots.db")
    ads = [ad("HTX", n, o, r, side="buy" if i % 2 else "sell") for i, (n, o, r) in enumerate(HTX)]
    ads += [ad("MEXC", n, o, r, asset="BTC" if i < 3 else "USDT", price=6e6 if i < 3 else 85.0)
            for i, (n, o, r) in enumerate(MEXC)]
    ads += [ad("LBank", f"l{i}", 1, 100.0) for i in range(3)]                      # мало мерчантов
    ads += [ad("BestChange", f"ex{i}", 5000, 100.0) for i in range(6)]            # отзывы, не сделки
    _write(path, ads, 1000.0)
    _write(path, ads, 1030.0)                                                      # те же группы — ссылкой на пакет
    return path


def test_helpers():
    assert MS.floor_sig1(77) == 70 and MS.floor_sig1(308) == 300 and MS.floor_sig1(1732) == 1000
    assert MS.floor_sig1(0) == 0 and MS.floor_sig1(None) == 0 and MS.floor_sig1(9.9) == 9
    assert MS.percentile([1, 2, 3, 4, 5], 25) == 2 and MS.percentile([10, 20], 50) == 15
    assert MS.percentile([], 10) is None


def test_suggestion_by_percentiles_and_floors(db):
    found = MS.merchants(db)
    assert len([k for k in found if k[0] == "HTX"]) == 6 and len([k for k in found if k[0] == "MEXC"]) == 6
    result, notes = MS.suggest(found)
    # HTX: сделок p25 = 305 → 300; % без выброса 25 → p10 96.7 → 96
    # MEXC: сделок p25 = 12.5 → 10, % p10 91.5 → 91 — мягче общих, поднимаем до 100/95
    assert result == {"HTX": (300, 96), "MEXC": (100, 95.0)}
    assert MS.line(result) == "HTX:300/96,MEXC:100/95"
    assert any(n.startswith("LBank: мало данных") for n in notes)
    assert not any("BestChange" in n for n in notes)
    assert "HTX: n=6" in notes[0] and "без 1 ниже 90%" in notes[0]
    assert p2p.parse_merchant_min(MS.line(result)) == {"HTX": (300, 96.0), "MEXC": (100, 95.0)}   # бот строку примет


def test_never_softer_than_100_95():
    found = {("Bybit", f"b{i}"): (1.0 + i, 50.0 + i) for i in range(10)}
    assert MS.suggest(found)[0] == {"Bybit": (100, 95.0)}


def test_latest_values_of_a_merchant_win(tmp_path):
    path = str(tmp_path / "s.db")
    base = [ad("KuCoin", f"k{i}", 2000, 99.0) for i in range(5)]
    _write(path, base + [ad("KuCoin", "k9", 10, 99.0)], 1000.0)
    _write(path, base + [ad("KuCoin", "k9", 3000, 99.0)], 1030.0)
    assert MS.merchants(path)[("KuCoin", "k9")] == (3000.0, 99.0)
    assert MS.merchants(path, since=1010.0)[("KuCoin", "k9")] == (3000.0, 99.0)
    assert ("KuCoin", "k9") not in MS.merchants(path, since=2000.0)


def test_cli_prints_line_and_does_not_touch_db(db, capsys):
    before = (os.path.getmtime(db), os.path.getsize(db))
    assert MS.main([db]) == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1] == "MERCHANT_MIN=HTX:300/96,MEXC:100/95"
    assert (os.path.getmtime(db), os.path.getsize(db)) == before
    assert MS.main([db, "--min-merchants", "7"]) == 1                    # нигде не хватает мерчантов
    assert "Предложения нет" in capsys.readouterr().out


def test_cli_missing_file(tmp_path, capsys):
    missing = str(tmp_path / "nope.db")
    assert MS.main([missing]) == 2 and not os.path.exists(missing)
