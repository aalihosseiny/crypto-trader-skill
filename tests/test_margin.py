import os
import unittest

from _load import Case, load, market, prices

M = load("crypto-margin", "margin.py")

MM = {"markets": {"BTCIRT": {"long": False, "short": True, "max_leverage": 5, "daily_fee": 0.001},
                  "BTCUSDT": {"long": True, "short": True, "max_leverage": 5, "daily_fee": 0.001},
                  "DOGEUSDT": {"long": True, "short": True, "max_leverage": 3, "daily_fee": 0.001}}}
POSITION = {  # shape from the Nobitex docs (/positions/list), prices in rial
    "id": 128, "srcCurrency": "btc", "dstCurrency": "rls", "side": "sell", "status": "Open",
    "collateral": "320000000", "leverage": "2", "liquidationPrice": "25174302690", "entryPrice": "6400000000",
    "delegatedAmount": "0.03", "liability": "0.0300450676", "totalAsset": "831712000", "marginRatio": "1.49",
    "unrealizedPNL": "−576435", "unrealizedPNLPercent": "−0.09", "expirationDate": "2022-11-20",
    "extensionFee": "320000", "markPrice": "6430000000"}


def cfg(**over):
    c = M.load_config()
    c.update({"allow_leverage": True})
    c.update(over)
    return c


def open_order(side="short", coin="BTC", q="IRT", margin=500000, lev=2.0):
    return {"id": "m1", "kind": "open", "side": side, "coin": coin, "quote": q, "margin": margin, "leverage": lev,
            "exchange": "nobitex"}


class Base(Case):
    def setUp(self):
        super().setUp()
        os.environ.pop("NOBITEX_TOKEN", None)
        self.calls = []
        self.write("prices.json", prices(market("BTC", 7000000000, bid=6995000000, ask=7005000000),
                                         market("BTC", 67000, bid=66990, ask=67010, quote="USDT"),
                                         market("DOGE", 0.2, quote="USDT")))
        self.write("margin_markets.json", MM)


class TestMath(Base):
    def test_liquidation_estimates(self):
        self.assertIsNone(M.est_liq("long", 100, 1))
        self.assertAlmostEqual(M.est_liq("long", 100, 2), 55)          # 45% drop at 2x
        self.assertAlmostEqual(M.est_liq("long", 100, 5), 88)          # 12% drop at 5x
        self.assertAlmostEqual(M.est_liq("short", 100, 2), 136.3636, places=3)

    def test_docs_example_liquidation(self):
        # the docs' own short: totalAsset / (1.1 x liability) ~ its liquidationPrice
        est = 831712000 / (1.1 * 0.0300450676)
        self.assertLess(abs(est / 25174302690 - 1), 0.001)

    def test_normalize_live_position(self):
        p = M.norm_live(POSITION)
        self.assertEqual((p["side"], p["quote"], p["coin"]), ("short", "IRT", "BTC"))
        self.assertEqual(p["entry"], 640000000)                         # rial -> toman
        self.assertEqual(p["liq"], 2517430269)
        self.assertAlmostEqual(p["upnl"], -57643.5)                     # unicode minus handled
        self.assertEqual(p["liability_str"], "0.0300450676")


class TestRisk(Base):
    def reasons(self, o, c=None, n_open=0, wallet=10 ** 9):
        return "; ".join(M.check_order(o, c or cfg(), MM, n_open, wallet))

    def test_pass(self):
        self.assertEqual(self.reasons(open_order()), "")

    def test_leverage_off_by_default(self):
        c = M.load_config()
        self.assertIn("allow_leverage", self.reasons(open_order(), c))

    def test_side_not_enabled(self):
        self.assertIn("long is not enabled", self.reasons(open_order(side="long")))

    def test_leverage_limits(self):
        self.assertIn("max_leverage", self.reasons(open_order(lev=3)))
        self.assertIn("market maximum", self.reasons(open_order(coin="DOGE", q="USDT", margin=10, lev=4), cfg(max_leverage=5)))
        self.assertIn("1.5", self.reasons(open_order(lev=1.7)))

    def test_size_limits(self):
        self.assertIn("max_margin", self.reasons(open_order(margin=2000000)))
        self.assertIn("minimum order", self.reasons(open_order(margin=100000, lev=2)))

    def test_liquidation_too_close(self):
        r = self.reasons(open_order(side="long", q="USDT", margin=20, lev=5), cfg(max_leverage=5))
        self.assertIn("liquidation", r)                                  # 12% < 15%

    def test_open_positions_and_wallet(self):
        self.assertIn("open positions", self.reasons(open_order(), n_open=3))
        self.assertIn("transfer more", self.reasons(open_order(), wallet=100000))

    def test_stop_must_be_between_price_and_liq(self):
        o = {"kind": "protect", "side": "short", "coin": "BTC", "quote": "IRT", "stop_price": 6900000000, "liq": 9.5e9}
        self.assertIn("above the price", self.reasons(o))
        o["stop_price"] = 9.6e9
        self.assertIn("liquidation", self.reasons(o))
        o["stop_price"] = 7.5e9
        self.assertEqual(self.reasons(o), "")


class TestTicket(Base):
    def test_short_payload_in_rial(self):
        t, prob = M.build_ticket(open_order(), cfg())
        self.assertIsNone(prob)
        p = t["payload"]
        self.assertEqual(t["path"], "/margin/orders/add")
        self.assertEqual((p["type"], p["leverage"], p["dstCurrency"], p["execution"]), ("sell", "2", "rls", "limit"))
        self.assertEqual(t["limit"], 6960025000)                           # bid - 0.5%, toman
        self.assertEqual(p["price"], "69600250000")                        # x10 rial
        self.assertEqual(p["amount"], "0.000143")                          # 1,000,000 / 6.96e9 rounded down
        self.assertIn("liquidation", t["words"])

    def test_close_payload(self):
        o = {"id": "m2", "kind": "close", "position_id": "128", "side": "short", "coin": "BTC", "quote": "IRT",
             "amount": "0.0300450676"}
        t, _ = M.build_ticket(o, cfg())
        self.assertEqual(t["path"], "/positions/128/close")
        self.assertEqual(t["payload"]["amount"], "0.0300450676")           # the whole liability, all 10 digits
        self.assertEqual(t["payload"]["price"], "70400250000")             # buying back: ask + 0.5%


class TestPaperFlow(Base):
    def test_open_mark_close(self):
        self.write("config.json", {"allow_leverage": True})
        self.write("portfolio.json", {"quote": "IRT", "cash": 2000000, "holdings": {}})
        out, code = self.run_main(M, ["transfer", "1000000", "--to", "margin", "--confirm", "1000000"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("portfolio.json")["cash"], 1000000)
        out, _ = self.run_main(M, ["open", "short", "BTC", "--margin", "500000", "--leverage", "2"])
        self.assertIn("PASS m1", out)
        out, code = self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.assertNotEqual(code, 0); self.assertIn("no recorded approval", out)
        self.run_main(M, ["approve", "m1", "--words", "آره شورت بزن"])
        out, code = self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.assertEqual(code, 0, out)
        book = self.read("margin_paper.json")
        pos = book["positions"][0]
        self.assertEqual((pos["id"], pos["side"], pos["status"]), ("p1", "short", "Open"))
        self.assertAlmostEqual(pos["entry"], 6995000000)                   # filled at the bid
        self.assertLess(book["wallet"]["IRT"], 1000000 - 490000)
        out, code = self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.assertNotEqual(code, 0); self.assertIn("already placed", out)
        # price falls 5%: the short is in profit
        pr = self.read("prices.json")
        pr["markets"]["BTCIRT"].update(last=6650000000, bid=6649000000, ask=6651000000)
        self.write("prices.json", pr)
        out, _ = self.run_main(M, ["positions"])
        self.assertIn("p1", out)
        self.run_main(M, ["close", "p1"])
        self.run_main(M, ["approve", "m2", "--words", "ببند"])
        out, code = self.run_main(M, ["place", "m2", "--confirm", "m2"])
        self.assertEqual(code, 0, out)
        pos = self.read("margin_paper.json")["positions"][0]
        self.assertEqual(pos["status"], "Closed")
        self.assertGreater(pos["pnl"], 0)

    def test_liquidation_and_stop_in_paper(self):
        self.write("config.json", {"allow_leverage": True})
        self.write("portfolio.json", {"quote": "IRT", "cash": 2000000, "holdings": {}})
        self.run_main(M, ["transfer", "1500000", "--to", "margin", "--confirm", "1500000"])
        for _ in range(2):
            self.run_main(M, ["open", "short", "BTC", "--margin", "500000", "--leverage", "2"])
        self.run_main(M, ["approve", "m1", "--words", "yes"]); self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.run_main(M, ["approve", "m2", "--words", "yes"]); self.run_main(M, ["place", "m2", "--confirm", "m2"])
        self.run_main(M, ["protect", "p2", "--stop", "7500000000"])
        self.run_main(M, ["approve", "m3", "--words", "yes"])
        out, code = self.run_main(M, ["place", "m3", "--confirm", "m3"])
        self.assertEqual(code, 0, out)
        pr = self.read("prices.json")
        pr["markets"]["BTCIRT"].update(last=9800000000, bid=9799000000, ask=9801000000)   # +40%
        self.write("prices.json", pr)
        self.run_main(M, ["positions", "--all"])
        ps = {p["id"]: p for p in self.read("margin_paper.json")["positions"]}
        self.assertEqual(ps["p1"]["status"], "Liquidated")                # no stop: the whole margin is gone
        self.assertEqual(ps["p2"]["status"], "Stopped")                   # stop at 7.5B fired first
        self.assertGreater(ps["p2"]["pnl"], -ps["p1"]["collateral"])


class TestLive(Base):
    def setUp(self):
        super().setUp()
        self.write("config.json", {"allow_leverage": True, "mode": "live"})

        def fake(method, url, headers=None, body=None, timeout=30):
            self.calls.append((method, url, body))
            if "/positions/list" in url:
                return {"status": "ok", "positions": [POSITION]}
            if "/users/wallets/list" in url:
                return {"status": "ok", "wallets": [{"currency": "rls", "activeBalance": "20000000"}]}
            if url.endswith("/wallets/transfer"):
                return {"status": "ok"}
            return {"status": "ok", "order": {"id": 25, "status": "Active"}}
        M.http = fake

    def tearDown(self):
        os.environ.pop("NOBITEX_TOKEN", None)
        super().tearDown()

    def test_no_token_refuses(self):
        out, code = self.run_main(M, ["open", "short", "BTC", "--margin", "500000", "--leverage", "2"])
        self.assertNotEqual(code, 0)
        self.assertIn("NOBITEX_TOKEN", out)

    def test_open_sends_once(self):
        os.environ["NOBITEX_TOKEN"] = "tok"
        out, _ = self.run_main(M, ["open", "short", "BTC", "--margin", "500000", "--leverage", "2"])
        self.assertIn("PASS", out)                                         # wallet 2,000,000 toman, 1 open position
        self.run_main(M, ["approve", "m1", "--words", "yes"])
        n = len(self.calls)
        out, code = self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.assertEqual(code, 0, out)
        sent = self.calls[n:]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1].endswith("/margin/orders/add"))
        self.assertEqual(sent[0][2]["leverage"], "2")
        out, code = self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.calls), n + 1)
        self.assertNotIn("tok", open(os.path.join(self.dir, "margin_log.json")).read())

    def test_in_flight(self):
        os.environ["NOBITEX_TOKEN"] = "tok"
        self.run_main(M, ["open", "short", "BTC", "--margin", "500000", "--leverage", "2"])
        self.run_main(M, ["approve", "m1", "--words", "yes"])
        good = M.http

        def dies(method, url, headers=None, body=None, timeout=30):
            if "orders/add" in url:
                raise OSError("reset")
            return good(method, url, headers, body, timeout)
        M.http = dies
        with self.assertRaises(OSError):
            M.main(["place", "m1", "--confirm", "m1"])
        out, code = self.run_main(M, ["place", "m1", "--confirm", "m1"])
        self.assertNotEqual(code, 0)
        self.assertIn("IN FLIGHT", out)

    def test_close_uses_exchange_liability(self):
        os.environ["NOBITEX_TOKEN"] = "tok"
        out, _ = self.run_main(M, ["close", "128"])
        self.assertIn("PASS", out)
        o = self.read("margin_orders.json")["orders"][0]
        self.assertEqual(o["amount"], "0.0300450676")

    def test_transfer_in_rial(self):
        os.environ["NOBITEX_TOKEN"] = "tok"
        out, code = self.run_main(M, ["transfer", "1000000", "--to", "margin", "--confirm", "100000"])
        self.assertNotEqual(code, 0)                                        # confirm must repeat the amount
        out, code = self.run_main(M, ["transfer", "1000000", "--to", "margin", "--confirm", "1000000"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls[-1][2], {"currency": "rls", "amount": "10000000", "src": "spot", "dst": "margin"})


if __name__ == "__main__":
    unittest.main()
