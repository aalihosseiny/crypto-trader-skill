import datetime as dt
import os
import unittest

from _load import Case, load, market, now_iso, prices

K = load("crypto-broker", "broker.py")
R = load("crypto-risk", "check.py")


class Base(Case):
    def setUp(self):
        super().setUp()
        for v in ("NOBITEX_TOKEN", "NOBITEX_TESTNET_TOKEN", "WALLEX_API_KEY"):
            os.environ.pop(v, None)
        self.calls = []

        def fake_http(method, url, headers=None, body=None, timeout=30):
            self.calls.append((method, url, headers, body))
            return {"status": "ok", "order": {"id": 991, "status": "Active"}}
        K.http = fake_http

    def setup_orders(self, exchange="nobitex", mode="paper", orders=None, pf=None):
        self.write("config.json", {"exchange": exchange, "mode": mode})
        self.write("prices.json", prices(market("BTC", 7000000000, bid=6995000000, ask=7005000000),
                                         market("ETH", 300000000), market("DOGE", 20000)))
        doc = {"quote": "IRT", "budget": 50000000, "source": "test", "orders": orders or [
            {"id": "o1", "coin": "BTC", "side": "buy", "quote": "IRT", "exchange": exchange, "notional": 10000000, "type": "limit"},
            {"id": "o2", "coin": "ETH", "side": "buy", "quote": "IRT", "exchange": exchange, "notional": 5000000, "type": "limit"}]}
        R.check(doc, R.load_config(), self.read("prices.json"), pf or {}, {})
        self.write("orders.json", doc)
        return doc


class TestTickets(Base):
    def test_nobitex_payload_is_rial(self):
        self.setup_orders()
        cfg = K.load_config()
        o = self.read("orders.json")["orders"][0]
        t, prob = K.build_ticket(o, cfg, self.read("prices.json"))
        self.assertIsNone(prob)
        self.assertEqual(t["limit"], 7040025000)                # ask 7,005,000,000 + 0.5% (toman)
        p = t["payload"]
        self.assertEqual(p["price"], "70400250000")             # x10 -> rial
        self.assertEqual(p["dstCurrency"], "rls")
        self.assertEqual(p["srcCurrency"], "btc")
        self.assertEqual(p["type"], "buy")
        self.assertEqual(p["execution"], "limit")
        self.assertEqual(p["amount"], "0.001420")               # 10M / 7.04B, 6 decimals, rounded DOWN
        self.assertLessEqual(len(p["clientOrderId"]), 32)
        self.assertLessEqual(t["value"], 10000000)

    def test_wallex_payload_is_toman(self):
        self.setup_orders(exchange="wallex")
        o = self.read("orders.json")["orders"][0]
        t, _ = K.build_ticket(o, K.load_config(), self.read("prices.json"))
        self.assertEqual(t["payload"]["symbol"], "BTCTMN")
        self.assertEqual(t["payload"]["price"], "7040025000")
        self.assertEqual(t["payload"]["side"], "BUY")
        self.assertEqual(t["payload"]["type"], "LIMIT")
        self.assertEqual(t["path"], "/v1/account/orders")

    def test_sell_guard_below_bid(self):
        self.write("config.json", {})
        o = {"id": "r1", "coin": "BTC", "side": "sell", "quote": "IRT", "qty": 0.0123456789, "exchange": "nobitex"}
        t, _ = K.build_ticket(o, K.load_config(), prices(market("BTC", 7e9, bid=6995000000, ask=7005000000)))
        self.assertEqual(t["limit"], 6960025000)                # bid - 0.5%
        self.assertEqual(t["payload"]["amount"], "0.012345")

    def test_cheap_coin_whole_units(self):
        self.write("config.json", {})
        o = {"id": "o9", "coin": "DOGE", "side": "buy", "quote": "IRT", "notional": 1000000, "exchange": "nobitex"}
        t, _ = K.build_ticket(o, K.load_config(), prices(market("DOGE", 20000), usdt_irt=100000))
        self.assertEqual(t["payload"]["amount"], "49.70")       # 0.2 USD a coin -> 2 decimals; 1M / 20,120

    def test_qty_override(self):
        self.write("config.json", {"qty_decimals": {"BTC": 4}})
        o = {"id": "o1", "coin": "BTC", "side": "buy", "quote": "IRT", "notional": 10000000, "exchange": "nobitex"}
        t, _ = K.build_ticket(o, K.load_config(), prices(market("BTC", 7e9)))
        self.assertEqual(t["payload"]["amount"], "0.0014")


class TestSafety(Base):
    def test_unchecked_and_changed_orders_cannot_be_approved(self):
        self.setup_orders()
        doc = self.read("orders.json")
        doc["orders"][0]["notional"] = 20000000                 # changed after the risk check
        del doc["orders"][1]["risk"]
        self.write("orders.json", doc)
        out, code = self.run_main(K, ["approve", "o1", "--words", "yes"])
        self.assertNotEqual(code, 0); self.assertIn("CHANGED", out)
        out, code = self.run_main(K, ["approve", "o2", "--words", "yes"])
        self.assertNotEqual(code, 0); self.assertIn("NOT CHECKED", out)

    def test_blocked_order_cannot_be_approved(self):
        self.setup_orders(orders=[{"id": "o1", "coin": "BTC", "side": "buy", "quote": "IRT", "exchange": "nobitex",
                                   "notional": 100, "type": "limit"}])
        out, code = self.run_main(K, ["approve", "o1", "--words", "yes"])
        self.assertNotEqual(code, 0)
        self.assertIn("BLOCKED", out)

    def test_place_needs_confirm_and_approval(self):
        self.setup_orders()
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o2"])
        self.assertNotEqual(code, 0); self.assertIn("--confirm", out)
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertNotEqual(code, 0); self.assertIn("no recorded approval", out)

    def test_empty_words_refused(self):
        self.setup_orders()
        out, code = self.run_main(K, ["approve", "o1", "--words", "  "])
        self.assertNotEqual(code, 0)

    def test_expired_approval(self):
        self.setup_orders()
        self.run_main(K, ["approve", "o1", "--words", "yes buy o1"])
        ap = self.read("approvals.json")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).replace(microsecond=0).isoformat()
        ap["approvals"]["o1"]["at"] = old
        self.write("approvals.json", ap)
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertNotEqual(code, 0); self.assertIn("minutes old", out)

    def test_stale_price_blocks_approval(self):
        self.setup_orders()
        pr = self.read("prices.json")
        pr["markets"]["BTCIRT"]["updated"] = now_iso(60)
        self.write("prices.json", pr)
        out, code = self.run_main(K, ["approve", "o1", "--words", "yes"])
        self.assertNotEqual(code, 0); self.assertIn("older than", out)


class TestPaper(Base):
    def test_paper_fill_updates_portfolio_and_fills(self):
        self.setup_orders(pf={"quote": "IRT", "cash": 50000000, "holdings": {}})
        self.run_main(K, ["paper-init", "--cash", "50000000"])
        out, code = self.run_main(K, ["approve", "o1", "--words", "بله بخر"])
        self.assertEqual(code, 0, out)
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertEqual(code, 0, out)
        self.assertIn("paper fill", out)
        pf = self.read("portfolio.json")
        self.assertAlmostEqual(pf["holdings"]["BTC"], 0.00142)
        cost = 0.00142 * 7005000000                              # filled at the ask, not the guard
        self.assertAlmostEqual(pf["cash"], 50000000 - cost * 1.0025, places=2)
        f = self.read("fills.json")["fills"][0]
        self.assertEqual((f["coin"], f["side"], f["source"]), ("BTC", "buy", "paper"))
        self.assertEqual(self.calls, [])                         # nothing left the computer
        self.assertEqual(self.read("approvals.json")["approvals"]["o1"]["words"], "بله بخر")
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertNotEqual(code, 0); self.assertIn("already placed", out)

    def test_new_batch_can_reuse_ids(self):
        self.setup_orders()
        self.run_main(K, ["paper-init", "--cash", "50000000"])
        self.run_main(K, ["approve", "o1", "--words", "yes"])
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertEqual(code, 0, out)
        # a new budget run next week: a different o1
        self.setup_orders(orders=[{"id": "o1", "coin": "BTC", "side": "buy", "quote": "IRT", "exchange": "nobitex",
                                   "notional": 4000000, "type": "limit"}], pf=self.read("portfolio.json"))
        self.run_main(K, ["approve", "o1", "--words", "yes again"])
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.read("fills.json")["fills"]), 2)

    def test_paper_needs_cash(self):
        self.setup_orders()
        self.run_main(K, ["paper-init", "--cash", "1000"])
        self.run_main(K, ["approve", "o1", "--words", "yes"])
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertNotEqual(code, 0); self.assertIn("not enough cash", out)


class TestLive(Base):
    def test_live_without_key_refuses(self):
        self.setup_orders(mode="live")
        self.run_main(K, ["approve", "o1", "--words", "yes"])
        out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
        self.assertNotEqual(code, 0); self.assertIn("NOBITEX_TOKEN", out)
        self.assertEqual(self.calls, [])

    def test_live_sends_exactly_once(self):
        self.setup_orders(mode="live")
        os.environ["NOBITEX_TOKEN"] = "secret-token"
        try:
            self.run_main(K, ["approve", "o1", "--words", "yes, place o1"])
            out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
            self.assertEqual(code, 0, out)
            self.assertEqual(len(self.calls), 1)
            method, url, headers, body = self.calls[0]
            self.assertEqual((method, url), ("POST", "https://apiv2.nobitex.ir/market/orders/add"))
            self.assertEqual(headers["Authorization"], "Token secret-token")
            self.assertEqual(body["price"], "70400250000")
            out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
            self.assertNotEqual(code, 0)
            self.assertEqual(len(self.calls), 1)
            log = open(os.path.join(self.dir, "broker_log.json")).read()
            self.assertNotIn("secret-token", log)                # keys never land on disk
        finally:
            os.environ.pop("NOBITEX_TOKEN", None)

    def test_in_flight_is_never_resent(self):
        self.setup_orders(mode="live")
        os.environ["NOBITEX_TOKEN"] = "t"

        def dies(*a, **k):
            raise OSError("connection reset")
        K.http = dies
        try:
            self.run_main(K, ["approve", "o1", "--words", "yes"])
            with self.assertRaises(OSError):
                K.main(["place", "o1", "--confirm", "o1"])
            out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
            self.assertNotEqual(code, 0)
            self.assertIn("IN FLIGHT", out)
        finally:
            os.environ.pop("NOBITEX_TOKEN", None)

    def test_exchange_rejection_is_logged_and_can_retry(self):
        self.setup_orders(mode="live")
        os.environ["NOBITEX_TOKEN"] = "t"
        K.http = lambda *a, **k: {"status": "failed", "code": "SmallOrder"}
        try:
            self.run_main(K, ["approve", "o1", "--words", "yes"])
            out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
            self.assertNotEqual(code, 0); self.assertIn("SmallOrder", out)
            self.assertEqual(K.placed_ids(), {})
        finally:
            os.environ.pop("NOBITEX_TOKEN", None)

    def test_mode_switch_needs_new_approval(self):
        self.setup_orders(mode="paper")
        self.run_main(K, ["approve", "o1", "--words", "yes"])
        self.write("config.json", {"exchange": "nobitex", "mode": "live"})
        os.environ["NOBITEX_TOKEN"] = "t"
        try:
            out, code = self.run_main(K, ["place", "o1", "--confirm", "o1"])
            self.assertNotEqual(code, 0); self.assertIn("approve again", out)
            self.assertEqual(self.calls, [])
        finally:
            os.environ.pop("NOBITEX_TOKEN", None)

    def test_balances_nobitex(self):
        self.write("config.json", {"exchange": "nobitex", "mode": "live"})
        os.environ["NOBITEX_TOKEN"] = "t"
        K.http = lambda *a, **k: {"status": "ok", "wallets": {"RLS": {"balance": "5000000000"}, "BTC": {"balance": "0.01"},
                                                              "ETH": {"balance": "0E-10"}}}
        try:
            out, code = self.run_main(K, ["balances"])
            pf = self.read("portfolio.json")
            self.assertEqual(pf["cash"], 500000000)              # rial -> toman
            self.assertEqual(pf["holdings"], {"BTC": 0.01})
        finally:
            os.environ.pop("NOBITEX_TOKEN", None)


class TestSigned(Case):
    SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")

    def test_ed25519_matches_rfc8032_vectors(self):
        self.assertEqual(K.ed25519_sign(self.SEED, b"").hex(),
                         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        seed3 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
        self.assertEqual(K.ed25519_sign(seed3, bytes.fromhex("72")).hex(),
                         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")

    def test_signed_headers_cover_path_query_and_body(self):
        import base64
        secret = base64.urlsafe_b64encode(self.SEED).decode()
        h = K.nobitex_signed_headers("pub", secret, "post", "https://apiv2.nobitex.ir/market/orders/add?a=1", b'{"x":1}')
        self.assertEqual(set(h), {"Nobitex-Key", "Nobitex-Signature", "Nobitex-Timestamp"})
        self.assertEqual(h["Nobitex-Key"], "pub")
        payload = (h["Nobitex-Timestamp"] + "POST/market/orders/add?a=1").encode() + b'{"x":1}'
        self.assertEqual(base64.urlsafe_b64decode(h["Nobitex-Signature"]), K.ed25519_sign(self.SEED, payload))

    def test_key_pair_wins_over_token_in_live(self):
        import base64
        secret = base64.urlsafe_b64encode(self.SEED).decode()
        old = {v: os.environ.get(v) for v in ("NOBITEX_TOKEN", "NOBITEX_API_KEY", "NOBITEX_API_SECRET")}
        os.environ.update(NOBITEX_TOKEN="t", NOBITEX_API_KEY="pub", NOBITEX_API_SECRET=secret)
        try:
            var, headers = K.credentials("nobitex", "live")
            self.assertEqual(var, "NOBITEX_API_KEY")
            self.assertEqual(headers, {"_nobitex_signed": ("pub", secret)})
            var, headers = K.credentials("nobitex", "testnet")
            self.assertEqual(headers, None)
        finally:
            for v, x in old.items():
                os.environ.pop(v, None) if x is None else os.environ.__setitem__(v, x)


if __name__ == "__main__":
    unittest.main()
