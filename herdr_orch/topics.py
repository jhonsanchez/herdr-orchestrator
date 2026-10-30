"""Topics: one folder per Topic in the plugin's config dir.

topics/<name>/topic.toml holds only what differs from the defaults; playbook.md,
brief.md and watches/ sit beside it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from fnmatch import fnmatch
from pathlib import Path
from typing import Mapping

from herdr_orch.herdr import Workspace
from herdr_orch.state import PLUGIN_ID

TEMPLATE = Path(__file__).parent / "templates" / "topic"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
YOURS_ALONE = re.compile(r"^## Yours alone\s*$", re.MULTILINE)


def expand_home(path: str, home: str) -> str:
    return home + path[1:] if path.startswith("~") else path


def config_root(env: Mapping[str, str] = os.environ) -> Path:
    if env.get("HERDR_ORCH_CONFIG"):
        return Path(env["HERDR_ORCH_CONFIG"])
    if env.get("HERDR_PLUGIN_CONFIG_DIR"):
        return Path(env["HERDR_PLUGIN_CONFIG_DIR"])
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".config/herdr/plugins/config" / PLUGIN_ID


MODEL_KINDS = ("claude", "codex")  # agent kinds known to take --model


@dataclass(frozen=True)
class Topic:
    name: str
    dir: Path
    match: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    orchestrator_model: str | None = None  # the Orchestrator's own; defaults to model
    agent_kind: str = "claude"  # the herdr agent kind of seated agents; the Orchestrator is always Claude
    agent_args: list[str] = field(default_factory=list)  # native args for seated agents, passed after `--`
    base: str | None = None
    notify_command: list[str] = field(default_factory=list)
    notify: str = "none"  # stop-hook | herdr | none; a notify_command wins over it
    deploy_check: list[str] = field(default_factory=list)  # exit 0 once a merge is deployed; empty: no gate
    validate: str = ""  # what "validated" means for this Topic; sent to the agent with the deploy prompt
    github: dict[str, str] | None = None  # the [github] table: its presence turns the GitHub Integration on
    heartbeat_window_min: int = 15
    stale_min: int = 40
    hung_min: int = 60
    stuck: dict[str, int] = field(default_factory=dict)  # minutes per waiting_on state, over watchdog.DEFAULT_LIMITS_MIN

    @property
    def config_root(self) -> Path:
        return self.dir.parent.parent

    @property
    def github_on(self) -> bool:
        """The GitHub Integration is on when topic.toml has a [github] table, even an empty one."""
        return self.github is not None

    @property
    def orchestrator(self) -> str:
        """The herdr agent name of this Topic's Orchestrator."""
        return f"orch-{self.name}"

    def has_yours_alone(self) -> bool:
        return self.playbook_path.exists() and bool(YOURS_ALONE.search(self.playbook_path.read_text()))

    @property
    def read_token_cmd(self) -> str:
        """The command whose output is the token for polling and lookups; empty: gh's own login."""
        return (self.github or {}).get("read_token_cmd", "")

    @property
    def write_token_cmd(self) -> str:
        """The command whose output is the token for marking ready, merging, comments and dispatches."""
        return (self.github or {}).get("write_token_cmd", "")

    @property
    def brief_path(self) -> Path:
        return self.dir / "brief.md"

    @property
    def playbook_path(self) -> Path:
        return self.dir / "playbook.md"

    @property
    def watches_dir(self) -> Path:
        return self.dir / "watches"


def load_topic(d: Path) -> Topic:
    toml = d / "topic.toml"
    data = tomllib.loads(toml.read_text()) if toml.exists() else {}
    known = {f.name for f in fields(Topic)} - {"name", "dir", "agent_kind", "agent_args"}
    # Keys left out keep the dataclass defaults, the one place they are written.
    agent = data.get("agent", {})
    unknown = set(agent) - {"kind", "args"}
    if unknown:
        raise ValueError(f"{toml}: unknown [agent] key(s): {', '.join(sorted(unknown))} (known: kind, args)")
    topic = Topic(name=d.name, dir=d, agent_kind=agent.get("kind", "claude"), agent_args=list(agent.get("args", [])),
                  **{k: v for k, v in data.items() if k in known})
    if topic.model and topic.agent_kind not in MODEL_KINDS:
        raise ValueError(f"{toml}: model is set but agent kind {topic.agent_kind!r} takes no --model "
                         f"(only {', '.join(MODEL_KINDS)}): drop model, or pass the kind's own flag in [agent] args")
    return topic


def load_topics(root: Path) -> list[Topic]:
    topics = root / "topics"
    if not topics.is_dir():
        return []
    return [load_topic(d) for d in sorted(topics.iterdir()) if d.is_dir()]


def topic_for(repo_root: str, topics: list[Topic], home: str | None = None) -> Topic | None:
    """The first Topic whose patterns match the repo root; None means not managed."""
    home = home or str(Path.home())
    for t in topics:
        for pattern in t.match:
            if fnmatch(repo_root, expand_home(pattern, home)):
                return t
    return None


def topic_for_workspace(workspaces: list[Workspace], workspace_id: str, topics: list[Topic], home: str | None = None) -> Topic | None:
    """The Topic of a herdr workspace, by its repo root; None when no Topic matches or herdr has no repo for it."""
    ws = next((w for w in workspaces if w.workspace_id == workspace_id), None)
    return topic_for(ws.repo_root, topics, home) if ws and ws.repo_root else None


NOTIFY_MODES = ("stop-hook", "herdr", "none")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL", re.IGNORECASE)


def parse_notify(text: str) -> tuple[str | None, list[str] | None]:
    """A push-alert answer: one of NOTIFY_MODES, or a command to run."""
    text = text.strip()
    if not text:
        return None, None
    return (text, None) if text in NOTIFY_MODES else (None, shlex.split(text))


@dataclass(frozen=True)
class Answers:
    """What the setup wizard settled; None leaves the template's default."""
    match: list[str] | None = None
    model: str | None = None
    env: dict[str, str] | None = None
    notify: str | None = None
    notify_command: list[str] | None = None
    yours_alone: list[str] | None = None


def _topic_toml(a: Answers) -> str:
    lines = ["# Written by herdr-orch init. Only what differs from the defaults; edit freely."]
    for key in ("match", "model", "notify", "notify_command"):
        value = getattr(a, key)
        if value:
            lines.append(f"{key} = {json.dumps(value)}")  # a JSON string or list of strings is valid TOML
    if a.env:
        lines += ["", "[env]"] + [f"{k} = {json.dumps(v)}" for k, v in a.env.items()]
    return "\n".join(lines) + "\n"


def _with_yours_alone(playbook: str, items: list[str]) -> str:
    """The playbook with its Yours alone list replaced; any section after it is kept."""
    head, _, rest = playbook.partition("## Yours alone")
    section, sep, after = rest.partition("\n## ")
    intro = next((line for line in section.splitlines() if line.strip() and not line.startswith("- ")), "")
    out = f"{head}## Yours alone\n\n{intro}\n\n" + "".join(f"- {i}\n" for i in items)
    return out + (f"\n## {after}" if sep else "")


def init_topic(root: Path, name: str, answers: Answers | None = None) -> Path:
    if not NAME.match(name):
        raise ValueError(f"topic name must match {NAME.pattern}: {name!r}")
    for key in (answers.env or {}) if answers else {}:
        if SECRET_NAME.search(key):
            raise ValueError(f"{key} looks like a secret: keep it in your shell or vault, never in a Topic")
    dest = root / "topics" / name
    if dest.exists():
        raise FileExistsError(dest)
    shutil.copytree(TEMPLATE, dest)
    if answers is not None:
        (dest / "topic.toml").write_text(_topic_toml(answers))
        if answers.yours_alone is not None:
            playbook = dest / "playbook.md"
            playbook.write_text(_with_yours_alone(playbook.read_text(), answers.yours_alone))
    return dest
