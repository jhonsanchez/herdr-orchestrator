"""The Attention pane's view model: a pure function from Inbox items, herdr's agents
and workspaces, and each Topic's status to grouped rows and Unattended banners.
The curses layer only draws what this returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from herdr_orch.herdr import Agent, Herdr, Workspace
from herdr_orch.ledger import ledger_lines
from herdr_orch.state import RunnerStatus, TopicState
from herdr_orch.topics import Topic
from herdr_orch.watches_core import is_unattended, open_obligations, runner_down

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
    runner: RunnerStatus | None = None


@dataclass(frozen=True)
class Followup:
    """A Managed agent that ended with no follow-up: shown until an Inbox item answers for it."""
    topic: str
    agent: str
    pane: str
    reason: str
    opened_at: float
    window_min: int
    pr: bool = False  # an open PR needs an owner or a fix
    text: str = ""  # shown as is, when set (a held prompt)


HELD_ALERT_S = 180.0  # a prompt held this long is raised to the person
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
    to: str = "agent"  # who an answer is for: the asking agent, or the Orchestrator that raised it

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
    status: list[str] = field(default_factory=list)  # liveness lines, shown dim
    scope: str | None = None  # the one Topic whose rows are shown; None: every Topic

    def rows(self) -> list[Row]:
        return [r for g in self.groups for r in g.rows]


def _banner(t: TopicStatus, now: float) -> str | None:
    if not is_unattended(now, t.heartbeat_at, t.pending_since, t.orchestrator_present, t.window_min):
        return None
    if not t.orchestrator_present:
        return f"Topic {t.name} is Unattended: its Orchestrator is not running"
    waited = int((now - (t.pending_since or now)) // 60)
    return f"Topic {t.name} is Unattended: Events unanswered for {waited}m"


def _followup_banner(f: Followup, now: float) -> str | None:
    if f.text or now - f.opened_at < f.window_min * 60:
        return None
    return f"Topic {f.topic}: {f.agent} {f.reason} {int((now - f.opened_at) // 60)}m ago with no follow-up"


def _runner_line(t: TopicStatus, now: float) -> tuple[str | None, str | None]:
    """(banner, liveness line) for a Topic's Watch runner. A Topic whose Orchestrator is gone
    already has its banner; the runner stops by itself then."""
    if t.runner is not None and not runner_down(now, t.runner.at):
        return None, f"Topic {t.name}: watching, {t.runner.ticks} ticks, last {int(now - t.runner.at)}s ago"
    if not t.orchestrator_present:
        return None, None
    return f"Topic {t.name}: its Watch runner is not running, so no Events are collected", None


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
    followups: Sequence[Followup] = (),
    scope: str | None = None,
    ledger: Sequence[str] = (),
) -> View:
    """Rows grouped by workspace, then agent, oldest first. `hidden` holds (pane, status)
    pairs the user skipped; such a row returns once that agent's status changes. With `scope`,
    only that Topic's rows and liveness show; every Topic's banners stay, as an Unattended Topic
    elsewhere still needs you."""
    workspace_of = {a.pane_id: a.workspace_id for a in agents}
    label_of = {w.workspace_id: w.label or w.workspace_id for w in workspaces}

    def group_label(pane: str) -> str:
        ws = workspace_of.get(pane)
        return label_of.get(ws, ws) if ws else GONE

    rows = [Row(
        agent=str(i["agent"]), pane=str(i["pane"]), kind=str(i["kind"]), text=str(i["question"]),
        options=[str(o) for o in i.get("options", [])], created_at=float(i["created_at"]),
        item_id=str(i["id"]), topic=str(i["topic"]), guess=bool(i.get("guess")), to=str(i.get("to", "agent")),
    ) for i in items]
    for f in followups:
        text = f.text or (f"{f.reason}: needs a follow-up (re-seat {f.agent} or tell the user)" if f.pr else
                          f"{f.agent} {f.reason}: needs a follow-up (PR, report or Inbox item)")
        rows.append(Row(agent=f.agent, pane=f.pane, kind="followup", topic=f.topic, created_at=f.opened_at, options=[],
                        text=text))
    for a in agents if scope is None else ():  # an agent no Orchestrator manages belongs to no Topic
        if a.pane_id not in managed and a.status in OPEN_ONLY and (a.pane_id, a.status) not in hidden:
            name = a.name or a.pane_id
            rows.append(Row(agent=name, pane=a.pane_id, kind=a.status, text=f"{name} is {a.status}",
                            options=[], created_at=None))

    if scope is not None:
        rows = [r for r in rows if r.topic == scope]
    oldest: dict[str, float] = {}
    for r in rows:
        oldest[r.pane] = min(oldest.get(r.pane, float("inf")), _when(r))
    groups: dict[str, Group] = {}
    for r in sorted(rows, key=lambda r: (oldest[r.pane], r.pane, _when(r))):
        groups.setdefault(group_label(r.pane), Group(group_label(r.pane))).rows.append(r)
    banners = [b for b in (_banner(t, now) for t in topics) if b]
    banners += [b for b in (_followup_banner(f, now) for f in followups) if b]
    lines = [_runner_line(t, now) for t in topics]
    banners += [b for b, _ in lines if b]
    shown = [s for t, (_, s) in zip(topics, lines) if s and scope in (None, t.name)]
    return View(list(groups.values()), banners, shown + list(ledger), scope)


def load(states: Path, topics: list[Topic], herdr: Herdr, now: float,
         hidden: set[tuple[str, str]] = frozenset(), scope: str | None = None) -> View:  # type: ignore[assignment]
    """Everything the pane shows, read from every Topic's state and herdr."""
    agents, workspaces = herdr.agents(), herdr.workspaces()
    present = {a.name for a in agents if a.name}
    items: list[dict[str, Any]] = []
    managed: set[str] = set()
    followups: list[Followup] = []
    status_of = {a.name: a.status for a in agents if a.name}
    statuses = []
    ledger: list[str] = []
    for topic in topics:
        state = TopicState(states, topic.name)
        items += current_items(state.inbox_lines())
        pane_of = {name: pane for pane, name in state.managed().items()}
        registered = set(state.managed())
        managed |= registered
        for o in open_obligations(state.events_since(float("-inf")), state.inbox_lines(), status_of, set(pane_of),
                                  state.tracked_branches()):
            followups.append(Followup(topic.name, o.agent, pane_of.get(o.agent, ""), o.reason, o.opened_at,
                                      topic.heartbeat_window_min, o.pr))
        for b in state.tracked_branches():
            st, led = b.get("stuck"), b.get("ledger")
            if isinstance(st, dict) and st.get("escalated") and isinstance(led, dict):
                held = int((now - float(led["since"])) // 60)
                followups.append(Followup(topic.name, str(b["agent"]), pane_of.get(str(b["agent"]), ""),
                                          f"{b['branch']} stuck on {led['waiting_on']} for {held}m",
                                          float(st["escalated"]), topic.heartbeat_window_min, True))
            hold = b.get("held")
            if isinstance(hold, dict) and now - float(hold["since"]) >= HELD_ALERT_S:
                followups.append(Followup(topic.name, str(b["agent"]), pane_of.get(str(b["agent"]), ""), "", float(hold["since"]),
                                          topic.heartbeat_window_min, text=(
                    f"{b['agent']}'s prompt is held: {hold['reason']} for {int((now - float(hold['since'])) // 60)}m. "
                    "Clear or submit it; the runner delivers on its next tick")))
        if scope in (None, topic.name):
            ledger += [f"{topic.name}: {line}" for line in ledger_lines(state, now)]
        heartbeat = state.heartbeat_at()
        waiting = state.events_since(heartbeat if heartbeat is not None else float("-inf"))
        pending_since = waiting[0].at if waiting else None
        # A Topic that was never started has nobody to be Unattended by.
        if waiting or registered or topic.orchestrator in present:
            statuses.append(TopicStatus(topic.name, topic.orchestrator in present, heartbeat,
                                        pending_since, topic.heartbeat_window_min, state.runner_status()))
    return build_view(items, agents, workspaces, managed, statuses, now, hidden, followups, scope, ledger)


def answer(states: Path, row: Row, text: str, herdr: Herdr, orchestrator: str | None = None) -> str:
    """Deliver the user's answer to whoever the item is for, then resolve it; returns who got it.
    An agent's own question goes to its pane (refused when another agent now holds it: the
    answer would reach the wrong one). A decision the Orchestrator raised goes to the
    Orchestrator, whether or not the agent it is about is still around. Nothing resolves
    unless delivery worked."""
    if not (row.item_id and row.topic):
        raise ValueError("only Inbox items can be answered")
    if row.to == "orchestrator":
        if not orchestrator:
            raise ValueError("this Topic has no Orchestrator to send the answer to")
        herdr.prompt(orchestrator, (
            f'The user answered Inbox item {row.item_id} ("{row.text}", about {row.agent}): {text}. '
            "Act on it now: do what the answer authorises, or hold if it says to, and report the outcome."))
        recipient = orchestrator
    else:
        holder = next((a.name for a in herdr.agents() if a.pane_id == row.pane), None)
        if holder != row.agent:
            raise ValueError(f"{row.agent} no longer holds pane {row.pane}; skip it or open it instead")
        herdr.prompt(row.pane, f'Answer from the user to your question "{row.text}": {text}')
        recipient = row.agent
    TopicState(states, row.topic).resolve(row.item_id, "resolved", text)
    return recipient


def dismiss(states: Path, row: Row) -> None:
    if row.item_id and row.topic:
        TopicState(states, row.topic).resolve(row.item_id, "dismissed")


def open_pane(row: Row, herdr: Herdr) -> None:
    herdr.focus(row.pane)
