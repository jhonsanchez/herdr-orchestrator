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


def parse_pr(raw: str) -> PullRequest | None:
    prs = json.loads(raw) if raw.strip() else []
    if not prs:
        return None
    pick = next((p for p in prs if p["state"] == "OPEN"), max(prs, key=lambda p: p["number"]))
    return PullRequest(pick["number"], pick["state"].lower(), checks_of(pick.get("statusCheckRollup") or []),
                       pick.get("url", ""))


class CliGitHub:
    def pr_for_branch(self, repo_root: str, branch: str) -> PullRequest | None:
        try:
            out = subprocess.run(
                ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "10",
                 "--json", "number,state,url,statusCheckRollup"],
                cwd=repo_root, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"gh could not be run: {e}") from e
        if out.returncode != 0:
            raise RuntimeError(f"gh pr list failed: {out.stderr.strip()}")
        return parse_pr(out.stdout)
