#!/usr/bin/env python3
"""crypto-budget: size every buy to your budget before anything is bought.

  python3 size.py --budget 50000000                     # 50 million toman, rules from crypto/config.json
  python3 size.py --budget 50000000 --cash-buffer 15 --cap 30 --write
  python3 size.py --budget 1000 --quote USDT --write
  python3 size.py rules                                 # show the rules in force

Reads crypto/targets.json (weights from crypto-whales or crypto-ta, or written by
hand) and crypto/prices.json (crypto-alerts). Writes crypto/orders.json with
--write. Nothing is placed: these are proposals for crypto-risk and crypto-broker.

Method
  1. keep a cash buffer and room for the fee: investable = budget x (1 - buffer) / (1 + fee)
  2. split investable by target weight
  3. cap any coin at cap % of the budget; the excess is shared out to the others
     in proportion to their weights (water-filling); if every coin is capped the
     rest stays in cash
  4. drop any slice under the minimum order (exchange minimum or yours, whichever
     is larger) and share it out again
  5. round each slice down to whole toman (or 0.01 USDT); the remainder is cash
"""
import argparse
import datetime as dt
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
}


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
    user = load_json(dpath("config.json"), {}) or {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def fmt(x, quote):
    return "{:,.0f}".format(x) if quote == "IRT" else "{:,.2f}".format(x)


# ---------------------------------------------------------------- sizing
def cap_fill(weights, amount, cap):
    """Split `amount` by `weights` with no slice above `cap` (water-filling)."""
    alloc = {k: 0.0 for k in weights}
    free = {k: w for k, w in weights.items() if w > 0}
    left = amount
    while free and left > 1e-9:
        tot = sum(free.values())
        over = {}
        for k, w in free.items():
            share = left * w / tot
            if alloc[k] + share > cap:
                over[k] = cap - alloc[k]
        if not over:
            for k, w in free.items():
                alloc[k] += left * w / tot
            left = 0.0
            break
        for k, room in over.items():
            alloc[k] += room
            left -= room
            del free[k]
    return alloc, left


def size(targets, budget, buffer_pct, cap_pct, min_order, quote, fee_pct=0.0):
    """-> (allocations {coin: amount}, cash, notes)"""
    weights = {}
    for t in targets:
        weights[t["coin"].upper()] = weights.get(t["coin"].upper(), 0.0) + float(t["weight"])
    # the buffer must survive the trading fee too, so fees come out of the investable part
    investable = budget * (1 - buffer_pct / 100.0) / (1 + fee_pct / 100.0)
    cap = budget * cap_pct / 100.0
    notes = []
    while True:
        alloc, idle = cap_fill(weights, investable, cap)
        small = [k for k, v in alloc.items() if 0 < v < min_order]
        if not small:
            break
        drop = min(small, key=lambda k: alloc[k])
        notes.append("%s dropped: %s is under the minimum order %s" % (drop, fmt(alloc[drop], quote), fmt(min_order, quote)))
        del weights[drop]
        if not weights:
            alloc, idle = {}, investable
            break
    unit = 1.0 if quote == "IRT" else 0.01
    rounded = {k: int(v / unit) * unit for k, v in alloc.items() if v > 0}
    # hand the rounding crumbs to the largest slice that still has room under the cap
    crumbs = investable - idle - sum(rounded.values())
    for k in sorted(rounded, key=lambda k: -rounded[k]):
        room = cap - rounded[k]
        give = int(min(crumbs, room) / unit) * unit
        if give > 0:
            rounded[k] += give
            crumbs -= give
        if crumbs < unit:
            break
    if idle > unit:
        notes.append("%s left idle: every coin hit the %.0f%% cap" % (fmt(idle, quote), cap_pct))
    cash = budget - sum(rounded.values())
    return rounded, cash, notes


def build_orders(alloc, prices, quote, exchange):
    orders, missing = [], []
    n = 0
    for coin in sorted(alloc, key=lambda c: -alloc[c]):
        m = (prices.get("markets") or {}).get(coin + quote)
        if not m:
            missing.append(coin)
            continue
        ref = m.get("ask") or m.get("last")
        n += 1
        orders.append({"id": "o%d" % n, "coin": coin, "side": "buy", "quote": quote, "exchange": exchange,
                       "notional": alloc[coin], "ref_price": ref,
                       "est_qty": alloc[coin] / ref if ref else None,
                       "type": "limit", "status": "proposed"})
    return orders, missing


def main(argv=None):
    p = argparse.ArgumentParser(description="Size crypto buys to a budget")
    p.add_argument("action", nargs="?", default="size", choices=["size", "rules"])
    p.add_argument("--budget", type=float)
    p.add_argument("--quote")
    p.add_argument("--cash-buffer", type=float)
    p.add_argument("--cap", type=float)
    p.add_argument("--min-order", type=float)
    p.add_argument("--targets", default=None)
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    quote = (args.quote or cfg["quote"]).upper()
    if args.action == "rules":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        print("(edit %s to change them)" % dpath("config.json"))
        return
    if not args.budget or args.budget <= 0:
        sys.exit("error: --budget is required (in %s)" % ("toman" if quote == "IRT" else "USDT"))
    tdoc = load_json(args.targets or dpath("targets.json"))
    if not tdoc or not tdoc.get("targets"):
        sys.exit("error: no targets. Run crypto-whales copy, crypto-ta scan --targets, or write crypto/targets.json")
    buffer_pct = cfg["cash_buffer_pct"] if args.cash_buffer is None else args.cash_buffer
    cap_pct = cfg["max_position_pct"] if args.cap is None else args.cap
    min_order = cfg["min_order"].get(quote, 0) if args.min_order is None else args.min_order
    warn = []
    if cap_pct > cfg["max_position_pct"]:
        warn.append("--cap %.0f%% is above max_position_pct %s%% in config.json: crypto-risk will block the bigger "
                    "slices. Change the config if the user wants the higher cap." % (cap_pct, cfg["max_position_pct"]))
    if buffer_pct < cfg["cash_buffer_pct"]:
        warn.append("--cash-buffer %.0f%% is below cash_buffer_pct %s%% in config.json: crypto-risk will block "
                    "the last buys." % (buffer_pct, cfg["cash_buffer_pct"]))
    prices = load_json(dpath("prices.json"), {}) or {}
    # the exchange minimum can be higher than the user's
    for t in tdoc["targets"]:
        m = (prices.get("markets") or {}).get(t["coin"].upper() + quote) or {}
        if m.get("min_notional") and m["min_notional"] > min_order:
            min_order = m["min_notional"]
    alloc, cash, notes = size(tdoc["targets"], args.budget, buffer_pct, cap_pct, min_order, quote, cfg["fee_pct"])
    orders, missing = build_orders(alloc, prices, quote, cfg["exchange"])
    unit = "toman" if quote == "IRT" else "USDT"
    print("budget %s %s   cash buffer %.0f%%   cap %.0f%% per coin   min order %s" % (
        fmt(args.budget, quote), unit, buffer_pct, cap_pct, fmt(min_order, quote)))
    print("source: %s" % tdoc.get("source", "targets.json"))
    print("%-4s %-7s %8s %20s %22s" % ("id", "coin", "weight", "amount (%s)" % unit, "~qty at ask"))
    tw = {t["coin"].upper(): t["weight"] for t in tdoc["targets"]}
    for o in orders:
        print("%-4s %-7s %7.2f%% %20s %22s" % (o["id"], o["coin"], tw.get(o["coin"], 0) * 100, fmt(o["notional"], quote),
                                              "%.8g" % o["est_qty"] if o["est_qty"] else "-"))
    print("%-21s %20s   (incl. ~%s for the %.2f%% fee)" % ("cash kept", fmt(cash, quote),
                                                       fmt(sum(alloc.values()) * cfg["fee_pct"] / 100, quote), cfg["fee_pct"]))
    for n in notes:
        print("note: " + n)
    for w in warn:
        print("WARNING: " + w)
    if missing:
        print("no price for %s: run crypto-alerts prices %s%s first (not included)" % (
            ", ".join(missing), " ".join(missing), " --quote USDT" if quote == "USDT" else ""))
    if args.write:
        save_json(dpath("orders.json"), {
            "created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "source": "crypto-budget", "targets_source": tdoc.get("source"), "exchange": cfg["exchange"],
            "quote": quote, "budget": args.budget, "cash_after": cash, "orders": orders})
        print("wrote %s as PROPOSALS -> next: crypto-risk" % dpath("orders.json"))
    else:
        print("(dry run; add --write to save the proposals)")


if __name__ == "__main__":
    main()
