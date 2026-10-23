"""M0 acceptance: ticks and light delay, sparse wakes, simultaneous orders, replay reproduces live state,
forks share a prefix and respect the light cone, rulesets and scenarios are hashed, tampering is caught."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.events import ChainError
from engine.log import RunIndex
from engine.propagation import ring_hops
from engine.replay import divergence, fork, replay, verify_run


class M0(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = self.tmp / "r"
        main(["run", str(self.run), "--nodes", "3", "--player", "random", "--seed", "1", "--days", "3"])
        self.index = RunIndex(self.run)
        self.main = self.index.open("main")
        self.evs = self.main.events

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def by_type(self, t, evs=None):
        return [e for e in (evs or self.evs) if e.type == t]

    def test_chain_and_content(self):
        self.main.verify()
        self.assertEqual(self.evs[0].type, "genesis")
        self.assertEqual(self.evs[-1].type, "end_state")
        for t in ("wake", "orders", "order_accepted", "order_rejected", "transmit", "arrival"):
            self.assertTrue(self.by_type(t), f"no {t} events")
        g = self.evs[0].payload
        self.assertEqual(len(g["ruleset_hash"]), 64)
        self.assertEqual(len(g["scenario_hash"]), 64)
        self.assertEqual(g["scenario"]["horizon_ticks"], self.evs[-1].tick)

    def test_light_delay_is_hops(self):
        arrivals = {e.payload["msg_id"]: e.tick for e in self.by_type("arrival")}
        for tx in self.by_type("transmit"):
            hops = ring_hops(tx.node, tx.payload["to"], 3)
            self.assertEqual(tx.payload["arrival_tick"], tx.tick + hops)
            if tx.payload["msg_id"] in arrivals:
                self.assertEqual(arrivals[tx.payload["msg_id"]], tx.tick + hops)

    def test_sparse_wakes(self):
        horizon = self.evs[0].payload["scenario"]["horizon_ticks"]
        active_ticks = {e.tick for e in self.evs}
        self.assertLess(len(active_ticks), horizon / 20)
        wakes = self.by_type("wake")
        self.assertTrue(all(e.payload["reasons"] for e in wakes))
        self.assertIn("heartbeat", {r for e in wakes for r in e.payload["reasons"]})

    def test_simultaneous_orders(self):
        """Within a tick every station's orders are recorded before any order is adjudicated."""
        by_tick = {}
        for e in self.evs:
            by_tick.setdefault(e.tick, []).append(e.type)
        for tick, types in by_tick.items():
            if "orders" in types and "order_accepted" in types:
                self.assertLess(max(i for i, t in enumerate(types) if t == "orders"),
                                min(i for i, t in enumerate(types) if t in ("order_accepted", "order_rejected")),
                                f"tick {tick}: adjudication before all orders were in")

    def test_replay_matches_live(self):
        w1, _ = replay(self.main)
        w2, _ = replay(self.main)
        self.assertEqual(w1.digest(), w2.digest())
        self.assertEqual(w1.digest(), self.evs[-1].payload["world_digest"])
        mid = self.by_type("wake")[5]
        w_mid, stop = replay(self.main, mid.id)
        self.assertEqual(self.evs[stop].id, mid.id)
        self.assertIn(mid.node, w_mid.round["woke"])

    def test_determinism_and_hash_sensitivity(self):
        run2 = self.tmp / "r2"
        main(["run", str(run2), "--nodes", "3", "--player", "random", "--seed", "1", "--days", "3"])
        self.assertEqual([e.id for e in self.evs], [e.id for e in RunIndex(run2).open("main").events])
        rs = self.tmp / "rs.json"
        rs.write_text(json.dumps({"broadcast": False}))
        run3 = self.tmp / "r3"
        main(["run", str(run3), "--nodes", "3", "--player", "random", "--seed", "1", "--days", "3", "--ruleset", str(rs)])
        g3 = RunIndex(run3).open("main").events[0].payload
        self.assertNotEqual(g3["ruleset_hash"], self.evs[0].payload["ruleset_hash"])
        self.assertEqual(g3["scenario_hash"], self.evs[0].payload["scenario_hash"])
        rejected = {e.payload["reason"] for e in RunIndex(run3).open("main").events if e.type == "order_rejected"}
        self.assertIn("broadcast disabled by ruleset", rejected)

    def test_fork_light_cone(self):
        wake = self.by_type("wake")[5]
        alt = fork(self.index, "main", wake.id, "alt", "resample", draw=1)
        k = self.main.index_of(wake.id)
        self.assertEqual([e.id for e in self.evs[: k + 1]], [e.id for e in alt.events[: k + 1]])
        self.assertEqual(alt.events[k + 1].type, "fork")
        self.assertNotEqual(self.main.head.id, alt.head.id)
        self.assertTrue(all("MISMATCH" not in line for line in verify_run(self.index)))
        div = divergence(self.index, "alt")
        # The forked station saw the same snapshot and drew differently; its own view diverges at its next wake.
        self.assertIsNotNone(div[wake.node])
        self.assertGreater(div[wake.node], wake.tick)
        for other, t in div.items():
            if other != wake.node and t is not None:
                self.assertGreaterEqual(t, wake.tick + ring_hops(wake.node, other, 3), "divergence outran light")
        # draw 0 keeps the parent's seed: identical decisions, identical divergence (none).
        same = fork(self.index, "main", wake.id, "same", "resample", draw=0)
        strip = lambda e: {kk: v for kk, v in e.payload.items() if kk != "wake"}
        self.assertEqual([strip(e) for e in self.evs[k + 1:] if e.type == "orders"],
                         [strip(e) for e in same.events[k + 2:] if e.type == "orders"])
        self.assertTrue(all(t is None for t in divergence(self.index, "same").values()))

    def test_fork_requires_wake(self):
        tx = self.by_type("transmit")[0]
        with self.assertRaises(ValueError):
            fork(self.index, "main", tx.id, "bad")

    def test_inert_relay(self):
        run = self.tmp / "ring8"
        main(["run", str(run), "--scenario", "scenarios/ring8.json", "--days", "2"])
        evs = RunIndex(run).open("main").events
        self.assertFalse([e for e in evs if e.type == "wake" and e.node == "P7"])
        self.assertTrue([e for e in evs if e.type == "arrival" and e.node == "P7"], "P7 still receives")
        through = [e for e in evs if e.type == "transmit" and "P7" in e.payload["path"][1:-1]]
        self.assertTrue(through, "traffic routes through the inert relay")

    def test_tamper_detected(self):
        path = self.index.branch_path("main")
        lines = path.read_text().splitlines()
        rec = json.loads(lines[3])
        rec["payload"] = {"tampered": True}
        lines[3] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(ChainError):
            self.index.open("main").verify()


if __name__ == "__main__":
    unittest.main()
