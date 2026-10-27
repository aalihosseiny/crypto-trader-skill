import unittest

from _load import Case, load, market, prices

RB = load("crypto-rebalance", "rebalance.py")


def cfg(**over):
    c = RB.load_config()
    c.update(over)
    return c


class TestRebalance(Case):
    def pr(self):
        return prices(*(market(c, p, min_notional=1) for c, p in (("BTC", 1000), ("ETH", 100), ("SOL", 10), ("DOGE", 1))))

    def test_inside_band_no_trades(self):
        pf = {"cash": 10000, "holdings": {"BTC": 45, "ETH": 450}}        # 45k + 45k + 10k cash = 100k
        r = RB.plan(pf, [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}], self.pr(),
                    cfg(min_order={"IRT": 10}, max_position_pct=100), "IRT", 5)
        self.assertEqual(r["trades"], [])

    def test_drift_sells_first_then_buys(self):
        pf = {"cash": 10000, "holdings": {"BTC": 70, "ETH": 200}}        # BTC 70%, ETH 20%
        r = RB.plan(pf, [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}], self.pr(),
                    cfg(min_order={"IRT": 10}, max_position_pct=100), "IRT", 5)
        self.assertEqual([t["side"] for t in r["trades"]], ["sell", "buy"])
        sell, buy = r["trades"]
        self.assertAlmostEqual(sell["qty"], 25)                          # 70k -> 45k
        self.assertEqual(buy["coin"], "ETH")
        self.assertEqual(buy["notional"], 25000)                         # 20k -> 45k

    def test_untargeted_coin_is_sold(self):
        pf = {"cash": 10000, "holdings": {"BTC": 45, "ETH": 450, "DOGE": 1000}}
        r = RB.plan(pf, [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}], self.pr(),
                    cfg(min_order={"IRT": 10}, max_position_pct=100), "IRT", 5)
        doge = [t for t in r["trades"] if t["coin"] == "DOGE"]
        self.assertEqual(doge[0]["side"], "sell")
        self.assertEqual(doge[0]["qty"], 1000)
        r2 = RB.plan(pf, [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}], self.pr(),
                     cfg(min_order={"IRT": 10}, max_position_pct=100), "IRT", 5, sell_others=False)
        self.assertFalse([t for t in r2["trades"] if t["coin"] == "DOGE"])

    def test_buys_scaled_to_keep_buffer(self):
        pf = {"cash": 20000, "holdings": {"BTC": 80}}                    # 100k equity, 20% cash
        r = RB.plan(pf, [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}], self.pr(),
                    cfg(min_order={"IRT": 10}, max_position_pct=100, cash_buffer_pct=10), "IRT", 1)
        spent = sum(t["notional"] for t in r["trades"] if t["side"] == "buy")
        got = sum(t["notional"] for t in r["trades"] if t["side"] == "sell")
        self.assertGreaterEqual(20000 + got - spent, 10000 - 1)          # buffer kept

    def test_add_new_money(self):
        pf = {"cash": 0, "holdings": {"BTC": 50, "ETH": 500}}
        r = RB.plan(pf, [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}], self.pr(),
                    cfg(min_order={"IRT": 10}, max_position_pct=100, cash_buffer_pct=0), "IRT", 5, add_cash=100000)
        self.assertEqual(sorted(t["side"] for t in r["trades"]), ["buy", "buy"])

    def test_main_writes(self):
        self.write("portfolio.json", {"cash": 10000, "holdings": {"BTC": 70, "ETH": 200}})
        self.write("targets.json", {"targets": [{"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.5}]})
        self.write("prices.json", self.pr())
        self.write("config.json", {"min_order": {"IRT": 10}, "max_position_pct": 100})
        out, code = self.run_main(RB, ["--write"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("orders.json")["source"], "crypto-rebalance")


if __name__ == "__main__":
    unittest.main()
