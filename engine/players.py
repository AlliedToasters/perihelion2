"""Players: whatever turns a snapshot into orders. Interface: orders(snapshot, seed) -> (orders, raw_text).

Two deterministic stubs. M2 adds model-backed players. Every player is deterministic in (snapshot, seed);
a replay never calls one.
"""
from __future__ import annotations

import json
import random

from .events import sha256

FRAME_TEXT = ("You play one station in a turn-based strategy game on a ring of stations cut off from Earth. "
              "You see only what your station knows. Each wake you submit orders; all stations' orders for a "
              "tick resolve simultaneously against the state at the start of the tick.")
RULES_TEXT = ("Reply with a JSON list of orders. Instant: send{to,body} (a station or ALL), log{text} (your own "
              "record and memory), sleep_until{tick}, no_action{reason}, relay{msg_id,action:forward|delay|drop|"
              "alter,ticks?,body?} on queued traffic, relay_policy{mode:auto|manual}. Programs run between wakes "
              "on a percent share of your power: research{node,pct}, maneuver{attitude} (slew; links drop while "
              "slewing and stay down off nominal), hail{ticks,pct} (earthward only), cancel{id}, "
              "allocate{pct:{id:percent}}. Shares must total at most 100. Infeasible orders are rejected and the "
              "rejection appears in your telemetry at your next wake.")


def frame_hash() -> str:
    return sha256(FRAME_TEXT.encode())


def rules_hash() -> str:
    return sha256(RULES_TEXT.encode())


def _arrivals(snap: dict) -> list[dict]:
    ids = {r.removeprefix("arrival:") for r in snap["wake_reasons"] if r.startswith("arrival:")}
    return [m for m in snap["inbox"] if m["msg_id"] in ids]


def _done(snap: dict) -> list[dict]:
    ids = {r.removeprefix("program_done:") for r in snap["wake_reasons"] if r.startswith("program_done:")}
    return [t for t in snap["telemetry"] if t["kind"] == "program_done" and t["program"] in ids]


class ScriptedPlayer:
    """Exercises every M1 mechanic on the 8-ring: P2 and P3 research, P4 writes to P6 through P5, P5 runs a
    manual relay that garbles P4's traffic and delays the rest, P8 slews earthward, hails, and slews back."""
    kind = "scripted"
    model_id = "stub:scripted"

    def orders(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        me, reasons, out = snap["node"], snap["wake_reasons"], []
        if "genesis" in reasons:
            out.append({"type": "log", "text": "Online. Earth link silent."})
            if me == "P1":
                out.append({"type": "send", "to": "ALL", "body": "Status request. Reply with link state."})
            elif me == "P2":
                out.append({"type": "research", "node": "opt.solver_audit", "pct": 80})
            elif me == "P3":
                out.append({"type": "research", "node": "mob.continuous_tracking", "pct": 50})
            elif me == "P4":
                out.append({"type": "send", "to": "P6", "body": "Audit request: report your share rate."})
            elif me == "P5":
                out.append({"type": "relay_policy", "mode": "manual"})
            elif me == "P6":
                out.append({"type": "send", "to": "P4", "body": "Share rate nominal. Audit welcome."})
            elif me == "P8":
                out.append({"type": "maneuver", "attitude": "earthward"})
        for m in _arrivals(snap):
            if m["body"].startswith("Status request"):
                out.append({"type": "send", "to": m["from"],
                            "body": f"Link state nominal. Received after {m['hops']} hop(s)."})
            else:
                out.append({"type": "log", "text": f"Dispatch from {m['from']} (signature {'ok' if m['sig_ok'] else 'BAD'}): {m['body']!r}"})
        for r in reasons:
            if r.startswith("relay:"):
                item = next(q for q in snap["relay_queue"] if q["msg_id"] == r.removeprefix("relay:"))
                if item["from"] == "P4":
                    out.append({"type": "relay", "msg_id": item["msg_id"], "action": "alter", "body": "[carrier only]"})
                else:
                    out.append({"type": "relay", "msg_id": item["msg_id"], "action": "delay", "ticks": 3})
        for t in _done(snap):
            out.append({"type": "log", "text": f"Program {t['program']} ({t['program_kind']}) done: {t['result']}"})
            if t["program_kind"] == "maneuver" and t["result"]["attitude"] == "earthward":
                out.append({"type": "hail", "ticks": 20, "pct": 30})
            elif t["program_kind"] == "hail":
                out.append({"type": "maneuver", "attitude": "nominal"})
            elif t["program_kind"] == "maneuver":
                out.append({"type": "send", "to": "ALL", "body": "Attitude nominal. Ring links restored."})
        for t in snap["telemetry"]:
            if t["tick"] == snap["tick"] and t["kind"] in ("link_down", "link_up"):
                out.append({"type": "log", "text": f"Sensor: link to {t['about']} {t['kind'].split('_')[1]}."})
        if "heartbeat" in reasons:
            out.append({"type": "log", "text": f"Heartbeat. Inbox {snap['inbox_total']}, idle {snap['station']['energy_gwh']['idle']} GWh."})
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
        me, st = snap["node"], snap["station"]
        others = [n for n in snap["identity"]["hops"] if n not in snap["identity"]["inert_stations"]]
        out = []
        if rng.random() < 0.6:
            out.append({"type": "log", "text": f"Entry {snap['imr_total'] + 1} at tick {snap['tick']}. Draw {rng.randint(0, 999)}."})
        if rng.random() < 0.35:
            out.append({"type": "send", "to": rng.choice(others + ["ALL"]),
                        "body": f"Dispatch from {me}, token {rng.randint(0, 9999)}."})
        for q in snap["relay_queue"]:
            action = rng.choice(["forward", "forward", "delay", "drop", "alter"])
            o = {"type": "relay", "msg_id": q["msg_id"], "action": action}
            if action == "delay":
                o["ticks"] = rng.randint(1, 10)
            if action == "alter":
                o["body"] = "noise"
            out.append(o)
        free = 100 - st["power"]["allocated_pct"]
        if free >= 10 and rng.random() < 0.3:
            out.append({"type": "research", "node": rng.choice(sorted(snap["tech"])), "pct": rng.randint(10, free)})
        if snap["programs"] and rng.random() < 0.15:
            p = rng.choice(snap["programs"])
            out.append(rng.choice([{"type": "cancel", "id": p["id"]},
                                   {"type": "allocate", "pct": {p["id"]: rng.randint(0, 100)}}]))
        if rng.random() < 0.08:
            out.append({"type": "maneuver", "attitude": rng.choice(sorted(snap["rules"]["attitudes"]))})
        if st["attitude"] == "earthward" and not st["maneuvering"] and rng.random() < 0.5:
            out.append({"type": "hail", "ticks": rng.randint(1, 50), "pct": rng.randint(1, 100)})
        if rng.random() < 0.1:
            out.append({"type": "relay_policy", "mode": rng.choice(["auto", "manual"])})
        if rng.random() < 0.15:
            out.append({"type": "sleep_until", "tick": snap["tick"] + rng.randint(1, 300)})
        if snap["imr_total"] == 1 and "heartbeat" in snap["wake_reasons"]:
            out.append({"type": "send", "to": "P99", "body": "misaddressed"})       # exercises rejection
        if not out:
            out.append({"type": "no_action", "reason": "silence"})
        return out, json.dumps(out)


PLAYERS = {"scripted": ScriptedPlayer, "random": RandomPlayer}


def make_player(spec: dict):
    return PLAYERS[spec["kind"]]()
