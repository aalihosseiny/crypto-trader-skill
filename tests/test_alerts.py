import unittest

from _load import Case, load, market, prices

A = load("crypto-alerts", "alerts.py")

NOBITEX_STATS = {  # shape from the Nobitex API docs; prices in RIAL
    "status": "ok",
    "stats": {
        "btc-rls": {"isClosed": False, "bestSell": "70500000000", "bestBuy": "70400000000", "volumeSrc": "12.5",
                    "latest": "70450000000", "dayLow": "69000000000", "dayHigh": "71000000000",
                    "dayOpen": "69500000000", "dayChange": "1.37"},
        "usdt-rls": {"isClosed": False, "bestSell": "1051000", "bestBuy": "1049000", "latest": "1050000",
                     "dayLow": "1040000", "dayHigh": "1060000", "dayOpen": "1045000", "dayChange": "0.48"},
        "eth-usdt": {"isClosed": False, "latest": "2500", "bestSell": "2501", "bestBuy": "2499", "dayChange": "-2"},
        "dead-rls": {"isClosed": True, "latest": "5"},
    },
}
WALLEX_MARKETS = {  # shape from Wallex /v1/markets; TMN prices in TOMAN
    "success": True,
    "result": {"symbols": {
        "BTCTMN": {"symbol": "BTCTMN", "baseAsset": "BTC", "quoteAsset": "TMN", "stepSize": 6, "minNotional": 100000,
                   "stats": {"lastPrice": "7040000000", "bidPrice": "7039000000", "askPrice": "7041000000",
                             "24h_ch": 1.2, "24h_highPrice": "7100000000", "24h_lowPrice": "6900000000", "24h_volume": "3.1"}},
        "BTCUSDT": {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "stepSize": 6, "minNotional": 5,
                    "stats": {"lastPrice": "67000", "bidPrice": "66990", "askPrice": "67010", "24h_ch": 1.0}},
    }},
}


class TestExchanges(Case):
    def test_nobitex_rial_becomes_toman(self):
        A.fetch = lambda url, **k: NOBITEX_STATS
        m = A.nobitex_markets(["BTC", "USDT"], "IRT")
        self.assertEqual(m["BTCIRT"]["last"], 7045000000)        # 70,450,000,000 rial / 10
        self.assertEqual(m["BTCIRT"]["ask"], 7050000000)         # bestSell is the ask
        self.assertEqual(m["BTCIRT"]["bid"], 7040000000)
        self.assertEqual(m["BTCIRT"]["min_notional"], 300000)    # 3,000,000 rial
        self.assertEqual(m["USDTIRT"]["last"], 105000)
        self.assertNotIn("ETHIRT", m)                             # a USDT market is not a toman market
        self.assertNotIn("DEADIRT", m)                            # closed markets are skipped

    def test_nobitex_usdt_markets_unchanged(self):
        A.fetch = lambda url, **k: NOBITEX_STATS
        m = A.nobitex_markets(["ETH"], "USDT")
        self.assertEqual(m["ETHUSDT"]["last"], 2500)
        self.assertEqual(m["ETHUSDT"]["min_notional"], 11)

    def test_wallex_toman(self):
        A.fetch = lambda url, **k: WALLEX_MARKETS
        m = A.wallex_markets(["BTC"], "IRT")
        self.assertEqual(list(m), ["BTCIRT"])
        self.assertEqual(m["BTCIRT"]["last"], 7040000000)
        self.assertEqual(m["BTCIRT"]["symbol"], "BTCTMN")
        self.assertAlmostEqual(m["BTCIRT"]["qty_step"], 1e-6)
        self.assertEqual(m["BTCIRT"]["min_notional"], 100000)

    def test_prices_command_writes_book_with_usdt_rate(self):
        A.fetch = lambda url, **k: NOBITEX_STATS
        out, code = self.run_main(A, ["--exchange", "nobitex", "prices", "BTC"])
        self.assertEqual(code, 0, out)
        book = self.read("prices.json")
        self.assertIn("BTCIRT", book["markets"])
        self.assertEqual(book["usdt_irt"], 105000)
        self.assertIn("updated", book["markets"]["BTCIRT"])


class TestAlerts(Case):
    def test_rings_once(self):
        self.write("prices.json", prices(market("BTC", 7000000000), market("ETH", 300000000, change=-6.5)))
        self.run_main(A, ["--exchange", "nobitex", "add", "BTC", "above", "6900000000"])
        self.run_main(A, ["--exchange", "nobitex", "add", "BTC", "below", "6000000000"])
        self.run_main(A, ["--exchange", "nobitex", "add", "ETH", "move", "5"])
        out, _ = self.run_main(A, ["--exchange", "nobitex", "check"])
        self.assertIn("RING a1", out)
        self.assertNotIn("RING a2", out)
        self.assertIn("RING a3", out)
        out2, _ = self.run_main(A, ["--exchange", "nobitex", "check"])
        self.assertNotIn("RING a1", out2)                         # once only
        self.run_main(A, ["--exchange", "nobitex", "reset", "a1"])
        out3, _ = self.run_main(A, ["--exchange", "nobitex", "check"])
        self.assertIn("RING a1", out3)

    def test_stop(self):
        msg = A.evaluate({"market": "SOLIRT", "kind": "stop", "level": 9000000, "quote": "IRT"},
                         {"last": 8900000})
        self.assertIn("STOP", msg)
        self.assertIsNone(A.evaluate({"market": "SOLIRT", "kind": "stop", "level": 9000000}, {"last": 9100000}))

    def test_bad_kind(self):
        out, code = self.run_main(A, ["--exchange", "nobitex", "add", "BTC", "sideways", "1"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
