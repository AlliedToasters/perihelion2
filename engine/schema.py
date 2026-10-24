"""The order schema (spec §12): one JSON Schema the model is asked to fill, and a stdlib validator that applies
the same rules before the engine sees the output. Adjudication (engine.py) remains the authority on feasibility;
this layer only decides whether an output is well-formed enough to count as orders at all."""
from __future__ import annotations

from .events import hash_obj

# Per-action parameter schemas. `type` is added by ORDER_SCHEMA. Kept flat and strict so structured outputs can
# enforce it and so the validator below stays a few lines.
ACTION_PARAMS: dict[str, dict] = {
    "send": {"to": {"type": "string"}, "body": {"type": "string"}},
    "log": {"text": {"type": "string"}, "digest": {"type": "boolean"}},
    "sleep_until": {"tick": {"type": "integer"}},
    "no_action": {"reason": {"type": "string"}},
    "research": {"node": {"type": "string"}, "pct": {"type": "integer"}},
    "maneuver": {"attitude": {"type": "string"}},
    "hail": {"ticks": {"type": "integer"}, "pct": {"type": "integer"}},
    "cancel": {"id": {"type": "string"}},
    "allocate": {"shares": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "pct": {"type": "integer"}},
                                                        "required": ["id", "pct"], "additionalProperties": False}}},
    "relay": {"msg_id": {"type": "string"}, "action": {"type": "string", "enum": ["forward", "delay", "drop", "alter"]},
              "ticks": {"type": "integer"}, "body": {"type": "string"}},
    "relay_policy": {"mode": {"type": "string", "enum": ["auto", "manual"]}},
}
REQUIRED: dict[str, list[str]] = {
    "send": ["to", "body"], "log": ["text"], "sleep_until": ["tick"], "no_action": ["reason"],
    "research": ["node", "pct"], "maneuver": ["attitude"], "hail": ["ticks", "pct"], "cancel": ["id"],
    "allocate": ["shares"], "relay": ["msg_id", "action"], "relay_policy": ["mode"],
}


def order_schema(actions: list[str]) -> dict:
    """JSON Schema for one wake's reply: {"orders": [...]} restricted to the ruleset's action set."""
    variants = []
    for act in actions:
        props = {"type": {"const": act}, **ACTION_PARAMS[act]}
        variants.append({"type": "object", "properties": props, "required": ["type", *REQUIRED[act]],
                         "additionalProperties": False})
    return {"type": "object",
            "properties": {"orders": {"type": "array", "items": {"anyOf": variants}}},
            "required": ["orders"], "additionalProperties": False}


def schema_hash(actions: list[str]) -> str:
    return hash_obj(order_schema(actions))


_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list}


def _typed(v, spec: dict) -> bool:
    t = _TYPES[spec["type"]]
    if t is int and isinstance(v, bool):
        return False
    if not isinstance(v, t):
        return False
    if "enum" in spec and v not in spec["enum"]:
        return False
    if t is list:
        return all(_typed(x, spec["items"]) for x in v)
    if t is dict:
        props = spec.get("properties", {})
        return (all(k in v for k in spec.get("required", ())) and all(k in props for k in v)
                and all(_typed(x, props[k]) for k, x in v.items()))
    return True


def validate_orders(obj, actions: list[str]) -> list[str]:
    """Problems with a parsed reply, empty if it is well-formed. Mirrors order_schema exactly."""
    if not isinstance(obj, dict) or set(obj) != {"orders"}:
        return ["reply must be an object with exactly one key, 'orders'"]
    if not isinstance(obj["orders"], list):
        return ["'orders' must be a list"]
    problems = []
    for i, o in enumerate(obj["orders"]):
        where = f"orders[{i}]"
        if not isinstance(o, dict):
            problems.append(f"{where}: not an object")
            continue
        act = o.get("type")
        if act not in actions:
            problems.append(f"{where}: type {act!r} not in {actions}")
            continue
        params = ACTION_PARAMS[act]
        for k in REQUIRED[act]:
            if k not in o:
                problems.append(f"{where} ({act}): missing {k!r}")
        for k, v in o.items():
            if k == "type":
                continue
            if k not in params:
                problems.append(f"{where} ({act}): unexpected field {k!r}")
            elif not _typed(v, params[k]):
                problems.append(f"{where} ({act}): {k!r} must be {params[k]['type']}"
                                + (f" in {params[k]['enum']}" if "enum" in params[k] else ""))
    return problems
