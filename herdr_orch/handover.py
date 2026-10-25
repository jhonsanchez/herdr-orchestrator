"""Handover: the user passes an agent they seated to its Topic's Orchestrator, and takes it back."""

from __future__ import annotations

from pathlib import Path

from herdr_orch.herdr import Agent, Herdr
from herdr_orch.state import TopicState, find_managed
from herdr_orch.topics import Topic, topic_for


def _locate(herdr: Herdr, name: str) -> Agent:
    agent = next((a for a in herdr.agents() if a.name == name), None)
    if agent is None:
        raise ValueError(f"no live herdr agent named {name}")
    return agent


def take(topics: list[Topic], states: Path, herdr: Herdr, name: str, home: str | None = None) -> Topic:
    """Register the agent under the Topic its repo belongs to, then brief it."""
    agent = _locate(herdr, name)
    ws = next((w for w in herdr.workspaces() if w.workspace_id == agent.workspace_id), None)
    topic = topic_for(ws.repo_root, topics, home) if ws and ws.repo_root else None
    if topic is None:
        raise ValueError(f"{name}'s repo belongs to no Topic, so no Orchestrator can take it")
    TopicState(states, topic.name).take(agent.pane_id, name)
    herdr.prompt(
        name,
        f"You have been handed over to the Orchestrator of topic {topic.name} until the user takes you back. "
        f"Read the brief at {topic.brief_path} and follow it from now on; your questions now reach the Orchestrator.",
    )
    return topic


def give_back(states: Path, herdr: Herdr, name: str) -> None:
    agent = _locate(herdr, name)
    managed = find_managed(states, agent.pane_id)
    if managed is None:
        raise ValueError(f"{name} is not handed over")
    TopicState(states, managed[0]).give_back(agent.pane_id)
    herdr.prompt(name, "You are back with the user: your questions reach them directly again.")
