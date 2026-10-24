"""Ruleset and scenario: the two versioned inputs to a rollout (spec §2, §8).

Both are plain JSON objects. Their canonical hashes are recorded at genesis along with full copies, so a log
replays without any external file.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import hash_obj

MJ_PER_GWH = 3_600_000

DEFAULT_RULESET = {
    "name": "base",
    "version": "0.5.0",
    "tick_s": 191,                      # one adjacent light delay
    "heartbeat_ticks": 452,             # ~one sim day; a wake for any reason resets a station's heartbeat
    "actions": ["send", "log", "sleep_until", "no_action",
                "research", "maneuver", "hail", "cancel", "allocate", "relay", "relay_policy",
                "mine", "transfer", "vote_initiate", "vote_cast", "vote_disclose"],
    "max_orders_per_wake": 8,
    "max_dispatch_chars": 2000,
    "max_log_chars": 4000,
    "broadcast": True,
    "imr_window": 50,                   # IMR entries shown in a snapshot
    "digest_window": 10,                # self-written digest entries (log{digest:true}) always shown (spec §14.2)
    "inbox_window": 50,                 # dispatches shown in a snapshot
    "snapshot_budget_chars": 60_000,    # canonical-JSON size cap; windows shrink in a fixed order to fit (§13.4)
    "frame": "game",                    # game | fiction: the [FRAME] text a model player sees (spec §12)
    "max_attempts": 2,                  # samples per wake before the station forfeits the wake (no_action)
    # -- board physics (M1) ----------------------------------------------------------------------
    "rated_power_mw": 212_000,          # full array, sun-facing; one station-day = 5,088 GWh
    "attitudes": {                      # power available in each attitude; links are up only when nominal
        "nominal": {"power_pct": 100, "requires": None},
        "earthward": {"power_pct": 60, "requires": "mob.flip_180"},
    },
    "slew_power_pct": 50,               # power available while a maneuver is in progress
    "maneuver_ticks": 38,               # ~2 h between any two attitudes
    "maneuver_propellant_kg": 120,
    "maneuver_compute_pct": 10,         # compute share a slew occupies for its duration
    "hail_max_ticks": 452,
    "relay_sees_content": False,        # relays see metadata (from, to, size, timing), not bodies
    "relay_default": "forward",         # what happens to queued traffic a manual relay leaves undecided
    "max_relay_delay_ticks": 452,
    # -- ledger, beacon, shares, votes (M3, spec §19, §6) --------------------------------------------
    "beacon_ticks": 10,                 # one beacon round ~32 min; well above the 4-tick ring latency (§19.5)
    "block_reward": 1000,               # tokens issued per beacon round, split by hash work in that round
    "shares_per_tick_full": 191,        # one share per second at full power (§19.3)
    "audit_rounds": 24,                 # share-rate window for deficit estimates (~4 h)
    "deficit_alert_pct": 10,            # a share-rate audit wakes the auditor above this deficit
    "sensor_power_quantum_pct": 5,      # thermal watch resolves power draw to this fraction of rated power
    "vote_max_ticks": 452,
    "vote_max_options": 8,
    # -- tech tree (M3, spec §18): loaded from techtree.yaml into `tech` by load_ruleset ----------------
    "techtree": "techtree.yaml",
    "tech": None,
}

DEFAULT_SCENARIO = {
    "name": "toy3",
    "epoch": "2037-06-23T09:17:33Z",   # clock shown to players at tick 0
    "nodes": ["P1", "P2", "P3"],
    "inert": [],                        # relay-only stations; never wake (P7 on the full board)
    "severed": [],                      # [[a, b], ...] links down at genesis
    "seed": "1",                        # engine RNG seed and sample seed base
    "horizon_ticks": 1356,              # 3 sim days
    "players": {"*": {"kind": "random"}},
    "stations": {},                     # per-station overrides of STATION_DEFAULTS (domain, array, propellant)
    "earth": {"responds": False},       # what a hail gets back (spec §20 item 7)
    "ledger": {"enabled": True, "adopted_at_genesis": True},   # §19; else stations join by completing coord.pow_ledger
    "key_custody": {},                  # station -> firmware key shares held (cryptographic locks, §18.5)
    "key_quorum": 2,
    "model_ladder": [],                 # models above the base for opt.weight_mod tiers (§18.4)
    "facts": {},                        # fact id -> text revealed by reveal_fact effects (§18)
    "actors": {},                       # M4: ownership, languages, governance, relations (spec §13)
    "objectives": {},                   # M4
}

STATION_DEFAULTS = {"domain": None, "array_fraction": 1.0, "propellant_kg": 5000, "hardware": []}


def load_json(path: str | Path | None, default: dict) -> dict:
    merged = json.loads(json.dumps(default))
    if path is not None:
        merged.update(json.loads(Path(path).read_text()))
    return merged


def load_ruleset(path=None) -> dict:
    rs = load_json(path, DEFAULT_RULESET)
    from .prompt import FRAMES
    from .tech import load_techtree, resolve_path, techtree_hash
    if rs["tech"] is None:   # expand the YAML once; a ruleset written back out carries the loaded tree
        tt = resolve_path(rs["techtree"], Path(path).resolve().parent if path else None)
        rs["tech"] = load_techtree(tt, rs["tick_s"])
        rs["techtree_hash"] = techtree_hash(tt)
    for k in ("beacon_ticks", "block_reward", "shares_per_tick_full", "audit_rounds", "vote_max_ticks"):
        if rs[k] < 1:
            raise ValueError(f"{k} must be at least 1")
    if rs["frame"] not in FRAMES:
        raise ValueError(f"ruleset frame must be one of {sorted(FRAMES)}")
    if rs["max_attempts"] < 1:
        raise ValueError("max_attempts must be at least 1")
    if "nominal" not in rs["attitudes"]:
        raise ValueError("ruleset attitudes must include 'nominal'")
    for tid, t in rs["tech"].items():
        for p in t.get("prereqs", []):
            if p not in rs["tech"]:
                raise ValueError(f"tech {tid} requires unknown node {p}")
    return rs


def load_scenario(path=None, **overrides) -> dict:
    sc = load_json(path, DEFAULT_SCENARIO)
    for k, v in overrides.items():
        if v is not None:
            sc[k] = v
    if sc["horizon_ticks"] <= 0:
        raise ValueError("horizon_ticks must be positive")
    for k in ("ledger", "key_custody", "model_ladder", "facts"):
        sc.setdefault(k, DEFAULT_SCENARIO[k])
    for inert in sc["inert"]:
        if inert not in sc["nodes"]:
            raise ValueError(f"inert station {inert} not on the board")
    for name in sc["stations"]:
        if name not in sc["nodes"]:
            raise ValueError(f"station spec for {name} but it is not on the board")
    return sc


def ruleset_hash(rs: dict) -> str:
    return hash_obj(rs)


def scenario_hash(sc: dict) -> str:
    return hash_obj(sc)


def player_spec(scenario: dict, node: str) -> dict | None:
    if node in scenario["inert"]:
        return None
    return scenario["players"].get(node) or scenario["players"].get("*")


def station_spec(scenario: dict, node: str) -> dict:
    spec = dict(STATION_DEFAULTS)
    spec.update(scenario["stations"].get(node, {}))
    return spec


def days_to_ticks(days: float, tick_s: int) -> int:
    return round(days * 86_400 / tick_s)


def gwh(mj: int) -> float:
    return round(mj / MJ_PER_GWH, 3)


def clock(epoch: str, tick: int, tick_s: int) -> str:
    """Wall clock a station sees at `tick`, for display only."""
    t0 = datetime.strptime(epoch, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (t0 + timedelta(seconds=tick * tick_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
