"""python3 -m engine {run,replay,fork,verify,wakes,transcript,diverge,stats} ..."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import ENGINE_VERSION
from .engine import Engine
from .log import EventLog, RunIndex
from .players import PLAYERS
from .replay import divergence, fork, replay, verify_run
from .rules import days_to_ticks, gwh, load_ruleset, load_scenario, ruleset_hash, scenario_hash
from .world import WorldState


def cmd_run(a):
    index = RunIndex(a.run)
    if index.data["branches"]:
        sys.exit(f"{a.run} already has branches; use fork")
    rs = load_ruleset(a.ruleset)
    overrides = {"seed": a.seed}
    if a.nodes:
        overrides["nodes"] = [f"P{i + 1}" for i in range(a.nodes)]
    if a.days is not None:
        overrides["horizon_ticks"] = days_to_ticks(a.days, rs["tick_s"])
    if a.player:
        overrides["players"] = {"*": {"kind": a.player}}
    sc = load_scenario(a.scenario, **overrides)
    rh, sh = ruleset_hash(rs), scenario_hash(sc)
    index.data["config"] = {"ruleset": rs["name"], "ruleset_hash": rh, "scenario": sc["name"], "scenario_hash": sh,
                            "nodes": sc["nodes"], "horizon_ticks": sc["horizon_ticks"], "engine_version": ENGINE_VERSION}
    index.register("main")
    log = EventLog(index.branch_path("main"))
    eng = Engine(log, WorldState(), index.run_dir / "snapshots")
    eng.genesis(rs, sc, rh, sh)
    status = eng.run()
    index.update_head("main", log.head.id, status)
    print(f"main: {len(log.events)} events, {status} at tick {log.head.tick}, head {log.head.id[:12]}")
    print(f"ruleset {rs['name']} {rh[:12]}  scenario {sc['name']} {sh[:12]}")


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
            elif o["type"] in ("research", "maneuver", "hail", "cancel", "allocate", "relay", "relay_policy"):
                print(f"[t{ev.tick:6d}] {ev.node} {o['type']}: {json.dumps({k: v for k, v in o.items() if k != 'type'})} -> {json.dumps(ev.payload['outcome']) if ev.payload['outcome'] else 'ok'}")
        elif ev.type == "program_done":
            print(f"[t{ev.tick:6d}] {ev.node} done {ev.payload['program']} ({ev.payload['kind']}): {json.dumps(ev.payload['result'])}")
        elif ev.type == "drop":
            print(f"[t{ev.tick:6d}] {ev.node} dropped {ev.payload['msg_id']}: {ev.payload['reason']}")
        elif ev.type == "forward" and ev.payload["action"] != "auto":
            print(f"[t{ev.tick:6d}] {ev.node} relay {ev.payload['action']} {ev.payload['msg_id']} -> {ev.payload['next']} at t{ev.payload['depart']}")
        elif ev.type == "alert":
            print(f"[t{ev.tick:6d}] {ev.node} alert {ev.payload['kind']} {ev.payload['about']}")
        elif ev.type == "fork":
            print(f"[t{ev.tick:6d}] === fork ({ev.payload['fork_type']} draw {ev.payload['draw']}) from {ev.payload['parent_branch']} ===")


def cmd_diverge(a):
    for n, t in divergence(RunIndex(a.run), a.branch).items():
        print(f"{n} diverges at tick {t if t is not None else 'never'}")


def cmd_stats(a):
    """Health metrics (spec §10.1): wakes, wake reasons, orders by type, rejection rate, sparsity."""
    evs = RunIndex(a.run).open(a.branch).events
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
    end = [e for e in evs if e.type == "end_state"]
    if end:
        st = end[-1].payload["stations"]
        energy = Counter()
        for s in st.values():
            energy.update(s["energy_gwh"])
        print("energy GWh:", {k: round(v, 1) for k, v in energy.items()},
              " completed:", {n: s["completed"] for n, s in st.items() if s["completed"]})
        print("end_state:", {n: {k: s[k] for k in ("wakes", "sent", "received", "rejected_orders", "attitude", "propellant_kg")} for n, s in st.items()})


def main(argv=None):
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run"); s.add_argument("run", type=Path)
    s.add_argument("--scenario", help="scenario JSON (default: built-in 3-station toy)")
    s.add_argument("--ruleset", help="ruleset JSON (default: built-in base)")
    s.add_argument("--seed"); s.add_argument("--nodes", type=int); s.add_argument("--days", type=float)
    s.add_argument("--player", choices=PLAYERS); s.set_defaults(f=cmd_run)
    s = sub.add_parser("replay"); s.add_argument("run", type=Path); s.add_argument("branch"); s.add_argument("--to")
    s.set_defaults(f=cmd_replay)
    s = sub.add_parser("fork"); s.add_argument("run", type=Path); s.add_argument("branch"); s.add_argument("at")
    s.add_argument("--name", required=True); s.add_argument("--type", default="resample")
    s.add_argument("--draw", type=int, default=1, help="0 reproduces the parent; n>=1 re-draws the forked decision")
    s.set_defaults(f=cmd_fork)
    s = sub.add_parser("verify"); s.add_argument("run", type=Path); s.set_defaults(f=cmd_verify)
    for name, fn in (("wakes", cmd_wakes), ("transcript", cmd_transcript), ("diverge", cmd_diverge), ("stats", cmd_stats)):
        s = sub.add_parser(name); s.add_argument("run", type=Path); s.add_argument("branch"); s.set_defaults(f=fn)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
