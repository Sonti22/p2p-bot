import asyncio
import functools
import time

import accounts
import bot as B
import p2p
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
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.max_signals = 2
    ds = [deal(5, "MEXC"), deal(4, "KuCoin"), deal(3, "HTX")]
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2          # повтор той же связки не шлём


def test_below_threshold_not_sent(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None: b"png")
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

    def fake_deal_card(d, c, amounts=None, rel=None):
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
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None: b"png")
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


def test_mark_done_unknown_id_not_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(B.trades, "log_trade", lambda *a, **k: logged.append(a))
    bot = Stub(p2p.Config())
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, 999))
    assert not logged
    assert "устарел" in bot.out[-1][1]["text"]


def test_calc_command_scans_with_custom_amount(offline, monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None: b"png")
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
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None: b"png")
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
