"""The GitHub adapter: every gh call goes through here, so tests swap in a fake."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Literal, Protocol

PrState = Literal["open", "merged", "closed"]
Checks = Literal["passing", "failing", "pending", "none"]

FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: PrState
    checks: Checks
    url: str = ""
    merge_sha: str = ""  # the merge commit, once merged


class GitHub(Protocol):
    def pr_for_branch(self, repo_root: str, branch: str) -> PullRequest | None:
        """The branch's PR (the open one, else the newest); None when it has none.
        RuntimeError when GitHub cannot be asked, which is not the same as no PR."""
        ...


def checks_of(rollup: list[dict[str, Any]]) -> Checks:
    """One verdict for a PR's statusCheckRollup: any failure fails it, any unfinished check holds it."""
    if not rollup:
        return "none"
    verdicts = [str(c.get("conclusion") or c.get("state") or "") for c in rollup]
    if any(v in FAILED for v in verdicts):
        return "failing"
    return "passing" if all(v in OK for v in verdicts) else "pending"


def parse_pr(raw: str, checks: Checks = "none") -> PullRequest | None:
    """The branch's PR from a REST pulls listing: the open one, else the newest."""
    prs = json.loads(raw) if raw.strip() else []
    if not prs:
        return None
    pick = next((p for p in prs if p["state"] == "open"), max(prs, key=lambda p: p["number"]))
    state: PrState = "merged" if pick["state"] == "closed" and pick.get("merged_at") else pick["state"]
    return PullRequest(pick["number"], state, checks, pick.get("html_url", ""),
                       (pick.get("merge_commit_sha") or "") if state == "merged" else "")


def pulls_args(branch: str) -> list[str]:
    """gh api arguments for the branch's PRs; gh fills {owner}/{repo} from the checkout it runs in."""
    return ["gh", "api", "-X", "GET", "repos/{owner}/{repo}/pulls",
            "-f", f"head={{owner}}:{branch}", "-f", "state=all", "-f", "per_page=10"]


def checks_args(sha: str) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs", "-f", "per_page=100"]


def status_args(sha: str) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/commits/{sha}/status", "-f", "per_page=100"]


def rollup_of(check_runs: dict[str, Any], status: dict[str, Any]) -> list[dict[str, Any]]:
    """REST check runs and commit statuses in the shape checks_of reads."""
    runs = [{"conclusion": str(r.get("conclusion") or "").upper()} for r in check_runs.get("check_runs", [])]
    return runs + [{"state": str(s.get("state", "")).upper()}
                   for s in status.get("statuses", [])]


class CliGitHub:
    def _api(self, args: list[str], repo_root: str) -> str:
        try:
            out = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"gh could not be run: {e}") from e
        if out.returncode != 0:
            raise RuntimeError(f"gh api failed: {out.stderr.strip()}")
        return out.stdout

    def pr_for_branch(self, repo_root: str, branch: str) -> PullRequest | None:
        raw = self._api(pulls_args(branch), repo_root)
        pr = parse_pr(raw)
        if pr is None or pr.state != "open":
            return pr
        sha = next(p for p in json.loads(raw) if p["number"] == pr.number)["head"]["sha"]
        runs = json.loads(self._api(checks_args(sha), repo_root))
        status = json.loads(self._api(status_args(sha), repo_root))
        return PullRequest(pr.number, pr.state, checks_of(rollup_of(runs, status)), pr.url, pr.merge_sha)
