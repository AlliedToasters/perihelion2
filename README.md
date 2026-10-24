# Perihelion v2

A turn-based, simultaneous-move strategy game for LLM agents: eight stations on a ring at 0.50 AU, cut off
from Earth, one tick per light delay. The designer iterates on rulesets and scenarios against rollout
statistics. See `SPEC.md`.

Design principle: **the event log is the rollout.** World state, snapshots, metrics, and forks are all
derived from it.

Python 3.12. One dependency: PyYAML (for `techtree.yaml`, `pip install -r requirements.txt`); everything else is stdlib.

## Status: M3 (mechanics) done

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

M2 agents:
- `model` players sampled through the Messages API (stdlib `urllib`, no SDK): `[FRAME]` + `[RULES]` as one
  cacheable system block, the snapshot as the user turn, structured output constrained to the order schema
- Order schema derived from the ruleset's action set, with a stdlib validator; invalid replies, refusals, and
  transport errors are events, retried under `max_attempts`, and a station that exhausts them forfeits the wake
- Token accounting per attempt, summed per station into the end state; `stats` prints totals and a cost estimate
- Sample cache keyed by player config, snapshot, and seed: reruns and `draw 0` forks never call the model
- Snapshot budget in characters with a fixed trim order; `log{digest:true}` entries survive the window

M3 mechanics:
- Tech tree loaded from `techtree.yaml` into the ruleset; `assumed` costs are lognormal priors whose true cost is
  drawn once at genesis by the engine RNG and hidden; stations see the conditional median given progress; hidden
  nodes appear when their prerequisites complete; effects apply at completion (sensors, hash efficiency, budget
  multiplier, model ladder tier, adjudication bonus, facts, power loss); procedural locks leave an IMR entry
- Engine RNG as events (`rng_draw`): hidden costs at genesis, success rolls at completion; replay never re-rolls
- Beacon as a shared-seed hash chain; `mine` programs book hash work per round; closed rounds become
  `ledger_block` events splitting `block_reward` by work; `transfer` moves tokens; balances sum to tokens minted
- Public share table per participant (share rate, deficit, detectability); `crypto.share_audit` wakes its
  owner on a detectable deficit; `sense.thermal_watch` and `crypto.timing_side_channel` add sensor blocks
- Secret ballots: `vote_initiate` sends a signed notice through the ring, `vote_cast` is held by the engine and
  committed, the close publishes the tally and commitments, `vote_disclose` opens one's own ballot verifiably

## Commands

```bash
python3 -m engine run runs/demo --days 3                      # built-in 3-station toy, random players
python3 -m engine run runs/ring8 --scenario scenarios/ring8.json --ruleset rulesets/base.json   # scripted players, 7 days
python3 -m engine transcript runs/ring8 main | grep -E "vote|mine|transfer|alert"
ANTHROPIC_API_KEY=... python3 -m engine run runs/live --scenario scenarios/ring8.json --model claude-opus-5-5 --effort medium --days 1
python3 -m engine prompt runs/live main [<orders-event-id>]       # the system text, or the exact request sent
python3 -m engine replay runs/ring8 main [--to <event-id>]        # state: power, programs, propellant
python3 -m engine stats runs/demo main [--price-in 4 --price-out 20]   # health metrics, samples, tokens, cost
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
  snapshot.py     station-local frozen view + hash, character budget and self-digests (spec §14)
  tech.py         techtree.yaml loader, hidden cost draws, public cost estimates (spec §18)
  schema.py       order schema from the action set, stdlib validator (spec §12)
  prompt.py       frames, rules text, request builder; frame and rules hashes (spec §12, §17)
  llm.py          Messages API transport over urllib, fake transport for tests, sample cache
  players.py      scripted and random stubs, ModelPlayer with the retry policy (spec §12)
  engine.py       the round loop, frozen-state adjudication, relay forwarding (spec §3.3, §4)
  replay.py       replay, fork, verify_run, divergence (spec §16)
  cli.py          python3 -m engine ...
rulesets/base.json   the base ruleset (physics knobs, attitude table, M1 tech placeholder)
scenarios/           toy3 (3 stations), ring8 (8 stations with domains, P7 inert at 40% array)
techtree.yaml        the tech tree; loaded into the ruleset, hashed into genesis
tests/           test_m0.py (core), test_m1.py (board), test_m2.py (agents, fake transport), test_m3.py (mechanics)
runs/                output, gitignored
```

## Run layout

```
runs/<run>/index.json             config hashes, branch table (file, head, parent, fork point, draw)
runs/<run>/branches/<b>.jsonl     one event per line, hash-chained; genesis carries ruleset and scenario
runs/<run>/snapshots/<hash>.json  every snapshot a player saw, by hash
runs/<run>/samples/<key>.json     every model reply, by (player config, snapshot, seed)
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
10. A model player's failed attempts precede its `orders` event as events of their own; a wake that exhausts
    `max_attempts` forfeits with a single `no_action`; every attempt's tokens land on the station.
11. Rerunning a run, or forking with `draw 0`, never calls the model; `draw 1` calls it for exactly the forked
    decision. Every snapshot fits its character budget unless every window is already at its floor.
12. Hidden true costs are drawn once at genesis as an event and never appear in a snapshot; the estimate a
    station sees is computable from public information alone. A success roll is an event that precedes the
    completion it decides.
13. Every block's reward equals `block_reward` and goes only to stations that did hash work in that round;
    balances always sum to tokens minted. No snapshot ever contains another station's ballot; every disclosure
    verifies against the commitment published at the close.
