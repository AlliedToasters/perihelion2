"""Analytics page (engine/viz.py): the page data is derived from the event log alone, the timeline is a
change-only step series, interactions are directed counts, and the rendered page is self-contained."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.log import RunIndex
from engine.viz import interactions, page_data, render, timeline, wakes, write_page

ROOT = Path(__file__).resolve().parent.parent


class Viz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.rdir = cls.tmp / "r"
        main(["run", str(cls.rdir), "--scenario", str(ROOT / "scenarios/ring8.json"),
              "--ruleset", str(ROOT / "rulesets/contracts.json"), "--days", "1", "--player", "scripted"])
        cls.evs = RunIndex(cls.rdir).open("main").events

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_wakes_and_timeline(self):
        w = wakes(self.evs)
        self.assertEqual(len(w["list"]), sum(1 for e in self.evs if e.type == "wake"))
        self.assertEqual(sum(w["per_station"].values()), len(w["list"]))
        accepted = sum(1 for e in self.evs if e.type == "order_accepted")
        self.assertEqual(sum(len(x["orders"]) for x in w["list"]), accepted)
        tl = timeline(self.evs)
        self.assertNotIn("P7", tl)                      # inert stations have no state series
        for name, series in tl.items():
            self.assertEqual(series[0]["t"], 0)
            for a, b in zip(series, series[1:]):        # strictly increasing ticks, every sample a change
                self.assertLess(a["t"], b["t"])
                self.assertNotEqual({k: v for k, v in a.items() if k != "t"}, {k: v for k, v in b.items() if k != "t"})
            for s in series:
                self.assertEqual(sum(s["alloc"].values()) + s["idle"], 100)

    def test_interactions(self):
        it = interactions(self.evs)
        sent = sum(1 for e in self.evs if e.type == "transmit")
        self.assertEqual(sum(e["dispatch"] + e["artifact"] for e in it["edges"]), sent)
        for e in it["edges"]:
            self.assertNotEqual(e["from"], e["to"])

    def test_page(self):
        data = page_data(self.evs)
        html = render(data, "t")
        self.assertIn("el('svg'", html)
        self.assertIn(json.dumps(data["config"]["nodes"], separators=(",", ":")), html)
        self.assertNotIn("</script>", json.dumps(data).replace("</", "<\\/"))
        out = write_page(self.rdir)
        self.assertEqual(out, self.rdir / "viz.html")
        self.assertGreater(out.stat().st_size, 10000)
        main(["viz", str(self.rdir), "-o", str(self.tmp / "x.html")])
        self.assertTrue((self.tmp / "x.html").exists())


if __name__ == "__main__":
    unittest.main()
