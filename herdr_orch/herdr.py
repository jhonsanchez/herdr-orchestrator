"""The herdr adapter: every herdr CLI call goes through here, so tests swap in a fake."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

from herdr_orch.state import PLUGIN_ID


@dataclass(frozen=True)
class Agent:
    name: str | None
    status: str
    pane_id: str
    workspace_id: str


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    label: str
    repo_root: str | None


class Herdr(Protocol):
    def agents(self) -> list[Agent]: ...
    def read_pane(self, pane_id: str, lines: int) -> str: ...
    def read_screen(self, pane_id: str) -> str: ...
    def prompt(self, agent: str, text: str) -> None: ...
    def workspaces(self) -> list[Workspace]: ...
    def workspace_create(self, cwd: str, label: str) -> tuple[str, str]: ...
    def worktree_create(self, workspace_id: str, branch: str, label: str, path: str, base: str | None) -> str: ...
    def pane_split(self, pane_id: str, cwd: str) -> str: ...
    def pane_run(self, pane_id: str, command: str) -> None: ...
    def agent_start(self, name: str, pane_id: str, args: list[str]) -> bool: ...
    def plugin_config_dir(self) -> str | None: ...
    def wait_ready(self, name: str, timeout_ms: int) -> bool: ...
    def focus(self, target: str) -> None: ...


def find_key(value: Any, key: str) -> str | None:
    """The first string under `key`, searched depth-first through nested JSON."""
    if isinstance(value, dict):
        if isinstance(value.get(key), str):
            return str(value[key])
        children: list[Any] = list(value.values())
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = find_key(child, key)
        if found:
            return found
    return None


class CliHerdr:
    """herdr through HERDR_BIN_PATH (what herdr injects into plugin commands), else PATH."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("HERDR_BIN_PATH") or "herdr"

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.binary, *args], check=check, capture_output=True, text=True)

    def _json(self, *args: str) -> Any:
        out = self._run(*args, check=True).stdout
        return json.loads(out) if out.strip() else {}

    def agents(self) -> list[Agent]:
        data = self._json("agent", "list")
        return [
            Agent(a.get("name"), a.get("agent_status", "unknown"), a["pane_id"], a["workspace_id"])
            for a in data.get("result", {}).get("agents", [])
        ]

    def read_pane(self, pane_id: str, lines: int) -> str:
        return self._run("pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", str(lines)).stdout

    def read_screen(self, pane_id: str) -> str:
        """What the pane shows now. Unlike read_pane, no scrollback: a dismissed screen is gone."""
        return self._run("pane", "read", pane_id, "--source", "visible", "--lines", "80").stdout

    def prompt(self, agent: str, text: str) -> None:
        """Send, then check the prompt reached the screen: one sent while Claude is still
        settling after a startup dialog can be swallowed. Resent once if it is missing;
        RuntimeError if it never arrives (the runner then retries on its next tick)."""
        snippet = text.splitlines()[0][:30]
        for _ in range(2):
            self._json("agent", "prompt", agent, text)
            for _ in range(8):
                time.sleep(1)
                screen = self._run("agent", "read", agent, "--source", "recent-unwrapped", "--lines", "200").stdout
                if snippet in screen:
                    return
        raise RuntimeError(
            f"the prompt never reached {agent}: it may be waiting at a dialog herdr doesn't recognise "
            "(such as enabling a project's MCP servers); answer it in its pane"
        )

    def workspaces(self) -> list[Workspace]:
        data = self._json("workspace", "list")
        return [
            Workspace(w["workspace_id"], w.get("label", ""), (w.get("worktree") or {}).get("repo_root"))
            for w in data.get("result", {}).get("workspaces", [])
        ]

    def workspace_create(self, cwd: str, label: str) -> tuple[str, str]:
        r = self._json("workspace", "create", "--cwd", cwd, "--label", label, "--no-focus")["result"]
        return r["workspace"]["workspace_id"], r["root_pane"]["pane_id"]

    def worktree_create(self, workspace_id: str, branch: str, label: str, path: str, base: str | None) -> str:
        args = ["worktree", "create", "--workspace", workspace_id, "--branch", branch,
                "--label", label, "--path", path, "--no-focus"]
        if base:
            args += ["--base", base]
        return find_key(self._json(*args), "pane_id") or ""

    def pane_split(self, pane_id: str, cwd: str) -> str:
        r = self._json("pane", "split", "--pane", pane_id, "--direction", "down", "--cwd", cwd, "--no-focus")
        return str(r["result"]["pane"]["pane_id"])

    def pane_run(self, pane_id: str, command: str) -> None:
        self._json("pane", "run", pane_id, command)

    def agent_start(self, name: str, pane_id: str, args: list[str]) -> bool:
        """False when the agent started but waits at a startup dialog (folder trust, login)."""
        cmd = ["agent", "start", name, "--kind", "claude", "--pane", pane_id, "--timeout", "60000"]
        try:
            self._json(*(cmd + (["--", *args] if args else [])))
        except subprocess.CalledProcessError as e:
            if "agent_not_ready" in (e.stderr or ""):
                return False
            raise
        return True

    def focus(self, target: str) -> None:
        self._json("agent", "focus", target)

    def plugin_config_dir(self) -> str | None:
        """Where herdr keeps this plugin's config; None when herdr can't say."""
        try:
            out = self._run("plugin", "config-dir", PLUGIN_ID)
        except OSError:
            return None
        return (out.stdout.strip() or None) if out.returncode == 0 else None

    def wait_ready(self, name: str, timeout_ms: int) -> bool:
        return self._run("agent", "wait", name, "--until", "idle", "--timeout", str(timeout_ms)).returncode == 0
