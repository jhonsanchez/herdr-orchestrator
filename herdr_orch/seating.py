"""Seating agents and starting a Topic's Orchestrator, through the herdr adapter."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from herdr_orch.github import GitHub
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


def fetch_branch(repo_root: str | None, branch: str) -> bool:
    """Whether origin has the branch; fetching it makes origin/<branch> available to base a worktree on."""
    if not repo_root:
        return False
    try:
        out = subprocess.run(["git", "-C", repo_root, "fetch", "-q", "origin", branch],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


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
    existing: bool = False,
    now: Callable[[], float] = time.time,
) -> Seated:
    """Seat an agent on a branch. `existing` seats it on a branch already pushed to origin,
    so a PR left without an agent gets an owner again."""
    home = home or str(Path.home())
    ws = next((w for w in herdr.workspaces() if w.workspace_id == workspace_id), None)
    if ws is None:
        raise ValueError(f"no herdr workspace {workspace_id}")
    repo = Path(ws.repo_root or ws.label).name
    path = f"{home}/.herdr/worktrees/{repo}/{branch}"
    if existing:
        if not fetch_branch(ws.repo_root, branch):
            raise ValueError(f"origin has no branch {branch} to seat on: push it, or seat a new branch")
        base: str | None = f"origin/{branch}"
    else:
        base = topic.base or default_base(ws.repo_root)  # a Topic's base overrides each repo's own default
    pane = herdr.worktree_create(workspace_id, branch, label or branch, path, base)
    for key, value in topic.env.items():
        herdr.pane_run(pane, f"export {key}={shlex.quote(expand_home(value, home))}")
    name = agent_name(branch)
    started = herdr.agent_start(name, pane, ["--model", topic.model] if topic.model else [])
    state.take(pane, name)
    worktree_ws = next((a.workspace_id for a in herdr.agents() if a.pane_id == pane), None)
    state.track_branch(branch, name, ws.repo_root, worktree_ws, now())
    if not _ready(herdr, name, pane, started, say):
        raise TimeoutError(f"{name} never got past its startup dialog; brief it by hand")
    resume = (f" {branch} already has work pushed, and may have an open PR: start from the PR for the branch (gh api repos/{{owner}}/{{repo}}/pulls), "
              "answer any review feedback and fix any failing check on the same branch.") if existing else ""
    herdr.prompt(
        name,
        f"You are seated on {branch} for topic {topic.name}. Read your brief at {topic.brief_path} "
        f"and follow it, then start the work. When your work is validated, run: herdr-orch validated {topic.name} "
        f"--branch {branch}.{resume}",
    )
    return Seated(name, pane)


def spawn_runner(cli: str, topic: Topic, state: TopicState) -> None:
    """Run the Watch runner as a background process of its own, detached from this command.
    herdr has no supervised background job for plugins, so the runner stops itself once its
    Orchestrator is gone (see Runner.done) and on SIGTERM. Its output goes to runner.log."""
    state.dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HERDR_ORCH_CONFIG": str(topic.config_root), "HERDR_ORCH_STATE": str(state.root)}
    with state.runner_log.open("ab") as log:
        subprocess.Popen(
            [cli, "watch", topic.name], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )


def start(
    topic: Topic,
    state: TopicState,
    herdr: Herdr,
    cli: str,
    say: Callable[[str], None] = _stderr,
    spawn: Callable[[str, Topic, TopicState], None] = spawn_runner,
) -> bool:
    """Seat the Orchestrator in its own workspace with the Attention pane beside it, and the
    Watch runner running in the background. False when its startup dialog was never answered,
    so it was not briefed."""
    name = topic.orchestrator
    cmd = _orchestrator_cmd(cli, topic, state)
    seated = next((a for a in herdr.agents() if a.name == name), None)
    if seated is not None:
        if state.briefed.exists():
            return True
        # A start that died after seating the Orchestrator left it unbriefed: finish the job.
        spawn(cli, topic, state)  # a no-op while a runner is alive
        return _brief(topic, state, herdr, cmd, seated.pane_id, True, say)
    if not topic.has_yours_alone():
        raise ValueError(f"{topic.playbook_path} needs a '## Yours alone' section before its Orchestrator starts")
    if any(w.label == name for w in herdr.workspaces()):
        raise RuntimeError(f"workspace {name} is open without its Orchestrator: close it, then start again")
    state.ensure()
    state.clear_briefed()
    workspace_id, root_pane = herdr.workspace_create(str(topic.dir), name)
    model = topic.orchestrator_model or topic.model
    started = herdr.agent_start(name, root_pane, ["--model", model] if model else [])
    # The runner comes second: before the Orchestrator exists, it would report the Topic Unattended.
    spawn(cli, topic, state)
    herdr.attention_open(workspace_id, root_pane)
    return _brief(topic, state, herdr, cmd, root_pane, started, say)


def _orchestrator_cmd(cli: str, topic: Topic, state: TopicState) -> str:
    """The Orchestrator runs in an ordinary shell: hand it the roots this process resolved."""
    roots = f"HERDR_ORCH_CONFIG={shlex.quote(str(topic.config_root))} HERDR_ORCH_STATE={shlex.quote(str(state.root))}"
    return f"{roots} {shlex.quote(cli)}"


def _brief(topic: Topic, state: TopicState, herdr: Herdr, cmd: str, pane: str, started: bool,
           say: Callable[[str], None]) -> bool:
    """Send the Orchestrator its playbook prompt once it is ready, and mark it briefed."""
    name = topic.orchestrator
    if not _ready(herdr, name, pane, started, say):
        return False
    herdr.prompt(
        name,
        f"You are the Orchestrator for topic {topic.name}. Read the playbook at {topic.playbook_path} "
        "and follow it; its Yours alone section is never yours to decide. New events arrive as prompts "
        "from herdr-orchestrator. When a PR event says its checks are green, merge it with gh pr merge if the "
        "playbook lets you merge (then release its branch); if the playbook keeps merging Yours alone, put it in "
        "the Inbox with a recommended answer. When checks fail, prompt the PR's agent with the failure. "
        f"Seat agents with: {cmd} seat {topic.name} --workspace <id> --branch <branch>. "
        f"After handling events, run: {cmd} heartbeat {topic.name}",
    )
    state.mark_briefed()
    return True


def release(state: TopicState, herdr: Herdr, github: GitHub, branch: str) -> None:
    """Clean up a branch: workspace, worktree (which ends its agent), Registry entry and tracking,
    once its PR is merged or closed. An open PR keeps them: its agent, or its next one, still
    needs the checkout."""
    record = state.branch_record(branch)
    if record is None:
        raise ValueError(f"{branch} is not tracked in Topic {state.topic}")
    if record.get("repo_root"):
        pr = github.pr_for_branch(str(record["repo_root"]), branch)  # RuntimeError if GitHub cannot say: keep it
        if pr is not None and pr.state == "open":
            raise RuntimeError(f"PR #{pr.number} for {branch} is still open: close or merge it first")
    if record.get("workspace_id"):
        herdr.worktree_remove(str(record["workspace_id"]))
    for pane, agent in state.managed().items():
        if agent == record.get("agent"):
            state.give_back(pane)
    state.untrack(branch)


def validated(state: TopicState, branch: str, now: float) -> None:
    """The agent's signal that its work is validated: the Orchestrator hears of it as an Event
    and releases the branch once its PR is merged or closed."""
    if not state.mark_validated(branch, now):
        raise ValueError(f"{branch} is not tracked in Topic {state.topic}")
    state.append_event("validated", f"{branch} is validated: release it once its PR is merged or closed", now)
