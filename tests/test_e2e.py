"""Сквозной тест конвейера без сети: scan (реальные адаптеры на фикстурах) → _stack/reliability
внутри scan → notify (реальная карточка отправляется Telegram-боту-заглушке). В отличие от
test_bot.py, где сделки собраны вручную через make_ad, здесь связка целиком выходит из p2p.scan()."""
import asyncio
import html

import bot as B
import p2p


class Stub(B.Bot):
    """Бот без сети: все вызовы Telegram пишутся в self.out, ответ настраивается через self.ok."""
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []
        self.ok = True

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": self.ok}

    async def send_photo(self, png, caption, markup=None):
        self.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": self.ok}


def _cfg():
    return p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"],
                      min_orders=0, min_rate=0, min_profit=-100.0)   # без порога — сигналим саму лучшую связку


def _run_pipeline(offline, monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    cfg = _cfg()
    snap = asyncio.run(p2p.scan(None, cfg))
    assert snap.deals, "фикстуры должны давать хотя бы одну связку"
    bot = Stub(cfg)
    bot.live_scans = 1   # тест про сам конвейер, не про «живость» сигнала
    return cfg, snap, bot


def test_scan_stack_reliability_notify_pipeline(offline, monkeypatch):
    cfg, snap, bot = _run_pipeline(offline, monkeypatch)
    top = snap.deals[0]   # лучшая связка по прибыли с поправкой на надёжность (сортировка внутри scan())

    asyncio.run(bot.notify(snap))

    photos = [p for m, p in bot.out if m == "sendPhoto"]
    assert photos   # хотя бы топ-1 связка ушла сигналом (max_signals может прислать до 3)
    caption = photos[0]["caption"]
    # первой картинкой уходит именно лучшая связка скана — с той же надёжностью, что и в самом снимке
    label, reasons = p2p.reliability(top, cfg, snap)
    assert html.escape(p2p.fmt_reliability(label, reasons)) in caption
    assert f"{top[0]:+.2f}%" in caption
    # повторный вызов в пределах cooldown — те же связки не дублируются новыми сообщениями
    asyncio.run(bot.notify(snap))
    assert len([p for m, p in bot.out if m == "sendPhoto"]) == len(photos)


def test_scan_notify_pipeline_not_marked_sent_on_telegram_failure(offline, monkeypatch):
    """ok:false в реальном конвейере (не на синтетической связке из make_ad) не должен тратить антидубль —
    следующий скан обязан повторить попытку отправки той же связки."""
    cfg, snap, bot = _run_pipeline(offline, monkeypatch)
    bot.ok = False

    asyncio.run(bot.notify(snap))
    assert not bot.sent   # доставка не подтверждена — связка не помечена отправленной

    bot.ok = True
    asyncio.run(bot.notify(snap))
    assert bot.sent   # тот же скан — сигнал ушёл и антидубль сработал
