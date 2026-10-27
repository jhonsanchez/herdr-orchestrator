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

    def summary(self) -> str:
        return (f"adopted {len(self.adopted)} branch(es){_list(self.adopted)}, "
                f"dropped {len(self.dropped)} Registry entr{'y' if len(self.dropped) == 1 else 'ies'}{_list(self.dropped)}, "
                f"compacted {self.compacted} Inbox line(s), "
                f"flagged {len(self.switched)} switched branch(es){_list(self.switched)}")


def _list(items: list[str]) -> str:
    return f" ({', '.join(items)})" if items else ""


def reconcile(
    topic: Topic,
    state: TopicState,
    herdr: Herdr,
    now: float,
    branch_of: Callable[[str], str | None] = checked_out_branch,
    home: str | None = None,
) -> Reconciled:
    """Rebuild missing branch records from the Topic's live worktrees, drop Registry entries whose
    agent is gone, and compact the Inbox by id. Safe to repeat: a second pass changes nothing."""
    agents = herdr.agents()
    dropped = []
    for pane, name in state.managed().items():
        if not any(a.name == name and a.pane_id == pane for a in agents):
            state.give_back(pane)
            dropped.append(name)

    managed = state.managed()
    adopted = []
    switched = []
    flagged = set()
    for ws in herdr.workspaces():
        if not (ws.repo_root and ws.checkout_path and ws.checkout_path != ws.repo_root):
            continue  # not a worktree
        if topic_for(ws.repo_root, [topic], home) is None:
            continue
        owner = next((a for a in agents if a.workspace_id == ws.workspace_id and managed.get(a.pane_id) == a.name), None)
        branch = branch_of(ws.checkout_path)
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

    return Reconciled(adopted, dropped, state.compact_inbox(), switched)


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
