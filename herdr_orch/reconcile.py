"""Reconcile a Topic's state with herdr and git: the plugin's records drift from what is live
(workspaces seated before branches were tracked, agents that went away, an Inbox that only grows)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Callable

from herdr_orch.attention import current_items
from herdr_orch.herdr import Agent, Herdr
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic, topic_for
from herdr_orch.watches_core import _ending, open_obligations


SWITCH_PREFIX = "switch-"


def checked_out_branch(path: str) -> str | None:
    """The branch checked out at a worktree; None when detached or unreadable."""
    try:
        out = subprocess.run(["git", "-C", path, "symbolic-ref", "--short", "-q", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


@dataclass(frozen=True)
class Reconciled:
    adopted: list[str]  # branches that gained a record
    dropped: list[str]  # Registry entries removed (agent names)
    compacted: int  # Inbox lines removed
    switched: list[str] = field(default_factory=list)  # branches an agent moved to inside a tracked workspace
    swept: list[str] = field(default_factory=list)  # agents whose open follow-ups or questions were dropped
    registered: list[str] = field(default_factory=list)  # hand-started agents put in the Registry (#94)
    unregistered: list[str] = field(default_factory=list)  # named agents in a Topic worktree that stay outside it
    refused: str | None = None  # why the Registry was left alone (#108)

    def summary(self) -> str:
        return (f"adopted {len(self.adopted)} branch(es){_list(self.adopted)}, "
                f"dropped {len(self.dropped)} Registry entr{'y' if len(self.dropped) == 1 else 'ies'}{_list(self.dropped)}, "
                f"compacted {self.compacted} Inbox line(s), "
                f"flagged {len(self.switched)} switched branch(es){_list(self.switched)}, "
                f"swept rows of {len(self.swept)} vanished agent(s){_list(self.swept)}, "
                f"registered {len(self.registered)} hand-started agent(s){_list(self.registered)}, "
                f"{len(self.unregistered)} named agent(s) not in the Registry{_list(self.unregistered)}"
                + (f", refused to drop Registry entries: {self.refused}" if self.refused else ""))


def _list(items: list[str]) -> str:
    return f" ({', '.join(items)})" if items else ""


def reconcile(
    topic: Topic,
    state: TopicState,
    herdr: Herdr,
    now: float,
    branch_of: Callable[[str], str | None] = checked_out_branch,
    home: str | None = None,
    orchestrator_up: bool | None = None,
) -> Reconciled:
    """Rebuild missing branch records from the Topic's live worktrees, drop Registry entries whose
    agent is gone, and compact the Inbox by id. Safe to repeat: a second pass changes nothing."""
    agents = herdr.agents()
    dropped = []
    refused = _drop_refusal(topic, state, agents, orchestrator_up)
    for pane, name in ({} if refused else state.managed()).items():
        if not any(a.name == name and a.pane_id == pane for a in agents):
            state.give_back(pane)
            dropped.append(name)

    managed = state.managed()
    adopted = []
    registered = []
    unregistered: list[str] = []
    switched = []
    flagged = set()
    for ws in herdr.workspaces():
        if not (ws.repo_root and ws.checkout_path and ws.checkout_path != ws.repo_root):
            continue  # not a worktree
        if topic_for(ws.repo_root, [topic], home) is None:
            continue
        owner = next((a for a in agents if a.workspace_id == ws.workspace_id and managed.get(a.pane_id) == a.name), None)
        branch = branch_of(ws.checkout_path)
        if owner is None:  # #94: an agent started by hand here was never registered
            loose = [a for a in agents if a.workspace_id == ws.workspace_id and a.name and a.pane_id not in managed]
            has_record = any(r.get("workspace_id") == ws.workspace_id for r in state.tracked_branches())
            if loose and branch is not None and not has_record and state.branch_record(branch) is None \
                    and not any(r.get("agent") == loose[0].name for r in state.tracked_branches()):
                state.take(loose[0].pane_id, str(loose[0].name))  # #59: only into a workspace without a record
                registered.append(str(loose[0].name))
                owner = loose[0]
                managed = state.managed()
            else:
                unregistered.extend(str(a.name) for a in loose)
        if owner is None or branch is None or state.branch_record(branch) is not None:
            continue
        held = next((r for r in state.tracked_branches()
                     if r.get("workspace_id") == ws.workspace_id or r.get("agent") == owner.name), None)
        if held is not None:  # one branch per workspace and agent: a second one is not ours to adopt
            flagged.add(_flag_switch(state, owner, ws.label, ws.workspace_id, branch, str(held["branch"]), now))
            switched.append(branch)
            continue
        state.track_branch(branch, str(owner.name), ws.repo_root, ws.workspace_id, now)
        adopted.append(branch)

    for item in current_items(state.inbox_lines()):  # released workspace or branch back: the flag is moot
        if str(item["id"]).startswith(SWITCH_PREFIX) and item["id"] not in flagged:
            state.resolve(str(item["id"]), "resolved")


    swept = _sweep(state, agents, now)
    return Reconciled(adopted, dropped, state.compact_inbox(), switched, swept, registered, unregistered, refused)


def _drop_refusal(topic: Topic, state: TopicState, agents: list[Agent], orchestrator_up: bool | None = None) -> str | None:
    """Why the Registry must not be pruned now: herdr shows none of the Topic's agents (likely the wrong
    session, #108), or most of a sizeable Registry is missing at once. None when pruning is sane."""
    managed = state.managed()
    if not managed:
        return None
    visible = sum(1 for pane, name in managed.items() if any(a.name == name and a.pane_id == pane for a in agents))
    if orchestrator_up is None:  # an Orchestrator in this session; a detached one is reported by the caller
        orchestrator_up = any(a.name == topic.orchestrator for a in agents)
    if visible == 0 and not orchestrator_up:
        return f"this herdr session shows none of the {len(managed)} Managed agents nor {topic.orchestrator}"
    missing = len(managed) - visible
    if len(managed) >= 3 and missing * 2 > len(managed):
        return f"{missing} of {len(managed)} Managed agents are missing at once"
    return None


def _sweep(state: TopicState, agents: list[Agent], now: float) -> list[str]:
    """Drop the rows of agents that are not live, not Managed and own no tracked branch: nobody is left to
    follow up with or to answer. Their gone Events are closed; their open Inbox items are dismissed."""
    live = {a.name for a in agents if a.name}
    owned = {str(r.get("agent")) for r in state.tracked_branches()} | set(state.managed().values())
    owned.discard("None")  # the pre-#67 flag stringified a missing name; it names no agent that could answer
    panes = {a.pane_id for a in agents}
    items = [i for i in current_items(state.inbox_lines()) if i.get("pane") not in panes]  # a live pane still owns its rows
    endings = (_ending(e) for e in state.events_since(float("-inf")))
    names = {e[0] for e in endings if e and e[1] == "gone"} | {str(i["agent"]) for i in items if i.get("agent")}
    swept = []
    for agent in sorted(names - live - owned):
        pending = [o for o in open_obligations(state.events_since(float("-inf")), [], {}, set()) if o.agent == agent]
        mine = [i for i in items if i.get("agent") == agent]
        if not (pending or mine):
            continue
        state.append_event("swept", f"{agent} swept: its pane is gone and its agent is not live", now)
        for i in mine:
            state.resolve(str(i["id"]), "dismissed", "swept: its pane is gone and its agent is not live")
        swept.append(agent)
    return swept


def _flag_switch(state: TopicState, owner: Agent, label: str, workspace_id: str, branch: str, held: str,
                 now: float) -> str:
    """Raise it once in the Attention pane: the agent works `branch` in the workspace of `held`.
    The id is derived from (workspace, branch), so every pass lands on the same item."""
    item_id = f"{SWITCH_PREFIX}{workspace_id}-{branch}"
    question = f"{owner.name or label} switched to {branch} in the workspace of {held}: seat {branch} separately"
    if not any(i["id"] == item_id for i in current_items(state.inbox_lines())):
        state.escalate(str(owner.name or label), owner.pane_id, "question", question, [], now, to="orchestrator",
                       item_id=item_id)
    return item_id
