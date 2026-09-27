"""research/data.py: постраничная загрузка, кеш, повторы, отрезки TON/GRAM — без сети (подменный getter)."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import pytest

from research import data

H = data.HOUR_MS
T0 = 1_700_000_000_000 // H * H          # сетка часов
NOW = (T0 + 5000 * H + 1800_000) / 1000  # «сейчас» посреди часа: последняя свеча ещё не закрыта


class FakeApi:
    """Имитация публичных GET Bybit/BingX по синтетическим рядам (новые записи сверху, как у бирж)."""

    def __init__(self, funding_h=8, bybit_err=None, fail_first=0, contracts=None, instruments=None):
        self.calls = []
        self.funding_h, self.bybit_err, self.fail_first = funding_h, bybit_err, fail_first
        self.contracts = contracts if contracts is not None else [{"symbol": "BTC-USDT", "size": "0.0001", "tradeMinQuantity": 0.0001,
                                        "tradeMinUSDT": 2, "takerFeeRate": 0.0005, "launchTime": 0, "status": 1}]
        self.instruments = instruments or {}

    @staticmethod
    def price(ts):
        return 100.0 + (ts - T0) / H * 0.01

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.fail_first:
            self.fail_first -= 1
            raise urllib.error.URLError("boom")
        u = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/v5/market/kline":
            if self.bybit_err:
                return json.dumps({"retCode": self.bybit_err, "retMsg": "x"}).encode()
            s, e = int(q["start"]), int(q["end"])
            rows = [[str(t), str(self.price(t)), str(self.price(t) + 1), str(self.price(t) - 1),
                     str(self.price(t + H)), "5", "0"] for t in range(s, e + 1, H)
                    if T0 <= t <= T0 + 5000 * H][::-1][:int(q["limit"])]
            return json.dumps({"retCode": 0, "result": {"list": rows}}).encode()
        if u.path == "/v5/market/funding/history":
            s, e = int(q["startTime"]), int(q["endTime"])
            step = self.funding_h * H
            first = -(-max(s, T0) // step) * step
            rows = [{"symbol": q["symbol"], "fundingRate": "0.0001", "fundingRateTimestamp": str(t)}
                    for t in range(first, min(e, T0 + 5000 * H) + 1, step)][::-1][:int(q["limit"])]
            return json.dumps({"retCode": 0, "result": {"list": rows}}).encode()
        if u.path == "/v5/market/instruments-info":
            inst = self.instruments.get(q["symbol"])
            return json.dumps({"retCode": 0, "result": {"list": [inst] if inst else []}}).encode()
        if u.path == "/openApi/swap/v3/quote/klines":
            s, e = int(q["startTime"]), int(q["endTime"])
            rows = [{"open": str(self.price(t)), "close": str(self.price(t + H)), "high": str(self.price(t) + 1),
                     "low": str(self.price(t) - 1), "volume": "1", "time": t}
                    for t in range(s, e + 1, H) if T0 + 4000 * H <= t <= T0 + 5000 * H][::-1][:int(q["limit"])]
            return json.dumps({"code": 0, "data": rows}).encode()
        if u.path == "/openApi/swap/v2/quote/fundingRate":
            s, e = int(q["startTime"]), int(q["endTime"])
            step = self.funding_h * H
            first = -(-max(s, T0) // step) * step
            rows = [{"symbol": q["symbol"], "fundingRate": "0.0002", "fundingTime": t, "markPrice": str(self.price(t))}
                    for t in range(first, min(e, T0 + 5000 * H) + 1, step)][::-1][:int(q["limit"])]
            return json.dumps({"code": 0, "data": rows}).encode()
        if u.path == "/openApi/swap/v2/quote/contracts":
            return json.dumps({"code": 0, "data": self.contracts}).encode()
        raise AssertionError(url)


def _http(tmp_path, api, **kw):
    return data.Http(cache_dir=str(tmp_path / "cache"), getter=api, clock=lambda: NOW, sleep=lambda s: None, **kw)


def test_bybit_klines_sorted_complete_closed_only_and_cached(tmp_path):
    api = FakeApi()
    http = _http(tmp_path, api)
    rows = data.bybit_klines(http, "linear", "BTCUSDT", T0, T0 + 6000 * H)
    ts = [r[0] for r in rows]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    assert ts[0] == T0 and ts[-1] == T0 + 4999 * H          # свеча T0+5000h ещё идёт — её нет
    assert rows[0][1:5] == [100.0, 101.0, 99.0, FakeApi.price(T0 + H)]
    n = len(api.calls)
    assert data.bybit_klines(http, "linear", "BTCUSDT", T0, T0 + 6000 * H) == rows
    # закрытые окна — из кеша; окно с текущим часом живёт час, но часы стоят — тоже кеш
    assert len(api.calls) == n and http.cache_hits >= n


def test_windows_are_on_a_fixed_grid(tmp_path):
    """Разные start дают одни и те же адреса окон — второй запуск с другой датой начала берёт кеш."""
    api = FakeApi()
    http = _http(tmp_path, api)
    data.bybit_klines(http, "spot", "BTCUSDT", T0 + 17 * H, T0 + 3000 * H)
    n = len(api.calls)
    data.bybit_klines(http, "spot", "BTCUSDT", T0 + 5 * H, T0 + 2900 * H)
    assert len(api.calls) == n


def test_live_window_refetched_after_ttl(tmp_path):
    api = FakeApi()
    clock = {"t": NOW}
    http = data.Http(cache_dir=str(tmp_path), getter=api, clock=lambda: clock["t"], sleep=lambda s: None)
    data.bybit_klines(http, "linear", "BTCUSDT", T0 + 4990 * H, T0 + 5000 * H)
    n = len(api.calls)
    clock["t"] += data.LIVE_TTL + 1
    data.bybit_klines(http, "linear", "BTCUSDT", T0 + 4990 * H, T0 + 5000 * H)
    assert len(api.calls) == n + 1


def test_funding_window_split_when_limit_hit(tmp_path):
    api = FakeApi(funding_h=1)      # часовой фандинг: 60 дней = 1440 записей > лимита 200
    http = _http(tmp_path, api)
    rows = data.bybit_funding(http, "TONUSDT", T0, T0 + 3000 * H)
    ts = [r[0] for r in rows]
    assert ts == list(range(T0, T0 + 3000 * H, H))
    assert all(r[1] == 0.0001 for r in rows)
    brows = data.bingx_funding(http, "BTC-USDT", T0, T0 + 3000 * H)
    assert [r[0] for r in brows] == ts and brows[5][2] == FakeApi.price(T0 + 5 * H)


def test_retry_on_network_error_then_success(tmp_path):
    api = FakeApi(fail_first=2)
    slept = []
    http = data.Http(cache_dir=str(tmp_path), getter=api, clock=lambda: NOW, sleep=slept.append)
    rows = data.bybit_klines(http, "linear", "BTCUSDT", T0, T0 + 10 * H)
    assert len(rows) == 10 and http.requests == 3
    assert slept.count(2.0) == 1 and slept.count(4.0) == 1   # экспоненциальная пауза


def test_venue_error_is_not_retried(tmp_path):
    api = FakeApi(bybit_err=10001)
    http = _http(tmp_path, api)
    with pytest.raises(data.DataError, match="10001"):
        data.bybit_klines(http, "spot", "TONUSDT", T0, T0 + 10 * H)
    assert len(api.calls) == 1


def test_rate_limit_code_is_retried_then_gives_up(tmp_path):
    api = FakeApi(bybit_err=10006)
    http = _http(tmp_path, api, retries=2)
    with pytest.raises(data.DataError, match="3 попыток"):
        data.bybit_klines(http, "spot", "BTCUSDT", T0, T0 + 10 * H)
    assert len(api.calls) == 3


def test_http_404_not_retried(tmp_path):
    def getter(url, timeout):
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    http = data.Http(cache_dir=str(tmp_path), getter=getter, clock=lambda: NOW, sleep=lambda s: None)
    with pytest.raises(data.DataError, match="HTTP 404"):
        http.get_json(data.BYBIT, "/v5/market/time", {})


def test_offline_mode_uses_cache_only(tmp_path):
    api = FakeApi()
    data.bybit_klines(_http(tmp_path, api), "linear", "BTCUSDT", T0, T0 + 10 * H)
    off = data.Http(cache_dir=str(tmp_path / "cache"), getter=None, offline=True, clock=lambda: NOW + 10**6)
    assert len(data.bybit_klines(off, "linear", "BTCUSDT", T0, T0 + 10 * H)) == 10
    with pytest.raises(data.DataError, match="offline"):
        data.bybit_klines(off, "linear", "ETHUSDT", T0, T0 + 10 * H)


def test_real_response_formats_parse(tmp_path):
    """Урезанные живые ответы (tests/fixtures/research_*.json) разбираются в [ts, o, h, l, c, v] / [ts, rate]."""
    fx = os.path.join(os.path.dirname(__file__), "fixtures")

    def load(name):
        with open(os.path.join(fx, name), encoding="utf-8") as f:
            return f.read().encode()

    def getter(url, timeout):
        for part, name in (("/v5/market/kline", "research_bybit_kline.json"),
                           ("/v5/market/funding", "research_bybit_funding.json"),
                           ("/swap/v3/quote/klines", "research_bingx_klines.json"),
                           ("/swap/v2/quote/fundingRate", "research_bingx_funding.json")):
            if part in url:
                return load(name)
        raise AssertionError(url)

    http = data.Http(cache_dir=str(tmp_path), getter=getter, clock=lambda: 1_790_500_000, sleep=lambda s: None)
    k = data.bybit_klines(http, "linear", "TONUSDT", 1781506800000, 1781514000000)
    assert k == [[1781506800000, 1.7916, 1.8228, 1.7742, 1.8118, 979438.1],
                 [1781510400000, 1.8118, 1.8118, 1.7517, 1.7917, 981308.5]]
    f = data.bybit_funding(http, "TONUSDT", 1781481600000, 1781510400001)
    assert f == [[1781481600000, -0.00011106], [1781496000000, 0.00005], [1781510400000, -0.000604]]
    bk = data.bingx_klines(http, "BTC-USDT", 1790488800000, 1790496000000)
    assert [r[0] for r in bk] == [1790488800000, 1790492400000] and bk[1][4] == 84474.6
    bf = data.bingx_funding(http, "BTC-USDT", 1790409600000, 1790467200001)
    assert bf[0] == [1790409600000, 0.000071, 84078.1] and len(bf) == 3


def test_load_dataset_segments_follow_listing_and_delivery(tmp_path):
    """TON: TONUSDT до поставки, GRAMUSDT с запуска; нет контракта BingX — пометка, а не падение."""
    inst = {
        "TONUSDT": {"symbol": "TONUSDT", "status": "Closed", "launchTime": str(T0 - 10 * H),
                    "deliveryTime": str(T0 + 2000 * H), "fundingInterval": 240,
                    "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"}},
        "GRAMUSDT": {"symbol": "GRAMUSDT", "status": "Trading", "launchTime": str(T0 + 2100 * H + 5),
                     "deliveryTime": "0", "fundingInterval": 240,
                     "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"}},
    }
    api = FakeApi(instruments=inst, contracts=[])
    ds = data.load_dataset(_http(tmp_path, api), T0, T0 + 9000 * H, coins=("TON",), log=lambda *a: None)
    segs = ds["coins"]["TON"]["perp"]
    assert [s["symbol"] for s in segs] == ["TONUSDT", "GRAMUSDT"]
    assert segs[0]["start"] == T0 and segs[0]["end"] == T0 + 2000 * H
    assert segs[0]["klines"][-1][0] == T0 + 1999 * H
    assert segs[1]["start"] == T0 + 2101 * H and segs[1]["spec"]["qty_step"] == 0.1
    assert ds["coins"]["TON"]["spot"]["symbol"] == "GRAMUSDT" and ds["coins"]["TON"]["bingx"] == []
    assert any("GRAMTON-USDT" in n for n in ds["meta"]["notes"])
    cov = data.coverage(ds)["TON"]
    assert cov["perp"][0]["klines"]["n"] == 2000 and cov["perp"][0]["klines"]["missing_steps"] == 0


def test_opener_ignores_windows_registry_proxy(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "socks4://127.0.0.1:10808"})
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    handlers = [h for h in data._opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert all(h.proxies == {} for h in handlers)      # пустой ProxyHandler в цепочку не встаёт — прямое соединение
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"https": "env-proxy-marker"})
    handlers = [h for h in data._opener().handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert [h.proxies for h in handlers] == [{"https": "env-proxy-marker"}]
