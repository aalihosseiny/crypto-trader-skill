#!/usr/bin/env python3
"""crypto-margin: leveraged long / short positions on Nobitex (معاملات تعهدی), one ticket and one yes at a time.

Look
  python3 margin.py markets                         # which markets allow long / short, max leverage, daily fee
  python3 margin.py wallet                          # margin wallet balance (toman / USDT)
  python3 margin.py positions                       # open positions: entry, mark, P&L, LIQUIDATION price and distance

Move collateral (spot <-> margin wallet)
  python3 margin.py transfer 1000000 --to margin --confirm 1000000
  python3 margin.py transfer 500000 --to spot --confirm 500000

Propose (writes crypto/margin_orders.json and runs the margin risk check; nothing is sent)
  python3 margin.py open short BTC --margin 500000 --leverage 2          # toman market
  python3 margin.py open long ETH --margin 20 --leverage 2 --quote USDT   # tether market
  python3 margin.py close P_ID                                            # close a whole position
  python3 margin.py protect P_ID --stop 7300000000                        # stop-loss order on a position

Execute (same chain as crypto-broker)
  python3 margin.py show
  python3 margin.py approve m1 --words "<the user's reply, verbatim>"
  python3 margin.py place m1 --confirm m1
  python3 margin.py cancel-order ORDER_ID --confirm ORDER_ID              # cancel an open margin order (live)

Rules (crypto/config.json): allow_leverage must be true; max_leverage (2);
max_margin per position (1,000,000 toman / 50 USDT); max_open_positions (3);
min_liq_distance_pct (15): a position whose estimated liquidation price is
closer than this to the current price is blocked. Leverage steps are 0.5 and
never above the market's own maximum.

Liquidation (Nobitex isolated margin): a position is liquidated when its margin
ratio falls to 1.1. Estimates used before opening:
  long : liq = entry x 1.1 x (1 - 1/leverage)      (no liquidation at 1x)
  short: liq = entry x (1 + 1/leverage) / 1.1
After opening, the exchange's own liquidationPrice is shown and used.
Positions expire after 30 days and pay a daily extension fee.

Modes follow crypto/config.json: paper (simulated, default), live
($NOBITEX_TOKEN). Nobitex has no margin on its testnet docs, so testnet is not
offered here. Units: toman in, rial (x10) in every Nobitex payload.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULTS = {
    "exchange": "nobitex",
    "quote": "IRT",
    "mode": "paper",
    "cash_buffer_pct": 10,
    "max_position_pct": 25,
    "min_order": {"IRT": 300000, "USDT": 11},
    "max_order": {"IRT": 100000000, "USDT": 2000},
    "max_orders_per_day": 20,
    "blocked": [],
    "allowed": None,
    "max_spread_pct": 1.5,
    "max_price_age_min": 15,
    "max_limit_deviation_pct": 3,
    "slippage_pct": 0.5,
    "fee_pct": 0.25,
    "allow_leverage": False,
    "approval_ttl_min": 10,
    "qty_decimals": {},
    "max_leverage": 2,
    "max_margin": {"IRT": 1000000, "USDT": 50},
    "max_open_positions": 3,
    "min_liq_distance_pct": 15,
}
BASE = os.environ.get("NOBITEX_BASE", "https://apiv2.nobitex.ir")
UA = "crypto-trader-skill/1.0"
LIQ_RATIO = 1.1
HASH_SKIP = ("risk", "status")


# ---------------------------------------------------------------- helpers
def data_dir():
    return os.environ.get("CRYPTO_DIR", "crypto")


def dpath(name):
    return os.path.join(data_dir(), name)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in (load_json(dpath("config.json"), {}) or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def now_utc():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def parse_time(s):
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def fmt(x, quote):
    if x is None:
        return "-"
    return "{:,.0f}".format(x) if quote == "IRT" else "{:,.4f}".format(x)


def unit(q):
    return "toman" if q == "IRT" else "USDT"


def fnum(x):
    try:
        return float(str(x).replace("−", "-"))  # Nobitex sometimes sends a unicode minus
    except (TypeError, ValueError):
        return None


def to_ex(x, q):
    """Toman -> rial for Nobitex payloads; USDT unchanged."""
    return x * 10 if q == "IRT" else x


def from_ex(x, q):
    v = fnum(x)
    return None if v is None else (v / 10 if q == "IRT" else v)


def price_str(x, q):
    return str(int(round(to_ex(x, q)))) if q == "IRT" else ("%.8f" % x).rstrip("0").rstrip(".")


def order_hash(o):
    core = {k: v for k, v in o.items() if k not in HASH_SKIP}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def okey(o):
    return "%s:%s" % (o["id"], order_hash(o))


def http(method, url, headers=None, body=None, timeout=30):
    """One HTTP call, never retried (a retried POST could open a second position). Tests replace this."""
    h = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"status": "failed", "http": e.code}


def auth():
    tok = os.environ.get("NOBITEX_TOKEN")
    return {"Authorization": "Token " + tok} if tok else None


def need_auth():
    h = auth()
    if not h:
        sys.exit("refused: $NOBITEX_TOKEN is not set. Set it in your own terminal, never paste keys in the chat.")
    return h


def log_event(ev):
    log = load_json(dpath("margin_log.json"), {"events": []}) or {"events": []}
    log["events"].append(dict(ev, at=now_utc().isoformat()))
    save_json(dpath("margin_log.json"), log)


def sent_state():
    log = load_json(dpath("margin_log.json"), {"events": []}) or {"events": []}
    state = {}
    for e in log["events"]:
        k = e.get("key")
        if e.get("action") == "sending":
            state[k] = dict(e, summary="IN FLIGHT since %s: no answer recorded; check `margin.py positions` and the "
                                       "Nobitex app before anything else" % e.get("at"))
        elif e.get("action") == "placed":
            state[k] = e
        elif e.get("action") == "rejected" and state.get(k, {}).get("action") == "sending":
            del state[k]
    return state


def placed_today():
    day = now_utc().date()
    return sum(1 for e in (load_json(dpath("margin_log.json"), {"events": []}) or {"events": []})["events"]
               if e.get("action") == "placed" and (parse_time(e.get("at")) or now_utc()).date() == day)


# ---------------------------------------------------------------- market info
def market_of(coin, q):
    return ((load_json(dpath("prices.json"), {}) or {}).get("markets") or {}).get(coin + q)


def margin_markets(refresh=False):
    cached = load_json(dpath("margin_markets.json"))
    if cached and not refresh:
        return cached
    d = http("GET", BASE + "/margin/markets/list", auth())
    if d.get("status") != "ok":
        raise RuntimeError("nobitex margin markets: %s" % d)
    out = {"updated": now_utc().isoformat(), "markets": {}}
    for sym, m in (d.get("markets") or {}).items():
        q = "IRT" if m.get("dstCurrency") == "rls" else "USDT"
        out["markets"][m["srcCurrency"].upper() + q] = {
            "long": bool(m.get("buyEnabled")), "short": bool(m.get("sellEnabled")),
            "max_leverage": fnum(m.get("maxLeverage")) or 1, "daily_fee": fnum(m.get("positionFeeRate"))}
    save_json(dpath("margin_markets.json"), out)
    return out


def est_liq(side, entry, lev):
    if side == "long":
        return None if lev <= 1 else entry * LIQ_RATIO * (1 - 1.0 / lev)
    return entry * (1 + 1.0 / lev) / LIQ_RATIO


def qty_decimals(coin, price, q, cfg):
    over = (cfg.get("qty_decimals") or {}).get(coin)
    if over is not None:
        return int(over)
    usdt = (load_json(dpath("prices.json"), {}) or {}).get("usdt_irt") or 100000
    usd = price if q == "USDT" else price / max(usdt, 1)
    return 6 if usd >= 1000 else 4 if usd >= 10 else 2 if usd >= 0.1 else 0


def floor_to(x, d):
    return math.floor(x * 10 ** d + 1e-9) / 10 ** d


# ---------------------------------------------------------------- paper book
def paper():
    return load_json(dpath("margin_paper.json"), {"wallet": {"IRT": 0.0, "USDT": 0.0}, "positions": [], "next": 1})


def paper_mark(pos, price, book=None, fee_rate=0.0025):
    """Update a paper position with the current price: a stop-loss fires first, then liquidation."""
    if pos["status"] != "Open" or price is None:
        return pos
    stop, liq = pos.get("stop"), pos.get("liq")
    if stop and ((pos["side"] == "long" and price <= stop) or (pos["side"] == "short" and price >= stop)):
        move = (stop - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - stop)
        fee = pos["qty"] * stop * fee_rate
        pos.update(status="Stopped", exit=stop, pnl=move * pos["qty"] - fee - pos.get("fees", 0), closed=now_utc().isoformat())
        if book is not None:
            book["wallet"][pos["quote"]] = book["wallet"].get(pos["quote"], 0.0) + pos["collateral"] + move * pos["qty"] - fee
        return pos
    if liq and ((pos["side"] == "long" and price <= liq) or (pos["side"] == "short" and price >= liq)):
        pos.update(status="Liquidated", exit=liq, pnl=-pos["collateral"] - pos.get("fees", 0), closed=now_utc().isoformat())
        return pos
    move = (price - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - price)
    pos["mark"] = price
    pos["upnl"] = move * pos["qty"]
    return pos


# ---------------------------------------------------------------- risk
def check_order(o, cfg, mm, open_count, wallet):
    """Return a list of reasons to block (empty = PASS)."""
    r = []
    q, coin = o["quote"], o["coin"]
    m = market_of(coin, q)
    if o["kind"] == "open":
        if not cfg.get("allow_leverage"):
            r.append("leverage is off: set allow_leverage to true in crypto/config.json first")
        info = (mm.get("markets") or {}).get(coin + q)
        if not info:
            r.append("%s%s has no margin market on Nobitex" % (coin, q))
        else:
            if not info.get(o["side"]):
                r.append("%s is not enabled on %s%s (Nobitex allows: %s)" % (
                    o["side"], coin, q, ", ".join(s for s in ("long", "short") if info.get(s)) or "nothing"))
            if o["leverage"] > info["max_leverage"]:
                r.append("leverage %sx is above the market maximum %sx" % (o["leverage"], info["max_leverage"]))
        if o["leverage"] > cfg["max_leverage"]:
            r.append("leverage %sx is above your max_leverage %sx" % (o["leverage"], cfg["max_leverage"]))
        if o["leverage"] < 1 or abs(o["leverage"] * 2 - round(o["leverage"] * 2)) > 1e-9:
            r.append("leverage must be 1, 1.5, 2, 2.5 ...")
        mx = cfg["max_margin"].get(q)
        if mx and o["margin"] > mx:
            r.append("margin %s is over your max_margin %s %s" % (fmt(o["margin"], q), fmt(mx, q), unit(q)))
        mn = cfg["min_order"].get(q, 0)
        if o["margin"] * o["leverage"] < mn:
            r.append("position size %s is under the minimum order %s" % (fmt(o["margin"] * o["leverage"], q), fmt(mn, q)))
        if open_count >= cfg["max_open_positions"]:
            r.append("already %d open positions (max_open_positions %d)" % (open_count, cfg["max_open_positions"]))
        if wallet is not None and wallet < o["margin"] * (1 + o["leverage"] * cfg["fee_pct"] / 100):
            r.append("margin wallet has %s %s; transfer more first" % (fmt(wallet, q), unit(q)))
        if (o["coin"] in [c.upper() for c in cfg.get("blocked") or []]):
            r.append("%s is on your blocked list" % coin)
    if not m or not m.get("last"):
        r.append("no price for %s%s: run crypto-alerts prices %s%s" % (coin, q, coin, " --quote USDT" if q == "USDT" else ""))
        return r
    t = parse_time(m.get("updated"))
    if not t or (now_utc() - t).total_seconds() / 60 > cfg["max_price_age_min"]:
        r.append("price is stale: refresh prices")
    bid, ask = m.get("bid"), m.get("ask")
    if bid and ask and (ask - bid) / ((ask + bid) / 2) * 100 > cfg["max_spread_pct"]:
        r.append("spread is over %s%%" % cfg["max_spread_pct"])
    if o["kind"] == "open":
        liq = est_liq(o["side"], m["last"], o["leverage"])
        if liq:
            dist = abs(liq / m["last"] - 1) * 100
            if dist < cfg["min_liq_distance_pct"]:
                r.append("estimated liquidation %s is only %.1f%% away (min_liq_distance_pct %s)" % (
                    fmt(liq, q), dist, cfg["min_liq_distance_pct"]))
    if o["kind"] == "protect":
        s, px, liq = o["stop_price"], m["last"], o.get("liq")
        if o["side"] == "long" and not s < px:
            r.append("a long's stop must be below the price %s" % fmt(px, q))
        if o["side"] == "short" and not s > px:
            r.append("a short's stop must be above the price %s" % fmt(px, q))
        if liq and ((o["side"] == "long" and s <= liq) or (o["side"] == "short" and s >= liq)):
            r.append("stop %s is beyond the liquidation price %s: it would never fire first" % (fmt(s, q), fmt(liq, q)))
    if o["kind"] in ("open",) and placed_today() >= cfg["max_orders_per_day"]:
        r.append("daily limit of %d margin orders reached" % cfg["max_orders_per_day"])
    return r


# ---------------------------------------------------------------- tickets
def build_ticket(o, cfg):
    q, coin = o["quote"], o["coin"]
    m = market_of(coin, q)
    if not m:
        return None, "no price for %s%s" % (coin, q)
    slip = cfg["slippage_pct"] / 100.0
    # opening a long or closing a short buys; opening a short or closing a long sells
    buying = (o["kind"] == "open" and o["side"] == "long") or (o["kind"] != "open" and o["side"] == "short")
    ref = (m.get("ask") if buying else m.get("bid")) or m["last"]
    limit = round(ref * (1 + slip) if buying else ref * (1 - slip), 4)
    if q == "IRT":
        limit = math.floor(limit) if buying else math.ceil(limit)
    else:
        limit = floor_to(limit, 2 if limit >= 10 else 6) if buying else math.ceil(limit * 100) / 100
    if o["kind"] == "open":
        notional = o["margin"] * o["leverage"]
        dec = qty_decimals(coin, limit, q, cfg)
        qty = floor_to(notional / limit, dec)
        if qty <= 0:
            return None, "quantity rounds to zero"
        payload = {"srcCurrency": coin.lower(), "dstCurrency": "rls" if q == "IRT" else "usdt",
                   "type": "buy" if o["side"] == "long" else "sell", "execution": "limit",
                   "leverage": ("%g" % o["leverage"]), "amount": ("{:.%df}" % dec).format(qty), "price": price_str(limit, q)}
        path = "/margin/orders/add"
        liq = est_liq(o["side"], limit, o["leverage"])
        words = "OPEN %s %sx: %s %s %s, price %s %s %s, margin %s %s, position about %s %s, est. liquidation %s" % (
            o["side"].upper(), "%g" % o["leverage"], "BUY" if buying else "SELL", payload["amount"], coin,
            "at most" if buying else "at least", fmt(limit, q), unit(q), fmt(o["margin"], q), unit(q),
            fmt(qty * limit, q), unit(q), fmt(liq, q) if liq else "none at 1x")
    elif o["kind"] == "close":
        payload = {"execution": "limit", "amount": o["amount"], "price": price_str(limit, q)}
        path = "/positions/%s/close" % o["position_id"]
        words = "CLOSE %s position %s: %s %s %s at %s %s %s" % (
            o["side"], o["position_id"], "BUY back" if buying else "SELL", o["amount"], coin,
            "at most" if buying else "at least", fmt(limit, q), unit(q))
    else:  # protect
        stop = o["stop_price"]
        worst = stop * (1 + 2 * slip) if buying else stop * (1 - 2 * slip)
        payload = {"execution": "stop_market", "amount": o["amount"], "stopPrice": price_str(stop, q),
                   "price": price_str(worst, q)}
        path = "/positions/%s/close" % o["position_id"]
        words = "STOP-LOSS on %s position %s: if the price reaches %s %s, %s %s %s at market" % (
            o["side"], o["position_id"], fmt(stop, q), unit(q), "buy back" if buying else "sell", o["amount"], coin)
    t = {"order_id": o["id"], "order_hash": order_hash(o), "kind": o["kind"], "path": path, "payload": payload,
         "limit": limit, "quote": q, "coin": coin, "words": words, "built_at": now_utc().isoformat()}
    return t, None


def readiness(o):
    r = o.get("risk")
    if not r:
        return "NOT CHECKED"
    if r["verdict"] != "PASS":
        return "BLOCKED: " + "; ".join(r["reasons"])
    if r["hash"] != order_hash(o):
        return "CHANGED after the check: propose it again"
    return None


# ---------------------------------------------------------------- commands
def load_orders():
    return load_json(dpath("margin_orders.json"), {"orders": []}) or {"orders": []}


def add_order(o, cfg):
    doc = load_orders()
    used = [int(x["id"][1:]) for x in doc["orders"] if x["id"][1:].isdigit()]
    o["id"] = "m%d" % (max(used or [0]) + 1)
    mm = load_json(dpath("margin_markets.json"), {}) or {}
    if o["kind"] == "open" and not mm and cfg["mode"] == "live":
        mm = margin_markets(refresh=True)
    if cfg["mode"] == "paper":
        book = paper()
        n_open = sum(1 for p in book["positions"] if p["status"] == "Open")
        wallet = book["wallet"].get(o["quote"], 0.0)
        if not mm:
            mm = {"markets": {o["coin"] + o["quote"]: {"long": True, "short": True, "max_leverage": 5, "daily_fee": 0.001}},
                  "note": "paper mode without Nobitex market data: assumes both sides and 5x are allowed"}
    else:
        n_open = len(live_positions()) if o["kind"] == "open" else 0
        wallet = live_wallet().get(o["quote"]) if o["kind"] == "open" else None
    reasons = check_order(o, cfg, mm, n_open, wallet)
    o["status"] = "proposed"
    o["risk"] = {"verdict": "BLOCK" if reasons else "PASS", "reasons": reasons, "hash": order_hash(o),
                 "checked_at": now_utc().isoformat()}
    doc["orders"].append(o)
    save_json(dpath("margin_orders.json"), doc)
    print("%s %s  %s" % (o["risk"]["verdict"], o["id"], "; ".join(reasons)))
    if not reasons:
        t, prob = build_ticket(o, cfg)
        print("   " + (t["words"] if t else prob))
        print("   show it to the user, get a yes, then: margin.py approve %s --words \"...\"" % o["id"])


def cmd_open(args, cfg):
    q = (args.quote or cfg["quote"]).upper()
    add_order({"kind": "open", "side": args.side, "coin": args.coin.upper(), "quote": q,
               "margin": float(args.margin), "leverage": float(args.leverage), "exchange": "nobitex"}, cfg)


def find_position(pid, cfg):
    if cfg["mode"] == "paper":
        for p in paper()["positions"]:
            if str(p["id"]) == str(pid) and p["status"] == "Open":
                return p
        sys.exit("no open paper position %s" % pid)
    for p in live_positions():
        if str(p["id"]) == str(pid):
            return p
    sys.exit("no open position %s on Nobitex" % pid)


def cmd_close(args, cfg):
    p = find_position(args.position, cfg)
    add_order({"kind": "close", "position_id": str(p["id"]), "side": p["side"], "coin": p["coin"], "quote": p["quote"],
               "amount": p["liability_str"], "exchange": "nobitex"}, cfg)


def cmd_protect(args, cfg):
    p = find_position(args.position, cfg)
    add_order({"kind": "protect", "position_id": str(p["id"]), "side": p["side"], "coin": p["coin"], "quote": p["quote"],
               "amount": p["liability_str"], "stop_price": float(args.stop), "liq": p.get("liq"), "exchange": "nobitex"}, cfg)


def cmd_show(args, cfg):
    doc = load_orders()
    st = sent_state()
    print("mode: %s" % cfg["mode"].upper())
    if not doc["orders"]:
        print("no margin orders proposed")
    for o in doc["orders"]:
        if okey(o) in st:
            print("%-4s %s" % (o["id"], "PLACED " + st[okey(o)].get("summary", "") if st[okey(o)]["action"] == "placed"
                                else st[okey(o)]["summary"]))
            continue
        why = readiness(o)
        t, prob = build_ticket(o, cfg)
        print("%-4s %-8s %s" % (o["id"], (why or prob or "READY").split(":")[0], t["words"] if t else ""))
        if why or prob:
            print("      " + (why or prob))


def get_order(oid):
    doc = load_orders()
    for o in doc["orders"]:
        if o["id"] == oid:
            return doc, o
    sys.exit("no margin order %s" % oid)


def cmd_approve(args, cfg):
    if not args.words.strip():
        sys.exit("approve needs --words with the user's own reply, word for word")
    doc, o = get_order(args.id)
    if okey(o) in sent_state():
        sys.exit("%s was already sent" % o["id"])
    why = readiness(o)
    if why:
        sys.exit("cannot approve %s: %s" % (o["id"], why))
    m = market_of(o["coin"], o["quote"]) or {}
    t_upd = parse_time(m.get("updated"))
    if not t_upd or (now_utc() - t_upd).total_seconds() / 60 > cfg["max_price_age_min"]:
        sys.exit("cannot approve %s: price is older than %d minutes; refresh prices and show the ticket again"
                 % (o["id"], cfg["max_price_age_min"]))
    t, prob = build_ticket(o, cfg)
    if prob:
        sys.exit("cannot approve %s: %s" % (o["id"], prob))
    ap = load_json(dpath("margin_approvals.json"), {"approvals": {}}) or {"approvals": {}}
    ap["approvals"][o["id"]] = {"ticket": t, "words": args.words.strip(), "at": now_utc().isoformat(), "mode": cfg["mode"]}
    save_json(dpath("margin_approvals.json"), ap)
    log_event({"action": "approved", "key": okey(o), "order_id": o["id"], "words": args.words.strip(), "mode": cfg["mode"]})
    print("approved %s: %s" % (o["id"], t["words"]))
    print("valid for %d minutes -> margin.py place %s --confirm %s" % (cfg["approval_ttl_min"], o["id"], o["id"]))


def paper_execute(o, t, cfg):
    book = paper()
    q = o["quote"]
    m = market_of(o["coin"], q)
    fee_rate = cfg["fee_pct"] / 100.0
    if o["kind"] == "open":
        px = m.get("ask") if o["side"] == "long" else m.get("bid")
        px = px or m["last"]
        qty = float(t["payload"]["amount"])
        notional = qty * px
        collateral = notional / o["leverage"]
        fee = notional * fee_rate
        if book["wallet"].get(q, 0.0) < collateral + fee - 1e-9:
            return None, "paper margin wallet has %s, needs %s: transfer first" % (fmt(book["wallet"].get(q, 0), q), fmt(collateral + fee, q))
        book["wallet"][q] = book["wallet"].get(q, 0.0) - collateral - fee
        pid = "p%d" % book["next"]
        book["next"] += 1
        pos = {"id": pid, "coin": o["coin"], "quote": q, "side": o["side"], "leverage": o["leverage"], "qty": qty,
               "entry": px, "collateral": collateral, "liq": est_liq(o["side"], px, o["leverage"]), "status": "Open",
               "opened": now_utc().isoformat(), "fees": fee, "liability_str": t["payload"]["amount"]}
        book["positions"].append(pos)
        msg = "paper: opened %s %s %sx, %s %s at %s, margin %s, liquidation ~%s" % (
            pid, o["side"], "%g" % o["leverage"], pos["qty"], o["coin"], fmt(px, q), fmt(collateral, q),
            fmt(pos["liq"], q) if pos["liq"] else "none")
    elif o["kind"] == "close":
        pos = next((p for p in book["positions"] if p["id"] == o["position_id"] and p["status"] == "Open"), None)
        if not pos:
            return None, "paper position %s is not open (liquidated?)" % o["position_id"]
        paper_mark(pos, m["last"], book, fee_rate)
        if pos["status"] != "Open":
            save_json(dpath("margin_paper.json"), book)
            return None, "paper position %s is already %s at %s" % (pos["id"], pos["status"].upper(), fmt(pos["exit"], q))
        px = (m.get("bid") if pos["side"] == "long" else m.get("ask")) or m["last"]
        move = (px - pos["entry"]) if pos["side"] == "long" else (pos["entry"] - px)
        fee = pos["qty"] * px * fee_rate
        pnl = move * pos["qty"] - fee - pos["fees"]
        book["wallet"][q] = book["wallet"].get(q, 0.0) + pos["collateral"] + move * pos["qty"] - fee
        pos.update(status="Closed", exit=px, pnl=pnl, closed=now_utc().isoformat())
        msg = "paper: closed %s at %s, P&L %s %s (fees included)" % (pos["id"], fmt(px, q), fmt(pnl, q), unit(q))
    else:
        pos = next((p for p in book["positions"] if p["id"] == o["position_id"] and p["status"] == "Open"), None)
        if not pos:
            return None, "paper position %s is not open" % o["position_id"]
        pos["stop"] = o["stop_price"]
        msg = "paper: stop-loss on %s at %s" % (pos["id"], fmt(o["stop_price"], q))
    save_json(dpath("margin_paper.json"), book)
    return msg, None


def cmd_place(args, cfg):
    if args.confirm != args.id:
        sys.exit("refused: --confirm must repeat the order id (%s)" % args.id)
    doc, o = get_order(args.id)
    st = sent_state()
    if okey(o) in st:
        e = st[okey(o)]
        sys.exit("refused: %s %s" % (o["id"], e["summary"] if e["action"] == "sending" else "was already placed (%s)" % e["at"]))
    why = readiness(o)
    if why:
        sys.exit("refused: %s" % why)
    ap = ((load_json(dpath("margin_approvals.json"), {}) or {}).get("approvals") or {}).get(o["id"])
    if not ap:
        sys.exit("refused: %s has no recorded approval; show the ticket, get the user's yes, then approve" % o["id"])
    t = ap["ticket"]
    if t["order_hash"] != order_hash(o):
        sys.exit("refused: the order changed after it was approved")
    if (now_utc() - parse_time(ap["at"])).total_seconds() / 60 > cfg["approval_ttl_min"]:
        sys.exit("refused: the approval is older than %d minutes; prices move, approve again" % cfg["approval_ttl_min"])
    if ap["mode"] != cfg["mode"]:
        sys.exit("refused: approved in %s mode but config is now %s; approve again" % (ap["mode"], cfg["mode"]))
    if o["kind"] == "open" and not cfg.get("allow_leverage"):
        sys.exit("refused: allow_leverage is false")
    if cfg["mode"] == "paper":
        msg, prob = paper_execute(o, t, cfg)
        if prob:
            log_event({"action": "rejected", "key": okey(o), "order_id": o["id"], "reason": prob, "mode": "paper"})
            sys.exit(prob)
        log_event({"action": "placed", "key": okey(o), "order_id": o["id"], "mode": "paper", "ticket": t, "summary": msg})
        print(msg)
        return
    if cfg["mode"] != "live":
        sys.exit("refused: margin runs in paper or live mode only (config mode is %s)" % cfg["mode"])
    h = need_auth()
    log_event({"action": "sending", "key": okey(o), "order_id": o["id"], "mode": "live", "ticket": t})
    resp = http("POST", BASE + t["path"], h, t["payload"])
    if resp.get("status") != "ok":
        log_event({"action": "rejected", "key": okey(o), "order_id": o["id"], "mode": "live", "response": resp})
        sys.exit("Nobitex refused: %s %s" % (resp.get("code", ""), resp.get("message", json.dumps(resp, ensure_ascii=False)[:300])))
    od = resp.get("order") or {}
    msg = "live: %s -> Nobitex order %s, status %s" % (t["words"], od.get("id"), od.get("status"))
    log_event({"action": "placed", "key": okey(o), "order_id": o["id"], "mode": "live", "ticket": t, "response": resp,
               "summary": msg})
    print(msg)
    print("the position appears once the order fills: margin.py positions")


def norm_live(p):
    q = "IRT" if p.get("dstCurrency") == "rls" else "USDT"
    return {"id": str(p["id"]), "coin": p["srcCurrency"].upper(), "quote": q,
            "side": "long" if p.get("side") == "buy" else "short", "status": p.get("status"),
            "leverage": fnum(p.get("leverage")), "qty": fnum(p.get("delegatedAmount")),
            "liability_str": str(p.get("liability") or p.get("delegatedAmount")),
            "entry": from_ex(p.get("entryPrice"), q), "mark": from_ex(p.get("markPrice"), q),
            "liq": from_ex(p.get("liquidationPrice"), q), "collateral": from_ex(p.get("collateral"), q),
            "upnl": from_ex(p.get("unrealizedPNL"), q), "upnl_pct": fnum(p.get("unrealizedPNLPercent")),
            "margin_ratio": fnum(p.get("marginRatio")), "expires": p.get("expirationDate"),
            "daily_fee": from_ex(p.get("extensionFee"), q)}


def live_positions():
    d = http("GET", BASE + "/positions/list?status=active", need_auth())
    if d.get("status") != "ok":
        raise RuntimeError("nobitex positions: %s" % d)
    return [norm_live(p) for p in d.get("positions") or [] if p.get("status") == "Open"]


def live_wallet():
    d = http("GET", BASE + "/users/wallets/list?type=margin", need_auth())
    if d.get("status") != "ok":
        raise RuntimeError("nobitex margin wallet: %s" % d)
    out = {}
    for w in d.get("wallets") or []:
        cur = (w.get("currency") or "").lower()
        if cur == "rls":
            out["IRT"] = (fnum(w.get("activeBalance")) or 0) / 10
        elif cur == "usdt":
            out["USDT"] = fnum(w.get("activeBalance")) or 0
    return out


def cmd_positions(args, cfg):
    if cfg["mode"] == "paper":
        book = paper()
        for p in book["positions"]:
            m = market_of(p["coin"], p["quote"])
            paper_mark(p, m["last"] if m else None, book, cfg["fee_pct"] / 100.0)
        save_json(dpath("margin_paper.json"), book)
        rows = [p for p in book["positions"] if p["status"] == "Open" or args.all]
    else:
        rows = live_positions()
    print("mode: %s" % cfg["mode"].upper())
    if not rows:
        print("no open positions")
    for p in rows:
        q = p["quote"]
        px = p.get("mark") or ((market_of(p["coin"], q) or {}).get("last"))
        dist = (abs(p["liq"] / px - 1) * 100) if (p.get("liq") and px) else None
        warn = "  << DANGER: close or add margin" if dist is not None and dist < 10 else ""
        print("%-6s %-5s %-5s %4sx qty %-12s entry %s  now %s  P&L %s %s  liq %s (%s away)  %s%s" % (
            p["id"], p["coin"] + "/" + q, p["side"], "%g" % (p.get("leverage") or 1), "%.8g" % (p.get("qty") or 0),
            fmt(p.get("entry"), q), fmt(px, q), fmt(p.get("upnl", p.get("pnl")), q), unit(q),
            fmt(p.get("liq"), q), "-" if dist is None else "%.1f%%" % dist, p.get("status"), warn))
        if p.get("expires"):
            print("        expires %s, daily fee %s %s" % (p["expires"], fmt(p.get("daily_fee"), q), unit(q)))


def cmd_wallet(args, cfg):
    if cfg["mode"] == "paper":
        w = paper()["wallet"]
    else:
        w = live_wallet()
    print("%s margin wallet: %s toman, %s USDT" % (cfg["mode"].upper(), fmt(w.get("IRT", 0), "IRT"), fmt(w.get("USDT", 0), "USDT")))


def cmd_markets(args, cfg):
    mm = margin_markets(refresh=True)
    print("%-10s %-6s %-6s %8s %10s" % ("market", "long", "short", "max lev", "daily fee"))
    for k, v in sorted(mm["markets"].items()):
        print("%-10s %-6s %-6s %7gx %9.2f%%" % (k, "yes" if v["long"] else "-", "yes" if v["short"] else "-",
                                               v["max_leverage"], (v["daily_fee"] or 0) * 100))
    print("(max leverage depends on your account level when $NOBITEX_TOKEN is set)")


def cmd_transfer(args, cfg):
    amt = float(args.amount)
    if args.confirm is None or float(args.confirm) != amt:
        sys.exit("refused: --confirm must repeat the amount (%s)" % args.amount)
    q = (args.quote or cfg["quote"]).upper()
    src, dst = ("spot", "margin") if args.to == "margin" else ("margin", "spot")
    if cfg["mode"] == "paper":
        book = paper()
        pf = load_json(dpath("portfolio.json"), {"quote": q, "cash": 0, "holdings": {}})
        if args.to == "margin":
            if float(pf.get("cash") or 0) < amt:
                sys.exit("paper spot cash is %s; not enough" % fmt(pf.get("cash"), q))
            pf["cash"] = float(pf["cash"]) - amt
            book["wallet"][q] = book["wallet"].get(q, 0.0) + amt
        else:
            if book["wallet"].get(q, 0.0) < amt:
                sys.exit("paper margin wallet is %s; not enough" % fmt(book["wallet"].get(q, 0), q))
            book["wallet"][q] -= amt
            pf["cash"] = float(pf.get("cash") or 0) + amt
        save_json(dpath("portfolio.json"), pf)
        save_json(dpath("margin_paper.json"), book)
        print("paper: moved %s %s %s -> %s" % (fmt(amt, q), unit(q), src, dst))
        return
    h = need_auth()
    body = {"currency": "rls" if q == "IRT" else "usdt",
            "amount": str(int(round(amt * 10))) if q == "IRT" else str(amt), "src": src, "dst": dst}
    resp = http("POST", BASE + "/wallets/transfer", h, body)
    log_event({"action": "transfer", "body": body, "response_status": resp.get("status"), "mode": "live"})
    if resp.get("status") != "ok":
        sys.exit("Nobitex refused the transfer: %s %s" % (resp.get("code", ""), resp.get("message", "")))
    print("moved %s %s from %s to %s" % (fmt(amt, q), unit(q), src, dst))


def cmd_cancel(args, cfg):
    if args.confirm != args.order_id:
        sys.exit("refused: --confirm must repeat the order id")
    if cfg["mode"] != "live":
        sys.exit("paper orders fill at once; nothing to cancel")
    resp = http("POST", BASE + "/market/orders/update-status", need_auth(), {"order": int(args.order_id), "status": "canceled"})
    log_event({"action": "cancel", "order": args.order_id, "response": resp, "mode": "live"})
    print(json.dumps(resp, ensure_ascii=False)[:600])


def main(argv=None):
    p = argparse.ArgumentParser(description="Leveraged positions on Nobitex")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("markets")
    sub.add_parser("wallet")
    s = sub.add_parser("positions"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("transfer"); s.add_argument("amount"); s.add_argument("--to", choices=["margin", "spot"], required=True)
    s.add_argument("--quote"); s.add_argument("--confirm")
    s = sub.add_parser("open"); s.add_argument("side", choices=["long", "short"]); s.add_argument("coin")
    s.add_argument("--margin", required=True); s.add_argument("--leverage", required=True); s.add_argument("--quote")
    s = sub.add_parser("close"); s.add_argument("position")
    s = sub.add_parser("protect"); s.add_argument("position"); s.add_argument("--stop", required=True)
    sub.add_parser("show")
    s = sub.add_parser("approve"); s.add_argument("id"); s.add_argument("--words", required=True)
    s = sub.add_parser("place"); s.add_argument("id"); s.add_argument("--confirm", required=True)
    s = sub.add_parser("cancel-order"); s.add_argument("order_id"); s.add_argument("--confirm", required=True)
    args = p.parse_args(argv)
    cfg = load_config()
    fn = {"markets": cmd_markets, "wallet": cmd_wallet, "positions": cmd_positions, "transfer": cmd_transfer,
          "open": cmd_open, "close": cmd_close, "protect": cmd_protect, "show": cmd_show, "approve": cmd_approve,
          "place": cmd_place, "cancel-order": cmd_cancel}[args.cmd]
    try:
        fn(args, cfg)
    except RuntimeError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
