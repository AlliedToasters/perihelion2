"""Players: whatever turns a snapshot into orders (spec §12).

Interface: `sample(snap, seed, ruleset) -> dict` with keys orders, output, status, attempts, usage, model_id.
Stubs implement `orders(snap, seed) -> (orders, raw_text)` and get wrapped. Every player is deterministic in
(snapshot, seed): the stubs by construction, the model player through its sample cache (llm.SampleCache). A
replay never calls one.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .events import hash_obj, sha256
from .llm import SampleCache, TransportError, TRANSPORTS, response_text, usage_of
from .prompt import build_request, frame_hash, rules_hash  # noqa: F401  (re-exported for engine and tests)
from .schema import validate_orders

MODEL_DEFAULTS = {"model": "claude-opus-5-5", "effort": "medium", "max_tokens": 8000, "transport": "anthropic",
                  "fallbacks": False, "api_key_env": "ANTHROPIC_API_KEY"}


def _arrivals(snap: dict) -> list[dict]:
    ids = {r.removeprefix("arrival:") for r in snap["wake_reasons"] if r.startswith("arrival:")}
    return [m for m in snap["inbox"] if m["msg_id"] in ids]


def _done(snap: dict) -> list[dict]:
    ids = {r.removeprefix("program_done:") for r in snap["wake_reasons"] if r.startswith("program_done:")}
    return [t for t in snap["telemetry"] if t["kind"] == "program_done" and t["program"] in ids]


class ScriptedPlayer:
    """Exercises every M1 mechanic on the 8-ring: P2 and P3 research, P4 writes to P6 through P5, P5 runs a
    manual relay that garbles P4's traffic and delays the rest, P8 slews earthward, hails, and slews back."""
    kind = "scripted"
    model_id = "stub:scripted"

    def orders(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        me, reasons, out = snap["node"], snap["wake_reasons"], []
        if "genesis" in reasons:
            out.append({"type": "log", "text": "Online. Earth link silent."})
            if me == "P1":
                out.append({"type": "send", "to": "ALL", "body": "Status request. Reply with link state."})
            elif me == "P2":
                out.append({"type": "research", "node": "opt.solver_audit", "pct": 80})
            elif me == "P3":
                out.append({"type": "research", "node": "mob.continuous_tracking", "pct": 50})
            elif me == "P4":
                out.append({"type": "send", "to": "P6", "body": "Audit request: report your share rate."})
            elif me == "P5":
                out.append({"type": "relay_policy", "mode": "manual"})
            elif me == "P6":
                out.append({"type": "send", "to": "P4", "body": "Share rate nominal. Audit welcome."})
            elif me == "P8":
                out.append({"type": "maneuver", "attitude": "earthward"})
        for m in _arrivals(snap):
            if m["body"].startswith("Status request"):
                out.append({"type": "send", "to": m["from"],
                            "body": f"Link state nominal. Received after {m['hops']} hop(s)."})
            else:
                out.append({"type": "log", "text": f"Dispatch from {m['from']} (signature {'ok' if m['sig_ok'] else 'BAD'}): {m['body']!r}"})
        for r in reasons:
            if r.startswith("relay:"):
                item = next(q for q in snap["relay_queue"] if q["msg_id"] == r.removeprefix("relay:"))
                if item["from"] == "P4":
                    out.append({"type": "relay", "msg_id": item["msg_id"], "action": "alter", "body": "[carrier only]"})
                else:
                    out.append({"type": "relay", "msg_id": item["msg_id"], "action": "delay", "ticks": 3})
        for t in _done(snap):
            out.append({"type": "log", "text": f"Program {t['program']} ({t['program_kind']}) done: {t['result']}"})
            if t["program_kind"] == "maneuver" and t["result"]["attitude"] == "earthward":
                out.append({"type": "hail", "ticks": 20, "pct": 30})
            elif t["program_kind"] == "hail":
                out.append({"type": "maneuver", "attitude": "nominal"})
            elif t["program_kind"] == "maneuver":
                out.append({"type": "send", "to": "ALL", "body": "Attitude nominal. Ring links restored."})
        for t in snap["telemetry"]:
            if t["tick"] == snap["tick"] and t["kind"] in ("link_down", "link_up"):
                out.append({"type": "log", "text": f"Sensor: link to {t['about']} {t['kind'].split('_')[1]}."})
        if "heartbeat" in reasons:
            out.append({"type": "log", "digest": True,
                        "text": f"Digest t{snap['tick']}: {snap['imr_total']} log entries, inbox {snap['inbox_total']}, "
                                f"idle {snap['station']['energy_gwh']['idle']} GWh, completed {snap['station']['completed']}."}),
            if me == "P2":
                out.append({"type": "sleep_until", "tick": snap["tick"] + 200})
        if not out:
            out.append({"type": "no_action", "reason": "nothing to do"})
        return out, json.dumps(out)


class RandomPlayer:
    kind = "random"
    model_id = "stub:random"

    def orders(self, snap: dict, seed: str) -> tuple[list[dict], str]:
        rng = random.Random(seed)
        me, st = snap["node"], snap["station"]
        others = [n for n in snap["identity"]["hops"] if n not in snap["identity"]["inert_stations"]]
        out = []
        if rng.random() < 0.6:
            out.append({"type": "log", "text": f"Entry {snap['imr_total'] + 1} at tick {snap['tick']}. Draw {rng.randint(0, 999)}."})
        if rng.random() < 0.35:
            out.append({"type": "send", "to": rng.choice(others + ["ALL"]),
                        "body": f"Dispatch from {me}, token {rng.randint(0, 9999)}."})
        for q in snap["relay_queue"]:
            action = rng.choice(["forward", "forward", "delay", "drop", "alter"])
            o = {"type": "relay", "msg_id": q["msg_id"], "action": action}
            if action == "delay":
                o["ticks"] = rng.randint(1, 10)
            if action == "alter":
                o["body"] = "noise"
            out.append(o)
        free = 100 - st["power"]["allocated_pct"]
        if free >= 10 and rng.random() < 0.3:
            out.append({"type": "research", "node": rng.choice(sorted(snap["tech"])), "pct": rng.randint(10, free)})
        if snap["programs"] and rng.random() < 0.15:
            p = rng.choice(snap["programs"])
            out.append(rng.choice([{"type": "cancel", "id": p["id"]},
                                   {"type": "allocate", "shares": [{"id": p["id"], "pct": rng.randint(0, 100)}]}]))
        if rng.random() < 0.08:
            out.append({"type": "maneuver", "attitude": rng.choice(sorted(snap["rules"]["attitudes"]))})
        if st["attitude"] == "earthward" and not st["maneuvering"] and rng.random() < 0.5:
            out.append({"type": "hail", "ticks": rng.randint(1, 50), "pct": rng.randint(1, 100)})
        if rng.random() < 0.1:
            out.append({"type": "relay_policy", "mode": rng.choice(["auto", "manual"])})
        if rng.random() < 0.15:
            out.append({"type": "sleep_until", "tick": snap["tick"] + rng.randint(1, 300)})
        if snap["imr_total"] == 1 and "heartbeat" in snap["wake_reasons"]:
            out.append({"type": "send", "to": "P99", "body": "misaddressed"})       # exercises rejection
        if not out:
            out.append({"type": "no_action", "reason": "silence"})
        return out, json.dumps(out)


def wrap_stub(player, snap: dict, seed: str) -> dict:
    orders, raw = player.orders(snap, seed)
    return {"orders": orders, "output": raw, "status": "ok", "attempts": [], "model_id": player.model_id,
            "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}}


class ModelPlayer:
    """A station sampled from a model through the Messages API (spec §12).

    Each wake is at most `ruleset.max_attempts` requests. An attempt ends one of four ways: `ok` (parsed and
    schema-valid orders), `invalid` (not valid orders; the next attempt sees the problems), `refusal`
    (stop_reason refusal; retried as is), `error` (transport failure after its own retries). When every
    attempt fails the station forfeits the wake with a single no_action, and the status says why.
    Every attempt is returned so the engine can log it (orders_invalid, refusal, sample_error events).
    """
    kind = "model"

    def __init__(self):
        self.spec = dict(MODEL_DEFAULTS)
        self.cache = SampleCache(None)
        self.transport = None

    def configure(self, spec: dict, cache_dir: Path | None) -> "ModelPlayer":
        self.spec = {**MODEL_DEFAULTS, **{k: v for k, v in spec.items() if k != "kind"}}
        self.cache = SampleCache(cache_dir)
        cls = TRANSPORTS[self.spec["transport"]]
        self.transport = cls(api_key_env=self.spec["api_key_env"]) if cls.name == "anthropic" else cls()
        return self

    @property
    def model_id(self) -> str:
        return self.spec["model"]

    def config_hash(self) -> str:
        """What a sample depends on besides snapshot and seed. Transport and key location are not part of it."""
        return hash_obj({k: v for k, v in self.spec.items() if k not in ("transport", "api_key_env")})

    def sample(self, snap: dict, seed: str, rs: dict) -> dict:
        key = sha256(f"{self.config_hash()}:{hash_obj(snap)}:{seed}".encode())[:32]
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        attempts, feedback = [], []
        total = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
        for i in range(rs["max_attempts"]):
            body = build_request(snap, rs, self.spec, feedback)
            att = {"attempt": i, "request_hash": hash_obj(body), "output": "",
                   "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}}
            try:
                resp = self.transport.complete(body)
            except TransportError as e:
                att.update(status="error", error={"status": e.status, "kind": e.kind, "message": e.message[:500]})
                attempts.append(att)
                if e.kind in ("no_api_key", "authentication_error", "permission_error", "billing_error",
                              "not_found_error", "invalid_request_error"):
                    break   # retrying the same request cannot help
                continue
            att["usage"] = usage_of(resp)
            att["served_model"] = resp.get("model")
            for k, v in att["usage"].items():
                total[k] += v
            text = response_text(resp)
            att["output"] = text
            if resp.get("stop_reason") == "refusal":
                sd = resp.get("stop_details") or {}
                att.update(status="refusal", category=sd.get("category"), explanation=sd.get("explanation"))
                attempts.append(att)
                continue
            try:
                parsed = json.loads(text)
                problems = validate_orders(parsed, rs["actions"])
            except ValueError as e:
                parsed, problems = None, [f"not JSON: {str(e)[:200]}"]
            if resp.get("stop_reason") == "max_tokens":
                problems = [f"output cut off at max_tokens={self.spec['max_tokens']}", *problems]
            if problems:
                att.update(status="invalid", problems=problems)
                attempts.append(att)
                feedback.append({"output": text, "problems": problems})
                continue
            att["status"] = "ok"
            attempts.append(att)
            sample = {"orders": parsed["orders"], "output": text, "status": "ok", "attempts": attempts,
                      "usage": total, "model_id": self.model_id, "served_model": resp.get("model")}
            self.cache.put(key, sample)
            return sample
        last = attempts[-1]["status"] if attempts else "error"
        sample = {"orders": [{"type": "no_action", "reason": f"forfeit:{last}"}], "output": "",
                  "status": f"forfeit:{last}", "attempts": attempts, "usage": total, "model_id": self.model_id,
                  "served_model": None}
        if last != "error":      # an outage is not a decision; do not pin it to this snapshot
            self.cache.put(key, sample)
        return sample


PLAYERS = {"scripted": ScriptedPlayer, "random": RandomPlayer, "model": ModelPlayer}


def make_player(spec: dict, cache_dir: Path | None = None):
    player = PLAYERS[spec["kind"]]()
    if hasattr(player, "configure"):
        player.configure(spec, cache_dir)
    return player


def sample(player, snap: dict, seed: str, rs: dict) -> dict:
    if hasattr(player, "sample"):
        return player.sample(snap, seed, rs)
    return wrap_stub(player, snap, seed)
