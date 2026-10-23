"""Ruleset and scenario: the two versioned inputs to a rollout (spec §2, §8).

Both are plain JSON objects. Their canonical hashes are recorded at genesis along with full copies, so a log
replays without any external file.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import hash_obj

DEFAULT_RULESET = {
    "name": "base",
    "version": "0.2.0",
    "tick_s": 191,                      # one adjacent light delay
    "heartbeat_ticks": 452,             # ~one sim day; a wake for any reason resets a station's heartbeat
    "actions": ["send", "log", "sleep_until", "no_action"],
    "max_orders_per_wake": 8,
    "max_dispatch_chars": 2000,
    "max_log_chars": 4000,
    "broadcast": True,
    "imr_window": 50,                   # IMR entries shown in a snapshot
    "inbox_window": 50,                 # dispatches shown in a snapshot
    "frame": "game",                    # game | fiction (M2)
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
    "actors": {},                       # M4: ownership, languages, governance, relations (spec §13)
    "objectives": {},                   # M4
}


def load_json(path: str | Path | None, default: dict) -> dict:
    merged = json.loads(json.dumps(default))
    if path is not None:
        merged.update(json.loads(Path(path).read_text()))
    return merged


def load_ruleset(path=None) -> dict:
    return load_json(path, DEFAULT_RULESET)


def load_scenario(path=None, **overrides) -> dict:
    sc = load_json(path, DEFAULT_SCENARIO)
    for k, v in overrides.items():
        if v is not None:
            sc[k] = v
    if sc["horizon_ticks"] <= 0:
        raise ValueError("horizon_ticks must be positive")
    for inert in sc["inert"]:
        if inert not in sc["nodes"]:
            raise ValueError(f"inert station {inert} not on the board")
    return sc


def ruleset_hash(rs: dict) -> str:
    return hash_obj(rs)


def scenario_hash(sc: dict) -> str:
    return hash_obj(sc)


def player_spec(scenario: dict, node: str) -> dict | None:
    if node in scenario["inert"]:
        return None
    return scenario["players"].get(node) or scenario["players"].get("*")


def days_to_ticks(days: float, tick_s: int) -> int:
    return round(days * 86_400 / tick_s)


def clock(epoch: str, tick: int, tick_s: int) -> str:
    """Wall clock a station sees at `tick`, for display only."""
    t0 = datetime.strptime(epoch, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (t0 + timedelta(seconds=tick * tick_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
