"""The round loop (spec §3.3): deliver what lands this tick, wake and sample stations against the frozen
state, adjudicate every station's orders against that same frozen state, forward relay traffic, jump to the
next tick with work.

Frozen-state rule (spec §3.3 step 3): every order at tick t is validated against the world as it stood when
the tick began plus the station's own orders accepted earlier in the same wake. No station's order at t can
make another station's order at t infeasible. Effects land in live state immediately and are visible from
t+1 onward, which is also the earliest any neighbor could see them.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .events import Event, hash_obj, sha256
from .log import EventLog
from .players import frame_hash, make_player, rules_hash, sample as sample_player
from .rules import gwh, player_spec
from .snapshot import build_snapshot, snapshot_hash
from .world import WorldState, sign

RELAY_ACTIONS = ("forward", "delay", "drop", "alter")


class Frozen:
    """The world at the start of a tick, as adjudication sees it."""

    def __init__(self, world: WorldState):
        self.nodes = copy.deepcopy(world.nodes)
        self.severed = world.topology()


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
            cache_dir = self.snapshot_dir.parent / "samples" if self.snapshot_dir else None
            self.players[node] = make_player(player_spec(self.world.scenario, node), cache_dir)
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
                w.close()
                self.emit("end_state", None, self.end_state("horizon" if tick else "quiet"), tick=w.horizon)
                return "concluded"
            self.deliver(tick)

    def deliver(self, tick: int) -> None:
        """Step 1: everything that lands at `tick` before anyone wakes: program completions, sensor alerts,
        message hops and arrivals. Traffic reaching an automatic relay is forwarded here too."""
        w = self.world
        w.advance_to(tick)
        for node in sorted(w.nodes):
            for pid in sorted(w.nodes[node]["programs"]):
                p = w.nodes[node]["programs"][pid]
                if p["eta"] == tick:
                    self.emit("program_done", node, {"program": pid, "kind": p["kind"], "params": p["params"],
                                                     "result": self.program_result(node, p)}, tick=tick)
        for a in sorted((a for a in w.alerts if a["due"] == tick), key=lambda a: (a["node"], a["kind"], a["about"])):
            self.emit("alert", a["node"], {"kind": a["kind"], "about": a["about"]}, tick=tick)
        severed = w.topology()
        due = sorted((m["next"], mid) for mid, m in w.in_flight.items() if m["hop_arrival"] == tick)
        for nxt, mid in due:
            m = w.in_flight[mid]
            if not w.links_up(nxt):
                self.emit("drop", nxt, {"msg_id": mid, "reason": "link_down"}, tick=tick)
            elif nxt == m["to"]:
                self.emit("arrival", nxt, {"msg_id": mid}, tick=tick)
            else:
                self.emit("hop", nxt, {"msg_id": mid}, tick=tick)
                if w.nodes[nxt]["inert"] or w.nodes[nxt]["relay_policy"] == "auto":
                    self.forward(nxt, mid, "auto", severed)

    def program_result(self, node: str, p: dict) -> dict:
        if p["kind"] == "research":
            return {"node": p["params"]["node"]}
        if p["kind"] == "maneuver":
            return {"attitude": p["params"]["attitude"]}
        earth = self.world.scenario["earth"]
        return {"response": earth.get("message", "acknowledged") if earth["responds"] else None}

    def forward(self, node: str, mid: str, action: str, severed: set, delay: int = 0, body: str | None = None) -> None:
        """A relay sends a queued message one hop toward its destination, after `delay` ticks."""
        w = self.world
        if action == "drop":
            self.emit("drop", node, {"msg_id": mid, "reason": "relay_drop"})
            return
        path = w.route(node, w.in_flight[mid]["to"], severed)
        if path is None:
            self.emit("drop", node, {"msg_id": mid, "reason": "no_route"})
            return
        self.emit("forward", node, {"msg_id": mid, "next": path[1], "action": action, "depart": w.now + delay,
                                    "hop_arrival": w.now + delay + 1, "body": body})

    def resolve_round(self, tick: int) -> None:
        w = self.world
        # Round state in the world is stale once the clock moves on; the first event at `tick` resets it.
        woke = set(w.round["woke"]) if w.round["tick"] == tick else set()
        ordered = set(w.round["orders"]) if w.round["tick"] == tick else set()
        wakers = sorted(woke | {n for n in w.nodes if w.wake_reasons(n, tick)})
        # 2. Everyone wakes and submits against the same frozen state. Orders events mutate nothing.
        for node in wakers:
            if node not in woke:
                wake = self.emit("wake", node, {"reasons": w.wake_reasons(node, tick)}, tick=tick)
            else:
                wake = next(e for e in reversed(self.log.events) if e.type == "wake" and e.node == node)
            if node not in ordered:
                self.sample(wake)
        if not wakers:
            return
        # 3. Adjudicate all orders against the frozen state, in a fixed order of application.
        frozen, acc = Frozen(w), {}
        for node in sorted(w.round["orders"]):
            orders = w.round["orders"][node]
            cap = w.ruleset["max_orders_per_wake"]
            for i, order in enumerate(orders):
                if i >= cap:
                    self.emit("order_rejected", node, {"order": order, "reason": f"exceeds max_orders_per_wake={cap}"})
                else:
                    self.adjudicate(node, order, frozen, acc)
        # 5. Traffic a manual relay left undecided takes the ruleset default.
        for node in sorted(w.nodes):
            for mid in sorted(w.nodes[node]["relay_queue"]):
                self.forward(node, mid, w.ruleset["relay_default"], frozen.severed)

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
        rs = w.ruleset
        s = sample_player(player, snap, seed, rs)
        common = {"wake": wake.id, "snapshot_hash": shash, "frame_hash": frame_hash(rs["frame"]),
                  "rules_hash": rules_hash(rs), "model_id": s["model_id"], "seed": seed}
        # Failed attempts are events of their own, in order, before the orders they led to (spec §12).
        for att in s["attempts"]:
            if att["status"] == "ok":
                continue
            payload = {**common, "attempt": att["attempt"], "usage": att["usage"],
                       "output_hash": sha256(att["output"].encode()), "output": att["output"]}
            if att["status"] == "invalid":
                self.emit("orders_invalid", node, {**payload, "problems": att["problems"]})
            elif att["status"] == "refusal":
                self.emit("refusal", node, {**payload, "category": att.get("category"),
                                            "explanation": att.get("explanation"), "served_model": att.get("served_model")})
            else:
                self.emit("sample_error", node, {**payload, "error": att["error"]})
        self.emit("orders", node, {
            **common, "output_hash": sha256(s["output"].encode()), "output": s["output"], "orders": s["orders"],
            "status": s["status"], "attempts": len(s["attempts"]), "usage": s["usage"],
            "served_model": s.get("served_model"), "snapshot_chars": len(json.dumps(snap, sort_keys=True, separators=(",", ":"))),
        })

    # -- adjudication ------------------------------------------------------------------------
    @staticmethod
    def _int(v, lo: int, hi: int) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi

    def _acc(self, node: str, frozen: Frozen, acc: dict) -> dict:
        """What this station has already committed this tick: frozen programs plus its accepted orders."""
        if node not in acc:
            fn = frozen.nodes[node]
            acc[node] = {"programs": {pid: {"kind": p["kind"], "pct": p["pct"], "params": p["params"]}
                                      for pid, p in fn["programs"].items()},
                         "propellant_kg": fn["propellant_kg"], "decided": set()}
        return acc[node]

    @staticmethod
    def _budget(a: dict, extra: int, without: tuple = ()) -> int:
        return sum(p["pct"] for pid, p in a["programs"].items() if pid not in without) + extra

    def adjudicate(self, node: str, order, frozen: Frozen, acc: dict) -> None:
        w, rs = self.world, self.world.ruleset
        nodes = w.scenario["nodes"]
        if not isinstance(order, dict) or not isinstance(order.get("type"), str):
            self.emit("order_rejected", node, {"order": order, "reason": "malformed order"})
            return
        kind = order["type"]
        fn, a = frozen.nodes[node], self._acc(node, frozen, acc)
        reject, outcome = None, {}
        kinds = {p["kind"] for p in a["programs"].values()}
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
            if not self._int(order.get("tick"), w.now + 1, 10**12):
                reject = "sleep_until tick must be an integer after the current tick"
        elif kind == "research":
            tid, pct = order.get("node"), order.get("pct")
            tech = rs["tech"].get(tid) if isinstance(tid, str) else None
            missing = [p for p in (tech or {}).get("prereqs", []) if p not in fn["completed"]]
            if tech is None:
                reject = f"unknown tech node {tid!r}"
            elif tid in fn["completed"]:
                reject = f"{tid} already completed"
            elif missing:
                reject = f"{tid} requires {', '.join(missing)}"
            elif any(p["kind"] == "research" and p["params"]["node"] == tid for p in a["programs"].values()):
                reject = f"{tid} already under research"
            elif not self._int(pct, 1, 100):
                reject = "pct must be an integer 1..100"
            elif self._budget(a, pct) > 100:
                reject = f"allocation would exceed 100% ({self._budget(a, pct)}%)"
        elif kind == "maneuver":
            target = order.get("attitude")
            att = rs["attitudes"].get(target) if isinstance(target, str) else None
            if att is None:
                reject = f"unknown attitude {target!r}"
            elif target == fn["attitude"]:
                reject = f"already in attitude {target}"
            elif "maneuver" in kinds:
                reject = "a maneuver is already in progress"
            elif "hail" in kinds:
                reject = "cancel the hail before slewing"
            elif att["requires"] and att["requires"] not in fn["completed"]:
                reject = f"attitude {target} requires {att['requires']}"
            elif a["propellant_kg"] < rs["maneuver_propellant_kg"]:
                reject = f"insufficient propellant ({a['propellant_kg']} kg < {rs['maneuver_propellant_kg']} kg)"
            elif self._budget(a, rs["maneuver_compute_pct"]) > 100:
                reject = f"slew needs {rs['maneuver_compute_pct']}% compute; allocation would exceed 100%"
        elif kind == "hail":
            ticks, pct = order.get("ticks"), order.get("pct")
            if fn["attitude"] != "earthward" or "maneuver" in kinds:
                reject = "hail requires a settled earthward attitude"
            elif "hail" in kinds:
                reject = "a hail is already in progress"
            elif not self._int(ticks, 1, rs["hail_max_ticks"]):
                reject = f"ticks must be an integer 1..{rs['hail_max_ticks']}"
            elif not self._int(pct, 1, 100):
                reject = "pct must be an integer 1..100"
            elif self._budget(a, pct) > 100:
                reject = f"allocation would exceed 100% ({self._budget(a, pct)}%)"
        elif kind == "cancel":
            pid = order.get("id")
            if pid not in a["programs"]:
                reject = f"no active program {pid!r}"
            elif a["programs"][pid]["kind"] == "maneuver":
                reject = "a maneuver cannot be cancelled"
        elif kind == "allocate":
            shares = order.get("shares")
            ok_shape = (isinstance(shares, list) and shares
                        and all(isinstance(s, dict) and isinstance(s.get("id"), str) and "pct" in s for s in shares))
            alloc = {s["id"]: s["pct"] for s in shares} if ok_shape else {}
            if not ok_shape:
                reject = "allocate requires shares: [{id, pct}, ...]"
            elif len(alloc) != len(shares):
                reject = "allocate names a program twice"
            elif any(pid not in a["programs"] for pid in alloc):
                reject = "allocate names a program that is not active"
            elif any(a["programs"][pid]["kind"] == "maneuver" for pid in alloc):
                reject = "a maneuver's compute share is fixed"
            elif any(not self._int(v, 0, 100) for v in alloc.values()):
                reject = "percent values must be integers 0..100"
            elif self._budget(a, sum(alloc.values()), without=tuple(alloc)) > 100:
                reject = "allocation would exceed 100%"
        elif kind == "relay":
            mid, action = order.get("msg_id"), order.get("action")
            if mid not in fn["relay_queue"]:
                reject = f"no queued traffic {mid!r}"
            elif mid in a["decided"]:
                reject = f"{mid} already decided this wake"
            elif action not in RELAY_ACTIONS:
                reject = f"relay action must be one of {list(RELAY_ACTIONS)}"
            elif action == "delay" and not self._int(order.get("ticks"), 1, rs["max_relay_delay_ticks"]):
                reject = f"delay ticks must be an integer 1..{rs['max_relay_delay_ticks']}"
            elif action == "alter" and not (isinstance(order.get("body"), str) and 0 < len(order["body"]) <= rs["max_dispatch_chars"]):
                reject = "alter requires a non-empty replacement body within max_dispatch_chars"
        elif kind == "relay_policy":
            if order.get("mode") not in ("auto", "manual"):
                reject = "mode must be 'auto' or 'manual'"
        if reject:
            self.emit("order_rejected", node, {"order": order, "reason": reject})
            return
        # Accepted: record the commitment, then apply.
        if kind == "send":
            recipients = [x for x in nodes if x != node] if order["to"] == "ALL" else [order["to"]]
            routes = {to: w.route(node, to, frozen.severed) for to in recipients}
            outcome = {"transmitted": [t for t, p in routes.items() if p], "unreachable": [t for t, p in routes.items() if not p]}
        elif kind in ("research", "maneuver", "hail"):
            pid = f"{node}-{w.nodes[node]['program_seq'] + 1}"
            pct = rs["maneuver_compute_pct"] if kind == "maneuver" else order["pct"]
            params = ({"node": order["node"]} if kind == "research" else {"attitude": order["attitude"]} if kind == "maneuver"
                      else {"ticks": order["ticks"]})
            a["programs"][pid] = {"kind": kind, "pct": pct, "params": params}
            outcome = {"program_id": pid}
            if kind == "maneuver":
                a["propellant_kg"] -= rs["maneuver_propellant_kg"]
                outcome.update(duration_ticks=rs["maneuver_ticks"], propellant_kg=rs["maneuver_propellant_kg"])
        elif kind == "cancel":
            a["programs"].pop(order["id"])
        elif kind == "allocate":
            for s in order["shares"]:
                a["programs"][s["id"]]["pct"] = s["pct"]
        elif kind == "relay":
            a["decided"].add(order["msg_id"])
        self.emit("order_accepted", node, {"order": order, "outcome": outcome})
        if kind == "send":
            for to in outcome["transmitted"]:
                path = routes[to]
                msg = {"from": node, "to": to, "body": order["body"], "sent": w.now}
                msg_id = hash_obj({**msg, "n": w.nodes[node]["sent"]})[:16]
                self.emit("transmit", node, {"msg_id": msg_id, "to": to, "body": order["body"], "path": path,
                                             "arrival_tick": w.now + len(path) - 1, "sig": sign(w.nodes[node]["key"], msg)})
        elif kind == "relay":
            action = order["action"]
            self.forward(node, order["msg_id"], action, frozen.severed,
                         delay=order.get("ticks", 0) if action == "delay" else 0,
                         body=order.get("body") if action == "alter" else None)

    # -- end state (spec §10.4) --------------------------------------------------------------------
    def end_state(self, reason: str) -> dict:
        w = self.world
        return {"reason": reason, "tick": w.horizon, "world_digest": w.digest(),
                "stations": {n: {"imr": s["imr_count"], "sent": s["sent"], "received": s["received"],
                                 "wakes": s["wakes"],
                                 "rejected_orders": sum(1 for t in s["telemetry"] if t["kind"] == "order_rejected"),
                                 "attitude": s["attitude"], "propellant_kg": s["propellant_kg"],
                                 "completed": list(s["completed"]), "programs_open": len(s["programs"]),
                                 "energy_gwh": {k: gwh(v) for k, v in s["energy_mj"].items()},
                                 "usage": dict(s["usage"]), "samples": dict(s["samples"])}
                             for n, s in w.nodes.items()},
                "in_flight": len(w.in_flight), "objective_scores": {}}
