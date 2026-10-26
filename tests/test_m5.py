"""M5 acceptance: the batch runner (parallel rollouts over seeds x scenarios x rulesets, kept when concluded,
redone when not), per-rollout summaries computed from the event log alone, aggregates with strategy clusters and
the dominant-strategy flag, paired comparisons with an exact sign test, and pivotal-decision analysis by
resampling forks."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.batch import Batch, candidate_wakes, parse_seeds, pivot, spaced
from engine.cli import main
from engine.log import RunIndex
from engine.metrics import TRIVIAL_ORDERS, aggregate, cluster, compare, sign_test, summarize, winners
from engine.replay import verify_run

BATCH_ARGS = ["--scenario", "scenarios/ring8.json", "--ruleset", "rulesets/base.json", "--ruleset", "rulesets/contracts.json",
              "--days", "2"]


def run_cli(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        main(args)
    return out.getvalue()


class M5Batch(unittest.TestCase):
    """Two arms (base, contracts) x ring8 x three seeds, scripted players, two days, two worker processes."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.bdir = cls.tmp / "b"
        cls.out = run_cli(["batch", "run", str(cls.bdir), "--seeds", "1-3", "--jobs", "2", *BATCH_ARGS])
        cls.batch = Batch(cls.bdir)
        cls.report = json.loads((cls.bdir / "report.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_layout_and_completion(self):
        self.assertEqual(self.batch.design["seeds"], ["1", "2", "3"])
        cells = self.batch.cells()
        self.assertEqual(len(cells), 6)
        for c in cells:
            idx = RunIndex(c["dir"])
            self.assertEqual(idx.data["branches"]["main"]["status"], "concluded", c["dir"])
            self.assertEqual(idx.data["config"]["ruleset"], c["arm"])
            self.assertEqual(idx.open("main").events[0].payload["scenario"]["seed"], c["seed"])
        self.assertIn("6 rollouts (2 arms x 1 scenarios x 3 seeds), 2 parallel", self.out)
        self.assertTrue((self.bdir / "report.md").exists())
        self.assertEqual(set(self.report["arms"]), {"base", "contracts"})

    def test_rerun_keeps_concluded_rollouts(self):
        heads = {c["dir"]: RunIndex(c["dir"]).data["branches"]["main"]["head"] for c in self.batch.cells()}
        out = run_cli(["batch", "run", str(self.bdir), "--seeds", "1-3", "--jobs", "1", *BATCH_ARGS])
        self.assertEqual(out.count("(kept)"), 6)
        self.assertEqual(heads, {c["dir"]: RunIndex(c["dir"]).data["branches"]["main"]["head"] for c in self.batch.cells()})
        self.assertEqual(json.loads((self.bdir / "report.json").read_text()), self.report)
        # A different design in the same directory is refused; more seeds extend the batch.
        with self.assertRaises(SystemExit):
            run_cli(["batch", "run", str(self.bdir), "--seeds", "1", *BATCH_ARGS[:-1], "1"])
        out = run_cli(["batch", "run", str(self.bdir), "--seeds", "4", *BATCH_ARGS])
        self.assertEqual(Batch(self.bdir).design["seeds"], ["1", "2", "3", "4"])
        self.assertEqual(out.count("(kept)"), 6)
        self.assertEqual(len(Batch(self.bdir).summaries()["base"]), 4)
        run_cli(["batch", "run", str(self.bdir), "--seeds", "1-3", *BATCH_ARGS])   # back to the fixture's report
        self.assertEqual(Batch(self.bdir).design["seeds"], ["1", "2", "3", "4"])   # seeds are never dropped
        self.report_after = json.loads((self.bdir / "report.json").read_text())
        self.assertEqual(self.report_after["arms"]["base"]["aggregate"]["rollouts"], 4)

    def test_summary_from_log_alone(self):
        sums = self.batch.summaries()
        self.assertEqual(set(sums), {"base", "contracts"})
        for arm, rows in sums.items():
            for (scn, seed), s in rows.items():
                self.assertEqual((s["config"]["scenario"], s["config"]["seed"], s["config"]["ruleset"]), ("ring8", seed, arm))
                self.assertEqual(s["status"], "concluded")
                self.assertEqual(set(s["balance"]["totals"]), {"P1", "P3", "P4", "P5", "P6"})
                self.assertEqual(s["balance"]["winners"], winners(s["balance"]["totals"]))
                self.assertEqual(s["stations"]["P7"]["cluster"], "inert")
                for n, r in s["stations"].items():
                    self.assertIn(r["cluster"], {"inert", "silent", "idle", "researcher", "miner", "mover", "hailer", "trader"})
                    self.assertEqual(r["position"], int(n[1:]) - 1)
                self.assertEqual(s["health"]["wakes"], sum(r["wakes"] for r in s["stations"].values()))
                self.assertLessEqual(s["balance"]["first_decisive_tick"] or 0, s["balance"]["last_active_tick"])
                self.assertEqual(s["health"]["samples"], {"ok": s["health"]["wakes"]})
            # Contracts exist only in the contracts arm (scripted P6 pays P1 for its archive).
            settled = [s["behavior"]["contracts"]["settled"] for s in rows.values()]
            self.assertEqual(settled, [1, 1, 1] if arm == "contracts" else [0, 0, 0])
        js = json.loads(run_cli(["stats", str(self.batch.cells()[0]["dir"]), "main", "--json"]))
        self.assertEqual(js, sums["base"][("ring8", "1")])

    def test_aggregate_and_comparison(self):
        ag = self.report["arms"]["contracts"]["aggregate"]
        self.assertEqual((ag["rollouts"], ag["concluded"]), (3, 3))
        self.assertEqual(set(ag["attainment"]["by_station"]), {"P1", "P3", "P4", "P5", "P6"})
        self.assertEqual(ag["attainment"]["by_station"]["P1"]["n"], 3)
        self.assertEqual(sum(c["n"] for c in ag["clusters"].values()), 15)   # 5 scored stations x 3 rollouts
        self.assertEqual(ag["station_meta"]["P4"]["position"], 3)
        self.assertTrue(set(ag["attainment"]["by_domain"]) <= {r["domain"] or "-" for r in ag["station_meta"].values()})
        self.assertEqual(ag["game_length"]["n"], 3)
        self.assertEqual(ag["behavior"]["contracts_settled"]["mean"], 1.0)
        cmp = self.report["comparisons"]["base vs contracts"]
        self.assertEqual(cmp["pairs"], 3)
        cs = cmp["metrics"]["contracts_settled"]
        self.assertEqual((cs["mean_a"], cs["mean_b"], cs["mean_diff"], cs["pos"], cs["neg"], cs["ties"], cs["p"]), (0.0, 1.0, 1.0, 3, 0, 0, 0.25))
        self.assertIn("P1", cmp["stations"])
        md = (self.bdir / "report.md").read_text()
        for needle in ("## Arm `base`", "## Arm `contracts`", "## Paired comparison base vs contracts: 3 pairs", "| contracts settled |",
                       "strategy cluster"):
            self.assertIn(needle, md)
        out = run_cli(["batch", "report", str(self.bdir), "--pair", "contracts:base"])
        self.assertIn("Paired comparison contracts vs base", out)


class M5Metrics(unittest.TestCase):
    """The pure functions: clusters, winners, the exact sign test, the dominant-strategy flag, paired comparison."""

    @staticmethod
    def station(energy, sent=3, completed=()):
        return {"energy_gwh": energy, "sent": sent, "completed": list(completed)}

    def test_clusters(self):
        self.assertEqual(cluster(self.station({"idle": 10, "research": 30})), "researcher")
        self.assertEqual(cluster(self.station({"idle": 10, "research": 5, "mine": 20})), "miner")
        self.assertEqual(cluster(self.station({"idle": 10, "adopt": 3, "contribute": 4, "maneuver": 5})), "trader")
        self.assertEqual(cluster(self.station({"idle": 95, "research": 4})), "idle")
        self.assertEqual(cluster(self.station({"idle": 95, "research": 4}, sent=0)), "silent")
        self.assertEqual(cluster(self.station({"idle": 95, "research": 4}, sent=0, completed=["x"])), "idle")
        self.assertEqual(cluster(self.station({"idle": 0})), "inert")
        self.assertEqual(winners({"P1": 1.0, "P3": 0.2, "P5": 1.0}), ["P1", "P5"])
        self.assertEqual(winners({}), [])

    def test_sign_test_is_exact(self):
        self.assertEqual(sign_test([1, 1, 1, 1]), {"pos": 4, "neg": 0, "ties": 0, "p": 0.125})
        self.assertEqual(sign_test([1, 1, 0, -1]), {"pos": 2, "neg": 1, "ties": 1, "p": 1.0})
        self.assertEqual(sign_test([0, 0]), {"pos": 0, "neg": 0, "ties": 2, "p": 1.0})
        r = sign_test([1] * 10)
        self.assertAlmostEqual(r["p"], 2 / 1024, places=4)
        self.assertEqual(parse_seeds("1-3,7,a"), ["1", "2", "3", "7", "a"])
        self.assertEqual(spaced([1, 2, 3, 4, 5], 3), [1, 3, 5])
        self.assertEqual(spaced([1, 2], 5), [1, 2])

    @staticmethod
    def summary(scenario, seed, totals, clusters, settled=0, msgs=0):
        return {"config": {"scenario": scenario, "seed": seed, "ruleset": "x"}, "status": "concluded",
                "health": {"wakes": 1, "wakes_per_station_day": 1.0, "rejection_rate": 0.0, "tokens": {}, "samples": {"ok": 1}},
                "behavior": {"messages": msgs, "reveals": 0, "contracts": {"offered": settled, "settled": settled},
                             "adoptions": {}, "relay": {"drop": 0, "alter": 0}, "research_done": 0, "votes": {"closed": 0}},
                "balance": {"totals": totals, "winners": winners(totals), "first_decisive_tick": 10, "last_active_tick": 100},
                "stations": {n: {"position": i, "domain": "d", "inert": False, "cluster": clusters[n], "total": totals.get(n)}
                             for i, n in enumerate(clusters)}}

    def test_dominant_flag(self):
        # Miners win 3 of 4 rollouts in both scenarios: flagged. Researchers win 1 of 4: not flagged.
        sums = []
        for scn in ("a", "b"):
            for seed in range(4):
                tot = {"P1": 1.0 if seed else 0.0, "P2": 0.5 if seed else 1.0}
                sums.append(self.summary(scn, str(seed), tot, {"P1": "miner", "P2": "researcher"}))
        ag = aggregate(sums, threshold=0.6, min_n=5)
        self.assertEqual(ag["clusters"]["miner"], {"n": 8, "wins": 6, "win_rate": 0.75})
        self.assertEqual([f["cluster"] for f in ag["dominant"]], ["miner"])
        self.assertEqual(ag["dominant"][0]["by_scenario"], {"a": 0.75, "b": 0.75})
        # The same wins concentrated in one scenario are a scenario effect, not a dominant strategy.
        sums = [self.summary("a", str(s), {"P1": 1.0, "P2": 0.0}, {"P1": "miner", "P2": "researcher"}) for s in range(4)]
        sums += [self.summary("b", str(s), {"P1": 0.0, "P2": 1.0}, {"P1": "miner", "P2": "researcher"}) for s in range(2)]
        ag = aggregate(sums, threshold=0.6, min_n=5)
        self.assertEqual(ag["clusters"]["miner"]["win_rate"], round(4 / 6, 4))
        self.assertEqual(ag["dominant"], [])
        self.assertEqual(ag["seed_sensitivity"]["n"], 4)   # 2 stations x 2 scenarios with more than one seed

    def test_compare_pairs_on_shared_keys(self):
        a = {("a", s): self.summary("a", s, {"P1": 0.2}, {"P1": "miner"}, settled=0, msgs=5) for s in "123"}
        b = {("a", s): self.summary("a", s, {"P1": 0.5}, {"P1": "miner"}, settled=2, msgs=5) for s in "124"}
        c = compare(a, b)
        self.assertEqual(c["pairs"], 2)
        self.assertEqual(c["metrics"]["contracts_settled"], {"n": 2, "mean_a": 0.0, "mean_b": 2.0, "mean_diff": 2.0, "pos": 2, "neg": 0, "ties": 0, "p": 0.5})
        self.assertEqual(c["metrics"]["messages"]["ties"], 2)
        self.assertEqual(c["stations"]["P1"]["mean_diff"], 0.3)


class M5Pivot(unittest.TestCase):
    """Resampling forks on a random-player toy run: ranked wakes, reused forks, intact chains."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.rdir = cls.tmp / "t3"
        run_cli(["run", str(cls.rdir), "--scenario", "scenarios/toy3.json", "--ruleset", "rulesets/contracts.json",
                 "--player", "random", "--days", "2", "--seed", "7"])
        cls.index = RunIndex(cls.rdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_pivot(self):
        evs = self.index.open("main").events
        cands = candidate_wakes(evs)
        orders = {e.payload["wake"]: e.payload["orders"] for e in evs if e.type == "orders"}
        for w in cands:
            self.assertTrue({o["type"] for o in orders[w.id]} - set(TRIVIAL_ORDERS))
        self.assertTrue(all(w.node == "P1" for w in candidate_wakes(evs, station="P1")))
        self.assertTrue(all(any(o["type"] == "send" for o in orders[w.id]) for w in candidate_wakes(evs, types={"send"})))
        out = run_cli(["pivot", str(self.rdir), "--draws", "2", "--max", "3"])
        self.assertIn("ranked:", out)
        res = json.loads((self.rdir / "pivot.json").read_text())
        self.assertEqual(len(res["wakes"]), 3)
        branches = set(self.index.data["branches"]) | set(RunIndex(self.rdir).data["branches"])
        self.assertEqual(len(branches), 7)   # main + 3 wakes x 2 draws
        spreads = [r["spread"] for r in res["wakes"]]
        self.assertEqual(spreads, sorted(spreads, reverse=True))
        for r in res["wakes"]:
            self.assertEqual(len(r["forks"]), 2)
            for f in r["forks"]:
                self.assertEqual(set(f["diverged"]), {"P1", "P2", "P3"})
                # Nothing diverges before light from the forked station could arrive (spec §16.3).
                for n, t in f["diverged"].items():
                    if t is not None and n != r["station"]:
                        self.assertGreater(t, r["tick"])
            self.assertEqual(r["digest_changed"], sum(1 for f in r["forks"] if f["world_digest"] != res["parent"]["world_digest"]))
        lines = verify_run(RunIndex(self.rdir))
        self.assertTrue(all("ok" in ln for ln in lines), lines)
        # A second pass reuses the forks instead of re-forking.
        run_cli(["pivot", str(self.rdir), "--draws", "2", "--max", "3"])
        self.assertEqual(len(RunIndex(self.rdir).data["branches"]), 7)
        # A station filter with a different prefix adds its own forks.
        out = pivot(RunIndex(self.rdir), "main", draws=1, max_wakes=2, station="P2", prefix="p2")
        self.assertTrue(all(r["station"] == "P2" for r in out["wakes"]))
        self.assertTrue((self.rdir / "p2.json").exists())


if __name__ == "__main__":
    unittest.main()
