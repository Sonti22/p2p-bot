"""Контракт кодов причин «сигнал не ушёл»: history.SIGNAL_REASONS — единственный источник правды, сверяется с
буквальными строками, которыми Bot.signal_reasons заполняет reason; NOT_MISSED — подмножество; причина
сохраняется в history.signals и классифицируется signal_stats как ожидается (пропуск/исключение)."""
import ast
import os
import sqlite3

import pytest

import history

BOT_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _reason_literals(source):
    """Строковые литералы, которыми в единственном методе Bot.signal_reasons присваивается reason (охватывает и
    IfExp вида 'quiet' if quiet else 'paused'; reason = None литерала не даёт)."""
    tree = ast.parse(source)
    bot_classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Bot"]
    if len(bot_classes) != 1:
        pytest.fail(f"ожидался ровно один класс Bot в bot.py, найдено {len(bot_classes)}")
    methods = [n for n in ast.walk(bot_classes[0])
               if isinstance(n, ast.FunctionDef) and n.name == "signal_reasons"]
    if len(methods) != 1:
        pytest.fail(f"ожидался ровно один метод Bot.signal_reasons, найдено {len(methods)}")
    out = set()
    for node in ast.walk(methods[0]):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "reason" for t in node.targets):
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.add(sub.value)
    return out


def test_bot_reason_literals_equal_history_constant():
    with open(BOT_PY, encoding="utf-8") as f:
        literals = _reason_literals(f.read())
    expected = set(history.SIGNAL_REASONS)
    only_bot, only_hist = literals - expected, expected - literals
    assert literals == expected, (f"Bot.signal_reasons и history.SIGNAL_REASONS разошлись: только в коде "
                                  f"{only_bot or '{}'}, только в константе {only_hist or '{}'} — обновить "
                                  f"history.SIGNAL_REASONS и решить, входит ли причина в NOT_MISSED")


def test_not_missed_is_subset_of_reasons():
    assert set(history.NOT_MISSED) <= set(history.SIGNAL_REASONS)
    assert len(set(history.SIGNAL_REASONS)) == len(history.SIGNAL_REASONS)


def test_extractor_sees_a_new_reason():
    """Девятая причина в Bot.signal_reasons без правки SIGNAL_REASONS красит test_bot_reason_literals_... —
    без правки самого bot.py, только синтетическим исходником."""
    src = ("class Bot:\n def signal_reasons(self):\n  reason = 'quiet' if q else 'paused'\n"
          "  reason = 'brand_new'\n  reason = None\n")
    assert _reason_literals(src) == {"quiet", "paused", "brand_new"}


T0, K = 1_700_000_000.0, ("Bybit", "USDT", "MEXC", "USDT")


def _scans(db, reason, n=16, step=20):
    """Связка K держится n сканов подряд (шаг step с), сигнал ни разу не уходит, причина одна и та же."""
    ids = {}
    for i in range(n):
        ids = history.track_signals([(K, 1.5, False, reason)], T0 + step * i, ids, path=db)
    return ids


@pytest.mark.parametrize("reason", history.SIGNAL_REASONS)
def test_reason_round_trips_and_is_classified(tmp_path, reason):
    db = str(tmp_path / "history.db")
    _scans(db, reason)
    st = history.signal_stats(path=db, now=T0 + 700, cooldown=600)
    if reason in history.NOT_MISSED:
        assert st["missed"] == 0 and st["excluded_reasons"] == {reason: 1}
    else:
        assert st["missed"] == 1 and st["reasons"] == {reason: 1}
    con = sqlite3.connect(db)
    assert con.execute("SELECT reason_not_signalled FROM signals").fetchone()[0] == reason
    con.close()
