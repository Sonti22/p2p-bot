"""«🏦 Мои банки»: свои банки, «остальные тоже мои», тарифы с бесплатным лимитом СБП; только владельцу."""
import asyncio
import functools

import bot as B
import p2p
import paper
import trades
from test_bot import Stub, texts
from test_guests import Stub as GuestStub, msg


def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(tmp_path / ".env")))
    monkeypatch.delenv("OWN_BANKS", raising=False)
    monkeypatch.delenv("SBP_FREE_LIMITS", raising=False)


def _buttons(kb):
    return {b["callback_data"]: b["text"] for row in kb["inline_keyboard"] for b in row}


def _press(bot, data):
    asyncio.run(bot.on_callback({"id": "1", "data": data, "message": {"message_id": 5}}))
    return [p for m, p in bot.out if m == "editMessageText"][-1]


def test_default_view_lists_owner_banks_and_tariffs(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    text, kb = B.mybanks_view()
    buttons = _buttons(kb)
    for bank in ("T-Bank", "Sberbank", "Alfa-bank", "VTB", "Rosselkhozbank", "MTS Bank"):
        assert buttons[f"ownbank:{bank}"].startswith("✅"), bank
    assert not buttons["ownbank:Ozon Bank"].startswith("✅")
    assert buttons["ownbank:*"].startswith("✅")
    assert buttons["sbplim:T-Bank:100000"].startswith("✅") and buttons["sbplim:VTB:300000"].startswith("✅")
    assert "Т-Банк — бесплатно по СБП 100 000 ₽" in text and "ВТБ — бесплатно по СБП 300 000 ₽" in text


def test_toggle_banks_and_star(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    edit = _press(bot, "ownbank:Ozon Bank")
    assert "Ozon Bank" in trades.own_banks()[0] and _buttons(edit["reply_markup"])["ownbank:Ozon Bank"].startswith("✅")
    _press(bot, "ownbank:Ozon Bank")
    assert "Ozon Bank" not in trades.own_banks()[0]
    _press(bot, "ownbank:*")
    assert trades.own_banks()[1] is False
    _press(bot, "ownbank:NotABank")                       # не из списка кнопок — ничего не меняем
    assert "NotABank" not in trades.own_banks()[0]


def test_tariff_buttons_set_free_limit_and_ignore_unknown(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    _press(bot, "sbplim:T-Bank:300000")
    assert trades.free_limit("T-Bank") == 300000
    _press(bot, "sbplim:VTB:inf")
    assert trades.free_limit("VTB") == float("inf") and trades.free_limit("T-Bank") == 300000
    _press(bot, "sbplim:T-Bank:999")                      # чужое значение
    _press(bot, "sbplim:Sberbank:inf")                    # у Сбера кнопок тарифа нет
    assert trades.free_limit("T-Bank") == 300000 and trades.free_limit("Sberbank") == 100000
    assert "без лимита" in B.mybanks_view()[0]


def test_settings_has_button_and_command_works(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bot = Stub(p2p.Config())
    assert "mybanks" in _buttons(bot.settings_view()[1])
    asyncio.run(bot.dispatch("/mybanks", ""))
    assert "Мои банки и лимиты СБП" in texts(bot)[-1]


def test_guest_cannot_see_or_change_banks(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    bot = GuestStub(p2p.Config(), guests=["42"])
    for data in ("mybanks", "ownbank:Ozon Bank", "ownbank:*", "sbplim:T-Bank:300000"):
        before = len(bot.out)
        cq = {"id": "1", "data": data, "message": {"chat": {"id": 42}, "message_id": 5}}
        asyncio.run(bot.on_update({"callback_query": cq}))
        assert [m for m, _ in bot.out[before:]] == ["answerCallbackQuery"], data
    asyncio.run(bot.on_update(msg(42, "/mybanks")))
    assert bot.out[-1][1]["text"] == B.GUEST_DENIED
    assert trades.own_banks() == trades.own_banks() and "Ozon Bank" not in trades.own_banks()[0]
    assert trades.free_limit("T-Bank") == 100000


def test_paper_card_says_how_owner_pays(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    for pays, expect in ((("Tinkoff",), "внутри банка (Т-Банк)"), (("SBP - Fast Bank Transfer",), "СБП с Т-Банк")):
        for c in paper.open_cycles():
            paper.finish_cycle(c["id"], "done", 0.0)
        b = p2p.Ad("HTX", "buy", 87.5, 1000, 500000, 10000, list(pays), "m", 1000, 100.0, "", "USDT", "", "")
        s = p2p.Ad("KuCoin", "sell", 91.0, 1000, 500000, 10000, ["SBP"], "k", 1000, 100.0, "", "USDT", "", "")
        snap = p2p.Snapshot(88.0, "t", {"USDT": 88.0}, {}, [(2.8, b, s, "r")], {}, {}, {},
                            groups={("HTX", "buy", "USDT"): [b], ("KuCoin", "sell", "USDT"): [s]})
        bot = Stub(p2p.Config(min_profit=2.0))
        bot.live_scans = 1
        asyncio.run(bot.notify(snap))
        card = [t for t in texts(bot) if "Сухой прогон" in t][-1]
        assert f"оплата: {expect}" in card, card
