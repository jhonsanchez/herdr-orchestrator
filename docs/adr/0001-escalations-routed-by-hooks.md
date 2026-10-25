# Escalations are routed by hooks and a handover registry, not by the brief

Seated agents told by their brief to "escalate, never wait on the user" still waited: a user's own rules may tell every session to ask through AskUserQuestion, Claude's default is to end a turn with a question, a long brief fades after compaction, and a permission prompt blocks before the model can act at all. We therefore route a Managed agent's questions with Claude Code hooks (`PreToolUse` on AskUserQuestion, `PermissionRequest`, `Stop`) that write them to its Topic's Inbox and refuse the dialog. Whether an agent is managed is read on every call from a registry keyed by its Herdr pane id, not from an environment variable, because a variable is fixed at process start and an agent the user seated must be handed over and taken back while it runs.

## Consequences

- A Managed agent never shows a dialog: its AskUserQuestion is refused and becomes an Inbox item. An agent the user is grilling must not be handed over until the grilling is done.
- The hooks run in every Claude session that has the plugin enabled; each exits without effect for a pane that is not in the registry.
- A turn that ends with a plain-text question becomes an Inbox item marked as a guess, so a false positive is easy to dismiss.
- Managed agents should not trigger the user's own per-turn notifications; only the Orchestrator and the Unattended alert reach the user.
- Validated by the #4 spike (Claude Code 2.1.310, auto mode): with the deny reason below, the agent took both refused dialogs (one question, then a three-question grilling round) as escalated. It never retried the dialog and never re-asked in prose. It stated its recommendation, kept to work that didn't depend on the answer, and ended its turn. An answer sent later as a prompt resumed the work. Reason used: "Escalated to the orchestrator: your question was recorded in its inbox. Do not ask it again, by dialog or in text. Keep working on anything that does not depend on the answer, then end your turn; the answer will arrive as a prompt."
