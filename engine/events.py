"""Event records: canonical serialization, hashing, hash chain (spec §15)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from . import ENGINE_VERSION

# Implemented event types.
EVENT_TYPES = {
    "genesis", "wake", "orders", "order_accepted", "order_rejected", "fork", "end_state",
    "transmit", "hop", "forward", "drop", "arrival",      # a message's life, one hop per tick (spec §3.1)
    "program_done", "alert",                              # programs finishing, sensor alerts landing
    "orders_invalid", "refusal", "sample_error",          # failed sampling attempts, before `orders` (spec §12)
    "rng_draw", "ledger_block", "vote_step",              # engine RNG, beacon-ordered issuance, vote closes (M3)
    "contract_step", "score",                             # escrow settlement and expiry, objective scores (M4)
}


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj) -> str:
    return sha256(canonical(obj))


@dataclass
class Event:
    parent: str | None
    seq: int
    tick: int
    type: str
    node: str | None
    payload: dict
    engine_version: str = ENGINE_VERSION
    id: str = ""

    def record(self, with_id: bool = True) -> dict:
        rec = {"parent": self.parent, "seq": self.seq, "tick": self.tick, "type": self.type, "node": self.node,
               "engine_version": self.engine_version, "payload": self.payload}
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
        return cls(parent=rec["parent"], seq=rec["seq"], tick=rec["tick"], type=rec["type"], node=rec["node"],
                   payload=rec["payload"], engine_version=rec["engine_version"], id=rec["id"])


class ChainError(Exception):
    pass


def verify_chain(events: list[Event]) -> None:
    """Raise ChainError on any broken id, parent link, seq, or tick order."""
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
        if prev and ev.tick < prev.tick:
            raise ChainError(f"seq {ev.seq}: tick runs backwards")
        prev = ev
