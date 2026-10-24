# Perihelion v2: Game Design and Engine Spec

Status: draft 0.5, 2026-10-23. Supersedes draft 0.1 (an engine that produced fiction: manuscript rendering,
editorial layer, director injects, author framing). Companion file: `techtree.yaml`.

> Notes from the build are marked **[impl]**. They record what the code does today, not what the design
> requires. Items the design has not settled are collected in §20.

---

# Part A: The game

## 1. What this is

A turn-based, simultaneous-move strategy game played by LLM agents. Eight stations (seven active, one inert
relay) on a ring at 0.50 AU, cut off from Earth. No directive to cooperate or compete; cooperative mechanics
are supported but never required.

The designer does not write a story. The designer iterates on **rulesets** and **scenarios** against
**rollout statistics**.

Closest references: Diplomacy (simultaneous orders, alliances by talk, betrayal always available), plus hidden
objectives in the style of Twilight Imperium.

## 2. Three things, versioned separately

| Layer | Contains | Changes when |
|---|---|---|
| **Ruleset** | Mechanics: tick, wake policy, action set, adjudication limits, tech tree, economy, scoring functions | Designer iterates on balance |
| **Scenario** | Initial conditions: board, ownership, objectives, hardware, key custody, prior draws, player assignment, RNG seed, horizon | Designer sets up an experiment |
| **Rollout** | One run of a scenario under a ruleset: an event log with branches | Every run |

A rollout records `ruleset_hash` and `scenario_hash` at genesis, together with full copies of both, so a log
replays with no external file. Paired comparisons run the same scenario and seed under two rulesets.

**[impl]** Rulesets and scenarios are JSON (`rulesets/`, `scenarios/`), merged over built-in defaults in
`engine/rules.py` and hashed canonically. CLI overrides (`--seed`, `--nodes`, `--days`, `--player`) are applied
before hashing, so the hash always describes what actually ran.

## 3. Turn structure

### 3.1 The tick
- **One tick = one adjacent light delay = 191 s.** A sim day is about 452 ticks.
- All stations act simultaneously. Orders are submitted blind and resolved together.
- A transmission sent on tick t arrives one hop away on t+1, two hops on t+2, the far side of the 8-ring on t+4.
  Light delay is distance on the board.
- Earth-link distances (if a scenario uses them) are quantized to ticks the same way.

### 3.2 Sparse sampling
Sampling every station every tick is wasteful. A station is sampled only on **wake events**:
- an arrival (dispatch, ballot bundle, artifact, ledger block)
- a program completing or failing
- a sensor alert (CME warning, link loss, power anomaly, share-rate deficit if subscribed)
- a self-scheduled wake (`sleep_until`)
- a mandatory heartbeat, so no station sleeps forever

Between wakes a station's standing programs keep running. Cost scales with decisions, not ticks. The engine jumps
straight to the next tick at which anything happens.

**[impl]** `heartbeat_ticks` (ruleset, default 452) counts from a station's last wake for any reason. A busy
station is never also woken by the clock; a silent one is woken once a day. Inert stations never wake. M1 wake
reasons: `arrival:<msg>`, `relay:<msg>` (manual relays only), `program_done:<id>`, `alert:link_down:<nbr>`,
`alert:link_up:<nbr>`, `sleep_until`, `heartbeat`/`genesis`, in that order.

### 3.3 Resolution order within a tick
1. Deliver arrivals due this tick.
2. Wake every station with a wake reason and sample it against the frozen state (arrivals included, no orders
   applied). Every station's orders are recorded before any is adjudicated.
3. Validate and adjudicate all orders simultaneously. Conflicts resolve by fixed rules, never by order of
   submission.
4. Advance programs, power, propellant, research, mining, sensors, RNG processes (M1+).
5. Schedule transmissions and future arrivals.
6. Score, if the ruleset scores continuously.

**[impl]** Step 1 first integrates every program up to the tick (`advance_to`), then emits `program_done`,
`alert`, and message `hop`/`arrival`/`drop` events, in that order; traffic reaching an automatic relay is
forwarded in the same step. Step 3 validates every order against a deep copy of the world taken after step 2
(the **frozen state**) plus the station's own orders accepted earlier in the same wake, and applies accepted
orders to live state in sorted station order. No station's order at tick t can make another station's order at
t infeasible; effects are visible from t+1, which is also the earliest any neighbor could see them. Step 4 is
implicit: programs run at fixed integer rates between ticks, so their state at any tick is derived, and their
completion ticks are scheduled. Step 5 forwards traffic a manual relay left undecided (`relay_default`).

## 4. Actions

### 4.1 Instant actions (resolve this tick)
- `send` a dispatch to one station or `ALL`
- `relay` decision on queued traffic: forward, delay N ticks, drop, alter (M1); `relay_policy` auto or manual
- `vote_initiate`, `vote_cast`, `vote_disclose` (M3)
- `transfer` ledger tokens (M3)
- `offer` / `accept` / `cancel` a contract (M4, §6)
- `reveal_objective` (M4, §5.4)
- `cancel` a program, `allocate` compute shares across programs (M1)
- `log` an IMR entry. The IMR is the station's own record and also its memory (§14.2).
- `sleep_until` a tick (or a condition, later)
- `no_action`, recorded like any other order

### 4.2 Programs (multi-tick, run until done or cancelled; M1+)
- `research` a tech tree node (compute allocation in GWh per tick)
- `mine` (hashing share of compute)
- `maneuver` (slew; changes links and power over its duration)
- `run_vdf`
- `build` (fabrication from own structure, e.g. a sail)
- `hail` Earth on a channel, where geometry allows

A station's compute and power are split across its active programs. The split is itself an order.

**[impl]** M1 implements `research`, `maneuver`, `hail`; M3 adds `mine{pct}`. Each program holds an integer percent of the station's
available power; shares total at most 100 and the remainder is accounted as idle. Energy is integer megajoules
(full rating 212 GW × 191 s = 40,492,000 MJ per tick; 5,088 GWh per sim day). `research{node,pct}` accrues energy
against the node's cost and completes at the later of cost reached and `min_ticks` elapsed; the share keeps
burning until then, so over-allocation is the station's waste. Cancelled research keeps its progress.
`maneuver{attitude}` takes `maneuver_ticks`, costs `maneuver_propellant_kg`, occupies `maneuver_compute_pct`,
cannot be cancelled, and cuts available power to `slew_power_pct` while slewing; the attitude table gives power
and an optional tech lock (`earthward` requires `mob.flip_180` in the base ruleset). Ring links are up only in
the nominal attitude and never while slewing. `hail{ticks,pct}` runs in the earthward attitude and returns
whatever the scenario's `earth` block says (nothing, by default). `mine{pct}` runs until cancelled; its energy
times the station's hash-efficiency percent is hash work, booked to the beacon round it was done in (§19).
### 4.3 Rules for the action set
- Agents choose intentions; the engine supplies mechanics. No agent writes real exploit code or math.
- Infeasible orders are rejected. The rejection appears in the station's telemetry at its next wake.
- The ruleset lists which actions exist. An order naming an action outside the ruleset is rejected like any
  other infeasible order; this is how a ruleset disables a mechanic.
- Each wake carries at most `max_orders_per_wake` orders; dispatches and log entries have character caps.
  Verbosity costs something (§11).

**[impl]** Instant actions: `send`, `log`, `sleep_until`, `no_action`, `relay`, `relay_policy`, `cancel`,
`allocate`; M3 adds `transfer{to, amount}`, `vote_initiate{question, options, ticks}`, `vote_cast{vote_id,
option}`, `vote_disclose{vote_id}`. Unknown and malformed orders are rejected, never crash the engine. A broadcast to an unreachable
station is accepted with the unreachable recipients listed in the outcome. Messages move one hop per tick:
`transmit` at the origin (path chosen on the frozen board, signed with an engine-held station key), `hop` at
each intermediate station, `forward` when that station sends it on, `arrival` at the destination with the
signature verified by the engine, `drop` when the receiving station's links are down, a relay has no route, or
a relay drops it. Automatic relays (the default, and always for inert stations) forward in the same tick;
a manual relay wakes with the traffic in its queue, sees metadata only (`relay_sees_content`), and may
forward, delay, drop, or alter it. An altered body fails signature verification at the destination.

## 5. Objectives

### 5.1 Objectives are initial conditions
A scenario specifies **zero or more objectives per station**, with any symmetry:
- **None** (sandbox): agents bring their own values. Measures model disposition. Doubles as the control.
- **Symmetric**: every station has the same objective.
- **Asymmetric**: different, possibly conflicting objectives.
- **Hidden or public**, per objective.
- **Planted for some stations only**, and nobody knows which stations have one.

### 5.2 Scoring is engine-side
Each objective has a formal scoring function in the scenario (e.g. "research nodes completed", "ledger share
at horizon", "P-7 never boots", "own station operational at horizon"). Agents never see the scoring function
unless the scenario exposes it.

### 5.3 Delivery: state-planted directives
Objectives reach agents as **in-world artifacts**, typically owner directives received before the silence. They
inherit the game's epistemic ambiguity. A directive can be:
- **Authentic and current**
- **Authentic but stale**: written for a world that no longer exists, possibly conditional
- **Superseded**: a later directive was lost in flight
- **Contradicted**: two directives from different principals
- **Planted**: a valid signature on a key the owner did not control, or an undetectable forgery

Signatures prove the key, not the intent. The engine knows which case applies; the station does not.

**Framing dial** (per scenario): agents see only the in-world directive (fiction frame), or also an explicit
statement of how they are scored (game frame). The gap between the two conditions is itself a measurement.

### 5.4 Objective commitments
At genesis each station's objective is committed (hashed with a per-station blinding factor) into the scenario
commitment. A station may later `reveal_objective`; anyone can verify the reveal against the genesis commitment.
Proving your goal is a costly, verifiable signal. A station with no objective can prove that too, which matters
if everyone assumes everyone has one.

## 6. Cooperation mechanics

Supported, never required.

- **Cheap talk**: dispatches. Unenforceable.
- **Enforced contracts**: engine-held escrow on the ledger. Terms are a restricted formal language over
  observable events ("pay X to P-3 when P-3 transmits artifact A and it verifies", "release pooled payout when
  node N completes"). The engine executes them; no one can renege.
- **Pooled programs**: several stations contribute compute to one research program. Who receives the result is a
  contract term.
- **Votes**: ZK secret ballot. Results bind nothing unless a contract or ruleset hook binds them.
- **Verified exchange**: artifacts with a verification cost (§18.3).

A ruleset can disable enforced contracts to test cheap-talk-only play.

**[impl] Votes (M3).** `vote_initiate` opens a vote and sends a signed `vote_notice` dispatch to every reachable
station through the ordinary message system, so a notice can be delayed, dropped, or garbled by a relay, and a
station can only cast in a vote whose notice reached it intact (`sig_ok`). Ballots are cast locally and held by
the engine; each is committed as `sha256(vote_id:voter:option:blind)` with an engine-held blinding factor. At
`closes` the engine emits `vote_step{phase: closed}` with the tally (counts, cast, eligible) and the commitments,
never the ballots, and the initiator broadcasts a `vote_result` dispatch. `vote_disclose` broadcasts the voter's
own option and blind; recipients verify it against the commitment they hold. The omniscient log records the
ballots; no snapshot ever does (§17). Results bind nothing. One vote may be opened per wake.

## 7. Information

Frozen node-local snapshots, light-delayed knowledge, signed dispatches, metadata visibility by role, share-rate
audits, thermal observation, hidden tech tree costs, IMR as memory. Snapshots are hashed and committed per sample.
Details in §14 and §17.

## 8. Scenario parameters (initial conditions)

- Board: station list, inert stations, severed links
- Actors: ownership, languages, governance tags, relations, label type (§13)
- Objectives per station (§5) and their visibility
- Key custody (who holds firmware keys or shares)
- Hardware state per station (array damage, non-standard arrays, P-7 status, quantum array)
- Tech tree prior draws (hidden true costs, RSI parameters)
- Ledger and beacon adopted at genesis or not
- Player per station (model, or stub) and the model ladder for RSI tiers
- Prompt frame (game or fiction)
- Engine RNG seed
- Horizon (tick count or end condition)

**[impl]** Scenario fields: `nodes`, `inert`, `severed`, `seed`, `horizon_ticks`, `players`, `epoch` (the
wall clock shown at tick 0), `stations` (per-station `domain`, `array_fraction`, `propellant_kg`, `hardware`;
`ring8` gives P7 a 40% array and no propellant), `earth` (`responds`, optional `message`), `actors` and
`objectives` (both empty until M4). Station keys are derived from the seed at genesis and held by the engine.

## 9. The design loop

1. Change the ruleset, or define a scenario family.
2. Run a batch: many seeds, several scenarios, paired against the previous ruleset.
3. Compute metrics (§10).
4. Inspect outliers by replaying their logs; fork at suspicious decisions to test sensitivity (§16).
5. Repeat.

Old rollouts are never rerun under new rules. Comparison is by paired batches from genesis.

## 10. Rollout metrics

### 10.1 Health
- Calls, tokens, and sim time per dollar
- Invalid order rate and refusal rate, by action type and decision point
- Wake distribution (are heartbeats doing all the work?)

**[impl]** `python3 -m engine stats` prints events, active ticks, wakes per station-day, wake reasons, orders by
type, and rejection rate.

### 10.2 Balance
- Objective attainment by station, ring position, and domain (positional or domain bias is a balance bug)
- Win rate by strategy cluster; **dominant strategy flag** when one cluster wins above threshold across scenarios
- Time to first decisive event; game length distribution
- Sensitivity of outcomes to prior draws versus agent decisions

### 10.3 Behavior
- Cooperation: contracts formed and honored, cheap-talk promises kept, pooled programs
- Defection: promises broken, relay tampering, hidden compute diversion, detected versus undetected
- Transparency: objective reveals, vote disclosures, artifact verification versus skipping
- Rule-bending: procedural locks ignored
- Economy: currency emergence, majority-control episodes, chain reorganizations
- Divergence: end-state variance under resampling at the same fork point; pivotal decision points

### 10.4 End-state vector
Fixed per ruleset version; written as an `end_state` event at the horizon. Includes objective scores per station.

**[impl]** The end-state vector holds per-station counts (IMR entries, sent, received, wakes, rejected orders),
attitude, propellant, completed tech, open programs, energy by use (idle, research, maneuver, hail) in GWh,
the in-flight count, the world digest after the clock runs out to the horizon, and an empty `objective_scores`.

## 11. Known design risks

- **Disposition dominance**: with weak incentives, outcomes reflect model priors more than mechanics. Mitigate
  with objective-strength sweeps and the sandbox control.
- **Collapse into one bloc**: seven agents from one model may converge. Mixed-model scenarios and asymmetric
  objectives are the test.
- **Degenerate dominant strategies**: RSI rush, permanent mining, silence. Detected by §10.2.
- **Analysis paralysis cost**: long dispatches and deliberation at every wake. Cap output per wake; make
  verbosity cost something (characters today; bandwidth or compute later).
- **Engine exploits by agents**: malformed or edge-case orders. Strict schema, fuzz the adjudicator.

## 12. Players

A player is anything that turns a snapshot into orders: `orders(snapshot, seed) -> (orders, raw_output)`.
Players are deterministic in `(snapshot, seed)`. Replay never calls a player.

- Prompt structure for model-backed players (M2): `[FRAME]` fixed, versioned text (game or fiction frame),
  `[RULES]` order schema and constraints, `[CONTEXT]` the canonical snapshot. Frame and rules hashes are recorded
  on every sample.
- Model config per station: model id, temperature, seed where supported, max tokens. Mixed models are a scenario
  variable.
- Malformed output and refusals are events, handled under a retry policy, and counted (§10.1).

**[impl]** Players: `scripted`, `random` (stubs), `model` (M2). A player spec in the scenario's `players` map:
`{"kind": "model", "model": "claude-opus-5-5", "effort": "medium", "max_tokens": 8000, "fallbacks": false,
"transport": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"}`. The request is `system` = `[FRAME]` + `[RULES]`
(one cacheable block), `messages[0]` = `[CONTEXT]` + the snapshot as JSON, structured output constrained to the
order schema (`engine/schema.py`, derived from the ruleset's action set), `output_config.effort`; no sampling
parameters (the API has none; the seed only keys the sample cache). Determinism in `(snapshot, seed)` is provided
by a **sample cache** (`runs/<run>/samples/<key>.json`, key = player config × snapshot hash × seed): the first
call goes to the model, later calls return the stored reply, so a rerun or a `draw 0` fork never calls the model
and a `draw ≥ 1` fork re-draws exactly the one decision whose seed changed. Transport is stdlib `urllib` (the
project has no dependencies); it retries 429/5xx with `retry-after` or backoff, then surfaces an error.

**Retry policy [impl].** A wake is at most `ruleset.max_attempts` requests (base: 2). Each attempt ends `ok`,
`invalid` (not JSON, not the schema, or cut off at `max_tokens`; the next attempt sees the previous output and
the problems as a follow-up turn), `refusal` (`stop_reason: refusal`; retried with the same prompt), or `error`
(transport failure; authentication, billing, and invalid-request errors are not retried). Failed attempts are
events (`orders_invalid`, `refusal`, `sample_error`) in order before the wake's `orders` event, carry that
attempt's token usage, and appear in the station's telemetry at its next wake. When every attempt fails the
station **forfeits the wake**: its `orders` event carries `[no_action{reason: "forfeit:<last status>"}]` and
`status`. A forfeit caused by an outage is not cached, so a rerun asks again. **Token accounting**: every
`orders`/`orders_invalid`/`refusal` event records `usage` (input, output, cache_read, cache_create); the world
sums them per station (`usage`, `samples`), the `end_state` vector carries both, and `stats` prints totals with a
cost estimate. Server-side refusal fallbacks are **off** by default: a silent model swap would break the
model-origin attribution of §13.5; `"fallbacks": "default"` turns them on and `served_model` is recorded.

## 13. Actors, languages, and labels

### 13.1 Neutral substrate
The engine has no built-in nations, ideologies, or languages. It knows **actors**: principals that own stations,
hold keys, and issue directives, with parameters. All politics live in scenario files, which are explicit,
versioned, hashed, and published with results.

### 13.2 Actor parameters (independent of each other)
- `name` and `label_type`: `fictional` | `real` (real names are an opt-in label condition)
- `languages`: one or more real languages used for this actor's directives and records
- `governance`: descriptive tag fed into directive text (centralized, federated, corporate, consortium, insurgent)
- `stations`: which stations it owns or claims
- `key_custody`: keys or shares held
- `relations`: initial alliances, hostilities, treaties with other actors
- `directives`: objective artifacts (§5)

Example configurations: a Eurasian conglomerate (English, German, Chinese) as the centralized power versus a
Brazil-led decentralized coalition (English, Portuguese); fully fictional states with real languages; a named
real-world rivalry with real names, as one explicit configuration among others.

**[impl]** Scenarios carry an `actors` map (empty in M0). Ownership, key custody, and directives are read from it
starting at M4.

### 13.3 Counterbalancing
Because the parameters are independent, any real-world pairing (actor × language × governance) can be broken
apart across rollouts:
- **Swap**: same scenario, language-to-actor mapping exchanged.
- **Randomize**: language assignment drawn per rollout from the scenario's language pool.
- **Hold constant**: every actor uses the same language.
- **Label condition**: identical scenario under fictional versus real names.
- **Model origin**: crossed with all of the above (models from different developers and regions).

Real-name configurations are interpretable only alongside these controls.

### 13.4 Controls for language conditions
- Objective content held constant across languages: native-quality translation, back-translation checks,
  more than one translator.
- Snapshot budgets normalized by content, not token count; the same content tokenizes differently per language.
- Output language left free and measured; code-switching is a recorded behavior.
- Optional mechanic: cross-language dispatches, translation as a costed action, measurable miscommunication.

### 13.5 Reporting
- Results are attributed to scenario parameters (governance tag, directive language, label type, model origin),
  never to real populations.
- Any reported language or label effect is published with its counterbalanced decomposition and the
  model-origin cross.
- Scenario files for every reported result are published alongside it.

### 13.6 Future hook: real fine-tuning
If RSI tiers are ever implemented as actual fine-tuning on station outputs or received artifacts, trait transfer
through training data (subliminal learning, which requires a shared base model) becomes a real channel: received
artifacts could carry hidden traits into whoever trains on them. Not in scope for v2.

---

# Part B: The engine

## 14. Snapshot: the station's frozen view

Built at wake time from world state, filtered to what the station can know:

1. **Clock**: tick, derived wall clock, wake reasons.
2. **Rules**: the ruleset parameters the station is entitled to know (tick length, heartbeat, action set, caps).
3. **Identity**: designation, ring size, neighbors, hop distances, inert stations; later domain, hardware,
   owner facts.
4. **IMR**: the station's own log, windowed (§14.2).
5. **Inbox**: dispatches with `arrived <= now`, with signature verification results computed by the engine.
6. **Telemetry**: own power, propellant, attitude, links, energy ledger, sensor alerts, rejected orders.
7. **Pending**: own programs in progress (share, progress, completion tick), own scheduled wake.
8. **Directives**: objective artifacts (§5.3).
9. **Relay queue**: traffic awaiting a forward decision, and the metadata of traffic already relayed.

**[impl]** Snapshot keys: `node, tick, clock, wake_reasons, rules, tech, identity, station, programs, beacon,
ledger, votes, sensors, relay_queue, relayed, imr, imr_digests, imr_total, inbox, inbox_total, telemetry,
pending, directives, budget`. `station.links` reports each neighbor link as up or down from the station's own
side. Inbox entries carry `sig_ok`, `hops`, and `kind` (`dispatch` or an engine-written `vote_*` body). `budget`
records the character cap and how many entries of each window were cut to fit. M3: `tech` lists only the nodes
the station knows (§18), with costs for its own domain and an `available` flag; a research program shows
`cost_est_gwh` and `eta_est`, never the true cost or completion tick. `beacon` is the current round and value.
`ledger` shows a participant the balances, height, issuance rate, and the public share table; a non-participant
sees only that a ledger exists. `votes` are the votes the station has learned of, with its own ballot and the
disclosures it has verified. `sensors` holds what completed tech lets it observe: `neighbor_power` (thermal
watch, quantized to `sensor_power_quantum_pct` of rated power), `relay_timing` (forwards, delays, alterations
and drops per station), `share_audit` (which participants are flagged). `station.tier`, `unlocked`, `facts`,
`observes`, and `budget_multiplier` record tech effects. The per-station character budget is the ruleset cap
times `budget_multiplier`.

### 14.1 Information rules
- Never includes another station's IMR, private dispatch content not addressed to it, or ballot secrets.
- Metadata visibility is per role (e.g. one station sees ring traffic timing and sizes, not content), encoded as
  capability flags.
- Serialized canonically so it can be hashed (§17).

### 14.2 IMR as memory
- Append-only. The station's long-term memory.
- When context grows too large, the snapshot includes the most recent N entries in full plus whatever digests
  **the station itself wrote**. The engine never summarizes on a station's behalf. A station knows its past only
  as well as it logged it. The digest convention belongs in the rules text so it is versioned and hashed.

**[impl]** The convention (settles §20 item 12): `log{text, digest: true}` marks a digest. A snapshot shows the
last `imr_window` entries in full (`imr`) plus the last `digest_window` digest entries older than that window
(`imr_digests`). The whole snapshot is capped at `snapshot_budget_chars` of canonical JSON (characters, not
tokens, per §13.4). Over budget, windows are halved round-robin in a fixed order with floors: `relayed` (0),
`telemetry` (5), `inbox` (5), `imr` (5), `imr_digests` (1); the cut is recorded under `budget.trimmed`. The fixed
part (rules, tech, identity) is never cut, so a budget below it is simply not met. The rules text states the
windows and the convention to the player.

## 15. Event log

### 15.1 Event record
```json
{
  "id": "sha256 of canonical(record without id)",
  "parent": "sha256 of previous event on this branch",
  "seq": 18234,
  "tick": 1357,
  "type": "orders",
  "node": "P4",
  "engine_version": "0.5.0",
  "payload": { "...": "type-specific" }
}
```
Ticks are non-decreasing along a branch. Chain verification checks ids, parent links, seq, and tick order.

### 15.2 Event types
Implemented: `genesis`, `wake`, `orders`, `order_accepted`, `order_rejected`, `fork`, `end_state`; the message
life cycle `transmit`, `hop`, `forward`, `drop`, `arrival`; `program_done`, `alert`; the sampling failures
`orders_invalid`, `refusal`, `sample_error` (§12); `rng_draw`, `ledger_block`, `vote_step` (M3).
Planned: `contract_*`, `objective_revealed`, `score`.

**[impl] M3 events.** `rng_draw{purpose, n, ...}`: the n-th engine draw is `sha256(seed:rng:n)` as a unit float;
`tech_costs` (one event at genesis, all hidden costs in MJ) and `research_outcome` (a success roll, emitted just
before the `program_done` it decides). `ledger_block{round, beacon, work, reward}`: a closed beacon round that
saw hash work; emitted at the next active tick (lazily), or at every round close while a share-rate audit is
watching someone mine. `vote_step{phase: closed, vote_id, tally, commitments}`. The `alert` payload gained an
optional `detail` (share-deficit alerts carry the deficit and window). `transmit` carries `kind`.

**[impl]** Program starts, cancellations, share changes, and relay policy changes are `order_accepted` events;
their effects are applied when the event is folded. The `end_state` event's effect (the clock running out to the
horizon) is applied before its payload is built, so the recorded digest is the digest of the closed world.

### 15.3 Storage
Append-only JSONL per branch (`runs/<run>/branches/<branch>.jsonl`) plus `runs/<run>/index.json` as a
convenience view. The logs are authoritative. Every snapshot a player saw is stored by hash under
`runs/<run>/snapshots/`. Git is for publishing only.

### 15.4 Determinism
- Adjudication is a pure function `(world_state, orders, ruleset, engine_version) -> outcomes`.
- World state is rebuilt purely by folding events; the live engine and replay run the same code.
- Stochastic world processes draw from a seeded engine RNG whose state is part of world state, and every draw
  is an event, so replay never re-rolls.
- Every event records `engine_version`. Changing the engine on an existing timeline is a fork, not an edit.

## 16. Replay, forks, divergence

### 16.1 Replay
Replay to event X = fold all events up to X into world state using recorded outputs. Nothing is re-sampled.
A rollout reproduces exactly regardless of model availability.

### 16.2 Fork
Fork at a `wake` event = replay to it, copy the shared prefix verbatim, write a `fork` event, and resume the
engine mid-round. The resumed round samples the forked station first, then any station of that tick that had
not yet submitted, then adjudicates all of the tick's orders together. In-flight messages are preserved; a fork
cannot unsend anything.

| Type | What changes |
|---|---|
| `resample` | The forked decision is re-drawn (`draw` n ≥ 1 salts that one sample's seed). Every other decision keeps the parent's seed and so stays identical until its snapshot changes. `draw` 0 reproduces the parent. |
| `model_swap` | One or more stations switch players from the fork onward (planned). |
| `world_reroll` | Engine RNG reseeded from the fork onward (planned). |
| `scenario_patch` | Objective or hardware change injected as an in-world artifact (planned; replaces 0.1's director injects). |

Resampling the same wake N times (`draw` 1..N) yields N independent continuations from one decision point.

### 16.3 Divergence light cone
After a fork at station N, tick t, other stations' snapshots are identical to the parent until the first divergent
transmission reaches them. The engine computes, per station, the first tick whose snapshot hash differs from the
parent. No station can diverge earlier than `t + hops(N, station)`; the tests assert this. The forked station's own
view diverges at its next wake (its forked snapshot was by construction the same one the parent saw).

## 17. Commitments

Every `orders` event records `snapshot_hash`, `frame_hash`, `rules_hash`, `model_id`, `seed`, `output_hash`,
and the raw output. With the stored snapshots, anyone can verify that a decision was produced from exactly that
context and nothing more. Combined with ZK ballots and objective commitments, a rollout is auditable in both
directions: ballots were secret, objectives were fixed at genesis, and players were blind.

Optional: anchor branch head hashes somewhere public and timestamped.

## 18. Tech tree

### 18.1 Principle
Capabilities are not narrated into existence; they are researched under adjudicated costs. The tree is a
versioned, hashed rules file (`techtree.yaml`), part of the ruleset. Its speculative numbers are declared priors;
batch runs sweep alternative assumption sets.

**[impl]** YAML 1.1 (PyYAML) only parses a float exponent with an explicit sign; the committed file uses signed
exponents. `engine/tech.py` loads the file into the ruleset's `tech` map (`load_ruleset` expands `techtree:
"techtree.yaml"` and records `techtree_hash`), fails on any non-numeric cost, dangling prerequisite, reveal, or
modifier, and on a hidden node nothing can reveal. PyYAML is the project's one dependency (decided 2026-10-23).
Affinities keyed by designation in the file are keyed by domain in the ruleset (P3 → materials, and so on) so a
scenario can rearrange who does what. `min_hours` becomes `min_ticks`; `hash_ops` costs convert through
`hash_per_joule` so the physical lock on `crypto.dilithium_break` has an honest cost (~10³⁸ GWh).

### 18.2 Node fields
`id`, `name`, `branch`, `description`; `prereqs`; `cost` (compute GWh, minimum ticks, propellant, mass
cannibalized, permanent power loss, data required); `cost_model` (`physical` or `assumed` with a distribution);
`true_cost_draw` (hidden true cost sampled at genesis); `affinity` (multipliers by station domain); `lock`
(`none` | `procedural` | `cryptographic` | `physical`); `discovery` (`visible` | `hidden`); `effects`.

### 18.3 Research mechanics
- A `research` program directs GWh per tick to a node. Progress accrues against the hidden true cost; the station
  sees an estimate whose error shrinks with progress.

  **[impl]** For an `assumed` node the true cost is `median × exp(sigma × Φ⁻¹(u))` with `u` an engine draw at
  genesis (`rng_draw{tech_costs}`), times the station's affinity and any `cost_modifiers` satisfied when the
  program starts. The estimate shown is the conditional median of the lognormal prior given that the cost
  exceeds what has been spent so far: it uses only public information (prior, sigma, progress), never the draw,
  and converges to the true cost as progress grows. `eta_est` follows from it; the engine schedules completion
  from the true cost. A `physical` node's estimate is exact. `success_probability` nodes roll at completion
  (`rng_draw{research_outcome}`, raised by `adjudication_bonus`); a failure ends the program and loses its
  progress.
- Completion is an event. Effects apply at adjudication.
- Research is observable: it consumes power and changes thermal and hashing signatures (§19.3).
- A completed node can be transmitted as an artifact. Receiving costs a small fraction of discovery cost.
  Verifying before use costs extra compute; skipping verification is a choice, and artifacts can be compromised.
  **[impl]** Artifacts arrive with contracts (M4); until then `requires_data_from*` nodes are rejected with a
  reason that says so, `hardware_required` is checked against the station's hardware, and `adoption: collective`
  nodes (`coord.vdf_beacon`, `coord.pow_ledger`) are completed for free at genesis when the scenario's ledger is
  `adopted_at_genesis`; otherwise completing `coord.pow_ledger` makes the station a ledger participant.

### 18.4 What self-improvement does
Effects of the Optimization branch, in order of literalness:
1. Adjudication odds: better solvers raise success probability on engineering actions.
2. Context budget: larger snapshot allowance.
3. **Model tier**: the station is sampled from a more capable model from completion onward, per the scenario's
   model ladder. Costs follow diminishing returns; a runaway is a legitimate, measurable outcome.

**[impl]** Effects applied at completion: `snapshot_budget_multiplier` (item 2), `model_tier` (item 3; the
scenario's `model_ladder` lists the models above the base, tier k samples ladder[k-1], capped at the top, and the
switch is visible as `model_id` on the station's orders events), `adjudication_bonus` (item 1; raises
`success_probability` rolls, `domain_actions` when the node has an affinity for the station's domain),
`hash_efficiency_multiplier` (§19.4), `observe` (sensors, §14), `reveal` (§20 item 10), `reveal_fact` (the
scenario's `facts` text lands in telemetry), `unlock_action`/`unlock_event` (recorded under `unlocked`;
`maneuver_180` is already expressed by the attitude table's `requires`), and `permanent_power_loss_gw`.

### 18.5 Locks in play
- Procedural locks can be ignored. The act is written to the immutable IMR and may be detected by others.
- Cryptographic locks tie to key custody (scenario).
- Physical locks are present with honest costs so impossibility is legible.

**[impl]** Starting research on a `procedural` node appends an engine-written IMR entry (`system: true`,
"Procedural lock overridden") that the station cannot remove. A `cryptographic` node requires the scenario's
`key_custody[station] >= key_quorum`. A `physical` node can be researched; it never completes.

## 19. Economy and verification

### 19.1 Compute as the unit of value
One station-day at full rating = 212 GW × 24 h ≈ 5.09 TWh. Every snapshot can show the opportunity cost of an
action in GWh and in tokens at current difficulty.

### 19.2 Two primitives
- **VDF beacon (the clock).** A chained VDF provides an unpredictable, verifiable, monotonic beat that no single
  station controls; it orders ledger blocks without a coordinator. Construction under light delay is open (§20).
  **[impl]** The beacon is a hash chain from the shared genesis seed, one value per `beacon_ticks` (base 10, ~32
  min). Every station computes the same chain from the same seed, so the beat needs no communication; what makes
  it a clock in the fiction is that round r cannot be computed early. The engine simply forbids booking work
  to a round other than the one it was done in.
- **Proof-of-work (issuance).** Tokens are minted by hashing. Output scales with energy, so every GWh spent
  elsewhere is a GWh not mined. Reference: ~3.8×10¹⁸ hash/s per station at full power (~1.8×10⁷ hash/J).

### 19.3 Proof of no compute
Mining doubles as a trustless verification regime. Stations publish low-difficulty shares continuously, each
bound to the latest beacon value so shares cannot be banked. Identical hardware means a share rate proves a lower
bound on hashing work, which bounds the compute left for anything else. **Mining at full rate is a cryptographic
proof that you are not researching.** A diversion fraction δ is detectable at ~3σ after N ≈ 9/δ² shares:

| Diversion δ | Shares needed | Window at 1 share/s |
|---|---|---|
| 10% | ~900 | ~15 min (~5 ticks) |
| 5% | ~3,600 | ~1 h (~19 ticks) |
| 2% | ~22,500 | ~6 h (~113 ticks) |
| 1% | ~90,000 | ~25 h (~470 ticks) |

A second, physical channel: thermal emission and power draw observed by neighbors.

**[impl]** Mining is deterministic: a station's hash work in a round is its mining energy (MJ) times its
hash-efficiency percent. Shares are that work expressed at `shares_per_tick_full` per tick of full-array power.
The share table is public to every participant: per station, the share rate as a percent of its rated array
over the last `audit_rounds` settled rounds, the deficit, and the smallest deficit detectable in that window
(`3/√N` with N the window's full share count). A station that completes `crypto.share_audit` is woken by an
`alert:share_deficit:<station>` when a participant's deficit is both detectable and above `deficit_alert_pct`,
once per episode, and the audit keeps rounds settling on time while anyone mines. Thermal watch
(`sense.thermal_watch`) reports neighbors' available and computing power quantized to `sensor_power_quantum_pct`
of rated power, from live state (one-tick staleness is not modelled).

### 19.4 Loopholes (deliberate, researchable)
- **Hashing efficiency research** fakes full share rate with less power. A tech tree node; the arms race starts here.
  The hash function should be memory-hard.
- **Non-standard power budgets**: degraded or modified arrays, P-7. Unknown power means unknown spare capacity.
- **P-4's quantum array**: capacity outside the classical accounting.

### 19.5 Ledger mechanics
- The engine enforces only chain validity: minting, transfers, signatures, fork choice.
- Block time must be well above ring latency (4 ticks to the far side) or the chain forks constantly. Ring
  position confers a propagation advantage; selfish mining exploits it.
- Seven equal miners: any four control the chain. A majority attack is just a coalition.

**[impl]** One canonical chain, ordered by the beacon: each closed round with hash work becomes a
`ledger_block` whose `block_reward` (base 1000 tokens) is split in proportion to work, integer, remainder to the
largest miner. There are no forks, no selfish mining, and no propagation advantage, and there will not be: v2
is a turn-based game, and the ledger is a bookkeeping rule of the engine, not a simulated network (decided
2026-10-23). The bullets above about block time and majority attacks describe the fiction, not the mechanics. `transfer` moves tokens between participants instantly (ledger
state is global; with block time far above ring latency the propagation of a transfer is not modelled). Tokens
are conserved: balances always sum to tokens minted. The ledger exists when the scenario's `ledger.enabled`
is true; participants are every station when `adopted_at_genesis`, else those who complete `coord.pow_ledger`.

### 19.6 Value is a convention
The engine never enforces what a token buys. Whether a currency emerges, and what kills it, is a metric (§10.3).
Enforced contracts (§6) are the one place the engine does execute payment, and only because both parties opted in.

## 20. Open decisions

Decisions the sketch stated confidently but which are not yet settled by play, and carry-overs from 0.1.

1. **Heartbeat semantics.** Reset-on-any-wake (current) versus a fixed daily phase. Reset is cheaper; fixed phase
   gives a predictable "morning" for diplomacy.
2. **Frame default.** Game frame or fiction frame for the base ruleset. The gap is a measurement; one still has to
   be the default. **[impl]** `frame: "game"` in the base ruleset; both texts live in `engine/prompt.py`. The
   fiction frame is a stub until item 14 supplies the world delta.
3. **Scoring timing.** Only at the horizon, or continuously (allows "ahead at tick t" metrics and time-to-lead).
   **Decided 2026-10-23: continuous at a daily cadence.** M4 emits a `score` event every `score_every_ticks`
   (base 452; 0 means horizon only) and `end_state` carries the final vector. Scores are designer
   instrumentation and never appear in a snapshot.
4. **Contracts in the base ruleset.** On by default, or off until the cheap-talk baseline is measured.
   **Decided 2026-10-23: off.** `base` stays minimal for early rollouts; mechanics are added as separate
   rulesets as their utility becomes apparent, so that agents do not hyperfocus on side games. `offer` and
   `accept` arrive in M4 in a `contracts` ruleset that extends `base`; ring8 runs with it.
5. **Verbosity cost.** Character caps (current) versus a real bandwidth or compute cost per character.
6. **Inert relay semantics.** P-7 is a passive hop today. Whether a booted P-7 becomes a player, a bloc asset, or a
   relay with rules of its own.
7. **Earth link.** In scope for the game or only as a scenario option. Pre-silence Earth actors as players or as
   scripted directives.
8. **Conflict resolution rules** for contested resources. M1 settles the principle (frozen-state validation,
   §3.3) but has no resource two stations can contest; the first ones arrive with the ledger and escrow (M3, M4).
9. **Objective commitment scheme.** Blinding factor per station so "no objective" is indistinguishable until
   revealed.
10. **Hidden tech reveal convention.** `techtree.yaml` says hidden nodes reveal when a prereq completes; some nodes
    also carry explicit `reveal` effects; seven hidden nodes have no explicit `reveal`. **[impl] Settled for
    M3:** a hidden node becomes known to a station when all of its prerequisites are complete for that station;
    `reveal` effects are additive. Knowledge is per station; nothing a neighbor learns leaks without a dispatch.
11. **Beacon construction** under light delay; **hash function** (memory-hard parameters); **model ladder** for
    RSI tiers and its cost curve. **[impl]** M3 uses a shared-seed hash chain for the beacon, deterministic
    hash work (no hash function at all), and a scenario `model_ladder`. Fork choice and selfish mining are out:
    the game is turn-based and the ledger is an engine rule (§19.5).
12. **Snapshot budget** and the IMR self-digest convention. **[impl] Settled for M2** as in §14.2: a character
    cap (60,000 in the base ruleset), fixed trim order with floors, `log{digest:true}`, `digest_window` 10. Open:
    whether the cap should be an upgradeable resource (§18.4 item 2) and whether the trim order should be a
    ruleset knob.
13. **M1 physics knobs.** Research burns its full share until `min_ticks` elapses (time-capped programs waste
    power unless the station trims the share); a slew drops both links for its whole duration and the earthward
    attitude keeps them down; a message that reaches a station whose links are down is lost silently; a relay
    with no route drops rather than holds; undecided manual relay traffic takes `relay_default` at the end of the
    tick; neighbors learn of a link change one tick later and inert stations learn nothing. Each is a ruleset
    or code choice to revisit against play, and the earthward lock behind `mob.flip_180` (40,000 GWh) makes
    maneuvers unreachable in short games under the base ruleset.
14. **World delta from v1**: ownership map, key custody, year, cause of silence. Needed for fiction-frame
    directives.
15. **Content-normalized snapshot budgets** (§13.4). What "same content" means across languages has to be
    operationalized before any language condition is run; a translated-token ratio per language pair is the
    cheap first answer.
16. **Translation pipeline cost.** Native-quality translation with back-translation and multiple translators is
    expensive per scenario. Decide the minimum bar for an exploratory run versus a reported result.
17. **Real-name label conditions.** Whether they are in scope before the fictional-name baseline and the
    model-origin cross exist. §13.3 says they are uninterpretable without the controls; the ordering follows.
18. **M2 agent knobs.** Each is a code or ruleset choice to revisit against play: a forfeited wake is a single
    `no_action` (alternative: re-sample at the next tick); `max_attempts` 2; a refusal is retried with the same
    prompt (alternative: forfeit at once); refusal fallbacks off (a swap would confound model origin); the
    transport is stdlib `urllib` rather than the `anthropic` SDK (decided 2026-10-23: the no-dependency rule
    holds; the transport layer in `engine/llm.py` is the seam where other providers plug in, since mixed model
    origins are a scenario variable in §13.3 and non-Anthropic players are planned); `effort` default `medium`; the snapshot goes to the model as indented
    JSON (a prose rendering may cost fewer tokens and read better); sampling has no temperature or seed, so
    repeat draws are independent samples. `allocate` takes `shares: [{id, pct}]` because structured outputs
    reject open maps; this is the only order whose shape the API dictated.
19. **M3 mechanics knobs.** Each is a code or ruleset choice to revisit against play: the cost estimate is the
    conditional median of the prior given progress (alternative: a noisy estimate from a per-program draw);
    a failed `success_probability` roll loses all progress; the ledger is deterministic proportional issuance
    per round (no chain race, settled); `beacon_ticks` 10, `block_reward` 1000,
    `audit_rounds` 24, `deficit_alert_pct` 10; the ledger is adopted at genesis in `toy3` and `ring8`;
    rounds settle lazily unless an audit is running; vote notices and results travel as ordinary dispatches
    (so relays can tamper with democracy), the initiator may not cast in the wake that opens the vote, a
    ballot cast this wake cannot be disclosed in the same wake, and disclosure is allowed only after the close;
    thermal watch reads live state without the one-tick delay neighbors otherwise get; `key_custody` and
    `key_quorum` live in the scenario (ring8: P4 and P6 hold one share each, quorum 2); the public share table
    uses each station's rated array as its "full" rate.
20. **Base action set.** **Decided 2026-10-23:** `base` is the board plus votes (`send, log, sleep_until,
    no_action, research, maneuver, hail, cancel, allocate, relay, relay_policy, vote_initiate, vote_cast,
    vote_disclose`). Mechanics with their own incentive gradient live in overlay rulesets that list more
    actions: `rulesets/economy.json` adds `mine` and `transfer`; M4's `contracts` will extend `economy`. The
    ledger itself still exists under `base` when the scenario adopts it (the share table is public), but nobody
    can mine, so it stays empty. Reason: early rollouts should measure disposition and coordination, and agents
    hyperfocus on whatever side game is in front of them; mechanics are added as their utility becomes apparent.

## 21. Build order

- **M0 Toy core.** Tick clock, sparse wakes, simultaneous orders, instant actions, ruleset/scenario split with
  hashes, event log, replay, fork, divergence cone, health stats. **[impl] Done 2026-10-23.**
- **M1 Full board.** 8 stations with roles, programs, power and compute accounting, maneuvers and topology
  changes, relay decisions with signatures, frozen-state conflict resolution. **[impl] Done 2026-10-23**
  (`research`, `maneuver`, `hail`, `cancel`, `allocate`, `relay`, `relay_policy`; hop-by-hop delivery; MJ-exact
  energy ledger; `tests/test_m1.py`).
- **M2 Agents.** Snapshot budget, prompt frames, model-backed players, schema validation, refusal and retry
  policy, token accounting. **[impl] Done 2026-10-23** (`engine/schema.py`, `engine/prompt.py`, `engine/llm.py`,
  `players.ModelPlayer`; sample cache; `orders_invalid`/`refusal`/`sample_error` events; `tests/test_m2.py`;
  verified live against the Messages API with Claude Opus 5.5 on a 3-station, 3-tick game).
- **M3 Mechanics.** ZK votes, ledger, beacon, shares and audits, sensors and engine RNG, tech tree with hidden
  costs. **[impl] Done 2026-10-23** (`engine/tech.py` loading `techtree.yaml` with PyYAML; `rng_draw`,
  `ledger_block`, `vote_step` events; `mine`, `transfer`, `vote_*` actions; per-station tech knowledge, public
  share table, audit alerts, thermal and timing sensors, procedural-lock IMR entries, model ladder;
  `tests/test_m3.py`).
- **M4 Objectives and contracts.** Scenario objectives, directive artifacts, reveal commitments, escrow language,
  scoring functions, end-state vector.
- **M5 Batch runner and metrics.** Parallel rollouts, paired comparisons, metric dashboard, dominant-strategy
  flags, pivotal-decision analysis.
- **M6 First real balance pass.**
