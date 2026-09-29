"""Подключение ядра к боту (trading/wiring.py и хуки в bot.py): старт, сверка и доставка событий, /trading, кнопки
trd_* только владельцу, подтверждение ордера одноразовым токеном, стоп торговли. Сеть — нет (заглушки), ключи — фиктивные.
"""
import asyncio
from decimal import Decimal

import pytest

import bot as B
import p2p
from helpers import arun
from trading import journal, keys, risk, switch, venues, wiring
from trading_stubs import CREDS, fresh_journal

OWNER = 1


class Stub(B.Bot):
    """Бот без сети: вызовы Telegram — в self.out; ответы sendMessage можно задать очередью replies."""

    def __init__(self, cfg=None, replies=None):
        super().__init__(None, "x", str(OWNER), cfg or p2p.Config())
        self.out, self.replies = [], list(replies or [])

    async def call(self, method, **p):
        self.out.append((method, p))
        if method == "sendMessage" and self.replies:
            return self.replies.pop(0)
        return {"ok": True, "result": {"message_id": len(self.out)}}


def sent(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def msg(chat, text, ctype="private", sender=None):
    return {"message": {"message_id": 5, "chat": {"id": chat, "type": ctype}, "text": text,
                        "from": {"id": chat if sender is None else sender, "first_name": "Вася"}}}


def press(chat, data, sender=None, message_id=7, ctype="private"):
    return {"callback_query": {"id": "cq", "data": data, "from": {"id": chat if sender is None else sender},
                               "message": {"message_id": message_id, "chat": {"id": chat, "type": ctype}}}}


def order(qty="0.001"):
    return venues.Order("bybit", "linear", "BTCUSDT", "buy", "market", Decimal(qty), stop_loss=Decimal("50000"))


@pytest.fixture(autouse=True)
def env_path(monkeypatch, tmp_path):
    """.env, который цикл и кнопки перечитывают на лету, — в tmp (по умолчанию его нет: торговля выключается)."""
    path = tmp_path / ".env"
    monkeypatch.setattr(wiring, "ENV_PATH", str(path))
    return path


@pytest.fixture
def core(monkeypatch, tmp_path):
    """Журнал и проверка ключей — в tmp; торговые ключи Bybit/BingX — фиктивные и «проверенные»; TRADING=1 confirm
    (и в процессе, и в .env)."""
    fresh_journal(monkeypatch, tmp_path)
    env_file(tmp_path)
    monkeypatch.setattr(keys, "raw_credentials", lambda v: CREDS)
    monkeypatch.setattr(switch, "STATE_PATH", str(tmp_path / "trading_state.json"))
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "confirm")
    calls = []

    async def fake_submit(s, o, creds, **kw):
        calls.append((o, creds, kw))
        return {"state": "open", "row": None, "reason": "", "event": None, "filled": Decimal(0)}

    monkeypatch.setattr(journal, "submit", fake_submit)
    return calls


def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TRADING=1\nTRADING_MODE=confirm\n", encoding="utf-8")
    return path


def saver(path):
    def save(k, v):
        B.save_env(k, v, path=str(path))
    return save


# --- старт ---

def test_startup_without_keys_turns_trading_off_and_tells_owner(monkeypatch, tmp_path):
    fresh_journal(monkeypatch, tmp_path)
    monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
    monkeypatch.setenv("TRADING", "1")
    bot = Stub()
    text = arun(wiring.startup(bot))
    assert not switch.enabled() and "проверенного торгового ключа нет" in text and sent(bot) == [text]


def test_startup_without_keys_and_trading_off_is_silent(monkeypatch, tmp_path):
    fresh_journal(monkeypatch, tmp_path)
    monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
    monkeypatch.setenv("TRADING", "0")
    bot = Stub()
    assert arun(wiring.startup(bot)) == "" and sent(bot) == []


def test_startup_key_with_withdraw_rights_turns_trading_off(core, monkeypatch):
    async def check(s, path=None):
        return {"bybit": keys.KeyCheck(False, "unsafe", "у ключа лишние права: Wallet:Withdraw", True),
                "bingx": keys.KeyCheck(True, "ok", "", True)}

    monkeypatch.setattr(keys, "startup_check", check)

    async def margin(s, v, sym, creds):
        return "isolated", ""

    monkeypatch.setattr(venues, "margin_mode", margin)
    bot = Stub()
    text = arun(wiring.startup(bot))
    assert not switch.enabled()
    assert "сверх торговли" in text and "Bybit" in text and "Wallet:Withdraw" in text
    assert len(sent(bot)) == 1                                        # всё — одним сообщением


def test_startup_warnings_in_one_message(core, monkeypatch):
    async def check(s, path=None):
        return {v: keys.KeyCheck(True, "ok", "", v == "bingx") for v in venues.VENUES}

    async def margin(s, v, sym, creds):
        return ("cross", "") if v == "bybit" else ("isolated", "")

    monkeypatch.setattr(keys, "startup_check", check)
    monkeypatch.setattr(venues, "margin_mode", margin)
    bot = Stub()
    text = arun(wiring.startup(bot))
    assert switch.enabled() and len(sent(bot)) == 1
    assert "кросс-маржа" in text and "без привязки к IP" in text and "режим confirm" in text


def test_main_takes_trading_switch_from_env_file_only(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TRADING=0\nTRADING_MODE=auto\n", encoding="utf-8")
    monkeypatch.setenv("TRADING", "1")
    monkeypatch.setenv("TRADING_MODE", "minlot")
    assert switch.switch_from_file(str(path)) == (False, "minlot")   # окружение не включит и не поднимет
    src = open(B.__file__, encoding="utf-8").read()
    assert "trading.switch.switch_from_file(ENV_PATH)" in src and "trading.wiring.run(bot)" in src


# --- цикл: доставка событий, сбои ---

def test_outbox_marked_delivered_only_after_telegram_ok(core):
    journal.emit("foreign", "BTCUSDT: ваша позиция", dedup="a")
    journal.emit("stop_missing", "BTCUSDT без стопа", dedup="b")
    bot = Stub(replies=[{"ok": True, "result": {"message_id": 1}}, {"ok": False, "error_code": 502}])
    assert arun(wiring.deliver(bot)) == 1
    left = journal.pending_events()
    assert [e["event"] for e in left] == ["stop_missing"]
    assert "ваша ручная позиция" in sent(bot)[0] and "BTCUSDT" in sent(bot)[0]
    assert arun(wiring.deliver(bot)) == 1 and journal.pending_events() == []


def test_event_texts_are_russian_and_escaped():
    ev = {"event": "closed_by_venue", "note": "<b>x</b>", "client_id": "t1"}
    text = wiring.event_text(ev)
    assert "закрыла биржа" in text and "&lt;b&gt;x" in text
    assert "событие ядра" in wiring.event_text({"event": "новое", "note": "", "client_id": ""})
    for e in ("unknown", "mismatch", "foreign", "stop_missing", "stop_removed", "closed_by_venue", "spot_dust",
              "day_stop"):
        assert e in wiring.EVENTS


def test_day_stop_event_once_per_day(core):
    journal.add_pnl("bybit", "BTCUSDT", "-1000", "trade", "r1")
    assert wiring.check_day_stop() and wiring.check_day_stop()
    assert [e["event"] for e in journal.pending_events()] == ["day_stop"]


def test_core_failure_does_not_break_loop_one_alarm(core, monkeypatch):
    bot = Stub()
    fails, sleeps = [], []

    async def boom(b):
        fails.append(1)
        raise RuntimeError("db")

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(wiring, "tick", boom)
    monkeypatch.setattr(wiring.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        arun(wiring.loop(bot))
    assert len(fails) == 3 and sleeps == [wiring.FAIL_PAUSE] * 3
    assert len([t for t in sent(bot) if "сбой сверки" in t]) == 1


def test_tick_reconciles_only_with_keys(core, monkeypatch):
    seen = []

    async def rec(s, creds_for, now=None, positions=True):
        seen.append(creds_for("bybit"))
        return []

    monkeypatch.setattr(journal, "reconcile", rec)
    arun(wiring.tick(Stub()))
    assert seen == [CREDS]
    monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
    arun(wiring.tick(Stub()))
    assert len(seen) == 1


# --- /trading и кнопки — только владельцу ---

def test_trading_view_for_owner(core):
    bot = Stub()
    arun(bot.on_update(msg(OWNER, "/trading")))
    text = sent(bot)[-1]
    assert "торговля включена, режим confirm" in text and "Bybit: ✅ проверен" in text and "Позиции бота" in text
    assert "hedge" in text and "лимите убытка" in text


def test_trading_not_for_group_guest_or_stranger(core, monkeypatch):
    monkeypatch.setenv("TG_GUESTS", "42")
    bot = Stub()
    arun(bot.on_update(msg(OWNER, "/trading", ctype="group")))            # чат владельца, но группа
    arun(bot.on_update(msg(42, "/trading")))                              # гость
    arun(bot.on_update(msg(777, "/trading")))                             # чужой
    assert not any("Торговля</b>" in t for t in sent(bot))
    for u in (press(OWNER, "trd_stop", ctype="group"), press(42, "trd_stop"), press(777, "trd_stop"),
              press(OWNER, "trd_stop", sender=42)):                         # кнопку в чате владельца нажал другой
        arun(bot.on_update(u))
    assert switch.enabled() and switch.mode() == "confirm"


# --- подтверждение ордера ---

def test_order_only_after_confirmed_token(core):
    bot = Stub()
    token = arun(wiring.request_order(bot, order(), "hedge"))
    assert token and core == []                                          # карточка — не ордер
    card = [p for m, p in bot.out if m == "sendMessage"][-1]
    assert "Подтвердите ордер" in card["text"] and "BTCUSDT" in card["text"] and "стоп 50000" in card["text"]
    data = [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert data == [f"trd_ok:{token}", f"trd_no:{token}"]
    assert token in bot.trading_link.tokens and core == []           # без нажатия — ни одного ордера


def test_token_one_time_ttl_and_bound_to_card(core):
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        await wiring.callback(bot, press(OWNER, f"trd_ok:{token}", message_id=mid + 100)["callback_query"],
                              f"trd_ok:{token}", None)                    # чужая карточка: токен погашен
        assert token not in bot.trading_link.tokens
        token2 = await wiring.request_order(bot, order(), "hedge")
        mid2 = bot.trading_link.tokens[token2]["message_id"]
        cq = press(OWNER, f"trd_ok:{token2}", message_id=mid2)["callback_query"]
        assert await wiring.callback(bot, cq, f"trd_ok:{token2}", None) == "отправляю ордер"
        await asyncio.gather(*bot.trading_link.tasks)
        assert await wiring.callback(bot, cq, f"trd_ok:{token2}", None) != "отправляю ордер"   # второй раз — нет
        token3 = await wiring.request_order(bot, order(), "hedge")
        bot.trading_link.tokens[token3]["expires"] = 0                       # истёк
        cq3 = press(OWNER, "x", message_id=bot.trading_link.tokens[token3]["message_id"])["callback_query"]
        assert "устарела" in await wiring.callback(bot, cq3, f"trd_ok:{token3}", None)

    arun(go())
    assert len(core) == 1
    o, creds, kw = core[0]
    assert o == order() and creds == CREDS and kw["purpose"] == "open" and kw["strategy"] == "hedge"
    assert kw["mode"] == "confirm"


def test_confirm_through_bot_update_submits_once(core):
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        await bot.on_update(press(OWNER, f"trd_ok:{token}", message_id=mid))
        await asyncio.gather(*bot.trading_link.tasks)
        await bot.on_update(press(OWNER, f"trd_ok:{token}", message_id=mid))
        await asyncio.gather(*bot.trading_link.tasks)

    arun(go())
    assert len(core) == 1 and any(t.startswith("✅ Ордер: open") for t in sent(bot))


def test_confirm_after_trading_off_sends_nothing(core):
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        switch.disable()                                                 # TRADING=0 после карточки
        toast = await wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_ok:{token}",
                                      None)
        await asyncio.gather(*bot.trading_link.tasks)
        return toast

    assert "выключена" in arun(go()) and core == []


def test_request_refused_when_off_paper_no_key_or_reducing(core, monkeypatch):
    bot = Stub()
    monkeypatch.setenv("TRADING_MODE", "paper")
    assert arun(wiring.request_order(bot, order(), "hedge")) is None
    monkeypatch.setenv("TRADING_MODE", "confirm")
    reduce = venues.Order("bybit", "linear", "BTCUSDT", "sell", "market", Decimal("0.001"), reduce_only=True)
    assert arun(wiring.request_order(bot, reduce, "hedge")) is None
    monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
    assert arun(wiring.request_order(bot, order(), "hedge")) is None
    assert sent(bot) == [] and core == []


def test_cancel_button_kills_token(core):
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        await wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_no:{token}", None)
        await wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_ok:{token}", None)

    arun(go())
    assert core == []


# --- стоп и режим ---

def test_stop_writes_trading_off_and_kills_tokens(core, tmp_path):
    path = env_file(tmp_path)
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        await bot.on_update(press(OWNER, "trd_stop"))
        return token, mid

    import functools
    orig = B.save_env
    B.save_env = functools.partial(orig, path=str(path))
    try:
        token, mid = arun(go())
    finally:
        B.save_env = orig
    text = path.read_text(encoding="utf-8")
    assert "TRADING=0" in text and "TRADING_MODE=paper" in text
    assert not switch.enabled() and switch.mode() == "paper" and bot.trading_link.tokens == {}
    assert any("Торговля остановлена" in t for t in sent(bot))
    arun(wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_ok:{token}", saver(path)))
    assert core == []


def test_mode_can_only_be_lowered_from_telegram(core, tmp_path):
    path = env_file(tmp_path)
    bot = Stub()
    toast = arun(wiring.callback(bot, press(OWNER, "x")["callback_query"], "trd_mode:auto", saver(path)))
    assert "нельзя" in toast and switch.mode() == "confirm"
    toast = arun(wiring.callback(bot, press(OWNER, "x")["callback_query"], "trd_mode:minlot", saver(path)))
    assert switch.mode() == "minlot" and "TRADING_MODE=minlot" in path.read_text(encoding="utf-8")
    kb = [b["callback_data"] for row in bot.out[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert "trd_mode:paper" in kb and "trd_mode:confirm" not in kb and "trd_mode:auto" not in kb


def test_no_submit_call_outside_confirm_path():
    """Единственный вызов journal.submit в подключении — отправка подтверждённого токена (_submit)."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(wiring))
    callers = [f.name for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(isinstance(n, ast.Attribute) and n.attr == "submit" for n in ast.walk(f))]
    assert callers == ["_submit"]
    src = open(B.__file__, encoding="utf-8").read()
    assert "journal.submit" not in src and "request_order" not in src     # в боте стратегий и ордеров нет


# --- ревью: устаревшая проверка ключей, позиции без ключа, лимит 0, сбой /trading ---

def _stale(now):
    for v in venues.VENUES:
        keys.save_check(v, CREDS, keys.KeyCheck(True, "ok", "", True), now=now - keys.CHECK_TTL - 1)


def test_stale_key_check_is_rechecked_and_reconcile_continues(core, monkeypatch):
    import time
    now = time.time()
    _stale(now)
    asked, rec = [], []

    async def check(s, v, creds=None, path=None):
        asked.append(v)
        res = keys.KeyCheck(True, "ok", "", True)
        keys.save_check(v, CREDS, res)
        return res

    async def reconcile(s, creds_for, now=None, positions=True):
        rec.append(creds_for("bybit"))
        return []

    monkeypatch.setattr(keys, "check", check)
    monkeypatch.setattr(journal, "reconcile", reconcile)
    assert not wiring.has_keys()
    arun(wiring.tick(Stub(), now))
    assert asked == ["bybit", "bingx"] and rec == [CREDS] and switch.enabled()
    arun(wiring.tick(Stub(), now + 60))
    assert asked == ["bybit", "bingx"]                                     # свежая — снова не спрашиваем


def test_recheck_finds_withdraw_rights_turns_trading_off(core, monkeypatch):
    import time
    now = time.time()
    _stale(now)

    async def check(s, v, creds=None, path=None):
        res = keys.KeyCheck(False, "unsafe", "у ключа лишние права: Wallet:Withdraw", True)
        keys.save_check(v, CREDS, res)
        return res

    monkeypatch.setattr(keys, "check", check)
    bot = Stub()
    arun(wiring.tick(bot, now))
    assert not switch.enabled()
    assert any("сверх торговли" in t and "Withdraw" in t for t in sent(bot))


def test_failed_recheck_with_positions_alarms_and_waits_before_retry(core, monkeypatch):
    import time
    now = time.time()
    _stale(now)
    asked = []

    async def check(s, v, creds=None, path=None):
        asked.append(v)
        res = keys.KeyCheck(False, "unknown", "права не проверить: timeout", None)
        keys.save_check(v, CREDS, res)
        return res

    monkeypatch.setattr(keys, "check", check)
    monkeypatch.setattr(journal, "_exposure_symbols", lambda path=None: [("bybit", "linear", "BTCUSDT")])
    bot = Stub()
    arun(wiring.tick(bot, now))
    texts = sent(bot)
    assert any("без сопровождения" in t or "не работают" in t for t in texts)          # тревога unmanaged
    assert any("перепроверка прав" in t for t in texts) and not switch.enabled()
    arun(wiring.tick(bot, now + 60))
    assert asked == ["bybit", "bingx"]                                     # повтор не раньше RECHECK_RETRY
    arun(wiring.tick(bot, now + wiring.RECHECK_RETRY))
    assert asked == ["bybit", "bingx"] * 2


def test_zero_daily_limit_does_not_spam_day_stop(core, monkeypatch):
    monkeypatch.setenv("TRADING_DAILY_LOSS_USDT", "0")
    assert not wiring.check_day_stop() and journal.pending_events() == []


def test_trading_view_failure_still_answers_button(core, monkeypatch):
    monkeypatch.setattr(wiring, "view", lambda bot: (_ for _ in ()).throw(RuntimeError("database is locked")))
    bot = Stub()
    arun(wiring.callback(bot, press(OWNER, "x")["callback_query"], "trd_view", None))
    assert [m for m, _ in bot.out] == ["answerCallbackQuery", "sendMessage"]
    assert "не прочитать" in sent(bot)[-1] and "trd_stop" in str(bot.out[-1][1]["reply_markup"])
    arun(wiring.command(bot))
    assert "не прочитать" in sent(bot)[-1]


# --- ревью 2: выключатель из .env на лету, доставка при сбоях, лимиты этапа, ключи, старт задачей, топик, база ---

def _no_reconcile(monkeypatch, seen=None):
    async def reconcile(s, creds_for, now=None, positions=True):
        if seen is not None:
            seen.append(creds_for("bybit"))
        return []

    monkeypatch.setattr(journal, "reconcile", reconcile)


def _keys_ok_at_start(monkeypatch):
    async def check(s, path=None):
        return {v: keys.KeyCheck(True, "ok", "", True) for v in venues.VENUES}

    async def margin(s, v, sym, creds):
        return "isolated", ""

    monkeypatch.setattr(keys, "startup_check", check)
    monkeypatch.setattr(venues, "margin_mode", margin)


def test_env_trading_off_written_while_running_blocks_confirm(core, env_path, monkeypatch):
    """launcher пишет TRADING=0 в .env, пока старый бот работает: сверка (tick) это видит, кнопка ничего не шлёт."""
    _no_reconcile(monkeypatch)
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        env_path.write_text("TRADING=0\nTRADING_MODE=confirm\n", encoding="utf-8")
        await wiring.tick(bot)
        assert not switch.enabled() and bot.trading_link.tokens == {}
        toast = await wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_ok:{token}",
                                      None)
        await asyncio.gather(*bot.trading_link.tasks)
        return toast

    assert "выключена" in arun(go()) and core == []


def test_env_trading_off_seen_at_button_press_without_tick(core, env_path):
    bot = Stub()

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        env_path.write_text("TRADING=0\nTRADING_MODE=confirm\n", encoding="utf-8")   # цикл ещё не заметил
        toast = await wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_ok:{token}",
                                      None)
        await asyncio.gather(*bot.trading_link.tasks)
        return toast

    assert "выключена" in arun(go()) and core == [] and bot.trading_link.tokens == {}


def test_env_trading_off_between_press_and_submit_sends_nothing(core, env_path):
    bot = Stub()
    rec = {"order": order(), "strategy": "hedge", "group": ""}
    env_path.write_text("TRADING=0\n", encoding="utf-8")
    assert arun(wiring._submit(bot, rec)) is None and core == []
    assert any("не отправлен" in t for t in sent(bot))


def test_env_path_is_the_bots_env_file():
    import inspect
    import os
    assert os.path.normcase(wiring.ROOT) == os.path.normcase(os.path.dirname(os.path.abspath(B.__file__)))
    assert 'ENV_PATH = os.path.join(ROOT, ".env")' in inspect.getsource(wiring)


def test_env_reread_never_raises_trading_or_mode(core, env_path, monkeypatch):
    _no_reconcile(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", "minlot")
    env_path.write_text("TRADING=1\nTRADING_MODE=auto\n", encoding="utf-8")
    arun(wiring.tick(Stub()))
    assert switch.enabled() and switch.mode() == "minlot"                  # режим из файла не поднялся
    switch.disable()
    arun(wiring.tick(Stub()))
    assert not switch.enabled()                                            # и TRADING=1 в файле не включил


def test_state_write_failure_does_not_block_event_delivery(core, monkeypatch):
    """Сбой записи trading_state.json (антивирус держит файл на os.replace) — stop_breached всё равно доходит."""
    _no_reconcile(monkeypatch)
    journal.emit("stop_breached", "BTCUSDT: цена 49000 за стопом 50000", dedup="sb")

    def locked(*a, **k):
        raise PermissionError("os.replace")

    monkeypatch.setattr(switch, "write_state", locked)
    bot = Stub()
    arun(wiring.tick(bot))                                                 # тик не упал
    assert journal.pending_events() == [] and any("цена за стопом" in t for t in sent(bot))
    assert not any("сбой сверки" in t for t in sent(bot))


def test_day_stop_failure_does_not_block_event_delivery(core, monkeypatch):
    _no_reconcile(monkeypatch)
    journal.emit("foreign", "BTCUSDT: ваша позиция", dedup="f")
    monkeypatch.setattr(journal, "pnl_today", lambda now=None: (_ for _ in ()).throw(RuntimeError("locked")))
    bot = Stub()
    arun(wiring.tick(bot))
    assert journal.pending_events() == [] and any("ручная позиция" in t for t in sent(bot))


def test_reconcile_failure_still_delivers_events_and_alarms_once(core, monkeypatch):
    journal.emit("stop_breached", "BTCUSDT", dedup="sb")

    async def boom(s, creds_for, now=None, positions=True):
        raise RuntimeError("database is locked")

    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(journal, "reconcile", boom)
    monkeypatch.setattr(wiring.asyncio, "sleep", fake_sleep)
    bot = Stub()
    with pytest.raises(asyncio.CancelledError):
        arun(wiring.loop(bot))
    assert journal.pending_events() == []
    assert len([t for t in sent(bot) if "цена за стопом" in t]) == 1
    assert len([t for t in sent(bot) if "сбой сверки" in t]) == 1 and sleeps == [wiring.FAIL_PAUSE] * 6


def test_key_recheck_failure_does_not_stop_reconcile_or_delivery(core, monkeypatch):
    import time
    now = time.time()
    _stale(now)
    for v in venues.VENUES[1:]:                                            # BingX — свежий, Bybit — устарел
        keys.save_check(v, CREDS, keys.KeyCheck(True, "ok", "", True), now=now)
    seen = []
    _no_reconcile(monkeypatch, seen)

    async def check(s, v, creds=None, path=None):
        raise PermissionError("trading_keycheck.json")

    monkeypatch.setattr(keys, "check", check)
    journal.emit("foreign", "ETHUSDT", dedup="f")
    bot = Stub()
    with pytest.raises(PermissionError):
        arun(wiring.tick(bot, now))                                        # в loop: одна тревога и пауза
    assert len(seen) == 1 and journal.pending_events() == []              # сверка по BingX и доставка — были


def _parse_env_example():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env.example")
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and line.startswith("TRADING") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.split(" #")[0].strip()
    return out


def test_env_example_sets_owner_stage_limits_for_confirm(monkeypatch):
    """Решение владельца (29.09, хедж кругов в confirm): позиция и всего открыто до 250 USDT, убыток дня 5 USDT, плечо
    2 — в .env.example (этап wiring.STAGE), а не потолки кода 1000/2000/50."""
    env = _parse_env_example()
    for st in risk.STRATEGIES:
        lim = risk.limits(st, "confirm", environ=env)
        assert lim["position_usdt"] <= wiring.STAGE["position_usdt"] == 250
        assert lim["daily_loss_usdt"] <= wiring.STAGE["daily_loss_usdt"] == 5 and lim["leverage"] <= 2
        assert lim["total_usdt"] <= wiring.STAGE["total_usdt"] == 250
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("TRADING_MODE", "confirm")
    assert wiring.day_limit() == 5 and wiring.stage_warnings() == []


def test_confirm_with_code_caps_warns_owner_at_start_and_in_view(core, monkeypatch):
    _keys_ok_at_start(monkeypatch)
    bot = Stub()
    text = arun(wiring.startup(bot))
    assert "TRADING_MAX_POSITION_USDT=250" in text and "1000" in text and "TRADING_DAILY_LOSS_USDT=5" in text
    assert "TRADING_MAX_TOTAL_USDT=250" in text and "2000" in text
    assert "TRADING_MAX_POSITION_USDT=250" in wiring.view(bot)[0]
    monkeypatch.setenv("TRADING_MAX_POSITION_USDT", "120")                  # 120 — хедж только кругов до ~10 000 ₽
    monkeypatch.setenv("TRADING_MAX_TOTAL_USDT", "250")
    monkeypatch.setenv("TRADING_DAILY_LOSS_USDT", "5")
    assert "TRADING_MAX_POSITION_USDT" not in wiring.view(bot)[0] and "−5 USDT" in wiring.view(bot)[0]
    monkeypatch.setenv("TRADING_MAX_POSITION_USDT", "251")                  # выше этапа — снова предупреждение
    assert "TRADING_MAX_POSITION_USDT=250" in wiring.view(bot)[0]
    monkeypatch.setenv("TRADING_MAX_POSITION_USDT", "250")
    monkeypatch.delenv("TRADING_MAX_TOTAL_USDT")                            # всего — потолок кода 2000
    assert "TRADING_MAX_TOTAL_USDT=250" in wiring.view(bot)[0]
    monkeypatch.delenv("TRADING_DAILY_LOSS_USDT")
    monkeypatch.setenv("TRADING_MODE", "minlot")                            # minlot: потолки 50/5 и так
    assert wiring.stage_warnings() == []


def test_key_check_expires_while_running_recheck_keeps_reconcile(core, monkeypatch):
    """Бот работает дольше CHECK_TTL без перезапуска: права перепроверяются, сверка не встаёт."""
    import time
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    asked, seen = [], []

    async def ask(s, v, creds):
        asked.append(v)
        return keys.KeyCheck(True, "ok", "", True)

    monkeypatch.setattr(keys, "_ask", ask)
    monkeypatch.setattr(venues, "margin_mode", lambda *a: asyncio.sleep(0, ("isolated", "")))
    _no_reconcile(monkeypatch, seen)
    bot = Stub()
    arun(wiring.startup(bot))
    assert asked == ["bybit", "bingx"]
    clock[0] += keys.CHECK_TTL + 1
    for _ in range(5):
        arun(wiring.tick(bot))
        clock[0] += wiring.INTERVAL
    assert seen == [CREDS] * 5 and asked == ["bybit", "bingx"] * 2 and switch.enabled()


def test_startup_network_failure_then_network_back_reconcile_resumes(core, monkeypatch):
    """На старте нет сети (ПК проснулся, VPN не поднят): проверка провалилась поверх удачной; сеть вернулась — сверка
    снова идёт, а торговля (выключенная на старте) сама не включается."""
    net = {"up": False}

    async def ask(s, v, creds):
        if not net["up"]:
            return keys.KeyCheck(False, "unknown", "права не проверить: ClientConnectorError", None)
        return keys.KeyCheck(True, "ok", "", True)

    seen = []
    monkeypatch.setattr(keys, "_ask", ask)
    _no_reconcile(monkeypatch, seen)
    bot = Stub()
    arun(wiring.startup(bot))
    assert not wiring.has_keys() and not switch.enabled()
    net["up"] = True
    for _ in range(3):
        arun(wiring.tick(bot))
    assert seen == [CREDS] * 3 and wiring.has_keys() and not switch.enabled()


def test_trading_startup_runs_in_its_own_task_not_before_bot_loops():
    import inspect
    src = inspect.getsource(B.main)
    assert "await trading.wiring" not in src                              # main биржи не ждёт
    assert "ensure_future(trading.wiring.run(bot))" in src
    assert src.index("trading.wiring.run(bot)") < src.index("asyncio.gather(")


def test_run_startup_failure_turns_trading_off_and_loop_still_runs(core, monkeypatch):
    looped = []

    async def boom(bot):
        raise RuntimeError("keys")

    async def fake_loop(bot):
        looped.append(bot)

    monkeypatch.setattr(wiring, "startup", boom)
    monkeypatch.setattr(wiring, "loop", fake_loop)
    bot = Stub()
    arun(wiring.run(bot))
    assert not switch.enabled() and looped == [bot]


def test_startup_message_failure_does_not_turn_trading_off(core, monkeypatch):
    _keys_ok_at_start(monkeypatch)

    class Down(Stub):
        async def send(self, *a, **k):
            raise asyncio.TimeoutError

    text = arun(wiring.startup(Down()))
    assert switch.enabled() and "Торговое ядро" in text


def _thread(bot, text_part):
    return [p.get("message_thread_id") for m, p in bot.out if m == "sendMessage" and text_part in p["text"]]


def test_trading_messages_go_to_journal_topic_and_result_to_pressed_thread(core, monkeypatch):
    real_sleep = asyncio.sleep
    _keys_ok_at_start(monkeypatch)
    bot = Stub()
    bot.topics, bot.cur_thread = {"journal": 22, "dev": 44}, 44             # владелец последним писал в «Разработку»
    arun(wiring.startup(bot))
    journal.emit("stop_missing", "BTCUSDT", dedup="sm")
    arun(wiring.deliver(bot))
    assert _thread(bot, "Торговое ядро") == [22] and _thread(bot, "без стопа") == [22]

    async def boom(b):
        raise RuntimeError("db")

    async def stop_sleep(sec):
        raise asyncio.CancelledError

    monkeypatch.setattr(wiring, "tick", boom)
    monkeypatch.setattr(wiring.asyncio, "sleep", stop_sleep)
    with pytest.raises(asyncio.CancelledError):
        arun(wiring.loop(bot))
    assert _thread(bot, "сбой сверки") == [22]

    async def go():
        token = await wiring.request_order(bot, order(), "hedge")
        mid = bot.trading_link.tokens[token]["message_id"]
        await wiring.callback(bot, press(OWNER, "x", message_id=mid)["callback_query"], f"trd_ok:{token}", None)
        bot.cur_thread = 55                                                  # пока ордер уходил, писали в другой
        await asyncio.gather(*bot.trading_link.tasks)

    monkeypatch.setattr(wiring.asyncio, "sleep", real_sleep)
    arun(go())
    assert len(core) == 1 and _thread(bot, "Ордер: open") == [44]


def test_no_keys_trading_off_start_and_tick_create_no_files(monkeypatch, tmp_path):
    import os
    fresh_journal(monkeypatch, tmp_path)
    monkeypatch.setattr(keys, "raw_credentials", lambda v: None)
    monkeypatch.setattr(switch, "STATE_PATH", str(tmp_path / "trading_state.json"))
    monkeypatch.setenv("TRADING", "0")
    bot = Stub()
    assert arun(wiring.startup(bot)) == ""
    for _ in range(2):
        arun(wiring.tick(bot))
    assert not os.path.exists(journal.DB_PATH) and not os.path.exists(switch.STATE_PATH) and sent(bot) == []
