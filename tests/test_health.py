"""Здоровье данных: свободное место на диске, размеры баз/логов, возраст резервной копии (health.py) и
алерт /status «Подробно» при нехватке места (Bot.disk_check)."""
import bot as B
import health
import p2p
from helpers import arun


class Stub(B.Bot):
    """Бот без сети: все вызовы Telegram пишутся в self.out; self.ok управляет тем, что вернёт call()."""
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []
        self.ok = True

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": self.ok}


def _make_data_dir(tmp_path, with_backup=True):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "trades.db").write_bytes(b"x" * 100)
    (data_dir / "history.db").write_bytes(b"y" * 300)
    (data_dir / "keys.json").write_text("FAKE")
    if with_backup:
        (data_dir / "backup" / "20260928-0930").mkdir(parents=True)
    log_path = tmp_path / "logs" / "bot.log"
    log_path.parent.mkdir()
    log_path.write_bytes(b"z" * 50)
    return str(data_dir), str(log_path)


# --- snapshot: только *.db, не keys.json и не backup ---------------------------------------------------------

def test_snapshot_lists_only_db_files_not_keys_or_backup(tmp_path):
    data_dir, log_path = _make_data_dir(tmp_path)
    snap = health.snapshot(data_dir=data_dir, log_path=log_path)
    names = {name for name, _ in snap["dbs"]}
    assert names == {"trades.db", "history.db"}
    assert "keys.json" not in names
    assert snap["logs_bytes"] == 50
    assert snap["backup_count"] == 1
    assert snap["backup_last_ts"] is not None


def test_snapshot_no_backup_yet(tmp_path):
    data_dir, log_path = _make_data_dir(tmp_path, with_backup=False)
    snap = health.snapshot(data_dir=data_dir, log_path=log_path)
    assert snap["backup_count"] == 0
    assert snap["backup_last_ts"] is None


def test_snapshot_disk_usage_oserror_gives_none(tmp_path, monkeypatch):
    data_dir, log_path = _make_data_dir(tmp_path)

    def boom(path):
        raise OSError("no such disk")
    monkeypatch.setattr(health.shutil, "disk_usage", boom)
    snap = health.snapshot(data_dir=data_dir, log_path=log_path)
    assert snap["free_bytes"] is None and snap["total_bytes"] is None


# --- fmt_size / low / lines ------------------------------------------------------------------------------------

def test_fmt_size():
    assert health.fmt_size(None) == "?"
    assert health.fmt_size(500_000_000) == "500.0 МБ"
    assert health.fmt_size(2_500_000_000) == "2.5 ГБ"


def test_low_boundary():
    assert health.low(health.LOW_FREE) is False        # ровно на границе — ещё не мало
    assert health.low(health.LOW_FREE - 1) is True
    assert health.low(None) is False


def test_lines_normal_case():
    snap = {"free_bytes": 5_000_000_000, "total_bytes": 20_000_000_000,
            "dbs": [("trades.db", 300_000_000), ("history.db", 100_000_000)],
            "logs_bytes": 5_000_000, "backup_count": 3, "backup_last_ts": 1_700_000_000.0}
    now = 1_700_000_000.0 + 2 * 3600 + 1
    out = health.lines(snap, now)
    assert "свободно 5.0 ГБ из 20.0 ГБ" in out[0]
    assert "trades.db 300.0 МБ" in out[0] and "history.db 100.0 МБ" in out[0] and "логи 5.0 МБ" in out[0]
    assert out[1] == "Копия баз: 2 ч назад, копий 3"


def test_lines_no_backup_yet():
    snap = {"free_bytes": 5_000_000_000, "total_bytes": 20_000_000_000, "dbs": [],
            "logs_bytes": 0, "backup_count": 0, "backup_last_ts": None}
    assert health.lines(snap, 1_700_000_000.0)[1] == "Копий баз ещё нет"


def test_lines_disk_unknown():
    snap = {"free_bytes": None, "total_bytes": None, "dbs": [], "logs_bytes": 0,
            "backup_count": 0, "backup_last_ts": None}
    assert health.lines(snap, 1_700_000_000.0)[0] == "💾 Диск: нет данных"


# --- Bot.disk_check ---------------------------------------------------------------------------------------

def test_disk_check_alerts_when_low(monkeypatch):
    bot = Stub(p2p.Config())
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": 500_000_000})
    arun(bot.disk_check(now=bot.disk_checked_ts + health.DISK_CHECK_EVERY + 1))
    assert bot.disk_alerted is True
    warns = [p["text"] for m, p in bot.out if m == "sendMessage"]
    assert len(warns) == 1 and "⚠️" in warns[0] and "data/" in warns[0]


def test_disk_check_no_repeat_within_interval(monkeypatch):
    bot = Stub(p2p.Config())
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": 500_000_000})
    t0 = bot.disk_checked_ts + health.DISK_CHECK_EVERY + 1
    arun(bot.disk_check(now=t0))
    assert len(bot.out) == 1
    arun(bot.disk_check(now=t0 + 10))        # тот же час — рано
    assert len(bot.out) == 1


def test_disk_check_does_not_duplicate_while_already_alerted(monkeypatch):
    bot = Stub(p2p.Config())
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": 500_000_000})
    t0 = bot.disk_checked_ts + health.DISK_CHECK_EVERY + 1
    arun(bot.disk_check(now=t0))
    arun(bot.disk_check(now=t0 + health.DISK_CHECK_EVERY + 1))
    warns = [p["text"] for m, p in bot.out if m == "sendMessage"]
    assert len(warns) == 1


def test_disk_check_repeats_after_failed_delivery(monkeypatch):
    bot = Stub(p2p.Config())
    bot.ok = False
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": 500_000_000})
    t0 = bot.disk_checked_ts + health.DISK_CHECK_EVERY + 1
    arun(bot.disk_check(now=t0))
    assert bot.disk_alerted is False
    bot.ok = True
    arun(bot.disk_check(now=t0 + health.DISK_CHECK_EVERY + 1))
    assert bot.disk_alerted is True
    warns = [p["text"] for m, p in bot.out if m == "sendMessage"]
    assert len(warns) == 2


def test_disk_check_recovery_message_and_reset(monkeypatch):
    bot = Stub(p2p.Config())
    bot.disk_alerted = True
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": int(1.6 * health.LOW_FREE)})
    t0 = bot.disk_checked_ts + health.DISK_CHECK_EVERY + 1
    arun(bot.disk_check(now=t0))
    assert bot.disk_alerted is False
    warns = [p["text"] for m, p in bot.out if m == "sendMessage"]
    assert warns == ["✅ Место на диске восстановилось"]


def test_disk_check_now_before_checked_ts_is_noop(monkeypatch):
    bot = Stub(p2p.Config())
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": 500_000_000})
    arun(bot.disk_check(now=bot.disk_checked_ts - 10))
    assert bot.out == []
    assert bot.disk_alerted is False


def test_disk_check_free_none_is_noop(monkeypatch):
    bot = Stub(p2p.Config())
    monkeypatch.setattr(health, "snapshot", lambda *a, **k: {"free_bytes": None})
    arun(bot.disk_check(now=bot.disk_checked_ts + health.DISK_CHECK_EVERY + 1))
    assert bot.out == []
    assert bot.disk_alerted is False


# --- status_view: health-блок и устойчивость к сбою --------------------------------------------------------

def test_status_view_includes_health_lines():
    """health.snapshot() без аргументов идёт в backup.DATA_DIR/p2p.LOG_PATH — conftest уже перенаправил их во
    временную папку теста (реальный диск под ней есть, так что строка свободного места не пустая)."""
    bot = Stub(p2p.Config())
    text = bot.status_view()
    assert "💾 Диск" in text or "Диск: нет данных" in text


def test_status_view_survives_broken_health(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("disk read failed")
    monkeypatch.setattr(health, "snapshot", boom)
    bot = Stub(p2p.Config())
    with caplog.at_level("WARNING"):
        text = bot.status_view()
    assert "Статус бота" in text
    assert "💾" not in text
    assert any("data health" in r.message for r in caplog.records)
