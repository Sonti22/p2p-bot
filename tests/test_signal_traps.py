"""«🪤 Ловушки» не приходят сигналом — ни обычным, ни по избранному маршруту, ни в ночной дайджест (SIGNAL_TRAPS=1 —
приходят); в /top и /best они видны с пометкой. Сухой прогон берёт их только при PAPER_TRAPS=1 (test_paper_realism)."""
import asyncio

import bot as B
import cards
import favorites
import p2p
from test_bot import Stub

PNG = b"\x89PNG\r\n\x1a\n"


def ad(ex, side, price):
    return p2p.Ad(ex, side, price, 1000, 500000, 10000, ["T-Bank"], f"{ex}-{side}", 1000, 100.0, "", "USDT", "", "")


def trap_deal():
    """Покупка на 3,4% ниже ориентира, продажа на 4,5% выше, спред ≥5% — три причины → «🪤 ловушка»."""
    return 8.0, ad("Bybit", "buy", 85.0), ad("MEXC", "sell", 92.0), "перевод на MEXC"


def good_deal():
    return 3.9, ad("HTX", "buy", 87.5), ad("KuCoin", "sell", 91.0), "перевод на KuCoin"


def snap_of(deals):
    return p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, list(deals), {}, {}, {})


def _bot(monkeypatch, min_profit=2.0):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.delenv("PAPER", raising=False)
    bot = Stub(p2p.Config(min_profit=min_profit))
    bot.live_scans = 1
    return bot


def captions(bot):
    return [p["caption"] for m, p in bot.out if m == "sendPhoto"]


def test_labels_used_here_are_as_intended():
    s, cfg = snap_of([]), p2p.Config()
    assert p2p.reliability(trap_deal(), cfg, s)[0] == p2p.TRAP
    assert p2p.reliability(good_deal(), cfg, s)[0] != p2p.TRAP


def test_trap_is_not_signalled_and_does_not_take_a_top_slot(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    bot = _bot(monkeypatch)
    bot.max_signals = 1
    asyncio.run(bot.notify(snap_of([trap_deal(), good_deal()])))
    (cap,) = captions(bot)                              # единственный слот топа — чистой связке, не ловушке
    assert "HTX" in cap and p2p.TRAP not in cap
    assert set(bot.sent) == {("HTX", "USDT", "KuCoin", "USDT")}


def test_signal_traps_env_brings_traps_back(monkeypatch):
    monkeypatch.setenv("SIGNAL_TRAPS", "1")
    bot = _bot(monkeypatch)
    asyncio.run(bot.notify(snap_of([trap_deal(), good_deal()])))
    caps = captions(bot)
    assert len(caps) == 2 and p2p.TRAP in caps[0]


def test_trap_on_favorite_route_is_not_signalled(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    favorites.toggle(("Bybit", "USDT", "MEXC", "USDT"))
    s = snap_of([trap_deal()])
    bot = _bot(monkeypatch, min_profit=50.0)            # выше общего порога нет ничего — только путь избранного
    asyncio.run(bot.notify(s))
    assert not captions(bot)
    monkeypatch.setenv("SIGNAL_TRAPS", "1")
    asyncio.run(bot.notify(s))
    (cap,) = captions(bot)
    assert cap.startswith("⭐") and p2p.TRAP in cap


def test_night_digest_skips_traps(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    bot = _bot(monkeypatch)
    bot.collect_night_deals(snap_of([trap_deal(), good_deal()]))
    assert set(bot.night_deals) == {("HTX", "USDT", "KuCoin", "USDT")}


def test_top_and_best_still_show_traps_marked(monkeypatch):
    monkeypatch.delenv("SIGNAL_TRAPS", raising=False)
    monkeypatch.setattr(B, "top_chart", lambda snap, c: b"png")
    bot = _bot(monkeypatch)
    s = snap_of([trap_deal(), good_deal()])
    asyncio.run(bot.show_top(s))
    assert "🪤 Ловушек: 1" in captions(bot)[-1] and "Связок всего: 2" in captions(bot)[-1]
    asyncio.run(bot.show_best(s))                       # /best — лучшая связка снимка, даже если это ловушка
    assert p2p.TRAP in captions(bot)[-1]
    assert p2p.TRAP in p2p.fmt_top(s, bot.cfg)          # текстовый /top — с меткой у каждой связки
    monkeypatch.setenv("SIGNAL_TRAPS", "1")             # ловушки и так приходят сигналом — отдельной строки нет
    asyncio.run(bot.show_top(s))
    assert "Ловушек" not in captions(bot)[-1]


def test_top_chart_renders_trap_row(monkeypatch):
    seen = []
    real = cards.reliability
    monkeypatch.setattr(cards, "reliability", lambda d, c, s: seen.append(d) or real(d, c, s))
    assert cards.top_chart(snap_of([trap_deal(), good_deal()]), p2p.Config())[:8] == PNG
    assert len(seen) == 2                               # метка считается по каждой строке графика
