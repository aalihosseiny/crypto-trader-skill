import datetime as dt
import unittest

from _load import Case, load, market, now_iso, prices

R = load("crypto-risk", "check.py")


def cfg(**over):
    c = R.load_config()
    c.update(over)
    return c


def buy(oid, coin, notional, **kw):
    o = {"id": oid, "coin": coin, "side": "buy", "quote": "IRT", "exchange": "nobitex", "notional": notional, "type": "limit"}
    o.update(kw)
    return o


class TestRisk(Case):
    def base(self):
        return prices(market("BTC", 7000000000), market("ETH", 300000000), market("SOL", 15000000))

    def verdicts(self, doc, c=None, pr=None, pf=None, log=None, now=None):
        R.check(doc, c or cfg(), pr or self.base(), pf or {}, log or {}, now)
        return {o["id"]: (o["risk"]["verdict"], "; ".join(o["risk"]["reasons"])) for o in doc["orders"]}

    def test_budget_batch_passes(self):
        doc = {"quote": "IRT", "budget": 50000000, "orders": [
            buy("o1", "BTC", 12500000), buy("o2", "ETH", 12500000), buy("o3", "SOL", 12000000), ]}
        v = self.verdicts(doc)
        self.assertEqual({k: x[0] for k, x in v.items()}, {"o1": "PASS", "o2": "PASS", "o3": "PASS"})
        self.assertEqual(len(doc["orders"][0]["risk"]["hash"]), 16)

    def test_blocks(self):
        doc = {"quote": "IRT", "budget": 50000000, "orders": [
            buy("o1", "BTC", 20000000),                         # over the 25% cap
            buy("o2", "ETH", 100000),                           # under the minimum
            buy("o3", "SOL", 5000000, leverage=3),              # leverage
            buy("o4", "SOL", 5000000),                          # fine (the o3 duplicate was blocked, not seen?)
        ]}
        v = self.verdicts(doc)
        self.assertEqual(v["o1"][0], "BLOCK"); self.assertIn("cap", v["o1"][1])
        self.assertEqual(v["o2"][0], "BLOCK"); self.assertIn("minimum", v["o2"][1])
        self.assertEqual(v["o3"][0], "BLOCK"); self.assertIn("leverage", v["o3"][1])
        self.assertEqual(v["o4"][0], "BLOCK"); self.assertIn("duplicate", v["o4"][1])

    def test_max_order_and_blocked_list(self):
        doc = {"quote": "IRT", "budget": 10 ** 10, "orders": [buy("o1", "BTC", 200000000), buy("o2", "ETH", 1000000)]}
        v = self.verdicts(doc, cfg(blocked=["ETH"], max_position_pct=100))
        self.assertIn("max order", v["o1"][1])
        self.assertIn("blocked list", v["o2"][1])
        v2 = self.verdicts({"quote": "IRT", "budget": 10 ** 9, "orders": [buy("o1", "ETH", 1000000)]},
                           cfg(allowed=["BTC"], max_position_pct=100))
        self.assertIn("allowed list", v2["o1"][1])

    def test_stale_price_and_spread(self):
        pr = prices(market("BTC", 7000000000, minutes_ago=30), market("ETH", 300000000, bid=290000000, ask=310000000))
        doc = {"quote": "IRT", "budget": 50000000, "orders": [buy("o1", "BTC", 5000000), buy("o2", "ETH", 5000000)]}
        v = self.verdicts(doc, pr=pr)
        self.assertIn("stale", v["o1"][1])
        self.assertIn("spread", v["o2"][1])

    def test_fat_finger_limit(self):
        doc = {"quote": "IRT", "budget": 50000000, "orders": [buy("o1", "BTC", 5000000, limit_price=7700000000)]}
        self.assertIn("away from the market", self.verdicts(doc)["o1"][1])

    def test_cash_buffer(self):
        doc = {"quote": "IRT", "orders": [buy("o1", "BTC", 2500000), buy("o2", "ETH", 2500000), buy("o3", "SOL", 2500000),
                                         buy("o4", "XRP", 2000000)]}
        pr = self.base()
        pr["markets"]["XRPIRT"] = market("XRP", 250000)
        v = self.verdicts(doc, pr=pr, pf={"quote": "IRT", "cash": 10000000, "holdings": {}})
        self.assertEqual([v[k][0] for k in ("o1", "o2", "o3")], ["PASS"] * 3)
        self.assertIn("buffer", v["o4"][1])                    # 10M - 7.5M - 2M = 0.5M < 1M buffer

    def test_sell_more_than_held(self):
        doc = {"quote": "IRT", "orders": [{"id": "r1", "coin": "ETH", "side": "sell", "quote": "IRT", "qty": 2.0,
                                          "notional": 600000000, "exchange": "nobitex"}]}
        v = self.verdicts(doc, c=cfg(max_order={"IRT": 10 ** 12}), pf={"cash": 0, "holdings": {"ETH": 1.0}})
        self.assertIn("you hold", v["r1"][1])

    def test_sells_fund_buys(self):
        doc = {"quote": "IRT", "orders": [
            buy("r2", "SOL", 30000000),
            {"id": "r1", "coin": "ETH", "side": "sell", "quote": "IRT", "qty": 0.5, "notional": 150000000, "exchange": "nobitex"}]}
        c = cfg(max_order={"IRT": 10 ** 12}, max_position_pct=100)
        v = self.verdicts(doc, c=c, pf={"cash": 0, "holdings": {"ETH": 1.0}})
        self.assertEqual(v["r1"][0], "PASS")
        self.assertEqual(v["r2"][0], "PASS")                   # the sell runs first and frees the cash

    def test_daily_limit(self):
        today = now_iso()
        log = {"events": [{"action": "placed", "at": today, "order_id": "x%d" % i} for i in range(20)]}
        doc = {"quote": "IRT", "budget": 50000000, "orders": [buy("o1", "BTC", 5000000)]}
        self.assertIn("daily limit", self.verdicts(doc, log=log)["o1"][1])

    def test_hash_changes_with_order(self):
        o = buy("o1", "BTC", 5000000)
        h = R.order_hash(o)
        o["notional"] = 6000000
        self.assertNotEqual(h, R.order_hash(o))

    def test_main_exit_code(self):
        self.write("orders.json", {"quote": "IRT", "budget": 50000000, "orders": [buy("o1", "BTC", 100)]})
        self.write("prices.json", self.base())
        out, code = self.run_main(R, [])
        self.assertEqual(code, 1)
        self.assertIn("BLOCK", out)
        self.assertEqual(self.read("orders.json")["orders"][0]["risk"]["verdict"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
