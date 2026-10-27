#!/usr/bin/env python3
"""crypto-rebalance: bring the portfolio back to its target weights when it drifts.

  python3 rebalance.py                      # drift table, band 5 percentage points
  python3 rebalance.py --band 3 --write     # propose trades to crypto/orders.json
  python3 rebalance.py --add 20000000       # include new money (toman) in the rebalance
  python3 rebalance.py --keep-others        # do not sell coins that are not in the targets

Reads crypto/portfolio.json (cash + holdings), crypto/targets.json and
crypto/prices.json. Weights are of total equity (cash + coins); the cash buffer
and the per-coin cap from crypto/config.json still apply. Only coins outside the
band trade; trades under the minimum order are skipped; sells come first and
buys are scaled down if the cash after sells would break the buffer.
Proposals only: crypto-risk and crypto-broker come next.
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
    "max_leverage": 2,
    "max_margin": {"IRT": 1000000, "USDT": 50},
    "max_open_positions": 3,
    "min_liq_distance_pct": 15,
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
    for k, v in (load_json(dpath("config.json"), {}) or {}).items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def fmt(x, quote):
    return "{:,.0f}".format(x) if quote == "IRT" else "{:,.2f}".format(x)


def capped_targets(targets, investable_share, cap_share):
    """Scale weights to the investable share of equity, capping each coin (water-filling)."""
    w = {}
    for t in targets:
        w[t["coin"].upper()] = w.get(t["coin"].upper(), 0.0) + float(t["weight"])
    tot = sum(w.values()) or 1.0
    w = {k: v / tot for k, v in w.items()}
    out = {k: 0.0 for k in w}
    free = dict(w)
    left = investable_share
    while free and left > 1e-12:
        s = sum(free.values())
        over = {k: cap_share - out[k] for k, v in free.items() if out[k] + left * v / s > cap_share}
        if not over:
            for k, v in free.items():
                out[k] += left * v / s
            break
        for k, room in over.items():
            out[k] += room
            left -= room
            del free[k]
    return out


def plan(portfolio, targets, prices, cfg, quote, band, add_cash=0.0, sell_others=True):
    markets = (prices or {}).get("markets") or {}
    holdings = {k.upper(): float(v) for k, v in (portfolio.get("holdings") or {}).items() if float(v) > 0}
    cash = float(portfolio.get("cash") or 0) + add_cash
    missing = []
    value = {}
    for c, q in holdings.items():
        m = markets.get(c + quote)
        if not m or not m.get("last"):
            missing.append(c)
            continue
        value[c] = q * m["last"]
    equity = cash + sum(value.values())
    if equity <= 0:
        raise RuntimeError("portfolio is empty: set cash with crypto-pnl or broker.py balances / paper-init")
    tw = capped_targets(targets, 1 - cfg["cash_buffer_pct"] / 100.0, cfg["max_position_pct"] / 100.0)
    coins = sorted(set(tw) | set(value))
    rows, trades = [], []
    min_o = cfg["min_order"].get(quote, 0)
    for c in coins:
        cur = value.get(c, 0.0)
        tgt_w = tw.get(c, 0.0)
        if c not in tw and not sell_others:
            tgt_w = cur / equity
        cur_w = cur / equity
        drift = (cur_w - tgt_w) * 100
        delta = tgt_w * equity - cur
        m = markets.get(c + quote) or {}
        trade = abs(drift) > band or (tgt_w == 0 and cur > 0 and sell_others)
        row = {"coin": c, "current_pct": cur_w * 100, "target_pct": tgt_w * 100, "drift_pp": drift,
               "value": cur, "delta": delta, "trade": False, "note": ""}
        if c in tw and not m.get("last"):
            row["note"] = "no price"
        elif trade:
            if abs(delta) < max(min_o, m.get("min_notional") or 0):
                row["note"] = "under minimum order"
            else:
                row["trade"] = True
        rows.append(row)
    sells = [r for r in rows if r["trade"] and r["delta"] < 0]
    buys = [r for r in rows if r["trade"] and r["delta"] > 0]
    n = 0
    cash_after = cash
    for r in sorted(sells, key=lambda r: r["delta"]):
        m = markets[r["coin"] + quote]
        full_exit = r["target_pct"] == 0
        qty = holdings[r["coin"]] if full_exit else min(holdings[r["coin"]], -r["delta"] / m["last"])
        n += 1
        trades.append({"id": "r%d" % n, "coin": r["coin"], "side": "sell", "quote": quote, "exchange": cfg["exchange"],
                       "qty": round(qty, 10), "notional": round(qty * m["last"], 2), "ref_price": m.get("bid") or m["last"],
                       "type": "limit", "status": "proposed"})
        cash_after += qty * m["last"]
    spendable = max(0.0, cash_after - equity * cfg["cash_buffer_pct"] / 100.0)
    want = sum(r["delta"] for r in buys)
    scale = min(1.0, spendable / want) if want > 0 else 1.0
    unit = 1.0 if quote == "IRT" else 0.01
    for r in sorted(buys, key=lambda r: -r["delta"]):
        amt = int(r["delta"] * scale / unit) * unit
        m = markets[r["coin"] + quote]
        if amt < max(min_o, m.get("min_notional") or 0):
            r["note"] = "scaled under the minimum"
            r["trade"] = False
            continue
        n += 1
        trades.append({"id": "r%d" % n, "coin": r["coin"], "side": "buy", "quote": quote, "exchange": cfg["exchange"],
                       "notional": amt, "ref_price": m.get("ask") or m["last"], "type": "limit", "status": "proposed"})
    return {"equity": equity, "cash": cash, "rows": rows, "trades": trades, "missing": missing, "buy_scale": scale}


def main(argv=None):
    p = argparse.ArgumentParser(description="Rebalance a crypto portfolio to target weights")
    p.add_argument("--band", type=float, default=5.0, help="percentage points of drift before trading")
    p.add_argument("--add", type=float, default=0.0, help="new cash to include")
    p.add_argument("--keep-others", action="store_true")
    p.add_argument("--quote")
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    quote = (args.quote or cfg["quote"]).upper()
    pf = load_json(dpath("portfolio.json"))
    tg = load_json(dpath("targets.json"))
    if not pf:
        sys.exit("no crypto/portfolio.json (broker.py balances, broker.py paper-init, or crypto-pnl sync)")
    if not tg or not tg.get("targets"):
        sys.exit("no crypto/targets.json")
    try:
        r = plan(pf, tg["targets"], load_json(dpath("prices.json"), {}), cfg, quote, args.band, args.add, not args.keep_others)
    except RuntimeError as e:
        sys.exit("error: %s" % e)
    print("equity %s %s (cash %s)   band %.1f pp   buffer %s%%   cap %s%%" % (
        fmt(r["equity"], quote), quote, fmt(r["cash"], quote), args.band, cfg["cash_buffer_pct"], cfg["max_position_pct"]))
    print("%-7s %9s %9s %9s %18s  %s" % ("coin", "now %", "target %", "drift pp", "trade", ""))
    for row in sorted(r["rows"], key=lambda x: -abs(x["drift_pp"])):
        tr = ("%s %s" % ("buy" if row["delta"] > 0 else "sell", fmt(abs(row["delta"]), quote))) if row["trade"] else "-"
        print("%-7s %8.2f%% %8.2f%% %+9.2f %18s  %s" % (row["coin"], row["current_pct"], row["target_pct"],
                                                      row["drift_pp"], tr, row["note"]))
    if r["buy_scale"] < 0.999:
        print("buys scaled to %.0f%% to keep the cash buffer" % (r["buy_scale"] * 100))
    if r["missing"]:
        print("no price for held coins %s: their value is left out" % ", ".join(r["missing"]))
    if not r["trades"]:
        print("nothing to trade: every coin is inside the band")
        return
    if args.write:
        save_json(dpath("orders.json"), {
            "created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "source": "crypto-rebalance", "exchange": cfg["exchange"], "quote": quote, "orders": r["trades"]})
        print("wrote %d proposals (sells first) to %s -> next: crypto-risk" % (len(r["trades"]), dpath("orders.json")))
    else:
        print("(dry run; add --write to save the proposals)")


if __name__ == "__main__":
    main()
