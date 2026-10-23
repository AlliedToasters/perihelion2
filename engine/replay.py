"""Replay to any event, fork from any wake, verify shared prefixes, divergence cone (spec §16)."""
from __future__ import annotations

from .engine import Engine
from .events import ChainError, verify_chain
from .log import EventLog, RunIndex
from .world import WorldState

FORK_TYPES = {"resample"}   # later: model_swap, world_reroll, scenario_patch


def replay(log: EventLog, upto: str | None = None) -> tuple[WorldState, int]:
    """Fold events into a fresh world. Returns (world, index of last applied event)."""
    verify_chain(log.events)
    stop = log.index_of(upto) if upto else len(log.events) - 1
    world = WorldState()
    for ev in log.events[: stop + 1]:
        world.apply(ev)
    return world, stop


def fork(index: RunIndex, parent: str, at: str, name: str, fork_type: str = "resample", draw: int = 1) -> EventLog:
    """Fork at a wake. draw=0 re-uses the parent's seed (reproduces it); draw>=1 re-draws that one decision.
    Every other station's decisions stay identical until its snapshot changes (the divergence cone)."""
    if fork_type not in FORK_TYPES:
        raise ValueError(f"fork type {fork_type!r} not implemented in {sorted(FORK_TYPES)}")
    if name in index.data["branches"]:
        raise ValueError(f"branch {name!r} exists")
    plog = index.open(parent)
    world, stop = replay(plog, at)
    wake = plog.events[stop]
    if wake.type != "wake":
        raise ValueError(f"fork point must be a wake event, got {wake.type}")
    index.register(name, parent, wake.id, fork_type, draw)
    log = EventLog(index.branch_path(name))
    for ev in plog.events[: stop + 1]:
        log.append_verbatim(ev)
    eng = Engine(log, world, index.run_dir / "snapshots", fork_wake=wake.id, draw=draw)
    eng.emit("fork", wake.node, {"fork_type": fork_type, "parent_branch": parent, "fork_point": wake.id, "draw": draw},
             tick=wake.tick)
    status = eng.run()   # resumes mid-round: the forked station's orders are sampled first
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


def divergence(index: RunIndex, branch: str) -> dict[str, int | None]:
    """Per station: first tick whose snapshot hash differs from the parent branch (spec §16.3)."""
    b = index.data["branches"][branch]
    if not b["parent_branch"]:
        return {}
    log, plog = index.open(branch), index.open(b["parent_branch"])
    k = log.index_of(b["fork_point"])

    def samples(evs):
        """(node, tick, reasons) -> snapshot hash. Wake ids differ past a fork; this key does not."""
        wakes = {e.id: e for e in evs if e.type == "wake"}
        return {(e.node, e.tick, tuple(wakes[e.payload["wake"]].payload["reasons"])): e.payload["snapshot_hash"]
                for e in evs if e.type == "orders"}

    parent, mine = samples(plog.events), samples(log.events[k:])
    first: dict[str, int] = {}
    for (n, t, reasons), h in sorted(mine.items(), key=lambda kv: kv[0][1]):
        if n not in first and parent.get((n, t, reasons)) != h:
            first[n] = t
    nodes = log.events[0].payload["scenario"]["nodes"]
    return {n: first.get(n) for n in nodes}
