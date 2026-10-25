import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from herdr_orch.cli import main
from herdr_orch.herdr import Agent, Workspace
from herdr_orch.state import TopicState
from tests.fakes import FakeHerdr


class HandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = {"HERDR_ORCH_CONFIG": str(root / "cfg"), "HERDR_ORCH_STATE": str(root / "state"), "HOME": "/home/u"}
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("falcon-9", "idle", "w4:p1", "w4"), Agent("loose", "idle", "w5:p1", "w5")]
        self.herdr.workspace_list = [
            Workspace("w4", "9-fix", "/home/u/projects/waki/waki-platform"),
            Workspace("w5", "scratch", "/home/u/projects/personal/x"),
        ]
        self.run_cli(["init", "waki"])
        (root / "cfg/topics/waki/topic.toml").write_text('match = ["~/projects/waki/*"]\n')
        self.state = TopicState(root / "state", "waki")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, argv: list[str], env: dict[str, str] | None = None) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(argv, env or self.env, adapter=self.herdr)

    def test_take_registers_the_agent_under_its_topic_and_briefs_it(self) -> None:
        self.assertEqual(self.run_cli(["take", "falcon-9"]), 0)
        self.assertEqual(self.state.managed(), {"w4:p1": "falcon-9"})
        [prompt] = self.herdr.of("prompt")
        self.assertEqual(prompt[1], "falcon-9")
        self.assertIn("brief.md", str(prompt[2]))

    def test_give_back_removes_it_and_tells_it(self) -> None:
        self.run_cli(["take", "falcon-9"])
        self.assertEqual(self.run_cli(["give-back", "falcon-9"]), 0)
        self.assertEqual(self.state.managed(), {})
        self.assertEqual(len(self.herdr.of("prompt")), 2)

    def test_an_agent_no_topic_matches_cannot_be_taken(self) -> None:
        self.assertEqual(self.run_cli(["take", "loose"]), 1)
        self.assertEqual(self.run_cli(["take", "nobody"]), 1)
        self.assertEqual(self.herdr.of("prompt"), [])

    def test_is_managed_answers_for_the_calling_pane(self) -> None:
        self.run_cli(["take", "falcon-9"])
        self.assertEqual(self.run_cli(["is-managed"], {**self.env, "HERDR_PANE_ID": "w4:p1"}), 0)
        self.assertEqual(self.run_cli(["is-managed"], {**self.env, "HERDR_PANE_ID": "w5:p1"}), 1)
        self.assertEqual(self.run_cli(["is-managed"], self.env), 1)


if __name__ == "__main__":
    unittest.main()
