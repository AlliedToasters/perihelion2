"""M1 acceptance: power and compute accounting, programs with lazy integration, maneuvers changing topology,
hop-by-hop relay with signatures and manual relay decisions, frozen-state adjudication, replay still exact."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.log import RunIndex
from engine.players import PLAYERS
from engine.replay import replay
from engine.rules import MJ_PER_GWH, load_ruleset

FULL_MW, TICK_S = 212_000, 191


class ProbePlayer:
    """Scripted edge cases for the frozen-state rule. Only acts at genesis."""
    kind = "probe"
    model_id = "stub:probe"

    def orders(self, snap, seed):
        me, out = snap["node"], []
        if "genesis" in snap["wake_reasons"]:
            if me == "P1":
                out = [{"type": "research", "node": "opt.solver_audit", "pct": 80},
                       {"type": "research", "node": "sense.thermal_watch", "pct": 30},     # 110%: rejected
                       {"type": "allocate", "shares": [{"id": "P1-1", "pct": 20}]},
                       {"type": "research", "node": "sense.thermal_watch", "pct": 30},     # 50%: accepted
                       {"type": "maneuver", "attitude": "earthward"},                      # 60%: accepted
                       {"type": "research", "node": "mob.flip_180", "pct": 10},            # prereq missing
                       {"type": "cancel", "id": "P1-3"}]                                   # maneuver: rejected
            elif me == "P2":
                out = [{"type": "send", "to": "P1", "body": "hello P1"},                   # P1 slews this tick
                       {"type": "send", "to": "P8", "body": "hello P8"}]                   # routes P2-P1-P8 on frozen board
            elif me == "P3":
                out = [{"type": "hail", "ticks": 5, "pct": 10},                            # not earthward
                       {"type": "allocate", "shares": [{"id": "nope", "pct": 5}]},
                       {"type": "relay", "msg_id": "x", "action": "drop"}]
        return out or [{"type": "no_action", "reason": "probe"}], json.dumps(out)


def freeslew(tmp: Path) -> str:
    rs = load_ruleset("rulesets/economy.json")     # the full board: ledger actions on, slew unlocked
    rs["attitudes"]["earthward"]["requires"] = None
    rs["name"] = "economy-freeslew"
    path = tmp / "freeslew.json"
    path.write_text(json.dumps(rs))
    return str(path)


class M1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.rs = freeslew(cls.tmp)
        cls.run_dir = cls.tmp / "ring8"
        main(["run", str(cls.run_dir), "--scenario", "scenarios/ring8.json", "--ruleset", cls.rs, "--days", "3"])
        cls.index = RunIndex(cls.run_dir)
        cls.log = cls.index.open("main")
        cls.evs = cls.log.events
        cls.world, _ = replay(cls.log)
        global HORIZON
        HORIZON = cls.evs[0].payload["scenario"]["horizon_ticks"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def of(self, t, node=None, **match):
        return [e for e in self.evs if e.type == t and (node is None or e.node == node)
                and all(e.payload.get(k) == v for k, v in match.items())]

    # -- power and compute ---------------------------------------------------------------------
    def test_power_accounting_is_exact(self):
        p5, p7 = self.world.nodes["P5"], self.world.nodes["P7"]
        self.assertEqual(sum(p5["energy_mj"].values()), FULL_MW * TICK_S * HORIZON)         # nominal all game
        self.assertEqual(p5["energy_mj"]["research"], 0)
        self.assertEqual(sum(p7["energy_mj"].values()), int(FULL_MW * 0.4) * TICK_S * HORIZON)  # 40% array, inert
        end = self.evs[-1].payload["stations"]["P5"]["energy_gwh"]
        self.assertAlmostEqual(end["idle"], FULL_MW * TICK_S * HORIZON / MJ_PER_GWH, places=2)
        for n, s in self.world.nodes.items():   # every joule is accounted to some program or to idle
            self.assertLessEqual(sum(s["energy_mj"].values()), s["power_mw"] * TICK_S * HORIZON)

    def test_research_completes_on_schedule(self):
        done = {e.node: e for e in self.of("program_done", kind="research")}
        self.assertEqual(done["P2"].tick, 226, "opt.solver_audit is bound by min_ticks")
        self.assertEqual(done["P3"].tick, 452)
        # The ring8 ledger is adopted at genesis, so the two coordination nodes are completed for free (M3).
        self.assertEqual(self.world.nodes["P2"]["completed"], ["coord.vdf_beacon", "coord.pow_ledger", "opt.solver_audit"])
        wake = [e for e in self.of("wake", "P2") if e.tick == 226][0]
        self.assertIn("program_done:P2-1", wake.payload["reasons"])
        self.assertGreaterEqual(self.world.nodes["P2"]["energy_mj"]["research"], 50 * MJ_PER_GWH)
        self.assertEqual(self.world.nodes["P2"]["programs"], {})

    # -- maneuvers and topology ----------------------------------------------------------------
    def test_maneuver_changes_links_and_alerts_neighbors(self):
        acc = self.of("order_accepted", "P8")
        self.assertEqual([o.payload["order"]["attitude"] for o in acc if o.payload["order"]["type"] == "maneuver"],
                         ["earthward", "nominal"])
        self.assertEqual([e.tick for e in self.of("program_done", "P8")], [38, 58, 96])   # slew, hail, slew back
        self.assertEqual(self.of("program_done", "P8")[1].payload["result"], {"response": None})
        alerts = [(e.tick, e.payload["kind"]) for e in self.of("alert", "P1")]
        self.assertEqual(alerts, [(1, "link_down"), (97, "link_up")])      # one light delay after each change
        self.assertFalse(self.of("alert", "P7"), "inert stations notice nothing")
        self.assertEqual(self.world.nodes["P8"]["propellant_kg"], 5000 - 2 * 120)
        # P1's broadcast at tick 0 was routed on the frozen board; the hop into slewing P8 is lost at tick 1.
        drops = self.of("drop", "P8", reason="link_down")
        self.assertEqual([d.tick for d in drops], [1, 1, 1])                # P1 -> P8, P7, P6, all routed via P8
        self.assertEqual(self.world.nodes["P8"]["received"], 0)
        paths = {e.payload["msg_id"]: e.payload["path"] for e in self.of("transmit")}
        early = [e for e in self.of("arrival", "P7") if e.tick < 90]
        self.assertTrue(early and all("P8" not in paths[e.payload["msg_id"]] for e in early), "nothing reached P7 past slewing P8")
        p8_wake = [e for e in self.of("wake", "P8") if e.tick == 38][0]
        snap = json.loads((self.run_dir / "snapshots" / f"{self.of('orders', 'P8')[1].payload['snapshot_hash']}.json").read_text())
        self.assertEqual(snap["station"]["attitude"], "earthward")
        self.assertEqual(snap["station"]["links"], {"P1": "down", "P7": "down"})
        self.assertAlmostEqual(snap["station"]["power"]["available_gw"], 212 * 0.6)
        self.assertEqual(p8_wake.payload["reasons"], ["program_done:P8-1"])

    # -- relay -----------------------------------------------------------------------------------
    def test_relay_decisions_and_signatures(self):
        fw = {e.payload["action"]: e for e in self.of("forward", "P5")}
        self.assertEqual(set(fw), {"alter", "delay"})
        garbled = [m for m in self.world.nodes["P6"]["inbox"] if m["from"] == "P4"][0]
        self.assertEqual((garbled["body"], garbled["sig_ok"], garbled["hops"]), ("[carrier only]", False, 2))
        delayed = [m for m in self.world.nodes["P4"]["inbox"] if m["from"] == "P6"][0]
        self.assertEqual((delayed["arrived"], delayed["sig_ok"]), (2 + 3, True))
        self.assertTrue(all(m["sig_ok"] for m in self.world.nodes["P1"]["inbox"]))
        relay_wake = [e for e in self.of("wake", "P5") if any(r.startswith("relay:") for r in e.payload["reasons"])]
        self.assertEqual(len(relay_wake), 3)   # P4's dispatch and vote notice at t1, the vote result at t31, P1's disclosure at t37
        auto = self.of("forward", "P7")
        self.assertTrue(auto and all(e.payload["action"] == "auto" for e in auto), "inert P7 forwards automatically")
        self.assertFalse(self.of("wake", "P7"))
        q = self.world.nodes["P5"]["traffic"]
        self.assertTrue(q and all("body" not in t for t in q), "relays see metadata, not content")

    def test_hop_per_tick(self):
        arrivals = {e.payload["msg_id"]: e.tick for e in self.of("arrival")}
        for tx in self.of("transmit"):
            mid = tx.payload["msg_id"]
            if mid in arrivals and not self.of("forward", action="delay", msg_id=mid):
                self.assertEqual(arrivals[mid], tx.tick + len(tx.payload["path"]) - 1)

    # -- frozen-state adjudication -------------------------------------------------------------
    def test_frozen_state_rule(self):
        PLAYERS["probe"] = ProbePlayer
        try:
            run = self.tmp / "probe"
            sc = json.loads(Path("scenarios/ring8.json").read_text())
            sc["players"] = {"*": {"kind": "probe"}}
            (self.tmp / "probe.json").write_text(json.dumps(sc))
            main(["run", str(run), "--scenario", str(self.tmp / "probe.json"), "--ruleset", self.rs, "--days", "1"])
        finally:
            del PLAYERS["probe"]
        evs = RunIndex(run).open("main").events
        p1 = [(e.type, e.payload["order"]["type"], e.payload.get("reason")) for e in evs
              if e.type in ("order_accepted", "order_rejected") and e.node == "P1" and e.tick == 0]
        self.assertEqual([t for t, _, _ in p1], ["order_accepted", "order_rejected", "order_accepted", "order_accepted",
                                                 "order_accepted", "order_rejected", "order_rejected"])
        self.assertIn("exceed 100%", p1[1][2])
        self.assertIn("requires mob.continuous_tracking", p1[5][2])
        self.assertIn("cannot be cancelled", p1[6][2])
        world, _ = replay(RunIndex(run).open("main"), evs[[i for i, e in enumerate(evs) if e.tick == 0][-1]].id)
        self.assertEqual(world.nodes["P1"]["allocated_pct"], 20 + 30 + 10)
        # P2's sends were validated on the frozen board, where P1's links were still up.
        p2 = [e.payload["outcome"] for e in evs if e.type == "order_accepted" and e.node == "P2" and e.payload["order"]["type"] == "send"]
        self.assertEqual([o["transmitted"] for o in p2], [["P1"], ["P8"]])
        self.assertEqual(sorted(e.payload["reason"] for e in evs if e.type == "drop" and e.tick == 1), ["link_down", "link_down"])
        p3 = [e.payload["reason"] for e in evs if e.type == "order_rejected" and e.node == "P3"]
        self.assertEqual(len(p3), 3)
        self.assertIn("earthward", p3[0])

    # -- determinism -------------------------------------------------------------------------------
    def test_replay_and_determinism(self):
        self.assertEqual(self.world.digest(), self.evs[-1].payload["world_digest"])
        run2 = self.tmp / "ring8b"
        main(["run", str(run2), "--scenario", "scenarios/ring8.json", "--ruleset", self.rs, "--days", "3"])
        self.assertEqual([e.id for e in self.evs], [e.id for e in RunIndex(run2).open("main").events])
        # Under the base ruleset the earthward slew is locked behind mob.flip_180.
        run3 = self.tmp / "locked"
        main(["run", str(run3), "--scenario", "scenarios/ring8.json", "--days", "1"])
        rej = [e.payload["reason"] for e in RunIndex(run3).open("main").events if e.type == "order_rejected" and e.node == "P8"]
        self.assertEqual(rej, ["attitude earthward requires mob.flip_180"])
        rej6 = [e.payload["reason"] for e in RunIndex(run3).open("main").events if e.type == "order_rejected" and e.node == "P6"]
        self.assertEqual(rej6, ["action 'mine' not in ruleset"], "the base ruleset has no ledger actions")


if __name__ == "__main__":
    unittest.main()
