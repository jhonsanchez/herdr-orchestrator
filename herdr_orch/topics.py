"""Topics: one folder per Topic in the plugin's config dir.

topics/<name>/topic.toml holds only what differs from the defaults; playbook.md,
brief.md and watches/ sit beside it.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Mapping

from herdr_orch.state import PLUGIN_ID

TEMPLATE = Path(__file__).parent / "templates" / "topic"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


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
    heartbeat_window_min: int = 15
    stale_min: int = 40
    hung_min: int = 60

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
    return Topic(
        name=d.name,
        dir=d,
        match=[str(p) for p in data.get("match", [])],
        env={str(k): str(v) for k, v in data.get("env", {}).items()},
        model=data.get("model"),
        base=data.get("base"),
        notify_command=[str(a) for a in data.get("notify_command", [])],
        heartbeat_window_min=int(data.get("heartbeat_window_min", 15)),
        stale_min=int(data.get("stale_min", 40)),
        hung_min=int(data.get("hung_min", 60)),
    )


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
            expanded = home + pattern[1:] if pattern.startswith("~") else pattern
            if fnmatch(repo_root, expanded):
                return t
    return None


def init_topic(root: Path, name: str) -> Path:
    if not NAME.match(name):
        raise ValueError(f"topic name must match {NAME.pattern}: {name!r}")
    dest = root / "topics" / name
    if dest.exists():
        raise FileExistsError(dest)
    shutil.copytree(TEMPLATE, dest)
    return dest
