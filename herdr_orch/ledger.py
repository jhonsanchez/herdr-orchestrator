"""The branch ledger: every tracked branch's state, owner and time-in-state, and every open PR of
the Topic's repos, recomputed each sweep from what GitHub and herdr show now (level-triggered).
The plugin's Events fire on transitions only; a stuck state or a sibling PR broken by a merge
emits none, so the ledger holds the standing facts instead. No timers or nudges live here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from herdr_orch.github import GitHub, OpenPr
from herdr_orch.state import TopicState

DETAIL_TTL_S = 600.0  # a PR's detail is re-read at least this often, even when nothing moved
CONFLICTED = ("dirty", "behind")


@dataclass(frozen=True)
class Entry:
    state: str  # working | waiting_on
    waiting_on: str | None  # ci | review | merge | deploy | validate | conflict | human | agent
    owner: str  # agent | runner | orchestrator | human

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state, "waiting_on": self.waiting_on, "owner": self.owner}

    def __str__(self) -> str:
        return f"waiting on {self.waiting_on} ({self.owner})" if self.state == "waiting_on" else f"working ({self.owner})"


def _wait(what: str, owner: str) -> Entry:
    return Entry("waiting_on", what, owner)


def classify_pr(pr: OpenPr, owner: str = "agent") -> Entry:
    """Where an open PR stands. `owner` is who fixes it: its agent, or the Orchestrator when no agent has it."""
    if pr.draft or pr.mergeable_state == "draft":
        return Entry("working", None, owner)
    if pr.mergeable_state in CONFLICTED:
        return _wait("conflict", owner)
    if pr.checks == "failing":
        return _wait("ci", owner)
    if pr.review == "changes_requested":
        return _wait("review", owner)
    if pr.checks == "pending" or pr.mergeable_state in ("", "unknown"):
        return _wait("ci", "runner")
    if pr.mergeable_state == "blocked" and pr.review != "approved":
        return _wait("review", "human")
    return _wait("merge", "orchestrator")


def classify_branch(branch: Mapping[str, Any], pr: OpenPr | None, agent_status: str | None,
                    deploy_gate: bool) -> Entry:
    """A tracked branch: its open PR when the sweep read one, else what its own record says."""
    state = branch.get("pr_state")
    if state == "closed":
        return _wait("human", "orchestrator")
    if state == "merged":
        if branch.get("validated_at") and str(branch.get("evidence") or "").strip():
            return Entry("working", None, "runner")  # the runner is releasing it
        if deploy_gate and not branch.get("deployed"):
            return _wait("deploy", "runner")
        return _wait("validate", "agent")
    if state == "open":
        if pr is not None:
            return classify_pr(pr, "agent" if agent_status else "orchestrator")
        return _wait("ci", "runner")  # the PR is known, this sweep could not read it
    if agent_status is None:
        return _wait("agent", "orchestrator")  # no PR and nobody on it: re-seat or tell the user
    if agent_status == "blocked":
        return _wait("human", "human")
    if agent_status in ("idle", "done"):
        return _wait("agent", "orchestrator")
    return Entry("working", None, "agent")


class Ledger:
    """One Topic's sweep. Reads one list call per repo; a PR's own reads (mergeable_state, checks,
    reviews) happen only when its head moved, a PR left its base, its state was still settling, or
    DETAIL_TTL_S passed. Keeps what it read between sweeps in memory."""

    def __init__(self, state: TopicState, github: GitHub, deploy_gate: bool) -> None:
        self.state, self.github, self.deploy_gate = state, github, deploy_gate
        self._seen: dict[str, dict[int, tuple[OpenPr, float]]] = {}

    def _read(self, repo: str, now: float) -> tuple[list[OpenPr], list[str]]:
        """The repo's open PRs with detail, and an Event line for each that just went dirty/behind
        because another PR left its base."""
        listed = self.github.open_pulls(repo)
        before = self._seen.get(repo, {})
        gone = [before[n][0] for n in before if n not in {p.number for p in listed}]
        moved_bases = {p.base_ref for p in gone}
        fresh: dict[int, tuple[OpenPr, float]] = {}
        events = []
        for p in listed:
            prev = before.get(p.number)
            reread = (prev is None or prev[0].head_sha != p.head_sha or p.base_ref in moved_bases
                      or prev[0].mergeable_state in ("", "unknown") or prev[0].checks == "pending"
                      or now - prev[1] >= DETAIL_TTL_S)
            if reread:
                p = self.github.pr_detail(repo, p)
                if prev and p.base_ref in moved_bases and p.mergeable_state in CONFLICTED \
                        and prev[0].mergeable_state not in CONFLICTED:
                    left = ", ".join(f"#{g.number}" for g in gone if g.base_ref == p.base_ref)
                    events.append(f"PR #{p.number} ({p.head_ref}) went {p.mergeable_state} after {left} "
                                  f"left {p.base_ref}")
                fresh[p.number] = (p, now)
            else:
                fresh[p.number] = (prev[0], prev[1])  # type: ignore[index]
        self._seen[repo] = fresh
        return [v[0] for v in fresh.values()], events

    def sweep(self, repos: list[str], agents: Mapping[str, str], now: float) -> list[str]:
        """Recompute the ledger. `agents` maps agent name to herdr status. Returns the Events to report.
        A repo GitHub cannot answer for keeps its last stored entries."""
        branches = self.state.tracked_branches()
        events: list[str] = []
        untracked: list[dict[str, object]] = []
        by_repo: dict[str, list[OpenPr]] = {}
        for repo in repos:
            try:
                by_repo[repo], found = self._read(repo, now)
            except RuntimeError:
                continue
            events += found
        owned = set()
        for b in branches:
            repo = str(b.get("repo_root") or "")
            pr = next((p for p in by_repo.get(repo, []) if p.head_ref == b["branch"]), None)
            if pr is not None:
                owned.add((repo, pr.number))
            if repo in by_repo or b.get("pr_state") != "open":
                entry = classify_branch(b, pr, agents.get(str(b.get("agent"))), self.deploy_gate)
                self.state.set_ledger(str(b["branch"]),
                                      {**entry.as_dict(), "mergeable_state": pr.mergeable_state if pr else None}, now)
        for repo, prs in by_repo.items():
            for p in prs:
                if (repo, p.number) in owned:
                    continue
                e = classify_pr(p, "orchestrator")
                untracked.append({"repo": repo, "number": p.number, "title": p.title, "head_ref": p.head_ref,
                                  "base_ref": p.base_ref, "author": p.author, "url": p.url,
                                  "mergeable_state": p.mergeable_state, "checks": p.checks, "review": p.review,
                                  **e.as_dict()})
        kept = [u for u in self.state.untracked_prs() if u["repo"] not in by_repo]  # repos not answered this sweep
        self.state.set_untracked(untracked + kept, now)
        return events


def _age(since: float, now: float) -> str:
    m = int(max(0, now - since) // 60)
    return f"{m}m" if m < 120 else f"{m // 60}h"


def held_text(held: object, now: float) -> str | None:
    """'held: unsent text in the input box, 12m' for a branch whose agent has an undelivered prompt."""
    if not isinstance(held, dict):
        return None
    return f"held: {held['reason']}, {_age(float(held['since']), now)}"


def ledger_lines(state: TopicState, now: float) -> list[str]:
    """The ledger's standing waits as one line each, for the Attention pane."""
    out = []
    for b in state.tracked_branches():
        led = b.get("ledger")
        if isinstance(led, dict) and led.get("state") == "waiting_on":
            held = held_text(b.get("held"), now)
            out.append(f"{b['branch']}: waiting on {led['waiting_on']} ({led['owner']}) {_age(float(led['since']), now)}"
                       + (f" - {held}" if held else ""))
    for u in state.untracked_prs():
        what = f"waiting on {u['waiting_on']}" if u["state"] == "waiting_on" else "working"
        out.append(f"untracked PR #{u['number']} ({u['head_ref']}): {what} ({u['owner']}) "
                   f"{_age(float(u['since']), now)}")  # type: ignore[arg-type]
    return out


def status_rows(state: TopicState, now: float) -> list[dict[str, object]]:
    """Every tracked branch and untracked open PR as the last sweep left it, one dict per row."""
    rows: list[dict[str, object]] = []
    for b in state.tracked_branches():
        led = b.get("ledger")
        if not isinstance(led, dict):
            continue  # not swept yet
        rows.append({"branch": b["branch"], "pr": b.get("pr"), "tracked": True, "state": led["state"],
                     "waiting_on": led["waiting_on"], "owner": led["owner"], "age_s": int(max(0, now - float(led["since"]))),
                     "checks": b.get("checks"), "mergeable_state": led.get("mergeable_state"),
                     **({"held": held_text(b["held"], now)} if b.get("held") else {})})
    for u in state.untracked_prs():
        rows.append({"branch": u["head_ref"], "pr": u["number"], "tracked": False, "state": u["state"],
                     "waiting_on": u["waiting_on"], "owner": u["owner"], "age_s": int(max(0, now - float(u["since"]))),  # type: ignore[arg-type]
                     "checks": u["checks"], "mergeable_state": u["mergeable_state"]})
    return rows


def status_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "no tracked branches or open PRs (the Watch runner's sweep has not recorded any yet)"
    head: tuple[str, ...] = ("BRANCH", "PR", "STATE", "OWNER", "IN STATE", "CHECKS", "MERGEABLE")
    body: list[tuple[str, ...]] = [(f"{r['branch']}{'' if r['tracked'] else ' (untracked PR)'}", f"#{r['pr']}" if r["pr"] else "-",
             f"waiting on {r['waiting_on']}" if r["state"] == "waiting_on" else "working", str(r["owner"]),
             _age(0, float(r["age_s"])), str(r["checks"] or "-"), str(r["mergeable_state"] or "-"))  # type: ignore[arg-type]
            for r in rows]
    widths = [max(len(x[i]) for x in [head, *body]) for i in range(len(head))]
    if any(r.get("held") for r in rows):
        head, body = (*head, "HELD"), [(*b, str(r.get("held") or "")) for b, r in zip(body, rows)]
        widths = [max(len(x[i]) for x in [head, *body]) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip() for line in [head, *body])
