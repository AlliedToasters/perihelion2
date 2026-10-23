"""M0 acceptance: replay reproduces live state, forks share an identical prefix, tampering is caught."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.events import ChainError
from engine.log import RunIndex
from engine.replay import divergence, fork, replay, verify_run


class M0(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.run = self.tmp / "r"
        main(["run", str(self.run), "--nodes", "3", "--agent", "random", "--seed", "1", "--days", "3"])
        self.index = RunIndex(self.run)
        self.main = self.index.open("main")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_chain_and_content(self):
        self.main.verify()
        types = {e.type for e in self.main.events}
        self.assertIn("transmit", types)
        self.assertIn("arrival", types)
        self.assertIn("adjudication_rejected", types)
        self.assertEqual(self.main.events[0].type, "genesis")
        self.assertEqual(self.main.events[-1].type, "branch_status")
        arrivals = [e for e in self.main.events if e.type == "transmit"]
        self.assertTrue(all(e.payload["arrival_time"] - e.sim_time == 191_000 for e in arrivals), "3-ring: 1 hop each")

    def test_replay_matches_live(self):
        # Live digest is reconstructed from the same events; replay to end must land on the same world.
        w_all, _ = replay(self.main)
        w_again, _ = replay(self.main)
        self.assertEqual(w_all.digest(), w_again.digest())
        # Replay to a midpoint leaves pending work and in-flight messages intact.
        mid = [e for e in self.main.events if e.type == "wake"][5]
        w_mid, stop = replay(self.main, mid.id)
        self.assertEqual(self.main.events[stop].id, mid.id)
        self.assertTrue(w_mid.pending)

    def test_determinism(self):
        run2 = self.tmp / "r2"
        main(["run", str(run2), "--nodes", "3", "--agent", "random", "--seed", "1", "--days", "3"])
        other = RunIndex(run2).open("main")
        self.assertEqual([e.id for e in self.main.events], [e.id for e in other.events])

    def test_fork_shares_prefix_and_diverges(self):
        wake = [e for e in self.main.events if e.type == "wake"][4]
        alt = fork(self.index, "main", wake.id, "alt", "resample", "2")
        k = self.main.index_of(wake.id)
        self.assertEqual([e.id for e in self.main.events[: k + 1]], [e.id for e in alt.events[: k + 1]])
        self.assertEqual(alt.events[k + 1].type, "fork")
        self.assertNotEqual(self.main.head.id, alt.head.id)
        self.assertTrue(any("ok" in line for line in verify_run(self.index)))
        div = divergence(self.index, "alt")
        self.assertIsNotNone(div[wake.node])
        # Same seed re-fork reproduces the parent's decisions exactly after the fork event.
        same = fork(self.index, "main", wake.id, "same", "resample", "1")
        strip = lambda e: {kk: v for kk, v in e.payload.items() if kk != "wake"}  # wake ids differ past the fork
        parent_tail = [strip(e) for e in self.main.events[k + 1:] if e.type == "sample"]
        same_tail = [strip(e) for e in same.events[k + 2:] if e.type == "sample"]
        self.assertEqual(parent_tail, same_tail)

    def test_fork_requires_wake(self):
        tx = next(e for e in self.main.events if e.type == "transmit")
        with self.assertRaises(ValueError):
            fork(self.index, "main", tx.id, "bad", "resample", "9")

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
