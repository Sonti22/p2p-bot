"""BestChange за VPN: если обычный путь молчит, выгрузка идёт с локального адреса ПК и рабочий адрес запоминается."""
import asyncio

import pytest

import p2p


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setitem(p2p._bc, "local", None)
    monkeypatch.delenv("BC_LOCAL_ADDR", raising=False)


def fake_download(monkeypatch, working, log):
    """Скачивание удаётся только для адресов из `working` (None = обычный путь); остальные — таймаут."""
    async def dl(s, local=None):
        log.append(local)
        if local in working:
            return b"zip-bytes"
        raise asyncio.TimeoutError()

    monkeypatch.setattr(p2p, "_bc_download", dl)


def run_fetch():
    return asyncio.run(p2p._bc_fetch(None))


def test_default_path_works_no_local_addr_remembered(monkeypatch):
    log = []
    fake_download(monkeypatch, {None}, log)
    monkeypatch.setattr(p2p, "_local_addrs", lambda: ["192.168.1.104"])
    assert run_fetch() == b"zip-bytes"
    assert log == [None] and p2p._bc["local"] is None


def test_vpn_blocks_default_falls_back_to_local_and_remembers(monkeypatch):
    log = []
    fake_download(monkeypatch, {"192.168.1.104"}, log)
    monkeypatch.setattr(p2p, "_local_addrs", lambda: ["10.0.85.2", "192.168.1.104", "172.24.240.1"])
    assert run_fetch() == b"zip-bytes"
    assert log == [None, "10.0.85.2", "192.168.1.104"] and p2p._bc["local"] == "192.168.1.104"
    log.clear()
    assert run_fetch() == b"zip-bytes"          # следующее скачивание — сразу рабочим адресом
    assert log == ["192.168.1.104"]


def test_remembered_addr_stops_working_falls_through_and_relearns(monkeypatch):
    p2p._bc["local"] = "192.168.1.104"
    log = []
    fake_download(monkeypatch, {None}, log)     # сменилась сеть: старый адрес мёртв, обычный путь снова открыт
    monkeypatch.setattr(p2p, "_local_addrs", lambda: ["192.168.1.104"])
    assert run_fetch() == b"zip-bytes"
    assert log == ["192.168.1.104", None] and p2p._bc["local"] is None


def test_forced_addr_from_env_is_tried_first(monkeypatch):
    monkeypatch.setenv("BC_LOCAL_ADDR", "192.168.5.5")
    log = []
    fake_download(monkeypatch, {"192.168.5.5"}, log)
    monkeypatch.setattr(p2p, "_local_addrs", lambda: ["192.168.1.104"])
    assert run_fetch() == b"zip-bytes"
    assert log == ["192.168.5.5"] and p2p._bc["local"] == "192.168.5.5"


def test_all_paths_fail_raises_last_error_and_forgets_stale_addr(monkeypatch):
    p2p._bc["local"] = "192.168.1.104"
    log = []
    fake_download(monkeypatch, set(), log)
    monkeypatch.setattr(p2p, "_local_addrs", lambda: ["192.168.1.104", "10.0.85.2"])
    with pytest.raises(asyncio.TimeoutError):
        run_fetch()
    assert log == ["192.168.1.104", None, "10.0.85.2"] and p2p._bc["local"] is None


def test_tries_are_capped(monkeypatch):
    log = []
    fake_download(monkeypatch, set(), log)
    monkeypatch.setattr(p2p, "_local_addrs", lambda: [f"192.168.1.{i}" for i in range(20)])
    with pytest.raises(asyncio.TimeoutError):
        run_fetch()
    assert len(log) == p2p.BC_MAX_TRIES


def test_local_addrs_filters_and_orders(monkeypatch):
    monkeypatch.setattr(p2p.socket, "gethostname", lambda: "pc")
    monkeypatch.setattr(p2p.socket, "gethostbyname_ex",
                        lambda h: ("pc", [], ["172.24.240.1", "127.0.0.1", "10.0.85.2", "169.254.228.32",
                                             "192.168.1.104", "10.0.85.2"]))
    assert p2p._local_addrs() == ["192.168.1.104", "10.0.85.2", "172.24.240.1"]
    monkeypatch.setattr(p2p.socket, "gethostbyname_ex", lambda h: (_ for _ in ()).throw(OSError("dns")))
    assert p2p._local_addrs() == []


def test_bestchange_fetcher_uses_fetch_and_parse(monkeypatch):
    """Полный путь fetcher-а: _bc_fetch -> _bc_parse -> фильтр по стороне/монете; сбой пробрасывается."""
    async def fetch(s):
        return b"data"

    ads = [p2p.Ad("BestChange", "buy", 90.0, 1, 2, 3, ["Сбербанк"], "x", 1, 100.0, "", "USDT", "TRC20"),
           p2p.Ad("BestChange", "sell", 91.0, 1, 2, 3, ["Сбербанк"], "y", 1, 100.0, "", "USDT", "TRC20")]
    monkeypatch.setattr(p2p, "_bc_fetch", fetch)
    monkeypatch.setattr(p2p, "_bc_parse", lambda data: ads)
    monkeypatch.setitem(p2p._bc, "t", 0.0)
    monkeypatch.setitem(p2p._bc, "tried", 0.0)
    got = asyncio.run(p2p.bestchange(None, p2p.Config(bc_refresh=120), "buy", "USDT"))
    assert [a.price for a in got] == [90.0]

    async def boom(s):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(p2p, "_bc_fetch", boom)
    monkeypatch.setitem(p2p._bc, "t", 0.0)
    monkeypatch.setitem(p2p._bc, "tried", 0.0)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(p2p.bestchange(None, p2p.Config(bc_refresh=120), "buy", "USDT"))
