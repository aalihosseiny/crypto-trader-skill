#!/usr/bin/env python3
"""crypto-risk: check every proposed order against your limits before it can reach the exchange.

  python3 check.py                 # checks crypto/orders.json, records PASS / BLOCK on each order
  python3 check.py --orders FILE
  python3 check.py limits          # print the limits in force

Checks, in order, for each order:
  malformed order (side, coin, size, quote)    blocked / not-allowed coins
  leverage, margin, futures (off by default)   minimum order (yours and the exchange's)
  maximum order size                           price present and fresh (max_price_age_min)
  bid/ask spread (max_spread_pct)              limit price too far from the market (fat finger)
  sells: you hold enough                       position after the fill <= max_position_pct
  cash buffer kept after all buys and fees             duplicates in one batch
  daily order limit (max_orders_per_day)

Each order gets order["risk"] = {verdict, reasons, hash, checked_at}. The hash
covers the fields that matter; if the order changes afterwards, crypto-broker
sees the mismatch and refuses it until it is checked again.
Exit code: 0 if everything passed, 1 if anything was blocked.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

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
HASH_FIELDS = ("id", "coin", "side", "quote", "exchange", "notional", "qty", "limit_price", "type", "leverage")


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


def order_hash(o):
    core = {k: o.get(k) for k in HASH_FIELDS}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def parse_time(s):
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def fmt(x, quote):
    return "{:,.0f}".format(x) if quote == "IRT" else "{:,.2f}".format(x)


def placed_today(log, now):
    day = now.date()
    n = 0
    for e in (log or {}).get("events", []):
        if e.get("action") == "placed":
            t = parse_time(e.get("at"))
            if t and t.date() == day:
                n += 1
    return n


def check(doc, cfg, prices, portfolio, log, now=None):
    """Annotate doc["orders"] in place. Returns (n_pass, n_block)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    markets = (prices or {}).get("markets") or {}
    pf = portfolio or {}
    holdings = {k.upper(): float(v) for k, v in (pf.get("holdings") or {}).items()}
    orders = doc.get("orders") or []
    quote = (doc.get("quote") or cfg["quote"]).upper()

    def price_of(coin):
        m = markets.get(coin + quote)
        return m.get("last") if m else None

    hold_val = sum(q * (price_of(c) or 0) for c, q in holdings.items())
    cash = float(pf.get("cash") or 0)
    if doc.get("budget") and pf.get("quote", quote) == quote:
        cash = max(cash, float(doc["budget"]))  # new money that the budget run assumes
    equity = cash + hold_val
    buffer_need = equity * cfg["cash_buffer_pct"] / 100.0
    cap = cfg["max_position_pct"]
    blocked = {c.upper() for c in cfg.get("blocked") or []}
    allowed = {c.upper() for c in cfg["allowed"]} if cfg.get("allowed") else None
    room_today = cfg["max_orders_per_day"] - placed_today(log, now)
    seen = set()
    n_pass = n_block = 0
    # sells first: they free the cash that the buys use
    for o in sorted(orders, key=lambda o: 0 if o.get("side") == "sell" else 1):
        reasons = []
        coin = str(o.get("coin") or "").upper()
        side = o.get("side")
        q = (o.get("quote") or quote).upper()
        m = markets.get(coin + q)
        px = (m or {}).get("last")
        if side not in ("buy", "sell"):
            reasons.append("side must be buy or sell")
        if not coin:
            reasons.append("no coin")
        if q != quote:
            reasons.append("quote %s differs from the batch quote %s" % (q, quote))
        notional = o.get("notional")
        if notional is None and o.get("qty") and px:
            notional = float(o["qty"]) * px
        if not notional or notional <= 0:
            reasons.append("no size (notional or qty)")
            notional = 0.0
        if coin in blocked:
            reasons.append("%s is on your blocked list" % coin)
        if allowed is not None and coin not in allowed:
            reasons.append("%s is not on your allowed list" % coin)
        if (o.get("leverage") or o.get("margin") or o.get("futures")) and not cfg["allow_leverage"]:
            reasons.append("leverage / margin / futures are off (allow_leverage=false)")
        min_o = max(cfg["min_order"].get(q, 0), (m or {}).get("min_notional") or 0)
        if notional and notional < min_o:
            reasons.append("%s is under the minimum order %s" % (fmt(notional, q), fmt(min_o, q)))
        max_o = cfg["max_order"].get(q)
        if max_o and notional > max_o:
            reasons.append("%s is over your max order %s" % (fmt(notional, q), fmt(max_o, q)))
        if not m or not px:
            reasons.append("no price for %s%s: run crypto-alerts prices" % (coin, q))
        else:
            t = parse_time(m.get("updated") or prices.get("updated"))
            age = (now - t).total_seconds() / 60 if t else None
            if age is None or age > cfg["max_price_age_min"]:
                reasons.append("price is stale (%s min old, max %s): refresh prices" % (
                    "?" if age is None else "%.0f" % age, cfg["max_price_age_min"]))
            bid, ask = m.get("bid"), m.get("ask")
            if bid and ask and ask >= bid:
                spread = (ask - bid) / ((ask + bid) / 2) * 100
                if spread > cfg["max_spread_pct"]:
                    reasons.append("spread %.2f%% is over %.2f%% (thin market)" % (spread, cfg["max_spread_pct"]))
            lp = o.get("limit_price")
            if lp:
                dev = abs(lp / px - 1) * 100
                if dev > cfg["max_limit_deviation_pct"]:
                    reasons.append("limit price is %.1f%% away from the market (max %s%%)" % (dev, cfg["max_limit_deviation_pct"]))
        key = (coin, side)
        if key in seen:
            reasons.append("duplicate %s %s in this batch" % (side, coin))
        seen.add(key)
        if side == "sell":
            held = holdings.get(coin, 0.0)
            qty = float(o.get("qty") or (notional / px if px else 0))
            if qty > held * 1.000001:
                reasons.append("selling %.8g %s but you hold %.8g" % (qty, coin, held))
        if side == "buy" and px:
            after = holdings.get(coin, 0.0) * px + notional
            if equity > 0 and after / equity * 100 > cap + 1e-6:
                reasons.append("%s would be %.1f%% of the portfolio (cap %s%%)" % (coin, after / equity * 100, cap))
            spend = notional * (1 + cfg["fee_pct"] / 100.0)
            if cash - spend < buffer_need - 1e-6:
                reasons.append("would leave %s cash after the fee, under the %s%% buffer (%s)" % (
                    fmt(cash - spend, q), cfg["cash_buffer_pct"], fmt(buffer_need, q)))
        if not reasons and room_today <= 0:
            reasons.append("daily limit of %d orders reached" % cfg["max_orders_per_day"])
        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            n_pass += 1
            room_today -= 1
            if side == "buy":
                cash -= notional * (1 + cfg["fee_pct"] / 100.0)
                holdings[coin] = holdings.get(coin, 0.0) + (notional / px if px else 0)
            else:
                cash += notional * (1 - cfg["fee_pct"] / 100.0)
                holdings[coin] = holdings.get(coin, 0.0) - float(o.get("qty") or (notional / px if px else 0))
        else:
            n_block += 1
        o["risk"] = {"verdict": verdict, "reasons": reasons, "hash": order_hash(o),
                     "checked_at": now.replace(microsecond=0).isoformat()}
    return n_pass, n_block


def main(argv=None):
    p = argparse.ArgumentParser(description="Risk check for crypto orders")
    p.add_argument("action", nargs="?", default="check", choices=["check", "limits"])
    p.add_argument("--orders")
    args = p.parse_args(argv)
    cfg = load_config()
    if args.action == "limits":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return
    path = args.orders or dpath("orders.json")
    doc = load_json(path)
    if not doc or not doc.get("orders"):
        sys.exit("error: no orders in %s (run crypto-budget or crypto-rebalance with --write)" % path)
    n_pass, n_block = check(doc, cfg, load_json(dpath("prices.json"), {}),
                            load_json(dpath("portfolio.json"), {}), load_json(dpath("broker_log.json"), {}))
    save_json(path, doc)
    q = (doc.get("quote") or cfg["quote"]).upper()
    for o in doc["orders"]:
        r = o["risk"]
        size = fmt(o["notional"], q) if o.get("notional") else ("%.8g %s" % (o.get("qty") or 0, o.get("coin")))
        print("%-5s %-4s %-4s %-6s %18s %s" % (r["verdict"], o.get("id"), o.get("side"), o.get("coin"), size,
                                              ("  " + "; ".join(r["reasons"])) if r["reasons"] else ""))
    print("%d passed, %d blocked (mode: %s) -> %s" % (n_pass, n_block, cfg["mode"],
                                                     "next: crypto-broker" if n_pass else "fix and check again"))
    sys.exit(1 if n_block else 0)


if __name__ == "__main__":
    main()
