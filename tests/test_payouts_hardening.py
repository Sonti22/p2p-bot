"""Выплаты Cryptomus, финальная проверка: «⛔ Стоп» во время перезапроса сервисов, «не найдено» при известном uuid,
база первой версии без create_kind, разделители строк в .env, поддельные кнопки мастера первого запуска."""
import asyncio
import sqlite3

import pytest

import bot as B
import payouts
from test_payouts import (INFO, SERVICE_LIST, SERVICES, Resp, Session, _payout_env, _sent_row, info_for,  # noqa: F401
                          owner, routes, run, to_preview)


class GatedOK(Resp):
    """Обычный ответ, но только когда тест откроет ворота."""
    def __init__(self, gate, status=200, body=None):
        super().__init__(status, body)
        self.gate = gate

    async def __aenter__(self):
        await self.gate.wait()
        return self


def _update(data):
    return {"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"message_id": 5, "chat": {"id": 1}}}}


def test_stop_during_services_recheck_sends_no_post():
    """Отправка перед POST заново берёт список сервисов. «⛔ Стоп», нажатый пока этот запрос идёт, — POST не уходит
    вовсе: выключатель проверяется ещё раз после ответа /v1/payout/services."""
    s = Session(routes({INFO: info_for({})}))
    bot = owner(s)
    token = to_preview(bot)

    async def go():
        gate = asyncio.Event()
        s.routes[SERVICES] = lambda call: GatedOK(gate, 200, {"state": 0, "result": SERVICE_LIST})
        await asyncio.wait_for(bot.on_update(_update(f"pay_ok:{token}")), 5)
        for _ in range(20):
            await asyncio.sleep(0)
        assert not bot.payout_task.done()
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
        run(bot.payout_callback({"id": "g", "data": "pay_hist", "message": {"message_id": 3, "chat": {"id": 1}}},
                                "pay_hist"))
    finally:
        B.REPLY_CHAT.reset(guest)
    new = bot.out[out:]
    assert [p["text"] for m, p in new if m == "sendMessage"] == [B.GUEST_DENIED] * 2
    assert [p.get("text") for m, p in new if m == "answerCallbackQuery"] == ["Только для владельца бота"]
    assert bot.payout_preview["token"] == token and len(bot.s.posts()) == posts
    async def owner_press():
        await bot.on_callback({"id": "o", "data": f"pay_ok:{token}", "message": {"message_id": 5, "chat": {"id": 1}}})
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
