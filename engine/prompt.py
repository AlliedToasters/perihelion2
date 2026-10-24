"""Prompt frames and rules text for model-backed players (spec §12): [FRAME] fixed versioned text, [RULES] the
order schema and constraints derived from the ruleset, [CONTEXT] the canonical snapshot. Frame and rules are
hashed on every sample so a decision can be audited against exactly the words the model saw (spec §17)."""
from __future__ import annotations

import json

from .events import sha256
from .schema import order_schema

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
        f"{rs['inbox_window']} dispatches; older material is gone unless you digested it. The engine never "
        "summarizes for you.",
        "Reply schema:",
        json.dumps(order_schema(acts), sort_keys=True, separators=(",", ":")),
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
        "output_config": {"effort": spec["effort"],
                          "format": {"type": "json_schema", "schema": order_schema(rs["actions"])}},
    }
    if spec.get("fallbacks"):
        body["fallbacks"] = spec["fallbacks"]
    return body
