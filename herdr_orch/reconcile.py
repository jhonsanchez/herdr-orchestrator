"""Reconcile a Topic's state with herdr and git: the plugin's records drift from what is live
(workspaces seated before branches were tracked, agents that went away, an Inbox that only grows)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

from herdr_orch.herdr import Herdr
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic, topic_for


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

    def summary(self) -> str:
        return (f"adopted {len(self.adopted)} branch(es){_list(self.adopted)}, "
                f"dropped {len(self.dropped)} Registry entr{'y' if len(self.dropped) == 1 else 'ies'}{_list(self.dropped)}, "
                f"compacted {self.compacted} Inbox line(s)")


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
    for ws in herdr.workspaces():
        if not (ws.repo_root and ws.checkout_path and ws.checkout_path != ws.repo_root):
            continue  # not a worktree
        if topic_for(ws.repo_root, [topic], home) is None:
            continue
        owner = next((a for a in agents if a.workspace_id == ws.workspace_id and managed.get(a.pane_id) == a.name), None)
        branch = branch_of(ws.checkout_path)
        if owner is None or branch is None or state.branch_record(branch) is not None:
            continue
        state.track_branch(branch, str(owner.name), ws.repo_root, ws.workspace_id, now)
        adopted.append(branch)

    return Reconciled(adopted, dropped, state.compact_inbox())
