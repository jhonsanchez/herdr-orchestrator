"""The core checks the runner makes beside a Topic's Watches, as pure functions
of what herdr reports and the time. Every string they return becomes an Event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from herdr_orch.github import PullRequest
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


def resumed_items(items: Sequence[dict[str, Any]], prev: dict[str, str], now: dict[str, str]) -> list[str]:
    """Ids of open agent-addressed Inbox items whose agent started a turn since the last tick.
    An agent that asked ended its turn; working again means the answer reached it, however it came."""
    started = {n for n, s in now.items() if s == "working" and prev.get(n, "working") != "working"}
    return [str(i["id"]) for i in items if i.get("to", "agent") == "agent" and i.get("agent") in started]


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


RUNNER_STALE_S = 60.0  # a Watch runner that has not ticked this long (4 ticks) is dead or wedged


def runner_down(now: float, runner_at: float | None) -> bool:
    return runner_at is None or now - runner_at >= RUNNER_STALE_S


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
    reason: str  # "done", "gone", "stale (idle)", or what is wrong with an open PR
    opened_at: float
    pr: bool = False  # the PR itself is the problem, not the agent's ending


def _ending(event: Event) -> tuple[str, str] | None:
    """(agent, reason) when an Event says a Managed agent ended without a word to the user."""
    for suffix in (" done", " gone"):
        if event.source == "agents" and event.text.endswith(suffix):
            return event.text[: -len(suffix)], suffix.strip()
    if event.source == "blocked" and " blocked " in event.text:
        return event.text.split(" blocked ")[0], "blocked at a dialog"
    if event.source == "stale" and " stale (idle)" in event.text:
        return event.text.split(" stale (idle)")[0], "stale (idle)"
    return None


def open_obligations(
    events: list[Event],
    inbox_items: list[dict[str, Any]],
    statuses: dict[str, str],
    managed: set[str],
    branches: Sequence[dict[str, Any]] = (),
) -> list[Obligation]:
    """Every done, gone or stale Managed agent still owed a follow-up.

    An obligation opens at the agent's latest done or gone Event (else its first idle-stale
    one: a repeated stale alert never reopens it) and closes when an Inbox item for the agent,
    in any state, was written at or after that. It also ends when the agent works again or
    is no longer Managed. Writing a chat message closes nothing.

    An agent whose branch has a PR is judged by the PR instead (see `pr_obligation`): a merged or closed PR owes nothing,
    the PR is its report, so finishing is no obligation, but an open PR that is orphaned or failing is."""
    with_pr = {str(b["agent"]) for b in branches if b.get("pr_state") in ("open", "merged", "closed")}
    anchors: dict[str, Obligation] = {}
    for event in events:
        ending = _ending(event)
        if ending is None or ending[0] in with_pr:
            continue
        agent, reason = ending
        if reason != "gone" and agent not in managed:
            continue  # a gone agent is deregistered by the runner, so its Registry entry proves nothing
        held = anchors.get(agent)
        if reason == "stale (idle)" and held is not None:
            continue  # the first stale (or a done, gone) stands
        anchors[agent] = Obligation(agent, reason, event.at)
    found = list(anchors.values())
    for b in branches:
        o = pr_obligation(b, statuses)
        if o is not None:
            found.append(o)
        if "validated_at" in b and not str(b.get("evidence") or "").strip():
            found.append(Obligation(str(b["agent"]), f"{b['branch']} validated without evidence",
                                    float(b["validated_at"]), pr=True))
    released = {e.text.partition(" ")[0]: e.at for e in events if e.source in ("released", "swept")}
    out = []
    for o in found:
        if released.get(o.agent, float("-inf")) >= o.opened_at:
            continue  # its branch was released after: nothing left to follow up
        if o.reason == "gone" and o.agent in statuses:
            continue  # the name is back: seated again
        if statuses.get(o.agent) == "working" and o.reason != "gone":
            continue
        if any(i.get("agent") == o.agent and float(i["created_at"]) >= o.opened_at for i in inbox_items):
            continue
        out.append(o)
    return sorted(out, key=lambda o: o.opened_at)


def pr_obligation(branch: dict[str, Any], statuses: dict[str, str]) -> Obligation | None:
    """What an open PR needs, if anything: an owner when its agent is gone, a fix when its
    checks fail or when it has none. An agent still working on it is the owner it needs."""
    if branch.get("pr_state") != "open":
        return None
    agent, number = str(branch["agent"]), branch["pr"]
    status = statuses.get(agent)
    if status is None:
        reason = f"PR #{number} is open with no agent on it"
    elif status == "working":
        return None
    elif branch.get("checks") == "failing":
        reason = f"PR #{number} has failing checks"
    elif branch.get("checks") == "none":
        reason = f"PR #{number} has no checks"
    else:
        return None
    return Obligation(agent, reason, float(branch["pr_since"]), pr=True)


def pr_change(branch: dict[str, Any], pr: PullRequest | None) -> str | None:
    """The Event for a branch's PR changing since it was last seen, if it did."""
    if pr is None:
        return None
    label = f"PR #{pr.number} ({branch['branch']})"
    if branch.get("pr") != pr.number:
        return f"{label} {pr.state}, checks {pr.checks}" + _next_step(pr)
    if branch.get("pr_state") != pr.state:
        return f"{label} {pr.state}" + ("; its workspace can be cleaned up" if pr.state != "open" else "")
    if branch.get("checks") != pr.checks:
        return f"{label} checks {pr.checks}" + _next_step(pr)
    return None


def _next_step(pr: PullRequest) -> str:
    """What the Orchestrator owes an open PR whose checks just settled."""
    if pr.state != "open":
        return ""
    if pr.checks == "passing":
        return "; green: merge it if the playbook lets you, else put it in the Inbox"
    if pr.checks == "failing":
        return "; send the failure back to its agent"
    return ""
