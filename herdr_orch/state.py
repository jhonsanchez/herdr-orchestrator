"""The state layout: where a Topic's Inbox, Registry, Events and Heartbeat live.

Root: HERDR_ORCH_STATE, else herdr's HERDR_PLUGIN_STATE_DIR, else the default
~/.local/state/herdr/plugins/jhonsanchez.orchestrator. Each Topic owns
topics/<topic>/{inbox.jsonl,registry/<pane-id>,events.log,heartbeat}.
Readers treat a missing file as empty.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, NamedTuple

PLUGIN_ID = "jhonsanchez.orchestrator"


def state_root(env: Mapping[str, str] = os.environ) -> Path:
    if env.get("HERDR_ORCH_STATE"):
        return Path(env["HERDR_ORCH_STATE"])
    if env.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(env["HERDR_PLUGIN_STATE_DIR"])
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local/state/herdr/plugins" / PLUGIN_ID


Kind = Literal["decision", "permission", "question"]
Status = Literal["open", "resolved", "dismissed"]
Addressee = Literal["agent", "orchestrator"]


class Managed(NamedTuple):
    topic: str
    agent: str


class RunnerStatus(NamedTuple):
    pid: int
    at: float
    ticks: int


@dataclass(frozen=True)
class Event:
    at: float
    source: str  # a Watch's name, or the core check that reported it
    text: str


class TopicState:
    def __init__(self, root: Path, topic: str) -> None:
        self.topic = topic
        self.root = root
        self.dir = root / "topics" / topic

    @property
    def inbox(self) -> Path:
        return self.dir / "inbox.jsonl"

    @property
    def registry(self) -> Path:
        return self.dir / "registry"

    @property
    def events(self) -> Path:
        return self.dir / "events.log"

    @property
    def branches(self) -> Path:
        return self.dir / "branches"

    @property
    def heartbeat(self) -> Path:
        return self.dir / "heartbeat"

    @property
    def briefed(self) -> Path:
        return self.dir / "briefed"

    @property
    def runner(self) -> Path:
        return self.dir / "runner"

    @property
    def socket_file(self) -> Path:
        return self.dir / "socket"

    @property
    def runner_log(self) -> Path:
        return self.dir / "runner.log"

    def ensure(self) -> None:
        self.registry.mkdir(parents=True, exist_ok=True)

    # Briefed marker: present once the Orchestrator has been sent its playbook prompt.
    def mark_briefed(self) -> None:
        self.ensure()
        self.briefed.write_text("")

    def clear_briefed(self) -> None:
        self.briefed.unlink(missing_ok=True)

    # Registry: one file per Managed agent's pane, holding the agent's name.
    def take(self, pane_id: str, agent: str) -> None:
        self.ensure()
        (self.registry / pane_id).write_text(agent + "\n")

    def give_back(self, pane_id: str) -> None:
        (self.registry / pane_id).unlink(missing_ok=True)

    def managed(self) -> dict[str, str]:
        if not self.registry.is_dir():
            return {}
        return {p.name: p.read_text().strip() for p in sorted(self.registry.iterdir()) if p.is_file()}

    # Branches: one JSON file per branch a Managed agent works on, with its PR as last seen.
    # It outlives the agent and its worktree: only `release` removes it.
    def _branch_file(self, branch: str) -> Path:
        return self.branches / (branch.replace("%", "%25").replace("/", "%2F") + ".json")

    def track_branch(self, branch: str, agent: str, repo_root: str | None, workspace_id: str | None,
                     now: float) -> None:
        """Record who works on a branch. A re-seat keeps what is known of the PR."""
        record = self.branch_record(branch) or {"branch": branch, "pr": None, "pr_state": None,
                                                "checks": None, "pr_since": now}
        record.update(agent=agent, repo_root=repo_root, workspace_id=workspace_id)
        self._write_branch(record)

    def set_pr(self, branch: str, pr: int | None, pr_state: str | None, checks: str | None, now: float,
               merge_sha: str | None = None) -> None:
        record = self.branch_record(branch)
        if record is None:
            return
        if record.get("pr") not in (None, pr):  # the branch's newest PR: its cycle starts over
            record = {k: v for k, v in record.items() if k not in ("told", "deployed", "deployed_at", "validated_at", "merge_sha")}
        self._write_branch({**record, "pr": pr, "pr_state": pr_state, "checks": checks, "pr_since": now,
                            "merge_sha": merge_sha or record.get("merge_sha")})

    def set_ledger(self, branch: str, entry: dict[str, object], now: float) -> bool:
        """Store a branch's computed ledger entry; its `since` is kept while the entry is unchanged.
        True when the entry changed (or was new)."""
        record = self.branch_record(branch)
        if record is None:
            return False
        old = record.get("ledger")
        # mergeable_state is shown by `status` but is not a transition: it never restarts `since`
        def core(d: dict[str, object]) -> dict[str, object]:
            return {k: v for k, v in d.items() if k not in ("since", "mergeable_state")}
        if isinstance(old, dict) and core(old) == core(entry):
            if old.get("mergeable_state") != entry.get("mergeable_state"):
                self._write_branch({**record, "ledger": {**old, "mergeable_state": entry.get("mergeable_state")}})
            return False
        self._write_branch({**record, "ledger": {**entry, "since": now}})
        return True

    def set_stuck(self, branch: str, record: dict[str, object]) -> None:
        """What the watchdog did for the branch's current ledger state (see watchdog.py)."""
        current = self.branch_record(branch)
        if current is not None:
            self._write_branch({**current, "stuck": record})

    def set_held(self, branch: str, reason: str | None, now: float) -> None:
        """The branch's agent has a prompt waiting that could not be delivered (reason), since when;
        `since` stays while the reason does. None clears the mark: the prompt was delivered."""
        record = self.branch_record(branch)
        if record is None:
            return
        old = record.get("held")
        if reason is None:
            if old is not None:
                self._write_branch({k: v for k, v in record.items() if k != "held"})
        elif not (isinstance(old, dict) and old.get("reason") == reason):
            self._write_branch({**record, "held": {"reason": reason, "since": now}})

    def mark_deployed(self, branch: str, now: float) -> None:
        record = self.branch_record(branch)
        if record is not None:
            self._write_branch({**record, "deployed": True, "deployed_at": now})

    def mark_told(self, branch: str, key: str) -> bool:
        """Record that the agent was prompted for a transition; False when it already was (or no record)."""
        record = self.branch_record(branch)
        told = list(record.get("told") or []) if record else []  # type: ignore[call-overload]
        if record is None or key in told:
            return False
        self._write_branch({**record, "told": [*told, key]})
        return True

    def mark_validated(self, branch: str, now: float, evidence: str = "") -> bool:
        """The agent says its work is validated, with the evidence of its check. False when the branch is not tracked."""
        record = self.branch_record(branch)
        if record is None:
            return False
        self._write_branch({**record, "validated_at": now, "evidence": evidence})
        return True

    def _write_branch(self, record: dict[str, object]) -> None:
        self.branches.mkdir(parents=True, exist_ok=True)
        path = self._branch_file(str(record["branch"]))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(path)

    def branch_record(self, branch: str) -> dict[str, object] | None:
        path = self._branch_file(branch)
        return json.loads(path.read_text()) if path.is_file() else None

    def tracked_branches(self) -> list[dict[str, object]]:
        if not self.branches.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.branches.glob("*.json"))]

    # Untracked PRs: open PRs of a Topic's repos that no tracked branch owns, from the last sweep.
    @property
    def untracked(self) -> Path:
        return self.dir / "untracked.json"

    def untracked_prs(self) -> list[dict[str, object]]:
        try:
            prs: list[dict[str, object]] = json.loads(self.untracked.read_text())
            return prs
        except (OSError, ValueError):
            return []

    def set_untracked(self, prs: list[dict[str, object]], now: float) -> None:
        """Replace the list; a PR whose state is unchanged keeps its `since`."""
        before = {(p["repo"], p["number"]): p for p in self.untracked_prs()}
        out = []
        for p in prs:
            old = before.get((p["repo"], p["number"]))
            same = old is not None and all(old.get(k) == p[k] for k in ("state", "waiting_on", "owner"))
            out.append({**p, "since": old["since"] if same and old else now})
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.untracked.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(self.untracked)

    def untrack(self, branch: str) -> None:
        self._branch_file(branch).unlink(missing_ok=True)

    # Events: one line per Event, "<epoch>\t<source>\t<text>".
    def append_event(self, source: str, text: str, now: float) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = " ".join(text.split())
        with self.events.open("a") as f:
            f.write(f"{now:.3f}\t{source}\t{line}\n")

    def events_since(self, since: float) -> list[Event]:
        if not self.events.exists():
            return []
        out = []
        for raw in self.events.read_text().splitlines():
            parts = raw.split("\t", 2)
            if len(parts) != 3:
                continue
            at = float(parts[0])
            if at > since:
                out.append(Event(at, parts[1], parts[2]))
        return out

    # Inbox: one JSON object per line, format v2 (docs/inbox-v1.md).
    def escalate(self, agent: str, pane: str, kind: Kind, question: str, options: list[str],
                 now: float, guess: bool = False, to: Addressee = "agent",
                 item_id: str | None = None) -> None:
        item: dict[str, object] = {
            "v": 2, "to": to, "id": item_id or uuid.uuid4().hex[:12], "topic": self.topic, "agent": agent, "pane": pane,
            "kind": kind, "question": question, "options": options, "guess": guess,
            "status": "open", "answer": None, "created_at": now,
        }
        self._append_inbox(item)

    def _append_inbox(self, item: dict[str, object]) -> None:
        """One whole line per write, under a lock: the hooks, the Orchestrator and the
        Attention pane all append to the same file."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.inbox.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def inbox_lines(self) -> list[dict[str, object]]:
        if not self.inbox.exists():
            return []
        out = []
        for raw in self.inbox.read_text().splitlines():
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue  # a torn line from a concurrent writer
        return out

    def compact_inbox(self) -> int:
        """Keep only each id's latest line, in order of first appearance; returns the lines removed.
        Rewritten in place under the append lock, so a concurrent append is never lost."""
        if not self.inbox.exists():
            return 0
        with self.inbox.open("r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            raw = f.read().splitlines()
            latest: dict[str, dict[str, object]] = {}
            for line in raw:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                latest[str(item.get("id"))] = item
            if len(latest) == len(raw):
                return 0
            f.seek(0)
            f.truncate()
            f.write("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in latest.values()))
            return len(raw) - len(latest)

    def resolve(self, item_id: str, status: Status, answer: str | None = None) -> None:
        """Record an item's new state by appending it again with the same id."""
        latest = next((i for i in reversed(self.inbox_lines()) if i.get("id") == item_id), None)
        if latest is None:
            return
        self._append_inbox({**latest, "status": status, "answer": answer})

    def touch_heartbeat(self, now: float) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text(f"{now:.3f}\n")

    def heartbeat_at(self) -> float | None:
        if not self.heartbeat.exists():
            return None
        return float(self.heartbeat.read_text().strip())

    # The herdr socket of the session the Topic's Orchestrator lives in: every later command uses it.
    def record_socket(self, path: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.socket_file.write_text(path + "\n")

    def socket(self) -> str | None:
        try:
            return self.socket_file.read_text().strip() or None
        except OSError:
            return None

    # Watch runner liveness: "<pid> <epoch of its last tick> <ticks>", rewritten every tick.
    def record_runner(self, pid: int, now: float, ticks: int) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.runner.with_suffix(".tmp")
        tmp.write_text(f"{pid} {now:.3f} {ticks}\n")
        tmp.replace(self.runner)

    def runner_status(self) -> RunnerStatus | None:
        try:
            pid, at, ticks = self.runner.read_text().split()
            return RunnerStatus(int(pid), float(at), int(ticks))
        except (OSError, ValueError):
            return None

    def clear_runner(self, pid: int) -> None:
        status = self.runner_status()
        if status is not None and status.pid == pid:
            self.runner.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def find_managed(root: Path, pane_id: str) -> Managed | None:
    """The (topic, agent) a pane is registered under, if any Topic manages it."""
    topics = root / "topics"
    if not topics.is_dir():
        return None
    for d in sorted(topics.iterdir()):
        entry = d / "registry" / pane_id
        if entry.is_file():
            return Managed(d.name, entry.read_text().strip())
    return None


def find_registration(root: Path, agent: str) -> tuple[str, str] | None:
    """The (topic, pane id) an agent is registered under, found by its name."""
    topics = root / "topics"
    if not topics.is_dir():
        return None
    for d in sorted(topics.iterdir()):
        for pane, name in TopicState(root, d.name).managed().items():
            if name == agent:
                return d.name, pane
    return None


def validation_stands(branch: Mapping[str, object], deploy_gate: bool) -> bool:
    """The branch's `validated` signal counts: it came after the merge's deploy (with a deploy gate) or
    after the merge or close (without one). An earlier one was given before there was anything to check."""
    if "validated_at" not in branch:
        return False
    validated_at = float(branch["validated_at"])  # type: ignore[arg-type]
    if deploy_gate and branch.get("pr_state") == "merged":
        return bool(branch.get("deployed")) and validated_at > float(branch.get("deployed_at") or 0)  # type: ignore[arg-type]
    return validated_at > float(branch.get("pr_since") or 0)  # type: ignore[arg-type]
