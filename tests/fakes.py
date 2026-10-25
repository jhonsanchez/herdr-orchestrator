from __future__ import annotations

from herdr_orch.herdr import Agent, Workspace


class FakeHerdr:
    """Records every call; agents and pane contents are set by the test."""

    def __init__(self) -> None:
        self.agent_list: list[Agent] = []
        self.panes: dict[str, str] = {}
        self.workspace_list: list[Workspace] = []
        self.calls: list[tuple[object, ...]] = []
        self._n = 0
        self.ready_after_start = True
        self.becomes_ready = True
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
        return self._pane()

    def pane_split(self, pane_id: str, cwd: str) -> str:
        self.calls.append(("pane_split", pane_id, cwd))
        return self._pane()

    def pane_run(self, pane_id: str, command: str) -> None:
        self.calls.append(("pane_run", pane_id, command))

    def agent_start(self, name: str, pane_id: str, args: list[str]) -> bool:
        self.calls.append(("agent_start", name, pane_id, tuple(args)))
        return self.ready_after_start

    def notification(self, title: str, body: str) -> None:
        self.calls.append(("notification", title, body))

    def open_setup(self, repo_root: str) -> None:
        self.calls.append(("open_setup", repo_root))

    def focus(self, target: str) -> None:
        self.calls.append(("focus", target))

    def plugin_config_dir(self) -> str | None:
        return None

    def wait_ready(self, name: str, timeout_ms: int) -> bool:
        self.calls.append(("wait_ready", name))
        return self.becomes_ready

    def of(self, kind: str) -> list[tuple[object, ...]]:
        return [c for c in self.calls if c[0] == kind]
