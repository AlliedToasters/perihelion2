"""Event records: canonical serialization, hashing, hash chain."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from . import ENGINE_VERSION

EVENT_TYPES = {
    "genesis", "wake", "sample", "sample_rejected_draw", "sample_invalid", "refusal",
    "action_adjudicated", "adjudication_rejected", "transmit", "arrival", "relay_action",
    "vote_step", "sensor", "rng_draw", "inject", "override", "fork", "branch_status", "end_state",
}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj) -> str:
    return sha256(canonical(obj))


def iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def parse_iso(s: str) -> int:
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000 + dt.microsecond // 1000


@dataclass
class Event:
    parent: str | None
    seq: int
    sim_time: int  # ms since Unix epoch, UTC
    type: str
    node: str | None
    payload: dict
    provenance: dict | None = None
    engine_version: str = ENGINE_VERSION
    id: str = ""

    def record(self, with_id: bool = True) -> dict:
        rec = {
            "parent": self.parent,
            "seq": self.seq,
            "sim_time": iso(self.sim_time),
            "type": self.type,
            "node": self.node,
            "engine_version": self.engine_version,
            "payload": self.payload,
            "provenance": self.provenance,
        }
        if with_id:
            rec["id"] = self.id
        return rec

    def compute_id(self) -> str:
        return hash_obj(self.record(with_id=False))

    def seal(self) -> "Event":
        self.id = self.compute_id()
        return self

    @classmethod
    def from_record(cls, rec: dict) -> "Event":
        return cls(
            parent=rec["parent"], seq=rec["seq"], sim_time=parse_iso(rec["sim_time"]),
            type=rec["type"], node=rec["node"], payload=rec["payload"],
            provenance=rec.get("provenance"), engine_version=rec["engine_version"], id=rec["id"],
        )


class ChainError(Exception):
    pass


def verify_chain(events: list[Event]) -> None:
    """Raise ChainError on any broken id, parent link, or seq."""
    prev = None
    for i, ev in enumerate(events):
        if ev.type not in EVENT_TYPES:
            raise ChainError(f"seq {ev.seq}: unknown event type {ev.type!r}")
        if ev.compute_id() != ev.id:
            raise ChainError(f"seq {ev.seq}: id mismatch (content altered)")
        expected_parent = prev.id if prev else None
        if ev.parent != expected_parent:
            raise ChainError(f"seq {ev.seq}: parent {ev.parent} != {expected_parent}")
        if ev.seq != i:
            raise ChainError(f"seq {ev.seq}: expected seq {i}")
        if prev and ev.sim_time < prev.sim_time:
            raise ChainError(f"seq {ev.seq}: sim_time runs backwards")
        prev = ev
