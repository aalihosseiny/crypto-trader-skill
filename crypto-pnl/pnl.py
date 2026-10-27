#!/usr/bin/env python3
"""crypto-pnl: cost basis, profit and loss for every coin, lot by lot.

  python3 pnl.py add buy BTC 0.01 6800000000 --fee 0.25%      # a trade you made by hand (toman)
  python3 pnl.py add sell ETH 0.5 260000000 --fee 65000 --at 2026-10-01
  python3 pnl.py add buy SOL 3 180 --quote USDT
  python3 pnl.py report                                        # unrealized + realized, per coin
  python3 pnl.py report --method hifo                          # highest cost sold first
  python3 pnl.py lots BTC                                      # every open lot
  python3 pnl.py whatif sell BTC 0.005                         # what a sale would realize
  python3 pnl.py losers --min-loss 2000000                     # coins sitting on a loss
  python3 pnl.py sync                                          # rebuild holdings in crypto/portfolio.json
  python3 pnl.py cash 25000000                                 # set the cash line of the portfolio

Trades live in crypto/fills.json; crypto-broker appends its fills there (paper or
live); `add` records the ones made elsewhere. Methods: fifo (default), hifo.
Fees: a buy fee adds to the cost basis, a sell fee is taken from the proceeds.
Everything is in the trade's own quote currency (toman or USDT); the two are
never mixed in one number.
This is bookkeeping, not tax advice.
"""
import argparse
import datetime as dt
import json
import os
import sys

EPS = 1e-12


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


def fmt(x, quote):
    if x is None:
        return "-"
    return "{:,.0f}".format(x) if quote == "IRT" else "{:,.2f}".format(x)


def load_fills():
    return load_json(dpath("fills.json"), {"fills": []}) or {"fills": []}


# ---------------------------------------------------------------- engine
def build(fills, method="fifo"):
    """Replay fills in time order. -> {(coin, quote): {"lots": [...], "realized": x, "sales": [...]}}"""
    book = {}
    for f in sorted(fills, key=lambda f: f.get("at", "")):
        key = (f["coin"].upper(), f.get("quote", "IRT").upper())
        b = book.setdefault(key, {"lots": [], "realized": 0.0, "sales": [], "unmatched": 0.0})
        qty, px, fee = float(f["qty"]), float(f["price"]), float(f.get("fee") or 0)
        if f["side"] == "buy":
            b["lots"].append({"at": f.get("at"), "qty": qty, "cost": qty * px + fee, "unit": (qty * px + fee) / qty})
            continue
        proceeds = qty * px - fee
        realized, left, basis = 0.0, qty, 0.0
        lots = b["lots"]
        order = sorted(range(len(lots)), key=lambda i: -lots[i]["unit"]) if method == "hifo" else list(range(len(lots)))
        for i in order:
            if left <= EPS:
                break
            lot = lots[i]
            take = min(lot["qty"], left)
            part = lot["cost"] * take / lot["qty"] if lot["qty"] > EPS else 0.0
            basis += part
            lot["cost"] -= part
            lot["qty"] -= take
            left -= take
        b["lots"] = [l for l in lots if l["qty"] > EPS]
        if left > EPS:
            b["unmatched"] += left  # sold more than recorded buys: basis unknown for this part
        realized = proceeds * (qty - left) / qty - basis
        b["realized"] += realized
        b["sales"].append({"at": f.get("at"), "qty": qty, "proceeds": proceeds, "basis": basis, "pnl": realized})
    return book


def position(b):
    q = sum(l["qty"] for l in b["lots"])
    c = sum(l["cost"] for l in b["lots"])
    return q, c


def whatif(b, qty, price, fee_pct, method):
    lots = [dict(l) for l in b["lots"]]
    order = sorted(lots, key=lambda l: -l["unit"]) if method == "hifo" else lots
    left, basis = qty, 0.0
    for l in order:
        if left <= EPS:
            break
        take = min(l["qty"], left)
        basis += l["cost"] * take / l["qty"]
        left -= take
    sold = qty - left
    proceeds = sold * price * (1 - fee_pct / 100.0)
    return {"sold": sold, "short": left, "proceeds": proceeds, "basis": basis, "pnl": proceeds - basis}


# ---------------------------------------------------------------- commands
def parse_fee(s, qty, price):
    if not s:
        return 0.0
    s = str(s).strip()
    if s.endswith("%"):
        return qty * price * float(s[:-1]) / 100.0
    return float(s)


def cmd_add(args):
    side = args.side.lower()
    if side not in ("buy", "sell"):
        sys.exit("side must be buy or sell")
    qty, price = float(args.qty), float(args.price)
    if qty <= 0 or price <= 0:
        sys.exit("qty and price must be positive")
    quote = args.quote.upper()
    ref = (prices_map().get(args.coin.upper() + quote) or {}).get("last")
    if ref and not (0.5 <= price / ref <= 2):
        print("warning: %s %s is far from the market price %s; check the unit (toman vs rial vs USDT)" % (
            fmt(price, quote), quote, fmt(ref, quote)))
    elif quote == "IRT" and price < 100000 and args.coin.upper() in ("BTC", "ETH", "BNB", "SOL", "LTC", "BCH", "AVAX"):
        print("warning: %s toman for one %s looks like a dollar price; did you mean --quote USDT?" % (args.price, args.coin.upper()))
    at = args.at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    doc = load_fills()
    f = {"id": "m%d" % (len(doc["fills"]) + 1), "at": at, "coin": args.coin.upper(), "side": side, "qty": qty,
         "price": price, "fee": parse_fee(args.fee, qty, price), "quote": quote, "source": "manual"}
    doc["fills"].append(f)
    save_json(dpath("fills.json"), doc)
    print("recorded %s %s %.8g at %s %s (fee %s)" % (side, f["coin"], qty, fmt(price, quote), quote, fmt(f["fee"], quote)))


def prices_map():
    return ((load_json(dpath("prices.json"), {}) or {}).get("markets")) or {}


def cmd_report(args):
    book = build(load_fills()["fills"], args.method)
    if not book:
        sys.exit("no trades yet (crypto-broker records them, or use: pnl.py add buy BTC QTY PRICE)")
    mk = prices_map()
    by_quote = {}
    print("method %s   prices from %s" % (args.method, (load_json(dpath("prices.json"), {}) or {}).get("updated", "never")))
    print("%-6s %-5s %16s %18s %18s %18s %8s %18s" % ("coin", "quote", "qty", "cost", "value now", "unrealized", "%", "realized"))
    for (coin, quote), b in sorted(book.items()):
        q, cost = position(b)
        m = mk.get(coin + quote)
        val = q * m["last"] if (m and m.get("last")) else None
        un = (val - cost) if val is not None else None
        t = by_quote.setdefault(quote, {"cost": 0.0, "value": 0.0, "un": 0.0, "real": 0.0, "unpriced": False})
        t["cost"] += cost
        t["real"] += b["realized"]
        if val is None:
            t["unpriced"] = t["unpriced"] or q > EPS
        else:
            t["value"] += val
            t["un"] += un
        print("%-6s %-5s %16.8g %18s %18s %18s %8s %18s%s" % (
            coin, quote, q, fmt(cost, quote), fmt(val, quote), fmt(un, quote),
            ("%+.1f" % (un / cost * 100)) if (un is not None and cost > 0) else "-", fmt(b["realized"], quote),
            "   (sold %.8g more than recorded buys)" % b["unmatched"] if b["unmatched"] > EPS else ""))
    for quote, t in by_quote.items():
        print("TOTAL %-5s cost %s  value %s  unrealized %s  realized %s%s" % (
            quote, fmt(t["cost"], quote), fmt(t["value"], quote), fmt(t["un"], quote), fmt(t["real"], quote),
            "  (some coins have no price; refresh crypto-alerts prices)" if t["unpriced"] else ""))


def cmd_lots(args):
    book = build(load_fills()["fills"], args.method)
    for (coin, quote), b in sorted(book.items()):
        if args.coin and coin != args.coin.upper():
            continue
        m = prices_map().get(coin + quote) or {}
        print("%s/%s" % (coin, quote))
        for l in b["lots"]:
            un = (m["last"] - l["unit"]) * l["qty"] if m.get("last") else None
            print("   %-25s %16.8g  unit cost %16s  P&L %s" % (l["at"], l["qty"], fmt(l["unit"], quote), fmt(un, quote)))


def cmd_whatif(args):
    book = build(load_fills()["fills"], args.method)
    key = (args.coin.upper(), args.quote.upper())
    if key not in book:
        sys.exit("no %s/%s trades on record" % key)
    m = prices_map().get(key[0] + key[1]) or {}
    price = args.price or m.get("bid") or m.get("last")
    if not price:
        sys.exit("no price: pass --price or run crypto-alerts prices")
    fee_pct = args.fee if args.fee is not None else 0.25
    r = whatif(book[key], float(args.qty), price, fee_pct, args.method)
    q = key[1]
    print("sell %.8g %s at %s %s (%s, fee %.2f%%)" % (r["sold"], key[0], fmt(price, q), q, args.method, fee_pct))
    print("  proceeds %s  cost basis %s  -> P&L %s" % (fmt(r["proceeds"], q), fmt(r["basis"], q), fmt(r["pnl"], q)))
    if r["short"] > EPS:
        print("  you only hold %.8g on record" % r["sold"])


def cmd_losers(args):
    book = build(load_fills()["fills"], args.method)
    mk = prices_map()
    rows = []
    for (coin, quote), b in book.items():
        q, cost = position(b)
        m = mk.get(coin + quote)
        if q > EPS and m and m.get("last"):
            un = q * m["last"] - cost
            if un <= -abs(args.min_loss):
                rows.append((un, coin, quote, q, cost))
    if not rows:
        print("no position is down by %s or more" % fmt(args.min_loss, "IRT"))
    for un, coin, quote, q, cost in sorted(rows):
        print("%-6s %-5s down %s (%.1f%%) on %.8g" % (coin, quote, fmt(-un, quote), un / cost * 100, q))


def cmd_sync(args):
    book = build(load_fills()["fills"], "fifo")
    pf = load_json(dpath("portfolio.json"), {}) or {}
    quote = (pf.get("quote") or "IRT").upper()
    hold = {coin: round(position(b)[0], 10) for (coin, q), b in book.items() if q == quote and position(b)[0] > EPS}
    pf["holdings"] = hold
    pf["quote"] = quote
    pf.setdefault("cash", 0)
    pf["updated"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    save_json(dpath("portfolio.json"), pf)
    print("portfolio holdings rebuilt from fills: %s" % (", ".join("%s %.8g" % kv for kv in hold.items()) or "none"))


def cmd_cash(args):
    pf = load_json(dpath("portfolio.json"), {}) or {}
    pf["cash"] = float(args.amount)
    pf["quote"] = (args.quote or pf.get("quote") or "IRT").upper()
    pf.setdefault("holdings", {})
    save_json(dpath("portfolio.json"), pf)
    print("cash set to %s %s" % (fmt(pf["cash"], pf["quote"]), pf["quote"]))


def main(argv=None):
    p = argparse.ArgumentParser(description="Crypto cost basis and P&L")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("side"); s.add_argument("coin"); s.add_argument("qty"); s.add_argument("price")
    s.add_argument("--fee"); s.add_argument("--quote", default="IRT"); s.add_argument("--at"); s.set_defaults(fn=cmd_add)
    for name, fn in (("report", cmd_report), ("lots", cmd_lots), ("losers", cmd_losers)):
        s = sub.add_parser(name); s.add_argument("--method", default="fifo", choices=["fifo", "hifo"]); s.set_defaults(fn=fn)
        if name == "lots":
            s.add_argument("coin", nargs="?")
        if name == "losers":
            s.add_argument("--min-loss", type=float, default=0)
    s = sub.add_parser("whatif"); s.add_argument("side", choices=["sell"]); s.add_argument("coin"); s.add_argument("qty")
    s.add_argument("--price", type=float); s.add_argument("--fee", type=float); s.add_argument("--quote", default="IRT")
    s.add_argument("--method", default="fifo", choices=["fifo", "hifo"]); s.set_defaults(fn=cmd_whatif)
    s = sub.add_parser("sync"); s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("cash"); s.add_argument("amount"); s.add_argument("--quote"); s.set_defaults(fn=cmd_cash)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
