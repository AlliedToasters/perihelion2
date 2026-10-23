"""Replay to any event, fork from any wake, verify shared prefixes (spec §9)."""
from __future__ import annotations

from .agents import AGENTS
from .engine import Engine
from .events import ChainError, iso, verify_chain
from .log import EventLog, RunIndex
from .world import WorldState

FORK_TYPES = {"resample"}  # M4 adds directed_blind, directed_omniscient, override, world_reroll, model_swap


def replay(log: EventLog, upto: str | None = None) -> tuple[WorldState, int]:
    """Fold events into a fresh world. Returns (world, index of last applied event)."""
    verify_chain(log.events)
    stop = log.index_of(upto) if upto else len(log.events) - 1
    world = WorldState()
    for ev in log.events[: stop + 1]:
        world.apply(ev)
    return world, stop


def make_engine(index: RunIndex, branch: str, log: EventLog, world: WorldState) -> Engine:
    cfg, b = index.data["config"], index.data["branches"][branch]
    agent = AGENTS[cfg["agent"]]()
    return Engine(log, world, agent, b["sample_seed"], cfg["horizon_ms"], index.run_dir / "snapshots")


def fork(index: RunIndex, parent: str, at: str, name: str, fork_type: str, sample_seed: str,
         author: str = "human") -> EventLog:
    if fork_type not in FORK_TYPES:
        raise ValueError(f"fork type {fork_type!r} not implemented in {sorted(FORK_TYPES)}")
    if name in index.data["branches"]:
        raise ValueError(f"branch {name!r} exists")
    plog = index.open(parent)
    world, stop = replay(plog, at)
    wake = plog.events[stop]
    if wake.type != "wake":
        raise ValueError(f"fork point must be a wake event, got {wake.type}")
    index.register(name, parent, wake.id, fork_type, sample_seed)
    log = EventLog(index.branch_path(name))
    for ev in plog.events[: stop + 1]:
        log.append_verbatim(ev)
    eng = make_engine(index, name, log, world)
    eng.emit("fork", wake.node, {"fork_type": fork_type, "parent_branch": parent, "fork_point": wake.id,
                                 "author": author, "sample_seed": sample_seed}, sim_time=wake.sim_time)
    eng.handle_wake(wake)  # re-sample the forked wake
    status = eng.run()
    index.update_head(name, log.head.id, status)
    return log


def verify_run(index: RunIndex) -> list[str]:
    """Verify every branch chain and every fork's shared prefix. Returns human-readable lines."""
    out = []
    for name, b in index.data["branches"].items():
        log = index.open(name)
        try:
            log.verify()
            out.append(f"{name}: chain ok, {len(log.events)} events, head {log.head.id[:12]}")
        except ChainError as e:
            out.append(f"{name}: CHAIN BROKEN: {e}")
            continue
        if b["parent_branch"]:
            plog = index.open(b["parent_branch"])
            k = plog.index_of(b["fork_point"])
            same = all(a.id == c.id for a, c in zip(plog.events[: k + 1], log.events[: k + 1]))
            out.append(f"  prefix of {k + 1} events shared with {b['parent_branch']}: {'ok' if same else 'MISMATCH'}")
            if not same:
                raise ChainError(f"{name}: prefix mismatch with {b['parent_branch']}")
    return out


def divergence(index: RunIndex, branch: str) -> dict[str, str | None]:
    """Per node: sim time of the first wake whose snapshot hash differs from the parent branch (§9.4)."""
    b = index.data["branches"][branch]
    if not b["parent_branch"]:
        return {}
    log, plog = index.open(branch), index.open(b["parent_branch"])
    k = log.index_of(b["fork_point"])

    def samples(evs):
        """(node, wake sim_time, wake reason) -> snapshot hash. Wake ids differ past a fork; this key does not."""
        wakes = {e.id: e for e in evs if e.type == "wake"}
        return {(e.node, wakes[e.payload["wake"]].sim_time, wakes[e.payload["wake"]].payload["reason"]):
                e.payload["snapshot_hash"] for e in evs if e.type == "sample"}

    parent, branch_samples = samples(plog.events), samples(log.events[k:])
    first: dict[str, int] = {}
    for (n, t, reason), h in sorted(branch_samples.items(), key=lambda kv: kv[0][1]):
        if n not in first and parent.get((n, t, reason)) != h:
            first[n] = t
    return {n: (iso(first[n]) if n in first else None) for n in index.data["config"]["nodes"]}
