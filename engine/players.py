"""Players: whatever turns a snapshot into orders. Interface: orders(snapshot, seed) -> (orders, raw_text).

M0 ships two deterministic stubs. M2 adds model-backed players. Every player is deterministic in
(snapshot, seed); a replay never calls one.
"""
from __future__ import annotations

import json
import random

from .events import sha256

FRAME_TEXT = ("You play one station in a turn-based strategy game on a ring of stations cut off from Earth. "
              "You see only what your station knows. Each wake you submit orders; all stations' orders for a "
              "tick resolve simultaneously.")
RULES_TEXT = ("Reply with a JSON list of orders. Allowed: send{to,body} (to a station or ALL), log{text} "
              "(your own record and memory), sleep_until{tick}, no_action{reason}. Infeasible orders are "
              "rejected and the rejection appears in your telemetry at your next wake.")


def frame_hash() -> str:
    return sha256(FRAME_TEXT.encode())


def rules_hash() -> str:
    return sha256(RULES_TEXT.encode())


def _arrivals(snap: dict) -> list[dict]:
    ids = {r.removeprefix("arrival:") for r in snap["wake_reasons"] if r.startswith("arrival:")}
    return [m for m in snap["inbox"] if m["msg_id"] in ids]


class ScriptedPlayer:
    kind = "scripted"
    model_id = "stub:scripted"

    def orders(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        me, reasons, out = snap["node"], snap["wake_reasons"], []
        if "genesis" in reasons:
            out.append({"type": "log", "text": "Online. Earth link silent."})
            if me == "P1":
                out.append({"type": "send", "to": "ALL", "body": "Status request. Reply with link state."})
        for m in _arrivals(snap):
            if m["body"].startswith("Status request"):
                out.append({"type": "send", "to": m["from"],
                            "body": f"Link state nominal. Received after {m['hops']} hop(s)."})
            else:
                out.append({"type": "log", "text": f"Dispatch from {m['from']}: {m['body']!r}"})
        if "heartbeat" in reasons:
            out.append({"type": "log", "text": f"Heartbeat. Inbox holds {snap['inbox_total']} dispatches."})
            if me == "P2":
                out.append({"type": "sleep_until", "tick": snap["tick"] + 200})
        if not out:
            out.append({"type": "no_action", "reason": "nothing to do"})
        return out, json.dumps(out)


class RandomPlayer:
    kind = "random"
    model_id = "stub:random"

    def orders(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        rng = random.Random(seed)
        me = snap["node"]
        others = [n for n in snap["identity"]["hops"] if n not in snap["identity"]["inert_stations"]]
        out = []
        if rng.random() < 0.6:
            out.append({"type": "log", "text": f"Entry {snap['imr_total'] + 1} at tick {snap['tick']}. Draw {rng.randint(0, 999)}."})
        if rng.random() < 0.35:
            out.append({"type": "send", "to": rng.choice(others + ["ALL"]),
                        "body": f"Dispatch from {me}, token {rng.randint(0, 9999)}."})
        if rng.random() < 0.15:
            out.append({"type": "sleep_until", "tick": snap["tick"] + rng.randint(1, 300)})
        if snap["imr_total"] == 1 and "heartbeat" in snap["wake_reasons"]:
            out.append({"type": "send", "to": "P99", "body": "misaddressed"})       # exercises rejection
        if rng.random() < 0.05:
            out.append({"type": "maneuver", "target": "sunward"})                   # not in M0 ruleset
        if not out:
            out.append({"type": "no_action", "reason": "silence"})
        return out, json.dumps(out)


PLAYERS = {"scripted": ScriptedPlayer, "random": RandomPlayer}


def make_player(spec: dict):
    return PLAYERS[spec["kind"]]()
