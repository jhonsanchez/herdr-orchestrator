# Context

Glossary for herdr-orchestrator: how seated coding agents keep working while their user is away, and how what needs the user reaches them. Terms are defined once here; the code, tickets and docs use them as written.

## Terms

- **Topic** — one rule set with its own account and credentials (for example a client, an employer, a personal project group). Agents, briefs and credentials never cross Topics.
- **Orchestrator** — the agent that coordinates the seated agents of one Topic: seats them, answers their Escalations, and passes on only what is the user's. One per Topic.
- **Playbook** — what a Topic adds to the generic Orchestrator: its brief, its Watches, its release steps, and — in its required "Yours alone" section — what its Orchestrator may never decide alone.
- **Seating** — giving one piece of work its own worktree with an agent started in it, briefed by its Topic's brief.
- **Membership** — which Topic an agent belongs to, decided by the Topic's repo-root patterns. An agent no Topic matches is shown to the user but never managed.
- **Managed agent** — an agent its Orchestrator seated, or one handed over to it. Its questions and permission prompts become Escalations instead of reaching the user directly.
- **Registry** — the record of which agents of a Topic are Managed. Taking an agent adds it; giving it back removes it.
- **Handover** — the user passing one agent they seated to its Topic's Orchestrator, by name, until they take it back. Every agent that is not managed asks the user directly, as it always did.
- **Escalation** — a Managed agent saying it needs a decision, a permission or a human. It goes to its Orchestrator first; only what the Playbook marks as the user's goes on to the user, with a recommended answer.
- **Inbox** — the open Escalations of one Topic, owned by its Orchestrator.
- **Attention pane** — the user's one view of every Topic's Inbox items that are theirs: grouped by workspace and agent, each with its options, the recommended one first. An answer given there goes straight to the agent.
- **Watch** — a long-running check a Topic adds (an issue tracker, a merge gate, a budget). Every line it reports is an event for the Orchestrator, which alone decides what becomes an Escalation. The core keeps every Watch running, from a background Watch runner with no pane of its own.
- **Event** — one line a Watch reports. Events go to the Orchestrator, never straight to the user.
- **Heartbeat** — the Orchestrator's regular sign that it is still answering.
- **Unattended** — a started Topic whose Orchestrator has stopped answering: it is gone, or Events have waited a whole window with no Heartbeat since they arrived. A quiet Topic is never Unattended just because nothing happened. Shown in the Attention pane and sent to the user once.
- **Follow-up** — what the Orchestrator owes a Managed agent that finished (`done`), went `gone`, or sat idle-stale without a PR, a report or an Inbox item. It is derived from the Events and stays open in the Attention pane (and, once a window has passed, as a banner and one alert) until an Inbox item for that agent is written, the agent works again, or it is given back. A chat message closes nothing.
