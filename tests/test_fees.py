import json
from datetime import date

import pytest

import fees
import p2p


def test_table_matches_json_and_feeds_route():
    t = fees.table()
    assert t[("Bybit", "USDT")]["BEP20"] == pytest.approx(0.2)
    assert t[("MEXC", "USDT")]["TON"] == pytest.approx(0.023)
    assert p2p.WITHDRAW == t                                    # p2p берёт таблицу из fees.json


def test_missing_or_broken_file_gives_empty_table(tmp_path):
    assert fees.table(str(tmp_path / "none.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert fees.table(str(bad)) == {}
    assert "не найден" in fees.view(str(bad))


def test_age_and_stale_warning(tmp_path):
    p = tmp_path / "fees.json"
    p.write_text(json.dumps({"checked": "2026-01-01", "fees": {"Bybit": {"USDT": {"TRC20": 1}}}}), encoding="utf-8")
    assert fees.age_days(str(p), today=date(2026, 1, 31)) == 30
    fresh = fees.view(str(p), today=date(2026, 1, 31))
    assert "30 дн. назад" in fresh and "старые" not in fresh
    stale = fees.view(str(p), today=date(2026, 3, 1))
    assert "старые" in stale


def test_view_compares_with_live_fees(tmp_path):
    p = tmp_path / "fees.json"
    p.write_text(json.dumps({"checked": "2026-09-01", "fees": {"MEXC": {"USDT": {"TRC20": 1.0, "BEP20": 0.01}}}}),
                 encoding="utf-8")
    live = {("MEXC", "USDT", "TRC20"): 1.0, ("MEXC", "USDT", "BEP20"): 0.5}
    text = fees.view(str(p), today=date(2026, 9, 2), live=lambda v, a, n: live.get((v, a, n)))
    assert "TRC20 1 ✓" in text and "BEP20 0.01 → сейчас 0.5 ⚠️" in text
