"""Выплаты Cryptomus, финальная проверка: «⛔ Стоп» во время перезапроса сервисов, «не найдено» при известном uuid,
база первой версии без create_kind, разделители строк в .env, поддельные кнопки мастера первого запуска."""
import ast
import asyncio
import inspect
import os
import sqlite3
from decimal import Decimal

import pytest

import bot as B
import payouts
from test_payouts import (ENTRIES, EVM, INFO, PAY, SERVICE_LIST, SERVICES, TRON2, Resp, Session,  # noqa: F401
                          _payout_env, _sent_row, echo, entry, info_for, owner, quote, rate, routes, run, svc,
                          to_preview, until, write_whitelist)


class GatedOK(Resp):
    """Обычный ответ, но только когда тест откроет ворота."""
    def __init__(self, gate, status=200, body=None):
        super().__init__(status, body)
        self.gate = gate

    async def __aenter__(self):
        await self.gate.wait()
        return self


def _update(data):
    """Кнопка владельца в его личном чате (type private, from.id == chat.id == TG_CHAT_ID)."""
    return {"update_id": 1, "callback_query": {"id": "c", "data": data, "from": {"id": 1},
                                               "message": {"message_id": 5, "chat": {"id": 1, "type": "private"}}}}


def _calls(s, since=0):
    """(метод, путь) запросов заглушки, начиная с номера since."""
    return [(c["method"], c["path"]) for c in s.calls[since:]]


def _sleeping():
    """send спит в паузе перед /info или повтором — в той, что «⛔ Стоп» прерывает сразу."""
    return bool(payouts._stop["wake"])


def _then_long_pause(answer, monkeypatch):
    """Ответ /info, после которого пауза отправки — 30 с. До /info пауза нулевая (RETRY_DELAY=0 из фикстуры), а в паузе
    перед повтором send спит, пока его не разбудит «⛔ Стоп»: Стоп приходит точно в эту паузу, без гонки с часами."""
    def reply(call):
        monkeypatch.setattr(payouts, "RETRY_DELAY", 30)
        return answer(call)
    return reply


def test_stop_during_services_recheck_sends_no_post():
    """Отправка перед POST заново берёт список сервисов. «⛔ Стоп», нажатый пока этот запрос идёт, — POST не уходит
    вовсе: выключатель проверяется ещё раз после ответа /v1/payout/services."""
    s = Session(routes({INFO: info_for({})}))
    bot = owner(s)
    token = to_preview(bot)

    async def go():
        gate = asyncio.Event()
        s.routes[SERVICES] = lambda call: GatedOK(gate, 200, {"state": 0, "result": SERVICE_LIST})
        before = len(s.calls)
        await asyncio.wait_for(bot.on_update(_update(f"pay_ok:{token}")), 5)
        await until(lambda: _calls(s, before), "send ждёт ответа /v1/payout/services")
        assert _calls(s, before) == [SERVICES] and not bot.payout_task.done()
        await bot.on_update(_update("pay_stop"))
        gate.set()
        await bot.payout_task
    run(go())
    assert s.posts() == [] and not payouts.enabled()


def test_not_found_with_known_uuid_stays_unknown():
    """Cryptomus уже выдавал uuid — «не найдено» потом не превращает выплату в rejected (иначе она выпала бы из
    лимита): остаётся unknown и одно событие notfound."""
    row = _sent_row()
    payouts._update(row["order_id"], state="unknown", create_kind="error", uuid="uuid-1")
    s = Session(routes({INFO: info_for({})}))
    events = run(payouts.poll(s))
    assert [e for e, _ in events] == ["notfound"]
    assert payouts.history()[0]["state"] == "unknown"


def test_old_database_without_create_kind_is_migrated(tmp_path):
    path = str(tmp_path / "payouts_v1.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE payouts (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT UNIQUE NOT NULL, "
                "created_ts REAL, wl_id TEXT, wl_name TEXT, currency TEXT, network TEXT, address TEXT, "
                "memo TEXT DEFAULT '', amount TEXT, fee TEXT, debit TEXT, usdt_value TEXT, state TEXT, "
                "uuid TEXT DEFAULT '', status TEXT DEFAULT '', is_final INTEGER DEFAULT 0, txid TEXT DEFAULT '', "
                "note TEXT DEFAULT '', updated_ts REAL)")
    con.execute("INSERT INTO payouts (order_id, state) VALUES ('old-1', 'unknown')")
    con.commit()
    con.close()
    con = payouts._connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(payouts)")}
    assert "create_kind" in cols
    assert con.execute("SELECT create_kind FROM payouts WHERE order_id='old-1'").fetchone()[0] == ""
    con.close()
    payouts._connect(path).close()   # повторное открытие — без ошибки «duplicate column»


@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\r", "\n"])
def test_save_env_rejects_every_line_separator(tmp_path, sep):
    path = str(tmp_path / ".env")
    with open(path, "w", encoding="utf-8") as f:
        f.write("PAYOUTS=0\n")
    with pytest.raises(ValueError):
        B.save_env("INCLUDE_PAY", f"x{sep}payouts=1", path)
    B.save_env("INCLUDE_PAY", "t-bank", path)                       # обычное значение пишется как раньше
    assert open(path, encoding="utf-8").read().splitlines() == ["PAYOUTS=0", "INCLUDE_PAY=t-bank"]


def _repo(tmp_path, monkeypatch, guard):
    """Настоящий git во временной папке: база main с payouts.py, тестами выплат и pytest.ini."""
    monkeypatch.chdir(tmp_path)
    g = lambda *a: guard.git("-c", "user.name=t", "-c", "user.email=t@t", *a)   # noqa: E731
    g("init", "-q", "-b", "main")
    (tmp_path / "tests").mkdir()
    (tmp_path / "payouts.py").write_text("DEFAULT_LIMIT = '2000'\nMAX_RESEND = 2\n", encoding="utf-8")
    (tmp_path / "tests" / "test_payouts.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-b", "claude/x")
    return g


def test_guard_sees_renamed_payout_code_and_pytest_config(tmp_path, monkeypatch):
    """Переименование не снимает защиту: «payouts.py → payouts/__init__.py» с правкой лимита и тесты выплат,
    переименованные в .txt, — ручная проверка; как и addopts в pytest.ini, которым тесты выплат выпали бы из CI."""
    from test_payouts import _load_guard
    guard = _load_guard()
    g = _repo(tmp_path, monkeypatch, guard)
    (tmp_path / "payouts").mkdir()
    g("mv", "payouts.py", "payouts/__init__.py")
    (tmp_path / "payouts" / "__init__.py").write_text("DEFAULT_LIMIT = '1000000'\nMAX_RESEND = 2\n", encoding="utf-8")
    g("mv", "tests/test_payouts.py", "tests/legacy.txt")
    g("add", "-A")
    g("commit", "-q", "-m", "refactor")
    found = guard.check("main")
    assert any("payouts.py" in p for p in found) and any("payouts/__init__.py" in p for p in found)
    assert any("tests/test_payouts.py" in p for p in found)
    g("checkout", "-q", "main")
    g("checkout", "-q", "-b", "claude/speedup")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\naddopts = --ignore-glob=*pay*\n",
                                         encoding="utf-8")
    g("commit", "-q", "-am", "speed")
    assert any("pytest.ini" in p for p in guard.check("main"))


@pytest.mark.parametrize("path, protected", [
    ("tests/sub/conftest.py", True), ("conftest.py", True), ("pkg/pytest.ini", True), ("x/pyproject.toml", True),
    ("a/b/setup.cfg", True), ("tools/tox.ini", True), ("sitecustomize.py", True), ("lib/usercustomize.py", True),
    ("evil.pth", True), ("site/Evil.PTH", True), ("requirements.txt", True), ("tools/requirements.txt", True),
    ("tests/Conftest.py", True), ("run.bat", True), ("tests/test_launcher_money_gate.py", True),
    ("tests/payout_stubs.py", True), ("tests/test_payout_pins.py", True),
    # регистр не важен: на Windows LAUNCHER.PY из коммита записался бы поверх launcher.py
    ("LAUNCHER.PY", True), (".GitHub/workflows/ci.yml", True), ("Scripts/Guard.py", True), ("claude.md", True),
    ("RUN.BAT", True), (".GITIGNORE", True),
    # как в launcher: торговый код, локальное состояние, которое merge перезаписал бы, .gitattributes
    (".gitattributes", True), ("trading/venues.py", True), ("tests/trading/test_risk.py", True),
    ("paper_Trading.py", True), ("data/keys.json", True), ("logs/approved_shas", True), (".env", True),
    (".last_good", True), (".dev_status.json", True),
    # исполняемое мимо .py (git.exe в папке бота launcher вызвал бы вместо git), кэш байткода, подмена stdlib в корне
    ("git.exe", True), ("tools/Evil.DLL", True), ("x.pyd", True), ("__pycache__/bot.cpython-310.pyc", True),
    ("lib/x.so", True), ("update.bat", True), ("x.CMD", True), ("tools/x.ps1", True), ("json.py", True),
    ("Hashlib.py", True), ("hashlib/__init__.py", True), ("email/x.py", True),
    ("scripts/json.py", False), ("tests/test_json.py", False), ("docs/token.md", False),
    ("tests/fixtures/bybit_ads.json", False),
    ("bot.py", False), ("tests/test_bot.py", False), ("tests/helpers.py", False), ("requirements-dev.md", False),
    ("docs/conftest.md", False), ("pthelper.py", False), (".env.example", False), ("docs/data/x.md", False),
    ("trades.py", False)])
def test_guard_protects_pytest_config_and_python_startup_files_at_any_depth(monkeypatch, path, protected):
    """conftest.py/pytest.ini во вложенной папке, sitecustomize/usercustomize и *.pth (Python выполняет их сам при
    старте), requirements.txt — на любой глубине только вручную: через них тесты выплат выпадали бы или подменялись."""
    from test_payouts import _load_guard
    guard = _load_guard()
    monkeypatch.setattr(guard, "git", lambda *args: path + "\0" if "--name-only" in args else "")
    assert bool(guard.check("origin/main")) is protected
    assert guard.protected(path) is protected


def test_guard_reads_code_in_fixtures_folder(tmp_path, monkeypatch):
    """tests/fixtures/ пропускается только для .json (данные): .py там — такой же код, строки выплат в нём — ручная
    проверка, иначе через «фикстуру» можно было бы подложить код выплат мимо guard."""
    from test_payouts import _load_guard
    guard = _load_guard()
    g = _repo(tmp_path, monkeypatch, guard)
    (tmp_path / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "fixtures" / "ads.json").write_text('{"note": "payout 100"}\n', encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "fixture")
    assert guard.check("main") == []
    (tmp_path / "tests" / "fixtures" / "Helper.py").write_text("PAYOUT_MAX_ONE = '1000000'\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "helper")
    assert any("Helper.py" in p and "код выплат" in p for p in guard.check("main"))


def _answers(bot):
    return [p.get("text", "") for m, p in bot.out if m == "answerCallbackQuery"]


def test_payout_handlers_refuse_non_owner_even_past_routing():
    """«Только владелец» проверяют сами запиненные обработчики выплат, а не только маршрутизация (on_update,
    dispatch, handle_guest — не запинены): гость, дошедший до них в обход, получает отказ, ничего не уходит,
    одноразовая кнопка владельца остаётся рабочей."""
    bot = owner()
    token = to_preview(bot)
    posts = len(bot.s.posts())
    for data in ("pay_to:w1", f"pay_ok:{token}", "pay_no:" + token, "pay_stop", "pay_hist"):   # мимо on_update
        run(bot.on_callback({"id": "g", "data": data, "message": {"message_id": 3, "chat": {"id": 42}}}))
    run(bot.payout_callback({"id": "g", "data": "pay_ok:" + token}, "pay_ok:" + token))   # без message — чей, неясно
    assert _answers(bot)[-6:] == ["Только для владельца бота"] * 6
    assert payouts.enabled() and bot.payout_preview["token"] == token and len(bot.s.posts()) == posts
    guest = B.REPLY_CHAT.set("42")                                  # команда гостя, дошедшая до обработчиков
    try:
        out = len(bot.out)
        run(bot.cmd_payout(""))
        run(bot.cmd_payout("history"))
        run(bot.payout_amount("w1", "25"))
        run(bot.payout_callback(_update("pay_hist")["callback_query"], "pay_hist"))
    finally:
        B.REPLY_CHAT.reset(guest)
    new = bot.out[out:]
    assert [p["text"] for m, p in new if m == "sendMessage"] == [B.GUEST_DENIED] * 2
    assert [p.get("text") for m, p in new if m == "answerCallbackQuery"] == ["Только для владельца бота"]
    assert bot.payout_preview["token"] == token and len(bot.s.posts()) == posts
    async def owner_press():
        await bot.on_callback(_update(f"pay_ok:{token}")["callback_query"])
        await bot.payout_task
    run(owner_press())
    assert len(bot.s.posts()) == posts + 1                         # владелец отправляет как обычно


def test_guard_reads_non_ascii_and_spaced_paths(tmp_path, monkeypatch):
    """Пути с русскими буквами и пробелами: без -z и core.quotepath=false git берёт их в кавычки с октальными кодами,
    а «+++ b/путь с пробелом» кончается на \\t — защита по префиксу и проверка FORBIDDEN в .py их пропускали."""
    from test_payouts import _load_guard
    guard = _load_guard()
    g = _repo(tmp_path, monkeypatch, guard)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "проверка.yml").write_text("on: push\n", encoding="utf-8")
    (tmp_path / "my tool.py").write_text("import subprocess\n", encoding="utf-8")
    (tmp_path / "заметки.md").write_text("subprocess — это просто слово в тексте\n", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "paths")
    found = guard.check("main")
    assert any("изменён защищённый файл: .github/workflows/проверка.yml" == p for p in found), found
    assert any(p.startswith("my tool.py: запрещено") for p in found), found
    assert not any("заметки.md" in p for p in found)


def test_forged_onboarding_bank_callback_is_ignored():
    bot = owner()
    bot.onboarding = {"step": "banks", "banks": set()}
    for name in ("x payouts=1", "x\x85payouts=1", "Evil Bank", ""):
        run(bot.on_callback({"id": "c", "data": f"onb_bank:{name}",
                             "message": {"message_id": 5, "chat": {"id": 1}}, "from": {"id": 1}}))
    assert bot.onboarding["banks"] == set()
    run(bot.on_callback({"id": "c", "data": f"onb_bank:{B.ONBOARD_BANKS[0]}",
                         "message": {"message_id": 5, "chat": {"id": 1}}, "from": {"id": 1}}))
    assert bot.onboarding["banks"] == {B.ONBOARD_BANKS[0]}           # настоящая кнопка работает


# --- ревью 2026-09-27: «⛔ Стоп» в паузе повтора, владелец — личный чат, свежий курс перед отправкой ---

RATE_BTC = ("GET", "/v1/exchange-rate/BTC/list")


def _msg(chat, text, uid=None, ctype="private"):
    """Сообщение Telegram: чат с типом и отправитель (в личном чате id отправителя = id чата)."""
    return {"message": {"message_id": 9, "chat": {"id": chat, "type": ctype}, "text": text,
                        "from": {"id": chat if uid is None else uid, "first_name": "Вася"}}}


def _cb(chat, data, uid=None, ctype="private"):
    """Нажатие кнопки: from — кто нажал, message.chat — где сообщение с кнопкой."""
    return {"callback_query": {"id": "c", "data": data, "from": {"id": chat if uid is None else uid},
                               "message": {"message_id": 5, "chat": {"id": chat, "type": ctype}}}}


def _texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def test_stop_during_resend_delay_sends_no_second_post(monkeypatch):
    """Неясный исход → /info «не найдено» → пауза перед повтором. «⛔ Стоп» в этой паузе — повтор не уходит:
    выключатель проверяется сразу перед каждым POST, после каждого await (раньше — только до паузы)."""
    s = Session(routes({PAY: [Resp(exc=asyncio.TimeoutError()), echo()],
                        INFO: _then_long_pause(info_for({}), monkeypatch)}))
    q = quote(s)

    async def go():
        task = asyncio.create_task(payouts.send(s, entry("w1"), Decimal("25"), q))
        await until(lambda: s.posts("/v1/payout/info") and _sleeping(), "send спит в паузе перед повтором")
        payouts.disable()
        return await asyncio.wait_for(task, 5)
    res = run(go())
    assert len(s.posts()) == 1                                      # без исправления — 2 (повтор после Стопа)
    assert res["state"] == "unknown" and "повтор не отправлен" in res["reason"]
    assert payouts.used_today() == q["usdt"]                        # исход неясен — в лимите


def test_stop_is_sticky_for_inflight_send_even_if_switch_flips_back(monkeypatch):
    """Стоп во время паузы, а потом PAYOUTS снова 1 (чем угодно в процессе) — эта отправка всё равно больше ничего не
    шлёт: кроме выключателя проверяется счётчик Стопа. Следующая выплата — уже по новому подтверждению — идёт."""
    s = Session(routes({PAY: [Resp(exc=asyncio.TimeoutError()), echo()],
                        INFO: _then_long_pause(info_for({}), monkeypatch)}))
    q = quote(s)

    async def go():
        task = asyncio.create_task(payouts.send(s, entry("w1"), Decimal("25"), q))
        await until(lambda: s.posts("/v1/payout/info") and _sleeping(), "send спит в паузе перед повтором")
        payouts.disable()
        monkeypatch.setenv("PAYOUTS", "1")
        return await asyncio.wait_for(task, 5)
    res = run(go())
    assert len(s.posts()) == 1 and res["state"] == "unknown" and "повтор не отправлен" in res["reason"]
    assert run(payouts.send(s, entry("w1"), Decimal("25"), quote(s)))["state"] == "sent"


def test_stop_interrupts_sleeping_resend_at_once(monkeypatch):
    """Пауза перед /info и повтором длинная (RETRY_DELAY × попытка) — «⛔ Стоп» будит её сразу: отправка кончается
    за доли секунды, без /info и без повтора; исход выяснит опрос (poll)."""
    monkeypatch.setattr(payouts, "RETRY_DELAY", 30)
    s = Session(routes({PAY: [Resp(exc=asyncio.TimeoutError()), echo()], INFO: info_for({})}))
    q = quote(s)

    async def go():
        task = asyncio.create_task(payouts.send(s, entry("w1"), Decimal("25"), q))
        await until(lambda: s.posts() and _sleeping(), "send спит в паузе перед /info")
        payouts.disable()
        return await asyncio.wait_for(task, 5)                      # без пробуждения — 30 с и таймаут теста
    res = run(go())
    assert len(s.posts()) == 1 and not s.posts("/v1/payout/info")
    assert res["state"] == "unknown" and "повтор не отправлен" in res["reason"]
    assert payouts.history()[0]["state"] == "unknown"


def test_stop_button_interrupts_background_resend(monkeypatch):
    """Через бота: «✅ Отправить» ушла фоном, первый POST — таймаут, отправка спит перед разбором; «⛔ Стоп» из
    command_loop будит её — ровно один POST, владельцу — «исход неясен»."""
    monkeypatch.setattr(payouts, "RETRY_DELAY", 30)
    s = Session(routes({PAY: [Resp(exc=asyncio.TimeoutError()), echo()], INFO: info_for({})}))
    bot = owner(s)
    token = to_preview(bot)

    async def go():
        await asyncio.wait_for(bot.on_update(_cb(1, f"pay_ok:{token}")), 5)
        await until(lambda: s.posts() and _sleeping(), "фоновая отправка спит в паузе перед /info")
        await bot.on_update(_cb(1, "pay_stop"))
        await asyncio.wait_for(bot.payout_task, 5)
    run(go())
    assert len(s.posts()) == 1 and not payouts.enabled()
    assert "неясен" in _texts(bot)[-1]


@pytest.mark.parametrize("ctype,chat,uid", [("group", -5, 7), ("supergroup", -1001, 7), ("channel", -1002, 7),
                                            ("private", 7, 8)])
def test_new_install_cannot_bind_owner_from_any_chat(monkeypatch, caplog, ctype, chat, uid):
    """TG_CHAT_ID пуст: ни группа, ни личный чат не назначают владельца. Настройка ID — только на ПК."""
    saved = []
    monkeypatch.setattr(B, "save_env", lambda k, v, path=None: saved.append((k, v)))
    bot = owner()
    bot.chat_id = ""
    run(bot.on_update(_msg(chat, "/start", uid=uid, ctype=ctype)))
    run(bot.on_update({"channel_post": {"message_id": 1, "chat": {"id": chat, "type": "channel"}, "text": "/start"}}))
    assert bot.chat_id == "" and saved == [] and bot.onboarding is None
    assert "TG_CHAT_ID" in caplog.text
    run(bot.on_update(_msg(7, "/start")))                           # личный чат — мастер первого запуска
    assert bot.chat_id == "" and saved == []
    assert bot.onboarding is None


@pytest.mark.parametrize("ctype", ["group", "supergroup", "channel"])
def test_group_as_tg_chat_id_gets_no_owner_actions(monkeypatch, ctype):
    """TG_CHAT_ID — группа (или канал): ни один её участник, включая самого владельца, не получает команд и кнопок
    владельца — настроек, ключей, выплат, «⛔ Стоп»; на команду — понятный отказ, на кнопку — всплывающий отказ."""
    saved = []
    monkeypatch.setattr(B, "save_env", lambda k, v, path=None: saved.append((k, v)))
    bot = owner()
    bot.chat_id = "-1001"
    for uid in (5, 6):
        for text in ("/payout", "/settings", "⚙️ Настройки", "/status", "/allow 42"):
            run(bot.on_update(_msg(-1001, text, uid=uid, ctype=ctype)))
    for data in ("pay_to:w1", "pay_ok:x", "pay_stop", "pay_hist", "settings", "acc_add:bybit", "paper_set:on"):
        run(bot.on_update(_cb(-1001, data, uid=5, ctype=ctype)))
    got = _texts(bot)
    assert len(got) == 1 and "личном чате" in got[0] and "TG_CHAT_ID" in got[0]   # отказ — раз в 10 минут на чат
    assert _answers(bot) and all("личном чате" in t for t in _answers(bot))
    assert saved == [] and payouts.enabled() and bot.awaiting_payout is None and bot.awaiting_key is None
    assert bot.guests == set() and not bot.s.calls


def test_callback_or_message_from_another_user_in_owner_chat_refused():
    """Кнопку нажал не владелец (callback.from.id ≠ TG_CHAT_ID), хотя сообщение с кнопкой — в чате владельца: отказ и
    в маршрутизации, и в самом обработчике выплат. Владелец после этого отправляет как обычно."""
    bot = owner()
    token = to_preview(bot)
    for data in (f"pay_ok:{token}", "pay_stop", "paper_set:on", "settings"):
        run(bot.on_update(_cb(1, data, uid=2)))
    run(bot.payout_callback(_cb(1, f"pay_ok:{token}", uid=2)["callback_query"], f"pay_ok:{token}"))
    run(bot.on_update(_msg(1, "/payout", uid=2)))
    assert not bot.s.posts() and payouts.enabled() and bot.payout_preview["token"] == token
    assert len(_answers(bot)) >= 5 and all("владельц" in t for t in _answers(bot)[-5:])

    async def owner_press():
        await bot.on_update(_cb(1, f"pay_ok:{token}"))
        await bot.payout_task
    run(owner_press())
    assert len(bot.s.posts()) == 1


def test_owner_private_chat_keeps_working_end_to_end():
    """Владелец в личном чате (type private, from.id == chat.id == TG_CHAT_ID): /payout → получатель → сумма →
    «✅ Отправить» → ровно один POST; «⛔ Стоп» выключает выплаты."""
    s = Session(routes())
    bot = owner(s)

    async def go():
        await bot.on_update(_msg(1, "/payout"))
        await bot.on_update(_cb(1, "pay_to:w1"))
        await bot.on_update(_msg(1, "25"))
        token = bot.payout_preview["token"]
        await bot.on_update(_cb(1, f"pay_ok:{token}"))
        await bot.payout_task
        await bot.on_update(_cb(1, "pay_stop"))
    run(go())
    assert len(s.posts()) == 1 and "Cryptomus принял выплату 25 USDT" in " ".join(_texts(bot))
    assert not payouts.enabled()


def test_owner_pressing_in_another_group_or_unsigned_press_is_not_owner():
    """Сам владелец (from.id == TG_CHAT_ID) жмёт кнопку в чужой группе — это не его личный чат: ничего. Нажатие в чате
    владельца без from (кто нажал — неизвестно) или с чатом без типа — отказ."""
    bot = owner()
    token = to_preview(bot)
    run(bot.on_update(_cb(-5, "pay_stop", uid=1, ctype="supergroup")))
    unsigned = _cb(1, f"pay_ok:{token}")
    del unsigned["callback_query"]["from"]
    untyped = _cb(1, "pay_stop")
    del untyped["callback_query"]["message"]["chat"]["type"]
    for u in (unsigned, untyped):
        run(bot.on_update(u))
        run(bot.payout_callback(u["callback_query"], u["callback_query"]["data"]))
    assert payouts.enabled() and not bot.s.posts() and bot.payout_preview["token"] == token


def test_stop_while_send_waits_for_lock_blocks_post_even_if_switch_flips_back(monkeypatch):
    """«✅ Отправить» ждёт замок (идёт опрос или другая отправка), тут «⛔ Стоп», а потом PAYOUTS снова 1 — эта отправка
    всё равно отказ: счётчик Стопа берётся до замка."""
    s = Session(routes())
    q = quote(s)

    async def go():
        lock = payouts._send_lock()
        await lock.acquire()
        task = asyncio.create_task(payouts.send(s, entry("w1"), Decimal("25"), q))
        await until(lambda: lock._waiters, "send ждёт замок")        # очередь ждущих asyncio.Lock
        payouts.disable()
        monkeypatch.setenv("PAYOUTS", "1")
        lock.release()
        return await asyncio.wait_for(task, 5)
    res = run(go())
    assert res["state"] == "refused" and not s.posts() and payouts.history() == []


def test_send_refreshes_rate_and_refuses_over_limit_at_new_price():
    """Предпросмотр по старому курсу: 0.03 BTC + 0.0001 комиссии ≈ 1806 USDT (лимит 2000). К отправке BTC подорожал —
    та же выплата уже ≈2107 USDT: отказ до POST с новой оценкой и просьбой подтвердить заново."""
    s = Session(routes())
    q = quote(s, "w2", "0.03")
    assert q["usdt"] == Decimal("1806.00")
    s.routes[RATE_BTC] = rate("BTC", "70000")
    res = run(payouts.send(s, entry("w2"), Decimal("0.03"), q))
    assert res["state"] == "refused" and "2107" in res["reason"] and "/payout" in res["reason"]
    assert not s.posts() and payouts.history() == []


def test_send_refuses_rate_drift_and_allows_small_drift(monkeypatch):
    """Курс сдвинулся больше PAYOUT_RATE_DRIFT (по умолчанию 2 %) — отказ, даже в пределах лимитов; небольшой сдвиг —
    отправка, в журнал и лимит — бо́льшая из оценок (предпросмотр или свежая)."""
    s = Session(routes())
    q = quote(s, "w2", "0.01")                                      # 0.0101 × 60000 = 606.00
    s.routes[RATE_BTC] = rate("BTC", "63000")                       # +5 %
    res = run(payouts.send(s, entry("w2"), Decimal("0.01"), q))
    assert res["state"] == "refused" and "курс" in res["reason"] and "/payout" in res["reason"] and not s.posts()
    s.routes[RATE_BTC] = rate("BTC", "60600")                       # +1 %
    res = run(payouts.send(s, entry("w2"), Decimal("0.01"), q))
    assert res["state"] == "sent" and res["row"]["usdt_value"] == "612.06" and len(s.posts()) == 1
    s.routes[RATE_BTC] = rate("BTC", "59400")                       # −1 %: в лимит — оценка предпросмотра
    res = run(payouts.send(s, entry("w2"), Decimal("0.01"), q))
    assert res["state"] == "sent" and res["row"]["usdt_value"] == "606.00"
    monkeypatch.setenv("PAYOUT_RATE_DRIFT", "10")
    s.routes[RATE_BTC] = rate("BTC", "63000")
    assert run(payouts.send(s, entry("w2"), Decimal("0.01"), q))["state"] == "sent"


def test_rate_drift_garbage_fails_closed_and_is_documented(monkeypatch):
    s = Session(routes())
    q = quote(s, "w2", "0.01")
    monkeypatch.setenv("PAYOUT_RATE_DRIFT", "много")                # мусор — допуск 0: любой сдвиг курса — отказ
    s.routes[RATE_BTC] = rate("BTC", "60001")
    assert run(payouts.send(s, entry("w2"), Decimal("0.01"), q))["state"] == "refused"
    s.routes[RATE_BTC] = rate("BTC", "60000")
    assert run(payouts.send(s, entry("w2"), Decimal("0.01"), q))["state"] == "sent"
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.example"), encoding="utf-8") as f:
        assert "PAYOUT_RATE_DRIFT=2" in f.read().splitlines()


def test_send_refuses_when_rate_unavailable_at_send():
    s = Session(routes())
    q = quote(s, "w2", "0.01")
    s.routes[RATE_BTC] = Resp(500, {"message": "Server error"})
    res = run(payouts.send(s, entry("w2"), Decimal("0.01"), q))
    assert res["state"] == "refused" and "нет курса" in res["reason"] and not s.posts()


@pytest.mark.parametrize("change", ["stop", "whitelist"])
def test_everything_rechecked_after_rate_refresh(change):
    """Свежий курс — ещё один await перед POST: «⛔ Стоп» или правка белого списка, пока он идёт, — POST не уходит."""
    s = Session(routes())
    q = quote(s, "w2", "0.01")
    body = {"state": 0, "result": [{"from": "BTC", "to": "USDT", "course": "60000"}]}

    async def go():
        gate = asyncio.Event()
        s.routes[RATE_BTC] = lambda call: GatedOK(gate, 200, body)
        before = len(s.calls)
        task = asyncio.create_task(payouts.send(s, entry("w2"), Decimal("0.01"), q))
        await until(lambda: RATE_BTC in _calls(s, before), "send ждёт свежий курс")
        if change == "stop":
            payouts.disable()
        else:
            write_whitelist([dict(x, name="Другой BTC") if x["id"] == "w2" else x for x in ENTRIES])
        gate.set()
        return await asyncio.wait_for(task, 5)
    res = run(go())
    assert res["state"] == "refused" and not s.posts() and payouts.history() == []


# --- ревью 2026-09-27, второй круг: абсурдный курс, Стоп во время запроса сервисов ---

@pytest.mark.parametrize("course", ["1e30", "1e1000000", "1e-1000000"])
def test_absurd_fresh_rate_is_a_refusal_not_a_crash(course):
    """Предпросмотр 0.01 BTC по 60000, к отправке Cryptomus отдаёт абсурдный курс: оценка в USDT не помещается в
    Decimal (quantize раньше бросал InvalidOperation из send, и бот писал «сбой, исход неясен, учтена в лимите»). Теперь
    это отказ «курс неправдоподобен» (полоса PAYOUT_RATE_BANDS) с короткой причиной: POST не уходит, в журнале и в
    лимите ничего."""
    s = Session(routes())
    q = quote(s, "w2", "0.01")
    s.routes[RATE_BTC] = rate("BTC", course)
    res = run(payouts.send(s, entry("w2"), Decimal("0.01"), q))
    assert res["state"] == "refused" and "курс BTC к USDT неправдоподобен" in res["reason"]
    assert len(res["reason"]) < 400                                 # не тысячи нулей в сообщении владельцу
    assert not s.posts() and payouts.history() == [] and payouts.used_today() == 0


def test_absurd_rate_in_preview_is_a_refusal_not_a_crash():
    s = Session(routes({RATE_BTC: rate("BTC", "1e30")}))
    q, why = run(payouts.quote(s, entry("w2"), Decimal("0.01")))
    assert q is None and "неправдоподобен" in why and "1.000E+30" in why
    s.routes[RATE_BTC] = rate("BTC", "60000")                       # нормальный курс — предпросмотр как раньше
    assert run(payouts.quote(s, entry("w2"), Decimal("0.01")))[0]["usdt"] == Decimal("606.00")


def test_absurd_fresh_rate_tells_owner_not_sent_instead_of_unclear():
    """Через бота: владелец видит «⛔ Выплата не отправлена», а не ложное «сбой, исход неясен, учтена в лимите»."""
    s = Session(routes())
    bot = owner(s)
    token = to_preview(bot, "0.01", "w2")
    s.routes[RATE_BTC] = rate("BTC", "1e30")

    async def go():
        await bot.on_update(_cb(1, f"pay_ok:{token}"))
        await bot.payout_task
    run(go())
    assert _texts(bot)[-1].startswith("⛔ Выплата не отправлена") and "курс" in _texts(bot)[-1]
    assert not any("Сбой" in t for t in _texts(bot)) and not s.posts() and payouts.history() == []


def test_stop_during_services_request_starts_no_rate_request():
    """«⛔ Стоп», пока идёт POST /v1/payout/services, — после его ответа send не начинает ни одного запроса: ни GET
    курса, ни тем более POST создания (выключатель и счётчик Стопа — после каждого await, как обещает докстринг)."""
    s = Session(routes())
    q = quote(s, "w2", "0.01")

    async def go():
        gate = asyncio.Event()
        s.routes[SERVICES] = lambda call: GatedOK(gate, 200, {"state": 0, "result": SERVICE_LIST})
        before = len(s.calls)
        task = asyncio.create_task(payouts.send(s, entry("w2"), Decimal("0.01"), q))
        await until(lambda: _calls(s, before), "send начал запрос сервисов")
        assert _calls(s, before) == [SERVICES]                     # send ждёт ответа сервисов
        payouts.disable()
        gate.set()
        return await asyncio.wait_for(task, 5), s.calls[before:]
    res, calls = run(go())
    assert res["state"] == "refused" and res["reason"] == payouts.OFF
    assert [(c["method"], c["path"]) for c in calls] == [SERVICES]  # без исправления — ещё GET курса BTC
    assert payouts.history() == []


# --- полоса правдоподобного курса к USDT (PAYOUT_RATE_BANDS): крошечный курс не выключает лимиты ---

IMPLAUSIBLE = "к USDT неправдоподобен"
USDC_ENTRY = {"id": "w5", "name": "USDC в BSC", "currency": "USDC", "network": "bsc", "address": EVM}


def test_rate_bands_cover_every_whitelist_coin_and_live_only_in_code():
    """Полоса есть у каждой монеты белого списка (GRAM — код TON у Cryptomus — та же, что у TON). Таблица — одна
    константа из литералов Decimal("…"), связана один раз и не читается из .env: подменённый .env её не расширит."""
    bands = payouts.PAYOUT_RATE_BANDS
    assert set(bands) == set(payouts.COINS) | {"GRAM"} and bands["GRAM"] == bands["TON"]
    assert bands["USDT"] == bands["USDC"] == (Decimal("0.95"), Decimal("1.05"))
    assert bands["BTC"] == (Decimal(1000), Decimal(10000000)) and bands["ETH"] == (Decimal(10), Decimal(1000000))
    assert bands["TON"] == (Decimal("0.01"), Decimal(1000))
    assert all(isinstance(lo, Decimal) and isinstance(hi, Decimal) and 0 < lo < hi for lo, hi in bands.values())
    tree = ast.parse(inspect.getsource(payouts))
    binds = [n for n in ast.walk(tree) if isinstance(n, (ast.Name, ast.Attribute, ast.Subscript))
             and isinstance(n.ctx, (ast.Store, ast.Del))
             and "PAYOUT_RATE_BANDS" in ast.unparse(n)]
    assert len(binds) == 1                                          # только само присваивание таблицы
    table = next(n for n in tree.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "PAYOUT_RATE_BANDS")
    calls = [c for c in ast.walk(table.value) if isinstance(c, ast.Call)]
    assert len(calls) == 12 and all(ast.unparse(c.func) == "Decimal" and not c.keywords and len(c.args) == 1
                                    and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)
                                    for c in calls)
    check = inspect.getsource(payouts._rate_band_error)
    assert "getenv" not in check and "environ" not in check


@pytest.mark.parametrize("coin,course,ok", [
    ("USDT", "1", True), ("USDT", "0.95", True), ("USDC", "1.05", True), ("USDT", "0.9", False),
    ("USDC", "0.9499", False), ("USDT", "1.0501", False),
    ("BTC", "1000", True), ("BTC", "10000000", True), ("BTC", "999.99", False), ("BTC", "10000000.01", False),
    ("ETH", "10", True), ("ETH", "1000000", True), ("ETH", "9.99", False), ("ETH", "1000000.01", False),
    ("TON", "0.01", True), ("TON", "1000", True), ("GRAM", "5", True), ("TON", "0.0099", False),
    ("GRAM", "1000.01", False),
    ("BTC", "1e-30", False), ("BTC", "1e30", False), ("TON", "1e-1000000", False), ("ETH", "0", False),
    ("DOGE", "1", False), ("", "1", False)])
def test_rate_band_edges_are_inclusive_and_unknown_coin_is_refused(coin, course, ok):
    why = payouts._rate_band_error(coin, Decimal(course))
    assert (why is None) is ok, why
    if not ok and coin in payouts.PAYOUT_RATE_BANDS:
        assert why == (f"курс {coin} к USDT неправдоподобен ({payouts._rate_text(Decimal(course))}) — "
                       f"лимит не проверить, выплату не отправляю")
    if not ok and coin not in payouts.PAYOUT_RATE_BANDS:
        assert "нет полосы" in why and "выплату не отправляю" in why


def test_rate_band_refuses_non_numbers_without_crashing():
    for bad in (None, Decimal("NaN"), Decimal("Infinity"), Decimal("-60000"), 60000.0, "60000"):
        assert IMPLAUSIBLE in payouts._rate_band_error("BTC", bad), bad


@pytest.mark.parametrize("course", ["1e-30", "0.000001", "999", "10000001", "1e30"])
def test_implausible_rate_in_preview_is_refused(course):
    """Предпросмотр по курсу вне полосы BTC [1000, 10 000 000] — отказ с курсом в причине: ни кнопки, ни журнала.
    До исправления 1e-30 давал оценку 0.01 USDT за 0.01 BTC, и лимиты в 2000 USDT её пропускали."""
    s = Session(routes({RATE_BTC: rate("BTC", course)}))
    q, why = run(payouts.quote(s, entry("w2"), Decimal("0.01")))
    shown = payouts._rate_text(Decimal(course))
    assert q is None and why == f"курс BTC к USDT неправдоподобен ({shown}) — лимит не проверить, выплату не отправляю"
    assert not s.posts() and payouts.history() == [] and payouts.used_today() == 0


@pytest.mark.parametrize("course", ["1e-30", "999", "10000001", "1e30"])
def test_implausible_rate_only_at_send_refused_before_post_and_counts_nothing(course):
    """Предпросмотр 0.03 BTC по 60000 (≈1806 USDT из 2000), к отправке курс вне полосы — отказ до POST. Ничего не
    засчитано: следующая выплата по нормальному курсу проходит на ту же сумму почти во весь дневной лимит."""
    s = Session(routes())
    q = quote(s, "w2", "0.03")
    s.routes[RATE_BTC] = rate("BTC", course)
    res = run(payouts.send(s, entry("w2"), Decimal("0.03"), q))
    assert res["state"] == "refused" and res["reason"].startswith(f"курс BTC {IMPLAUSIBLE} (")
    assert not s.posts() and payouts.history() == [] and payouts.used_today() == 0
    s.routes[RATE_BTC] = rate("BTC", "60000")
    res = run(payouts.send(s, entry("w2"), Decimal("0.03"), q))
    assert res["state"] == "sent" and len(s.posts()) == 1 and payouts.used_today() == Decimal("1806.00")


def test_same_tiny_rate_at_preview_and_send_no_longer_disables_limits():
    """Сама дыра: Cryptomus отдаёт 1e-30 и в предпросмотре, и перед отправкой. Сдвига курса нет, 1 BTC ≈ 0.01 USDT —
    лимиты 2000 USDT не держат. Теперь отказ и в quote, и в send — даже с оценкой, сделанной до исправления."""
    s = Session(routes({RATE_BTC: rate("BTC", "1e-30")}))
    q, why = run(payouts.quote(s, entry("w2"), Decimal("1")))
    assert q is None and IMPLAUSIBLE in why
    stale = {"fee": Decimal("0.0001"), "debit": Decimal("1.0001"), "rate": Decimal("1e-30"), "usdt": Decimal("0.01")}
    res = run(payouts.send(s, entry("w2"), Decimal("1"), stale))
    assert res["state"] == "refused" and IMPLAUSIBLE in res["reason"]
    assert not s.posts() and payouts.history() == [] and payouts.used_today() == 0


@pytest.mark.parametrize("course", ["0.9", "1.06"])
def test_stable_coin_off_peg_rate_is_refused(monkeypatch, course):
    """USDT/USDC к USDT — 1:1 (±5 %). Курс стейблкоина вне [0.95, 1.05] — отказ и в предпросмотре, и перед отправкой."""
    s = Session(routes())
    q = quote(s, "w1", "25")

    async def off_peg(session, coin):
        return Decimal(course)
    monkeypatch.setattr(payouts, "usdt_rate", off_peg)
    q2, why = run(payouts.quote(s, entry("w1"), Decimal("25")))
    assert q2 is None and why == f"курс USDT {IMPLAUSIBLE} ({course}) — лимит не проверить, выплату не отправляю"
    res = run(payouts.send(s, entry("w1"), Decimal("25"), q))
    assert res["state"] == "refused" and res["reason"] == why
    assert not s.posts() and payouts.history() == [] and payouts.used_today() == 0


@pytest.mark.parametrize("eid,amount,course", [
    ("w1", "25", None), ("w5", "25", None),
    ("w2", "0.001", "1000"), ("w2", "0.001", "60000"), ("w2", "0.0001", "10000000"),
    ("w4", "0.001", "10"), ("w4", "0.001", "3000"), ("w4", "0.001", "1000000"),
    ("w3", "1.5", "0.01"), ("w3", "1.5", "5"), ("w3", "1.5", "1000")])
def test_in_band_rates_pass_for_every_coin(monkeypatch, eid, amount, course):
    """Курс в полосе (включая границы) — предпросмотр и отправка как раньше, для каждой монеты белого списка."""
    monkeypatch.setenv("PAYOUT_MAX_ONE", "100000")
    monkeypatch.setenv("PAYOUT_DAILY_LIMIT", "100000")
    write_whitelist(ENTRIES + [USDC_ENTRY])
    over = {SERVICES: Resp(200, {"state": 0, "result": SERVICE_LIST + [svc("USDC", "BSC")]})}
    cur = entry(eid)["currency"]
    if course:
        code = payouts.CODES.get(cur, cur)
        over[("GET", f"/v1/exchange-rate/{code}/list")] = rate(code, course)
    s = Session(routes(over))
    q = quote(s, eid, amount)
    assert q["rate"] == Decimal(course or 1)
    res = run(payouts.send(s, entry(eid), Decimal(amount), q))
    assert res["state"] == "sent" and len(s.posts()) == 1 and payouts.used_today() == q["usdt"]


def test_implausible_rate_through_bot_shows_reason_and_no_send_button():
    """Через бота: сумма введена, курс BTC 1e-30 — «⛔ Выплата не готова» с причиной, кнопки «✅ Отправить» нет."""
    s = Session(routes({RATE_BTC: rate("BTC", "1e-30")}))
    bot = owner(s)

    async def go():
        await bot.on_update(_msg(1, "/payout"))
        await bot.on_update(_cb(1, "pay_to:w2"))
        await bot.on_update(_msg(1, "0.01"))
    run(go())
    assert _texts(bot)[-1].startswith("⛔ Выплата не готова: курс BTC к USDT неправдоподобен (1.000E-30)")
    assert bot.payout_preview is None and not s.posts() and payouts.history() == []


# --- неправдоподобная комиссия Cryptomus: отказ с причиной, а не исключение ---

FEE_REASON = "Cryptomus не отдал правдоподобную комиссию"


def _services(*items):
    return Resp(200, {"state": 0, "result": list(items)})


@pytest.mark.parametrize("fee,pct,shown", [
    ("1e30", "0", "fee_amount 1.000E+30, percent 0"), ("1e1000000", "0", "fee_amount 1.000E+1000000, percent 0"),
    ("1", "1e30", "fee_amount 1, percent 1.000E+30"), ("1", "1e1000000", "fee_amount 1, percent 1.000E+1000000"),
    ("NaN", "0", "fee_amount ?, percent 0"), ("Infinity", "0", "fee_amount ?, percent 0"),
    ("-1", "0", "fee_amount -1, percent 0"), ("1", "-0.5", "fee_amount 1, percent -0.5"),
    ("abc", "0", "fee_amount ?, percent 0"), (None, "0", "fee_amount ?, percent 0"),
    ([1], "0", "fee_amount ?, percent 0"), ({"x": 1}, "0", "fee_amount ?, percent 0"),
    (True, "0", "fee_amount ?, percent 0"), ("1", "много", "fee_amount 1, percent ?")])
def test_absurd_fee_in_preview_is_a_refusal_not_a_crash(fee, pct, shown):
    """Комиссия из /v1/payout/services — не число, NaN, отрицательная или такая, что расчёт не помещается в Decimal
    (quantize раньше бросал InvalidOperation из quote): отказ с причиной и значениями, коротко; ничего не записано."""
    s = Session(routes({SERVICES: _services(svc("USDT", "TRON", fee=fee, pct=pct))}))
    q, why = run(payouts.quote(s, entry("w1"), Decimal("25")))
    assert q is None and why == f"{FEE_REASON} ({shown}) — лимит не проверить, выплату не отправляю"
    assert len(why) < 200 and not s.posts() and payouts.history() == [] and payouts.used_today() == 0


@pytest.mark.parametrize("fee,pct", [("1e30", "0"), ("1", "1e1000000"), ("NaN", "0"), ("-1", "0"), ("abc", "0")])
def test_absurd_fee_at_send_refused_before_post_and_counts_nothing(fee, pct):
    """Предпросмотр с нормальной комиссией, к отправке Cryptomus отдаёт неправдоподобную — отказ до POST (раньше
    1e30 бросал исключение из send, и бот писал «сбой»); после этого выплата с нормальной комиссией идёт."""
    s = Session(routes())
    q = quote(s, "w1", "25")
    s.routes[SERVICES] = _services(svc("USDT", "TRON", fee=fee, pct=pct))
    res = run(payouts.send(s, entry("w1"), Decimal("25"), q))
    assert res["state"] == "refused" and res["reason"].startswith(FEE_REASON)
    assert not s.posts() and payouts.history() == [] and payouts.used_today() == 0
    s.routes[SERVICES] = _services(*SERVICE_LIST)
    assert run(payouts.send(s, entry("w1"), Decimal("25"), q))["state"] == "sent" and len(s.posts()) == 1


@pytest.mark.parametrize("limit,commission,reason", [
    ("abc", {"fee_amount": "1", "percent": "0"}, "не отдал лимиты"),
    ([1, 2], {"fee_amount": "1", "percent": "0"}, "не отдал лимиты"),
    ({"min_amount": "NaN", "max_amount": "100000"}, {"fee_amount": "1", "percent": "0"}, "не отдал лимиты"),
    ({"min_amount": "1", "max_amount": "100000"}, "abc", FEE_REASON),
    ({"min_amount": "1", "max_amount": "100000"}, [1], FEE_REASON),
    ({"min_amount": "1", "max_amount": "100000"}, None, FEE_REASON)])
def test_garbage_limit_or_commission_objects_refused_without_crash(limit, commission, reason):
    """limit/commission не словарь (строка, список) — раньше AttributeError на .get; теперь отказ с причиной."""
    service = {"network": "TRON", "currency": "USDT", "is_available": True, "limit": limit, "commission": commission}
    s = Session(routes({SERVICES: _services(service)}))
    q, why = run(payouts.quote(s, entry("w1"), Decimal("25")))
    assert q is None and reason in why and payouts.history() == []


def test_huge_but_countable_fee_is_stopped_by_limits():
    """Огромная, но считаемая комиссия (1e15 USDT) не проходит мимо лимитов: списание = сумма + комиссия."""
    s = Session(routes({SERVICES: _services(svc("USDT", "TRON", fee="1e15"))}))
    q, why = run(payouts.quote(s, entry("w1"), Decimal("25")))
    assert q is None and "больше лимита одной выплаты" in why and payouts.history() == []


def test_absurd_fee_through_bot_shows_reason_and_no_send_button():
    s = Session(routes({SERVICES: _services(svc("USDT", "TRON", fee="1e30"))}))
    bot = owner(s)

    async def go():
        await bot.on_update(_msg(1, "/payout"))
        await bot.on_update(_cb(1, "pay_to:w1"))
        await bot.on_update(_msg(1, "25"))
    run(go())
    assert _texts(bot)[-1].startswith(f"⛔ Выплата не готова: {FEE_REASON} (fee_amount 1.000E+30, percent 0)")
    assert bot.payout_preview is None and not s.posts() and payouts.history() == []
