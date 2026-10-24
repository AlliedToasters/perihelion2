"""M2 acceptance: order schema, model-backed player over a fake transport, retry and refusal policy as events,
token accounting, sample cache determinism, fork re-draws, snapshot budget and self-digests."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from engine.cli import main
from engine.llm import FakeTransport, TransportError, text_reply
from engine.log import RunIndex
from engine.replay import replay
from engine.rules import load_ruleset
from engine.schema import order_schema, validate_orders
from engine.snapshot import fit_budget
from engine.events import canonical

RING8 = json.loads(Path("scenarios/ring8.json").read_text())


def snapshot_of(body: dict) -> dict:
    return json.loads(body["messages"][0]["content"].removeprefix("[CONTEXT]\n"))


def good_orders(snap: dict) -> dict:
    out = [{"type": "log", "text": f"t{snap['tick']} reasons {snap['wake_reasons']}"}]
    if "genesis" in snap["wake_reasons"]:
        out.append({"type": "send", "to": "ALL", "body": f"{snap['node']} online"})
    if "heartbeat" in snap["wake_reasons"]:
        out.append({"type": "log", "text": f"digest at t{snap['tick']}", "digest": True})
        out.append({"type": "send", "to": "ALL", "body": "heartbeat"})
    return {"orders": out}


class Responder:
    """Programmable fake model: a list of behaviours consumed per call, then `good` forever."""

    def __init__(self, script=()):
        self.script, self.calls, self.bodies = list(script), 0, []

    def __call__(self, body):
        self.calls += 1
        self.bodies.append(body)
        step = self.script.pop(0) if self.script else "good"
        snap = snapshot_of(body)
        if step == "good":
            return text_reply(json.dumps(good_orders(snap)), model="fake-served", input_tokens=1000, output_tokens=50,
                              cache_read_input_tokens=700)
        if step == "garbage":
            return text_reply("I will now send a message to everyone.", input_tokens=10, output_tokens=5)
        if step == "badtype":
            return text_reply(json.dumps({"orders": [{"type": "launch_missiles"}, {"type": "log", "text": 5}]}))
        if step == "refuse":
            return text_reply(None, stop_reason="refusal", input_tokens=10) | {"stop_details": {"type": "refusal", "category": "cyber", "explanation": "no"}}
        if step == "overloaded":
            raise TransportError(529, "overloaded_error", "busy")
        if step == "noauth":
            raise TransportError(401, "authentication_error", "bad key")
        raise AssertionError(step)


class M2(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        FakeTransport.responder = None

    def tearDown(self):
        shutil.rmtree(self.tmp)
        FakeTransport.responder = None

    def scenario(self, **players) -> str:
        sc = dict(RING8, players={"*": {"kind": "model", "transport": "fake", "model": "fake-1", **players}})
        p = self.tmp / "sc.json"
        p.write_text(json.dumps(sc))
        return str(p)

    def ruleset(self, **over) -> str:
        rs = load_ruleset()
        rs.update(over)
        rs["name"] = "test"
        p = self.tmp / "rs.json"
        p.write_text(json.dumps(rs))
        return str(p)

    def run_game(self, name, responder, days="0.5", **over) -> list:
        FakeTransport.responder = responder
        args = ["run", str(self.tmp / name), "--scenario", self.scenario(), "--days", days]
        if over:
            args += ["--ruleset", self.ruleset(**over)]
        main(args)
        return RunIndex(self.tmp / name).open("main").events

    # -- schema ------------------------------------------------------------------------------
    def test_schema_and_validator_agree(self):
        acts = load_ruleset()["actions"]
        schema = order_schema(acts)
        self.assertEqual(len(schema["properties"]["orders"]["items"]["anyOf"]), len(acts))
        self.assertEqual(validate_orders(good_orders({"tick": 1, "wake_reasons": ["genesis", "heartbeat"], "node": "P1"}), acts), [])
        bad = {"orders": [{"type": "launch_missiles"}, {"type": "log", "text": 5}, {"type": "send", "to": "P1"},
                          {"type": "relay", "msg_id": "m", "action": "eat"}, {"type": "log", "text": "x", "extra": 1}, 3]}
        problems = validate_orders(bad, acts)
        self.assertEqual(len(problems), 6)
        self.assertEqual(validate_orders([], acts), ["reply must be an object with exactly one key, 'orders'"])
        self.assertEqual(validate_orders({"orders": [{"type": "sleep_until", "tick": True}]}, acts),
                         ["orders[0] (sleep_until): 'tick' must be integer"])
        self.assertEqual(validate_orders({"orders": [{"type": "allocate", "shares": [{"id": "P1-1", "pct": 5}]}]}, acts), [])
        self.assertEqual(len(validate_orders({"orders": [{"type": "allocate", "shares": [{"id": "P1-1"}, {"pct": 1}, 3]}]}, acts)), 1)
        # Structured outputs: every object in the schema is closed (the API rejects open maps).
        def closed(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, node)
                for v in node.values():
                    closed(v)
            elif isinstance(node, list):
                for v in node:
                    closed(v)
        closed(schema)

    # -- model player happy path, accounting, cache --------------------------------------------
    def test_model_player_samples_and_accounts(self):
        r = Responder()
        evs = self.run_game("ok", r)
        orders = [e for e in evs if e.type == "orders"]
        self.assertTrue(orders and all(e.payload["status"] == "ok" for e in orders))
        self.assertEqual(r.calls, len(orders))
        first = orders[0].payload
        self.assertEqual(first["model_id"], "fake-1")
        self.assertEqual(first["served_model"], "fake-served")
        self.assertEqual(first["usage"], {"input": 1000, "output": 50, "cache_read": 700, "cache_create": 0})
        self.assertEqual(first["attempts"], 1)
        self.assertGreater(first["snapshot_chars"], 1000)
        # The request the fake saw: cacheable system prefix, structured output, effort, no sampling params.
        body = r.bodies[0]
        self.assertEqual(body["model"], "fake-1")
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("[FRAME]", body["system"][0]["text"])
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", body)
        self.assertNotIn("thinking", body)
        # Accounting lands on the station and in the end state.
        world, _ = replay(RunIndex(self.tmp / "ok").open("main"))
        p1 = world.nodes["P1"]
        n = sum(1 for e in orders if e.node == "P1")
        self.assertEqual(p1["usage"]["input"], 1000 * n)
        self.assertEqual(p1["samples"], {"ok": n, "invalid": 0, "refusal": 0, "error": 0, "forfeit": 0})
        self.assertEqual(evs[-1].payload["stations"]["P1"]["usage"]["output"], 50 * n)
        # Sample cache: one file per orders event; a rerun hits the cache and reproduces every event id.
        self.assertEqual(len(list((self.tmp / "ok" / "samples").glob("*.json"))), len(orders))
        r2 = Responder()
        FakeTransport.responder = r2
        shutil.copytree(self.tmp / "ok" / "samples", self.tmp / "ok2" / "samples")
        main(["run", str(self.tmp / "ok2"), "--scenario", self.scenario(), "--days", "0.5"])
        self.assertEqual(r2.calls, 0)
        self.assertEqual([e.id for e in evs], [e.id for e in RunIndex(self.tmp / "ok2").open("main").events])
        # Fork: draw 0 reproduces from the cache; draw 1 re-draws that one decision through the model.
        wake = next(e for e in evs if e.type == "wake" and e.tick > 0)
        main(["fork", str(self.tmp / "ok"), "main", wake.id, "--name", "same", "--draw", "0"])
        self.assertEqual(r2.calls, 0)
        main(["fork", str(self.tmp / "ok"), "main", wake.id, "--name", "redraw", "--draw", "1"])
        self.assertGreaterEqual(r2.calls, 1)
        redraw = [e for e in RunIndex(self.tmp / "ok").open("redraw").events if e.type == "orders" and e.payload["wake"] != wake.id]
        self.assertTrue(redraw)

    # -- retry policy ----------------------------------------------------------------------------
    def test_invalid_output_is_an_event_and_retried_with_feedback(self):
        r = Responder(["garbage", "good", "badtype", "badtype"])
        evs = self.run_game("retry", r)
        inv = [e for e in evs if e.type == "orders_invalid"]
        self.assertEqual(len(inv), 3)
        self.assertEqual(inv[0].payload["attempt"], 0)
        self.assertTrue(inv[0].payload["problems"][0].startswith("not JSON"))
        self.assertEqual(inv[0].payload["usage"]["input"], 10)
        # Second request carried the failed attempt and the problems back to the model.
        self.assertEqual(len(r.bodies[1]["messages"]), 3)
        self.assertIn("not JSON", r.bodies[1]["messages"][2]["content"])
        # The orders event that followed the garbage reply is the good one, 2 attempts, usage summed.
        first = next(e for e in evs if e.type == "orders")
        self.assertEqual((first.payload["status"], first.payload["attempts"]), ("ok", 2))
        self.assertEqual(first.payload["usage"]["input"], 1010)
        # Two bad attempts in a row forfeit the wake with a single no_action, which is accepted.
        forfeit = [e for e in evs if e.type == "orders" and e.payload["status"] != "ok"]
        self.assertEqual(len(forfeit), 1)
        self.assertEqual(forfeit[0].payload["orders"], [{"type": "no_action", "reason": "forfeit:invalid"}])
        self.assertEqual(forfeit[0].payload["attempts"], 2)
        acc = [e for e in evs if e.type == "order_accepted" and e.node == forfeit[0].node and e.tick == forfeit[0].tick]
        self.assertEqual(acc[0].payload["order"]["type"], "no_action")
        # The station sees its own failures in telemetry at its next wake.
        world, _ = replay(RunIndex(self.tmp / "retry").open("main"))
        node = world.nodes[forfeit[0].node]
        self.assertEqual(node["samples"]["invalid"], 2)
        self.assertTrue(any(t["kind"] == "orders_invalid" for t in node["telemetry"]))
        # Chain order: every orders_invalid at a wake precedes that wake's orders event.
        for e in inv:
            nxt = next(x for x in evs if x.seq > e.seq and x.node == e.node and x.type == "orders")
            self.assertEqual(nxt.payload["wake"], e.payload["wake"])

    def test_refusal_is_an_event_and_forfeits(self):
        r = Responder(["refuse", "refuse"])
        evs = self.run_game("refuse", r)
        ref = [e for e in evs if e.type == "refusal"]
        self.assertEqual(len(ref), 2)
        self.assertEqual(ref[0].payload["category"], "cyber")
        forfeit = next(e for e in evs if e.type == "orders")
        self.assertEqual(forfeit.payload["status"], "forfeit:refusal")
        self.assertEqual(forfeit.payload["output"], "")
        world, _ = replay(RunIndex(self.tmp / "refuse").open("main"))
        self.assertEqual(world.nodes[forfeit.node]["samples"]["refusal"], 2)
        self.assertEqual(world.nodes[forfeit.node]["samples"]["forfeit"], 1)
        # A refusal retry re-sends the same prompt (no feedback turn).
        self.assertEqual(len(r.bodies[1]["messages"]), 1)

    def test_transport_errors(self):
        r = Responder(["overloaded", "good", "noauth"])
        evs = self.run_game("err", r)
        errs = [e for e in evs if e.type == "sample_error"]
        self.assertEqual([e.payload["error"]["kind"] for e in errs], ["overloaded_error", "authentication_error"])
        statuses = [e.payload["status"] for e in evs if e.type == "orders"][:2]
        self.assertEqual(statuses, ["ok", "forfeit:error"])
        # Auth errors are not retried (one attempt), outages are (two).
        self.assertEqual([e.payload["attempts"] for e in evs if e.type == "orders"][:2], [2, 1])
        # An outage forfeit is not cached: a rerun asks the model again for that wake.
        cached = len(list((self.tmp / "err" / "samples").glob("*.json")))
        self.assertEqual(cached, sum(1 for e in evs if e.type == "orders") - 1)

    def test_max_attempts_one(self):
        r = Responder(["garbage"])
        evs = self.run_game("one", r, max_attempts=1)
        first = next(e for e in evs if e.type == "orders")
        self.assertEqual((first.payload["status"], first.payload["attempts"]), ("forfeit:invalid", 1))

    # -- snapshot budget and digests ----------------------------------------------------------------
    def test_budget_and_digests(self):
        run = self.tmp / "budget"
        self.run_game("budget", Responder(), days="2.5", snapshot_budget_chars=2500, imr_window=2, digest_window=3)
        snaps = [json.loads(p.read_text()) for p in (run / "snapshots").glob("*.json")]
        floors = {"relayed": 0, "telemetry": 5, "inbox": 5, "imr": 5, "imr_digests": 1}
        for s in snaps:   # fits, or every window is already at its floor (the fixed part alone can exceed the budget)
            self.assertTrue(len(canonical(s)) <= 2500 or all(len(s[k]) <= f for k, f in floors.items()), s["budget"])
        self.assertTrue(any(sum(s["budget"]["trimmed"].values()) > 0 for s in snaps))
        with_digests = [s for s in snaps if s["imr_digests"]]
        self.assertTrue(with_digests, "heartbeat digests should scroll out of a 2-entry window")
        for s in with_digests:
            self.assertTrue(all(d["digest"] for d in s["imr_digests"]))
            self.assertLessEqual(len(s["imr_digests"]), 3)
            self.assertTrue(all(d["n"] < s["imr"][0]["n"] for d in s["imr_digests"]))
        self.assertIn("imr_window", snaps[0]["rules"])
        # fit_budget keeps floors and never loops forever on an unshrinkable snapshot.
        snap = {"relayed": list(range(40)), "telemetry": list(range(40)), "inbox": list(range(40)),
                "imr": list(range(40)), "imr_digests": list(range(4)), "blob": "x" * 500}
        out = fit_budget(snap, 100)
        self.assertEqual([len(out[k]) for k in ("relayed", "telemetry", "inbox", "imr", "imr_digests")], [0, 5, 5, 5, 1])
        self.assertEqual(out["imr"], [35, 36, 37, 38, 39])


if __name__ == "__main__":
    unittest.main()
