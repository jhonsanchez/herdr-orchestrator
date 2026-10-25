"""herdr-orch: the command line behind the plugin's actions and panes.

herdr plugin actions take no arguments, so everything that needs one (a topic,
a branch) goes through this CLI; the start action finds its topic from the
workspace it was invoked in.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # tomllib; macOS's /usr/bin/python3 is 3.9
    raise SystemExit("herdr-orchestrator needs Python 3.11 or newer")

import argparse
import json
import os
import signal
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from herdr_orch.handover import give_back, take
from herdr_orch.herdr import CliHerdr, Herdr, find_key
from herdr_orch.notify import notifier
from herdr_orch.runner import Runner
from herdr_orch.seating import seat, start
from herdr_orch.state import TopicState, find_managed, state_root
from herdr_orch.supervisor import Supervisor
from herdr_orch.topics import Topic, config_root, init_topic, load_topics, topic_for, topic_for_workspace

CLI = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-orch")
TICK_SECONDS = 15.0


def resolve_topic(context_json: str, workspace_env: str, topics: list[Topic], herdr: Herdr, home: str | None = None) -> Topic | None:
    """The Topic of the workspace an action ran in: its repo root, matched by membership."""
    try:
        context = json.loads(context_json) if context_json else {}
    except json.JSONDecodeError:
        context = {}
    repo_root = find_key(context, "repo_root")
    if repo_root:
        return topic_for(repo_root, topics, home)
    ws_id = find_key(context, "workspace_id") or workspace_env
    return topic_for_workspace(herdr.workspaces(), ws_id, topics, home)


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] = os.environ,
    now: Callable[[], float] = time.time,
    adapter: Herdr | None = None,
) -> int:
    p = argparse.ArgumentParser(prog="herdr-orch")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="scaffold a Topic").add_argument("topic")
    start_cmd = sub.add_parser("start", help="seat a Topic's Orchestrator (Topic from the workspace when omitted)")
    start_cmd.add_argument("topic", nargs="?")
    seat_cmd = sub.add_parser("seat", help="seat an agent on a branch")
    seat_cmd.add_argument("topic")
    seat_cmd.add_argument("--workspace", required=True)
    seat_cmd.add_argument("--branch", required=True)
    seat_cmd.add_argument("--label")
    sub.add_parser("heartbeat", help="record the Orchestrator's Heartbeat").add_argument("topic")
    sub.add_parser("watch", help="run a Topic's Watch runner").add_argument("topic")
    sub.add_parser("take", help="hand an agent you seated over to its Topic's Orchestrator").add_argument("agent")
    sub.add_parser("give-back", help="take a handed-over agent back").add_argument("agent")
    sub.add_parser("is-managed", help="exit 0 when the calling pane's agent is Managed (for your own hooks)")
    args = p.parse_args(argv)

    if args.cmd == "is-managed":
        pane = env.get("HERDR_PANE_ID")
        return 0 if pane and find_managed(state_root(env), pane) else 1

    herdr: Herdr = adapter or CliHerdr()
    configs, states = config_root(env), state_root(env)
    if not (env.get("HERDR_ORCH_CONFIG") or env.get("HERDR_PLUGIN_CONFIG_DIR")):
        # Outside a plugin command, ask herdr rather than assume its default path.
        configs = Path(herdr.plugin_config_dir() or configs)
    if args.cmd == "init":
        print(init_topic(configs, args.topic))
        TopicState(states, args.topic).ensure()
        return 0

    topics = load_topics(configs)
    if args.cmd in ("take", "give-back"):
        try:
            if args.cmd == "take":
                print(f"{args.agent} handed over to Topic {take(topics, states, herdr, args.agent, env.get('HOME')).name}")
            else:
                give_back(states, herdr, args.agent)
                print(f"{args.agent} given back")
        except (ValueError, RuntimeError) as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    if args.cmd == "start" and not args.topic:
        topic = resolve_topic(env.get("HERDR_PLUGIN_CONTEXT_JSON", ""), env.get("HERDR_WORKSPACE_ID", ""), topics, herdr, env.get("HOME"))
        if topic is None:
            print("no Topic matches this workspace's repo", file=sys.stderr)
            return 2
    else:
        topic = next((t for t in topics if t.name == args.topic), None)
        if topic is None:
            print(f"unknown Topic: {args.topic} (in {configs / 'topics'})", file=sys.stderr)
            return 2
    state = TopicState(states, topic.name)

    try:
        if args.cmd == "heartbeat":
            state.touch_heartbeat(now())
        elif args.cmd == "start":
            if not start(topic, state, herdr, CLI):
                return 1
        elif args.cmd == "seat":
            seated = seat(topic, state, herdr, args.workspace, args.branch, args.label, env.get("HOME"))
            print(f"{seated.name} {seated.pane_id}")
        elif args.cmd == "watch":
            _watch(topic, state, herdr, now)
    except (ValueError, RuntimeError, TimeoutError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _stop(signum: int, frame: object) -> None:
    raise SystemExit(0)


def _watch(topic: Topic, state: TopicState, herdr: Herdr, now: Callable[[], float]) -> None:
    """Runs until stopped. herdr requires plugins to stop their children on exit, so
    SIGTERM and SIGHUP (the pane closing) stop the Watches too."""
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    watches = Supervisor(topic.watches_dir)
    runner = Runner(topic, state, herdr, watches, notifier(topic.notify_command))
    try:
        while True:
            try:
                runner.tick(now())
            except Exception:
                # A failed herdr call must not end an unattended night: report it and tick again.
                traceback.print_exc()
                watches.poll(now())
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        watches.stop()


if __name__ == "__main__":
    sys.exit(main())
