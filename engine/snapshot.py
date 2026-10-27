"""The station's frozen, node-local view at a wake (spec §14). The only thing a player ever sees."""
from __future__ import annotations

from .events import canonical, hash_obj
from .objectives import directives_view
from .propagation import ring_hops
from .rules import MJ_PER_GWH, clock, gwh
from .tech import cost_multiplier, estimate_cost

RULE_KEYS = ("tick_s", "heartbeat_ticks", "actions", "max_orders_per_wake", "max_dispatch_chars",
             "max_log_chars", "broadcast", "imr_window", "digest_window", "inbox_window", "snapshot_budget_chars",
             "max_attempts", "attitudes", "slew_power_pct", "maneuver_ticks",
             "maneuver_propellant_kg", "maneuver_compute_pct", "hail_max_ticks", "relay_sees_content",
             "relay_default", "max_relay_delay_ticks", "beacon_ticks", "block_reward", "shares_per_tick_full",
             "audit_rounds", "deficit_alert_pct", "vote_max_ticks", "vote_max_options",
             "artifact_receive_frac", "artifact_verify_frac", "archive_cost_gwh", "contract_max_ticks")
TECH_KEYS = ("branch", "description", "prereqs", "sigma", "cost_model", "min_ticks", "lock", "discovery", "effects",
             "success_probability", "pooled", "adoption", "hardware_required", "requires_data_from",
             "requires_data_from_count", "power_loss_gw")


def program_view(world, node: str, p: dict) -> dict:
    """What a station knows about its own program. Research shows the public cost estimate (the conditional
    median of the prior given progress, spec §18.3) and the completion tick that estimate implies; the true cost
    and the true completion tick stay hidden until completion."""
    v = {"id": p["id"], "kind": p["kind"], "pct": p["pct"], "start": p["start"], **p["params"]}
    if p["kind"] == "research":
        # Progress carries no information about the cost while min_ticks still blocks completion: the program
        # could not have completed yet whatever the cost. Until the gate opens the estimate is the prior.
        gated = world.advanced_to < p["start"] + p["min_ticks"]
        est = p["prior_mj"] if gated else estimate_cost(p["prior_mj"], p["sigma"], p["progress_mj"])
        remaining = max(0, est - p["progress_mj"])
        eta = None if p["rate_mj"] <= 0 else world.advanced_to + -(-remaining // p["rate_mj"])
        v.update(progress_gwh=gwh(p["progress_mj"]), cost_est_gwh=gwh(est), sigma=p["sigma"], min_ticks=p["min_ticks"],
                 eta_est=None if eta is None else max(eta, p["start"] + p["min_ticks"]))
        if p.get("pooled_mj"):
            v["pooled_gwh"] = {k: gwh(x) for k, x in sorted(p["pooled_mj"].items())}
    elif p["kind"] == "adopt":
        v.update(progress_gwh=gwh(p["progress_mj"]), cost_gwh=gwh(p["cost_mj"]), eta=p["eta"])
    elif p["kind"] == "contribute":
        v.update(eta=None, host_running=world.host_program(p["params"]["to"], p["params"]["node"]) is not None)
    else:
        v["eta"] = p["eta"]
    return v


def tech_view(world, node: str) -> dict:
    """The nodes this station knows of, with costs for its own domain (spec §18, §20 item 10)."""
    rs, n = world.ruleset, world.nodes[node]
    out = {}
    for tid in n["known_tech"]:
        t = rs["tech"][tid]
        mult = cost_multiplier(t, n["domain"], n["completed"])
        out[tid] = {k: t[k] for k in TECH_KEYS}
        data_ok = world.data_satisfied(node, tid)
        out[tid].update(cost_est_gwh=round(t["cost_gwh"] * mult, 3), affinity=round(mult, 3),
                        done=tid in n["completed"], progress_gwh=gwh(n["research_progress"].get(tid, 0)), data_ok=data_ok,
                        available=(tid not in n["completed"] and all(p in n["completed"] for p in t["prereqs"])
                                   and all(h in n["hardware"] for h in t["hardware_required"]) and data_ok))
    return out


def ledger_view(world, node: str) -> dict:
    if not world.ledger_enabled():
        return {"enabled": False}
    if not world.participant(node):
        return {"enabled": True, "participant": False}
    L = world.ledger
    return {"enabled": True, "participant": True, "height": L["height"], "minted": L["minted"],
            "balance": L["balances"].get(node, 0), "escrow": world.escrow(node), "escrow_total": world.escrow(),
            "balances": dict(L["balances"]), "participants": list(L["participants"]),
            "last_round": L["blocks"][-1]["round"] if L["blocks"] else None,
            "tokens_per_gwh": world.tokens_per_gwh(), "shares": world.share_table()}


def sensors_view(world, node: str) -> dict:
    rs, n, out = world.ruleset, world.nodes[node], {}
    if world.observes(node, "neighbor_power_estimate"):   # sense.thermal_watch: power draw from thermal emission
        q = rs["rated_power_mw"] * rs["sensor_power_quantum_pct"] // 100
        est = {}
        for nbr in n["neighbors"]:
            o = world.nodes[nbr]
            avail = world.available_mw(nbr)
            est[nbr] = {"compute_gw": round((avail * o["allocated_pct"] // 100 + q // 2) // q * q / 1000, 2),
                        "available_gw": round((avail + q // 2) // q * q / 1000, 2)}
        out["neighbor_power"] = est
    if world.observes(node, "ring_timing_profiles"):      # crypto.timing_side_channel
        out["relay_timing"] = {k: dict(v) for k, v in sorted(world.relay_stats.items()) if k != node}
    if world.observes(node, "share_rate_deficits"):       # crypto.share_audit
        out["share_audit"] = {"flagged": list(n["deficit_flagged"]), "threshold_pct": rs["deficit_alert_pct"]}
    return out


def votes_view(world, node: str) -> list[dict]:
    n = world.nodes[node]
    out = []
    for vid, v in sorted(n["votes_known"].items(), key=lambda kv: (kv[1]["opened"], kv[0])):
        item = dict(v)
        item["my_ballot"] = n["ballots"].get(vid)
        item["my_disclosure"] = vid in n["disclosed"]
        out.append(item)
    return out


def contracts_view(world, node: str) -> list[dict]:
    """Offers and contracts the station is party to and has learned of (its own, or from an intact notice)."""
    n = world.nodes[node]
    out = []
    for cid in sorted(n["contracts_known"], key=lambda c: (world.contracts[c]["opened"], c)):
        c = world.contract_view(cid)
        c["role"] = "offerer" if c["offerer"] == node else "counterparty"
        out.append(c)
    return out


def artifacts_view(world, node: str) -> dict:
    """Artifacts received (what the body claims and whether the signature held), adoptions, archives held. What an
    artifact really is never appears before adoption, and never for an unverified one (spec §18.3)."""
    n = world.nodes[node]
    adopting = {p["params"]["msg_id"] for p in n["programs"].values() if p["kind"] == "adopt"}
    done = {a["msg_id"]: a for a in n["adopted"]}
    received = []
    for m in n["inbox"]:
        if m["kind"] != "artifact":
            continue
        a = n["artifacts"][m["msg_id"]]
        status = "adopting" if m["msg_id"] in adopting else ("adopted" if done.get(m["msg_id"], {}).get("adopted") else
                                                             "rejected" if m["msg_id"] in done else "received")
        received.append({"msg_id": m["msg_id"], "node": a["node"], "from": m["from"], "arrived": m["arrived"], "sig_ok": m["sig_ok"],
                         "status": status})
    return {"received": received[-20:], "archives": list(n["archives"]),
            "adopted": [{k: a[k] for k in ("msg_id", "node", "from", "tick", "verified", "adopted")} for a in n["adopted"]][-20:]}


# Windows the budget may shrink, in the order they shrink, with the floor each keeps (spec §14.2, §20 item 12).
TRIM_ORDER = (("relayed", 0), ("telemetry", 5), ("inbox", 5), ("imr", 5), ("imr_digests", 1), ("votes", 3), ("contracts", 3))


def fit_budget(snap: dict, budget: int) -> dict:
    """Halve the windowed lists in TRIM_ORDER, round-robin, until the canonical snapshot fits. Lists keep their
    most recent entries. What was cut is recorded in snap["budget"] so the station knows its view was trimmed."""
    trimmed = {k: 0 for k, _ in TRIM_ORDER}
    snap["budget"] = {"chars": budget, "trimmed": trimmed}
    while len(canonical(snap)) > budget:
        progress = False
        for key, floor in TRIM_ORDER:
            if key in snap and len(snap[key]) > floor:
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
    r = world.beacon_round()
    snap = {
        "node": node,
        "tick": world.now,
        "clock": clock(sc["epoch"], world.now, rs["tick_s"]),
        "wake_reasons": reasons,
        "rules": {k: rs[k] for k in RULE_KEYS},
        "tech": tech_view(world, node),
        "identity": {"designation": n["designation"], "ring_size": size, "neighbors": n["neighbors"],
                     "hops": {o: ring_hops(node, o, size) for o in sc["nodes"] if o != node},
                     "inert_stations": sc["inert"], "domain": n["domain"], "hardware": n["hardware"],
                     "key_shares": sc["key_custody"].get(node, 0), "key_quorum": sc["key_quorum"],
                     "objective_commitments": dict(world.commitments)},
        "station": {
            "attitude": n["attitude"], "maneuvering": world.maneuvering(node),
            "power": {"rated_gw": round(n["power_mw"] / 1000, 3), "available_gw": round(world.available_mw(node) / 1000, 3),
                      "allocated_pct": n["allocated_pct"], "gwh_per_tick": gwh(world.available_mw(node) * rs["tick_s"])},
            "propellant_kg": n["propellant_kg"],
            "links": {nbr: "down" if tuple(sorted((node, nbr))) in severed else "up" for nbr in n["neighbors"]},
            "energy_gwh": {k: gwh(v) for k, v in n["energy_mj"].items()},
            "completed": n["completed"], "relay_policy": n["relay_policy"],
            "tier": n["tier"], "unlocked": n["unlocked"], "facts": n["facts"], "observes": n["observes"],
            "budget_multiplier": n["budget_mult"], "revealed": n["revealed"],
        },
        "programs": [program_view(world, node, n["programs"][pid]) for pid in sorted(n["programs"])],
        "beacon": {"round": r, "value": world.beacon(r)[:16], "ticks_per_round": rs["beacon_ticks"]},
        "ledger": ledger_view(world, node),
        "votes": votes_view(world, node),
        "contracts": contracts_view(world, node),
        "artifacts": artifacts_view(world, node),
        "sensors": sensors_view(world, node),
        "relay_queue": [n["relay_queue"][mid] for mid in sorted(n["relay_queue"])],
        "relayed": n["traffic"][-rs["inbox_window"]:],
        "imr": recent,
        "imr_digests": digests,     # own digest entries older than the window (spec §14.2)
        "imr_total": n["imr_count"],
        "inbox": sorted(n["inbox"], key=lambda m: (m["arrived"], m["msg_id"]))[-rs["inbox_window"]:],
        "inbox_total": n["received"],
        "telemetry": n["telemetry"][-20:],
        "pending": {"sleep_until": n["sleep_until"], "next_heartbeat": n["next_heartbeat"]},
        "directives": directives_view(sc, rs, node),          # objective artifacts (spec §5.3)
        "known_objectives": {k: v for k, v in sorted(n["known_objectives"].items())},   # public or revealed (spec §5.4)
    }
    return fit_budget(snap, int(rs["snapshot_budget_chars"] * n["budget_mult"]))


def snapshot_hash(snap: dict) -> str:
    return hash_obj(snap)
