"""The station's frozen, node-local view at a wake (spec §7). The only thing a player ever sees."""
from __future__ import annotations

from .events import hash_obj
from .propagation import ring_hops
from .rules import clock

RULE_KEYS = ("tick_s", "heartbeat_ticks", "actions", "max_orders_per_wake", "max_dispatch_chars",
             "max_log_chars", "broadcast")


def build_snapshot(world, node: str, reasons: list[str]) -> dict:
    rs, sc, n = world.ruleset, world.scenario, world.nodes[node]
    size = len(sc["nodes"])
    return {
        "node": node,
        "tick": world.now,
        "clock": clock(sc["epoch"], world.now, rs["tick_s"]),
        "wake_reasons": reasons,
        "rules": {k: rs[k] for k in RULE_KEYS},
        "identity": {"designation": n["designation"], "ring_size": size, "neighbors": n["neighbors"],
                     "hops": {o: ring_hops(node, o, size) for o in sc["nodes"] if o != node},
                     "inert_stations": sc["inert"]},
        "imr": n["imr"][-rs["imr_window"]:],
        "imr_total": n["imr_count"],
        "inbox": sorted(n["inbox"], key=lambda m: (m["arrived"], m["msg_id"]))[-rs["inbox_window"]:],
        "inbox_total": n["received"],
        "telemetry": n["telemetry"][-20:],
        "pending": {"sleep_until": n["sleep_until"], "next_heartbeat": n["next_heartbeat"]},
        "directives": [],   # M4: objective artifacts
    }


def snapshot_hash(snap: dict) -> str:
    return hash_obj(snap)
