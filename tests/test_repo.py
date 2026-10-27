"""Repo-wide checks: every skill is complete, the shared rules agree, and the
whole paper pipeline runs end to end (targets -> budget -> risk -> broker -> pnl)."""
import ast
import json
import os
import re
import sys
import unittest

from _load import ROOT, Case, load, market, prices

SKILLS = ["crypto-whales", "crypto-news", "crypto-ta", "crypto-alerts", "crypto-budget",
          "crypto-risk", "crypto-broker", "crypto-rebalance", "crypto-pnl", "crypto-margin"]
STDLIB_OK = {"argparse", "datetime", "json", "os", "sys", "time", "urllib", "math", "hashlib", "re", "html",
             "email", "xml", "tempfile"}


class TestRepo(unittest.TestCase):
    def test_every_skill_has_frontmatter(self):
        for s in SKILLS:
            text = open(os.path.join(ROOT, s, "SKILL.md"), encoding="utf-8").read()
            m = re.match(r"---\n(.*?)\n---\n", text, re.S)
            self.assertTrue(m, s)
            self.assertIn("name: %s\n" % s, m.group(1))
            self.assertIn("description:", m.group(1))

    def test_scripts_use_only_the_standard_library(self):
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if not f.endswith(".py"):
                    continue
                tree = ast.parse(open(os.path.join(ROOT, s, f), encoding="utf-8").read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [(node.module or "").split(".")[0]]
                    else:
                        continue
                    for n in names:
                        self.assertIn(n, STDLIB_OK, "%s/%s imports %s" % (s, f, n))

    def test_shared_defaults_agree(self):
        example = json.load(open(os.path.join(ROOT, "crypto-budget", "config.example.json")))
        for skill, script in (("crypto-budget", "size.py"), ("crypto-risk", "check.py"),
                              ("crypto-broker", "broker.py"), ("crypto-rebalance", "rebalance.py"),
                              ("crypto-margin", "margin.py")):
            self.assertEqual(load(skill, script).DEFAULTS, example, skill)

    def test_paper_is_the_default(self):
        example = json.load(open(os.path.join(ROOT, "crypto-budget", "config.example.json")))
        self.assertEqual(example["mode"], "paper")
        self.assertFalse(example["allow_leverage"])

    def test_risk_and_broker_hash_the_same_way(self):
        r, b = load("crypto-risk", "check.py"), load("crypto-broker", "broker.py")
        o = {"id": "o1", "coin": "BTC", "side": "buy", "quote": "IRT", "exchange": "nobitex", "notional": 5}
        self.assertEqual(r.HASH_FIELDS, b.HASH_FIELDS)
        self.assertEqual(r.order_hash(o), b.order_hash(o))

    def test_plugin_manifests(self):
        for f in ("plugin.json", "marketplace.json"):
            json.load(open(os.path.join(ROOT, ".claude-plugin", f)))


class TestPipeline(Case):
    def test_paper_end_to_end(self):
        B = load("crypto-budget", "size.py")
        R = load("crypto-risk", "check.py")
        K = load("crypto-broker", "broker.py")
        P = load("crypto-pnl", "pnl.py")
        K.http = lambda *a, **k: self.fail("paper mode must not touch the network")
        self.write("targets.json", {"source": "test", "targets": [
            {"coin": "BTC", "weight": 0.5}, {"coin": "ETH", "weight": 0.3}, {"coin": "SOL", "weight": 0.2}]})
        self.write("prices.json", prices(market("BTC", 7000000000), market("ETH", 300000000), market("SOL", 15000000)))
        self.write("config.json", {"max_position_pct": 40})
        self.run_main(K, ["paper-init", "--cash", "50000000"])
        out, code = self.run_main(B, ["--budget", "50000000", "--cap", "40", "--write"])
        self.assertEqual(code, 0, out)
        out, code = self.run_main(R, [])
        self.assertEqual(code, 0, out)
        for o in self.read("orders.json")["orders"]:
            self.run_main(K, ["approve", o["id"], "--words", "yes " + o["id"]])
            out, code = self.run_main(K, ["place", o["id"], "--confirm", o["id"]])
            self.assertEqual(code, 0, out)
        pf = self.read("portfolio.json")
        self.assertEqual(sorted(pf["holdings"]), ["BTC", "ETH", "SOL"])
        self.assertGreaterEqual(pf["cash"], 50000000 * 0.10 - 1)   # the full buffer survived fees and rounding
        out, code = self.run_main(P, ["report"])
        self.assertEqual(code, 0, out)
        self.assertIn("TOTAL IRT", out)


if __name__ == "__main__":
    unittest.main()
