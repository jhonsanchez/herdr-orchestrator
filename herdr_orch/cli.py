"""herdr-orch: the command line behind the plugin's actions and panes.

herdr plugin actions take no arguments, so everything that needs one (a topic,
a branch) goes through this CLI; the start action finds its topic from the
workspace it was invoked in.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from herdr_orch.herdr import CliHerdr, Herdr
from herdr_orch.notify import notifier
from herdr_orch.runner import Runner
from herdr_orch.seating import seat, start
from herdr_orch.state import TopicState, state_root
from herdr_orch.supervisor import Supervisor
from herdr_orch.topics import Topic, config_root, init_topic, load_topics, topic_for

CLI = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-orch")
TICK_SECONDS = 15.0


def _find(context: Any, key: str) -> str | None:
    if isinstance(context, dict):
        if isinstance(context.get(key), str):
            return str(context[key])
        for v in context.values():
            found = _find(v, key)
            if found:
                return found
    return None


def resolve_topic(context_json: str, workspace_env: str, topics: list[Topic], herdr: Herdr, home: str | None = None) -> Topic | None:
    """The Topic of the workspace an action ran in: its repo root, matched by membership."""
    try:
        context = json.loads(context_json) if context_json else {}
    except json.JSONDecodeError:
        context = {}
    repo_root = _find(context, "repo_root")
    if not repo_root:
        ws_id = _find(context, "workspace_id") or workspace_env
        ws = next((w for w in herdr.workspaces() if w.workspace_id == ws_id), None)
        repo_root = ws.repo_root if ws else None
    return topic_for(repo_root, topics, home) if repo_root else None


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] = os.environ,
    now: Callable[[], float] = time.time,
    herdr: Herdr | None = None,
) -> int:
    p = argparse.ArgumentParser(prog="herdr-orch")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="scaffold a topic").add_argument("topic")
    s = sub.add_parser("start", help="seat a topic's orchestrator (topic from the workspace when omitted)")
    s.add_argument("topic", nargs="?")
    s = sub.add_parser("seat", help="seat an agent on a branch")
    s.add_argument("topic")
    s.add_argument("--workspace", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--label")
    sub.add_parser("heartbeat", help="record the orchestrator's heartbeat").add_argument("topic")
    sub.add_parser("watch", help="run a topic's watch runner").add_argument("topic")
    args = p.parse_args(argv)

    cfg, st = config_root(env), state_root(env)
    if args.cmd == "init":
        print(init_topic(cfg, args.topic))
        return 0

    topics = load_topics(cfg)
    h = herdr or CliHerdr()
    if args.cmd == "start" and not args.topic:
        topic = resolve_topic(env.get("HERDR_PLUGIN_CONTEXT_JSON", ""), env.get("HERDR_WORKSPACE_ID", ""), topics, h, env.get("HOME"))
        if topic is None:
            print("no topic matches this workspace's repo", file=sys.stderr)
            return 2
    else:
        topic = next((t for t in topics if t.name == args.topic), None)
        if topic is None:
            print(f"unknown topic: {args.topic} (in {cfg / 'topics'})", file=sys.stderr)
            return 2
    state = TopicState(st, topic.name)

    if args.cmd == "heartbeat":
        state.touch_heartbeat(now())
    elif args.cmd == "start":
        if not start(topic, state, h, CLI):
            return 1
    elif args.cmd == "seat":
        r = seat(topic, state, h, args.workspace, args.branch, args.label, env.get("HOME"))
        print(f"{r.name} {r.pane_id}")
    elif args.cmd == "watch":
        _watch(topic, state, h, now)
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
