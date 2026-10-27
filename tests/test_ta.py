import math
import unittest

from _load import Case, load

T = load("crypto-ta", "ta.py")


def series(closes):
    return {"t": list(range(len(closes))), "o": closes[:], "h": [c * 1.01 for c in closes],
            "l": [c * 0.99 for c in closes], "c": closes[:], "v": [100.0] * len(closes)}


class TestIndicators(Case):
    def test_sma_ema(self):
        xs = [1, 2, 3, 4, 5, 6]
        self.assertEqual(T.sma(xs, 3), [None, None, 2, 3, 4, 5])
        e = T.ema(xs, 3)
        self.assertEqual(e[2], 2)                     # seeded with the SMA
        self.assertAlmostEqual(e[3], 3.0)             # 4*0.5 + 2*0.5
        self.assertAlmostEqual(e[5], 5.0)

    def test_rsi_extremes(self):
        up = [float(i) for i in range(1, 40)]
        self.assertEqual(T.rsi(up)[-1], 100.0)
        down = [float(i) for i in range(40, 1, -1)]
        self.assertAlmostEqual(T.rsi(down)[-1], 0.0)

    def test_rsi_known_value(self):
        # 14 alternating moves of +1 / -1 -> average gain = average loss -> 50
        xs = [10.0]
        for i in range(14):
            xs.append(xs[-1] + (1 if i % 2 == 0 else -1))
        self.assertAlmostEqual(T.rsi(xs)[14], 50.0)

    def test_macd_sign_follows_trend(self):
        up = [100 * 1.01 ** i for i in range(80)]
        line, sig, hist = T.macd(up)
        self.assertGreater(line[-1], 0)
        down = [100 * 0.99 ** i for i in range(80)]
        self.assertLess(T.macd(down)[0][-1], 0)

    def test_bollinger_flat(self):
        up, mid, lo = T.bollinger([5.0] * 25)
        self.assertEqual((up[-1], mid[-1], lo[-1]), (5.0, 5.0, 5.0))

    def test_atr(self):
        c = [100.0] * 20
        a = T.atr([101.0] * 20, [99.0] * 20, c)
        self.assertAlmostEqual(a[-1], 2.0)

    def test_levels(self):
        h = [1, 2, 3, 10, 3, 2, 1, 2, 3, 2, 1, 2, 3, 4, 5, 6]
        l = [x - 0.5 for x in h]
        sup, res = T.levels(h, l, price=5.5)
        self.assertEqual(res, 10)


class TestAnalysis(Case):
    def test_uptrend_is_bullish(self):
        closes = [100 * 1.003 ** i + 8 * math.sin(i / 2.5) for i in range(260)]  # trend + swings
        a = T.analyze(series(closes))
        self.assertGreaterEqual(a["score"], 30)
        self.assertEqual(a["bias"], "bullish")
        self.assertTrue(any("EMA200" in r for r in a["reasons"]))

    def test_smooth_rally_is_flagged_overbought(self):
        a = T.analyze(series([100 * 1.004 ** i for i in range(260)]))
        self.assertTrue(any("overbought" in r for r in a["reasons"]))

    def test_downtrend_is_bearish(self):
        closes = [100 * 0.997 ** i + 2.4 * math.sin(i / 2.5) for i in range(260)]
        self.assertEqual(T.analyze(series(closes))["bias"], "bearish")

    def test_too_few_bars(self):
        with self.assertRaises(RuntimeError):
            T.analyze(series([1.0] * 20))

    def test_nobitex_candles_rial_to_toman(self):
        T.fetch = lambda url, **k: {"s": "ok", "t": [1, 2], "o": [10, 20], "h": [30, 40], "l": [5, 6], "c": [100, 200], "v": [1, 2]}
        c = T.candles_nobitex("BTC", "IRT", "1d")
        self.assertEqual(c["c"], [10.0, 20.0])
        c = T.candles_nobitex("BTC", "USDT", "1d")
        self.assertEqual(c["c"], [100, 200])

    def test_rules(self):
        a = {"score": 40, "rsi": 55, "price": 10, "ema200": 8, "ema50": 9, "macd_hist": 1, "change_7": 3,
             "change_30": 10, "atr_pct": 4, "volume_ratio": 1.2}
        self.assertEqual(T.apply_rules(a, {"above_ema200": True, "rsi_max": 60}), [])
        self.assertEqual(T.apply_rules(a, {"rsi_max": 50}), ["rsi_max=50"])
        with self.assertRaises(RuntimeError):
            T.apply_rules(a, {"moon": 1})

    def test_scan_writes_targets(self):
        up = series([100 * 1.004 ** i for i in range(260)])
        down = series([100 * 0.996 ** i for i in range(260)])
        data = {"BTC": up, "ETH": up, "DOGE": down}
        T.get_candles = lambda coin, quote, tf, source, file=None: data[coin]
        self.write("rules.json", {"above_ema200": True})
        import os
        out, code = self.run_main(T, ["scan", "BTC", "ETH", "DOGE", "--rules", os.path.join(self.dir, "rules.json"),
                                      "--targets", "5", "--pause", "0"])
        self.assertEqual(code, 0, out)
        t = self.read("targets.json")
        self.assertEqual(sorted(x["coin"] for x in t["targets"]), ["BTC", "ETH"])
        self.assertAlmostEqual(sum(x["weight"] for x in t["targets"]), 1.0, places=4)
        self.assertEqual(len(self.read("signals.json")["rows"]), 3)


if __name__ == "__main__":
    unittest.main()
