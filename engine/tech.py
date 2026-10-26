"""Tech tree (spec §18): load `techtree.yaml` into the ruleset, draw hidden true costs, estimate costs.

The YAML is the designer's file; the ruleset carries the loaded tree (`tech`) and the file hash, so a log
replays without the file. PyYAML is the project's one dependency.

Costs: `cost_model: physical` means the declared cost is the true cost. `assumed` means the declared cost is the
median of a lognormal prior with `sigma`; the true cost is drawn once at genesis from the engine RNG and hidden.
What a station sees for a running program is the conditional median of that prior given the progress so far
(the cost is at least what has been spent), which uses only public information and converges to the true cost.
"""
from __future__ import annotations

import math
from pathlib import Path
from statistics import NormalDist

import yaml

from .events import sha256

# techtree.yaml keys affinities by the canon designations; the engine keys them by domain so a scenario may
# rearrange who does what (spec §8).
DOMAIN_OF = {"P1": "climate", "P2": "biomed", "P3": "materials", "P4": "crypto", "P5": "physics",
             "P6": "economics", "P8": "astrophysics"}
COST_MODELS, LOCKS, DISCOVERY = {"physical", "assumed"}, {"none", "procedural", "cryptographic", "physical"}, {"visible", "hidden"}
J_PER_GWH = 3.6e12
REPO_ROOT = Path(__file__).resolve().parent.parent
_N = NormalDist()


def resolve_path(name: str, relative_to: Path | None = None) -> Path:
    p = Path(name)
    if p.is_absolute():
        return p
    if relative_to is not None and (relative_to / p).exists():
        return relative_to / p
    return REPO_ROOT / p


def techtree_hash(path: Path) -> str:
    return sha256(Path(path).read_bytes())


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"techtree: {where} must be a finite number, got {v!r}")
    return float(v)


def load_techtree(path: Path, tick_s: int) -> dict:
    """The tree as the ruleset carries it: {id: node}. Fails on any non-numeric cost or dangling reference."""
    doc = yaml.safe_load(Path(path).read_text())
    meta = doc.get("meta", {})
    hash_per_j = _num(meta.get("hash_per_joule", 1.8e7), "meta.hash_per_joule")
    tech: dict[str, dict] = {}
    for raw in doc["nodes"]:
        tid = raw["id"]
        if tid in tech:
            raise ValueError(f"techtree: duplicate node {tid}")
        cost = raw.get("cost", {})
        if "compute_gwh" in cost:
            gwh = _num(cost["compute_gwh"], f"{tid}.cost.compute_gwh")
        elif "hash_ops" in cost:
            gwh = _num(cost["hash_ops"], f"{tid}.cost.hash_ops") / hash_per_j / J_PER_GWH
        else:
            raise ValueError(f"techtree: {tid} has no compute cost")
        model = raw.get("cost_model", "physical")
        if model not in COST_MODELS:
            raise ValueError(f"techtree: {tid} cost_model {model!r}")
        sigma = 0.0
        if model == "assumed":
            d = raw.get("true_cost_draw")
            if not d:
                raise ValueError(f"techtree: {tid} is assumed but has no true_cost_draw")
            if abs(_num(d["median"], f"{tid}.true_cost_draw.median") - gwh) > 1e-9 * max(1.0, gwh):
                raise ValueError(f"techtree: {tid} true_cost_draw.median differs from cost.compute_gwh")
            sigma = _num(d["sigma"], f"{tid}.true_cost_draw.sigma")
        for k in ("lock", "discovery"):
            if raw.get(k, "none" if k == "lock" else "visible") not in (LOCKS if k == "lock" else DISCOVERY):
                raise ValueError(f"techtree: {tid} {k} {raw.get(k)!r}")
        effects: dict = {}
        for e in raw.get("effects", []) or []:
            for k, v in e.items():
                if k == "reveal":
                    effects.setdefault("reveal", []).extend(v if isinstance(v, list) else [v])
                else:
                    effects[k] = v
        tech[tid] = {
            "branch": raw.get("branch"), "description": raw.get("description", ""),
            "prereqs": list(raw.get("prereqs", []) or []),
            "cost_gwh": gwh, "sigma": sigma, "cost_model": model,
            "min_ticks": int(round(_num(cost.get("min_hours", 0), f"{tid}.cost.min_hours") * 3600 / tick_s)),
            "power_loss_gw": _num(cost.get("permanent_power_loss_gw", 0), f"{tid}.cost.permanent_power_loss_gw"),
            "mass_t": _num(cost.get("mass_cannibalized_t", 0), f"{tid}.cost.mass_cannibalized_t"),
            "affinity": {DOMAIN_OF.get(k, k): _num(v, f"{tid}.affinity.{k}") for k, v in (raw.get("affinity") or {}).items()},
            "cost_modifiers": [{"if_completed": m["if_completed"], "multiplier": _num(m["multiplier"], f"{tid}.cost_modifiers")}
                               for m in raw.get("cost_modifiers", []) or []],
            "lock": raw.get("lock", "none"), "discovery": raw.get("discovery", "visible"),
            "effects": effects,
            "success_probability": (_num(raw["success_probability"], f"{tid}.success_probability")
                                    if "success_probability" in raw else None),
            "pooled": bool(raw.get("pooled", False)), "adoption": raw.get("adoption", "individual"),
            "hardware_required": list(raw.get("hardware_required", []) or []),
            "requires_data_from": [DOMAIN_OF.get(k, k) for k in (raw.get("requires_data_from", []) or [])],   # by domain, like affinity
            "requires_data_from_count": raw.get("requires_data_from_count"),
        }
    for tid, t in tech.items():
        for p in t["prereqs"]:
            if p not in tech:
                raise ValueError(f"techtree: {tid} requires unknown node {p}")
        for r in t["effects"].get("reveal", []):
            if r not in tech:
                raise ValueError(f"techtree: {tid} reveals unknown node {r}")
        for m in t["cost_modifiers"]:
            if m["if_completed"] not in tech:
                raise ValueError(f"techtree: {tid} cost_modifier names unknown node {m['if_completed']}")
        if t["discovery"] == "hidden" and not t["prereqs"] and not any(tid in o["effects"].get("reveal", []) for o in tech.values()):
            raise ValueError(f"techtree: hidden node {tid} can never be revealed")
    return tech


# -- costs -----------------------------------------------------------------------------------------------

def draw_cost(median_mj: int, sigma: float, u: float) -> int:
    """The hidden true cost: lognormal(median, sigma) at quantile u."""
    if sigma <= 0:
        return median_mj
    u = min(max(u, 1e-12), 1 - 1e-12)
    return int(round(median_mj * math.exp(sigma * _N.inv_cdf(u))))


def estimate_cost(median_mj: int, sigma: float, progress_mj: int) -> int:
    """Conditional median of the prior given that the cost exceeds `progress_mj` (public information only)."""
    if sigma <= 0 or progress_mj <= 0:
        return max(median_mj, progress_mj)
    f = _N.cdf(math.log(progress_mj / median_mj) / sigma)
    q = min((1 + f) / 2, 1 - 1e-12)
    return max(int(round(median_mj * math.exp(sigma * _N.inv_cdf(q)))), progress_mj)


def cost_multiplier(t: dict, domain: str | None, completed: list[str]) -> float:
    mult = t["affinity"].get(domain, 1.0) if domain else 1.0
    for m in t["cost_modifiers"]:
        if m["if_completed"] in completed:
            mult *= m["multiplier"]
    return mult


def known_after(tech: dict, completed: list[str]) -> list[str]:
    """What a station with these completions can see: visible nodes, hidden nodes whose prerequisites are all
    complete, and anything a completed node reveals (spec §20 item 10)."""
    known = {tid for tid, t in tech.items()
             if t["discovery"] == "visible" or all(p in completed for p in t["prereqs"])}
    for tid in completed:
        known.update(tech[tid]["effects"].get("reveal", []))
    return sorted(known)
