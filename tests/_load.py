"""Load a skill script as a module and give every test its own crypto/ folder."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(skill, script):
    path = os.path.join(ROOT, skill, script)
    name = "%s_%s" % (skill.replace("-", "_"), script[:-3])
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def now_iso(minutes_ago=0):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return t.replace(microsecond=0).isoformat()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cts-")
        self.dir = os.path.join(self.tmp, "crypto")
        os.makedirs(self.dir)
        self._old = os.environ.get("CRYPTO_DIR")
        os.environ["CRYPTO_DIR"] = self.dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CRYPTO_DIR", None)
        else:
            os.environ["CRYPTO_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, obj):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return json.load(f)

    def exists(self, name):
        return os.path.exists(os.path.join(self.dir, name))

    def run_main(self, mod, argv):
        """Run mod.main(argv); return (stdout, exit code)."""
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                mod.main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                if isinstance(e.code, str):
                    out.write(e.code)
        return out.getvalue(), code


def market(coin, last, bid=None, ask=None, quote="IRT", minutes_ago=0, change=0.0, min_notional=None):
    return {"coin": coin, "quote": quote, "exchange": "nobitex", "last": last,
            "bid": bid if bid is not None else last * 0.999, "ask": ask if ask is not None else last * 1.001,
            "change_pct": change, "min_notional": min_notional if min_notional is not None else (300000 if quote == "IRT" else 11),
            "qty_step": None, "updated": now_iso(minutes_ago)}


def prices(*mkts, usdt_irt=100000):
    return {"updated": now_iso(), "exchange": "nobitex", "usdt_irt": usdt_irt,
            "markets": {m["coin"] + m["quote"]: m for m in mkts}}
