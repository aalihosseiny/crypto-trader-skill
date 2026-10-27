import unittest

from _load import Case, load, market, prices

B = load("crypto-budget", "size.py")


class TestSizing(Case):
    def test_cap_fill_redistributes(self):
        alloc, idle = B.cap_fill({"A": 0.7, "B": 0.2, "C": 0.1}, 90, 25)
        self.assertAlmostEqual(alloc["A"], 25)
        self.assertAlmostEqual(alloc["B"], 25)
        self.assertAlmostEqual(alloc["C"], 25)
        self.assertAlmostEqual(idle, 15)                         # all capped -> rest stays cash

    def test_cap_fill_partial(self):
        alloc, idle = B.cap_fill({"A": 0.6, "B": 0.2, "C": 0.2}, 100, 50)
        self.assertAlmostEqual(alloc["A"], 50)
        self.assertAlmostEqual(alloc["B"], 25)
        self.assertAlmostEqual(alloc["C"], 25)
        self.assertAlmostEqual(idle, 0)

    def test_size_example(self):
        targets = [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.25},
                   {"coin": "SOL", "weight": 0.15}, {"coin": "TON", "weight": 0.10}]
        alloc, cash, notes = B.size(targets, 50000000, 10, 25, 300000, "IRT")
        # investable 45M; BTC capped at 12.5M, the 10M excess spread 25:15:10 over the rest
        self.assertEqual(alloc["BTC"], 12500000)
        self.assertEqual(alloc["ETH"], 12500000)                 # 11.25M + 5M*... hits the cap too
        self.assertAlmostEqual(alloc["SOL"] + alloc["TON"], 20000000, delta=1)
        self.assertAlmostEqual(alloc["SOL"] / alloc["TON"], 1.5, places=3)
        self.assertEqual(cash, 50000000 - sum(alloc.values()))
        self.assertAlmostEqual(cash, 5000000, delta=1)
        for v in alloc.values():
            self.assertEqual(v, int(v))                           # whole toman

    def test_fee_is_reserved(self):
        alloc, cash, _ = B.size([{"coin": "BTC", "weight": 1}], 10000000, 10, 100, 300000, "IRT", fee_pct=0.25)
        self.assertGreaterEqual(cash - alloc["BTC"] * 0.0025, 1000000 - 1)   # buffer intact after the fee

    def test_min_order_drops_dust(self):
        targets = [{"coin": "BTC", "weight": 0.98}, {"coin": "PEPE", "weight": 0.02}]
        alloc, cash, notes = B.size(targets, 10000000, 10, 100, 300000, "IRT")
        self.assertNotIn("PEPE", alloc)                           # 2% of 9M = 180k < 300k
        self.assertEqual(alloc["BTC"], 9000000)
        self.assertTrue(any("PEPE dropped" in n for n in notes))

    def test_usdt_rounding(self):
        alloc, cash, _ = B.size([{"coin": "BTC", "weight": 1}], 1000.555, 10, 100, 11, "USDT")
        self.assertAlmostEqual(alloc["BTC"], 900.49, places=2)

    def test_main_writes_proposals(self):
        self.write("targets.json", {"source": "test", "targets": [{"coin": "BTC", "weight": 0.6}, {"coin": "ETH", "weight": 0.4}]})
        self.write("prices.json", prices(market("BTC", 7000000000), market("ETH", 300000000)))
        out, code = self.run_main(B, ["--budget", "20000000", "--cap", "60", "--write"])
        self.assertEqual(code, 0, out)
        doc = self.read("orders.json")
        self.assertEqual([o["coin"] for o in doc["orders"]], ["BTC", "ETH"])
        self.assertTrue(all(o["status"] == "proposed" and o["side"] == "buy" for o in doc["orders"]))
        self.assertEqual(doc["budget"], 20000000)

    def test_warns_when_cap_exceeds_config(self):
        self.write("targets.json", {"targets": [{"coin": "BTC", "weight": 1}]})
        self.write("prices.json", prices(market("BTC", 7e9)))
        out, _ = self.run_main(B, ["--budget", "10000000", "--cap", "60"])
        self.assertIn("WARNING", out)

    def test_exchange_minimum_wins(self):
        self.write("targets.json", {"targets": [{"coin": "BTC", "weight": 0.9}, {"coin": "XRP", "weight": 0.1}]})
        self.write("prices.json", prices(market("BTC", 7e9), market("XRP", 250000, min_notional=2000000)))
        out, code = self.run_main(B, ["--budget", "10000000", "--cap", "100", "--write"])
        self.assertIn("XRP dropped", out)


if __name__ == "__main__":
    unittest.main()
