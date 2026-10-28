#!/usr/bin/env python3
"""crypto-backtest: replay the crypto-daily rules on history, honestly.

  python3 backtest.py fetch --days 1095                 # download / extend daily + 4h candles (Nobitex)
  python3 backtest.py run                               # the rules as they are, over all history
  python3 backtest.py walkforward                       # out-of-sample test of a small parameter grid
  python3 backtest.py adopt                             # use the walk-forward choice in crypto-daily (only if it earned it)
  python3 backtest.py report                            # the last report again
  python3 backtest.py ensure --auto-adopt               # crypto-daily's first step: refresh only if missing / older than 30 days
  python3 backtest.py odds long BTCIRT 65               # historical odds of one setup (side, market, daily score)

Same rules as crypto-daily, imported from crypto-daily/daily.py (direction, stop,
target, fees, liquidation gap) and crypto-ta/ta.py (indicators), so the test and
the live plan cannot drift apart.

No look-ahead
  A decision at the close of day D sees only daily candles that closed by then and
  4h candles that closed by then. The trade enters at the OPEN of the next 4h bar,
  and exits are walked 4h bar by 4h bar: a bar that touches both the stop and the
  target counts as the STOP (pessimistic); a gap through a level fills at the open;
  a gap through the liquidation price loses the whole margin. Positions expire
  after 30 days at the next open. Fees: open + close + the daily extension fee.

Against overfitting
  * the rules were fixed before any test (crypto-daily defaults), so `run` on the
    defaults is an out-of-sample test of those rules
  * the grid is small on purpose: min score {30, 40, 50} x min R:R {1.5, 1.8, 2.2}
  * walk-forward: choose on a training window (365 days), judge on the next unseen
    window (90 days), slide, repeat; only the unseen windows count
  * a window whose best training result is not positive (with >= 20 trades) sits out
  * a choice is adopted only if, out of sample, it beats the defaults AND its
    neighbours in the grid are positive too (not a lone lucky spike)
  * every number has a range: Wilson 90% interval for the win rate, bootstrap 90%
    interval for the average result in R; under 30 trades it says "too few"
"""
import argparse
import bisect
import hashlib
import csv
import datetime as dt
import importlib.util
import json
import math
import os
import random
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86400
H4 = 14400
EXPIRY_DAYS = 30
GRID = {"min_score": [30, 40, 50], "min_rr": [1.5, 1.8, 2.2]}
DEFAULTS = {"min_score": 40, "min_rr": 1.8, "margin": 300000, "margin_usdt": None, "leverage": 2.0, "max_new": 2,
            "max_open": 3, "min_liq_gap": 10.0}
Z90 = 1.6449


# ---------------------------------------------------------------- plumbing
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
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sibling(folder, script, name):
    path = os.path.join(HERE, "..", folder, script)
    if not os.path.exists(path):
        sys.exit("error: %s is not installed next to crypto-backtest" % folder)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TA = None
DAILY = None


def mods():
    global TA, DAILY
    if TA is None:
        TA = sibling("crypto-ta", "ta.py", "crypto_ta_for_backtest")
    if DAILY is None:
        DAILY = sibling("crypto-daily", "daily.py", "crypto_daily_for_backtest")
    return TA, DAILY


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


def margin_markets():
    mm = load_json(dpath("margin_markets.json"))
    if not mm or not mm.get("markets"):
        sys.exit("error: no crypto/margin_markets.json; run: python3 crypto-margin/margin.py markets")
    return mm["markets"]


def split_key(key):
    q = "IRT" if key.endswith("IRT") else "USDT"
    return key[: -len(q)], q


# ---------------------------------------------------------------- history
def fetch_history(coin, q, tf, days, now=None):
    """Nobitex candles going back `days`, 500 at a time, merged into crypto/history/. Toman for IRT."""
    ta, _ = mods()
    path = dpath("history", "%s%s_%s.json" % (coin, q, tf))
    have = load_json(path, {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []})
    res = ta.TF[tf][0]
    sym = coin.upper() + ("IRT" if q == "IRT" else "USDT")
    div = 10.0 if q == "IRT" else 1.0
    now = int(now or time.time())
    start = now - days * DAY
    rows = {t: (o, h, l, c, v) for t, o, h, l, c, v in zip(have["t"], have["o"], have["h"], have["l"], have["c"], have["v"])}
    to = now
    newest_known = max(rows) if rows else None
    for _ in range(60):
        qs = urllib.parse.urlencode({"symbol": sym, "resolution": res, "to": to, "countback": 500})
        d = ta.fetch(ta.NOBITEX_BASE + "/market/udf/history?" + qs)
        if d.get("s") != "ok" or not d.get("t"):
            break
        for i, t in enumerate(d["t"]):
            rows[int(t)] = (d["o"][i] / div, d["h"][i] / div, d["l"][i] / div, d["c"][i] / div, d["v"][i])
        first = min(int(t) for t in d["t"])
        if first <= start or (newest_known and first <= newest_known and min(rows) <= start):
            break
        to = first - 1
        time.sleep(0.3)
    ts = sorted(t for t in rows if t >= start)
    out = {"t": ts, "o": [rows[t][0] for t in ts], "h": [rows[t][1] for t in ts], "l": [rows[t][2] for t in ts],
           "c": [rows[t][3] for t in ts], "v": [rows[t][4] for t in ts]}
    save_json(path, out)
    return out


def load_history(key, tf):
    return load_json(dpath("history", "%s_%s.json" % (key, tf)))


def slice_c(c, a, b):
    return {k: c[k][a:b] for k in ("t", "o", "h", "l", "c", "v")}


# ---------------------------------------------------------------- signals (param-free, no look-ahead)
def signals_for(D, H, warmup=60, keep_d=400, keep_h=300):
    """One entry per closed day: what crypto-daily would have seen at that day's close."""
    ta, _ = mods()
    out = []
    for i in range(warmup, len(D["t"])):
        close_t = D["t"][i] + DAY
        j = bisect.bisect_right(H["t"], close_t - H4)       # 4h bars that CLOSED by close_t
        if j < 60:
            continue
        try:
            d = ta.analyze(slice_c(D, max(0, i + 1 - keep_d), i + 1))
            h = ta.analyze(slice_c(H, max(0, j - keep_h), j))
        except RuntimeError:
            continue
        out.append({"t": close_t, "score": d["score"], "h": h["score"], "atr": d["atr"], "support": d["support"],
                    "resistance": d["resistance"], "price": d["price"], "reasons": d["reasons"][:2]})
    return out


def rules_version():
    """Changes whenever the indicator code changes, so cached signals are never stale."""
    ta_path = os.path.join(HERE, "..", "crypto-ta", "ta.py")
    with open(ta_path, "rb") as f:
        return hashlib.sha256(f.read() + b"signals-v1").hexdigest()[:12]


def cached_signals(key, D, H):
    fp = "%s|%d|%s|%d|%s" % (rules_version(), len(D["t"]), D["t"][-1], len(H["t"]), H["t"][-1])
    path = dpath("history", "%s_signals.json" % key)
    c = load_json(path)
    if c and c.get("fp") == fp:
        return c["sig"]
    sig = signals_for(D, H)
    save_json(path, {"fp": fp, "sig": sig})
    return sig


def build_universe(keys=None):
    mm = margin_markets()
    uni = {}
    for key, info in sorted(mm.items()):
        if keys and key not in keys:
            continue
        if not (info.get("long") or info.get("short")):
            continue
        D, H = load_history(key, "1d"), load_history(key, "4h")
        if not D or not H or len(D["t"]) < 100:
            continue
        uni[key] = {"info": info, "H": H, "sig": cached_signals(key, D, H)}
    if not uni:
        sys.exit("error: no history; run: python3 crypto-backtest/backtest.py fetch")
    return uni


# ---------------------------------------------------------------- simulation
class Args:
    def __init__(self, p):
        self.__dict__.update(p)


def advance(pos, H, until_t):
    """Walk 4h bars of an open position up to (not including) until_t. Returns True if it exited."""
    side = 1 if pos["side"] == "long" else -1
    n = len(H["t"])
    while pos["idx"] < n and H["t"][pos["idx"]] < until_t:
        i = pos["idx"]
        t, o, hi, lo = H["t"][i], H["o"][i], H["h"][i], H["l"][i]
        if i > pos["entry_idx"] and t >= pos["entry_t"] + EXPIRY_DAYS * DAY:
            return close(pos, t, o, "expired")
        liq = pos.get("liq")
        if liq and ((side > 0 and o <= liq) or (side < 0 and o >= liq)):
            return close(pos, t, liq, "liquidated")
        if (side > 0 and o <= pos["stop"]) or (side < 0 and o >= pos["stop"]):
            return close(pos, t, o, "stop")
        if (side > 0 and o >= pos["take"]) or (side < 0 and o <= pos["take"]):
            return close(pos, t, o, "target")
        if (side > 0 and lo <= pos["stop"]) or (side < 0 and hi >= pos["stop"]):
            return close(pos, t, pos["stop"], "stop")             # both touched in one bar -> stop first
        if (side > 0 and hi >= pos["take"]) or (side < 0 and lo <= pos["take"]):
            return close(pos, t, pos["take"], "target")
        pos["idx"] += 1
    return False


def close(pos, t, price, why):
    side = 1 if pos["side"] == "long" else -1
    days = max((t - pos["entry_t"]) / DAY, 1 / 6)
    notional = pos["notional"]
    fees = notional * 2 * pos["fee_pct"] / 100 + notional * (pos["daily_fee"] or 0) * math.ceil(days)
    if why == "liquidated":
        pnl = -pos["margin"] - notional * pos["fee_pct"] / 100
    else:
        pnl = side * (price - pos["entry"]) * pos["qty"] - fees
    pos.update(exit_t=t, exit=price, outcome=why, days=days, pnl=pnl, fees=fees, r=pnl / pos["risk_amount"])
    return True


def simulate(uni, p, t0=None, t1=None, fee_pct=0.25):
    """Portfolio replay of crypto-daily between t0 and t1 (decision times). Trades opened inside run to their end."""
    _, daily = mods()
    a = Args(p)
    times = sorted({s["t"] for u in uni.values() for s in u["sig"]
                    if (t0 is None or s["t"] >= t0) and (t1 is None or s["t"] < t1)})
    by_t = {k: {s["t"]: s for s in u["sig"]} for k, u in uni.items()}
    open_pos, done = [], []
    for T in times:
        for pos in list(open_pos):
            if advance(pos, uni[pos["key"]]["H"], T):
                open_pos.remove(pos)
                done.append(pos)
        held = {p_["key"] for p_ in open_pos}
        cands = []
        for key, u in uni.items():
            s = by_t[key].get(T)
            if not s or key in held:
                continue
            side = daily.direction(s["score"], s["h"], u["info"], a.min_score)
            if not side or a.leverage > (u["info"].get("max_leverage") or 1):
                continue
            H = u["H"]
            j = bisect.bisect_left(H["t"], T)                  # the first 4h bar that opens at/after the decision
            if j >= len(H["t"]):
                continue
            coin, q = split_key(key)
            entry = H["o"][j]
            row, why = daily.build(coin, q, side, entry, s, a, fee_pct, u["info"].get("daily_fee"))
            if why:
                continue
            cands.append((row, key, j, s))
        cands.sort(key=lambda c: -c[0]["rank"])
        room = min(a.max_new, a.max_open - len(open_pos))
        for row, key, j, s in cands[:max(0, room)]:
            H = uni[key]["H"]
            pos = {"key": key, "side": row["side"], "decided": T, "entry_t": H["t"][j], "entry_idx": j, "idx": j,
                   "entry": row["entry"], "stop": row["stop"], "take": row["take"], "liq": row["liq"],
                   "qty": row["qty"], "notional": row["margin"] * a.leverage, "margin": row["margin"], "fee_pct": fee_pct,
                   "daily_fee": uni[key]["info"].get("daily_fee"), "risk_amount": row["max_loss"],
                   "planned_rr": row["net_rr"], "score_1d": s["score"], "score_4h": s["h"],
                   "bucket": "%s|%s" % (row["side"], daily.score_bucket(s["score"]))}
            open_pos.append(pos)
    for pos in open_pos:                                        # let the last trades finish on later data
        if advance(pos, uni[pos["key"]]["H"], float("inf")):
            done.append(pos)
    return sorted(done, key=lambda x: x["entry_t"])


# ---------------------------------------------------------------- statistics
def wilson(k, n, z=Z90):
    if n == 0:
        return 0.0, 0.0, 0.0
    ph = k / n
    den = 1 + z * z / n
    mid = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return ph, max(0.0, mid - half), min(1.0, mid + half)


def boot_mean(xs, reps=2000, seed=7):
    if not xs:
        return 0.0, 0.0
    rnd = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rnd.randrange(n)] for _ in range(n)) / n for _ in range(reps))
    return means[int(0.05 * reps)], means[int(0.95 * reps) - 1]


def summarize(trades):
    rs = [t["r"] for t in trades]
    n = len(rs)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr, lo, hi = wilson(wins, n)
    gain = sum(r for r in rs if r > 0)
    loss = -sum(r for r in rs if r < 0)
    eq, peak, mdd, streak, worst = 0.0, 0.0, 0.0, 0, 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
    elo, ehi = boot_mean(rs)
    return {"n": n, "wins": wins, "win_rate": wr, "ci_lo": lo, "ci_hi": hi,
            "exp_r": (sum(rs) / n) if n else 0.0, "exp_r_lo": elo, "exp_r_hi": ehi, "total_r": sum(rs),
            "profit_factor": (gain / loss) if loss else None, "max_dd_r": mdd, "worst_streak": worst,
            "avg_days": (sum(t["days"] for t in trades) / n) if n else 0.0,
            "outcomes": {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "expired", "liquidated")},
            "enough": n >= 30}


def stats_doc(trades, source, params):
    groups = {}
    for t in trades:
        groups.setdefault(("buckets", t["bucket"]), []).append(t)
        groups.setdefault(("by_side", t["side"]), []).append(t)
        groups.setdefault(("by_coin", "%s|%s" % (t["key"], t["side"])), []).append(t)
    doc = {"created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "source": source,
           "params": params, "min_n": 30, "overall": summarize(trades), "buckets": {}, "by_side": {}, "by_coin": {}}
    for (kind, k), ts in groups.items():
        doc[kind][k] = summarize(ts)
    return doc


def window_split(times, train_days, test_days):
    if not times:
        return []
    start, end = min(times), max(times)
    out, s = [], start
    while s + (train_days + test_days) * DAY <= end + DAY:
        out.append((s, s + train_days * DAY, s + (train_days + test_days) * DAY))
        s += test_days * DAY
    return out


def grid_params(base):
    for ms in GRID["min_score"]:
        for rr in GRID["min_rr"]:
            p = dict(base)
            p.update(min_score=ms, min_rr=rr)
            yield p


def walkforward(uni, base, train_days=365, test_days=90, min_trades=20, fee_pct=0.25):
    times = sorted({s["t"] for u in uni.values() for s in u["sig"]})
    wins = window_split(times, train_days, test_days)
    if not wins:
        raise RuntimeError("not enough history for one %d+%d-day window; fetch more days" % (train_days, test_days))
    oos, oos_default, log = [], [], []
    for (a, b, c) in wins:
        best, best_exp = None, None
        for p in grid_params(base):
            tr = simulate(uni, p, a, b, fee_pct)
            e = (sum(t["r"] for t in tr) / len(tr)) if len(tr) >= min_trades else None
            if e is not None and (best_exp is None or e > best_exp + 1e-12 or
                                  (abs(e - best_exp) <= 1e-12 and p["min_score"] == base["min_score"] and p["min_rr"] == base["min_rr"])):
                best, best_exp = p, e
        if best is None or best_exp <= 0:
            choice, test_trades = None, []
        else:
            choice = {"min_score": best["min_score"], "min_rr": best["min_rr"]}
            test_trades = simulate(uni, best, b, c, fee_pct)
        dflt = simulate(uni, base, b, c, fee_pct)
        oos += test_trades
        oos_default += dflt
        log.append({"train": [iso(a), iso(b)], "test": [iso(b), iso(c)], "choice": choice,
                    "train_exp_r": best_exp, "test_n": len(test_trades),
                    "test_exp_r": (sum(t["r"] for t in test_trades) / len(test_trades)) if test_trades else None,
                    "default_test_n": len(dflt),
                    "default_test_exp_r": (sum(t["r"] for t in dflt) / len(dflt)) if dflt else None})
    # robustness map on the whole period (in-sample, only to spot a lone lucky cell)
    grid = {}
    for p in grid_params(base):
        tr = simulate(uni, p, None, None, fee_pct)
        grid["%s|%s" % (p["min_score"], p["min_rr"])] = {"n": len(tr), "exp_r": (sum(t["r"] for t in tr) / len(tr)) if tr else None}
    last = next((w["choice"] for w in reversed(log) if w["choice"]), None)
    s_wf, s_def = summarize(oos), summarize(oos_default)
    rec = recommend(last, s_wf, s_def, grid)
    return {"windows": log, "wf": s_wf, "default": s_def, "grid": grid, "last_choice": last,
            "recommendation": rec, "trades": oos, "default_trades": oos_default, "n_combos": len(grid)}


def recommend(last, s_wf, s_def, grid):
    if not last:
        return {"adopt": False, "why": "no window found a positive setting; keep the defaults (or do not trade)"}
    if not s_wf["enough"]:
        return {"adopt": False, "why": "only %d out-of-sample trades; too few to trust" % s_wf["n"]}
    if s_wf["exp_r"] <= s_def["exp_r"] or s_wf["exp_r_lo"] < s_def["exp_r_lo"]:
        return {"adopt": False, "why": "out of sample it did not beat the defaults (%+.2fR vs %+.2fR)" % (s_wf["exp_r"], s_def["exp_r"])}
    ms, rr = last["min_score"], last["min_rr"]
    i, j = GRID["min_score"].index(ms), GRID["min_rr"].index(rr)
    neigh = []
    for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        a, b = i + di, j + dj
        if 0 <= a < 3 and 0 <= b < 3:
            g = grid["%s|%s" % (GRID["min_score"][a], GRID["min_rr"][b])]
            neigh.append(g["exp_r"] is not None and g["exp_r"] > 0)
    if not all(neigh):
        return {"adopt": False, "why": "its neighbours in the grid are not all positive: a lone lucky spot"}
    return {"adopt": True, "why": "beat the defaults out of sample and its neighbours agree", "params": last}


# ---------------------------------------------------------------- output
def pct(x):
    return "%.0f%%" % (x * 100)


def print_summary(title, s):
    print("== %s" % title)
    if not s["n"]:
        print("   no trades")
        return
    print("   trades %d  (%s)" % (s["n"], ", ".join("%s %d" % kv for kv in s["outcomes"].items() if kv[1])))
    print("   closed in profit %s  (90%% range %s-%s)%s" % (pct(s["win_rate"]), pct(s["ci_lo"]), pct(s["ci_hi"]),
                                                          "" if s["enough"] else "   TOO FEW TRADES TO TRUST"))
    print("   average result %+.2fR  (90%% range %+.2f to %+.2f)   total %+.1fR" % (s["exp_r"], s["exp_r_lo"], s["exp_r_hi"], s["total_r"]))
    print("   profit factor %s   worst drawdown %.1fR   worst losing streak %d   average %.1f days" % (
        "-" if s["profit_factor"] is None else "%.2f" % s["profit_factor"], s["max_dd_r"], s["worst_streak"], s["avg_days"]))


def write_trades(trades, name):
    path = dpath(name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["market", "side", "decided", "entry_time", "entry", "stop", "take", "exit_time", "exit", "outcome",
                    "days", "r", "pnl", "score_1d", "score_4h", "planned_rr"])
        eq = 0.0
        for t in trades:
            eq += t["r"]
            w.writerow([t["key"], t["side"], iso(t["decided"]), iso(t["entry_t"]), "%.8g" % t["entry"], "%.8g" % t["stop"],
                        "%.8g" % t["take"], iso(t["exit_t"]), "%.8g" % t["exit"], t["outcome"], "%.1f" % t["days"],
                        "%.3f" % t["r"], "%.0f" % t["pnl"], t["score_1d"], t["score_4h"], "%.2f" % t["planned_rr"]])
    return path


def base_params(args):
    p = dict(DEFAULTS)
    for k in ("margin", "margin_usdt", "leverage", "max_new", "max_open", "min_score", "min_rr"):
        v = getattr(args, k, None)
        if v is not None:
            p[k] = v
    st = load_json(dpath("strategy.json"), {}) or {}
    for k in ("min_score", "min_rr"):
        if getattr(args, k, None) is None and k in (st.get("params") or {}):
            p[k] = st["params"][k]
    return p


def fee_pct():
    return float((load_json(dpath("config.json"), {}) or {}).get("fee_pct", 0.25))


def cmd_fetch(args):
    mm = margin_markets()
    keys = [k for k, i in sorted(mm.items()) if (i.get("long") or i.get("short"))]
    if args.coins:
        keys = [k for k in keys if split_key(k)[0] in {c.upper() for c in args.coins}]
    if args.quote:
        keys = [k for k in keys if k.endswith(args.quote.upper())]
    for k in keys:
        coin, q = split_key(k)
        try:
            d = fetch_history(coin, q, "1d", args.days)
            h = fetch_history(coin, q, "4h", args.days)
            print("%-10s 1d %4d bars from %s   4h %5d bars" % (k, len(d["t"]), iso(d["t"][0]) if d["t"] else "-", len(h["t"])))
        except (RuntimeError, SystemExit) as e:
            print("%-10s skipped: %s" % (k, e))


def cmd_run(args):
    p = base_params(args)
    uni = build_universe()
    trades = simulate(uni, p, None, None, fee_pct())
    st = load_json(dpath("strategy.json"), {}) or {}
    tuned = bool(st.get("params")) and args.min_score is None and args.min_rr is None
    src = "run %s, %s to %s%s" % ("score>=%s R:R>=%s" % (p["min_score"], p["min_rr"]),
                                   iso(trades[0]["entry_t"]) if trades else "-", iso(trades[-1]["entry_t"]) if trades else "-",
                                   " (IN-SAMPLE: these settings were tuned on this data)" if tuned else "")
    s = summarize(trades)
    print_summary("rules as used by crypto-daily: min score %s, min R:R %s, %gx, %d markets" % (
        p["min_score"], p["min_rr"], p["leverage"], len(uni)), s)
    for side in ("long", "short"):
        ts = [t for t in trades if t["side"] == side]
        if ts:
            x = summarize(ts)
            print("   %-5s n=%d  profit %s (%s-%s)  avg %+.2fR" % (side, x["n"], pct(x["win_rate"]), pct(x["ci_lo"]), pct(x["ci_hi"]), x["exp_r"]))
    path = write_trades(trades, "backtest_trades.csv")
    if not tuned:
        save_json(dpath("backtest_stats.json"), stats_doc(trades, src, p))
        print("odds for crypto-daily saved (%s)" % dpath("backtest_stats.json"))
    else:
        print("odds NOT saved: settings tuned on this same data would flatter themselves; walkforward saves honest odds")
    save_json(dpath("backtest_report.json"), {"kind": "run", "source": src, "params": p, "summary": s})
    print("trades: %s" % path)


def cmd_walkforward(args):
    p = base_params(args)
    p.update(min_score=DEFAULTS["min_score"], min_rr=DEFAULTS["min_rr"])   # the a-priori rules are the benchmark
    uni = build_universe()
    r = walkforward(uni, p, args.train_days, args.test_days, args.min_trades, fee_pct())
    print("walk-forward: %d windows, train %d days -> test %d days, grid of %d settings, %d markets" % (
        len(r["windows"]), args.train_days, args.test_days, r["n_combos"], len(uni)))
    for w in r["windows"]:
        print("   test %s..%s  choice %-22s  test n=%-3d %s   defaults n=%-3d %s" % (
            w["test"][0], w["test"][1],
            ("score>=%s R:R>=%s" % (w["choice"]["min_score"], w["choice"]["min_rr"])) if w["choice"] else "sit out",
            w["test_n"], "-" if w["test_exp_r"] is None else "%+.2fR" % w["test_exp_r"],
            w["default_test_n"], "-" if w["default_test_exp_r"] is None else "%+.2fR" % w["default_test_exp_r"]))
    print_summary("OUT OF SAMPLE, walk-forward choices", r["wf"])
    print_summary("OUT OF SAMPLE, fixed defaults (score>=40, R:R>=1.8)", r["default"])
    print("grid over all history (in-sample, robustness only):")
    for ms in GRID["min_score"]:
        print("   score>=%-3s " % ms + "  ".join(
            "R:R %.1f %s" % (rr, "-" if r["grid"]["%s|%s" % (ms, rr)]["exp_r"] is None else "%+.2fR(n=%d)" % (
                r["grid"]["%s|%s" % (ms, rr)]["exp_r"], r["grid"]["%s|%s" % (ms, rr)]["n"])) for rr in GRID["min_rr"]))
    rec = r["recommendation"]
    print("RECOMMENDATION: %s - %s" % ("adopt " + json.dumps(rec.get("params")) if rec["adopt"] else "keep the defaults", rec["why"]))
    use = r["trades"] if rec["adopt"] else r["default_trades"]
    src = "walk-forward out-of-sample, %s" % ("adopted choices" if rec["adopt"] else "fixed defaults")
    save_json(dpath("backtest_stats.json"), stats_doc(use, src, rec.get("params") or {"min_score": 40, "min_rr": 1.8}))
    save_json(dpath("strategy_candidate.json"), {"created": dt.datetime.now(dt.timezone.utc).date().isoformat(),
                                                 "recommendation": rec, "oos": r["wf"], "oos_default": r["default"]})
    save_json(dpath("backtest_report.json"), {"kind": "walkforward", "windows": r["windows"], "wf": r["wf"],
                                              "default": r["default"], "grid": r["grid"], "recommendation": rec})
    write_trades(r["trades"], "backtest_oos_trades.csv")
    print("odds for crypto-daily saved from out-of-sample trades (%s)" % dpath("backtest_stats.json"))


def cmd_adopt(args):
    cand = load_json(dpath("strategy_candidate.json"))
    if not cand:
        sys.exit("run walkforward first")
    rec = cand["recommendation"]
    if not rec.get("adopt") and not args.force:
        sys.exit("not adopted: %s (the defaults stay)" % rec["why"])
    params = rec.get("params") or {"min_score": DEFAULTS["min_score"], "min_rr": DEFAULTS["min_rr"]}
    save_json(dpath("strategy.json"), {"adopted": cand["created"], "params": params, "evidence": cand.get("oos"),
                                       "forced": bool(args.force and not rec.get("adopt"))})
    print("crypto-daily now uses %s" % json.dumps(params))


def verdict(stats):
    """One plain line on whether the rules showed an edge, from a stats doc."""
    o = (stats or {}).get("overall") or {}
    if not o.get("n"):
        return "NO TRADES in the backtest: the rules never fired on this history"
    if not o.get("enough"):
        return "TOO FEW backtest trades (%d) to judge the rules" % o["n"]
    if o["exp_r_lo"] > 0:
        return "EDGE: average %+.2fR per trade, 90%% range %+.2f to %+.2f (n=%d)" % (o["exp_r"], o["exp_r_lo"], o["exp_r_hi"], o["n"])
    if o["exp_r"] > 0:
        return "NO PROVEN EDGE: average %+.2fR but the 90%% range reaches %+.2f (n=%d)" % (o["exp_r"], o["exp_r_lo"], o["n"])
    return "NEGATIVE HISTORY: average %+.2fR per trade (n=%d); trading these rules lost money in the past" % (o["exp_r"], o["n"])


def stats_age_days(stats, now=None):
    t = None
    try:
        t = dt.datetime.fromisoformat((stats or {}).get("created"))
    except (TypeError, ValueError):
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - t).total_seconds() / DAY


def cmd_ensure(args):
    """Make sure crypto-daily has fresh, honest odds: refresh the backtest when missing or old."""
    st = load_json(dpath("backtest_stats.json"))
    age = stats_age_days(st)
    if st and age is not None and age <= args.max_age:
        print("backtest fresh: %.0f days old (%s)" % (age, st.get("source")))
        print("VERDICT: " + verdict(st))
        return
    print("backtest %s: updating (history %d days, walk-forward); this can take a few minutes" % (
        "missing" if not st else "%.0f days old" % age, args.days))
    cmd_fetch(args)
    try:
        cmd_walkforward(args)
    except (RuntimeError, SystemExit) as e:
        print("walk-forward not possible (%s); falling back to a plain run of the default rules" % e)
        for k in ("min_score", "min_rr"):
            setattr(args, k, None)
        if os.path.exists(dpath("strategy.json")):
            os.replace(dpath("strategy.json"), dpath("strategy.retired.json"))
        cmd_run(args)
    cand = load_json(dpath("strategy_candidate.json")) or {}
    rec = cand.get("recommendation") or {}
    if args.auto_adopt and rec.get("adopt"):
        cmd_adopt(argparse.Namespace(force=False))
    elif rec and not rec.get("adopt") and os.path.exists(dpath("strategy.json")):
        os.replace(dpath("strategy.json"), dpath("strategy.retired.json"))
        print("earlier adopted settings no longer earn their place: back to the defaults")
    print("VERDICT: " + verdict(load_json(dpath("backtest_stats.json"))))


def cmd_report(args):
    r = load_json(dpath("backtest_report.json"))
    if not r:
        sys.exit("no report yet")
    if r["kind"] == "run":
        print(r["source"])
        print_summary("rules", r["summary"])
    else:
        print_summary("OUT OF SAMPLE, walk-forward", r["wf"])
        print_summary("OUT OF SAMPLE, fixed defaults", r["default"])
        print("RECOMMENDATION: %s" % r["recommendation"]["why"])


def cmd_odds(args):
    _, daily = mods()
    st = load_json(dpath("backtest_stats.json"))
    if not st:
        sys.exit("no backtest yet")
    coin, q = split_key(args.market.upper())
    row = {"side": args.side, "score_1d": args.score, "coin": coin, "quote": q, "net_rr": args.rr}
    c = daily.chance_for(row, st)
    if not c:
        sys.exit("no comparable trades in the backtest")
    if c.get("too_few"):
        print("only %d comparable trades (%s): too few for reliable odds" % (c["n"], c["basis"]))
        return
    print("%s %s at daily score %+d: closed in profit %s of the time (90%% range %s-%s, n=%d, %s); average %+.2fR; "
          "a %.1fR target needs %s to break even" % (args.side, args.market.upper(), args.score, pct(c["win_rate"]),
                                                    pct(c["ci_lo"]), pct(c["ci_hi"]), c["n"], c["basis"], c["exp_r"],
                                                    args.rr, pct(c["breakeven"])))


def main(argv=None):
    p = argparse.ArgumentParser(description="Backtest the crypto-daily rules without fooling yourself")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fetch"); s.add_argument("--days", type=int, default=1095); s.add_argument("--coins", nargs="*")
    s.add_argument("--quote", choices=["IRT", "USDT", "irt", "usdt"]); s.set_defaults(fn=cmd_fetch)
    for name, fn in (("run", cmd_run), ("walkforward", cmd_walkforward)):
        s = sub.add_parser(name); s.set_defaults(fn=fn)
        s.add_argument("--margin", type=float); s.add_argument("--margin-usdt", type=float)
        s.add_argument("--leverage", type=float); s.add_argument("--max-new", type=int); s.add_argument("--max-open", type=int)
        if name == "run":
            s.add_argument("--min-score", type=int); s.add_argument("--min-rr", type=float)
        else:
            s.add_argument("--train-days", type=int, default=365); s.add_argument("--test-days", type=int, default=90)
            s.add_argument("--min-trades", type=int, default=20)
    s = sub.add_parser("adopt"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_adopt)
    s = sub.add_parser("ensure"); s.set_defaults(fn=cmd_ensure)
    s.add_argument("--max-age", type=float, default=30, help="days before the backtest is refreshed")
    s.add_argument("--auto-adopt", action="store_true", help="apply walk-forward settings when they are recommended")
    s.add_argument("--days", type=int, default=1095); s.add_argument("--coins", nargs="*")
    s.add_argument("--quote", choices=["IRT", "USDT", "irt", "usdt"])
    s.add_argument("--train-days", type=int, default=365); s.add_argument("--test-days", type=int, default=90)
    s.add_argument("--min-trades", type=int, default=20)
    for k in ("margin", "margin_usdt", "leverage", "max_new", "max_open"):
        s.set_defaults(**{k: None})
    s = sub.add_parser("report"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("odds"); s.add_argument("side", choices=["long", "short"]); s.add_argument("market")
    s.add_argument("score", type=int); s.add_argument("--rr", type=float, default=1.8); s.set_defaults(fn=cmd_odds)
    args = p.parse_args(argv)
    for k in ("min_score", "min_rr"):
        if not hasattr(args, k):
            setattr(args, k, None)
    try:
        args.fn(args)
    except RuntimeError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
