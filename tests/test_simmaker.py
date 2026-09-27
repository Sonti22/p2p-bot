"""Бумажный мейкер (simmaker): место в очереди, слабый прокси исполнения на синтетических стаканах, P&L и
комиссии кругов, оценка позиции по ориентиру, коридор цены, выключен по умолчанию, отчёт только владельцу.
Всё офлайн: только снимки, собранные в тесте, база — во временной папке."""
import asyncio
import dataclasses

import pytest

import bot as B
import p2p
import simmaker as S
from helpers import make_ad

REF = 90.0
FEE = p2p.MAKER_FEE["Bybit"]["buy_ad"]   # 0.3% с объявления на покупку


def ad(side, price, nick, avail=1000.0, max_amt=500000):
    return dataclasses.replace(make_ad("Bybit", side, price, avail=avail, max_amt=max_amt), nick=nick)


def books(*ads):
    """(ex, side, asset) -> объявления, отсортированные как в scan(): продавцы по возрастанию, покупатели по убыванию."""
    g = {}
    for a in ads:
        g.setdefault((a.ex, a.side, a.asset), []).append(a)
    for key, grp in g.items():
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
    return g


def snap(ads, hidden=(), ref=REF):
    """Снимок: groups — прошедшие свои фильтры, book — вся выдача (плюс `hidden`, отсеянные своими фильтрами)."""
    return p2p.Snapshot(ref, "t", {"USDT": ref}, {}, [], {}, {}, {}, books(*ads), {}, frozenset(),
                        books(*ads, *hidden))


def market(b1=1000.0, b2=500.0, b3=800.0, s1=1000.0, s2=600.0, drop_s2=False):
    """Синтетический стакан Bybit USDT: покупатели монеты (очередь своего объявления на покупку) 89.50/89.40/89.00,
    продавцы (очередь объявления на продажу) 90.50/90.60. Цены мейкера: купить 89.51, продать 90.49."""
    ads = [ad("sell", 89.50, "B1", b1), ad("sell", 89.40, "B2", b2), ad("sell", 89.00, "B3", b3),
           ad("buy", 90.50, "S1", s1)]
    if not drop_s2:
        ads.append(ad("buy", 90.60, "S2", s2))
    return ads


def cfg_env(monkeypatch, **env):
    base = {"SIM_MAKER": "1", "SIM_MAKER_AMOUNT": "9000"}   # лот 9000 ₽ / ориентир 90 = 100 USDT
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    return S.settings(p2p.Config())


def run(snaps, s, t0=1000.0, dt=20.0):
    """Прогнать шаги по последовательности снимков, собрать исполнения и круги."""
    st, fills, rounds = S._new_state(t0), [], []
    for i, sn in enumerate(snaps):
        f, r = S.step(st, sn, "Bybit", "USDT", s, t0 + i * dt)
        fills += f
        rounds += r
    return st, fills, rounds


# --- выключен по умолчанию ---

def test_off_by_default_and_defaults(monkeypatch):
    for k in ("SIM_MAKER", "SIM_MAKER_EX", "SIM_MAKER_ASSETS", "SIM_MAKER_SIDES", "SIM_MAKER_AMOUNT"):
        monkeypatch.delenv(k, raising=False)
    s = S.settings(p2p.Config(amount=50000))
    assert not S.enabled() and not s["on"]
    assert s["venues"] == ["Bybit"] and s["assets"] == ["USDT"] and s["sides"] == ["buy_ad", "sell_ad"]
    assert s["amount"] == 50000 and s["max_lots"] == 1 and s["capture"] == 0.5
    monkeypatch.setenv("SIM_MAKER_BAND", "мусор")
    assert S.settings()["band"] == 2.0   # опечатка в .env — значение по умолчанию, а не падение


def test_scan_loop_skips_simmaker_when_off_and_runs_when_on(monkeypatch):
    calls = []

    async def fake_scan(s, cfg):
        return snap(market())

    async def no_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(B, "scan", fake_scan)
    monkeypatch.setattr(B.history, "record", lambda *a, **k: False)
    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(S, "on_scan", lambda snap, cfg: calls.append(snap))
    bot = B.Bot(None, "x", "", p2p.Config())
    for env, expected in (("0", 0), ("1", 1)):
        monkeypatch.setenv("SIM_MAKER", env)
        try:
            asyncio.run(bot.scan_loop())
        except asyncio.CancelledError:
            pass
        assert len(calls) == expected


# --- коридор ---

def test_band_price_clamps_toward_reference_or_refuses():
    assert S.band_price("buy_ad", 91.0, 90.0, 0.5) == (pytest.approx(90.45), True)    # переплата — на границу
    assert S.band_price("buy_ad", 89.6, 90.0, 0.5) == (89.6, False)
    assert S.band_price("buy_ad", 89.0, 90.0, 0.5) is None                           # ниже коридора — не ставим
    assert S.band_price("sell_ad", 89.0, 90.0, 0.5) == (pytest.approx(89.55), True)   # демпинг — на границу
    assert S.band_price("sell_ad", 91.0, 90.0, 0.5) is None
    with pytest.raises(ValueError):
        S.band_price("swap", 90.0, 90.0, 0.5)


def test_step_keeps_ads_inside_band(monkeypatch):
    s = cfg_env(monkeypatch, SIM_MAKER_BAND="0.3")   # коридор 89.73–90.27: цены мейкера 89.51/90.49 за ним
    st, _, _ = run([snap(market())], s)
    assert st["ads"] == {} and st["paused"] == {"buy_ad": {"band": 1}, "sell_ad": {"band": 1}}
    s = cfg_env(monkeypatch, SIM_MAKER_BAND="0.5")
    st, _, _ = run([snap(market(), ref=89.0)], s)   # коридор 88.555–89.445: покупка 89.51 — на верхнюю границу
    buy = st["ads"]["buy_ad"]
    assert buy["price"] == pytest.approx(89.0 * 1.005) and buy["clamped"]
    assert 89.0 * 0.995 - 1e-9 <= buy["price"] <= 89.0 * 1.005 + 1e-9
    assert "sell_ad" not in st["ads"] and st["paused"]["sell_ad"] == {"band": 1}   # 90.49 выше коридора: дешевле не ставим
    assert buy["place"] == 2 and st["quotes"]["buy_ad"] == (1, 2, 1)   # граница ниже лучшего (89.50) — второе место


def test_narrow_spread_pauses_opening_but_not_unwinding(monkeypatch):
    s = cfg_env(monkeypatch, SIM_MAKER_MIN_SPREAD="1.0")   # спред круга 1.09% − 0.3% = 0.79% < 1%
    st, _, _ = run([snap(market())], s)
    assert st["ads"] == {} and st["paused"]["buy_ad"] == {"spread": 1}
    st["lots"] = [[50.0, 89.0, 0.0, 1000.0]]               # держим монету — продажа её закрывает, спред не мешает
    S.step(st, snap(market()), "Bybit", "USDT", s, 1020.0)
    assert set(st["ads"]) == {"sell_ad"} and st["ads"]["sell_ad"]["qty"] == pytest.approx(50.0)


# --- место в очереди ---

def test_queue_place_counts_ads_hidden_by_own_filters(monkeypatch):
    s = cfg_env(monkeypatch)
    st, _, _ = run([snap(market(), hidden=[ad("sell", 89.60, "H1"), ad("buy", 90.40, "H2"), ad("buy", 90.49, "H3")])], s)
    buy, sell = st["ads"]["buy_ad"], st["ads"]["sell_ad"]
    assert buy["price"] == pytest.approx(89.51) and (buy["place"], buy["total"]) == (2, 5)
    assert sell["price"] == pytest.approx(90.49) and (sell["place"], sell["total"]) == (3, 5)   # равная цена — старше
    st, _, _ = run([snap(market())], s)
    assert st["ads"]["buy_ad"]["place"] == 1 and st["ads"]["sell_ad"]["place"] == 1
    assert st["quotes"]["buy_ad"] == (1, 1, 0)


# --- прокси исполнения ---

def rows(*spec):
    return [[nick, price, avail, 500000] for nick, price, avail in spec]


def test_flow_counts_only_ads_at_or_better_for_us():
    prev = rows(("A", 89.60, 100), ("B", 89.51, 100), ("C", 89.40, 100), ("D", 89.00, 50))
    cur = rows(("A", 89.60, 10), ("B", 89.51, 80), ("C", 89.40, 70), ("E", 89.30, 999))
    drop, gone = S.flow(prev, cur, "buy_ad", 89.51)
    assert drop == pytest.approx(20 + 30)   # A впереди нас (дороже) — его поток до нас не дошёл
    assert gone == pytest.approx(50)        # D пропал; новичок E не в счёт
    prev = rows(("S", 90.40, 100), ("T", 90.49, 100), ("U", 90.60, 100))
    cur = rows(("S", 90.40, 0), ("T", 90.49, 100), ("U", 90.60, 60))
    assert S.flow(prev, cur, "sell_ad", 90.49) == (pytest.approx(40), 0.0)   # S дешевле нас — впереди


def test_flow_ignores_topups_duplicates_and_caps_vanished_ad():
    prev = rows(("A", 89.0, 100), ("B", 89.0, 100), ("B", 88.9, 100), ("", 88.8, 100))
    cur = rows(("A", 89.0, 150), ("B", 88.9, 10))
    assert S.flow(prev, cur, "buy_ad", 89.5) == (0.0, 0.0)   # долив не поток; дубль ника и безымянный — пропуск
    prev = [["Z", 89.0, 1000.0, 8900.0]]                      # верх лимита 8900 ₽ = 100 монет за раз
    assert S.flow(prev, [], "buy_ad", 89.5) == (0.0, pytest.approx(100.0))


def test_fill_share_depends_on_place_and_is_capped():
    assert S.fill_qty(100, 1, 60, 0, 0.5) == pytest.approx(30)
    assert S.fill_qty(100, 2, 60, 0, 0.5) == pytest.approx(15)
    assert S.fill_qty(100, 1, 300, 200, 0.5) == pytest.approx(100)


def test_synthetic_sequence_fills_buy_then_sell_into_round(monkeypatch):
    s = cfg_env(monkeypatch)
    seq = [snap(market()),
           snap(market(b2=430)),               # у B2 (89.40 ≤ 89.51) убыло 70 → купили 35
           snap(market(b2=430, b3=500)),       # у B3 убыло 300 → 150, но осталось 65 до лота
           snap(market(b2=430, b3=500))]       # ничего не менялось — исполнений нет
    st, fills, rounds = run(seq, s)
    assert [(f["side"], round(f["qty"], 6)) for f in fills] == [("buy_ad", 35.0), ("buy_ad", 65.0)]
    assert all(f["price"] == pytest.approx(89.51) and f["place"] == 1 for f in fills)
    assert fills[0]["fee"] == pytest.approx(35 * 89.51 * FEE / 100)
    assert fills[1]["wait_s"] == pytest.approx(20)   # очередь считается заново после первого исполнения
    assert S.position(st["lots"]) == pytest.approx(100) and not rounds
    assert "buy_ad" not in st["ads"] and st["paused"]["buy_ad"] == {"limit": 2}   # лимит позиции 1 лот
    # продажа: S2 (90.60 ≥ 90.49) пропал — остаток 600 → половина, но не больше позиции
    st2, fills2, rounds2 = run(seq[:3] + [snap(market(b2=430, b3=500, drop_s2=True))], s)
    assert fills2[-1]["side"] == "sell_ad" and fills2[-1]["qty"] == pytest.approx(100)
    assert fills2[-1]["flow_gone"] == pytest.approx(600) and fills2[-1]["fee"] == 0
    assert len(rounds2) == 2 and S.position(st2["lots"]) == pytest.approx(0)
    pnl = sum(r["pnl"] for r in rounds2)
    assert pnl == pytest.approx(100 * (90.49 - 89.51) - 100 * 89.51 * FEE / 100)
    assert st2["realized"] == pytest.approx(pnl) and st2["fees"] == pytest.approx(100 * 89.51 * FEE / 100)


def test_long_gap_and_silent_venue(monkeypatch):
    s = cfg_env(monkeypatch)
    st, fills, _ = run([snap(market()), snap(market(b2=0))], s, dt=600)   # 10 мин без сканов — не приписываем
    assert not fills and st["ads"]["buy_ad"]["since"] == 1600
    st, fills, _ = run([snap(market()), p2p.Snapshot(REF, "t", {"USDT": REF}, {}, [], {}, {}, {}),
                        snap(market(b2=430))], s)                       # площадка молчала один скан
    assert [round(f["qty"], 6) for f in fills] == [35.0] and fills[0]["wait_s"] == 40


# --- P&L, комиссии, позиция ---

def test_apply_fill_fifo_rounds_both_directions():
    lots, rounds = S.apply_fill([], "sell_ad", 10, 91.0, 0.0, 0)          # продали из своего запаса
    lots, rounds = S.apply_fill(lots, "buy_ad", 4, 89.0, 0.267, 60)       # откупили часть
    assert rounds[0]["first"] == "sell" and rounds[0]["qty"] == 4 and rounds[0]["hold_s"] == 60
    assert rounds[0]["pnl"] == pytest.approx(4 * 2.0 - 4 * 0.267) and rounds[0]["spread_pct"] == pytest.approx(2 / 89 * 100)
    assert lots == [[-6, 91.0, 0.0, 0]]
    lots, rounds = S.apply_fill(lots, "buy_ad", 10, 90.0, 0.27, 120)      # закрыли остаток и открыли длинную
    assert rounds[0]["qty"] == 6 and rounds[0]["pnl"] == pytest.approx(6 * 1.0 - 6 * 0.27)
    assert lots == [[pytest.approx(4), 90.0, 0.27, 120]]


def test_inventory_marked_to_reference_and_drawdown(monkeypatch):
    s = cfg_env(monkeypatch, SIM_MAKER_BAND="3")
    seq = [snap(market()), snap(market(b2=300)), snap(market(b2=300), ref=88.0)]   # купили 100, ориентир упал
    st, fills, _ = run(seq, s)
    assert sum(f["qty"] for f in fills) == pytest.approx(100)
    unreal, open_fees = S.mark(st["lots"], st["ref"])
    assert unreal == pytest.approx(100 * (88.0 - 89.51)) and open_fees == pytest.approx(100 * 89.51 * FEE / 100)
    assert st["worst_inv"] == pytest.approx(unreal)
    assert st["max_dd"] == pytest.approx(100 * (90.0 - 88.0))   # пик — при ориентире 90, дно — при 88


def test_open_qty_limits_position_both_ways():
    assert S.open_qty("buy_ad", 0, 100, 1) == (100, True)
    assert S.open_qty("buy_ad", 100, 100, 1) == (0, True)
    assert S.open_qty("sell_ad", 100, 100, 1) == (100, False)
    assert S.open_qty("sell_ad", 30, 100, 2) == (30, False)
    assert S.open_qty("sell_ad", -150, 100, 2) == (50, True)


# --- база, отчёт, доступ ---

def test_on_scan_persists_and_report_is_labelled_weak(monkeypatch, tmp_path):
    cfg_env(monkeypatch, SIM_MAKER_MAX_GAP="7200")
    db = str(tmp_path / "sim_maker.db")
    for i, sn in enumerate([snap(market()), snap(market(b2=300)), snap(market(b2=300, drop_s2=True))]):
        S.on_scan(sn, p2p.Config(), path=db, now=1000.0 + i * 3600)
    rows_ = S.stats(db)
    assert len(rows_) == 1
    r = rows_[0]
    assert (r["ex"], r["asset"], r["fills"], r["rounds"], r["fills_gone"]) == ("Bybit", "USDT", 2, 1, 1)
    assert r["total"] == pytest.approx(100 * (90.49 - 89.51) - 100 * 89.51 * FEE / 100)
    assert r["wait_min"] == pytest.approx(90)   # покупка ждала 60 мин, продажа — 120 (стоит с первого скана)
    assert r["fills_per_day"] == pytest.approx(2 / (7200 / 86400))
    text = S.report_view(p2p.Config(), path=db)
    assert "слабый прокси" in text and "Bybit USDT" in text and "Кругов: 1" in text
    assert "≥ 30 дней" in text and "реальных объявлений и сделок нет" in text


def test_report_without_data_and_off(monkeypatch, tmp_path):
    monkeypatch.delenv("SIM_MAKER", raising=False)
    text = S.report_view(path=str(tmp_path / "none.db"))
    assert "выключен" in text and "Данных пока нет" in text and not (tmp_path / "none.db").exists()


class Stub(B.Bot):
    def __init__(self, guests=()):
        super().__init__(None, "x", "1", p2p.Config())
        self.guests = set(guests)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True, "result": {"message_id": len(self.out)}}


def test_maker_paper_owner_only(monkeypatch):
    monkeypatch.setattr(S, "report_view", lambda cfg=None, path=None: "🧪 отчёт")
    bot = Stub(guests=["42"])
    asyncio.run(bot.on_update({"message": {"chat": {"id": 42}, "text": "/maker paper", "from": {}}}))
    last = [p for m, p in bot.out if m == "sendMessage"][-1]
    assert last["chat_id"] == "42" and last["text"] == B.GUEST_DENIED
    asyncio.run(bot.handle("/maker paper"))
    last = [p for m, p in bot.out if m == "sendMessage"][-1]
    assert last["chat_id"] == "1" and last["text"] == "🧪 отчёт"
