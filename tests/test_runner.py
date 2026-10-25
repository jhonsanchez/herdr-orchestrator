import tempfile
import unittest
from pathlib import Path

from herdr_orch.herdr import Agent
from herdr_orch.runner import Runner
from herdr_orch.state import TopicState
from herdr_orch.topics import Topic
from tests.fakes import FakeHerdr

MIN = 60.0


class FakeWatches:
    def __init__(self) -> None:
        self.pending: list[tuple[str, str]] = []

    def poll(self, now: float) -> None:
        pass

    def lines(self) -> list[tuple[str, str]]:
        out, self.pending = self.pending, []
        return out


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.topic = Topic(name="waki", dir=root / "cfg", heartbeat_window_min=15)
        self.state = TopicState(root, "waki")
        self.herdr = FakeHerdr()
        self.watches = FakeWatches()
        self.notes: list[tuple[str, str]] = []
        self.runner = Runner(self.topic, self.state, self.herdr, self.watches,
                             notify=lambda t, b: self.notes.append((t, b)))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def agents(self, orch: str | None = "idle", **managed: str) -> None:
        self.herdr.agent_list = [Agent(n, s, f"w1:{n}", "w1") for n, s in managed.items()]
        if orch:
            self.herdr.agent_list.append(Agent("orch-waki", orch, "w2:p1", "w2"))

    def texts(self) -> list[str]:
        return [e.text for e in self.state.events_since(0)]

    def test_a_managed_agent_turning_blocked_becomes_an_event(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.agents(orch="working", **{"waki-1": "working"})
        self.runner.tick(now=0)
        self.agents(orch="working", **{"waki-1": "blocked"})
        self.runner.tick(now=10)
        self.assertEqual(self.texts(), ["waki-1 blocked"])

    def test_watch_lines_become_events(self) -> None:
        self.agents(orch="working")
        self.watches.pending = [("jira", "WAKI-7 Done")]
        self.runner.tick(now=5)
        self.assertEqual([(e.source, e.text) for e in self.state.events_since(0)], [("jira", "WAKI-7 Done")])

    def test_events_reach_an_idle_orchestrator_once_and_wait_while_it_works(self) -> None:
        self.agents(orch="working")
        self.watches.pending = [("jira", "WAKI-7 Done")]
        self.runner.tick(now=5)
        self.assertEqual(self.herdr.of("prompt"), [])
        self.agents(orch="idle")
        self.runner.tick(now=10)
        [prompt] = self.herdr.of("prompt")
        self.assertEqual(prompt[1], "orch-waki")
        self.assertIn("jira: WAKI-7 Done", str(prompt[2]))
        self.runner.tick(now=20)
        self.assertEqual(len(self.herdr.of("prompt")), 1)

    def test_a_restarted_runner_redelivers_what_was_never_answered(self) -> None:
        self.state.append_event("jira", "answered", now=100)
        self.state.touch_heartbeat(now=150)
        self.state.append_event("jira", "logged before a crash", now=200)
        runner = Runner(self.topic, self.state, self.herdr, self.watches, notify=lambda t, b: None)
        self.agents(orch="idle")
        runner.tick(now=300)
        [prompt] = self.herdr.of("prompt")
        self.assertIn("logged before a crash", str(prompt[2]))
        self.assertNotIn("answered\n", str(prompt[2]) + "\n")

    def test_the_turn_after_a_delivery_records_a_heartbeat(self) -> None:
        self.agents(orch="idle")
        self.watches.pending = [("jira", "x")]
        self.runner.tick(now=10)
        self.assertIsNone(self.state.heartbeat_at())
        self.agents(orch="done")
        self.runner.tick(now=40)
        self.assertEqual(self.state.heartbeat_at(), 40)

    def test_a_missing_orchestrator_notifies_once_until_it_returns(self) -> None:
        self.agents(orch=None)
        self.runner.tick(now=0)
        self.runner.tick(now=10)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("waki", self.notes[0][0])
        self.agents(orch="idle")
        self.runner.tick(now=20)
        self.agents(orch=None)
        self.runner.tick(now=30)
        self.assertEqual(len(self.notes), 2)

    def test_events_waiting_a_whole_window_without_heartbeat_notify(self) -> None:
        self.agents(orch="blocked")
        self.watches.pending = [("jira", "x")]
        self.runner.tick(now=0)
        self.runner.tick(now=14 * MIN)
        self.assertEqual(self.notes, [])
        self.runner.tick(now=15 * MIN)
        self.assertEqual(len(self.notes), 1)

    def test_a_silent_managed_agent_is_reported_stale(self) -> None:
        self.state.take("w1:waki-1", "waki-1")
        self.agents(orch="working", **{"waki-1": "idle"})
        self.herdr.panes["w1:waki-1"] = "same screen"
        self.runner.tick(now=0)
        self.runner.tick(now=40 * MIN)
        self.assertEqual(self.texts(), ["waki-1 stale (idle) 40m"])


if __name__ == "__main__":
    unittest.main()
