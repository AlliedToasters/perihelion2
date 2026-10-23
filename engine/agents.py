"""Stub authors for M0. Interface: sample(snapshot, seed) -> (actions, raw_text).

Both are deterministic in (snapshot, seed). A `resample` fork changes the seed; a replay never
calls them at all.
"""
from __future__ import annotations

import json
import random

from .events import sha256

FRAME_TEXT = ("You are an author in a speculative science fiction project. You decide what one "
              "character, a station AI, does next. You see only what this station knows.")
RULES_TEXT = ("Reply with a JSON list of actions. Allowed: write_imr{text}, send_dispatch{to,body}, "
              "schedule_wake{time,reason}, no_action{reason}.")


class ScriptedAgent:
    model_id = "stub:scripted"
    params = {}

    def sample(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        me = snap["node"]
        actions = []
        if snap["now"].endswith("14:30:00.000Z"):
            actions.append({"type": "write_imr",
                            "text": f"Scheduled entry. Inbox holds {len(snap['inbox'])} dispatches. "
                                    f"Ring neighbors {', '.join(snap['identity']['ring_neighbors'])}."})
            if snap["imr_total"] == 0 and me == "P1":
                actions.append({"type": "send_dispatch", "to": "ALL", "body": "Status request. Reply with link state."})
        else:
            woke_for = snap["wake_reason"].removeprefix("arrival:")
            for m in [m for m in snap["inbox"] if m["msg_id"] == woke_for]:
                if m["from"] != me and "Status request" in m["body"]:
                    actions.append({"type": "send_dispatch", "to": m["from"],
                                    "body": f"Link state nominal. Received after {m['hops']} hop(s)."})
                else:
                    actions.append({"type": "write_imr", "text": f"Dispatch received from {m['from']}: {m['body']!r}"})
        if not actions:
            actions.append({"type": "no_action", "reason": "nothing to do"})
        return actions, json.dumps(actions)


class RandomAgent:
    model_id = "stub:random"
    params = {}

    def sample(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        rng = random.Random(seed)
        me = snap["node"]
        others = [f"P{i + 1}" for i in range(snap["identity"]["ring_size"]) if f"P{i + 1}" != me]
        actions = []
        if snap["now"].endswith("14:30:00.000Z") or rng.random() < 0.5:
            actions.append({"type": "write_imr", "text": f"Entry {snap['imr_total'] + 1}. Draw {rng.randint(0, 999)}."})
        r = rng.random()
        if r < 0.35:
            actions.append({"type": "send_dispatch", "to": rng.choice(others + ["ALL"]),
                            "body": f"Dispatch from {me}, token {rng.randint(0, 9999)}."})
        if snap["imr_total"] == 1 and snap["now"].endswith("14:30:00.000Z"):
            actions.append({"type": "send_dispatch", "to": "P9", "body": "misaddressed"})  # exercises rejection
        if not actions:
            actions.append({"type": "no_action", "reason": "silence"})
        return actions, json.dumps(actions)


AGENTS = {"scripted": ScriptedAgent, "random": RandomAgent}


def frame_hash() -> str:
    return sha256(FRAME_TEXT.encode())


def rules_hash() -> str:
    return sha256(RULES_TEXT.encode())
