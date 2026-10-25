"""Seating agents and starting a Topic's Orchestrator, through the herdr adapter."""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from herdr_orch.herdr import Herdr
from herdr_orch.runner import orchestrator_name
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic


def agent_name(branch: str) -> str:
    """herdr agent names: a lowercase letter, then lowercase letters, digits, - or _; 32 at most."""
    name = re.sub(r"[^a-z0-9_-]+", "-", branch.lower()).strip("-")
    if not name or not name[0].isalpha():
        name = "a-" + name
    return name[:32]


def _expand(value: str, home: str) -> str:
    return home + value[1:] if value.startswith("~") else value


DIALOG_TIMEOUT_MS = 15 * 60 * 1000


def _ready(herdr: Herdr, name: str, started: bool, say: Callable[[str], None]) -> bool:
    """A new folder makes Claude ask whether to trust it; wait for a person to answer."""
    if started:
        return True
    say(f"{name} is waiting at a startup dialog (usually folder trust): answer it in its pane.")
    return herdr.wait_ready(name, DIALOG_TIMEOUT_MS)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


@dataclass(frozen=True)
class Seated:
    name: str
    pane_id: str


def seat(
    topic: Topic,
    state: TopicState,
    herdr: Herdr,
    workspace_id: str,
    branch: str,
    label: str | None = None,
    home: str | None = None,
    say: Callable[[str], None] = _stderr,
) -> Seated:
    home = home or str(Path.home())
    ws = next(w for w in herdr.workspaces() if w.workspace_id == workspace_id)
    repo = Path(ws.repo_root or ws.label).name
    path = f"{home}/.herdr/worktrees/{repo}/{branch}"
    pane = herdr.worktree_create(workspace_id, branch, label or branch, path, topic.base)
    for key, value in topic.env.items():
        herdr.pane_run(pane, f"export {key}={shlex.quote(_expand(value, home))}")
    name = agent_name(branch)
    started = herdr.agent_start(name, pane, ["--model", topic.model] if topic.model else [])
    state.take(pane, name)
    if not _ready(herdr, name, started, say):
        raise TimeoutError(f"{name} never got past its startup dialog; brief it by hand")
    herdr.prompt(
        name,
        f"You are seated on {branch} for topic {topic.name}. Read your brief at {topic.brief_path} "
        "and follow it, then start the work.",
    )
    return Seated(name, pane)


def start(topic: Topic, state: TopicState, herdr: Herdr, cli: str, say: Callable[[str], None] = _stderr) -> bool:
    """Seat the Orchestrator in its own workspace, with the Watch runner in a pane beside it.
    False when its startup dialog was never answered, so it was not briefed."""
    name = orchestrator_name(topic.name)
    if any(a.name == name for a in herdr.agents()):
        return True
    state.ensure()
    _, root_pane = herdr.workspace_create(str(topic.dir), name)
    runner_pane = herdr.pane_split(root_pane, str(topic.dir))
    # The runner pane and the Orchestrator run in ordinary shells: hand them the roots this process resolved.
    roots = f"HERDR_ORCH_CONFIG={shlex.quote(str(topic.dir.parent.parent))} HERDR_ORCH_STATE={shlex.quote(str(state.dir.parent.parent))}"
    cmd = f"{roots} {shlex.quote(cli)}"
    herdr.pane_run(runner_pane, f"{cmd} watch {topic.name}")
    if not _ready(herdr, name, herdr.agent_start(name, root_pane, []), say):
        return False
    herdr.prompt(
        name,
        f"You are the Orchestrator for topic {topic.name}. Read the playbook at {topic.playbook_path} "
        "and follow it; its Yours alone section is never yours to decide. New events arrive as prompts "
        f"from herdr-orchestrator. Seat agents with: {cmd} seat {topic.name} --workspace <id> --branch <branch>. "
        f"After handling events, run: {cmd} heartbeat {topic.name}",
    )
    return True
