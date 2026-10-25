"""The Attention pane's view model: a pure function from Inbox items, herdr's agents
and workspaces, and each Topic's status to grouped rows and Unattended banners.
The curses layer only draws what this returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from herdr_orch.herdr import Agent, Herdr, Workspace
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from herdr_orch.watches_core import is_unattended

OPEN_ONLY = ("blocked", "done")  # herdr states worth a look on an agent no Orchestrator manages
GONE = "(agent gone)"


def current_items(lines: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inbox v1 is append-only: the last line for an id is its state. Only open items remain."""
    latest: dict[str, dict[str, Any]] = {}
    for line in lines:
        latest[str(line["id"])] = line
    return [i for i in latest.values() if i.get("status") == "open"]


@dataclass(frozen=True)
class TopicStatus:
    name: str
    orchestrator_present: bool
    heartbeat_at: float | None
    pending_since: float | None
    window_min: int


@dataclass(frozen=True)
class Row:
    agent: str
    pane: str
    kind: str
    question: str
    options: list[str]
    created_at: float
    item_id: str | None = None
    topic: str | None = None
    guess: bool = False

    @property
    def answerable(self) -> bool:
        """Decisions and questions are answered from the pane; anything else opens the agent's own pane."""
        return self.item_id is not None and self.kind in ("decision", "question")


@dataclass
class Group:
    label: str
    rows: list[Row] = field(default_factory=list)


@dataclass(frozen=True)
class View:
    groups: list[Group]
    banners: list[str]

    def rows(self) -> list[Row]:
        return [r for g in self.groups for r in g.rows]


def _banner(t: TopicStatus, now: float) -> str | None:
    if not is_unattended(now, t.heartbeat_at, t.pending_since, t.orchestrator_present, t.window_min):
        return None
    if not t.orchestrator_present:
        return f"Topic {t.name} is Unattended: its Orchestrator is not running"
    waited = int((now - (t.pending_since or now)) // 60)
    return f"Topic {t.name} is Unattended: Events unanswered for {waited}m"


def build_view(
    items: list[dict[str, Any]],
    agents: list[Agent],
    workspaces: list[Workspace],
    managed: set[str],
    topics: list[TopicStatus],
    now: float,
) -> View:
    workspace_of = {a.pane_id: a.workspace_id for a in agents}
    label_of = {w.workspace_id: w.label or w.workspace_id for w in workspaces}

    def group_label(pane: str) -> str:
        ws = workspace_of.get(pane)
        return label_of.get(ws, ws) if ws else GONE

    rows: list[tuple[str, Row]] = []
    for i in items:
        pane = str(i["pane"])
        rows.append((group_label(pane), Row(
            agent=str(i["agent"]), pane=pane, kind=str(i["kind"]), question=str(i["question"]),
            options=[str(o) for o in i.get("options", [])], created_at=float(i["created_at"]),
            item_id=str(i["id"]), topic=str(i["topic"]), guess=bool(i.get("guess")),
        )))
    for a in agents:
        if a.pane_id not in managed and a.status in OPEN_ONLY:
            rows.append((group_label(a.pane_id), Row(
                agent=a.name or a.pane_id, pane=a.pane_id, kind=a.status,
                question=f"{a.name or a.pane_id} is {a.status}", options=[], created_at=now,
            )))

    groups: dict[str, Group] = {}
    for label, row in sorted(rows, key=lambda lr: lr[1].created_at):
        groups.setdefault(label, Group(label)).rows.append(row)
    banners = [b for b in (_banner(t, now) for t in topics) if b]
    return View(list(groups.values()), banners)


def load(states: Path, topics: list[Topic], herdr: Herdr, now: float) -> View:
    """Everything the pane shows, read from every Topic's state and herdr."""
    agents, workspaces = herdr.agents(), herdr.workspaces()
    present = {a.name for a in agents if a.name}
    items: list[dict[str, Any]] = []
    managed: set[str] = set()
    statuses = []
    for topic in topics:
        state = TopicState(states, topic.name)
        items += current_items(state.inbox_lines())
        managed |= set(state.managed())
        heartbeat = state.heartbeat_at()
        waiting = state.events_since(heartbeat if heartbeat is not None else float("-inf"))
        if waiting or state.managed() or topic.orchestrator in present:
            statuses.append(TopicStatus(topic.name, topic.orchestrator in present, heartbeat,
                                        waiting[0].at if waiting else None, topic.heartbeat_window_min))
    return build_view(items, agents, workspaces, managed, statuses, now)


def answer(states: Path, row: Row, text: str, herdr: Herdr) -> None:
    """Send the user's answer straight to the agent, then resolve the item."""
    assert row.item_id and row.topic
    herdr.prompt(row.agent, f'Answer from the user to your question "{row.question}": {text}')
    TopicState(states, row.topic).resolve(row.item_id, "resolved", text)


def dismiss(states: Path, row: Row) -> None:
    if row.item_id and row.topic:
        TopicState(states, row.topic).resolve(row.item_id, "dismissed")


def open_pane(row: Row, herdr: Herdr) -> None:
    herdr.focus(row.pane)
