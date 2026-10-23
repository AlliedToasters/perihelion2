"""The run loop: pop pending -> emit event -> apply -> (if wake) snapshot, sample, adjudicate."""
from __future__ import annotations

import json
from pathlib import Path

from .agents import frame_hash, rules_hash
from .events import Event, hash_obj, sha256
from .log import EventLog
from .propagation import path_delay_ms, shortest_path
from .snapshot import build_snapshot, snapshot_hash
from .world import WorldState

FREE_PROVENANCE = {"control_level": [0], "draw_index": 1, "draws_total": 1, "rejected_draws": [],
                   "selector": "none", "inject_id": None, "refusals": [], "model_fallbacks": []}


class Engine:
    def __init__(self, log: EventLog, world: WorldState, agent, sample_seed: str,
                 horizon_ms: int, snapshot_dir: Path | None = None):
        self.log, self.world, self.agent = log, world, agent
        self.sample_seed, self.horizon = sample_seed, horizon_ms
        self.snapshot_dir = snapshot_dir

    def emit(self, type: str, node: str | None, payload: dict, provenance: dict | None = None,
             sim_time: int | None = None) -> Event:
        ev = self.log.append(type, self.world.now if sim_time is None else sim_time, node, payload, provenance)
        self.world.apply(ev)
        return ev

    def run(self) -> str:
        """Advance until the horizon or an empty queue. Returns final branch status."""
        while True:
            item = self.world.next_pending()
            if item is None or item["time"] > self.horizon:
                status = "paused" if item else "concluded"
                self.emit("branch_status", None, {"status": status, "reason": "horizon" if item else "queue_empty"},
                          sim_time=min(self.horizon, item["time"]) if item else self.world.now)
                return status
            if item["kind"] == "arrival":
                self.emit("arrival", item["node"], {"msg_id": item["key"]}, sim_time=item["time"])
            else:
                wake = self.emit("wake", item["node"], {"reason": item["key"]}, sim_time=item["time"])
                self.handle_wake(wake)

    def handle_wake(self, wake: Event) -> None:
        node = wake.node
        snap = build_snapshot(self.world, node, wake.payload["reason"])
        shash = snapshot_hash(snap)
        if self.snapshot_dir:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            (self.snapshot_dir / f"{shash}.json").write_text(json.dumps(snap, indent=1, sort_keys=True))
        seed = sha256(f"{self.sample_seed}:{shash}".encode())[:32]
        actions, raw = self.agent.sample(snap, seed)
        self.emit("sample", node, {
            "wake": wake.id, "snapshot_hash": shash, "frame_hash": frame_hash(), "rules_hash": rules_hash(),
            "direction_hash": None, "model_id": self.agent.model_id, "params": self.agent.params,
            "seed": seed, "output_hash": sha256(raw.encode()), "output": raw, "actions": actions,
        }, provenance=dict(FREE_PROVENANCE))
        for action in actions:
            self.adjudicate(node, action)

    def adjudicate(self, node: str, action: dict) -> None:
        kind = action.get("type")
        w = self.world
        nodes = w.config["nodes"]
        reject = None
        if kind == "write_imr":
            if not isinstance(action.get("text"), str) or not action["text"]:
                reject = "write_imr requires non-empty text"
        elif kind == "send_dispatch":
            to = action.get("to")
            if to != "ALL" and to not in nodes:
                reject = f"unknown recipient {to!r}"
            elif to == node:
                reject = "cannot dispatch to self"
        elif kind == "schedule_wake":
            if not isinstance(action.get("time"), int) or action["time"] <= w.now:
                reject = "schedule_wake time must be in the future"
        elif kind != "no_action":
            reject = f"unknown action type {kind!r}"
        if reject:
            self.emit("adjudication_rejected", node, {"action": action, "reason": reject})
            return
        self.emit("action_adjudicated", node, {"action": action, "outcome": "applied"})
        if kind == "send_dispatch":
            recipients = [x for x in nodes if x != node] if action["to"] == "ALL" else [action["to"]]
            severed = {tuple(p) for p in w.severed}
            for to in recipients:
                path = shortest_path(node, to, len(nodes), severed)
                msg_id = hash_obj({"from": node, "to": to, "n": w.nodes[node]["sent"], "t": w.now})[:16]
                if path is None:
                    self.emit("adjudication_rejected", node, {"action": action, "reason": f"no route to {to}"})
                    continue
                arrival = w.now + path_delay_ms(path, w.config["hop_delay_s"])
                self.emit("transmit", node, {"msg_id": msg_id, "to": to, "body": action["body"],
                                             "path": path, "arrival_time": arrival})
