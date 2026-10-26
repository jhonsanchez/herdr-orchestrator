# Inbox format, v1

Each Topic's Inbox is `topics/<topic>/inbox.jsonl` under the state root (`~/.local/state/herdr/plugins/jhonsanchez.orchestrator`, or `HERDR_ORCH_STATE`). It holds one JSON object per line. Writers only append. A change to an item is recorded by appending the item again with the same `id`, and the last line for an `id` is its current state.

| Field | Type | Meaning |
|---|---|---|
| `v` | `1` | Format version. A breaking change bumps it. |
| `id` | string | Unique per item. |
| `topic` | string | The Topic whose Inbox this is. |
| `agent` | string | The herdr agent name that escalated. |
| `pane` | string | That agent's herdr pane id, which is the handle for answering it or opening its pane. |
| `kind` | `decision` \| `permission` \| `question` | Which it is: an AskUserQuestion, a permission prompt, or a turn that ended in a question. |
| `question` | string | What is asked, in the agent's words. |
| `options` | string[] | Choices with the recommended one first. The hooks move a `(Recommended)` label to the front. Empty when the agent gave none. Always empty for `permission`: the answer is given in the agent's own pane. |
| `guess` | bool | `true` when the item was inferred (a turn ending in `?`), so a false positive is easy to dismiss. |
| `status` | `open` \| `resolved` \| `dismissed` | Writers create items `open`. |
| `answer` | string \| null | The answer that was sent, once there is one. |
| `created_at` | number | Unix time in seconds. |

Writers today: the routing hooks (`herdr_orch/hooks.py`) and the Orchestrator, through `herdr-orch inbox add --topic T --agent A --question Q --option O...` (the recommended option first). The Orchestrator and the Attention pane (#2) append resolutions.
