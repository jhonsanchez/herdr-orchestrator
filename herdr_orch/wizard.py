"""The setup wizard: detected defaults for a new Topic, and the questions that confirm them.

Detection is pure (it takes paths and the environment); asking goes through an
`ask` callable, so a terminal, a herdr popup or a test can answer.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Mapping

from herdr_orch.topics import NAME, Answers, expand_home, parse_notify

Ask = Callable[[str, str], str]  # (question, default) -> answer; an empty answer means the default
CHECKLIST = [
    "Merging pull requests",
    "Production deploys, promotions and cut-overs",
    "Spending money: billing, budgets, paid plans",
    "Secrets and credentials: creating, granting, rotating",
]


def suggest_name(repo_root: str) -> str:
    """A Topic name from the repo's parent folder: ~/projects/personal/x → personal."""
    name = re.sub(r"[^a-z0-9-]+", "-", Path(repo_root).parent.name.lower()).strip("-")
    if not name or not name[0].isalpha():
        name = "t-" + name
    return name[:32]


def _from_home(path: str, home: str) -> str:
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def suggest_match(repo_root: str, home: str) -> str:
    """The repo's parent folder as a pattern, written from ~ when it lives there."""
    return f"{_from_home(str(Path(repo_root).parent), home)}/*"


def matching_repos(pattern: str, repo_roots: list[str], home: str) -> list[str]:
    return sorted({r for r in repo_roots if fnmatch(r, expand_home(pattern, home))})


def suggest_account(env: Mapping[str, str], home: str) -> dict[str, str]:
    """The Claude account the current shell uses, as the env a seated agent needs; none for the default."""
    config = env.get("CLAUDE_CONFIG_DIR", "").rstrip("/")
    if not config or config == f"{home}/.claude":
        return {}
    return {"CLAUDE_CONFIG_DIR": _from_home(config, home)}


def _account(answer: str) -> dict[str, str] | None:
    pairs = [p for p in answer.split() if "=" in p]
    return dict(p.split("=", 1) for p in pairs) or None


def _checklist(untick: str, extra: str) -> list[str]:
    drop = {int(n) for n in re.findall(r"\d+", untick)}
    kept = [item for i, item in enumerate(CHECKLIST, 1) if i not in drop]
    return kept + [x.strip() for x in extra.split(";") if x.strip() and x.strip().lower() != "none"]


def _free(name: str, taken: set[str] | frozenset[str]) -> str:
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name[:29]}-{n}", n + 1
    return candidate


def ask_answers(repo_root: str, repo_roots: list[str], env: Mapping[str, str], home: str, ask: Ask,
                has_stop_hook: bool, taken: set[str] | frozenset[str] = frozenset(),
                name_default: str | None = None) -> tuple[str, Answers]:
    """The wizard's questions, each with its detected default. Returns the Topic name and Answers."""
    suggestion = _free(name_default or suggest_name(repo_root), taken)
    name = ask("Topic name", suggestion)
    while name in taken or not NAME.match(name):
        suggestion = _free(suggestion, taken | {name})
        name = ask(f"'{name}' is taken or not a valid name (lowercase letters, digits, -). Topic name", suggestion)
    default_match = suggest_match(repo_root, home)
    match = ask(f"Repos (a pattern; {default_match} covers {len(matching_repos(default_match, repo_roots, home))} repos "
                "open in herdr)", default_match)
    model = ask("Model for seated agents", "sonnet")
    account = suggest_account(env, home)
    env_answer = ask("Claude account: env a seated agent needs, KEY=VALUE, or none",
                     " ".join(f"{k}={v}" for k, v in account.items()) or "none")
    notify = ask("Push alerts: stop-hook (your Claude Stop hook), herdr, none, or a command",
                 "stop-hook" if has_stop_hook else "herdr")
    numbered = "; ".join(f"{i}. {item}" for i, item in enumerate(CHECKLIST, 1))
    untick = ask(f"Yours alone, all ticked: {numbered}. Numbers to untick", "none")
    extra = ask("More items that are yours alone, separated by ';'", "none")
    mode, command = parse_notify(notify)
    return name, Answers(match=[match], model=model, env=_account(env_answer), notify=mode,
                         notify_command=command, yours_alone=_checklist(untick, extra))
