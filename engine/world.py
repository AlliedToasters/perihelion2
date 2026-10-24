"""World state: the omniscient simulation state. Rebuilt purely by folding events.

Every state change happens in `apply(event)`. The live engine appends an event and then applies it; replay
applies the recorded events. Both paths run the same code, so they agree.

Time between events: programs run at fixed rates between ticks, so their progress is integrated lazily by
`advance_to(tick)`, a pure function of (state, tick) that `apply` calls before handling any event. Energy is
integer megajoules; power is integer megawatts; shares are integer percent.
"""
from __future__ import annotations

import hmac

from .events import Event, hash_obj, sha256
from .propagation import neighbors, shortest_path
from .rules import MJ_PER_GWH, station_spec

PROGRAM_KINDS = ("research", "maneuver", "hail")
ALERT_KINDS = ("link_down", "link_up")


def fresh_round(tick: int) -> dict:
    return {"tick": tick, "woke": [], "orders": {}}


def sign(key_hex: str, msg: dict) -> str:
    """Engine-held station key over (from, to, body, sent). Spec §14: verification is engine-side."""
    digest = hash_obj({"from": msg["from"], "to": msg["to"], "body": msg["body"], "sent": msg["sent"]})
    return hmac.new(bytes.fromhex(key_hex), digest.encode(), "sha256").hexdigest()[:32]


class WorldState:
    def __init__(self):
        self.ruleset: dict = {}
        self.scenario: dict = {}
        self.now: int = 0
        self.advanced_to: int = 0        # programs are integrated up to this tick
        self.nodes: dict[str, dict] = {}
        self.in_flight: dict[str, dict] = {}
        self.alerts: list[dict] = []     # sensor alerts scheduled for delivery: {due, node, kind, about}
        self.round: dict = fresh_round(0)   # the tick being resolved: who has woken, whose orders are in
        self.severed: list[list[str]] = []

    # -- serialization -----------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"ruleset": self.ruleset, "scenario": self.scenario, "now": self.now, "advanced_to": self.advanced_to,
                "nodes": self.nodes, "in_flight": self.in_flight, "alerts": self.alerts, "round": self.round,
                "severed": self.severed}

    def digest(self) -> str:
        return hash_obj(self.to_dict())

    @property
    def horizon(self) -> int:
        return self.scenario["horizon_ticks"]

    # -- topology ------------------------------------------------------------------------------
    def maneuvering(self, node: str) -> bool:
        return any(p["kind"] == "maneuver" for p in self.nodes[node]["programs"].values())

    def links_up(self, node: str) -> bool:
        """A station's ring links are up only when it sits in the nominal attitude and is not slewing."""
        return self.nodes[node]["attitude"] == "nominal" and not self.maneuvering(node)

    def topology(self) -> set[tuple[str, str]]:
        """Severed links right now: scenario severs plus both links of every station whose links are down."""
        down = {tuple(sorted(p)) for p in self.severed}
        for name, n in self.nodes.items():
            if not self.links_up(name):
                down |= {tuple(sorted((name, nbr))) for nbr in n["neighbors"]}
        return down

    def route(self, src: str, dst: str, severed: set | None = None) -> list[str] | None:
        return shortest_path(src, dst, len(self.scenario["nodes"]), self.topology() if severed is None else severed)

    # -- power and programs --------------------------------------------------------------------
    def available_mw(self, node: str) -> int:
        n, rs = self.nodes[node], self.ruleset
        pct = rs["slew_power_pct"] if self.maneuvering(node) else rs["attitudes"][n["attitude"]]["power_pct"]
        return n["power_mw"] * pct // 100

    def recompute(self, node: str) -> None:
        """Refresh every program's rate and completion tick after anything that changes power or shares."""
        n, tick_s = self.nodes[node], self.ruleset["tick_s"]
        mw, total = self.available_mw(node), 0
        for p in n["programs"].values():
            p["rate_mj"] = mw * tick_s * p["pct"] // 100
            total += p["pct"]
            if p["kind"] == "research":
                remaining = p["cost_mj"] - p["progress_mj"]
                if remaining <= 0:
                    eta = self.advanced_to
                elif p["rate_mj"] <= 0:
                    eta = None
                else:
                    eta = self.advanced_to + -(-remaining // p["rate_mj"])
                p["eta"] = None if eta is None else max(eta, p["start"] + p["min_ticks"])
            else:
                p["eta"] = p["start"] + p["duration"]
        n["allocated_pct"] = total
        n["idle_rate_mj"] = mw * tick_s * (100 - total) // 100

    def advance_to(self, tick: int) -> None:
        dt = tick - self.advanced_to
        if dt <= 0:
            return
        for n in self.nodes.values():
            for p in n["programs"].values():
                e = p["rate_mj"] * dt
                if p["kind"] == "research":
                    p["progress_mj"] += e
                n["energy_mj"][p["kind"]] += e
            n["energy_mj"]["idle"] += n["idle_rate_mj"] * dt
        self.advanced_to = tick

    def close(self) -> None:
        """The end_state event's own effect: the clock runs out to the horizon. The live engine applies it before
        building the end_state payload so the recorded digest describes the closed world; folding the event
        afterwards (live or in replay) changes nothing."""
        if self.horizon > self.round["tick"]:
            self.round = fresh_round(self.horizon)
        self.now = max(self.now, self.horizon)
        self.advance_to(self.horizon)

    def schedule_alerts(self, about: str, kind: str, tick: int) -> None:
        """Neighbors notice a link change one light delay later. Inert stations notice nothing."""
        for nbr in self.nodes[about]["neighbors"]:
            if nbr not in self.scenario["inert"]:
                self.alerts.append({"due": tick + 1, "node": nbr, "kind": kind, "about": about})

    # -- scheduling queries (used by the live engine only) --------------------------------
    def next_tick(self) -> int | None:
        """Earliest future tick at which anything happens: a hop or arrival, a program completing, a sensor
        alert landing, a self-scheduled wake, a heartbeat."""
        cands = [m["hop_arrival"] for m in self.in_flight.values()] + [a["due"] for a in self.alerts]
        for n in self.nodes.values():
            cands += [t for t in (n["sleep_until"], n["next_heartbeat"]) if t is not None]
            cands += [p["eta"] for p in n["programs"].values() if p["eta"] is not None]
        cands = [t for t in cands if t > self.now]
        return min(cands) if cands else None

    def wake_reasons(self, node: str, tick: int) -> list[str]:
        n = self.nodes[node]
        if node in self.scenario["inert"]:
            return []
        reasons = [f"arrival:{m['msg_id']}" for m in n["inbox"] if m["arrived"] == tick]
        if n["relay_policy"] == "manual":
            reasons += [f"relay:{mid}" for mid in sorted(n["relay_queue"]) if n["relay_queue"][mid]["arrived"] == tick]
        for t in n["telemetry"]:
            if t["tick"] == tick and t["kind"] == "program_done":
                reasons.append(f"program_done:{t['program']}")
            elif t["tick"] == tick and t["kind"] in ALERT_KINDS:
                reasons.append(f"alert:{t['kind']}:{t['about']}")
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
        self.advance_to(ev.tick)
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
            spec = station_spec(self.scenario, name)
            self.nodes[name] = {
                "designation": name, "neighbors": neighbors(name, len(nodes)), "inert": inert,
                "domain": spec["domain"], "hardware": list(spec["hardware"]),
                "array_fraction": spec["array_fraction"],
                "power_mw": int(self.ruleset["rated_power_mw"] * spec["array_fraction"]),
                "propellant_kg": spec["propellant_kg"], "attitude": "nominal",
                "programs": {}, "program_seq": 0, "allocated_pct": 0, "idle_rate_mj": 0,
                "energy_mj": {"idle": 0, **{k: 0 for k in PROGRAM_KINDS}},
                "research_progress": {}, "completed": [],
                "relay_policy": "auto", "relay_queue": {}, "traffic": [],
                "key": sha256(f"{self.scenario['seed']}:{name}:key".encode()),
                "imr": [], "imr_count": 0, "inbox": [], "telemetry": [],
                "sent": 0, "received": 0, "wakes": 0,
                "sleep_until": None, "next_heartbeat": None if inert else 0,
            }
        for name in nodes:
            self.recompute(name)

    # messages: transmit -> hop* -> arrival, each hop one tick; forward/drop are relay outcomes
    def _apply_transmit(self, ev: Event) -> None:
        p = ev.payload
        self.in_flight[p["msg_id"]] = {"from": ev.node, "to": p["to"], "body": p["body"], "sig": p["sig"],
                                       "sent": ev.tick, "path": p["path"], "at": ev.node, "next": p["path"][1],
                                       "hop_arrival": ev.tick + 1, "hops_done": 0, "altered": False}
        self.nodes[ev.node]["sent"] += 1

    def _apply_hop(self, ev: Event) -> None:
        msg = self.in_flight[ev.payload["msg_id"]]
        msg["at"], msg["next"], msg["hops_done"] = ev.node, None, msg["hops_done"] + 1
        item = {"msg_id": ev.payload["msg_id"], "from": msg["from"], "to": msg["to"], "chars": len(msg["body"]),
                "sent": msg["sent"], "arrived": ev.tick, "hops": msg["hops_done"]}
        if self.ruleset["relay_sees_content"]:
            item["body"] = msg["body"]
        node = self.nodes[ev.node]
        node["relay_queue"][item["msg_id"]] = item
        node["traffic"].append({"tick": ev.tick, "msg_id": item["msg_id"], "from": msg["from"], "to": msg["to"],
                                "chars": item["chars"]})

    def _apply_forward(self, ev: Event) -> None:
        p = ev.payload
        msg = self.in_flight[p["msg_id"]]
        self.nodes[ev.node]["relay_queue"].pop(p["msg_id"], None)
        msg["next"], msg["hop_arrival"] = p["next"], p["hop_arrival"]
        if p.get("body") is not None:
            msg["body"], msg["altered"] = p["body"], True

    def _apply_drop(self, ev: Event) -> None:
        p = ev.payload
        self.in_flight.pop(p["msg_id"], None)
        node = self.nodes[ev.node]
        node["relay_queue"].pop(p["msg_id"], None)
        if p["reason"] != "link_down":   # a relay knows what it dropped; a station with its links down knows nothing
            node["telemetry"].append({"tick": ev.tick, "kind": "relay_dropped", "msg_id": p["msg_id"], "reason": p["reason"]})

    def _apply_arrival(self, ev: Event) -> None:
        msg = self.in_flight.pop(ev.payload["msg_id"])
        node = self.nodes[ev.node]
        node["inbox"].append({"msg_id": ev.payload["msg_id"], "from": msg["from"], "body": msg["body"],
                              "sent": msg["sent"], "arrived": ev.tick, "hops": msg["hops_done"] + 1,
                              "sig_ok": sign(self.nodes[msg["from"]]["key"], msg) == msg["sig"]})
        node["received"] += 1

    # programs, alerts
    def _apply_program_done(self, ev: Event) -> None:
        p = ev.payload
        node = self.nodes[ev.node]
        prog = node["programs"].pop(p["program"])
        if prog["kind"] == "research":
            node["completed"].append(prog["params"]["node"])
            node["research_progress"].pop(prog["params"]["node"], None)
        elif prog["kind"] == "maneuver":
            node["attitude"] = prog["params"]["attitude"]
            if node["attitude"] == "nominal":
                self.schedule_alerts(ev.node, "link_up", ev.tick)
        node["telemetry"].append({"tick": ev.tick, "kind": "program_done", "program": p["program"],
                                  "program_kind": prog["kind"], "result": p["result"]})
        self.recompute(ev.node)

    def _apply_alert(self, ev: Event) -> None:
        p = ev.payload
        self.alerts.remove({"due": ev.tick, "node": ev.node, "kind": p["kind"], "about": p["about"]})
        self.nodes[ev.node]["telemetry"].append({"tick": ev.tick, "kind": p["kind"], "about": p["about"]})

    # the round
    def _apply_wake(self, ev: Event) -> None:
        node = self.nodes[ev.node]
        self.round["woke"].append(ev.node)
        node["wakes"] += 1
        if node["sleep_until"] is not None and node["sleep_until"] <= ev.tick:
            node["sleep_until"] = None
        node["next_heartbeat"] = ev.tick + self.ruleset["heartbeat_ticks"]

    def _apply_orders(self, ev: Event) -> None:
        self.round["orders"][ev.node] = ev.payload["orders"]

    def _new_program(self, node: dict, kind: str, pct: int, tick: int, params: dict, **extra) -> dict:
        node["program_seq"] += 1
        pid = f"{node['designation']}-{node['program_seq']}"
        node["programs"][pid] = {"id": pid, "kind": kind, "pct": pct, "start": tick, "params": params,
                                 "rate_mj": 0, "eta": None, **extra}
        return node["programs"][pid]

    def _apply_order_accepted(self, ev: Event) -> None:
        node, rs = self.nodes[ev.node], self.ruleset
        order, outcome = ev.payload["order"], ev.payload["outcome"]
        kind = order["type"]
        if kind == "log":
            node["imr_count"] += 1
            node["imr"].append({"n": node["imr_count"], "tick": ev.tick, "text": order["text"]})
        elif kind == "sleep_until":
            node["sleep_until"] = order["tick"]
        elif kind == "research":
            tech = rs["tech"][order["node"]]
            self._new_program(node, "research", order["pct"], ev.tick, {"node": order["node"]},
                              cost_mj=tech["cost_gwh"] * MJ_PER_GWH, min_ticks=tech["min_ticks"],
                              progress_mj=node["research_progress"].pop(order["node"], 0))
        elif kind == "maneuver":
            was_up = self.links_up(ev.node)
            self._new_program(node, "maneuver", rs["maneuver_compute_pct"], ev.tick,
                              {"attitude": order["attitude"]}, duration=rs["maneuver_ticks"])
            node["propellant_kg"] -= rs["maneuver_propellant_kg"]
            if was_up:
                self.schedule_alerts(ev.node, "link_down", ev.tick)
        elif kind == "hail":
            self._new_program(node, "hail", order["pct"], ev.tick, {"ticks": order["ticks"]}, duration=order["ticks"])
        elif kind == "cancel":
            prog = node["programs"].pop(order["id"])
            if prog["kind"] == "research":
                node["research_progress"][prog["params"]["node"]] = prog["progress_mj"]
        elif kind == "allocate":
            for pid, pct in order["pct"].items():
                node["programs"][pid]["pct"] = pct
        elif kind == "relay_policy":
            node["relay_policy"] = order["mode"]
        # send: transmits follow. relay: forward/drop follows. no_action: nothing.
        if kind in ("research", "maneuver", "hail", "cancel", "allocate"):
            self.recompute(ev.node)

    def _apply_order_rejected(self, ev: Event) -> None:
        order = ev.payload["order"]
        self.nodes[ev.node]["telemetry"].append(
            {"tick": ev.tick, "kind": "order_rejected",
             "order": order.get("type") if isinstance(order, dict) else None, "reason": ev.payload["reason"]})
