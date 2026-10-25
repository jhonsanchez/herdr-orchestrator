"""The state layout: where a Topic's Inbox, Registry, Events and Heartbeat live.

Root: HERDR_ORCH_STATE, else herdr's HERDR_PLUGIN_STATE_DIR, else the default
~/.local/state/herdr/plugins/jhonsanchez.orchestrator. Each Topic owns
topics/<topic>/{inbox.jsonl,registry/<pane-id>,events.log,heartbeat}.
Readers treat a missing file as empty.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, NamedTuple

PLUGIN_ID = "jhonsanchez.orchestrator"


def state_root(env: Mapping[str, str] = os.environ) -> Path:
    if env.get("HERDR_ORCH_STATE"):
        return Path(env["HERDR_ORCH_STATE"])
    if env.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(env["HERDR_PLUGIN_STATE_DIR"])
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local/state/herdr/plugins" / PLUGIN_ID


Kind = Literal["decision", "permission", "question"]


class Managed(NamedTuple):
    topic: str
    agent: str


@dataclass(frozen=True)
class Event:
    at: float
    source: str  # a Watch's name, or the core check that reported it
    text: str


class TopicState:
    def __init__(self, root: Path, topic: str) -> None:
        self.topic = topic
        self.root = root
        self.dir = root / "topics" / topic

    @property
    def inbox(self) -> Path:
        return self.dir / "inbox.jsonl"

    @property
    def registry(self) -> Path:
        return self.dir / "registry"

    @property
    def events(self) -> Path:
        return self.dir / "events.log"

    @property
    def heartbeat(self) -> Path:
        return self.dir / "heartbeat"

    def ensure(self) -> None:
        self.registry.mkdir(parents=True, exist_ok=True)

    # Registry: one file per Managed agent's pane, holding the agent's name.
    def take(self, pane_id: str, agent: str) -> None:
        self.ensure()
        (self.registry / pane_id).write_text(agent + "\n")

    def give_back(self, pane_id: str) -> None:
        (self.registry / pane_id).unlink(missing_ok=True)

    def managed(self) -> dict[str, str]:
        if not self.registry.is_dir():
            return {}
        return {p.name: p.read_text().strip() for p in sorted(self.registry.iterdir()) if p.is_file()}

    # Events: one line per Event, "<epoch>\t<source>\t<text>".
    def append_event(self, source: str, text: str, now: float) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = " ".join(text.split())
        with self.events.open("a") as f:
            f.write(f"{now:.3f}\t{source}\t{line}\n")

    def events_since(self, since: float) -> list[Event]:
        if not self.events.exists():
            return []
        out = []
        for raw in self.events.read_text().splitlines():
            parts = raw.split("\t", 2)
            if len(parts) != 3:
                continue
            at = float(parts[0])
            if at > since:
                out.append(Event(at, parts[1], parts[2]))
        return out

    # Inbox: one JSON object per line, format v1 (docs/inbox-v1.md).
    def escalate(self, agent: str, pane: str, kind: Kind, question: str, options: list[str],
                 now: float, guess: bool = False) -> None:
        item = {
            "v": 1, "id": uuid.uuid4().hex[:12], "topic": self.topic, "agent": agent, "pane": pane,
            "kind": kind, "question": question, "options": options, "guess": guess,
            "status": "open", "answer": None, "created_at": now,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def inbox_lines(self) -> list[dict[str, object]]:
        if not self.inbox.exists():
            return []
        out = []
        for raw in self.inbox.read_text().splitlines():
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue  # a torn line from a concurrent writer
        return out

    def resolve(self, item_id: str, status: str, answer: str | None = None) -> None:
        """Record an item's new state by appending it again with the same id."""
        latest = next((i for i in reversed(self.inbox_lines()) if i.get("id") == item_id), None)
        if latest is None:
            return
        with self.inbox.open("a") as f:
            f.write(json.dumps({**latest, "status": status, "answer": answer}, ensure_ascii=False) + "\n")

    def touch_heartbeat(self, now: float) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text(f"{now:.3f}\n")

    def heartbeat_at(self) -> float | None:
        if not self.heartbeat.exists():
            return None
        return float(self.heartbeat.read_text().strip())


def find_managed(root: Path, pane_id: str) -> Managed | None:
    """The (topic, agent) a pane is registered under, if any Topic manages it."""
    topics = root / "topics"
    if not topics.is_dir():
        return None
    for d in sorted(topics.iterdir()):
        entry = d / "registry" / pane_id
        if entry.is_file():
            return Managed(d.name, entry.read_text().strip())
    return None


def find_registration(root: Path, agent: str) -> tuple[str, str] | None:
    """The (topic, pane id) an agent is registered under, found by its name."""
    topics = root / "topics"
    if not topics.is_dir():
        return None
    for d in sorted(topics.iterdir()):
        for pane, name in TopicState(root, d.name).managed().items():
            if name == agent:
                return d.name, pane
    return None
