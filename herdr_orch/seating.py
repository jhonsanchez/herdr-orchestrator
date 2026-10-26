"""Seating agents and starting a Topic's Orchestrator, through the herdr adapter."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from herdr_orch.herdr import Herdr
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic, expand_home


def agent_name(branch: str) -> str:
    """herdr agent names: a lowercase letter, then lowercase letters, digits, - or _; 32 at most."""
    name = re.sub(r"[^a-z0-9_-]+", "-", branch.lower()).strip("-")
    if not name or not name[0].isalpha():
        name = "a-" + name
    return name[:32]


DIALOG_TIMEOUT_SECONDS = 15 * 60
POLL_SECONDS = 2.0
# Screens Claude can open before its prompt works. herdr flags folder trust as blocked,
# but not the MCP one: it reports the agent idle, and a brief sent then is lost (#11).
STARTUP_SCREENS = [
    (re.compile(r"new MCP servers? found in this project"), "enable this project's MCP servers"),
    (re.compile(r"Is this a project you created or one you trust"), "trust this folder"),
]


def startup_screen(screen: str) -> str | None:
    """What the visible screen asks the user to do before Claude's prompt works, if anything."""
    return next((what for pattern, what in STARTUP_SCREENS if pattern.search(screen)), None)


def _ready(herdr: Herdr, name: str, pane: str, started: bool, say: Callable[[str], None]) -> bool:
    """Wait for a person to clear Claude's startup screens; False if they never do."""
    if not started:  # herdr recognised a dialog (folder trust)
        say(f"{name} is waiting at a startup dialog (usually folder trust): answer it in its pane.")
        if not herdr.wait_ready(name, DIALOG_TIMEOUT_SECONDS * 1000):
            return False
    deadline = time.monotonic() + DIALOG_TIMEOUT_SECONDS
    told = False
    while (screen := startup_screen(herdr.read_screen(pane))) is not None:
        if not told:
            say(f"{name} is waiting at a startup screen ({screen}): answer it in its pane.")
            told = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def default_base(repo_root: str | None) -> str | None:
    """The repo's default branch as origin/<name>, from origin/HEAD; None leaves the choice to herdr."""
    if not repo_root:
        return None
    try:
        out = subprocess.run(["git", "-C", repo_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


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
    ws = next((w for w in herdr.workspaces() if w.workspace_id == workspace_id), None)
    if ws is None:
        raise ValueError(f"no herdr workspace {workspace_id}")
    repo = Path(ws.repo_root or ws.label).name
    path = f"{home}/.herdr/worktrees/{repo}/{branch}"
    base = topic.base or default_base(ws.repo_root)  # a Topic's base overrides each repo's own default
    pane = herdr.worktree_create(workspace_id, branch, label or branch, path, base)
    for key, value in topic.env.items():
        herdr.pane_run(pane, f"export {key}={shlex.quote(expand_home(value, home))}")
    name = agent_name(branch)
    started = herdr.agent_start(name, pane, ["--model", topic.model] if topic.model else [])
    state.take(pane, name)
    if not _ready(herdr, name, pane, started, say):
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
    name = topic.orchestrator
    if any(a.name == name for a in herdr.agents()):
        return True
    if not topic.has_yours_alone():
        raise ValueError(f"{topic.playbook_path} needs a '## Yours alone' section before its Orchestrator starts")
    if any(w.label == name for w in herdr.workspaces()):
        raise RuntimeError(f"workspace {name} is open without its Orchestrator: close it, then start again")
    state.ensure()
    _, root_pane = herdr.workspace_create(str(topic.dir), name)
    # The runner and the Orchestrator run in ordinary shells: hand them the roots this process resolved.
    roots = f"HERDR_ORCH_CONFIG={shlex.quote(str(topic.config_root))} HERDR_ORCH_STATE={shlex.quote(str(state.root))}"
    cmd = f"{roots} {shlex.quote(cli)}"
    model = topic.orchestrator_model or topic.model
    started = herdr.agent_start(name, root_pane, ["--model", model] if model else [])
    # The runner comes second: before the Orchestrator exists, it would report the Topic Unattended.
    runner_pane = herdr.pane_split(root_pane, str(topic.dir))
    # `clear` runs first, so the pane shows the runner's output, not the command typed into it.
    herdr.pane_run(runner_pane, f"clear; {cmd} watch {topic.name}")
    if not _ready(herdr, name, root_pane, started, say):
        return False
    herdr.prompt(
        name,
        f"You are the Orchestrator for topic {topic.name}. Read the playbook at {topic.playbook_path} "
        "and follow it; its Yours alone section is never yours to decide. New events arrive as prompts "
        f"from herdr-orchestrator. Seat agents with: {cmd} seat {topic.name} --workspace <id> --branch <branch>. "
        f"After handling events, run: {cmd} heartbeat {topic.name}",
    )
    return True
