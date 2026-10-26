# Inbox format, v2 (v1 plus `to`)

Each Topic's Inbox is `topics/<topic>/inbox.jsonl` under the state root (`~/.local/state/herdr/plugins/jhonsanchez.orchestrator`, or `HERDR_ORCH_STATE`). It holds one JSON object per line. Writers only append. A change to an item is recorded by appending the item again with the same `id`, and the last line for an `id` is its current state.

| Field | Type | Meaning |
|---|---|---|
| `v` | `1` \| `2` | Format version. A breaking change bumps it. Readers treat a missing `to` (every v1 line) as `agent`. |
| `to` | `agent` \| `orchestrator` | v2. Who an answer is for. `agent`: the asking agent, and the answer is typed into its pane. `orchestrator`: a decision the Orchestrator put to the user, and the answer is sent to the Topic's Orchestrator as a prompt naming the item `id`; the subject agent need not be alive and is never prompted. |
| `id` | string | Unique per item. |
| `topic` | string | The Topic whose Inbox this is. |
| `agent` | string | The herdr agent name that escalated; for `to: orchestrator`, the agent the decision is about. |
| `pane` | string | That agent's herdr pane id, which is the handle for answering it or opening its pane. |
| `kind` | `decision` \| `permission` \| `question` | Which it is: an AskUserQuestion, a permission prompt, or a turn that ended in a question. |
| `question` | string | What is asked, in the agent's words. |
| `options` | string[] | Choices with the recommended one first. The hooks move a `(Recommended)` label to the front. Empty when the agent gave none. Always empty for `permission`: the answer is given in the agent's own pane. |
| `guess` | bool | `true` when the item was inferred (a turn ending in `?`), so a false positive is easy to dismiss. |
| `status` | `open` \| `resolved` \| `dismissed` | Writers create items `open`. |
| `answer` | string \| null | The answer that was sent, once there is one. |
| `created_at` | number | Unix time in seconds. |

Writers today: the routing hooks (`herdr_orch/hooks.py`) and the Orchestrator, through `herdr-orch inbox add --topic T --agent A --question Q --option O...` (the recommended option first). The Orchestrator and the Attention pane (#2) append resolutions.

An answer resolves its item only once delivered. A delivery failure leaves the item `open`, shows the error in the Attention pane, and the item can be dismissed or its pane opened. `herdr-orch inbox add` writes `to: orchestrator`; the hooks write `to: agent`.
