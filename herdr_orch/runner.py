"""The Watch runner for one Topic: one tick turns what changed into Events,
hands new Events to the Orchestrator when it is idle, records a Heartbeat when
the Orchestrator finishes that turn, and notifies the user once when the Topic
becomes Unattended.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Callable, Protocol

from herdr_orch.herdr import Herdr
from herdr_orch.state import Event, TopicState
from herdr_orch.topics import Topic
from herdr_orch.watches_core import StaleTracker, agent_changes, is_unattended

READY = ("idle", "done")
# Spinner and timer lines change every second without any progress.
NOISE = re.compile(r"^\s*$|[✻✶✢✽✳·*] [A-Z][a-z]+…|\([0-9]+m? ?[0-9]*s")


class WatchSource(Protocol):
    def poll(self, now: float) -> None: ...
    def lines(self) -> list[tuple[str, str]]: ...


def orchestrator_name(topic: str) -> str:
    return f"orch-{topic}"


def _screen_hash(text: str) -> str:
    kept = "\n".join(line for line in text.splitlines() if not NOISE.search(line))
    return hashlib.sha1(kept.encode()).hexdigest()


def _batch(topic: str, events: list[Event]) -> str:
    lines = [f"- {time.strftime('%H:%M', time.localtime(e.at))} {e.watch}: {e.text}" for e in events]
    return f"[herdr-orchestrator] New events for topic {topic}:\n" + "\n".join(lines)


class Runner:
    def __init__(
        self,
        topic: Topic,
        state: TopicState,
        herdr: Herdr,
        watches: WatchSource,
        notify: Callable[[str, str], None],
    ) -> None:
        self.topic, self.state, self.herdr, self.watches, self.notify = topic, state, herdr, watches, notify
        self.orchestrator = orchestrator_name(topic.name)
        self.stale = StaleTracker(topic.stale_min, topic.hung_min)
        self._prev: dict[str, str] = {}
        # Never redeliver what an earlier runner already handed over.
        past = state.events_since(float("-inf"))
        self._delivered_upto = past[-1].at if past else float("-inf")
        self._delivering = False
        self._alerted = False

    def tick(self, now: float) -> None:
        agents = {a.name: a for a in self.herdr.agents() if a.name}
        managed = self.state.managed()  # pane -> agent name
        names = set(managed.values())

        statuses = {n: agents[n].status for n in names if n in agents}
        for text in agent_changes(self._prev, statuses, names):
            self.state.append_event("agents", text, now)
        self._prev = statuses

        for pane, name in managed.items():
            if name in agents:
                stale = self.stale.observe(name, agents[name].status, _screen_hash(self.herdr.read_pane(pane, 40)), now)
                if stale:
                    self.state.append_event("stale", stale, now)
        self.stale.forget(names)

        self.watches.poll(now)
        for watch, line in self.watches.lines():
            self.state.append_event(watch, line, now)

        orch = agents.get(self.orchestrator)
        if orch is not None and orch.status in READY:
            if self._delivering:
                # A successful prompt starts a turn; seeing the Orchestrator ready again means it answered.
                self.state.touch_heartbeat(now)
                self._delivering = False
            new = self.state.events_since(self._delivered_upto)
            if new:
                self.herdr.prompt(self.orchestrator, _batch(self.topic.name, new))
                self._delivered_upto = new[-1].at
                self._delivering = True

        heartbeat = self.state.heartbeat_at()
        waiting = self.state.events_since(heartbeat if heartbeat is not None else float("-inf"))
        unattended = is_unattended(
            now, heartbeat, waiting[0].at if waiting else None, orch is not None, self.topic.heartbeat_window_min
        )
        if unattended and not self._alerted:
            reason = "its orchestrator is not running" if orch is None else f"{len(waiting)} events unanswered"
            self.notify(f"Topic {self.topic.name} unattended", reason)
        self._alerted = unattended
