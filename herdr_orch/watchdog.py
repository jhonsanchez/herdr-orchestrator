"""The watchdog: a ledger state held past its limit raises one `stuck` Event, nudges the owning
agent once, and after a second limit becomes an Attention follow-up for the person. What was done
for each (branch, state, since) lives in the branch record, so a runner restart repeats nothing and
a state that changes starts over."""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_LIMITS_MIN = {"ci": 20, "review": 60, "conflict": 15, "deploy": 15, "validate": 10, "agent": 10}


def limit_min(waiting_on: str, overrides: Mapping[str, int]) -> int | None:
    """Minutes a state may be held; None when it has no limit (merge and human have none unless set)."""
    return overrides.get(waiting_on, DEFAULT_LIMITS_MIN.get(waiting_on))


def stuck_key(ledger: Mapping[str, Any]) -> str:
    return f"{ledger['waiting_on']}@{ledger['since']}"


def stage(ledger: Mapping[str, Any], record: Mapping[str, Any] | None, overrides: Mapping[str, int],
          now: float) -> str | None:
    """'stuck' when the state just passed its limit and no Event was raised for it, 'escalate' once it is
    past a second limit and not yet raised to the person, 'nudge' while the agent still owes taking the prompt; None when nothing is due."""
    if ledger.get("state") != "waiting_on":
        return None
    limit = limit_min(str(ledger["waiting_on"]), overrides)
    if limit is None:
        return None
    held = now - float(ledger["since"])
    done = record if record and record.get("key") == stuck_key(ledger) else {}
    if held >= 2 * limit * 60 and done.get("event") and not done.get("escalated"):
        return "escalate"
    if held >= limit * 60 and not done.get("event"):
        return "stuck"
    if held >= limit * 60 and not done.get("nudged") and not done.get("escalated"):
        return "nudge"  # the Event is out but the agent did not take the prompt yet
    return None


def nudge_text(waiting_on: str, minutes: int) -> str:
    return (f"[herdr-orchestrator] You have been waiting on {waiting_on} for {minutes} minutes: "
            "say what you are waiting on, or continue.")
