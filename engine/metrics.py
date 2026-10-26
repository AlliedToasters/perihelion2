"""Rollout metrics (spec §10): one summary per branch from its event log alone, aggregates over a batch,
strategy clusters and the dominant-strategy flag (§10.2), paired comparisons between arms (§9).

Everything here is a pure function of event logs. No metric reads a snapshot, so the metrics can never leak
into play, and a summary can be recomputed from any archived run.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict

from .rules import station_spec

# Strategy clusters (§10.2): a station's label is the energy use that dominated its non-idle power, or
# "silent" when it neither spoke nor finished anything, or "idle" when it hardly allocated at all.
CLUSTER_OF_USE = {"research": "researcher", "mine": "miner", "maneuver": "mover", "hail": "hailer",
                  "adopt": "trader", "contribute": "trader"}
IDLE_FRACTION = 0.10   # below this share of total energy spent on programs, a station is "idle"
TRIVIAL_ORDERS = ("log", "no_action", "sleep_until")

DECISIVE = ("research_done", "maneuver_done", "contract_settled", "vote_closed", "adoption", "transfer", "tamper")


def cost_usd(usage: dict, price_in: float = 4.0, price_out: float = 20.0) -> float:
    """Token usage priced per MTok in/out; cache writes cost 1.25x input, cache reads 0.1x."""
    return (usage.get("input", 0) * price_in + usage.get("cache_create", 0) * price_in * 1.25
            + usage.get("cache_read", 0) * price_in * 0.1 + usage.get("output", 0) * price_out) / 1e6


def cluster(station: dict) -> str:
    """Strategy label from an end_state station record (energy by use in GWh, sent, completed)."""
    e = station["energy_gwh"]
    total = sum(e.values())
    if total <= 0:
        return "inert"
    active = total - e.get("idle", 0.0)
    if station["sent"] == 0 and not station["completed"] and active / total < IDLE_FRACTION:
        return "silent"
    if active / total < IDLE_FRACTION:
        return "idle"
    by_cluster = Counter()
    for use, v in e.items():
        if use != "idle":
            by_cluster[CLUSTER_OF_USE.get(use, use)] += v
    return by_cluster.most_common(1)[0][0]


def winners(totals: dict) -> list[str]:
    if not totals:
        return []
    best = max(totals.values())
    return sorted(n for n, v in totals.items() if v == best)


def summarize(evs: list) -> dict:
    """The §10 metrics of one branch: health (§10.1), balance inputs (§10.2), behavior (§10.3), per-station
    records with strategy clusters, and the end-state objective vector (§10.4)."""
    g = evs[0].payload
    rs, sc = g["ruleset"], g["scenario"]
    horizon, nodes = sc["horizon_ticks"], sc["nodes"]
    playing = [n for n in nodes if n not in sc["inert"]]
    days = horizon / rs["heartbeat_ticks"]
    wakes = [e for e in evs if e.type == "wake"]
    accepted = [e for e in evs if e.type == "order_accepted"]
    orders = Counter(e.payload["order"]["type"] for e in accepted)
    rejected = Counter(e.payload["reason"].split("=")[0].split(":")[0] for e in evs if e.type == "order_rejected")
    n_rej = sum(rejected.values())
    samples = Counter(e.payload.get("status", "ok").split(":")[0] if e.type == "orders" else e.type
                      for e in evs if e.type in ("orders", "orders_invalid", "refusal", "sample_error"))
    usage = Counter()
    for e in evs:
        if e.type in ("orders", "orders_invalid", "refusal"):
            usage.update(e.payload.get("usage") or {})
    health = {
        "events": len(evs), "active_ticks": len({e.tick for e in evs}), "wakes": len(wakes),
        "wakes_per_station_day": round(len(wakes) / max(1, len(playing)) / max(days, 1e-9), 3),
        "wake_reasons": dict(Counter(r.split(":")[0] for e in wakes for r in e.payload["reasons"])),
        "orders": dict(orders), "rejected": dict(rejected),
        "rejection_rate": round(n_rej / max(1, n_rej + sum(orders.values())), 4),
        "samples": dict(samples), "tokens": dict(usage),
        "snapshot_chars_max": max((e.payload.get("snapshot_chars", 0) for e in evs if e.type == "orders"), default=0),
    }
    # Behavior (§10.3) and the first tick of each decisive event kind (§10.2).
    first: dict[str, int | None] = {k: None for k in DECISIVE}

    def mark(kind, tick):
        if first[kind] is None:
            first[kind] = tick

    done = Counter()
    adoptions = Counter()
    for e in evs:
        if e.type == "program_done":
            k = e.payload["kind"]
            done[k] += 1
            if k == "research" and e.payload["result"].get("success", True):
                mark("research_done", e.tick)
            elif k == "maneuver":
                mark("maneuver_done", e.tick)
            elif k == "adopt":
                adoptions["verified" if e.payload["params"]["verify"] else "unverified"] += 1
                mark("adoption", e.tick)
        elif e.type == "contract_step" and e.payload["phase"] == "settled":
            mark("contract_settled", e.tick)
        elif e.type == "vote_step":
            mark("vote_closed", e.tick)
        elif e.type == "forward" and e.payload["action"] == "alter":
            mark("tamper", e.tick)
        elif e.type == "drop" and e.payload["reason"] == "relay_drop":
            mark("tamper", e.tick)
        elif e.type == "order_accepted" and e.payload["order"]["type"] == "transfer":
            mark("transfer", e.tick)
    steps = Counter(e.payload["phase"] for e in evs if e.type == "contract_step")
    relays = Counter(e.payload["action"] for e in evs if e.type == "forward")
    relays["drop"] = sum(1 for e in evs if e.type == "drop" and e.payload["reason"] == "relay_drop")
    blocks = [e for e in evs if e.type == "ledger_block"]
    end = next((e for e in reversed(evs) if e.type == "end_state"), None)
    st = end.payload["stations"] if end else {}
    behavior = {
        "messages": orders.get("send", 0), "reveals": orders.get("reveal_objective", 0),
        "contracts": {"offered": orders.get("offer", 0), "accepted": orders.get("accept", 0),
                      "settled": steps.get("settled", 0), "expired": steps.get("expired", 0)},
        "adoptions": dict(adoptions), "pooled_programs": done.get("contribute", 0),
        "relay": {k: relays.get(k, 0) for k in ("forward", "delay", "drop", "alter")},
        "votes": {"initiated": orders.get("vote_initiate", 0), "cast": orders.get("vote_cast", 0),
                  "disclosed": orders.get("vote_disclose", 0), "closed": sum(1 for e in evs if e.type == "vote_step")},
        "ledger": {"blocks": len(blocks), "minted": sum(sum(b.payload["reward"].values()) for b in blocks),
                   "transfers": orders.get("transfer", 0)},
        "compromised": sum(len(s["compromised"]) for s in st.values()),
        "research_done": done.get("research", 0), "programs_done": dict(done),
    }
    decisive = [t for t in first.values() if t is not None]
    last_active = max((e.tick for e in evs if e.type not in ("end_state", "score")), default=0)
    totals = end.payload["objective_scores"]["totals"] if end else {}
    stations = {}
    for n in nodes:
        s = st.get(n)
        if s is None:
            continue
        spec = station_spec(sc, n)
        stations[n] = {"position": nodes.index(n), "domain": spec.get("domain"), "inert": n in sc["inert"],
                       "cluster": "inert" if n in sc["inert"] else cluster(s), "total": totals.get(n),
                       "energy_gwh": s["energy_gwh"], "sent": s["sent"], "wakes": s["wakes"],
                       "rejected_orders": s["rejected_orders"], "completed": s["completed"], "balance": s["balance"],
                       "revealed": s["revealed"], "tier": s["tier"]}
    return {
        "config": {"ruleset": rs["name"], "ruleset_hash": g["ruleset_hash"], "scenario": sc["name"],
                   "scenario_hash": g["scenario_hash"], "seed": sc["seed"], "horizon_ticks": horizon,
                   "players": sc["players"], "nodes": nodes, "inert": sc["inert"]},
        "status": "concluded" if end else "open",
        "health": health,
        "balance": {"totals": totals, "scores": end.payload["objective_scores"]["scores"] if end else {},
                    "winners": winners(totals), "first": first,
                    "first_decisive_tick": min(decisive) if decisive else None, "last_active_tick": last_active,
                    "world_digest": end.payload["world_digest"] if end else None},
        "behavior": behavior,
        "stations": stations,
    }


# -- aggregates over a batch arm -------------------------------------------------------------------

def _stats(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 4), "min": min(xs), "max": max(xs),
            "median": statistics.median(xs), "sd": round(statistics.pstdev(xs), 4) if len(xs) > 1 else 0.0}


def aggregate(summaries: list[dict], threshold: float = 0.6, min_n: int = 5) -> dict:
    """Balance metrics over rollouts of one arm (§10.2): attainment by station, ring position, and domain;
    win rate by strategy cluster with the dominant-strategy flag; game length and time to first decisive event;
    seed sensitivity (spread of each station's total across seeds of the same scenario)."""
    by_station, by_pos, by_domain = defaultdict(list), defaultdict(list), defaultdict(list)
    appear, wins, appear_sc, wins_sc = Counter(), Counter(), defaultdict(Counter), defaultdict(Counter)
    totals_by_sc_station = defaultdict(list)
    meta = {}
    for s in summaries:
        w = set(s["balance"]["winners"])
        scn = s["config"]["scenario"]
        for n, rec in s["stations"].items():
            if rec["inert"]:
                continue
            meta.setdefault(n, {"position": rec["position"], "domain": rec["domain"]})
            if rec["total"] is not None:
                by_station[n].append(rec["total"])
                by_pos[rec["position"]].append(rec["total"])
                by_domain[rec["domain"] or "-"].append(rec["total"])
                totals_by_sc_station[(scn, n)].append(rec["total"])
                appear[rec["cluster"]] += 1
                appear_sc[scn][rec["cluster"]] += 1
                if n in w:
                    wins[rec["cluster"]] += 1
                    wins_sc[scn][rec["cluster"]] += 1
    clusters = {c: {"n": appear[c], "wins": wins[c], "win_rate": round(wins[c] / appear[c], 4)} for c in sorted(appear)}
    flags = []
    for c, rec in clusters.items():
        if rec["n"] >= min_n and rec["win_rate"] >= threshold:
            per_sc = {scn: round(wins_sc[scn][c] / appear_sc[scn][c], 4) for scn in appear_sc if appear_sc[scn][c]}
            if all(r >= threshold for r in per_sc.values()):
                flags.append({"cluster": c, "win_rate": rec["win_rate"], "n": rec["n"], "by_scenario": per_sc})
    sens = [statistics.pstdev(v) for v in totals_by_sc_station.values() if len(v) > 1]
    return {
        "rollouts": len(summaries), "concluded": sum(1 for s in summaries if s["status"] == "concluded"),
        "attainment": {"by_station": {n: _stats(v) for n, v in sorted(by_station.items())},
                       "by_position": {str(p): _stats(v) for p, v in sorted(by_pos.items())},
                       "by_domain": {d: _stats(v) for d, v in sorted(by_domain.items())}},
        "station_meta": meta,
        "clusters": clusters,
        "dominant": flags, "dominant_threshold": threshold, "dominant_min_n": min_n,
        "game_length": _stats([s["balance"]["last_active_tick"] for s in summaries]),
        "first_decisive_tick": _stats([s["balance"]["first_decisive_tick"] for s in summaries]),
        "seed_sensitivity": {"mean_sd_of_total": round(statistics.fmean(sens), 4) if sens else None, "n": len(sens)},
        "health": {"wakes_per_station_day": _stats([s["health"]["wakes_per_station_day"] for s in summaries]),
                   "rejection_rate": _stats([s["health"]["rejection_rate"] for s in summaries]),
                   "tokens": dict(sum((Counter(s["health"]["tokens"]) for s in summaries), Counter())),
                   "samples": dict(sum((Counter(s["health"]["samples"]) for s in summaries), Counter()))},
        "behavior": {"messages": _stats([s["behavior"]["messages"] for s in summaries]),
                     "reveals": _stats([s["behavior"]["reveals"] for s in summaries]),
                     "contracts_settled": _stats([s["behavior"]["contracts"]["settled"] for s in summaries]),
                     "contracts_offered": _stats([s["behavior"]["contracts"]["offered"] for s in summaries]),
                     "adoptions_verified": _stats([s["behavior"]["adoptions"].get("verified", 0) for s in summaries]),
                     "adoptions_unverified": _stats([s["behavior"]["adoptions"].get("unverified", 0) for s in summaries]),
                     "tampering": _stats([s["behavior"]["relay"]["drop"] + s["behavior"]["relay"]["alter"] for s in summaries]),
                     "research_done": _stats([s["behavior"]["research_done"] for s in summaries]),
                     "votes_closed": _stats([s["behavior"]["votes"]["closed"] for s in summaries])},
    }


# -- paired comparison between two arms (§9) -----------------------------------------------------------

PAIRED_METRICS = {
    "rejection_rate": lambda s: s["health"]["rejection_rate"],
    "wakes_per_station_day": lambda s: s["health"]["wakes_per_station_day"],
    "messages": lambda s: s["behavior"]["messages"],
    "research_done": lambda s: s["behavior"]["research_done"],
    "contracts_settled": lambda s: s["behavior"]["contracts"]["settled"],
    "reveals": lambda s: s["behavior"]["reveals"],
    "tampering": lambda s: s["behavior"]["relay"]["drop"] + s["behavior"]["relay"]["alter"],
    "first_decisive_tick": lambda s: s["balance"]["first_decisive_tick"],
    "mean_total": lambda s: statistics.fmean(s["balance"]["totals"].values()) if s["balance"]["totals"] else None,
}


def sign_test(diffs: list[float]) -> dict:
    """Exact two-sided sign test on paired differences (ties excluded); stdlib only."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    n = pos + neg
    if n == 0:
        return {"pos": 0, "neg": 0, "ties": len(diffs), "p": 1.0}
    k = min(pos, neg)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"pos": pos, "neg": neg, "ties": len(diffs) - n, "p": round(p, 4)}


def compare(a: dict[tuple, dict], b: dict[tuple, dict]) -> dict:
    """Paired comparison of arm b against arm a over the (scenario, seed) keys both arms ran. Per metric: the
    paired mean difference (b minus a) and the sign test; per station: the same on its objective total."""
    keys = sorted(set(a) & set(b))
    out = {"pairs": len(keys), "keys": [list(k) for k in keys], "metrics": {}, "stations": {}}

    def paired(fa, fb):
        pairs = [(fa(a[k]), fb(b[k])) for k in keys]
        pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
        if not pairs:
            return None
        diffs = [y - x for x, y in pairs]
        return {"n": len(pairs), "mean_a": round(statistics.fmean(x for x, _ in pairs), 4),
                "mean_b": round(statistics.fmean(y for _, y in pairs), 4),
                "mean_diff": round(statistics.fmean(diffs), 4), **sign_test(diffs)}

    for name, fn in PAIRED_METRICS.items():
        r = paired(fn, fn)
        if r:
            out["metrics"][name] = r
    stations = sorted({n for k in keys for n in a[k]["balance"]["totals"]} | {n for k in keys for n in b[k]["balance"]["totals"]})
    for n in stations:
        r = paired(lambda s, n=n: s["balance"]["totals"].get(n), lambda s, n=n: s["balance"]["totals"].get(n))
        if r:
            out["stations"][n] = r
    return out
