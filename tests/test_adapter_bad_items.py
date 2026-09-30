"""_parse_ads: одно битое объявление у bybit/htx/kucoin/mexc/bitpapa не роняет весь адаптер (раньше price=None,
пропущенный ключ или inf в числовом поле роняли всю площадку в venue_failed/backoff); price nan/inf/0/отрицательная
отбрасывается молча; если не разобралось ни одного объявления — площадка по-прежнему падает (смена схемы API)."""
import copy

import pytest

import p2p
from helpers import arun

# площадка -> кусок URL адреса со списком объявлений (не справочников/кэшей), путь до списка в JSON, ключ цены
VENUES = {
    "bybit": {"url": "otc/item/online", "path": ("result", "items"), "price_key": "price"},
    "htx": {"url": "trade-market", "path": ("data",), "price_key": "price"},
    "kucoin": {"url": "_api/otc/ad/list", "path": ("items",), "price_key": "floatPrice"},
    "mexc": {"url": "p2p.mexc.com/api/market", "path": ("data",), "price_key": "price"},
    "bitpapa": {"url": "bitpapa.com/api/v1/pro/search", "path": ("ads",), "price_key": "price"},
}


def _items(j, path):
    for k in path:
        j = j[k]
    return j


def _patch(monkeypatch, offline, venue, edit):
    """Оборачивает fake_json (offline): для запроса списка объявлений venue применяет edit(items) к копии JSON,
    остальные запросы (справочники банков/монет, спот, Rapira) идут как раньше — прелоады _bybit_pay/_mexc_pay/
    _mexc_coins не ломаются."""
    cfg = VENUES[venue]

    async def wrapped(s, method, url, body=None):
        j = await offline(s, method, url, body)
        if cfg["url"] in url:
            j = copy.deepcopy(j)
            edit(_items(j, cfg["path"]))
        return j

    monkeypatch.setattr(p2p, "_json", wrapped)


def _fetch(venue, side="buy", asset="USDT"):
    return arun(p2p.FETCHERS[venue](None, p2p.Config(), side, asset))


@pytest.mark.parametrize("venue", list(VENUES))
def test_one_bad_item_among_several_is_skipped(offline, monkeypatch, venue):
    price_key = VENUES[venue]["price_key"]

    def edit(items):
        assert len(items) >= 2, venue
        items[0][price_key] = None   # float(None) -> TypeError, объявление битое

    _patch(monkeypatch, offline, venue, edit)
    ads = _fetch(venue)
    assert ads   # остальные объявления разобраны без исключения
    assert p2p.ADS_SKIPPED[(venue, "buy", "USDT")] == 1


@pytest.mark.parametrize("venue", list(VENUES))
@pytest.mark.parametrize("bad_price", [0, -5, "nan", "inf"])
def test_bad_price_is_dropped(offline, monkeypatch, venue, bad_price):
    price_key = VENUES[venue]["price_key"]

    def edit(items):
        items[0][price_key] = bad_price

    _patch(monkeypatch, offline, venue, edit)
    ads = _fetch(venue)
    assert p2p.ADS_SKIPPED[(venue, "buy", "USDT")] == 1
    assert all(a.price > 0 for a in ads)


@pytest.mark.parametrize("venue", list(VENUES))
def test_clean_response_unchanged_and_zero_skipped(offline, venue):
    ads = _fetch(venue)
    assert ads
    assert p2p.ADS_SKIPPED[(venue, "buy", "USDT")] == 0
    assert all(a.price > 0 for a in ads)


@pytest.mark.parametrize("venue", list(VENUES))
def test_all_bad_items_raise_value_error(offline, monkeypatch, venue):
    price_key = VENUES[venue]["price_key"]

    def edit(items):
        for it in items:
            it[price_key] = None

    _patch(monkeypatch, offline, venue, edit)
    with pytest.raises(ValueError, match=venue):
        _fetch(venue)


def test_missing_required_key_is_skipped_not_raised(offline, monkeypatch):
    def edit(items):
        del items[0]["nickName"]   # обязательный ключ пропал — KeyError на этом объявлении

    _patch(monkeypatch, offline, "bybit", edit)
    ads = _fetch("bybit")
    assert ads
    assert p2p.ADS_SKIPPED[("bybit", "buy", "USDT")] == 1


def test_inf_in_integer_field_is_skipped_not_raised(offline, monkeypatch):
    def edit(items):
        items[0]["recentOrderNum"] = float("inf")   # int(inf) -> OverflowError

    _patch(monkeypatch, offline, "bybit", edit)
    ads = _fetch("bybit")
    assert ads
    assert p2p.ADS_SKIPPED[("bybit", "buy", "USDT")] == 1


def test_bitpapa_is_suspicious_filtered_not_counted_as_bad(offline, monkeypatch):
    def edit(items):
        items[0]["user"]["is_suspicious"] = True

    _patch(monkeypatch, offline, "bitpapa", edit)
    ads = _fetch("bitpapa")
    assert len(ads) == 3   # четвёртый (суспишн) молча отфильтрован
    assert p2p.ADS_SKIPPED[("bitpapa", "buy", "USDT")] == 0   # намеренный фильтр — не битое объявление


@pytest.mark.parametrize("venue", list(VENUES))
def test_collect_one_bad_item_does_not_fail_venue(offline, monkeypatch, venue):
    price_key = VENUES[venue]["price_key"]

    def edit(items):
        items[0][price_key] = None

    _patch(monkeypatch, offline, venue, edit)
    cfg = p2p.Config(assets=["USDT"], exchanges=[venue])
    raw = arun(p2p.collect(None, cfg))
    assert f"{venue}/USDT" not in raw["errors"]
    assert venue not in p2p._venue_backoff


@pytest.mark.parametrize("venue", list(VENUES))
def test_collect_all_bad_items_fails_venue_with_backoff(offline, monkeypatch, venue):
    price_key = VENUES[venue]["price_key"]

    def edit(items):
        for it in items:
            it[price_key] = None

    _patch(monkeypatch, offline, venue, edit)
    cfg = p2p.Config(assets=["USDT"], exchanges=[venue])
    raw = arun(p2p.collect(None, cfg))
    assert f"{venue}/USDT" in raw["errors"]
    assert venue in p2p._venue_backoff
