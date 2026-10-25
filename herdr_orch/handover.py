"""Handover: the user passes an agent they seated to its Topic's Orchestrator, and takes it back."""

from __future__ import annotations

from pathlib import Path

from herdr_orch.herdr import Herdr
from herdr_orch.state import TopicState, find_registration
from herdr_orch.topics import Topic, topic_for_workspace


def take(topics: list[Topic], states: Path, herdr: Herdr, name: str, home: str | None = None) -> Topic:
    """Register the agent under the Topic its repo belongs to, then brief it."""
    agent = next((a for a in herdr.agents() if a.name == name), None)
    if agent is None:
        raise ValueError(f"no live herdr agent named {name}")
    topic = topic_for_workspace(herdr.workspaces(), agent.workspace_id, topics, home)
    if topic is None:
        raise ValueError(f"{name}'s repo belongs to no Topic, so no Orchestrator can take it")
    TopicState(states, topic.name).take(agent.pane_id, name)
    herdr.prompt(
        name,
        f"You have been handed over to the Orchestrator of Topic {topic.name} until the user takes you back. "
        f"Read the brief at {topic.brief_path} and follow it from now on; your questions now reach the Orchestrator.",
    )
    return topic


def give_back(states: Path, herdr: Herdr, name: str) -> None:
    """Remove the agent from the Registry, found by name, so it works even after the agent exited."""
    registration = find_registration(states, name)
    if registration is None:
        raise ValueError(f"{name} is not handed over")
    topic, pane = registration
    TopicState(states, topic).give_back(pane)
    if any(a.name == name for a in herdr.agents()):
        herdr.prompt(name, "You are back with the user: your questions reach them directly again.")
