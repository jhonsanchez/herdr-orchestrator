# GitHub is an opt-in Integration, not assumed

The plugin grew inside one user's Topics, where every repo was on GitHub, so the Watch runner always followed PRs, kept the branch ledger and gated `release` and `validated` on a merged PR, all through that user's own `gh` login. A Topic whose repos live elsewhere, or that never opens PRs, got errors and Events about PRs that cannot exist. We therefore make GitHub an Integration a Topic opts into with a `[github]` table in `topic.toml`: with no table, the core seats agents, routes Escalations and ends a branch on the agent's evidence alone. We chose explicit opt-in over detecting a github.com remote, because a detected default silently starts polling (and spending rate limit) on a remote the user never meant to watch, and over an opt-out flag, because the person installing the plugin should not have to know about GitHub to have a working Topic.

## Consequences

- PR following, the ledger, the deploy gate and the `--pr` form of `validated` belong to the GitHub Integration; without it, `validated` needs only `--evidence` and the Orchestrator runs `release`.
- `deploy_check` needs a merge to wait for, so `start` refuses a Topic that sets it without `[github]`.
- The Orchestrator's start prompt and the deploy prompt are a core text plus a GitHub paragraph; how to merge is the Topic's playbook's to say, not the plugin's.
- Identity is the Integration's too: optional `read_token_cmd` and `write_token_cmd`, falling back to the user's `gh` login, so it works for anyone and lets a Topic keep machine traffic off a person's rate limit.
- The setup wizard offers GitHub, pre-ticked, only when the repo's origin is on github.com.
- Existing Topics that relied on the old default need `[github]` added by hand; there is no migration code.
