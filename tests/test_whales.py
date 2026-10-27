import unittest

from _load import Case, load, market, prices

W = load("crypto-whales", "whales.py")

VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
ETHPLORER = {  # shape of api.ethplorer.io/getAddressInfo
    "address": VITALIK.lower(),
    "ETH": {"balance": 100.0, "price": {"rate": 3000}},
    "tokens": [
        {"tokenInfo": {"symbol": "WETH", "decimals": "18", "price": {"rate": 3000}, "address": "0xw"},
         "balance": 5e19, "rawBalance": "50000000000000000000"},
        {"tokenInfo": {"symbol": "USDC", "decimals": "6", "price": {"rate": 1}, "address": "0xu"},
         "balance": 2e11, "rawBalance": "200000000000"},
        {"tokenInfo": {"symbol": "UNI", "decimals": "18", "price": {"rate": 10}, "address": "0xn"},
         "rawBalance": "10000000000000000000000"},
        {"tokenInfo": {"symbol": "SPAM", "decimals": "18", "price": False, "address": "0xs"},
         "rawBalance": "999000000000000000000000"},
    ],
}


class TestChain(Case):
    def test_chain_of(self):
        self.assertEqual(W.chain_of(VITALIK), "eth")
        self.assertEqual(W.chain_of("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"), "btc")
        self.assertEqual(W.chain_of("1A1zP1eP5QGefi2DMPTfTL5SNcCPqyBDDw"), "btc")
        with self.assertRaises(RuntimeError):
            W.chain_of("hello")

    def test_parse_ethplorer(self):
        h = W.parse_ethplorer(VITALIK, ETHPLORER)
        a = h["assets"]
        self.assertAlmostEqual(a["ETH"]["qty"], 150.0)        # 100 ETH + 50 WETH
        self.assertAlmostEqual(a["ETH"]["usd"], 450000.0)
        self.assertAlmostEqual(a["USDC"]["qty"], 200000.0)
        self.assertAlmostEqual(a["UNI"]["usd"], 100000.0)
        self.assertEqual(a["SPAM"]["usd"], 0.0)                # no price -> no value
        self.assertAlmostEqual(h["total_usd"], 750000.0)

    def test_btc(self):
        def fake(url, **k):
            if url.endswith("/v1/prices"):
                return {"USD": 60000}
            return {"chain_stats": {"funded_txo_sum": 300000000, "spent_txo_sum": 100000000, "tx_count": 4},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0}}
        W.fetch = fake
        h = W.btc_holdings("1A1zP1eP5QGefi2DMPTfTL5SNcCPqyBDDw")
        self.assertAlmostEqual(h["assets"]["BTC"]["qty"], 2.0)
        self.assertAlmostEqual(h["total_usd"], 120000.0)


class TestCopy(Case):
    def test_weights_drop_stables_and_untradable(self):
        h = W.parse_ethplorer(VITALIK, ETHPLORER)
        t, skipped = W.weights([h], top=5, tradable={"ETH", "BTC"})
        self.assertEqual([x["coin"] for x in t], ["ETH"])
        self.assertEqual(t[0]["weight"], 1.0)
        self.assertIn("UNI", skipped)
        t2, _ = W.weights([h], top=5)
        self.assertEqual([x["coin"] for x in t2], ["ETH", "UNI"])
        self.assertAlmostEqual(t2[0]["weight"], 450 / 550, places=5)
        t3, _ = W.weights([h], top=5, keep_stables=True)
        self.assertIn("USDC", [x["coin"] for x in t3])

    def test_blend_two_wallets_equally(self):
        a = {"assets": {"ETH": {"qty": 1, "usd": 900, "price": 900}, "UNI": {"qty": 10, "usd": 100, "price": 10}}}
        b = {"assets": {"BTC": {"qty": 1, "usd": 1e6, "price": 1e6}}}
        t, _ = W.weights([a, b], top=5)
        w = {x["coin"]: x["weight"] for x in t}
        self.assertAlmostEqual(w["BTC"], 0.5)                  # each wallet counts the same, not by size
        self.assertAlmostEqual(w["ETH"], 0.45)

    def test_diff(self):
        old = {"assets": {"ETH": {"qty": 10, "price": 3000}, "UNI": {"qty": 100, "price": 10}, "OLD": {"qty": 5, "price": 1e6}}}
        new = {"assets": {"ETH": {"qty": 20, "price": 3000}, "UNI": {"qty": 50, "price": 10}, "NEWC": {"qty": 1, "price": 2e6}}}
        d = {r["coin"]: r for r in W.diff(old, new)}
        self.assertEqual(d["ETH"]["kind"], "added")
        self.assertEqual(d["UNI"]["kind"], "trimmed")
        self.assertEqual(d["OLD"]["kind"], "SOLD OUT")
        self.assertEqual(d["NEWC"]["kind"], "NEW")
        big = W.diff(old, new, min_usd=10000)
        self.assertNotIn("UNI", [r["coin"] for r in big])      # a $500 trim is noise here

    def test_copy_command(self):
        W.fetch = lambda url, **k: ETHPLORER
        self.write("prices.json", prices(market("ETH", 300000000), market("UNI", 1000000), market("BTC", 7e9)))
        out, code = self.run_main(W, ["copy", VITALIK, "--top", "5", "--pause", "0"])
        self.assertEqual(code, 0, out)
        t = self.read("targets.json")
        self.assertEqual([x["coin"] for x in t["targets"]], ["ETH", "UNI"])
        self.assertTrue(t["source"].startswith("crypto-whales"))

    def test_watch_and_check(self):
        state = {"n": 0}

        def fake(url, **k):
            state["n"] += 1
            d = dict(ETHPLORER)
            d["ETH"] = {"balance": 100.0 + (50 if state["n"] > 1 else 0), "price": {"rate": 3000}}
            return d
        W.fetch = fake
        self.run_main(W, ["watch", "add", VITALIK, "--label", "vitalik"])
        out1, _ = self.run_main(W, ["check", "--pause", "0"])
        self.assertIn("first snapshot", out1)
        out2, _ = self.run_main(W, ["check", "--pause", "0", "--min-usd", "1000"])
        self.assertIn("added", out2)
        self.assertIn("ETH", out2)


if __name__ == "__main__":
    unittest.main()
