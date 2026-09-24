import logging
import os

import p2p


def test_setup_logging_creates_file_and_writes_messages(tmp_path):
    log_path = str(tmp_path / "sub" / "bot.log")   # родительской папки ещё нет — должна создаться сама
    p2p.setup_logging(log_path)
    logging.getLogger("p2p_test_logger").info("hello world")
    assert os.path.exists(log_path)
    assert "hello world" in open(log_path, encoding="utf-8").read()


def test_setup_logging_configures_rotation_5x1mb(tmp_path):
    p2p.setup_logging(str(tmp_path / "bot.log"))
    file_handlers = [h for h in p2p._log_handlers if hasattr(h, "maxBytes")]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 1_000_000 and file_handlers[0].backupCount == 5


def test_setup_logging_is_idempotent_and_switches_file(tmp_path):
    path_a, path_b = str(tmp_path / "a.log"), str(tmp_path / "b.log")
    p2p.setup_logging(path_a)
    old_handlers = list(p2p._log_handlers)
    p2p.setup_logging(path_b)
    root = logging.getLogger()
    for h in old_handlers:
        assert h not in root.handlers   # старые хендлеры сняты (и закрыты — не плодим дубликаты)
    logging.getLogger("p2p_test_logger2").info("to b")
    assert "to b" in open(path_b, encoding="utf-8").read()
