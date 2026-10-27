#!/usr/bin/env python3
"""crypto-ta: technical analysis and a rule-based scanner for crypto.

  python3 ta.py analyze BTC                       # daily, toman market on Nobitex
  python3 ta.py analyze ETH --quote USDT --tf 4h
  python3 ta.py analyze SOL --source binance      # Binance USDT candles (public)
  python3 ta.py scan BTC ETH SOL XRP DOGE TON --rules crypto/ta_rules.json
  python3 ta.py scan BTC ETH SOL --targets 3      # top 3 by score -> crypto/targets.json
  python3 ta.py analyze BTC --file candles.json   # offline: {"t":[],"o":[],"h":[],"l":[],"c":[],"v":[]}

Indicators: SMA/EMA 20-50-200, RSI 14 (Wilder), MACD 12/26/9, Bollinger 20/2,
ATR 14, volume vs its 20-bar average, swing support / resistance, 7- and
30-bar change. The score (-100..+100) is a plain sum of named reasons; it
describes the chart, it does not predict it.

Candles: Nobitex /market/udf/history (toman markets come back in RIAL and are
divided by 10 here) or Binance /api/v3/klines for USDT pairs. Prices on Wallex
track Nobitex closely, so the same candles serve both exchanges.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "crypto-trader-skill/1.0 (+https://github.com/crypto-trader-skill)"
NOBITEX_BASE = os.environ.get("NOBITEX_BASE", "https://apiv2.nobitex.ir")
BINANCE_BASE = os.environ.get("BINANCE_DATA_BASE", "https://data-api.binance.vision")
TF = {  # name: (nobitex resolution, binance interval, seconds)
    "15m": ("15", "15m", 900), "1h": ("60", "1h", 3600), "4h": ("240", "4h", 14400),
    "1d": ("D", "1d", 86400), "d": ("D", "1d", 86400),
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


def fetch(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError("could not fetch %s: %s" % (url, last))


# ---------------------------------------------------------------- candles
def candles_nobitex(coin, quote, tf, bars=300):
    res, _, sec = TF[tf]
    sym = coin.upper() + ("IRT" if quote == "IRT" else "USDT")
    to = int(time.time())
    q = urllib.parse.urlencode({"symbol": sym, "resolution": res, "to": to, "countback": min(bars, 500)})
    d = fetch(NOBITEX_BASE + "/market/udf/history?" + q)
    if d.get("s") != "ok":
        raise RuntimeError("nobitex has no candles for %s (%s)" % (sym, d.get("s") or d.get("errmsg")))
    div = 10.0 if quote == "IRT" else 1.0
    return {"t": list(d["t"]), "o": [x / div for x in d["o"]], "h": [x / div for x in d["h"]],
            "l": [x / div for x in d["l"]], "c": [x / div for x in d["c"]], "v": list(d["v"])}


def candles_binance(coin, tf, bars=300):
    _, interval, _ = TF[tf]
    sym = coin.upper() + "USDT"
    q = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": min(bars, 1000)})
    rows = fetch(BINANCE_BASE + "/api/v3/klines?" + q)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("binance has no candles for %s" % sym)
    return {"t": [int(r[0]) // 1000 for r in rows], "o": [float(r[1]) for r in rows],
            "h": [float(r[2]) for r in rows], "l": [float(r[3]) for r in rows],
            "c": [float(r[4]) for r in rows], "v": [float(r[5]) for r in rows]}


def get_candles(coin, quote, tf, source, file=None):
    if file:
        d = load_json(file)
        if not d:
            sys.exit("error: cannot read %s" % file)
        return d
    if source == "binance":
        if quote != "USDT":
            sys.exit("error: binance candles are USDT pairs; use --quote USDT")
        return candles_binance(coin, tf)
    return candles_nobitex(coin, quote, tf)


# ---------------------------------------------------------------- indicators
def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(xs[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(xs, n=14):
    """Wilder's RSI."""
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = xs[i] - xs[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(xs)):
        ch = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(xs, fast=12, slow=26, signal=9):
    ef, es = ema(xs, fast), ema(xs, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig = [None] * len(xs)
    if start is not None:
        tail = ema(line[start:], signal)
        sig[start:] = tail
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(xs, n=20, k=2.0):
    mid = sma(xs, n)
    up, lo = [None] * len(xs), [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
    return up, mid, lo


def atr(h, l, c, n=14):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out = [None] * len(c)
    if len(c) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(c)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def pivots(h, l, left=3, right=3):
    """Swing highs / lows: a bar higher (lower) than `left` bars before and `right` after."""
    highs, lows = [], []
    for i in range(left, len(h) - right):
        if all(h[i] > h[j] for j in range(i - left, i + right + 1) if j != i):
            highs.append(h[i])
        if all(l[i] < l[j] for j in range(i - left, i + right + 1) if j != i):
            lows.append(l[i])
    return highs, lows


def levels(h, l, price, lookback=120):
    hs, ls = pivots(h[-lookback:], l[-lookback:])
    res = sorted(x for x in hs + ls if x > price)
    sup = sorted((x for x in hs + ls if x < price), reverse=True)
    return (sup[0] if sup else None), (res[0] if res else None)


def pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


# ---------------------------------------------------------------- analysis
def analyze(c):
    close, high, low, vol = c["c"], c["h"], c["l"], c.get("v") or [0] * len(c["c"])
    n = len(close)
    if n < 35:
        raise RuntimeError("need at least 35 candles, got %d" % n)
    e20, e50, e200 = ema(close, 20), ema(close, 50), ema(close, 200)
    r = rsi(close)
    ml, ms, mh = macd(close)
    bu, bm, bl = bollinger(close)
    a = atr(high, low, close)
    vavg = sma(vol, 20)
    price = close[-1]
    sup, res = levels(high, low, price)
    out = {
        "price": price, "bars": n,
        "ema20": e20[-1], "ema50": e50[-1], "ema200": e200[-1],
        "rsi": r[-1], "rsi_prev": r[-2],
        "macd": ml[-1], "macd_signal": ms[-1], "macd_hist": mh[-1], "macd_hist_prev": mh[-2],
        "bb_upper": bu[-1], "bb_mid": bm[-1], "bb_lower": bl[-1],
        "atr": a[-1], "atr_pct": (a[-1] / price * 100) if a[-1] else None,
        "volume_ratio": (vol[-1] / vavg[-1]) if vavg[-1] else None,
        "change_7": pct(price, close[-8]) if n > 7 else None,
        "change_30": pct(price, close[-31]) if n > 30 else None,
        "support": sup, "resistance": res,
    }
    score, reasons = 0, []

    def add(points, why):
        nonlocal score
        score += points
        reasons.append(("%+d " % points) + why)

    if out["ema200"] is not None:
        add(20 if price > out["ema200"] else -20,
            "price %s EMA200 (long-term trend %s)" % ("above" if price > out["ema200"] else "below",
                                                   "up" if price > out["ema200"] else "down"))
    if out["ema50"] is not None:
        add(15 if price > out["ema50"] else -15, "price %s EMA50" % ("above" if price > out["ema50"] else "below"))
    if out["ema20"] is not None and out["ema50"] is not None:
        add(10 if out["ema20"] > out["ema50"] else -10,
            "EMA20 %s EMA50" % ("above" if out["ema20"] > out["ema50"] else "below"))
    rv = out["rsi"]
    if rv is not None:
        if rv >= 70:
            add(-10, "RSI %.0f overbought" % rv)
        elif rv <= 30:
            add(10, "RSI %.0f oversold" % rv)
        elif rv >= 50:
            add(5, "RSI %.0f above 50 (momentum up)" % rv)
        else:
            add(-5, "RSI %.0f below 50 (momentum down)" % rv)
    h, hp = out["macd_hist"], out["macd_hist_prev"]
    if h is not None and hp is not None:
        if hp <= 0 < h:
            add(20, "MACD crossed above its signal (bullish cross)")
        elif hp >= 0 > h:
            add(-20, "MACD crossed below its signal (bearish cross)")
        else:
            add(10 if h > 0 else -10, "MACD %s its signal" % ("above" if h > 0 else "below"))
    if out["bb_upper"] is not None:
        if price > out["bb_upper"]:
            add(-5, "close above the upper Bollinger band (stretched)")
        elif price < out["bb_lower"]:
            add(5, "close below the lower Bollinger band (stretched down)")
    if out["volume_ratio"] is not None and out["volume_ratio"] >= 1.5:
        up = close[-1] >= close[-2]
        add(10 if up else -10, "volume %.1fx its average on a %s bar" % (out["volume_ratio"], "rising" if up else "falling"))
    score = max(-100, min(100, score))
    out["score"] = score
    out["bias"] = "bullish" if score >= 30 else "bearish" if score <= -30 else "neutral"
    out["reasons"] = reasons
    if out["atr"]:
        out["stop_hint"] = price - 2 * out["atr"]
    return out


RULE_KEYS = {
    "min_score": lambda a, v: a["score"] >= v,
    "max_score": lambda a, v: a["score"] <= v,
    "rsi_min": lambda a, v: a["rsi"] is not None and a["rsi"] >= v,
    "rsi_max": lambda a, v: a["rsi"] is not None and a["rsi"] <= v,
    "above_ema200": lambda a, v: a["ema200"] is not None and (a["price"] > a["ema200"]) == bool(v),
    "above_ema50": lambda a, v: a["ema50"] is not None and (a["price"] > a["ema50"]) == bool(v),
    "macd_positive": lambda a, v: a["macd_hist"] is not None and (a["macd_hist"] > 0) == bool(v),
    "min_change_7": lambda a, v: a["change_7"] is not None and a["change_7"] >= v,
    "max_change_7": lambda a, v: a["change_7"] is not None and a["change_7"] <= v,
    "min_change_30": lambda a, v: a["change_30"] is not None and a["change_30"] >= v,
    "max_atr_pct": lambda a, v: a["atr_pct"] is not None and a["atr_pct"] <= v,
    "min_volume_ratio": lambda a, v: a["volume_ratio"] is not None and a["volume_ratio"] >= v,
}


def apply_rules(a, rules):
    fails = []
    for k, v in (rules or {}).items():
        fn = RULE_KEYS.get(k)
        if fn is None:
            raise RuntimeError("unknown rule %r (known: %s)" % (k, ", ".join(sorted(RULE_KEYS))))
        if not fn(a, v):
            fails.append("%s=%s" % (k, v))
    return fails


# ---------------------------------------------------------------- output
def fmt(x, quote):
    if x is None:
        return "-"
    if quote == "IRT":
        return "{:,.0f}".format(x)
    return "{:,.2f}".format(x) if abs(x) >= 100 else "{:,.6g}".format(x)


def print_analysis(coin, quote, tf, a):
    q = quote
    print("%s/%s  %s  %d bars" % (coin, q, tf, a["bars"]))
    print("  price      %s" % fmt(a["price"], q))
    print("  EMA 20/50/200  %s / %s / %s" % (fmt(a["ema20"], q), fmt(a["ema50"], q), fmt(a["ema200"], q)))
    print("  RSI 14     %s" % ("-" if a["rsi"] is None else "%.1f" % a["rsi"]))
    print("  MACD hist  %s" % ("-" if a["macd_hist"] is None else "%.6g" % a["macd_hist"]))
    print("  Bollinger  %s  ..  %s" % (fmt(a["bb_lower"], q), fmt(a["bb_upper"], q)))
    print("  ATR 14     %s (%.2f%% of price)" % (fmt(a["atr"], q), a["atr_pct"] or 0))
    print("  support    %s   resistance %s" % (fmt(a["support"], q), fmt(a["resistance"], q)))
    for k, lab in (("change_7", "7 bars"), ("change_30", "30 bars")):
        if a[k] is not None:
            print("  change %-7s %+.2f%%" % (lab, a[k]))
    print("  SCORE %+d  -> %s" % (a["score"], a["bias"]))
    for r in a["reasons"]:
        print("     " + r)
    if a.get("stop_hint"):
        print("  a 2xATR stop would sit near %s" % fmt(a["stop_hint"], q))


def cmd_analyze(args):
    q = args.quote.upper()
    c = get_candles(args.coin, q, args.tf, args.source, args.file)
    a = analyze(c)
    if args.json:
        print(json.dumps(a, indent=2))
    else:
        print_analysis(args.coin.upper(), q, args.tf, a)


def cmd_scan(args):
    q = args.quote.upper()
    rules = load_json(args.rules, {}) if args.rules else {}
    rows = []
    for coin in args.coins:
        coin = coin.upper()
        try:
            a = analyze(get_candles(coin, q, args.tf, args.source))
        except RuntimeError as e:
            print("skip %s: %s" % (coin, e))
            continue
        fails = apply_rules(a, rules)
        rows.append({"coin": coin, "pass": not fails, "fails": fails, **a})
        time.sleep(args.pause)
    rows.sort(key=lambda r: (not r["pass"], -r["score"]))
    print("%-6s %-5s %6s %7s %9s %9s  %s" % ("coin", "pass", "score", "RSI", "7-bar %", "30-bar %", "why not"))
    for r in rows:
        print("%-6s %-5s %+6d %7s %9s %9s  %s" % (
            r["coin"], "yes" if r["pass"] else "no", r["score"],
            "-" if r["rsi"] is None else "%.1f" % r["rsi"],
            "-" if r["change_7"] is None else "%+.1f" % r["change_7"],
            "-" if r["change_30"] is None else "%+.1f" % r["change_30"],
            ", ".join(r["fails"])))
    save_json(dpath("signals.json"), {"tf": args.tf, "quote": q, "rules": rules, "rows": rows})
    print("wrote %s" % dpath("signals.json"))
    if args.targets:
        picked = [r for r in rows if r["pass"]][:args.targets]
        if not picked:
            print("nothing passed; targets not written")
            return
        w = round(1.0 / len(picked), 6)
        save_json(dpath("targets.json"), {
            "source": "crypto-ta scan (%s, %s)" % (args.tf, ", ".join(r["coin"] for r in picked)),
            "quote": q, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "targets": [{"coin": r["coin"], "weight": w} for r in picked]})
        print("wrote %s: %s, equal weight" % (dpath("targets.json"), ", ".join(r["coin"] for r in picked)))


def main(argv=None):
    p = argparse.ArgumentParser(description="Technical analysis for crypto")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("analyze", "scan"):
        s = sub.add_parser(name)
        if name == "analyze":
            s.add_argument("coin")
            s.add_argument("--file")
            s.add_argument("--json", action="store_true")
            s.set_defaults(fn=cmd_analyze)
        else:
            s.add_argument("coins", nargs="+")
            s.add_argument("--rules")
            s.add_argument("--targets", type=int, default=0)
            s.add_argument("--pause", type=float, default=0.5)
            s.set_defaults(fn=cmd_scan)
        s.add_argument("--quote", default="IRT")
        s.add_argument("--tf", default="1d", choices=sorted(TF))
        s.add_argument("--source", default="nobitex", choices=["nobitex", "binance"])
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except RuntimeError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
