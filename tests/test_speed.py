"""Этап 2.7 «скорость»: каждый запрос скана — своя задача, скан ждёт её до VENUE_TIMEOUT; медленная площадка не
задерживает остальные. Не успевший запрос идёт в фоне (не дольше REQUEST_MAX) и не считается ошибкой — его ответ берёт
следующий скан; ответа нет вовсе — данные площадки устарели (прошлый ответ не старше FRESH_WINDOW, Ad.stale): в /top
видны с пометкой, но не идут в сигнал (и места в топ-N не занимают), серию LIVE_SCANS, сухой прогон, дайджест и алерты;
скорость сканов и площадок (p50/p90) — в /status; снимки и replay помнят пометку."""
from helpers import arun
import asyncio
import dataclasses
import time

import pytest

import alerts
import bot as B
import netstatus
import p2p
import paper
import replay
import snapshots
from test_bot import Stub
from test_paper_realism import _on, snap_of as paper_snap_of
from test_signal_traps import _bot, ad, captions, good_deal

SLOW = 30.0      # сек «зависшей» площадки — в разы больше срока
TIMEOUT = 0.5    # VENUE_TIMEOUT в тестах
EPS = 1.5        # запас на сборку снимка и медленный CI


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Прошлые ответы площадок, запросы в полёте и конец прошлого сбора — модульное состояние p2p; у теста свои."""
    monkeypatch.setattr(p2p, "_venue_last", {})
    monkeypatch.setattr(p2p, "_inflight", {})
    monkeypatch.setattr(p2p, "_collect_end", {"t": 0.0})
    monkeypatch.setattr(p2p, "_venue_backoff", {})


def _cfg(**kw):
    base = dict(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"], min_orders=0, min_rate=0,
                min_profit=-100.0, venue_timeout=TIMEOUT)
    base.update(kw)
    return p2p.Config(**base)


def _slow(monkeypatch, name="htx", delay=SLOW, calls=None):
    """Площадка name отвечает через delay сек (как зависшая); остальное — фикстуры. calls — список запросов."""
    orig = p2p.FETCHERS[name]

    async def slow(s, cfg, side, asset):
        if calls is not None:
            calls.append((side, asset))
        await asyncio.sleep(delay)
        return await orig(s, cfg, side, asset)
    monkeypatch.setitem(p2p.FETCHERS, name, slow)


def _calm_scan(cfg):
    """Скан с большим сроком: все площадки успевают — есть прошлый ответ (медленная машина, -X dev)."""
    return arun(p2p.scan(None, dataclasses.replace(cfg, venue_timeout=10.0)))


def _timed_scan(cfg):
    t0 = time.monotonic()
    snap = arun(p2p.scan(None, cfg))
    return snap, time.monotonic() - t0


def _key(d):
    return d[1].ex, d[1].asset, d[2].ex, d[2].asset


# --- скан не ждёт медленную площадку ---

def test_slow_venue_does_not_delay_others(offline, monkeypatch):
    _slow(monkeypatch)
    snap, took = _timed_scan(_cfg())
    assert took < TIMEOUT + EPS                                          # не SLOW: скан не ждал HTX
    assert {a.ex for a in snap.ads} == {"Bybit", "KuCoin", "MEXC", "BitPapa"}   # остальные площадки на месте
    assert snap.deals and all("HTX" not in _key(d) for d in snap.deals)
    assert snap.errors["htx/USDT"].startswith(f"таймаут {TIMEOUT:g} с")   # причина — в ошибках (/status)
    jobs = [j for j in snap.jobs if j["ex"] == "htx"]
    assert len(jobs) == 2 and all(j["timeout"] and j["stale"] is False and j["n"] == 0 for j in jobs)
    assert all(snap.ts + TIMEOUT * 0.9 <= j["t1"] < snap.ts + TIMEOUT + EPS for j in jobs)   # ждали до срока (часы — 16 мс)
    assert all(not j.get("timeout") for j in snap.jobs if j["ex"] != "htx")
    assert not p2p._venue_paused_until("htx")                            # таймаут скана — не ошибка: запрос ещё идёт
    assert not p2p._venue_paused_until("bybit")


def test_slow_venue_never_backs_off_and_its_answer_arrives_next_scan(offline, monkeypatch):
    """Площадка отвечает дольше срока скана, но в пределах REQUEST_MAX (как 10 с при VENUE_TIMEOUT 8): в паузу не
    уходит, её ответ, докачанный в фоне, следующий скан берёт как новые данные; запросов — один на скан, как было."""
    calls = []
    _slow(monkeypatch, delay=TIMEOUT * 1.6, calls=calls)
    cfg = _cfg()

    async def go():
        snaps = []
        for _ in range(4):
            snaps.append(await p2p.scan(None, cfg))
            await asyncio.sleep(TIMEOUT * 2)                               # INTERVAL: запрос успевает до скана
        return snaps

    first, *rest = arun(go())
    assert not [a for a in first.ads if a.ex == "HTX"] and "htx/USDT" in first.errors
    for snap in rest:
        htx = [a for a in snap.ads if a.ex == "HTX"]
        assert htx and not any(a.stale for a in htx) and "htx/USDT" not in snap.errors
        deals = [d for d in snap.deals if "HTX" in (d[1].ex, d[2].ex)]
        assert deals and all(p2p.deal_fresh(d, snap) and not p2p.deal_stale(d) for d in deals)
        jobs = [j for j in snap.jobs if j["ex"] == "htx"]
        assert all(j["timeout"] and j["late"] and j["cached"] and j["n"] for j in jobs)
        assert not p2p._venue_paused_until("htx")
    assert len(calls) == 2 * 4                                            # buy + sell на каждый скан, без лишних


def test_request_in_flight_is_not_repeated(offline, monkeypatch):
    """Пока запрос прошлого скана идёт, новый по тому же ключу не шлём; его ответ достаётся следующему скану."""
    calls = []
    _slow(monkeypatch, delay=TIMEOUT * 6, calls=calls)
    cfg = _cfg()

    async def go():
        snaps = []
        for _ in range(3):                                                # все три — пока первый запрос идёт
            snaps.append(await p2p.scan(None, cfg))
            await asyncio.sleep(TIMEOUT * 0.1)
        n = len(calls)
        await asyncio.sleep(TIMEOUT * 5)                                  # запрос докачался
        snaps.append(await p2p.scan(None, cfg))
        return snaps, n

    snaps, during = arun(go())
    assert during == 2                                                    # один запрос на сторону за три скана
    assert [bool([a for a in s.ads if a.ex == "HTX"]) for s in snaps] == [False, False, False, True]
    assert len(calls) == 4 and not p2p._venue_paused_until("htx")         # дальше — снова запрос на скан


def test_request_over_hard_limit_is_error_and_backs_off(offline, monkeypatch):
    """Дольше REQUEST_MAX (общий таймаут HTTP бота) — ошибка площадки: бэкофф, как раньше; ошибка — тоже бэкофф."""
    monkeypatch.setattr(p2p, "REQUEST_MAX", TIMEOUT * 2)
    _slow(monkeypatch)
    cfg = _cfg()

    async def go():
        first = await p2p.scan(None, cfg)
        paused = p2p._venue_paused_until("htx")
        await asyncio.sleep(TIMEOUT * 2)
        return first, paused, await p2p.scan(None, cfg)

    first, paused_after_first, second = arun(go())
    assert paused_after_first is None and "таймаут" in first.errors["htx/USDT"]
    assert "нет ответа за" in second.errors["htx/USDT"] and p2p._venue_paused_until("htx")

    async def boom(s, cfg, side, asset):
        raise RuntimeError("down")
    monkeypatch.setitem(p2p.FETCHERS, "kucoin", boom)
    arun(p2p.scan(None, cfg))
    assert p2p._venue_paused_until("kucoin")


def test_timed_out_venue_uses_previous_answer_flagged_stale(offline, monkeypatch):
    cfg = _cfg()
    first = _calm_scan(cfg)
    htx_deals = {_key(d) for d in first.deals if "HTX" in (d[1].ex, d[2].ex)}
    assert htx_deals and not any(a.stale for a in first.ads)
    _slow(monkeypatch)
    snap, took = _timed_scan(cfg)
    assert took < TIMEOUT + EPS
    htx = [a for a in snap.ads if a.ex == "HTX"]
    assert htx and all(a.stale for a in htx) and not any(a.stale for a in snap.ads if a.ex != "HTX")
    assert {(a.side, a.price, a.nick) for a in htx} == {(a.side, a.price, a.nick) for a in first.ads if a.ex == "HTX"}
    assert "устарели" in snap.errors["htx/USDT"]
    jobs = [j for j in snap.jobs if j["ex"] == "htx"]
    assert all(j["timeout"] and j["stale"] and j["cached"] and j["n"] > 0 and j["age"] >= 0 for j in jobs)
    stale = [d for d in snap.deals if "HTX" in (d[1].ex, d[2].ex)]
    assert {_key(d) for d in stale} == htx_deals                        # связки видны, как по прошлым данным
    assert all(p2p.deal_stale(d) and not p2p.deal_fresh(d, snap) for d in stale)
    assert all(not p2p.deal_stale(d) for d in snap.deals if "HTX" not in (d[1].ex, d[2].ex))
    text = p2p.fmt_deal(stale[0], cfg, snap)
    assert p2p.STALE_REASON in text and "сигналом не придёт" in text   # причина видна в /top и /best
    assert p2p.STALE_REASON in p2p.fmt_signal(stale[0], cfg, snap)
    assert p2p.STALE_REASON in p2p.fmt_top(snap, cfg, n=50)


def test_previous_answer_older_than_window_or_other_amount_not_used(offline, monkeypatch):
    cfg = _cfg()
    _calm_scan(cfg)
    _slow(monkeypatch)
    old = _cfg(fresh_window=0.001)                                      # прошлый ответ уже старше окна
    snap = arun(p2p.scan(None, old))
    assert not [a for a in snap.ads if a.ex == "HTX"]
    assert "не старше" in snap.errors["htx/USDT"]
    p2p._venue_backoff.clear()
    other = _cfg(amount=cfg.amount * 2)                                 # другая сумма круга — другие объявления
    snap = arun(p2p.scan(None, other))
    assert not [a for a in snap.ads if a.ex == "HTX"]


def test_stale_alt_copies_leave_alt_cache_after_window(offline, monkeypatch):
    cfg = _cfg(assets=["USDT", "ETH"], exchanges=["bybit", "htx"], alt_interval=0)
    _calm_scan(cfg)                                    # ETH опрошена: прошлый ответ есть
    _slow(monkeypatch)
    snap = arun(p2p.scan(None, cfg))
    eth = [a for a in snap.ads if a.ex == "HTX" and a.asset == "ETH"]
    assert eth and all(a.stale for a in eth)                            # копия легла и в кэш монет _alt
    p2p._venue_backoff.clear()
    cfg.alt_interval, cfg.fresh_window = 3600, 0.001                     # следующий скан берёт ETH из кэша _alt
    snap = arun(p2p.scan(None, cfg))
    assert not [a for a in snap.ads if a.asset == "ETH" and a.ex == "HTX"]   # копия старше окна — не показываем
    assert [a for a in snap.ads if a.asset == "ETH" and a.ex == "Bybit"]


def test_slow_spot_rapira_and_networks_do_not_delay_scan(offline, monkeypatch):
    real = p2p._json
    done = []

    async def slow_json(s, method, url, body=None):
        if "api.htx.com/market/tickers" in url or "rapira.net" in url:
            await asyncio.sleep(SLOW)
        return await real(s, method, url, body)

    async def slow_refresh(s, assets, exchanges, get_json):
        await asyncio.sleep(TIMEOUT * 2)
        done.append(True)
        return {}

    monkeypatch.setattr(p2p, "_json", slow_json)
    monkeypatch.setattr(netstatus, "refresh_if_due", slow_refresh)

    async def go():
        t0 = time.monotonic()
        snap = await p2p.scan(None, _cfg(exchanges=["bybit", "kucoin"], assets=["USDT", "BTC"]))
        took = time.monotonic() - t0
        await asyncio.sleep(TIMEOUT * 3)                                  # справочник сетей дообновляется в фоне
        return snap, took

    snap, took = arun(go())
    assert took < TIMEOUT + EPS
    assert set(snap.spot["HTX"]) == {"USDT"} and "BTC" in snap.spot["Bybit"]   # спот HTX не успел, остальные — да
    assert snap.ref_src == "медиана P2P"                                  # Rapira не успела — ориентир по P2P, как при сбое
    jobs = {j["ex"]: j for j in snap.jobs}
    assert jobs["rapira"]["timeout"] and jobs["networks"]["timeout"] and done == [True]


# --- BestChange: выгрузка не обрывается таймаутом скана ---

def test_bestchange_download_finishes_in_background_and_counts_fresh(offline, monkeypatch):
    got = []
    bc_ad = p2p.Ad("BestChange", "sell", 90.0, 1000, 500000, 10000, ["T-Bank"], "Obmen [TRC20]", 500, 99.0,
                   asset="USDT", net="TRC20")

    async def fetch(s):
        await asyncio.sleep(TIMEOUT * 2)                                  # дольше срока скана
        got.append(time.time())
        return b"zip"

    monkeypatch.setattr(p2p, "_bc_fetch", fetch)
    monkeypatch.setattr(p2p, "_bc_parse", lambda data: [dataclasses.replace(bc_ad)])
    cfg = _cfg(exchanges=["bybit", "bestchange"])

    async def go():
        first = await p2p.scan(None, cfg)
        await asyncio.sleep(TIMEOUT * 3)
        return first, await p2p.scan(None, cfg)

    first, second = arun(go())
    assert got and len(got) == 1                                          # скачали один раз, не оборвали и не повторили
    assert "таймаут" in first.errors["bestchange/USDT"] and not [a for a in first.ads if a.ex == "BestChange"]
    assert not p2p._venue_paused_until("bestchange")                     # докачка в фоне — не ошибка площадки
    bc = [d for d in second.deals if d[2].ex == "BestChange"]
    assert bc and "bestchange/USDT" not in second.errors
    assert all(not p2p.deal_stale(d) and p2p.deal_fresh(d, second) for d in bc)   # новые данные для этого скана
    third = arun(p2p.scan(None, cfg))                              # та же выгрузка ещё раз — уже не новая
    assert not any(p2p.deal_fresh(d, third) for d in third.deals if d[2].ex == "BestChange")


def test_bestchange_background_error_raised_once_by_next_scan(monkeypatch):
    monkeypatch.setattr(p2p, "_bc", {"t": 0.0, "ads": [], "local": None})

    async def boom(s):
        raise OSError("down")

    monkeypatch.setattr(p2p, "_bc_fetch", boom)
    cfg = p2p.Config(bc_refresh=120)

    async def go():
        a = await asyncio.gather(p2p.bestchange(None, cfg, "buy", "USDT"),
                                 p2p.bestchange(None, cfg, "sell", "USDT"), return_exceptions=True)
        return a, await p2p.bestchange(None, cfg, "buy", "USDT")

    first, again = arun(go())
    assert sum(isinstance(r, OSError) for r in first) == 1 and again == []   # ошибка — один раз, повтор не раньше минуты
    assert p2p._venue_paused_until("bestchange")                          # и бэкофф — по самой выгрузке


def test_bestchange_backoff_grows_when_dump_fails_after_scan_deadline(offline, monkeypatch):
    """Выгрузка падает уже после срока скана — 4 круга подряд: пауза растёт 30 → 60 → 120 → 240 с, скан, который её
    только не дождался (и ответы из кэша), паузу не сбрасывает; удачная выгрузка — сбрасывает."""
    fail = [True]

    async def fetch(s):
        await asyncio.sleep(TIMEOUT * 1.5)
        if fail[0]:
            raise OSError("BestChange молчит")
        return b"zip"

    monkeypatch.setattr(p2p, "_bc_fetch", fetch)
    monkeypatch.setattr(p2p, "_bc_parse", lambda data: [])
    cfg = _cfg(exchanges=["bybit", "bestchange"])

    async def cycle():
        st = p2p._venue_backoff.get("bestchange")
        if st:
            st["until"] = time.time() - 1                                  # пауза прошла
        p2p._bc["tried"] = 0.0                                             # и минута после прошлой попытки
        snap = await p2p.scan(None, cfg)
        assert "bestchange/USDT" in snap.errors
        assert p2p._venue_backoff.get("bestchange", {}).get("delay", 0) == (st or {}).get("delay", 0)   # не сброшена
        await asyncio.sleep(TIMEOUT * 1.2)                                 # выгрузка упала в фоне
        return p2p._venue_backoff.get("bestchange", {}).get("delay")

    async def go():
        delays = [await cycle() for _ in range(4)]
        fail[0] = False
        await cycle()
        for _ in range(30):                                                # медленная машина: ждём итог выгрузки
            if "bestchange" not in p2p._venue_backoff:
                break
            await asyncio.sleep(0.1)
        return delays

    assert arun(go()) == [30, 60, 120, 240]
    assert "bestchange" not in p2p._venue_backoff                          # выгрузка удалась — пауза сброшена


def test_bestchange_time_is_stamped_after_parse(monkeypatch):
    """Время выгрузки — после разбора: с ним deal_fresh сверяет конец сбора прошлого скана (Snapshot.since)."""
    monkeypatch.setattr(p2p, "_bc", {"t": 0.0, "ads": [], "local": None})
    parsed = []

    async def fetch(s):
        return b"zip"

    def parse(data):
        time.sleep(0.05)
        parsed.append(time.time())
        return [p2p.Ad("BestChange", "buy", 85.0, 1, 2, 3, ["T-Bank"], "x", 1, 100.0, asset="USDT", net="TRC20")]

    monkeypatch.setattr(p2p, "_bc_fetch", fetch)
    monkeypatch.setattr(p2p, "_bc_parse", parse)
    got = arun(p2p.bestchange(None, p2p.Config(bc_refresh=120), "buy", "USDT"))
    assert got and got[0].fetched_ts >= parsed[0] and p2p._bc["t"] == got[0].fetched_ts


# --- устаревшие данные не подтверждают и не сигналят ---

def _stale_copy(d):
    profit, b, s, route = d
    return profit, b, dataclasses.replace(s, stale=True), route


def _snap(deals, ts):
    return p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, list(deals), {}, {}, {}, ts=ts)


def test_stale_side_is_never_fresh():
    d = good_deal()
    for a in (d[1], d[2]):
        a.fetched_ts = 1001.0
    assert p2p.deal_fresh(d, _snap([d], 1000.0))
    st = _stale_copy(d)
    assert not p2p.deal_fresh(st, _snap([st], 1000.0)) and p2p.deal_stale(st)
    assert not p2p.ad_fresh(dataclasses.replace(d[1], stale=True, fetched_ts=0.0), 0.0)
    stack = p2p._stack([d[1], dataclasses.replace(d[1], stale=True)], 900000)   # стек из свежего и устаревшего
    assert stack.parts == 2 and stack.stale


def test_data_arrived_between_scans_is_fresh_once():
    """Скан начался в 1000, прошлый сбор закончился в 990: данные 995 (докачались в фоне) — новые; 985 — нет."""
    d = good_deal()
    d[1].fetched_ts, d[2].fetched_ts = 1001.0, 995.0
    s = p2p.Snapshot(88.0, "t", {}, {}, [d], {}, {}, {}, ts=1000.0, since=990.0)
    assert p2p.deal_fresh(d, s)
    d[2].fetched_ts = 985.0
    assert not p2p.deal_fresh(d, s)


def test_stale_side_not_signalled_keeps_streak_and_card(monkeypatch):
    bot = _bot(monkeypatch)
    bot.live_scans = 2
    fresh = good_deal()
    key = _key(fresh)
    for ts in (1000.0, 1030.0):
        s = _snap([fresh], ts)
        bot.track_liveness(s, now=ts)
    arun(bot.notify(s))
    assert len(captions(bot)) == 1                                        # подтверждённая связка — сигнал
    bot.out.clear()
    bot.sent.clear()                                                      # антидубль не мешает — дело только в данных
    stale = _snap([_stale_copy(fresh)], 1060.0)
    bot.track_liveness(stale, now=1060.0)
    assert bot.live[key]["streak"] == 2                                   # серию не продлевает и не сбрасывает
    arun(bot.notify(stale))
    assert not bot.out                                                    # ни сигнала, ни правки, ни «⌛ устарела»
    assert [r for _, _, r in bot.signal_reasons(stale, 0)] == ["stale"]
    arun(bot.notify(_snap([fresh], 1090.0)))
    assert len(captions(bot)) == 1                                        # данные снова свежие — сигнал


def _other_deal():
    return 3.0, ad("MEXC", "buy", 87.6), ad("Bybit", "sell", 90.5), "перевод на Bybit"


def test_stale_deal_does_not_take_a_top_slot(monkeypatch):
    """Устаревшая связка выше всех в списке не занимает место MAX_SIGNALS: сигнал, прогон и ночной дайджест берут
    свежую за ней; её карточке «⌛ связка устарела» не ставим."""
    bot = _bot(monkeypatch)
    _on(monkeypatch)
    bot.max_signals = 1
    stale, fresh = _stale_copy(_other_deal()), good_deal()
    s = paper_snap_of([stale, fresh])
    assert [_key(d) for d in bot._signal_deals(s)] == [_key(fresh)]
    bot.live_msg[_key(stale)] = {"message_id": 7, "photo": True, "last_edit": 0.0, "caption": "c", "stale": False}
    arun(bot.notify(s))
    assert len(captions(bot)) == 1 and "HTX" in captions(bot)[0]          # сигнал — свежей связке
    assert not bot.live_msg[_key(stale)]["stale"]                         # карточку устаревшей не трогаем
    (cycle,) = paper.open_cycles()
    assert cycle["buy_ex"] == "HTX"                                       # прогон — тоже свежая
    reasons = {k: r for k, _d, r in bot.signal_reasons(s, 0)}
    assert reasons == {_key(stale): "stale", _key(fresh): None}
    bot.collect_night_deals(s)
    assert set(bot.night_deals) == {_key(fresh)}


def test_stale_deal_not_taken_by_paper(monkeypatch):
    _on(monkeypatch)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    st = _stale_copy(good_deal())
    arun(bot.notify(paper_snap_of([st])))
    assert not paper.open_cycles()
    arun(bot.notify(paper_snap_of([good_deal()])))
    assert len(paper.open_cycles()) == 1


def test_stale_best_does_not_fire_alert(tmp_path):
    db = str(tmp_path / "alerts.db")
    alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    ad = p2p.Ad("MEXC", "sell", 93.0, 1000, 500000, 10000, ["T-Bank"], "n", 200, 100.0, stale=True)
    snap = p2p.Snapshot(88.0, "t", {}, {("MEXC", "sell", "USDT"): ad}, [], {}, {}, {})
    assert alerts.due(snap, p2p.Config(), path=db) == []
    snap.best[("MEXC", "sell", "USDT")] = dataclasses.replace(ad, stale=False)
    assert len(alerts.due(snap, p2p.Config(), path=db)) == 1


def test_snapshot_keeps_stale_and_since_and_replay_skips_stale(offline, monkeypatch):
    """Снимок помнит пометку stale (группы) и Snapshot.since; replay помечает объявления так же и не берёт такие связки
    в сигналы, как бот. Снимок без этих ключей (до этапа 2.7) — всё свежее, since 0."""
    cfg = _cfg()
    _calm_scan(cfg)
    _slow(monkeypatch)
    snap = arun(p2p.scan(None, cfg))
    assert any(p2p.deal_stale(d) for d in snap.deals) and snap.since > 0
    scan = snapshots.load(snapshots.save(snap, cfg))
    assert scan["since"] == snap.since and {tuple(k)[0] for k in scan["stale"]} == {"HTX"}
    ads = snapshots.ads_of(scan)
    assert {a.ex for a in ads if a.stale} == {"HTX"}
    again = replay.rebuild(scan, cfg)
    assert again.since == snap.since
    assert {_key(d) for d in again.deals if p2p.deal_stale(d)} == {_key(d) for d in snap.deals if p2p.deal_stale(d)}
    sig = replay.signals(again, cfg, top=100)
    assert sig and not any(p2p.deal_stale(d) for d in sig)
    old = {k: v for k, v in scan.items() if k not in ("stale", "since")}
    assert not any(a.stale for a in snapshots.ads_of(old)) and replay.rebuild(old, cfg).since == 0.0


# --- скорость: кольцевой буфер и /status ---

def _jobs_snap(ts, jobs):
    return p2p.Snapshot(88.0, "t", {}, {}, [], {}, {}, {}, ts=ts, jobs=jobs)


def test_scan_speed_ring_buffer_percentiles():
    sp = p2p.ScanSpeed(size=10)
    assert sp.summary() is None
    for i in range(1, 13):                                              # 12 сканов, в буфере — последние 10
        ts = 1000.0 * i
        sp.add(_jobs_snap(ts, [
            {"ex": "bybit", "side": "buy", "asset": "USDT", "t0": ts, "t1": ts + 0.1 * i, "cached": False},
            {"ex": "bybit", "side": "sell", "asset": "USDT", "t0": ts, "t1": ts + 0.05, "cached": False},
            {"ex": "htx", "t0": ts, "t1": ts + 8.0, "timeout": True, "cached": True, "stale": True} if i % 5 == 0
            else {"ex": "htx", "t0": ts, "t1": ts + 0.2, "cached": False},
            {"ex": "bestchange", "t0": ts, "t1": ts + 0.001, "cached": True},          # из кэша — не запрос
            {"ex": "kucoin", "asset": "ETH", "t0": ts - 50, "t1": ts - 49, "cached": True},   # замер кэша монет
            {"ex": "networks", "t0": ts, "t1": ts, "cached": True},
            {"ex": "spot", "t0": ts, "t1": ts + 0.3}]), duration=float(i))
    got = sp.summary()
    assert got["n"] == 10 and got["scan"] == (7.0, 11.0)                 # длительности 3..12: p50 — 7, p90 — 11
    by = got["venues"]
    assert set(by) == {"bybit", "htx", "spot"}
    assert by["bybit"]["n"] == 10 and by["bybit"]["p50"] == pytest.approx(0.7) and by["bybit"]["p90"] == pytest.approx(1.1)
    assert by["htx"]["timeouts"] == 2 and by["htx"]["p90"] == pytest.approx(8.0) and by["htx"]["p50"] == pytest.approx(0.2)
    assert by["spot"]["timeouts"] == 0


def test_status_shows_speed_and_timeout_reason(offline, monkeypatch, tmp_path):
    bot = B.Bot(None, "x", "1", _cfg())
    assert "Скорость" not in bot.status_view(status_path=str(tmp_path / "none.json"))
    _slow(monkeypatch)
    for _ in range(3):
        t0 = time.time()
        bot.last = arun(p2p.scan(None, bot.cfg))
        bot.last_scan_ts, bot.last_scan_duration = time.time(), time.time() - t0
        bot.speed.add(bot.last, bot.last_scan_duration)
        p2p._venue_backoff.clear()
    text = bot.status_view(status_path=str(tmp_path / "none.json"))
    assert "Скорость" in text and "последние 3 скан" in text and f"таймаут площадки {TIMEOUT:g} с" in text
    assert "скан: p50" in text and "• HTX: p50" in text and "таймаут в 3 из 3" in text and "• Bybit: p50" in text
    assert f"htx/USDT: таймаут {TIMEOUT:g} с" in text                     # причина устаревания — в ошибках


def test_env_settings(monkeypatch):
    monkeypatch.setenv("VENUE_TIMEOUT", "5,5")
    monkeypatch.setenv("FRESH_WINDOW", "90")
    cfg = p2p.Config.from_env()
    assert cfg.venue_timeout == 5.5 and cfg.fresh_window == 90.0
    monkeypatch.setenv("VENUE_TIMEOUT", "0")
    monkeypatch.setenv("FRESH_WINDOW", "abc")
    cfg = p2p.Config.from_env()
    assert cfg.venue_timeout == p2p.VENUE_TIMEOUT_DEFAULT and cfg.fresh_window == p2p.FRESH_WINDOW_DEFAULT


# Paper assertions in this module exercise the preserved historical engine.
import pytest as _compat_pytest
pytestmark = _compat_pytest.mark.usefixtures("legacy_paper_engine")
