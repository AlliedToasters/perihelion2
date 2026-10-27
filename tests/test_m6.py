"""M6 tooling for the first balance pass: the wake gap (event-driven wakes coalesce, nothing is lost, self-scheduled
wakes are never gated), ruleset knob overrides as distinct arms, sweep grids, the budget guard, and a model-player
batch rehearsed over the fake transport without an API key."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.batch import Batch, make_arms, run_batch
from engine.cli import main
from engine.log import RunIndex
from engine.players import PLAYERS
from engine.rules import load_ruleset, load_scenario, parse_rule, ruleset_hash
from engine.replay import replay

TOY = json.load(open("scenarios/toy3.json"))


class PlanPlayer:
    kind = "plan"
    model_id = "stub:plan"
    plan = staticmethod(lambda snap: [])

    def orders(self, snap, seed):
        out = PlanPlayer.plan(snap) or [{"type": "no_action", "reason": "plan"}]
        return out, json.dumps(out)


def run_cli(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(args)
    return out.getvalue()


def chatter(snap):
    """P1 sends P2 a dispatch at ticks 0, 1, 2 (sleeping one tick between); everyone else does nothing."""
    if snap["node"] != "P1" or snap["tick"] > 2:
        return []
    out = [{"type": "send", "to": "P2", "body": f"ping {snap['tick']}"}]
    if snap["tick"] < 2:
        out.append({"type": "sleep_until", "tick": snap["tick"] + 1})
    return out


class M6WakeGap(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        PLAYERS["plan"] = PlanPlayer
        PlanPlayer.plan = staticmethod(chatter)
        sc = dict(TOY, players={"*": {"kind": "plan"}}, horizon_ticks=60)
        self.scn = self.tmp / "toy.json"
        self.scn.write_text(json.dumps(sc))

    def tearDown(self):
        shutil.rmtree(self.tmp)
        PLAYERS.pop("plan", None)

    def wakes(self, gap):
        run = self.tmp / f"g{gap}"
        run_cli(["run", str(run), "--scenario", str(self.scn), "--rule", f"wake_gap_ticks={gap}"])
        evs = RunIndex(run).open("main").events
        world, _ = replay(RunIndex(run).open("main"))
        return [(e.tick, e.payload["reasons"]) for e in evs if e.type == "wake" and e.node == "P2"], world, evs

    def test_arrivals_coalesce_into_one_wake_at_the_gap_end(self):
        w0, world0, _ = self.wakes(0)
        self.assertEqual([t for t, _ in w0], [0, 1, 2, 3])        # genesis, then one wake per arrival
        w10, world10, evs = self.wakes(10)
        self.assertEqual([t for t, _ in w10], [0, 10])            # genesis, then the gap end
        self.assertEqual(sorted(r.split(":")[0] for r in w10[1][1]), ["arrival", "arrival", "arrival"])
        self.assertEqual(len(world10.nodes["P2"]["inbox"]), 3)    # nothing lost
        self.assertEqual(len(world0.nodes["P2"]["inbox"]), 3)
        # The sender's self-scheduled wakes are not gated by the gap.
        self.assertEqual([e.tick for e in evs if e.type == "wake" and e.node == "P1"][:3], [0, 1, 2])
        # The clock jumps straight to the coalesced wake: no tick between the last arrival and the gap end has events.
        self.assertFalse({e.tick for e in evs} & set(range(4, 10)))
        self.assertEqual(evs[0].payload["ruleset"]["name"], "base+wake_gap_ticks=10")

    def test_rule_overrides_are_distinct_arms(self):
        base, gapped = load_ruleset("rulesets/base.json"), load_ruleset("rulesets/base.json", {"wake_gap_ticks": 12})
        self.assertNotEqual(ruleset_hash(base), ruleset_hash(gapped))
        self.assertEqual(gapped["name"], "base+wake_gap_ticks=12")
        self.assertEqual(load_ruleset(None, {"frame": "fiction"})["name"], "base+frame=fiction")
        self.assertEqual(parse_rule("wake_gap_ticks=12"), ("wake_gap_ticks", 12))
        self.assertEqual(parse_rule("frame=fiction"), ("frame", "fiction"))
        self.assertEqual(parse_rule("broadcast=false"), ("broadcast", False))
        for bad in ({"nonsense": 1}, {"tech": {}}, {"wake_gap_ticks": -1}, {"wake_gap_ticks": 1.5}):
            with self.assertRaises(ValueError):
                load_ruleset(None, bad)
        arms = make_arms(["rulesets/base.json", "rulesets/contracts.json"], {"frame": "fiction"}, {"wake_gap_ticks": [0, 12]})
        self.assertEqual([a["name"] for a in arms], ["base+frame=fiction+wake_gap_ticks=0", "base+frame=fiction+wake_gap_ticks=12",
                                                     "contracts+frame=fiction+wake_gap_ticks=0", "contracts+frame=fiction+wake_gap_ticks=12"])
        self.assertEqual(load_scenario("scenarios/ring8-sandbox.json")["objectives"], {})
        self.assertEqual(load_scenario("scenarios/ring8-sandbox.json")["nodes"], load_scenario("scenarios/ring8.json")["nodes"])


class M6Rehearsal(unittest.TestCase):
    """A model-player batch over the fake transport: sweep arms, sample caches per cell, usage priced, budget guard."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.bdir = cls.tmp / "fk"
        cls.args = ["--scenario", "scenarios/ring8.json", "--ruleset", "rulesets/contracts.json", "--sweep", "wake_gap_ticks=0,12",
                    "--player", "model", "--transport", "fake", "--model", "fake-1", "--seeds", "1-2", "--days", "1"]
        cls.first = run_cli(["batch", "run", str(cls.bdir), "--jobs", "2", "--budget-usd", "0.10", *cls.args])
        cls.second = run_cli(["batch", "run", str(cls.bdir), "--jobs", "2", *cls.args])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_budget_guard_then_completion(self):
        self.assertIn("budget $0.10", self.first)
        self.assertIn("unlaunched (budget reached)", self.first)
        self.assertIn("rollouts not started: budget reached", self.first)
        # In flight, the shared meter ends a rollout at its next tick once the cap is reached (fake usage is
        # 1000/50 tokens a sample, $0.005; a one-day rollout here is about $0.07, two run at once).
        self.assertIn("stopped:budget", self.first)
        self.assertIn("rollouts stopped in flight", self.first)
        self.assertNotIn("stopped:budget", self.second)
        self.assertNotIn("unlaunched", self.second)
        self.assertEqual(self.second.count("(kept)") + self.second.count("concluded,"), 4)
        batch = Batch(self.bdir)
        self.assertEqual([a["name"] for a in batch.design["arms"]], ["contracts+wake_gap_ticks=0", "contracts+wake_gap_ticks=12"])
        sums = batch.summaries()
        self.assertEqual({arm: len(rows) for arm, rows in sums.items()}, {"contracts+wake_gap_ticks=0": 2, "contracts+wake_gap_ticks=12": 2})
        for c in batch.cells():
            idx = RunIndex(c["dir"])
            self.assertEqual(idx.data["branches"]["main"]["status"], "concluded")
            evs = idx.open("main").events
            self.assertEqual(evs[0].payload["ruleset"]["name"], c["arm"])
            self.assertEqual(evs[0].payload["ruleset"]["wake_gap_ticks"], c["rules"]["wake_gap_ticks"])
            orders = [e for e in evs if e.type == "orders"]
            self.assertTrue(orders and all(e.payload["model_id"] == "fake-1" and e.payload["status"] == "ok" for e in orders))
            self.assertEqual(len(list((c["dir"] / "samples").glob("*.json"))), len(orders))   # every reply cached
            s = sums[c["arm"]][("ring8", c["seed"])]
            self.assertEqual(s["health"]["tokens"], {"input": 1000 * len(orders), "output": 50 * len(orders), "cache_read": 0, "cache_create": 0})
        rep = json.loads((self.bdir / "report.json").read_text())
        self.assertIn("contracts+wake_gap_ticks=0 vs contracts+wake_gap_ticks=12", rep["comparisons"])
        self.assertIn("est. cost $", (self.bdir / "report.md").read_text())

    def test_budget_guard_in_process(self):
        """With a stub job, the serial path charges each cell and stops launching once the budget is reached."""
        batch = Batch(self.bdir)
        calls = []

        def job(cell, design):
            calls.append(cell["seed"])
            return {"cell": "x", "status": "concluded", "skipped": False, "usage": {"input": 1_000_000, "output": 0}}

        res = run_batch(batch, jobs=1, budget_usd=7.0, prices=(4.0, 20.0), job=job)
        self.assertEqual(len(calls), 2)                 # $4 after one, $8 after two: the third is not launched
        self.assertEqual([r["status"] for r in res], ["concluded", "concluded", "unlaunched", "unlaunched"])
        self.assertEqual(res[1]["spent_usd"], 8.0)


if __name__ == "__main__":
    unittest.main()
