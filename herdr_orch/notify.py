"""Push alerts for a Topic.

A Topic's notify_command gets "<title>\\n<body>\\n" on stdin. Without one, its
`notify` mode decides: `stop-hook` replays the user's own Claude Stop hooks
(whatever they wired, ntfy or Slack, with no credential copied into the Topic),
`herdr` shows herdr's own notification, `none` sends nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping

from herdr_orch.topics import Topic

Notify = Callable[[str, str], None]


def notifier(command: list[str]) -> Notify:
    def send(title: str, body: str) -> None:
        if not command:
            return
        try:
            subprocess.run(command, input=f"{title}\n{body}\n", text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"notify_command failed: {e}", file=sys.stderr)

    return send


def stop_hooks(env: Mapping[str, str]) -> list[str]:
    """The Stop hook commands in the Claude settings of the shell's account."""
    config = env.get("CLAUDE_CONFIG_DIR") or str(Path(env.get("HOME", str(Path.home()))) / ".claude")
    try:
        settings = json.loads((Path(config) / "settings.json").read_text())
    except (OSError, ValueError):
        return []
    return [h["command"] for group in settings.get("hooks", {}).get("Stop", [])
            for h in group.get("hooks", []) if h.get("type") == "command" and h.get("command")]


def _via_stop_hooks(topic: str, env: Mapping[str, str]) -> Notify:
    def send(title: str, body: str) -> None:
        payload = json.dumps({"hook_event_name": "Stop", "cwd": f"/herdr-orchestrator/{topic}",
                              "last_assistant_message": f"{title}: {body}", "stop_hook_active": False})
        # Not as any pane: a hook that skips Managed agents must not skip this alert.
        child_env = {k: v for k, v in {**os.environ, **env}.items() if k != "HERDR_PANE_ID"}
        for command in stop_hooks(env):
            try:
                subprocess.run(["bash", "-c", command], input=payload, text=True, env=child_env,
                               timeout=30, check=False, stdout=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"Stop hook failed: {e}", file=sys.stderr)

    return send


def _via_herdr(env: Mapping[str, str]) -> Notify:
    def send(title: str, body: str) -> None:
        herdr = env.get("HERDR_BIN_PATH") or "herdr"
        try:
            subprocess.run([herdr, "notification", "show", title, "--body", body], timeout=30, check=False,
                           capture_output=True)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"herdr notification failed: {e}", file=sys.stderr)

    return send


def notifier_for(topic: Topic, env: Mapping[str, str] = os.environ) -> Notify:
    if topic.notify_command:
        return notifier(topic.notify_command)
    if topic.notify == "stop-hook":
        return _via_stop_hooks(topic.name, env)
    if topic.notify == "herdr":
        return _via_herdr(env)
    return notifier([])
