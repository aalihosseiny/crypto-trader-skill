import math
import os
import random
import unittest

from _load import Case, load, prices

BT = load("crypto-backtest", "backtest.py")
DAY, H4 = 86400, 14400
T0 = 1640995200  # 2022-01-01 00:00 UTC


def make_market(seed, days=520, drift=0.0, vol=0.012, start=1000.0):
    """Synthetic 4h candles with trending regimes, and the matching daily candles."""
    rnd = random.Random(seed)
    H = {k: [] for k in "tohlcv"}
    price = start
    for i in range(days * 6):
        regime = math.sin(i / 300.0) * 0.004 + drift
        o = price
        c = o * (1 + regime / 6 + rnd.gauss(0, vol))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        for k, v in zip("tohlcv", (T0 + i * H4, o, hi, lo, c, 10.0)):
            H[k].append(v)
        price = c
    D = {k: [] for k in "tohlcv"}
    for d in range(days):
        a, b = d * 6, d * 6 + 6
        for k, v in zip("tohlcv", (T0 + d * DAY, H["o"][a], max(H["h"][a:b]), min(H["l"][a:b]), H["c"][b - 1], 60.0)):
            D[k].append(v)
    return D, H


class Base(Case):
    def setUp(self):
        super().setUp()
        self.write("margin_markets.json", {"markets": {
            "AAAIRT": {"long": True, "short": True, "max_leverage": 5, "daily_fee": 0.001},
            "BBBIRT": {"long": True, "short": True, "max_leverage": 5, "daily_fee": 0.001},
            "CCCIRT": {"long": False, "short": True, "max_leverage": 5, "daily_fee": 0.001}}})
        self.write("prices.json", prices(usdt_irt=100000))
        os.makedirs(os.path.join(self.dir, "history"))
        for key, seed in (("AAAIRT", 1), ("BBBIRT", 2), ("CCCIRT", 3)):
            D, H = make_market(seed, start=1e6 * seed)
            self.write("history/%s_1d.json" % key, D)
            self.write("history/%s_4h.json" % key, H)


def pos(side="long", entry=100.0, stop=95.0, take=110.0, liq=None, t=T0):
    return {"key": "X", "side": side, "entry": entry, "stop": stop, "take": take, "liq": liq, "entry_t": t,
            "entry_idx": 0, "idx": 0, "qty": 1.0, "notional": 100.0, "margin": 50.0, "fee_pct": 0.0,
            "daily_fee": 0.0, "risk_amount": 5.0}


def bars(*rows):
    H = {k: [] for k in "tohlcv"}
    for i, (o, h, l, c) in enumerate(rows):
        for k, v in zip("tohlcv", (T0 + i * H4, o, h, l, c, 1)):
            H[k].append(v)
    return H


class TestExits(Base):
    def test_stop_first_when_one_bar_touches_both(self):
        p = pos()
        self.assertTrue(BT.advance(p, bars((100, 111, 94, 105)), float("inf")))
        self.assertEqual(p["outcome"], "stop")
        self.assertAlmostEqual(p["r"], -1.0)

    def test_target(self):
        p = pos()
        BT.advance(p, bars((100, 104, 99, 103), (103, 111, 102, 110)), float("inf"))
        self.assertEqual((p["outcome"], p["exit"]), ("target", 110))
        self.assertAlmostEqual(p["r"], 2.0)

    def test_gap_fills_at_the_open(self):
        p = pos()
        BT.advance(p, bars((100, 101, 99, 100), (90, 91, 88, 89)), float("inf"))
        self.assertEqual((p["outcome"], p["exit"]), ("stop", 90))           # worse than the stop: honest
        p = pos(side="short", stop=105, take=90)
        BT.advance(p, bars((100, 101, 99, 100), (85, 86, 84, 85)), float("inf"))
        self.assertEqual((p["outcome"], p["exit"]), ("target", 85))

    def test_liquidation_on_a_gap(self):
        p = pos(liq=80.0)
        BT.advance(p, bars((100, 101, 99, 100), (70, 71, 69, 70)), float("inf"))
        self.assertEqual(p["outcome"], "liquidated")
        self.assertAlmostEqual(p["pnl"], -50.0)                              # the whole margin

    def test_expiry_after_30_days(self):
        rows = [(100, 101, 99, 100)] * (31 * 6)
        p = pos()
        BT.advance(p, bars(*rows), float("inf"))
        self.assertEqual(p["outcome"], "expired")
        self.assertGreaterEqual(p["days"], 30)

    def test_stops_at_the_decision_time(self):
        p = pos()
        H = bars((100, 101, 99, 100), (100, 101, 99, 100), (100, 120, 99, 115))
        self.assertFalse(BT.advance(p, H, T0 + 2 * H4))                    # the target bar is not reached yet
        self.assertEqual(p["idx"], 2)


class TestNoLookahead(Base):
    def test_future_prices_do_not_change_past_decisions(self):
        D, H = make_market(9)
        before = BT.signals_for(D, H)
        cut = before[len(before) // 2]["t"]
        D2 = {k: list(v) for k, v in D.items()}
        H2 = {k: list(v) for k, v in H.items()}
        for i, t in enumerate(D2["t"]):
            if t + DAY > cut:
                for k in "ohlc":
                    D2[k][i] *= 3                                          # rewrite the future
        for i, t in enumerate(H2["t"]):
            if t + H4 > cut:
                for k in "ohlc":
                    H2[k][i] *= 3
        after = BT.signals_for(D2, H2)
        old = [s for s in before if s["t"] <= cut]
        new = [s for s in after if s["t"] <= cut]
        self.assertEqual(len(old), len(new))
        for a, b in zip(old, new):
            self.assertEqual((a["t"], a["score"], a["h"]), (b["t"], b["score"], b["h"]))

    def test_entry_is_the_next_4h_open(self):
        uni = BT.build_universe()
        trades = BT.simulate(uni, dict(BT.DEFAULTS))
        self.assertTrue(trades)
        for t in trades:
            H = uni[t["key"]]["H"]
            self.assertGreaterEqual(t["entry_t"], t["decided"])
            j = H["t"].index(t["entry_t"])
            self.assertEqual(t["entry"], H["o"][j])


class TestPortfolio(Base):
    def test_limits_and_fields(self):
        uni = BT.build_universe()
        trades = BT.simulate(uni, dict(BT.DEFAULTS))
        events = sorted([(t["entry_t"], 1) for t in trades] + [(t["exit_t"], -1) for t in trades])
        live = peak = 0
        for _, d in events:
            live += d
            peak = max(peak, live)
        self.assertLessEqual(peak, 3)                                        # max_open
        per_day = {}
        for t in trades:
            per_day[t["decided"]] = per_day.get(t["decided"], 0) + 1
        self.assertLessEqual(max(per_day.values()), 2)                       # max_new
        self.assertFalse(any(t["key"] == "CCCIRT" and t["side"] == "long" for t in trades))  # long disabled there
        for t in trades:
            self.assertIn(t["outcome"], ("target", "stop", "expired", "liquidated"))


class TestStats(Base):
    def test_wilson(self):
        p, lo, hi = BT.wilson(50, 100)
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(lo, 1 - hi, places=9)
        self.assertTrue(0.41 < lo < 0.42)
        self.assertEqual(BT.wilson(0, 0), (0.0, 0.0, 0.0))

    def test_bootstrap_is_repeatable_and_brackets_the_mean(self):
        xs = [1, -1, 2, -1, -1, 2.5, -1, 0.5] * 5
        lo, hi = BT.boot_mean(xs)
        self.assertEqual((lo, hi), BT.boot_mean(xs))
        self.assertLess(lo, sum(xs) / len(xs)); self.assertGreater(hi, sum(xs) / len(xs))

    def test_summary(self):
        tr = [{"r": r, "pnl": r, "days": 2, "outcome": "target" if r > 0 else "stop"} for r in [2, -1, -1, 2, -1]]
        s = BT.summarize(tr)
        self.assertEqual((s["n"], s["wins"]), (5, 2))
        self.assertAlmostEqual(s["exp_r"], 0.2)
        self.assertAlmostEqual(s["profit_factor"], 4 / 3)
        self.assertAlmostEqual(s["max_dd_r"], 2.0)
        self.assertFalse(s["enough"])


class TestOverfittingGuards(Base):
    def s(self, n, exp, lo):
        return {"n": n, "enough": n >= 30, "exp_r": exp, "exp_r_lo": lo}

    def grid(self, val=0.1, spike=None):
        g = {"%s|%s" % (a, b): {"n": 50, "exp_r": val} for a in BT.GRID["min_score"] for b in BT.GRID["min_rr"]}
        if spike:
            for k in g:
                g[k]["exp_r"] = -0.2
            g[spike]["exp_r"] = 0.5
        return g

    def test_too_few_trades(self):
        r = BT.recommend({"min_score": 50, "min_rr": 2.2}, self.s(12, 0.5, 0.1), self.s(40, 0.1, -0.1), self.grid())
        self.assertFalse(r["adopt"])

    def test_must_beat_defaults(self):
        r = BT.recommend({"min_score": 50, "min_rr": 2.2}, self.s(40, 0.05, -0.2), self.s(40, 0.1, -0.1), self.grid())
        self.assertFalse(r["adopt"])

    def test_lone_spike_rejected(self):
        r = BT.recommend({"min_score": 30, "min_rr": 1.5}, self.s(60, 0.4, 0.1), self.s(60, 0.0, -0.2),
                         self.grid(spike="30|1.5"))
        self.assertFalse(r["adopt"])
        self.assertIn("neighbours", r["why"])

    def test_robust_winner_adopted(self):
        r = BT.recommend({"min_score": 40, "min_rr": 2.2}, self.s(60, 0.4, 0.1), self.s(60, 0.0, -0.2), self.grid(0.2))
        self.assertTrue(r["adopt"])

    def test_walkforward_runs_only_judges_unseen_windows(self):
        uni = BT.build_universe()
        r = BT.walkforward(uni, dict(BT.DEFAULTS), train_days=200, test_days=60, min_trades=5)
        self.assertTrue(r["windows"])
        for w in r["windows"]:
            self.assertLessEqual(w["train"][1], w["test"][0])               # the test starts after the training
        for t in r["trades"]:
            self.assertGreaterEqual(BT.iso(t["decided"]), r["windows"][0]["test"][0])
        self.assertEqual(r["n_combos"], 9)


class TestCommands(Base):
    def test_run_saves_odds_that_daily_reads(self):
        D = load("crypto-daily", "daily.py")
        out, code = self.run_main(BT, ["run"])
        self.assertEqual(code, 0, out)
        st = self.read("backtest_stats.json")
        self.assertIn("long", st["by_side"])
        row = {"side": "long", "score_1d": 55, "coin": "AAA", "quote": "IRT", "net_rr": 2.0}
        c = D.chance_for(row, st)
        self.assertIsNotNone(c)
        self.assertAlmostEqual(c["breakeven"], 1 / 3)
        self.assertTrue(0 <= c["ci_lo"] <= c["win_rate"] <= c["ci_hi"] <= 1)
        self.assertTrue(self.exists("backtest_trades.csv"))

    def test_too_few_trades_are_flagged_not_quoted(self):
        D = load("crypto-daily", "daily.py")
        st = {"min_n": 30, "buckets": {}, "by_side": {"short": {"n": 3, "win_rate": 0.0, "ci_lo": 0.0, "ci_hi": 0.47,
                                                                 "exp_r": -0.7}}}
        c = D.chance_for({"side": "short", "score_1d": -55, "coin": "X", "quote": "IRT", "net_rr": 2.0}, st)
        self.assertTrue(c["too_few"])

    def test_adopt_only_when_earned(self):
        self.write("strategy_candidate.json", {"created": "2026-10-28", "recommendation": {"adopt": False, "why": "x"}})
        out, code = self.run_main(BT, ["adopt"])
        self.assertNotEqual(code, 0)
        self.assertFalse(self.exists("strategy.json"))
        self.write("strategy_candidate.json", {"created": "2026-10-28", "recommendation": {
            "adopt": True, "why": "ok", "params": {"min_score": 50, "min_rr": 2.2}}})
        self.run_main(BT, ["adopt"])
        D = load("crypto-daily", "daily.py")
        import argparse
        a = D.apply_strategy(argparse.Namespace(min_score=None, min_rr=None))
        self.assertEqual((a.min_score, a.min_rr), (50, 2.2))
        a = D.apply_strategy(argparse.Namespace(min_score=30, min_rr=None))   # the command line still wins
        self.assertEqual((a.min_score, a.min_rr), (30, 2.2))

    def test_run_with_tuned_settings_does_not_save_flattering_odds(self):
        self.write("strategy.json", {"adopted": "x", "params": {"min_score": 50, "min_rr": 2.2}})
        out, _ = self.run_main(BT, ["run"])
        self.assertIn("NOT saved", out)
        self.assertFalse(self.exists("backtest_stats.json"))


class TestCache(Base):
    def test_signals_are_cached_and_invalidated_by_new_data(self):
        BT.build_universe()
        path = os.path.join(self.dir, "history", "AAAIRT_signals.json")
        self.assertTrue(os.path.exists(path))
        D = self.read("history/AAAIRT_1d.json")
        H = self.read("history/AAAIRT_4h.json")
        self.assertEqual(BT.cached_signals("AAAIRT", D, H), self.read("history/AAAIRT_signals.json")["sig"])
        fp1 = self.read("history/AAAIRT_signals.json")["fp"]
        D2 = {k: v[:-1] for k, v in D.items()}
        BT.cached_signals("AAAIRT", D2, H)
        self.assertNotEqual(fp1, self.read("history/AAAIRT_signals.json")["fp"])


class TestFetch(Base):
    def test_pages_backwards_and_converts_rial(self):
        ta, _ = BT.mods()
        calls = []

        def fake(url, **k):
            calls.append(url)
            to = int(url.split("to=")[1].split("&")[0])
            ts = [to - DAY * i for i in range(500, 0, -1)]
            return {"s": "ok", "t": ts, "o": [10.0] * 500, "h": [20.0] * 500, "l": [5.0] * 500, "c": [10.0] * 500, "v": [1] * 500}
        old = ta.fetch
        ta.fetch = fake
        try:
            d = BT.fetch_history("ZZZ", "IRT", "1d", 1200, now=T0 + 2000 * DAY)
        finally:
            ta.fetch = old
        self.assertGreaterEqual(len(calls), 3)                             # 1200 days need three pages of 500
        self.assertEqual(d["c"][0], 1.0)                                    # rial / 10
        self.assertEqual(d["t"], sorted(set(d["t"])))
        self.assertGreaterEqual(len(d["t"]), 1195)


if __name__ == "__main__":
    unittest.main()
