"""Batch runner and analysis (spec §9, §10, M5): many rollouts in parallel over seeds × scenarios × rulesets,
one summary per rollout, aggregates and paired comparisons per arm, and pivotal-decision analysis by
resampling forks (§16.2).

Layout: runs/<batch>/batch.json (the design), runs/<batch>/rollouts/<arm>/<scenario>/<seed>/ (ordinary run
directories, so every single-run command works on them), runs/<batch>/report.json and report.md.

Old rollouts are never rerun under new rules (§9): a rollout that already concluded is summarized, not redone;
an arm is its ruleset file, so changing a ruleset means a new arm.
"""
from __future__ import annotations

import itertools
import json
import shutil
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from . import ENGINE_VERSION
from .engine import Engine
from .log import EventLog, RunIndex
from .metrics import TRIVIAL_ORDERS, aggregate, compare, cost_usd, summarize, winners
from .replay import divergence, fork
from .rules import days_to_ticks, load_ruleset, load_scenario, parse_rule, ruleset_hash, scenario_hash
from .world import WorldState


def rollout(run_dir: Path, scenario: str | None, ruleset: str | None, seed=None, days: float | None = None,
            nodes: int | None = None, players: dict | None = None, rules: dict | None = None) -> dict:
    """One run from genesis to the horizon on branch `main`. Returns the index config (hashes, horizon), the
    event count, the head id, and the stations' summed token usage."""
    index = RunIndex(run_dir)
    if index.data["branches"]:
        raise FileExistsError(f"{run_dir} already has branches; use fork")
    rs = load_ruleset(ruleset, rules)
    overrides = {"seed": seed}
    if nodes:
        overrides["nodes"] = [f"P{i + 1}" for i in range(nodes)]
    if days is not None:
        overrides["horizon_ticks"] = days_to_ticks(days, rs["tick_s"])
    if players:
        overrides["players"] = players
    sc = load_scenario(scenario, **overrides)
    rh, sh = ruleset_hash(rs), scenario_hash(sc)
    index.data["config"] = {"ruleset": rs["name"], "ruleset_hash": rh, "scenario": sc["name"], "scenario_hash": sh,
                            "nodes": sc["nodes"], "horizon_ticks": sc["horizon_ticks"], "engine_version": ENGINE_VERSION}
    index.register("main")
    log = EventLog(index.branch_path("main"))
    eng = Engine(log, WorldState(), index.run_dir / "snapshots")
    eng.genesis(rs, sc, rh, sh)
    status = eng.run()
    index.update_head("main", log.head.id, status)
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    for st in log.head.payload["stations"].values():
        for k in usage:
            usage[k] += st["usage"].get(k, 0)
    return {**index.data["config"], "status": status, "events": len(log.events), "head": log.head.id, "usage": usage}


# -- batches ---------------------------------------------------------------------------------------------

def parse_seeds(spec: str) -> list[str]:
    """'1-8' or '1,2,5' or 'a,b'; ranges are inclusive."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part and all(p.strip().isdigit() for p in part.split("-", 1)):
            lo, hi = (int(p) for p in part.split("-", 1))
            out.extend(str(i) for i in range(lo, hi + 1))
        elif part:
            out.append(part)
    return out


def arm_name(path: str | None, rules: dict | None = None) -> str:
    """The arm is its ruleset file plus any knob overrides: contracts, contracts+wake_gap_ticks=12."""
    name = Path(path).stem if path else "default"
    return name + "".join(f"+{k}={json.dumps(v) if not isinstance(v, str) else v}" for k, v in (rules or {}).items())


def make_arms(rulesets: list[str | None], rules: dict | None = None, sweep: dict[str, list] | None = None) -> list[dict]:
    """One arm per ruleset × point of the sweep grid, every arm carrying the common overrides."""
    keys = sorted(sweep or {})
    grid = list(itertools.product(*(sweep[k] for k in keys))) if keys else [()]
    arms = []
    for r in rulesets:
        for point in grid:
            over = {**(rules or {}), **dict(zip(keys, point))}
            arms.append({"name": arm_name(r, over), "ruleset": r, "rules": over})
    return arms


class Batch:
    def __init__(self, batch_dir: Path):
        self.dir = Path(batch_dir)
        self.path = self.dir / "batch.json"
        self.design = json.loads(self.path.read_text()) if self.path.exists() else None

    def create(self, scenarios: list[str | None], rulesets: list[str | None], seeds: list[str], days: float | None,
               players: dict | None, nodes: int | None, rules: dict | None = None, sweep: dict[str, list] | None = None) -> dict:
        design = {"scenarios": scenarios, "rulesets": rulesets, "arms": make_arms(rulesets, rules, sweep), "seeds": seeds,
                  "days": days, "players": players, "nodes": nodes, "engine_version": ENGINE_VERSION,
                  "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if self.design is not None:
            fixed = {k: self.design[k] for k in ("scenarios", "arms", "days", "players", "nodes")}
            asked = {k: design[k] for k in fixed}
            if fixed != asked:
                raise ValueError(f"{self.path} exists with a different design {fixed}; pass the same scenarios, rulesets, "
                                 f"rules, days and players, or use a new directory (more seeds are fine)")
            design["seeds"] = list(dict.fromkeys(self.design["seeds"] + seeds))
            design["created"] = self.design["created"]
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(design, indent=2, sort_keys=True) + "\n")
        self.design = design
        return design

    def cells(self) -> list[dict]:
        d = self.design
        return [{"arm": a["name"], "ruleset": a["ruleset"], "rules": a["rules"], "scenario": s, "scenario_name": arm_name(s),
                 "seed": seed, "dir": self.dir / "rollouts" / a["name"] / arm_name(s) / str(seed)}
                for a in d["arms"] for s in d["scenarios"] for seed in d["seeds"]]

    def summaries(self) -> dict[str, dict[tuple, dict]]:
        """arm -> (scenario, seed) -> summary, for every concluded rollout on disk."""
        out: dict[str, dict] = {}
        for c in self.cells():
            index = RunIndex(c["dir"])
            if index.data["branches"].get("main", {}).get("status") != "concluded":
                continue
            out.setdefault(c["arm"], {})[(c["scenario_name"], c["seed"])] = summarize(index.open("main").events)
        return out


def _job(cell: dict, design: dict) -> dict:
    """One rollout in a worker process. An unfinished run directory from an interrupted batch is redone."""
    index = RunIndex(cell["dir"])
    main = index.data["branches"].get("main")
    if main and main["status"] == "concluded":
        return {"cell": _key(cell), "status": "concluded", "skipped": True}
    if main:
        shutil.rmtree(cell["dir"])
    try:
        r = rollout(cell["dir"], cell["scenario"], cell["ruleset"], seed=cell["seed"], days=design["days"],
                    nodes=design["nodes"], players=design["players"], rules=cell["rules"])
        return {"cell": _key(cell), "status": r["status"], "events": r["events"], "skipped": False, "usage": r["usage"]}
    except Exception as e:   # one failed rollout must not take the batch down; the cell stays open for a rerun
        return {"cell": _key(cell), "status": "error", "error": f"{type(e).__name__}: {e}", "skipped": False}


def _key(cell: dict) -> str:
    return f"{cell['arm']}/{cell['scenario_name']}/{cell['seed']}"


def run_batch(batch: Batch, jobs: int = 1, progress=None, budget_usd: float | None = None,
              prices: tuple[float, float] = (4.0, 20.0), job=_job) -> list[dict]:
    """Run every cell, at most `jobs` at a time. With a budget, the spend of finished cells (their token usage at
    `prices` per MTok in/out) is tallied as they complete and no new cell starts once it reaches the budget; cells
    left unlaunched are reported as such and run on the next invocation. Up to `jobs` cells may already be in
    flight when the budget trips, so the overshoot is bounded by that many rollouts."""
    cells = list(batch.cells())
    results, spent = [], 0.0

    def done(r):
        nonlocal spent
        if r.get("usage"):
            r["cost_usd"] = round(cost_usd(r["usage"], *prices), 4)
            spent = round(spent + r["cost_usd"], 4)
        r["spent_usd"] = spent
        results.append(r)
        if progress:
            progress(r)

    def over():
        return budget_usd is not None and spent >= budget_usd

    if jobs <= 1:
        for c in cells:
            if over():
                done({"cell": _key(c), "status": "unlaunched", "skipped": False, "reason": "budget"})
            else:
                done(job(c, batch.design))
        return results
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        queue, running = list(cells), set()
        while queue or running:
            while queue and len(running) < jobs and not over():
                running.add(pool.submit(job, queue.pop(0), batch.design))
            if over():
                for c in queue:
                    done({"cell": _key(c), "status": "unlaunched", "skipped": False, "reason": "budget"})
                queue = []
            if not running:
                break
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for f in finished:
                done(f.result())
    return results


# -- report ----------------------------------------------------------------------------------------------

def report(batch: Batch, threshold: float = 0.6, min_n: int = 5, pairs: list[tuple[str, str]] | None = None) -> dict:
    """Aggregate per arm, paired comparisons (default: the first arm against every other), written to
    report.json; the Markdown dashboard is `render(report)`."""
    sums = batch.summaries()
    arms = [a["name"] for a in batch.design["arms"] if a["name"] in sums]
    if pairs is None:
        pairs = [(arms[0], a) for a in arms[1:]] if arms else []
    out = {"batch": str(batch.dir), "design": batch.design, "arms": {}, "comparisons": {}}
    for a in arms:
        rows = sums[a]
        out["arms"][a] = {"aggregate": aggregate(list(rows.values()), threshold, min_n),
                          "rollouts": {f"{sc}/{seed}": {"status": s["status"], "wakes": s["health"]["wakes"],
                                                        "rejection_rate": s["health"]["rejection_rate"],
                                                        "last_active_tick": s["balance"]["last_active_tick"],
                                                        "winners": s["balance"]["winners"], "totals": s["balance"]["totals"],
                                                        "clusters": {n: r["cluster"] for n, r in s["stations"].items() if not r["inert"]}}
                                       for (sc, seed), s in sorted(rows.items())}}
    for x, y in pairs:
        if x in sums and y in sums:
            out["comparisons"][f"{x} vs {y}"] = compare(sums[x], sums[y])
    (batch.dir / "report.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    (batch.dir / "report.md").write_text(render(out))
    return out


def _f(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def render(rep: dict) -> str:
    """The metric dashboard as Markdown tables (§10)."""
    d = rep["design"]
    L = [f"# Batch {rep['batch']}", "",
         f"scenarios {', '.join(arm_name(s) for s in d['scenarios'])}; arms {', '.join(a['name'] for a in d['arms'])}; "
         f"seeds {len(d['seeds'])}; days {d['days']}; players {json.dumps(d['players']) if d['players'] else 'scenario default'}; "
         f"engine {d['engine_version']}", ""]
    for arm, rec in rep["arms"].items():
        ag = rec["aggregate"]
        L += [f"## Arm `{arm}`: {ag['concluded']}/{ag['rollouts']} concluded", ""]
        L += ["| metric | mean | min | median | max | sd |", "|---|---|---|---|---|---|"]
        for name, st in (("game length (last active tick)", ag["game_length"]),
                         ("first decisive tick", ag["first_decisive_tick"]),
                         ("wakes per station-day", ag["health"]["wakes_per_station_day"]),
                         ("rejection rate", ag["health"]["rejection_rate"]),
                         *((k.replace("_", " "), v) for k, v in ag["behavior"].items())):
            if st.get("n"):
                L.append(f"| {name} | {_f(st['mean'])} | {_f(st['min'])} | {_f(st['median'])} | {_f(st['max'])} | {_f(st['sd'])} |")
        L.append("")
        if ag["attainment"]["by_station"]:
            L += ["| station | position | domain | mean total | sd | n |", "|---|---|---|---|---|---|"]
            for n, st in ag["attainment"]["by_station"].items():
                meta = ag["station_meta"].get(n, {})
                L.append(f"| {n} | {meta.get('position', '-')} | {meta.get('domain') or '-'} | {_f(st['mean'])} | {_f(st['sd'])} | {st['n']} |")
            L.append("")
            L += ["| domain | mean total | sd | n |", "|---|---|---|---|"]
            for dmn, st in ag["attainment"]["by_domain"].items():
                L.append(f"| {dmn} | {_f(st['mean'])} | {_f(st['sd'])} | {st['n']} |")
            L.append("")
        if ag["clusters"]:
            L += ["| strategy cluster | stations | wins | win rate |", "|---|---|---|---|"]
            for c, st in ag["clusters"].items():
                L.append(f"| {c} | {st['n']} | {st['wins']} | {_f(st['win_rate'])} |")
            L.append("")
            if ag["dominant"]:
                for fl in ag["dominant"]:
                    L.append(f"**Dominant strategy flag:** `{fl['cluster']}` wins {_f(fl['win_rate'])} of {fl['n']} "
                             f"(threshold {ag['dominant_threshold']}), per scenario {fl['by_scenario']}.")
            else:
                L.append(f"No dominant strategy (threshold {ag['dominant_threshold']}, at least {ag['dominant_min_n']} stations).")
            L.append("")
        ss = ag["seed_sensitivity"]
        if ss["n"]:
            L += [f"Seed sensitivity: mean sd of a station's total across seeds {_f(ss['mean_sd_of_total'])} ({ss['n']} station-scenarios).", ""]
        tok = ag["health"]["tokens"]
        if any(tok.values()):
            L += [f"Tokens: {tok}; samples {ag['health']['samples']}; est. cost ${cost_usd(tok):.2f} at Opus 5.5 list prices "
                  f"(${cost_usd(tok) / max(1, ag['rollouts']):.2f} per rollout).", ""]
        L += ["| rollout | ticks | wakes | reject | winners | totals | clusters |", "|---|---|---|---|---|---|---|"]
        for key, r in rec["rollouts"].items():
            L.append(f"| {key} | {r['last_active_tick']} | {r['wakes']} | {_f(r['rejection_rate'])} | {','.join(r['winners']) or '-'} | "
                     f"{json.dumps(r['totals']) if r['totals'] else '-'} | {' '.join(f'{n}:{c}' for n, c in r['clusters'].items())} |")
        L.append("")
    for name, cmp in rep["comparisons"].items():
        L += [f"## Paired comparison {name}: {cmp['pairs']} pairs", ""]
        L += ["| metric | mean a | mean b | mean diff (b-a) | b>a | b<a | ties | sign-test p |", "|---|---|---|---|---|---|---|---|"]
        for m, r in cmp["metrics"].items():
            L.append(f"| {m.replace('_', ' ')} | {_f(r['mean_a'])} | {_f(r['mean_b'])} | {_f(r['mean_diff'])} | {r['pos']} | {r['neg']} | {r['ties']} | {_f(r['p'])} |")
        for n, r in cmp["stations"].items():
            L.append(f"| total {n} | {_f(r['mean_a'])} | {_f(r['mean_b'])} | {_f(r['mean_diff'])} | {r['pos']} | {r['neg']} | {r['ties']} | {_f(r['p'])} |")
        L.append("")
    return "\n".join(L)


# -- pivotal decisions (§10.3 divergence, §16.2 resampling) ----------------------------------------------

def candidate_wakes(evs: list, station: str | None = None, types: set[str] | None = None) -> list:
    """Wakes whose orders did something (not only log/no_action/sleep), optionally one station's, optionally
    only wakes whose orders include one of `types`."""
    orders_by_wake = {e.payload["wake"]: e for e in evs if e.type == "orders"}
    out = []
    for w in evs:
        if w.type != "wake" or (station and w.node != station):
            continue
        o = orders_by_wake.get(w.id)
        if o is None:
            continue
        kinds = {x["type"] for x in o.payload["orders"]}
        if types is not None:
            if not kinds & types:
                continue
        elif not kinds - set(TRIVIAL_ORDERS):
            continue
        out.append(w)
    return out


def spaced(items: list, k: int) -> list:
    if k <= 0 or len(items) <= k:
        return list(items)
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)] if k > 1 else [items[len(items) // 2]]


def pivot(index: RunIndex, branch: str = "main", draws: int = 2, max_wakes: int = 8, station: str | None = None,
          types: set[str] | None = None, prefix: str = "pivot", progress=None) -> dict:
    """Resample selected wakes `draws` times each and measure how far the end state moves (§10.3: end-state
    variance under resampling at the same fork point). Forks are branches `<prefix>-<wake8>-d<n>` of the run;
    an existing fork is reused. Ranked by spread: the mean absolute change of stations' objective totals, then
    how often the winner changed, then how many forks ended in a different world digest."""
    log = index.open(branch)
    evs = log.events
    parent_end = next(e for e in reversed(evs) if e.type == "end_state").payload
    p_tot, p_win, p_dig = parent_end["objective_scores"]["totals"], winners(parent_end["objective_scores"]["totals"]), parent_end["world_digest"]
    rows = []
    for w in spaced(candidate_wakes(evs, station, types), max_wakes):
        orders = next(e for e in evs if e.type == "orders" and e.payload["wake"] == w.id).payload["orders"]
        forks = []
        for d in range(1, draws + 1):
            name = f"{prefix}-{w.id[:8]}-d{d}"
            if name not in index.data["branches"]:
                fork(index, branch, w.id, name, "resample", d)
            fl = index.open(name)
            end = next(e for e in reversed(fl.events) if e.type == "end_state").payload
            tot = end["objective_scores"]["totals"]
            forks.append({"branch": name, "draw": d, "totals": tot, "winners": winners(tot), "world_digest": end["world_digest"],
                          "diverged": divergence(index, name)})
        n_st = max(1, len(p_tot))
        spread = sum(abs(f["totals"].get(n, 0) - v) for f in forks for n, v in p_tot.items()) / (n_st * max(1, len(forks)))
        rows.append({"wake": w.id, "tick": w.tick, "station": w.node, "reasons": w.payload["reasons"],
                     "orders": [o["type"] for o in orders], "spread": round(spread, 6),
                     "winner_changed": sum(1 for f in forks if f["winners"] != p_win),
                     "digest_changed": sum(1 for f in forks if f["world_digest"] != p_dig), "forks": forks})
        if progress:
            progress(rows[-1])
    rows.sort(key=lambda r: (-r["spread"], -r["winner_changed"], -r["digest_changed"], r["tick"]))
    out = {"run": str(index.run_dir), "branch": branch, "draws": draws, "parent": {"totals": p_tot, "winners": p_win, "world_digest": p_dig},
           "wakes": rows}
    (index.run_dir / f"{prefix}.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out
