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


OTHER = "Other…"
ANSWERABLE = ("decision", "question")  # Inbox kinds answered from the pane; anything else opens the agent's pane


@dataclass(frozen=True)
class Row:
    agent: str
    pane: str
    kind: str  # an Inbox kind, or the herdr status of an agent no Orchestrator manages
    text: str
    options: list[str]
    created_at: float | None  # None: herdr doesn't say when an agent's state began
    item_id: str | None = None
    topic: str | None = None
    guess: bool = False

    @property
    def answerable(self) -> bool:
        return self.item_id is not None and self.kind in ANSWERABLE

    @property
    def choices(self) -> list[str]:
        return self.options + [OTHER] if self.answerable else []


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


def _when(row: Row) -> float:
    return row.created_at if row.created_at is not None else float("inf")


def build_view(
    items: list[dict[str, Any]],
    agents: list[Agent],
    workspaces: list[Workspace],
    managed: set[str],
    topics: list[TopicStatus],
    now: float,
    hidden: set[tuple[str, str]] = frozenset(),  # type: ignore[assignment]
) -> View:
    """Rows grouped by workspace, then agent, oldest first. `hidden` holds (pane, status)
    pairs the user skipped; such a row returns once that agent's status changes."""
    workspace_of = {a.pane_id: a.workspace_id for a in agents}
    label_of = {w.workspace_id: w.label or w.workspace_id for w in workspaces}

    def group_label(pane: str) -> str:
        ws = workspace_of.get(pane)
        return label_of.get(ws, ws) if ws else GONE

    rows = [Row(
        agent=str(i["agent"]), pane=str(i["pane"]), kind=str(i["kind"]), text=str(i["question"]),
        options=[str(o) for o in i.get("options", [])], created_at=float(i["created_at"]),
        item_id=str(i["id"]), topic=str(i["topic"]), guess=bool(i.get("guess")),
    ) for i in items]
    for a in agents:
        if a.pane_id not in managed and a.status in OPEN_ONLY and (a.pane_id, a.status) not in hidden:
            name = a.name or a.pane_id
            rows.append(Row(agent=name, pane=a.pane_id, kind=a.status, text=f"{name} is {a.status}",
                            options=[], created_at=None))

    oldest: dict[str, float] = {}
    for r in rows:
        oldest[r.pane] = min(oldest.get(r.pane, float("inf")), _when(r))
    groups: dict[str, Group] = {}
    for r in sorted(rows, key=lambda r: (oldest[r.pane], r.pane, _when(r))):
        groups.setdefault(group_label(r.pane), Group(group_label(r.pane))).rows.append(r)
    banners = [b for b in (_banner(t, now) for t in topics) if b]
    return View(list(groups.values()), banners)


def load(states: Path, topics: list[Topic], herdr: Herdr, now: float,
         hidden: set[tuple[str, str]] = frozenset()) -> View:  # type: ignore[assignment]
    """Everything the pane shows, read from every Topic's state and herdr."""
    agents, workspaces = herdr.agents(), herdr.workspaces()
    present = {a.name for a in agents if a.name}
    items: list[dict[str, Any]] = []
    managed: set[str] = set()
    statuses = []
    for topic in topics:
        state = TopicState(states, topic.name)
        items += current_items(state.inbox_lines())
        registered = set(state.managed())
        managed |= registered
        heartbeat = state.heartbeat_at()
        waiting = state.events_since(heartbeat if heartbeat is not None else float("-inf"))
        pending_since = waiting[0].at if waiting else None
        # A Topic that was never started has nobody to be Unattended by.
        if waiting or registered or topic.orchestrator in present:
            statuses.append(TopicStatus(topic.name, topic.orchestrator in present, heartbeat,
                                        pending_since, topic.heartbeat_window_min))
    return build_view(items, agents, workspaces, managed, statuses, now, hidden)


def answer(states: Path, row: Row, text: str, herdr: Herdr) -> None:
    """Send the user's answer straight to the agent's pane, then resolve the item.
    Refused when another agent now holds that pane: the answer would reach the wrong one."""
    if not (row.item_id and row.topic):
        raise ValueError("only Inbox items can be answered")
    holder = next((a.name for a in herdr.agents() if a.pane_id == row.pane), None)
    if holder != row.agent:
        raise ValueError(f"{row.agent} no longer holds pane {row.pane}; open it instead")
    herdr.prompt(row.pane, f'Answer from the user to your question "{row.text}": {text}')
    TopicState(states, row.topic).resolve(row.item_id, "resolved", text)


def dismiss(states: Path, row: Row) -> None:
    if row.item_id and row.topic:
        TopicState(states, row.topic).resolve(row.item_id, "dismissed")


def open_pane(row: Row, herdr: Herdr) -> None:
    herdr.focus(row.pane)
