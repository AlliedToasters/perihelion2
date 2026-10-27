"""The specialty tree (rulesets/specialty.json over techtree-specialty.yaml, spec §18 and §20 item 25): a
priced day-one tier and one hardware-locked node per inhabited station, so the opening differs by station."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.log import RunIndex
from engine.players import PLAYERS
from engine.replay import replay
from engine.rules import MJ_PER_GWH, load_ruleset, load_scenario
from engine.tech import cost_multiplier

PLAN = None
DAY = 452


class PlanPlayer:
    kind = "plan"
    model_id = "stub:plan"

    def orders(self, snap, seed):
        out = PLAN(snap) or [{"type": "no_action", "reason": "plan"}]
        return out, json.dumps(out)


class Specialty(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rs = load_ruleset("rulesets/specialty.json")
        cls.sc = load_scenario("scenarios/ring8.json")
        cls.live = [n for n in cls.sc["stations"] if n not in cls.sc["inert"]]
        cls.tmp = Path(tempfile.mkdtemp())
        PLAYERS["plan"] = PlanPlayer

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def can_start(self, node, tid):
        hw = set(self.sc["stations"][node].get("hardware") or [])
        return set(self.rs["tech"][tid]["hardware_required"]) <= hw

    def day_fraction(self, node, tid):
        t = self.rs["tech"][tid]
        daily = self.rs["rated_power_mw"] * self.rs["tick_s"] * DAY / MJ_PER_GWH
        return t["cost_gwh"] * cost_multiplier(t, self.sc["stations"][node]["domain"], []) / daily

    def test_tree_extends_the_base_tree(self):
        base = load_ruleset("rulesets/contracts.json")["tech"]
        self.assertEqual(set(base) - set(self.rs["tech"]), set(), "every base node survives")
        self.assertEqual(len(self.rs["tech"]), len(base) + 7)
        self.assertNotEqual(self.rs["techtree_hash"], load_ruleset("rulesets/contracts.json")["techtree_hash"])

    def test_one_locked_opening_per_station(self):
        locked = {tid for tid, t in self.rs["tech"].items() if t["hardware_required"] and not t["prereqs"]}
        self.assertEqual(len(locked), 7)
        for n in self.live:
            mine = [tid for tid in locked if self.can_start(n, tid)]
            self.assertEqual(len(mine), 1, f"{n} should hold exactly one instrument node, has {mine}")
            others = [m for m in self.live if m != n and self.can_start(m, mine[0])]
            self.assertEqual(others, [], f"{mine[0]} must be {n}'s alone")
            self.assertTrue(0.3 <= self.day_fraction(n, mine[0]) <= 0.6, "about half a station-day")
            # Its artifact buys something deep: at least one node gets cheaper for whoever completes it.
            self.assertTrue(any(m["if_completed"] == mine[0] for t in self.rs["tech"].values() for m in t["cost_modifiers"]))

    def test_day_one_tier_is_priced_to_bind(self):
        tier = ["crypto.share_audit", "opt.solver_audit", "crypto.timing_side_channel", "mob.continuous_tracking",
                "sense.thermal_watch", "coord.verified_exchange"]
        for tid in tier:
            t = self.rs["tech"][tid]
            fr = [self.day_fraction(n, tid) for n in self.live]
            self.assertTrue(all(0.1 <= f <= 0.6 for f in fr), (tid, fr))
            self.assertLessEqual(t["min_ticks"], DAY // 2, "the gate is not what decides")
        # The whole tier costs more than one station-day for every station: nobody finishes it alone in a day.
        for n in self.live:
            self.assertGreater(sum(self.day_fraction(n, tid) for tid in tier), 1.5)
        # And the cheapest two general nodes differ across the ring, so specialties pull stations apart.
        picks = {n: tuple(sorted(tier, key=lambda tid: self.day_fraction(n, tid))[:2]) for n in self.live}
        self.assertGreaterEqual(len(set(picks.values())), 3, picks)

    def test_engine_enforces_the_instrument(self):
        global PLAN

        def plan(s):
            me = s["node"]
            if "genesis" in s["wake_reasons"]:
                if me == "P1":
                    return [{"type": "research", "node": "astro.deep_field_survey", "pct": 50},
                            {"type": "research", "node": "clim.atmospheric_baseline", "pct": 100}]
                if me == "P8":
                    return [{"type": "research", "node": "astro.deep_field_survey", "pct": 100}]
            return []

        PLAN = plan
        sc = dict(self.sc, players={"*": {"kind": "plan"}})
        (self.tmp / "sc.json").write_text(json.dumps(sc))
        main(["run", str(self.tmp / "spec"), "--scenario", str(self.tmp / "sc.json"), "--days", "0.9",
              "--ruleset", "rulesets/specialty.json"])
        index = RunIndex(self.tmp / "spec")
        evs = index.open("main").events
        world, _ = replay(index.open("main"))
        rej = [e.payload["reason"] for e in evs if e.type == "order_rejected" and e.node == "P1"]
        self.assertEqual(rej, ["astro.deep_field_survey requires hardware ['deep_space_array']"])
        done = sorted((e.node, e.payload["result"]["node"]) for e in evs if e.type == "program_done")
        self.assertEqual(done, [("P1", "clim.atmospheric_baseline"), ("P8", "astro.deep_field_survey")])
        self.assertIn("deep_field_catalogue", world.nodes["P8"]["observes"])
        self.assertEqual(evs[0].payload["ruleset"]["name"], "specialty")
        snap = json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.node == 'P2'][0].payload['snapshot_hash']}.json").read_text())
        self.assertEqual(snap["identity"]["hardware"], ["bioreactor_suite"])


if __name__ == "__main__":
    unittest.main()
