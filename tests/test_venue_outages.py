"""История недоступности площадок: эпизод простоя пишется в history.db при восстановлении (переживает перезапуск
бота), /status «Подробно» — сводка за 7 дней."""
import time

import bot as B
import history
import p2p
from helpers import arun

NOW = 1_790_000_000.0


class Stub(B.Bot):
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": 1}}


def snap(errors):
    return p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, errors)


def test_record_and_stats_window_and_min_duration(tmp_path):
    db = str(tmp_path / "history.db")
    history.record_outage("bybit", NOW - 3600, NOW - 1800, 150, True, "Timeout", path=db)     # 30 мин, алерт
    history.record_outage("bybit", NOW - 900, NOW - 840, 6, False, "", path=db)               # 1 мин
    history.record_outage("bybit", NOW - 100, NOW - 90, 1, False, "", path=db)                # 10 с — разовый
    history.record_outage("htx", NOW - 8 * 86400 - 600, NOW - 8 * 86400, 60, False, "", path=db)   # вне окна
    history.record_outage("mexc", NOW - 7 * 86400 - 600, NOW - 7 * 86400 + 60, 7, False, "", path=db)  # на краю
    st = history.outage_stats(7, path=db, now=NOW, min_seconds=60)
    assert set(st) == {"bybit", "mexc"}
    assert st["bybit"] == {"count": 2, "total": 1860.0, "longest": 1800.0, "alerted": 1}
    assert st["mexc"]["total"] == 60.0                                          # часть до окна не считается
    assert history.outage_stats(7, path=str(tmp_path / "none.db")) == {}


def test_check_venues_writes_episode_on_recovery(monkeypatch):
    bot = Stub(p2p.Config(exchanges=["bybit", "htx"]))
    clock = {"t": NOW}
    monkeypatch.setattr(B.time, "time", lambda: clock["t"])
    for i in range(4):
        clock["t"] = NOW + i * 10
        arun(bot.check_venues(snap({"bybit/USDT": "TimeoutError"})))
    clock["t"] = NOW + 40
    arun(bot.check_venues(snap({})))
    st = history.outage_stats(7, now=NOW + 60)
    assert st == {"bybit": {"count": 1, "total": 40.0, "longest": 40.0, "alerted": 1}}
    arun(bot.check_venues(snap({})))                                            # дальше доступна — не дублируется
    assert history.outage_stats(7, now=NOW + 60)["bybit"]["count"] == 1


def test_outage_lines_and_status_view(tmp_path, monkeypatch):
    assert B.outage_lines({}) == []
    lines = B.outage_lines({"htx": {"count": 1, "total": 120.0, "longest": 120.0, "alerted": 0},
                            "bybit": {"count": 3, "total": 5400.0, "longest": 3700.0, "alerted": 2}})
    assert lines[1].startswith("📉 <b>Недоступность площадок за 7 дн.</b>")
    assert lines[2] == "• Bybit: 3 раз, всего 1 ч 30 мин, дольше всего 1 ч 1 мин, с алертом 2"
    assert lines[3] == "• HTX: 1 раз, всего 2 мин, дольше всего 2 мин"
    history.record_outage("kucoin", time.time() - 700, time.time() - 100, 60, True, "x")
    bot = Stub(p2p.Config())
    bot.last_scan_ts, bot.last = time.time(), snap({})
    text = bot.status_view(str(tmp_path / "none.json"))
    assert "Недоступность площадок" in text and "• KuCoin: 1 раз, всего 10 мин" in text


def test_status_survives_broken_history(tmp_path, monkeypatch):
    monkeypatch.setattr(B.history, "outage_stats", lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    bot = Stub(p2p.Config())
    bot.last_scan_ts, bot.last = time.time(), snap({})
    assert "Недоступность" not in bot.status_view(str(tmp_path / "none.json"))
