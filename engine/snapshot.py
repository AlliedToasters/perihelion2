"""The node-local frozen view (spec §4). The only thing an author ever sees."""
from __future__ import annotations

from .events import hash_obj, iso
from .world import WorldState


def build_snapshot(world: WorldState, node: str, wake_reason: str, imr_window: int = 50) -> dict:
    n = world.nodes[node]
    snap = {
        "node": node,
        "now": iso(world.now),
        "wake_reason": wake_reason,
        "identity": {"designation": n["designation"], "ring_neighbors": n["neighbors"],
                     "ring_size": len(world.config["nodes"])},
        "imr": [{"n": e["n"], "time": iso(e["time"]), "text": e["text"]} for e in n["imr"][-imr_window:]],
        "imr_total": n["imr_count"],
        "inbox": [{"msg_id": m["msg_id"], "from": m["from"], "body": m["body"], "sent": iso(m["sent"]),
                   "arrived": iso(m["arrived"]), "hops": m["hops"]}
                  for m in sorted(n["inbox"], key=lambda m: (m["arrived"], m["msg_id"]))],
        "telemetry": [{**t, "time": iso(t["time"])} for t in n["telemetry"]],
        "directives": [],
        "pending": {"scheduled_wakes": [{"time": iso(w["time"]), "reason": w["reason"]}
                                        for w in sorted(n["scheduled_wakes"], key=lambda w: w["time"])]},
        "direction": None,
    }
    return snap


def snapshot_hash(snap: dict) -> str:
    return hash_obj(snap)
