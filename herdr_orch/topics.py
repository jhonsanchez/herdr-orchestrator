"""Topics: one folder per Topic in the plugin's config dir.

topics/<name>/topic.toml holds only what differs from the defaults; playbook.md,
brief.md and watches/ sit beside it.
"""

from __future__ import annotations

import json
import os
import re
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


@dataclass(frozen=True)
class Topic:
    name: str
    dir: Path
    match: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    base: str | None = None
    notify_command: list[str] = field(default_factory=list)
    notify: str = "none"  # stop-hook | herdr | none; a notify_command wins over it
    heartbeat_window_min: int = 15
    stale_min: int = 40
    hung_min: int = 60

    @property
    def config_root(self) -> Path:
        return self.dir.parent.parent

    @property
    def orchestrator(self) -> str:
        """The herdr agent name of this Topic's Orchestrator."""
        return f"orch-{self.name}"

    def has_yours_alone(self) -> bool:
        return self.playbook_path.exists() and bool(YOURS_ALONE.search(self.playbook_path.read_text()))

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
    known = {f.name for f in fields(Topic)} - {"name", "dir"}
    # Keys left out keep the dataclass defaults, the one place they are written.
    return Topic(name=d.name, dir=d, **{k: v for k, v in data.items() if k in known})


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
    head, _, rest = playbook.partition("## Yours alone")
    intro = [line for line in rest.splitlines() if line.strip() and not line.startswith("- ")]
    body = "\n".join(intro[:1])
    return f"{head}## Yours alone\n\n{body}\n\n" + "".join(f"- {i}\n" for i in items)


def init_topic(root: Path, name: str, answers: Answers | None = None) -> Path:
    if not NAME.match(name):
        raise ValueError(f"topic name must match {NAME.pattern}: {name!r}")
    dest = root / "topics" / name
    if dest.exists():
        raise FileExistsError(dest)
    shutil.copytree(TEMPLATE, dest)
    if answers is not None:
        (dest / "topic.toml").write_text(_topic_toml(answers))
        if answers.yours_alone:
            playbook = dest / "playbook.md"
            playbook.write_text(_with_yours_alone(playbook.read_text(), answers.yours_alone))
    return dest
