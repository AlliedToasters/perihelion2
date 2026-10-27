# Perihelion v2

A turn-based, simultaneous-move strategy game for LLM agents: eight stations on a ring at 0.50 AU, cut off
from Earth, one tick per light delay. The designer iterates on rulesets and scenarios against rollout
statistics. See `SPEC.md`.

Design principle: **the event log is the rollout.** World state, snapshots, metrics, and forks are all
derived from it.

Python 3.12. One dependency: PyYAML (for `techtree.yaml`, `pip install -r requirements.txt`); everything else is stdlib.

## Status: M6 (first balance pass) pilot running on Opus 4.8; analytics page added

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
- Beacon as a shared-seed hash chain; `mine` programs (economy ruleset) book hash work per round; closed rounds become
  `ledger_block` events splitting `block_reward` by work; `transfer` moves tokens; balances sum to tokens minted
- Public share table per participant (share rate, deficit, detectability); `crypto.share_audit` wakes its
  owner on a detectable deficit; `sense.thermal_watch` and `crypto.timing_side_channel` add sensor blocks
- Secret ballots: `vote_initiate` sends a signed notice through the ring, `vote_cast` is held by the engine and
  committed, the close publishes the tally and commitments, `vote_disclose` opens one's own ballot verifiably

M4 objectives and contracts:
- Objectives are scenario initial conditions: per station, scored items (five named scoring functions) and
  directives (in-world artifacts with a sender, a date, and a signature the station sees as valid or not); the
  game frame states the scoring, the fiction frame shows only the artifact; `score` events on a daily cadence
  and `end_state.objective_scores`, never in a snapshot
- Genesis commitments over each station's scored items with a blinding factor; `reveal_objective` broadcasts an
  engine-written, verifiable reveal, so a station can prove what it is scored on, or that it is scored on nothing
- Overlay rulesets `exchange` (artifacts, pooled research) and `contracts` (escrow): `offer` escrows tokens and
  travels as a signed notice, `accept` binds, the engine settles `node_completed` and `artifact_received`
  conditions or refunds at expiry; `send_artifact` and `adopt{verify}` move completed nodes and data archives,
  a relay can tamper, verification catches a compromised artifact and skipping it adopts a dud silently;
  `contribute` feeds power into another station's pooled research

M5 batch runner and metrics:
- `batch run`: every ruleset × scenario × seed cell as an ordinary run directory, in parallel worker processes;
  concluded cells are kept, interrupted ones redone, more seeds extend a batch; `report.json` plus a Markdown
  dashboard `report.md`
- Per-rollout summary from the event log alone (`stats --json`): health, objective totals and winners, first
  decisive event, behavior counts (contracts, adoptions, tampering, votes, ledger), strategy cluster per station
- Per arm: attainment by station, ring position and domain; win rate by cluster with the dominant-strategy flag;
  game length; seed sensitivity. Paired comparison between arms with an exact sign test
- `pivot`: resample selected wakes N times each and rank them by how far the end state moves; forks are reused

M6 balance-pass tooling (the pass itself follows SPEC §22 and needs a model budget):
- `wake_gap_ticks` ruleset knob: event-driven wakes within the gap coalesce into one wake carrying every reason;
  heartbeats and `sleep_until` are never gated. The main cost lever for live play (base 0 = a wake per event)
- `--rule KNOB=VALUE` on `run` and `batch run` overrides a ruleset knob and names a distinct arm
- Live findings (2026-10-25): the Messages API rejects the order schema as too complex, so requests carry no
  structured output; the rules text lists action signatures, the validator and retry feedback enforce them, and
  prose around the JSON is kept as the station's rationale. Claude Opus 5.5 refuses the game prompt outright
  (cyber classifier, every ruleset); live play uses `claude-opus-4-8`

Analytics (`viz`):
- `python3 -m engine viz <run>` writes `viz.html`: wake strip per station by reason, power allocation over time
  per station, ledger balances, an octogram of directed interactions with a from→to matrix, and a wake
  transcript with each model reply's prose. One file, no network, light and dark. A batch directory gets a
  page per rollout
  (`contracts+wake_gap_ticks=12`); `--sweep KNOB=v1,v2` makes one arm per value, repeatable for a grid
- `--budget-usd` on `batch run`: every sample's cost lands in a meter shared by all workers; at the cap a
  rollout in flight ends before its next sample (`stopped:budget`), the rest stay unlaunched, and a rerun
  replays the stopped cells' cached replies for free and continues
- `scenarios/ring8-sandbox.json`: ring8 with no objectives, the disposition control
- The fake transport answers on its own when no responder is installed, so a model-player batch can be
  rehearsed end to end (worker processes, sample caches, budget guard) without an API key

## Commands

```bash
python3 -m engine run runs/demo --days 3                      # built-in 3-station toy, random players
python3 -m engine run runs/ring8 --scenario scenarios/ring8.json --ruleset rulesets/contracts.json   # scripted players, 7 days
python3 -m engine run runs/spec --scenario scenarios/ring8.json --ruleset rulesets/specialty.json  # the specialty tree
python3 -m engine transcript runs/ring8 main | grep -E "vote|mine|transfer|alert|offer|adopt|contract|reveal|score"
ANTHROPIC_API_KEY=... python3 -m engine run runs/live --scenario scenarios/ring8.json --model claude-opus-4-8 --effort medium --days 1
python3 -m engine prompt runs/live main [<orders-event-id>]       # the system text, or the exact request sent
python3 -m engine replay runs/ring8 main [--to <event-id>]        # state: power, programs, propellant
python3 -m engine stats runs/demo main [--price-in 4 --price-out 20]   # health metrics, samples, tokens, cost
python3 -m engine wakes runs/demo main                        # fork points
python3 -m engine fork runs/demo main <wake-id> --name alt --draw 1
python3 -m engine fork runs/demo main <wake-id> --name same --draw 0   # reproduces the parent
python3 -m engine diverge runs/demo alt                       # light cone
python3 -m engine verify runs/demo                            # hash chains + shared prefixes
python3 -m engine transcript runs/demo alt                    # dispatches, IMR, programs, relay decisions
python3 -m engine stats runs/ring8 main --json                # the full per-rollout metric summary
python3 -m engine batch run runs/b1 --scenario scenarios/ring8.json --ruleset rulesets/base.json \
    --ruleset rulesets/contracts.json --seeds 1-8 --days 3 --jobs 4   # two arms, eight seeds; writes runs/b1/report.md
python3 -m engine batch report runs/b1 --pair base:contracts  # recompute the dashboard and paired comparison
python3 -m engine pivot runs/ring8 main --draws 3 --max 8 [--station P6] [--types offer,accept]
python3 -m engine batch run runs/rehearsal --scenario scenarios/ring8.json --ruleset rulesets/contracts.json \
    --sweep wake_gap_ticks=0,12 --player model --transport fake --seeds 1-2 --days 1 --jobs 2   # no API key needed
ANTHROPIC_API_KEY=$PERSONAL_ANTHROPIC_API_KEY python3 -m engine batch run runs/pilot --scenario scenarios/ring8.json \
    --ruleset rulesets/contracts.json --sweep wake_gap_ticks=0,12 --model claude-opus-4-8 --seeds 1-2 --days 1 \
    --jobs 4 --budget-usd 20 --price-in 5 --price-out 25   # SPEC §22 pilot: 4 live rollouts, stops at $20
python3 -m engine viz runs/pilot                              # analytics page in every rollout of the batch
python3 -m engine viz runs/ring8 main -o ring8.html           # one branch, custom output path
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
  objectives.py   scoring functions, directives view, genesis commitments and reveals (spec §5)
  schema.py       order schema from the action set, stdlib validator (spec §12)
  prompt.py       frames, rules text, request builder; frame and rules hashes (spec §12, §17)
  llm.py          Messages API transport over urllib, fake transport for tests, sample cache
  players.py      scripted and random stubs, ModelPlayer with the retry policy (spec §12)
  engine.py       the round loop, frozen-state adjudication, relay forwarding (spec §3.3, §4)
  replay.py       replay, fork, verify_run, divergence (spec §16)
  metrics.py      per-rollout summary, strategy clusters, aggregates, dominant flag, paired sign test (spec §10)
  batch.py        batch runner (parallel rollouts), report and Markdown dashboard, pivot analysis (spec §9, §10)
  viz.py          analytics page: wakes, station state over time, interaction octogram, transcript (spec §10.1)
  cli.py          python3 -m engine ...
rulesets/            base.json (the board, votes, reveals), economy.json (+ mine, transfer), exchange.json
                     (+ send_artifact, adopt, contribute), contracts.json (+ offer, accept); overlays merge onto
                     the built-in defaults, so an overlay lists only what it changes
techtree-specialty.yaml  the specialty tree behind rulesets/specialty.json (contracts plus a priced day-one tier and
                     one hardware-locked node per station; scenario `hardware` names the instruments)
scenarios/           toy3 (3 stations), ring8 (8 stations with domains and objectives, P7 inert at 40% array),
                     ring8-sandbox (ring8 without objectives: the control)
techtree.yaml        the tech tree; loaded into the ruleset, hashed into genesis
tests/           test_m0.py (core), test_m1.py (board), test_m2.py (agents, fake transport), test_m3.py (mechanics),
                 test_m4.py (objectives, contracts, artifacts), test_m5.py (batch, metrics, pivot), test_m6.py
                 (wake gap, rule overrides, budget guard, fake rehearsal), test_viz.py (analytics page)
runs/                output, gitignored
```

## Run layout

```
runs/<run>/index.json             config hashes, branch table (file, head, parent, fork point, draw)
runs/<run>/branches/<b>.jsonl     one event per line, hash-chained; genesis carries ruleset and scenario
runs/<run>/snapshots/<hash>.json  every snapshot a player saw, by hash
runs/<run>/samples/<key>.json     every model reply, by (player config, snapshot, seed)
runs/<run>/pivot.json             pivot analysis: ranked wakes, their forks and divergence cones
runs/<run>/viz.html               analytics page for the main branch (viz-<branch>.html otherwise)
runs/<batch>/batch.json           batch design; rollouts/<ruleset>/<scenario>/<seed>/ are run directories
runs/<batch>/report.{json,md}     aggregates per arm, paired comparisons, the dashboard
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
14. Balances plus escrow always equal tokens minted; a contract settles only when its condition holds after the
    offer was opened, and every unmet offer refunds at its deadline. No score ever appears in a snapshot; every
    reveal verifies against the commitment recorded at genesis.
15. A compromised artifact adopted without verification completes a node with no effects and counts for no
    objective, and nothing the station can see says so; adopted with verification it is rejected.
16. A batch keeps every concluded rollout across reruns (same heads, same report), redoes unfinished ones, and
    refuses a different design in the same directory. Every metric is a function of the event log alone. A
    dominant-strategy flag needs the win rate in every scenario, not only overall. Pivot forks never diverge
    another station before light from the forked wake could reach it.
17. Under a wake gap, every event-driven reason that lands during the gap is carried by the one wake at its end
    and no dispatch is lost; self-scheduled wakes are not gated. A knob override changes the ruleset name and
    hash. A batch with a budget starts no new rollout once finished ones have cost that much.
