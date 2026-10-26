import json
import tempfile
import unittest
from pathlib import Path

from herdr_orch.attention import Row, answer, dismiss, load, open_pane
from herdr_orch.herdr import Agent, Workspace
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr


class AttentionActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = TopicState(self.root / "state", "waki")
        self.state.take("w1:p1", "waki-1")
        self.state.escalate("waki-1", "w1:p1", "decision", "Which color?", ["Red (Recommended)", "Blue"], now=100)
        self.herdr = FakeHerdr()
        self.herdr.agent_list = [Agent("waki-1", "idle", "w1:p1", "w1"), Agent("orch-waki", "idle", "w2:p1", "w2")]
        self.herdr.workspace_list = [Workspace("w1", "1-logo", None)]
        self.topics = [Topic(name="waki", dir=self.root / "cfg/topics/waki")]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def lines(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.state.inbox.read_text().splitlines()]

    def test_load_builds_the_view_from_every_topic(self) -> None:
        view = load(self.root / "state", self.topics, self.herdr, now=200)
        [row] = view.rows()
        self.assertEqual((row.agent, row.text, row.topic), ("waki-1", "Which color?", "waki"))
        self.assertEqual(view.banners, [])

    def test_answering_prompts_the_agent_and_resolves_the_item(self) -> None:
        [row] = load(self.root / "state", self.topics, self.herdr, now=200).rows()
        answer(self.root / "state", row, "Red (Recommended)", self.herdr)
        [(_, target, text)] = self.herdr.of("prompt")
        self.assertEqual(target, "w1:p1")
        self.assertIn("Which color?", str(text))
        self.assertIn("Red (Recommended)", str(text))
        last = self.lines()[-1]
        self.assertEqual((last["status"], last["answer"], last["question"]), ("resolved", "Red (Recommended)", "Which color?"))
        self.assertEqual(load(self.root / "state", self.topics, self.herdr, now=300).rows(), [])

    def test_an_answer_is_refused_when_another_agent_now_holds_the_pane(self) -> None:
        [row] = load(self.root / "state", self.topics, self.herdr, now=200).rows()
        self.herdr.agent_list = [Agent("someone-else", "idle", "w1:p1", "w1")]
        with self.assertRaises(ValueError):
            answer(self.root / "state", row, "Red", self.herdr)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.assertEqual(self.lines()[-1]["status"], "open")

    def decision(self) -> Row:
        self.state.escalate("waki-1", "w1:p1", "decision", "Merge PR #20?", ["Merge (Recommended)", "Hold"],
                            now=150, to="orchestrator")
        return next(r for r in load(self.root / "state", self.topics, self.herdr, now=200).rows() if r.to == "orchestrator")

    def test_an_orchestrator_decision_reaches_the_orchestrator_when_the_agent_is_gone(self) -> None:
        row = self.decision()
        self.herdr.agent_list = [Agent("orch-waki", "idle", "w2:p1", "w2")]
        answer(self.root / "state", row, "Merge (Recommended)", self.herdr, "orch-waki")
        [(_, target, text)] = self.herdr.of("prompt")
        self.assertEqual(target, "orch-waki")
        self.assertIn(str(row.item_id), str(text))
        self.assertIn("Merge (Recommended)", str(text))
        self.assertEqual(self.lines()[-1]["status"], "resolved")
        self.assertEqual([r.to for r in load(self.root / "state", self.topics, self.herdr, now=300).rows()], ["agent"])

    def test_an_orchestrator_decision_types_nothing_into_an_idle_subject(self) -> None:
        row = self.decision()
        answer(self.root / "state", row, "Hold", self.herdr, "orch-waki")
        self.assertEqual([c[1] for c in self.herdr.of("prompt")], ["orch-waki"])

    def test_a_failed_delivery_raises_and_keeps_the_item_open(self) -> None:
        row = self.decision()
        self.herdr.prompt_error = "orch-waki never took the prompt"
        with self.assertRaisesRegex(RuntimeError, "never took"):
            answer(self.root / "state", row, "Merge", self.herdr, "orch-waki")
        self.assertEqual(self.lines()[-1]["status"], "open")

    def test_an_orchestrator_decision_without_an_orchestrator_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            answer(self.root / "state", self.decision(), "Merge", self.herdr, None)
        self.assertEqual(self.herdr.of("prompt"), [])

    def test_skipping_dismisses_without_prompting(self) -> None:
        [row] = load(self.root / "state", self.topics, self.herdr, now=200).rows()
        dismiss(self.root / "state", row)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.assertEqual(self.lines()[-1]["status"], "dismissed")

    def test_open_focuses_the_agents_pane(self) -> None:
        [row] = load(self.root / "state", self.topics, self.herdr, now=200).rows()
        open_pane(row, self.herdr)
        self.assertEqual(self.herdr.of("focus"), [("focus", "w1:p1")])


if __name__ == "__main__":
    unittest.main()
