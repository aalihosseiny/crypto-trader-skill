#!/usr/bin/env python3
"""crypto-broker: turn checked orders into Nobitex / Wallex orders, one at a time, only after your yes.

  python3 broker.py show                         # every order as a ticket: READY or why not
  python3 broker.py approve o1 --words "yes, buy o1"   # records your exact words + freezes the ticket
  python3 broker.py place o1 --confirm o1        # sends it (paper by default)
  python3 broker.py status o1 | cancel o1        # live / testnet orders
  python3 broker.py balances                     # live: read your wallet into crypto/portfolio.json
  python3 broker.py paper-init --cash 50000000   # start a paper account with this much cash
  python3 broker.py mode                         # which mode, which exchange, which key is set

Modes (crypto/config.json "mode"):
  paper    default. Fills are simulated against crypto/prices.json and written to
           crypto/portfolio.json and crypto/fills.json. Nothing leaves the computer.
  testnet  Nobitex test exchange (testnetapiv2.nobitex.ir) with $NOBITEX_TESTNET_TOKEN.
  live     real money. Needs "mode": "live" in config AND $NOBITEX_TOKEN or $WALLEX_API_KEY.

Safety chain, every check must pass for `place`:
  1. crypto-risk gave this order PASS, and the order has not changed since (hash match)
  2. you approved this exact order: `approve` stored your words and froze the ticket
  3. the approval is younger than approval_ttl_min (default 10 minutes)
  4. --confirm repeats the order id
  5. the order was not placed before (same clientOrderId is never sent twice)
Every order is a LIMIT order with a price guard (ask + slippage for buys, bid -
slippage for sells), never an unbounded market order. Keys come only from
environment variables; this tool never reads or prints them from chat.

Units: toman markets are kept in toman; Nobitex wants RIAL, so prices are x10
in the Nobitex payload. Wallex TMN markets take toman as is.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import urllib.error
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
}
HASH_FIELDS = ("id", "coin", "side", "quote", "exchange", "notional", "qty", "limit_price", "type", "leverage")
BASES = {
    ("nobitex", "live"): os.environ.get("NOBITEX_BASE", "https://apiv2.nobitex.ir"),
    ("nobitex", "testnet"): os.environ.get("NOBITEX_TESTNET_BASE", "https://testnetapiv2.nobitex.ir"),
    ("wallex", "live"): os.environ.get("WALLEX_BASE", "https://api.wallex.ir"),
}
UA = "crypto-trader-skill/1.0"


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


def order_hash(o):
    core = {k: o.get(k) for k in HASH_FIELDS}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def fmt(x, quote):
    if x is None:
        return "-"
    return "{:,.0f}".format(x) if quote == "IRT" else "{:,.4f}".format(x)


def log_event(ev):
    log = load_json(dpath("broker_log.json"), {"events": []}) or {"events": []}
    ev = dict(ev, at=now_utc().isoformat())
    log["events"].append(ev)
    save_json(dpath("broker_log.json"), log)


def http(method, url, headers=None, body=None, timeout=30):
    """One HTTP call, no retries (a retried POST could double an order). Tests replace this."""
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


# ---------------------------------------------------------------- tickets
def qty_decimals(coin, price, quote, cfg, market):
    over = (cfg.get("qty_decimals") or {}).get(coin)
    if over is not None:
        return int(over)
    if market and market.get("qty_step"):
        return max(0, int(round(-math.log10(market["qty_step"]))))
    usd = price if quote == "USDT" else price / max(((load_json(dpath("prices.json"), {}) or {}).get("usdt_irt") or 100000), 1)
    if usd >= 1000:
        return 6
    if usd >= 10:
        return 4
    if usd >= 0.1:
        return 2
    return 0


def floor_to(x, decimals):
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def build_ticket(o, cfg, prices):
    """-> (ticket dict, problem or None). The ticket holds the exact exchange payload."""
    q = (o.get("quote") or cfg["quote"]).upper()
    coin = o["coin"].upper()
    ex = (o.get("exchange") or cfg["exchange"]).lower()
    m = ((prices or {}).get("markets") or {}).get(coin + q)
    if not m:
        return None, "no price for %s%s" % (coin, q)
    slip = cfg["slippage_pct"] / 100.0
    if o["side"] == "buy":
        ref = m.get("ask") or m.get("last")
        limit = o.get("limit_price") or ref * (1 + slip)
    else:
        ref = m.get("bid") or m.get("last")
        limit = o.get("limit_price") or ref * (1 - slip)
    limit = round(limit, 4)  # strip float noise (7005000000 * 1.005 = 7040024999.999...)
    limit = math.floor(limit) if (q == "IRT" and o["side"] == "buy") else (math.ceil(limit) if q == "IRT" else limit)
    if q == "USDT":
        pd = 2 if limit >= 10 else 4 if limit >= 0.1 else 8
        limit = floor_to(limit, pd) if o["side"] == "buy" else math.ceil(limit * 10 ** pd) / 10 ** pd
    dec = qty_decimals(coin, limit, q, cfg, m)
    if o.get("qty"):
        qty = floor_to(float(o["qty"]), dec)
    else:
        qty = floor_to(float(o["notional"]) / limit, dec)
    if qty <= 0:
        return None, "quantity rounds to zero at %d decimals" % dec
    cid = ("cts%s%s" % (o["id"], order_hash(o)))[:32]
    qty_s = ("{:.%df}" % dec).format(qty)
    if ex == "nobitex":
        price_out = limit * 10 if q == "IRT" else limit
        price_s = str(int(round(price_out))) if q == "IRT" else repr(price_out)
        payload = {"type": o["side"], "execution": "limit", "srcCurrency": coin.lower(),
                   "dstCurrency": "rls" if q == "IRT" else "usdt", "amount": qty_s, "price": price_s,
                   "clientOrderId": cid}
        path = "/market/orders/add"
    elif ex == "wallex":
        price_s = str(int(limit)) if q == "IRT" else repr(limit)
        payload = {"symbol": coin + ("TMN" if q == "IRT" else "USDT"), "type": "LIMIT", "side": o["side"].upper(),
                   "price": price_s, "quantity": qty_s, "clientOrderId": cid}
        path = "/v1/account/orders"
    else:
        return None, "unknown exchange %s" % ex
    t = {"order_id": o["id"], "order_hash": order_hash(o), "exchange": ex, "coin": coin, "quote": q,
         "side": o["side"], "qty": qty, "limit": limit, "value": qty * limit, "client_order_id": cid,
         "path": path, "payload": payload, "built_at": now_utc().isoformat()}
    t["ticket_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return t, None


def readiness(o):
    r = o.get("risk")
    if not r:
        return "NOT CHECKED: run crypto-risk first"
    if r.get("verdict") != "PASS":
        return "BLOCKED by risk: " + "; ".join(r.get("reasons") or [])
    if r.get("hash") != order_hash(o):
        return "CHANGED after the risk check: run crypto-risk again"
    return None


def okey(o):
    """Identity of one order: its id plus its content hash. A new batch can reuse 'o1'
    without colliding with an older o1 that was already placed."""
    return "%s:%s" % (o["id"], order_hash(o))


def placed_ids(log=None):
    """{order key: event} for orders that were placed OR are in flight (a 'sending'
    with no answer yet). An in-flight order is never sent again: check it first."""
    log = log or load_json(dpath("broker_log.json"), {"events": []}) or {"events": []}
    state = {}
    for e in log["events"]:
        k = e.get("key") or e.get("order_id")
        if e.get("action") == "sending":
            state[k] = dict(e, summary="IN FLIGHT since %s: no answer was recorded; check the exchange "
                                       "(broker.py status %s) before doing anything else" % (e.get("at"), e.get("order_id")))
        elif e.get("action") == "placed":
            state[k] = e
        elif e.get("action") == "rejected" and state.get(k, {}).get("action") == "sending":
            del state[k]
    return state


def latest_sent(oid):
    """The most recent placed / in-flight event for an order id (for status and cancel)."""
    found = None
    for k, e in placed_ids().items():
        if e.get("order_id") == oid and (found is None or e.get("at", "") >= found.get("at", "")):
            found = e
    return found


def describe(t):
    unit = "toman" if t["quote"] == "IRT" else "USDT"
    return "%s %s %s, price %s %s %s (about %s %s), LIMIT on %s" % (
        t["side"].upper(), ("{:.8f}".format(t["qty"])).rstrip("0").rstrip("."), t["coin"],
        "at most" if t["side"] == "buy" else "at least", fmt(t["limit"], t["quote"]), unit,
        fmt(t["value"], t["quote"]), unit, t["exchange"])


# ---------------------------------------------------------------- commands
def get_order(doc, oid):
    for o in (doc or {}).get("orders", []):
        if o.get("id") == oid:
            return o
    sys.exit("no order %s in %s" % (oid, dpath("orders.json")))


def cmd_mode(args, cfg):
    ex = cfg["exchange"]
    print("mode: %s   exchange: %s   quote: %s" % (cfg["mode"], ex, cfg["quote"]))
    for var in ("NOBITEX_TOKEN", "NOBITEX_TESTNET_TOKEN", "WALLEX_API_KEY"):
        print("  %-22s %s" % (var, "set" if os.environ.get(var) else "not set"))
    if cfg["mode"] == "paper":
        pf = load_json(dpath("portfolio.json"), {}) or {}
        print("paper cash: %s" % fmt(pf.get("cash"), cfg["quote"]))


def cmd_show(args, cfg):
    doc = load_json(dpath("orders.json"))
    if not doc or not doc.get("orders"):
        sys.exit("no orders (run crypto-budget or crypto-rebalance with --write, then crypto-risk)")
    prices = load_json(dpath("prices.json"), {})
    done = placed_ids()
    approvals = (load_json(dpath("approvals.json"), {}) or {}).get("approvals", {})
    print("mode: %s   (orders from %s, %s)" % (cfg["mode"].upper(), doc.get("source"), doc.get("created")))
    for o in doc["orders"]:
        if okey(o) in done:
            print("%-4s PLACED %s" % (o["id"], done[okey(o)].get("summary", "")))
            continue
        why = readiness(o)
        t, prob = build_ticket(o, cfg, prices)
        state = why or prob or ("APPROVED" if o["id"] in approvals else "READY")
        print("%-4s %-9s %s" % (o["id"], state.split(":")[0], describe(t) if t else ""))
        if why or prob:
            print("      " + (why or prob))
        elif args.json:
            print("      " + json.dumps(t["payload"], ensure_ascii=False))


def cmd_approve(args, cfg):
    if not args.words or not args.words.strip():
        sys.exit("approve needs --words with the user's own reply, word for word")
    doc = load_json(dpath("orders.json"))
    o = get_order(doc, args.id)
    if okey(o) in placed_ids():
        sys.exit("%s was already placed" % o["id"])
    why = readiness(o)
    if why:
        sys.exit("cannot approve %s: %s" % (o["id"], why))
    prices = load_json(dpath("prices.json"), {}) or {}
    m = (prices.get("markets") or {}).get(o["coin"].upper() + (o.get("quote") or cfg["quote"]).upper()) or {}
    t_upd = parse_time(m.get("updated"))
    if not t_upd or (now_utc() - t_upd).total_seconds() / 60 > cfg["max_price_age_min"]:
        sys.exit("cannot approve %s: price is older than %d minutes; refresh prices and show the ticket again"
                 % (o["id"], cfg["max_price_age_min"]))
    t, prob = build_ticket(o, cfg, prices)
    if prob:
        sys.exit("cannot approve %s: %s" % (o["id"], prob))
    ap = load_json(dpath("approvals.json"), {"approvals": {}}) or {"approvals": {}}
    ap["approvals"][o["id"]] = {"ticket": t, "words": args.words.strip(), "at": now_utc().isoformat(), "mode": cfg["mode"]}
    save_json(dpath("approvals.json"), ap)
    log_event({"action": "approved", "order_id": o["id"], "key": okey(o), "words": args.words.strip(), "mode": cfg["mode"],
               "ticket_hash": t["ticket_hash"]})
    print("approved %s: %s" % (o["id"], describe(t)))
    print("valid for %d minutes -> place with: broker.py place %s --confirm %s" % (cfg["approval_ttl_min"], o["id"], o["id"]))


def credentials(ex, mode):
    if ex == "nobitex":
        var = "NOBITEX_TESTNET_TOKEN" if mode == "testnet" else "NOBITEX_TOKEN"
        tok = os.environ.get(var)
        return var, ({"Authorization": "Token " + tok} if tok else None)
    var = "WALLEX_API_KEY"
    tok = os.environ.get(var)
    return var, ({"X-API-Key": tok} if tok else None)


def paper_fill(t, cfg, prices):
    m = ((prices or {}).get("markets") or {}).get(t["coin"] + t["quote"]) or {}
    pf = load_json(dpath("portfolio.json"), {"quote": t["quote"], "cash": 0, "holdings": {}})
    pf.setdefault("holdings", {})
    fee_rate = cfg["fee_pct"] / 100.0
    if t["side"] == "buy":
        ask = m.get("ask") or m.get("last")
        if ask and t["limit"] < ask:
            return None, "paper: limit %s is under the ask %s, the order would rest unfilled" % (fmt(t["limit"], t["quote"]), fmt(ask, t["quote"]))
        px = min(t["limit"], ask) if ask else t["limit"]
        cost = t["qty"] * px
        fee = cost * fee_rate
        if cost + fee > float(pf.get("cash") or 0) + 1e-9:
            return None, "paper: not enough cash (%s needed, %s available); run paper-init" % (
                fmt(cost + fee, t["quote"]), fmt(pf.get("cash"), t["quote"]))
        pf["cash"] = float(pf.get("cash") or 0) - cost - fee
        pf["holdings"][t["coin"]] = float(pf["holdings"].get(t["coin"], 0)) + t["qty"]
    else:
        bid = m.get("bid") or m.get("last")
        if bid and t["limit"] > bid:
            return None, "paper: limit %s is over the bid %s, the order would rest unfilled" % (fmt(t["limit"], t["quote"]), fmt(bid, t["quote"]))
        held = float(pf["holdings"].get(t["coin"], 0))
        if t["qty"] > held + 1e-12:
            return None, "paper: you hold %.8g %s" % (held, t["coin"])
        px = max(t["limit"], bid) if bid else t["limit"]
        proceeds = t["qty"] * px
        fee = proceeds * fee_rate
        pf["cash"] = float(pf.get("cash") or 0) + proceeds - fee
        pf["holdings"][t["coin"]] = held - t["qty"]
    pf["quote"] = t["quote"]
    pf["updated"] = now_utc().isoformat()
    save_json(dpath("portfolio.json"), pf)
    fills = load_json(dpath("fills.json"), {"fills": []}) or {"fills": []}
    fill = {"id": t["client_order_id"], "at": now_utc().isoformat(), "coin": t["coin"], "side": t["side"],
            "qty": t["qty"], "price": px, "fee": fee, "quote": t["quote"], "source": "paper"}
    fills["fills"].append(fill)
    save_json(dpath("fills.json"), fills)
    return fill, None


def cmd_place(args, cfg):
    if args.confirm != args.id:
        sys.exit("refused: --confirm must repeat the order id (%s)" % args.id)
    doc = load_json(dpath("orders.json"))
    o = get_order(doc, args.id)
    done = placed_ids()
    if okey(o) in done:
        ev = done[okey(o)]
        if ev.get("action") == "sending":
            sys.exit("refused: %s is %s" % (o["id"], ev["summary"]))
        sys.exit("refused: %s was already placed (%s)" % (o["id"], ev.get("at")))
    why = readiness(o)
    if why:
        sys.exit("refused: %s" % why)
    ap = ((load_json(dpath("approvals.json"), {}) or {}).get("approvals") or {}).get(o["id"])
    if not ap:
        sys.exit("refused: %s has no recorded approval; show the ticket, get the user's yes, then approve" % o["id"])
    t = ap["ticket"]
    if t["order_hash"] != order_hash(o):
        sys.exit("refused: the order changed after it was approved; check and approve again")
    age = (now_utc() - parse_time(ap["at"])).total_seconds() / 60
    if age > cfg["approval_ttl_min"]:
        sys.exit("refused: the approval is %.0f minutes old (max %d); prices move, approve again" % (age, cfg["approval_ttl_min"]))
    if ap.get("mode") != cfg["mode"]:
        sys.exit("refused: approved in %s mode but config is now %s; approve again" % (ap.get("mode"), cfg["mode"]))
    mode = cfg["mode"]
    if mode == "paper":
        fill, prob = paper_fill(t, cfg, load_json(dpath("prices.json"), {}))
        if prob:
            log_event({"action": "rejected", "order_id": o["id"], "key": okey(o), "mode": mode, "reason": prob})
            sys.exit(prob)
        summary = "paper fill: %s %.8g %s at %s, fee %s" % (t["side"], fill["qty"], t["coin"], fmt(fill["price"], t["quote"]), fmt(fill["fee"], t["quote"]))
        log_event({"action": "placed", "order_id": o["id"], "key": okey(o), "mode": mode, "ticket": t, "summary": summary})
        o["status"] = "filled (paper)"
        save_json(dpath("orders.json"), doc)
        print(summary)
        return
    if mode not in ("live", "testnet"):
        sys.exit("refused: unknown mode %r (paper, testnet or live)" % mode)
    if mode == "testnet" and t["exchange"] != "nobitex":
        sys.exit("refused: only Nobitex has a testnet")
    var, headers = credentials(t["exchange"], mode)
    if not headers:
        sys.exit("refused: $%s is not set. Set it in your shell, never paste keys in the chat." % var)
    base = BASES[(t["exchange"], mode)]
    # the log entry is written BEFORE the call so a crash mid-request can never lead to a second send
    log_event({"action": "sending", "order_id": o["id"], "key": okey(o), "mode": mode, "ticket": t})
    resp = http("POST", base + t["path"], headers, t["payload"])
    ok = resp.get("status") == "ok" if t["exchange"] == "nobitex" else bool(resp.get("success"))
    if not ok:
        log_event({"action": "rejected", "order_id": o["id"], "key": okey(o), "mode": mode, "response": resp})
        sys.exit("exchange refused the order: %s" % json.dumps(resp, ensure_ascii=False)[:500])
    ex_order = resp.get("order") or resp.get("result") or {}
    summary = "%s %s: %s (exchange id %s, status %s)" % (mode, t["exchange"], describe(t), ex_order.get("id", ex_order.get("clientOrderId")),
                                                     ex_order.get("status"))
    log_event({"action": "placed", "order_id": o["id"], "key": okey(o), "mode": mode, "ticket": t, "response": resp,
               "summary": summary})
    o["status"] = "sent (%s)" % mode
    save_json(dpath("orders.json"), doc)
    print(summary)
    print("check with: broker.py status %s" % o["id"])


def cmd_status(args, cfg, cancel=False):
    ev = latest_sent(args.id)
    if not ev:
        sys.exit("%s was not placed" % args.id)
    if ev.get("mode") == "paper" and cancel:
        sys.exit("paper fills are immediate; nothing to cancel")
    if ev["mode"] == "paper":
        print(ev.get("summary"))
        return
    t = ev["ticket"]
    var, headers = credentials(t["exchange"], ev["mode"])
    if not headers:
        sys.exit("$%s is not set" % var)
    base = BASES[(t["exchange"], ev["mode"])]
    if t["exchange"] == "nobitex":
        if cancel:
            resp = http("POST", base + "/market/orders/update-status", headers,
                        {"clientOrderId": t["client_order_id"], "status": "canceled"})
        else:
            resp = http("POST", base + "/market/orders/status", headers, {"clientOrderId": t["client_order_id"]})
    else:
        if cancel:
            resp = http("DELETE", base + "/v1/account/orders?clientOrderId=" + t["client_order_id"], headers)
        else:
            resp = http("GET", base + "/v1/account/orders/" + t["client_order_id"], headers)
    if cancel:
        log_event({"action": "cancel", "order_id": args.id, "mode": ev["mode"], "response": resp})
    print(json.dumps(resp, ensure_ascii=False, indent=2)[:3000])


def cmd_balances(args, cfg):
    mode = cfg["mode"]
    if mode == "paper":
        print(json.dumps(load_json(dpath("portfolio.json"), {}), ensure_ascii=False, indent=2))
        return
    ex = cfg["exchange"]
    var, headers = credentials(ex, mode)
    if not headers:
        sys.exit("$%s is not set" % var)
    base = BASES[(ex, mode)]
    holdings, cash, usdt = {}, 0.0, 0.0
    if ex == "nobitex":
        resp = http("GET", base + "/v2/wallets", headers)
        if resp.get("status") != "ok":
            sys.exit("nobitex: %s" % resp)
        for cur, w in (resp.get("wallets") or {}).items():
            bal = float(w.get("balance") or 0)
            if cur.upper() == "RLS":
                cash = bal / 10.0
            elif bal > 0:
                holdings[cur.upper()] = bal
    else:
        resp = http("GET", base + "/v1/account/balances", headers)
        if not resp.get("success"):
            sys.exit("wallex: %s" % resp)
        for cur, w in ((resp.get("result") or {}).get("balances") or {}).items():
            bal = float(w.get("value") or 0)
            if cur.upper() == "TMN":
                cash = bal
            elif bal > 0:
                holdings[cur.upper()] = bal
    if cfg["quote"] == "USDT":
        usdt = holdings.pop("USDT", 0.0)
        cash_out = usdt
    else:
        cash_out = cash
    pf = {"quote": cfg["quote"], "cash": cash_out, "holdings": holdings, "source": "%s %s" % (ex, mode),
          "updated": now_utc().isoformat()}
    save_json(dpath("portfolio.json"), pf)
    print("cash %s %s; %d coins -> %s" % (fmt(cash_out, cfg["quote"]), cfg["quote"], len(holdings), dpath("portfolio.json")))


def cmd_paper_init(args, cfg):
    if cfg["mode"] != "paper":
        sys.exit("paper-init only works in paper mode")
    pf = load_json(dpath("portfolio.json"))
    if pf and pf.get("holdings") and any(v > 0 for v in pf["holdings"].values()) and not args.reset:
        sys.exit("a paper portfolio with holdings exists; add --reset to wipe it")
    save_json(dpath("portfolio.json"), {"quote": (args.quote or cfg["quote"]).upper(), "cash": args.cash,
                                        "holdings": {}, "source": "paper", "updated": now_utc().isoformat()})
    if args.reset:
        save_json(dpath("fills.json"), {"fills": []})
    print("paper account: %s %s cash" % (fmt(args.cash, (args.quote or cfg["quote"]).upper()), (args.quote or cfg["quote"]).upper()))


def main(argv=None):
    p = argparse.ArgumentParser(description="Place approved crypto orders on Nobitex / Wallex")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mode")
    s = sub.add_parser("show"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("approve"); s.add_argument("id"); s.add_argument("--words", required=True)
    s = sub.add_parser("place"); s.add_argument("id"); s.add_argument("--confirm", required=True)
    s = sub.add_parser("status"); s.add_argument("id")
    s = sub.add_parser("cancel"); s.add_argument("id")
    sub.add_parser("balances")
    s = sub.add_parser("paper-init"); s.add_argument("--cash", type=float, required=True)
    s.add_argument("--quote"); s.add_argument("--reset", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    fn = {"mode": cmd_mode, "show": cmd_show, "approve": cmd_approve, "place": cmd_place,
          "balances": cmd_balances, "paper-init": cmd_paper_init}.get(args.cmd)
    if args.cmd == "status":
        cmd_status(args, cfg)
    elif args.cmd == "cancel":
        cmd_status(args, cfg, cancel=True)
    else:
        fn(args, cfg)


if __name__ == "__main__":
    main()
