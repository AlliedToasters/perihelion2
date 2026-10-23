"""Append-only JSONL event log per branch, plus a branch index per run."""
from __future__ import annotations

import json
from pathlib import Path

from .events import Event, verify_chain


class EventLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.events: list[Event] = []
        if self.path.exists():
            with self.path.open() as f:
                self.events = [Event.from_record(json.loads(line)) for line in f if line.strip()]

    @property
    def head(self) -> Event | None:
        return self.events[-1] if self.events else None

    def append(self, type: str, tick: int, node: str | None, payload: dict) -> Event:
        head = self.head
        ev = Event(parent=head.id if head else None, seq=len(self.events), tick=tick, type=type, node=node,
                   payload=payload).seal()
        self._write(ev)
        return ev

    def append_verbatim(self, ev: Event) -> None:
        """Copy an existing sealed event (a fork copying its shared prefix)."""
        head = self.head
        if ev.parent != (head.id if head else None) or ev.seq != len(self.events):
            raise ValueError("verbatim append would break the chain")
        self._write(ev)

    def _write(self, ev: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(ev.record(), sort_keys=True, separators=(",", ":")) + "\n")
        self.events.append(ev)

    def verify(self) -> None:
        verify_chain(self.events)

    def index_of(self, event_id: str) -> int:
        for i, ev in enumerate(self.events):
            if ev.id == event_id or ev.id.startswith(event_id):
                return i
        raise KeyError(event_id)


class RunIndex:
    """runs/<run>/index.json: convenience view. The logs are authoritative; genesis carries ruleset and scenario."""

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "index.json"
        self.data = {"config": {}, "branches": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    def save(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    def branch_path(self, name: str) -> Path:
        return self.run_dir / "branches" / f"{name}.jsonl"

    def register(self, name: str, parent_branch: str | None = None, fork_point: str | None = None,
                 fork_type: str | None = None, draw: int = 0) -> None:
        self.data["branches"][name] = {
            "file": str(self.branch_path(name).relative_to(self.run_dir)),
            "parent_branch": parent_branch, "fork_point": fork_point, "fork_type": fork_type, "draw": draw,
            "head": None, "status": "open",
        }
        self.save()

    def update_head(self, name: str, head: str, status: str) -> None:
        self.data["branches"][name].update(head=head, status=status)
        self.save()

    def open(self, name: str) -> EventLog:
        if name not in self.data["branches"]:
            raise KeyError(f"no branch {name!r} in {self.run_dir}")
        return EventLog(self.branch_path(name))
