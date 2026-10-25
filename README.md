# herdr-orchestrator

Let coding agents in [herdr](https://herdr.dev) keep working while you're away, with one view of what needs you.

**Status:** the herdr plugin core (topics, the Watch runner, the heartbeat, seating) is in place. The routing hooks (#1) and the Attention pane (#2) are next. The vocabulary is in [CONTEXT.md](CONTEXT.md) and the decisions are in [docs/adr/](docs/adr/).

## Requirements
- herdr 0.9.5 or newer, on Linux or macOS
- Python 3.11 or newer (stdlib only)

## Install
```sh
herdr plugin install jhonsanchez/herdr-orchestrator
```

## Topics
A Topic is one rule set with its own account and credentials. Each Topic has its own Orchestrator. Create one with:

```sh
~/.config/herdr/plugins/github/<install>/bin/herdr-orch init acme
```

That creates `~/.config/herdr/plugins/config/jhonsanchez.orchestrator/topics/acme/` containing:

| File | What it holds |
|---|---|
| `topic.toml` | Only what differs from the defaults: `match` (repo-root patterns), `model`, `base`, `[env]`, `notify_command`, and the minute windows |
| `playbook.md` | What this Topic's Orchestrator does. Its **Yours alone** section lists what it must never decide alone |
| `brief.md` | The brief every agent seated for this Topic reads |
| `watches/` | Any executable here is kept running, and each line it prints becomes an Event |

## Use
- **Start an Orchestrator:** run the plugin action `jhonsanchez.orchestrator.start` from a workspace whose repo matches a Topic, or run `herdr-orch start acme`. You get an `orch-acme` workspace with the Orchestrator agent and, beside it, the Watch runner.
- **Seat an agent:** `herdr-orch seat acme --workspace <id> --branch <branch>`. This creates the worktree, exports the Topic's env, starts the agent, adds it to the Registry and sends it its brief.
- **Events:**
  - agents that become `blocked`, `done` or `gone`
  - silent panes
  - your watches' output

  They reach the Orchestrator as a prompt when it's idle. When it finishes that turn, the runner records a Heartbeat.
- **Unattended:** if the Orchestrator agent is missing, or Events wait a whole window with no Heartbeat, `notify_command` is called once, with the title and body on stdin.

State lives in `~/.local/state/herdr/plugins/jhonsanchez.orchestrator/topics/<topic>/` (`HERDR_ORCH_STATE` overrides it).

## Develop
```sh
python3 -m unittest
uvx mypy
```
