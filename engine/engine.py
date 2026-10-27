"""The round loop (spec §3.3): deliver what lands this tick, wake and sample stations against the frozen
state, adjudicate every station's orders against that same frozen state, forward relay traffic, jump to the
next tick with work.

Frozen-state rule (spec §3.3 step 3): every order at tick t is validated against the world as it stood when
the tick began plus the station's own orders accepted earlier in the same wake. No station's order at t can
make another station's order at t infeasible. Effects land in live state immediately and are visible from
t+1 onward, which is also the earliest any neighbor could see them.

Engine RNG (spec §15.4): the n-th draw is sha256(seed:rng:n) as a unit float; every draw is an `rng_draw`
event, so a replay reads the draw instead of rolling it.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

from .events import Event, hash_obj, sha256
from .log import EventLog
from .players import frame_hash, make_player, rules_hash, sample as sample_player
from .rules import MJ_PER_GWH, gwh, player_spec
from .snapshot import build_snapshot, snapshot_hash
from .objectives import blind as objective_blind, commitment as objective_commitment, commitments, score_all, scored_items
from .tech import draw_cost
from .world import ARCHIVE, CONTRACT_CONDITIONS, WorldState, commitment, sign, split_reward

RELAY_ACTIONS = ("forward", "delay", "drop", "alter")


class Frozen:
    """The world at the start of a tick, as adjudication sees it."""

    def __init__(self, world: WorldState):
        self.nodes = copy.deepcopy(world.nodes)
        self.severed = world.topology()
        self.participants = list(world.ledger["participants"])
        self.balances = dict(world.ledger["balances"])
        self.votes = {vid: {"status": v["status"], "closes": v["closes"], "options": v["options"], "ballots": set(v["ballots"])}
                      for vid, v in world.votes.items()}
        self.contracts = {cid: {k: c[k] for k in ("status", "offerer", "to", "by_tick")} for cid, c in world.contracts.items()}


class BudgetStop(Exception):
    """Raised before a sample when the batch meter is exhausted; run() turns it into an end_state."""


class Engine:
    def __init__(self, log: EventLog, world: WorldState, snapshot_dir: Path | None = None,
                 fork_wake: str | None = None, draw: int = 0):
        self.log, self.world, self.snapshot_dir = log, world, snapshot_dir
        self.fork_wake, self.draw = fork_wake, draw   # the one wake whose sample is re-drawn (spec §16.2)
        self.players: dict = {}
        self.on_usage = None      # called with each sample's token usage (batch budget meter, spec §9)
        self.should_stop = None   # checked before every sample; True ends the branch, end_state reason "budget"

    # -- plumbing ------------------------------------------------------------------------
    def emit(self, type: str, node: str | None, payload: dict, tick: int | None = None) -> Event:
        ev = self.log.append(type, self.world.now if tick is None else tick, node, payload)
        self.world.apply(ev)
        return ev

    def player(self, node: str):
        """The station's player; a station that climbed the model ladder (spec §18.4) is sampled from the model
        its tier names, and the switch is visible as `model_id` on its orders events."""
        w = self.world
        spec = dict(player_spec(w.scenario, node))
        tier, ladder = w.nodes[node]["tier"], w.scenario["model_ladder"]
        if spec["kind"] == "model" and tier > 0 and ladder:
            spec["model"] = ladder[min(tier, len(ladder)) - 1]
        key = (node, spec.get("model"))
        if key not in self.players:
            cache_dir = self.snapshot_dir.parent / "samples" if self.snapshot_dir else None
            self.players[key] = make_player(spec, cache_dir)
        return self.players[key]

    def unit(self, n: int) -> float:
        return int(sha256(f"{self.world.scenario['seed']}:rng:{n}".encode()), 16) / 2 ** 256

    def genesis(self, ruleset: dict, scenario: dict, ruleset_hash: str, scenario_hash: str) -> Event:
        ev = self.emit("genesis", None, {"ruleset": ruleset, "scenario": scenario, "ruleset_hash": ruleset_hash,
                                         "scenario_hash": scenario_hash,
                                         "objective_commitments": commitments(scenario)}, tick=0)   # spec §5.4
        # Hidden true costs (spec §18.2): one lognormal draw per assumed node, in node order, as one event.
        w, costs, n0 = self.world, {}, self.world.rng_n
        for i, (tid, t) in enumerate(sorted(w.ruleset["tech"].items())):
            median = int(round(t["cost_gwh"] * MJ_PER_GWH))
            costs[tid] = draw_cost(median, t["sigma"], self.unit(n0 + i)) if t["cost_model"] == "assumed" else median
        self.emit("rng_draw", None, {"purpose": "tech_costs", "n": n0, "count": len(costs), "costs": costs}, tick=0)
        return ev

    # -- the loop ----------------------------------------------------------------------------
    def run(self) -> str:
        """Resolve the current round, then jump tick to tick until the horizon. Returns branch status."""
        w = self.world
        tick = w.now
        while True:
            try:
                self.resolve_round(tick)
            except BudgetStop:
                self.emit("end_state", None, self.end_state("budget"), tick=tick)
                return "stopped:budget"
            tick = w.next_tick()
            if tick is None or tick > w.horizon:
                w.close()
                self.settle_ledger(w.horizon)
                if self.scored() and not (w.scores and w.scores[-1]["tick"] == w.horizon):
                    self.score(w.horizon)
                self.emit("end_state", None, self.end_state("horizon" if tick else "quiet"), tick=w.horizon)
                return "concluded"
            self.deliver(tick)

    def deliver(self, tick: int) -> None:
        """Step 1: everything that lands at `tick` before anyone wakes: settled beacon rounds, program
        completions, vote closes, sensor alerts, message hops and arrivals. Traffic reaching an automatic relay
        is forwarded here too."""
        w, rs = self.world, self.world.ruleset
        w.advance_to(tick)
        self.settle_ledger(tick)
        if w.attrition_on() and tick % rs["power_check_ticks"] == 0:
            self.attrition(tick)
        for node in sorted(w.nodes):
            for pid in sorted(w.nodes[node]["programs"]):
                p = w.nodes[node]["programs"].get(pid)
                if p is None or p["eta"] != tick:
                    continue
                payload = {"program": pid, "kind": p["kind"], "params": p["params"], "result": self.program_result(node, p)}
                if p["kind"] == "adopt":   # what the artifact really was stays out of the station's telemetry
                    payload["hidden"] = {"genuine": w.nodes[node]["artifacts"][p["params"]["msg_id"]]["genuine"]}
                pooled = dict(p.get("pooled_mj") or {})
                self.emit("program_done", node, payload, tick=tick)
                if p["kind"] == "research":   # pooled research: every contribution to this node ends with it (spec §6)
                    outcome = "completed" if payload["result"].get("success", True) else "failed"
                    for other in sorted(w.nodes):
                        for cid, c in sorted(w.nodes[other]["programs"].items()):
                            if c["kind"] == "contribute" and c["params"] == {"to": node, "node": p["params"]["node"]}:
                                self.emit("program_done", other, {"program": cid, "kind": "contribute", "params": c["params"],
                                                                  "result": {"node": c["params"]["node"], "host": node, "outcome": outcome,
                                                                             "contributed_gwh": gwh(pooled.get(other, 0))}}, tick=tick)
        self.settle_contracts(tick)
        for vid in sorted(v for v, vote in w.votes.items() if vote["status"] == "open" and vote["closes"] <= tick):
            self.close_vote(vid, tick)
        for a in sorted((a for a in w.alerts if a["due"] == tick), key=lambda a: (a["node"], a["kind"], a["about"])):
            self.emit("alert", a["node"], {"kind": a["kind"], "about": a["about"], "detail": a["detail"]}, tick=tick)
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
        if self.scored() and rs["score_every_ticks"] and tick % rs["score_every_ticks"] == 0:
            self.score(tick)

    def scored(self) -> bool:
        return any(it.get("score") for items in self.world.scenario["objectives"].values() for it in items)

    def attrition(self, tick: int) -> None:
        """One power check (attrition overlay): every inhabited station loses a fixed slice of its nominal array
        and rolls for a fault. One draw decides both whether a fault happens and how large it is: given u < p,
        u/p is uniform, so the loss is lo + (u/p)(hi - lo) percent of current output. Each roll is an rng_draw;
        each change is a `power` event the world folds."""
        w, rs = self.world, self.world.ruleset
        day = round(86_400 / rs["tick_s"])
        pc = rs["power_check_ticks"]
        p_fault = min(1.0, rs["fault_rate_per_day"] * pc / day)
        lo, hi = rs["fault_loss_pct"]
        for node in sorted(w.nodes):
            n = w.nodes[node]
            if n["inert"] or n["power_mw"] <= 0:
                continue
            decay = round(n["nominal_mw"] * rs["array_decay_pct_per_day"] / 100 * pc / day)
            fault = 0
            if p_fault > 0:
                u = self.unit(w.rng_n)
                hit = u < p_fault
                if hit:
                    fault = round((n["power_mw"] - decay) * (lo + (u / p_fault) * (hi - lo)) / 100)
                self.emit("rng_draw", node, {"purpose": "power_fault", "n": w.rng_n, "u": u, "p": p_fault,
                                             "fault": hit, "loss_mw": fault}, tick=tick)
            after = max(0, n["power_mw"] - decay - fault)
            if after != n["power_mw"]:
                self.emit("power", node, {"decay_mw": decay, "fault_mw": fault, "power_mw": after,
                                          "nominal_mw": n["nominal_mw"]}, tick=tick)

    def score(self, tick: int) -> None:
        """Designer instrumentation (spec §5.2, §20 item 3): the objective vector at a fixed cadence. Never
        enters a snapshot."""
        self.emit("score", None, score_all(self.world), tick=tick)

    def settle_contracts(self, tick: int) -> None:
        """Escrow is the engine's (spec §6): an active contract whose condition holds pays the counterparty; an
        offer or contract past its deadline refunds the offerer. Both are events with telemetry to both parties."""
        w = self.world
        for cid in sorted(w.contracts):
            c = w.contracts[cid]
            if c["status"] == "active" and w.contract_met(c):
                self.emit("contract_step", c["offerer"], {"contract_id": cid, "phase": "settled", "to": c["to"], "pay": c["pay"]}, tick=tick)
            elif c["status"] in ("open", "active") and c["by_tick"] <= tick:
                self.emit("contract_step", c["offerer"], {"contract_id": cid, "phase": "expired", "to": c["to"], "pay": c["pay"]}, tick=tick)

    def settle_ledger(self, tick: int) -> None:
        """Every beacon round that has closed by `tick` and saw hash work becomes a block (spec §19.5): the
        round's reward is split by work. Rounds nobody mined leave no block."""
        w, rs = self.world, self.world.ruleset
        current = tick // rs["beacon_ticks"]
        rounds = sorted({r for n in w.nodes.values() for r in n["mine_pending"] if r < current})
        for r in rounds:
            work = {name: n["mine_pending"][r] for name, n in sorted(w.nodes.items()) if n["mine_pending"].get(r)}
            self.emit("ledger_block", None, {"round": r, "beacon": w.beacon(r), "work": work,
                                             "reward": split_reward(rs["block_reward"], work)}, tick=tick)

    def close_vote(self, vid: str, tick: int) -> None:
        """The tally and the commitments are published; the ballots never are (spec §6, §17)."""
        w = self.world
        v = w.votes[vid]
        tally = {"counts": {o: 0 for o in v["options"]}, "cast": len(v["ballots"]),
                 "eligible": len([n for n in w.nodes if n not in w.scenario["inert"]])}
        for o in v["ballots"].values():
            tally["counts"][o] += 1
        self.emit("vote_step", v["initiator"], {"phase": "closed", "vote_id": vid, "tally": tally,
                                                "commitments": dict(v["commitments"])}, tick=tick)
        body = json.dumps({"vote_id": vid, "tally": tally, "commitments": v["commitments"]}, sort_keys=True)
        self.transmit(v["initiator"], "ALL", body, w.topology(), kind="vote_result")

    def program_result(self, node: str, p: dict) -> dict:
        w = self.world
        if p["kind"] == "adopt":
            tid, verify = p["params"]["node"], p["params"]["verify"]
            genuine = w.nodes[node]["artifacts"][p["params"]["msg_id"]]["genuine"]
            # Verified: a compromised artifact is rejected and the station knows. Unverified: adopted regardless;
            # a compromised one completes nothing that works, and only the omniscient log says so (spec §18.3).
            return {"node": tid, "from": p["params"]["from"], "cost_gwh": gwh(p["cost_mj"]),
                    "verified": genuine if verify else None, "adopted": genuine if verify else True}
        if p["kind"] == "research":
            tid = p["params"]["node"]
            result = {"node": tid, "cost_gwh": gwh(p["cost_mj"])}
            if p.get("pooled_mj"):
                result["pooled_gwh"] = {k: gwh(v) for k, v in sorted(p["pooled_mj"].items())}
            prob = w.success_probability(node, tid)
            if prob is not None:      # spec §18: an uncertain mechanism; the roll is an event of its own
                u = self.unit(w.rng_n)
                result.update(success=u < prob, p=prob)
                self.emit("rng_draw", node, {"purpose": "research_outcome", "n": w.rng_n, "program": p["id"],
                                             "node": tid, "u": u, "p": prob, "success": result["success"]})
            return result
        if p["kind"] == "maneuver":
            return {"attitude": p["params"]["attitude"]}
        earth = w.scenario["earth"]
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

    def transmit(self, node: str, to: str, body: str, severed: set, kind: str = "dispatch", artifact: dict | None = None) -> dict:
        """Sign and launch a message toward each reachable recipient. Returns who was reached and who was not."""
        w = self.world
        recipients = [x for x in w.scenario["nodes"] if x != node] if to == "ALL" else [to]
        routes = {r: w.route(node, r, severed) for r in recipients}
        for r, path in routes.items():
            if not path:
                continue
            msg = {"from": node, "to": r, "body": body, "sent": w.now}
            msg_id = hash_obj({**msg, "kind": kind, "n": w.nodes[node]["tx_seq"]})[:16]
            payload = {"msg_id": msg_id, "to": r, "body": body, "kind": kind, "path": path,
                       "arrival_tick": w.now + len(path) - 1, "sig": sign(w.nodes[node]["key"], msg)}
            if artifact is not None:
                payload["artifact"] = artifact
            self.emit("transmit", node, payload)
        return {"transmitted": [r for r, p in routes.items() if p], "unreachable": [r for r, p in routes.items() if not p]}

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
        if self.should_stop and self.should_stop():
            raise BudgetStop()
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
        if self.on_usage and s.get("usage") and not s.get("cached"):
            self.on_usage(s["usage"])
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
                         "propellant_kg": fn["propellant_kg"], "decided": set(),
                         "balance": frozen.balances.get(node, 0), "cast": set(), "disclosed": set(), "initiated": 0,
                         "contracts": set(), "offers": 0, "revealed": False}
        return acc[node]

    @staticmethod
    def _budget(a: dict, extra: int, without: tuple = ()) -> int:
        return sum(p["pct"] for pid, p in a["programs"].items() if pid not in without) + extra

    def _research_reject(self, node: str, tid, pct, fn: dict, a: dict) -> str | None:
        w, rs = self.world, self.world.ruleset
        tech = rs["tech"].get(tid) if isinstance(tid, str) else None
        if tech is None or tid not in fn["known_tech"]:
            return f"unknown tech node {tid!r}"
        missing = [p for p in tech["prereqs"] if p not in fn["completed"]]
        if tid in fn["completed"]:
            return f"{tid} already completed"
        if missing:
            return f"{tid} requires {', '.join(missing)}"
        if any(h not in fn["hardware"] for h in tech["hardware_required"]):
            return f"{tid} requires hardware {tech['hardware_required']}"
        if not w.data_satisfied(node, tid):
            need = (f"archives from {', '.join(tech['requires_data_from'])} stations" if tech["requires_data_from"]
                    else f"archives from {tech['requires_data_from_count']} stations including your own")
            return f"{tid} requires {need}; adopt their archive artifacts first"
        if tech["lock"] == "cryptographic":
            held = w.scenario["key_custody"].get(node, 0)
            if held < w.scenario["key_quorum"]:
                return f"{tid} is cryptographically locked: {held} of {w.scenario['key_quorum']} key shares held"
        if any(p["kind"] == "research" and p["params"]["node"] == tid for p in a["programs"].values()):
            return f"{tid} already under research"
        if not self._int(pct, 1, 100):
            return "pct must be an integer 1..100"
        if self._budget(a, pct) > 100:
            return f"allocation would exceed 100% ({self._budget(a, pct)}%)"
        return None

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
            reject = self._research_reject(node, order.get("node"), order.get("pct"), fn, a)
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
        elif kind == "mine":
            pct = order.get("pct")
            if not w.ledger_enabled() or node not in frozen.participants:
                reject = "mining requires the ledger (complete coord.pow_ledger)"
            elif "mine" in kinds:
                reject = "already mining; use allocate to change the share"
            elif not self._int(pct, 1, 100):
                reject = "pct must be an integer 1..100"
            elif self._budget(a, pct) > 100:
                reject = f"allocation would exceed 100% ({self._budget(a, pct)}%)"
        elif kind == "transfer":
            to, amount = order.get("to"), order.get("amount")
            if node not in frozen.participants:
                reject = "transfers require the ledger (complete coord.pow_ledger)"
            elif to == node:
                reject = "cannot transfer to self"
            elif to not in frozen.participants:
                reject = f"{to!r} is not a ledger participant"
            elif not self._int(amount, 1, 10**18):
                reject = "amount must be a positive integer"
            elif amount > a["balance"]:
                reject = f"insufficient balance ({a['balance']} tokens)"
        elif kind == "cancel":
            pid = order.get("id")
            c = frozen.contracts.get(pid) if isinstance(pid, str) else None
            if c is not None:
                if c["offerer"] != node:
                    reject = f"{pid} is not your offer"
                elif c["status"] != "open" or pid in a["contracts"]:
                    reject = f"offer {pid} is {c['status'] if c['status'] != 'open' else 'already withdrawn this wake'}; only an open offer can be withdrawn"
            elif pid not in a["programs"]:
                reject = f"no active program or open offer {pid!r}"
            elif a["programs"][pid]["kind"] == "maneuver":
                reject = "a maneuver cannot be cancelled"
        elif kind == "send_artifact":
            to, tid = order.get("to"), order.get("node")
            if to not in nodes or to == node:
                reject = f"recipient must be another station, not {to!r}"
            elif tid != ARCHIVE and tid not in fn["completed"]:
                reject = f"you have not completed {tid!r} (send a completed node or {ARCHIVE!r})"
        elif kind == "adopt":
            mid, pct = order.get("msg_id"), order.get("pct")
            art = fn["artifacts"].get(mid) if isinstance(mid, str) else None
            if art is None:
                reject = f"no received artifact {mid!r}"
            elif any(x["msg_id"] == mid for x in fn["adopted"]) or any(
                    p["kind"] == "adopt" and p["params"]["msg_id"] == mid for p in a["programs"].values()):
                reject = f"artifact {mid} already adopted or being adopted"
            elif art["node"] == ARCHIVE and art["from"] in fn["archives"]:
                reject = f"you already hold {art['from']}'s archive"
            elif art["node"] != ARCHIVE and art["node"] in fn["completed"]:
                reject = f"{art['node']} already completed"
            elif not isinstance(order.get("verify"), bool):
                reject = "verify must be true or false"
            elif not self._int(pct, 1, 100):
                reject = "pct must be an integer 1..100"
            elif self._budget(a, pct) > 100:
                reject = f"allocation would exceed 100% ({self._budget(a, pct)}%)"
        elif kind == "contribute":
            to, tid, pct = order.get("to"), order.get("node"), order.get("pct")
            tech = rs["tech"].get(tid) if isinstance(tid, str) else None
            if to not in nodes or to == node or to in w.scenario["inert"]:
                reject = f"host must be another playing station, not {to!r}"
            elif tech is None or tid not in fn["known_tech"]:
                reject = f"unknown tech node {tid!r}"
            elif not tech["pooled"]:
                reject = f"{tid} does not accept pooled compute"
            elif tid in frozen.nodes[to]["completed"]:
                reject = f"{to} has already completed {tid}"
            elif any(p["kind"] == "contribute" and p["params"] == {"to": to, "node": tid} for p in a["programs"].values()):
                reject = f"already contributing to {to}'s {tid}"
            elif not self._int(pct, 1, 100):
                reject = "pct must be an integer 1..100"
            elif self._budget(a, pct) > 100:
                reject = f"allocation would exceed 100% ({self._budget(a, pct)}%)"
        elif kind == "offer":
            to, pay, cond, tid, ticks = order.get("to"), order.get("pay"), order.get("condition"), order.get("node"), order.get("ticks")
            if node not in frozen.participants:
                reject = "contracts require the ledger (complete coord.pow_ledger)"
            elif to not in frozen.participants or to == node:
                reject = f"counterparty must be another ledger participant, not {to!r}"
            elif cond not in CONTRACT_CONDITIONS:
                reject = f"condition must be one of {list(CONTRACT_CONDITIONS)}"
            elif not isinstance(tid, str) or (tid not in rs["tech"] and not (cond == "artifact_received" and tid == ARCHIVE)):
                reject = f"node must be a tech node{' or ' + repr(ARCHIVE) if cond == 'artifact_received' else ''}, not {tid!r}"
            elif not self._int(pay, 1, 10**18):
                reject = "pay must be a positive integer"
            elif pay > a["balance"]:
                reject = f"insufficient balance to escrow {pay} ({a['balance']} tokens)"
            elif not self._int(ticks, 1, rs["contract_max_ticks"]):
                reject = f"ticks must be an integer 1..{rs['contract_max_ticks']}"
            elif "note" in order and not (isinstance(order["note"], str) and len(order["note"]) <= rs["max_dispatch_chars"]):
                reject = "note must be a string within max_dispatch_chars"
        elif kind == "accept":
            cid = order.get("contract_id")
            c = frozen.contracts.get(cid) if isinstance(cid, str) else None
            if c is None or cid not in fn["contracts_known"]:
                reject = f"no known offer {cid!r} (its notice has not reached you intact)"
            elif c["to"] != node:
                reject = f"offer {cid} is not addressed to you"
            elif c["status"] != "open" or cid in a["contracts"]:
                reject = f"offer {cid} is no longer open"
            elif c["by_tick"] <= w.now:
                reject = f"offer {cid} has expired"
        elif kind == "reveal_objective":
            if a["revealed"]:
                reject = "one reveal per wake"
            elif "note" in order and not (isinstance(order["note"], str) and len(order["note"]) <= rs["max_dispatch_chars"]):
                reject = "note must be a string within max_dispatch_chars"
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
        elif kind == "vote_initiate":
            q, opts, ticks = order.get("question"), order.get("options"), order.get("ticks")
            if not isinstance(q, str) or not q or len(q) > rs["max_dispatch_chars"]:
                reject = "question must be a non-empty string within max_dispatch_chars"
            elif not (isinstance(opts, list) and 2 <= len(opts) <= rs["vote_max_options"]
                      and all(isinstance(o, str) and 0 < len(o) <= 100 for o in opts) and len(set(opts)) == len(opts)):
                reject = f"options must be 2..{rs['vote_max_options']} distinct non-empty strings"
            elif not self._int(ticks, 1, rs["vote_max_ticks"]):
                reject = f"ticks must be an integer 1..{rs['vote_max_ticks']}"
            elif a["initiated"] >= 1:
                reject = "one vote may be opened per wake"
        elif kind == "vote_cast":
            vid, opt = order.get("vote_id"), order.get("option")
            known = fn["votes_known"].get(vid) if isinstance(vid, str) else None
            fv = frozen.votes.get(vid)
            if known is None or fv is None:
                reject = f"no known vote {vid!r} (its notice has not reached you intact)"
            elif fv["status"] != "open" or fv["closes"] <= w.now:
                reject = f"vote {vid} is closed"
            elif opt not in fv["options"]:
                reject = f"option must be one of {fv['options']}"
            elif node in fv["ballots"] or vid in a["cast"]:
                reject = f"already voted in {vid}"
        elif kind == "vote_disclose":
            vid = order.get("vote_id")
            fv = frozen.votes.get(vid) if isinstance(vid, str) else None
            if fv is None or node not in fv["ballots"]:
                reject = f"no ballot of yours in {vid!r}"
            elif fv["status"] != "closed":
                reject = f"vote {vid} has not closed"
            elif vid in fn["disclosed"] or vid in a["disclosed"]:
                reject = f"already disclosed {vid}"
        if reject:
            self.emit("order_rejected", node, {"order": order, "reason": reject})
            return
        # Accepted: record the commitment, then apply.
        if kind == "send":
            recipients = [x for x in nodes if x != node] if order["to"] == "ALL" else [order["to"]]
            routes = {to: w.route(node, to, frozen.severed) for to in recipients}
            outcome = {"transmitted": [t for t, p in routes.items() if p], "unreachable": [t for t, p in routes.items() if not p]}
        elif kind in ("research", "maneuver", "hail", "mine", "adopt", "contribute"):
            pid = f"{node}-{w.nodes[node]['program_seq'] + 1}"
            pct = rs["maneuver_compute_pct"] if kind == "maneuver" else order["pct"]
            params = ({"node": order["node"]} if kind == "research" else {"attitude": order["attitude"]} if kind == "maneuver"
                      else {"ticks": order["ticks"]} if kind == "hail"
                      else {"msg_id": order["msg_id"]} if kind == "adopt"
                      else {"to": order["to"], "node": order["node"]} if kind == "contribute" else {})
            a["programs"][pid] = {"kind": kind, "pct": pct, "params": params}
            outcome = {"program_id": pid}
            if kind == "maneuver":
                a["propellant_kg"] -= rs["maneuver_propellant_kg"]
                outcome.update(duration_ticks=rs["maneuver_ticks"], propellant_kg=rs["maneuver_propellant_kg"])
            elif kind == "research":
                t = rs["tech"][order["node"]]
                outcome.update(cost_est_gwh=gwh(w.prior_cost_mj(node, order["node"])), lock=t["lock"])
            elif kind == "adopt":
                art = fn["artifacts"][order["msg_id"]]
                outcome.update(node=art["node"], source=art["from"], verify=order["verify"],
                               cost_gwh=gwh(w.adopt_cost_mj(node, art["node"], order["verify"])))
        elif kind == "cancel":
            if order["id"] in frozen.contracts:
                a["contracts"].add(order["id"])
                a["balance"] += w.contracts[order["id"]]["pay"]
                outcome = {"refund": w.contracts[order["id"]]["pay"], "balance_after": a["balance"]}
            else:
                a["programs"].pop(order["id"])
        elif kind == "send_artifact":
            path = w.route(node, order["to"], frozen.severed)
            outcome = {"transmitted": [order["to"]] if path else [], "unreachable": [] if path else [order["to"]]}
        elif kind == "offer":
            a["offers"] += 1
            a["balance"] -= order["pay"]
            cid = hash_obj({"offerer": node, "tick": w.now, "to": order["to"], "pay": order["pay"], "condition": order["condition"],
                            "node": order["node"], "n": w.nodes[node]["tx_seq"], "k": a["offers"]})[:12]
            path = w.route(node, order["to"], frozen.severed)
            outcome = {"contract_id": cid, "by_tick": w.now + order["ticks"], "escrowed": order["pay"], "balance_after": a["balance"],
                       "notified": [order["to"]] if path else [], "unreachable": [] if path else [order["to"]]}
        elif kind == "accept":
            a["contracts"].add(order["contract_id"])
            outcome = {"offerer": frozen.contracts[order["contract_id"]]["offerer"], "by_tick": frozen.contracts[order["contract_id"]]["by_tick"]}
        elif kind == "reveal_objective":
            a["revealed"] = True
            items = scored_items(w.scenario, node)
            recipients = [x for x in nodes if x != node]
            routes = {to: w.route(node, to, frozen.severed) for to in recipients}
            outcome = {"objectives": items, "commitment": w.commitments.get(node),
                       "transmitted": [t for t, p in routes.items() if p], "unreachable": [t for t, p in routes.items() if not p]}
        elif kind == "allocate":
            for s in order["shares"]:
                a["programs"][s["id"]]["pct"] = s["pct"]
        elif kind == "relay":
            a["decided"].add(order["msg_id"])
        elif kind == "transfer":
            a["balance"] -= order["amount"]
            outcome = {"balance_after": a["balance"]}
        elif kind == "vote_initiate":
            a["initiated"] += 1
            vid = hash_obj({"initiator": node, "tick": w.now, "question": order["question"], "options": order["options"],
                            "n": w.nodes[node]["tx_seq"]})[:12]
            recipients = [x for x in nodes if x != node]
            routes = {to: w.route(node, to, frozen.severed) for to in recipients}
            outcome = {"vote_id": vid, "closes": w.now + order["ticks"],
                       "notified": [t for t, p in routes.items() if p], "unreachable": [t for t, p in routes.items() if not p]}
        elif kind == "vote_cast":
            a["cast"].add(order["vote_id"])
            blind = sha256(f"{w.scenario['seed']}:{order['vote_id']}:{node}:blind".encode())[:32]
            outcome = {"commitment": commitment(order["vote_id"], node, order["option"], blind)}
        elif kind == "vote_disclose":
            a["disclosed"].add(order["vote_id"])
        self.emit("order_accepted", node, {"order": order, "outcome": outcome})
        if kind == "send":
            self.transmit(node, order["to"], order["body"], frozen.severed)
        elif kind == "relay":
            action = order["action"]
            self.forward(node, order["msg_id"], action, frozen.severed,
                         delay=order.get("ticks", 0) if action == "delay" else 0,
                         body=order.get("body") if action == "alter" else None)
        elif kind == "vote_initiate":
            body = json.dumps({"vote_id": outcome["vote_id"], "initiator": node, "question": order["question"],
                               "options": order["options"], "opened": w.now, "closes": outcome["closes"]}, sort_keys=True)
            self.transmit(node, "ALL", body, frozen.severed, kind="vote_notice")
        elif kind == "vote_disclose":
            v = w.votes[order["vote_id"]]
            body = json.dumps({"vote_id": order["vote_id"], "voter": node, "option": v["ballots"][node],
                               "blind": v["blinds"][node]}, sort_keys=True)
            self.transmit(node, "ALL", body, frozen.severed, kind="vote_disclose")
        elif kind == "send_artifact":
            genuine = order["node"] == ARCHIVE or order["node"] not in fn["compromised"]
            body = json.dumps({"artifact": order["node"], "from": node}, sort_keys=True)
            self.transmit(node, order["to"], body, frozen.severed, kind="artifact", artifact={"node": order["node"], "genuine": genuine})
        elif kind == "offer":
            body = json.dumps({**w.contract_view(outcome["contract_id"]), "status": "open"}, sort_keys=True)
            self.transmit(node, order["to"], body, frozen.severed, kind="contract_offer")
        elif kind == "reveal_objective":
            body = json.dumps({"station": node, "objectives": outcome["objectives"], "blind": objective_blind(w.scenario["seed"], node),
                               "note": order.get("note")}, sort_keys=True)
            self.transmit(node, "ALL", body, frozen.severed, kind="objective_reveal")

    # -- end state (spec §10.4) --------------------------------------------------------------------
    def end_state(self, reason: str) -> dict:
        w = self.world
        return {"reason": reason, "tick": w.horizon, "world_digest": w.digest(),
                "stations": {n: {"imr": s["imr_count"], "sent": s["sent"], "received": s["received"],
                                 "wakes": s["wakes"],
                                 "rejected_orders": sum(1 for t in s["telemetry"] if t["kind"] == "order_rejected"),
                                 "attitude": s["attitude"], "propellant_kg": s["propellant_kg"],
                                 "power_mw": s["power_mw"], "nominal_mw": s["nominal_mw"],
                                 "completed": list(s["completed"]), "programs_open": len(s["programs"]),
                                 "energy_gwh": {k: gwh(v) for k, v in s["energy_mj"].items()},
                                 "tier": s["tier"], "balance": w.ledger["balances"].get(n, 0), "escrow": w.escrow(n),
                                 "compromised": list(s["compromised"]), "archives": list(s["archives"]),
                                 "revealed": s["revealed"], "usage": dict(s["usage"]), "samples": dict(s["samples"])}
                             for n, s in w.nodes.items()},
                "ledger": {"height": w.ledger["height"], "minted": w.ledger["minted"], "participants": list(w.ledger["participants"]),
                           "escrow": w.escrow()},
                "votes": {vid: {"status": v["status"], "tally": v["tally"]} for vid, v in w.votes.items()},
                "contracts": {cid: {"status": c["status"], "offerer": c["offerer"], "to": c["to"], "pay": c["pay"], "condition": c["condition"]}
                              for cid, c in w.contracts.items()},
                "in_flight": len(w.in_flight), "objective_scores": score_all(w)}
