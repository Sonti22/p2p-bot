import asyncio
import functools

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


def photos(bot):
    return [m for m in bot.out if m[0] == "sendPhoto"]


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


def test_duration_formats():
    assert B._duration(45) == "45с"
    assert B._duration(125) == "2м 5с"
    assert B._duration(3725) == "1ч 2м"


def test_git_sha_reads_current_commit():
    import re
    assert re.fullmatch(r"[0-9a-f]{7}", B.git_sha())


def test_status_reports_scan_and_errors(monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: 1010.0)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.started, bot.scan_at, bot.scan_dur = 900.0, 1000.0, 0.5
    bot.last = snap([deal(5), deal(1)])
    bot.last.errors["bybit/USDT"] = "TimeoutError: x"
    asyncio.run(bot.show_status())
    text = bot.out[-1][1]["text"]
    assert "Связок ≥2%: 1 из 2" in text
    assert "bybit/USDT: TimeoutError: x" in text
    assert "10с назад, длился 0.5 с" in text
    assert "Аптайм: 1м 50с" in text


def test_status_before_first_scan():
    bot = Stub(p2p.Config())
    asyncio.run(bot.show_status())
    assert "ещё не выполнялся" in bot.out[-1][1]["text"]
