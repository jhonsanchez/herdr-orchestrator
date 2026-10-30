from __future__ import annotations

from herdr_orch.herdr import Agent, AgentBlocked, AgentBusy, Workspace


class FakeHerdr:
    """Records every call; agents and pane contents are set by the test."""

    def __init__(self) -> None:
        self.agent_list: list[Agent] = []
        self.panes: dict[str, str] = {}
        self.gone_workspaces: set[str] = set()  # workspaces herdr answers workspace_not_found for
        self.workspace_list: list[Workspace] = []
        self.calls: list[tuple[object, ...]] = []
        self.kinds: dict[str, str] = {}  # agent name -> kind it was started as
        self._n = 0
        self.ready_after_start = True
        self.attention: set[str] = set()
        self.becomes_ready = True
        self.prompt_error: str | None = None  # when set, prompt() fails with it
        self.blocked_agents: set[str] = set()  # agents herdr refuses prompts to (agent_blocked)
        self.busy_agents: set[str] = set()  # agents whose input box has the person's text in it
        self.screens: dict[str, list[str]] = {}  # pane -> visible screens in turn; the last one stays

    def _pane(self) -> str:
        self._n += 1
        return f"w9:p{self._n}"

    def agents(self) -> list[Agent]:
        return list(self.agent_list)

    def read_pane(self, pane_id: str, lines: int) -> str:
        return self.panes.get(pane_id, "")

    def read_screen(self, pane_id: str) -> str:
        self.calls.append(("read_screen", pane_id))
        shown = self.screens.get(pane_id, [""])
        return shown.pop(0) if len(shown) > 1 else shown[0]

    def prompt(self, agent: str, text: str) -> None:
        if agent in self.blocked_agents:
            raise AgentBlocked(f"{agent} is blocked")
        if agent in self.busy_agents:
            raise AgentBusy(f"{agent} has text in its input box")
        if self.prompt_error:
            raise RuntimeError(self.prompt_error)
        self.calls.append(("prompt", agent, text))

    def workspaces(self) -> list[Workspace]:
        return list(self.workspace_list)

    def workspace_create(self, cwd: str, label: str) -> tuple[str, str]:
        pane = self._pane()
        ws = Workspace(f"w{len(self.workspace_list) + 50}", label, None)
        self.workspace_list.append(ws)
        self.calls.append(("workspace_create", cwd, label))
        return ws.workspace_id, pane

    def worktree_create(self, workspace_id: str, branch: str, label: str, path: str, base: str | None) -> str:
        self.calls.append(("worktree_create", workspace_id, branch, label, path, base))
        self.workspace_list.append(Workspace(f"wt{len(self.of('worktree_create'))}", label, None, path))
        return self._pane()

    def worktree_remove(self, workspace_id: str) -> bool:
        self.calls.append(("worktree_remove", workspace_id))
        self.workspace_list = [w for w in self.workspace_list if w.workspace_id != workspace_id]
        return workspace_id not in self.gone_workspaces

    def workspace_close(self, workspace_id: str) -> None:
        self.calls.append(("workspace_close", workspace_id))
        self.workspace_list = [w for w in self.workspace_list if w.workspace_id != workspace_id]
        self.agent_list = [a for a in self.agent_list if a.workspace_id != workspace_id]

    def pane_split(self, pane_id: str, cwd: str) -> str:
        self.calls.append(("pane_split", pane_id, cwd))
        return self._pane()

    def pane_run(self, pane_id: str, command: str) -> None:
        self.calls.append(("pane_run", pane_id, command))

    def agent_start(self, name: str, pane_id: str, args: list[str], kind: str = "claude") -> bool:
        self.calls.append(("agent_start", name, pane_id, tuple(args)))
        self.kinds[name] = kind
        return self.ready_after_start

    def notification(self, title: str, body: str) -> None:
        self.calls.append(("notification", title, body))

    def open_setup(self, repo_root: str) -> None:
        self.calls.append(("open_setup", repo_root))

    def focus(self, target: str) -> None:
        self.calls.append(("focus", target))

    def has_attention(self, workspace_id: str) -> bool:
        return workspace_id in self.attention

    def attention_open(self, workspace_id: str, target_pane: str, topic: str | None = None) -> None:
        self.calls.append(("attention_open", workspace_id, target_pane, topic))
        self.attention.add(workspace_id)

    def plugin_config_dir(self) -> str | None:
        return None

    def wait_ready(self, name: str, timeout_ms: int) -> bool:
        self.calls.append(("wait_ready", name))
        return self.becomes_ready

    def of(self, kind: str) -> list[tuple[object, ...]]:
        return [c for c in self.calls if c[0] == kind]
