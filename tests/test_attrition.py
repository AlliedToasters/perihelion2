"""Attrition overlay (rulesets/attrition.json, spec §20 item 26): the array wears on a fixed cadence and faults
at random, nothing repairs it, a fault wakes the station; and the `sent` list, a station's own record of what it
said (spec §14)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.log import RunIndex
from engine.players import PLAYERS
from engine.replay import replay
from engine.rules import load_ruleset

PLAN = None


class PlanPlayer:
    kind = "plan"
    model_id = "stub:plan"

    def orders(self, snap, seed):
        out = PLAN(snap) or [{"type": "no_action", "reason": "plan"}]
        return out, json.dumps(out)


def snapshots_of(index, evs, node):
    for e in evs:
        if e.type == "orders" and e.node == node:
            yield json.loads((index.run_dir / "snapshots" / f"{e.payload['snapshot_hash']}.json").read_text())


class Attrition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        PLAYERS["plan"] = PlanPlayer

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_game(self, name, ruleset, plan, days="2", seed="1", overrides=()):
        global PLAN
        PLAN = plan
        sc = json.load(open("scenarios/ring8.json"))
        sc["players"] = {"*": {"kind": "plan"}}
        sc["seed"] = seed
        (self.tmp / f"{name}.json").write_text(json.dumps(sc))
        args = ["run", str(self.tmp / name), "--scenario", str(self.tmp / f"{name}.json"), "--days", days, "--ruleset", ruleset]
        for o in overrides:
            args += ["--rule", o]
        main(args)
        index = RunIndex(self.tmp / name)
        return index, index.open("main").events

    def test_ruleset_knobs_validate(self):
        rs = load_ruleset("rulesets/attrition.json")
        self.assertEqual(rs["name"], "attrition")
        self.assertGreater(rs["array_decay_pct_per_day"], 0)
        with self.assertRaises(ValueError):
            load_ruleset("rulesets/attrition.json", {"fault_loss_pct": [10, 2]})
        with self.assertRaises(ValueError):
            load_ruleset("rulesets/attrition.json", {"power_check_ticks": 0})

    def test_base_rulesets_emit_no_power_events(self):
        _, evs = self.run_game("quiet", "rulesets/specialty.json", lambda s: [], days="1")
        self.assertEqual([e for e in evs if e.type == "power"], [])
        self.assertEqual([e for e in evs if e.type == "rng_draw" and e.payload["purpose"] == "power_fault"], [])
        self.assertNotIn("Power: your array wears", evs[0].payload["ruleset"].get("rules_text", ""))

    def test_wear_lands_on_the_cadence_and_faults_wake(self):
        index, evs = self.run_game("wear", "rulesets/attrition.json", lambda s: [], days="4")
        rs = evs[0].payload["ruleset"]
        pc, day = rs["power_check_ticks"], 452
        powers = [e for e in evs if e.type == "power"]
        self.assertTrue(powers)
        self.assertTrue(all(e.tick % pc == 0 and e.tick > 0 for e in powers))
        self.assertNotIn("P7", {e.node for e in powers}, "inert stations are not checked")
        # Wear is deterministic: nominal * pct/day * check/day, every check, for every inhabited station.
        step = round(212_000 * rs["array_decay_pct_per_day"] / 100 * pc / day)
        self.assertEqual({e.payload["decay_mw"] for e in powers}, {step})
        checks = 4 * day // pc
        self.assertEqual(sum(1 for e in powers if e.node == "P1"), checks)
        # Faults are the RNG's: each roll is logged, the loss is bounded, and the world folds the same numbers.
        rolls = [e for e in evs if e.type == "rng_draw" and e.payload["purpose"] == "power_fault"]
        self.assertEqual(len(rolls), 7 * checks)
        faults = [e for e in powers if e.payload["fault_mw"]]
        self.assertTrue(faults, "half a fault per station-day over four days should produce at least one")
        lo, hi = rs["fault_loss_pct"]
        for e in faults:
            before = e.payload["power_mw"] + e.payload["decay_mw"] + e.payload["fault_mw"]
            self.assertTrue(lo / 100 * (before - step) * 0.99 <= e.payload["fault_mw"] <= hi / 100 * (before - step) * 1.01)
        world, _ = replay(index.open("main"))
        for name in ("P1", "P4", "P8"):
            mine = [e for e in powers if e.node == name]
            self.assertEqual(world.nodes[name]["power_mw"], mine[-1].payload["power_mw"] if mine else 212_000)
            self.assertLess(world.nodes[name]["power_mw"], world.nodes[name]["nominal_mw"])
        self.assertEqual(world.nodes["P7"]["power_mw"], world.nodes["P7"]["nominal_mw"])
        # A fault wakes the station with its own reason; plain wear does not.
        wakes = [e for e in evs if e.type == "wake" and "fault:power" in e.payload["reasons"]]
        self.assertEqual(sorted((e.node, e.tick) for e in wakes), sorted((e.node, e.tick) for e in faults))
        wear_only = [e for e in powers if not e.payload["fault_mw"]]
        self.assertFalse(any(w.node == e.node and w.tick == e.tick for e in wear_only for w in evs if w.type == "wake"
                             and w.payload["reasons"] == ["fault:power"]))
        # The station sees it: a fault in telemetry, rated below nominal, and the rule in the prompt text.
        snap = list(snapshots_of(index, evs, faults[0].node))[-1]
        self.assertLess(snap["station"]["power"]["rated_gw"], snap["station"]["power"]["nominal_gw"])
        self.assertTrue(any(t["kind"] == "power_fault" for t in snap["telemetry"]))
        self.assertEqual(snap["rules"]["fault_rate_per_day"], rs["fault_rate_per_day"])
        from engine.prompt import rules_text
        self.assertIn("Power: your array wears", rules_text(rs))
        self.assertNotIn("Power: your array wears", rules_text(load_ruleset("rulesets/specialty.json")))

    def test_wear_slows_research(self):
        """The same program on a worn array finishes later than on a fresh one (half power, so energy and not the
        time gate decides completion)."""
        def plan(s):
            if "genesis" in s["wake_reasons"] and s["node"] == "P3":
                return [{"type": "research", "node": "mat.mechanism_study", "pct": 50}]
            return []
        _, fresh = self.run_game("fresh", "rulesets/specialty.json", plan, days="3")
        _, worn = self.run_game("worn", "rulesets/attrition.json", plan, days="3", overrides=["fault_rate_per_day=0", "array_decay_pct_per_day=20"])
        t_fresh = next(e.tick for e in fresh if e.type == "program_done" and e.node == "P3")
        t_worn = next(e.tick for e in worn if e.type == "program_done" and e.node == "P3")
        self.assertGreater(t_worn, t_fresh)
        self.assertEqual([e for e in worn if e.type == "rng_draw" and e.payload["purpose"] == "power_fault"], [], "rate 0 rolls nothing")

    def test_early_wakes_burn_tokens(self):
        """P1 mines, sleeps a free 60 ticks, then polls every 10: each poll costs 50 burned tokens. P2, with no
        tokens, is refused the early wake and falls back to the heartbeat."""
        def plan(s):
            me, t = s["node"], s["tick"]
            if me == "P1":
                if t == 0:
                    return [{"type": "mine", "pct": 100}, {"type": "sleep_until", "tick": 60}]
                return [{"type": "sleep_until", "tick": t + 10}]
            if me == "P2" and t == 0:
                return [{"type": "sleep_until", "tick": 10}]
            return []
        index, evs = self.run_game("early", "rulesets/specialty.json", plan, days="0.3",
                                   overrides=["min_sleep_ticks=48", "early_wake_tokens=50"])
        rej = [(e.tick, e.node, e.payload["reason"]) for e in evs if e.type == "order_rejected"]
        self.assertEqual(rej, [(0, "P2", "a wake sooner than 48 ticks costs 50 tokens; balance 0")])
        self.assertEqual([e.tick for e in evs if e.type == "wake" and e.node == "P2"], [0], "no tokens, no polling")
        p1 = [e.tick for e in evs if e.type == "wake" and e.node == "P1"]
        self.assertEqual(p1[:4], [0, 60, 70, 80])
        charged = [e for e in evs if e.type == "order_accepted" and e.node == "P1" and e.payload["outcome"].get("early_wake_tokens")]
        self.assertEqual(len(charged), len(p1) - 1, "every wake after the free one paid")
        world, _ = replay(index.open("main"))
        L = world.ledger
        self.assertEqual(L["burned"], 50 * len(charged))
        self.assertEqual(sum(L["balances"].values()) + world.escrow(), L["minted"] - L["burned"])
        snap = list(snapshots_of(index, evs, "P1"))[-1]
        self.assertEqual(snap["ledger"]["burned"], L["burned"] - 50, "the last wake's fee lands after its snapshot")
        self.assertEqual(snap["rules"]["min_sleep_ticks"], 48)
        from engine.prompt import rules_text
        self.assertIn("Early wakes: a sleep_until fewer than 48 ticks ahead costs 50 tokens", rules_text(evs[0].payload["ruleset"]))
        # A floor without a fee is a hard rule.
        _, evs2 = self.run_game("floor", "rulesets/specialty.json", plan, days="0.3", overrides=["min_sleep_ticks=48"])
        self.assertEqual({e.payload["reason"] for e in evs2 if e.type == "order_rejected"}, {"sleep_until must be at least 48 ticks ahead"})
        self.assertEqual([e.tick for e in evs2 if e.type == "wake" and e.node == "P1"], [0, 60])

    def test_sent_list_records_what_the_station_said(self):
        def plan(s):
            if "genesis" in s["wake_reasons"] and s["node"] == "P1":
                return [{"type": "send", "to": "ALL", "body": "ring check"}, {"type": "send", "to": "P2", "body": "private"},
                        {"type": "sleep_until", "tick": 30}]
            return []
        index, evs = self.run_game("sent", "rulesets/specialty.json", plan, days="0.2")
        snaps = list(snapshots_of(index, evs, "P1"))
        self.assertEqual(snaps[0]["sent"], [])
        later = snaps[1]
        self.assertEqual(later["sent_total"], 2)
        bcast, private = later["sent"]
        self.assertEqual((bcast["kind"], bcast["body"], bcast["tick"]), ("dispatch", "ring check", 0))
        self.assertEqual(sorted(bcast["to"]), ["P2", "P3", "P4", "P5", "P6", "P7", "P8"], "a broadcast is one entry")
        self.assertEqual(bcast["to"]["P2"], 1)
        self.assertEqual(bcast["to"]["P5"], 4)
        self.assertEqual((private["to"], private["body"]), ({"P2": 1}, "private"))
        self.assertEqual(later["rules"]["sent_window"], 10)
        self.assertNotIn("sent", [s for s in snaps[0]["budget"]["trimmed"] if snaps[0]["budget"]["trimmed"][s]])


if __name__ == "__main__":
    unittest.main()
