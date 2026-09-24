import asyncio
import functools
import time
from datetime import datetime

import accounts
import bot as B
import blacklist
import history
import p2p
import presets
import trades
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
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.max_signals = 2
    ds = [deal(5, "MEXC"), deal(4, "KuCoin"), deal(3, "HTX")]
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2          # повтор той же связки не шлём


def test_below_threshold_not_sent(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
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


def test_send_deal_passes_amount_breakdown_from_snap(monkeypatch):
    captured = {}

    def fake_deal_card(d, c, amounts=None, rel=None, breakdown=None):
        captured["amounts"] = amounts
        return b"png"

    monkeypatch.setattr(B, "deal_card", fake_deal_card)
    bot = Stub(p2p.Config())
    d = deal(5, "MEXC")
    asyncio.run(bot.send_deal(d, snap=snap([d])))
    assert captured["amounts"] is not None and set(captured["amounts"]) == set(p2p.DEPTH_AMOUNTS)


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


def test_send_deal_adds_done_button(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config())
    asyncio.run(bot.send_deal(deal(), "🔔 "))
    markup = photos(bot)[0][1]["markup"]
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b.get("callback_data", "").startswith("did:") for b in buttons)
    assert len(bot.deals_by_id) == 1


def test_mark_done_logs_trade_and_clears_button(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    st = trades.stats(path=db)
    assert st["day"]["count"] == 1 and st["day"]["amount"] == 70000
    assert deal_id not in bot.deals_by_id
    method, params = bot.out[-1]
    assert method == "editMessageReplyMarkup"
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(b.get("callback_data", "").startswith("did:") for b in buttons)


def test_deal_markup_has_hide_button():
    kb = B.deal_markup(deal(), deal_id=7)["inline_keyboard"]
    buttons = [b for row in kb for b in row]
    assert any(b.get("callback_data") == "bl:7" for b in buttons)


def test_hide_deal_blacklists_both_sides_and_clears_button(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    bot = Stub(p2p.Config())
    d = deal(5.0)
    deal_id = bot.remember_deal(d)
    asyncio.run(bot.hide_deal({"id": "1", "message": {"message_id": 9}}, deal_id))
    assert blacklist.blocked(path=db) == {("Bybit", "nick"), ("MEXC", "nick")}
    assert deal_id not in bot.deals_by_id
    method, params = bot.out[-1]
    assert method == "editMessageReplyMarkup"
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(b.get("callback_data", "").startswith("bl:") for b in buttons)


def test_hide_deal_unknown_id_not_blacklisted(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.hide_deal({"id": "1", "message": {"message_id": 9}}, 999))
    assert blacklist.blocked(path=db) == set()
    assert "устарел" in bot.out[-1][1]["text"]


def test_blacklist_command_lists_entries(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "list_all", functools.partial(B.blacklist.list_all, path=db))
    blacklist.add("Bybit", "Плохой", path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/blacklist"))
    text = texts(bot)[-1]
    assert "Плохой" in text


def test_blacklist_command_empty():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/blacklist"))
    assert "пуст" in texts(bot)[-1].lower()


def test_unbl_callback_removes_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "list_all", functools.partial(B.blacklist.list_all, path=db))
    monkeypatch.setattr(B.blacklist, "remove", functools.partial(B.blacklist.remove, path=db))
    blacklist.add("Bybit", "Плохой", path=db)
    entry_id = blacklist.list_all(path=db)[0][0]
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": f"unbl:{entry_id}", "message": {"message_id": 3}}))
    assert blacklist.list_all(path=db) == []
    method, params = bot.out[-1]
    assert method == "editMessageText" and "пуст" in params["text"].lower()


def test_add_alert_creates_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d"))
    rows = B.alerts.list_all("1", path=db)
    assert [(a, s, r, c, v, rl) for _, a, s, r, _, c, v, rl in rows] == \
        [("USDT", "sell", 92.0, None, None, 0)]
    assert "Алерт создан" in texts(bot)[-1]


def test_add_alert_repeat_creates_entry_with_cooldown(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d repeat 1h"))
    rows = B.alerts.list_all("1", path=db)
    assert [(a, s, r, c) for _, a, s, r, _, c, _, _ in rows] == [("USDT", "sell", 92.0, 3600)]
    assert "повтор" in texts(bot)[-1].lower()


def test_add_alert_volume_and_reliable_creates_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d vol 50к reliable repeat 1h"))
    rows = B.alerts.list_all("1", path=db)
    assert [(a, s, r, c, v, rl) for _, a, s, r, _, c, v, rl in rows] == \
        [("USDT", "sell", 92.0, 3600, 50_000.0, 1)]
    text = texts(bot)[-1]
    assert "объём" in text.lower() and "надёжность" in text.lower()


def test_add_alert_bad_volume():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d vol abc"))
    assert "Объём" in texts(bot)[-1]


def test_add_alert_unknown_token_sends_help():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d bogus"))
    assert "Формат" in texts(bot)[-1]


def test_add_alert_bad_repeat_duration():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d repeat 999d"))
    assert "Кулдаун" in texts(bot)[-1]


def test_add_alert_bad_format_sends_help():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell"))
    assert "Формат" in texts(bot)[-1]


def test_add_alert_unknown_asset():
    bot = Stub(p2p.Config(assets=["USDT"]))
    asyncio.run(bot.handle("/alert BTC sell 92 7d"))
    assert "не отслеживается" in texts(bot)[-1]


def test_add_alert_bad_duration():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 999d"))
    assert "Срок" in texts(bot)[-1]


def test_alerts_command_lists_entries(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "list_all", functools.partial(B.alerts.list_all, path=db))
    B.alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alerts"))
    assert "USDT" in texts(bot)[-1]


def test_alerts_command_empty():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alerts"))
    assert "нет" in texts(bot)[-1].lower()


def test_delalert_callback_removes_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "list_all", functools.partial(B.alerts.list_all, path=db))
    monkeypatch.setattr(B.alerts, "remove", functools.partial(B.alerts.remove, path=db))
    alert_id = B.alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": f"delalert:{alert_id}", "message": {"message_id": 3}}))
    assert B.alerts.list_all("1", path=db) == []
    method, params = bot.out[-1]
    assert method == "editMessageText" and "нет" in params["text"].lower()


def test_check_alerts_sends_message(monkeypatch):
    ad = make_ad("MEXC", "sell", 93.0)
    monkeypatch.setattr(B.alerts, "due", lambda snap, cfg: [(1, "1", "USDT", "sell", 92.0, 93.0, ad)])
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_alerts(snap([])))
    assert "Алерт сработал" in texts(bot)[-1]


def test_mark_done_unknown_id_not_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(B.trades, "log_trade", lambda *a, **k: logged.append(a))
    bot = Stub(p2p.Config())
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, 999))
    assert not logged
    assert "устарел" in bot.out[-1][1]["text"]


def test_calc_command_scans_with_custom_amount(offline, monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "top_chart", lambda snap, c: b"png")
    bot = Stub(p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"],
                          min_orders=0, min_rate=0, amount=50000))
    asyncio.run(bot.handle("/calc 20000"))
    caps = [p["caption"] for m, p in photos(bot)]
    assert any("20 000" in c for c in caps)
    assert bot.cfg.amount == 50000            # настройки не изменились


def test_calc_command_rejects_garbage_amount():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/calc много"))
    assert "сумму" in texts(bot)[-1].lower()
    assert not bot.out or bot.out[-1][0] == "sendMessage"


def test_calc_command_needs_argument():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/calc"))
    assert "сумма" in texts(bot)[-1].lower()


def test_settings_view_has_custom_amount_button():
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "amt_custom" for b in buttons)


def test_amt_custom_button_arms_waiting_state():
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "amt_custom", "message": {"message_id": 1}}))
    assert bot.awaiting_amount
    assert "сумму" in texts(bot)[-1].lower()


def test_custom_amount_text_scans_and_saves(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "top_chart", lambda snap, c: b"png")
    env = tmp_path / ".env"
    env.write_text("AMOUNT=50000\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"],
                          min_orders=0, min_rate=0, amount=50000))
    bot.awaiting_amount = True
    asyncio.run(bot.handle("30 000"))
    assert not bot.awaiting_amount
    assert bot.cfg.amount == 30000
    assert "AMOUNT=30000" in env.read_text(encoding="utf-8")
    caps = [p["caption"] for m, p in photos(bot)]
    assert any("30 000" in c for c in caps)


def test_custom_amount_garbage_reports_error():
    bot = Stub(p2p.Config())
    bot.awaiting_amount = True
    asyncio.run(bot.handle("ерунда"))
    assert not bot.awaiting_amount
    assert "сумму" in texts(bot)[-1].lower()


def test_awaiting_amount_reset_by_other_button():
    bot = Stub(p2p.Config())
    bot.awaiting_amount = True
    asyncio.run(bot.on_callback({"id": "1", "data": "best", "message": {"message_id": 1}}))
    assert not bot.awaiting_amount


def test_awaiting_amount_reset_by_other_command():
    bot = Stub(p2p.Config())
    bot.awaiting_amount = True
    asyncio.run(bot.handle("/best"))
    assert not bot.awaiting_amount


def test_accounts_view_lists_exchanges_with_status(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "abcd1234", "secret")
    cfg = p2p.Config(exchanges=["bybit", "mexc", "htx"])
    text, kb = B.accounts_view(cfg)
    assert "✅ Bybit" in text and "➖ MEXC" in text and "➖ HTX" in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc:bybit" in callbacks and "acc:mexc" in callbacks


def test_account_view_connected_shows_masked_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "abcd1234", "secret")
    text, kb = B.account_view("bybit")
    assert accounts.mask("abcd1234") in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc_check:bybit" in callbacks and "acc_del:bybit" in callbacks


def test_account_view_not_connected_offers_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    text, kb = B.account_view("mexc")
    assert "не подключён" in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc_add:mexc" in callbacks


def test_account_view_hint_is_exchange_specific():
    bybit_text, _ = B.account_view("bybit")
    mexc_text, _ = B.account_view("mexc")
    assert "API Management" in mexc_text and "API Management" not in bybit_text
    assert "Create New Key" in bybit_text and "Create New Key" not in mexc_text
    assert "Read-Only" in bybit_text and "Read Info" in mexc_text


def test_acc_add_sends_exchange_specific_hint():
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_add:mexc", "message": {"message_id": 1}}))
    assert any("API Management" in p.get("text", "") for m, p in bot.out if m == "sendMessage")


def test_account_view_unsupported_exchange_has_no_connect_button(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    text, kb = B.account_view("bitpapa")
    assert "не реализовано" in text
    callbacks = [b.get("callback_data", "") for row in kb["inline_keyboard"] for b in row]
    assert not any(c.startswith("acc_add:") for c in callbacks)


def test_account_view_kucoin_offers_connect_button(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    text, kb = B.account_view("kucoin")
    assert "не подключён" in text and "passphrase" in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc_add:kucoin" in callbacks


def test_acc_add_kucoin_arms_awaiting_key_three_step_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return False, "kucoin: подпись запросов пока не реализована"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_add:kucoin", "message": {"message_id": 1}}))
    assert bot.awaiting_key == {"ex": "kucoin", "step": "key"}

    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    assert bot.awaiting_key == {"ex": "kucoin", "step": "secret", "key": "APIKEY123"}

    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert bot.awaiting_key == {"ex": "kucoin", "step": "passphrase", "key": "APIKEY123", "secret": "SECRET456"}
    assert accounts.keys("kucoin") is None   # ещё не сохранён — ждём passphrase

    asyncio.run(bot.handle_key_input("PASS789", 57))
    assert bot.awaiting_key is None
    assert accounts.keys("kucoin") == ("APIKEY123", "SECRET456")
    assert accounts.passphrase("kucoin") == "PASS789"


def test_settings_view_has_accounts_button():
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "accounts" for b in buttons)


def test_acc_add_callback_arms_awaiting_key():
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_add:bybit", "message": {"message_id": 1}}))
    assert bot.awaiting_key == {"ex": "bybit", "step": "key"}
    assert "API key" in texts(bot)[-1]


def test_handle_key_input_flow_saves_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return True, "ключ рабочий, доступ только для чтения"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    assert ("deleteMessage", {"chat_id": "1", "message_id": 55}) in bot.out
    assert bot.awaiting_key == {"ex": "bybit", "step": "secret", "key": "APIKEY123"}

    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert bot.awaiting_key is None
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert any("✅ Подключено" in t for t in texts(bot))


def test_handle_key_input_reports_failed_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return False, "Invalid api_key"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert any("Invalid api_key" in t for t in texts(bot))


def test_on_update_routes_plain_text_to_key_input_when_awaiting():
    bot = Stub(p2p.Config())
    bot.chat_id = "1"
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.on_update({"message": {"chat": {"id": 1}, "text": "APIKEY123", "message_id": 7}}))
    assert ("deleteMessage", {"chat_id": "1", "message_id": 7}) in bot.out
    assert bot.awaiting_key == {"ex": "bybit", "step": "secret", "key": "APIKEY123"}


def test_on_update_command_bypasses_key_input():
    bot = Stub(p2p.Config())
    bot.chat_id = "1"
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.on_update({"message": {"chat": {"id": 1}, "text": "/best", "message_id": 7}}))
    assert not any(m == "deleteMessage" for m, _ in bot.out)
    assert bot.awaiting_key is None


def test_other_callback_resets_awaiting_key():
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.on_callback({"id": "1", "data": "best", "message": {"message_id": 1}}))
    assert bot.awaiting_key is None


def test_command_resets_awaiting_key():
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle("/best"))
    assert bot.awaiting_key is None


def test_acc_check_callback_reports_status(monkeypatch):
    async def fake_verify(s, ex):
        return False, "bad key"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert "bad key" in texts(bot)[-1]


def test_acc_del_callback_removes_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_del:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") is None
    assert any("удалён" in t for t in texts(bot))


def test_check_key_safety_removes_unsafe_key_and_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")

    async def fake_permissions(s, ex):
        return False, "торговля"

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert accounts.keys("bybit") is None
    warning = texts(bot)[-1]
    assert "торговля" in warning and "ТОЛЬКО для чтения" in warning


def test_check_key_safety_keeps_readonly_key_and_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")

    async def fake_permissions(s, ex):
        return True, ""

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert accounts.keys("mexc") == ("k", "s")
    assert texts(bot) == []


def test_check_key_safety_skips_exchanges_without_saved_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []

    async def fake_permissions(s, ex):
        calls.append(ex)
        return True, ""

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert calls == []


def test_stats_view_reports_counts(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "stats", functools.partial(B.trades.stats, path=db))
    trades.log_trade(deal(2.5), 50000, path=db)
    bot = Stub(p2p.Config())
    text = bot.stats_view()
    assert "За сегодня: 1 сделок" in text and "За неделю: сделок нет" not in text
    assert "За месяц" in text


def test_traps_view_empty():
    p2p.TRAPS_LOG.clear()
    text = B.traps_view()
    assert "Пока ни одной" in text


def test_traps_view_lists_reasons():
    p2p.TRAPS_LOG.clear()
    p2p.TRAPS_LOG.append(p2p._trap_entry(make_ad(side="sell", price=120.0), ref=90.0, cfg=p2p.Config()))
    text = B.traps_view()
    assert "продать" in text and "выше рынка" in text


def test_portfolio_view_no_exchanges_connected():
    text = B.portfolio_view({}, None)
    assert "Ни одна биржа не подключена" in text


def test_portfolio_view_sums_total_in_rub():
    port = {"bybit": {"USDT": 10.0, "BTC": 0.01}, "mexc": {"USDT": 5.0}}
    snap = p2p.Snapshot(88.0, "test", {"USDT": 88.0, "BTC": 5_000_000.0}, {}, [], {}, {}, {})
    text = B.portfolio_view(port, snap)
    assert "Bybit" in text and "MEXC" in text
    assert "10 USDT" in text and "0.01 BTC" in text
    # 10*88 + 0.01*5_000_000 + 5*88 = 880 + 50000 + 440 = 51320
    assert "51 320" in text


def test_portfolio_view_missing_ref_marked_instead_of_crashing():
    port = {"bybit": {"TON": 100.0}}
    snap = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})
    text = B.portfolio_view(port, snap)
    assert "нет ориентира" in text


def test_portfolio_rows_matches_totals_from_view():
    port = {"bybit": {"USDT": 10.0, "BTC": 0.01}, "mexc": {"USDT": 5.0}}
    snap = p2p.Snapshot(88.0, "test", {"USDT": 88.0, "BTC": 5_000_000.0}, {}, [], {}, {}, {})
    rows, total = B.portfolio_rows(port, snap)
    assert total == 51_320.0
    names = [name for name, _ in rows]
    assert names == ["Bybit", "MEXC"]
    assert rows[0][1] == [("BTC", 0.01, 50_000.0), ("USDT", 10.0, 880.0)]


def test_balance_command_sends_card_with_refresh_button(monkeypatch):
    async def fake_portfolio(s):
        return {"mexc": {"USDT": 1.0}}

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    monkeypatch.setattr(B, "portfolio_card", lambda rows, total: b"png")
    bot = Stub(p2p.Config())
    bot.last = p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, [], {}, {}, {})
    asyncio.run(bot.handle("/balance"))
    pics = photos(bot)
    assert len(pics) == 1
    assert "MEXC" in pics[0][1]["caption"]
    assert pics[0][1]["markup"]["inline_keyboard"][0][0]["callback_data"] == "balance"


def test_balance_falls_back_to_text_when_card_render_fails(monkeypatch):
    async def fake_portfolio(s):
        return {"mexc": {"USDT": 1.0}}

    def boom(rows, total):
        raise ValueError("render error")

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    monkeypatch.setattr(B, "portfolio_card", boom)
    bot = Stub(p2p.Config())
    bot.last = p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, [], {}, {}, {})
    asyncio.run(bot.handle("/balance"))
    assert not photos(bot)
    assert any("MEXC" in t for t in texts(bot))


def test_balance_callback_refreshes(monkeypatch):
    async def fake_portfolio(s):
        return {}

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "balance", "message": {"message_id": 1}}))
    assert any("Баланс" in p.get("text", "") for m, p in bot.out if m == "sendMessage")


def test_hist_text_formats_each_kind():
    dep = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    wd = {"kind": "withdraw", "asset": "USDT", "amount": 50.0, "ts": 1.0}
    trade = {"kind": "trade", "asset": "TON", "side": "buy", "amount": 10.0, "price": 3.5, "ts": 1.0}
    p2p_order = {"id": "1", "side": "sell", "asset": "USDT", "fiat": "RUB", "amount": 20.0, "price": 88.0, "ts": 1.0}
    assert "пришёл депозит" in B.hist_text("mexc", dep) and "100" in B.hist_text("mexc", dep)
    assert "исполнен вывод" in B.hist_text("mexc", wd)
    assert "купил" in B.hist_text("mexc", trade) and "TON" in B.hist_text("mexc", trade)
    assert "продал" in B.hist_text("bybit", p2p_order) and "RUB" in B.hist_text("bybit", p2p_order)


def test_hist_key_uses_id_or_composite():
    assert B.hist_key({"id": "42", "kind": "trade"}) == "42"
    a = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    b = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 2.0}
    assert B.hist_key(a) != B.hist_key(b)


def test_check_accounts_first_poll_is_silent_then_new_items_notify(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    hist = [{"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}]

    async def fake_history(s, ex, limit=20):
        return hist if ex == "mexc" else None

    monkeypatch.setattr(B.accounts, "account_history", fake_history)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_accounts())
    assert texts(bot) == []          # первый опрос — только запоминаем

    hist.append({"kind": "withdraw", "asset": "USDT", "amount": 30.0, "ts": 2.0})
    asyncio.run(bot.check_accounts())
    msgs = texts(bot)
    assert len(msgs) == 1 and "исполнен вывод" in msgs[0]

    asyncio.run(bot.check_accounts())
    assert len(texts(bot)) == 1       # повтор той же истории не шлём


def test_deal_markup_has_steps_button():
    kb = B.deal_markup(deal(), deal_id=7)["inline_keyboard"]
    buttons = [b for row in kb for b in row]
    assert any(b.get("callback_data") == "steps:7" for b in buttons)


def test_steps_view_lists_route_prices_and_breakdown():
    d = deal(5.0)
    _, b, s, _ = d
    text = B.steps_view(d, p2p.Config(amount=70000), snap([d]))
    assert f"по {p2p._price(b.price)}" in text and f"по {p2p._price(s.price)}" in text
    assert "Чистыми" in text
    assert "перепроверь перед сделкой" in text


def test_steps_view_shows_spot_rate_for_cross_asset_leg():
    d = deal(5.0, s_asset="ETH", route="спот USDT→ETH на Bybit (−0.1%)")
    sp = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2500.0, 2501.0)}}
    snapshot = p2p.Snapshot(88.0, "test", {}, {}, [d], {}, {}, {}, spot=sp)
    text = B.steps_view(d, p2p.Config(amount=70000), snapshot)
    assert "курс 2500/2501" in text


def test_show_steps_sends_message():
    bot = Stub(p2p.Config())
    d = deal(5.0)
    deal_id = bot.remember_deal(d, snap=snap([d]))
    asyncio.run(bot.show_steps({"id": "1"}, deal_id))
    assert "Шаги связки" in texts(bot)[-1]


def test_show_steps_unknown_id_not_sent():
    bot = Stub(p2p.Config())
    asyncio.run(bot.show_steps({"id": "1"}, 999))
    method, params = bot.out[-1]
    assert method == "answerCallbackQuery" and "устарел" in params["text"]


def test_check_accounts_skips_exchanges_without_saved_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []

    async def fake_history(s, ex, limit=20):
        calls.append(ex)
        return None

    monkeypatch.setattr(B.accounts, "account_history", fake_history)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_accounts())
    assert calls == []


# --- тихие часы и пауза сигналов ---

def msk_ts(hour, minute=0, day=24):
    """Unix-timestamp для заданного часа:минуты 24 сентября 2026 по МСК — удобно для тестов без сети."""
    return datetime(2026, 9, day, hour, minute, tzinfo=B.MSK).timestamp()


def test_parse_pause_arg_variants():
    assert B.parse_pause_arg("30m") == 1800
    assert B.parse_pause_arg("1h") == 3600
    assert B.parse_pause_arg("3h") == 10800
    assert B.parse_pause_arg("до утра") == "morning"
    assert B.parse_pause_arg("Утра") == "morning"
    assert B.parse_pause_arg("ерунда") is None
    assert B.parse_pause_arg("") is None


def test_in_quiet_hours_window_and_edges():
    assert B.in_quiet_hours("01:00-08:00", msk_ts(2, 30))
    assert not B.in_quiet_hours("01:00-08:00", msk_ts(9, 0))
    assert not B.in_quiet_hours("01:00-08:00", msk_ts(0, 30))
    assert not B.in_quiet_hours("", msk_ts(2, 0))
    assert not B.in_quiet_hours("ерунда", msk_ts(2, 0))


def test_in_quiet_hours_wraps_midnight():
    assert B.in_quiet_hours("23:00-07:00", msk_ts(0, 30))
    assert B.in_quiet_hours("23:00-07:00", msk_ts(23, 30))
    assert not B.in_quiet_hours("23:00-07:00", msk_ts(12, 0))


def test_quiet_hours_end_ts_next_occurrence():
    end = B.quiet_hours_end_ts("01:00-08:00", msk_ts(2, 30))
    assert datetime.fromtimestamp(end, B.MSK).strftime("%d %H:%M") == "24 08:00"
    end2 = B.quiet_hours_end_ts("01:00-08:00", msk_ts(9, 0))   # уже позже конца окна — переносим на завтра
    assert datetime.fromtimestamp(end2, B.MSK).strftime("%d %H:%M") == "25 08:00"


def test_quiet_hours_blocks_signal_and_stores_for_digest(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    d = deal(5, "MEXC")
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert not bot.out                              # сигнал не отправлен
    assert list(bot.night_deals.values()) == [d]     # но накоплен для утреннего дайджеста


def test_quiet_hours_off_sends_signal_as_usual(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config(min_profit=2.0))
    d = deal(5, "MEXC")
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert photos(bot)                               # тихие часы выключены — сигнал уходит как обычно


def test_night_digest_aggregates_across_scans_and_sends_top3_once(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    a, bd, c = deal(5, "MEXC"), deal(4, "KuCoin"), deal(6, "HTX")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([a, bd])))
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(3, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([c])))
    assert not texts(bot)                            # всю ночь — тишина, дайджеста ещё нет
    assert len(bot.night_deals) == 3

    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))     # тихие часы закончились
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    msgs = texts(bot)
    assert len(msgs) == 1 and "Топ-3 связки за ночь" in msgs[0]
    assert msgs[0].index("+6.00%") < msgs[0].index("+5.00%") < msgs[0].index("+4.00%")  # по убыванию прибыли
    assert bot.night_deals == {}

    asyncio.run(bot.quiet_and_pause_tick(snap([])))   # повторный тик после конца ночи — дайджест не дублируем
    assert len(texts(bot)) == 1


def test_night_digest_empty_when_nothing_above_threshold(monkeypatch):
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    assert "связок выше порога не было" in texts(bot)[-1]


def test_pause_command_no_arg_is_indefinite():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/pause"))
    assert bot.paused and bot.pause_until == 0.0
    assert "паузе" in texts(bot)[-1]


def test_pause_1h_blocks_signals_and_expires(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    now = [1_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.handle("/pause 1h"))
    assert bot.pause_until == now[0] + 3600 and not bot.paused

    d = deal(5, "MEXC")
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert not photos(bot)                 # сигналы блокированы на время паузы

    now[0] += 3601                          # час прошёл — пауза истекла сама, без /resume
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert photos(bot)                      # сигналы снова идут


def test_pause_bad_arg_reports_error():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/pause ерунда"))
    assert "срок" in texts(bot)[-1].lower()
    assert bot.pause_until == 0.0 and not bot.paused


def test_pause_until_morning_uses_quiet_hours_end(monkeypatch):
    ts = msk_ts(2, 0)
    monkeypatch.setattr(B.time, "time", lambda: ts)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/pause до утра"))
    assert bot.pause_until == B.quiet_hours_end_ts(bot.quiet_hours, ts)
    assert "08:00" in texts(bot)[-1]


def test_pause_until_morning_without_quiet_hours_configured():
    bot = Stub(p2p.Config())
    bot.quiet_hours = ""
    asyncio.run(bot.handle("/pause до утра"))
    assert bot.pause_until == 0.0
    assert "QUIET_HOURS" in texts(bot)[-1]


def test_resume_command_clears_manual_and_timed_pause():
    bot = Stub(p2p.Config())
    bot.paused = True
    bot.pause_until = time.time() + 999
    asyncio.run(bot.handle("/resume"))
    assert not bot.paused and bot.pause_until == 0.0
    assert "включены" in texts(bot)[-1].lower()


def test_apply_resume_button_clears_timed_pause():
    bot = Stub(p2p.Config())
    bot.pause_until = time.time() + 500
    bot.apply("resume")
    assert bot.pause_until == 0.0 and not bot.paused


def test_settings_view_shows_quiet_hours_until(monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config())
    bot.quiet_on = True
    text, _ = bot.settings_view()
    assert "тихие часы до 08:00" in text


def test_settings_view_shows_pause_until():
    bot = Stub(p2p.Config())
    bot.pause_until = time.time() + 3600
    text, _ = bot.settings_view()
    assert "пауза до" in text


def test_settings_view_pause_button_reflects_timed_pause():
    bot = Stub(p2p.Config())
    bot.pause_until = time.time() + 3600
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "resume" for b in buttons)


def test_settings_view_quiet_toggle_button_and_persist(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("QUIET_HOURS_ON=0\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "quiet_on" for b in buttons)

    toast = bot.apply("quiet_on")
    assert bot.quiet_on and "включены" in toast
    assert "QUIET_HOURS_ON=1" in env.read_text(encoding="utf-8")

    toast2 = bot.apply("quiet_off")
    assert not bot.quiet_on and "выключены" in toast2
    assert "QUIET_HOURS_ON=0" in env.read_text(encoding="utf-8")


# --- Фильтры монет/площадок и пресеты ---

def test_settings_view_has_filters_button():
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "filters" for b in buttons)


def test_filters_view_lists_asset_and_exchange_toggles():
    bot = Stub(p2p.Config())
    text, kb = B.filters_view(bot.cfg)
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "flt_a:USDT" for b in buttons)
    assert any(b.get("callback_data") == "flt_e:bybit" for b in buttons)
    assert any(b.get("callback_data") == "flt_e:bestchange" for b in buttons)
    assert "USDT" in text and "bybit" in text


def test_filters_view_marks_enabled_and_disabled():
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"]))
    _, kb = B.filters_view(bot.cfg)
    buttons = {b["callback_data"]: b["text"] for row in kb["inline_keyboard"] for b in row}
    assert buttons["flt_a:USDT"].startswith("✅")
    assert buttons["flt_a:BTC"].startswith("⬜")
    assert buttons["flt_e:bybit"].startswith("✅")
    assert buttons["flt_e:mexc"].startswith("⬜")


def test_toggle_asset_off_updates_cfg_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ASSETS=USDT,USDC,BTC,ETH,TON\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    toast = bot.apply("flt_a:BTC")
    assert "Выключено" in toast and "BTC" not in bot.cfg.assets
    saved = [x.strip() for x in env.read_text(encoding="utf-8").split("ASSETS=", 1)[1].splitlines()[0].split(",")]
    assert "BTC" not in saved


def test_toggle_asset_on_updates_cfg_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ASSETS=USDT\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT"]))
    toast = bot.apply("flt_a:BTC")
    assert "Включено" in toast and "BTC" in bot.cfg.assets
    assert "ASSETS=USDT,BTC" in env.read_text(encoding="utf-8")


def test_toggle_exchange_updates_cfg_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EXCHANGES=bybit,mexc\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(exchanges=["bybit", "mexc"]))
    toast = bot.apply("flt_e:mexc")
    assert "Выключено" in toast and bot.cfg.exchanges == ["bybit"]
    assert "EXCHANGES=bybit" in env.read_text(encoding="utf-8")


def test_cannot_disable_last_asset(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT"]))
    toast = bot.apply("flt_a:USDT")
    assert "нельзя" in toast.lower()
    assert bot.cfg.assets == ["USDT"]
    assert env.read_text(encoding="utf-8") == ""   # .env не тронут


def test_cannot_disable_last_exchange(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    toast = bot.apply("flt_e:bybit")
    assert "нельзя" in toast.lower()
    assert bot.cfg.exchanges == ["bybit"]
    assert env.read_text(encoding="utf-8") == ""


def test_flt_callback_rerenders_filters_view(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ASSETS=USDT,BTC\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT", "BTC"]))
    asyncio.run(bot.on_callback({"id": "1", "data": "flt_a:BTC", "message": {"message_id": 1}}))
    method, params = bot.out[-1]
    assert method == "editMessageText" and "Фильтры" in params["text"]


def test_preset_save_flow_writes_current_filters(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    monkeypatch.setattr(B.presets, "save_preset", functools.partial(B.presets.save_preset, path=str(pfile)))
    monkeypatch.setattr(B.presets, "list_custom", functools.partial(B.presets.list_custom, path=str(pfile)))
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"], min_profit=3.0, amount=70000))
    asyncio.run(bot.on_callback({"id": "1", "data": "preset_save", "message": {"message_id": 1}}))
    assert bot.awaiting_preset_name
    asyncio.run(bot.handle("Мой набор"))
    assert not bot.awaiting_preset_name
    saved = presets.list_custom(path=str(pfile))
    assert saved["Мой набор"]["assets"] == ["USDT"] and saved["Мой набор"]["amount"] == 70000
    assert any("сохранён" in t for t in texts(bot))


def test_preset_save_empty_name_not_saved(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    monkeypatch.setattr(B.presets, "save_preset", functools.partial(B.presets.save_preset, path=str(pfile)))
    bot = Stub(p2p.Config())
    bot.awaiting_preset_name = True
    asyncio.run(bot.handle("   "))
    assert not pfile.exists()
    assert "не сохранён" in texts(bot)[-1].lower()


def test_apply_saved_preset_updates_cfg_and_env(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    presets.save_preset("Быстрый", p2p.Config(assets=["USDT"], exchanges=["bybit", "mexc"],
                                              min_profit=3.0, amount=70000), path=str(pfile))
    monkeypatch.setattr(B.presets, "get_preset", functools.partial(B.presets.get_preset, path=str(pfile)))
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    toast = bot.apply("preset_apply:Быстрый")
    assert "Быстрый" in toast
    assert bot.cfg.assets == ["USDT"] and bot.cfg.exchanges == ["bybit", "mexc"]
    assert bot.cfg.min_profit == 3.0 and bot.cfg.amount == 70000
    text = env.read_text(encoding="utf-8")
    assert "MIN_PROFIT=3" in text and "AMOUNT=70000" in text


def test_apply_builtin_preset_usdt_no_transfer(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    bot.apply("preset_apply:USDT без переводов")
    assert bot.cfg.assets == ["USDT"] and bot.cfg.same_venue_only is True
    assert "SAME_VENUE_ONLY=1" in env.read_text(encoding="utf-8")


def test_apply_builtin_preset_all_venues_resets(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"], same_venue_only=True))
    bot.apply("preset_apply:Все площадки")
    assert set(bot.cfg.exchanges) == set(p2p.ALL_EXCHANGES.split(","))
    assert set(bot.cfg.assets) == set(p2p.DEFAULT_ASSETS.split(","))
    assert bot.cfg.same_venue_only is False


def test_apply_unknown_preset_reports_not_found():
    bot = Stub(p2p.Config())
    assert "не найден" in bot.apply("preset_apply:нет такого").lower()


def test_presets_view_lists_builtin_and_custom(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    presets.save_preset("Мой", p2p.Config(), path=str(pfile))
    monkeypatch.setattr(B.presets, "list_custom", functools.partial(B.presets.list_custom, path=str(pfile)))
    text, kb = B.presets_view(p2p.Config())
    assert "USDT без переводов" in text and "Мой" in text
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "preset_apply:Мой" for b in buttons)
    assert any(b.get("callback_data") == "preset_del:Мой" for b in buttons)
    assert not any(b.get("callback_data") == "preset_del:USDT без переводов" for b in buttons)


def test_preset_del_callback_removes_and_rerenders(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    presets.save_preset("Старый", p2p.Config(), path=str(pfile))
    monkeypatch.setattr(B.presets, "delete_preset", functools.partial(B.presets.delete_preset, path=str(pfile)))
    monkeypatch.setattr(B.presets, "list_custom", functools.partial(B.presets.list_custom, path=str(pfile)))
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "preset_del:Старый", "message": {"message_id": 1}}))
    assert "Старый" not in presets.list_custom(path=str(pfile))
    method, params = bot.out[-1]
    assert method == "editMessageText" and "Пресеты" in params["text"]


def test_history_command_empty_sends_message_no_photos(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/history"))
    assert "пуста" in texts(bot)[-1]
    assert not photos(bot)


def test_history_command_renders_two_photos(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    for name in ("is_empty", "hourly_avg", "heatmap", "median_vs_bestchange"):
        monkeypatch.setattr(B.history, name, functools.partial(getattr(B.history, name), path=db))
    history._insert([(time.time(), "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0)], db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/history"))
    assert len(photos(bot)) == 2


def test_history_callback_routes_to_show_history(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "history", "message": {"message_id": 1}}))
    assert "пуста" in texts(bot)[-1]
