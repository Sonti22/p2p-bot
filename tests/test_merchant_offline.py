"""Мерчант офлайн (план 2.2): флаг площадки Ad.online (из фикстур его отдаёт только LBank — поле online в
tests/fixtures/lbank_ads*.json) и Ad.last_seen (в ответах из фикстур такого поля нет ни у одной площадки — только
синтетика) → причина «мерчант офлайн» (MERCHANT_OFFLINE=reason, по умолчанию) или отсев объявления из стакана и
сигналов (MERCHANT_OFFLINE=skip)."""
import asyncio
import logging

import p2p
import replay
from helpers import make_ad

LBANK_OPEN = p2p.parse_merchant_min("LBank:0/0")   # у мерчантов LBank в фикстуре 0–2 сделки — иначе их отсеет порог
FETCHED = 10_000.0


def _cfg(mode="reason"):
    return p2p.Config(assets=["USDT"], merchant_min=LBANK_OPEN, merchant_offline=mode)


def _fixture_ads(cfg, fetchers=(p2p.lbank, p2p.bybit)):
    return [a for fetch in fetchers for side in ("buy", "sell") for a in asyncio.run(fetch(None, cfg, side, "USDT"))]


def test_only_lbank_reports_online(offline):
    ads = _fixture_ads(_cfg(), (p2p.lbank, p2p.bybit, p2p.htx, p2p.kucoin, p2p.mexc, p2p.bitpapa))
    assert {a.nick: a.online for a in ads if a.ex == "LBank"} == {
        "own_lbank_seller": False, "$~DIMASIK~$": False, "O.E.LLIaMaH": False, "☄️dimaxcec☄️": False, "ko****": True}
    others = [a for a in ads if a.ex != "LBank"]
    assert others and all(a.online is None and a.last_seen == 0.0 for a in others)   # не знаем — не офлайн
    assert not any(p2p.merchant_offline(a, p2p.Config()) for a in others)


def test_offline_reason_on_fixture_deals_and_signal_caption(offline):
    cfg = _cfg()
    snap = p2p.assemble(cfg, _fixture_ads(cfg), ts=1.0)
    lbank = [d for d in snap.deals if "LBank" in (d[1].ex, d[2].ex)]
    assert lbank
    for d in lbank:
        reasons = p2p.reliability(d, cfg, snap)[1]
        for ad, side in ((d[1], "покупка"), (d[2], "продажа")):
            assert (f"{side}: мерчант офлайн" in reasons) == (ad.ex == "LBank"), (d, reasons)
    d = next(d for d in lbank if d[1].ex != "LBank")                  # офлайн только покупатель на LBank
    assert p2p.reliability(d, cfg, snap)[1][0] == "продажа: мерчант офлайн"
    assert "продажа: мерчант офлайн" in p2p.fmt_signal(d, cfg, snap)   # причина видна в карточке сигнала


def test_skip_drops_offline_ads_from_book_and_deals(offline):
    ads = _fixture_ads(_cfg())
    reason = p2p.assemble(_cfg(), ads, ts=1.0)
    skip = p2p.assemble(_cfg("skip"), ads, ts=1.0)
    assert [k for k in reason.groups if k[0] == "LBank"]
    # в фикстуре все мерчанты LBank, прошедшие остальные фильтры, офлайн (ko**** онлайн, но его цена — аномалия)
    assert not [k for k in skip.groups if k[0] == "LBank"]
    assert not [d for d in skip.deals if "LBank" in (d[1].ex, d[2].ex)]
    assert not [a for a in skip.best.values() if a.ex == "LBank"]
    assert {k for k in skip.groups} == {k for k in reason.groups if k[0] != "LBank"}   # остальное — как было


def test_filters_skip_only_in_skip_mode():
    ok, off = make_ad(), make_ad()
    off.online = False
    for f in (p2p.usable, p2p._signal_ok):
        assert f(off, p2p.Config()) and f(ok, p2p.Config())               # reason: объявление остаётся
        skip = p2p.Config(merchant_offline="skip")
        assert not f(off, skip) and f(ok, skip)
        assert not f(off, p2p.Config(merchant_offline=" SKIP "))          # replay --set пишет строку как есть


def test_last_seen_limit():
    cfg = p2p.Config()
    a = make_ad()
    a.fetched_ts = FETCHED
    a.last_seen = FETCHED - 16 * 60
    assert p2p.merchant_offline(a, cfg)                                    # старше 15 мин по умолчанию
    a.last_seen = FETCHED - 14 * 60
    assert not p2p.merchant_offline(a, cfg)
    assert p2p.merchant_offline(a, p2p.Config(merchant_offline_min=10))
    a.last_seen = 0.0                                                      # время не отдают — не знаем
    assert not p2p.merchant_offline(a, cfg)
    a.online = False
    assert p2p.merchant_offline(a, cfg)


def test_reason_text_and_weight():
    b = make_ad("Bybit", "buy", 85.0, orders=5000)
    b.fetched_ts, b.last_seen = FETCHED, FETCHED - 20 * 60
    s = make_ad("HTX", "sell", 86.0, orders=5000)
    s.online = False
    snap = p2p.Snapshot(85.5, "t", {"USDT": 85.5}, {}, [], {}, {}, {})
    risks = p2p._risks((1.0, b, s, "внутри биржи"), p2p.Config(), snap)
    assert risks == [(1, "покупка: мерчант офлайн (был 20 мин назад)"), (1, "продажа: мерчант офлайн")]
    b.last_seen, s.online = 0.0, None
    assert p2p._risks((1.0, b, s, "внутри биржи"), p2p.Config(), snap) == []


def test_stack_is_offline_if_any_merchant_is():
    a, b = make_ad(price=85.0, max_amt=30000, avail=400), make_ad(price=85.5, max_amt=30000, avail=400)
    b.online, b.last_seen, a.last_seen = False, 500.0, 900.0
    st = p2p._stack([a, b], 50000)
    assert st.parts == 2 and st.online is False and st.last_seen == 500.0 and p2p.merchant_offline(st, p2p.Config())
    a.online = b.online = True
    assert p2p._stack([a, b], 50000).online is True
    a.online = None
    assert p2p._stack([a, b], 50000).online is None


def test_from_env(monkeypatch, caplog):
    for name in ("MERCHANT_OFFLINE", "MERCHANT_OFFLINE_MIN"):
        monkeypatch.delenv(name, raising=False)
    c = p2p.Config.from_env()
    assert (c.merchant_offline, c.merchant_offline_min) == ("reason", 15.0)
    monkeypatch.setenv("MERCHANT_OFFLINE", " SKIP ")
    monkeypatch.setenv("MERCHANT_OFFLINE_MIN", "7,5")
    c = p2p.Config.from_env()
    assert (c.merchant_offline, c.merchant_offline_min) == ("skip", 7.5)
    monkeypatch.setenv("MERCHANT_OFFLINE", "drop")
    monkeypatch.setenv("MERCHANT_OFFLINE_MIN", "0")
    with caplog.at_level(logging.WARNING, logger="p2p"):
        c = p2p.Config.from_env()
    assert (c.merchant_offline, c.merchant_offline_min) == ("reason", 15.0)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "MERCHANT_OFFLINE=drop" in msgs and "MERCHANT_OFFLINE_MIN=0" in msgs


def test_replay_override():
    cfg = replay.override(p2p.Config(), ["merchant_offline=skip", "merchant_offline_min=5"])
    assert (cfg.merchant_offline, cfg.merchant_offline_min) == ("skip", 5.0)
