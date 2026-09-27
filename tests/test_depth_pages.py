"""Глубина стакана (план, этап 2.3): вторая страница, когда стек годных объявлений стороны меньше DEPTH_PAGE2 × сумма
круга; запросы под фишки сумм — только для связки карточки; бюджет DEPTH_EXTRA_MAX на скан, кэш, бэкофф, счётчик
snap.extra. Всё офлайн: фикстуры tests/fixtures (вторая страница Bybit — bybit_ads_p2.json)."""
import asyncio
import dataclasses
import os
import time

import pytest

import bot as B
import p2p
from conftest import load
from test_bot import Stub


def _cfg(**kw):
    kw.setdefault("exchanges", ["bybit"])
    kw.setdefault("assets", ["USDT"])
    return p2p.Config(**kw)


@pytest.fixture
def paged(offline, monkeypatch):
    """offline + вторая страница Bybit (сторона «бот покупает», USDT) — своя фикстура; остальные вторые страницы
    offline отдаёт как первую (выдача не сдвинулась — одни дубли). Каждый запрос — в calls, и каждый обязан входить в
    p2p.JSON_ALLOWED (тот же хост и путь, другие только параметры)."""
    calls = []

    async def fake(s, method, url, body=None):
        assert p2p.json_allowed(method, url), url
        calls.append((url, dict(body or {})))
        if "otc/item/online" in url and (body or {}).get("page") == "2" and body.get("side") == "1" \
                and body.get("tokenId") == "USDT":
            return load("bybit_ads_p2.json")
        return await offline(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", fake)
    p2p._page2_skip.clear()
    p2p._amount_cache.clear()
    yield calls
    p2p._page2_skip.clear()
    p2p._amount_cache.clear()


def _pages(calls, page="2", side=None, token="USDT"):
    return [b for u, b in calls if "otc/item/online" in u and b.get("page") == page
            and (side is None or b.get("side") == side) and b.get("tokenId") == token]


def _nicks(snap, side="buy", asset="USDT"):
    return [a.nick for a in snap.groups.get(("Bybit", side, asset), [])]


def test_page2_when_stack_short_merged_without_duplicates(paged):
    # сумма 100 000: годных покупок на первой странице 140 000 (VaSa 90 000 + FuckGG 50 000) < 1,5 × сумма
    snap = asyncio.run(p2p.scan(None, _cfg(amount=100_000)))
    assert len(_pages(paged, side="1")) == 1                    # вторая страница покупки
    assert not _pages(paged, side="0")                          # продажа (200 000 ₽) глубокая — не просим
    nicks = _nicks(snap)
    assert {"KursPlus", "NovyKurs"} <= set(nicks) and nicks.count("VaSa") == 1   # дубль сдвига выдачи отброшен
    assert snap.extra == {"page2": 1}
    (rec,) = [j for j in snap.jobs if j.get("page") == 2]
    assert rec["ex"] == "bybit" and rec["side"] == "buy" and rec["new"] == 2 and "err" not in rec
    assert p2p.usable_depth(snap.groups[("Bybit", "buy", "USDT")], _cfg(amount=100_000), snap.refs) > 140_000


def test_no_page2_when_stack_deep_enough(paged):
    snap = asyncio.run(p2p.scan(None, _cfg(amount=50_000)))    # 140 000 ≥ 75 000 и 200 000 ≥ 75 000
    assert not _pages(paged) and snap.extra == {"page2": 0}
    assert "KursPlus" not in _nicks(snap)


def test_page2_budget_shortest_side_first_and_switch_off(paged, monkeypatch):
    # сумма 150 000: коротки обе стороны (140 000 и 200 000 < 225 000), бюджет 1 — только самая короткая (покупка)
    monkeypatch.setenv("DEPTH_EXTRA_MAX", "1")
    snap = asyncio.run(p2p.scan(None, _cfg(amount=150_000)))
    assert len(_pages(paged, side="1")) == 1 and not _pages(paged, side="0") and snap.extra == {"page2": 1}
    paged.clear()
    monkeypatch.setenv("DEPTH_EXTRA_MAX", "6")
    monkeypatch.setenv("DEPTH_PAGE2", "0")                       # выключено
    snap = asyncio.run(p2p.scan(None, _cfg(amount=150_000)))
    assert not _pages(paged) and snap.extra == {"page2": 0}
    monkeypatch.setenv("DEPTH_PAGE2", "мусор")                   # кривое значение — по умолчанию 1.5
    assert p2p.depth_settings() == {"page2": 1.5, "extra_max": 6}


def test_page2_without_new_ads_is_not_repeated(paged):
    """Вторая страница продажи — одни дубли первой: следующие DEPTH_PAGE2_RETRY сек её не просим; покупки дала новые
    объявления — просим каждый скан, пока стек короткий."""
    asyncio.run(p2p.scan(None, _cfg(amount=150_000)))
    assert len(_pages(paged, side="1")) == 1 and len(_pages(paged, side="0")) == 1
    assert ("bybit", "sell", "USDT") in p2p._page2_skip and ("bybit", "buy", "USDT") not in p2p._page2_skip
    paged.clear()
    snap = asyncio.run(p2p.scan(None, _cfg(amount=150_000)))
    assert len(_pages(paged, side="1")) == 1 and not _pages(paged, side="0") and snap.extra == {"page2": 1}


def test_page2_error_keeps_page1_and_backs_off_venue(paged, monkeypatch):
    inner = p2p._json

    async def broken(s, method, url, body=None):
        if (body or {}).get("page") == "2":
            raise TimeoutError("page 2")
        return await inner(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", broken)
    snap = asyncio.run(p2p.scan(None, _cfg(amount=100_000)))
    assert "VaSa" in _nicks(snap) and "KursPlus" not in _nicks(snap)   # первая страница в скане
    assert not snap.errors                                             # площадка ответила — не «ошибка площадки»
    assert [j["err"] for j in snap.jobs if j.get("page") == 2] == ["TimeoutError: page 2"]
    assert p2p._venue_paused_until("bybit")                            # бэкофф, как у любой ошибки площадки
    paged.clear()
    snap = asyncio.run(p2p.scan(None, _cfg(amount=100_000)))
    assert not _pages(paged, page="1") and not _pages(paged) and "пауза до" in snap.errors["bybit"]   # на паузе


def test_page2_for_other_coins_only_with_their_poll(paged):
    """Монеты кроме USDT опрашиваются раз в ALT_INTERVAL — их вторая страница тоже, и кэшируется вместе с первой."""
    cfg = _cfg(amount=1_000_000, assets=["USDT", "BTC"])          # всё коротко
    asyncio.run(p2p.scan(None, cfg))
    assert _pages(paged, token="BTC")                               # опрос BTC идёт — и вторая страница
    paged.clear()
    snap = asyncio.run(p2p.scan(None, cfg))                         # BTC из кэша _alt — запросов по нему нет
    assert not [b for u, b in paged if b.get("tokenId") == "BTC"]
    assert _pages(paged, side="1")                                  # USDT опрашивается каждый скан
    assert [a for a in snap.ads if a.asset == "BTC"]               # объявления BTC — из кэша вместе со второй страницей


def test_paged_urls_stay_in_allowlist(monkeypatch):
    """Номер страницы — параметр запроса, путь тот же: адреса второй страницы входят в JSON_ALLOWED (пин владельца)."""
    captured = []

    async def grab(s, method, url, body=None):
        captured.append((method, url))
        return {}

    cfg = p2p.Config()
    monkeypatch.setattr(p2p, "_json", grab)
    try:
        for n in ("htx", "kucoin", "mexc", "bitpapa", "lbank"):
            if n == "mexc":
                p2p._mexc_pay.setdefault("1", "x")
                p2p._mexc_coins.setdefault("USDT", "id")
            try:
                asyncio.run(p2p.FETCHERS[n](None, cfg, "buy", "USDT", page=2))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass   # пустой ответ — адаптеру разбирать нечего; важен сам адрес
    finally:
        p2p._mexc_pay.clear()
        p2p._mexc_coins.clear()
    assert len(captured) == 5
    for method, url in captured:
        assert p2p.json_allowed(method, url), url
        assert any(f"{k}=2" in url for k in ("page", "currPage", "pageNo")), url


# --- фишки сумм (50/100/300 тыс.) — только для связки карточки ---

def _amount_fake(paged_calls, offline, monkeypatch, delay=0.0, fail=False, log=None):
    """Bybit под сумму 300 000 отдаёт крупные объявления (покупка от 200 000, продажа до 400 000) — их нет в выдаче
    под сумму круга; остальные суммы — как offline. delay — площадка отвечает не сразу, fail — ответ с ошибкой,
    log — список, куда пишется ("chip_done", сторона), когда ответ под сумму пришёл."""
    big_buy = {"result": {"items": [{"price": "85.10", "minAmount": "200000", "maxAmount": "400000",
                                     "lastQuantity": "5000", "payments": ["14"], "nickName": "BigSeller",
                                     "recentOrderNum": 900, "recentExecuteRate": 100, "remark": ""}]}}
    big_sell = {"result": {"items": [{"price": "89.80", "minAmount": "100000", "maxAmount": "400000",
                                      "lastQuantity": "5000", "payments": ["14"], "nickName": "BigBuyer",
                                      "recentOrderNum": 900, "recentExecuteRate": 100, "remark": ""}]}}
    inner = p2p._json

    async def fake(s, method, url, body=None):
        if "otc/item/online" in url and (body or {}).get("amount") == "300000":
            paged_calls.append((url, dict(body)))
            if delay:
                await asyncio.sleep(delay)
            if fail:
                raise TimeoutError("amount")
            if log is not None:
                log.append(("chip_done", body.get("side")))
            return big_buy if body.get("side") == "1" else big_sell
        return await inner(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", fake)


def _amount_calls(calls, amount):
    return [b for u, b in calls if "otc/item/online" in u and b.get("amount") == str(amount)]


def test_amount_chips_fetch_only_for_card_deal_with_cache(paged, offline, monkeypatch):
    cfg = _cfg(amount=100_000)
    snap = asyncio.run(p2p.scan(None, cfg))
    deal = snap.deals[0]
    assert deal[1].ex == "Bybit" and deal[2].ex == "Bybit"
    assert not _amount_calls(paged, 300000)                         # скан под фишки ничего не просит
    assert p2p.deal_amounts(deal, cfg, snap)[300_000] is None       # на 300 000 глубины скана не хватает
    _amount_fake(paged, offline, monkeypatch)
    before, n100 = snap.extra["page2"], len(_amount_calls(paged, 100000))
    richer = asyncio.run(p2p.depth_for_deal(object(), cfg, snap, deal))
    assert len(_amount_calls(paged, 300000)) == 2 and len(_amount_calls(paged, 50000)) == 2   # обе стороны
    assert len(_amount_calls(paged, 100000)) == n100                # сумма круга — уже в скане, не просим
    assert snap.extra == {"page2": before, "amounts": 4}
    assert p2p.deal_amounts(deal, cfg, richer)[300_000] is not None  # крупные объявления закрыли фишку
    assert _nicks(richer).count("VaSa") == 1                         # ответ под 50 000 — те же объявления, без дублей
    assert "BigSeller" not in _nicks(snap)                           # снимок скана не меняется
    n = len(paged)
    again = asyncio.run(p2p.depth_for_deal(object(), cfg, snap, deal))   # кэш на INTERVAL — без запросов
    assert len(paged) == n and snap.extra["amounts"] == 4 and "BigSeller" in _nicks(again)


def test_amount_chips_budget_pause_error_and_other_venues(paged, offline, monkeypatch):
    cfg = _cfg(amount=100_000)
    snap = asyncio.run(p2p.scan(None, cfg))                         # page2 = 1 (покупка короткая)
    deal = snap.deals[0]
    _amount_fake(paged, offline, monkeypatch)
    monkeypatch.setenv("DEPTH_EXTRA_MAX", "2")                      # на фишки остаётся 1 запрос
    asyncio.run(p2p.depth_for_deal(object(), cfg, snap, deal))
    assert snap.extra == {"page2": 1, "amounts": 1}
    monkeypatch.setenv("DEPTH_EXTRA_MAX", "6")
    p2p._amount_cache.clear()
    assert asyncio.run(p2p.depth_for_deal(None, cfg, snap, deal)) is snap          # без сессии — без запросов
    p2p._venue_backoff["bybit"] = {"delay": 30, "until": time.time() + 30}        # площадка на паузе
    n = len(paged)
    assert asyncio.run(p2p.depth_for_deal(object(), cfg, snap, deal)) is snap and len(paged) == n
    p2p._venue_backoff.clear()
    inner = p2p._json

    async def broken(s, method, url, body=None):
        if (body or {}).get("amount") == "300000":
            raise TimeoutError("amount")
        return await inner(s, method, url, body)

    monkeypatch.setattr(p2p, "_json", broken)
    asyncio.run(p2p.depth_for_deal(object(), cfg, dataclasses.replace(snap, extra={}), deal))
    assert p2p._venue_paused_until("bybit")                         # ошибка — бэкофф площадки
    # площадки, которые не отбирают выдачу по сумме (KuCoin, BitPapa, LBank, BestChange), под фишки не опрашиваются
    kb = p2p.Ad("KuCoin", "buy", 85.0, 1000, 500000, 10000, ["SBP"], "k", 500, 99.0)
    ks = p2p.Ad("BestChange", "sell", 90.0, 1000, 500000, 10000, ["SBP"], "x [TRC20]", 500, 99.0, net="TRC20")
    p2p._venue_backoff.clear()
    n = len(paged)
    other = asyncio.run(p2p.depth_for_deal(object(), _cfg(amount=100_000, exchanges=["kucoin", "bestchange"]),
                                           snap, (3.0, kb, ks, "r")))
    assert other is snap and len(paged) == n


class CardBot(Stub):
    """Бот без сети, карточка «ушла» с message_id — как настоящая отправка."""
    async def send_photo(self, png, caption, markup=None, **kw):
        self.out.append(("sendPhoto", {"caption": caption}))
        return {"ok": True, "result": {"message_id": 77}}


def _card_bot(cfg, snap, monkeypatch):
    captured = {}
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: captured.update(a=a) or b"png")
    bot = CardBot(cfg)
    bot.s = object()                                                 # сессия есть — запросы под фишки идут
    bot.last = snap
    return bot, captured


def _send_and_wait(bot, deal, snap):
    """Отправить карточку; вернуть, что было в bot.out сразу после отправки, — потом дождаться фоновых задач."""
    async def run():
        t0 = time.monotonic()
        await bot.send_deal(deal, snap=snap, topic="signals")
        right_after = ([m for m, _ in bot.out], time.monotonic() - t0)
        await asyncio.gather(*list(bot.chip_tasks), return_exceptions=True)
        return right_after
    return asyncio.run(run())


def test_card_is_sent_before_chip_requests_and_edited_later(paged, offline, monkeypatch):
    cfg = _cfg(amount=100_000)
    snap = asyncio.run(p2p.scan(None, cfg))
    deal = snap.deals[0]
    bot, captured = _card_bot(cfg, snap, monkeypatch)
    _amount_fake(paged, offline, monkeypatch, delay=0.3, log=bot.out)   # площадка под суммы отвечает медленно
    (methods, took) = _send_and_wait(bot, deal, snap)
    assert methods == ["sendPhoto"] and took < 0.3                  # карточка ушла, ни один запрос под суммы не ответил
    assert captured["a"][300_000] is None                            # на картинке — фишки по стакану скана
    methods = [m for m, _ in bot.out]
    assert methods.index("sendPhoto") < methods.index("chip_done") < methods.index("editMessageCaption")
    edit = [p for m, p in bot.out if m == "editMessageCaption"][0]
    assert edit["message_id"] == 77 and edit["caption"].startswith(bot.live_msg[bot._deal_key(deal)]["caption"][:40])
    line = edit["caption"].split("\n")[-1]
    assert line.startswith("📏 На другую сумму:") and "300 000 ₽ +" in line and "нет объёма" not in line
    assert bot.live_msg[bot._deal_key(deal)]["chips"] == line        # живая правка строку не потеряет
    bot.live_msg[bot._deal_key(deal)]["last_edit"] = 0
    asyncio.run(bot.update_live_card(bot._deal_key(deal), deal, snap, time.time()))
    assert [p for m, p in bot.out if m == "editMessageCaption"][-1]["caption"].endswith(line)
    assert "вторые страницы 1, под суммы 4 (лимит 6)" in bot.status_view()


@pytest.mark.parametrize("mode", ["fail", "timeout"])
def test_chip_failure_or_timeout_leaves_original_card(paged, offline, monkeypatch, mode):
    cfg = _cfg(amount=100_000)
    snap = asyncio.run(p2p.scan(None, cfg))
    bot, _ = _card_bot(cfg, snap, monkeypatch)
    if mode == "timeout":
        monkeypatch.setenv("CHIP_DEPTH_TIMEOUT", "0.05")
        _amount_fake(paged, offline, monkeypatch, delay=1.0, log=bot.out)
    else:
        _amount_fake(paged, offline, monkeypatch, fail=True, log=bot.out)
    _send_and_wait(bot, snap.deals[0], snap)
    assert [m for m, _ in bot.out] == ["sendPhoto"]                 # правки нет, карточка как была
    assert "chips" not in bot.live_msg[bot._deal_key(snap.deals[0])]


def test_chip_timeout_setting_and_line():
    assert B.chip_depth_timeout() == 4.0
    for bad in ("0", "-1", "nan", "мусор"):
        os.environ["CHIP_DEPTH_TIMEOUT"] = bad
        assert B.chip_depth_timeout() == 4.0
    os.environ["CHIP_DEPTH_TIMEOUT"] = "2.5"
    assert B.chip_depth_timeout() == 2.5
    assert B.chips_line({50_000: 1.234, 300_000: None}) == "📏 На другую сумму: 50 000 ₽ +1.23% · 300 000 ₽ нет объёма"
