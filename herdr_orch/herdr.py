"""The herdr adapter: every herdr CLI call goes through here, so tests swap in a fake."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from herdr_orch.state import PLUGIN_ID


ATTENTION_LABEL = "Attention"  # the title herdr gives the pane in herdr-plugin.toml


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
    checkout_path: str | None = None


class Herdr(Protocol):
    def agents(self) -> list[Agent]: ...
    def read_pane(self, pane_id: str, lines: int) -> str: ...
    def read_screen(self, pane_id: str) -> str: ...
    def prompt(self, agent: str, text: str) -> None: ...
    def workspaces(self) -> list[Workspace]: ...
    def workspace_create(self, cwd: str, label: str) -> tuple[str, str]: ...
    def worktree_create(self, workspace_id: str, branch: str, label: str, path: str, base: str | None) -> str: ...
    def worktree_remove(self, workspace_id: str) -> None: ...
    def pane_split(self, pane_id: str, cwd: str) -> str: ...
    def pane_run(self, pane_id: str, command: str) -> None: ...
    def agent_start(self, name: str, pane_id: str, args: list[str]) -> bool: ...
    def plugin_config_dir(self) -> str | None: ...
    def wait_ready(self, name: str, timeout_ms: int) -> bool: ...
    def focus(self, target: str) -> None: ...
    def open_setup(self, repo_root: str) -> None: ...
    def notification(self, title: str, body: str) -> None: ...
    def has_attention(self, workspace_id: str) -> bool: ...
    def attention_open(self, workspace_id: str, target_pane: str) -> None: ...


Delivery = Literal["answered", "pasted", "missing"]


def delivery(screen: str, snippet: str) -> Delivery:
    """Where a sent prompt ended up on the agent's visible screen: answered (the agent's
    reply follows it), pasted (left unsent in the input box), or missing."""
    lines = screen.splitlines()
    at = max((i for i, line in enumerate(lines) if snippet in line), default=-1)
    if at < 0:
        return "missing"
    return "answered" if any(line.startswith("●") for line in lines[at + 1:]) else "pasted"


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

    def _picked_up(self, agent: str, seconds: int) -> bool:
        for _ in range(seconds):
            time.sleep(1)
            status = (self._json("agent", "get", agent).get("result") or {}).get("agent", {}).get("agent_status")
            if status in ("working", "blocked"):
                return True
        return False

    def _screen_of(self, agent: str) -> str:
        return self._run("agent", "read", agent, "--source", "visible", "--lines", "80").stdout

    def prompt(self, agent: str, text: str) -> None:
        """Send, then make sure the agent took it. Text on screen isn't enough: a prompt
        can be left pasted in the input box with its Enter lost, or swallowed while
        Claude settles after a startup screen. A pasted one gets Enter pressed once, a
        missing one is resent once; RuntimeError otherwise (the runner then retries
        on its next tick)."""
        snippet = text.splitlines()[0][:30]
        for _ in range(2):
            self._json("agent", "prompt", agent, text)
            if self._picked_up(agent, 8):
                return
            where = delivery(self._screen_of(agent), snippet)
            if where == "answered":  # a quick turn, already over
                return
            if where == "pasted":
                self._json("agent", "send-keys", agent, "enter")
                if self._picked_up(agent, 8) or delivery(self._screen_of(agent), snippet) == "answered":
                    return
                break  # never resend over text still in the box
        raise RuntimeError(
            f"{agent} never took the prompt: it may be waiting at a screen herdr doesn't recognise, "
            "or the text is still in its input box; check its pane"
        )

    def workspaces(self) -> list[Workspace]:
        data = self._json("workspace", "list")
        return [
            Workspace(w["workspace_id"], w.get("label", ""), (w.get("worktree") or {}).get("repo_root"),
                      (w.get("worktree") or {}).get("checkout_path"))
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

    def worktree_remove(self, workspace_id: str) -> None:
        self._json("worktree", "remove", "--workspace", workspace_id)

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

    def open_setup(self, repo_root: str) -> None:
        """The setup wizard in a herdr popup: plugin actions have no terminal to ask in."""
        # No --placement: the CLI flag has no popup value; the manifest's placement = "popup" applies.
        self._json("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "setup",
                   "--env", f"HERDR_ORCH_SETUP_REPO={repo_root}")

    def notification(self, title: str, body: str) -> None:
        self._json("notification", "show", title, "--body", body)

    def has_attention(self, workspace_id: str) -> bool:
        data = self._json("pane", "list", "--workspace", workspace_id)
        return any(p.get("label") == ATTENTION_LABEL for p in data.get("result", {}).get("panes", []))

    def attention_open(self, workspace_id: str, target_pane: str) -> None:
        """The Attention pane in a right-hand column of the workspace, without taking focus."""
        self._json("plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "attention",
                   "--placement", "split", "--direction", "right", "--workspace", workspace_id,
                   "--target-pane", target_pane, "--no-focus")

    def plugin_config_dir(self) -> str | None:
        """Where herdr keeps this plugin's config; None when herdr can't say."""
        try:
            out = self._run("plugin", "config-dir", PLUGIN_ID)
        except OSError:
            return None
        return (out.stdout.strip() or None) if out.returncode == 0 else None

    def wait_ready(self, name: str, timeout_ms: int) -> bool:
        return self._run("agent", "wait", name, "--until", "idle", "--timeout", str(timeout_ms)).returncode == 0
