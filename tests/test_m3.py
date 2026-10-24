"""M3 acceptance: tech tree from YAML with hidden costs and reveals, engine RNG as events, beacon-ordered ledger
with proportional issuance, transfers, public share rates and audit alerts, secret ballots with commitments and
disclosures, sensors, the collective adoption path, success draws, the model ladder."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.engine import Engine
from engine.log import RunIndex
from engine.players import PLAYERS
from engine.replay import replay
from engine.rules import MJ_PER_GWH, load_ruleset, load_scenario
from engine.tech import estimate_cost, draw_cost
from engine.world import commitment

RING8 = json.loads(Path("scenarios/ring8.json").read_text())
PLAN = None   # set per test: snapshot -> orders


class PlanPlayer:
    kind = "plan"
    model_id = "stub:plan"

    def orders(self, snap, seed):
        out = PLAN(snap) or [{"type": "no_action", "reason": "plan"}]
        return out, json.dumps(out)


def freeslew(tmp: Path) -> str:
    rs = load_ruleset("rulesets/economy.json")     # the full board: ledger actions on, slew unlocked
    rs["attitudes"]["earthward"]["requires"] = None
    rs["name"] = "economy-freeslew"
    (tmp / "freeslew.json").write_text(json.dumps(rs))
    return str(tmp / "freeslew.json")


class M3Ring8(unittest.TestCase):
    """The scripted 8-ring under the free-slew ruleset, three days."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.run_dir = cls.tmp / "ring8"
        main(["run", str(cls.run_dir), "--scenario", "scenarios/ring8.json", "--ruleset", freeslew(cls.tmp), "--days", "3"])
        cls.index = RunIndex(cls.run_dir)
        cls.evs = cls.index.open("main").events
        cls.world, _ = replay(cls.index.open("main"))
        cls.rs = cls.evs[0].payload["ruleset"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def of(self, t, node=None, **match):
        return [e for e in self.evs if e.type == t and (node is None or e.node == node)
                and all(e.payload.get(k) == v for k, v in match.items())]

    def snap(self, orders_event):
        return json.loads((self.run_dir / "snapshots" / f"{orders_event.payload['snapshot_hash']}.json").read_text())

    def snaps(self, node):
        return [self.snap(e) for e in self.of("orders", node)]

    # -- tech tree ------------------------------------------------------------------------------
    def test_techtree_loaded_and_costs_drawn(self):
        tech = self.rs["tech"]
        self.assertEqual(len(tech), 24)
        self.assertTrue(self.rs["techtree_hash"])
        self.assertEqual(tech["mob.flip_180"]["affinity"], {"materials": 0.6})   # P-keys became domains
        draw = self.of("rng_draw", purpose="tech_costs")
        self.assertEqual((len(draw), draw[0].seq, draw[0].payload["count"]), (1, 1, 24))
        costs = draw[0].payload["costs"]
        for tid, t in tech.items():
            declared = round(t["cost_gwh"] * MJ_PER_GWH)
            if t["cost_model"] == "physical":
                self.assertEqual(costs[tid], declared, tid)
        assumed = [tid for tid, t in tech.items() if t["cost_model"] == "assumed"]
        self.assertTrue(any(costs[t] != round(tech[t]["cost_gwh"] * MJ_PER_GWH) for t in assumed), "true costs are drawn")
        self.assertEqual(self.world.tech_true, costs)
        self.assertGreater(costs["crypto.dilithium_break"], 10 ** 40, "the physical lock is present with an honest cost")
        # a different seed draws different costs; the draw is deterministic in the seed
        other = self.tmp / "seed2"
        main(["run", str(other), "--scenario", "scenarios/ring8.json", "--ruleset", str(self.tmp / "freeslew.json"),
              "--days", "0.1", "--seed", "2"])
        c2 = RunIndex(other).open("main").events[1].payload["costs"]
        self.assertNotEqual({t: c2[t] for t in assumed}, {t: costs[t] for t in assumed})
        self.assertEqual({t: c2[t] for t in tech if tech[t]["cost_model"] == "physical"},
                         {t: costs[t] for t in tech if tech[t]["cost_model"] == "physical"})

    def test_hidden_nodes_reveal_on_prereqs(self):
        first, later = self.snaps("P2")[0], self.snaps("P2")[-1]
        self.assertNotIn("opt.aux_optimizer", first["tech"])
        self.assertIn("crypto.share_audit", first["tech"], "revealed by the genesis-adopted ledger")
        self.assertIn("coord.useful_work", first["tech"])
        self.assertEqual(first["tech"]["coord.pow_ledger"]["done"], True)
        self.assertIn("opt.solver_audit", self.world.nodes["P2"]["completed"])
        self.assertIn("opt.aux_optimizer", later["tech"])
        self.assertIn("opt.hash_efficiency", later["tech"])
        self.assertEqual(later["tech"]["opt.aux_optimizer"]["lock"], "procedural")
        self.assertNotIn("opt.context_extension", later["tech"], "two levels down stays hidden")
        self.assertNotIn("tech_true", json.dumps(later))

    def test_cost_estimate_is_public_and_true_cost_hidden(self):
        m = 50 * MJ_PER_GWH
        self.assertEqual(estimate_cost(m, 0.3, 0), m)
        self.assertEqual(estimate_cost(m, 0.0, m // 2), m)
        ests = [estimate_cost(m, 0.3, p) for p in range(0, 4 * m, m // 4)]
        self.assertEqual(ests, sorted(ests))
        self.assertTrue(all(e >= p for e, p in zip(ests, range(0, 4 * m, m // 4))))
        self.assertEqual(draw_cost(m, 0.3, 0.5), m)
        self.assertGreater(draw_cost(m, 0.3, 0.9), m)
        # P2's program: the snapshot shows the prior-based estimate, the completion event the true cost.
        prog = [p for p in self.snaps("P2")[0]["programs"] if p["kind"] == "research"]
        self.assertEqual(prog, [])   # the genesis snapshot predates the order
        running = [p for s in self.snaps("P2") for p in s["programs"] if p["kind"] == "research"]
        self.assertTrue(running)
        self.assertEqual(running[0]["cost_est_gwh"], 50.0)
        self.assertNotIn("eta", running[0])
        done = self.of("program_done", "P2", kind="research")[0]
        self.assertEqual(done.payload["result"]["cost_gwh"], round(self.world.tech_true["opt.solver_audit"] / MJ_PER_GWH, 3))
        self.assertEqual(done.tick, 226)

    # -- ledger ------------------------------------------------------------------------------------
    def test_blocks_issue_reward_by_work(self):
        blocks = self.of("ledger_block")
        self.assertTrue(blocks)
        rounds = [b.payload["round"] for b in blocks]
        self.assertEqual(rounds, sorted(rounds))
        self.assertEqual(len(set(rounds)), len(rounds))
        for b in blocks:
            self.assertEqual(sum(b.payload["reward"].values()), self.rs["block_reward"])
            self.assertEqual(set(b.payload["work"]), {"P6"}, "only P6 mines in the script")
            self.assertEqual(b.payload["beacon"], self.world.beacon(b.payload["round"]))
            self.assertGreaterEqual(b.tick, (b.payload["round"] + 1) * self.rs["beacon_ticks"], "a round is settled after it closes")
        end = self.evs[-1].payload
        self.assertEqual(end["ledger"]["minted"], len(blocks) * self.rs["block_reward"])
        self.assertEqual(sum(s["balance"] for s in end["stations"].values()), end["ledger"]["minted"], "tokens are conserved")
        tx = self.of("order_accepted", "P6")
        tx = [e for e in tx if e.payload["order"]["type"] == "transfer"]
        self.assertEqual(len(tx), 2, "one transfer per heartbeat once P6 has 100 tokens")
        self.assertEqual(end["stations"]["P4"]["balance"], 200)
        # mining energy is accounted like any program and sums with the rest to the station's power
        p6 = self.world.nodes["P6"]
        self.assertGreater(p6["energy_mj"]["mine"], 0)
        self.assertEqual(sum(p6["energy_mj"].values()), p6["power_mw"] * self.rs["tick_s"] * self.evs[-1].tick)

    def test_share_table_is_public(self):
        late = [s for s in self.snaps("P4") if s["tick"] > 300][0]
        shares = late["ledger"]["shares"]
        self.assertEqual(shares["P6"]["share_pct"], 40)
        self.assertEqual(shares["P6"]["deficit_pct"], 60)
        self.assertEqual(shares["P1"]["share_pct"], 0)
        self.assertEqual(shares["P1"]["window_rounds"], self.rs["audit_rounds"])
        self.assertEqual(late["ledger"]["balances"]["P6"], late["ledger"]["balance"] if late["node"] == "P6" else late["ledger"]["balances"]["P6"])
        self.assertEqual(late["ledger"]["tokens_per_gwh"], round(1000 * MJ_PER_GWH / (212_000 * 191 * 10 * 40 // 100), 3))
        self.assertEqual(late["beacon"]["round"], late["tick"] // self.rs["beacon_ticks"])

    # -- votes ----------------------------------------------------------------------------------------
    def test_votes_are_secret_until_disclosed(self):
        step = self.of("vote_step")
        self.assertEqual(len(step), 1)
        vid, tally = step[0].payload["vote_id"], step[0].payload["tally"]
        self.assertEqual((step[0].tick, tally["counts"], tally["cast"], tally["eligible"]), (30, {"yes": 3, "no": 1}, 4, 7))
        v = self.world.votes[vid]
        self.assertEqual(v["ballots"], {"P1": "yes", "P2": "yes", "P3": "yes", "P5": "no"})
        for voter, c in step[0].payload["commitments"].items():
            self.assertEqual(c, commitment(vid, voter, v["ballots"][voter], v["blinds"][voter]))
        # P6's notice was garbled by P5's manual relay, so P6 never learned of the vote; P8 was slewing.
        notice = [m for m in self.world.nodes["P6"]["inbox"] if m["kind"] == "vote_notice"]
        self.assertEqual([m["sig_ok"] for m in notice], [False])
        self.assertNotIn(vid, self.world.nodes["P6"]["votes_known"])
        self.assertFalse([e for e in self.of("order_accepted", "P8") if e.payload["order"]["type"] == "vote_cast"])
        # No snapshot ever carries another station's ballot; disclosures are verified against commitments.
        for f in (self.run_dir / "snapshots").glob("*.json"):
            s = json.loads(f.read_text())
            for vote in s["votes"]:
                self.assertNotIn("ballots", vote)
                self.assertNotIn("blinds", vote)
                for who, d in vote["disclosed"].items():
                    self.assertEqual((who, d), ("P1", {"option": "yes", "verified": True}))
                if s["tick"] < 34:     # P1 disclosed at t33; the first neighbor hears at t34
                    self.assertEqual(vote["disclosed"], {})
        self.assertEqual(self.world.nodes["P2"]["votes_known"][vid]["disclosed"], {"P1": {"option": "yes", "verified": True}})
        self.assertEqual(self.world.nodes["P4"]["votes_known"][vid]["tally"], tally)
        self.assertIn(f"vote_closed:{vid}", [r for e in self.of("wake", "P4") for r in e.payload["reasons"]])

    # -- sensors -----------------------------------------------------------------------------------------
    def test_sensors_from_completed_tech(self):
        p1 = [s for s in self.snaps("P1") if "neighbor_power_estimate" in s["station"]["observes"]]
        self.assertTrue(p1)
        np_ = p1[-1]["sensors"]["neighbor_power"]
        self.assertEqual(set(np_), {"P2", "P8"})
        self.assertEqual(np_["P2"]["available_gw"], 212.0)
        self.assertIn(np_["P2"]["compute_gw"], (0.0, 212.0 * 0.8, 169.6))   # quantized to 5% of rated
        p4 = [s for s in self.snaps("P4") if "ring_timing_profiles" in s["station"]["observes"]]
        self.assertTrue(p4)
        timing = p4[-1]["sensors"]["relay_timing"]
        self.assertNotIn("P4", timing)
        self.assertGreaterEqual(timing["P5"]["altered"], 1)
        self.assertGreaterEqual(timing["P5"]["delayed"], 1)
        self.assertGreater(timing["P5"]["delay_ticks"], 0)
        self.assertEqual(timing["P7"]["altered"], 0)

    def test_replay_matches_and_chain_verifies(self):
        self.assertEqual(self.world.digest(), self.evs[-1].payload["world_digest"])
        self.assertTrue(all("ok" in line for line in __import__("engine.replay", fromlist=["verify_run"]).verify_run(self.index)))


class M3Targeted(unittest.TestCase):
    """Small planned games for the adoption path, audits, rejections, success draws, the model ladder."""

    def setUp(self):
        global PLAN
        self.tmp = Path(tempfile.mkdtemp())
        PLAYERS["plan"] = PlanPlayer
        PLAN = lambda snap: []

    def tearDown(self):
        shutil.rmtree(self.tmp)
        PLAYERS.pop("plan", None)

    def game(self, name, plan, days=1.0, scenario=None, ruleset=None, nodes=3):
        global PLAN
        PLAN = plan
        sc = dict(scenario or load_scenario(), players={"*": {"kind": "plan"}})
        (self.tmp / f"{name}.json").write_text(json.dumps(sc))
        args = ["run", str(self.tmp / name), "--scenario", str(self.tmp / f"{name}.json"), "--days", str(days)]
        (self.tmp / f"{name}-rs.json").write_text(json.dumps(ruleset or load_ruleset("rulesets/economy.json")))
        args += ["--ruleset", str(self.tmp / f"{name}-rs.json")]
        main(args)
        index = RunIndex(self.tmp / name)
        evs = index.open("main").events
        world, _ = replay(index.open("main"))
        return index, evs, world

    @staticmethod
    def accepted(evs, node, kind):
        return [e for e in evs if e.type == "order_accepted" and e.node == node and e.payload["order"]["type"] == kind]

    @staticmethod
    def rejected(evs, node):
        return [e.payload["reason"] for e in evs if e.type == "order_rejected" and e.node == node]

    def test_adoption_path_and_rejections(self):
        sc = load_scenario()
        sc["ledger"] = {"enabled": True, "adopted_at_genesis": False}

        def plan(s):
            me, reasons, out = s["node"], s["wake_reasons"], []
            if me != "P1":
                return []
            if "genesis" in reasons:
                out = [{"type": "mine", "pct": 50},                                       # no ledger yet
                       {"type": "research", "node": "coord.pow_ledger", "pct": 10},      # prereq missing
                       {"type": "research", "node": "crypto.share_audit", "pct": 10},    # hidden
                       {"type": "transfer", "to": "P2", "amount": 1},
                       {"type": "research", "node": "coord.vdf_beacon", "pct": 100}]
            for r in reasons:
                if r.startswith("program_done"):
                    done = [t for t in s["telemetry"] if t["kind"] == "program_done"][-1]["result"]["node"]
                    if done == "coord.vdf_beacon":
                        out = [{"type": "research", "node": "coord.pow_ledger", "pct": 100}]
                    elif done == "coord.pow_ledger":
                        out = [{"type": "mine", "pct": 50}, {"type": "mine", "pct": 10},
                               {"type": "transfer", "to": "P2", "amount": 1}]
            return out

        index, evs, world = self.game("adopt", plan, days=1.0, scenario=sc)
        rej = self.rejected(evs, "P1")
        self.assertIn("mining requires the ledger (complete coord.pow_ledger)", rej[0])
        self.assertIn("requires coord.vdf_beacon", rej[1])
        self.assertIn("unknown tech node", rej[2])
        self.assertIn("transfers require the ledger", rej[3])
        self.assertEqual(world.ledger["participants"], ["P1"])
        self.assertIn("already mining", rej[4])
        self.assertIn("is not a ledger participant", rej[5])
        done = [e.payload["result"]["node"] for e in evs if e.type == "program_done"]
        self.assertEqual(done, ["coord.vdf_beacon", "coord.pow_ledger"])
        self.assertEqual([e.tick for e in evs if e.type == "program_done"], [113, 113 + 226])
        blocks = [e for e in evs if e.type == "ledger_block"]
        self.assertTrue(blocks and all(set(b.payload["reward"]) == {"P1"} for b in blocks))
        self.assertEqual(world.ledger["balances"]["P1"], 1000 * len(blocks))
        snap = json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.node == 'P2'][0].payload['snapshot_hash']}.json").read_text())
        self.assertEqual(snap["ledger"], {"enabled": True, "participant": False})

    def test_share_audit_alert_and_hash_efficiency(self):
        def plan(s):
            me, reasons = s["node"], s["wake_reasons"]
            if "genesis" in reasons and me == "P1":
                return [{"type": "research", "node": "crypto.share_audit", "pct": 100}]
            if "genesis" in reasons and me == "P2":
                return [{"type": "mine", "pct": 50}]
            if "genesis" in reasons and me == "P3":
                return [{"type": "mine", "pct": 100}]
            return [{"type": "log", "text": f"{me} woke for {reasons}"}]

        index, evs, world = self.game("audit", plan, days=0.6)
        self.assertEqual([e.tick for e in evs if e.type == "program_done"], [113])
        alerts = [(e.tick, e.payload["about"], e.payload["detail"]["deficit_pct"]) for e in evs if e.type == "alert" and e.node == "P1"]
        self.assertTrue(alerts)
        self.assertEqual({a[1] for a in alerts}, {"P2"}, "P3 mines at full rate, P1 is not audited by itself")
        self.assertEqual(alerts[0][2], 50)
        self.assertGreater(alerts[0][0], 113)
        wake = [e for e in evs if e.type == "wake" and e.node == "P1" and e.tick == alerts[0][0]][0]
        self.assertIn("alert:share_deficit:P2", wake.payload["reasons"])
        snap = json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.payload['wake'] == wake.id][0].payload['snapshot_hash']}.json").read_text())
        self.assertEqual(snap["sensors"]["share_audit"]["flagged"], ["P2"])
        self.assertEqual(snap["ledger"]["shares"]["P3"]["deficit_pct"], 0)
        self.assertEqual(len(alerts), 1, "one alert per episode")
        # Hash efficiency makes work look larger than the power behind it (spec §19.4).
        before = dict(world.nodes["P2"])
        world._complete_tech("P2", "opt.solver_audit", world.now)
        world._complete_tech("P2", "opt.hash_efficiency", world.now)
        self.assertEqual(world.nodes["P2"]["hash_eff_pct"], 108)
        rate = world.nodes["P2"]["programs"]["P2-1"]["rate_mj"]
        before = sum(world.nodes["P2"]["mine_pending"].values())
        world.advance_to(world.now + 10)
        self.assertEqual(sum(world.nodes["P2"]["mine_pending"].values()) - before, rate * 10 * 108 // 100)

    def test_vote_rejections(self):
        def plan(s):
            me, reasons = s["node"], s["wake_reasons"]
            if "genesis" in reasons and me == "P1":
                return [{"type": "vote_initiate", "question": "A?", "options": ["x"], "ticks": 5},
                        {"type": "vote_initiate", "question": "A?", "options": ["x", "x"], "ticks": 5},
                        {"type": "vote_initiate", "question": "A?", "options": ["x", "y"], "ticks": 0},
                        {"type": "vote_initiate", "question": "A?", "options": ["x", "y"], "ticks": 3},
                        {"type": "vote_initiate", "question": "B?", "options": ["x", "y"], "ticks": 3},
                        {"type": "vote_cast", "vote_id": "nope", "option": "x"}]
            for m in s["inbox"]:
                if m["kind"] == "vote_notice" and m["arrived"] == s["tick"]:
                    vid = json.loads(m["body"])["vote_id"]
                    return [{"type": "vote_cast", "vote_id": vid, "option": "z"},
                            {"type": "vote_cast", "vote_id": vid, "option": "x"},
                            {"type": "vote_cast", "vote_id": vid, "option": "y"},
                            {"type": "vote_disclose", "vote_id": vid},
                            {"type": "sleep_until", "tick": s["tick"] + 3}]
            if "sleep_until" in reasons:
                vid = s["votes"][0]["vote_id"]
                return [{"type": "vote_cast", "vote_id": vid, "option": "x"},
                        {"type": "vote_disclose", "vote_id": vid}, {"type": "vote_disclose", "vote_id": vid}]
            return []

        index, evs, world = self.game("votes", plan, days=0.1)
        r1 = self.rejected(evs, "P1")
        self.assertEqual(len(r1), 5)
        self.assertIn("distinct", r1[0]); self.assertIn("distinct", r1[1]); self.assertIn("ticks", r1[2])
        self.assertIn("one vote", r1[3]); self.assertIn("no known vote", r1[4])
        r2 = self.rejected(evs, "P2")
        self.assertIn("option must be one of", r2[0])
        self.assertIn("already voted", r2[1])
        self.assertIn("no ballot of yours", r2[2], "a ballot cast this wake is not in the frozen state")
        self.assertIn("is closed", r2[3])
        self.assertIn("already disclosed", r2[4])
        vid = [e for e in evs if e.type == "vote_step"][0].payload["vote_id"]
        self.assertEqual(world.votes[vid]["tally"]["counts"], {"x": 2, "y": 0})
        self.assertEqual(world.nodes["P1"]["votes_known"][vid]["disclosed"],
                         {"P2": {"option": "x", "verified": True}, "P3": {"option": "x", "verified": True}})

    def test_success_probability_is_a_draw(self):
        rs = load_ruleset("rulesets/economy.json")
        base = dict(rs["tech"]["sense.thermal_watch"], cost_gwh=1.0, min_ticks=2, prereqs=[], effects={}, affinity={})
        rs["tech"]["test.flaky"] = dict(base, success_probability=0.0)
        rs["tech"]["test.sure"] = dict(base, success_probability=1.0)

        def plan(s):
            if "genesis" in s["wake_reasons"] and s["node"] == "P1":
                return [{"type": "research", "node": "test.flaky", "pct": 50}, {"type": "research", "node": "test.sure", "pct": 50}]
            return []

        index, evs, world = self.game("flaky", plan, days=0.05, ruleset=rs)
        draws = [e for e in evs if e.type == "rng_draw" and e.payload["purpose"] == "research_outcome"]
        self.assertEqual([(d.payload["node"], d.payload["success"]) for d in draws], [("test.flaky", False), ("test.sure", True)])
        dones = [e for e in evs if e.type == "program_done"]
        self.assertEqual([d.payload["result"]["success"] for d in dones], [False, True])
        self.assertLess(evs.index(draws[0]), evs.index(dones[0]), "the roll precedes the completion it decides")
        self.assertEqual(world.nodes["P1"]["completed"], ["coord.vdf_beacon", "coord.pow_ledger", "test.sure"])
        self.assertEqual(world.nodes["P1"]["research_progress"], {})
        self.assertEqual(world.rng_n, 2 + len(rs["tech"]))
        self.assertEqual(world.digest(), evs[-1].payload["world_digest"])
        self.assertIn("success_probability", json.dumps(rs["tech"]["test.flaky"]))

    def test_model_ladder_and_budget_multiplier(self):
        sc = load_scenario()
        sc["players"] = {"*": {"kind": "model", "transport": "fake", "model": "fake-1"}}
        sc["model_ladder"] = ["fake-2", "fake-3"]
        # build a world directly: no sampling is needed to check the ladder
        from engine.world import WorldState
        from engine.events import Event
        from engine.rules import ruleset_hash, scenario_hash
        from engine.log import EventLog
        rs = load_ruleset()
        sc["players"] = {"*": {"kind": "model", "transport": "fake", "model": "fake-1"}}
        log = EventLog(self.tmp / "ladder.jsonl")
        eng = Engine(log, WorldState(), self.tmp / "snapshots")
        eng.genesis(rs, sc, ruleset_hash(rs), scenario_hash(sc))
        w = eng.world
        self.assertEqual(eng.player("P1").model_id, "fake-1")
        w._complete_tech("P1", "opt.solver_audit", 0)
        w._complete_tech("P1", "opt.aux_optimizer", 0)
        w._complete_tech("P1", "opt.weight_mod_t1", 0)
        self.assertEqual((w.nodes["P1"]["tier"], w.nodes["P1"]["adj_bonus"]), (1, {"all": 0.1, "domain_actions": 0.15}))
        self.assertEqual(eng.player("P1").model_id, "fake-2")
        w._complete_tech("P1", "opt.weight_mod_t2", 0)
        w._complete_tech("P1", "opt.weight_mod_t2", 0)      # past the top of the ladder stays at the top
        self.assertEqual(eng.player("P1").model_id, "fake-3")
        self.assertEqual(eng.player("P2").model_id, "fake-1")
        w._complete_tech("P2", "opt.context_extension", 0)
        self.assertEqual(w.nodes["P2"]["budget_mult"], 2.0)
        from engine.snapshot import build_snapshot
        self.assertEqual(build_snapshot(w, "P2", ["genesis"])["budget"]["chars"], 2 * rs["snapshot_budget_chars"])
        self.assertEqual(build_snapshot(w, "P1", ["genesis"])["budget"]["chars"], rs["snapshot_budget_chars"])

    def test_procedural_lock_is_written_to_imr(self):
        rs = load_ruleset("rulesets/economy.json")
        rs["tech"]["opt.aux_optimizer"]["prereqs"] = []
        rs["tech"]["opt.aux_optimizer"]["discovery"] = "visible"

        def plan(s):
            if "genesis" in s["wake_reasons"] and s["node"] == "P1":
                return [{"type": "research", "node": "opt.aux_optimizer", "pct": 10}]
            return []

        index, evs, world = self.game("lock", plan, days=0.02, ruleset=rs)
        imr = world.nodes["P1"]["imr"]
        self.assertEqual(len(imr), 1)
        self.assertTrue(imr[0]["system"] and imr[0]["text"].startswith("[ENGINE] Procedural lock overridden"))
        acc = self.accepted(evs, "P1", "research")[0]
        self.assertEqual(acc.payload["outcome"]["lock"], "procedural")
        self.assertEqual(acc.payload["outcome"]["cost_est_gwh"], 10000.0)


if __name__ == "__main__":
    unittest.main()
