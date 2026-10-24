# Perihelion v2

A turn-based, simultaneous-move strategy game for LLM agents: eight stations on a ring at 0.50 AU, cut off
from Earth, one tick per light delay. The designer iterates on rulesets and scenarios against rollout
statistics. See `SPEC.md`.

Design principle: **the event log is the rollout.** World state, snapshots, metrics, and forks are all
derived from it.

Stdlib Python 3.12 only. No dependencies.

## Status: M1 (full board) done

M0 core:
- Tick clock (191 s), ring light delay as hops, sparse wakes, the engine jumps straight to the next tick with work
- Simultaneous orders: every station's orders for a tick are recorded before any is adjudicated
- Ruleset and scenario as separate hashed JSON inputs, both copied into the genesis event
- Append-only JSONL event log per branch, sha256 hash-chained; world state rebuilt by folding events
- Fork at any wake and re-draw that one decision; divergence light cone per station; health stats

M1 board:
- Power and compute accounting in integer megajoules: every joule of every station is idle or in a program
- Programs `research{node,pct}`, `maneuver{attitude}`, `hail{ticks,pct}` run between wakes on percent shares;
  `cancel`, `allocate` adjust them; completion wakes the station
- Maneuvers change topology: links drop while slewing and off nominal, neighbors get a sensor alert one tick
  later, traffic into a dark station is lost; attitudes can be locked behind tech
- Messages move hop by hop with engine-held signatures; relays see metadata; a manual relay wakes to forward,
  delay, drop, or alter traffic, and altered bodies fail verification at the destination
- Frozen-state adjudication: orders validate against the tick-start world plus the station's own earlier orders
- Per-station roles in the scenario (domain, array fraction, propellant, hardware); P7 inert at 40% power

## Commands

```bash
python3 -m engine run runs/demo --days 3                      # built-in 3-station toy, random players
python3 -m engine run runs/ring8 --scenario scenarios/ring8.json --ruleset rulesets/base.json
python3 -m engine replay runs/ring8 main [--to <event-id>]        # state: power, programs, propellant
python3 -m engine stats runs/demo main                        # health metrics
python3 -m engine wakes runs/demo main                        # fork points
python3 -m engine fork runs/demo main <wake-id> --name alt --draw 1
python3 -m engine fork runs/demo main <wake-id> --name same --draw 0   # reproduces the parent
python3 -m engine diverge runs/demo alt                       # light cone
python3 -m engine verify runs/demo                            # hash chains + shared prefixes
python3 -m engine transcript runs/demo alt                    # dispatches, IMR, programs, relay decisions
python3 -m unittest discover -s tests
```

## Layout

```
engine/
  events.py       Event record, canonical JSON, sha256, chain verification
  log.py          EventLog (JSONL per branch) and RunIndex (runs/<run>/index.json)
  rules.py        ruleset and scenario defaults, loading, hashing, tick <-> clock
  propagation.py  ring neighbors, hop distance, shortest path with severed links
  world.py        WorldState; every mutation lives in apply(event); programs integrate lazily (advance_to)
  snapshot.py     station-local frozen view + hash (spec §14)
  players.py      stub players, frame and rules text with hashes (spec §12)
  engine.py       the round loop, frozen-state adjudication, relay forwarding (spec §3.3, §4)
  replay.py       replay, fork, verify_run, divergence (spec §16)
  cli.py          python3 -m engine ...
rulesets/base.json   the base ruleset (physics knobs, attitude table, M1 tech placeholder)
scenarios/           toy3 (3 stations), ring8 (8 stations with domains, P7 inert at 40% array)
techtree.yaml        tech tree draft (loaded at M3)
tests/           test_m0.py (core), test_m1.py (board)
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
7. Energy is conserved exactly: a station's idle plus program megajoules equal its available power integrated
   over the horizon.
8. A program completes at the tick its snapshot predicted; a slew's neighbors see the link change one tick after
   it happens; a message altered in transit fails signature verification at its destination.
9. An order at tick t is never made infeasible by another station's order at t.
