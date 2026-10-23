"""python3 -m engine {run,replay,fork,verify,wakes,render,diverge} ..."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ENGINE_VERSION
from .agents import AGENTS
from .engine import Engine
from .events import iso
from .log import EventLog, RunIndex
from .replay import divergence, fork, make_engine, replay, verify_run
from .world import DAY_MS, WorldState, utc

LOS_ET_MS = utc(2037, 6, 23, 9, 17, 33)


def cmd_run(a):
    index = RunIndex(a.run)
    if index.data["branches"]:
        sys.exit(f"{a.run} already has branches; use fork")
    nodes = [f"P{i + 1}" for i in range(a.nodes)]
    index.data["config"] = {"nodes": nodes, "hop_delay_s": 191, "imr_wake_utc": "14:30:00", "agent": a.agent,
                            "start": iso(LOS_ET_MS), "horizon_ms": LOS_ET_MS + a.days * DAY_MS,
                            "engine_version": ENGINE_VERSION}
    index.register("main", None, None, None, a.seed)
    log = EventLog(index.branch_path("main"))
    world = WorldState()
    eng = make_engine(index, "main", log, world)
    eng.emit("genesis", None, {"config": index.data["config"]}, sim_time=LOS_ET_MS)
    status = eng.run()
    index.update_head("main", log.head.id, status)
    print(f"main: {len(log.events)} events, status {status}, head {log.head.id[:12]}")


def cmd_replay(a):
    index = RunIndex(a.run)
    log = index.open(a.branch)
    world, stop = replay(log, a.to)
    ev = log.events[stop]
    print(f"replayed {stop + 1}/{len(log.events)} events to {ev.type} {ev.id[:12]} at {iso(world.now)}")
    print(f"world digest {world.digest()[:16]}  in flight {len(world.in_flight)}  pending {len(world.pending)}")
    for n, s in world.nodes.items():
        print(f"  {n}: imr {s['imr_count']}  inbox {len(s['inbox'])}  sent {s['sent']}  rejected {len(s['telemetry'])}")


def cmd_fork(a):
    index = RunIndex(a.run)
    log = fork(index, a.branch, a.at, a.name, a.type, a.seed)
    print(f"{a.name}: {len(log.events)} events, head {log.head.id[:12]}")
    for n, t in divergence(index, a.name).items():
        print(f"  {n} diverges at {t or 'never'}")


def cmd_verify(a):
    print("\n".join(verify_run(RunIndex(a.run))))


def cmd_wakes(a):
    log = RunIndex(a.run).open(a.branch)
    for ev in log.events:
        if ev.type == "wake":
            print(f"{ev.id[:12]}  seq {ev.seq:5d}  {iso(ev.sim_time)}  {ev.node}  {ev.payload['reason']}")


def cmd_render(a):
    log = RunIndex(a.run).open(a.branch)
    for ev in log.events:
        if ev.type == "action_adjudicated":
            act = ev.payload["action"]
            if act["type"] == "write_imr":
                print(f"[{iso(ev.sim_time)}] {ev.node} IMR: {act['text']}")
            elif act["type"] == "send_dispatch":
                print(f"[{iso(ev.sim_time)}] {ev.node} -> {act['to']}: {act['body']}")
        elif ev.type == "fork":
            print(f"[{iso(ev.sim_time)}] === fork ({ev.payload['fork_type']}) from {ev.payload['parent_branch']} ===")


def cmd_diverge(a):
    for n, t in divergence(RunIndex(a.run), a.branch).items():
        print(f"{n} diverges at {t or 'never'}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run"); s.add_argument("run", type=Path); s.add_argument("--nodes", type=int, default=3)
    s.add_argument("--agent", choices=AGENTS, default="random"); s.add_argument("--seed", default="1")
    s.add_argument("--days", type=int, default=3); s.set_defaults(f=cmd_run)
    s = sub.add_parser("replay"); s.add_argument("run", type=Path); s.add_argument("branch"); s.add_argument("--to")
    s.set_defaults(f=cmd_replay)
    s = sub.add_parser("fork"); s.add_argument("run", type=Path); s.add_argument("branch"); s.add_argument("at")
    s.add_argument("--name", required=True); s.add_argument("--type", default="resample"); s.add_argument("--seed", required=True)
    s.set_defaults(f=cmd_fork)
    for name, fn in (("verify", cmd_verify),):
        s = sub.add_parser(name); s.add_argument("run", type=Path); s.set_defaults(f=fn)
    for name, fn in (("wakes", cmd_wakes), ("render", cmd_render), ("diverge", cmd_diverge)):
        s = sub.add_parser(name); s.add_argument("run", type=Path); s.add_argument("branch"); s.set_defaults(f=fn)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
