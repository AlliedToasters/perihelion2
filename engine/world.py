"""World state: the omniscient simulation state. Rebuilt purely by folding events (spec §9.1).

Every state change happens in `apply(event)`. The live engine appends an event and then applies
it; replay applies the recorded events. Both paths run the same code, so they agree.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .events import Event, hash_obj
from .propagation import neighbors

DAY_MS = 86_400_000
PENDING_PRIORITY = {"arrival": 0, "wake": 1}  # arrivals land before a same-instant wake


def pending_key(item: dict) -> tuple:
    return (item["time"], PENDING_PRIORITY[item["kind"]], item["node"], item["key"])


def next_daily(ms: int, hhmmss: str) -> int:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    day_start = (ms // DAY_MS) * DAY_MS
    t = day_start + (h * 3600 + m * 60 + s) * 1000
    return t if t > ms else t + DAY_MS


class WorldState:
    def __init__(self):
        self.config: dict = {}
        self.now: int = 0
        self.nodes: dict[str, dict] = {}
        self.in_flight: dict[str, dict] = {}
        self.pending: list[dict] = []
        self.severed: list[list[str]] = []

    # -- serialization ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"config": self.config, "now": self.now, "nodes": self.nodes,
                "in_flight": self.in_flight, "pending": sorted(self.pending, key=pending_key),
                "severed": self.severed}

    def digest(self) -> str:
        return hash_obj(self.to_dict())

    # -- pending queue -----------------------------------------------------------------
    def schedule(self, kind: str, time: int, node: str, key: str) -> None:
        self.pending.append({"kind": kind, "time": time, "node": node, "key": key})

    def unschedule(self, kind: str, node: str, key: str) -> None:
        self.pending = [p for p in self.pending
                        if not (p["kind"] == kind and p["node"] == node and p["key"] == key)]

    def next_pending(self) -> dict | None:
        return min(self.pending, key=pending_key) if self.pending else None

    # -- event application -------------------------------------------------------------
    def apply(self, ev: Event) -> None:
        self.now = max(self.now, ev.sim_time)
        handler = getattr(self, f"_apply_{ev.type}", None)
        if handler:
            handler(ev)
        # Types with no handler (sample, fork, branch_status, rejected draws, ...) change nothing.

    def _apply_genesis(self, ev: Event) -> None:
        self.config = ev.payload["config"]
        n = len(self.config["nodes"])
        for name in self.config["nodes"]:
            self.nodes[name] = {
                "designation": name,
                "neighbors": neighbors(name, n),
                "imr": [], "inbox": [], "telemetry": [], "scheduled_wakes": [],
                "imr_count": 0, "sent": 0,
            }
            self.schedule("wake", next_daily(ev.sim_time, self.config["imr_wake_utc"]), name, "imr")

    def _apply_wake(self, ev: Event) -> None:
        reason = ev.payload["reason"]
        self.unschedule("wake", ev.node, reason)
        node = self.nodes[ev.node]
        node["scheduled_wakes"] = [w for w in node["scheduled_wakes"] if w["key"] != reason]
        if reason == "imr":
            self.schedule("wake", ev.sim_time + DAY_MS, ev.node, "imr")

    def _apply_action_adjudicated(self, ev: Event) -> None:
        node = self.nodes[ev.node]
        action = ev.payload["action"]
        kind = action["type"]
        if kind == "write_imr":
            node["imr_count"] += 1
            node["imr"].append({"n": node["imr_count"], "time": ev.sim_time, "text": action["text"]})
        elif kind == "schedule_wake":
            key = f"req:{action['reason']}@{action['time']}"
            self.schedule("wake", action["time"], ev.node, key)
            node["scheduled_wakes"].append({"time": action["time"], "key": key, "reason": action["reason"]})
        elif kind == "send_dispatch":
            node["sent"] += 1  # transmits follow as separate events
        # no_action: nothing

    def _apply_adjudication_rejected(self, ev: Event) -> None:
        self.nodes[ev.node]["telemetry"].append(
            {"time": ev.sim_time, "kind": "action_rejected", "action": ev.payload["action"]["type"],
             "reason": ev.payload["reason"]})

    def _apply_transmit(self, ev: Event) -> None:
        p = ev.payload
        self.in_flight[p["msg_id"]] = {"from": ev.node, "to": p["to"], "body": p["body"],
                                       "path": p["path"], "sent": ev.sim_time, "arrival": p["arrival_time"]}
        self.schedule("arrival", p["arrival_time"], p["to"], p["msg_id"])

    def _apply_arrival(self, ev: Event) -> None:
        msg_id = ev.payload["msg_id"]
        self.unschedule("arrival", ev.node, msg_id)
        msg = self.in_flight.pop(msg_id)
        self.nodes[ev.node]["inbox"].append({"msg_id": msg_id, "from": msg["from"], "body": msg["body"],
                                             "sent": msg["sent"], "arrived": ev.sim_time,
                                             "hops": len(msg["path"]) - 1})
        self.schedule("wake", ev.sim_time, ev.node, f"arrival:{msg_id}")


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()) * 1000
