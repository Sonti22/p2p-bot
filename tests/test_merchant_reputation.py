"""Автоматическая репутация мерчанта (reputation.py): срывы и «ушёл до оплаты» по кругам сухого прогона, частота
смены цены по снимкам сканов; метка — строка в карточке сигнала, без автоблокировки."""
import functools
import time

import bot as B
import p2p
import paper
import reputation
import snapshots
from helpers import arun, make_ad

T0 = 1_790_000_000.0


def ad(ex, side, price, nick, ad_id="1", ts=T0):
    a = make_ad(ex, side, price)
    a.nick, a.ad_id, a.fetched_ts = nick, ad_id, ts
    return a


def write_scan(path, ts, ads):
    snap = p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=ts, ads=ads)
    snapshots.write(snapshots.collect(snap, p2p.Config()), path, now=ts)


def test_price_changes_per_hour_from_snapshots(tmp_path):
    path = str(tmp_path / "snapshots.db")
    snapshots._seen.clear()
    for i in range(7):   # 2 часа: у «jumpy» цена меняется каждые 20 минут, у «calm» — ни разу
        ts = T0 + i * 1200
        write_scan(path, ts, [ad("Bybit", "buy", 85.0 + (i % 2) * 0.1, "jumpy", ts=ts),
                              ad("Bybit", "buy", 86.0, "calm", "2", ts=ts)])
    out = reputation.from_snapshots(T0 - 1, T0 + 3 * 3600, path)
    assert out[("Bybit", "jumpy")]["changes"] == 6 and abs(out[("Bybit", "jumpy")]["hours"] - 2.0) < 1e-6
    assert out[("Bybit", "calm")]["changes"] == 0
    assert reputation.label_text(out[("Bybit", "jumpy")]) is None               # 3/ч — ниже порога
    rec = dict(out[("Bybit", "jumpy")], changes=16)
    assert reputation.label_text(rec) == "🧾 цена меняется ~8/ч"


def test_snapshot_sampling_caps_scans_read(tmp_path, monkeypatch):
    path = str(tmp_path / "snapshots.db")
    snapshots._seen.clear()
    for i in range(10):
        write_scan(path, T0 + i * 60, [ad("Bybit", "buy", 85.0 + i, "m", ts=T0 + i * 60)])
    loaded = []
    real = snapshots.load
    monkeypatch.setattr(snapshots, "load", lambda sid, p: loaded.append(sid) or real(sid, p))
    out = reputation.from_snapshots(T0 - 1, None, path, max_scans=4)
    assert len(loaded) == 4 and out[("Bybit", "m")]["changes"] == 3


def _cycle(db, result, note="", buy_nick="seller1", sell_nick="buyer1", nicks=None):
    b, s = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    b.nick, s.nick = buy_nick, sell_nick
    if nicks:
        b.nicks = tuple(nicks)
    cid = paper.start_cycle(10000, b, s, "route", 2.0, path=db)
    paper.finish_cycle(cid, result, realized_pct=0.0 if result != "done" else 1.0, note=note, path=db)


def test_failures_and_gone_counted_on_merchants_own_stage(tmp_path):
    db = str(tmp_path / "paper.db")
    _cycle(db, "done")
    _cycle(db, "failed_buy", "объявление покупки исчезло")
    _cycle(db, "failed_buy", "цена ушла: покупка по 86 ₽ вместо 85 ₽ (+1.18%, допуск 0.5%)")
    _cycle(db, "failed_sell", "не хватает глубины стакана продажи")
    _cycle(db, "failed_buy", "часть мерчантов покупки ушла, остальные сумму не покрывают",
           nicks=("seller1", "seller2"))
    out = reputation.from_paper(0, db)
    s1 = out[("Bybit", "seller1")]
    assert (s1["cycles"], s1["failed"], s1["gone"]) == (5, 3, 2)   # «цена ушла» — срыв, но не «ушёл до оплаты»
    assert out[("Bybit", "seller2")] == {"cycles": 1, "failed": 1, "gone": 1, "changes": 0, "hours": 0.0}
    b1 = out[("MEXC", "buyer1")]
    assert (b1["cycles"], b1["failed"], b1["gone"]) == (5, 1, 0)   # срывы покупки мерчанту продажи не в счёт
    assert reputation.label_text(s1) == "🧾 срывы 3 из 5 кругов, ушёл до оплаты ×2"
    assert reputation.label_text(b1) is None                        # 1 из 5 — ниже порога
    assert reputation.from_paper(0, str(tmp_path / "none.db")) == {}


def test_failure_share_needs_minimum_cycles():
    rec = {"cycles": 2, "failed": 2, "gone": 0, "changes": 0, "hours": 0.0}
    assert reputation.label_text(rec) is None
    assert reputation.label_text(dict(rec, cycles=3)) == "🧾 срывы 2 из 3 кругов"


def test_refresh_merges_sources_and_marks_time(tmp_path):
    db = str(tmp_path / "paper.db")
    for _ in range(3):
        _cycle(db, "failed_buy", "объявление покупки исчезло")
    n = reputation.refresh(now=time.time(), paper_path=db, snap_path=str(tmp_path / "none.db"))
    try:
        assert n == 1 and not reputation.due()
        assert reputation.LABELS[("Bybit", "seller1")] == "🧾 срывы 3 из 3 кругов, ушёл до оплаты ×3"
    finally:
        reputation.LABELS.clear()
        reputation._state["ts"] = 0.0


def test_signal_card_shows_label_without_blocking(monkeypatch):
    monkeypatch.setattr(reputation, "LABELS", {("Bybit", "seller1"): "🧾 срывы 3 из 4 кругов"})
    b, s = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    b.nick = "seller1"
    text = p2p.fmt_signal((2.0, b, s, "перевод −1 USDT (TRC20) на MEXC"), p2p.Config())
    buy_part = text[text.index("Купить"):text.index("Продать")]
    assert "• 🧾 срывы 3 из 4 кругов" in buy_part
    assert text.count("🧾") == 1                                     # у мерчанта продажи метки нет
    stack = make_ad("Bybit", "buy", 85.0)
    stack.nicks = ("other", "seller1")
    assert reputation.label(stack) == "🧾 срывы 3 из 4 кругов (seller1)"


def test_signal_card_without_labels_unchanged(monkeypatch):
    monkeypatch.setattr(reputation, "LABELS", {})
    d = (2.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "внутри биржи")
    assert "🧾" not in p2p.fmt_signal(d, p2p.Config())


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)


def test_bot_schedules_one_background_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(reputation, "_state", {"ts": 0.0})
    monkeypatch.setattr(reputation, "refresh", lambda: calls.append(1) or reputation._state.update(ts=time.time()) or 0)
    bot = Stub(p2p.Config())

    async def run():
        bot.schedule_reputation()
        bot.schedule_reputation()          # пересчёт уже идёт — второй не ставим
        await bot.rep_task
        bot.schedule_reputation()          # только что пересчитали — рано

    arun(run())
    assert calls == [1]


def test_bot_refresh_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(reputation, "refresh", functools.partial(_boom))
    arun(Stub(p2p.Config()).refresh_reputation())
    assert "reputation: db locked" in caplog.text


def _boom():
    raise RuntimeError("db locked")
