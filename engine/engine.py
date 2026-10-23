"""The round loop (spec §3.3): deliver arrivals, wake and sample stations against the frozen state,
adjudicate every station's orders simultaneously, schedule transmissions, jump to the next tick with work.
"""
from __future__ import annotations

import json
from pathlib import Path

from .events import Event, hash_obj, sha256
from .log import EventLog
from .players import frame_hash, make_player, rules_hash
from .propagation import path_ticks, shortest_path
from .rules import player_spec
from .snapshot import build_snapshot, snapshot_hash
from .world import WorldState


class Engine:
    def __init__(self, log: EventLog, world: WorldState, snapshot_dir: Path | None = None,
                 fork_wake: str | None = None, draw: int = 0):
        self.log, self.world, self.snapshot_dir = log, world, snapshot_dir
        self.fork_wake, self.draw = fork_wake, draw   # the one wake whose sample is re-drawn (spec §16.2)
        self.players: dict = {}

    # -- plumbing ------------------------------------------------------------------------
    def emit(self, type: str, node: str | None, payload: dict, tick: int | None = None) -> Event:
        ev = self.log.append(type, self.world.now if tick is None else tick, node, payload)
        self.world.apply(ev)
        return ev

    def player(self, node: str):
        if node not in self.players:
            self.players[node] = make_player(player_spec(self.world.scenario, node))
        return self.players[node]

    def genesis(self, ruleset: dict, scenario: dict, ruleset_hash: str, scenario_hash: str) -> Event:
        return self.emit("genesis", None, {"ruleset": ruleset, "scenario": scenario, "ruleset_hash": ruleset_hash,
                                           "scenario_hash": scenario_hash}, tick=0)

    # -- the loop ----------------------------------------------------------------------------
    def run(self) -> str:
        """Resolve the current round, then jump tick to tick until the horizon. Returns branch status."""
        w = self.world
        tick = w.now
        while True:
            self.resolve_round(tick)
            tick = w.next_tick()
            if tick is None or tick > w.horizon:
                self.emit("end_state", None, self.end_state("horizon" if tick else "quiet"), tick=w.horizon)
                return "concluded"
            self.deliver(tick)

    def deliver(self, tick: int) -> None:
        due = sorted((m["arrival"], m["to"], mid) for mid, m in self.world.in_flight.items() if m["arrival"] == tick)
        for _, to, mid in due:
            self.emit("arrival", to, {"msg_id": mid}, tick=tick)

    def resolve_round(self, tick: int) -> None:
        w = self.world
        # Round state in the world is stale once the clock moves on; the first event at `tick` resets it.
        woke = set(w.round["woke"]) if w.round["tick"] == tick else set()
        ordered = set(w.round["orders"]) if w.round["tick"] == tick else set()
        wakers = sorted(woke | {n for n in w.nodes if w.wake_reasons(n, tick)})
        # 1. Everyone wakes and submits against the same frozen state. Orders events mutate nothing.
        for node in wakers:
            if node not in woke:
                wake = self.emit("wake", node, {"reasons": w.wake_reasons(node, tick)}, tick=tick)
            else:
                wake = next(e for e in reversed(self.log.events) if e.type == "wake" and e.node == node)
            if node not in ordered:
                self.sample(wake)
        # 2. Adjudicate all orders simultaneously (fixed order of application, no conflicts in M0).
        if not wakers:
            return
        for node in sorted(w.round["orders"]):
            orders = w.round["orders"][node]
            cap = w.ruleset["max_orders_per_wake"]
            for i, order in enumerate(orders):
                if i >= cap:
                    self.emit("order_rejected", node, {"order": order, "reason": f"exceeds max_orders_per_wake={cap}"})
                else:
                    self.adjudicate(node, order)

    def sample(self, wake: Event) -> None:
        node, w = wake.node, self.world
        snap = build_snapshot(w, node, wake.payload["reasons"])
        shash = snapshot_hash(snap)
        if self.snapshot_dir:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            (self.snapshot_dir / f"{shash}.json").write_text(json.dumps(snap, indent=1, sort_keys=True))
        salt = f":draw:{self.draw}" if (wake.id == self.fork_wake and self.draw) else ""
        seed = sha256(f"{w.scenario['seed']}:{shash}{salt}".encode())[:32]
        player = self.player(node)
        orders, raw = player.orders(snap, seed)
        self.emit("orders", node, {
            "wake": wake.id, "snapshot_hash": shash, "frame_hash": frame_hash(), "rules_hash": rules_hash(),
            "model_id": player.model_id, "seed": seed, "output_hash": sha256(raw.encode()), "output": raw,
            "orders": orders,
        })

    # -- adjudication ------------------------------------------------------------------------
    def adjudicate(self, node: str, order) -> None:
        w, rs = self.world, self.world.ruleset
        nodes = w.scenario["nodes"]
        if not isinstance(order, dict) or not isinstance(order.get("type"), str):
            self.emit("order_rejected", node, {"order": order, "reason": "malformed order"})
            return
        kind = order["type"]
        reject, outcome = None, {}
        if kind not in rs["actions"]:
            reject = f"action {kind!r} not in ruleset"
        elif kind == "send":
            to, body = order.get("to"), order.get("body")
            if not isinstance(body, str) or not body:
                reject = "send requires non-empty body"
            elif len(body) > rs["max_dispatch_chars"]:
                reject = f"body exceeds max_dispatch_chars={rs['max_dispatch_chars']}"
            elif to == "ALL" and not rs["broadcast"]:
                reject = "broadcast disabled by ruleset"
            elif to != "ALL" and to not in nodes:
                reject = f"unknown recipient {to!r}"
            elif to == node:
                reject = "cannot send to self"
        elif kind == "log":
            text = order.get("text")
            if not isinstance(text, str) or not text:
                reject = "log requires non-empty text"
            elif len(text) > rs["max_log_chars"]:
                reject = f"text exceeds max_log_chars={rs['max_log_chars']}"
        elif kind == "sleep_until":
            t = order.get("tick")
            if not isinstance(t, int) or isinstance(t, bool) or t <= w.now:
                reject = "sleep_until tick must be an integer after the current tick"
        if reject:
            self.emit("order_rejected", node, {"order": order, "reason": reject})
            return
        if kind == "send":
            recipients = [x for x in nodes if x != node] if order["to"] == "ALL" else [order["to"]]
            severed = {tuple(p) for p in w.severed}
            routes = {to: shortest_path(node, to, len(nodes), severed) for to in recipients}
            outcome = {"transmitted": [t for t, p in routes.items() if p], "unreachable": [t for t, p in routes.items() if not p]}
        self.emit("order_accepted", node, {"order": order, "outcome": outcome})
        if kind == "send":
            for to in outcome["transmitted"]:
                path = routes[to]
                msg_id = hash_obj({"from": node, "to": to, "n": w.nodes[node]["sent"], "tick": w.now})[:16]
                self.emit("transmit", node, {"msg_id": msg_id, "to": to, "body": order["body"], "path": path,
                                             "arrival_tick": w.now + path_ticks(path)})

    # -- end state (spec §10.4) --------------------------------------------------------------------
    def end_state(self, reason: str) -> dict:
        w = self.world
        return {"reason": reason, "tick": w.horizon, "world_digest": w.digest(),
                "stations": {n: {"imr": s["imr_count"], "sent": s["sent"], "received": s["received"],
                                 "wakes": s["wakes"], "rejected_orders": len(s["telemetry"])}
                             for n, s in w.nodes.items()},
                "in_flight": len(w.in_flight), "objective_scores": {}}
