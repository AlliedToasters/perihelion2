# Perihelion v2

A forkable, auditable multi-agent wargame engine that produces fiction. See `SPEC.md`.
Design principle: **the event log is the canon.** Everything else is a rendering of it.

Stdlib Python 3.12 only. No dependencies.

## Status: M0 (toy core) done

- N-node ring with 191 s per-hop light delay and shortest-path routing
- Append-only JSONL event log per branch, sha256 hash-chained, canonical serialization
- World state rebuilt purely by folding events; replay to any event
- Fork (`resample`) at any wake; shared prefix copied verbatim and verified
- Divergence light cone per node (first wake whose snapshot hash differs from the parent)
- Stub authors: `scripted` (deterministic) and `random` (seeded by sample seed + snapshot hash)
- Provenance block on every sample (control level 0 only)

## Commands

```bash
python3 -m engine run runs/demo --nodes 3 --agent random --seed 1 --days 3
python3 -m engine wakes runs/demo main                 # list fork points
python3 -m engine fork runs/demo main <wake-id> --name alt --seed 7
python3 -m engine verify runs/demo                     # hash chains + shared prefixes
python3 -m engine replay runs/demo alt [--to <event-id>]
python3 -m engine render runs/demo alt                 # IMR entries and dispatches
python3 -m engine diverge runs/demo alt
python3 -m unittest discover -s tests
```

## Layout

```
engine/
  events.py       Event record, canonical JSON, sha256, chain verification
  log.py          EventLog (JSONL per branch) and RunIndex (runs/<run>/index.json)
  propagation.py  ring index, neighbors, shortest path with severed links, hop delay
  world.py        WorldState; every mutation lives in apply(event)
  snapshot.py     node-local frozen view + hash (spec §4)
  agents.py       stub authors, frame/rules text and hashes
  engine.py       run loop, wake handling, adjudication
  replay.py       replay, fork, verify_run, divergence
  cli.py          python3 -m engine ...
tests/test_m0.py  acceptance tests for M0
runs/             output, gitignored
```

## Run layout

```
runs/<run>/index.json           config, canon branch, branch table (file, head, parent, fork point, seed)
runs/<run>/branches/<b>.jsonl   one event per line, hash-chained
runs/<run>/snapshots/<hash>.json  every snapshot an author saw, by hash
```

## Invariants the tests enforce

1. Replaying a branch twice yields the same world digest.
2. Two runs with the same seed yield identical event ids.
3. A fork shares an identical prefix with its parent through the fork wake, then a `fork` event.
4. A fork with the parent's seed reproduces the parent's samples (snapshot hash, actions, output).
5. Editing any event payload breaks `verify`.
6. Fork points must be `wake` events.

## Next: M1

8-node ring plus Earth-link geometry (per-node distance to Earth, L1 and Luna relays as a function
of sim time), topology changes from maneuvers, relay queues with per-item forward/delay/drop/alter
decisions, origin signatures on dispatches.
