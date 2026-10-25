"""The core checks the runner makes beside a Topic's Watches, as pure functions
of what herdr reports and the time. Every string they return becomes an Event.
"""

from __future__ import annotations

from dataclasses import dataclass

REPORTED = ("blocked", "done")


def agent_changes(prev: dict[str, str], now: dict[str, str], managed: set[str]) -> list[str]:
    """Events for Managed agents that became blocked or done, or disappeared."""
    out = []
    for name in sorted(managed):
        before, after = prev.get(name), now.get(name)
        if before is None:
            continue
        if after is None:
            out.append(f"{name} gone")
        elif after != before and after in REPORTED:
            out.append(f"{name} {after}")
    return out


@dataclass
class _Seen:
    content: str
    since: float
    alerted: float | None = None


class StaleTracker:
    """Reports an agent whose pane hasn't changed: stale_min when idle, hung_min when working."""

    def __init__(self, stale_min: int, hung_min: int, realert_min: int = 60) -> None:
        self.stale_min, self.hung_min, self.realert_min = stale_min, hung_min, realert_min
        self._seen: dict[str, _Seen] = {}

    def observe(self, agent: str, status: str, content: str, now: float) -> str | None:
        seen = self._seen.get(agent)
        if seen is None or seen.content != content:
            self._seen[agent] = _Seen(content, now)
            return None
        age_min = int((now - seen.since) // 60)
        limit = self.hung_min if status == "working" else self.stale_min
        if age_min < limit:
            return None
        if seen.alerted is not None and now - seen.alerted < self.realert_min * 60:
            return None
        seen.alerted = now
        return f"{agent} stale ({status}) {age_min}m"

    def forget(self, keep: set[str]) -> None:
        for name in set(self._seen) - keep:
            del self._seen[name]


def is_unattended(
    now: float,
    heartbeat_at: float | None,
    pending_since: float | None,
    orchestrator_present: bool,
    window_min: int,
) -> bool:
    """A Topic is Unattended when its Orchestrator is gone, or Events have waited a
    whole window with no Heartbeat since they arrived. A quiet Topic is never
    Unattended just because nothing happened."""
    if not orchestrator_present:
        return True
    if pending_since is None:
        return False
    if heartbeat_at is not None and heartbeat_at >= pending_since:
        return False
    return now - pending_since >= window_min * 60
