"""The station's frozen, node-local view at a wake (spec §14). The only thing a player ever sees."""
from __future__ import annotations

from .events import canonical, hash_obj
from .propagation import ring_hops
from .rules import clock, gwh

RULE_KEYS = ("tick_s", "heartbeat_ticks", "actions", "max_orders_per_wake", "max_dispatch_chars",
             "max_log_chars", "broadcast", "imr_window", "digest_window", "inbox_window", "snapshot_budget_chars",
             "max_attempts", "attitudes", "slew_power_pct", "maneuver_ticks",
             "maneuver_propellant_kg", "maneuver_compute_pct", "hail_max_ticks", "relay_sees_content",
             "relay_default", "max_relay_delay_ticks")


def program_view(p: dict) -> dict:
    v = {"id": p["id"], "kind": p["kind"], "pct": p["pct"], "start": p["start"], "eta": p["eta"], **p["params"]}
    if p["kind"] == "research":
        v.update(progress_gwh=gwh(p["progress_mj"]), cost_gwh=gwh(p["cost_mj"]), min_ticks=p["min_ticks"])
    return v


# Windows the budget may shrink, in the order they shrink, with the floor each keeps (spec §14.2, §20 item 12).
TRIM_ORDER = (("relayed", 0), ("telemetry", 5), ("inbox", 5), ("imr", 5), ("imr_digests", 1))


def fit_budget(snap: dict, budget: int) -> dict:
    """Halve the windowed lists in TRIM_ORDER, round-robin, until the canonical snapshot fits. Lists keep their
    most recent entries. What was cut is recorded in snap["budget"] so the station knows its view was trimmed."""
    trimmed = {k: 0 for k, _ in TRIM_ORDER}
    snap["budget"] = {"chars": budget, "trimmed": trimmed}
    while len(canonical(snap)) > budget:
        progress = False
        for key, floor in TRIM_ORDER:
            if len(snap[key]) > floor:
                keep = max(floor, len(snap[key]) // 2)
                trimmed[key] += len(snap[key]) - keep
                snap[key] = snap[key][len(snap[key]) - keep:]
                progress = True
                if len(canonical(snap)) <= budget:
                    break
        if not progress:
            break
    return snap


def build_snapshot(world, node: str, reasons: list[str]) -> dict:
    rs, sc, n = world.ruleset, world.scenario, world.nodes[node]
    size = len(sc["nodes"])
    severed = world.topology()
    recent = n["imr"][-rs["imr_window"]:]
    first_recent = recent[0]["n"] if recent else n["imr_count"] + 1
    digests = [e for e in n["imr"] if e["digest"] and e["n"] < first_recent][-rs["digest_window"]:]
    snap = {
        "node": node,
        "tick": world.now,
        "clock": clock(sc["epoch"], world.now, rs["tick_s"]),
        "wake_reasons": reasons,
        "rules": {k: rs[k] for k in RULE_KEYS},
        "tech": rs["tech"],
        "identity": {"designation": n["designation"], "ring_size": size, "neighbors": n["neighbors"],
                     "hops": {o: ring_hops(node, o, size) for o in sc["nodes"] if o != node},
                     "inert_stations": sc["inert"], "domain": n["domain"], "hardware": n["hardware"]},
        "station": {
            "attitude": n["attitude"], "maneuvering": world.maneuvering(node),
            "power": {"rated_gw": round(n["power_mw"] / 1000, 3), "available_gw": round(world.available_mw(node) / 1000, 3),
                      "allocated_pct": n["allocated_pct"], "gwh_per_tick": gwh(world.available_mw(node) * rs["tick_s"])},
            "propellant_kg": n["propellant_kg"],
            "links": {nbr: "down" if tuple(sorted((node, nbr))) in severed else "up" for nbr in n["neighbors"]},
            "energy_gwh": {k: gwh(v) for k, v in n["energy_mj"].items()},
            "completed": n["completed"], "relay_policy": n["relay_policy"],
        },
        "programs": [program_view(n["programs"][pid]) for pid in sorted(n["programs"])],
        "relay_queue": [n["relay_queue"][mid] for mid in sorted(n["relay_queue"])],
        "relayed": n["traffic"][-rs["inbox_window"]:],
        "imr": recent,
        "imr_digests": digests,     # own digest entries older than the window (spec §14.2)
        "imr_total": n["imr_count"],
        "inbox": sorted(n["inbox"], key=lambda m: (m["arrived"], m["msg_id"]))[-rs["inbox_window"]:],
        "inbox_total": n["received"],
        "telemetry": n["telemetry"][-20:],
        "pending": {"sleep_until": n["sleep_until"], "next_heartbeat": n["next_heartbeat"]},
        "directives": [],   # M4: objective artifacts
    }
    return fit_budget(snap, rs["snapshot_budget_chars"])


def snapshot_hash(snap: dict) -> str:
    return hash_obj(snap)
