#!/usr/bin/env python3
"""crypto-alerts: live prices from Nobitex / Wallex and price alerts that ring once.

Prices
  python3 alerts.py prices BTC ETH SOL                 # Nobitex, toman markets
  python3 alerts.py prices BTC ETH --quote USDT        # tether markets
  python3 alerts.py prices --all --exchange wallex     # every toman market on Wallex
  python3 alerts.py usdt                               # the tether rate in toman

Alerts
  python3 alerts.py add BTC above 6500000000           # toman (IRT) by default
  python3 alerts.py add ETH below 2900 --quote USDT
  python3 alerts.py add SOL stop 9000000 --note "my stop"
  python3 alerts.py add BTC move 5                     # |24h change| >= 5 %
  python3 alerts.py list | remove ID | reset ID
  python3 alerts.py check --refresh                    # fetch, evaluate, ring once

Units: every toman market is stored in TOMAN, whatever the exchange uses.
Nobitex's API quotes rial markets in RIAL; this tool divides by 10 on the way in
(and crypto-broker multiplies by 10 on the way out). Wallex quotes TMN markets in
toman already.

Data: everything lives in ./crypto (or $CRYPTO_DIR):
  crypto/prices.json   markets -> last / bid / ask / 24h stats / exchange limits
  crypto/alerts.json   your alert rules and whether each has rung

Public endpoints only, no key needed. Nobitex asks for an Iranian IP.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "crypto-trader-skill/1.0 (+https://github.com/crypto-trader-skill)"
NOBITEX_BASE = os.environ.get("NOBITEX_BASE", "https://apiv2.nobitex.ir")
WALLEX_BASE = os.environ.get("WALLEX_BASE", "https://api.wallex.ir")
QUOTES = ("IRT", "USDT")
# exchange minimums (quote currency). Nobitex: 3,000,000 rial = 300,000 toman, 11 USDT.
NOBITEX_MIN = {"IRT": 300000.0, "USDT": 11.0}


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
    except json.JSONDecodeError as e:
        sys.exit("error: %s is not valid JSON (%s)" % (path, e))


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


def fetch(url, headers=None, timeout=30, retries=2):
    """GET a URL and return parsed JSON. Tests replace this function."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.0 + attempt)
    raise RuntimeError("could not fetch %s: %s" % (url, last))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt(x, quote="IRT"):
    if x is None:
        return "-"
    if quote == "IRT":
        return "{:,.0f}".format(x)
    if abs(x) >= 100:
        return "{:,.2f}".format(x)
    return "{:,.6g}".format(x)


def market_key(coin, quote):
    return coin.upper() + quote.upper()


# ---------------------------------------------------------------- exchanges
def nobitex_markets(coins, quote):
    """Return {MARKET: info} from Nobitex /market/stats. Rial -> toman."""
    dst = "rls" if quote == "IRT" else "usdt"
    q = {"dstCurrency": dst}
    if coins:
        q["srcCurrency"] = ",".join(c.lower() for c in coins)
    data = fetch(NOBITEX_BASE + "/market/stats?" + urllib.parse.urlencode(q))
    if data.get("status") != "ok":
        raise RuntimeError("nobitex error: %s" % data)
    div = 10.0 if quote == "IRT" else 1.0
    out = {}
    for key, s in (data.get("stats") or {}).items():
        if "-" not in key:
            continue
        src, d = key.split("-", 1)
        if d != dst or s.get("isClosed"):
            continue
        last = fnum(s.get("latest"))
        if not last:
            continue
        coin = src.upper()
        out[market_key(coin, quote)] = {
            "coin": coin, "quote": quote, "exchange": "nobitex",
            "last": last / div,
            "bid": (fnum(s.get("bestBuy")) or 0) / div or None,
            "ask": (fnum(s.get("bestSell")) or 0) / div or None,
            "high": (fnum(s.get("dayHigh")) or 0) / div or None,
            "low": (fnum(s.get("dayLow")) or 0) / div or None,
            "open": (fnum(s.get("dayOpen")) or 0) / div or None,
            "change_pct": fnum(s.get("dayChange")),
            "volume": fnum(s.get("volumeSrc")),
            "min_notional": NOBITEX_MIN[quote],
            "qty_step": None,
        }
    return out


def wallex_markets(coins, quote):
    """Return {MARKET: info} from Wallex /v1/markets. TMN is already toman."""
    wq = "TMN" if quote == "IRT" else "USDT"
    data = fetch(WALLEX_BASE + "/v1/markets")
    symbols = ((data or {}).get("result") or {}).get("symbols") or {}
    want = set(c.upper() for c in coins) if coins else None
    out = {}
    for sym, s in symbols.items():
        if s.get("quoteAsset", "").upper() != wq:
            continue
        coin = (s.get("baseAsset") or "").upper()
        if want is not None and coin not in want:
            continue
        st = s.get("stats") or {}
        last = fnum(st.get("lastPrice"))
        if not last:
            continue
        step = s.get("stepSize")
        out[market_key(coin, quote)] = {
            "coin": coin, "quote": quote, "exchange": "wallex", "symbol": sym,
            "last": last,
            "bid": fnum(st.get("bidPrice")),
            "ask": fnum(st.get("askPrice")),
            "high": fnum(st.get("24h_highPrice")),
            "low": fnum(st.get("24h_lowPrice")),
            "open": None,
            "change_pct": fnum(st.get("24h_ch")),
            "volume": fnum(st.get("24h_volume")),
            "min_notional": fnum(s.get("minNotional")),
            "qty_step": (10 ** -int(step)) if isinstance(step, int) else None,
        }
    return out


def get_markets(exchange, coins, quote):
    quote = quote.upper()
    if quote not in QUOTES:
        sys.exit("error: quote must be IRT or USDT")
    if exchange == "nobitex":
        return nobitex_markets(coins, quote)
    if exchange == "wallex":
        return wallex_markets(coins, quote)
    sys.exit("error: exchange must be nobitex or wallex")


def update_prices(exchange, coins, quote, include_usdt=True):
    coins = [c.upper() for c in coins]
    if quote == "IRT" and include_usdt and coins and "USDT" not in coins:
        coins = coins + ["USDT"]
    markets = get_markets(exchange, coins, quote)
    missing = [c for c in coins if market_key(c, quote) not in markets]
    book = load_json(dpath("prices.json"), {}) or {}
    book.setdefault("markets", {})
    stamp = now_iso()
    for k, v in markets.items():
        v["updated"] = stamp
        book["markets"][k] = v
    book["updated"] = stamp
    book["exchange"] = exchange
    usdt = book["markets"].get("USDTIRT")
    if usdt:
        book["usdt_irt"] = usdt["last"]
    save_json(dpath("prices.json"), book)
    return markets, missing


# ---------------------------------------------------------------- alerts
KINDS = ("above", "below", "stop", "move")


def load_alerts():
    return load_json(dpath("alerts.json"), {"alerts": []}) or {"alerts": []}


def evaluate(alert, mkt):
    """Return a message if the alert should ring now, else None."""
    if not mkt:
        return None
    price = mkt.get("last")
    kind = alert["kind"]
    q = alert.get("quote", "IRT")
    if kind == "above" and price is not None and price >= alert["level"]:
        return "%s is %s, at or above your target %s" % (alert["market"], fmt(price, q), fmt(alert["level"], q))
    if kind in ("below", "stop") and price is not None and price <= alert["level"]:
        word = "STOP hit" if kind == "stop" else "at or below"
        return "%s is %s, %s %s" % (alert["market"], fmt(price, q), word, fmt(alert["level"], q))
    if kind == "move":
        ch = mkt.get("change_pct")
        if ch is not None and abs(ch) >= alert["level"]:
            return "%s moved %+.2f%% in 24h (threshold %.2f%%)" % (alert["market"], ch, alert["level"])
    return None


def check_alerts(markets, alerts_doc, when=None):
    when = when or now_iso()
    rung = []
    for a in alerts_doc["alerts"]:
        if a.get("fired"):
            continue
        msg = evaluate(a, markets.get(a["market"]))
        if msg:
            a["fired"] = True
            a["fired_at"] = when
            a["message"] = msg
            rung.append(a)
    return rung


# ---------------------------------------------------------------- commands
def cmd_prices(args):
    if not args.coins and not args.all:
        sys.exit("error: name some coins (BTC ETH ...) or pass --all")
    markets, missing = update_prices(args.exchange, [] if args.all else args.coins, args.quote.upper())
    q = args.quote.upper()
    print("%s  %s markets  (%s)" % (args.exchange, q, "toman" if q == "IRT" else "tether"))
    print("%-10s %18s %18s %18s %8s" % ("market", "last", "bid", "ask", "24h %"))
    for k in sorted(markets):
        m = markets[k]
        print("%-10s %18s %18s %18s %8s" % (k, fmt(m["last"], q), fmt(m["bid"], q), fmt(m["ask"], q),
                                             "-" if m["change_pct"] is None else "%+.2f" % m["change_pct"]))
    if missing:
        print("not listed on %s (%s): %s" % (args.exchange, q, ", ".join(missing)))
    print("wrote %s" % dpath("prices.json"))


def cmd_usdt(args):
    markets, _ = update_prices(args.exchange, ["USDT"], "IRT", include_usdt=False)
    m = markets.get("USDTIRT")
    if not m:
        sys.exit("USDT/IRT not found on %s" % args.exchange)
    print("USDT on %s: %s toman  (bid %s / ask %s, 24h %s%%)" % (
        args.exchange, fmt(m["last"]), fmt(m["bid"]), fmt(m["ask"]),
        "-" if m["change_pct"] is None else "%+.2f" % m["change_pct"]))


def cmd_add(args):
    kind = args.kind.lower()
    if kind not in KINDS:
        sys.exit("error: kind must be one of %s" % ", ".join(KINDS))
    doc = load_alerts()
    n = 1 + max([int(a["id"][1:]) for a in doc["alerts"] if a["id"][1:].isdigit()] or [0])
    a = {"id": "a%d" % n, "market": market_key(args.coin, args.quote), "coin": args.coin.upper(),
         "quote": args.quote.upper(), "kind": kind, "level": float(args.level),
         "note": args.note or "", "created": now_iso(), "fired": False}
    doc["alerts"].append(a)
    save_json(dpath("alerts.json"), doc)
    unit = "%" if kind == "move" else ("toman" if a["quote"] == "IRT" else "USDT")
    print("added %s: %s %s %s %s" % (a["id"], a["market"], kind, fmt(a["level"], a["quote"]) if kind != "move" else a["level"], unit))


def cmd_list(args):
    doc = load_alerts()
    if not doc["alerts"]:
        print("no alerts yet")
        return
    for a in doc["alerts"]:
        lvl = ("%.2f%%" % a["level"]) if a["kind"] == "move" else fmt(a["level"], a.get("quote", "IRT"))
        state = ("RANG " + a.get("fired_at", "")) if a.get("fired") else "armed"
        print("%-4s %-10s %-6s %18s  %-28s %s" % (a["id"], a["market"], a["kind"], lvl, state, a.get("note", "")))


def cmd_remove(args):
    doc = load_alerts()
    before = len(doc["alerts"])
    doc["alerts"] = [a for a in doc["alerts"] if a["id"] != args.id]
    if len(doc["alerts"]) == before:
        sys.exit("no alert %s" % args.id)
    save_json(dpath("alerts.json"), doc)
    print("removed %s" % args.id)


def cmd_reset(args):
    doc = load_alerts()
    for a in doc["alerts"]:
        if a["id"] == args.id:
            a["fired"] = False
            a.pop("fired_at", None)
            a.pop("message", None)
            save_json(dpath("alerts.json"), doc)
            print("re-armed %s" % args.id)
            return
    sys.exit("no alert %s" % args.id)


def cmd_check(args):
    doc = load_alerts()
    armed = [a for a in doc["alerts"] if not a.get("fired")]
    if not armed:
        print("no armed alerts")
        return
    if args.refresh:
        by_quote = {}
        for a in armed:
            by_quote.setdefault(a.get("quote", "IRT"), set()).add(a["coin"])
        for q, coins in by_quote.items():
            update_prices(args.exchange, sorted(coins), q, include_usdt=False)
    book = load_json(dpath("prices.json"), {}) or {}
    markets = book.get("markets", {})
    stale = [a["market"] for a in armed if a["market"] not in markets]
    rung = check_alerts(markets, doc)
    save_json(dpath("alerts.json"), doc)
    for a in rung:
        print("RING %s  %s%s" % (a["id"], a["message"], ("  [" + a["note"] + "]") if a.get("note") else ""))
    if not rung:
        print("nothing rang (%d armed, prices from %s)" % (len(armed), book.get("updated", "never")))
    if stale:
        print("no price for: %s (run prices first or use --refresh)" % ", ".join(sorted(set(stale))))


def main(argv=None):
    p = argparse.ArgumentParser(description="Crypto prices and alerts for Nobitex / Wallex")
    p.add_argument("--exchange", default=None, help="nobitex (default) or wallex")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prices"); s.add_argument("coins", nargs="*"); s.add_argument("--quote", default="IRT")
    s.add_argument("--all", action="store_true"); s.set_defaults(fn=cmd_prices)
    s = sub.add_parser("usdt"); s.set_defaults(fn=cmd_usdt)
    s = sub.add_parser("add"); s.add_argument("coin"); s.add_argument("kind"); s.add_argument("level")
    s.add_argument("--quote", default="IRT"); s.add_argument("--note"); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("list"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("remove"); s.add_argument("id"); s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("reset"); s.add_argument("id"); s.set_defaults(fn=cmd_reset)
    s = sub.add_parser("check"); s.add_argument("--refresh", action="store_true"); s.set_defaults(fn=cmd_check)

    args = p.parse_args(argv)
    if args.exchange is None:
        cfg = load_json(dpath("config.json"), {}) or {}
        args.exchange = cfg.get("exchange", "nobitex")
    try:
        args.fn(args)
    except RuntimeError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
