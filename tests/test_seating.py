import tempfile
import unittest
from unittest import mock
from pathlib import Path

from herdr_orch.herdr import Agent, Workspace
from herdr_orch import seating
from herdr_orch.seating import agent_name, seat, start, startup_screen
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr


class AgentNameTest(unittest.TestCase):
    def test_branch_becomes_a_valid_herdr_agent_name(self) -> None:
        self.assertEqual(agent_name("WAKI-1412"), "waki-1412")
        self.assertEqual(agent_name("feat/Add Sync"), "feat-add-sync")
        self.assertEqual(agent_name("123-fix"), "a-123-fix")
        self.assertEqual(len(agent_name("x" * 50)), 32)


MCP_SCREEN = """ 2 new MCP servers found in this project
  Select any you wish to enable.
  MCP servers may execute code or access system resources. All tool calls require approval.
  ❯ [✔] ms365
    [✔] trilium
       Enable selected
 Space to select · Esc to reject all"""
TRUST_SCREEN = """ Quick safety check: Is this a project you created or one you trust? (Like your own code)
 ❯ No, exit
   Yes, I trust this folder"""
PROMPT = """ ☀  Clear  0% of context
❯ Try "how does <filepath> work?"
  ⏵⏵ auto mode on (shift+tab to cycle)"""


class StartupScreenTest(unittest.TestCase):
    def test_recognises_claudes_startup_screens(self) -> None:
        self.assertEqual(startup_screen(MCP_SCREEN), "enable this project's MCP servers")
        self.assertEqual(startup_screen(MCP_SCREEN.replace("2 new MCP servers", "1 new MCP server")), "enable this project's MCP servers")
        self.assertEqual(startup_screen(TRUST_SCREEN), "trust this folder")
        self.assertIsNone(startup_screen(PROMPT))


@mock.patch.object(seating, "POLL_SECONDS", 0)
class SeatingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(
            name="zebra", dir=root / "cfg/topics/zebra", model="sonnet", base="origin/develop",
            env={"CLAUDE_CONFIG_DIR": "~/.claude-zebra"},
        )
        self.state = TopicState(root / "state", "zebra")
        self.herdr = FakeHerdr()
        self.herdr.workspace_list = [Workspace("w3", "zpc", "/home/u/projects/zebra/zpc")]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seat_creates_worktree_exports_env_starts_registers_and_briefs(self) -> None:
        r = seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u")
        self.assertEqual(self.herdr.of("worktree_create"), [
            ("worktree_create", "w3", "FALCON-12", "FALCON-12", "/home/u/.herdr/worktrees/zpc/FALCON-12", "origin/develop"),
        ])
        self.assertEqual(self.herdr.of("pane_run"), [("pane_run", r.pane_id, "export CLAUDE_CONFIG_DIR=/home/u/.claude-zebra")])
        self.assertEqual(self.herdr.of("agent_start"), [("agent_start", "falcon-12", r.pane_id, ("--model", "sonnet"))])
        self.assertEqual(self.state.managed(), {r.pane_id: "falcon-12"})
        [prompt] = self.herdr.of("prompt")
        self.assertEqual(prompt[1], "falcon-12")
        self.assertIn(str(self.topic.brief_path), str(prompt[2]))

    def write_playbook(self, text: str = "# Playbook\n\n## Yours alone\n\n- Production.\n") -> None:
        self.topic.dir.mkdir(parents=True, exist_ok=True)
        self.topic.playbook_path.write_text(text)

    def test_start_opens_workspace_runner_pane_and_orchestrator(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/opt/plug/bin/herdr-orch")
        [(_, cwd, label)] = self.herdr.of("workspace_create")
        self.assertEqual(label, "orch-zebra")
        [(_, runner_pane, cmd)] = self.herdr.of("pane_run")
        self.assertEqual(
            cmd,
            f"HERDR_ORCH_CONFIG={self.topic.dir.parent.parent} HERDR_ORCH_STATE={self.state.dir.parent.parent} "
            "/opt/plug/bin/herdr-orch watch zebra",
        )
        [(_, name, pane, args)] = self.herdr.of("agent_start")
        self.assertEqual(name, "orch-zebra")
        self.assertNotEqual(pane, runner_pane)
        [prompt] = self.herdr.of("prompt")
        self.assertIn(str(self.topic.playbook_path), str(prompt[2]))
        self.assertIn(f"HERDR_ORCH_STATE={self.state.dir.parent.parent} /opt/plug/bin/herdr-orch heartbeat zebra", str(prompt[2]))

    def test_the_runner_starts_only_after_the_orchestrator_exists(self) -> None:
        self.write_playbook()
        start(self.topic, self.state, self.herdr, cli="/x")
        kinds = [c[0] for c in self.herdr.calls]
        self.assertLess(kinds.index("agent_start"), kinds.index("pane_run"))

    def test_start_refuses_a_playbook_without_yours_alone(self) -> None:
        self.write_playbook("# Playbook\n\nAnything goes.\n")
        with self.assertRaises(ValueError):
            start(self.topic, self.state, self.herdr, cli="/x")
        self.assertEqual(self.herdr.calls, [])

    def test_start_refuses_a_leftover_workspace_without_its_orchestrator(self) -> None:
        self.write_playbook()
        self.herdr.workspace_list.append(Workspace("w7", "orch-zebra", None))
        with self.assertRaises(RuntimeError):
            start(self.topic, self.state, self.herdr, cli="/x")
        self.assertEqual(self.herdr.of("workspace_create"), [])

    def test_seat_on_an_unknown_workspace_says_so(self) -> None:
        with self.assertRaisesRegex(ValueError, "w404"):
            seat(self.topic, self.state, self.herdr, workspace_id="w404", branch="x", home="/home/u")

    def test_start_waits_for_a_startup_dialog_before_briefing(self) -> None:
        self.write_playbook()
        self.herdr.ready_after_start = False
        said: list[str] = []
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", say=said.append))
        self.assertEqual(self.herdr.of("wait_ready"), [("wait_ready", "orch-zebra")])
        self.assertIn("orch-zebra", said[0])
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_start_gives_up_without_briefing_when_the_dialog_is_never_answered(self) -> None:
        self.write_playbook()
        self.herdr.ready_after_start = False
        self.herdr.becomes_ready = False
        self.assertFalse(start(self.topic, self.state, self.herdr, cli="/x", say=lambda m: None))
        self.assertEqual(self.herdr.of("prompt"), [])

    def test_seat_waits_for_a_startup_dialog_too(self) -> None:
        self.herdr.ready_after_start = False
        seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=lambda m: None)
        self.assertEqual(self.herdr.of("wait_ready"), [("wait_ready", "falcon-12")])
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_seat_waits_out_the_mcp_screen_before_briefing(self) -> None:
        self.herdr.screens["w9:p1"] = [MCP_SCREEN, MCP_SCREEN, PROMPT]
        said: list[str] = []
        seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=said.append)
        self.assertEqual(said, ["falcon-12 is waiting at a startup screen (enable this project's MCP servers): answer it in its pane."])
        self.assertEqual(len(self.herdr.of("read_screen")), 3)
        kinds = [c[0] for c in self.herdr.calls]
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "read_screen"), kinds.index("prompt"))

    def test_seat_gives_up_when_the_screen_never_clears(self) -> None:
        self.herdr.screens["w9:p1"] = [MCP_SCREEN]
        with mock.patch.object(seating, "DIALOG_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(TimeoutError):
                seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=lambda m: None)
        self.assertEqual(self.herdr.of("prompt"), [])

    def test_start_waits_out_the_mcp_screen_too(self) -> None:
        self.write_playbook()
        self.herdr.screens["w9:p1"] = [MCP_SCREEN, PROMPT]
        self.assertTrue(start(self.topic, self.state, self.herdr, cli="/x", say=lambda m: None))
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_a_clean_start_reads_the_screen_once_and_briefs(self) -> None:
        seat(self.topic, self.state, self.herdr, workspace_id="w3", branch="FALCON-12", home="/home/u", say=lambda m: self.fail(m))
        self.assertEqual(len(self.herdr.of("read_screen")), 1)
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_start_does_nothing_when_the_orchestrator_already_runs(self) -> None:
        self.herdr.agent_list = [Agent("orch-zebra", "idle", "w5:p1", "w5")]
        start(self.topic, self.state, self.herdr, cli="/opt/plug/bin/herdr-orch")
        self.assertEqual(self.herdr.calls, [])


if __name__ == "__main__":
    unittest.main()
