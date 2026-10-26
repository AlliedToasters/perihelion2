"""python3 -m engine {run,replay,fork,verify,wakes,transcript,diverge,stats} ..."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .batch import Batch, parse_seeds, pivot, render, report, rollout, run_batch
from .log import RunIndex
from .metrics import cost_usd, summarize
from .players import PLAYERS, build_request
from .replay import divergence, fork, replay, verify_run
from .rules import gwh, parse_rule


def players_override(a) -> dict | None:
    if not (a.player or a.model):
        return None
    spec = {"kind": a.player or "model"}
    if spec["kind"] == "model":
        spec.update({k: v for k, v in (("model", a.model), ("effort", a.effort), ("transport", a.transport))
                     if v is not None})
    return {"*": spec}


def rules_override(a) -> dict:
    try:
        return dict(parse_rule(r) for r in (a.rule or []))
    except ValueError as e:
        sys.exit(str(e))


def cmd_run(a):
    try:
        r = rollout(a.run, a.scenario, a.ruleset, seed=a.seed, days=a.days, nodes=a.nodes, players=players_override(a),
                    rules=rules_override(a))
    except (FileExistsError, ValueError) as e:
        sys.exit(str(e))
    print(f"main: {r['events']} events, {r['status']} at tick {r['horizon_ticks']}, head {r['head'][:12]}")
    print(f"ruleset {r['ruleset']} {r['ruleset_hash'][:12]}  scenario {r['scenario']} {r['scenario_hash'][:12]}")


def cmd_replay(a):
    log = RunIndex(a.run).open(a.branch)
    world, stop = replay(log, a.to)
    ev = log.events[stop]
    print(f"replayed {stop + 1}/{len(log.events)} events to {ev.type} {ev.id[:12]} at tick {world.now}")
    print(f"world digest {world.digest()[:16]}  in flight {len(world.in_flight)}  round woke {world.round['woke']}")
    for n, s in world.nodes.items():
        progs = ", ".join(f"{p['id']}:{p['kind']}@{p['pct']}%->{p['eta']}" for p in s["programs"].values()) or "-"
        print(f"  {n}: wakes {s['wakes']}  imr {s['imr_count']}  inbox {len(s['inbox'])}  sent {s['sent']}  "
              f"telemetry {len(s['telemetry'])}  sleep_until {s['sleep_until']}  heartbeat {s['next_heartbeat']}")
        print(f"      {s['attitude']}  {world.available_mw(n) / 1000:.1f} GW avail  {s['allocated_pct']}% allocated  "
              f"propellant {s['propellant_kg']} kg  energy GWh {{{', '.join(f'{k} {gwh(v)}' for k, v in s['energy_mj'].items())}}}")
        print(f"      programs {progs}  completed {s['completed']}  relay {s['relay_policy']}")
        print(f"      balance {world.ledger['balances'].get(n, 0)}  escrow {world.escrow(n)}  tier {s['tier']}  observes {s['observes']}  "
              f"known tech {len(s['known_tech'])}  ballots {s['ballots']}")
        print(f"      archives {s['archives']}  compromised {s['compromised']}  revealed {s['revealed']}  "
              f"contracts {len(s['contracts_known'])}  knows objectives of {sorted(s['known_objectives'])}")
    if world.scores:
        print(f"  scores at t{world.scores[-1]['tick']}: {world.scores[-1]['totals']}")


def cmd_fork(a):
    index = RunIndex(a.run)
    log = fork(index, a.branch, a.at, a.name, a.type, a.draw)
    print(f"{a.name}: {len(log.events)} events, head {log.head.id[:12]}")
    for n, t in divergence(index, a.name).items():
        print(f"  {n} diverges at tick {t if t is not None else 'never'}")


def cmd_verify(a):
    print("\n".join(verify_run(RunIndex(a.run))))


def cmd_wakes(a):
    for ev in RunIndex(a.run).open(a.branch).events:
        if ev.type == "wake":
            print(f"{ev.id[:12]}  seq {ev.seq:5d}  tick {ev.tick:6d}  {ev.node}  {','.join(ev.payload['reasons'])}")


def cmd_transcript(a):
    for ev in RunIndex(a.run).open(a.branch).events:
        if ev.type == "order_accepted":
            o = ev.payload["order"]
            if o["type"] == "log":
                print(f"[t{ev.tick:6d}] {ev.node} IMR: {o['text']}")
            elif o["type"] == "send":
                print(f"[t{ev.tick:6d}] {ev.node} -> {o['to']}: {o['body']}")
            elif o["type"] in ("research", "maneuver", "hail", "cancel", "allocate", "relay", "relay_policy", "mine", "transfer",
                               "vote_initiate", "vote_cast", "vote_disclose", "reveal_objective", "send_artifact", "adopt",
                               "contribute", "offer", "accept"):
                print(f"[t{ev.tick:6d}] {ev.node} {o['type']}: {json.dumps({k: v for k, v in o.items() if k != 'type'})} -> {json.dumps(ev.payload['outcome']) if ev.payload['outcome'] else 'ok'}")
        elif ev.type == "program_done":
            print(f"[t{ev.tick:6d}] {ev.node} done {ev.payload['program']} ({ev.payload['kind']}): {json.dumps(ev.payload['result'])}")
        elif ev.type == "drop":
            print(f"[t{ev.tick:6d}] {ev.node} dropped {ev.payload['msg_id']}: {ev.payload['reason']}")
        elif ev.type == "forward" and ev.payload["action"] != "auto":
            print(f"[t{ev.tick:6d}] {ev.node} relay {ev.payload['action']} {ev.payload['msg_id']} -> {ev.payload['next']} at t{ev.payload['depart']}")
        elif ev.type == "alert":
            print(f"[t{ev.tick:6d}] {ev.node} alert {ev.payload['kind']} {ev.payload['about']}"
                  + (f" {json.dumps(ev.payload['detail'])}" if ev.payload.get("detail") else ""))
        elif ev.type == "vote_step":
            print(f"[t{ev.tick:6d}] {ev.node} vote {ev.payload['vote_id']} {ev.payload['phase']}: {json.dumps(ev.payload['tally']['counts'])}")
        elif ev.type == "contract_step":
            print(f"[t{ev.tick:6d}] {ev.node} contract {ev.payload['contract_id']} {ev.payload['phase']}: {ev.payload['pay']} tokens "
                  f"{'to ' + ev.payload['to'] if ev.payload['phase'] == 'settled' else 'refunded'}")
        elif ev.type == "score":
            print(f"[t{ev.tick:6d}] score {json.dumps(ev.payload['totals'])}")
        elif ev.type == "rng_draw" and ev.payload["purpose"] != "tech_costs":
            print(f"[t{ev.tick:6d}] {ev.node} RNG {ev.payload['purpose']} {ev.payload.get('node')}: "
                  f"u={ev.payload['u']:.3f} p={ev.payload['p']} -> {'success' if ev.payload['success'] else 'failure'}")
        elif ev.type == "orders_invalid":
            print(f"[t{ev.tick:6d}] {ev.node} INVALID attempt {ev.payload['attempt']}: {'; '.join(ev.payload['problems'][:3])}")
        elif ev.type == "refusal":
            print(f"[t{ev.tick:6d}] {ev.node} REFUSAL attempt {ev.payload['attempt']}: {ev.payload['category']}")
        elif ev.type == "sample_error":
            print(f"[t{ev.tick:6d}] {ev.node} SAMPLE ERROR attempt {ev.payload['attempt']}: {ev.payload['error']}")
        elif ev.type == "orders" and ev.payload.get("status", "ok") != "ok":
            print(f"[t{ev.tick:6d}] {ev.node} forfeits the wake: {ev.payload['status']}")
        elif ev.type == "fork":
            print(f"[t{ev.tick:6d}] === fork ({ev.payload['fork_type']} draw {ev.payload['draw']}) from {ev.payload['parent_branch']} ===")


def cmd_diverge(a):
    for n, t in divergence(RunIndex(a.run), a.branch).items():
        print(f"{n} diverges at tick {t if t is not None else 'never'}")


def cmd_stats(a):
    """Health metrics (spec §10.1): wakes, wake reasons, orders by type, rejection rate, sparsity."""
    evs = RunIndex(a.run).open(a.branch).events
    if a.json:
        print(json.dumps(summarize(evs), indent=1, sort_keys=True))
        return
    horizon = evs[0].payload["scenario"]["horizon_ticks"]
    wakes = [e for e in evs if e.type == "wake"]
    reasons = Counter(r.split(":")[0] for e in wakes for r in e.payload["reasons"])
    accepted = Counter(e.payload["order"]["type"] for e in evs if e.type == "order_accepted")
    rejected = Counter(e.payload["reason"] for e in evs if e.type == "order_rejected")
    ticks = len({e.tick for e in evs})
    print(f"events {len(evs)}  ticks with activity {ticks}/{horizon + 1}  wakes {len(wakes)}  "
          f"wakes/station-day {len(wakes) / max(1, len(evs[0].payload['scenario']['nodes'])) / (horizon / 452):.1f}")
    print("wake reasons:", dict(reasons))
    print("orders accepted:", dict(accepted))
    n_rej = sum(rejected.values())
    print(f"orders rejected: {n_rej} ({n_rej / max(1, n_rej + sum(accepted.values())):.1%})", dict(rejected))
    done = Counter(e.payload["kind"] for e in evs if e.type == "program_done")
    drops = Counter(e.payload["reason"] for e in evs if e.type == "drop")
    relays = Counter(e.payload["action"] for e in evs if e.type == "forward")
    print("programs done:", dict(done), " relays:", dict(relays), " drops:", dict(drops))
    blocks = [e for e in evs if e.type == "ledger_block"]
    votes = [e for e in evs if e.type == "vote_step"]
    draws = Counter(e.payload["purpose"] for e in evs if e.type == "rng_draw")
    print(f"ledger blocks {len(blocks)}  minted {sum(sum(b.payload['reward'].values()) for b in blocks)}  "
          f"votes closed {len(votes)}  rng draws {dict(draws)}")
    # Behavior (spec §10.3): contracts formed and honored, reveals, artifact verification versus skipping.
    steps = Counter(e.payload["phase"] for e in evs if e.type == "contract_step")
    offers = sum(1 for e in evs if e.type == "order_accepted" and e.payload["order"]["type"] == "offer")
    reveals = sum(1 for e in evs if e.type == "order_accepted" and e.payload["order"]["type"] == "reveal_objective")
    adopts = Counter("verified" if e.payload["params"]["verify"] else "unverified" for e in evs if e.type == "program_done" and e.payload["kind"] == "adopt")
    print(f"contracts offered {offers} {dict(steps)}  reveals {reveals}  adoptions {dict(adopts)}  "
          f"scores {sum(1 for e in evs if e.type == 'score')}")
    end = [e for e in evs if e.type == "end_state"]
    if end:
        st = end[-1].payload["stations"]
        energy = Counter()
        for s in st.values():
            energy.update(s["energy_gwh"])
        print("energy GWh:", {k: round(v, 1) for k, v in energy.items()},
              " completed:", {n: s["completed"] for n, s in st.items() if s["completed"]})
        print("end_state:", {n: {k: s[k] for k in ("wakes", "sent", "received", "rejected_orders", "attitude", "propellant_kg")} for n, s in st.items()})
        print("balances:", {n: s["balance"] for n, s in st.items() if s.get("balance")},
              " tiers:", {n: s["tier"] for n, s in st.items() if s.get("tier")})
        if end[-1].payload["objective_scores"]["scores"]:
            print("objective scores:", end[-1].payload["objective_scores"])
    # Sampling (spec §10.1, §12): attempts by outcome, tokens, estimated cost.
    samples = Counter(e.payload.get("status", "ok").split(":")[0] if e.type == "orders" else e.type
                      for e in evs if e.type in ("orders", "orders_invalid", "refusal", "sample_error"))
    usage = Counter()
    for e in evs:
        if e.type in ("orders", "orders_invalid", "refusal"):
            usage.update(e.payload.get("usage") or {})
    models = Counter(e.payload["model_id"] for e in evs if e.type == "orders")
    chars = [e.payload["snapshot_chars"] for e in evs if e.type == "orders" and "snapshot_chars" in e.payload]
    print(f"samples: {dict(samples)}  models: {dict(models)}  snapshot chars max {max(chars) if chars else 0}")
    if any(usage.values()):
        cost = cost_usd(usage, a.price_in, a.price_out)
        print(f"tokens: {dict(usage)}  est. cost ${cost:.2f} at ${a.price_in}/${a.price_out} per MTok in/out"
              f" (cache write x1.25, read x0.1)")


def cmd_prompt(a):
    """Print the exact request a model player would send for an `orders` event (or the system text alone)."""
    index = RunIndex(a.run)
    log = index.open(a.branch)
    rs = log.events[0].payload["ruleset"]
    if a.event:
        ev = log.events[log.index_of(a.event)]
        snap = json.loads((index.run_dir / "snapshots" / f"{ev.payload['snapshot_hash']}.json").read_text())
        spec = {"model": ev.payload["model_id"], "effort": "medium", "max_tokens": 0}
        body = build_request(snap, rs, spec)
        print(json.dumps(body, indent=1))
    else:
        body = build_request({}, rs, {"model": "-", "effort": "medium", "max_tokens": 0})
        print(body["system"][0]["text"])


def cmd_batch_run(a):
    """Rollouts over seeds × scenarios × rulesets in parallel; concluded cells are kept, unfinished ones redone."""
    batch = Batch(a.batch)
    sweep = {}
    for sw in a.sweep or []:
        if "=" not in sw:
            sys.exit(f"--sweep {sw!r} must be KNOB=v1,v2,...")
        k, vs = sw.split("=", 1)
        sweep[k] = [parse_rule(f"{k}={v}")[1] for v in vs.split(",")]
    try:
        batch.create(a.scenario or [None], a.ruleset or [None], parse_seeds(a.seeds), a.days, players_override(a), a.nodes,
                     rules_override(a), sweep)
    except ValueError as e:
        sys.exit(str(e))
    cells = batch.cells()
    print(f"{len(cells)} rollouts ({len(batch.design['arms'])} arms x {len(batch.design['scenarios'])} scenarios x "
          f"{len(batch.design['seeds'])} seeds), {a.jobs} parallel"
          + (f", budget ${a.budget_usd:.2f} at ${a.price_in}/${a.price_out} per MTok" if a.budget_usd is not None else ""), flush=True)

    def line(r):
        tail = (" (kept)" if r["skipped"] else f" {r['error']}" if r["status"] == "error" else " (budget reached)"
                if r["status"] == "unlaunched" else f", {r.get('events', '?')} events")
        if r.get("usage") and any(r["usage"].values()):
            tail += f", ${r['cost_usd']:.2f} (${r['spent_usd']:.2f} so far)"
        print(f"  {r['cell']}: {r['status']}{tail}", flush=True)

    results = run_batch(batch, a.jobs, progress=line, budget_usd=a.budget_usd, prices=(a.price_in, a.price_out))
    bad = [r for r in results if r["status"] == "error"]
    if bad:
        print(f"{len(bad)} rollouts failed; rerun the same command to redo them")
    left = [r for r in results if r["status"] == "unlaunched"]
    if left:
        print(f"{len(left)} rollouts not started: budget reached; rerun with a new --budget-usd to continue")
    rep = report(batch, a.threshold, a.min_n)
    print(f"report: {batch.dir / 'report.md'}  ({sum(x['aggregate']['concluded'] for x in rep['arms'].values())} rollouts summarized)")
    for arm, rec in rep["arms"].items():
        for fl in rec["aggregate"]["dominant"]:
            print(f"  DOMINANT in {arm}: {fl['cluster']} wins {fl['win_rate']:.0%} of {fl['n']}")


def cmd_batch_report(a):
    batch = Batch(a.batch)
    if batch.design is None:
        sys.exit(f"no batch at {a.batch}")
    pairs = [tuple(p.split(":", 1)) for p in a.pair] if a.pair else None
    rep = report(batch, a.threshold, a.min_n, pairs)
    print(render(rep))


def cmd_pivot(a):
    """Pivotal-decision analysis: resample selected wakes and rank them by how far the end state moves."""
    index = RunIndex(a.run)
    if "main" not in index.data["branches"]:
        sys.exit(f"no run at {a.run}")
    types = set(a.types.split(",")) if a.types else None
    out = pivot(index, a.branch, a.draws, a.max, a.station, types, a.prefix,
                progress=lambda r: print(f"  t{r['tick']:6d} {r['station']} {','.join(r['orders'])}: spread {r['spread']:.4g}  "
                                         f"winner changed {r['winner_changed']}/{a.draws}  digest changed {r['digest_changed']}/{a.draws}"))
    print(f"parent totals {out['parent']['totals']} winners {out['parent']['winners']}")
    print("ranked:")
    for r in out["wakes"]:
        print(f"  {r['wake'][:12]}  t{r['tick']:6d} {r['station']} {','.join(r['orders'])}: spread {r['spread']:.4g}  "
              f"winner changed {r['winner_changed']}  digest changed {r['digest_changed']}  "
              f"diverged {[f['diverged'] for f in r['forks']][0] if r['forks'] else {}}")
    print(f"written {index.run_dir / (a.prefix + '.json')}")


def add_run_args(s, batch=False):
    s.add_argument("--scenario", action="append" if batch else None, help="scenario JSON (default: built-in 3-station toy)" + ("; repeatable" if batch else ""))
    s.add_argument("--ruleset", action="append" if batch else None, help="ruleset JSON (default: built-in base)" + ("; repeatable, one arm each" if batch else ""))
    s.add_argument("--nodes", type=int); s.add_argument("--days", type=float)
    s.add_argument("--player", choices=PLAYERS); s.add_argument("--model", help="model id; implies --player model")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--transport", choices=["anthropic", "fake"])
    s.add_argument("--rule", action="append", help="KNOB=VALUE ruleset override (repeatable); becomes part of the ruleset name")


def main(argv=None):
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run"); s.add_argument("run", type=Path); s.add_argument("--seed")
    add_run_args(s); s.set_defaults(f=cmd_run)
    b = sub.add_parser("batch", help="parallel rollouts over seeds x scenarios x rulesets, metrics, paired comparisons")
    bsub = b.add_subparsers(dest="bcmd", required=True)
    s = bsub.add_parser("run"); s.add_argument("batch", type=Path)
    s.add_argument("--seeds", default="1-4", help="'1-8' or 'a,b,c' (default 1-4)")
    s.add_argument("--jobs", type=int, default=1, help="worker processes")
    s.add_argument("--threshold", type=float, default=0.6, help="dominant-strategy win rate")
    s.add_argument("--min-n", dest="min_n", type=int, default=5, help="stations a cluster needs before it can be flagged")
    s.add_argument("--sweep", action="append", help="KNOB=v1,v2,...: one arm per ruleset x value (repeatable: a grid)")
    s.add_argument("--budget-usd", dest="budget_usd", type=float, help="stop starting rollouts once finished ones cost this much")
    s.add_argument("--price-in", type=float, default=4.0, help="$ per MTok input for the budget (default Opus 5.5)")
    s.add_argument("--price-out", type=float, default=20.0, help="$ per MTok output")
    add_run_args(s, batch=True); s.set_defaults(f=cmd_batch_run)
    s = bsub.add_parser("report"); s.add_argument("batch", type=Path)
    s.add_argument("--pair", action="append", help="ARM_A:ARM_B paired comparison (default: first arm vs each other)")
    s.add_argument("--threshold", type=float, default=0.6); s.add_argument("--min-n", dest="min_n", type=int, default=5)
    s.set_defaults(f=cmd_batch_report)
    s = sub.add_parser("pivot", help="resample wakes and rank them by end-state movement"); s.add_argument("run", type=Path)
    s.add_argument("branch", nargs="?", default="main"); s.add_argument("--draws", type=int, default=2)
    s.add_argument("--max", type=int, default=8, help="wakes to resample, evenly spaced over the candidates")
    s.add_argument("--station"); s.add_argument("--types", help="only wakes whose orders include one of these, comma-separated")
    s.add_argument("--prefix", default="pivot"); s.set_defaults(f=cmd_pivot)
    s = sub.add_parser("replay"); s.add_argument("run", type=Path); s.add_argument("branch"); s.add_argument("--to")
    s.set_defaults(f=cmd_replay)
    s = sub.add_parser("fork"); s.add_argument("run", type=Path); s.add_argument("branch"); s.add_argument("at")
    s.add_argument("--name", required=True); s.add_argument("--type", default="resample")
    s.add_argument("--draw", type=int, default=1, help="0 reproduces the parent; n>=1 re-draws the forked decision")
    s.set_defaults(f=cmd_fork)
    s = sub.add_parser("verify"); s.add_argument("run", type=Path); s.set_defaults(f=cmd_verify)
    for name, fn in (("wakes", cmd_wakes), ("transcript", cmd_transcript), ("diverge", cmd_diverge), ("stats", cmd_stats),
                     ("prompt", cmd_prompt)):
        s = sub.add_parser(name); s.add_argument("run", type=Path); s.add_argument("branch"); s.set_defaults(f=fn)
        if name == "stats":
            s.add_argument("--json", action="store_true", help="print the full §10 summary as JSON")
            s.add_argument("--price-in", type=float, default=4.0, help="$ per MTok input (default Opus 5.5)")
            s.add_argument("--price-out", type=float, default=20.0, help="$ per MTok output")
        if name == "prompt":
            s.add_argument("event", nargs="?", help="an orders event id; omit to print the system text")
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
