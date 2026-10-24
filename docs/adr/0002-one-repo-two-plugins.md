# One repo ships two plugins: a herdr plugin and a Claude Code plugin

The Attention pane, the Watch runner, the heartbeat and the seat/start/init actions need herdr (plugin panes, actions, startup commands); the routing hooks and the Orchestrator skill need Claude Code. A herdr-only package would have to write hooks into each user's `settings.json`, which users who manage that file (with chezmoi or by hand) would see reverted or would have to mirror. We therefore ship both from one repo, versioned together, so the Inbox format they share never drifts between two releases.

## Consequences

- Installing takes two commands (`herdr plugin install` and `claude plugin install`); the README states both.
- The Inbox format (JSONL, versioned) is the contract between the two halves and with future agent adapters (Copilot, Codex); a breaking change bumps its version.
- Linux and macOS only at first: the pane uses Python's stdlib curses and the hooks are POSIX shell.
