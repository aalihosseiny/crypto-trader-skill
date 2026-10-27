import argparse
import math
import unittest

from _load import Case, load, market, prices

D = load("crypto-daily", "daily.py")
T = load("crypto-ta", "ta.py")


def args(**kw):
    a = dict(margin=300000, leverage=2.0, max_new=2, min_rr=1.8, min_score=40, min_liq_gap=10, quote=None,
             exclude=None, pause=0)
    a.update(kw)
    return argparse.Namespace(**a)


def series(closes):
    return {"t": list(range(len(closes))), "o": closes[:], "h": [c * 1.01 for c in closes],
            "l": [c * 0.99 for c in closes], "c": closes[:], "v": [100.0] * len(closes)}


UP = series([1000 * 1.003 ** i + 25 * math.sin(i / 2.5) for i in range(260)])
DOWN = series([1000 * 0.997 ** i + 6 * math.sin(i / 2.5) for i in range(260)])
FLAT = series([1000 + 20 * math.sin(i / 2.5) for i in range(260)])


class FakeTA:
    """crypto-ta with fixed candles per coin."""
    def __init__(self, data):
        self.data = data
        self.analyze = T.analyze

    def get_candles(self, coin, q, tf, source):
        if coin not in self.data:
            raise RuntimeError("no candles")
        return self.data[coin]


class TestBuild(Case):
    def d(self, price=100.0, atr=5.0, sup=None, res=None, score=60):
        return {"atr": atr, "support": sup, "resistance": res, "score": score, "price": price,
                "reasons": ["+20 a", "+15 b"]}

    def test_long_with_room(self):
        row, why = D.build("BTC", "IRT", "long", 100.0, self.d(sup=94, res=117), args(margin=10 ** 7), 0.25, 0.001)
        self.assertIsNone(why, why)
        self.assertAlmostEqual(row["stop"], 94 - 1.25)                  # just under the support
        self.assertAlmostEqual(row["take"], 117)                        # the next resistance (>= 1.8R after fees, under 3R)
        self.assertGreaterEqual(row["net_rr"], 1.8)
        self.assertAlmostEqual(row["days"], 17 / 5)                      # 17 points of target / 5 ATR

    def test_far_target_is_capped_at_3r(self):
        row, _ = D.build("BTC", "IRT", "long", 100.0, self.d(sup=94, res=130), args(margin=10 ** 7), 0.25, 0.001)
        self.assertAlmostEqual(row["take"], 100 + 3 * (100 - 92.75))

    def test_wall_in_the_way(self):
        row, why = D.build("BTC", "IRT", "long", 100.0, self.d(res=104), args(margin=10 ** 7), 0.25, 0.001)
        self.assertIsNone(row)
        self.assertIn("in the way", why)

    def test_short_mirror(self):
        row, why = D.build("BTC", "IRT", "short", 100.0, self.d(res=106, sup=80, score=-60), args(margin=10 ** 7), 0.25, 0.001)
        self.assertIsNone(why, why)
        self.assertAlmostEqual(row["stop"], 106 + 1.25)
        self.assertAlmostEqual(row["take"], 80)
        self.assertGreater(row["liq"], row["stop"])                     # liquidation beyond the stop

    def test_fees_kill_tiny_targets(self):
        row, why = D.build("X", "IRT", "long", 100.0, self.d(atr=0.2), args(margin=10 ** 7, min_rr=1.8), 2.0, 0.01)
        self.assertIsNone(row)
        self.assertTrue("fees" in why or "after fees" in why)

    def test_liquidation_too_close_at_high_leverage(self):
        row, why = D.build("BTC", "USDT", "long", 100.0, self.d(atr=8), args(margin=50, leverage=5), 0.1, 0.0)
        self.assertIsNone(row)
        self.assertIn("liquidation", why)


class TestPlan(Case):
    def setUp(self):
        super().setUp()
        self.write("margin_markets.json", {"markets": {
            "AAAIRT": {"long": False, "short": True, "max_leverage": 5, "daily_fee": 0.001},
            "BBBUSDT": {"long": True, "short": True, "max_leverage": 5, "daily_fee": 0.001},
            "CCCIRT": {"long": False, "short": True, "max_leverage": 5, "daily_fee": 0.001},
            "DDDIRT": {"long": False, "short": True, "max_leverage": 1, "daily_fee": 0.001}}})
        self.write("prices.json", prices())

    def test_plan_picks_only_clear_enabled_sides(self):
        ta = FakeTA({"AAA": UP, "BBB": UP, "CCC": DOWN, "DDD": DOWN})
        doc = D.plan(args(), ta)
        sides = {(r["coin"], r["side"]) for r in doc["picked"] + doc["others"]}
        self.assertNotIn(("AAA", "long"), sides)                         # long is disabled on AAAIRT
        skipped = dict(doc["skipped"])
        self.assertIn("max leverage", skipped["DDDIRT"])
        for r in doc["picked"]:
            self.assertGreaterEqual(r["net_rr"], 1.8)
            if r["side"] == "long":
                self.assertLess(r["stop"], r["entry"]); self.assertGreater(r["take"], r["entry"])
            else:
                self.assertGreater(r["stop"], r["entry"]); self.assertLess(r["take"], r["entry"])
        self.assertTrue(self.exists("daily_plan.json"))

    def test_flat_market_means_no_trade(self):
        doc = D.plan(args(), FakeTA({"AAA": FLAT, "BBB": FLAT, "CCC": FLAT}))
        self.assertEqual(doc["picked"], [])

    def test_exclude_open_coins_and_rank_order(self):
        doc = D.plan(args(exclude=["CCC"], max_new=5), FakeTA({"AAA": UP, "BBB": UP, "CCC": DOWN}))
        self.assertIn(("CCCIRT", "already open"), doc["skipped"])
        ranks = [r["rank"] for r in doc["picked"]]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_needs_margin_markets(self):
        import os
        os.remove(os.path.join(self.dir, "margin_markets.json"))
        with self.assertRaises(SystemExit):
            D.plan(args(), FakeTA({}))


if __name__ == "__main__":
    unittest.main()
