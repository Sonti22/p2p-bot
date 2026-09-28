"""Утренний дайджест v2: одно сообщение до 4000 символов — связки выше порога по площадкам за ночь, лучшие связки
ночи, сухой прогон за сутки (результат в ₽, срывы и их причины)."""
import functools
from datetime import datetime

import bot as B
import history
import p2p
import paper
from helpers import arun, make_ad

K_MEXC = ("Bybit", "USDT", "MEXC", "USDT")
K_MEXC_BTC = ("Bybit", "BTC", "MEXC", "BTC")
K_HTX = ("Bybit", "USDT", "HTX", "USDT")


def msk(hour, day=24):
    return datetime(2026, 9, day, hour, 0, tzinfo=B.MSK).timestamp()


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def deal(profit, s_ex="MEXC"):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad(s_ex, "sell", 90.0), "маршрут"


def _signals(db):
    """Ночь: Bybit→MEXC появлялась дважды (USDT, с перерывом больше COOLDOWN) и раз BTC; Bybit→HTX — один раз;
    вечерний эпизод до начала ночи в окно не попадает."""
    ids = history.track_signals([(K_HTX, 2.0, True, None)], msk(20, 23), {}, path=db)
    history.track_signals([], msk(20, 23) + 60, ids, path=db)
    ids = history.track_signals([(K_MEXC, 1.5, False, "quiet"), (K_MEXC_BTC, 3.1, False, "quiet")], msk(1), {},
                                path=db)
    ids = history.track_signals([(K_MEXC, 2.4, False, "quiet")], msk(1) + 60, ids, path=db)
    history.track_signals([], msk(2), ids, path=db)
    ids = history.track_signals([(K_MEXC, 1.8, False, "quiet"), (K_HTX, 1.2, False, "quiet")], msk(4), {}, path=db)
    history.track_signals([], msk(4) + 60, ids, path=db)


def test_venue_signals_groups_episodes_by_direction(tmp_path):
    db = str(tmp_path / "history.db")
    _signals(db)
    out = history.venue_signals(msk(23, 23), msk(9), path=db, cooldown=600)
    assert out == {("Bybit", "MEXC"): {"episodes": 3, "best": 3.1}, ("Bybit", "HTX"): {"episodes": 1, "best": 1.2}}
    assert history.venue_signals(msk(23, 23), msk(9), path=str(tmp_path / "none.db")) == {}


def test_venue_signals_merges_short_gap_into_one_episode(tmp_path):
    db = str(tmp_path / "history.db")
    ids = history.track_signals([(K_MEXC, 2.0, False, "quiet")], msk(1), {}, path=db)
    history.track_signals([], msk(1) + 60, ids, path=db)
    history.track_signals([(K_MEXC, 2.5, False, "quiet")], msk(1) + 120, {}, path=db)   # вернулась через минуту
    assert history.venue_signals(msk(0), msk(9), path=db, cooldown=600)[("Bybit", "MEXC")] == \
        {"episodes": 1, "best": 2.5}


def _paper(db, now):
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.init_balance(10000, path=db)
    old = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(old, "done", realized_pct=5.0, path=db)
    con = paper._connect(db)
    with con:
        con.execute("UPDATE cycles SET ts_start = ? WHERE id = ?", (now - 2 * 86400, old))   # позавчера — не в сутках
    con.close()
    for pct in (2.0, 1.0):
        cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
        paper.finish_cycle(cid, "done", realized_pct=pct, path=db)
    for stage, note in (("failed_sell", "цена ушла"), ("failed_sell", "цена ушла"), ("failed_buy", "объявление снято")):
        cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
        paper.finish_cycle(cid, stage, realized_pct=0.0, note=note, path=db)


def test_paper_summary_since_counts_profit_and_failure_notes(tmp_path):
    import time
    db = str(tmp_path / "paper.db")
    now = time.time()
    _paper(db, now)
    p = paper.summary_since(now - 86400, path=db)
    assert (p["total"], p["done"], p["failed"]) == (5, 2, 3)
    assert p["failed_by_reason"] == {"failed_sell": 2, "failed_buy": 1}
    assert abs(p["profit_rub"] - 300.0) < 1e-6          # 10 000 × (2% + 1%)
    assert p["top_notes"] == [("цена ушла", 2), ("объявление снято", 1)]
    assert paper.summary_since(now, path=str(tmp_path / "none.db"))["total"] == 0


def _bot(monkeypatch, tmp_path, paper_on=True):
    hdb, pdb = str(tmp_path / "history.db"), str(tmp_path / "paper.db")
    monkeypatch.setattr(B.history, "venue_signals", functools.partial(history.venue_signals, path=hdb, cooldown=600))
    for name in ("summary_since", "get_balance", "balance_change"):
        monkeypatch.setattr(B.paper, name, functools.partial(getattr(paper, name), path=pdb))
    if paper_on:
        monkeypatch.setenv("PAPER", "1")
    else:
        monkeypatch.delenv("PAPER", raising=False)
    bot = Stub(p2p.Config(min_profit=1.0))
    bot.quiet_on = True
    return bot, hdb, pdb


def test_morning_digest_is_one_message_with_all_sections(monkeypatch, tmp_path):
    bot, hdb, pdb = _bot(monkeypatch, tmp_path)
    _signals(hdb)
    _paper(pdb, msk(9))
    monkeypatch.setattr(B, "in_quiet_hours", lambda hours: 0 <= (B.time.time() - msk(23, 23)) < 10 * 3600)
    monkeypatch.setattr(B.time, "time", lambda: msk(23, 23))
    arun(bot.quiet_and_pause_tick(p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})))
    assert bot.quiet_since == msk(23, 23)
    monkeypatch.setattr(B.time, "time", lambda: msk(3))
    arun(bot.quiet_and_pause_tick(p2p.Snapshot(88.0, "t", {}, {}, [deal(4.0), deal(2.5, "HTX")], {}, {}, {})))
    monkeypatch.setattr(B.time, "time", lambda: msk(9))
    arun(bot.quiet_and_pause_tick(p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {})))
    msgs = texts(bot)
    assert len(msgs) == 1
    text = msgs[0]
    assert "Итоги ночи</b> (23:00–09:00 МСК)" in text
    assert "Связки выше порога</b>: 4 по 2 направл." in text
    assert "• Bybit → MEXC: 3 (лучшая +3.10%)" in text and "• Bybit → HTX: 1 (лучшая +1.20%)" in text
    assert text.index("Bybit → MEXC: 3") < text.index("Bybit → HTX: 1")
    assert "Топ-3 связки за ночь" in text and text.index("+4.00%") < text.index("+2.50%")
    assert "Сухой прогон за сутки: 5 кругов, исполнилось 2, сорвалось 3 (продажа 2, покупка 1)" in text
    assert "Итог исполнившихся за сутки: +300 ₽" in text
    assert "Причины срывов: цена ушла ×2; объявление снято ×1" in text
    assert len(text) <= B.DIGEST_MAX
    assert bot.quiet_since is None


def test_digest_without_paper_and_signals_says_nothing_above_threshold(monkeypatch, tmp_path):
    bot, _, _ = _bot(monkeypatch, tmp_path, paper_on=False)
    text = bot.night_digest_text([], msk(23, 23), msk(9))
    assert "связок выше порога не было" in text
    assert "Сухой прогон" not in text and "📡" not in text


def test_digest_fits_4000_chars_with_many_directions_and_long_cards(monkeypatch, tmp_path):
    bot, hdb, _ = _bot(monkeypatch, tmp_path, paper_on=False)
    rows = [((f"Venue{i:02d}" + "x" * 60, "USDT", f"Sell{i:02d}" + "y" * 60, "USDT"), 1.0 + i / 100, False, "quiet")
            for i in range(40)]
    history.track_signals(rows, msk(1), {}, path=hdb)
    monkeypatch.setattr(B, "fmt_deal", lambda d, cfg: "длинная карточка " * 200)
    deals = [(deal(3.0), p2p.Config()), (deal(2.0, "HTX"), p2p.Config())]
    text = bot.night_digest_text(deals, msk(23, 23), msk(9))
    assert len(text) <= B.DIGEST_MAX
    assert "длинная карточка" not in text                       # карточки свёрнуты в строки
    assert "1) Bybit → MEXC USDT/USDT <b>+3.00%</b>" in text
    assert "…ещё 32 направл." in text                          # по умолчанию 8 направлений


def test_digest_window_falls_back_when_night_start_unknown(monkeypatch, tmp_path):
    bot, _, _ = _bot(monkeypatch, tmp_path, paper_on=False)
    monkeypatch.setattr(B.time, "time", lambda: msk(9))
    bot.quiet_since = None                       # бот перезапущен посреди ночи
    arun(bot.send_night_digest())
    assert f"({B._hhmm_msk(msk(9) - B.DIGEST_FALLBACK_WINDOW)}–09:00 МСК)" in texts(bot)[-1]
