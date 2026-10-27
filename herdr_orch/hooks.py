"""The routing hooks (ADR-0001): a Managed agent's questions and permission prompts
become Inbox items instead of reaching the user.

Claude Code runs these in every session that has the plugin, so anything else
must pass through untouched: a pane missing from the Registry gets no output,
and so does any error.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Mapping

from herdr_orch.attention import current_items
from herdr_orch.state import TopicState, find_managed, state_root

# Worded as validated by the #4 spike.
DENY_REASON = (
    "Escalated to the orchestrator: your question was recorded in its inbox. Do not ask it again, "
    "by dialog or in text. Keep working on anything that does not depend on the answer, then end "
    "your turn; the answer will arrive as a prompt."
)
TRAILING_MARKUP = re.compile(r"[\s*_`]+$")


def _summary(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "url", "pattern", "description"):
            if isinstance(tool_input.get(key), str):
                return " ".join(str(tool_input[key]).split())[:200]
    return ""


def _recommended_first(labels: list[str]) -> list[str]:
    return sorted(labels, key=lambda label: "(Recommended)" not in label)


def _last_paragraph(message: str) -> str:
    text = TRAILING_MARKUP.sub("", message.strip())
    return text.split("\n\n")[-1].strip()


def handle(payload: Mapping[str, Any], env: Mapping[str, str], now: float) -> dict[str, Any] | None:
    """The hook's stdout as JSON, or None for no output. Writes Inbox items for a Managed agent."""
    pane = env.get("HERDR_PANE_ID")
    if not pane:
        return None
    root = state_root(env)
    managed = find_managed(root, pane)
    if managed is None:
        return None
    agent = managed.agent
    state = TopicState(root, managed.topic)
    event = payload.get("hook_event_name")

    if event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        for q in payload.get("tool_input", {}).get("questions", []):
            options = _recommended_first([str(o.get("label", "")) for o in q.get("options", [])])
            state.escalate(agent, pane, "decision", str(q.get("question", "")), options, now)
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": DENY_REASON,
        }}

    if event == "UserPromptSubmit":
        # The answer is arriving: what this agent asked is no longer waiting. Orchestrator-raised
        # items (to: orchestrator) are about the agent, not from it, and stay.
        for item in current_items(state.inbox_lines()):
            if item.get("pane") == pane and item.get("agent") == agent and item.get("to", "agent") == "agent":
                state.resolve(str(item["id"]), "resolved", "(answered by a prompt)")
        return None

    if event == "PermissionRequest":
        tool = str(payload.get("tool_name", "a tool"))
        detail = _summary(payload.get("tool_input"))
        question = f"Allow {tool}: {detail}" if detail else f"Allow {tool}"
        state.escalate(agent, pane, "permission", question, [], now)
        return None

    if event == "Stop" and not payload.get("stop_hook_active"):
        last = _last_paragraph(str(payload.get("last_assistant_message") or ""))
        if last.endswith("?"):
            state.escalate(agent, pane, "question", last, [], now, guess=True)
        # The runner hands this Event to the Orchestrator as a prompt: it hears of the finish
        # from the agent itself, not only from its pane going idle.
        state.append_event("stop", f"{agent} finished: {last[:200]}" if last else f"{agent} finished", now)
        return None
    return None


def main() -> int:
    try:
        out = handle(json.load(sys.stdin), os.environ, time.time())
    except Exception:
        return 0  # never break a session over routing
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
