"""The GitHub adapter: every gh call goes through here, so tests swap in a fake."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

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


@dataclass(frozen=True)
class OpenPr:
    """An open PR as the REST sweep sees it. The list call fills the first fields; mergeable_state,
    checks and review need the PR's own reads (pr_detail) and are empty until then."""
    number: int
    title: str
    head_ref: str
    base_ref: str
    head_sha: str
    author: str = ""
    url: str = ""
    draft: bool = False
    mergeable_state: str = ""  # clean | dirty | behind | blocked | unstable | draft | has_hooks | unknown | ""
    checks: Checks = "none"
    review: str = "none"  # none | approved | changes_requested


class GitHub(Protocol):
    def open_pulls(self, repo_root: str) -> list[OpenPr]:
        """Every open PR of the repo, any author: one list call. RuntimeError when GitHub cannot be asked."""
        ...

    def pr_detail(self, repo_root: str, pr: OpenPr) -> OpenPr:
        """The PR with its mergeable_state, checks and review read in."""
        ...

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


def parse_open_pulls(raw: str) -> list[OpenPr]:
    """A REST pulls listing (state=open) as OpenPrs; mergeable_state is not in a listing."""
    return [OpenPr(p["number"], p.get("title") or "", p["head"]["ref"], p["base"]["ref"], p["head"]["sha"],
                   (p.get("user") or {}).get("login", ""), p.get("html_url", ""), bool(p.get("draft")))
            for p in (json.loads(raw) if raw.strip() else [])]


def review_of(reviews: list[dict[str, Any]]) -> str:
    """The standing verdict of a PR's reviews: each reviewer's last APPROVED / CHANGES_REQUESTED /
    DISMISSED counts (a comment changes nothing); any outstanding change request wins."""
    latest: dict[str, str] = {}
    for r in sorted(reviews, key=lambda r: str(r.get("submitted_at") or "")):
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[str((r.get("user") or {}).get("login", ""))] = str(r["state"])
    if "CHANGES_REQUESTED" in latest.values():
        return "changes_requested"
    return "approved" if "APPROVED" in latest.values() else "none"


def open_pulls_args() -> list[str]:
    return ["gh", "api", "-X", "GET", "repos/{owner}/{repo}/pulls", "-f", "state=open", "-f", "per_page=100"]


def pull_args(number: int) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/pulls/{number}"]


def reviews_args(number: int) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/pulls/{number}/reviews", "-f", "per_page=100"]


def pulls_args(branch: str) -> list[str]:
    """gh api arguments for the branch's PRs; gh fills {owner}/{repo} from the checkout it runs in,
    but only in the endpoint (not in -f values), so head rides in its query string."""
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/pulls?head={{owner}}:{branch}",
            "-f", "state=all", "-f", "per_page=10"]


def checks_args(sha: str) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs", "-f", "per_page=100"]


def status_args(sha: str) -> list[str]:
    return ["gh", "api", "-X", "GET", f"repos/{{owner}}/{{repo}}/commits/{sha}/status", "-f", "per_page=100"]


def rollup_of(check_runs: dict[str, Any], status: dict[str, Any]) -> list[dict[str, Any]]:
    """REST check runs and commit statuses in the shape checks_of reads."""
    runs = [{"conclusion": str(r.get("conclusion") or "").upper()} for r in check_runs.get("check_runs", [])]
    return runs + [{"state": str(s.get("state", "")).upper()}
                   for s in status.get("statuses", [])]


TOKEN_TTL_SECONDS = 30 * 60.0  # GitHub App installation tokens last an hour: ask again well before


def token_from(command: str, home: str | None = None) -> str:
    """The token a Topic's token command prints. RuntimeError when it fails or prints nothing;
    the message never carries the token."""
    home = home or str(Path.home())
    argv = [home + a[1:] if a.startswith("~") else a for a in shlex.split(command)]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"token command could not be run: {e}") from e
    token = out.stdout.strip()
    if out.returncode != 0 or not token:
        raise RuntimeError(f"token command failed (exit {out.returncode}): {out.stderr.strip()[:200]}")
    return token


def gh_env(token: str | None) -> dict[str, str] | None:
    """The environment for one gh call: the token as GH_TOKEN, else None (inherit)."""
    return {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token} if token else None


class CliGitHub:
    """gh through the Topic's read token when it has one (its own rate limit, never the user's login)."""

    def __init__(self, read_token_cmd: str = "", home: str | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.read_token_cmd, self.home, self.clock = read_token_cmd, home, clock
        self._token = ""
        self._token_at = 0.0

    def _read_token(self) -> str | None:
        if not self.read_token_cmd:
            return None
        if not self._token or self.clock() - self._token_at >= TOKEN_TTL_SECONDS:
            self._token, self._token_at = token_from(self.read_token_cmd, self.home), self.clock()
        return self._token

    def _api(self, args: list[str], repo_root: str) -> str:
        try:
            out = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, timeout=60,
                                 env=gh_env(self._read_token()))
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

    def open_pulls(self, repo_root: str) -> list[OpenPr]:
        return parse_open_pulls(self._api(open_pulls_args(), repo_root))

    def pr_detail(self, repo_root: str, pr: OpenPr) -> OpenPr:
        detail = json.loads(self._api(pull_args(pr.number), repo_root))
        runs = json.loads(self._api(checks_args(pr.head_sha), repo_root))
        status = json.loads(self._api(status_args(pr.head_sha), repo_root))
        reviews = json.loads(self._api(reviews_args(pr.number), repo_root))
        return replace(pr, mergeable_state=str(detail.get("mergeable_state") or ""),
                       checks=checks_of(rollup_of(runs, status)), review=review_of(reviews))
