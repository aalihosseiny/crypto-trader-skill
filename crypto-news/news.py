#!/usr/bin/env python3
"""crypto-news: headlines, market mood and token unlocks for the coins you hold.

  python3 news.py headlines --coins BTC ETH SOL --hours 24   # only what is new
  python3 news.py headlines --all --hours 6                  # every story, not only matches
  python3 news.py sentiment --coins BTC ETH SOL              # Fear & Greed, dominance, funding
  python3 news.py unlocks --days 30                          # from crypto/unlocks.json
  python3 news.py unlocks add ARB 2026-11-16 --pct 2.1 --usd 95000000 --source https://...
  python3 news.py briefing --coins BTC ETH                   # all three, one screen

Sources (public, no key):
  RSS: CoinDesk, Cointelegraph, Decrypt, The Block and ArzDigital (Persian)
  Fear & Greed: api.alternative.me/fng
  Market: api.coingecko.com/api/v3/global (BTC dominance, total cap)
  Funding: Binance fapi premiumIndex, OKX public funding-rate as fallback
Token unlocks have no free, reliable API, so they live in crypto/unlocks.json,
filled by you or by Claude from a web search, every row with its source link.

If --coins is not given, the coins come from crypto/portfolio.json.
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; crypto-trader-skill/1.0)"
FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
    "theblock": "https://www.theblock.co/rss.xml",
    "arzdigital": "https://arzdigital.com/feed/",
}
FNG_URL = "https://api.alternative.me/fng/?limit=8"
GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
BINANCE_FUND = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=%sUSDT"
OKX_FUND = "https://www.okx.com/api/v5/public/funding-rate?instId=%s-USDT-SWAP"

# names a headline may use for a coin (English + Persian)
ALIASES = {  # full names only; the ticker itself is matched case-sensitively (BTC, $BTC)
    "BTC": ["bitcoin", "بیت کوین", "بیت‌کوین", "بیتکوین"],
    "ETH": ["ethereum", "ether", "اتریوم"],
    "USDT": ["tether", "تتر"],
    "BNB": ["binance coin", "بی ان بی"],
    "SOL": ["solana", "سولانا"],
    "XRP": ["ripple", "ریپل"],
    "DOGE": ["dogecoin", "دوج کوین", "دوج‌کوین"],
    "TON": ["toncoin", "the open network", "تون کوین", "تون‌کوین"],
    "ADA": ["cardano", "کاردانو"],
    "TRX": ["tron", "ترون"],
    "AVAX": ["avalanche", "آوالانچ"],
    "SHIB": ["shiba inu", "شیبا"],
    "DOT": ["polkadot", "پولکادات"],
    "LINK": ["chainlink", "چین لینک", "چین‌لینک"],
    "LTC": ["litecoin", "لایت کوین", "لایت‌کوین"],
    "NOT": ["notcoin", "نات کوین", "نات‌کوین"],
    "ARB": ["arbitrum", "آربیتروم"],
    "OP": ["optimism", "اپتیمیزم"],
    "SUI": ["sui network", "سویی"],
    "PEPE": ["pepe", "پپه"],
}
# tags: words that usually move a price
TAGS = [
    ("hack", ["hack", "exploit", "drained", "stolen", "هک"]),
    ("regulation", ["sec ", "lawsuit", "regulator", " ban ", "banned", "sanction", "court", "تحریم", "ممنوع"]),
    ("etf", ["etf"]),
    ("listing", ["listing", "lists ", "delist", "لیست"]),
    ("unlock", ["unlock", "آنلاک"]),
    ("macro", ["fed ", "rate cut", "rate hike", "inflation", "cpi", "fomc", "تورم", "نرخ بهره"]),
    ("liquidation", ["liquidat", "لیکوئید"]),
    ("whale", ["whale", "نهنگ"]),
    ("upgrade", ["upgrade", "hard fork", "mainnet", "halving", "هاوینگ", "ارتقا"]),
]


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


def fetch_raw(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch_raw(url).decode("utf-8"))


# ---------------------------------------------------------------- headlines
def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_date(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def parse_feed(raw, source):
    """RSS 2.0 or Atom -> [{title, link, published, summary, source}]"""
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        items.append({
            "title": strip_html(it.findtext("title")), "link": (it.findtext("link") or "").strip(),
            "published": parse_date(it.findtext("pubDate")),
            "summary": strip_html(it.findtext("description"))[:400], "source": source})
    for it in root.iter(atom + "entry"):
        link = it.find(atom + "link")
        items.append({
            "title": strip_html(it.findtext(atom + "title")),
            "link": link.get("href") if link is not None else "",
            "published": parse_date(it.findtext(atom + "updated") or it.findtext(atom + "published")),
            "summary": strip_html(it.findtext(atom + "summary"))[:400], "source": source})
    return items


def match_coins(text, coins):
    low = text.lower()
    hit = []
    for c in coins:
        if re.search(r"(?<![A-Za-z0-9])\$?" + re.escape(c) + r"(?![A-Za-z0-9])", text):
            hit.append(c)
            continue
        for n in ALIASES.get(c, []):
            found = (re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", low)
                     if n.isascii() else n in low)
            if found:
                hit.append(c)
                break
    return hit


def tag(text):
    low = " " + text.lower() + " "
    return [t for t, words in TAGS if any(w in low for w in words)]


def holdings():
    pf = load_json(dpath("portfolio.json"), {}) or {}
    return sorted(k for k, v in (pf.get("holdings") or {}).items() if (v or 0) > 0)


def collect(feeds, coins, hours, show_all, seen, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=hours)
    out, errors = [], []
    for name, url_or_raw in feeds.items():
        try:
            raw = url_or_raw if isinstance(url_or_raw, (bytes, bytearray)) else fetch_raw(url_or_raw)
        except Exception as e:  # one dead feed must not kill the briefing
            errors.append("%s: %s" % (name, e))
            continue
        for it in parse_feed(raw, name):
            key = it["link"] or it["title"]
            if not key or key in seen:
                continue
            if it["published"] and it["published"] < cutoff:
                continue
            text = it["title"] + " " + it["summary"]
            it["coins"] = match_coins(text, coins)
            it["tags"] = tag(text)
            if it["coins"] or show_all:
                out.append(it)
    out.sort(key=lambda i: i["published"] or cutoff, reverse=True)
    return out, errors


def cmd_headlines(args, quiet_header=False):
    coins = [c.upper() for c in (args.coins or holdings())]
    if not coins and not args.all:
        sys.exit("no coins: pass --coins BTC ETH or fill crypto/portfolio.json (or use --all)")
    seen_doc = load_json(dpath("news_seen.json"), {"seen": []}) or {"seen": []}
    seen = set(seen_doc["seen"])
    feeds = {k: v for k, v in FEEDS.items() if not args.feeds or k in args.feeds}
    items, errors = collect(feeds, coins, args.hours, args.all, seen)
    held = set(holdings())
    if not quiet_header:
        print("== headlines, last %dh, coins: %s" % (args.hours, ", ".join(coins) or "all"))
    if not items:
        print("nothing new")
    for it in items:
        when = it["published"].strftime("%m-%d %H:%M") if it["published"] else "     ?     "
        flag = "*" if set(it["coins"]) & held else " "
        print("%s %s [%s] %s" % (flag, when, it["source"], it["title"]))
        extra = []
        if it["coins"]:
            extra.append("coins: " + ", ".join(it["coins"]))
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        print("      %s  %s" % ("  ".join(extra), it["link"]))
        seen.add(it["link"] or it["title"])
    if errors:
        print("feeds that failed: " + "; ".join(errors))
    if not args.peek:
        seen_doc["seen"] = sorted(seen)[-5000:]
        save_json(dpath("news_seen.json"), seen_doc)
    if held and items:
        print("(* = a coin you hold)")


# ---------------------------------------------------------------- sentiment
def fear_greed():
    d = fetch_json(FNG_URL)
    rows = d.get("data") or []
    return [{"value": int(r["value"]), "label": r["value_classification"],
             "date": dt.datetime.fromtimestamp(int(r["timestamp"]), dt.timezone.utc).date().isoformat()} for r in rows]


def global_market():
    d = (fetch_json(GLOBAL_URL) or {}).get("data") or {}
    mcp = d.get("market_cap_percentage") or {}
    return {"btc_dominance": mcp.get("btc"), "eth_dominance": mcp.get("eth"),
            "total_cap_usd": (d.get("total_market_cap") or {}).get("usd"),
            "cap_change_24h": d.get("market_cap_change_percentage_24h_usd")}


def funding(coin):
    """Latest perpetual funding rate in percent per 8h; Binance first, OKX fallback."""
    try:
        d = fetch_json(BINANCE_FUND % coin)
        if "lastFundingRate" in d:
            return float(d["lastFundingRate"]) * 100, "binance"
    except Exception:
        pass
    try:
        d = fetch_json(OKX_FUND % coin)
        rows = d.get("data") or []
        if rows:
            return float(rows[0]["fundingRate"]) * 100, "okx"
    except Exception:
        pass
    return None, None


def read_fng(v):
    if v <= 24:
        return "extreme fear: crowds are selling; historically a zone where patient buyers accumulate, but it can get worse"
    if v <= 44:
        return "fear"
    if v <= 55:
        return "neutral"
    if v <= 74:
        return "greed"
    return "extreme greed: crowds are euphoric; a common zone for sharp pullbacks"


def read_funding(r):
    if r is None:
        return ""
    if r >= 0.05:
        return "longs crowded (overheated)"
    if r <= -0.02:
        return "shorts crowded (squeeze risk)"
    return "balanced"


def cmd_sentiment(args, quiet_header=False):
    coins = [c.upper() for c in (args.coins or holdings() or ["BTC", "ETH"])]
    if not quiet_header:
        print("== market mood")
    try:
        fg = fear_greed()
        if fg:
            t = fg[0]
            trend = " -> ".join(str(r["value"]) for r in reversed(fg))
            print("Fear & Greed  %d (%s)   last %d days: %s" % (t["value"], t["label"], len(fg), trend))
            print("   " + read_fng(t["value"]))
    except Exception as e:
        print("Fear & Greed unavailable: %s" % e)
    try:
        g = global_market()
        print("BTC dominance %.1f%%   ETH %.1f%%   total cap $%.2fT (%+.2f%% 24h)" % (
            g["btc_dominance"] or 0, g["eth_dominance"] or 0, (g["total_cap_usd"] or 0) / 1e12, g["cap_change_24h"] or 0))
    except Exception as e:
        print("global market data unavailable: %s" % e)
    print("funding (perpetuals, per 8h):")
    for c in coins:
        if c in ("USDT", "USDC", "DAI"):
            continue
        r, src = funding(c)
        if r is None:
            print("   %-6s no perpetual found" % c)
        else:
            print("   %-6s %+.4f%%  %-30s (%s)" % (c, r, read_funding(r), src))
        time.sleep(0.2)


# ---------------------------------------------------------------- unlocks
def load_unlocks():
    return load_json(dpath("unlocks.json"), {"unlocks": []}) or {"unlocks": []}


def upcoming(doc, days, today=None):
    today = today or dt.date.today()
    end = today + dt.timedelta(days=days)
    rows = []
    for u in doc["unlocks"]:
        try:
            d = dt.date.fromisoformat(u["date"])
        except (KeyError, ValueError):
            continue
        if today <= d <= end:
            rows.append(dict(u, days_left=(d - today).days))
    rows.sort(key=lambda r: r["date"])
    return rows


def cmd_unlocks(args, quiet_header=False):
    if getattr(args, "action", None) == "add":
        if not args.coin or not args.date:
            sys.exit("usage: unlocks add COIN YYYY-MM-DD [--pct P] [--usd N] [--source URL] [--note TEXT]")
        dt.date.fromisoformat(args.date)
        doc = load_unlocks()
        doc["unlocks"].append({"coin": args.coin.upper(), "date": args.date, "pct_supply": args.pct,
                               "usd": args.usd, "source": args.source or "", "note": args.note or ""})
        save_json(dpath("unlocks.json"), doc)
        print("added unlock: %s on %s" % (args.coin.upper(), args.date))
        return
    held = set(holdings())
    rows = upcoming(load_unlocks(), args.days)
    if not quiet_header:
        print("== token unlocks, next %d days" % args.days)
    if not rows:
        print("none on file (add them with: news.py unlocks add COIN DATE --pct ... --source URL)")
    for r in rows:
        size = []
        if r.get("pct_supply") is not None:
            size.append("%.2f%% of supply" % r["pct_supply"])
        if r.get("usd"):
            size.append("~$%.1fM" % (r["usd"] / 1e6))
        big = r.get("pct_supply") is not None and r["pct_supply"] >= 1
        print("%s %s  %-6s in %2d days  %s %s%s" % (
            "*" if r["coin"] in held else " ", r["date"], r["coin"], r["days_left"], ", ".join(size),
            "LARGE " if big else "", r.get("note", "")))
        if r.get("source"):
            print("      " + r["source"])


def cmd_briefing(args):
    print("crypto briefing  %s UTC\n" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"))
    cmd_sentiment(args)
    print()
    args.days = 14
    cmd_unlocks(args)
    print()
    args.all = False
    cmd_headlines(args)


def main(argv=None):
    p = argparse.ArgumentParser(description="Crypto news, mood and unlocks")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("headlines"); s.set_defaults(fn=cmd_headlines)
    s.add_argument("--coins", nargs="*"); s.add_argument("--hours", type=int, default=24)
    s.add_argument("--all", action="store_true"); s.add_argument("--feeds", nargs="*")
    s.add_argument("--peek", action="store_true", help="do not mark items as seen")
    s = sub.add_parser("sentiment"); s.add_argument("--coins", nargs="*"); s.set_defaults(fn=cmd_sentiment)
    s = sub.add_parser("unlocks"); s.set_defaults(fn=cmd_unlocks)
    s.add_argument("action", nargs="?", choices=["list", "add"], default="list")
    s.add_argument("coin", nargs="?"); s.add_argument("date", nargs="?")
    s.add_argument("--days", type=int, default=30); s.add_argument("--pct", type=float)
    s.add_argument("--usd", type=float); s.add_argument("--source"); s.add_argument("--note")
    s = sub.add_parser("briefing"); s.set_defaults(fn=cmd_briefing)
    s.add_argument("--coins", nargs="*"); s.add_argument("--hours", type=int, default=24)
    s.add_argument("--feeds", nargs="*"); s.add_argument("--peek", action="store_true")
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
