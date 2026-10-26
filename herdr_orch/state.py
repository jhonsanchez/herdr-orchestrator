"""The state layout: where a Topic's Inbox, Registry, Events and Heartbeat live.

Root: HERDR_ORCH_STATE, else herdr's HERDR_PLUGIN_STATE_DIR, else the default
~/.local/state/herdr/plugins/jhonsanchez.orchestrator. Each Topic owns
topics/<topic>/{inbox.jsonl,registry/<pane-id>,events.log,heartbeat}.
Readers treat a missing file as empty.
"""

from __future__ import annotations

import fcntl
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
Status = Literal["open", "resolved", "dismissed"]
Addressee = Literal["agent", "orchestrator"]


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
    def branches(self) -> Path:
        return self.dir / "branches"

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

    # Branches: one JSON file per branch a Managed agent works on, with its PR as last seen.
    # It outlives the agent and its worktree: only `release` removes it.
    def _branch_file(self, branch: str) -> Path:
        return self.branches / (branch.replace("%", "%25").replace("/", "%2F") + ".json")

    def track_branch(self, branch: str, agent: str, repo_root: str | None, workspace_id: str | None,
                     now: float) -> None:
        """Record who works on a branch. A re-seat keeps what is known of the PR."""
        record = self.branch_record(branch) or {"branch": branch, "pr": None, "pr_state": None,
                                                "checks": None, "pr_since": now}
        record.update(agent=agent, repo_root=repo_root, workspace_id=workspace_id)
        self._write_branch(record)

    def set_pr(self, branch: str, pr: int | None, pr_state: str | None, checks: str | None, now: float) -> None:
        record = self.branch_record(branch)
        if record is not None:
            self._write_branch({**record, "pr": pr, "pr_state": pr_state, "checks": checks, "pr_since": now})

    def _write_branch(self, record: dict[str, object]) -> None:
        self.branches.mkdir(parents=True, exist_ok=True)
        path = self._branch_file(str(record["branch"]))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(path)

    def branch_record(self, branch: str) -> dict[str, object] | None:
        path = self._branch_file(branch)
        return json.loads(path.read_text()) if path.is_file() else None

    def tracked_branches(self) -> list[dict[str, object]]:
        if not self.branches.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.branches.glob("*.json"))]

    def untrack(self, branch: str) -> None:
        self._branch_file(branch).unlink(missing_ok=True)

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

    # Inbox: one JSON object per line, format v2 (docs/inbox-v1.md).
    def escalate(self, agent: str, pane: str, kind: Kind, question: str, options: list[str],
                 now: float, guess: bool = False, to: Addressee = "agent") -> None:
        item: dict[str, object] = {
            "v": 2, "to": to, "id": uuid.uuid4().hex[:12], "topic": self.topic, "agent": agent, "pane": pane,
            "kind": kind, "question": question, "options": options, "guess": guess,
            "status": "open", "answer": None, "created_at": now,
        }
        self._append_inbox(item)

    def _append_inbox(self, item: dict[str, object]) -> None:
        """One whole line per write, under a lock: the hooks, the Orchestrator and the
        Attention pane all append to the same file."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
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

    def resolve(self, item_id: str, status: Status, answer: str | None = None) -> None:
        """Record an item's new state by appending it again with the same id."""
        latest = next((i for i in reversed(self.inbox_lines()) if i.get("id") == item_id), None)
        if latest is None:
            return
        self._append_inbox({**latest, "status": status, "answer": answer})

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
