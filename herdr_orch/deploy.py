"""The deploy check for this plugin's own repo: has a merge reached the installed plugin
checkout, and is the Topic's Watch runner running on it?"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping

from herdr_orch.state import PLUGIN_ID, TopicState


def installed_checkout(plugins_dir: Path) -> Path | None:
    """The newest installation herdr keeps (it leaves old ones behind after an update).
    herdr 0.9.5 lays one out as <plugin>-<hash>/<version>-<n>/checkout; older ones had the checkout directly."""
    bins = [b for kind in ("github-installations", "github")
            for pattern in (f"*{PLUGIN_ID}-*/*/checkout/bin/herdr-orch", f"*{PLUGIN_ID}-*/bin/herdr-orch")
            for b in (plugins_dir / kind).glob(pattern)]
    return max(bins, key=lambda b: b.stat().st_mtime).parent.parent if bins else None


def holds(checkout: Path, sha: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", sha, "HEAD"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _environ(pid: int) -> str:
    """A process's environment as text: /proc on Linux, `ps e` where there is none (macOS)."""
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().replace(b"\0", b"\n").decode(errors="replace")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "eww", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.replace(" ", "\n")


def runner_on(checkout: Path, pid: int) -> bool:
    """Whether a process was started from the checkout: bin/herdr-orch puts it first on PYTHONPATH
    (as a resolved path, so compare the resolved checkout too)."""
    environ = _environ(pid)
    return any(f"PYTHONPATH={c}/" in environ for c in {str(checkout), str(checkout.resolve())})


def is_deployed(sha: str, plugins_dir: Path, state: TopicState) -> bool:
    checkout = installed_checkout(plugins_dir)
    status = state.runner_status()
    return bool(sha and checkout and status and holds(checkout, sha) and runner_on(checkout, status.pid))


def plugins_dir(env: Mapping[str, str], home: str) -> Path:
    return Path(env.get("HERDR_ORCH_PLUGINS") or f"{home}/.config/herdr/plugins")
