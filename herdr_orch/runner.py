"""The Watch runner for one Topic: one tick turns what changed into Events,
hands new Events to the Orchestrator when it is idle, records a Heartbeat when
the Orchestrator finishes that turn, and notifies the user once when the Topic
becomes Unattended.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from typing import Callable, Protocol

from herdr_orch.github import GitHub
from herdr_orch.herdr import Agent, Herdr
from herdr_orch.seating import release
from herdr_orch.state import Event, TopicState
from herdr_orch.topics import Topic
from herdr_orch.watches_core import StaleTracker, agent_changes, is_unattended, open_obligations, pr_change

GONE_GRACE_S = 60.0  # how long the Orchestrator may be missing before the runner gives up
DEPLOY_POLL_SECONDS = 30.0
DEPLOY_CHECK_TIMEOUT_S = 60
READY = ("idle", "done")
PR_POLL_SECONDS = 120.0  # gh is slower and rate-limited next to herdr: ask about PRs less often than a tick
# Spinner and timer lines change every second without any progress.
NOISE = re.compile(r"^\s*$|[✻✶✢✽✳·*] [A-Z][a-z]+…|\([0-9]+m? ?[0-9]*s")


class WatchSource(Protocol):
    def poll(self, now: float) -> None: ...
    def lines(self) -> list[tuple[str, str]]: ...


def _screen_hash(text: str) -> str:
    kept = "\n".join(line for line in text.splitlines() if not NOISE.search(line))
    return hashlib.sha1(kept.encode()).hexdigest()


def _batch(topic: str, events: list[Event]) -> str:
    lines = [f"- {time.strftime('%H:%M', time.localtime(e.at))} {e.source}: {e.text}" for e in events]
    return f"[herdr-orchestrator] New Events for Topic {topic}:\n" + "\n".join(lines)


class Runner:
    def __init__(
        self,
        topic: Topic,
        state: TopicState,
        herdr: Herdr,
        watches: WatchSource,
        notify: Callable[[str, str], None],
        say: Callable[[str], None] = lambda line: None,
        github: GitHub | None = None,
    ) -> None:
        self.topic, self.state, self.herdr, self.watches, self.notify = topic, state, herdr, watches, notify
        self.say, self.github = say, github
        self._pr_polled_at = float("-inf")
        self._deploy_polled_at = float("-inf")
        self.orchestrator = topic.orchestrator
        self.stale = StaleTracker(topic.stale_min, topic.hung_min)
        self._prev: dict[str, str] = {}
        # A new runner hands over again everything since the last Heartbeat: an Event an
        # earlier runner logged but never delivered must not be lost; a repeat is harmless.
        heartbeat = state.heartbeat_at()
        self._delivered_upto = heartbeat if heartbeat is not None else float("-inf")
        self._delivering = False
        self._alerted = False
        self._chased: set[tuple[str, float]] = set()
        self._failures: dict[str, str] = {}
        self._missing_since: float | None = None
        self.ticks = 0
        self.done = False  # set once the Orchestrator has been gone a whole grace period

    def tick(self, now: float) -> None:
        before = len(self.state.events_since(float("-inf")))
        self._tick(now)
        seen = len(self.state.events_since(float("-inf"))) - before
        if seen > 0:
            self.say(f"{time.strftime('%H:%M:%S', time.localtime(now))} {seen} new event{'s' * (seen != 1)}")

    def _tick(self, now: float) -> None:
        agents = {a.name: a for a in self.herdr.agents() if a.name}
        managed = self.state.managed()  # pane -> agent name
        names = set(managed.values())

        statuses = {n: agents[n].status for n in names if n in agents}
        for text in agent_changes(self._prev, statuses, names):
            self.state.append_event("agents", text, now)
        # A gone agent's Registry entry must not outlive it: herdr may hand its pane id to
        # another session, which the hooks would then treat as Managed.
        for pane, name in managed.items():
            if name in self._prev and not any(a.name == name and a.pane_id == pane for a in agents.values()):
                self.state.give_back(pane)
        self._prev = statuses

        for pane, name in managed.items():
            if name in agents:
                stale = self.stale.observe(name, agents[name].status, _screen_hash(self.herdr.read_pane(pane, 40)), now)
                if stale:
                    self.state.append_event("stale", stale, now)
        self.stale.forget(names)

        self._step("follow PRs", self._follow_prs, now)
        self._step("deploy gate", self._await_deploys, now, agents)
        self._step("release", self._release_validated, now)

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
            reason = "its Orchestrator is not running" if orch is None else f"{len(waiting)} Events unanswered"
            self.notify(f"Topic {self.topic.name} is Unattended", reason)
        self._alerted = unattended

        pane_names = set(managed.values())
        status_of = {n: a.status for n, a in agents.items()}
        for o in open_obligations(self.state.events_since(float("-inf")), self.state.inbox_lines(), status_of, pane_names,
                                  self.state.tracked_branches()):
            key = (o.agent, o.opened_at)
            if now - o.opened_at >= self.topic.heartbeat_window_min * 60 and key not in self._chased:
                self._chased.add(key)
                detail = o.reason if o.pr else f"{o.agent} {o.reason} with no PR, report or Inbox item"
                self.notify(f"Topic {self.topic.name}: {o.agent} needs a follow-up", detail)

        # Closing the Orchestrator's workspace ends the Topic: stop, after the alert above had its chance.
        if orch is None:
            self._missing_since = self._missing_since if self._missing_since is not None else now
            self.done = now - self._missing_since >= GONE_GRACE_S
        else:
            self._missing_since = None
            if not self.herdr.has_attention(orch.workspace_id):
                self.herdr.attention_open(orch.workspace_id, orch.pane_id, self.topic.name)
        self.ticks += 1
        self.state.record_runner(os.getpid(), now, self.ticks)

    def _step(self, what: str, step: Callable[..., None], *args: object) -> None:
        """One failing step must not starve the rest of the tick: log it once and carry on."""
        try:
            step(*args)
        except Exception as e:  # noqa: BLE001 - the tick must outlive any one step
            self._failed(what, what, e)

    def _failed(self, key: str, what: str, error: Exception) -> bool:
        """Log a failure the first time it appears; True when it was new."""
        text = f"{what} failed: {type(error).__name__}: {error}"
        if self._failures.get(key) == text:
            return False
        self._failures[key] = text
        self.say(text)
        return True

    def _follow_prs(self, now: float) -> None:
        """Report each tracked branch's PR changing, until the PR is merged or closed. A merged branch
        recorded without its merge commit (merged before it was kept) is asked again until it has one."""
        if self.github is None or now - self._pr_polled_at < PR_POLL_SECONDS:
            return
        self._pr_polled_at = now
        for branch in self.state.tracked_branches():
            if not branch.get("repo_root"):
                continue
            backfill = branch.get("pr_state") == "merged" and not branch.get("merge_sha")
            ended = branch.get("pr_state") in ("merged", "closed")
            try:
                pr = self.github.pr_for_branch(str(branch["repo_root"]), str(branch["branch"]))
            except RuntimeError:
                continue  # GitHub is unreachable or rate-limited: the PR's state is unknown, not changed
            if backfill and pr is not None and pr.number == branch.get("pr"):
                if pr.state == "merged" and pr.merge_sha:
                    self.state.set_pr(str(branch["branch"]), pr.number, pr.state, pr.checks, now, pr.merge_sha)
                continue
            if ended and (pr is None or pr.number == branch.get("pr")):
                continue  # the same PR, already reported; only a newer PR on the branch starts a new cycle
            text = pr_change(branch, pr)
            if pr is not None and text is not None:
                self.state.append_event("pr", text, now)
                self.state.set_pr(str(branch["branch"]), pr.number, pr.state, pr.checks, now, pr.merge_sha)
            if pr is not None and pr.state == "open" and pr.checks == "passing":
                self._tell(branch, "green", f"Your PR #{pr.number}'s checks are green. The Orchestrator merges it; "
                                            "wait, and answer any review it sends.")
            elif pr is not None and pr.state == "merged":
                self._tell(branch, "merged", f"Your PR #{pr.number} is merged ({(pr.merge_sha or 'unknown')[:8]}). "
                                             "Wait for the deploy prompt before validating.")

    def _tell(self, branch: dict[str, object], key: str, text: str) -> None:
        """Prompt a branch's agent once per transition; the branch record remembers it across restarts."""
        agent = str(branch.get("agent"))
        if agent in {a.name for a in self.herdr.agents()} and self.state.mark_told(str(branch["branch"]), key):
            self.herdr.prompt(agent, f"[herdr-orchestrator] {text}")

    def _release_validated(self, now: float) -> None:
        """Release a validated branch whose PR is merged or closed. It never waits for the deploy
        gate: the agent validates after it, and a branch merged before the gate existed has none."""
        if self.github is None:
            return
        for branch in self.state.tracked_branches():
            if "validated_at" not in branch or branch.get("pr_state") not in ("merged", "closed"):
                continue
            name = str(branch["branch"])
            self._tell(branch, "released", f"{name} is validated and merged: your workspace is being released. "
                                           "Stop; do not run anything more.")
            try:
                release(self.state, self.herdr, self.github, name)
            except (RuntimeError, ValueError):
                continue  # GitHub cannot say, or the PR is open again: keep it, try next tick
            except Exception as e:  # noqa: BLE001 - e.g. herdr failing: keep the record, tell the user once
                if self._failed(f"release {name}", f"release of {name}", e):
                    self.notify(f"Topic {self.topic.name}: {name} needs a follow-up",
                                f"releasing {name} keeps failing: {e}")
                continue
            self._failures.pop(f"release {name}", None)
            self.state.append_event("released", f"{name} released: validated and its PR is {branch['pr_state']}", now)

    def _deployed(self, branch: dict[str, object]) -> bool:
        env = {**os.environ, "HERDR_ORCH_MERGE_SHA": str(branch["merge_sha"]), "HERDR_ORCH_TOPIC": self.topic.name,
               "HERDR_ORCH_REPO_ROOT": str(branch.get("repo_root") or ""), "HERDR_ORCH_BRANCH": str(branch["branch"])}
        try:
            done = subprocess.run([os.path.expanduser(a) for a in self.topic.deploy_check], env=env,
                                  capture_output=True, timeout=DEPLOY_CHECK_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return False  # a check that cannot run holds the gate
        return done.returncode == 0

    def _await_deploys(self, now: float, agents: dict[str, Agent]) -> None:
        """Once a merged branch's deploy check passes, report it and prompt its agent to validate."""
        if not self.topic.deploy_check or now - self._deploy_polled_at < DEPLOY_POLL_SECONDS:
            return
        self._deploy_polled_at = now
        for branch in self.state.tracked_branches():
            if branch.get("pr_state") != "merged" or not branch.get("merge_sha") or branch.get("deployed"):
                continue
            if not self._deployed(branch):
                continue
            name = str(branch["branch"])
            self.state.mark_deployed(name)
            sha = str(branch["merge_sha"])[:8]
            self.state.append_event("deploy", f"{name} deployed at {sha}; its agent is prompted to validate", now)
            if str(branch.get("agent")) in agents:
                text = (f"Your PR #{branch.get('pr')}'s merge ({sha}) is deployed. Validate it now: check that the installed plugin is at "
                        f"{sha}. {self.topic.validate} Run `herdr-orch validated {self.topic.name} --branch {name} "
                        "--evidence '<what you ran and its last output>'` only after you have checked: an empty "
                        "evidence is refused.")
                self.herdr.prompt(str(branch["agent"]), f"[herdr-orchestrator] {' '.join(text.split())}")
