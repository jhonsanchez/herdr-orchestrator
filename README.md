# herdr-orchestrator

Let coding agents in [herdr](https://herdr.dev) keep working while you're away, with one view of what needs you.

**Status: design.** Nothing is implemented yet. The vocabulary is in [CONTEXT.md](CONTEXT.md) and the decisions in [docs/adr/](docs/adr/).

## What it will be

One Orchestrator per Topic (a rule set with its own account). It seats agents, answers their Escalations, and passes on only what is yours. You see those items in an Attention pane inside herdr, with the recommended answer first.

- **herdr plugin** `jhonsanchez.orchestrator`: the Attention pane, the Watch runner, the heartbeat, and the `start`, `init` and seat actions.
- **Claude Code plugin**: the Orchestrator skill and the hooks that route a Managed agent's questions and permission prompts to its Inbox.
- **Topics** are folders in `$(herdr plugin config-dir jhonsanchez.orchestrator)/topics/<name>/`:
  - `topic.toml`: only what differs from the defaults (membership patterns, env exports, model, base branch, `notify_command`);
  - `playbook.md`, with a required "Yours alone" section;
  - `brief.md`;
  - `watches/`: any executable in it is picked up.

Linux and macOS.
