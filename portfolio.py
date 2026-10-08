"""Durable paper portfolio. No exchange requests or real money operations.

Decimal strings are the source of truth; every state transition and its audit
event are committed together. Legacy paper.db is deliberately not imported.
"""
import csv
import html
import json
import os
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext

import p2p
import paper
import spotbook
import bankmodel
import execution_review

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "paper_portfolio.db")
EXPORT_PATH = os.path.join(os.path.dirname(__file__), "data", "paper_portfolio.csv")
ZERO = Decimal(0)
COIN = Decimal("0.000000000000000001")
RUB = Decimal("0.01")


def dec(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("Non-finite portfolio amount")
    return value


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS wallet (id INTEGER PRIMARY KEY CHECK(id=1), initial TEXT, cash TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run_id INTEGER, ts REAL, kind TEXT, details TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS consumed (key TEXT PRIMARY KEY, qty TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, value TEXT)")
    bankmodel.schema(con)
    con.commit()
    return con


def _event(con, run, kind, details, now):
    con.execute("INSERT INTO events(run_id,ts,kind,details) VALUES (?,?,?,?)",
                (run, now, kind, json.dumps(details, ensure_ascii=False)))


def _save(con, run):
    con.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(run, ensure_ascii=False), run["id"]))


def runs(path=DB_PATH, active=False):
    if not os.path.exists(path):
        return []
    con = connect(path)
    try:
        values = [json.loads(r[0]) for r in con.execute("SELECT state FROM runs ORDER BY id")]
        return [r for r in values if r["stage"] not in ("done", "cancelled")] if active else values
    finally:
        con.close()


def start(amount, buy, sell, hops, planned_pct, pay_fee=0, path=DB_PATH, now=None, max_open=1,
          bank="", pay_kind="", spot_fees=None, measures=None):
    """Reserve principal atomically; a second process cannot spend the same cash."""
    now = time.time() if now is None else now
    amount = dec(amount).quantize(RUB, rounding=ROUND_DOWN)
    fee = dec(pay_fee)
    if amount <= 0 or not ZERO <= fee < 100 or not hops or not hops.get("hops") or max_open < 1:
        raise ValueError("Invalid amount, fee or missing immutable route")
    if hops["hops"][0]["asset"] != buy.asset or hops["hops"][-1]["asset"] != sell.asset:
        raise ValueError("Route assets do not match the offers")
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT OR IGNORE INTO wallet VALUES (1, '50000.00', '50000.00')")
        initial, cash = con.execute("SELECT initial,cash FROM wallet").fetchone()
        open_count = sum(json.loads(r[0])["stage"] not in ("done", "cancelled")
                         for r in con.execute("SELECT state FROM runs"))
        if dec(cash) < amount or open_count >= max_open:
            con.rollback()
            return None
        route = []
        hs = hops["hops"]
        for index, h in enumerate(hs):
            if h["frm"] == h["to"] == "BestChange":
                route.extend([dict(h, kind="transfer", to="Bybit", to_net=h.get("frm_net") or "", fee=0),
                              dict(h, kind="transfer", frm="Bybit", frm_net=h.get("frm_net") or "")])
            else:
                route.append(dict(h, kind="transfer"))
            if index + 1 < len(hs) and h["asset"] != hs[index + 1]["asset"]:
                source, target = h["asset"], hs[index + 1]["asset"]
                if source != "USDT" and target != "USDT":
                    route.append({"kind": "spot", "venue": h["to"], "asset": source, "target": "USDT"})
                    source = "USDT"
                route.append({"kind": "spot", "venue": h["to"], "asset": source, "target": target})
        for leg in route:
            if leg["kind"] == "spot":
                leg["fee_pct"] = str(dec((spot_fees or {}).get(leg["venue"], p2p._spot_fee(p2p.Config(), leg["venue"]))))
                if not 0 <= dec(leg["fee_pct"]) < 100:
                    raise ValueError("Invalid spot fee")
            else:
                leg["minutes"] = paper.hops_transfer_minutes([leg], paper.net_minutes_table(), paper.settings()["transfer_minutes"])
        bank_data = None
        bank_profile = None
        if os.getenv("PAPER_BANK_MODEL", "strict") == "strict":
            bank_data = bankmodel.load()
            bankmodel.initialize(con, bank_data, now)
            bank_profile, pay_kind, _ = bankmodel.select(con, bank_data, buy.pays, "out", amount, now, venue=buy.ex)
            receipt_profile, _, _ = bankmodel.select(con, bank_data, sell.pays, "in", amount, now, venue=sell.ex)
            execution_review.check(bank_profile, buy, now)
            execution_review.check(receipt_profile, sell, now)
            bank = bank_profile["bank"]
            for leg in route:
                if leg["kind"] == "spot":
                    spec = bank_profile.get("exchange_fees", {}).get(leg["venue"], {})
                    if spec.get("confirmed") is not True or spec.get("currency") != "received":
                        raise bankmodel.Blocked("Не подтверждены ставка и валюта комиссии спота: " + leg["venue"])
                    rate = dec(spec.get("percent", "-1"))
                    if not 0 <= rate < 100:
                        raise bankmodel.Blocked("Некорректная персональная комиссия спота")
                    leg["fee_pct"] = str(rate)
        cur = con.execute("INSERT INTO runs(state) VALUES ('{}')")
        run = {"id": cur.lastrowid, "start": now, "stage_ts": now, "stage": "buy", "amount": str(amount),
               "reserved": str(amount), "spent": "0", "proceeds": "0", "qty": "0", "cost": "0",
               "realized": "0", "asset": buy.asset, "venue": buy.ex, "net": buy.net or "",
               "buy_ex": buy.ex, "buy_asset": buy.asset, "buy_price": str(buy.price), "buy_nick": buy.nick,
               "buy_pays": list(buy.pays), "sell_ex": sell.ex, "sell_asset": sell.asset, "sell_net": sell.net or "",
               "route": route, "leg": 0, "pay_fee": str(fee), "planned_pct": planned_pct,
               "bank": bank, "pay_kind": pay_kind,
               "settings": paper.settings(),
               "measures": measures or {},
               "note": "", "assumptions": [], "in_transit": False, "dust": []}
        run["settings"]["bank_model"] = "strict" if bank_profile else "legacy"
        if bank_profile:
            run["bank_profile"] = bank_profile
            run["bank_account"] = bank_profile["id"]
            bankmodel.reserve(con, run["id"], bank_profile, pay_kind, amount, now)
        run["settings"]["spot_model"] = os.getenv("PAPER_SPOT_MODEL", "depth")
        _save(con, run)
        if not bank_profile and con.execute("SELECT 1 FROM bank_accounts LIMIT 1").fetchone():
            _cash(con, -amount, now=now)
        else:
            con.execute("UPDATE wallet SET cash=?", (str(dec(cash) - amount),))
        _event(con, run["id"], "reserve", {"rub": str(amount), "initial": initial}, now)
        _event(con, run["id"], "state", run, now)
        con.commit()
        return run
    except bankmodel.Blocked as exc:
        con.rollback()
        previous = con.execute("SELECT details FROM events WHERE kind='bank_blocked' ORDER BY id DESC LIMIT 1").fetchone()
        if not previous or json.loads(previous[0])["reason"] != str(exc):
            _event(con, None, "bank_blocked", {"reason": str(exc)}, now)
        con.commit()
        return None
    finally:
        con.close()


def _cash(con, delta, account=None, now=None):
    cash, = con.execute("SELECT cash FROM wallet").fetchone()
    value = dec(cash) + delta
    if value < 0:
        raise ValueError("Negative free cash")
    con.execute("UPDATE wallet SET cash=?", (str(value),))
    if con.execute("SELECT 1 FROM bank_accounts LIMIT 1").fetchone():
        if account is None:
            row = con.execute("SELECT details FROM bank_events WHERE kind='migration' ORDER BY id LIMIT 1").fetchone()
            account = json.loads(row[0])["start_account"]
        bankmodel.cash(con, account, delta, time.time() if now is None else now)


def _ads(snap, venue, side, asset, net, now):
    if paper._venue_down(snap, venue, asset):
        return []
    return sorted((a for a in snap.groups.get((venue, side, asset), [])
                   if a.fetched_ts > 0 and 0 <= now - a.fetched_ts <= 120
                   and (venue != "BestChange" or not net or a.net == net) and a.price > 0 and a.avail > 0),
                  key=lambda a: a.price, reverse=side == "sell")


def _take(con, ad, wanted):
    """A cached offer's quantity can be consumed only once across all runs."""
    key = _offer_key(ad)
    row = con.execute("SELECT qty FROM consumed WHERE key=?", (key,)).fetchone()
    used = dec(row[0]) if row else ZERO
    take = min(wanted, max(ZERO, dec(ad.avail) - used), dec(ad.max_amt) / dec(ad.price))
    take = take.quantize(COIN, rounding=ROUND_DOWN)
    if take <= 0 or take * dec(ad.price) < dec(ad.min_amt):
        return ZERO
    con.execute("INSERT OR REPLACE INTO consumed VALUES (?,?)", (key, str(used + take)))
    return take


def _offer_key(ad):
    if os.getenv("PAPER_BANK_MODEL", "strict") == "strict":
        return json.dumps(["unreplenished", ad.ex, ad.side, ad.asset, ad.ad_id or ad.nick, ad.net])
    return json.dumps([ad.ex, ad.side, ad.asset, ad.ad_id or ad.nick, ad.net, ad.fetched_ts, ad.price])



def _receivable(con, run, ad, now):
    if run["settings"].get("bank_model") != "strict":
        return None
    try:
        data = bankmodel.load()
        available = con.execute("SELECT qty FROM consumed WHERE key=?", (_offer_key(ad),)).fetchone()
        left = max(ZERO, dec(ad.avail) - (dec(available[0]) if available else ZERO))
        # Limits checked again at the exact quantity after offer selection.
        estimate = min(dec(run["qty"]), left, dec(ad.max_amt) / dec(ad.price)) * dec(ad.price)
        profile, _, q = bankmodel.select(con, data, ad.pays, "in", estimate, now, run["id"], venue=ad.ex)
        review = execution_review.check(profile, ad, now)
        if now - run["stage_ts"] < review["payment_seconds"]:
            raise bankmodel.Blocked("Ожидание моделируемого банковского зачисления")
        q["receipt_model"] = "elapsed_time_not_real_bank_confirmation"
        return q
    except bankmodel.Blocked as exc:
        run["note"] = "Продажа заблокирована: " + str(exc)
        return False

def tick(snap, cfg, path=DB_PATH, now=None, books=None):
    """Execute one durable step per scan; no fabricated fills on missing data."""
    now = time.time() if now is None else now
    notices = []
    if cfg.fiat != "RUB":
        return notices  # Preserve holdings; foreign-fiat offers cannot price this RUB wallet.
    if not os.path.exists(path):
        return notices
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM bank_accounts LIMIT 1").fetchone():
            try:
                data = bankmodel.load()
                bankmodel.initialize(con, data, now)
                bankmodel.settle(con, data, now)
                bankmodel.service_charges(con, data, now)
            except bankmodel.Blocked:
                pass
        for state, in con.execute("SELECT state FROM runs ORDER BY id").fetchall():
            run = json.loads(state)
            was_terminal = run["stage"] in ("done", "cancelled")
            with localcontext() as ctx:
                ctx.prec = 40
                _sell_dust(con, run, snap, now)
                if not was_terminal:
                    _step(con, run, snap, cfg, now, books or {})
            _save(con, run)
            if state != json.dumps(run, ensure_ascii=False):
                _event(con, run["id"], "state", run, now)
            if not was_terminal and run["stage"] in ("done", "cancelled"):
                label = "завершён" if run["stage"] == "done" else "покупка отменена"
                notices.append(f"Круг #{run['id']} {label}, результат {dec(run['realized']):+.2f} ₽")
        with localcontext() as ctx:
            ctx.prec = 40
            _mark(con, snap, now)
            _verify(con)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return notices


def _step(con, r, snap, cfg, now, books):
    st = r["settings"]
    elapsed = now - r["stage_ts"]
    if r["stage"] == "release":
        if elapsed < r.get("release_seconds", 0):
            r["note"] = "Монеты заблокированы в моделируемом P2P-эскроу"
            return
        r["stage"], r["stage_ts"], r["in_transit"] = "route", now, False
        _event(con, r["id"], "p2p_release", {"qty": r["qty"], "model": "elapsed_time"}, now)
        return
    if r["stage"] == "buy":
        if elapsed < st["pay_minutes"] * 60:
            return
        budget = dec(r["reserved"])
        strict_bank = r["settings"].get("bank_model") == "strict"
        if strict_bank:
            try:
                current = bankmodel.load()
                profile = next(p for p in current["accounts"] if p["id"] == r["bank_account"])
                if profile != r["bank_profile"]:
                    raise bankmodel.Blocked("Условия счёта изменились после резервирования")
                bankmodel.validate(profile, now)
            except (bankmodel.Blocked, StopIteration, ValueError, TypeError) as exc:
                r["note"] = str(exc) or "Профиль счёта удалён"
                _cash(con, budget, r["bank_account"], now)
                con.execute("DELETE FROM bank_reservations WHERE run_id=?", (r["id"],))
                r["reserved"] = "0"
                r["stage"] = ("release" if strict_bank and r.get("release_seconds", 0) else "route") if dec(r["qty"]) else "cancelled"
                r["stage_ts"] = now
                r["in_transit"] = r["stage"] == "release"
                return
        for ad in _ads(snap, r["venue"], "buy", r["asset"], r["net"], now):
            if not set(ad.pays).intersection(r["buy_pays"]):
                continue
            if dec(ad.price) > dec(r["buy_price"]) * (1 + dec(st["buy_slip_max"]) / 100):
                continue
            price = dec(ad.price)
            multiplier = 1 - dec(r["pay_fee"]) / 100
            principal = budget * multiplier
            if strict_bank:
                if bankmodel.compatible(profile, ad.pays) != r["pay_kind"]:
                    continue
                try:
                    review = execution_review.check(profile, ad, now)
                    if elapsed < review["payment_seconds"]:
                        r["note"] = "Ожидание моделируемой оплаты P2P"
                        continue
                    r["release_seconds"] = max(r.get("release_seconds", 0), review["release_seconds"])
                except bankmodel.Blocked as exc:
                    r["note"] = str(exc)
                    continue
                principal = bankmodel.affordable(con, profile, r["pay_kind"], budget, now, r["id"])
            qty = _take(con, ad, principal / price)
            if not qty:
                continue
            paid = (qty * price / multiplier).quantize(RUB, rounding=ROUND_UP)
            bank_quote = None
            if strict_bank:
                bank_quote = bankmodel.quote(con, profile, r["pay_kind"], "out", qty * price, now, r["id"])
                paid = dec(bank_quote["amount"]) + dec(bank_quote["fee"])
                bankmodel.payment(con, bank_quote, now, r["id"])
            budget -= paid
            if strict_bank:
                con.execute("UPDATE bank_reservations SET amount=? WHERE run_id=?", (str(budget), r["id"]))
            r["qty"] = str(dec(r["qty"]) + qty)
            r["spent"] = str(dec(r["spent"]) + paid)
            r["cost"] = r["spent"]
            _event(con, r["id"], "buy", {"qty": str(qty), "asset": r["asset"], "rub": str(paid),
                                         "bank_fee_rub": str(paid - qty * price), "price": str(price),
                                         "bank": r["bank"], "pay_kind": r["pay_kind"], "bank_quote": bank_quote,
                                         "offer": ad.ad_id or ad.nick, "quote_ts": ad.fetched_ts}, now)
        r["reserved"] = str(budget)
        if budget <= RUB or elapsed >= (st["pay_minutes"] + 30) * 60:
            _cash(con, budget, r.get("bank_account"), now)
            con.execute("DELETE FROM bank_reservations WHERE run_id=?", (r["id"],))
            r["reserved"] = "0"
            r["stage"] = ("release" if strict_bank and r.get("release_seconds", 0) else "route") if dec(r["qty"]) else "cancelled"
            r["stage_ts"] = now
            r["in_transit"] = r["stage"] == "release"
            _event(con, r["id"], "release", {"rub": str(budget)}, now)
        return
    if r["stage"] == "route":
        if r["leg"] >= len(r["route"]):
            r["stage"], r["stage_ts"] = "sell", now
            return
        leg = r["route"][r["leg"]]
        if leg["kind"] == "spot":
            if r["settings"].get("bank_model") == "strict":
                try:
                    data = bankmodel.load()
                    profile = next(p for p in data["accounts"] if p["id"] == r["bank_account"])
                    bankmodel.validate(profile, now)
                    spec = profile.get("exchange_fees", {}).get(leg["venue"], {})
                    if (spec.get("confirmed") is not True or spec.get("currency") != "received"
                            or dec(spec.get("percent", "-1")) != dec(leg["fee_pct"])):
                        raise bankmodel.Blocked("Комиссия спота не подтверждена для текущего счёта")
                except (bankmodel.Blocked, StopIteration) as exc:
                    r["note"] = str(exc) or "Профиль счёта отсутствует"
                    return
            venue = leg["venue"]
            alt = leg["target"] if r["asset"] == "USDT" else r["asset"]
            if st.get("spot_model", "depth") != "ticker":
                book = books.get((venue, alt))
                if book is None or book.get("venue") != venue or book.get("asset") != alt \
                        or not now - 15 <= book["ts"] <= now + 2 or not 0 <= now - book["received"] <= 15:
                    r["note"] = "Нет свежего стакана спота и правил пары; монеты сохранены"
                    if elapsed >= 1800:
                        r["stage"], r["stage_ts"] = "sell", now - 1800
                    return
                used = {key: value for key, value in con.execute("SELECT key,qty FROM consumed WHERE key LIKE 'spot:%'")}
                fill_book = dict(book, id="unreplenished") if r["settings"].get("bank_model") == "strict" else book
                fill = spotbook.fill(fill_book, r["asset"], r["qty"], used)
                if fill is None:
                    r["note"] = "Не хватает глубины спота или объём вне ограничений пары"
                    if elapsed >= 1800:
                        r["stage"], r["stage_ts"] = "sell", now - 1800
                    return
                qty, spent = dec(r["qty"]), dec(fill["spent"])
                gross = dec(fill["gross"])
                fee = gross * dec(leg["fee_pct"]) / 100
                output = (gross - fee).quantize(COIN, rounding=ROUND_DOWN)
                if output <= 0:
                    return
                leftover = qty - spent
                if leftover:
                    cost = dec(r["cost"]) * leftover / qty
                    r.setdefault("dust", []).append({"asset": r["asset"], "venue": venue, "net": r["net"],
                                                      "qty": str(leftover), "cost": str(cost)})
                    r["cost"] = str(dec(r["cost"]) - cost)
                for key, take, price in fill["takes"]:
                    con.execute("INSERT OR REPLACE INTO consumed VALUES (?,?)", (key, str(dec(used.get(key, 0)) + dec(take))))
                _event(con, r["id"], "spot", {"input": str(spent), "input_asset": r["asset"], "target": leg["target"],
                                              "output": str(output), "fee": str(fee), "dust": str(leftover),
                                              "venue": venue, "model": "orderbook_fok", "fill": fill, "book": book}, now)
                r["qty"], r["asset"], r["venue"], r["net"] = str(output), leg["target"], venue, ""
                r["leg"] += 1
                r["stage_ts"], r["note"] = now, ""
                return
            q = snap.spot.get(venue, {}).get(alt)
            if not q or paper._venue_down(snap, venue, alt) or not 0 < getattr(snap, "ts", 0) <= now <= snap.ts + 120:
                r["note"] = "Нет котировки спота; монеты сохранены"
                if elapsed >= 1800:
                    r["stage"], r["stage_ts"] = "sell", now - 1800
                return
            bid, ask = map(dec, q)
            if bid <= 0 or ask < bid:
                return
            qty = dec(r["qty"])
            gross = qty / ask if r["asset"] == "USDT" else qty * bid
            fee = dec(leg["fee_pct"]) / 100
            output = (gross * (1 - fee)).quantize(COIN, rounding=ROUND_DOWN)
            if output <= 0:
                return
            _event(con, r["id"], "spot", {"input": str(qty), "asset": r["asset"], "target": leg["target"],
                                          "output": str(output), "fee": str(gross * fee), "venue": venue,
                                          "bid": str(bid), "ask": str(ask), "snapshot_ts": snap.ts,
                                          "model": "ticker_without_depth"}, now)
            r["qty"], r["asset"], r["venue"] = str(output), leg["target"], venue
            r["net"] = ""
            if "Спот: котировка без глубины" not in r["assumptions"]:
                r["assumptions"].append("Спот: котировка без глубины")
        else:
            if not r["in_transit"]:
                closed, unknown = paper._hop_state(leg) if leg["frm"] != leg["to"] else ([], [])
                if closed or unknown:
                    r["note"] = "; ".join(closed + unknown) + "; монеты сохранены"
                    if elapsed >= 1800:
                        r["stage"], r["stage_ts"] = "sell", now - 1800
                    return
                qty, fee = dec(r["qty"]), dec(leg.get("fee") or 0)
                if leg["frm"] != leg["to"]:
                    current_fee, _, current_net = p2p._hop_detail(
                        cfg, leg["frm"], leg.get("frm_net") or "", leg["to"], leg.get("to_net") or "",
                        leg["asset"], qty=float(qty), parts=leg.get("parts") or 1)
                    if current_fee is None or (leg.get("to_net") and current_net != leg["to_net"]):
                        r["note"] = "Перевод недоступен для фактического остатка"
                        if elapsed >= 1800:
                            r["stage"], r["stage_ts"] = "sell", now - 1800
                        return
                    fee = dec(current_fee)
                if qty <= fee:
                    r["note"] = "Остаток меньше комиссии перевода"
                    return
                r["qty"] = str(qty - fee)
                r["in_transit"], r["stage_ts"] = True, now
                _event(con, r["id"], "transfer_sent", {"qty": r["qty"], "fee": str(fee), "hop": leg}, now)
                return
            delay = leg["minutes"]
            if elapsed < delay * 60:
                return
            if leg["frm"] != leg["to"]:
                closed, unknown = paper._hop_state(leg)
                if closed or unknown:
                    r["note"] = "Зачисление не подтверждено: " + "; ".join(closed + unknown)
                    return
            r["venue"], r["net"], r["in_transit"] = leg["to"], leg.get("to_net") or "", False
            _event(con, r["id"], "transfer_received", {"qty": r["qty"], "venue": r["venue"]}, now)
        r["leg"] += 1
        r["stage_ts"], r["note"] = now, ""
        return
    if r["stage"] == "sell":
        remaining = dec(r["qty"])
        for ad in _ads(snap, r["venue"], "sell", r["asset"], r["net"], now):
            bank_quote = _receivable(con, r, ad, now)
            if bank_quote is False:
                continue
            qty = _take(con, ad, remaining)
            if not qty:
                continue
            price = dec(ad.price)
            # First 30 minutes preserve the projected break-even; afterwards accept losses.
            if elapsed < 1800 and qty * price - (dec(bank_quote["fee"]) if bank_quote else ZERO) < dec(r["cost"]) * qty / remaining:
                # No execution: undo the provisional offer consumption.
                key = _offer_key(ad)
                used, = con.execute("SELECT qty FROM consumed WHERE key=?", (key,)).fetchone()
                con.execute("UPDATE consumed SET qty=? WHERE key=?", (str(dec(used) - qty), key))
                continue
            proceeds = (qty * price).quantize(RUB, rounding=ROUND_DOWN)
            if bank_quote:
                proceeds -= dec(bank_quote["fee"])
                if proceeds < 0:
                    raise bankmodel.Blocked("Комиссия получения превышает платёж")
                bankmodel.payment(con, bank_quote, now, r["id"])
            cost = dec(r["cost"]) * qty / remaining
            r["cost"] = str(dec(r["cost"]) - cost)
            r["realized"] = str(dec(r["realized"]) + proceeds - cost)
            r["proceeds"] = str(dec(r["proceeds"]) + proceeds)
            remaining -= qty
            _cash(con, proceeds, bank_quote["account"] if bank_quote else None, now)
            _event(con, r["id"], "sell", {"qty": str(qty), "rub": str(proceeds), "cost": str(cost),
                                          "price": str(price), "quote_ts": ad.fetched_ts, "bank_quote": bank_quote}, now)
        r["qty"] = str(remaining)
        if remaining == 0:
            r["stage"] = "done"
        else:
            r["note"] = "Остаток сохранён; ждём доступную продажу"


def summary(path=DB_PATH):
    if not os.path.exists(path):
        return {"initial": "50000.00", "cash": "50000.00", "reserved": "0", "realized": "0", "runs": []}
    con = connect(path)
    try:
        row = con.execute("SELECT initial,cash FROM wallet").fetchone()
        values = [json.loads(r[0]) for r in con.execute("SELECT state FROM runs")]
        last_mark = con.execute("SELECT ts,kind,details FROM events WHERE kind IN ('valuation','valuation_unknown') "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        marks = (last_mark[0], json.loads(last_mark[2])["value"]) if last_mark and last_mark[1] == "valuation" else None
        peak, max_drawdown = dec("50000"), ZERO
        for value, in con.execute("SELECT value FROM equity ORDER BY ts"):
            peak = max(peak, dec(value))
            max_drawdown = max(max_drawdown, (peak - dec(value)) / peak * 100)
        now = time.time()
        periods = {}
        for name, since in (("day", paper._day_start(now)), ("week", now - 7 * 86400)):
            periods[name] = str(sum((dec(d["rub"]) - dec(d["cost"]) for raw, in
                                    con.execute("SELECT details FROM events WHERE kind='sell' AND ts>=?", (since,))
                                    for d in [json.loads(raw)]), ZERO))
        expenses = sum((dec(v) for v, in con.execute("SELECT amount FROM bank_expenses")), ZERO)
        transit = sum((dec(v) for v, in con.execute("SELECT amount FROM bank_transfers WHERE state='pending'")), ZERO)
        strict_profit = sum((dec(r["realized"]) for r in values
                             if r["settings"].get("bank_model") == "strict" and not r.get("origin_model")), ZERO)
        return {"bank_expenses": str(expenses), "bank_in_transit": str(transit),
                "strict_realized": str(strict_profit),
                "net_realized": str(sum((dec(r["realized"]) for r in values), ZERO) - expenses),
                "initial": row[0] if row else "50000.00", "cash": row[1] if row else "50000.00",
                "reserved": str(sum((dec(r["reserved"]) for r in values), ZERO)),
                "realized": str(sum((dec(r["realized"]) for r in values), ZERO)), "runs": values,
                "equity": marks[1] if marks else None, "mark_ts": marks[0] if marks else None,
                "period_profit": periods,
                "drawdown_pct": str(max_drawdown) if marks else None}
    finally:
        con.close()


def report_lines(path=DB_PATH):
    s = summary(path)
    lines = ["🧪 <b>Виртуальный портфель</b>", f"Начальный капитал: {dec(s['initial']):.2f} ₽",
             f"Свободно: {dec(s['cash']):.2f} ₽ · резерв: {dec(s['reserved']):.2f} ₽",
             f"Прибыль исполненных продаж: {dec(s['realized']):+.2f} ₽",
             "Открытые позиции оцениваются отдельно; неизвестная цена не равна нулю."]
    if "bank_expenses" in s:
        lines.append(f"Расходы счетов/собственных переводов: {dec(s['bank_expenses']):.2f} ₽ · "
                     f"результат после расходов: {dec(s['net_realized']):+.2f} ₽")
        lines.append(f"Рубли между своими счетами в пути: {dec(s['bank_in_transit']):.2f} ₽")
        lines.append(f"Прибыль новых кругов строгого этапа: {dec(s['strict_realized']):+.2f} ₽")
    if s.get("equity") is not None:
        pnl = dec(s["equity"]) - dec(s["initial"])
        lines.append(f"Оценка портфеля: {dec(s['equity']):.2f} ₽ · результат {pnl:+.2f} ₽ "
                     f"({pnl / dec(s['initial']) * 100:+.2f}%) · макс. просадка {dec(s['drawdown_pct']):.2f}%")
        lines.append(f"Оценка на {time.strftime('%d.%m %H:%M:%S', time.localtime(s['mark_ts']))}")
        lines.append(f"Переоценка открытых позиций: {pnl - dec(s['realized']):+.2f} ₽")
    else:
        lines.append("Общая оценка и доходность: нет свежей полной оценки открытых позиций.")
    if s.get("period_profit"):
        lines.append(f"Исполненные продажи: сегодня {dec(s['period_profit']['day']):+.2f} ₽ · "
                     f"7 дней {dec(s['period_profit']['week']):+.2f} ₽")
    for r in s["runs"]:
        for dust in r.get("dust", []):
            if dec(dust["qty"]):
                lines.append(f"Остаток округления #{r['id']}: {dust['qty']} {dust['asset']} на {html.escape(dust['venue'])}")
        if r["stage"] in ("done", "cancelled"):
            continue
        lines.append(f"#{r['id']} · {r['stage']} · {r['venue']} · {r['qty']} {r['asset']}"
                     + (" (в пути)" if r["in_transit"] else "") + f" · себестоимость {dec(r['cost']):.2f} ₽")
        if r["note"]:
            lines.append(html.escape(r["note"]))
    if any("Спот: котировка без глубины" in r["assumptions"] for r in s["runs"]):
        lines.append("В истории есть обмены по тикеру без глубины; они помечены как приближённые.")
    if any(r.get("origin_model") or r["settings"].get("bank_model", "legacy") != "strict" for r in s["runs"]):
        lines.append("В истории есть операции прежней модели без проверки персональных банковских условий.")
    lines.append("P2P: модель исполнения объявлений; спот: весь допустимый объём по стакану либо ожидание. Хеджи исключены.")
    return lines


def _mark(con, snap, now):
    """Net liquidation estimate with temporary cumulative bank-limit consumption."""
    row = con.execute("SELECT cash FROM wallet").fetchone()
    total = dec(row[0]) if row else dec("50000")
    positions = {}
    strict = os.getenv("PAPER_BANK_MODEL", "strict") == "strict"
    reason = "bank_transfer_in_transit" if con.execute(
        "SELECT 1 FROM bank_transfers WHERE state='pending' LIMIT 1").fetchone() else None
    for state, in con.execute("SELECT state FROM runs"):
        r = json.loads(state)
        total += dec(r["reserved"])
        strict = strict or r["settings"].get("bank_model") == "strict"
        for position in r.get("dust", []) + ([r] if dec(r["qty"]) else []):
            if position.get("in_transit"):
                reason = "in_transit"
                break
            key = (position["venue"], position["asset"],
                   position["net"] if position["venue"] == "BestChange" else "")
            positions[key] = positions.get(key, ZERO) + dec(position["qty"])
    con.execute("SAVEPOINT bank_valuation")
    try:
        data = bankmodel.load() if strict and positions else None
        for (venue, asset, net), qty in positions.items():
            remaining = qty
            for ad in _ads(snap, venue, "sell", asset, net, now):
                used = con.execute("SELECT qty FROM consumed WHERE key=?", (_offer_key(ad),)).fetchone()
                available = max(ZERO, dec(ad.avail) - (dec(used[0]) if used else ZERO))
                take = min(remaining, available, dec(ad.max_amt) / dec(ad.price))
                gross = (take * dec(ad.price)).quantize(RUB, rounding=ROUND_DOWN)
                if take <= 0 or take * dec(ad.price) < dec(ad.min_amt):
                    continue
                fee = ZERO
                if data:
                    try:
                        profile, _, q = bankmodel.select(con, data, ad.pays, "in", gross, now, venue=venue)
                        execution_review.check(profile, ad, now)
                        fee = dec(q["fee"])
                        if fee > gross:
                            continue
                        bankmodel.payment(con, q, now, None)
                    except bankmodel.Blocked:
                        continue
                total += gross - fee
                remaining -= take
            if remaining > 0:
                reason = "insufficient_fresh_depth_or_bank_limits"
                break
    except bankmodel.Blocked:
        reason = "bank_conditions_not_confirmed"
    finally:
        con.execute("ROLLBACK TO bank_valuation")
        con.execute("RELEASE bank_valuation")
    if reason:
        _event(con, None, "valuation_unknown", {"reason": reason}, now)
        return
    value = str(total.quantize(RUB, rounding=ROUND_DOWN))
    con.execute("INSERT OR REPLACE INTO equity VALUES (?,?)", (now, value))
    _event(con, None, "valuation", {"value": value}, now)


def _verify(con):
    """Cash + reserved + remaining historical cost - realized P&L = initial capital."""
    row = con.execute("SELECT initial,cash FROM wallet").fetchone()
    if not row:
        return
    initial, cash = map(dec, row)
    balance = cash
    balance += sum((dec(amount) for amount, in con.execute("SELECT amount FROM bank_transfers WHERE state='pending'")), ZERO)
    balance += sum((dec(amount) for amount, in con.execute("SELECT amount FROM bank_expenses")), ZERO)
    for state, in con.execute("SELECT state FROM runs"):
        r = json.loads(state)
        for name in ("reserved", "qty", "cost"):
            if dec(r[name]) < 0:
                raise ValueError(f"Negative {name} in run {r['id']}")
        balance += dec(r["reserved"]) + dec(r["cost"]) - dec(r["realized"])
        for dust in r.get("dust", []):
            if dec(dust["qty"]) < 0 or dec(dust["cost"]) < 0:
                raise ValueError("Negative rounding remainder")
            balance += dec(dust["cost"])
    bankmodel.reconcile(con)
    if abs(balance - initial) > Decimal("0.000000000001"):
        raise ValueError("Portfolio reconciliation failed")


def reset(path=DB_PATH):
    """Archive a consistent SQLite snapshot before resetting the independent wallet."""
    if not os.path.exists(path):
        return None
    archive = os.path.join(os.path.dirname(path), f"paper-portfolio-archive-{time.time_ns()}.db")
    con = connect(path)
    source = sqlite3.connect(path)
    target = sqlite3.connect(archive)
    try:
        con.execute("BEGIN IMMEDIATE")
        source.backup(target)
        target.close()
        for table in ("wallet", "runs", "events", "consumed", "equity", "bank_accounts", "bank_payments", "bank_reservations", "bank_events", "bank_transfers", "bank_expenses"):
            con.execute(f"DELETE FROM {table}")
        con.execute("INSERT INTO wallet VALUES (1,'50000.00','50000.00')")
        _event(con, None, "reset", {"archive": os.path.basename(archive)}, time.time())
        con.commit()
        return archive
    finally:
        source.close()
        target.close()
        con.close()


def bank_month_total(bank, path=DB_PATH, now=None):
    now = time.time() if now is None else now
    if not os.path.exists(path):
        return 0.0
    con = connect(path)
    try:
        purchases = [json.loads(d) for d, in con.execute("SELECT details FROM events WHERE kind='buy' AND ts>=?",
                                                       (paper._month_start(now),))]
        return float(sum((dec(p["rub"]) for p in purchases if p.get("bank") == bank and p.get("pay_kind") == "sbp"), ZERO))
    finally:
        con.close()


def replay(path=DB_PATH):
    """Reconstruct latest run states and free RUB from the audit journal."""
    con = connect(path)
    try:
        states = {}
        for detail, in con.execute("SELECT details FROM events WHERE kind='state' ORDER BY id"):
            state = json.loads(detail)
            states[state["id"]] = state
        with localcontext() as ctx:
            ctx.prec = 40
            cash = dec("50000") + sum((dec(r["proceeds"]) - dec(r["spent"]) - dec(r["reserved"])
                                       for r in states.values()), ZERO)
        transfers, expenses = {}, ZERO
        for kind, raw in con.execute("SELECT kind,details FROM bank_events ORDER BY id"):
            detail = json.loads(raw)
            if kind == "transfer_sent":
                transfers[detail["id"]] = dec(detail["amount"])
                expenses += dec(detail["fee"])
            elif kind == "transfer_received":
                transfers.pop(detail["id"], None)
            elif kind == "service_fee":
                expenses += dec(detail["amount"])
        cash -= expenses + sum(transfers.values(), ZERO)
        return {"cash": str(cash), "runs": list(states.values()), "bank_accounts": bankmodel.replay(con)}
    finally:
        con.close()


def needed_books(path=DB_PATH):
    pairs = set()
    for r in runs(path, active=True):
        if r["stage"] != "route" or r["leg"] >= len(r["route"]) or r["settings"].get("spot_model", "depth") == "ticker":
            continue
        leg = r["route"][r["leg"]]
        if leg["kind"] == "spot":
            pairs.add((leg["venue"], leg["target"] if leg["asset"] == "USDT" else leg["asset"]))
    return pairs


def _sell_dust(con, run, snap, now):
    for dust in run.get("dust", []):
        remaining = dec(dust["qty"])
        for ad in _ads(snap, dust["venue"], "sell", dust["asset"], dust["net"], now):
            bank_quote = _receivable(con, dict(run, qty=str(remaining)), ad, now)
            if bank_quote is False:
                run["note"] = "Не подтверждён счёт для продажи остатка"
                continue
            qty = _take(con, ad, remaining)
            if not qty:
                continue
            proceeds = (qty * dec(ad.price)).quantize(RUB, rounding=ROUND_DOWN)
            if bank_quote:
                proceeds -= dec(bank_quote["fee"])
                if proceeds < 0:
                    raise bankmodel.Blocked("Комиссия получения превышает платёж")
                bankmodel.payment(con, bank_quote, now, run["id"])
            cost = dec(dust["cost"]) * qty / remaining
            dust["cost"] = str(dec(dust["cost"]) - cost)
            remaining -= qty
            run["proceeds"] = str(dec(run["proceeds"]) + proceeds)
            run["realized"] = str(dec(run["realized"]) + proceeds - cost)
            _cash(con, proceeds, bank_quote["account"] if bank_quote else None, now)
            _event(con, run["id"], "sell", {"qty": str(qty), "rub": str(proceeds), "cost": str(cost),
                                            "asset": dust["asset"], "venue": dust["venue"], "rounding_remainder": True, "bank_quote": bank_quote}, now)
        dust["qty"] = str(remaining)


def export(destination, path=DB_PATH):
    con = connect(path)
    try:
        rows = con.execute("SELECT id,run_id,ts,kind,details FROM events ORDER BY id").fetchall()
        rows += [("bank:" + str(eid), None, ts, "bank:" + kind, details) for eid, ts, kind, details in
                 con.execute("SELECT id,ts,kind,details FROM bank_events ORDER BY id")]
        rows.sort(key=lambda item: item[2])
        with open(destination, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("event_id", "run_id", "timestamp", "operation", "details"))
            writer.writerows(rows)
        return destination
    finally:
        con.close()


def bank_report(path=DB_PATH):
    try:
        data = bankmodel.load()
        con = connect(path)
        try:
            lines = bankmodel.report(con, data, time.time())
            row = con.execute("SELECT details FROM events WHERE kind='bank_blocked' ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                lines.append("Последний запрет: " + html.escape(json.loads(row[0])["reason"]))
            return lines
        finally:
            con.close()
    except bankmodel.Blocked as exc:
        return ["🏦 " + html.escape(str(exc))]


def own_transfer(source, target, amount, delay_seconds, path=DB_PATH, now=None):
    now = time.time() if now is None else now
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        data = bankmodel.load()
        bankmodel.initialize(con, data, now)
        result = bankmodel.transfer(con, data, source, target, amount, now, delay_seconds)
        _verify(con)
        con.commit()
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def activate_banks(path=DB_PATH, now=None):
    """Upgrade outstanding actions; retain every historical amount and cost basis."""
    now = time.time() if now is None else now
    data = bankmodel.load()
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        bankmodel.initialize(con, data, now)
        start_profile = next(p for p in data['accounts'] if p['id'] == data['start_account'])
        upgraded = []
        for state, in con.execute("SELECT state FROM runs").fetchall():
            run = json.loads(state)
            outstanding = run['stage'] not in ('done', 'cancelled') or any(
                dec(d['qty']) > 0 for d in run.get('dust', []))
            if not outstanding or run['settings'].get('bank_model') == 'strict':
                continue
            run['origin_model'] = 'pre_bank_policy'
            run['settings']['bank_model'] = 'strict'
            run['bank_account'] = start_profile['id']
            run['bank_profile'] = start_profile
            if dec(run['reserved']):
                con.execute('INSERT INTO bank_reservations VALUES (?,?,?,?,?,?)',
                            (run['id'], start_profile['id'], start_profile['scope'],
                             run['pay_kind'], run['reserved'], now))
            _save(con, run)
            _event(con, run['id'], 'state', run, now)
            upgraded.append(run['id'])
        _event(con, None, 'bank_model_activation', {'version': 1, 'upgraded_runs': upgraded,
                                                  'history_repriced': False}, now)
        _verify(con)
        con.commit()
        return upgraded
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
