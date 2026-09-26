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
