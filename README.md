# Perihelion v2

A turn-based, simultaneous-move strategy game for LLM agents: eight stations on a ring at 0.50 AU, cut off
from Earth, one tick per light delay. The designer iterates on rulesets and scenarios against rollout
statistics. See `SPEC.md`.

Design principle: **the event log is the rollout.** World state, snapshots, metrics, and forks are all
derived from it.

Stdlib Python 3.12 only. No dependencies.

## Status: M0 (toy core) done

- Tick clock (191 s), ring light delay as hops, sparse wakes (arrival, `sleep_until`, heartbeat), the engine
  jumps straight to the next tick with work
- Simultaneous orders: every station's orders for a tick are recorded before any is adjudicated
- Instant actions `send`, `log`, `sleep_until`, `no_action`; infeasible or unknown orders are rejected into telemetry
- Ruleset and scenario as separate hashed JSON inputs, both copied into the genesis event
- Append-only JSONL event log per branch, sha256 hash-chained; world state rebuilt by folding events
- Fork at any wake and re-draw that one decision; every other station stays identical until its view changes
- Divergence light cone per station; health stats (wakes, reasons, order mix, rejection rate)
- Stub players `scripted` and `random`; inert relay stations (P7) that receive and forward but never act

## Commands

```bash
python3 -m engine run runs/demo --days 3                      # built-in 3-station toy, random players
python3 -m engine run runs/ring8 --scenario scenarios/ring8.json --ruleset rulesets/base.json
python3 -m engine stats runs/demo main                        # health metrics
python3 -m engine wakes runs/demo main                        # fork points
python3 -m engine fork runs/demo main <wake-id> --name alt --draw 1
python3 -m engine fork runs/demo main <wake-id> --name same --draw 0   # reproduces the parent
python3 -m engine diverge runs/demo alt                       # light cone
python3 -m engine verify runs/demo                            # hash chains + shared prefixes
python3 -m engine replay runs/demo alt [--to <event-id>]
python3 -m engine transcript runs/demo alt                    # dispatches and IMR entries
python3 -m unittest discover -s tests
```

## Layout

```
engine/
  events.py       Event record, canonical JSON, sha256, chain verification
  log.py          EventLog (JSONL per branch) and RunIndex (runs/<run>/index.json)
  rules.py        ruleset and scenario defaults, loading, hashing, tick <-> clock
  propagation.py  ring neighbors, hop distance, shortest path with severed links
  world.py        WorldState; every mutation lives in apply(event)
  snapshot.py     station-local frozen view + hash (spec §14)
  players.py      stub players, frame and rules text with hashes (spec §12)
  engine.py       the round loop and adjudication (spec §3.3, §4)
  replay.py       replay, fork, verify_run, divergence (spec §16)
  cli.py          python3 -m engine ...
rulesets/base.json   the base ruleset
scenarios/           toy3 (3 stations), ring8 (8 stations, P7 inert)
techtree.yaml        tech tree draft (loaded at M3)
tests/test_m0.py     acceptance tests for M0
runs/                output, gitignored
```

## Run layout

```
runs/<run>/index.json             config hashes, branch table (file, head, parent, fork point, draw)
runs/<run>/branches/<b>.jsonl     one event per line, hash-chained; genesis carries ruleset and scenario
runs/<run>/snapshots/<hash>.json  every snapshot a player saw, by hash
```

## Invariants the tests enforce

1. Replaying a branch yields the digest recorded in its `end_state`.
2. Two runs with the same ruleset, scenario, and seed yield identical event ids; changing the ruleset changes
   its hash and the run.
3. Within a tick, all `orders` precede any adjudication.
4. A fork shares an identical prefix with its parent through the fork wake, then a `fork` event.
5. No station's view diverges from the parent earlier than light from the forked station could reach it.
6. Any edit to a logged event breaks chain verification.
