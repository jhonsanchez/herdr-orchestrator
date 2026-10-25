# herdr-orchestrator

Let coding agents in [herdr](https://herdr.dev) keep working while you're away, with one view of what needs you.

**Status:** the herdr plugin core (Topics, the Watch runner, the Heartbeat, Seating) the routing hooks with Handover, and the Attention pane are in place. The vocabulary is in [CONTEXT.md](CONTEXT.md) and the decisions are in [docs/adr/](docs/adr/).

## Requirements
- herdr 0.9.5 or newer, on Linux or macOS
- Python 3.11 or newer for `herdr-orch` (stdlib only). The routing hooks also run on older Python 3; they never block a session

## Install
Two plugins come from this one repo (ADR-0002):

```sh
herdr plugin install jhonsanchez/herdr-orchestrator          # Topics, Watch runner, Seating, actions
claude plugin marketplace add jhonsanchez/herdr-orchestrator
claude plugin install herdr-orchestrator@herdr-orchestrator  # the routing hooks
```

The hooks change nothing for an agent that isn't Managed. For a Managed one:
- an AskUserQuestion becomes Inbox items, and the dialog is refused with a reason the agent follows (validated in #4);
- a permission prompt is recorded, and the agent still waits;
- a turn that ends in a question is recorded as a guess.

The format is in [docs/inbox-v1.md](docs/inbox-v1.md).

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
- **See what needs you:** run the plugin action `jhonsanchez.orchestrator.attention` (bind it to a key with `[[keys.command]]`, `type = "plugin_action"`) or `herdr-orch attention`.
  - The list shows every Topic's open Inbox items, grouped by workspace, oldest first, plus any agent no Orchestrator manages that is `blocked` or `done`. Unattended Topics get a banner at the top.
  - The details show the selected item with its options, recommended first, then **Other…**.
  - **Keys:** `enter` answers (the answer goes straight to the agent), `o` opens the agent's pane, `s` skips, `q` quits.
- **Hand over an agent you seated:** `herdr-orch take <agent>` registers it under the Topic its repo belongs to and sends it the brief. `herdr-orch give-back <agent>` returns it to you.
- **Your own hooks:** `herdr-orch is-managed` exits 0 when the calling pane's agent is Managed. Use it to skip your own per-turn notifications for those agents.
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
