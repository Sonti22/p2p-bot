import asyncio
import dataclasses
import functools
import json
import logging
import os
import time
from datetime import datetime

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

import accounts
import bot as B
import blacklist
import history
import p2p
import paper
import presets
import trades
from helpers import make_ad


class Stub(B.Bot):
    """Бот без сети: все вызовы Telegram пишутся в self.out."""
    def __init__(self, cfg):
        super().__init__(None, "x", "1", cfg)
        self.out = []

    async def call(self, method, **p):
        self.out.append((method, p))
        return {"ok": True}

    async def send_photo(self, png, caption, markup=None):
        self.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": True}

    async def send_document(self, path, caption="", topic=None, chat_id=None):
        self.out.append(("sendDocument", {"path": path, "caption": caption}))
        return {"ok": True}


def deal(profit=3.0, s_ex="MEXC", s_asset="USDT", route="перевод −0.2 USDT (BEP20) на MEXC"):
    return profit, make_ad("Bybit", "buy", 85.0), make_ad(s_ex, "sell", 90.0, asset=s_asset), route


def snap(deals):
    return p2p.Snapshot(88.0, "test", {}, {}, deals, {}, {}, {})


def snap_groups(deals):
    """Как snap(), но со стаканом (snap.groups) под первую связку — для deal_for_amount/сухого прогона."""
    b, s = deals[0][1], deals[0][2]
    groups = {(b.ex, "buy", b.asset): [b], (s.ex, "sell", s.asset): [s]}
    return p2p.Snapshot(88.0, "test", {}, {}, deals, {}, {}, {}, groups=groups)


def err_snap(errors):
    return p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, errors)


def photos(bot):
    return [m for m in bot.out if m[0] == "sendPhoto"]


def texts(bot):
    return [p["text"] for m, p in bot.out if m == "sendMessage"]


def test_notify_top_n_and_dedup(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1   # тест про антидубль/паузу, не про живость
    bot.max_signals = 2
    ds = [deal(5, "MEXC"), deal(4, "KuCoin"), deal(3, "HTX")]
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2          # повтор той же связки не шлём


def test_below_threshold_not_sent(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=5.0))
    asyncio.run(bot.notify(snap([deal(3)])))
    assert not bot.out


def _patch_paper_db(monkeypatch, db):
    monkeypatch.setattr(B.paper, "open_cycles", functools.partial(B.paper.open_cycles, path=db))
    monkeypatch.setattr(B.paper, "init_balance", functools.partial(B.paper.init_balance, path=db))
    monkeypatch.setattr(B.paper, "start_cycle", functools.partial(B.paper.start_cycle, path=db))
    monkeypatch.setattr(B.paper, "finish_cycle", functools.partial(B.paper.finish_cycle, path=db))
    monkeypatch.setattr(B.paper, "set_stage", functools.partial(B.paper.set_stage, path=db))
    monkeypatch.setattr(B.paper, "get_balance", functools.partial(B.paper.get_balance, path=db))
    monkeypatch.setattr(B.paper, "balance_change", functools.partial(B.paper.balance_change, path=db))
    monkeypatch.setattr(B.paper, "stats", functools.partial(B.paper.stats, path=db))
    monkeypatch.setattr(B.paper, "ladder_suggestion", functools.partial(B.paper.ladder_suggestion, path=db))
    monkeypatch.setattr(B.paper, "banks_this_month", functools.partial(B.paper.banks_this_month, path=db))
    monkeypatch.setattr(B.paper, "report_rows", functools.partial(B.paper.report_rows, path=db))


def test_paper_cycle_starts_on_signal_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    monkeypatch.setenv("PAPER_MAX_OPEN", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    bot.guests = {"999"}   # гостям про сухой прогон — ничего
    ds = [deal(5)]
    asyncio.run(bot.notify(snap_groups(ds)))
    cycles = paper.open_cycles(path=db)
    assert len(cycles) == 1
    c = cycles[0]
    assert c["stage"] == "buy" and c["result"] is None
    assert c["buy_ex"] == "Bybit" and c["buy_price"] == 85.0 and c["amount"] == 10000.0
    paper_msgs = [t for t in texts(bot) if "Сухой прогон" in t]
    assert len(paper_msgs) == 1
    assert "T-Bank" in paper_msgs[0]
    # запросы sendMessage не несут явный chat_id гостя — сообщение только владельцу
    assert all(p.get("chat_id") in (None, bot.chat_id) for m, p in bot.out
              if m == "sendMessage" and "Сухой прогон" in p.get("text", ""))


def test_paper_cycle_stores_route_hops(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    monkeypatch.setenv("PAPER_MAX_OPEN", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.notify(snap_groups([deal(5)])))
    c = paper.open_cycles(path=db)[0]
    hops = json.loads(c["route_hops"])
    assert json.loads(c["route_venues"]) == []   # покупка/продажа USDT — конвертации на споте нет
    assert len(hops) == 1
    assert hops[0]["frm"] == "Bybit" and hops[0]["to"] == "MEXC" and hops[0]["asset"] == "USDT"


def test_paper_cycle_off_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.delenv("PAPER", raising=False)
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.notify(snap_groups([deal(5)])))
    assert not paper.open_cycles(path=db)
    assert not [t for t in texts(bot) if "Сухой прогон" in t]


def test_paper_cycle_skips_when_slot_full(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    monkeypatch.setenv("PAPER_MAX_OPEN", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)   # слот уже занят
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.notify(snap_groups([deal(5)])))
    assert len(paper.open_cycles(path=db)) == 1   # новый круг не завёлся
    assert not [t for t in texts(bot) if "Сухой прогон" in t]


def test_process_paper_cycles_waits_before_pay_minutes(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "5")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time())
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(snap_groups([deal(5)])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "buy" and c["result"] is None   # рано — ещё не прошло PAPER_PAY_MINUTES


def test_process_paper_cycles_advances_when_ad_still_there(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "5")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    s = snap_groups([deal(5)])   # groups: (Bybit, buy, USDT) -> [та же связка, цена 85.0]
    asyncio.run(bot.process_paper_cycles(s))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "transfer" and c["result"] is None
    assert not [t for t in texts(bot) if "сорвался" in t]


def test_process_paper_cycles_fails_when_ad_gone(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "5")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    s = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {},
                     groups={("Bybit", "buy", "USDT"): []})   # площадка ответила, объявления нет
    asyncio.run(bot.process_paper_cycles(s))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "buy" and c["result"] == "failed_buy" and c["realized_pct"] == 0.0
    msgs = [t for t in texts(bot) if "сорвался" in t]
    assert len(msgs) == 1 and "исчезло" in msgs[0]


def test_process_paper_cycles_buy_price_change_does_not_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_PAY_MINUTES", "5")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    worse = make_ad("Bybit", "buy", 86.0)   # тот же мерчант, цена выросла — ордер уже по старой цене
    s = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups={("Bybit", "buy", "USDT"): [worse]})
    asyncio.run(bot.process_paper_cycles(s))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "transfer" and c["result"] is None
    assert not [t for t in texts(bot) if "сорвался" in t]


def test_process_paper_cycles_noop_without_chat_id(monkeypatch, tmp_path):
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.chat_id = None
    asyncio.run(bot.process_paper_cycles(snap_groups([deal(5)])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "buy" and c["result"] is None   # без chat_id стадии не проверяем


def test_process_paper_cycles_transfer_waits_before_transfer_minutes(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_TRANSFER_MINUTES", "3")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time())
    paper.set_stage(cid, "transfer", path=db)
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(snap([])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "transfer" and c["result"] is None   # рано — ещё не прошло PAPER_TRANSFER_MINUTES


def test_process_paper_cycles_transfer_advances_to_sell(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_TRANSFER_MINUTES", "3")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    paper.set_stage(cid, "transfer", path=db, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(snap([])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "sell" and c["result"] is None
    assert not [t for t in texts(bot) if "сорвался" in t]


def test_process_paper_cycles_transfer_fails_when_withdraw_closed(monkeypatch, tmp_path):
    import netstatus
    monkeypatch.setenv("PAPER_TRANSFER_MINUTES", "3")
    netstatus._apply("Bybit", "USDT", {n: {"dep": True, "wd": False, "fee": 1.0}
                                        for n in ("TRC20", "BEP20", "ERC20", "TON")})
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    paper.set_stage(cid, "transfer", path=db, ts=time.time() - 400)
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(snap([])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "transfer" and c["result"] == "failed_transfer" and c["realized_pct"] == 0.0
    msgs = [t for t in texts(bot) if "сорвался" in t]
    assert len(msgs) == 1 and "переводе" in msgs[0] and "закрыт" in msgs[0]


def _sell_stage_snap(sell_ex="MEXC", sell_asset="USDT", ads=()):
    return p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups={(sell_ex, "sell", sell_asset): list(ads)})


def test_process_paper_cycles_sell_completes_and_updates_balance(monkeypatch, tmp_path):
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    paper.set_stage(cid, "sell", path=db)
    paper.init_balance(10000, path=db)
    bot = Stub(p2p.Config(min_profit=2.0))
    better = make_ad("MEXC", "sell", 91.0)   # продали дороже плана — факт лучше плана
    asyncio.run(bot.process_paper_cycles(_sell_stage_snap(ads=[better])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "sell" and c["result"] == "done"
    assert c["realized_pct"] == pytest.approx((1.02 * 91.0 / 90.0 - 1) * 100)
    assert paper.get_balance(path=db) == pytest.approx(10000 + 10000 * c["realized_pct"] / 100)
    msgs = [t for t in texts(bot) if "завершён" in t]
    assert len(msgs) == 1 and "план 2.00%" in msgs[0]


def test_process_paper_cycles_sell_fails_when_nobody_buys(monkeypatch, tmp_path):
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=time.time() - 400)
    paper.set_stage(cid, "sell", path=db)
    bot = Stub(p2p.Config(min_profit=2.0))
    asyncio.run(bot.process_paper_cycles(_sell_stage_snap(ads=[])))
    c = paper.get_cycle(cid, path=db)
    assert c["stage"] == "sell" and c["result"] == "failed_sell" and c["realized_pct"] == 0.0
    msgs = [t for t in texts(bot) if "сорвался" in t]
    assert len(msgs) == 1 and "продаже" in msgs[0] and "глубины" in msgs[0]


def test_paper_cycle_skips_when_depth_insufficient(monkeypatch, tmp_path):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000000")   # больше глубины стакана в снимке
    monkeypatch.setenv("PAPER_MAX_OPEN", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.notify(snap_groups([deal(5)])))
    assert not paper.open_cycles(path=db)
    assert not [t for t in texts(bot) if "Сухой прогон" in t]


def test_notify_threshold_before_top_n(monkeypatch):
    """Сканер ставит надёжную 1.5% выше рискованной 3%: связка выше порога за ней всё равно уходит."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    bot.max_signals = 3
    asyncio.run(bot.notify(snap([deal(1.5, "MEXC"), deal(3, "KuCoin")])))
    caps = [p["caption"] for m, p in photos(bot)]
    assert len(caps) == 1 and "KuCoin" in caps[0] and "+3.00%" in caps[0]


def test_notify_slices_after_threshold(monkeypatch):
    """Топ-N режется уже после порога: три надёжные связки ниже порога не съедают MAX_SIGNALS."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    bot.max_signals = 3
    asyncio.run(bot.notify(snap([deal(1.5, "MEXC"), deal(1.5, "HTX"), deal(1.5, "Bitpapa"), deal(3, "KuCoin")])))
    caps = [p["caption"] for m, p in photos(bot)]
    assert len(caps) == 1 and "KuCoin" in caps[0]


def test_notify_top_n_counts_only_above_threshold(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    bot.max_signals = 2
    asyncio.run(bot.notify(snap([deal(1.5, "MEXC"), deal(5, "KuCoin"), deal(4, "HTX"), deal(3, "Bitpapa")])))
    caps = [p["caption"] for m, p in photos(bot)]
    assert len(caps) == 2 and "KuCoin" in caps[0] and "HTX" in caps[1]


def test_settings_apply_persists(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MIN_PROFIT=2\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    assert "3%" in bot.apply("min:3")
    bot.apply("amt:100000")
    text = env.read_text(encoding="utf-8")
    assert "MIN_PROFIT=3" in text and "AMOUNT=100000" in text
    assert bot.cfg.min_profit == 3 and bot.cfg.amount == 100000


def _env_bot(tmp_path, monkeypatch, **cfg):
    env = tmp_path / ".env"
    env.write_text("TG_CHAT_ID=1\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    return Stub(p2p.Config(**cfg)), env


def test_amount_command_rejects_bad_values(tmp_path, monkeypatch):
    bot, env = _env_bot(tmp_path, monkeypatch, amount=50000)
    for arg in ("0", "-5", "nan", "inf", "1e999", "99999999"):
        asyncio.run(bot.handle(f"/amount {arg}"))
        assert "от 1 000 до 5 000 000" in texts(bot)[-1], arg
        assert bot.cfg.amount == 50000, arg
    assert "AMOUNT" not in env.read_text(encoding="utf-8")


def test_amount_command_accepts_units(tmp_path, monkeypatch):
    bot, env = _env_bot(tmp_path, monkeypatch, amount=50000)
    asyncio.run(bot.handle("/amount 20к"))
    assert bot.cfg.amount == 20000
    assert "AMOUNT=20000" in env.read_text(encoding="utf-8")
    assert "20 000" in texts(bot)[-1]


def test_min_command_rejects_bad_values(tmp_path, monkeypatch):
    bot, env = _env_bot(tmp_path, monkeypatch, min_profit=2.0)
    for arg in ("nan", "-100", "inf", "1e308", "0"):
        asyncio.run(bot.handle(f"/min {arg}"))
        assert "Не понял порог" in texts(bot)[-1], arg
        assert bot.cfg.min_profit == 2.0, arg
    assert "MIN_PROFIT" not in env.read_text(encoding="utf-8")


def test_min_command_accepts_comma(tmp_path, monkeypatch):
    bot, env = _env_bot(tmp_path, monkeypatch, min_profit=2.0)
    asyncio.run(bot.handle("/min 1,5"))
    assert bot.cfg.min_profit == 1.5
    assert "MIN_PROFIT=1.5" in env.read_text(encoding="utf-8")
    assert "Порог 1.5%" in texts(bot)[-1]


def test_apply_callback_rejects_forged_values(tmp_path, monkeypatch):
    """callback_data «amt:»/«min:» можно подделать клиентом — apply проверяет значение тем же парсером."""
    bot, env = _env_bot(tmp_path, monkeypatch, amount=50000, min_profit=2.0)
    for data in ("amt:nan", "amt:0", "min:inf", "min:-1"):
        asyncio.run(bot.on_callback({"id": "1", "data": data, "message": {"message_id": 3}}))
        assert ("answerCallbackQuery", {"callback_query_id": "1", "text": "Некорректное значение"}) in bot.out, data
    assert bot.cfg.amount == 50000 and bot.cfg.min_profit == 2.0
    assert env.read_text(encoding="utf-8") == "TG_CHAT_ID=1\n"


def test_account_poll_interval_reads_dotenv_after_load(tmp_path, monkeypatch):
    """ACCOUNT_POLL_INTERVAL раньше читался в момент импорта bot.py — до load_env() в main() — и .env
    игнорировался. account_poll_interval() должен подхватывать значение уже после load_env()."""
    monkeypatch.delenv("ACCOUNT_POLL_INTERVAL", raising=False)
    assert B.account_poll_interval() == B.ACCOUNT_POLL_INTERVAL_DEFAULT
    env = tmp_path / ".env"
    env.write_text("ACCOUNT_POLL_INTERVAL=45\n", encoding="utf-8")
    try:
        p2p.load_env(str(env))   # os.environ.setdefault — не отслеживается monkeypatch, снимаем сами
        assert B.account_poll_interval() == 45
    finally:
        os.environ.pop("ACCOUNT_POLL_INTERVAL", None)


def test_send_deal_passes_amount_breakdown_from_snap(monkeypatch):
    captured = {}

    def fake_deal_card(d, c, amounts=None, rel=None, breakdown=None):
        captured["amounts"] = amounts
        return b"png"

    monkeypatch.setattr(B, "deal_card", fake_deal_card)
    bot = Stub(p2p.Config())
    d = deal(5, "MEXC")
    asyncio.run(bot.send_deal(d, snap=snap([d])))
    assert captured["amounts"] is not None and set(captured["amounts"]) == set(p2p.DEPTH_AMOUNTS)


def test_live_card_edits_instead_of_resending(monkeypatch):
    """Живая карточка: пока связка почти не меняется, вместо нового сообщения правим старое."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1

    async def fake_send_photo(png, caption, markup=None):
        bot.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": True, "result": {"message_id": 555}}

    bot.send_photo = fake_send_photo
    asyncio.run(bot.notify(snap([deal(5)])))
    assert len(photos(bot)) == 1
    key = next(iter(bot.live_msg))
    bot.live_msg[key]["last_edit"] -= B.LIVE_EDIT_INTERVAL + 1   # прошло достаточно времени для правки

    asyncio.run(bot.notify(snap([deal(5.1)])))   # почти та же прибыль — новое сообщение не шлём
    assert len(photos(bot)) == 1
    edits = [p for m, p in bot.out if m == "editMessageCaption"]
    assert len(edits) == 1 and edits[0]["message_id"] == 555


def test_live_card_too_soon_not_edited(monkeypatch):
    """Не чаще раза в LIVE_EDIT_INTERVAL секунд — сразу после отправки правку не делаем."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1

    async def fake_send_photo(png, caption, markup=None):
        return {"ok": True, "result": {"message_id": 1}}

    bot.send_photo = fake_send_photo
    asyncio.run(bot.notify(snap([deal(5)])))
    asyncio.run(bot.notify(snap([deal(5.1)])))
    assert not [p for m, p in bot.out if m == "editMessageCaption"]


def test_live_card_marks_stale_when_deal_disappears(monkeypatch):
    """Связка ушла из топа — последний сигнал по ней помечается «⌛ устарел», и только один раз."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1

    async def fake_send_photo(png, caption, markup=None):
        return {"ok": True, "result": {"message_id": 42}}

    bot.send_photo = fake_send_photo
    asyncio.run(bot.notify(snap([deal(5)])))
    asyncio.run(bot.notify(snap([])))   # связка пропала из скана
    edits = [p for m, p in bot.out if m == "editMessageCaption"]
    assert len(edits) == 1 and "устарел" in edits[0]["caption"] and edits[0]["message_id"] == 42

    asyncio.run(bot.notify(snap([])))   # повторно помечать не нужно
    assert len([p for m, p in bot.out if m == "editMessageCaption"]) == 1


def test_deal_markup_links():
    kb = B.deal_markup(deal(route="спот USDT→ETH на Bybit (−0.1%)", s_asset="ETH"))["inline_keyboard"]
    urls = [b["url"] for row in kb for b in row if "url" in b]
    assert any("bybit.com/fiat/trade/otc" in u for u in urls)
    assert any("bybit.com/trade/spot/ETH/USDT" in u for u in urls)


def test_fmt_top_fits_telegram():
    ds = [deal(5 - i * 0.1) for i in range(30)]
    assert len(p2p.fmt_top(snap(ds), p2p.Config(), n=30)) <= 4000


def test_venue_alert_after_fail_streak():
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    bad = err_snap({"bybit/USDT": "TimeoutError: x"})
    asyncio.run(bot.check_venues(bad))
    asyncio.run(bot.check_venues(bad))
    assert not texts(bot)                 # 2 подряд — ещё рано
    asyncio.run(bot.check_venues(bad))
    assert any("bybit" in t and "недоступна" in t for t in texts(bot))


def test_venue_alert_cooldown_then_recovery():
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    bad, ok = err_snap({"bybit/USDT": "err"}), err_snap({})
    for _ in range(5):
        asyncio.run(bot.check_venues(bad))
    assert len(texts(bot)) == 1            # повтор в течение часа не шлём
    asyncio.run(bot.check_venues(ok))
    msgs = texts(bot)
    assert len(msgs) == 2 and "снова доступна" in msgs[-1]


def test_venue_alert_after_15min_without_streak():
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    bad = err_snap({"bybit/USDT": "err"})
    asyncio.run(bot.check_venues(bad))     # streak 1, down_since = сейчас
    bot.venue["bybit"]["down_since"] = time.time() - B.VENUE_DOWN_AFTER - 1
    asyncio.run(bot.check_venues(bad))     # streak 2, но уже дольше 15 мин
    assert any("bybit" in t and "недоступна" in t for t in texts(bot))


def test_venue_no_alert_when_healthy():
    bot = Stub(p2p.Config(exchanges=["bybit", "mexc"]))
    asyncio.run(bot.check_venues(err_snap({})))
    assert not texts(bot)


def test_dev_view(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"version": "abc1234", "repo": "https://github.com/o/r", "started_at": "24.09 14:00", '
                      '"log": [{"sha": "abc1234", "date": "2026-09-24", "subject": "Add /status"}]}', encoding="utf-8")
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text("## Очередь\n- [x] первая\n- [ ] `/status` вторая\n- [ ] третья\n## Идеи\n- [ ] не считать\n",
                       encoding="utf-8")
    assert B.roadmap_progress(str(roadmap)) == (1, 3, "/status вторая")
    text, kb = B.dev_view(str(status), str(roadmap))
    assert "abc1234" in text and "1 из 3" in text and "Add /status" in text
    urls = [b.get("url", "") for row in kb["inline_keyboard"] for b in row]
    assert "https://github.com/o/r/commits/main" in urls


def test_dev_view_without_files(tmp_path):
    text, kb = B.dev_view(str(tmp_path / "none.json"), str(tmp_path / "none.md"))
    assert "Разработка" in text and kb["inline_keyboard"]


def test_send_deal_adds_done_button(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config())
    asyncio.run(bot.send_deal(deal(), "🔔 "))
    markup = photos(bot)[0][1]["markup"]
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    assert any(b.get("callback_data", "").startswith("did:") for b in buttons)
    assert len(bot.deals_by_id) == 1


def test_mark_done_logs_trade_and_clears_button(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    st = trades.stats(path=db)
    assert st["day"]["count"] == 1 and st["day"]["amount"] == 70000
    assert deal_id not in bot.deals_by_id
    method, params = bot.out[-1]
    assert method == "editMessageReplyMarkup"
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(b.get("callback_data", "").startswith("did:") for b in buttons)


def test_mark_done_uses_amount_at_signal_time(tmp_path, monkeypatch):
    """Сумму сменили после сигнала — «✅ Сделал» старой карточки пишет сумму, под которую считали %."""
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=50000))
    deal_id = bot.remember_deal(deal(5.0))
    bot.cfg.amount = 200000
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    assert trades.stats(path=db)["day"]["amount"] == 50000


def test_show_steps_uses_cfg_at_signal_time():
    bot = Stub(p2p.Config(amount=50000))
    d = deal(5.0)
    deal_id = bot.remember_deal(d, snap=snap([d]))
    bot.cfg.amount = 200000
    asyncio.run(bot.show_steps({"id": "1"}, deal_id))
    head = texts(bot)[-1].splitlines()[0]
    assert "50 000" in head and "200 000" not in head


def test_amount_change_via_settings_keeps_old_card_amount(tmp_path, monkeypatch):
    """Сценарий целиком: сигнал на 50 000 → «amt:200000» в настройках → «✅ Сделал» старой карточки."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "save_env", lambda *a, **k: None)
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=50000, min_profit=2.0))
    bot.live_scans = 1
    asyncio.run(bot.notify(snap([deal(5.0)])))
    deal_id = next(iter(bot.deals_by_id))
    assert bot.deals_by_id[deal_id][1] is not bot.cfg
    bot.apply("amt:200000")
    asyncio.run(bot.on_callback({"id": "1", "data": f"did:{deal_id}", "message": {"message_id": 9}}))
    assert trades.stats(path=db)["day"]["amount"] == 50000


def test_live_card_edit_moves_buttons_to_edited_amount(tmp_path, monkeypatch):
    """Сигнал на 50 000 → «amt:200000» → живая карточка переписана «на 200 000»: кнопки того же сообщения
    («📝 Инструкция», «✅ Сделал») — по той же сумме и связке, что теперь в подписи."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "save_env", lambda *a, **k: None)
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=50000, min_profit=2.0))
    bot.live_scans = 1

    async def fake_send_photo(png, caption, markup=None):
        bot.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": True, "result": {"message_id": 555}}

    bot.send_photo = fake_send_photo
    asyncio.run(bot.notify(snap([deal(5.0)])))
    buttons = _callbacks(photos(bot)[0][1]["markup"])
    bot.apply("amt:200000")
    bot.live_msg[next(iter(bot.live_msg))]["last_edit"] -= B.LIVE_EDIT_INTERVAL + 1
    asyncio.run(bot.notify(snap([deal(5.1)])))   # в пределах cooldown — правка вместо нового сообщения
    edits = [p for m, p in bot.out if m == "editMessageCaption"]
    assert len(photos(bot)) == 1 and len(edits) == 1 and "200 000" in edits[0]["caption"]

    asyncio.run(bot.on_callback({"id": "1", "data": buttons["steps"], "message": {"message_id": 555}}))
    head = texts(bot)[-1].splitlines()[0]
    assert "200 000" in head and "50 000" not in head
    asyncio.run(bot.on_callback({"id": "2", "data": buttons["did"], "message": {"message_id": 555}}))
    assert trades.stats(path=db)["day"]["amount"] == 200000
    assert any(t.startswith("Расчёт был +5.10%") for t in texts(bot))   # по связке из правки


def test_live_card_edit_failed_keeps_buttons_on_old_snapshot(tmp_path, monkeypatch):
    """Telegram не принял правку — на карточке старая подпись, кнопки остаются на старом снимке."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "save_env", lambda *a, **k: None)
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=50000, min_profit=2.0))
    bot.live_scans = 1

    async def fake_send_photo(png, caption, markup=None):
        bot.out.append(("sendPhoto", {"caption": caption, "markup": markup}))
        return {"ok": True, "result": {"message_id": 555}}

    async def call(method, **p):
        bot.out.append((method, p))
        return {"ok": False, "error_code": 400} if method == "editMessageCaption" else {"ok": True}

    bot.send_photo, bot.call = fake_send_photo, call
    asyncio.run(bot.notify(snap([deal(5.0)])))
    buttons = _callbacks(photos(bot)[0][1]["markup"])
    bot.apply("amt:200000")
    bot.live_msg[next(iter(bot.live_msg))]["last_edit"] -= B.LIVE_EDIT_INTERVAL + 1
    asyncio.run(bot.notify(snap([deal(5.1)])))
    asyncio.run(bot.on_callback({"id": "2", "data": buttons["did"], "message": {"message_id": 555}}))
    assert trades.stats(path=db)["day"]["amount"] == 50000


def test_deal_snapshot_does_not_share_filter_lists():
    """Снимок настроек — глубокая копия: _toggle меняет списки self.cfg на месте, снимок это не задевает."""
    bot = Stub(p2p.Config(assets=["USDT"]))
    deal_id = bot.remember_deal(deal(5.0))
    bot.cfg.assets.append("BTC")
    assert bot.deals_by_id[deal_id][1].assets == ["USDT"]


def _callbacks(markup):
    return {b["callback_data"].split(":", 1)[0]: b["callback_data"]
            for row in markup["inline_keyboard"] for b in row if ":" in b.get("callback_data", "")}


def _restarted_bots(monkeypatch):
    """Бот до рестарта отправил связку в MEXC, бот после рестарта — в KuCoin; кнопки первой карточки."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B.time, "time", lambda: 1_790_000_000.0)
    old = Stub(p2p.Config())
    asyncio.run(old.send_deal(deal(5.0, "MEXC")))
    monkeypatch.setattr(B.time, "time", lambda: 1_790_000_600.0)   # рестарт через 10 минут
    new = Stub(p2p.Config())
    asyncio.run(new.send_deal(deal(5.0, "KuCoin")))
    new.out.clear()
    return new, _callbacks(photos(old)[0][1]["markup"])


def test_deal_ids_unique_across_restarts(monkeypatch):
    new, old_buttons = _restarted_bots(monkeypatch)
    assert int(old_buttons["did"][4:]) not in new.deals_by_id


def test_old_did_button_after_restart_is_stale(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    new, old_buttons = _restarted_bots(monkeypatch)
    for data in (old_buttons["did"], "did:1"):   # кнопка прошлого запуска и кнопка до этой правки
        asyncio.run(new.on_callback({"id": "1", "data": data, "message": {"message_id": 9}}))
        assert new.out[-1] == ("answerCallbackQuery", {"callback_query_id": "1", "text": "Сигнал устарел, не записан"})
    assert trades.stats(path=db)["day"]["count"] == 0
    assert not [m for m, p in new.out if m == "editMessageReplyMarkup"]


def test_old_bl_and_steps_buttons_after_restart_are_stale(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    new, old_buttons = _restarted_bots(monkeypatch)
    asyncio.run(new.on_callback({"id": "1", "data": old_buttons["bl"], "message": {"message_id": 9}}))
    assert "устарел" in new.out[-1][1]["text"]
    assert blacklist.blocked(path=db) == set()
    asyncio.run(new.on_callback({"id": "1", "data": old_buttons["steps"], "message": {"message_id": 9}}))
    assert "устарел" in new.out[-1][1]["text"]
    assert not any("Шаги связки" in t for t in texts(new))


def test_deal_markup_has_hide_button():
    kb = B.deal_markup(deal(), deal_id=7)["inline_keyboard"]
    buttons = [b for row in kb for b in row]
    assert any(b.get("callback_data") == "bl:7" for b in buttons)


def test_hide_deal_blacklists_both_sides_and_clears_button(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    bot = Stub(p2p.Config())
    d = deal(5.0)
    deal_id = bot.remember_deal(d)
    asyncio.run(bot.hide_deal({"id": "1", "message": {"message_id": 9}}, deal_id))
    assert blacklist.blocked(path=db) == {("Bybit", "nick"), ("MEXC", "nick")}
    assert deal_id not in bot.deals_by_id
    method, params = bot.out[-1]
    assert method == "editMessageReplyMarkup"
    buttons = [b for row in params["reply_markup"]["inline_keyboard"] for b in row]
    assert not any(b.get("callback_data", "").startswith("bl:") for b in buttons)


def test_hide_deal_unknown_id_not_blacklisted(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.hide_deal({"id": "1", "message": {"message_id": 9}}, 999))
    assert blacklist.blocked(path=db) == set()
    assert "устарел" in bot.out[-1][1]["text"]


def test_blacklist_command_lists_entries(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "list_all", functools.partial(B.blacklist.list_all, path=db))
    blacklist.add("Bybit", "Плохой", path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/blacklist"))
    text = texts(bot)[-1]
    assert "Плохой" in text


def test_blacklist_command_empty():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/blacklist"))
    assert "пуст" in texts(bot)[-1].lower()


def test_unbl_callback_removes_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "list_all", functools.partial(B.blacklist.list_all, path=db))
    monkeypatch.setattr(B.blacklist, "remove", functools.partial(B.blacklist.remove, path=db))
    blacklist.add("Bybit", "Плохой", path=db)
    entry_id = blacklist.list_all(path=db)[0][0]
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": f"unbl:{entry_id}", "message": {"message_id": 3}}))
    assert blacklist.list_all(path=db) == []
    method, params = bot.out[-1]
    assert method == "editMessageText" and "пуст" in params["text"].lower()


def test_blacklist_view_shows_id_age_and_note():
    now = time.time()
    old = blacklist.add("Bybit", "Плохой", ts=now - 3 * 86400 - 60)
    blacklist.set_note(old, "не <отпускал>")
    fresh = blacklist.add("MEXC", "Новый", ts=now)
    text, kb = B.blacklist_view(now=now)
    assert f"Bybit: Плохой (id {old}), в списке 3 дн. — 📝 не &lt;отпускал&gt;" in text
    assert f"MEXC: Новый (id {fresh}), в списке 0 дн.\n" in text                  # без причины — без «📝»
    assert "/blacklist note &lt;id&gt; &lt;текст&gt;" in text
    assert [b["callback_data"] for row in kb["inline_keyboard"] for b in row] == [f"unbl:{old}", f"unbl:{fresh}"]


def test_hide_deal_tells_how_to_add_reason(tmp_path, monkeypatch):
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    bot = Stub(p2p.Config())
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.hide_deal({"id": "1", "message": {"message_id": 9}}, deal_id))
    rows = blacklist.list_all(path=db)
    ids = {ex: entry_id for entry_id, ex, *_ in rows}
    text = texts(bot)[-1]
    assert f"Bybit: nick (id {ids['Bybit']})" in text and f"MEXC: nick (id {ids['MEXC']})" in text
    assert "/blacklist note &lt;id&gt; &lt;текст&gt;" in text
    assert all(r[3] for r in rows)                                           # время добавления записано


def _hide(monkeypatch, tmp_path, d):
    """Нажать «🚫» под связкой d; вернуть ({(площадка, ник): id} из блэклиста, текст подтверждения, бот)."""
    db = str(tmp_path / "blacklist.db")
    monkeypatch.setattr(B.blacklist, "add", functools.partial(B.blacklist.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.hide_deal({"id": "1", "message": {"message_id": 9}}, bot.remember_deal(d)))
    return {(ex, nick): entry_id for entry_id, ex, nick, *_ in blacklist.list_all(path=db)}, texts(bot)[-1], bot


def _stack(ad, *nicks):
    return dataclasses.replace(ad, nick=f"{len(nicks)} объявл.", nicks=nicks, parts=len(nicks))


def test_hide_deal_stacked_buy_blacklists_every_merchant(tmp_path, monkeypatch):
    b = _stack(make_ad("Bybit", "buy", 85.0), "Вася", "Петя", "Вася")        # Вася — два объявления стакана
    rows, text, bot = _hide(monkeypatch, tmp_path, (3.0, b, make_ad("MEXC", "sell", 90.0), "маршрут"))
    assert set(rows) == {("Bybit", "Вася"), ("Bybit", "Петя"), ("MEXC", "nick")}   # без «3 объявл.»
    for (ex, nick), entry_id in rows.items():
        assert f"{ex}: {nick} (id {entry_id})" in text
    assert "объявл." not in text
    assert bot.out[-1][0] == "editMessageReplyMarkup"


def test_hide_deal_stacked_sell_blacklists_every_merchant(tmp_path, monkeypatch):
    s = _stack(make_ad("BestChange", "sell", 90.0), "Обменник1 [TRC20]", "Обменник2 [TRC20]")
    rows, text, _ = _hide(monkeypatch, tmp_path, (3.0, make_ad("Bybit", "buy", 85.0), s, "маршрут"))
    assert set(rows) == {("Bybit", "nick"), ("BestChange", "Обменник1 [TRC20]"), ("BestChange", "Обменник2 [TRC20]")}
    for (ex, nick), entry_id in rows.items():
        assert f"{ex}: {nick} (id {entry_id})" in text


def test_hide_deal_single_ad_unchanged(tmp_path, monkeypatch):
    b = dataclasses.replace(make_ad("Bybit", "buy", 85.0), nicks=("nick",))  # одно объявление после _combined
    rows, text, _ = _hide(monkeypatch, tmp_path, (3.0, b, make_ad("MEXC", "sell", 90.0), "маршрут"))
    assert set(rows) == {("Bybit", "nick"), ("MEXC", "nick")}
    assert text.count("(id ") == 2


def test_blacklist_note_command_valid_and_invalid_id():
    entry_id = blacklist.add("Bybit", "Плохой")
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle(f"/blacklist note {entry_id} тянул с оплатой"))
    assert "Причина записана" in texts(bot)[-1]
    assert blacklist.list_all()[0][4] == "тянул с оплатой"
    asyncio.run(bot.handle(f"/blacklist note {entry_id + 50} другое"))
    assert f"нет записи с id {entry_id + 50}" in texts(bot)[-1]
    for bad in ("/blacklist note abc текст", f"/blacklist note {entry_id}", "/blacklist что-то"):
        asyncio.run(bot.handle(bad))
        assert "/blacklist note &lt;id&gt;" in texts(bot)[-1], bad
    assert blacklist.list_all()[0][4] == "тянул с оплатой"               # ни одна кривая команда не затёрла
    asyncio.run(bot.handle("/blacklist"))
    assert "📝 тянул с оплатой" in texts(bot)[-1]


def test_help_points_to_safety_where_sbp_delay_rule_lives():
    """Правило ЦБ ОД-2506 — общая информация: одна справка для всех, само правило — в /safety."""
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/help"))
    assert "/safety" in texts(bot)[-1] and B.OWNER_GUIDE == B.GUIDE
    assert "ОД-2506" in B.SAFETY and "200 000 ₽" in B.SAFETY


def test_add_alert_creates_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d"))
    rows = B.alerts.list_all("1", path=db)
    assert [(a, s, r, c, v, rl) for _, a, s, r, _, c, v, rl in rows] == \
        [("USDT", "sell", 92.0, None, None, 0)]
    assert "Алерт создан" in texts(bot)[-1]


def test_add_alert_repeat_creates_entry_with_cooldown(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d repeat 1h"))
    rows = B.alerts.list_all("1", path=db)
    assert [(a, s, r, c) for _, a, s, r, _, c, _, _ in rows] == [("USDT", "sell", 92.0, 3600)]
    assert "повтор" in texts(bot)[-1].lower()


def test_add_alert_volume_and_reliable_creates_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "add", functools.partial(B.alerts.add, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d vol 50к reliable repeat 1h"))
    rows = B.alerts.list_all("1", path=db)
    assert [(a, s, r, c, v, rl) for _, a, s, r, _, c, v, rl in rows] == \
        [("USDT", "sell", 92.0, 3600, 50_000.0, 1)]
    text = texts(bot)[-1]
    assert "объём" in text.lower() and "надёжность" in text.lower()


def test_add_alert_bad_volume():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d vol abc"))
    assert "Объём" in texts(bot)[-1]


def test_add_alert_unknown_token_sends_help():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d bogus"))
    assert "Формат" in texts(bot)[-1]


def test_add_alert_bad_repeat_duration():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 7d repeat 999d"))
    assert "Кулдаун" in texts(bot)[-1]


def test_add_alert_bad_format_sends_help():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell"))
    assert "Формат" in texts(bot)[-1]


def test_add_alert_unknown_asset():
    bot = Stub(p2p.Config(assets=["USDT"]))
    asyncio.run(bot.handle("/alert BTC sell 92 7d"))
    assert "не отслеживается" in texts(bot)[-1]


def test_add_alert_bad_duration():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alert USDT sell 92 999d"))
    assert "Срок" in texts(bot)[-1]


def test_alerts_command_lists_entries(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "list_all", functools.partial(B.alerts.list_all, path=db))
    B.alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alerts"))
    assert "USDT" in texts(bot)[-1]


def test_alerts_command_empty():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/alerts"))
    assert "нет" in texts(bot)[-1].lower()


def test_delalert_callback_removes_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    monkeypatch.setattr(B.alerts, "list_all", functools.partial(B.alerts.list_all, path=db))
    monkeypatch.setattr(B.alerts, "remove", functools.partial(B.alerts.remove, path=db))
    alert_id = B.alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400, path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": f"delalert:{alert_id}", "message": {"message_id": 3}}))
    assert B.alerts.list_all("1", path=db) == []
    method, params = bot.out[-1]
    assert method == "editMessageText" and "нет" in params["text"].lower()


def test_check_alerts_sends_message(monkeypatch):
    ad = make_ad("MEXC", "sell", 93.0)
    monkeypatch.setattr(B.alerts, "due", lambda snap, cfg: [(1, "1", "USDT", "sell", 92.0, 93.0, ad)])
    marked = []
    monkeypatch.setattr(B.alerts, "mark_fired", marked.append)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_alerts(snap([])))
    assert "Алерт сработал" in texts(bot)[-1]
    assert marked == [1]   # доставлено — помечаем сработавшим


class Flaky(Stub):
    """Telegram не принимает сообщения, пока fail задан: «raise» — сбой сети, число — ответ ok=False с этим
    error_code, иначе ok=False (429). Попытки отправки — в self.tries."""
    def __init__(self, cfg, fail="raise"):
        super().__init__(cfg)
        self.fail = fail
        self.tries = 0
        self.fancy = False   # без разового повтора с обычными кнопками (_fancy_failed)

    def _failure(self):
        self.tries += 1
        if self.fail == "raise":
            raise aiohttp.ClientConnectionError("telegram down")
        code = self.fail if isinstance(self.fail, int) else 429
        return {"ok": False, "error_code": code, "description": f"error {code}"}

    async def call(self, method, **p):
        if self.fail and method == "sendMessage":
            return self._failure()
        return await super().call(method, **p)

    async def send_photo(self, png, caption, markup=None):
        if self.fail:
            return self._failure()
        return await super().send_photo(png, caption, markup)


def test_notify_not_marked_sent_when_telegram_raises(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Flaky(p2p.Config(min_profit=2.0))
    bot.live_scans = 1
    ds = [deal(5, "MEXC"), deal(4, "KuCoin")]
    asyncio.run(bot.notify(snap(ds)))
    assert bot.sent == {} and not photos(bot)
    bot.fail = None                      # сеть вернулась — обе связки уходят в следующем скане
    asyncio.run(bot.notify(snap(ds)))
    assert len(photos(bot)) == 2 and len(bot.sent) == 2


def test_notify_not_marked_sent_when_not_ok(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Flaky(p2p.Config(min_profit=2.0), fail="not_ok")
    bot.live_scans = 1
    asyncio.run(bot.notify(snap([deal(5)])))
    assert bot.sent == {}
    bot.fail = None
    asyncio.run(bot.notify(snap([deal(5)])))
    assert len(photos(bot)) == 1 and len(bot.sent) == 1


def test_notify_permanent_refusal_consumes_signal(monkeypatch):
    """400/403 повтор не исправит: связка считается отправленной и не долбит Telegram каждый скан."""
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    for code in (400, 403):
        bot = Flaky(p2p.Config(min_profit=2.0), fail=code)
        bot.live_scans = 1
        asyncio.run(bot.notify(snap([deal(5)])))
        tries = bot.tries
        assert tries and bot._deal_key(deal(5)) in bot.sent, code
        asyncio.run(bot.notify(snap([deal(5)])))
        assert bot.tries == tries, code


def test_notify_rate_limit_retries_next_scan(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Flaky(p2p.Config(min_profit=2.0), fail=429)
    bot.live_scans = 1
    asyncio.run(bot.notify(snap([deal(5)])))
    tries = bot.tries
    asyncio.run(bot.notify(snap([deal(5)])))
    assert bot.sent == {} and bot.tries > tries


def test_telegram_send_errors_logged_without_bot_token(monkeypatch, caplog):
    """str() ошибки aiohttp содержит URL запроса, а в нём токен бота — в лог только код и причина."""
    url = URL("https://api.telegram.org/bot123456:TEST-BOT-TOKEN/sendMessage")
    err = aiohttp.ClientResponseError(aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url), (),
                                      status=502, message="Bad Gateway")
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    ad = make_ad("MEXC", "sell", 93.0)
    monkeypatch.setattr(B.alerts, "due", lambda snap, cfg: [(1, "1", "USDT", "sell", 92.0, 93.0, ad)])
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans, bot.fancy = 1, False

    async def call(method, **p):
        raise err

    async def send_photo(png, caption, markup=None):
        return {"ok": False, "description": "no photo"}

    bot.call, bot.send_photo = call, send_photo
    caplog.set_level(logging.WARNING)
    assert asyncio.run(bot.delete_message(5)) is False
    asyncio.run(bot.notify(snap([deal(5)])))
    asyncio.run(bot.check_alerts(snap([])))
    assert caplog.text.count("HTTP 502") == 3
    assert "TEST-BOT-TOKEN" not in caplog.text


def _alerts_db(tmp_path, monkeypatch):
    db = str(tmp_path / "alerts.db")
    for name in ("add", "due", "mark_fired", "list_all"):
        monkeypatch.setattr(B.alerts, name, functools.partial(getattr(B.alerts, name), path=db))
    return {("MEXC", "sell", "USDT"): make_ad("MEXC", "sell", 93.0)}


def test_check_alerts_keeps_alert_when_send_fails(tmp_path, monkeypatch):
    best = _alerts_db(tmp_path, monkeypatch)
    B.alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400)
    s = p2p.Snapshot(88.0, "test", {}, best, [], {}, {}, {})
    bot = Flaky(p2p.Config())
    asyncio.run(bot.check_alerts(s))
    assert len(B.alerts.list_all("1")) == 1          # одноразовый не удалён: сообщение не дошло
    bot.fail = None
    asyncio.run(bot.check_alerts(s))
    assert "Алерт сработал" in texts(bot)[-1]
    assert B.alerts.list_all("1") == []


def test_check_alerts_marks_only_delivered(tmp_path, monkeypatch):
    best = _alerts_db(tmp_path, monkeypatch)
    B.alerts.add("2", "USDT", "sell", 92.0, time.time() + 86400)   # чат, куда Telegram не доставил
    B.alerts.add("1", "USDT", "sell", 91.0, time.time() + 86400)
    s = p2p.Snapshot(88.0, "test", {}, best, [], {}, {}, {})
    bot = Stub(p2p.Config())

    async def call(method, **p):
        bot.out.append((method, p))
        return {"ok": p.get("chat_id") != "2", "description": "Bad Request: chat not found"}

    bot.call = call
    asyncio.run(bot.check_alerts(s))
    assert len(texts(bot)) == 2
    assert len(B.alerts.list_all("2")) == 1 and B.alerts.list_all("1") == []


def test_check_alerts_permanent_refusal_marks_fired(tmp_path, monkeypatch):
    """429 — алерт ждёт следующего скана; 403 (бот заблокирован) — повтор не поможет, одноразовый израсходован."""
    best = _alerts_db(tmp_path, monkeypatch)
    B.alerts.add("1", "USDT", "sell", 92.0, time.time() + 86400)
    s = p2p.Snapshot(88.0, "test", {}, best, [], {}, {}, {})
    bot = Flaky(p2p.Config(), fail=429)
    asyncio.run(bot.check_alerts(s))
    assert len(B.alerts.list_all("1")) == 1
    bot.fail = 403
    asyncio.run(bot.check_alerts(s))
    assert B.alerts.list_all("1") == []


def test_mark_done_unknown_id_not_logged(monkeypatch):
    logged = []
    monkeypatch.setattr(B.trades, "log_trade", lambda *a, **k: logged.append(a))
    bot = Stub(p2p.Config())
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, 999))
    assert not logged
    assert "устарел" in bot.out[-1][1]["text"]


def test_calc_command_scans_with_custom_amount(offline, monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "top_chart", lambda snap, c: b"png")
    bot = Stub(p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"],
                          min_orders=0, min_rate=0, amount=50000))
    asyncio.run(bot.handle("/calc 20000"))
    caps = [p["caption"] for m, p in photos(bot)]
    assert any("20 000" in c for c in caps)
    assert bot.cfg.amount == 50000            # настройки не изменились


def test_calc_command_rejects_garbage_amount():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/calc много"))
    assert "сумму" in texts(bot)[-1].lower()
    assert not bot.out or bot.out[-1][0] == "sendMessage"


def test_calc_command_needs_argument():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/calc"))
    assert "сумма" in texts(bot)[-1].lower()


def test_settings_view_has_custom_amount_button():
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "amt_custom" for b in buttons)


def test_amt_custom_button_arms_waiting_state():
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "amt_custom", "message": {"message_id": 1}}))
    assert bot.awaiting_amount
    assert "сумму" in texts(bot)[-1].lower()


def test_custom_amount_text_scans_and_saves(tmp_path, monkeypatch, offline):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B, "top_chart", lambda snap, c: b"png")
    env = tmp_path / ".env"
    env.write_text("AMOUNT=50000\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(exchanges=["bybit", "htx", "kucoin", "mexc", "bitpapa"], assets=["USDT"],
                          min_orders=0, min_rate=0, amount=50000))
    bot.awaiting_amount = True
    asyncio.run(bot.handle("30 000"))
    assert not bot.awaiting_amount
    assert bot.cfg.amount == 30000
    assert "AMOUNT=30000" in env.read_text(encoding="utf-8")
    caps = [p["caption"] for m, p in photos(bot)]
    assert any("30 000" in c for c in caps)


def test_custom_amount_garbage_reports_error():
    bot = Stub(p2p.Config())
    bot.awaiting_amount = True
    asyncio.run(bot.handle("ерунда"))
    assert not bot.awaiting_amount
    assert "сумму" in texts(bot)[-1].lower()


def test_awaiting_amount_reset_by_other_button():
    bot = Stub(p2p.Config())
    bot.awaiting_amount = True
    asyncio.run(bot.on_callback({"id": "1", "data": "best", "message": {"message_id": 1}}))
    assert not bot.awaiting_amount


def test_awaiting_amount_reset_by_other_command():
    bot = Stub(p2p.Config())
    bot.awaiting_amount = True
    asyncio.run(bot.handle("/best"))
    assert not bot.awaiting_amount


def test_accounts_view_lists_exchanges_with_status(tmp_path, monkeypatch):
    """Ключ подключён, но ещё не проверялся → «❓» (не «✅»): «✅» только после подтверждённой
    проверки только чтение (accounts.set_verified)."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "abcd1234", "secret")
    cfg = p2p.Config(exchanges=["bybit", "mexc", "htx"])
    text, kb = B.accounts_view(cfg)
    assert "❓ Bybit" in text and "➖ MEXC" in text and "➖ HTX" in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc:bybit" in callbacks and "acc:mexc" in callbacks


def test_accounts_view_shows_confirmed_readonly_and_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "abcd1234", "secret")
    accounts.set_verified("bybit", "ok")
    accounts.save_key("mexc", "k", "s")
    accounts.set_verified("mexc", "error", "Invalid api_key")
    cfg = p2p.Config(exchanges=["bybit", "mexc"])
    text, kb = B.accounts_view(cfg)
    assert "✅ Bybit" in text and "⚠️ MEXC" in text


def test_account_view_connected_shows_masked_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "abcd1234", "secret")
    text, kb = B.account_view("bybit")
    assert accounts.mask("abcd1234") in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc_check:bybit" in callbacks and "acc_del:bybit" in callbacks


def test_account_view_not_connected_offers_connect(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    text, kb = B.account_view("mexc")
    assert "не подключён" in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc_add:mexc" in callbacks


def test_account_view_hint_is_exchange_specific():
    bybit_text, _ = B.account_view("bybit")
    mexc_text, _ = B.account_view("mexc")
    assert "API Management" in mexc_text and "API Management" not in bybit_text
    assert "Create New Key" in bybit_text and "Create New Key" not in mexc_text
    assert "Read-Only" in bybit_text and "Read Info" in mexc_text


def test_acc_add_sends_exchange_specific_hint():
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_add:mexc", "message": {"message_id": 1}}))
    assert any("API Management" in p.get("text", "") for m, p in bot.out if m == "sendMessage")


def test_account_view_unsupported_exchange_has_no_connect_button(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    text, kb = B.account_view("bitpapa")
    assert "не реализовано" in text
    callbacks = [b.get("callback_data", "") for row in kb["inline_keyboard"] for b in row]
    assert not any(c.startswith("acc_add:") for c in callbacks)


def test_account_view_kucoin_offers_connect_button(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "no_such.json"))
    text, kb = B.account_view("kucoin")
    assert "не подключён" in text and "passphrase" in text
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "acc_add:kucoin" in callbacks


async def _perms_readonly(s, ex):
    """key_permissions: биржа подтвердила «только чтение»."""
    return True, ""


def test_acc_add_kucoin_arms_awaiting_key_three_step_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return False, "kucoin: подпись запросов пока не реализована"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    monkeypatch.setattr(B.accounts, "key_permissions", _perms_readonly)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_add:kucoin", "message": {"message_id": 1}}))
    assert bot.awaiting_key == {"ex": "kucoin", "step": "key"}

    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    assert bot.awaiting_key == {"ex": "kucoin", "step": "secret", "key": "APIKEY123"}

    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert bot.awaiting_key == {"ex": "kucoin", "step": "passphrase", "key": "APIKEY123", "secret": "SECRET456"}
    assert accounts.keys("kucoin") is None   # ещё не сохранён — ждём passphrase

    asyncio.run(bot.handle_key_input("PASS789", 57))
    assert bot.awaiting_key is None
    assert accounts.keys("kucoin") == ("APIKEY123", "SECRET456")
    assert accounts.passphrase("kucoin") == "PASS789"


def test_settings_view_has_accounts_button():
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "accounts" for b in buttons)


def test_acc_add_callback_arms_awaiting_key():
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_add:bybit", "message": {"message_id": 1}}))
    assert bot.awaiting_key == {"ex": "bybit", "step": "key"}
    assert "API key" in texts(bot)[-1]


def test_handle_key_input_flow_saves_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return True, "ключ рабочий, доступ только для чтения"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    monkeypatch.setattr(B.accounts, "key_permissions", _perms_readonly)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    assert ("deleteMessage", {"chat_id": "1", "message_id": 55}) in bot.out
    assert bot.awaiting_key == {"ex": "bybit", "step": "secret", "key": "APIKEY123"}

    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert bot.awaiting_key is None
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert any("✅ Подключено" in t for t in texts(bot))


def test_handle_key_input_reports_failed_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return False, "Invalid api_key"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    monkeypatch.setattr(B.accounts, "key_permissions", _perms_readonly)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert any("Invalid api_key" in t for t in texts(bot))


def test_on_update_routes_plain_text_to_key_input_when_awaiting():
    bot = Stub(p2p.Config())
    bot.chat_id = "1"
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.on_update({"message": {"chat": {"id": 1}, "text": "APIKEY123", "message_id": 7}}))
    assert ("deleteMessage", {"chat_id": "1", "message_id": 7}) in bot.out
    assert bot.awaiting_key == {"ex": "bybit", "step": "secret", "key": "APIKEY123"}


def test_on_update_command_bypasses_key_input():
    bot = Stub(p2p.Config())
    bot.chat_id = "1"
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.on_update({"message": {"chat": {"id": 1}, "text": "/best", "message_id": 7}}))
    assert not any(m == "deleteMessage" for m, _ in bot.out)
    assert bot.awaiting_key is None


def test_other_callback_resets_awaiting_key():
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.on_callback({"id": "1", "data": "best", "message": {"message_id": 1}}))
    assert bot.awaiting_key is None


def test_command_resets_awaiting_key():
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle("/best"))
    assert bot.awaiting_key is None


def test_acc_check_callback_reports_status(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return False, "bad key"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    monkeypatch.setattr(B.accounts, "key_permissions", _perms_readonly)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert "bad key" in texts(bot)[-1]


def test_acc_check_callback_escapes_error_html(monkeypatch):
    async def fake_verify(s, ex):
        return False, "HTTP 400: <bad request>"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert "&lt;bad request&gt;" in texts(bot)[-1] and "<bad" not in texts(bot)[-1]


def test_handle_key_input_escapes_error_html(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_verify(s, ex):
        return False, "HTTP 400: <bad request>"

    monkeypatch.setattr(B.accounts, "verify", fake_verify)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert any("&lt;bad request&gt;" in t for t in texts(bot))
    assert not any("<bad" in t for t in texts(bot))


def test_check_accounts_error_log_has_no_url_with_key(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("htx", "AKID-TEST-KEY-1234", "s")
    url = URL("https://api.htx.com/v1/query/deposit-withdraw?AccessKeyId=AKID-TEST-KEY-1234&Signature=abc%3D")

    async def fake_history(s, ex, limit=20):
        raise aiohttp.ClientResponseError(aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url), (),
                                          status=403, message="Forbidden")

    monkeypatch.setattr(B.accounts, "account_history", fake_history)
    caplog.set_level(logging.WARNING)
    asyncio.run(Stub(p2p.Config()).check_accounts())
    assert "HTTP 403" in caplog.text
    assert "AKID-TEST-KEY-1234" not in caplog.text and "Signature" not in caplog.text


def test_acc_del_callback_removes_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_del:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") is None
    assert any("удалён" in t for t in texts(bot))


def _clear_env_keys(monkeypatch):
    for ex in ("BYBIT", "MEXC", "HTX", "KUCOIN"):
        for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
            monkeypatch.delenv(f"{ex}_{suffix}", raising=False)


def test_acc_del_callback_disables_env_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    monkeypatch.setenv("BYBIT_API_KEY", "envkey")
    monkeypatch.setenv("BYBIT_API_SECRET", "envsecret")
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_del:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") is None
    out = texts(bot)
    assert any("удалён" in t and ".env" in t for t in out)
    assert any("Ключ не подключён" in t for t in out)
    assert not any("Ключ подключён" in t for t in out)
    assert not any("envkey" in t or "envsecret" in t for t in out)


def test_acc_del_callback_without_key_says_not_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_del:bybit", "message": {"message_id": 1}}))
    out = texts(bot)
    assert "не был подключён" in out[0] and not any("удалён" in t for t in out)


def test_check_key_safety_disables_env_only_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _clear_env_keys(monkeypatch)
    monkeypatch.setenv("BYBIT_API_KEY", "envkey")
    monkeypatch.setenv("BYBIT_API_SECRET", "envsecret")
    calls = []

    async def fake_permissions(s, ex):
        calls.append(ex)
        return False, "торговля"

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert accounts.keys("bybit") is None
    assert len(texts(bot)) == 1 and ".env" in texts(bot)[0]
    asyncio.run(bot.check_key_safety())   # следующий старт: ключ выключен, проверять и предупреждать нечего
    assert calls == ["bybit"] and len(texts(bot)) == 1


def test_check_key_safety_removes_unsafe_key_and_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")

    async def fake_permissions(s, ex):
        return False, "торговля"

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert accounts.keys("bybit") is None
    warning = texts(bot)[-1]
    assert "торговля" in warning and "ТОЛЬКО для чтения" in warning


def test_check_key_safety_keeps_readonly_key_and_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")

    async def fake_permissions(s, ex):
        return True, ""

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert accounts.keys("mexc") == ("k", "s")
    assert texts(bot) == []


def test_check_key_safety_skips_exchanges_without_saved_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []

    async def fake_permissions(s, ex):
        calls.append(ex)
        return True, ""

    monkeypatch.setattr(B.accounts, "api_permissions", fake_permissions)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_key_safety())
    assert calls == []


# --- #10: права ключа проверяются при подключении и по «🔄 Проверить», а не только при старте ---

def _perms(result, calls=None):
    async def fake(s, ex):
        if calls is not None:
            calls.append("perm")
        return result
    return fake


def _verify_ok(calls=None):
    async def fake(s, ex):
        if calls is not None:
            calls.append("verify")
        return True, "ключ рабочий"
    return fake


def _verify_fail(msg="Invalid api_key"):
    async def fake(s, ex):
        return False, msg
    return fake


def test_handle_key_input_drops_trade_key_and_never_says_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((False, "торговля"), calls))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok(calls))
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.keys("bybit") is None
    assert calls == ["perm"]                    # баланс торгового ключа даже не запрашивали
    assert any("больше, чем чтение" in t and "торговля" in t for t in texts(bot))
    assert not any("✅ Подключено" in t for t in texts(bot))
    assert "не подключён" in texts(bot)[-1]     # карточка биржи — уже без ключа


def test_handle_key_input_checks_permissions_before_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, ""), calls))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok(calls))
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert calls == ["perm", "verify"]
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert "✅ Подключено (только чтение)" in texts(bot)


def test_handle_key_input_unverified_permissions_not_called_readonly(tmp_path, monkeypatch):
    """Биржа не ответила на запрос прав — ключ остаётся, но «только чтение» не обещаем."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((None, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    connected = [t for t in texts(bot) if t.startswith("✅ Подключено")]
    assert connected and "проверить не удалось" in connected[0]
    assert not any("только чтение" in t for t in texts(bot))


def test_handle_key_input_remembers_confirmed_readonly_status(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.verify_status("bybit") == ("ok", "")


def test_handle_key_input_unverified_permissions_status_is_unknown_not_error(tmp_path, monkeypatch):
    """safe=None (права не удалось узнать), но сам verify() прошёл — статус «неизвестно», не «ошибка»."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((None, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.verify_status("bybit") == ("unknown", "")


def test_handle_key_input_verify_failure_remembers_error_status(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_fail("Invalid api_key"))
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.verify_status("bybit") == ("error", "Invalid api_key")
    text, kb = B.account_view("bybit")
    assert "⚠️ Ошибка последней проверки: Invalid api_key" in text


def test_acc_check_updates_status_from_ok_to_error(tmp_path, monkeypatch):
    """Повторная «🔄 Проверить» с новым результатом перезаписывает прошлый статус."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert accounts.verify_status("bybit") == ("ok", "")
    monkeypatch.setattr(B.accounts, "verify", _verify_fail("network down"))
    asyncio.run(bot.on_callback({"id": "2", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert accounts.verify_status("bybit") == ("error", "network down")


def test_acc_check_drops_trade_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    calls = []
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((False, "вывод"), calls))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok(calls))
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") is None
    assert calls == ["perm"]
    assert any("больше, чем чтение" in t and "вывод" in t for t in texts(bot))
    assert not any("✅ Ключ рабочий" in t for t in texts(bot))


def test_acc_check_readonly_key_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") == ("k", "s")
    assert texts(bot)[-1] == "✅ Ключ рабочий (только чтение)"


def test_acc_check_unverified_permissions_not_called_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "k", "s")
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((None, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") == ("k", "s")
    assert texts(bot)[-1].startswith("✅ Ключ рабочий") and "проверить не удалось" in texts(bot)[-1]
    assert "(только чтение)" not in texts(bot)[-1]


def test_unverified_key_never_claimed_readonly_in_any_message(tmp_path, monkeypatch):
    """Права не проверены (safe=None): ни «Подключено», ни карточка биржи после него, ни «🔄 Проверить»,
    ни открытая заново карточка не утверждают «только чтение»."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((None, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    asyncio.run(bot.on_callback({"id": "2", "data": "acc:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert len(texts(bot)) >= 4 and "Ключ подключён" in texts(bot)[-1]
    assert not any("только чтение" in t for t in texts(bot))


def test_account_view_does_not_claim_readonly(tmp_path, monkeypatch):
    """Карточка биржи не знает, проверены ли права, — «Доступ: только чтение» не пишет."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "APIKEY123", "SECRET456")
    text, kb = B.account_view("bybit")
    assert "Ключ подключён" in text and "только чтение" not in text
    assert any(b.get("callback_data") == "acc_check:bybit" for row in kb["inline_keyboard"] for b in row)


# те же сценарии с настоящими key_permissions/verify — подменён только транспорт к бирже
BYBIT_TRADE_KEY = {"retCode": 0, "result": {"readOnly": 0, "permissions": {
    "ContractTrade": ["Order", "Position"], "Wallet": ["AccountTransfer"], "Spot": ["SpotTrade"]}}}
BYBIT_READONLY_KEY = {"retCode": 0, "result": {"readOnly": 1, "permissions": {"Spot": [], "Wallet": []}}}


def _bybit_transport(monkeypatch, query_api):
    paths = []

    async def fake_bybit_get(s, api_key, api_secret, path, params=None):
        paths.append(path)
        if path == "/v5/user/query-api":
            return query_api
        if path == "/v5/account/wallet-balance":
            return {"retCode": 0, "result": {"list": []}}
        raise AssertionError(f"unexpected bybit path {path}")

    monkeypatch.setattr(accounts, "bybit_get", fake_bybit_get)
    return paths


def test_connect_bybit_trade_key_via_api_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    paths = _bybit_transport(monkeypatch, BYBIT_TRADE_KEY)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.keys("bybit") is None
    assert paths == ["/v5/user/query-api"]
    assert not any("только чтение)" in t for t in texts(bot))
    assert any("больше, чем чтение" in t for t in texts(bot))


def test_connect_bybit_readonly_key_via_api_says_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    paths = _bybit_transport(monkeypatch, BYBIT_READONLY_KEY)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert paths == ["/v5/user/query-api", "/v5/account/wallet-balance"]
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert "✅ Подключено (только чтение)" in texts(bot)


def test_connect_bybit_permissions_api_error_not_called_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    _bybit_transport(monkeypatch, {"retCode": 10005, "retMsg": "Permission denied"})
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert any(t.startswith("✅ Подключено") and "проверить не удалось" in t for t in texts(bot))
    assert not any("только чтение" in t for t in texts(bot))   # и в карточке биржи после «Подключено»


def test_acc_check_bybit_trade_key_via_api_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("bybit", "APIKEY123", "SECRET456")
    _bybit_transport(monkeypatch, BYBIT_TRADE_KEY)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "acc_check:bybit", "message": {"message_id": 1}}))
    assert accounts.keys("bybit") is None
    assert not any("✅ Ключ рабочий" in t for t in texts(bot))


def test_connect_mexc_trade_withdraw_key_via_api_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))

    async def fake_mexc_get(s, api_key, api_secret, path, params=None):
        assert path == "/api/v3/account"
        return {"canTrade": True, "canWithdraw": True, "balances": []}

    monkeypatch.setattr(accounts, "mexc_get", fake_mexc_get)
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "mexc", "step": "secret", "key": "k"}
    asyncio.run(bot.handle_key_input("s", 56))
    assert accounts.keys("mexc") is None
    assert any("больше, чем чтение" in t and "торговля, вывод" in t for t in texts(bot))
    assert not any("✅ Подключено" in t for t in texts(bot))


# --- #11: «сообщение удалено» — только если Telegram подтвердил deleteMessage ---

class RefusingDelete(Stub):
    """Telegram отказывает в deleteMessage (группа без прав админа и т.п.)."""
    async def call(self, method, **p):
        r = await super().call(method, **p)
        if method == "deleteMessage":
            return {"ok": False, "error_code": 400, "description": "Bad Request: message can't be deleted"}
        return r


class RaisingDelete(Stub):
    """deleteMessage падает сетевой ошибкой."""
    async def call(self, method, **p):
        if method == "deleteMessage":
            raise aiohttp.ClientConnectionError("network down")
        return await super().call(method, **p)


def test_handle_key_input_warns_when_delete_refused(caplog):
    bot = RefusingDelete(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    assert ("deleteMessage", {"chat_id": "1", "message_id": 55}) in bot.out
    assert not any("удалено" in t for t in texts(bot))
    assert any("вручную" in t for t in texts(bot))
    assert bot.awaiting_key == {"ex": "bybit", "step": "secret", "key": "APIKEY123"}   # ввод продолжается
    assert "APIKEY123" not in caplog.text                                               # ключ не в логах


def test_handle_key_input_warns_when_delete_raises():
    bot = RaisingDelete(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))     # исключение не вылетает наружу
    assert not any("удалено" in t for t in texts(bot))
    assert any("вручную" in t for t in texts(bot))
    assert bot.awaiting_key == {"ex": "bybit", "step": "secret", "key": "APIKEY123"}


def test_handle_key_input_final_step_warns_when_delete_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, ""), calls))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok(calls))
    bot = RefusingDelete(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "secret", "key": "APIKEY123"}
    asyncio.run(bot.handle_key_input("SECRET456", 56))
    assert accounts.keys("bybit") == ("APIKEY123", "SECRET456")
    assert calls == ["perm", "verify"]
    assert any("вручную" in t for t in texts(bot))
    assert "✅ Подключено (только чтение)" in texts(bot)


def test_handle_key_input_kucoin_every_step_warns_when_delete_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    monkeypatch.setattr(B.accounts, "key_permissions", _perms((True, "")))
    monkeypatch.setattr(B.accounts, "verify", _verify_ok())
    bot = RefusingDelete(p2p.Config())
    bot.awaiting_key = {"ex": "kucoin", "step": "key"}
    for n, (text, mid) in enumerate((("APIKEY123", 55), ("SECRET456", 56), ("PASS789", 57)), 1):
        asyncio.run(bot.handle_key_input(text, mid))
        assert sum("вручную" in t for t in texts(bot)) == n    # предупреждение на каждом шаге
    assert not any("удалено" in t for t in texts(bot))
    assert accounts.passphrase("kucoin") == "PASS789"


def test_handle_key_input_delete_ok_still_says_deleted():
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", 55))
    assert texts(bot) == ["Ключ получен, сообщение удалено. Теперь пришли <b>secret</b> для Bybit."]


def test_handle_key_input_without_message_id_claims_nothing():
    bot = Stub(p2p.Config())
    bot.awaiting_key = {"ex": "bybit", "step": "key"}
    asyncio.run(bot.handle_key_input("APIKEY123", None))
    assert not any(m == "deleteMessage" for m, _ in bot.out)
    assert not any("удалено" in t or "вручную" in t for t in texts(bot))


def test_stats_view_reports_counts(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "stats", functools.partial(B.trades.stats, path=db))
    trades.log_trade(deal(2.5), 50000, path=db)
    bot = Stub(p2p.Config())
    text = bot.stats_view()
    assert "За сегодня: 1 сделок" in text and "За неделю: сделок нет" not in text
    assert "За месяц" in text


def _nick_deal(buy_nick, sell_nick, pays=("T-Bank",)):
    return 2.0, dataclasses.replace(make_ad("Bybit", "buy", 85.0, pays=pays), nick=buy_nick), \
        dataclasses.replace(make_ad("MEXC", "sell", 90.0), nick=sell_nick), "маршрут"


def test_stats_view_counterparties_block(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    for name in ("stats", "month_banks", "counterparties"):
        monkeypatch.setattr(B.trades, name, functools.partial(getattr(B.trades, name), path=db))
    bot = Stub(p2p.Config())
    assert "Контрагенты" not in bot.stats_view()                           # сделок нет — блока нет
    now = time.time()
    for i in range(4):                                                     # 8 разных мерчантов с Т-Банка —
        trades.log_trade(_nick_deal(f"b{i}", f"s{i}"), 10000, path=db, ts=now)   # 80% от 10 в день
    trades.log_trade(_nick_deal("b0", "s9", pays=("Sberbank",)), 10000, path=db, ts=now)
    text = bot.stats_view()
    assert "Контрагенты по картам (ориентир ЦБ 16-МР: &gt;10 в день, &gt;50 в месяц)" in text
    assert "• Т-Банк: сегодня 8 ⚠️, за месяц 8\n" in text                   # за месяц 8 из 50 — без ⚠️
    assert "• Сбер: сегодня 2, за месяц 2\n" in text


def test_traps_view_empty():
    p2p.TRAPS_LOG.clear()
    text = B.traps_view()
    assert "Пока ни одной" in text


def test_paper_view_off_no_cycles_no_balance(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    bot = Stub(p2p.Config())
    text = bot.paper_view()
    assert "⚪ выключен" in text
    assert "Открытых кругов нет." in text
    assert "За сегодня: кругов не было" in text
    assert "Виртуальный баланс" not in text


def test_paper_view_shows_open_cycle_and_stats(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    now = time.time()
    paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now - 120)   # остаётся открытым
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=now)
    paper.init_balance(10000, path=db)
    paper.finish_cycle(cid, "failed_sell", realized_pct=0.0, note="цена ушла", path=db, ts=now)
    text = Stub(p2p.Config()).paper_view()
    assert "🟢 включён" in text
    assert "Bybit→MEXC" in text and "стадия «оплата»" in text
    assert "исполнилось 0" in text and "сорвалось 1 (продажа 1)" in text
    assert "Виртуальный баланс: 10 000 ₽ (изменение с начала: +0 ₽)" in text


def test_paper_view_avg_diff_and_balance_change(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.init_balance(10000, path=db)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid, "done", realized_pct=2.5, path=db)
    text = Stub(p2p.Config()).paper_view()
    assert "исполнилось 1" in text and "факт vs план +0.50 п.п." in text
    assert "Виртуальный баланс: 10 250 ₽ (изменение с начала: +250 ₽)" in text


def test_paper_view_shows_bank_limit_progress(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0, pays=("SBP",)), make_ad("MEXC", "sell", 90.0)
    paper.start_cycle(60000, buy, sell, "route", 2.0, path=db)
    paper.start_cycle(60000, buy, sell, "route", 2.0, path=db)   # 60к + 60к по СБП с Т-Банка — выше 100к
    text = Stub(p2p.Config()).paper_view()
    assert "Лимит СБП за месяц (виртуальный оборот):" in text
    assert "⚠️ Т-Банк: 120 000 ₽ / 100 000 ₽" in text


def test_paper_view_no_bank_section_when_no_cycles(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    text = Stub(p2p.Config()).paper_view()
    assert "Лимит СБП" not in text


def test_paper_digest_line_none_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    assert Stub(p2p.Config()).paper_digest_line() is None


def test_paper_digest_line_none_when_no_cycles_today(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    assert Stub(p2p.Config()).paper_digest_line() is None


def test_paper_digest_line_summarizes_todays_cycles(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.init_balance(10000, path=db)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid, "done", realized_pct=2.5, path=db)
    cid2 = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid2, "failed_sell", realized_pct=0.0, note="цена ушла", path=db)
    line = Stub(p2p.Config()).paper_digest_line()
    assert line.startswith("🧪 Сухой прогон за сутки: 2 кругов, исполнилось 1, сорвалось 1 (продажа 1)")
    assert "факт vs план +0.50 п.п." in line
    assert "баланс 10 250 ₽ (+250 ₽)" in line


def test_cmd_paper_on_off_writes_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper("on"))
    assert "PAPER=1" in env.read_text()
    assert "включён" in texts(bot)[-1]
    asyncio.run(bot.cmd_paper("off"))
    assert "PAPER=0" in env.read_text()
    assert "выключен" in texts(bot)[-1]


def _fill_ladder_cycles(db, done=18, failed=2, ts=None):
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    for _ in range(done):
        cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=ts)
        paper.finish_cycle(cid, "done", realized_pct=2.0, path=db, ts=ts)
    for _ in range(failed):
        cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db, ts=ts)
        paper.finish_cycle(cid, "failed_sell", realized_pct=0.0, note="цена ушла", path=db, ts=ts)


def test_check_paper_ladder_sends_up_suggestion_with_button(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    _fill_ladder_cycles(db)   # 20 кругов, срывов 10%, факт == план — критерии выполнены
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_paper_ladder())
    msgs = [(t, p.get("reply_markup")) for m, p in bot.out if m == "sendMessage"
            for t in [p["text"]] if "20 000" in t]
    assert len(msgs) == 1
    text, markup = msgs[0]
    assert "стабилен" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "paper_ladder:20000"


def test_check_paper_ladder_sends_down_suggestion(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "20000")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    now = time.time()
    _fill_ladder_cycles(db, done=2, failed=3, ts=now)   # неделя: 60% сорвалось > 40%
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_paper_ladder())
    msgs = [t for t in texts(bot) if "10 000" in t]
    assert len(msgs) == 1 and "срывов" in msgs[0]


def test_check_paper_ladder_respects_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    _fill_ladder_cycles(db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_paper_ladder())
    asyncio.run(bot.check_paper_ladder())   # второй раз сразу — кулдаун не прошёл
    msgs = [t for t in texts(bot) if "20 000" in t]
    assert len(msgs) == 1


def test_check_paper_ladder_noop_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    _fill_ladder_cycles(db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_paper_ladder())
    assert not bot.out


def test_check_paper_ladder_noop_without_chat_id(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    monkeypatch.setenv("PAPER_AMOUNT", "10000")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    _fill_ladder_cycles(db)
    bot = Stub(p2p.Config())
    bot.chat_id = None
    asyncio.run(bot.check_paper_ladder())
    assert not bot.out


def test_paper_ladder_callback_saves_env_and_confirms(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "paper_ladder:20000", "message": {"message_id": 9}}))
    assert "PAPER_AMOUNT=20000" in env.read_text()
    assert "20 000" in texts(bot)[-1]


def test_cmd_paper_amount_writes_env(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper("amount 20000"))
    assert "PAPER_AMOUNT=20000" in env.read_text()
    assert "20 000" in texts(bot)[-1]


def test_cmd_paper_amount_bad_value():
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper("amount не число"))
    assert "Не понял сумму" in texts(bot)[-1]


def test_cmd_paper_no_arg_shows_view(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper(""))
    assert "🧪 <b>Сухой прогон</b>" in texts(bot)[-1]


def test_dispatch_paper_routes_to_cmd_paper(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    bot = Stub(p2p.Config())
    asyncio.run(bot.dispatch("/paper", ""))
    assert "🧪 <b>Сухой прогон</b>" in texts(bot)[-1]


def test_cmd_paper_report_no_cycles_sends_text_only(monkeypatch, tmp_path):
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper("report"))
    assert "завершённых кругов ещё нет" in texts(bot)[-1]
    assert not [m for m in bot.out if m[0] == "sendDocument"]


def test_cmd_paper_report_sends_summary_and_csv(monkeypatch, tmp_path):
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    csv_path = str(tmp_path / "paper_report.csv")
    monkeypatch.setattr(B.paper, "write_report_csv", functools.partial(B.paper.write_report_csv, path=csv_path))
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid, "done", realized_pct=2.5, path=db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.cmd_paper("report"))
    text = texts(bot)[-1]
    assert "Bybit→MEXC (USDT→USDT)" in text and "план +2.00%" in text and "факт +2.50%" in text
    docs = [m for m in bot.out if m[0] == "sendDocument"]
    assert len(docs) == 1 and docs[0][1]["path"] == csv_path
    assert os.path.exists(csv_path)


def test_traps_view_lists_reasons():
    p2p.TRAPS_LOG.clear()
    p2p.TRAPS_LOG.append(p2p._trap_entry(make_ad(side="sell", price=120.0), ref=90.0, cfg=p2p.Config()))
    text = B.traps_view()
    assert "продать" in text and "выше рынка" in text


def _groups(*ads):
    """Собрать snap.groups так же, как это делает scan(): по (ex, side, asset), отсортировано по цене."""
    g = {}
    for a in ads:
        g.setdefault((a.ex, a.side, a.asset), []).append(a)
    for key, grp in g.items():
        grp.sort(key=lambda a: a.price, reverse=(key[1] == "sell"))
    return g


def test_maker_view_lists_quotes_per_venue():
    g = _groups(make_ad("MEXC", "buy", 90.0), make_ad("MEXC", "sell", 92.0))
    s = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=g)
    cfg = p2p.Config(exchanges=["mexc"])
    text = B.maker_view(s, cfg, "USDT")
    assert "MEXC" in text and "купить" in text and "продать" in text
    assert "переплата" in text and "недополучим" in text


def test_maker_view_skips_venue_without_both_sides():
    g = _groups(make_ad("MEXC", "buy", 90.0))   # только одна сторона стакана
    s = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=g)
    cfg = p2p.Config(exchanges=["mexc"])
    text = B.maker_view(s, cfg, "USDT")
    assert "Нет обеих сторон стакана" in text


def test_maker_command_requires_known_asset():
    bot = Stub(p2p.Config())
    bot.last = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})
    asyncio.run(bot.maker("DOGE"))
    assert "не отслеживается" in texts(bot)[-1]


def test_maker_command_waits_for_first_scan():
    bot = Stub(p2p.Config())
    asyncio.run(bot.maker("USDT"))
    assert texts(bot)[-1] == B.WAIT


def test_maker_command_sends_quotes():
    g = _groups(make_ad("MEXC", "buy", 90.0), make_ad("MEXC", "sell", 92.0))
    bot = Stub(p2p.Config(exchanges=["mexc"]))
    bot.last = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=g)
    asyncio.run(bot.maker("usdt"))
    assert "MEXC" in texts(bot)[-1]


def test_banks_view_lists_volume_per_bank():
    g = _groups(make_ad("MEXC", "buy", 90.0, pays=("T-Bank",), max_amt=50000, avail=1000),
                make_ad("MEXC", "sell", 92.0, pays=("Sberbank",), max_amt=20000, avail=1000))
    s = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=g)
    cfg = p2p.Config(exchanges=["mexc"])
    text = B.banks_view(s, cfg, "USDT")
    assert "MEXC" in text and "T-Bank" in text and "Sberbank" in text
    assert "купить" in text and "продать" in text


def test_banks_view_no_ads_anywhere():
    s = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})
    cfg = p2p.Config(exchanges=["mexc"])
    text = B.banks_view(s, cfg, "USDT")
    assert "Нет объявлений" in text


def test_banks_command_requires_known_asset():
    bot = Stub(p2p.Config())
    bot.last = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})
    asyncio.run(bot.banks("DOGE"))
    assert "не отслеживается" in texts(bot)[-1]


def test_banks_command_waits_for_first_scan():
    bot = Stub(p2p.Config())
    asyncio.run(bot.banks("USDT"))
    assert texts(bot)[-1] == B.WAIT


def test_banks_command_sends_liquidity():
    g = _groups(make_ad("MEXC", "buy", 90.0, pays=("T-Bank",), max_amt=50000, avail=1000))
    bot = Stub(p2p.Config(exchanges=["mexc"]))
    bot.last = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {}, groups=g)
    asyncio.run(bot.banks("usdt"))
    assert "T-Bank" in texts(bot)[-1]


def test_portfolio_view_no_exchanges_connected():
    text = B.portfolio_view({}, None)
    assert "Ни одна биржа не подключена" in text


def test_portfolio_view_sums_total_in_rub():
    port = {"bybit": {"USDT": 10.0, "BTC": 0.01}, "mexc": {"USDT": 5.0}}
    snap = p2p.Snapshot(88.0, "test", {"USDT": 88.0, "BTC": 5_000_000.0}, {}, [], {}, {}, {})
    text = B.portfolio_view(port, snap)
    assert "Bybit" in text and "MEXC" in text
    assert "10 USDT" in text and "0.01 BTC" in text
    # 10*88 + 0.01*5_000_000 + 5*88 = 880 + 50000 + 440 = 51320
    assert "51 320" in text


def test_portfolio_view_missing_ref_marked_instead_of_crashing():
    port = {"bybit": {"TON": 100.0}}
    snap = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})
    text = B.portfolio_view(port, snap)
    assert "нет ориентира" in text


def test_portfolio_rows_matches_totals_from_view():
    port = {"bybit": {"USDT": 10.0, "BTC": 0.01}, "mexc": {"USDT": 5.0}}
    snap = p2p.Snapshot(88.0, "test", {"USDT": 88.0, "BTC": 5_000_000.0}, {}, [], {}, {}, {})
    rows, total = B.portfolio_rows(port, snap)
    assert total == 51_320.0
    names = [name for name, _ in rows]
    assert names == ["Bybit", "MEXC"]
    assert rows[0][1] == [("BTC", 0.01, 50_000.0), ("USDT", 10.0, 880.0)]


def test_portfolio_rows_without_snapshot_has_no_rub_values():
    port = {"bybit": {"USDT": 100.0, "USDC": 50.0}}
    rows, total = B.portfolio_rows(port, None)
    assert rows == [("Bybit", [("USDC", 50.0, None), ("USDT", 100.0, None)])]
    assert total == 0.0


def test_portfolio_view_without_snapshot_says_rate_not_received():
    text = B.portfolio_view({"bybit": {"USDT": 100.0, "USDC": 50.0}}, None)
    assert "100 USDT" in text and "курс ещё не получен" in text
    assert "≈ 100 ₽" not in text and "Итого:</b> ≈" not in text and "нет ориентира" not in text


def test_balance_before_first_scan_sends_text_without_card(monkeypatch):
    async def fake_portfolio(s):
        return {"bybit": {"USDT": 100.0}}

    def boom(rows, total):
        raise AssertionError("карточка до первого скана не нужна")

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    monkeypatch.setattr(B, "portfolio_card", boom)
    bot = Stub(p2p.Config())
    assert bot.last is None
    asyncio.run(bot.handle("/balance"))
    assert not photos(bot)
    (method, params), = bot.out
    assert method == "sendMessage" and "курс ещё не получен" in params["text"] and "≈ 100 ₽" not in params["text"]
    assert params["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "balance"


def test_balance_command_sends_card_with_refresh_button(monkeypatch):
    async def fake_portfolio(s):
        return {"mexc": {"USDT": 1.0}}

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    monkeypatch.setattr(B, "portfolio_card", lambda rows, total: b"png")
    bot = Stub(p2p.Config())
    bot.last = p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, [], {}, {}, {})
    asyncio.run(bot.handle("/balance"))
    pics = photos(bot)
    assert len(pics) == 1
    assert "MEXC" in pics[0][1]["caption"]
    assert pics[0][1]["markup"]["inline_keyboard"][0][0]["callback_data"] == "balance"


def test_balance_falls_back_to_text_when_card_render_fails(monkeypatch):
    async def fake_portfolio(s):
        return {"mexc": {"USDT": 1.0}}

    def boom(rows, total):
        raise ValueError("render error")

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    monkeypatch.setattr(B, "portfolio_card", boom)
    bot = Stub(p2p.Config())
    bot.last = p2p.Snapshot(88.0, "test", {"USDT": 88.0}, {}, [], {}, {}, {})
    asyncio.run(bot.handle("/balance"))
    assert not photos(bot)
    assert any("MEXC" in t for t in texts(bot))


def test_balance_callback_refreshes(monkeypatch):
    async def fake_portfolio(s):
        return {}

    monkeypatch.setattr(B.accounts, "portfolio", fake_portfolio)
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "balance", "message": {"message_id": 1}}))
    assert any("Баланс" in p.get("text", "") for m, p in bot.out if m == "sendMessage")


def test_hist_text_formats_each_kind():
    dep = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    wd = {"kind": "withdraw", "asset": "USDT", "amount": 50.0, "ts": 1.0}
    trade = {"kind": "trade", "asset": "TON", "side": "buy", "amount": 10.0, "price": 3.5, "ts": 1.0}
    p2p_order = {"id": "1", "side": "sell", "asset": "USDT", "fiat": "RUB", "amount": 20.0, "price": 88.0, "ts": 1.0}
    assert "пришёл депозит" in B.hist_text("mexc", dep) and "100" in B.hist_text("mexc", dep)
    assert "исполнен вывод" in B.hist_text("mexc", wd)
    assert "купил" in B.hist_text("mexc", trade) and "TON" in B.hist_text("mexc", trade)
    assert "продал" in B.hist_text("bybit", p2p_order) and "RUB" in B.hist_text("bybit", p2p_order)


def test_hist_key_uses_id_or_composite():
    assert B.hist_key({"id": "42", "kind": "trade"}) == "42"
    a = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    b = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 2.0}
    assert B.hist_key(a) != B.hist_key(b)


def test_check_accounts_first_poll_is_silent_then_new_items_notify(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    hist = [{"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}]

    async def fake_history(s, ex, limit=20):
        return hist if ex == "mexc" else None

    monkeypatch.setattr(B.accounts, "account_history", fake_history)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_accounts())
    assert texts(bot) == []          # первый опрос — только запоминаем

    hist.append({"kind": "withdraw", "asset": "USDT", "amount": 30.0, "ts": 2.0})
    asyncio.run(bot.check_accounts())
    msgs = texts(bot)
    assert len(msgs) == 1 and "исполнен вывод" in msgs[0]

    asyncio.run(bot.check_accounts())
    assert len(texts(bot)) == 1       # повтор той же истории не шлём


def _history_bot(tmp_path, monkeypatch, answers):
    """Бот с ключом MEXC; account_history отдаёт по очереди ответы из answers."""
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    accounts.save_key("mexc", "k", "s")
    answers = list(answers)

    async def fake_history(s, ex, limit=20):
        return answers.pop(0) if ex == "mexc" else None

    monkeypatch.setattr(B.accounts, "account_history", fake_history)
    return Stub(p2p.Config())


def test_check_accounts_empty_history_then_first_deposit_notifies(tmp_path, monkeypatch):
    dep = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    bot = _history_bot(tmp_path, monkeypatch, [[], [dep], [dep]])
    asyncio.run(bot.check_accounts())
    assert bot.acc_seen["mexc"] == set() and texts(bot) == []   # пустой успешный ответ — это первый опрос
    asyncio.run(bot.check_accounts())
    msgs = texts(bot)
    assert len(msgs) == 1 and "пришёл депозит" in msgs[0]
    asyncio.run(bot.check_accounts())
    assert len(texts(bot)) == 1


def test_check_accounts_none_history_does_not_seed_baseline(tmp_path, monkeypatch):
    dep = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    bot = _history_bot(tmp_path, monkeypatch, [None, [dep]])
    asyncio.run(bot.check_accounts())
    assert bot.acc_seen.get("mexc") is None                   # ошибка — не первый опрос
    asyncio.run(bot.check_accounts())
    assert texts(bot) == [] and bot.acc_seen["mexc"] == {B.hist_key(dep)}


def test_check_accounts_partial_failure_does_not_forget_items(tmp_path, monkeypatch):
    """Источник депозитов на один опрос не ответил (в ленте только вывод): его записи не забываем —
    после восстановления старый депозит не приходит как новый."""
    dep = {"kind": "deposit", "asset": "USDT", "amount": 100.0, "ts": 1.0}
    wd = {"kind": "withdraw", "asset": "USDT", "amount": 30.0, "ts": 2.0}
    bot = _history_bot(tmp_path, monkeypatch, [[wd, dep], [wd], [wd, dep]])
    for _ in range(3):
        asyncio.run(bot.check_accounts())
    assert texts(bot) == []


def test_deal_markup_has_steps_button():
    kb = B.deal_markup(deal(), deal_id=7)["inline_keyboard"]
    buttons = [b for row in kb for b in row]
    assert any(b.get("callback_data") == "steps:7" for b in buttons)


def test_steps_view_lists_route_prices_and_breakdown():
    d = deal(5.0)
    _, b, s, _ = d
    text = B.steps_view(d, p2p.Config(amount=70000), snap([d]))
    assert f"по {p2p._price(b.price)}" in text and f"по {p2p._price(s.price)}" in text
    assert "Чистыми" in text
    assert "перепроверь перед сделкой" in text


def test_steps_view_shows_spot_rate_for_cross_asset_leg():
    d = deal(5.0, s_asset="ETH", route="спот USDT→ETH на Bybit (−0.1%)")
    sp = {"Bybit": {"USDT": (1.0, 1.0), "ETH": (2500.0, 2501.0)}}
    snapshot = p2p.Snapshot(88.0, "test", {}, {}, [d], {}, {}, {}, spot=sp)
    text = B.steps_view(d, p2p.Config(amount=70000), snapshot)
    assert "курс 2500/2501" in text


def test_show_steps_sends_message():
    bot = Stub(p2p.Config())
    d = deal(5.0)
    deal_id = bot.remember_deal(d, snap=snap([d]))
    asyncio.run(bot.show_steps({"id": "1"}, deal_id))
    assert "Шаги связки" in texts(bot)[-1]


def test_show_steps_unknown_id_not_sent():
    bot = Stub(p2p.Config())
    asyncio.run(bot.show_steps({"id": "1"}, 999))
    method, params = bot.out[-1]
    assert method == "answerCallbackQuery" and "устарел" in params["text"]


def test_check_accounts_skips_exchanges_without_saved_key(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "KEYS_PATH", str(tmp_path / "keys.json"))
    calls = []

    async def fake_history(s, ex, limit=20):
        calls.append(ex)
        return None

    monkeypatch.setattr(B.accounts, "account_history", fake_history)
    bot = Stub(p2p.Config())
    asyncio.run(bot.check_accounts())
    assert calls == []


# --- тихие часы и пауза сигналов ---

def msk_ts(hour, minute=0, day=24):
    """Unix-timestamp для заданного часа:минуты 24 сентября 2026 по МСК — удобно для тестов без сети."""
    return datetime(2026, 9, day, hour, minute, tzinfo=B.MSK).timestamp()


def test_parse_pause_arg_variants():
    assert B.parse_pause_arg("30m") == 1800
    assert B.parse_pause_arg("1h") == 3600
    assert B.parse_pause_arg("3h") == 10800
    assert B.parse_pause_arg("до утра") == "morning"
    assert B.parse_pause_arg("Утра") == "morning"
    assert B.parse_pause_arg("ерунда") is None
    assert B.parse_pause_arg("") is None


def test_in_quiet_hours_window_and_edges():
    assert B.in_quiet_hours("01:00-08:00", msk_ts(2, 30))
    assert not B.in_quiet_hours("01:00-08:00", msk_ts(9, 0))
    assert not B.in_quiet_hours("01:00-08:00", msk_ts(0, 30))
    assert not B.in_quiet_hours("", msk_ts(2, 0))
    assert not B.in_quiet_hours("ерунда", msk_ts(2, 0))


def test_in_quiet_hours_wraps_midnight():
    assert B.in_quiet_hours("23:00-07:00", msk_ts(0, 30))
    assert B.in_quiet_hours("23:00-07:00", msk_ts(23, 30))
    assert not B.in_quiet_hours("23:00-07:00", msk_ts(12, 0))


def test_quiet_hours_end_ts_next_occurrence():
    end = B.quiet_hours_end_ts("01:00-08:00", msk_ts(2, 30))
    assert datetime.fromtimestamp(end, B.MSK).strftime("%d %H:%M") == "24 08:00"
    end2 = B.quiet_hours_end_ts("01:00-08:00", msk_ts(9, 0))   # уже позже конца окна — переносим на завтра
    assert datetime.fromtimestamp(end2, B.MSK).strftime("%d %H:%M") == "25 08:00"


def test_quiet_hours_blocks_signal_and_stores_for_digest(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    d = deal(5, "MEXC")
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert not bot.out                              # сигнал не отправлен
    assert list(bot.night_deals.values()) == [d]     # но накоплен для утреннего дайджеста


def test_night_digest_collects_after_low_deal(monkeypatch):
    """Ночной дайджест тоже не обрывается на связке ниже порога, стоящей выше в списке сканера."""
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    asyncio.run(bot.quiet_and_pause_tick(snap([deal(1.5, "MEXC"), deal(3, "KuCoin")])))
    assert not bot.out
    assert [d[0] for d in bot.night_deals.values()] == [3]


def test_quiet_hours_off_sends_signal_as_usual(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1   # тест про антидубль/паузу, не про живость
    d = deal(5, "MEXC")
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert photos(bot)                               # тихие часы выключены — сигнал уходит как обычно


def test_night_digest_aggregates_across_scans_and_sends_top3_once(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    a, bd, c = deal(5, "MEXC"), deal(4, "KuCoin"), deal(6, "HTX")
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([a, bd])))
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(3, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([c])))
    assert not texts(bot)                            # всю ночь — тишина, дайджеста ещё нет
    assert len(bot.night_deals) == 3

    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))     # тихие часы закончились
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    msgs = texts(bot)
    assert len(msgs) == 1 and "Топ-3 связки за ночь" in msgs[0]
    assert msgs[0].index("+6.00%") < msgs[0].index("+5.00%") < msgs[0].index("+4.00%")  # по убыванию прибыли
    assert bot.night_deals == {}

    asyncio.run(bot.quiet_and_pause_tick(snap([])))   # повторный тик после конца ночи — дайджест не дублируем
    assert len(texts(bot)) == 1


def test_night_digest_empty_when_nothing_above_threshold(monkeypatch):
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    assert "связок выше порога не было" in texts(bot)[-1]


def test_night_digest_includes_paper_summary_when_on(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER", "1")
    db = str(tmp_path / "paper.db")
    _patch_paper_db(monkeypatch, db)
    buy, sell = make_ad("Bybit", "buy", 85.0), make_ad("MEXC", "sell", 90.0)
    paper.init_balance(10000, path=db)
    cid = paper.start_cycle(10000, buy, sell, "route", 2.0, path=db)
    paper.finish_cycle(cid, "done", realized_pct=2.5, path=db)

    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    msgs = texts(bot)
    assert any("Сухой прогон за сутки" in m for m in msgs)


def test_night_digest_skips_paper_summary_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPER", raising=False)
    _patch_paper_db(monkeypatch, str(tmp_path / "paper.db"))
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.quiet_on = True
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(9, 0))
    asyncio.run(bot.quiet_and_pause_tick(snap([])))
    assert not any("Сухой прогон за сутки" in m for m in texts(bot))


def test_pause_command_no_arg_is_indefinite():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/pause"))
    assert bot.paused and bot.pause_until == 0.0
    assert "паузе" in texts(bot)[-1]


def test_pause_1h_blocks_signals_and_expires(monkeypatch):
    monkeypatch.setattr(B, "deal_card", lambda d, c, a=None, r=None, breakdown=None: b"png")
    now = [1_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.live_scans = 1   # тест про антидубль/паузу, не про живость
    asyncio.run(bot.handle("/pause 1h"))
    assert bot.pause_until == now[0] + 3600 and not bot.paused

    d = deal(5, "MEXC")
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert not photos(bot)                 # сигналы блокированы на время паузы

    now[0] += 3601                          # час прошёл — пауза истекла сама, без /resume
    asyncio.run(bot.quiet_and_pause_tick(snap([d])))
    assert photos(bot)                      # сигналы снова идут


def test_pause_bad_arg_reports_error():
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/pause ерунда"))
    assert "срок" in texts(bot)[-1].lower()
    assert bot.pause_until == 0.0 and not bot.paused


def test_pause_until_morning_uses_quiet_hours_end(monkeypatch):
    ts = msk_ts(2, 0)
    monkeypatch.setattr(B.time, "time", lambda: ts)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/pause до утра"))
    assert bot.pause_until == B.quiet_hours_end_ts(bot.quiet_hours, ts)
    assert "08:00" in texts(bot)[-1]


def test_pause_until_morning_without_quiet_hours_configured():
    bot = Stub(p2p.Config())
    bot.quiet_hours = ""
    asyncio.run(bot.handle("/pause до утра"))
    assert bot.pause_until == 0.0
    assert "QUIET_HOURS" in texts(bot)[-1]


def test_resume_command_clears_manual_and_timed_pause():
    bot = Stub(p2p.Config())
    bot.paused = True
    bot.pause_until = time.time() + 999
    asyncio.run(bot.handle("/resume"))
    assert not bot.paused and bot.pause_until == 0.0
    assert "включены" in texts(bot)[-1].lower()


def test_apply_resume_button_clears_timed_pause():
    bot = Stub(p2p.Config())
    bot.pause_until = time.time() + 500
    bot.apply("resume")
    assert bot.pause_until == 0.0 and not bot.paused


def test_settings_view_shows_quiet_hours_until(monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: msk_ts(2, 0))
    bot = Stub(p2p.Config())
    bot.quiet_on = True
    text, _ = bot.settings_view()
    assert "тихие часы до 08:00" in text


def test_settings_view_shows_pause_until():
    bot = Stub(p2p.Config())
    bot.pause_until = time.time() + 3600
    text, _ = bot.settings_view()
    assert "пауза до" in text


def test_settings_view_pause_button_reflects_timed_pause():
    bot = Stub(p2p.Config())
    bot.pause_until = time.time() + 3600
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "resume" for b in buttons)


def test_settings_view_quiet_toggle_button_and_persist(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("QUIET_HOURS_ON=0\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "quiet_on" for b in buttons)

    toast = bot.apply("quiet_on")
    assert bot.quiet_on and "включены" in toast
    assert "QUIET_HOURS_ON=1" in env.read_text(encoding="utf-8")

    toast2 = bot.apply("quiet_off")
    assert not bot.quiet_on and "выключены" in toast2
    assert "QUIET_HOURS_ON=0" in env.read_text(encoding="utf-8")


# --- Фильтры монет/площадок и пресеты ---

def test_settings_view_has_filters_button():
    bot = Stub(p2p.Config())
    _, kb = bot.settings_view()
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "filters" for b in buttons)


def test_filters_view_lists_asset_and_exchange_toggles():
    bot = Stub(p2p.Config())
    text, kb = B.filters_view(bot.cfg)
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "flt_a:USDT" for b in buttons)
    assert any(b.get("callback_data") == "flt_e:bybit" for b in buttons)
    assert any(b.get("callback_data") == "flt_e:bestchange" for b in buttons)
    assert "USDT" in text and "bybit" in text


def test_filters_view_marks_enabled_and_disabled():
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"]))
    _, kb = B.filters_view(bot.cfg)
    buttons = {b["callback_data"]: b["text"] for row in kb["inline_keyboard"] for b in row}
    assert buttons["flt_a:USDT"].startswith("✅")
    assert buttons["flt_a:BTC"].startswith("⬜")
    assert buttons["flt_e:bybit"].startswith("✅")
    assert buttons["flt_e:mexc"].startswith("⬜")


def test_toggle_asset_off_updates_cfg_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ASSETS=USDT,USDC,BTC,ETH,TON\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config())
    toast = bot.apply("flt_a:BTC")
    assert "Выключено" in toast and "BTC" not in bot.cfg.assets
    saved = [x.strip() for x in env.read_text(encoding="utf-8").split("ASSETS=", 1)[1].splitlines()[0].split(",")]
    assert "BTC" not in saved


def test_toggle_asset_on_updates_cfg_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ASSETS=USDT\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT"]))
    toast = bot.apply("flt_a:BTC")
    assert "Включено" in toast and "BTC" in bot.cfg.assets
    assert "ASSETS=USDT,BTC" in env.read_text(encoding="utf-8")


def test_toggle_exchange_updates_cfg_and_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EXCHANGES=bybit,mexc\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(exchanges=["bybit", "mexc"]))
    toast = bot.apply("flt_e:mexc")
    assert "Выключено" in toast and bot.cfg.exchanges == ["bybit"]
    assert "EXCHANGES=bybit" in env.read_text(encoding="utf-8")


def test_cannot_disable_last_asset(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT"]))
    toast = bot.apply("flt_a:USDT")
    assert "нельзя" in toast.lower()
    assert bot.cfg.assets == ["USDT"]
    assert env.read_text(encoding="utf-8") == ""   # .env не тронут


def test_cannot_disable_last_exchange(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(exchanges=["bybit"]))
    toast = bot.apply("flt_e:bybit")
    assert "нельзя" in toast.lower()
    assert bot.cfg.exchanges == ["bybit"]
    assert env.read_text(encoding="utf-8") == ""


def test_flt_callback_rerenders_filters_view(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("ASSETS=USDT,BTC\n", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    bot = Stub(p2p.Config(assets=["USDT", "BTC"]))
    asyncio.run(bot.on_callback({"id": "1", "data": "flt_a:BTC", "message": {"message_id": 1}}))
    method, params = bot.out[-1]
    assert method == "editMessageText" and "Фильтры" in params["text"]


def use_presets_file(monkeypatch, pfile):
    """Весь ввод-вывод presets — в файл из tmp_path (data/ в тестах не трогаем)."""
    load, save = presets._load, presets._save
    monkeypatch.setattr(presets, "_load", lambda path=None: load(str(pfile)))
    monkeypatch.setattr(presets, "_save", lambda data, path=None: save(data, str(pfile)))


def env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    return env


def test_preset_save_flow_writes_current_filters(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    monkeypatch.setattr(B.presets, "save_preset", functools.partial(B.presets.save_preset, path=str(pfile)))
    monkeypatch.setattr(B.presets, "list_custom", functools.partial(B.presets.list_custom, path=str(pfile)))
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"], min_profit=3.0, amount=70000))
    asyncio.run(bot.on_callback({"id": "1", "data": "preset_save", "message": {"message_id": 1}}))
    assert bot.awaiting_preset_name
    asyncio.run(bot.handle("Мой набор"))
    assert not bot.awaiting_preset_name
    saved = presets.list_custom(path=str(pfile))
    assert saved["Мой набор"]["assets"] == ["USDT"] and saved["Мой набор"]["amount"] == 70000
    assert any("сохранён" in t for t in texts(bot))


def test_preset_save_empty_name_not_saved(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    monkeypatch.setattr(B.presets, "save_preset", functools.partial(B.presets.save_preset, path=str(pfile)))
    bot = Stub(p2p.Config())
    bot.awaiting_preset_name = True
    asyncio.run(bot.handle("   "))
    assert not pfile.exists()
    assert "не сохранён" in texts(bot)[-1].lower()


def test_apply_saved_preset_updates_cfg_and_env(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    presets.save_preset("Быстрый", p2p.Config(assets=["USDT"], exchanges=["bybit", "mexc"],
                                              min_profit=3.0, amount=70000), path=str(pfile))
    use_presets_file(monkeypatch, pfile)
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config())
    toast = bot.apply("preset_apply:" + presets.preset_id("Быстрый"))
    assert "Быстрый" in toast
    assert bot.cfg.assets == ["USDT"] and bot.cfg.exchanges == ["bybit", "mexc"]
    assert bot.cfg.min_profit == 3.0 and bot.cfg.amount == 70000
    text = env.read_text(encoding="utf-8")
    assert "MIN_PROFIT=3" in text and "AMOUNT=70000" in text


def test_apply_builtin_preset_usdt_no_transfer(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    use_presets_file(monkeypatch, tmp_path / "presets.json")
    bot = Stub(p2p.Config())
    bot.apply("preset_apply:" + presets.preset_id("USDT без переводов"))
    assert bot.cfg.assets == ["USDT"] and bot.cfg.same_venue_only is True
    assert "SAME_VENUE_ONLY=1" in env.read_text(encoding="utf-8")


def test_apply_builtin_preset_all_venues_resets(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(B, "save_env", functools.partial(B.save_env, path=str(env)))
    use_presets_file(monkeypatch, tmp_path / "presets.json")
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"], same_venue_only=True))
    bot.apply("preset_apply:" + presets.preset_id("Все площадки"))
    assert set(bot.cfg.exchanges) == set(p2p.ALL_EXCHANGES.split(","))
    assert set(bot.cfg.assets) == set(p2p.DEFAULT_ASSETS.split(","))
    assert bot.cfg.same_venue_only is False


def test_apply_unknown_preset_reports_not_found(tmp_path, monkeypatch):
    use_presets_file(monkeypatch, tmp_path / "presets.json")
    bot = Stub(p2p.Config())
    assert "не найден" in bot.apply("preset_apply:нет такого").lower()
    assert "не найден" in bot.apply("preset_apply:" + presets.preset_id("нет такого")).lower()


def test_presets_view_lists_builtin_and_custom(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    presets.save_preset("Мой", p2p.Config(), path=str(pfile))
    monkeypatch.setattr(B.presets, "list_custom", functools.partial(B.presets.list_custom, path=str(pfile)))
    text, kb = B.presets_view(p2p.Config())
    assert "USDT без переводов" in text and "Мой" in text
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "preset_apply:" + presets.preset_id("Мой") for b in buttons)
    assert any(b.get("callback_data") == "preset_del:" + presets.preset_id("Мой") for b in buttons)
    assert any(b.get("callback_data") == "preset_apply:" + presets.preset_id("USDT без переводов") for b in buttons)
    assert not any(b.get("callback_data") == "preset_del:" + presets.preset_id("USDT без переводов")
                   for b in buttons)


def test_preset_del_callback_removes_and_rerenders(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    presets.save_preset("Старый", p2p.Config(), path=str(pfile))
    use_presets_file(monkeypatch, pfile)
    bot = Stub(p2p.Config())
    data = "preset_del:" + presets.preset_id("Старый")
    asyncio.run(bot.on_callback({"id": "1", "data": data, "message": {"message_id": 1}}))
    assert "Старый" not in presets.list_custom(path=str(pfile))
    method, params = bot.out[-1]
    assert method == "editMessageText" and "Пресеты" in params["text"]


# callback_data у Telegram — не больше 64 байт: пресет в кнопке — коротким id, не именем

def callbacks(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]


def test_presets_view_callback_data_fits_64_bytes_for_long_names(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    for name in ("г" * 40, "Мои любимые банки и площадки на все дни", "🔥💰🚀 ночные связки без переводов 🌙"):
        presets.save_preset(name, p2p.Config(), path=str(pfile))
    use_presets_file(monkeypatch, pfile)
    _, kb = B.presets_view(p2p.Config(include_pay=["sberbank"]))
    data = callbacks(kb)
    assert len(data) == 3 + 2 * 3 + 1   # 3 встроенных, по 2 кнопки на 3 своих, «⬅️ Фильтры»
    assert all(len(d.encode("utf-8")) <= 64 for d in data)


def test_preset_long_cyrillic_name_save_apply_delete_via_buttons(tmp_path, monkeypatch):
    """Полный путь из меню: сохранить длинное русское имя → кнопки валидны → ▶️ применяет, 🗑 удаляет."""
    pfile = tmp_path / "presets.json"
    use_presets_file(monkeypatch, pfile)
    env_file(tmp_path, monkeypatch)
    name = "Мои любимые банки и площадки на все дни"   # 39 букв: с префиксом по имени было 91 байт
    bot = Stub(p2p.Config(assets=["USDT"], exchanges=["bybit"], min_profit=3.0, amount=70000))
    asyncio.run(bot.on_callback({"id": "1", "data": "preset_save", "message": {"message_id": 1}}))
    asyncio.run(bot.handle(name))
    method, params = bot.out[-1]
    assert method == "sendMessage" and "Пресеты" in params["text"]
    data = callbacks(params["reply_markup"])
    assert all(len(d.encode("utf-8")) <= 64 for d in data)
    apply_btn = "preset_apply:" + presets.preset_id(name)
    del_btn = "preset_del:" + presets.preset_id(name)
    assert apply_btn in data and del_btn in data
    bot.cfg = p2p.Config(assets=["BTC"], exchanges=["mexc"], min_profit=1.0, amount=20000)
    asyncio.run(bot.on_callback({"id": "2", "data": apply_btn, "message": {"message_id": 1}}))
    assert bot.cfg.assets == ["USDT"] and bot.cfg.exchanges == ["bybit"]
    assert bot.cfg.min_profit == 3.0 and bot.cfg.amount == 70000
    toast = next(p["text"] for m, p in bot.out if m == "answerCallbackQuery" and p["callback_query_id"] == "2")
    assert name in toast
    asyncio.run(bot.on_callback({"id": "3", "data": del_btn, "message": {"message_id": 1}}))
    assert name not in presets.list_custom(path=str(pfile))


def test_preset_name_is_html_escaped_in_messages(tmp_path, monkeypatch):
    """Имя пресета в HTML-сообщениях экранируется, иначе Telegram не разберёт разметку всего сообщения."""
    pfile = tmp_path / "presets.json"
    use_presets_file(monkeypatch, pfile)
    env_file(tmp_path, monkeypatch)
    name = "<b>Банки</b> & <i>x"
    bot = Stub(p2p.Config())
    bot.awaiting_preset_name = True
    asyncio.run(bot.handle(name))
    saved_msg, view = texts(bot)[-2:]
    escaped = "&lt;b&gt;Банки&lt;/b&gt; &amp; &lt;i&gt;x"
    assert escaped in saved_msg and "<i>" not in saved_msg
    assert escaped in view and "<i>" not in view
    kb = bot.out[-1][1]["reply_markup"]
    assert any(b["text"] == f"▶️ {name}" for row in kb["inline_keyboard"] for b in row)   # текст кнопки — как есть
    toast = bot.apply("preset_apply:" + presets.preset_id(name))   # тост answerCallbackQuery — простой текст
    assert name in toast


def test_old_preset_buttons_with_name_still_work(tmp_path, monkeypatch):
    """Кнопки из сообщений до перехода на id несли имя — применяются и удаляют как раньше."""
    pfile = tmp_path / "presets.json"
    presets.save_preset("Мой", p2p.Config(assets=["USDT"]), path=str(pfile))
    use_presets_file(monkeypatch, pfile)
    env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config(assets=["BTC"]))
    assert "Мой" in bot.apply("preset_apply:Мой") and bot.cfg.assets == ["USDT"]
    bot.apply("preset_apply:USDT без переводов")
    assert bot.cfg.same_venue_only is True
    asyncio.run(bot.on_callback({"id": "1", "data": "preset_del:Мой", "message": {"message_id": 1}}))
    assert "Мой" not in presets.list_custom(path=str(pfile))


# пользовательский пресет — полный снимок фильтров, включая «без переводов» (same_venue_only)

def test_custom_preset_restores_same_venue_only(tmp_path, monkeypatch):
    pfile = tmp_path / "presets.json"
    use_presets_file(monkeypatch, pfile)
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config())
    bot.apply("preset_apply:" + presets.preset_id("USDT без переводов"))
    assert bot.cfg.same_venue_only is True
    B.presets.save_preset("Мой", bot.cfg)
    bot.apply("preset_apply:" + presets.preset_id("Все площадки"))
    assert bot.cfg.same_venue_only is False
    bot.apply("preset_apply:" + presets.preset_id("Мой"))
    assert bot.cfg.same_venue_only is True and bot.cfg.assets == ["USDT"]
    assert "SAME_VENUE_ONLY=1" in env.read_text(encoding="utf-8")


def test_legacy_custom_preset_without_same_venue_only_keeps_flag(tmp_path, monkeypatch):
    """Пресет, сохранённый до правки (без same_venue_only), применяется без ошибок и флаг не трогает."""
    pfile = tmp_path / "presets.json"
    pfile.write_text('{"Старый": {"assets": ["USDT"], "exchanges": ["bybit"], "include_pay": [], '
                     '"min_profit": 2.0, "amount": 50000}}', encoding="utf-8")
    use_presets_file(monkeypatch, pfile)
    env = env_file(tmp_path, monkeypatch)
    bot = Stub(p2p.Config(same_venue_only=True))
    assert "Старый" in bot.apply("preset_apply:" + presets.preset_id("Старый"))
    assert bot.cfg.assets == ["USDT"] and bot.cfg.same_venue_only is True
    assert "SAME_VENUE_ONLY" not in env.read_text(encoding="utf-8")


def test_history_command_empty_sends_message_no_photos(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/history"))
    assert "пуста" in texts(bot)[-1]
    assert not photos(bot)


def test_history_command_renders_two_photos(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    for name in ("is_empty", "hourly_avg", "heatmap", "median_vs_bestchange"):
        monkeypatch.setattr(B.history, name, functools.partial(getattr(B.history, name), path=db))
    history._insert([(time.time(), "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0)], db)
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/history"))
    assert len(photos(bot)) == 2


def test_history_callback_routes_to_show_history(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "history", "message": {"message_id": 1}}))
    assert "пуста" in texts(bot)[-1]


def _fact_prompt_trade_id(bot):
    """Достать id сделки из кнопок факта, отправленных «✅ Сделал»."""
    msg = next(p for m, p in bot.out if m == "sendMessage" and "факт" in p["text"].lower())
    return int(msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1])


def test_mark_done_offers_fact_quick_buttons(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    msg = next(p for m, p in bot.out if m == "sendMessage" and "факт" in p["text"].lower())
    buttons = [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]
    modes = {b["callback_data"].rsplit(":", 1)[-1] for b in buttons}
    assert modes == {"calc", "minus", "plus", "manual"}
    # editMessageReplyMarkup (снятие «✅ Сделал») по-прежнему последним вызовом, как раньше
    method, _ = bot.out[-1]
    assert method == "editMessageReplyMarkup"


def test_fact_button_calc_records_calc_profit(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    monkeypatch.setattr(B.trades, "get_trade", functools.partial(B.trades.get_trade, path=db))
    monkeypatch.setattr(B.trades, "set_fact", functools.partial(B.trades.set_fact, path=db))
    monkeypatch.setattr(B.trades, "stats", functools.partial(B.trades.stats, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    trade_id = _fact_prompt_trade_id(bot)
    asyncio.run(bot.on_callback({"id": "2", "data": f"fact:{trade_id}:calc", "message": {"message_id": 10}}))
    assert "Факт: +5.00%" in texts(bot)[-1]
    assert "факт указан у 1 из 1" in bot.stats_view()


def test_fact_button_plus_minus_offsets_calc(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    monkeypatch.setattr(B.trades, "get_trade", functools.partial(B.trades.get_trade, path=db))
    monkeypatch.setattr(B.trades, "set_fact", functools.partial(B.trades.set_fact, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    trade_id = _fact_prompt_trade_id(bot)
    asyncio.run(bot.on_callback({"id": "2", "data": f"fact:{trade_id}:minus", "message": {"message_id": 10}}))
    assert "Факт: +4.50%" in texts(bot)[-1]


def test_fact_manual_button_arms_awaiting_then_parses_text(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    monkeypatch.setattr(B.trades, "get_trade", functools.partial(B.trades.get_trade, path=db))
    monkeypatch.setattr(B.trades, "set_fact", functools.partial(B.trades.set_fact, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    trade_id = _fact_prompt_trade_id(bot)
    asyncio.run(bot.on_callback({"id": "2", "data": f"fact:{trade_id}:manual", "message": {"message_id": 10}}))
    assert bot.awaiting_fact == trade_id
    asyncio.run(bot.handle("650 ₽"))
    assert bot.awaiting_fact is None
    assert "Факт:" in texts(bot)[-1]


def test_fact_manual_garbage_reports_error_and_resets_awaiting(tmp_path, monkeypatch):
    db = str(tmp_path / "trades.db")
    monkeypatch.setattr(B.trades, "log_trade", functools.partial(B.trades.log_trade, path=db))
    monkeypatch.setattr(B.trades, "get_trade", functools.partial(B.trades.get_trade, path=db))
    bot = Stub(p2p.Config(amount=70000))
    deal_id = bot.remember_deal(deal(5.0))
    asyncio.run(bot.mark_done({"id": "1", "message": {"message_id": 9}}, deal_id))
    trade_id = _fact_prompt_trade_id(bot)
    bot.awaiting_fact = trade_id
    asyncio.run(bot.handle("ерунда"))
    assert bot.awaiting_fact is None
    assert "не понял" in texts(bot)[-1].lower()


def test_awaiting_fact_reset_by_other_button():
    bot = Stub(p2p.Config())
    bot.awaiting_fact = 42
    asyncio.run(bot.on_callback({"id": "1", "data": "best", "message": {"message_id": 1}}))
    assert bot.awaiting_fact is None


def test_awaiting_fact_reset_by_other_command():
    bot = Stub(p2p.Config())
    bot.awaiting_fact = 42
    asyncio.run(bot.handle("/best"))
    assert bot.awaiting_fact is None


def test_backtest_command_reports_top_pairs_and_disclaimer(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    monkeypatch.setattr(B.history, "backtest", functools.partial(B.history.backtest, path=db))
    history._insert([(time.time(), "Bybit", "MEXC", "USDT", "USDT", 3.0, 88.0)], db)
    bot = Stub(p2p.Config(min_profit=2.0, amount=50000))
    asyncio.run(bot.handle("/backtest"))
    text = texts(bot)[-1]
    assert "Bybit" in text and "MEXC" in text
    assert "прошлое — не прогноз" in text.lower()
    assert "оценка по сохранённым снимкам" in text.lower()


def test_backtest_command_empty_history_message(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/backtest"))
    assert "пуста" in texts(bot)[-1]


def test_backtest_callback_routes_same_as_command(tmp_path, monkeypatch):
    db = str(tmp_path / "history.db")
    monkeypatch.setattr(B.history, "is_empty", functools.partial(B.history.is_empty, path=db))
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "backtest", "message": {"message_id": 1}}))
    assert "пуста" in texts(bot)[-1]


def test_history_markup_has_backtest_button():
    buttons = [b for row in B.HISTORY_MARKUP["inline_keyboard"] for b in row]
    assert any(b.get("callback_data") == "backtest" for b in buttons)


def test_uptime_str_formats_seconds_and_days():
    assert B._uptime_str(5) == "00:00:05"
    assert B._uptime_str(3725) == "01:02:05"
    assert B._uptime_str(90000) == "1д 01:00:00"


def test_status_reports_version_uptime_scan_and_errors(tmp_path):
    status = tmp_path / "status.json"
    status.write_text('{"version": "abc1234"}', encoding="utf-8")
    bot = Stub(p2p.Config(min_profit=2.0))
    bot.start_ts = time.time() - 3725
    bot.last_scan_ts = time.time() - 1
    bot.last_scan_duration = 1.5
    ds = [deal(5, "MEXC"), deal(1, "KuCoin")]
    bot.last = p2p.Snapshot(88.0, "test", {}, {}, ds, {}, {}, {"bybit/USDT": "TimeoutError: x"})
    text = bot.status_view(str(status))
    assert "abc1234" in text
    assert "01:02:0" in text                       # аптайм ~1ч02м (секунда могла чуть уйти)
    assert "1.5 с" in text
    assert "Связок выше порога 2%: 1" in text       # только одна связка (5%) выше порога 2%
    assert "bybit/USDT" in text and "TimeoutError" in text


def test_status_before_first_scan(tmp_path):
    status = tmp_path / "status.json"
    bot = Stub(p2p.Config())
    text = bot.status_view(str(status))
    assert "?" in text                              # версии в .dev_status.json ещё нет
    assert "Последний скан: ещё не было" in text
    assert "Скан ещё не выполнялся." in text


def test_status_no_errors_says_all_ok(tmp_path):
    status = tmp_path / "status.json"
    bot = Stub(p2p.Config(min_profit=1.0))
    bot.last_scan_ts = time.time()
    bot.last = p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})
    text = bot.status_view(str(status))
    assert "Ошибок нет" in text


def test_status_command_sends_status_view(monkeypatch):
    monkeypatch.setattr(B.Bot, "status_view", lambda self, status_path=B.DEV_STATUS: "STATUS TEXT")
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/status"))
    assert texts(bot)[-1] == "STATUS TEXT"


def test_status_callback_sends_status_view(monkeypatch):
    monkeypatch.setattr(B.Bot, "status_view", lambda self, status_path=B.DEV_STATUS: "STATUS TEXT")
    bot = Stub(p2p.Config())
    asyncio.run(bot.on_callback({"id": "1", "data": "status", "message": {"message_id": 1}}))
    assert texts(bot)[-1] == "STATUS TEXT"


def test_logs_view_reads_tail_and_escapes_html(tmp_path):
    log = tmp_path / "bot.log"
    lines = [f"24.09 10:00:{i:02d} INFO bot: line {i}" for i in range(40)]
    log.write_text("\n".join(lines) + "\n<script>bad</script>\n", encoding="utf-8")
    text = B.logs_view(str(log), n=30)
    assert "line 39" in text and "line 0" not in text     # только последние 30 строк
    assert "&lt;script&gt;" in text and "<script>bad" not in text


def test_logs_view_missing_file(tmp_path):
    text = B.logs_view(str(tmp_path / "none.log"))
    assert "пока пуст" in text


def test_logs_view_empty_file(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("", encoding="utf-8")
    text = B.logs_view(str(log))
    assert "пуст" in text


def test_logs_command_sends_logs_view(tmp_path, monkeypatch):
    log = tmp_path / "bot.log"
    log.write_text("24.09 10:00:00 INFO bot: hello\n", encoding="utf-8")
    monkeypatch.setattr(B, "LOG_PATH", str(log))
    bot = Stub(p2p.Config())
    asyncio.run(bot.handle("/logs"))
    assert "hello" in texts(bot)[-1]


def test_scan_loop_records_last_scan_timestamp_and_duration(monkeypatch):
    async def fake_scan(s, cfg):
        return p2p.Snapshot(88.0, "test", {}, {}, [], {}, {}, {})

    monkeypatch.setattr(B, "scan", fake_scan)
    monkeypatch.setattr(B.history, "record", lambda snap: False)   # не пишем в реальный data/history.db

    async def no_sleep(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(B.asyncio, "sleep", no_sleep)
    bot = Stub(p2p.Config())
    bot.chat_id = ""   # без чата — не шлём алерты/дайджесты
    try:
        asyncio.run(bot.scan_loop())
    except asyncio.CancelledError:
        pass
    assert bot.last_scan_ts > 0 and bot.last_scan_duration >= 0
