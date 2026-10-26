"""The core checks the runner makes beside a Topic's Watches, as pure functions
of what herdr reports and the time. Every string they return becomes an Event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from herdr_orch.state import Event

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


@dataclass(frozen=True)
class Obligation:
    """A Managed agent that finished or stopped and has not been followed up."""
    agent: str
    reason: str  # "done", "gone" or "stale (idle)"
    opened_at: float


def _ending(event: Event) -> tuple[str, str] | None:
    """(agent, reason) when an Event says a Managed agent ended without a word to the user."""
    for suffix in (" done", " gone"):
        if event.source == "agents" and event.text.endswith(suffix):
            return event.text[: -len(suffix)], suffix.strip()
    if event.source == "stale" and " stale (idle)" in event.text:
        return event.text.split(" stale (idle)")[0], "stale (idle)"
    return None


def open_obligations(
    events: list[Event],
    inbox_items: list[dict[str, Any]],
    statuses: dict[str, str],
    managed: set[str],
) -> list[Obligation]:
    """Every done, gone or stale Managed agent still owed a follow-up.

    An obligation opens at the agent's latest done or gone Event (else its first idle-stale
    one: a repeated stale alert never reopens it) and closes when an Inbox item for the agent,
    in any state, was written at or after that. It also ends when the agent works again or
    is no longer Managed. Writing a chat message closes nothing."""
    anchors: dict[str, Obligation] = {}
    for event in events:
        ending = _ending(event)
        if ending is None:
            continue
        agent, reason = ending
        if reason != "gone" and agent not in managed:
            continue  # a gone agent is deregistered by the runner, so its Registry entry proves nothing
        held = anchors.get(agent)
        if reason == "stale (idle)" and held is not None:
            continue  # the first stale (or a done, gone) stands
        anchors[agent] = Obligation(agent, reason, event.at)
    out = []
    for o in anchors.values():
        if o.reason == "gone" and o.agent in statuses:
            continue  # the name is back: seated again
        if statuses.get(o.agent) == "working" and o.reason != "gone":
            continue
        if any(i.get("agent") == o.agent and float(i["created_at"]) >= o.opened_at for i in inbox_items):
            continue
        out.append(o)
    return sorted(out, key=lambda o: o.opened_at)
