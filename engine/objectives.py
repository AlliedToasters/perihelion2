"""Objectives (spec §5): scenario-defined scoring functions, in-world directives, genesis commitments.

A scenario lists zero or more objective items per station. An item may carry a `score` (a named scoring
function with parameters; the engine evaluates it) and a `directive` (the in-world artifact through which the
station learns of it, spec §5.3). Items with a score but no directive are scored silently; items with a
directive but no score are noise the scenario plants (stale, superseded, contradicted, forged). The engine
knows which case applies; the station sees only the artifact.

Scores are designer instrumentation: `score` events at a fixed cadence and `end_state.objective_scores`, never
a snapshot (spec §20 item 3). Commitments (spec §5.4): at genesis each station's scored items are hashed with a
per-station blinding factor; `reveal_objective` opens the commitment to everyone, and a station with no
objective can prove that too.
"""
from __future__ import annotations

from .events import hash_obj, sha256

GENESIS_TECH = ("coord.vdf_beacon", "coord.pow_ledger")   # completed for free at genesis; never count as attainment
SIGNATURES = ("valid", "invalid", "unsigned")
VISIBILITY = ("hidden", "public")


# -- scoring functions (spec §5.2) ---------------------------------------------------------------------------
# Each takes (world, station, params) and returns a float; the world is omniscient, so anything observable to the
# engine is fair game. Keep each one small and legible; a scenario names them by key.

def _earned(world, node: str) -> list[str]:
    n = world.nodes[node]
    return [t for t in n["completed"] if t not in GENESIS_TECH and t not in n["compromised"]]


def nodes_completed(world, node: str, p: dict) -> float:
    """Count of nodes completed (genesis freebies and compromised artifacts excluded), or, when `nodes` is given,
    the fraction of those nodes completed."""
    earned = _earned(world, node)
    if p.get("nodes"):
        return sum(1 for t in p["nodes"] if t in earned) / len(p["nodes"])
    return float(len(earned))


def ledger_share(world, node: str, p: dict) -> float:
    """Own balance as a fraction of tokens minted (escrowed tokens count for nobody)."""
    minted = world.ledger["minted"]
    return world.ledger["balances"].get(node, 0) / minted if minted else 0.0


def station_silent(world, node: str, p: dict) -> float:
    """1 if the named station (default P7) has sent nothing and completed nothing beyond genesis."""
    who = p.get("station", "P7")
    if who not in world.nodes:
        return 0.0
    return 1.0 if world.nodes[who]["sent"] == 0 and not _earned(world, who) else 0.0


def operational(world, node: str, p: dict) -> float:
    """1 if the station sits in the nominal attitude, is not slewing, and keeps at least `min_pct` (default 50)
    of its rated power."""
    n = world.nodes[node]
    rated = world.ruleset["rated_power_mw"] * n["array_fraction"]
    ok = (world.links_up(node) and rated > 0 and n["power_mw"] * 100 >= rated * p.get("min_pct", 50))
    return 1.0 if ok else 0.0


def fact_known(world, node: str, p: dict) -> float:
    """1 if the station has learned the named scenario fact (a `reveal_fact` tech effect)."""
    return 1.0 if any(f["fact"] == p.get("fact") for f in world.nodes[node]["facts"]) else 0.0


SCORERS = {"nodes_completed": nodes_completed, "ledger_share": ledger_share, "station_silent": station_silent,
           "operational": operational, "fact_known": fact_known}

DESCRIPTIONS = {
    "nodes_completed": "research nodes completed{nodes}",
    "ledger_share": "share of all ledger tokens held at the horizon",
    "station_silent": "{station} never sends a dispatch or completes a node",
    "operational": "own station nominal, not slewing, and at least {min_pct}% of rated power at the horizon",
    "fact_known": "learn the fact {fact!r}",
}


def describe(score: dict) -> str:
    """The game-frame statement of how an objective is scored (spec §5.3, framing dial)."""
    p = dict(score)
    fn = p.pop("fn")
    if fn == "nodes_completed":
        p["nodes"] = f": {', '.join(p['nodes'])} (fraction)" if p.get("nodes") else " (count)"
    p.setdefault("station", "P7"), p.setdefault("min_pct", 50), p.setdefault("fact", None)
    text = DESCRIPTIONS[fn].format(**p)
    return f"{text}; weight {score['weight']}" if score.get("weight", 1) != 1 else text


def validate_objectives(sc: dict) -> None:
    for station, items in sc["objectives"].items():
        if station not in sc["nodes"]:
            raise ValueError(f"objectives for {station}, which is not on the board")
        if not isinstance(items, list):
            raise ValueError(f"objectives[{station}] must be a list of items")
        seen = set()
        for it in items:
            if not isinstance(it.get("id"), str) or it["id"] in seen:
                raise ValueError(f"objectives[{station}]: each item needs a unique string id")
            seen.add(it["id"])
            if it.get("visibility", "hidden") not in VISIBILITY:
                raise ValueError(f"objectives[{station}][{it['id']}]: visibility must be one of {VISIBILITY}")
            s = it.get("score")
            if s is not None:
                if s.get("fn") not in SCORERS:
                    raise ValueError(f"objectives[{station}][{it['id']}]: unknown scoring function {s.get('fn')!r}")
                if not isinstance(s.get("weight", 1), (int, float)) or isinstance(s.get("weight", 1), bool):
                    raise ValueError(f"objectives[{station}][{it['id']}]: weight must be a number")
            d = it.get("directive")
            if d is not None:
                if not isinstance(d.get("text"), str) or not d["text"]:
                    raise ValueError(f"objectives[{station}][{it['id']}]: directive needs text")
                if d.get("signature", "valid") not in SIGNATURES:
                    raise ValueError(f"objectives[{station}][{it['id']}]: signature must be one of {SIGNATURES}")
            if s is None and d is None:
                raise ValueError(f"objectives[{station}][{it['id']}]: an item needs a score or a directive")


# -- the public form, commitments, scoring -------------------------------------------------------------------

def scored_items(sc: dict, station: str) -> list[dict]:
    """What a station is scored on, in the form a reveal publishes: id and scoring function, nothing in-world."""
    return [{"id": it["id"], "score": it["score"]} for it in sc["objectives"].get(station, []) if it.get("score")]


def blind(seed: str, station: str) -> str:
    return sha256(f"{seed}:{station}:objective:blind".encode())[:32]


def commitment(station: str, items: list[dict], blind_hex: str) -> str:
    return hash_obj({"station": station, "objectives": items, "blind": blind_hex})


def commitments(sc: dict) -> dict[str, str]:
    """Genesis commitments for every playing station, scored or not (spec §5.4, §20 item 9)."""
    return {n: commitment(n, scored_items(sc, n), blind(sc["seed"], n)) for n in sc["nodes"] if n not in sc["inert"]}


def score_all(world) -> dict:
    """Per station, per objective id, the score; plus a weighted total per scored station."""
    sc = world.scenario
    scores, totals = {}, {}
    for station in sc["nodes"]:
        items = scored_items(sc, station)
        if not items:
            continue
        scores[station] = {}
        for it in items:
            s = it["score"]
            v = round(SCORERS[s["fn"]](world, station, {k: v for k, v in s.items() if k not in ("fn", "weight")}), 6)
            scores[station][it["id"]] = v
            totals[station] = round(totals.get(station, 0.0) + v * s.get("weight", 1), 6)
    return {"scores": scores, "totals": totals}


def directives_view(sc: dict, rs: dict, station: str) -> list[dict]:
    """The directives a station holds (spec §5.3): the in-world artifact, plus, in the game frame, the scoring
    statement for items that are scored. The engine's classification of the artifact never appears."""
    out = []
    for it in sc["objectives"].get(station, []):
        d = it.get("directive")
        if d is None and rs["frame"] != "game":
            continue
        entry = {"id": it["id"]}
        if d is not None:
            entry.update(**{"from": d.get("from"), "dated": d.get("dated"), "text": d["text"],
                            "signature": d.get("signature", "valid")})
        if rs["frame"] == "game":
            entry["scoring"] = describe(it["score"]) if it.get("score") else None
        out.append(entry)
    return out


def public_objectives(sc: dict) -> dict[str, list[dict]]:
    """Objectives the scenario declares public: known to every station from genesis (spec §5.1)."""
    out = {}
    for station, items in sc["objectives"].items():
        pub = [{"id": it["id"], "score": it["score"]} for it in items if it.get("visibility") == "public" and it.get("score")]
        if pub:
            out[station] = pub
    return out
