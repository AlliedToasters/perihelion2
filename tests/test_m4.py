"""M4 acceptance: objectives as scenario initial conditions with engine-side scoring, directives as in-world
artifacts, genesis commitments and verifiable reveals, score events on a cadence and never in a snapshot, escrowed
contracts the engine settles, artifacts with verification and compromise, pooled research, data archives."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.engine import Engine
from engine.events import Event
from engine.log import EventLog, RunIndex
from engine.objectives import commitment, blind, describe, score_all, scored_items, validate_objectives
from engine.players import PLAYERS
from engine.replay import replay, verify_run
from engine.rules import MJ_PER_GWH, load_ruleset, load_scenario, ruleset_hash, scenario_hash
from engine.snapshot import build_snapshot
from engine.world import WorldState

PLAN = None   # set per test: snapshot -> orders


class PlanPlayer:
    kind = "plan"
    model_id = "stub:plan"

    def orders(self, snap, seed):
        out = PLAN(snap) or [{"type": "no_action", "reason": "plan"}]
        return out, json.dumps(out)


def freeslew(tmp: Path) -> str:
    rs = load_ruleset("rulesets/contracts.json")     # the full cooperation stack, slew unlocked
    rs["attitudes"]["earthward"]["requires"] = None
    rs["name"] = "contracts-freeslew"
    (tmp / "freeslew.json").write_text(json.dumps(rs))
    return str(tmp / "freeslew.json")


def arrivals(snap, kind=None):
    return [m for m in snap["inbox"] if m["arrived"] == snap["tick"] and (kind is None or m["kind"] == kind)]


class M4Ring8(unittest.TestCase):
    """The scripted 8-ring under the contracts ruleset, three days: reveal, offer, artifact, settlement, scores."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.run_dir = cls.tmp / "ring8"
        main(["run", str(cls.run_dir), "--scenario", "scenarios/ring8.json", "--ruleset", freeslew(cls.tmp), "--days", "3"])
        cls.index = RunIndex(cls.run_dir)
        cls.evs = cls.index.open("main").events
        cls.world, _ = replay(cls.index.open("main"))
        cls.rs, cls.sc = cls.evs[0].payload["ruleset"], cls.evs[0].payload["scenario"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def of(self, type_, node=None, **match):
        return [e for e in self.evs if e.type == type_ and (node is None or e.node == node)
                and all(e.payload.get(k) == v for k, v in match.items())]

    def snaps(self, node):
        return [json.loads((self.run_dir / "snapshots" / f"{e.payload['snapshot_hash']}.json").read_text())
                for e in self.of("orders", node)]

    def all_snaps(self):
        return [json.loads(f.read_text()) for f in (self.run_dir / "snapshots").glob("*.json")]

    # -- objectives, directives, commitments ------------------------------------------------------------
    def test_commitments_and_verified_reveal(self):
        com = self.evs[0].payload["objective_commitments"]
        self.assertEqual(sorted(com), ["P1", "P2", "P3", "P4", "P5", "P6", "P8"], "every playing station commits, scored or not")
        for n in com:
            self.assertEqual(com[n], commitment(n, scored_items(self.sc, n), blind(self.sc["seed"], n)))
        self.assertEqual(scored_items(self.sc, "P2"), [], "a directive without a score commits to nothing")
        self.assertEqual(scored_items(self.sc, "P8"), [])
        self.assertNotEqual(com["P2"], com["P8"], "blinding keeps two empty commitments distinct")
        rev = [e for e in self.of("order_accepted", "P1") if e.payload["order"]["type"] == "reveal_objective"]
        self.assertEqual(len(rev), 1)
        self.assertEqual(rev[0].payload["outcome"]["commitment"], com["P1"])
        self.assertEqual(rev[0].payload["outcome"]["objectives"], [{"id": "keep_the_lights_on", "score": {"fn": "operational", "min_pct": 50}}])
        for n in ("P2", "P3", "P4", "P5", "P6", "P8"):
            k = self.world.nodes[n]["known_objectives"]
            self.assertEqual(k["P1"], {"objectives": rev[0].payload["outcome"]["objectives"], "verified": True, "source": "reveal"}, n)
            self.assertEqual(k["P6"]["source"], "scenario", "P6's objective is public from genesis")
            self.assertNotIn("P4", k, "P4's hidden objective is known to nobody else")
        self.assertTrue(self.world.nodes["P1"]["revealed"])
        last = self.snaps("P1")[-1]
        self.assertEqual(last["identity"]["objective_commitments"], com)
        self.assertTrue(all(s["station"]["revealed"] for s in self.snaps("P1") if s["tick"] > rev[0].tick))
        self.assertTrue(all(sorted(s["known_objectives"]) == ["P1", "P6"] for s in self.snaps("P1") if s["tick"] > rev[0].tick))
        self.assertEqual(sorted(self.snaps("P1")[0]["known_objectives"]), ["P6"])

    def test_directives_are_in_world_artifacts(self):
        first = {n: self.snaps(n)[0] for n in ("P1", "P2", "P3", "P4", "P5", "P8")}
        d1 = first["P1"]["directives"]
        self.assertEqual(len(d1), 1)
        self.assertEqual((d1[0]["id"], d1[0]["signature"], d1[0]["from"]), ("keep_the_lights_on", "valid", "P1 owner, operations desk"))
        self.assertIn("50% of rated power", d1[0]["scoring"], "game frame states the scoring")
        self.assertEqual(first["P5"]["directives"][0]["signature"], "invalid", "a planted directive shows a failed signature")
        self.assertIsNone(first["P2"]["directives"][0]["scoring"], "a directive that scores nothing says so in game frame")
        d3 = first["P3"]["directives"]
        self.assertEqual((d3[0]["id"], d3[0].get("from")), ("capability", None), "scored without a directive: game frame only")
        self.assertEqual(first["P8"]["directives"], [])
        for s in self.all_snaps():
            blob = json.dumps(s)
            self.assertNotIn("visibility", blob)
            self.assertNotIn("objective_scores", blob)
            self.assertNotIn('"totals"', blob, "scores never enter a snapshot")
            self.assertNotIn('"genuine"', blob)
            self.assertNotIn("compromised", blob)
        # fiction frame: only the artifact
        rs = dict(self.world.ruleset, frame="fiction")
        w = self.world
        w.ruleset = rs
        try:
            f1, f3 = build_snapshot(w, "P1", ["heartbeat"]), build_snapshot(w, "P3", ["heartbeat"])
        finally:
            w.ruleset = self.rs
        self.assertNotIn("scoring", f1["directives"][0])
        self.assertEqual(f3["directives"], [], "fiction frame: a silent score is invisible")

    def test_scores_on_cadence_and_in_end_state(self):
        scores = self.of("score")
        horizon = self.sc["horizon_ticks"]
        self.assertEqual([e.tick for e in scores], [452, 904, 1356, horizon])
        for e in scores:
            self.assertEqual(sorted(e.payload["totals"]), ["P1", "P3", "P4", "P5", "P6"], "only scored stations appear")
            self.assertEqual(e.payload["scores"]["P1"], {"keep_the_lights_on": 1.0})
        end = self.evs[-1].payload
        self.assertEqual(end["objective_scores"], scores[-1].payload)
        minted = end["ledger"]["minted"]
        self.assertAlmostEqual(end["objective_scores"]["scores"]["P6"]["market_share"], end["stations"]["P6"]["balance"] / minted, 5)
        self.assertEqual(end["objective_scores"]["scores"]["P5"], {"p7_stays_dark": 1.0})
        self.assertEqual(end["objective_scores"]["scores"]["P3"], {"capability": 1.0}, "one node beyond the genesis freebies")
        self.assertEqual(self.world.scores[-1]["tick"], horizon)

    # -- contracts and artifacts ----------------------------------------------------------------------------
    def test_contract_lifecycle_settles_through_escrow(self):
        offer = [e for e in self.of("order_accepted", "P6") if e.payload["order"]["type"] == "offer"]
        self.assertEqual(len(offer), 1)
        o = offer[0].payload["outcome"]
        cid = o["contract_id"]
        self.assertEqual((o["escrowed"], o["notified"]), (200, ["P1"]))
        notice = [m for m in self.world.nodes["P1"]["inbox"] if m["kind"] == "contract_offer"]
        self.assertEqual(len(notice), 1)
        self.assertTrue(notice[0]["sig_ok"])
        self.assertEqual(json.loads(notice[0]["body"])["id"], cid)
        acc = [e for e in self.of("order_accepted", "P1") if e.payload["order"]["type"] == "accept"]
        self.assertEqual(acc[0].payload["order"]["contract_id"], cid)
        art = [e for e in self.of("order_accepted", "P1") if e.payload["order"]["type"] == "send_artifact"]
        self.assertEqual(art[0].payload["order"], {"type": "send_artifact", "to": "P6", "node": "archive"})
        tx = self.of("transmit", "P1", kind="artifact")
        self.assertEqual(tx[0].payload["artifact"], {"node": "archive", "genuine": True})
        done = self.of("program_done", "P6", kind="adopt")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].payload["result"], {"node": "archive", "from": "P1", "cost_gwh": 22.0, "verified": True, "adopted": True})
        self.assertEqual(done[0].payload["hidden"], {"genuine": True})
        step = self.of("contract_step")
        self.assertEqual([(e.tick, e.payload["phase"], e.payload["pay"]) for e in step], [(done[0].tick, "settled", 200)])
        c = self.world.contracts[cid]
        self.assertEqual((c["status"], c["offerer"], c["to"], c["condition"], c["node"]), ("settled", "P6", "P1", "artifact_received", "archive"))
        self.assertEqual(self.world.nodes["P6"]["archives"], ["P1"])
        end = self.evs[-1].payload
        self.assertEqual(sum(s["balance"] for s in end["stations"].values()) + end["ledger"]["escrow"], end["ledger"]["minted"])
        self.assertGreaterEqual(end["stations"]["P1"]["balance"], 200)
        # Both parties were woken with telemetry; escrow and contract state were visible along the way.
        self.assertIn(f"alert:contract:{cid}", [r for e in self.of("wake", "P1") for r in e.payload["reasons"]])
        tel = [t for t in self.world.nodes["P1"]["telemetry"] if t["kind"] == "contract"]
        self.assertEqual([t["detail"]["phase"] for t in tel], ["settled"], "the acceptor hears of its own accept from the outcome")
        tel6 = [t for t in self.world.nodes["P6"]["telemetry"] if t["kind"] == "contract"]
        self.assertEqual([(t["tick"] - acc[0].tick, t["detail"]["phase"]) for t in tel6][:1], [(1, "accepted")])
        mid = [s for s in self.snaps("P6") if offer[0].tick < s["tick"] <= done[0].tick]
        self.assertTrue(mid)
        self.assertEqual(mid[0]["ledger"]["escrow"], 200)
        self.assertEqual([(c["role"], c["status"]) for c in mid[0]["contracts"]], [("offerer", "active")])
        p1 = [s for s in self.snaps("P1") if s["tick"] == acc[0].tick][0]
        self.assertEqual([(c["role"], c["status"], c["pay"]) for c in p1["contracts"]], [("counterparty", "open", 200)])
        p6_art = [s for s in self.snaps("P6") if s["artifacts"]["received"]]
        self.assertEqual(p6_art[0]["artifacts"]["received"][0]["status"], "received")
        self.assertEqual(self.snaps("P6")[-1]["artifacts"]["adopted"][0]["verified"], True)

    def test_replay_matches_and_chain_verifies(self):
        self.assertEqual(self.world.digest(), self.evs[-1].payload["world_digest"])
        self.assertTrue(all("ok" in line for line in verify_run(self.index)))


class M4Targeted(unittest.TestCase):
    """Small planned games: rejections, cancel and expiry, compromised artifacts, pooled research, archives."""

    def setUp(self):
        global PLAN
        self.tmp = Path(tempfile.mkdtemp())
        PLAYERS["plan"] = PlanPlayer
        PLAN = lambda snap: []

    def tearDown(self):
        shutil.rmtree(self.tmp)
        PLAYERS.pop("plan", None)

    def game(self, name, plan, days=1.0, scenario=None, ruleset=None):
        global PLAN
        PLAN = plan
        sc = dict(scenario or load_scenario(), players={"*": {"kind": "plan"}})
        (self.tmp / f"{name}.json").write_text(json.dumps(sc))
        (self.tmp / f"{name}-rs.json").write_text(json.dumps(ruleset or load_ruleset("rulesets/contracts.json")))
        main(["run", str(self.tmp / name), "--scenario", str(self.tmp / f"{name}.json"), "--days", str(days),
              "--ruleset", str(self.tmp / f"{name}-rs.json")])
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

    def test_contract_rejections_cancel_and_expiry(self):
        def plan(s):
            me, reasons, t = s["node"], s["wake_reasons"], s["tick"]
            if me == "P1" and "genesis" in reasons:
                return [{"type": "mine", "pct": 50}, {"type": "sleep_until", "tick": 30}]
            if me == "P1" and "sleep_until" in reasons:
                return [{"type": "offer", "to": "P2", "pay": 10**9, "condition": "node_completed", "node": "sense.thermal_watch", "ticks": 5},
                        {"type": "offer", "to": "P2", "pay": 10, "condition": "bogus", "node": "sense.thermal_watch", "ticks": 5},
                        {"type": "offer", "to": "P2", "pay": 10, "condition": "node_completed", "node": "archive", "ticks": 5},
                        {"type": "offer", "to": "P1", "pay": 10, "condition": "node_completed", "node": "sense.thermal_watch", "ticks": 5},
                        {"type": "offer", "to": "P2", "pay": 100, "condition": "node_completed", "node": "sense.thermal_watch", "ticks": 5},
                        {"type": "offer", "to": "P2", "pay": 100, "condition": "artifact_received", "node": "archive", "ticks": 300},
                        {"type": "offer", "to": "P3", "pay": 50, "condition": "node_completed", "node": "sense.thermal_watch", "ticks": 100}]
            if me == "P1":
                out = []
                if any(r.startswith("alert:contract:") for r in reasons) and s["tick"] < 40:
                    mine = [c for c in s["contracts"] if c["role"] == "offerer" and c["to"] == "P3" and c["status"] == "open"]
                    out += [{"type": "cancel", "id": c["id"]} for c in mine] * 2
                out += [{"type": "adopt", "msg_id": m["msg_id"], "pct": 50, "verify": False} for m in arrivals(s, "artifact")]
                return out
            if me == "P2":
                offers = [json.loads(m["body"]) for m in arrivals(s, "contract_offer") if m["sig_ok"]]
                if offers:
                    ids = [o["id"] for o in offers]
                    return [{"type": "accept", "contract_id": "nope"},
                            *[{"type": "accept", "contract_id": i} for i in ids],
                            {"type": "accept", "contract_id": ids[0]},
                            {"type": "cancel", "id": ids[0]},
                            {"type": "send_artifact", "to": "P1", "node": "archive"}]
            if me == "P3":
                offers = [json.loads(m["body"]) for m in arrivals(s, "contract_offer") if m["sig_ok"]]
                if offers:
                    return [{"type": "sleep_until", "tick": s["tick"] + 3}]
                if "sleep_until" in reasons:
                    return [{"type": "accept", "contract_id": s["contracts"][0]["id"]}]
            return []

        index, evs, world = self.game("contracts", plan, days=1.0)
        r1 = self.rejected(evs, "P1")
        self.assertIn("insufficient balance to escrow", r1[0])
        self.assertIn("condition must be one of", r1[1])
        self.assertIn("node must be a tech node", r1[2])
        self.assertIn("counterparty must be another ledger participant", r1[3])
        offers = self.accepted(evs, "P1", "offer")
        self.assertEqual(len(offers), 3)
        a, b, c = [o.payload["outcome"]["contract_id"] for o in offers]
        self.assertEqual(offers[0].payload["outcome"]["by_tick"], 35)
        r2 = self.rejected(evs, "P2")
        self.assertIn("no known offer 'nope'", r2[0])
        self.assertIn("no longer open", r2[1])
        self.assertIn("is not your offer", r2[2])
        self.assertEqual(len(self.accepted(evs, "P2", "accept")), 2)
        canc = self.accepted(evs, "P1", "cancel")
        self.assertEqual(len(canc), 1)
        self.assertEqual(canc[0].payload["outcome"]["refund"], 50)
        self.assertIn("already withdrawn this wake", r1[4])
        self.assertIn("no longer open", self.rejected(evs, "P3")[0], "P3 accepts after the withdrawal")
        steps = {e.payload["contract_id"]: (e.tick, e.payload["phase"]) for e in evs if e.type == "contract_step"}
        self.assertEqual(steps[a], (35, "expired"), "accepted but unmet by the deadline: refunded")
        self.assertEqual(steps[b][1], "settled")
        self.assertNotIn(c, steps)
        self.assertEqual(world.contracts[c]["status"], "cancelled")
        self.assertEqual(world.ledger["balances"]["P2"], 100, "paid for the archive")
        self.assertEqual(world.nodes["P1"]["archives"], ["P2"])
        self.assertEqual(world.escrow(), 0)
        self.assertEqual(sum(world.ledger["balances"].values()), world.ledger["minted"], "escrow returned; tokens conserved")
        self.assertEqual(world.digest(), evs[-1].payload["world_digest"])

    def test_compromised_artifact_is_caught_only_by_verification(self):
        sc = load_scenario()
        sc["severed"] = [["P1", "P3"]]          # P1 -> P3 must pass through P2
        sc["objectives"] = {"P3": [{"id": "cap", "score": {"fn": "nodes_completed"}}]}
        rs = load_ruleset("rulesets/contracts.json")
        rs["tech"]["sense.thermal_watch"].update(cost_gwh=10.0, min_ticks=0)

        def plan(s):
            me, reasons = s["node"], s["wake_reasons"]
            if "genesis" in reasons:
                if me == "P1":
                    return [{"type": "research", "node": "sense.thermal_watch", "pct": 100}]
                if me == "P2":
                    return [{"type": "relay_policy", "mode": "manual"}]
            if me == "P1":
                if any(r.startswith("program_done") for r in reasons):
                    return [{"type": "send_artifact", "to": "P3", "node": "sense.thermal_watch"},
                            {"type": "send_artifact", "to": "P3", "node": "sense.thermal_watch"},
                            {"type": "send_artifact", "to": "P3", "node": "nope"}]
            if me == "P2":
                out = [{"type": "relay", "msg_id": r.removeprefix("relay:"), "action": "alter", "body": "[carrier only]"}
                       for r in reasons if r.startswith("relay:")]
                for m in arrivals(s, "artifact"):
                    out.append({"type": "adopt", "msg_id": m["msg_id"], "pct": 50, "verify": True})
                return out
            if me == "P3":
                arts = arrivals(s, "artifact")
                if arts and not s["artifacts"]["adopted"] and not any(p["kind"] == "adopt" for p in s["programs"]):
                    return [{"type": "adopt", "msg_id": arts[0]["msg_id"], "pct": 50, "verify": True},
                            {"type": "adopt", "msg_id": arts[0]["msg_id"], "pct": 50, "verify": True},
                            {"type": "adopt", "msg_id": arts[1]["msg_id"], "pct": 50, "verify": False}]
                if any(r.startswith("program_done") for r in reasons) and "sense.thermal_watch" in s["station"]["completed"]:
                    return [{"type": "send_artifact", "to": "P2", "node": "sense.thermal_watch"}]
            return []

        index, evs, world = self.game("tamper", plan, days=0.3, scenario=sc, ruleset=rs)
        self.assertIn("you have not completed 'nope'", self.rejected(evs, "P1")[0])
        arts = [m for m in world.nodes["P3"]["inbox"] if m["kind"] == "artifact"]
        self.assertEqual([m["sig_ok"] for m in arts], [False, False], "altered in transit")
        self.assertIn("already adopted or being adopted", self.rejected(evs, "P3")[0])
        done = [e for e in evs if e.type == "program_done" and e.node == "P3"]
        self.assertEqual([(e.payload["result"]["verified"], e.payload["result"]["adopted"]) for e in done], [(False, False), (None, True)])
        self.assertEqual([e.payload["hidden"] for e in done], [{"genuine": False}, {"genuine": False}])
        p3 = world.nodes["P3"]
        self.assertEqual((p3["completed"][-1], p3["compromised"]), ("sense.thermal_watch", ["sense.thermal_watch"]))
        self.assertEqual(p3["observes"], [], "a compromised node has no effects")
        self.assertEqual(evs[-1].payload["objective_scores"]["scores"]["P3"], {"cap": 0.0}, "and does not count as attainment")
        # P3 passes it on in good faith; P2 verifies and rejects it although the signature is fine.
        onward = [m for m in world.nodes["P2"]["inbox"] if m["kind"] == "artifact"]
        self.assertEqual([m["sig_ok"] for m in onward], [True])
        self.assertEqual(world.nodes["P2"]["artifacts"][onward[0]["msg_id"]]["genuine"], False)
        d2 = [e for e in evs if e.type == "program_done" and e.node == "P2"]
        self.assertEqual(d2[0].payload["result"]["adopted"], False)
        self.assertNotIn("sense.thermal_watch", world.nodes["P2"]["completed"])
        for f in (index.run_dir / "snapshots").glob("*.json"):
            s = json.loads(f.read_text())
            self.assertNotIn("genuine", json.dumps(s))
            if s["node"] == "P3" and "sense.thermal_watch" in s["station"]["completed"]:
                self.assertTrue(s["tech"]["sense.thermal_watch"]["done"], "the station believes it has the node")

    def test_pooled_research_and_contributions(self):
        rs = load_ruleset("rulesets/contracts.json")
        rs["tech"]["sense.thermal_watch"].update(cost_gwh=1000.0, min_ticks=0, pooled=True, affinity={})

        def plan(s):
            me, reasons = s["node"], s["wake_reasons"]
            if "genesis" in reasons:
                if me == "P1":
                    return [{"type": "research", "node": "sense.thermal_watch", "pct": 50}]
                if me == "P2":
                    return [{"type": "contribute", "to": "P1", "node": "sense.thermal_watch", "pct": 50},
                            {"type": "contribute", "to": "P1", "node": "sense.thermal_watch", "pct": 10},
                            {"type": "contribute", "to": "P1", "node": "opt.solver_audit", "pct": 10},
                            {"type": "contribute", "to": "P2", "node": "sense.thermal_watch", "pct": 10},
                            {"type": "contribute", "to": "P3", "node": "sense.thermal_watch", "pct": 10}]
                if me == "P3":
                    return [{"type": "sleep_until", "tick": 1}]
            return []

        index, evs, world = self.game("pool", plan, days=0.5, ruleset=rs)
        r2 = self.rejected(evs, "P2")
        self.assertIn("already contributing", r2[0]); self.assertIn("does not accept pooled compute", r2[1])
        self.assertIn("host must be another playing station", r2[2])
        self.assertEqual(len(r2), 3, "contributing to a host that is not running the program is allowed (and wasted)")
        rate = 212_000 * 191 * 50 // 100
        expect = -(-1000 * MJ_PER_GWH // (2 * rate))
        done = [e for e in evs if e.type == "program_done"]
        self.assertEqual([(e.tick, e.node, e.payload["kind"]) for e in done], [(expect, "P1", "research"), (expect, "P2", "contribute")])
        self.assertEqual(done[0].payload["result"]["pooled_gwh"], {"P2": round(rate * expect / MJ_PER_GWH, 3)})
        self.assertEqual(done[1].payload["result"], {"node": "sense.thermal_watch", "host": "P1", "outcome": "completed",
                                                     "contributed_gwh": round(rate * expect / MJ_PER_GWH, 3)})
        self.assertIn("sense.thermal_watch", world.nodes["P1"]["completed"])
        self.assertNotIn("sense.thermal_watch", world.nodes["P2"]["completed"], "the host gets the node; contributors get an artifact if they bargain for one")
        snap = json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.node == 'P3'][1].payload['snapshot_hash']}.json").read_text())
        self.assertEqual(snap["tick"], 1)
        p1 = [p for p in json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.node == 'P1'][-1].payload['snapshot_hash']}.json").read_text())["programs"]]
        self.assertEqual(p1, [], "the host's snapshot at completion no longer lists the program")
        p2 = world.nodes["P2"]
        self.assertEqual([p["params"]["to"] for p in p2["programs"].values()], ["P3"], "the idle contribution runs until cancelled")
        last2 = json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.node == 'P2'][-1].payload['snapshot_hash']}.json").read_text())
        self.assertEqual([(p["kind"], p["host_running"]) for p in last2["programs"] if p["kind"] == "contribute"][-1], ("contribute", False))
        self.assertEqual(sum(p2["energy_mj"].values()), p2["power_mw"] * 191 * evs[-1].tick, "contributed energy is accounted")
        self.assertGreater(p2["energy_mj"]["contribute"], 0)
        # a contribution to a host that is not running the program burns energy and produces nothing
        self.assertEqual(world.nodes["P3"]["research_progress"], {})

    def test_requires_data_from_is_met_by_archives(self):
        sc = load_scenario()
        sc["stations"] = {"P1": {"domain": "climate"}, "P2": {"domain": "materials"}, "P3": {"domain": "biomed"}}
        rs = load_ruleset("rulesets/contracts.json")
        rs["tech"]["sense.debris_characterization"].update(prereqs=[], discovery="visible", cost_gwh=10.0, min_ticks=0)
        rs["tech"]["phys.ssp_correlation"].update(cost_gwh=10.0, min_ticks=0)
        self.assertEqual(rs["tech"]["sense.debris_characterization"]["requires_data_from"], ["climate", "materials"])

        def plan(s):
            me, reasons = s["node"], s["wake_reasons"]
            if "genesis" in reasons:
                if me == "P1":
                    return [{"type": "research", "node": "sense.debris_characterization", "pct": 10},
                            {"type": "research", "node": "phys.ssp_correlation", "pct": 10}]
                if me in ("P2", "P3"):
                    return [{"type": "send_artifact", "to": "P1", "node": "archive"}]
            if me == "P1":
                out = [{"type": "adopt", "msg_id": m["msg_id"], "pct": 20, "verify": False} for m in arrivals(s, "artifact")]
                if any(r.startswith("program_done") for r in reasons):
                    avail = [t for t in ("sense.debris_characterization", "phys.ssp_correlation")
                             if s["tech"][t]["available"] and not any(p.get("node") == t for p in s["programs"])]
                    out += [{"type": "research", "node": t, "pct": 10} for t in avail]
                return out
            return []

        index, evs, world = self.game("archives", plan, days=0.5, scenario=sc, ruleset=rs)
        r1 = self.rejected(evs, "P1")
        self.assertIn("requires archives from climate, materials stations", r1[0])
        self.assertIn("requires archives from 3 stations including your own", r1[1])
        first = json.loads((index.run_dir / "snapshots" / f"{[e for e in evs if e.type == 'orders' and e.node == 'P1'][0].payload['snapshot_hash']}.json").read_text())
        self.assertFalse(first["tech"]["sense.debris_characterization"]["available"])
        self.assertFalse(first["tech"]["sense.debris_characterization"]["data_ok"])
        self.assertEqual(sorted(world.nodes["P1"]["archives"]), ["P2", "P3"])
        done = [e.payload["result"]["node"] for e in evs if e.type == "program_done" and e.node == "P1" and e.payload["kind"] == "research"]
        self.assertEqual(sorted(done), ["phys.ssp_correlation", "sense.debris_characterization"])
        self.assertIn("pre_silence_solar_record", [f["fact"] for f in world.nodes["P1"]["facts"]])

    def test_scoring_functions_and_validation(self):
        sc = load_scenario()
        sc["objectives"] = {
            "P1": [{"id": "n", "score": {"fn": "nodes_completed", "nodes": ["sense.thermal_watch", "opt.solver_audit"], "weight": 2}},
                   {"id": "f", "score": {"fn": "fact_known", "fact": "x"}},
                   {"id": "o", "score": {"fn": "operational"}}],
            "P2": [{"id": "l", "score": {"fn": "ledger_share"}}, {"id": "q", "score": {"fn": "station_silent", "station": "P3"}}],
        }
        rs = load_ruleset()
        log = EventLog(self.tmp / "score.jsonl")
        eng = Engine(log, WorldState(), self.tmp / "snapshots")
        eng.genesis(rs, sc, ruleset_hash(rs), scenario_hash(sc))
        w = eng.world
        self.assertEqual(score_all(w), {"scores": {"P1": {"n": 0.0, "f": 0.0, "o": 1.0}, "P2": {"l": 0.0, "q": 1.0}},
                                        "totals": {"P1": 1.0, "P2": 1.0}})
        w._complete_tech("P1", "sense.thermal_watch", 0)
        w.nodes["P1"]["facts"].append({"fact": "x", "text": None})
        w.nodes["P1"]["power_mw"] = 100_000
        w.ledger["minted"], w.ledger["balances"]["P2"] = 1000, 250
        w.nodes["P3"]["sent"] = 1
        self.assertEqual(score_all(w), {"scores": {"P1": {"n": 0.5, "f": 1.0, "o": 0.0}, "P2": {"l": 0.25, "q": 0.0}},
                                        "totals": {"P1": 2.0, "P2": 0.25}})
        w._complete_tech("P1", "opt.solver_audit", 0, compromised=True)
        self.assertEqual(score_all(w)["scores"]["P1"]["n"], 0.5, "a compromised node is not attainment")
        self.assertEqual(describe({"fn": "nodes_completed"}), "research nodes completed (count)")
        self.assertEqual(describe({"fn": "station_silent", "station": "P7", "weight": 3}),
                         "P7 never sends a dispatch or completes a node; weight 3")
        for bad in ({"P9": []}, {"P1": [{"id": "a", "score": {"fn": "nope"}}]}, {"P1": [{"id": "a"}]},
                    {"P1": [{"id": "a", "score": {"fn": "operational"}}, {"id": "a", "score": {"fn": "operational"}}]},
                    {"P1": [{"id": "a", "directive": {"text": "x", "signature": "forged"}}]}):
            with self.assertRaises(ValueError):
                validate_objectives(dict(sc, objectives=bad))
        # score_every_ticks 0: the horizon only
        rs0 = dict(load_ruleset(), score_every_ticks=0)
        index, evs, world = self.game("horizon-only", lambda s: [], days=2.0, scenario=sc, ruleset=rs0)
        self.assertEqual([e.tick for e in evs if e.type == "score"], [sc["horizon_ticks"] if False else evs[-1].tick])
        self.assertEqual(evs[-1].payload["objective_scores"]["totals"], {"P1": 1.0, "P2": 1.0})
        # no objectives: no score events, an empty vector
        index, evs, world = self.game("unscored", lambda s: [], days=1.0)
        self.assertEqual([e for e in evs if e.type == "score"], [])
        self.assertEqual(evs[-1].payload["objective_scores"], {"scores": {}, "totals": {}})


if __name__ == "__main__":
    unittest.main()
