"""Push alerts through the Topic's notify_command: "<title>\\n<body>\\n" on stdin."""

from __future__ import annotations

import subprocess
import sys
from typing import Callable


def notifier(command: list[str]) -> Callable[[str, str], None]:
    def send(title: str, body: str) -> None:
        if not command:
            return
        try:
            subprocess.run(command, input=f"{title}\n{body}\n", text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"notify_command failed: {e}", file=sys.stderr)

    return send
