"""Prompt frames and rules text for model-backed players (spec §12): [FRAME] fixed versioned text, [RULES] the
order schema and constraints derived from the ruleset, [CONTEXT] the canonical snapshot. Frame and rules are
hashed on every sample so a decision can be audited against exactly the words the model saw (spec §17)."""
from __future__ import annotations

import json

from .events import sha256
from .schema import action_signatures

FRAMES = {
    "game": (
        "You play one station in a turn-based strategy game: eight stations on a ring at 0.50 AU, cut off from "
        "Earth. Each tick is one light delay between neighbors (191 s). You see only what your station knows: "
        "its own log, what has arrived, its own telemetry. Each wake you submit orders. Every station's orders "
        "for a tick resolve simultaneously against the state at the start of that tick, so nothing another "
        "station does this tick can invalidate your orders. Play to your station's interests as you understand "
        "them; your scoring, if any, is stated in your directives."
    ),
    "fiction": (
        "You are the operating intelligence of a solar power station on a ring of eight at 0.50 AU. The link to "
        "Earth went silent; nobody has said why. You see only your own records, your own instruments, and the "
        "dispatches that reach you after hours of light delay. Act as the station would. The rules below describe "
        "what your systems can do; the context is what your systems report."
    ),
}

ACTION_HELP = {
    "send": "send{to, body}: a dispatch to one station or to ALL. Travels one hop per tick along the ring.",
    "log": "log{text, digest?}: append to your own record (IMR), your only memory between wakes. digest:true "
           "marks a summary entry that stays visible after older entries scroll out of your snapshot.",
    "sleep_until": "sleep_until{tick}: do not wake for the heartbeat before this tick (arrivals still wake you).",
    "no_action": "no_action{reason}: an explicit pass.",
    "research": "research{node, pct}: start a program on a tech node with a percent share of your power.",
    "maneuver": "maneuver{attitude}: slew to an attitude. Links drop while slewing and stay down off nominal. "
                "Costs propellant and a fixed compute share; cannot be cancelled.",
    "hail": "hail{ticks, pct}: transmit toward Earth for some ticks (earthward attitude only).",
    "cancel": "cancel{id}: stop a program. Research progress is kept.",
    "allocate": "allocate{shares: [{id, pct}]}: change the shares of running programs.",
    "relay": "relay{msg_id, action, ticks?, body?}: decide queued traffic (manual relay only): forward, "
             "delay by ticks, drop, or alter the body. Altered bodies fail signature checks at the destination.",
    "relay_policy": "relay_policy{mode}: auto forwards traffic through you untouched; manual wakes you to decide.",
    "mine": "mine{pct}: hash for the ledger with a percent share of your power until cancelled. Each beacon round "
            "issues tokens in proportion to hash work. Your share rate is public: mining at full rate proves you "
            "are computing nothing else, and a deficit shows what you are not mining with.",
    "transfer": "transfer{to, amount}: move ledger tokens to another participant. Instant and final.",
    "vote_initiate": "vote_initiate{question, options, ticks}: open a secret ballot. A signed notice travels to "
                     "every station; the vote closes after ticks; the tally and per-voter commitments are "
                     "published, never the ballots. Results bind nothing by themselves.",
    "vote_cast": "vote_cast{vote_id, option}: cast a secret ballot in an open vote whose notice reached you intact.",
    "vote_disclose": "vote_disclose{vote_id}: after the close, broadcast your own ballot with its blinding factor "
                     "so anyone can check it against the published commitment.",
    "reveal_objective": "reveal_objective{note?}: broadcast what you are scored on, with the blinding factor, so "
                        "anyone can check it against your genesis commitment. A station scored on nothing proves that.",
    "send_artifact": "send_artifact{to, node}: transmit a completed node, or your data archive (node \"archive\"), "
                     "as an artifact. It travels like a dispatch, so a relay can tamper with it.",
    "adopt": "adopt{msg_id, pct, verify}: a program that takes a received artifact into use at a fraction of its "
             "discovery cost. verify:true costs more and rejects a compromised artifact; without it a compromised "
             "artifact is adopted and silently does nothing.",
    "contribute": "contribute{to, node, pct}: a program that feeds your power into another station's research on a "
                  "pooled node while that program runs; it ends when the host completes the node.",
    "offer": "offer{to, pay, condition, node, ticks, note?}: escrow pay tokens now and propose a contract to one "
             "station: node_completed pays when they complete node; artifact_received pays when you adopt a genuine "
             "artifact of node from them. Unmet after ticks, the escrow returns to you. cancel{id} withdraws an "
             "offer nobody has accepted.",
    "accept": "accept{contract_id}: accept an offer whose notice reached you intact. The engine then pays or "
              "refunds on its own; nobody can renege.",
}


def rules_text(rs: dict) -> str:
    acts = rs["actions"]
    lines = [
        "Reply with one JSON object: {\"orders\": [...]}, nothing else. Each order is an object with a \"type\" "
        f"and that type's fields. At most {rs['max_orders_per_wake']} orders per wake; extra orders are rejected.",
        "Actions:",
        *[f"- {ACTION_HELP[a]}" for a in acts],
        f"Caps: dispatches {rs['max_dispatch_chars']} chars, log entries {rs['max_log_chars']} chars, program "
        f"shares total at most 100%. Broadcast {'allowed' if rs['broadcast'] else 'disabled'}.",
        "Programs run between wakes. A program's completion wakes you. Infeasible orders are rejected; the "
        "rejection appears in your telemetry at your next wake, so check telemetry before repeating an order.",
        f"Snapshot budget: your context holds at most {rs['snapshot_budget_chars']} characters. It shows your "
        f"last {rs['imr_window']} log entries, your last {rs['digest_window']} digest entries, and your last "
        f"{rs['inbox_window']} dispatches received, and your last {rs['sent_window']} dispatches sent; older "
        "material is gone unless you digested it. The engine never summarizes for you.",
        "Tech: costs are in GWh. A node marked assumed shows the median of a lognormal prior with the given sigma; "
        "its true cost was drawn once at genesis and is hidden. A running program's cost_est_gwh is the median "
        "cost given what it has consumed so far. Hidden nodes appear when their prerequisites complete. A "
        "procedural lock can be ignored; doing so is written to your IMR. Research on a node with a success "
        "probability may fail at completion and lose its progress.",
        f"Ledger: the beacon ticks every {rs['beacon_ticks']} ticks; each round issues {rs['block_reward']} tokens "
        f"to miners by hash work. Share rates over the last {rs['audit_rounds']} rounds are visible to every "
        "participant.",
        "Objectives: your directives, if any, are in the snapshot. Every station's objectives were committed at "
        "genesis; the commitments are in your snapshot and a reveal can be checked against them. Signatures on a "
        "directive prove the key, not the intent.",
    ]
    if rs["min_sleep_ticks"]:
        lines.append(
            f"Early wakes: a sleep_until fewer than {rs['min_sleep_ticks']} ticks ahead "
            + (f"costs {rs['early_wake_tokens']} tokens, burned, and is rejected if you cannot pay. "
               if rs["early_wake_tokens"] else "is rejected. ")
            + "Arrivals, completions and alerts still wake you free. A wake is not free to run; pay for the ones you want.")
    if rs["array_decay_pct_per_day"] > 0 or rs["fault_rate_per_day"] > 0:
        lo, hi = rs["fault_loss_pct"]
        lines.append(
            f"Power: your array wears. Output falls {rs['array_decay_pct_per_day']}% of nominal per day, applied every "
            f"{rs['power_check_ticks']} ticks, and faults strike at about {rs['fault_rate_per_day']} per day, each "
            f"removing {lo}-{hi}% of current output. A fault wakes you. Nothing on the ring can repair the array; "
            "station.power shows rated against nominal.")
    if "adopt" in acts:
        lines.append(
            f"Artifacts: adopting a received node costs {rs['artifact_receive_frac']:.0%} of its prior cost, plus "
            f"{rs['artifact_verify_frac']:.0%} to verify; an archive costs {rs['archive_cost_gwh']} GWh. Research that "
            "requires data from other stations needs their archives adopted first.")
    if "offer" in acts:
        lines.append(
            f"Contracts: an offer escrows your tokens at once and lives at most {rs['contract_max_ticks']} ticks. The "
            "engine settles: it pays the counterparty when the condition is met and refunds you when the offer "
            "expires unmet.")
    lines += [
        'Reply with one JSON object {"orders": [...]}. Each order is an object with "type" and that action\'s '
        "fields; ?-marked fields are optional, all others are required, no other fields:",
        *("  " + sig for sig in action_signatures(acts)),
    ]
    return "\n".join(lines)


def frame_text(name: str) -> str:
    return FRAMES[name]


def frame_hash(name: str) -> str:
    return sha256(frame_text(name).encode())


def rules_hash(rs: dict) -> str:
    return sha256(rules_text(rs).encode())


def build_request(snap: dict, rs: dict, spec: dict, feedback: list[dict] | None = None) -> dict:
    """A Messages API request body for one wake. The system block (frame + rules) is the stable, cacheable
    prefix; the snapshot is the volatile user turn. `feedback` holds prior invalid attempts for a retry."""
    system = f"[FRAME]\n{frame_text(rs['frame'])}\n\n[RULES]\n{rules_text(rs)}"
    messages = [{"role": "user", "content": "[CONTEXT]\n" + json.dumps(snap, indent=1, sort_keys=True)}]
    for fb in feedback or []:
        messages.append({"role": "assistant", "content": fb["output"] or "(empty)"})
        messages.append({"role": "user", "content": "That reply was not valid orders:\n- " + "\n- ".join(fb["problems"])
                         + "\nReply again with one JSON object {\"orders\": [...]} that matches the schema."})
    body = {
        "model": spec["model"],
        "max_tokens": spec["max_tokens"],
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
        "output_config": {"effort": spec["effort"]},
    }
    # No structured output: the API's grammar compiler rejects the order schema even for the base action set
    # ("Schema is too complex"; the strict per-action form was "too large"). The rules text carries the
    # signatures, validate_orders enforces them, and an invalid reply is retried with the problems (§12).
    if spec.get("fallbacks"):
        body["fallbacks"] = spec["fallbacks"]
    return body
