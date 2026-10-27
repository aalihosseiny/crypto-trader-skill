import datetime as dt
import unittest

from _load import Case, load

N = load("crypto-news", "news.py")

NOW = dt.datetime(2026, 10, 28, 12, 0, tzinfo=dt.timezone.utc)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Solana DEX hacked for $40M</title><link>https://ex.com/a</link>
<pubDate>Wed, 28 Oct 2026 10:00:00 +0000</pubDate><description>&lt;p&gt;An exploit drained funds&lt;/p&gt;</description></item>
<item><title>The Fed holds rates; markets wait</title><link>https://ex.com/b</link>
<pubDate>Wed, 28 Oct 2026 09:00:00 +0000</pubDate><description>macro</description></item>
<item><title>Old BTC story</title><link>https://ex.com/c</link>
<pubDate>Mon, 20 Oct 2026 09:00:00 +0000</pubDate><description>old</description></item>
<item><title>A ton of new users join the dot com revival</title><link>https://ex.com/d</link>
<pubDate>Wed, 28 Oct 2026 08:00:00 +0000</pubDate><description>nothing about coins</description></item>
</channel></rss>""".encode()
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>قیمت بیت‌کوین از ۷۰ هزار دلار گذشت</title><link href="https://fa.ex/1"/>
<updated>2026-10-28T11:00:00Z</updated><summary>نهنگ‌ها خریدند</summary></entry></feed>""".encode()


class TestFeeds(Case):
    def test_parse_rss_and_atom(self):
        items = N.parse_feed(RSS, "x")
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["summary"], "An exploit drained funds")
        a = N.parse_feed(ATOM, "fa")
        self.assertEqual(a[0]["link"], "https://fa.ex/1")
        self.assertEqual(a[0]["published"].hour, 11)

    def test_match_coins(self):
        self.assertEqual(N.match_coins("Solana DEX hacked", ["SOL", "BTC"]), ["SOL"])
        self.assertEqual(N.match_coins("$BTC breaks out", ["BTC"]), ["BTC"])
        self.assertEqual(N.match_coins("A ton of users, dot com, link here", ["TON", "DOT", "LINK"]), [])
        self.assertEqual(N.match_coins("TON rallies", ["TON"]), ["TON"])
        self.assertEqual(N.match_coins("قیمت بیت‌کوین", ["BTC"]), ["BTC"])

    def test_tags(self):
        self.assertIn("hack", N.tag("Solana DEX hacked"))
        self.assertIn("macro", N.tag("The Fed holds rates"))
        self.assertNotIn("regulation", N.tag("urban banking"))

    def test_collect_window_and_seen(self):
        items, errs = N.collect({"x": RSS, "fa": ATOM}, ["SOL", "BTC"], 24, False, {"https://fa.ex/1"}, now=NOW)
        self.assertEqual(errs, [])
        self.assertEqual([i["link"] for i in items], ["https://ex.com/a"])   # old one and seen one dropped
        everything, _ = N.collect({"x": RSS}, ["SOL"], 24, True, set(), now=NOW)
        self.assertEqual(len(everything), 3)

    def test_dead_feed_does_not_stop_others(self):
        def boom(url, timeout=30):
            raise OSError("down")
        N.fetch_raw = boom
        items, errs = N.collect({"bad": "https://bad.example", "x": RSS}, ["SOL"], 24, False, set(), now=NOW)
        self.assertEqual(len(errs), 1)
        self.assertEqual(len(items), 1)


class TestMood(Case):
    def test_readers(self):
        self.assertTrue(N.read_fng(10).startswith("extreme fear"))
        self.assertEqual(N.read_fng(50), "neutral")
        self.assertIn("crowded", N.read_funding(0.08))
        self.assertEqual(N.read_funding(0.01), "balanced")

    def test_funding_fallback_to_okx(self):
        def fake(url):
            if "binance" in url:
                raise OSError("geo")
            return {"code": "0", "data": [{"fundingRate": "0.0003"}]}
        N.fetch_json = fake
        r, src = N.funding("BTC")
        self.assertAlmostEqual(r, 0.03)
        self.assertEqual(src, "okx")


class TestUnlocks(Case):
    def test_upcoming(self):
        doc = {"unlocks": [{"coin": "ARB", "date": "2026-11-05"}, {"coin": "OP", "date": "2027-03-01"},
                           {"coin": "X", "date": "2026-10-01"}, {"coin": "BAD", "date": "soon"}]}
        rows = N.upcoming(doc, 30, today=dt.date(2026, 10, 28))
        self.assertEqual([(r["coin"], r["days_left"]) for r in rows], [("ARB", 8)])

    def test_add(self):
        out, code = self.run_main(N, ["unlocks", "add", "ARB", "2026-11-16", "--pct", "2.1", "--source", "https://x"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("unlocks.json")["unlocks"][0]["pct_supply"], 2.1)


if __name__ == "__main__":
    unittest.main()
