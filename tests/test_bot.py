import asyncio
import functools
import time

import bot as B
import p2p
from helpers import make_ad


class Stub(B.Bot):
    """Бот без сети: все вызовы Telegram пишутся в self.out."""
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True}

    async def send_photo(self, png, caption, markup=None):
        self.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": True}


def deal(profit=3.0, s_ex="MEXC", s_asset="USDT", route="перевод −0.2 USDT (BEP20) на MEXC"):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad(s_ex, "sell", 90.0, asset=s_asset), route


def snap(deals):
    return p2p.Snapshot(88.0, "test", {}, {}, deals, {}, {}, {})


def err_snap(errors):
    return p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, errors)


def photos(bot):
    return [m for m in bot.out if m[0] == "sendPhoto"]


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def test_notify_top_n_and_dedup(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.max_signals = 2
    ds = [deal(5, "MEXC"), deal(4, "KuCoin"), deal(3, "HTX")]
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2          # повтор той же связки не шлём


def test_below_threshold_not_sent(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c: b"png")
    bot = Stub(p2p.Config(min_profit=5.0))
    asyncio.run(bot.notify(snap([deal(3)])))
    assert not bot.out


def test_settings_apply_persists(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MIN_PROFIT=2\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    assert "3%" in bot.apply("min:3")
    bot.apply("amt:100000")
    text = env.read_text(encoding="utf-8")
    assert "MIN_PROFIT=3" in text and "AMOUNT=100000" in text
    assert bot.cfg.min_profit == 3 and bot.cfg.amount == 100000


def test_deal_markup_links():
    kb = B.deal_markup(deal(route="спот USDT→ETH на Bybit (−0.1%)", s_asset="ETH"))["inline_keyboard"]
    urls = [b["url"] for row in kb for b in row if "url" in b]
    assert any("bybit.com/fiat/trade/otc" in u for u in urls)
    assert any("bybit.com/trade/spot/ETH/USDT" in u for u in urls)


def test_fmt_top_fits_telegram():
    ds = [deal(5 - i * 0.1) for i in range(30)]
    assert len(p2p.fmt_top(snap(ds), p2p.Config(), n=30)) <= 4000


def test_venue_alert_after_fail_streak():
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    bad = err_snap({"bybit/USDT": "TimeoutError: x"})
    asyncio.run(bot.check_venues(bad))
    asyncio.run(bot.check_venues(bad))
    assert not texts(bot)                 # 2 подряд — ещё рано
    asyncio.run(bot.check_venues(bad))
    assert any("bybit" in t and "недоступна" in t for t in texts(bot))


def test_venue_alert_cooldown_then_recovery():
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    bad, ok = err_snap({"bybit/USDT": "err"}), err_snap({})
    for _ in range(5):
        asyncio.run(bot.check_venues(bad))
    assert len(texts(bot)) == 1            # повтор в течение часа не шлём
    asyncio.run(bot.check_venues(ok))
    msgs = texts(bot)
    assert len(msgs) == 2 and "снова доступна" in msgs[-1]


def test_venue_alert_after_15min_without_streak():
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    bad = err_snap({"bybit/USDT": "err"})
    asyncio.run(bot.check_venues(bad))     # streak 1, down_since = сейчас
    bot.venue["bybit"]["down_since"] = time.time() - B.VENUE_DOWN_AFTER - 1
    asyncio.run(bot.check_venues(bad))     # streak 2, но уже дольше 15 мин
    assert any("bybit" in t and "недоступна" in t for t in texts(bot))


def test_venue_no_alert_when_healthy():
    bot = Stub(p2p.Config(exchanges=["bybit", "mexc"]))
    asyncio.run(bot.check_venues(err_snap({})))
    assert not texts(bot)


def test_dev_view(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"version": "abc1234", "repo": "https://github.com/o/r", "started_at": "24.09 14:00", '
                      '"log": [{"sha": "abc1234", "date": "2026-09-24", "subject": "Add /status"}]}', encoding="utf-8")
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("## Очередь\n- [x] первая\n- [ ] `/status` вторая\n- [ ] третья\n## Идеи\n- [ ] не считать\n",
                       encoding="utf-8")
    assert B.roadmap_progress(str(roadmap)) == (1, 3, "/status вторая")
    text, kb = B.dev_view(str(status), str(roadmap))
    assert "abc1234" in text and "1 из 3" in text and "Add /status" in text
    urls = [b.get("url", "") for row in kb["inline_keyboard"] for b in row]
    assert "https://github.com/o/r/commits/main" in urls


def test_dev_view_without_files(tmp_path):
    text, kb = B.dev_view(str(tmp_path / "none.json"), str(tmp_path / "none.md"))
    assert "Разработка" in text and kb["inline_keyboard"]
