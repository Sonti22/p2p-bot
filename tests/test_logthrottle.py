"""RepeatThrottle (logthrottle.py): одинаковые WARNING/ERROR не чаще раза в окно, с счётчиком подавленного;
emit никогда не бросает исключение и не мутирует исходную запись."""
import io
import logging

import pytest

import logthrottle
import p2p


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _handler(clock, window=logthrottle.WINDOW, max_keys=logthrottle.MAX_KEYS):
    buf = io.StringIO()
    h = logthrottle.ThrottledStreamHandler(buf, window=window, clock=clock, max_keys=max_keys)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h, buf


def _logger(name, handler):
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.addHandler(handler)
    return log


def _lines(buf):
    return [l for l in buf.getvalue().splitlines() if l]


def test_repeated_warning_suppressed_within_window_then_shows_count(monkeypatch):
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.repeat", h)
    try:
        for _ in range(1000):
            log.warning("boom")
            clock.t += 0.05
        assert _lines(buf) == ["boom"]
        clock.t += logthrottle.WINDOW
        log.warning("boom")
        lines = _lines(buf)
        assert len(lines) == 2 and "ещё 999 таких же" in lines[1]
    finally:
        log.removeHandler(h)
        h.close()


def test_different_texts_do_not_suppress_each_other():
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.distinct", h)
    try:
        log.warning("a")
        log.warning("b")
        log.warning("a")
        assert _lines(buf) == ["a", "b"]
    finally:
        log.removeHandler(h)
        h.close()


def test_info_and_below_not_throttled_and_no_key():
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.info", h)
    try:
        for _ in range(5):
            log.info("hi")
        assert len(_lines(buf)) == 5
        assert h._seen == {}
    finally:
        log.removeHandler(h)
        h.close()


def test_warning_and_error_same_text_are_different_keys():
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.levels", h)
    try:
        log.warning("same")
        log.error("same")
        assert _lines(buf) == ["same", "same"]
    finally:
        log.removeHandler(h)
        h.close()


def test_original_record_not_mutated_second_handler_sees_original():
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    plain_buf = io.StringIO()
    plain = logging.StreamHandler(plain_buf)
    plain.setFormatter(logging.Formatter("%(message)s|%(args)r"))
    log = _logger("throttle.mutation", h)
    log.addHandler(plain)
    try:
        log.warning("x %s", 1)      # проходит
        log.warning("x %s", 1)      # подавлена (тот же текст, в окне)
        clock.t += logthrottle.WINDOW
        log.warning("x %s", 1)      # окно истекло, 1 подавленная — суффикс
        assert "ещё 1 таких же" in _lines(buf)[-1]
        assert _lines(plain_buf) == ["x 1|(1,)"] * 3   # второй хендлер видит все три, без суффикса и с прежними args
    finally:
        log.removeHandler(h)
        log.removeHandler(plain)
        h.close()
        plain.close()


def test_max_keys_overflow_does_not_grow_or_crash():
    clock = _Clock(0.0)
    h, buf = _handler(clock, max_keys=500)
    log = _logger("throttle.overflow", h)
    try:
        for i in range(600):
            log.warning("msg-%d", i)
        assert len(h._seen) <= 500
        assert len(_lines(buf)) == 600
    finally:
        log.removeHandler(h)
        h.close()


def test_expired_keys_evicted_before_live_ones():
    clock = _Clock(0.0)
    h, buf = _handler(clock, max_keys=3)
    log = _logger("throttle.evict", h)
    try:
        log.warning("old")                       # ts=0, сразу устареет
        clock.t += logthrottle.WINDOW
        log.warning("fresh1")                     # ts=window — ещё живой
        log.warning("fresh2")                     # ts=window — ещё живой (уже 3 ключа: old/fresh1/fresh2)
        log.warning("new")                        # 4-й ключ: old просрочен (age=window >= window) — вытесняется первым
        assert "old" not in h._seen
        assert set(h._seen) == {("throttle.evict", logging.WARNING, "fresh1"),
                                ("throttle.evict", logging.WARNING, "fresh2"),
                                ("throttle.evict", logging.WARNING, "new")}
    finally:
        log.removeHandler(h)
        h.close()


def test_emit_never_raises_on_bad_format_args(monkeypatch):
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.badfmt", h)
    monkeypatch.setattr(logging, "raiseExceptions", False)
    try:
        log.warning("bad %d", "s")   # %d против строки — getMessage() бросает TypeError внутри emit
        assert True   # не упало
    finally:
        log.removeHandler(h)
        h.close()


def test_clock_going_backward_is_treated_as_expired():
    clock = _Clock(100.0)
    h, buf = _handler(clock)
    log = _logger("throttle.backward", h)
    try:
        log.warning("tick")
        clock.t = 10.0   # часы пошли назад
        log.warning("tick")
        assert _lines(buf) == ["tick", "tick"]   # не подавлено — окно считается истёкшим
    finally:
        log.removeHandler(h)
        h.close()


def test_percent_args_key_by_final_text():
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.percent", h)
    try:
        log.warning("x %s", 1)
        log.warning("x %s", 2)
        log.warning("x %s", 1)
        assert _lines(buf) == ["x 1", "x 2"]
    finally:
        log.removeHandler(h)
        h.close()


def test_key_truncated_at_key_len():
    clock = _Clock(0.0)
    h, buf = _handler(clock)
    log = _logger("throttle.trunc", h)
    try:
        base = "z" * logthrottle.KEY_LEN
        log.warning(base + "AAAA")
        log.warning(base + "BBBB")   # тот же префикс длиной KEY_LEN — один ключ
        assert len(_lines(buf)) == 1
    finally:
        log.removeHandler(h)
        h.close()


def test_setup_logging_throttles_file_and_keeps_rotation_config(tmp_path):
    path = str(tmp_path / "bot.log")
    p2p.setup_logging(path)
    logging.getLogger("throttle.setup").warning("dup warning")
    logging.getLogger("throttle.setup").warning("dup warning")
    content = open(path, encoding="utf-8").read()
    assert content.count("dup warning") == 1
    file_handlers = [h for h in p2p._log_handlers if hasattr(h, "maxBytes")]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 1_000_000 and file_handlers[0].backupCount == 5

    old = list(p2p._log_handlers)
    p2p.setup_logging(str(tmp_path / "bot2.log"))
    for h in old:
        assert h not in p2p._log_handlers
