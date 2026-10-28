#!/usr/bin/env python3
"""crypto-daily: the once-a-day margin plan. Finds trade candidates, sets a stop-loss
and a take-profit for each, and ranks them: fastest and richest first. Places nothing.

  python3 daily.py plan                                   # defaults: 300,000 toman margin, 2x, top 2
  python3 daily.py plan --margin 300000 --leverage 2 --max-new 2 --exclude BTC ETH
  python3 daily.py plan --quote USDT --margin 15          # tether markets only

Needs crypto/margin_markets.json (run: crypto-margin/margin.py markets) and reads
candles through crypto-ta/ta.py next to this folder. Writes crypto/daily_plan.json.

How a candidate is built
  direction  long  if the daily score >= +min-score, the 4h score > 0 and long is enabled
             short if the daily score <= -min-score, the 4h score < 0 and short is enabled
  entry      the current price (ask for a long, bid for a short)
  stop       just beyond the nearest daily support (long) / resistance (short) when it is
             0.5-2 daily ATR away, else 1.5 daily ATR from the entry
  target     the next daily resistance (long) / support (short) if it gives at least
             min-rr; capped at 3R; with no level in the way, 2R. A level closer than
             min-rr blocks the trade ("wall in the way").
  fees       open + close fee on the position, plus the daily extension fee for the
             estimated days; the reward-to-risk is computed AFTER fees
  safety     the estimated liquidation price must lie at least min-liq-gap % of the
             price beyond the stop, so the stop always fires first
  ranking    speed first (target distance in daily ATRs = rough days), then size
             (net reward-to-risk): rank = net_rr / max(days, 0.5)
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIQ_RATIO = 1.1


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


def load_ta():
    path = os.path.join(HERE, "..", "crypto-ta", "ta.py")
    if not os.path.exists(path):
        sys.exit("error: crypto-ta is not installed next to crypto-daily (%s)" % os.path.normpath(path))
    spec = importlib.util.spec_from_file_location("crypto_ta_for_daily", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fmt(x, q):
    if x is None:
        return "-"
    return "{:,.0f}".format(x) if q == "IRT" else ("{:,.2f}".format(x) if abs(x) >= 100 else "{:,.6g}".format(x))


def est_liq(side, entry, lev):
    if side == "long":
        return None if lev <= 1 else entry * LIQ_RATIO * (1 - 1.0 / lev)
    return entry * (1 + 1.0 / lev) / LIQ_RATIO


def direction(score_1d, score_4h, info, min_score):
    """The entry rule, shared with crypto-backtest: daily and 4h agree, daily is strong, the side is enabled."""
    if score_1d >= min_score and score_4h > 0 and info.get("long"):
        return "long"
    if score_1d <= -min_score and score_4h < 0 and info.get("short"):
        return "short"
    return None


def score_bucket(score):
    a = abs(score)
    return "80+" if a >= 80 else "60-79" if a >= 60 else "40-59" if a >= 40 else "<40"


def chance_for(row, stats):
    """Historical odds for a planned row, from crypto/backtest_stats.json (crypto-backtest)."""
    if not stats:
        return None
    key = "%s|%s" % (row["side"], score_bucket(row["score_1d"]))
    b = (stats.get("buckets") or {}).get(key)
    src = "setup %s" % key
    if not b or b.get("n", 0) < stats.get("min_n", 30):
        b = (stats.get("by_side") or {}).get(row["side"])
        src = "all %ss" % row["side"]
    if not b or not b.get("n"):
        return None
    coin = (stats.get("by_coin") or {}).get("%s%s|%s" % (row["coin"], row["quote"], row["side"]))
    out = {"win_rate": b["win_rate"], "ci_lo": b["ci_lo"], "ci_hi": b["ci_hi"], "n": b["n"], "exp_r": b["exp_r"],
           "exp_r_lo": b.get("exp_r_lo"), "basis": src, "source": stats.get("source"),
           "breakeven": 1.0 / (1.0 + row["net_rr"]), "too_few": b["n"] < stats.get("min_n", 30)}
    if coin and coin.get("n", 0) >= 10:
        out["coin"] = {"n": coin["n"], "win_rate": coin["win_rate"], "exp_r": coin["exp_r"]}
    return out


def margin_for(q, args):
    """--margin is toman; tether markets use --margin-usdt, or the same money converted at the USDT rate."""
    if q == "IRT":
        return args.margin
    mu = getattr(args, "margin_usdt", None)
    if mu:
        return mu
    rate = (load_json(dpath("prices.json"), {}) or {}).get("usdt_irt") or 100000
    return round(args.margin / rate, 2)


def build(coin, q, side, entry, d, args, fee_pct, daily_fee):
    """One candidate from the daily analysis `d`. Returns (row, None) or (None, reason)."""
    atr = d.get("atr")
    if not atr:
        return None, "no daily ATR"
    sup, res = d.get("support"), d.get("resistance")
    if side == "long":
        level = sup if (sup and 0.5 * atr <= entry - sup <= 2 * atr) else None
        stop = level - 0.25 * atr if level else entry - 1.5 * atr
        risk = entry - stop
        wall = res if (res and res > entry) else None
        if wall is not None and (wall - entry) < args.min_rr * risk:
            return None, "resistance %s is in the way (under %.1fR)" % (fmt(wall, q), args.min_rr)
        target = min(wall, entry + 3 * risk) if wall else entry + 2 * risk
        reward = target - entry
    else:
        level = res if (res and 0.5 * atr <= res - entry <= 2 * atr) else None
        stop = level + 0.25 * atr if level else entry + 1.5 * atr
        risk = stop - entry
        wall = sup if (sup and sup < entry) else None
        if wall is not None and (entry - wall) < args.min_rr * risk:
            return None, "support %s is in the way (under %.1fR)" % (fmt(wall, q), args.min_rr)
        target = max(wall, entry - 3 * risk) if wall else entry - 2 * risk
        reward = entry - target
    if risk <= 0 or reward <= 0:
        return None, "no room for a stop and a target"
    margin = margin_for(q, args)
    notional = margin * args.leverage
    qty = notional / entry
    days = reward / atr
    fees = notional * 2 * fee_pct / 100.0 + notional * (daily_fee or 0) * max(1.0, days)
    max_loss = risk * qty + fees
    profit = reward * qty - fees
    if profit <= 0:
        return None, "fees eat the whole target"
    net_rr = profit / max_loss
    if net_rr < args.min_rr:
        return None, "reward-to-risk after fees %.2f is under %.1f" % (net_rr, args.min_rr)
    liq = est_liq(side, entry, args.leverage)
    if liq is not None:
        gap = ((stop - liq) if side == "long" else (liq - stop)) / entry * 100
        if gap < args.min_liq_gap:
            return None, "liquidation %s is only %.1f%% beyond the stop" % (fmt(liq, q), gap)
    if notional < (300000 if q == "IRT" else 11):
        return None, "position %s %s is under the exchange minimum (%s)" % (
            fmt(notional, q), "toman" if q == "IRT" else "USDT", "300,000 toman" if q == "IRT" else "11 USDT")
    row = {"coin": coin, "quote": q, "side": side, "entry": entry, "stop": stop, "take": target,
           "risk_pct": risk / entry * 100, "reward_pct": reward / entry * 100, "net_rr": net_rr, "days": days,
           "liq": liq, "margin": margin, "leverage": args.leverage, "qty": qty,
           "max_loss": max_loss, "profit": profit, "fees": fees,
           "score_1d": d["score"], "stop_basis": "level" if level else "1.5 ATR",
           "rank": net_rr / max(days, 0.5)}
    return row, None


def plan(args, ta=None, now=None):
    ta = ta or load_ta()
    mm = load_json(dpath("margin_markets.json"))
    if not mm or not mm.get("markets"):
        sys.exit("error: no crypto/margin_markets.json; run: python3 crypto-margin/margin.py markets")
    cfg = load_json(dpath("config.json"), {}) or {}
    fee_pct = float(cfg.get("fee_pct", 0.25))
    book = (load_json(dpath("prices.json"), {}) or {}).get("markets") or {}
    exclude = {c.upper() for c in args.exclude or []}
    rows, skipped = [], []
    for key, info in sorted(mm["markets"].items()):
        q = "IRT" if key.endswith("IRT") else "USDT"
        coin = key[: -len(q)]
        if args.quote and q != args.quote.upper():
            continue
        if coin in exclude:
            skipped.append((key, "already open"))
            continue
        if not (info.get("long") or info.get("short")):
            continue
        if args.leverage > (info.get("max_leverage") or 1):
            skipped.append((key, "max leverage %sx" % info.get("max_leverage")))
            continue
        try:
            d = ta.analyze(ta.get_candles(coin, q, "1d", "nobitex"))
            h = ta.analyze(ta.get_candles(coin, q, "4h", "nobitex"))
        except (RuntimeError, SystemExit) as e:
            skipped.append((key, "no candles (%s)" % e))
            continue
        if args.pause:
            time.sleep(args.pause)
        side = direction(d["score"], h["score"], info, args.min_score)
        if not side:
            skipped.append((key, "no clear direction (1d %+d, 4h %+d)" % (d["score"], h["score"])))
            continue
        m = book.get(key) or {}
        entry = (m.get("ask") if side == "long" else m.get("bid")) or m.get("last") or d["price"]
        row, why = build(coin, q, side, entry, d, args, fee_pct, info.get("daily_fee"))
        if why:
            skipped.append((key, "%s: %s" % (side, why)))
            continue
        row["score_4h"] = h["score"]
        row["why"] = "; ".join(r.split(" ", 1)[1] for r in d["reasons"][:2])
        rows.append(row)
    stats = load_json(dpath("backtest_stats.json"))
    for r in rows:
        r["chance"] = chance_for(r, stats)
    rows.sort(key=lambda r: -r["rank"])
    picked = rows[: args.max_new]
    doc = {"created": (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0).isoformat(),
           "settings": {"margin": args.margin, "leverage": args.leverage, "min_rr": args.min_rr,
                        "min_score": args.min_score, "min_liq_gap": args.min_liq_gap,
                        "strategy_source": getattr(args, "strategy_source", "defaults")},
           "backtest": (stats or {}).get("source"),
           "picked": picked, "others": rows[args.max_new:], "skipped": skipped}
    save_json(dpath("daily_plan.json"), doc)
    return doc


STRATEGY_DEFAULTS = {"min_score": 40, "min_rr": 1.8}


def apply_strategy(args):
    """Rule parameters: the command line wins, then crypto/strategy.json (adopted after a
    walk-forward backtest), then the built-in defaults."""
    st = load_json(dpath("strategy.json"), {}) or {}
    used = []
    for k, v in STRATEGY_DEFAULTS.items():
        if getattr(args, k, None) is None:
            if k in (st.get("params") or {}):
                setattr(args, k, st["params"][k])
                used.append(k)
            else:
                setattr(args, k, v)
    args.strategy_source = ("strategy.json (%s)" % st.get("adopted", "")) if used else "defaults"
    return args


def main(argv=None):
    p = argparse.ArgumentParser(description="Once-a-day margin plan with stop-loss and take-profit")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plan")
    s.add_argument("--margin", type=float, default=300000, help="toman per trade on toman markets")
    s.add_argument("--margin-usdt", type=float, default=None, help="USDT per trade on tether markets (default: --margin converted)")
    s.add_argument("--leverage", type=float, default=2)
    s.add_argument("--max-new", type=int, default=2)
    s.add_argument("--min-rr", type=float, default=None)
    s.add_argument("--min-score", type=int, default=None)
    s.add_argument("--min-liq-gap", type=float, default=10)
    s.add_argument("--quote", choices=["IRT", "USDT", "irt", "usdt"])
    s.add_argument("--exclude", nargs="*")
    s.add_argument("--pause", type=float, default=0.4)
    args = p.parse_args(argv)
    apply_strategy(args)
    doc = plan(args)
    if not doc["picked"]:
        print("NO TRADE TODAY: nothing passed every rule")
    for i, r in enumerate(doc["picked"], 1):
        q = r["quote"]
        print("%d) %s/%s %s %gx  entry %s  stop %s (-%.1f%%)  take %s (+%.1f%%)  R:R %.2f  ~%.1f days  liq %s" % (
            i, r["coin"], q, r["side"].upper(), r["leverage"], fmt(r["entry"], q), fmt(r["stop"], q), r["risk_pct"],
            fmt(r["take"], q), r["reward_pct"], r["net_rr"], r["days"], fmt(r["liq"], q)))
        print("   margin %s, max loss %s, profit at target %s (fees %s)  | 1d %+d, 4h %+d | %s" % (
            fmt(r["margin"], q), fmt(r["max_loss"], q), fmt(r["profit"], q), fmt(r["fees"], q),
            r["score_1d"], r["score_4h"], r["why"]))
        c = r.get("chance")
        if c and c.get("too_few"):
            print("   backtest: only %d similar trades in history: too few for reliable odds" % c["n"])
        elif c:
            print("   backtest: closed in profit %.0f%% of the time (90%% range %.0f-%.0f%%, n=%d, %s); "
                  "needs %.0f%% to break even; average %+.2fR" % (
                      c["win_rate"] * 100, c["ci_lo"] * 100, c["ci_hi"] * 100, c["n"], c["basis"],
                      c["breakeven"] * 100, c["exp_r"]))
        else:
            print("   backtest: none yet (run crypto-backtest)")
    if doc["others"]:
        print("also passed, ranked lower: " + ", ".join("%s %s" % (r["coin"], r["side"]) for r in doc["others"]))
    print("rules: min score %s, min R:R %s (%s)" % (args.min_score, args.min_rr, args.strategy_source))
    print("skipped %d markets; details in %s" % (len(doc["skipped"]), dpath("daily_plan.json")))


if __name__ == "__main__":
    main()
