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
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from herdr_orch.attention import answer, dismiss, open_item
from herdr_orch.deploy import is_deployed, newer_install, plugins_dir
from herdr_orch.handover import give_back, take
from herdr_orch.herdr import CliHerdr, Herdr, find_key
from herdr_orch.notify import notifier_for, stop_hooks
from herdr_orch.reconcile import reconcile
from herdr_orch.runner import Runner
from herdr_orch.github import CliGitHub
from herdr_orch.ledger import status_rows, status_table
from herdr_orch.seating import ensure_link, release, seat, start, validated
from herdr_orch.state import TopicState, find_managed, state_root
from herdr_orch.supervisor import Supervisor
from herdr_orch.topics import Answers, Topic, parse_notify, config_root, init_topic, load_topic, load_topics, topic_for
from herdr_orch.wizard import ask_answers
from herdr_orch.watches_core import RUNNER_STALE_S

CLI = str(Path(__file__).resolve().parent.parent / "bin" / "herdr-orch")
TICK_SECONDS = 15.0
# A quiet pane still shows it is alive: one line every 5 minutes.
ALIVE_EVERY = 20


def context_repo_root(context_json: str, workspace_env: str, herdr: Herdr) -> str | None:
    """The repo root of the workspace an action or pane ran in."""
    try:
        context = json.loads(context_json) if context_json else {}
    except json.JSONDecodeError:
        context = {}
    repo_root = find_key(context, "repo_root")
    if repo_root:
        return repo_root
    ws_id = find_key(context, "workspace_id") or workspace_env
    ws = next((w for w in herdr.workspaces() if w.workspace_id == ws_id), None)
    return ws.repo_root if ws else None


def resolve_topic(context_json: str, workspace_env: str, topics: list[Topic], herdr: Herdr, home: str | None = None) -> Topic | None:
    """The Topic of the workspace an action ran in: its repo root, matched by membership."""
    repo_root = context_repo_root(context_json, workspace_env, herdr)
    return topic_for(repo_root, topics, home) if repo_root else None


def _checkout_root(cwd: str = ".") -> str | None:
    """The main checkout of the git repo around cwd (a worktree resolves to its main checkout)."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return str(Path(out.stdout.strip()).parent) if out.returncode == 0 and out.stdout.strip() else None


def _terminal_ask(question: str, default: str) -> str:
    return input(f"{question} [{default}]: ").strip() or default


def _setup(configs: Path, states: Path, herdr: Herdr, env: Mapping[str, str], repo_root: str,
           name: str | None = None, then_start: bool = True) -> int:
    """The wizard in a terminal: ask, write the Topic, send a test alert, then start its Orchestrator."""
    home = env.get("HOME") or str(Path.home())
    print(f"No Topic covers {repo_root} yet. Press Enter to take each suggestion.")
    roots = [w.repo_root for w in herdr.workspaces() if w.repo_root]
    taken = {t.name for t in load_topics(configs)}
    topic_name, answers = ask_answers(repo_root, roots, env, home, _terminal_ask, has_stop_hook=bool(stop_hooks(env)),
                                      taken=taken, name_default=name)
    draft = Topic(name=topic_name, dir=configs / "topics" / topic_name, env=answers.env or {},
                  notify=answers.notify or "none", notify_command=answers.notify_command or [])
    if _terminal_ask("Send a test alert before saving?", "yes").lower().startswith("y"):
        notifier_for(draft, env, herdr)("herdr-orchestrator test", f"alerts for Topic {topic_name} reach you")
        if not _terminal_ask("Did it arrive?", "yes").lower().startswith("y"):
            print("Saving anyway: change notify or notify_command in topic.toml, then run setup again to test.")
    try:
        topic = load_topic(init_topic(configs, topic_name, answers))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    TopicState(states, topic.name).ensure()
    print(f"Wrote {topic.dir}")
    if not then_start:
        return 0
    return 0 if start(topic, TopicState(states, topic.name), herdr, CLI) else 1


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] = os.environ,
    now: Callable[[], float] = time.time,
    adapter: Herdr | None = None,
) -> int:
    p = argparse.ArgumentParser(prog="herdr-orch")
    sub = p.add_subparsers(dest="cmd", required=True)
    init_cmd = sub.add_parser("init", help="create a Topic: asks in a terminal; flags answer without asking")
    init_cmd.add_argument("topic")
    init_cmd.add_argument("--match", action="append", help="repo-root pattern (repeatable)")
    init_cmd.add_argument("--model")
    init_cmd.add_argument("--account", action="append", metavar="KEY=VALUE", help="env a seated agent needs (repeatable)")
    init_cmd.add_argument("--notify", help="stop-hook, herdr, none, or a command")
    init_cmd.add_argument("--yours-alone", action="append", metavar="ITEM", help="never decided by the Orchestrator (repeatable)")
    start_cmd = sub.add_parser("start", help="seat a Topic's Orchestrator (Topic from the workspace when omitted)")
    start_cmd.add_argument("topic", nargs="?")
    seat_cmd = sub.add_parser("seat", help="seat an agent on a branch")
    seat_cmd.add_argument("topic")
    seat_cmd.add_argument("--workspace", required=True)
    seat_cmd.add_argument("--branch", required=True)
    seat_cmd.add_argument("--label")
    seat_cmd.add_argument("--name", help="agent name (default: from the branch, with a short suffix when long or taken)")
    seat_cmd.add_argument("--existing", action="store_true",
                          help="the branch is already pushed to origin: seat on it (an orphaned PR gets an owner again)")
    release_cmd = sub.add_parser("release", help="remove a branch's workspace and worktree once its PR is merged or closed")
    release_cmd.add_argument("topic")
    release_cmd.add_argument("--branch", required=True)
    validated_cmd = sub.add_parser("validated", help="an agent's signal that its branch is validated and can be released")
    validated_cmd.add_argument("topic")
    validated_cmd.add_argument("--branch", required=True)
    validated_cmd.add_argument("--evidence", required=True,
                               help="what you ran to check the deployed change and what it showed (the command's exit and last lines)")
    inbox_cmd = sub.add_parser("inbox", help="write Inbox items as the Orchestrator").add_subparsers(dest="inbox_cmd", required=True)
    add_cmd = inbox_cmd.add_parser("add", help="put a decision for the user in the Attention pane")
    add_cmd.add_argument("--topic", required=True)
    add_cmd.add_argument("--agent", required=True)
    add_cmd.add_argument("--question", required=True)
    add_cmd.add_argument("--option", action="append", default=[], help="a choice, the recommended one first (repeatable)")
    add_cmd.add_argument("--kind", choices=("decision", "question"), default="decision")
    answer_cmd = sub.add_parser("answer", help="answer an Inbox item: deliver the text to whoever it is for, then close it")
    answer_cmd.add_argument("topic")
    answer_cmd.add_argument("item_id")
    answer_cmd.add_argument("text", nargs="?")
    answer_cmd.add_argument("--dismiss", action="store_true", help="close the item without delivering anything (made moot by events)")
    deployed_cmd = sub.add_parser("deployed", help="exit 0 when a merge is in the installed plugin and the Watch runner runs on it")
    deployed_cmd.add_argument("topic", nargs="?", default=os.environ.get("HERDR_ORCH_TOPIC"))
    deployed_cmd.add_argument("--sha", default=os.environ.get("HERDR_ORCH_MERGE_SHA", ""))
    sub.add_parser("heartbeat", help="record the Orchestrator's Heartbeat").add_argument("topic")
    status_cmd = sub.add_parser("status", help="print the branch ledger: every tracked branch and untracked open PR")
    status_cmd.add_argument("topic")
    status_cmd.add_argument("--json", action="store_true", help="rows as JSON, for scripts")
    sub.add_parser("reconcile", help="adopt live worktrees, drop dead Registry entries, compact the Inbox").add_argument("topic")
    sub.add_parser("watch", help="run a Topic's Watch runner").add_argument("topic")
    sub.add_parser("setup", help="the setup wizard for this repo's Topic, then start its Orchestrator")
    sub.add_parser("take", help="hand an agent you seated over to its Topic's Orchestrator").add_argument("agent")
    sub.add_parser("give-back", help="take a handed-over agent back").add_argument("agent")
    sub.add_parser("is-managed", help="exit 0 when the calling pane's agent is Managed (for your own hooks)")
    sub.add_parser("attention", help="the Attention pane: every Topic's items that need you")
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
        answers = _answers(args)
        if answers is None and sys.stdin.isatty():
            repo = _checkout_root()
            if repo:
                return _setup(configs, states, herdr, env, repo, name=args.topic, then_start=False)
        try:
            print(init_topic(configs, args.topic, answers))
        except (ValueError, FileExistsError) as e:
            print(e, file=sys.stderr)
            return 2
        TopicState(states, args.topic).ensure()
        return 0

    topics = load_topics(configs)
    if args.cmd == "attention":
        import curses

        from herdr_orch.pane import SCOPE_ENV, App

        os.environ.setdefault("ESCDELAY", "25")  # curses waits a full second after Esc otherwise
        scope = env.get(SCOPE_ENV) or None
        curses.wrapper(App(states, topics, herdr, scope=scope).run)
        return 0
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
    if args.cmd == "setup" or (args.cmd == "start" and not args.topic):
        repo = (env.get("HERDR_ORCH_SETUP_REPO")
                or context_repo_root(env.get("HERDR_PLUGIN_CONTEXT_JSON", ""), env.get("HERDR_WORKSPACE_ID", ""), herdr)
                or _checkout_root())
        found = topic_for(repo, topics, env.get("HOME")) if repo else None
        if found is None:
            if repo is None:
                print("not in a git repo, and herdr names no workspace repo", file=sys.stderr)
                return 2
            if args.cmd == "setup" or sys.stdin.isatty():
                return _setup(configs, states, herdr, env, repo)
            herdr.open_setup(repo)  # a plugin action: no terminal here, so ask in a popup
            print(f"No Topic covers {repo}: opened the setup wizard.")
            return 0
        topic = found
        if args.cmd == "setup":
            print(f"Topic {topic.name} already covers {repo}; starting it.")
            args.cmd = "start"
    else:
        named = next((t for t in topics if t.name == args.topic), None)
        if named is None:
            print(f"unknown Topic: {args.topic} (in {configs / 'topics'})", file=sys.stderr)
            return 2
        topic = named
    state = TopicState(states, topic.name)

    try:
        if args.cmd == "heartbeat":
            state.touch_heartbeat(now())
        elif args.cmd == "status":
            rows = status_rows(state, now())
            print(json.dumps(rows) if args.json else status_table(rows))
        elif args.cmd == "deployed":
            home = env.get("HOME") or str(Path.home())
            return 0 if is_deployed(args.sha, plugins_dir(env, home), state) else 1
        elif args.cmd == "start":
            home = env.get("HOME") or str(Path.home())
            if not start(topic, state, herdr, ensure_link(CLI, home, plugins_dir(env, home))):
                return 1
        elif args.cmd == "inbox":
            pane = next((p for p, n in state.managed().items() if n == args.agent), None) or next(
                (a.pane_id for a in herdr.agents() if a.name == args.agent), None)
            if pane is None:
                raise ValueError(f"no agent named {args.agent}")
            state.escalate(args.agent, pane, args.kind, args.question, args.option, now(), to="orchestrator")
        elif args.cmd == "answer":
            row = open_item(state, args.item_id)
            if args.dismiss:
                dismiss(states, row)
            elif not args.text:
                raise ValueError("give the answer text, or --dismiss to close the item without one")
            else:
                print(f"answered {args.item_id}: sent to {answer(states, row, args.text, herdr, topic.orchestrator)}")
        elif args.cmd == "seat":
            seated = seat(topic, state, herdr, args.workspace, args.branch, args.label, env.get("HOME"),
                          existing=args.existing, now=now, name=args.name,
                          cli=ensure_link(CLI, env.get("HOME") or str(Path.home()),
                                          plugins_dir(env, env.get("HOME") or str(Path.home()))))
            print(f"{seated.name} {seated.pane_id}")
        elif args.cmd == "release":
            release(state, herdr, CliGitHub(), args.branch)
            print(f"{args.branch} released")
        elif args.cmd == "validated":
            validated(state, args.branch, now(), args.evidence, bool(topic.deploy_check))
            print(f"{args.branch} marked validated")
        elif args.cmd == "reconcile":
            print(reconcile(topic, state, herdr, now(), home=env.get("HOME")).summary())
        elif args.cmd == "watch":
            _watch(topic, state, herdr, now, env)
    except (ValueError, RuntimeError, TimeoutError, subprocess.CalledProcessError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _answers(args: argparse.Namespace) -> Answers | None:
    """The init flags as Answers; None when none was given (the template as it ships)."""
    if not any((args.match, args.model, args.account, args.notify, args.yours_alone)):
        return None
    notify, command = parse_notify(args.notify or "")
    return Answers(
        match=args.match, model=args.model,
        env=dict(a.split("=", 1) for a in args.account) if args.account else None,
        notify=notify, notify_command=command, yours_alone=args.yours_alone,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop(signum: int, frame: object) -> None:
    raise SystemExit(0)


def _reexec_if_updated(topic: Topic, env: Mapping[str, str], watches: Supervisor, state: TopicState) -> None:
    """Between ticks: when a newer install sits next to the one running, repoint the stable link at it and
    re-exec the runner through that link, so an update needs no manual restart. Returns when there is none."""
    home = env.get("HOME") or str(Path.home())
    plugins = plugins_dir(env, home)
    if not newer_install(plugins, CLI):
        return
    link = ensure_link(CLI, home, plugins)
    print(f"newer install found: restarting the Watch runner from {Path(link).resolve()}", flush=True)
    watches.stop()
    state.clear_runner(os.getpid())
    os.execve(link, [link, "watch", topic.name], {k: v for k, v in os.environ.items() if k != "PYTHONPATH"})


def _watch(topic: Topic, state: TopicState, herdr: Herdr, now: Callable[[], float],
           env: Mapping[str, str] = os.environ) -> None:
    """Runs until stopped, or until its Orchestrator has been gone a grace period. herdr requires
    plugins to stop their children on exit, so SIGTERM and SIGHUP stop the Watches too."""
    existing = state.runner_status()
    if existing and existing.pid != os.getpid() and now() - existing.at < RUNNER_STALE_S and _alive(existing.pid):
        print(f"a Watch runner for {topic.name} is already running (pid {existing.pid})", file=sys.stderr)
        return
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    watches = Supervisor(topic.watches_dir)
    def say(line: str) -> None:
        print(line, flush=True)

    runner = Runner(topic, state, herdr, watches, notifier_for(topic, herdr=herdr), say, CliGitHub())
    say(f"Watch runner: topic {topic.name}")
    try:
        say(f"reconciled: {reconcile(topic, state, herdr, now()).summary()}")
    except Exception:
        traceback.print_exc()  # stale records are no reason to skip watching
    try:
        ticks = 0
        while True:
            try:
                runner.tick(now())
                ticks += 1
                if ticks % ALIVE_EVERY == 0:
                    say(f"{time.strftime('%H:%M:%S')} watching, {ticks} ticks")
            except Exception:
                # A failed herdr call must not end an unattended night: report it and tick again.
                traceback.print_exc()
                watches.poll(now())
            if runner.done:
                break
            _reexec_if_updated(topic, env, watches, state)
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        watches.stop()
        state.clear_runner(os.getpid())


if __name__ == "__main__":
    sys.exit(main())
