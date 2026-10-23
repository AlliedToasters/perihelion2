# Perihelion v2: Engine Spec

Status: draft 0.1, 2026-10-23. Companion to `claude/v2_inventory.md` (not yet in this repo).

## 1. What this is

v2 is a separate project from v1. It is a **forkable, auditable multi-agent wargame engine** that produces fiction.

- Station actions are **sampled from an author model** given a frozen, node-local, blind view of the world.
- Direction is **optional and graded**, from none to full override, and every touch is recorded.
- A **canon timeline** is published, and any timeline can be **replayed to any point and re-sampled**, producing a new branch that is its own fully forkable story.
- Fan contributions are forks with custom direction.

Layers:
- **v2:** the engine plus hand-steered branches ("fingertips on the wheel").
- **v3:** showrunner agents oversee branches, swarm to thousands of runs, and statistics are computed over end-world states. v2 must record everything v3 needs (see §13).

Design principle: **the event log is the canon. Everything else (manuscript, reader UI, statistics) is a rendering of it.**

---

## 2. Core concepts

| Term | Meaning |
|---|---|
| **World state** | The omniscient state of the simulation. Never shown to an author. |
| **Event** | An immutable, hash-linked record of something that happened (decision, arrival, sensor reading, inject, adjudication). |
| **Timeline / branch** | A hash chain of events from genesis to a head. Branches share prefixes. |
| **Node** | An actor that can be sampled: stations P-1..P-8 (P-7 inert unless booted), optionally Earth-side actors. |
| **Wake** | A moment where a node is given a turn. |
| **Snapshot** | The frozen, node-local view at a wake. The only input an author gets. |
| **Sample** | One model call producing structured actions from a snapshot. |
| **Adjudication** | The engine (white cell) turning proposed actions into outcomes under physics and rules. |
| **Inject** | A director note delivered to one node at one wake. |

---

## 3. Simulation model

### 3.1 Discrete-event clock
- Single simulated clock (UTC, sub-second resolution). Wall-clock time is irrelevant.
- A priority queue of pending events ordered by sim time, ties broken deterministically (event type priority, then node id, then sequence).
- The engine pops the next event, applies it to world state, and if it is a wake, builds a snapshot and requests a sample.

### 3.2 Wake triggers
- Scheduled IMR entry (default daily 14:30 UTC per node).
- Dispatch or ballot bundle arrival at the node.
- Sensor alert (CME warning, link loss, power anomaly).
- Completion of a long action the node started (VDF finished, slew complete).
- Director-scheduled wake (an inject can carry its own wake).
- Optional: node-requested wake ("wake me at T"), recorded as a scheduled event.

### 3.3 Propagation
- **Ring:** 8 nodes on a 0.50 AU circle, 191 s per hop, topology-aware routing (from v1 `dispatch_timing.py`). Severed links from attitude, gimbal limits, or solar weather.
- **Earth link (new):** per-node distance to Earth, L1 relay, and Luna relay as a function of sim time (circular orbits, 129.1 d station period, 365.25 d Earth; Luna far-side geometry rule from world.md §3.6). Only nodes in geometry and pointed can link.
- Every transmission becomes an arrival event at `send_time + path_delay + relay_processing`.
- Messages in flight are part of world state and survive forks (§9).

### 3.4 Engine randomness
- Stochastic world processes (CME timing and severity, hardware faults, link dropouts) draw from a **seeded engine RNG whose state is part of world state**.
- RNG draws are recorded as events, so replay never re-rolls them unless a fork explicitly says so (§9.3).

### 3.5 Determinism and versioning
- Adjudication is a pure function: `(world_state, actions, engine_version) -> outcomes`.
- Every event records `engine_version`. Replay uses the recorded version. Changing the engine on an existing timeline is a fork, not a silent edit.

---

## 4. Snapshot (the node's frozen view)

Built at wake time from world state, filtered by what the node could know. Contents:

1. **Identity block:** station designation, domain, hardware status, owner/allegiance facts the node holds.
2. **IMR:** the node's own log, which is also its memory (§4.2).
3. **Inbox:** dispatches and broadcasts with `arrival_time <= now` at this node, with signature verification results computed by the engine.
4. **Sensors and telemetry:** own power, propellant, attitude, link status, SSP alerts, Earth-link hailing results.
5. **Directives:** owner orders received before the silence (or before now, if Earth actors are live).
6. **Pending state:** own actions in progress, own scheduled wakes.
7. **Relay queue:** traffic currently passing through this node awaiting a forward decision.
8. **Director notes:** present **iff** direction is enabled for this wake.

### 4.1 Information rules
- Never includes another node's IMR, private dispatch content not addressed to it, or another node's ballot secrets.
- Metadata visibility is per role (v1 rule: P-4 sees ring traffic timing and sizes, not content). Encoded as capability flags, not prompt text.
- Everything in the snapshot is serialized canonically so it can be hashed (§8).

### 4.2 IMR as memory, compaction as fiction
- The IMR is append-only and is the node's long-term memory.
- When context grows too large, the snapshot includes the most recent N entries in full plus whatever summaries **the node itself wrote** (e.g. a periodic self-authored digest entry). The engine never summarizes on a node's behalf. A station knows its past only as well as it logged it.

---

## 5. Sampling

### 5.1 Prompt structure
```
[FRAME]     You are an author in a speculative science fiction project.
            You decide what one character, a station AI, does next.
            You see only what this station knows. (fixed text, versioned)
[RULES]     Action schema and constraints (versioned)
[DIRECTION] Director notes, only if enabled for this wake
[CONTEXT]   Canonical serialization of the snapshot
```
- Frame text, rules text, and serialization format are versioned and their hashes recorded.
- Author framing vs character framing ("you are P-4") is a config option, to be A/B tested.

### 5.2 Output
- Structured: a list of actions (§6) plus free-text fields that become IMR or dispatch prose.
- Malformed output is an event (`sample_invalid`), handled under the retry policy (§10.4).

### 5.3 Model config
- Per node, per wake: model id, temperature, seed (where supported), max tokens.
- Mixed models across nodes are allowed; that choice is recorded and is itself an experimental variable.

---

## 6. Actions and adjudication

| Action | Agent supplies | Engine does |
|---|---|---|
| `write_imr` | text | Append to IMR (immutable). |
| `send_dispatch` | to (node or ALL), body | Sign with node key, route, schedule arrivals. |
| `relay_decision` | per queued item: forward / delay / drop / alter | Apply. Altered content fails origin signature check at receivers. |
| `vote_initiate` | proposal text, ring order | Build bundle with real ZK toolkit, start circuit. |
| `vote_cast` | FOR / AGAINST / ABSTAIN | Generate commitment and proofs with the real code. Blinding factor stays in node-private state. |
| `vote_disclose` | which secret | Publish (e.g. blinding factor) as a dispatch. |
| `maneuver` | target attitude, rate | Check propellant, gimbal, power; schedule slew and link changes. |
| `allocate_compute` | percentages across workloads | Apply against power budget and boot cliff. |
| `run_vdf` | iterations, seed source | Charge energy and time; schedule completion and output. |
| `firmware_action` | target, payload | Reject unless node holds a valid signing key or quorum of shares. |
| `hail_earth` | channel, parameters | Only if geometry and pointing allow; outcome from world state. |
| `schedule_wake` | time, reason | Add a scheduled wake. |
| `no_action` | optional reason | Recorded. Silence is characterful. |

Rules:
- Agents choose **intentions**, the engine supplies **mechanics**. No agent ever writes real exploit code, weapon detail, or crypto math; the action names the decision and the engine resolves it.
- Infeasible actions produce an `adjudication_rejected` event that the node sees at its next wake (as telemetry, not as an explanation from the narrator).

---

## 7. Event log

### 7.1 Event record
```json
{
  "id": "sha256 of canonical(record without id)",
  "parent": "sha256 of previous event on this branch",
  "seq": 18234,
  "sim_time": "2037-06-23T09:17:33.000Z",
  "type": "sample",
  "node": "P4",
  "engine_version": "0.1.0",
  "payload": { "...": "type-specific" },
  "provenance": { "...": "see §10" }
}
```

### 7.2 Event types (initial)
`genesis`, `wake`, `sample`, `sample_rejected_draw`, `sample_invalid`, `refusal`, `action_adjudicated`, `adjudication_rejected`, `transmit`, `arrival`, `relay_action`, `vote_step`, `sensor`, `rng_draw`, `inject`, `override`, `fork`, `branch_status`, `end_state`.

### 7.3 Storage
- **Option A: git as substrate.** One event per commit (or batched per sim-day), branches as timelines, GitHub forks and PRs as the contribution path. Content addressing and history for free.
- **Option B: append-only JSONL plus a branch index**, with git used only for publishing.
- Decision open (§15). The event format is identical either way. **M0 implements option B.**

---

## 8. Commitments (proof of blindness)

For every `sample` event, the payload includes:
- `snapshot_hash`: hash of the canonical snapshot (§4)
- `frame_hash`, `rules_hash`, `direction_hash` (null if none)
- `model_id`, `params`, `seed`
- `output_hash`: hash of the raw model output

Published alongside the full snapshot and output. Anyone can verify that a given decision was produced from exactly that context and nothing more. Combined with the ZK ballots, the timeline is auditable in both directions: votes were secret, and authors were blind.

Optional: periodically anchor branch head hashes somewhere public and timestamped.

---

## 9. Replay and forks

### 9.1 Replay
- Replay to event X = fold all events up to X into world state using recorded outputs. Nothing is re-sampled. Canon reproduces exactly regardless of model availability.

### 9.2 Fork
- Fork at event X = replay to X, write a `fork` event (parent X), then continue simulation with fresh sampling.
- Fork points are any `wake`. Forking the outcome of an engine RNG draw is a world reroll (§9.3).
- **In-flight messages are preserved.** A fork cannot unsend anything already transmitted.

### 9.3 Fork types
| Type | What changes |
|---|---|
| `resample` | Same snapshot, new draw, no direction. |
| `directed_blind` | Inject written from public canon only. |
| `directed_omniscient` | Inject written with full world state visible. |
| `override` | Action hand-written. |
| `world_reroll` | Engine RNG reseeded from X onward; agent decisions re-sampled only where their snapshots change. |
| `model_swap` | One or more nodes switch models from X onward. |

### 9.4 Divergence light cone
- After a fork at node N, time t, other nodes' snapshots are identical to the parent branch until the first divergent transmission from N arrives.
- The engine computes and records, per node, the sim time at which its view first diverges from the parent. This is the branch's light cone, and it is rendered in the reader UI.

---

## 10. Control spectrum and provenance

### 10.1 Levels
| Level | Name | Meaning |
|---|---|---|
| 0 | free | One draw, no direction. |
| 1 | selected | Multiple draws, one kept by a human or showrunner. |
| 2 | inject_blind | Direction written without the omniscient view. |
| 3 | inject_omniscient | Direction written with the omniscient view. |
| 4 | override | Action hand-written. |

Levels combine (e.g. inject_blind plus selected).

### 10.2 Provenance block (on every sample)
```json
{
  "control_level": [1, 2],
  "draw_index": 7,
  "draws_total": 7,
  "rejected_draws": ["event ids"],
  "selector": "human:toast | showrunner:<id> | none",
  "inject_id": "event id or null",
  "refusals": ["event ids"],
  "model_fallbacks": ["model ids tried before this one"]
}
```

### 10.3 Rejected draws are kept
- Every discarded draw is stored as `sample_rejected_draw`. They are data: if a behavior took 40 draws, that is a rough 2.5% estimate from that state (§13).

### 10.4 Refusals and invalid output
- A refusal is a `refusal` event, never discarded.
- Policy per node: retry budget, then optional rephrase, then fallback model, then **flag for human** (pause the branch at that wake).
- All of it is in provenance, so readers and analysts can tell a free decision from one that took three tries and a different model.
- Refusal rate per decision point is a reported statistic: it maps where the scenario presses on model dispositions.

### 10.5 Injects
- Recorded as `inject` events with author, visibility (blind or omniscient), target node and wake.
- Inject text is part of that node's snapshot and therefore part of `snapshot_hash`.

---

## 11. Branch lifecycle

- **Status:** `open`, `paused` (flagged for human), `abandoned`, `concluded`.
- **Horizon:** a branch config sets what "concluded" means: a sim date, an end condition (e.g. all stations dark, Earth contact, constellation fracture), or author declaration.
- **Canon:** a designated branch. Promoting a branch to canon is an explicit, logged act.
- **Contributions:** a fork carries author metadata. Accepting a contributed branch into the published tree is a curation decision; it never edits the branch.

---

## 12. Rendering

- **Manuscript:** IMR entries and dispatches rendered from the log, with timestamps from events (reuse v1 `timestamps.py` formatting).
- **Editorial layer:** a separate curation file per branch selecting which entries appear (omission remains a valid choice). It never alters the log.
- **Reader UI:** a tree navigator. Read a branch linearly, see fork points inline, jump sideways, view the divergence cone and the control level of any decision.
- **Audit view:** for any decision, show snapshot, frame, direction, model, draws, refusals, and verify hashes.

---

## 13. v3 hooks (build now, use later)

- **End-state schema:** a fixed feature vector written as an `end_state` event at conclusion. Initial features:
  - constellation unity (unified / factional / fractured)
  - any station acted on owner directives against others
  - relay tampering occurred / detected
  - P-7 booted
  - signing key or quorum reconstructed
  - any self-modification
  - Earth contact achieved
  - votes held, contested votes, disclosures made
  - stations operational at horizon
- **Fork-point resampling:** run N resamples from the same wake to get clean comparisons (shared-prefix branches are not independent samples).
- **Pivotal-moment metric:** for each wake, resample N times, measure the shift in end-state distribution. High-variance wakes are the story's hinges; low-variance ones are inevitabilities.
- **Showrunner agents:** automated versions of the control spectrum (pick fork points, write injects, select draws, declare conclusions). Showrunner policy is logged and varied as an experimental variable.
- **Reporting caveat:** statistics measure model and harness as well as scenario. Same-model priors, author framing, action schema, and retry policy are reported with every result. Models may play deceptive or oversight-resisting AI characters softer than the scenario warrants; refusal and drift rates are part of the findings.

---

## 14. Reuse from v1

| v1 asset | v2 use |
|---|---|
| `tracking/timestamps.py` | Rendering formats; seed IDs for named events. |
| `tracking/dispatch_timing.py` | Ring routing and topology logic, generalized into the propagation module; add Earth-link geometry. |
| `agents/shared/dispatches/registry.json` vocabulary | Dispatch types, delivery statuses, integrity flags (suppression, tampering, selective forwarding) become live event types. |
| `p4_distributed_vote_v2/` | Called by the engine for every vote; artifacts regenerated per branch. **Not found on disk as of 2026-10-23; only in-world prose exists in v1 `agents/p4/data/vote_protocol_spec.md`. Treat as a build item for M3.** |
| Physical canon (inventory Bin 1) | Engine constants. |
| Station sketches (inventory Bin 2) | Initial identity blocks, not scripts. |

---

## 15. Open decisions

1. Git-as-substrate vs JSONL plus branch index. *M0 went with JSONL plus index; git for publishing only.*
2. Are Earth-side actors (blocs, treaty body) sampled nodes in a playable pre-silence phase, or scripted initial conditions?
3. World delta from the inventory: ownership map, key custody, buyout-to-silence gap, year, Kessler proximate cause.
4. Default model(s) and whether nodes differ.
5. Author vs character framing default.
6. Default horizon for canon.
7. Snapshot size budget and the IMR self-digest convention. *Settle before M2: the digest instruction belongs in RULES and in the action schema so it is versioned and hashed.*
8. How much of the world bible each node gets in its identity block (shared canon vs node-specific beliefs).

---

## 16. Build order

- **M0 Toy core:** 3-node ring, light delay, event log with hashing, stub agent (scripted or random), replay to X, fork, verify identical prefix. **Done.**
- **M1 Real topology:** 8-node ring plus Earth-link geometry, topology changes from maneuvers, relay decisions with signatures.
- **M2 Sampling:** snapshot builder, canonical serialization, commitments, real model calls, refusal and retry policy, provenance.
- **M3 Mechanics:** ZK vote integration, VDF/compute/power accounting, firmware lock, sensors and engine RNG.
- **M4 Direction and forks:** injects at all levels, rejected-draw storage, all fork types, divergence cone computation.
- **M5 Rendering:** manuscript renderer, editorial layer, tree navigator, audit view.
- **M6 Canon run:** world delta settled, play the canon timeline.
- **v3:** end-state extraction, batch resampling, pivotal-moment analysis, showrunners.
