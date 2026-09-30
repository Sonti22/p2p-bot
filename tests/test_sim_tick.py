"""Bot.sim_tick: сбой одной бумажной симуляции (фандинг или направленные сделки) не должен мешать другой и не
должен вылетать наружу в perp_loop. tests/test_simfunding.py:154 раньше проверял это без единого assert — см.
tests/test_hygiene.py, который теперь не пропустит такой тест. Оба tick всегда подменены — сеть и реальные
симуляции не запускаются."""
import logging

import bot as B
import p2p
import test_bot as TB


def _patch(monkeypatch, fail=None):
    """Подменить simfunding.tick/simdirectional.tick: каждый добавляет своё имя в calls и, если имя == fail,
    бросает RuntimeError('x'). Возвращает calls."""
    calls = []

    def make(name):
        def tick(*a, **k):
            calls.append(name)
            if name == fail:
                raise RuntimeError("x")
        return tick

    monkeypatch.setattr(B.simfunding, "tick", make("simfunding"))
    monkeypatch.setattr(B.simdirectional, "tick", make("simdirectional"))
    return calls


def test_first_sim_failure_does_not_block_second(monkeypatch, caplog):
    calls = _patch(monkeypatch, fail="simfunding")
    bot = TB.Stub(p2p.Config())
    with caplog.at_level(logging.ERROR):
        bot.sim_tick()
    assert calls == ["simfunding", "simdirectional"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == ["simfunding: x"]


def test_second_sim_failure_does_not_block_first(monkeypatch, caplog):
    calls = _patch(monkeypatch, fail="simdirectional")
    bot = TB.Stub(p2p.Config())
    with caplog.at_level(logging.ERROR):
        bot.sim_tick()
    assert calls == ["simfunding", "simdirectional"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == ["simdirectional: x"]


def test_both_sims_run_in_order_without_errors(monkeypatch, caplog):
    calls = _patch(monkeypatch, fail=None)
    bot = TB.Stub(p2p.Config())
    with caplog.at_level(logging.ERROR):
        bot.sim_tick()
    assert calls == ["simfunding", "simdirectional"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
