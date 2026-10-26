# herdr-orchestrator

Let coding agents in [herdr](https://herdr.dev) keep working while you're away, with one view of what needs you.

**Status:** the herdr plugin core (Topics, the Watch runner, the Heartbeat, Seating), the routing hooks with Handover, and the Attention pane are in place. The vocabulary is in [CONTEXT.md](CONTEXT.md) and the decisions are in [docs/adr/](docs/adr/).

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

Herdr doesn't report where it installed a plugin, and keeps old installations after `herdr plugin update`. To get a stable path for scripts, run the `link` action once after installing and after each update; it points `~/.local/bin/herdr-orch` at the current installation:

```sh
herdr plugin action invoke jhonsanchez.orchestrator.link
```

The hooks change nothing for an agent that isn't Managed. For a Managed one:
- an AskUserQuestion becomes Inbox items, and the dialog is refused with a reason the agent follows (validated in #4);
- a permission prompt is recorded, and the agent still waits;
- a turn that ends in a question is recorded as a guess.

The format is in [docs/inbox-v1.md](docs/inbox-v1.md).

## Topics
A Topic is one rule set with its own account and credentials. Each Topic has its own Orchestrator.

**You don't need to create one first.** Start an Orchestrator from a repo that no Topic covers, and the setup wizard asks for one. Every question has a detected default, so pressing Enter all the way through gives a working Topic:

| Question | Default |
|---|---|
| Name | the repo's parent folder (`~/projects/personal/x` → `personal`) |
| Repos | that folder as a pattern, with how many repos open in herdr it covers |
| Model | sonnet, for the Orchestrator and the agents it seats (`orchestrator_model` in `topic.toml` gives the Orchestrator its own) |
| Claude account | what your shell uses (`CLAUDE_CONFIG_DIR` when set) |
| Push alerts | `stop-hook`: your own Claude Stop hook, run with the alert as its message, so nothing is copied into the Topic. Without one, `herdr` (herdr's notification). Or `none`, or any command |
| Yours alone | a pre-ticked checklist: merging, production, spending, secrets |

It then offers a test alert and starts the Orchestrator. The `start` action opens the wizard in a herdr popup; in a terminal, `herdr-orch setup` (or `herdr-orch init <name>`, which doesn't start anything) asks inline. Every answer is also a flag, so `init` runs without questions: `herdr-orch init acme --match '~/code/*' --model sonnet --notify stop-hook --yours-alone 'Merging pull requests'`.

There's no base-branch question: `seat` branches from each repo's own default branch (`origin/HEAD`). Set `base` in `topic.toml` only when a Topic branches from somewhere else.

A Topic lives in `~/.config/herdr/plugins/config/jhonsanchez.orchestrator/topics/<name>/`:

| File | What it holds |
|---|---|
| `topic.toml` | Only what differs from the defaults: `match` (repo-root patterns), `model`, `base`, `[env]`, `notify` (`stop-hook`, `herdr`, `none`) or `notify_command`, and the minute windows |
| `playbook.md` | What this Topic's Orchestrator does. Its **Yours alone** section lists what it must never decide alone |
| `brief.md` | The brief every agent seated for this Topic reads |
| `watches/` | Any executable here is kept running, and each line it prints becomes an Event |

## Use
- **Start an Orchestrator:** run the plugin action `jhonsanchez.orchestrator.start` from a workspace whose repo matches a Topic, or run `herdr-orch start acme`. You get an `orch-acme` workspace with the Orchestrator agent and the Attention pane in a column on its right (the runner reopens the pane if you close it). The Watch runner runs as a background process of its own, with no pane; its output goes to `runner.log` in the Topic's state folder, and it stops by itself a minute after the Orchestrator's workspace is gone.
- **Seat an agent:** `herdr-orch seat acme --workspace <id> --branch <branch>`. This creates the worktree, exports the Topic's env, starts the agent, adds it to the Registry and sends it its brief.
- **Follow a branch's PR:** `seat` records each branch; the Watch runner asks GitHub (`gh`) about its PR every two minutes and reports it opening, its checks changing and its merge or close as `pr` Events, until it is merged or closed. An open PR whose agent is gone, whose checks fail, or which has no checks shows as a `followup` row in the Attention pane (and alerts once after a Heartbeat window). `herdr-orch seat T --workspace <id> --branch <b> --existing` re-seats an agent on a branch already pushed to origin, so an orphaned PR gets an owner again. `herdr-orch release T --branch <b>` removes the branch's workspace and worktree, and refuses while its PR is open.
- **See what needs you:** run the plugin action `jhonsanchez.orchestrator.attention` (bind it to a key with `[[keys.command]]`, `type = "plugin_action"`) or `herdr-orch attention`.
  - The list shows every Topic's open Inbox items, grouped by workspace and then agent, oldest first, plus any agent no Orchestrator manages that is `blocked` or `done`. Unattended Topics get a banner at the top, as does a Topic whose Watch runner has stopped ticking; a live runner shows as a dim `watching, N ticks` line. A Managed agent that finished, went gone or sat idle with no Inbox item written for it shows as a `followup` row until the Orchestrator writes one with `herdr-orch inbox add --topic T --agent A --question Q --option O...`; after a Heartbeat window it also gets a banner and one alert.
  - The details show the selected item with its options, recommended first, then **Other…**.
  - **Keys:** `enter` answers (the answer goes straight to the agent's pane), `o` opens that pane, `s` skips (a skipped `blocked`/`done` row comes back when the agent's status changes), `q` quits.
- **Hand over an agent you seated:** `herdr-orch take <agent>` registers it under the Topic its repo belongs to and sends it the brief. `herdr-orch give-back <agent>` returns it to you.
- **Your own hooks:** `herdr-orch is-managed` exits 0 when the calling pane's agent is Managed. Use it to skip your own per-turn notifications for those agents.
- **Events:**
  - agents that become `blocked`, `done` or `gone`
  - silent panes
  - your watches' output

  They reach the Orchestrator as a prompt when it's idle. When it finishes that turn, the runner records a Heartbeat.
- **Unattended:** if the Orchestrator agent is missing, or Events wait a whole window with no Heartbeat, you get one alert: through `notify_command` (title and body on stdin) when set, else through the `notify` mode.

State lives in `~/.local/state/herdr/plugins/jhonsanchez.orchestrator/topics/<topic>/` (`HERDR_ORCH_STATE` overrides it).

## Develop
```sh
python3 -m unittest
uvx mypy
```
