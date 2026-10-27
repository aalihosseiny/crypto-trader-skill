import unittest

from _load import Case, load, market, prices

P = load("crypto-pnl", "pnl.py")

FILLS = [
    {"at": "2026-01-01", "coin": "BTC", "side": "buy", "qty": 1.0, "price": 100, "fee": 0, "quote": "IRT"},
    {"at": "2026-02-01", "coin": "BTC", "side": "buy", "qty": 1.0, "price": 200, "fee": 0, "quote": "IRT"},
    {"at": "2026-03-01", "coin": "BTC", "side": "sell", "qty": 1.0, "price": 300, "fee": 0, "quote": "IRT"},
]


class TestLots(Case):
    def test_fifo(self):
        b = P.build(FILLS, "fifo")[("BTC", "IRT")]
        self.assertAlmostEqual(b["realized"], 200)          # sold the 100 lot at 300
        self.assertEqual(P.position(b), (1.0, 200.0))

    def test_hifo(self):
        b = P.build(FILLS, "hifo")[("BTC", "IRT")]
        self.assertAlmostEqual(b["realized"], 100)          # sold the 200 lot
        self.assertEqual(P.position(b), (1.0, 100.0))

    def test_fees(self):
        fills = [{"at": "1", "coin": "ETH", "side": "buy", "qty": 2, "price": 100, "fee": 10, "quote": "USDT"},
                 {"at": "2", "coin": "ETH", "side": "sell", "qty": 1, "price": 150, "fee": 5, "quote": "USDT"}]
        b = P.build(fills)[("ETH", "USDT")]
        self.assertAlmostEqual(b["realized"], (150 - 5) - 105)   # basis is half of 210
        self.assertAlmostEqual(P.position(b)[1], 105)

    def test_partial_lots(self):
        fills = FILLS[:2] + [{"at": "3", "coin": "BTC", "side": "sell", "qty": 1.5, "price": 300, "fee": 0, "quote": "IRT"}]
        b = P.build(fills)[("BTC", "IRT")]
        self.assertAlmostEqual(b["realized"], 450 - 200)          # 100 + half of 200
        self.assertAlmostEqual(P.position(b)[0], 0.5)

    def test_oversold_is_flagged(self):
        fills = [{"at": "1", "coin": "X", "side": "sell", "qty": 1, "price": 10, "fee": 0, "quote": "IRT"}]
        self.assertAlmostEqual(P.build(fills)[("X", "IRT")]["unmatched"], 1)

    def test_whatif(self):
        b = P.build(FILLS[:2])[("BTC", "IRT")]
        r = P.whatif(b, 1.0, 300, 0, "fifo")
        self.assertAlmostEqual(r["pnl"], 200)
        r = P.whatif(b, 1.0, 300, 0, "hifo")
        self.assertAlmostEqual(r["pnl"], 100)
        r = P.whatif(b, 5.0, 300, 0, "fifo")
        self.assertAlmostEqual(r["short"], 3.0)


class TestCommands(Case):
    def test_add_report_sync(self):
        self.run_main(P, ["add", "buy", "BTC", "0.01", "6800000000", "--fee", "0.25%"])
        self.run_main(P, ["add", "buy", "SOL", "3", "180", "--quote", "USDT"])
        f = self.read("fills.json")["fills"]
        self.assertAlmostEqual(f[0]["fee"], 0.01 * 6800000000 * 0.0025)
        self.write("prices.json", prices(market("BTC", 7000000000), market("SOL", 200, quote="USDT")))
        out, code = self.run_main(P, ["report"])
        self.assertEqual(code, 0, out)
        self.assertIn("TOTAL IRT", out)
        self.assertIn("TOTAL USDT", out)
        self.run_main(P, ["cash", "1000000"])
        self.run_main(P, ["sync"])
        pf = self.read("portfolio.json")
        self.assertEqual(pf["holdings"], {"BTC": 0.01})               # the USDT position is not mixed into toman
        self.assertEqual(pf["cash"], 1000000)

    def test_dollar_price_warning(self):
        out, _ = self.run_main(P, ["add", "buy", "ETH", "1", "2500"])
        self.assertIn("dollar price", out)


if __name__ == "__main__":
    unittest.main()
