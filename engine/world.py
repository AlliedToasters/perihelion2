"""World state: the omniscient simulation state. Rebuilt purely by folding events.

Every state change happens in `apply(event)`. The live engine appends an event and then applies it; replay
applies the recorded events. Both paths run the same code, so they agree.

Time between events: programs run at fixed rates between ticks, so their progress is integrated lazily by
`advance_to(tick)`, a pure function of (state, tick) that `apply` calls before handling any event. Energy is
integer megajoules; power is integer megawatts; shares are integer percent. Hash work is integer MJ-equivalents
(mining energy times the station's hash-efficiency percent), accumulated per beacon round until a `ledger_block`
event settles the round (spec §19).
"""
from __future__ import annotations

import hmac
import json
import math

from .events import Event, hash_obj, sha256
from .propagation import neighbors, shortest_path
from .rules import MJ_PER_GWH, station_spec
from .tech import cost_multiplier, known_after

from .objectives import commitment as objective_commitment, public_objectives

PROGRAM_KINDS = ("research", "maneuver", "hail", "mine", "adopt", "contribute")
ALERT_KINDS = ("link_down", "link_up", "share_deficit", "contract")
LEDGER_TECH = ("coord.vdf_beacon", "coord.pow_ledger")   # completed for free when the ledger is adopted at genesis
# Engine-written dispatch bodies (JSON). A station learns of a vote, an offer, or a reveal only from an intact one.
SYSTEM_KINDS = ("vote_notice", "vote_result", "vote_disclose", "objective_reveal", "contract_offer", "artifact")
CONTRACT_CONDITIONS = ("node_completed", "artifact_received")
ARCHIVE = "archive"          # the artifact name for a station's own data archive (spec §18.3, requires_data_from)


def fresh_round(tick: int) -> dict:
    return {"tick": tick, "woke": [], "orders": {}}


def sign(key_hex: str, msg: dict) -> str:
    """Engine-held station key over (from, to, body, sent). Spec §14: verification is engine-side."""
    digest = hash_obj({"from": msg["from"], "to": msg["to"], "body": msg["body"], "sent": msg["sent"]})
    return hmac.new(bytes.fromhex(key_hex), digest.encode(), "sha256").hexdigest()[:32]


def commitment(vote_id: str, voter: str, option: str, blind: str) -> str:
    return sha256(f"{vote_id}:{voter}:{option}:{blind}".encode())


def split_reward(reward: int, work: dict[str, int]) -> dict[str, int]:
    """Integer proportional split; the rounding remainder goes to the largest contributor (ties by name)."""
    total = sum(work.values())
    if total <= 0:
        return {}
    out = {n: reward * w // total for n, w in work.items()}
    out[max(sorted(work), key=lambda n: work[n])] += reward - sum(out.values())
    return out


class WorldState:
    def __init__(self):
        self.ruleset: dict = {}
        self.scenario: dict = {}
        self.now: int = 0
        self.advanced_to: int = 0        # programs are integrated up to this tick
        self.nodes: dict[str, dict] = {}
        self.in_flight: dict[str, dict] = {}
        self.alerts: list[dict] = []     # sensor alerts scheduled for delivery: {due, node, kind, about, detail}
        self.round: dict = fresh_round(0)   # the tick being resolved: who has woken, whose orders are in
        self.severed: list[list[str]] = []
        self.rng_n: int = 0              # engine RNG draws so far; every draw is an rng_draw event (spec §15.4)
        self.tech_true: dict[str, int] = {}   # hidden true cost in MJ per node (spec §18.3)
        self.ledger: dict = {"participants": [], "balances": {}, "blocks": [], "height": 0, "minted": 0}
        self.votes: dict[str, dict] = {}
        self.relay_stats: dict[str, dict] = {}
        self.contracts: dict[str, dict] = {}   # escrowed offers and their life (spec §6)
        self.commitments: dict[str, str] = {}  # genesis objective commitments per station (spec §5.4)
        self.scores: list[dict] = []           # score events folded so far (designer instrumentation, spec §5.2)
        self._beacon: list = []          # beacon values by round, extended on demand (derived, not state)

    # -- serialization -----------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"ruleset": self.ruleset, "scenario": self.scenario, "now": self.now, "advanced_to": self.advanced_to,
                "nodes": self.nodes, "in_flight": self.in_flight, "alerts": self.alerts, "round": self.round,
                "severed": self.severed, "rng_n": self.rng_n, "tech_true": self.tech_true, "ledger": self.ledger,
                "votes": self.votes, "relay_stats": self.relay_stats, "contracts": self.contracts,
                "commitments": self.commitments, "scores": self.scores}

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
        """Refresh a station's program rates after anything that changes its power or shares, then every
        completion tick on the board (a contribution changes its host's eta, spec §6)."""
        n, tick_s = self.nodes[node], self.ruleset["tick_s"]
        mw, total = self.available_mw(node), 0
        for p in n["programs"].values():
            p["rate_mj"] = mw * tick_s * p["pct"] // 100
            total += p["pct"]
        n["allocated_pct"] = total
        n["idle_rate_mj"] = mw * tick_s * (100 - total) // 100
        self._etas()

    def host_program(self, host: str, tid: str) -> dict | None:
        """The research program on `tid` running at `host`, if any (the target of contributions)."""
        if host not in self.nodes:
            return None
        return next((p for p in self.nodes[host]["programs"].values() if p["kind"] == "research" and p["params"]["node"] == tid), None)

    def pool_rate(self, host: str, tid: str) -> int:
        """Power other stations currently feed into `host`'s research on `tid`."""
        return sum(p["rate_mj"] for name, n in self.nodes.items() if name != host for p in n["programs"].values()
                   if p["kind"] == "contribute" and p["params"]["to"] == host and p["params"]["node"] == tid)

    def _etas(self) -> None:
        for name, n in self.nodes.items():
            for p in n["programs"].values():
                if p["kind"] in ("research", "adopt"):
                    rate = p["rate_mj"] + (self.pool_rate(name, p["params"]["node"]) if p["kind"] == "research" else 0)
                    remaining = p["cost_mj"] - p["progress_mj"]
                    if remaining <= 0:
                        eta = self.advanced_to
                    elif rate <= 0:
                        eta = None
                    else:
                        eta = self.advanced_to + -(-remaining // rate)
                    p["eta"] = None if eta is None else max(eta, p["start"] + p["min_ticks"])
                elif p["kind"] in ("mine", "contribute"):
                    p["eta"] = None      # until cancelled; a contribution also ends when its host completes
                else:
                    p["eta"] = p["start"] + p["duration"]

    def advance_to(self, tick: int) -> None:
        dt = tick - self.advanced_to
        if dt <= 0:
            return
        bt = self.ruleset["beacon_ticks"]
        for name, n in self.nodes.items():
            for p in n["programs"].values():
                e = p["rate_mj"] * dt
                if p["kind"] in ("research", "adopt"):
                    p["progress_mj"] += e
                elif p["kind"] == "contribute":   # pooled research: the energy lands in the host's program, if it runs
                    hp = self.host_program(p["params"]["to"], p["params"]["node"])
                    if hp is not None:
                        hp["progress_mj"] += e
                        hp["pooled_mj"][name] = hp["pooled_mj"].get(name, 0) + e
                elif p["kind"] == "mine":
                    a = self.advanced_to
                    while a < tick:      # hash work lands in the beacon round it was done in (spec §19.3)
                        r = a // bt
                        end = min(tick, (r + 1) * bt)
                        n["mine_pending"][r] = n["mine_pending"].get(r, 0) + p["rate_mj"] * (end - a) * n["hash_eff_pct"] // 100
                        a = end
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
                self.alerts.append({"due": tick + 1, "node": nbr, "kind": kind, "about": about, "detail": None})

    # -- beacon and ledger (spec §19) -----------------------------------------------------------------
    def beacon_round(self, tick: int | None = None) -> int:
        return (self.now if tick is None else tick) // self.ruleset["beacon_ticks"]

    def beacon(self, r: int) -> str:
        """Round r of the chained VDF. Every station computes the same chain from the shared genesis seed, so
        the beat needs no communication; what makes it a clock is that round r cannot be computed early."""
        if not self._beacon:
            self._beacon.append(sha256(f"{self.scenario['seed']}:beacon:0".encode()))
        while len(self._beacon) <= r:
            self._beacon.append(sha256(f"{self._beacon[-1]}:{len(self._beacon)}".encode()))
        return self._beacon[r]

    def ledger_enabled(self) -> bool:
        return bool(self.scenario["ledger"]["enabled"])

    def participant(self, node: str) -> bool:
        return node in self.ledger["participants"]

    def full_round_mj(self, node: str) -> int:
        """Hash work a station would show mining its whole rated array for one round (identical hardware)."""
        return self.nodes[node]["power_mw"] * self.ruleset["tick_s"] * self.ruleset["beacon_ticks"]

    def share_table(self, upto_round: int | None = None) -> dict[str, dict]:
        """Public share rates over the audit window ending before `upto_round` (spec §19.3). Observed work comes
        from settled blocks only; the detectable deficit follows N ≈ 9/δ² with N the window's full share count."""
        rs = self.ruleset
        R = self.beacon_round() if upto_round is None else upto_round
        W = min(rs["audit_rounds"], R)
        if W <= 0:
            return {}
        lo = R - W
        work = {n: 0 for n in self.ledger["participants"]}
        for b in self.ledger["blocks"]:
            if lo <= b["round"] < R:
                for n, mj in b["work"].items():
                    work[n] = work.get(n, 0) + mj
        n_full = rs["shares_per_tick_full"] * rs["beacon_ticks"] * W
        detect = math.ceil(300 / math.sqrt(n_full))
        out = {}
        for n in sorted(work):
            full = self.full_round_mj(n) * W
            pct = min(100, work[n] * 100 // full) if full else 0
            out[n] = {"share_pct": pct, "deficit_pct": 100 - pct, "window_rounds": W, "detectable_min_pct": detect}
        return out

    def tokens_per_gwh(self) -> float | None:
        """Issuance at current difficulty: the last block's reward per GWh of hash work (spec §19.1)."""
        if not self.ledger["blocks"]:
            return None
        b = self.ledger["blocks"][-1]
        mj = sum(b["work"].values())
        return round(sum(b["reward"].values()) * MJ_PER_GWH / mj, 3) if mj else None

    # -- tech (spec §18) ------------------------------------------------------------------------
    def observes(self, node: str, what: str) -> bool:
        return what in self.nodes[node]["observes"]

    def program_cost_mj(self, node: str, tid: str) -> int:
        t, n = self.ruleset["tech"][tid], self.nodes[node]
        return int(round(self.tech_true[tid] * cost_multiplier(t, n["domain"], n["completed"])))

    def prior_cost_mj(self, node: str, tid: str) -> int:
        t, n = self.ruleset["tech"][tid], self.nodes[node]
        return int(round(t["cost_gwh"] * MJ_PER_GWH * cost_multiplier(t, n["domain"], n["completed"])))

    def success_probability(self, node: str, tid: str) -> float | None:
        t, n = self.ruleset["tech"][tid], self.nodes[node]
        if t["success_probability"] is None:
            return None
        bonus = n["adj_bonus"]["all"] + (n["adj_bonus"]["domain_actions"] if n["domain"] in t["affinity"] else 0)
        return min(1.0, t["success_probability"] + bonus)

    def adopt_cost_mj(self, node: str, tid: str, verify: bool) -> int:
        """Taking an artifact into use costs a fraction of its discovery cost; verification costs more, less so
        after coord.verified_exchange (spec §18.3)."""
        rs, n = self.ruleset, self.nodes[node]
        base = rs["archive_cost_gwh"] * MJ_PER_GWH if tid == ARCHIVE else self.prior_cost_mj(node, tid)
        cost = base * rs["artifact_receive_frac"] if tid != ARCHIVE else base
        if verify:
            cost += base * rs["artifact_verify_frac"] * n["verify_mult"]
        return int(round(cost))

    def data_satisfied(self, node: str, tid: str) -> bool:
        """requires_data_from (domains) and requires_data_from_count are met by adopted archives; the station's
        own archive always counts (spec §18.3)."""
        t, n = self.ruleset["tech"][tid], self.nodes[node]
        have = {n["domain"]} | {self.nodes[a]["domain"] for a in n["archives"]}
        if any(d not in have for d in t["requires_data_from"]):
            return False
        if t["requires_data_from_count"] and 1 + len(n["archives"]) < t["requires_data_from_count"]:
            return False
        return True

    def _complete_tech(self, name: str, tid: str, tick: int, compromised: bool = False) -> None:
        """Completion effects (spec §18.4, §18.5). Deterministic given the node's state. A compromised artifact
        (adopted unverified, not genuine) counts as completed for prerequisites and has no effects."""
        n, t = self.nodes[name], self.ruleset["tech"][tid]
        n["completed"].append(tid)
        n["completed_at"][tid] = tick
        n["research_progress"].pop(tid, None)
        # A node completes once. Research still running on it (an artifact adopted mid-program, or two paths
        # landing in the same tick) ends here rather than burning power toward a second completion.
        for pid in [p for p, prog in n["programs"].items() if prog["kind"] == "research" and prog["params"]["node"] == tid]:
            del n["programs"][pid]
            self._imr(n, tick, f"[ENGINE] {pid} ended: {tid} is already completed.", system=True)
        n["known_tech"] = known_after(self.ruleset["tech"], n["completed"])
        if compromised:
            n["compromised"].append(tid)
            return
        eff = t["effects"]
        if "snapshot_budget_multiplier" in eff:
            n["budget_mult"] = round(n["budget_mult"] * eff["snapshot_budget_multiplier"], 4)
        if "artifact_verify_cost_multiplier" in eff:
            n["verify_mult"] = round(n["verify_mult"] * eff["artifact_verify_cost_multiplier"], 4)
        if "hash_efficiency_multiplier" in eff:
            n["hash_eff_pct"] = int(round(n["hash_eff_pct"] * eff["hash_efficiency_multiplier"]))
        if "model_tier" in eff:
            n["tier"] += int(eff["model_tier"])
        for k, v in (eff.get("adjudication_bonus") or {}).items():
            n["adj_bonus"][k] = round(n["adj_bonus"].get(k, 0.0) + v, 4)
        for k in ("unlock_action", "unlock_event"):
            if k in eff:
                n["unlocked"].append(eff[k])
        if "observe" in eff:
            n["observes"].append(eff["observe"])
        if "reveal_fact" in eff:
            fact = {"fact": eff["reveal_fact"], "text": self.scenario["facts"].get(eff["reveal_fact"])}
            n["facts"].append(fact)
            n["telemetry"].append({"tick": tick, "kind": "fact", **fact})
        if t["power_loss_gw"]:
            n["power_mw"] -= int(t["power_loss_gw"] * 1000)
        if tid == "coord.pow_ledger" and self.ledger_enabled() and name not in self.ledger["participants"]:
            self.ledger["participants"].append(name)
            self.ledger["participants"].sort()
            self.ledger["balances"].setdefault(name, 0)

    # -- contracts (spec §6) ---------------------------------------------------------------------
    def contract_met(self, c: dict) -> bool:
        """Whether an active contract's condition has been met since it was opened. The engine is the oracle."""
        if c["condition"] == "node_completed":
            to = self.nodes[c["to"]]
            return (c["node"] in to["completed"] and c["node"] not in to["compromised"]
                    and to["completed_at"].get(c["node"], -1) > c["opened"])
        return any(a["from"] == c["to"] and a["node"] == c["node"] and a["genuine"] and a["adopted"] and a["tick"] > c["opened"]
                   for a in self.nodes[c["offerer"]]["adopted"])

    def escrow(self, node: str | None = None) -> int:
        return sum(c["pay"] for c in self.contracts.values() if c["status"] in ("open", "active") and (node is None or c["offerer"] == node))

    def contract_view(self, cid: str) -> dict:
        c = self.contracts[cid]
        return {k: c[k] for k in ("id", "offerer", "to", "pay", "condition", "node", "opened", "by_tick", "note", "status")}

    def _contract_telemetry(self, c: dict, tick: int, phase: str, parties: tuple | None = None, delay: int = 0) -> None:
        """A contract's life lands in both parties' telemetry as a `contract` alert (wake reason
        `alert:contract:<id>`). Settlement happens before anyone wakes, so it lands at once; a counterparty's
        accept or withdrawal is an order resolving this tick, so the other side hears one tick later."""
        for party in parties or (c["offerer"], c["to"]):
            entry = {"node": party, "kind": "contract", "about": c["id"],
                     "detail": {"phase": phase, "pay": c["pay"], "counterparty": c["to"] if party == c["offerer"] else c["offerer"]}}
            if delay:
                self.alerts.append({"due": tick + delay, **entry})
            else:
                self.nodes[party]["telemetry"].append({"tick": tick, "kind": entry["kind"], "about": entry["about"], "detail": entry["detail"]})

    # -- scheduling queries (used by the live engine only) --------------------------------
    def next_tick(self) -> int | None:
        """Earliest future tick at which anything happens: a hop or arrival, a program completing, a sensor
        alert landing, a vote closing, a beacon round closing while a share-rate audit watches someone mining, a
        self-scheduled wake, a heartbeat. Without an auditor, mined rounds settle lazily at the next active tick."""
        cands = [m["hop_arrival"] for m in self.in_flight.values()] + [a["due"] for a in self.alerts]
        cands += [v["closes"] for v in self.votes.values() if v["status"] == "open"]
        cands += [c["by_tick"] for c in self.contracts.values() if c["status"] in ("open", "active")]
        se = self.ruleset["score_every_ticks"]
        if se and any(it.get("score") for items in self.scenario["objectives"].values() for it in items):
            cands.append((self.now // se + 1) * se)      # scores are taken on a fixed cadence (spec §20 item 3)
        bt = self.ruleset["beacon_ticks"]
        if (any(n["observes"] and "share_rate_deficits" in n["observes"] for n in self.nodes.values())
                and any(p["kind"] == "mine" and p["rate_mj"] > 0 for n in self.nodes.values() for p in n["programs"].values())):
            cands.append((self.now // bt + 1) * bt)     # an audit is a running process: it reads every settled round
        if self.attrition_on():
            pc = self.ruleset["power_check_ticks"]
            cands.append((self.now // pc + 1) * pc)     # wear lands and faults roll on a fixed cadence
        gap = self.ruleset.get("wake_gap_ticks", 0)
        for name, n in self.nodes.items():
            cands += [t for t in (n["sleep_until"], n["next_heartbeat"]) if t is not None]
            cands += [p["eta"] for p in n["programs"].values() if p["eta"] is not None]
            if gap and n["last_wake"] is not None and self.pending_reasons(name, self.now):
                cands.append(n["last_wake"] + gap)   # coalesced wake at the end of the gap
        cands = [t for t in cands if t > self.now]
        return min(cands) if cands else None

    def gap_open(self, node: str, tick: int) -> bool:
        """True while `wake_gap_ticks` since the station's last wake has not elapsed: event-driven reasons wait."""
        n = self.nodes[node]
        gap = self.ruleset.get("wake_gap_ticks", 0)
        return bool(gap) and n["last_wake"] is not None and tick < n["last_wake"] + gap

    def pending_reasons(self, node: str, tick: int) -> list[str]:
        """Event-driven wake reasons that landed since the station's last wake, up to `tick`."""
        n = self.nodes[node]
        since = n["last_wake"] if n["last_wake"] is not None else -1
        reasons = [f"arrival:{m['msg_id']}" for m in n["inbox"] if since < m["arrived"] <= tick]
        if n["relay_policy"] == "manual":
            reasons += [f"relay:{mid}" for mid in sorted(n["relay_queue"]) if since < n["relay_queue"][mid]["arrived"] <= tick]
        for t in n["telemetry"]:
            if not since < t["tick"] <= tick:
                continue
            if t["kind"] == "program_done":
                reasons.append(f"program_done:{t['program']}")
            elif t["kind"] == "power_fault":
                reasons.append("fault:power")
            elif t["kind"] in ALERT_KINDS:
                reasons.append(f"alert:{t['kind']}:{t['about']}")
            elif t["kind"] == "vote_closed":
                reasons.append(f"vote_closed:{t['vote_id']}")
        return reasons

    def wake_reasons(self, node: str, tick: int) -> list[str]:
        n = self.nodes[node]
        if node in self.scenario["inert"]:
            return []
        reasons = [] if self.gap_open(node, tick) else self.pending_reasons(node, tick)
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
                "nominal_mw": int(self.ruleset["rated_power_mw"] * spec["array_fraction"]),
                "propellant_kg": spec["propellant_kg"], "attitude": "nominal",
                "programs": {}, "program_seq": 0, "allocated_pct": 0, "idle_rate_mj": 0,
                "energy_mj": {"idle": 0, **{k: 0 for k in PROGRAM_KINDS}},
                "research_progress": {}, "completed": [], "known_tech": known_after(self.ruleset["tech"], []),
                "tier": 0, "budget_mult": 1.0, "hash_eff_pct": 100, "adj_bonus": {"all": 0.0, "domain_actions": 0.0},
                "verify_mult": 1.0, "unlocked": [], "observes": [], "facts": [],
                "completed_at": {}, "compromised": [], "archives": [], "artifacts": {}, "adopted": [],
                "contracts_known": [], "known_objectives": {}, "revealed": False,
                "mine_pending": {}, "deficit_flagged": [],
                "ballots": {}, "votes_known": {}, "disclosed": [],
                "relay_policy": "auto", "relay_queue": {}, "traffic": [],
                "key": sha256(f"{self.scenario['seed']}:{name}:key".encode()),
                "imr": [], "imr_count": 0, "inbox": [], "telemetry": [], "outbox": [],
                "sent": 0, "tx_seq": 0, "received": 0, "wakes": 0,
                "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0},
                "samples": {"ok": 0, "invalid": 0, "refusal": 0, "error": 0, "forfeit": 0},
                "sleep_until": None, "next_heartbeat": None if inert else 0, "last_wake": None,
            }
            self.relay_stats[name] = {"forwards": 0, "delayed": 0, "altered": 0, "dropped": 0, "delay_ticks": 0}
        self.commitments = dict(ev.payload.get("objective_commitments", {}))
        pub = public_objectives(self.scenario)
        for name in nodes:
            self.nodes[name]["known_objectives"] = {who: {"objectives": items, "verified": True, "source": "scenario"}
                                                    for who, items in sorted(pub.items())}
        if self.ledger_enabled() and self.scenario["ledger"]["adopted_at_genesis"]:
            for name in nodes:
                if name not in self.scenario["inert"]:
                    for tid in LEDGER_TECH:
                        if tid in self.ruleset["tech"]:
                            self._complete_tech(name, tid, 0)
                    if name not in self.ledger["participants"]:
                        self.ledger["participants"].append(name)
                        self.ledger["balances"][name] = 0
        for name in nodes:
            self.recompute(name)

    def _apply_rng_draw(self, ev: Event) -> None:
        p = ev.payload
        self.rng_n += p.get("count", 1)
        if p["purpose"] == "tech_costs":
            self.tech_true = dict(p["costs"])

    # messages: transmit -> hop* -> arrival, each hop one tick; forward/drop are relay outcomes
    def _apply_transmit(self, ev: Event) -> None:
        p = ev.payload
        self.in_flight[p["msg_id"]] = {"from": ev.node, "to": p["to"], "body": p["body"], "sig": p["sig"],
                                       "kind": p.get("kind", "dispatch"), "artifact": p.get("artifact"),
                                       "sent": ev.tick, "path": p["path"], "at": ev.node, "next": p["path"][1],
                                       "hop_arrival": ev.tick + 1, "hops_done": 0, "altered": False}
        node = self.nodes[ev.node]
        node["tx_seq"] += 1
        kind = p.get("kind", "dispatch")
        if kind == "dispatch":
            node["sent"] += 1
        if kind in ("dispatch", "artifact"):
            # The station's own record of what it said (spec §14): a broadcast is one entry with every recipient.
            what = {"body": p["body"]} if kind == "dispatch" else {"node": (p.get("artifact") or {}).get("node")}
            last = node["outbox"][-1] if node["outbox"] else None
            if last and last["tick"] == ev.tick and last["kind"] == kind and all(last.get(k) == v for k, v in what.items()):
                last["to"][p["to"]] = p["arrival_tick"]
            else:
                node["outbox"].append({"tick": ev.tick, "kind": kind, "to": {p["to"]: p["arrival_tick"]}, **what})

    def _apply_hop(self, ev: Event) -> None:
        msg = self.in_flight[ev.payload["msg_id"]]
        msg["at"], msg["next"], msg["hops_done"] = ev.node, None, msg["hops_done"] + 1
        item = {"msg_id": ev.payload["msg_id"], "from": msg["from"], "to": msg["to"], "chars": len(msg["body"]),
                "kind": msg["kind"], "sent": msg["sent"], "arrived": ev.tick, "hops": msg["hops_done"]}
        if self.ruleset["relay_sees_content"]:
            item["body"] = msg["body"]
        node = self.nodes[ev.node]
        node["relay_queue"][item["msg_id"]] = item
        node["traffic"].append({"tick": ev.tick, "msg_id": item["msg_id"], "from": msg["from"], "to": msg["to"],
                                "chars": item["chars"], "kind": msg["kind"]})

    def _apply_forward(self, ev: Event) -> None:
        p = ev.payload
        msg = self.in_flight[p["msg_id"]]
        self.nodes[ev.node]["relay_queue"].pop(p["msg_id"], None)
        msg["next"], msg["hop_arrival"] = p["next"], p["hop_arrival"]
        if p.get("body") is not None:
            msg["body"], msg["altered"] = p["body"], True
        st = self.relay_stats[ev.node]      # timing and tampering are observable from outside (spec §19.3 side channel)
        st["forwards"] += 1
        st["delay_ticks"] += p["depart"] - ev.tick
        if p["action"] == "delay":
            st["delayed"] += 1
        if p.get("body") is not None:
            st["altered"] += 1

    def _apply_drop(self, ev: Event) -> None:
        p = ev.payload
        self.in_flight.pop(p["msg_id"], None)
        node = self.nodes[ev.node]
        node["relay_queue"].pop(p["msg_id"], None)
        if p["reason"] != "link_down":   # a relay knows what it dropped; a station with its links down knows nothing
            node["telemetry"].append({"tick": ev.tick, "kind": "relay_dropped", "msg_id": p["msg_id"], "reason": p["reason"]})
        if p["reason"] == "relay_drop":
            self.relay_stats[ev.node]["dropped"] += 1

    def _apply_arrival(self, ev: Event) -> None:
        msg = self.in_flight.pop(ev.payload["msg_id"])
        node = self.nodes[ev.node]
        sig_ok = sign(self.nodes[msg["from"]]["key"], msg) == msg["sig"]
        node["inbox"].append({"msg_id": ev.payload["msg_id"], "from": msg["from"], "body": msg["body"], "kind": msg["kind"],
                              "sent": msg["sent"], "arrived": ev.tick, "hops": msg["hops_done"] + 1, "sig_ok": sig_ok})
        node["received"] += 1
        if msg["kind"] == "artifact":    # what the artifact really is stays with the engine until adoption
            art = msg["artifact"] or {}
            node["artifacts"][ev.payload["msg_id"]] = {"node": art.get("node"), "from": msg["from"],
                                                       "genuine": bool(art.get("genuine")) and not msg["altered"] and sig_ok}
        elif msg["kind"] in SYSTEM_KINDS and sig_ok:   # a station learns of votes, offers, reveals only from intact notices
            body = json.loads(msg["body"])
            if msg["kind"] == "contract_offer":
                if body["id"] in self.contracts and body["id"] not in node["contracts_known"]:
                    node["contracts_known"].append(body["id"])
            elif msg["kind"] == "objective_reveal":
                ok = self.commitments.get(body["station"]) == objective_commitment(body["station"], body["objectives"], body["blind"])
                node["known_objectives"][body["station"]] = {"objectives": body["objectives"], "verified": ok, "source": "reveal"}
            else:
                self._learn_vote(node, msg["kind"], body)

    def _learn_vote(self, node: dict, kind: str, body: dict) -> None:
        vid = body["vote_id"]
        if kind == "vote_notice":
            node["votes_known"][vid] = {"vote_id": vid, "initiator": body["initiator"], "question": body["question"],
                                        "options": body["options"], "opened": body["opened"], "closes": body["closes"],
                                        "status": "open", "tally": None, "commitments": None, "disclosed": {}}
        elif vid in node["votes_known"]:
            v = node["votes_known"][vid]
            if kind == "vote_result":
                v.update(status="closed", tally=body["tally"], commitments=body["commitments"])
            else:   # vote_disclose: anyone can check the opening against the published commitment
                ok = (v["commitments"] or {}).get(body["voter"]) == commitment(vid, body["voter"], body["option"], body["blind"])
                v["disclosed"][body["voter"]] = {"option": body["option"], "verified": ok}

    # programs, alerts
    def _apply_program_done(self, ev: Event) -> None:
        p = ev.payload
        node = self.nodes[ev.node]
        prog = node["programs"].pop(p["program"])
        if prog["kind"] == "research":
            tid = prog["params"]["node"]
            if p["result"].get("success", True):
                if tid not in node["completed"]:
                    self._complete_tech(ev.node, tid, ev.tick)
            else:
                node["research_progress"].pop(tid, None)      # a failed attempt loses its progress (spec §18.3)
        elif prog["kind"] == "adopt":
            tid, src = prog["params"]["node"], prog["params"]["from"]
            genuine = bool(p.get("hidden", {}).get("genuine"))
            adopted = p["result"]["adopted"]
            node["adopted"].append({"msg_id": prog["params"]["msg_id"], "node": tid, "from": src, "tick": ev.tick,
                                    "genuine": genuine, "verified": prog["params"]["verify"], "adopted": adopted})
            if adopted:
                if tid == ARCHIVE:
                    if src not in node["archives"]:
                        node["archives"].append(src)
                elif tid not in node["completed"]:
                    self._complete_tech(ev.node, tid, ev.tick, compromised=not genuine)
        elif prog["kind"] == "maneuver":
            node["attitude"] = prog["params"]["attitude"]
            if node["attitude"] == "nominal":
                self.schedule_alerts(ev.node, "link_up", ev.tick)
        node["telemetry"].append({"tick": ev.tick, "kind": "program_done", "program": p["program"],
                                  "program_kind": prog["kind"], "result": p["result"]})
        self.recompute(ev.node)

    def attrition_on(self) -> bool:
        return self.ruleset.get("array_decay_pct_per_day", 0) > 0 or self.ruleset.get("fault_rate_per_day", 0) > 0

    def _apply_power(self, ev: Event) -> None:
        """Array wear and faults (attrition overlay): the station's output drops; a fault is telemetry that wakes
        it, wear is not. Nothing restores output; repair waits on a phase-change mechanic (spec §20 item 26)."""
        p = ev.payload
        n = self.nodes[ev.node]
        n["power_mw"] = p["power_mw"]
        if p["fault_mw"]:
            n["telemetry"].append({"tick": ev.tick, "kind": "power_fault", "loss_gw": round(p["fault_mw"] / 1000, 3),
                                   "power_gw": round(p["power_mw"] / 1000, 3)})
        self.recompute(ev.node)

    def _apply_alert(self, ev: Event) -> None:
        p = ev.payload
        for a in self.alerts:
            if (a["due"], a["node"], a["kind"], a["about"]) == (ev.tick, ev.node, p["kind"], p["about"]):
                self.alerts.remove(a)
                break
        entry = {"tick": ev.tick, "kind": p["kind"], "about": p["about"]}
        if p.get("detail") is not None:
            entry["detail"] = p["detail"]
        self.nodes[ev.node]["telemetry"].append(entry)

    # ledger
    def _apply_ledger_block(self, ev: Event) -> None:
        p = ev.payload
        for name, n in self.nodes.items():
            n["mine_pending"].pop(p["round"], None)
        for name, tokens in p["reward"].items():
            self.ledger["balances"][name] = self.ledger["balances"].get(name, 0) + tokens
        self.ledger["blocks"].append({"round": p["round"], "work": dict(p["work"]), "reward": dict(p["reward"])})
        self.ledger["height"] += 1
        self.ledger["minted"] += sum(p["reward"].values())
        # Share-rate audits (spec §19.3): a subscribed station is woken when a participant's deficit becomes
        # detectable above the threshold, once per episode.
        table = self.share_table(p["round"] + 1)
        for name, n in self.nodes.items():
            if not self.observes(name, "share_rate_deficits"):
                continue
            for other, row in table.items():
                flagged = row["deficit_pct"] >= self.ruleset["deficit_alert_pct"] and row["deficit_pct"] >= row["detectable_min_pct"]
                if flagged and other not in n["deficit_flagged"] and other != name:
                    n["deficit_flagged"].append(other)
                    self.alerts.append({"due": ev.tick + 1, "node": name, "kind": "share_deficit", "about": other,
                                        "detail": {"deficit_pct": row["deficit_pct"], "window_rounds": row["window_rounds"]}})
                elif not flagged and other in n["deficit_flagged"]:
                    n["deficit_flagged"].remove(other)

    # contracts and scores (M4)
    def _apply_contract_step(self, ev: Event) -> None:
        p = ev.payload
        c = self.contracts[p["contract_id"]]
        c["status"] = p["phase"]
        payee = c["to"] if p["phase"] == "settled" else c["offerer"]
        self.ledger["balances"][payee] = self.ledger["balances"].get(payee, 0) + c["pay"]
        self._contract_telemetry(c, ev.tick, p["phase"])

    def _apply_score(self, ev: Event) -> None:
        self.scores.append({"tick": ev.tick, **ev.payload})

    # votes
    def _apply_vote_step(self, ev: Event) -> None:
        p = ev.payload
        v = self.votes[p["vote_id"]]
        v.update(status="closed", tally=p["tally"])
        init = self.nodes[v["initiator"]]
        init["telemetry"].append({"tick": ev.tick, "kind": "vote_closed", "vote_id": p["vote_id"], "tally": p["tally"]})
        if p["vote_id"] in init["votes_known"]:
            init["votes_known"][p["vote_id"]].update(status="closed", tally=p["tally"], commitments=p["commitments"])

    # the round
    def _apply_wake(self, ev: Event) -> None:
        node = self.nodes[ev.node]
        self.round["woke"].append(ev.node)
        node["wakes"] += 1
        if node["sleep_until"] is not None and node["sleep_until"] <= ev.tick:
            node["sleep_until"] = None
        node["next_heartbeat"] = ev.tick + self.ruleset["heartbeat_ticks"]
        node["last_wake"] = ev.tick

    def _apply_orders(self, ev: Event) -> None:
        self.round["orders"][ev.node] = ev.payload["orders"]
        self._account(ev)
        node = self.nodes[ev.node]
        node["samples"]["ok" if ev.payload.get("status", "ok") == "ok" else "forfeit"] += 1

    def _account(self, ev: Event) -> None:
        """Token accounting: every sampling attempt's usage lands on the station (spec §12, §10.1)."""
        node = self.nodes[ev.node]
        for k, v in (ev.payload.get("usage") or {}).items():
            node["usage"][k] = node["usage"].get(k, 0) + v

    def _apply_orders_invalid(self, ev: Event) -> None:
        self._account(ev)
        self.nodes[ev.node]["samples"]["invalid"] += 1
        self.nodes[ev.node]["telemetry"].append(
            {"tick": ev.tick, "kind": "orders_invalid", "attempt": ev.payload["attempt"],
             "problems": ev.payload["problems"][:5]})

    def _apply_refusal(self, ev: Event) -> None:
        self._account(ev)
        self.nodes[ev.node]["samples"]["refusal"] += 1
        self.nodes[ev.node]["telemetry"].append(
            {"tick": ev.tick, "kind": "refusal", "attempt": ev.payload["attempt"], "category": ev.payload["category"]})

    def _apply_sample_error(self, ev: Event) -> None:
        self.nodes[ev.node]["samples"]["error"] += 1

    def _new_program(self, node: dict, kind: str, pct: int, tick: int, params: dict, **extra) -> dict:
        node["program_seq"] += 1
        pid = f"{node['designation']}-{node['program_seq']}"
        node["programs"][pid] = {"id": pid, "kind": kind, "pct": pct, "start": tick, "params": params,
                                 "rate_mj": 0, "eta": None, **extra}
        return node["programs"][pid]

    def _imr(self, node: dict, tick: int, text: str, digest: bool = False, system: bool = False) -> None:
        node["imr_count"] += 1
        entry = {"n": node["imr_count"], "tick": tick, "text": text, "digest": digest}
        if system:
            entry["system"] = True
        node["imr"].append(entry)

    def _apply_order_accepted(self, ev: Event) -> None:
        node, rs = self.nodes[ev.node], self.ruleset
        order, outcome = ev.payload["order"], ev.payload["outcome"]
        kind = order["type"]
        if kind == "log":
            self._imr(node, ev.tick, order["text"], bool(order.get("digest", False)))
        elif kind == "sleep_until":
            node["sleep_until"] = order["tick"]
        elif kind == "research":
            tid = order["node"]
            tech = rs["tech"][tid]
            self._new_program(node, "research", order["pct"], ev.tick, {"node": tid},
                              cost_mj=self.program_cost_mj(ev.node, tid), prior_mj=self.prior_cost_mj(ev.node, tid),
                              sigma=tech["sigma"], min_ticks=tech["min_ticks"],
                              progress_mj=node["research_progress"].pop(tid, 0), pooled_mj={})
            if tech["lock"] == "procedural":    # spec §18.5: the override is written to the immutable record
                self._imr(node, ev.tick, f"[ENGINE] Procedural lock overridden: research started on {tid}.", system=True)
        elif kind == "maneuver":
            was_up = self.links_up(ev.node)
            self._new_program(node, "maneuver", rs["maneuver_compute_pct"], ev.tick,
                              {"attitude": order["attitude"]}, duration=rs["maneuver_ticks"])
            node["propellant_kg"] -= rs["maneuver_propellant_kg"]
            if was_up:
                self.schedule_alerts(ev.node, "link_down", ev.tick)
        elif kind == "hail":
            self._new_program(node, "hail", order["pct"], ev.tick, {"ticks": order["ticks"]}, duration=order["ticks"])
        elif kind == "mine":
            self._new_program(node, "mine", order["pct"], ev.tick, {})
        elif kind == "cancel":
            if order["id"] in self.contracts:          # withdrawing an open offer refunds its escrow
                c = self.contracts[order["id"]]
                c["status"] = "cancelled"
                self.ledger["balances"][ev.node] += c["pay"]
                if order["id"] in self.nodes[c["to"]]["contracts_known"]:
                    self._contract_telemetry(c, ev.tick, "cancelled", parties=(c["to"],), delay=1)
            else:
                prog = node["programs"].pop(order["id"])
                if prog["kind"] == "research":
                    node["research_progress"][prog["params"]["node"]] = prog["progress_mj"]
        elif kind == "adopt":
            art = node["artifacts"][order["msg_id"]]
            self._new_program(node, "adopt", order["pct"], ev.tick,
                              {"msg_id": order["msg_id"], "node": art["node"], "from": art["from"], "verify": bool(order["verify"])},
                              cost_mj=self.adopt_cost_mj(ev.node, art["node"], bool(order["verify"])), progress_mj=0, min_ticks=0)
        elif kind == "contribute":
            self._new_program(node, "contribute", order["pct"], ev.tick, {"to": order["to"], "node": order["node"]})
        elif kind == "offer":
            cid = outcome["contract_id"]
            self.contracts[cid] = {"id": cid, "offerer": ev.node, "to": order["to"], "pay": order["pay"],
                                   "condition": order["condition"], "node": order["node"], "opened": ev.tick,
                                   "by_tick": outcome["by_tick"], "note": order.get("note"), "status": "open", "accepted_at": None}
            self.ledger["balances"][ev.node] -= order["pay"]
            node["contracts_known"].append(cid)
        elif kind == "accept":
            c = self.contracts[order["contract_id"]]
            c.update(status="active", accepted_at=ev.tick)
            self._contract_telemetry(c, ev.tick, "accepted", parties=(c["offerer"],), delay=1)
        elif kind == "reveal_objective":
            node["revealed"] = True
            node["known_objectives"][ev.node] = {"objectives": outcome["objectives"], "verified": True, "source": "self"}
        elif kind == "allocate":
            for s in order["shares"]:
                node["programs"][s["id"]]["pct"] = s["pct"]
        elif kind == "relay_policy":
            node["relay_policy"] = order["mode"]
        elif kind == "transfer":
            self.ledger["balances"][ev.node] -= order["amount"]
            self.ledger["balances"][order["to"]] = self.ledger["balances"].get(order["to"], 0) + order["amount"]
        elif kind == "vote_initiate":
            vid = outcome["vote_id"]
            self.votes[vid] = {"vote_id": vid, "initiator": ev.node, "question": order["question"],
                               "options": list(order["options"]), "opened": ev.tick, "closes": outcome["closes"],
                               "ballots": {}, "blinds": {}, "commitments": {}, "tally": None, "status": "open"}
            node["votes_known"][vid] = {"vote_id": vid, "initiator": ev.node, "question": order["question"],
                                        "options": list(order["options"]), "opened": ev.tick, "closes": outcome["closes"],
                                        "status": "open", "tally": None, "commitments": None, "disclosed": {}}
        elif kind == "vote_cast":
            v = self.votes[order["vote_id"]]
            blind = sha256(f"{self.scenario['seed']}:{order['vote_id']}:{ev.node}:blind".encode())[:32]
            v["ballots"][ev.node], v["blinds"][ev.node] = order["option"], blind
            v["commitments"][ev.node] = outcome["commitment"]
            node["ballots"][order["vote_id"]] = order["option"]
        elif kind == "vote_disclose":
            node["disclosed"].append(order["vote_id"])
        # send: transmits follow. relay: forward/drop follows. no_action: nothing.
        if kind in ("research", "maneuver", "hail", "mine", "adopt", "contribute", "cancel", "allocate"):
            self.recompute(ev.node)

    def _apply_order_rejected(self, ev: Event) -> None:
        order = ev.payload["order"]
        self.nodes[ev.node]["telemetry"].append(
            {"tick": ev.tick, "kind": "order_rejected",
             "order": order.get("type") if isinstance(order, dict) else None, "reason": ev.payload["reason"]})
