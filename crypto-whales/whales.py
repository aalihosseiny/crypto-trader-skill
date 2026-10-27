#!/usr/bin/env python3
"""crypto-whales: watch big wallets on-chain and copy what they hold.

Look
  python3 whales.py eth 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045   # ETH + every ERC-20, in USD
  python3 whales.py btc bc1q...                                     # balance + latest transactions

Watch
  python3 whales.py watch add 0xABC... --label "fund X"             # chain is detected from the address
  python3 whales.py watch list | watch remove 0xABC...
  python3 whales.py check --min-usd 1000000                         # what changed since the last check

Copy
  python3 whales.py copy 0xABC... --top 5                           # weights -> crypto/targets.json
  python3 whales.py copy 0xA... 0xB... --top 8                      # blend several wallets equally
  python3 whales.py copy 0xABC... --top 5 --keep-stables            # stablecoins stay in the weights

Sources (public): Ethplorer (api.ethplorer.io, "freekey" unless $ETHPLORER_KEY is set)
for Ethereum wallets and token prices, mempool.space for Bitcoin.
On-chain data shows what a wallet holds, not why. A wallet labelled "fund" or
"whale" on a website can be an exchange, a custodian or a bridge: check the label.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "crypto-trader-skill/1.0 (+https://github.com/crypto-trader-skill)"
ETHPLORER = os.environ.get("ETHPLORER_BASE", "https://api.ethplorer.io")
MEMPOOL = os.environ.get("MEMPOOL_BASE", "https://mempool.space/api")
STABLES = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FDUSD", "PYUSD", "USDE", "USDS", "GUSD", "FRAX", "LUSD"}
# wrapped or staked forms that trade as the underlying coin on Iranian exchanges
ALIAS = {"WETH": "ETH", "STETH": "ETH", "WSTETH": "ETH", "WEETH": "ETH", "CBETH": "ETH", "RETH": "ETH",
         "WBTC": "BTC", "CBBTC": "BTC", "TBTC": "BTC"}


def data_dir():
    return os.environ.get("CRYPTO_DIR", "crypto")


def dpath(*names):
    return os.path.join(data_dir(), *names)


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


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch(url, timeout=40, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError("could not fetch %s: %s" % (url.split("apiKey=")[0], last))


def chain_of(addr):
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return "eth"
    if re.fullmatch(r"(bc1[0-9a-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})", addr):
        return "btc"
    raise RuntimeError("cannot tell the chain of %r (Ethereum 0x... or Bitcoin 1.../3.../bc1...)" % addr)


def usd(x):
    if x is None:
        return "-"
    if abs(x) >= 1e9:
        return "$%.2fB" % (x / 1e9)
    if abs(x) >= 1e6:
        return "$%.2fM" % (x / 1e6)
    if abs(x) >= 1e3:
        return "$%.1fK" % (x / 1e3)
    return "$%.2f" % x


# ---------------------------------------------------------------- holdings
def eth_holdings(addr):
    """-> {"chain","address","total_usd","assets":{SYMBOL:{"qty","usd","price","token"}}}"""
    key = os.environ.get("ETHPLORER_KEY", "freekey")
    d = fetch("%s/getAddressInfo/%s?apiKey=%s" % (ETHPLORER, addr, key))
    if "error" in d:
        raise RuntimeError("ethplorer: %s" % d["error"].get("message", d["error"]))
    return parse_ethplorer(addr, d)


def parse_ethplorer(addr, d):
    assets = {}

    def put(sym, qty, price, token):
        sym = ALIAS.get(sym.upper(), sym.upper())
        a = assets.setdefault(sym, {"qty": 0.0, "usd": 0.0, "price": price, "tokens": []})
        a["qty"] += qty
        if price:
            a["usd"] += qty * price
            a["price"] = price
        a["tokens"].append(token)

    eth = d.get("ETH") or {}
    eth_price = ((eth.get("price") or {}) or {}).get("rate")
    if eth.get("balance"):
        put("ETH", float(eth["balance"]), eth_price, "native")
    for t in d.get("tokens") or []:
        ti = t.get("tokenInfo") or {}
        sym = (ti.get("symbol") or "").strip()
        if not sym or len(sym) > 12:
            continue
        try:
            dec = int(ti.get("decimals") or 0)
        except ValueError:
            continue
        raw = t.get("rawBalance")
        qty = int(raw) / 10 ** dec if raw not in (None, "") else float(t.get("balance") or 0) / 10 ** dec
        price = (ti.get("price") or {}).get("rate") if isinstance(ti.get("price"), dict) else None
        if qty <= 0:
            continue
        put(sym, qty, price, ti.get("address", ""))
    # tokens with no market price are left at 0 USD and never copied
    total = sum(a["usd"] for a in assets.values())
    return {"chain": "eth", "address": addr, "total_usd": total, "assets": assets, "at": now_iso()}


def btc_holdings(addr, btc_price=None):
    d = fetch("%s/address/%s" % (MEMPOOL, addr))
    cs, ms = d.get("chain_stats") or {}, d.get("mempool_stats") or {}
    sats = (cs.get("funded_txo_sum", 0) - cs.get("spent_txo_sum", 0)
            + ms.get("funded_txo_sum", 0) - ms.get("spent_txo_sum", 0))
    if btc_price is None:
        try:
            btc_price = float(fetch("%s/v1/prices" % MEMPOOL).get("USD"))
        except Exception:
            btc_price = None
    qty = sats / 1e8
    val = qty * btc_price if btc_price else 0.0
    return {"chain": "btc", "address": addr, "total_usd": val, "tx_count": cs.get("tx_count"),
            "assets": {"BTC": {"qty": qty, "usd": val, "price": btc_price, "tokens": ["native"]}}, "at": now_iso()}


def holdings_of(addr):
    return eth_holdings(addr) if chain_of(addr) == "eth" else btc_holdings(addr)


def btc_recent(addr, limit=10):
    txs = fetch("%s/address/%s/txs" % (MEMPOOL, addr))[:limit]
    rows = []
    for tx in txs:
        got = sum(o.get("value", 0) for o in tx.get("vout", []) if o.get("scriptpubkey_address") == addr)
        sent = sum((i.get("prevout") or {}).get("value", 0) for i in tx.get("vin", [])
                   if (i.get("prevout") or {}).get("scriptpubkey_address") == addr)
        st = tx.get("status") or {}
        when = dt.datetime.fromtimestamp(st["block_time"], dt.timezone.utc).strftime("%Y-%m-%d %H:%M") \
            if st.get("block_time") else "unconfirmed"
        rows.append({"txid": tx.get("txid"), "when": when, "net_btc": (got - sent) / 1e8})
    return rows


# ---------------------------------------------------------------- diff & copy
def diff(old, new, min_usd=0.0):
    """Compare two holdings snapshots -> list of changes, biggest first."""
    out = []
    oa, na = (old or {}).get("assets", {}), new.get("assets", {})
    for sym in set(oa) | set(na):
        o, n = oa.get(sym, {}).get("qty", 0.0), na.get(sym, {}).get("qty", 0.0)
        price = na.get(sym, {}).get("price") or oa.get(sym, {}).get("price")
        if abs(n - o) <= 1e-12 * max(abs(o), 1):
            continue
        dusd = (n - o) * price if price else None
        if dusd is not None and abs(dusd) < min_usd:
            continue
        if o == 0:
            kind = "NEW"
        elif n == 0:
            kind = "SOLD OUT"
        elif n > o:
            kind = "added"
        else:
            kind = "trimmed"
        out.append({"coin": sym, "kind": kind, "old_qty": o, "new_qty": n,
                    "change_pct": ((n / o - 1) * 100) if o else None, "usd": dusd})
    out.sort(key=lambda r: -abs(r["usd"] or 0))
    return out


def weights(snapshots, top=10, keep_stables=False, tradable=None, min_weight_pct=0.0):
    """Blend one or more holdings snapshots into copy weights that sum to 1."""
    blend = {}
    skipped = {}
    for snap in snapshots:
        tot = sum(a["usd"] for s, a in snap["assets"].items()
                  if a["usd"] > 0 and (keep_stables or s not in STABLES))
        if tot <= 0:
            continue
        for s, a in snap["assets"].items():
            if a["usd"] <= 0 or (not keep_stables and s in STABLES):
                continue
            blend[s] = blend.get(s, 0.0) + a["usd"] / tot / len(snapshots)
    ranked = sorted(blend.items(), key=lambda kv: -kv[1])
    picked = []
    for s, w in ranked:
        if tradable is not None and s not in tradable:
            skipped[s] = "not listed on your exchange"
            continue
        if w * 100 < min_weight_pct:
            skipped[s] = "under %.2f%% of the wallet" % min_weight_pct
            continue
        picked.append((s, w))
        if len(picked) >= top:
            break
    tot = sum(w for _, w in picked)
    targets = [{"coin": s, "weight": round(w / tot, 6), "wallet_weight": round(w, 6)} for s, w in picked] if tot else []
    return targets, skipped


def tradable_coins(quote):
    book = load_json(dpath("prices.json"), {}) or {}
    coins = {m["coin"] for m in (book.get("markets") or {}).values() if m.get("quote") == quote}
    return coins or None


# ---------------------------------------------------------------- commands
def print_holdings(h, limit=25):
    print("%s wallet %s   total %s   (%s)" % (h["chain"].upper(), h["address"], usd(h["total_usd"]), h["at"]))
    rows = sorted(h["assets"].items(), key=lambda kv: -kv[1]["usd"])
    for s, a in rows[:limit]:
        share = (a["usd"] / h["total_usd"] * 100) if h["total_usd"] else 0
        print("  %-8s %20.6f   %12s  %6.2f%%%s" % (s, a["qty"], usd(a["usd"]) if a["usd"] else "no price", share,
                                                  "  (stable)" if s in STABLES else ""))
    if len(rows) > limit:
        print("  ... %d more" % (len(rows) - limit))


def cmd_eth(args):
    print_holdings(eth_holdings(args.address), args.limit)


def cmd_btc(args):
    h = btc_holdings(args.address)
    print_holdings(h)
    for r in btc_recent(args.address, args.limit):
        print("  %s  %+14.8f BTC  %s" % (r["when"], r["net_btc"], r["txid"]))


def load_watch():
    return load_json(dpath("whales.json"), {"wallets": []}) or {"wallets": []}


def cmd_watch(args):
    doc = load_watch()
    if args.action == "list":
        if not doc["wallets"]:
            print("no wallets watched yet")
        for w in doc["wallets"]:
            print("%-4s %-44s %-24s last check %s" % (w["chain"], w["address"], w.get("label", ""), w.get("last_check", "never")))
        return
    if not args.address:
        sys.exit("usage: watch add|remove ADDRESS [--label TEXT]")
    if args.action == "add":
        ch = chain_of(args.address)
        if any(w["address"].lower() == args.address.lower() for w in doc["wallets"]):
            sys.exit("already watching %s" % args.address)
        doc["wallets"].append({"address": args.address, "chain": ch, "label": args.label or "", "added": now_iso()})
        save_json(dpath("whales.json"), doc)
        print("watching %s (%s) %s" % (args.address, ch, args.label or ""))
    elif args.action == "remove":
        n = len(doc["wallets"])
        doc["wallets"] = [w for w in doc["wallets"] if w["address"].lower() != args.address.lower()]
        if len(doc["wallets"]) == n:
            sys.exit("not watching %s" % args.address)
        save_json(dpath("whales.json"), doc)
        print("stopped watching %s" % args.address)


def snap_path(addr):
    return dpath("whales", addr.lower() + ".json")


def cmd_check(args):
    doc = load_watch()
    if not doc["wallets"]:
        sys.exit("no wallets watched: whales.py watch add ADDRESS --label ...")
    held = set((load_json(dpath("portfolio.json"), {}) or {}).get("holdings", {}) or {})
    for w in doc["wallets"]:
        try:
            new = holdings_of(w["address"])
        except RuntimeError as e:
            print("%s %s: %s" % (w.get("label") or "", w["address"], e))
            continue
        old = load_json(snap_path(w["address"]))
        print("== %s %s  total %s" % (w.get("label") or w["chain"], w["address"], usd(new["total_usd"])))
        if old is None:
            print("   first snapshot saved; changes show from the next check")
        else:
            ch = diff(old, new, args.min_usd)
            if not ch:
                print("   no change over %s since %s" % (usd(args.min_usd), old.get("at")))
            for c in ch:
                print("   %s%-8s %-8s %s -> %s  %s%s" % (
                    "*" if c["coin"] in held else " ", c["kind"], c["coin"],
                    "%.6g" % c["old_qty"], "%.6g" % c["new_qty"],
                    usd(c["usd"]) if c["usd"] is not None else "",
                    ("  (%+.1f%%)" % c["change_pct"]) if c["change_pct"] is not None else ""))
        save_json(snap_path(w["address"]), new)
        w["last_check"] = new["at"]
        time.sleep(args.pause)
    save_json(dpath("whales.json"), doc)
    if held:
        print("(* = a coin you hold)")


def cmd_copy(args):
    snaps = []
    for a in args.addresses:
        h = holdings_of(a)
        snaps.append(h)
        save_json(snap_path(a), h)
        time.sleep(args.pause)
    trad = None if args.any_coin else tradable_coins(args.quote.upper())
    if trad is None and not args.any_coin:
        print("note: no crypto/prices.json yet, so tradability on your exchange is not checked.\n"
              "      run: python3 crypto-alerts/alerts.py prices --all   then copy again")
    targets, skipped = weights(snaps, args.top, args.keep_stables, trad, args.min_weight)
    if not targets:
        sys.exit("nothing to copy (all holdings were stablecoins, unpriced or not tradable)")
    prev = load_json(dpath("targets.json"), {}) or {}
    prev_w = {t["coin"]: t["weight"] for t in prev.get("targets", [])}
    labels = {w["address"].lower(): w.get("label") for w in load_watch()["wallets"]}
    src = ", ".join((labels.get(a.lower()) or a) for a in args.addresses)
    print("copy of %s  (top %d%s)" % (src, args.top, ", stables kept" if args.keep_stables else ", stables removed"))
    print("%-8s %10s %12s %10s" % ("coin", "target %", "in wallet %", "vs last"))
    for t in targets:
        pw = prev_w.get(t["coin"])
        vs = "new" if pw is None else "%+.2f pp" % ((t["weight"] - pw) * 100)
        print("%-8s %9.2f%% %11.2f%% %10s" % (t["coin"], t["weight"] * 100, t["wallet_weight"] * 100, vs))
    dropped = [c for c in prev_w if c not in {t["coin"] for t in targets}]
    if dropped and prev.get("source", "").startswith("crypto-whales"):
        print("left the copy since last time: " + ", ".join(dropped))
    if skipped:
        print("skipped: " + "; ".join("%s (%s)" % kv for kv in list(skipped.items())[:12]))
    save_json(dpath("targets.json"), {"source": "crypto-whales copy of " + src, "quote": args.quote.upper(),
                                      "created": now_iso(), "wallets": args.addresses, "targets": targets})
    print("wrote %s -> next: crypto-budget" % dpath("targets.json"))


def main(argv=None):
    p = argparse.ArgumentParser(description="Whale wallets: look, watch, copy")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("eth"); s.add_argument("address"); s.add_argument("--limit", type=int, default=25); s.set_defaults(fn=cmd_eth)
    s = sub.add_parser("btc"); s.add_argument("address"); s.add_argument("--limit", type=int, default=10); s.set_defaults(fn=cmd_btc)
    s = sub.add_parser("watch"); s.add_argument("action", choices=["add", "list", "remove"])
    s.add_argument("address", nargs="?"); s.add_argument("--label"); s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("check"); s.add_argument("--min-usd", type=float, default=100000)
    s.add_argument("--pause", type=float, default=1.5); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("copy"); s.add_argument("addresses", nargs="+"); s.add_argument("--top", type=int, default=5)
    s.add_argument("--quote", default="IRT"); s.add_argument("--keep-stables", action="store_true")
    s.add_argument("--min-weight", type=float, default=0.5, help="skip coins under this %% of the wallet")
    s.add_argument("--any-coin", action="store_true", help="do not check tradability")
    s.add_argument("--pause", type=float, default=1.5); s.set_defaults(fn=cmd_copy)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except RuntimeError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
