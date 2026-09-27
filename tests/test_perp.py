"""perp.py: разбор публичных ответов Bybit/BingX, опрос с бэкоффом, стакан, лоты, точный учёт фандинга."""
import inspect
import json
import os
import re

import pytest

import p2p
import perp
from perpfx import api, install, make_get, quote
from helpers import arun

NOW = 1790494000.0


@pytest.fixture(autouse=True)
def _clean_perp():
    perp.reset()
    yield
    perp.reset()


def test_parse_bybit_ticker_and_instrument():
    j = api()
    d = perp.parse_bybit_ticker(j["bybit_ticker_BTCUSDT"], "BTCUSDT")
    assert d["mark"] == 84477.45 and d["index"] == 84515.90 and d["last"] == 84477.20
    assert d["bid"] == 84477.10 and d["ask"] == 84477.20
    assert d["funding_rate"] == pytest.approx(0.00002264) and d["next_funding"] == 1790496000.0
    assert d["interval_h"] == 8
    inst = perp.parse_bybit_instrument(j["bybit_instr_BTCUSDT"], "BTCUSDT")
    assert inst.active and inst.lot == 0.001 and inst.min_qty == 0.001 and inst.min_notional == 5
    assert inst.interval_h == 8
    ton = perp.parse_bybit_instrument(j["bybit_instr_TONUSDT"], "TONUSDT")
    assert not ton.active and "Closed" in ton.note
    empty = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": []}}
    assert not perp.parse_bybit_instrument(empty, "TONUSDT").active


def test_parse_bybit_rejects_error_and_spot_category():
    with pytest.raises(ValueError):
        perp.parse_bybit_ticker({"retCode": 10001, "retMsg": "params error"}, "BTCUSDT")
    spot = {"retCode": 0, "result": {"category": "spot", "list": [{"symbol": "BTCUSDT", "bid1Price": "1"}]}}
    with pytest.raises(ValueError):   # спот-тикер вместо линейного — не выдавать за перп
        perp.parse_bybit_ticker(spot, "BTCUSDT")


def test_parse_bingx_contracts_premium_depth():
    j = api()
    inst = perp.parse_bingx_contracts(j["bingx_contracts"], ["BTCUSDT", "ETHUSDT", "TONUSDT"])
    assert inst["BTCUSDT"].active and inst["BTCUSDT"].lot == 0.0001 and inst["BTCUSDT"].min_notional == 2
    assert inst["BTCUSDT"].taker_fee == pytest.approx(0.05)
    assert not inst["TONUSDT"].active and "нет" in inst["TONUSDT"].note
    d = perp.parse_bingx_premium(j["bingx_premium_BTC-USDT"])
    assert d["mark"] == 84474.5 and d["funding_rate"] == pytest.approx(0.000024) and d["interval_h"] == 8
    bids, asks = perp.parse_bingx_depth(j["bingx_depth_BTC-USDT"])
    assert bids[0] == (84474.5, 0.001)                     # количество в монете (bidsCoin), не контракты
    assert [p for p, _ in asks] == sorted(p for p, _ in asks)   # аски по возрастанию, даже если пришли вразнобой
    with pytest.raises(ValueError):
        perp.parse_bingx_premium(j["bingx_premium_missing"])


def test_walk_and_lots():
    levels = ((100.0, 1.0), (101.0, 2.0))
    assert perp.walk(levels, 0.5) == 100.0
    assert perp.walk(levels, 2.0) == pytest.approx((100 + 101) / 2)
    assert perp.walk(levels, 3.5) is None                 # глубины не хватает
    assert perp.round_lot(0.00142, 0.001, 0.001) == 0.001
    assert perp.round_lot(0.00162, 0.001, 0.001) == 0.002
    assert perp.round_lot(0.0004, 0.001, 0.001) == 0.0     # меньше минимального лота
    assert perp.floor_lot(0.0199, 0.01, 0.01) == 0.01


def test_refresh_builds_quotes_and_skips_closed_symbols():
    calls = []
    errors = arun(perp.refresh(None, make_get(calls=calls), now=NOW))
    assert errors == {}
    q = perp._quotes[("Bybit", "BTCUSDT")]
    assert q.mark == 84477.45 and q.bid == 84477.10 and q.ask == 84477.20 and q.lot == 0.001
    assert q.taker_fee == pytest.approx(0.055) and q.next_funding == 1790496000.0
    bx = perp._quotes[("BingX", "BTCUSDT")]
    assert bx.funding_rate == pytest.approx(0.000024) and bx.lot == 0.0001 and bx.taker_fee == pytest.approx(0.05)
    assert bx.bids[0] == (84474.5, 0.001)
    gram = perp._quotes[("Bybit", "GRAMUSDT")]   # бывший TON: фандинг раз в 4 ч, лот 0.1
    assert gram.interval_h == 4 and gram.lot == 0.1 and gram.asset == "TON"
    gx = perp._quotes[("BingX", "GRAMTONUSDT")]   # на BingX тот же TON — GRAMTON-USDT
    assert gx.interval_h == 4 and gx.lot == 0.001 and gx.min_qty == 1.26 and gx.asset == "TON"
    assert perp.quote_for("BingX", "TON", now=gx.ts) is gx and perp.spot_for("Bybit", "TON", now=gx.ts).kind == "spot"
    assert any("premiumIndex?symbol=GRAMTON-USDT" in u for u in calls)
    assert not any("GRAM-USDT" in u for u in calls)   # BingX GRAM-USDT — другой токен, его не спрашиваем
    assert perp._spot[("Bybit", "BTCUSDT")].kind == "spot"
    assert perp.klines("Bybit", "BTCUSDT")[0][0] < perp.klines("Bybit", "BTCUSDT")[-1][0]
    assert all(u.split("/")[2] in perp.HOSTS for u in calls)
    assert perp.status()["closed"] == {}


def test_venue_symbol_table():
    assert perp.venue_symbol("Bybit", "TON") == "GRAMUSDT" and perp.venue_symbol("BingX", "TON") == "GRAMTONUSDT"
    assert perp.venue_symbol("BingX", "btc") == "BTCUSDT" and perp.bingx_symbol("GRAMTONUSDT") == "GRAMTON-USDT"
    assert perp.venue_symbols("BingX", ["BTC", "TON"]) == {"BTCUSDT": "BTC", "GRAMTONUSDT": "TON"}


def test_closed_or_offline_symbol_is_skipped_not_an_error(monkeypatch):
    """Символ закрыт (Bybit TONUSDT — Closed) или снят (BingX TONCOIN-USDT — 109418): тикер и стаканы не запрашиваем,
    ошибок нет, котировки BTC и пауза площадки не страдают."""
    monkeypatch.setitem(perp.VENUE_SYMBOLS, "TON", {"Bybit": "TONUSDT", "BingX": "TONCOINUSDT"})
    monkeypatch.setenv("PERP_ASSETS", "BTC,TON")
    calls = []
    j = api()
    errors = arun(perp.refresh(None, make_get({"bybit_spot_TONUSDT": j["bybit_spot_TONUSDT_error"]},
                                                     calls=calls), now=NOW))
    assert errors == {}
    assert ("Bybit", "TONUSDT") not in perp._quotes and ("Bybit", "BTCUSDT") in perp._quotes
    assert [u for u in calls if "TON" in u] == [
        f"{perp.BYBIT}/v5/market/instruments-info?category=linear&symbol=TONUSDT",
        f"{perp.BINGX}/openApi/swap/v2/quote/contracts?symbol=TONCOIN-USDT"]
    closed = perp.status()["closed"]
    assert "Closed" in closed[("Bybit", "TONUSDT")] and "offline" in closed[("BingX", "TONCOINUSDT")]
    assert not perp._backoff


def test_backoff_per_venue_and_recovery():
    errors = arun(perp.refresh(None, make_get(fail=("open-api.bingx.com",)), now=NOW))
    assert "BingX" in errors and ("Bybit", "BTCUSDT") in perp._quotes
    assert perp._backoff["BingX"]["delay"] == perp.BACKOFF_BASE
    calls = []
    arun(perp.refresh(None, make_get(calls=calls), now=NOW + 1))   # BingX на паузе — не опрашиваем
    assert not any("bingx" in u for u in calls)
    assert "BingX" in perp.status(now=NOW + 1)["paused"]
    arun(perp.refresh(None, make_get(), now=NOW + perp.BACKOFF_BASE + 1))
    assert "BingX" not in perp._backoff and ("BingX", "BTCUSDT") in perp._quotes


def test_refresh_if_due_interval_and_switch(monkeypatch):
    calls = []
    assert arun(perp.refresh_if_due(None, make_get(calls=calls), now=NOW)) == {}
    n = len(calls)
    assert arun(perp.refresh_if_due(None, make_get(calls=calls), now=NOW + 5)) is None
    assert len(calls) == n
    monkeypatch.setenv("PERPS", "0")
    assert arun(perp.refresh_if_due(None, make_get(calls=calls), now=NOW + 100)) is None


def test_get_refuses_other_hosts():
    with pytest.raises(ValueError):
        arun(perp._get(None, "https://api.mexc.com/api/v3/time"))   # домен разрешён guard, но не perp


def test_quote_freshness():
    install(quote(ts=NOW))
    assert perp.quote("Bybit", "BTCUSDT", now=NOW + 10) is not None
    assert perp.quote("Bybit", "BTCUSDT", now=NOW + 1000) is None
    assert perp.quote("BingX", "BTCUSDT", now=NOW) is None


def test_settle_uses_rate_seen_before_settlement():
    t = NOW + 600
    st = {}
    assert perp.settle(st, quote(rate=0.0001, mark=84000, next_funding=t, ts=NOW), now=NOW) == []
    assert perp.settle(st, quote(rate=0.0002, mark=84100, next_funding=t, ts=NOW + 300), now=NOW + 300) == []
    after = quote(rate=-0.0005, mark=85000, next_funding=t + 8 * 3600, ts=t + 20)   # после расчёта — новая ставка
    ev = perp.settle(st, after, now=t + 20)
    assert ev == [(t, 0.0002, 84100, False)]            # списана ставка из последней котировки до расчёта
    assert st["next"] == t + 8 * 3600 and st["rate"] == -0.0005
    stale = quote(rate=0.0009, mark=1.0, next_funding=t, ts=t - 5)   # старая котировка про прошедший расчёт
    assert perp.settle(st, stale, now=t + 30) == [] and st["rate"] == -0.0005


def test_settle_missed_windows_are_approx():
    t = NOW + 60
    st = {}
    perp.settle(st, quote(rate=0.0001, mark=100.0, next_funding=t, ts=NOW), now=NOW)
    ev = perp.settle(st, None, now=t + 17 * 3600)   # бот стоял 17 ч: три расчёта
    assert [e[0] for e in ev] == [t, t + 8 * 3600, t + 16 * 3600]
    assert [e[3] for e in ev] == [False, True, True]


def test_funding_windows():
    q = quote(next_funding=NOW + 3600)
    assert perp.funding_windows(q, NOW, 0.5) == 0
    assert perp.funding_windows(q, NOW, 2) == 1
    assert perp.funding_windows(q, NOW, 17) == 3


def test_scan_attaches_perps(offline):
    install(quote())
    snap = arun(p2p.scan(None, p2p.Config(assets=["USDT"], exchanges=["bybit"])))
    assert ("Bybit", "BTCUSDT") in snap.perps and snap.perps[("Bybit", "BTCUSDT")].mid == 84000.0


def test_assemble_never_reads_live_perps(offline):
    """Живые котировки кладёт только scan() после сборки: assemble чистая — replay старого снимка их не подмешивает."""
    install(quote())
    cfg = p2p.Config(assets=["USDT"], exchanges=["bybit"])
    raw = arun(p2p.collect(None, cfg))
    assert p2p.assemble(cfg, **raw).perps == {}


def test_public_get_only():
    src = inspect.getsource(perp)
    assert ".post(" not in src and "order/create" not in src and "X-BAPI-SIGN" not in src


def test_settle_marks_stale_rate_as_approx():
    t = NOW + 3600
    st = {}
    perp.settle(st, quote(rate=0.0001, mark=100.0, next_funding=t, ts=NOW), now=NOW)   # за час до расчёта
    ev = perp.settle(st, None, now=t + 5)
    assert ev == [(t, 0.0001, 100.0, True)]


def test_settle_ignores_quote_fetched_after_its_settlement():
    """Площадка ещё показывает прошедший расчёт, а ставка уже следующего периода — такой котировкой не списываем."""
    t = NOW + 600
    st = {}
    perp.settle(st, quote(rate=0.0001, mark=100.0, next_funding=t, ts=t - 20), now=t - 20)
    late = quote(rate=0.0009, mark=101.0, next_funding=t, ts=t + 5)   # получена после расчёта, время ещё старое
    assert perp.settle(st, late, now=t + 5) == [(t, 0.0001, 100.0, False)]
    assert st["next"] == t + 8 * 3600 and st["rate"] == 0.0001


def test_settle_and_windows_are_bounded_on_bad_interval():
    """Мусорный интервал (≤ 0 или доли часа) не крутит цикл: шаг — 8 ч."""
    t = NOW + 60
    for bad in (-1.0, 0.0001):
        st = {}
        perp.settle(st, quote(rate=0.0001, mark=100.0, next_funding=t, ts=NOW, interval_h=bad), now=NOW)
        assert [e[0] for e in perp.settle(st, None, now=t + 17 * 3600)] == [t, t + 8 * 3600, t + 16 * 3600]
        assert perp.funding_windows(quote(next_funding=NOW + 3600, interval_h=bad), NOW, 17) == 3
    assert perp.funding_windows(quote(next_funding=1000.0), NOW, 8) == 1   # расчёт в далёком прошлом — без цикла
    j = api()
    j["bybit_ticker_BTCUSDT"]["result"]["list"][0]["fundingIntervalHour"] = "-8"
    assert perp.parse_bybit_ticker(j["bybit_ticker_BTCUSDT"], "BTCUSDT")["interval_h"] is None


def test_env_typos_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("PERP_INTERVAL", "30s")
    monkeypatch.setenv("PERP_MAX_AGE", "nan")
    monkeypatch.setenv("PERP_ASSETS", "BTC, eth&limit=1000,ton")
    cfg = perp.settings()
    assert cfg["interval"] == 30 and cfg["max_age"] == 90
    assert cfg["assets"] == ["BTC", "TON"]   # в URL — только буквы и цифры
    monkeypatch.setenv("X_TEST_NUM", "1,5")
    assert perp.env_float("X_TEST_NUM", 2) == 1.5 and perp.env_float("X_TEST_MISSING", 2) == 2


def test_get_requires_https_allowed_host_and_no_redirect():
    for url in (perp.BYBIT.replace("https", "http") + "/v5/market/time",   # не https
                perp.BYBIT + ":8443/v5/market/time",                        # чужой порт
                perp.BYBIT.replace("://", "://x@") + "/v5/market/time"):    # userinfo
        with pytest.raises(ValueError):
            arun(perp._get(None, url))

    seen = {}

    class Resp:
        status = 301

        def raise_for_status(self):
            pass

        async def json(self, content_type=None):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, url, **kw):
            seen.update(kw)
            return Resp()

    with pytest.raises(ValueError):   # 3xx: не идём по редиректу и не читаем тело как ответ
        arun(perp._get(Session(), perp.BYBIT + "/v5/market/time"))
    assert seen["allow_redirects"] is False and seen["timeout"].total == perp.REQUEST_TIMEOUT


def test_time_sync_failure_keeps_quotes():
    errors = arun(perp.refresh(None, make_get(fail=("/v5/market/time", "server/time")), now=NOW))
    assert errors == {} and ("Bybit", "BTCUSDT") in perp._quotes and ("BingX", "BTCUSDT") in perp._quotes
    assert not perp._backoff


def test_bybit_empty_book_is_an_error_not_a_zero_quote():
    j = api()
    empty = dict(j["bybit_book_BTCUSDT"], result={"s": "BTCUSDT", "b": [], "a": []})
    errors = arun(perp.refresh(None, make_get({"bybit_book_BTCUSDT": empty}), now=NOW))
    assert "Bybit/BTCUSDT" in errors and ("Bybit", "BTCUSDT") not in perp._quotes
    assert ("Bybit", "ETHUSDT") in perp._quotes


def test_bingx_interval_inferred_from_roll_when_missing():
    """Нет fundingIntervalHours в premiumIndex: интервал — сдвиг следующего расчёта между соседними опросами."""
    j = api()
    prem = j["bingx_premium_GRAMTON-USDT"]
    prem["data"].pop("fundingIntervalHours")
    arun(perp.refresh(None, make_get({"bingx_premium_GRAMTON-USDT": prem}), now=NOW))
    assert perp._quotes[("BingX", "GRAMTONUSDT")].interval_h == 8   # пока не видно — по умолчанию
    rolled = json_copy(prem)
    rolled["data"]["nextFundingTime"] += 4 * 3600 * 1000
    arun(perp.refresh(None, make_get({"bingx_premium_GRAMTON-USDT": rolled}), now=NOW + 30))
    assert perp._quotes[("BingX", "GRAMTONUSDT")].interval_h == 4
    arun(perp.refresh(None, make_get({"bingx_premium_GRAMTON-USDT": rolled}), now=NOW + 60))
    assert perp._quotes[("BingX", "GRAMTONUSDT")].interval_h == 4   # держится до следующего сдвига


def json_copy(x):
    return json.loads(json.dumps(x))


def test_klines_refetched_right_after_hour_close_and_timestamped():
    edge = (NOW // 3600 + 1) * 3600   # ближайшее закрытие часа
    no_time = ("/v5/market/time", "server/time")   # без сдвига часов: время сервера = локальное

    def kline_calls(now):
        calls = []
        arun(perp.refresh(None, make_get(calls=calls, fail=no_time), now=now))
        return [u for u in calls if "kline" in u and "BTCUSDT" in u]

    assert kline_calls(edge - 10) and perp.kline_time("Bybit", "BTCUSDT") == edge - 10
    assert kline_calls(edge + 5)            # 15 с спустя, но час закрылся — докачали сразу
    assert perp.kline_time("Bybit", "BTCUSDT") == edge + 5
    assert not kline_calls(edge + 20)       # этот час уже есть, KLINE_TTL не прошёл


def test_every_setting_is_documented_in_env_example():
    """Каждая настройка перпов и бумажных симуляций описана в .env.example."""
    import simdirectional
    import simfunding
    import simperp
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".env.example"), encoding="utf-8") as f:
        documented = set(re.findall(r"^([A-Z_]+)=", f.read(), re.M))
    names = set()
    for mod in (perp, simperp, simfunding, simdirectional):
        names |= set(re.findall(r"(?:getenv|env_float|_on|f)\(\"([A-Z][A-Z_]+)\"", inspect.getsource(mod)))
    assert {"PERPS", "PAPER_HEDGE", "SIM_FUNDING", "SIM_DIRECTIONAL", "FUND_SPOT_FEE", "DIR_STOP_SLIP"} <= names
    assert names - documented == set()
