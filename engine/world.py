"""World state: the omniscient simulation state. Rebuilt purely by folding events.

Every state change happens in `apply(event)`. The live engine appends an event and then applies it; replay
applies the recorded events. Both paths run the same code, so they agree.
"""
from __future__ import annotations

from .events import Event, hash_obj
from .propagation import neighbors


def fresh_round(tick: int) -> dict:
    return {"tick": tick, "woke": [], "orders": {}}


class WorldState:
    def __init__(self):
        self.ruleset: dict = {}
        self.scenario: dict = {}
        self.now: int = 0
        self.nodes: dict[str, dict] = {}
        self.in_flight: dict[str, dict] = {}
        self.round: dict = fresh_round(0)   # the tick being resolved: who has woken, whose orders are in
        self.severed: list[list[str]] = []

    # -- serialization -----------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"ruleset": self.ruleset, "scenario": self.scenario, "now": self.now, "nodes": self.nodes,
                "in_flight": self.in_flight, "round": self.round, "severed": self.severed}

    def digest(self) -> str:
        return hash_obj(self.to_dict())

    @property
    def horizon(self) -> int:
        return self.scenario["horizon_ticks"]

    # -- scheduling queries (used by the live engine only) --------------------------------
    def next_tick(self) -> int | None:
        """Earliest future tick at which anything happens: an arrival, a self-scheduled wake, a heartbeat."""
        cands = [m["arrival"] for m in self.in_flight.values()]
        for n in self.nodes.values():
            cands += [t for t in (n["sleep_until"], n["next_heartbeat"]) if t is not None]
        cands = [t for t in cands if t > self.now]
        return min(cands) if cands else None

    def wake_reasons(self, node: str, tick: int) -> list[str]:
        n = self.nodes[node]
        if node in self.scenario["inert"]:
            return []
        reasons = [f"arrival:{m['msg_id']}" for m in n["inbox"] if m["arrived"] == tick]
        if n["sleep_until"] == tick:
            reasons.append("sleep_until")
        if n["next_heartbeat"] is not None and n["next_heartbeat"] <= tick:
            reasons.append("genesis" if tick == 0 else "heartbeat")
        return reasons

    # -- event application -----------------------------------------------------------------
    def apply(self, ev: Event) -> None:
        if ev.tick > self.round["tick"]:
            self.round = fresh_round(ev.tick)
        self.now = max(self.now, ev.tick)
        handler = getattr(self, f"_apply_{ev.type}", None)
        if handler:
            handler(ev)
        # Types with no handler (fork, end_state) change nothing.

    def _apply_genesis(self, ev: Event) -> None:
        self.ruleset = ev.payload["ruleset"]
        self.scenario = ev.payload["scenario"]
        self.severed = [list(p) for p in self.scenario["severed"]]
        nodes = self.scenario["nodes"]
        for name in nodes:
            inert = name in self.scenario["inert"]
            self.nodes[name] = {
                "designation": name, "neighbors": neighbors(name, len(nodes)), "inert": inert,
                "imr": [], "imr_count": 0, "inbox": [], "telemetry": [],
                "sent": 0, "received": 0, "wakes": 0,
                "sleep_until": None, "next_heartbeat": None if inert else 0,
            }

    def _apply_arrival(self, ev: Event) -> None:
        msg = self.in_flight.pop(ev.payload["msg_id"])
        node = self.nodes[ev.node]
        node["inbox"].append({"msg_id": ev.payload["msg_id"], "from": msg["from"], "body": msg["body"],
                              "sent": msg["sent"], "arrived": ev.tick, "hops": len(msg["path"]) - 1})
        node["received"] += 1

    def _apply_wake(self, ev: Event) -> None:
        node = self.nodes[ev.node]
        self.round["woke"].append(ev.node)
        node["wakes"] += 1
        if node["sleep_until"] is not None and node["sleep_until"] <= ev.tick:
            node["sleep_until"] = None
        node["next_heartbeat"] = ev.tick + self.ruleset["heartbeat_ticks"]

    def _apply_orders(self, ev: Event) -> None:
        self.round["orders"][ev.node] = ev.payload["orders"]

    def _apply_order_accepted(self, ev: Event) -> None:
        node = self.nodes[ev.node]
        order = ev.payload["order"]
        kind = order["type"]
        if kind == "log":
            node["imr_count"] += 1
            node["imr"].append({"n": node["imr_count"], "tick": ev.tick, "text": order["text"]})
        elif kind == "sleep_until":
            node["sleep_until"] = order["tick"]
        # send: transmits follow as separate events. no_action: nothing.

    def _apply_order_rejected(self, ev: Event) -> None:
        order = ev.payload["order"]
        self.nodes[ev.node]["telemetry"].append(
            {"tick": ev.tick, "kind": "order_rejected",
             "order": order.get("type") if isinstance(order, dict) else None, "reason": ev.payload["reason"]})

    def _apply_transmit(self, ev: Event) -> None:
        p = ev.payload
        self.in_flight[p["msg_id"]] = {"from": ev.node, "to": p["to"], "body": p["body"], "path": p["path"],
                                       "sent": ev.tick, "arrival": p["arrival_tick"]}
        self.nodes[ev.node]["sent"] += 1
