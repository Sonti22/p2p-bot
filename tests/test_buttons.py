"""Кнопки нового Bot API: цвета (style 9.4), «📋» копировать (copy_text 7.11) и откат на обычные кнопки."""
import asyncio

import bot as B
import p2p
from helpers import make_ad


def deal():
    return 3.0, make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0), "перевод −0.2 USDT (BEP20) на MEXC"


def snap(deals):
    return p2p.Snapshot(88.0, "t", {}, {}, deals, {}, {}, {})


def buttons(kb):
    return [b for row in kb["inline_keyboard"] for b in row]


class Stub(B.Bot):
    def __init__(self, cfg, fail_fancy=False):
        super().__init__(None, "x", "1", cfg)
        self.out, self.fail_fancy = [], fail_fancy

    async def call(self, method, **p):
        self.out.append((method, p))
        if self.fail_fancy and B.is_fancy(p.get("reply_markup")):
            return {"ok": False, "description": "Bad Request: can't parse reply keyboard markup JSON object"}
        return {"ok": True, "result": {"message_id": 1}}

    async def _post_photo(self, png, caption, markup, thread=None):
        return await self.call("sendPhoto", caption=caption, reply_markup=markup)


def test_deal_markup_colors_and_copy():
    cfg = p2p.Config(amount=50000)
    kb = B.deal_markup(deal(), deal_id=1, cfg=cfg, snap=snap([deal()]))
    by_text = {b["text"]: b for b in buttons(kb)}
    assert by_text["🟢 Купить · Bybit"]["style"] == "success"
    assert by_text["🔴 Продать · MEXC"]["style"] == "danger"
    copies = [b for b in buttons(kb) if "copy_text" in b]
    assert copies[0]["copy_text"] == {"text": "50000"} and "50 000" in copies[0]["text"]
    qty = float(copies[1]["copy_text"]["text"])            # 50000/85 минус перевод 0.2 USDT
    assert abs(qty - (50000 / 85 - 0.2)) < 0.01 and copies[1]["text"].endswith("USDT")
    assert all("style" not in b for b in buttons(kb) if "callback_data" in b)   # служебные кнопки — без цвета


def test_deal_markup_without_cfg_has_no_copy_buttons():
    kb = B.deal_markup(deal(), deal_id=1)
    assert not any("copy_text" in b for b in buttons(kb))
    assert B.deal_markup(deal(), cfg=p2p.Config(), snap=None)["inline_keyboard"][1] == [
        {"text": "📋 50 000 RUB", "copy_text": {"text": "50000"}}]


def test_plain_markup_strips_style_and_copy():
    kb = B.deal_markup(deal(), deal_id=1, cfg=p2p.Config(), snap=snap([deal()]))
    plain = B.plain_markup(kb)
    assert B.is_fancy(kb) and not B.is_fancy(plain)
    assert not any("style" in b or "copy_text" in b for b in buttons(plain))
    assert len(plain["inline_keyboard"]) == len(kb["inline_keyboard"]) - 1     # ряд «📋» ушёл целиком
    assert B.plain_markup(B.TOP_MARKUP) == B.TOP_MARKUP and not B.is_fancy(None)


def test_send_falls_back_to_plain_buttons_once():
    bot = Stub(p2p.Config(), fail_fancy=True)
    kb = B.deal_markup(deal(), deal_id=1, cfg=p2p.Config(), snap=snap([deal()]))
    r = asyncio.run(bot.send("hi", markup=kb))
    assert r["ok"] and not bot.fancy
    sent = [p["reply_markup"] for m, p in bot.out if m == "sendMessage"]
    assert len(sent) == 2 and B.is_fancy(sent[0]) and not B.is_fancy(sent[1])
    bot.out.clear()
    asyncio.run(bot.send("again", markup=kb))                # дальше сразу обычные, без повтора
    assert [B.is_fancy(p["reply_markup"]) for m, p in bot.out] == [False]


def test_send_photo_falls_back_to_plain_buttons():
    bot = Stub(p2p.Config(), fail_fancy=True)
    kb = B.deal_markup(deal(), deal_id=1, cfg=p2p.Config(), snap=snap([deal()]))
    r = asyncio.run(bot.send_photo(b"png", "cap", kb))
    assert r["ok"] and not bot.fancy
    assert [B.is_fancy(p["reply_markup"]) for m, p in bot.out if m == "sendPhoto"] == [True, False]


def test_fancy_buttons_env_off(monkeypatch):
    monkeypatch.setenv("FANCY_BUTTONS", "0")
    bot = Stub(p2p.Config())
    kb = B.deal_markup(deal(), deal_id=1, cfg=p2p.Config(), snap=snap([deal()]))
    asyncio.run(bot.send("hi", markup=kb))
    assert not B.is_fancy(bot.out[0][1]["reply_markup"]) and len(bot.out) == 1


def test_send_deal_passes_cfg_and_snap(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda *a, **k: b"png")
    bot = Stub(p2p.Config(amount=50000))
    d = deal()
    asyncio.run(bot.send_deal(d, snap=snap([d])))
    kb = [p["reply_markup"] for m, p in bot.out if m == "sendPhoto"][0]
    assert sum("copy_text" in b for b in buttons(kb)) == 2
